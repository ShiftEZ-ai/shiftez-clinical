import json
import re
from collections import defaultdict
from functools import cache
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from pydantic import Field
from sqlalchemy import JSON, Text, cast, event, inspect, literal, or_, select, union_all
from packages.contracts import schemas as s
from packages.domain.recurrence import expand
from apps.api import models as m
from apps.api.auth import _local_today, require_permission, require_role, person_access
from apps.api.errors import INTEGRITY_CONFLICT_MESSAGE, Problem, require
from apps.api.serialization import record, json_value
from .common import CATALOGUES, PROGRAMME_CATALOGUES, scoped, expected, bump_inputs, history, history_many, range_overlaps, batch_input_invalidations
from . import independence, announcements, tags


def person_inactive_from_transition(db, actor, row, active, current):
    """Normalize the dated state for catalogue writes and import Undo."""
    if active:
        return None
    if current is None and (row is None or row.active):
        return _local_today(db, actor.department_id)
    return current


def exception_identity_rows(db, actor, exceptions=None):
    """Load the identity facts used by exception approval/proposer actions once."""
    if not actor.permits('approver', 'coordinator'):
        return []
    if exceptions is None:
        creator_ids = select(m.PolicyException.created_by).where(
            m.PolicyException.department_id == actor.department_id).distinct()
        return list(db.scalars(select(m.Identity).where(
            or_(m.Identity.id == actor.identity_id, m.Identity.id.in_(creator_ids)))))
    if not exceptions:
        return []
    identities = {actor.identity_id, *(row['created_by'] for row in exceptions)}
    return list(db.scalars(select(m.Identity).where(m.Identity.id.in_(identities))))


def exception_identity_stamp(identities):
    """Stable conditional-read inputs for the identity facts used by exception actions."""
    return tuple(sorted((row.id, row.revision, row.person_id, row.disabled) for row in identities))


def exception_actions(db, actor, exceptions, policies, *, identities=None):
    """Add this reader's lifecycle eligibility to copies after the shared catalogue cache is read."""
    from .policies import effective_sources

    if not exceptions:
        return []
    approver = actor.permits('approver')
    coordinator = actor.permits('coordinator')
    waived = approver and independence.permission_for(db, actor, 'exception.approve') is not None
    # Keep these rows alive so the canonical person_key resolver uses the identity map, not one query per exception.
    _identities = exception_identity_rows(db, actor, exceptions) if identities is None else identities
    policies_by_id = {p['id']: p for p in policies}
    # Retired versions are not in the operational policies list (21e096a0d): absent counts as retired.
    retired = {r['policy_id'] for r in exceptions if r['status'] in {'proposed', 'ended'}
               and (policies_by_id.get(r['policy_id']) or {'status': 'retired'})['status'] == 'retired'}
    if approver and retired:
        policies_by_id.update(effective_sources(db, actor, list(db.scalars(select(m.Policy).where(
            m.Policy.department_id == actor.department_id, m.Policy.id.in_(retired))))))

    def can_approve(row, status='proposed'):
        policy = policies_by_id.get(row['policy_id']) or {}
        definition = policy.get('definition') or {}
        return bool(approver and row['status'] == status and policy.get('status') == 'active'
                    and policy.get('revision') == row['policy_revision'] and definition.get('exception_allowed')
                    and not s.safety_rule(definition)
                    and (waived or independence.subject_independent(db, actor, (row['person_id'],), row['created_by'])))

    def proposer(row):
        return coordinator and independence.person_key(db, row['created_by']) == independence.person_key(db, actor.identity_id)

    return [{**row, 'can_approve': can_approve(row),
             'can_end': bool(approver and row['status'] in {'approved', 'proposed'}
                             or row['status'] == 'proposed' and proposer(row)),
             'can_undo': bool(row.get('approved_by') and can_approve(row, 'ended')
                              or row['status'] == 'withdrawn' and (approver or proposer(row)))}
            for row in exceptions]


class CatalogueWrite(s.SeriesOverlapAcknowledgement):
    catalogue: str
    value: dict
    reason: s.Reason
    members: s.TagMemberChange | None = None
    owner_identity_id: None = None


def rank_code(value):
    """Explicit code, or the legacy grade suffix imported into the rank's name; never rewrite it."""
    code = value.get('short_code') or value.get('name', '').rsplit(' · ', 1)[-1]
    return code if re.fullmatch(r'[A-Z0-9]{1,6}', code) else ''


def duplicate_rank_codes(db, actor):
    grouped = {}
    for row in db.scalars(select(m.Category).where(m.Category.department_id == actor.department_id).order_by(m.Category.name, m.Category.id)):
        value = record(row)
        code = rank_code(value)
        if code:
            grouped.setdefault(code, []).append({'id': row.id, 'name': row.name,
                'short_code': value.get('short_code', ''), 'active': row.active,
                'starts_on': value['starts_on'], 'ends_on': value['ends_on'],
                'code_source': 'short_code' if value.get('short_code') else 'legacy_name'})
    return {'department_id': actor.department_id,
            'duplicates': [{'short_code': code, 'ranks': grouped[code]} for code in sorted(grouped) if len(grouped[code]) > 1]}


def programme_access(db, actor, programme_id):
    """Director writes stay in their current programmes, including the old scope of an edit or removal."""
    if actor.permits("admin", "coordinator"):
        return
    require_role(actor, "programme_lead")
    from .training import lead_programmes
    key = ("director_programmes", actor.identity_id, actor.department_id, actor.person_id)
    # One serialized command per transaction; directors cannot change their own authority-bearing memberships.
    if key not in db.info:
        db.info[key] = set(db.scalars(lead_programmes(db, actor)))
    require(programme_id is not None and programme_id in db.info[key],
        "FORBIDDEN", "Programme leads may change only records in their own programmes", 403)


def programme_write_access(db, actor, kind, value):
    programme_id = (getattr(value, "id", None) if kind == "programmes" else
                    scoped(db, m.Cohort, value.cohort_id, actor).programme_id if kind == "rotations" else
                    getattr(value, "programme_id", None))
    programme_access(db, actor, programme_id)
    if kind == "programme_memberships" and not actor.permits("admin", "coordinator"):
        require(value.person_id != actor.person_id, "FORBIDDEN", "A different coordinator must change your own programme membership", 403)


def programme_memberships(db, actor, programme_ids, person_id=None):
    query = select(m.ProgrammeMembership).where(m.ProgrammeMembership.department_id == actor.department_id,
        m.ProgrammeMembership.programme_id.in_(programme_ids))
    if person_id is not None:
        query = query.where(m.ProgrammeMembership.person_id == person_id)
    candidates = dict.fromkeys([*db.scalars(query),
        *(row for row in [*db.new, *db.dirty] if isinstance(row, m.ProgrammeMembership))])
    return [row for row in candidates if row not in db.deleted and not row.withdrawn and row.department_id == actor.department_id
            and row.programme_id in programme_ids and (person_id is None or row.person_id == person_id)]


def membership_covers(rows, start, end):
    """The union of dated memberships must cover the whole half-open interval, including adjacent rows."""
    covered = start
    for first, last in sorted(rows):
        if first > covered:
            break
        covered = max(covered, last)
    return covered >= end


def rotation_membership(db, actor, person_id, programme_id, start, end, rotation_context=None):
    """Consecutive membership records can cover a rotation; a gap cannot."""
    if rotation_context is not None:
        rows = rotation_context["memberships"].get((person_id, programme_id), ())
    else:
        rows = [(row.starts_on, row.ends_on) for row in programme_memberships(db, actor, {programme_id}, person_id)
                if row.starts_on < end and row.ends_on > start]
    require(membership_covers(rows, start, end), "ROTATION_MEMBERSHIP",
            "The trainee must belong to the cohort's programme for the whole rotation")


def validate_references(db, actor, kind, value, existing_id=None, *, rotation_context=None, series_context=None):
    if kind == 'operating_calendars':
        from .date_sets import references
        for rule in value.definition.date_set_rules:
            references(db, actor, [rule.date_set_id], rule.entry_names)
    if kind == 'leave_types':
        from sqlalchemy import func, or_
        clash = db.scalar(select(m.LeaveType.id).where(m.LeaveType.department_id == actor.department_id,
            m.LeaveType.id != (existing_id or ''),
            or_(func.lower(m.LeaveType.name) == value.name.lower(), m.LeaveType.short_code == value.short_code)).limit(1))
        require(clash is None, 'LEAVE_TYPE_TAKEN', 'A leave type already uses this name or short code', 409)
    if kind == 'programme_calendar_entries':
        scoped(db, m.Programme, value.programme_id, actor)
        # Calendar planning may precede confirmed membership; it grants no assignment or absence.
    if kind == 'mentorships':
        person_access(db, actor, value.person_id)
        person_access(db, actor, value.mentor_id)
        if value.programme_id:
            programme = scoped(db, m.Programme, value.programme_id, actor)
            require(programme.starts_on <= value.starts_on and (programme.ends_on is None
                    or value.ends_on is not None and value.ends_on <= programme.ends_on),
                    'MENTORSHIP_WINDOW', 'Mentorship must lie within the programme dates')
        for typ in value.assignment_type_ids:
            scoped(db, m.AssignmentType, typ, actor)
        for service in value.service_ids:
            scoped(db, m.Service, service, actor)
        others = db.scalars(select(m.Mentorship).where(m.Mentorship.department_id == actor.department_id,
            m.Mentorship.person_id == value.person_id, m.Mentorship.programme_id == value.programme_id))
        # Two attachments of one trainee may share dates only for different duty types or clinics.
        meets = lambda mine, theirs: not mine or not theirs or bool(set(mine) & set(theirs))
        require(not any(r.id != existing_id and range_overlaps(r.starts_on, r.ends_on, value.starts_on, value.ends_on)
                        and meets(value.assignment_type_ids, r.assignment_type_ids or []) and meets(value.service_ids, r.service_ids or [])
                        for r in others),
                'DUPLICATE_RELATIONSHIP', 'This trainee is already attached to a consultant for these dates, duty types and clinics', 409)
    if kind in {"locations", "resource_pools", "services"} and value.calendar_id:
        scoped(db, m.OperatingCalendar, value.calendar_id, actor)
    if kind == "resource_pools" and value.location_id:
        scoped(db, m.Location, value.location_id, actor)
    if kind == "locations":
        scoped(db, m.LocationType, value.location_type_id, actor)
        visited = {existing_id} if existing_id else set()
        parent = value.parent_id
        while parent:
            require(parent not in visited, "LOCATION_CYCLE", "A location cannot contain itself through its parent chain")
            visited.add(parent)
            parent = scoped(db, m.Location, parent, actor).parent_id
    if kind == "assignment_types":
        definition = value.definition
        require(definition.session_unit_basis == 'fixed', 'FIXED_SESSION_UNITS_REQUIRED',
                'Session units count per complete assignment; configure a separate weight for shorter duties')
        for demand in definition.resources:
            scoped(db, m.ResourcePool, demand.resource_pool_id, actor)
        for category_id in definition.allowed_category_ids:
            scoped(db, m.Category, category_id, actor)
        for location_type_id in definition.allowed_location_type_ids:
            scoped(db, m.LocationType, location_type_id, actor)
        for segment in definition.requirements:
            for clause in segment.capabilities:
                scoped(db, m.Capability, clause.capability_id, actor)
        if definition.supervision:
            scoped(db, m.Capability, definition.supervision.capability_id, actor)
            for category_id in definition.supervision.trainee_category_ids:
                scoped(db, m.Category, category_id, actor)
        if definition.lead:
            for capability_id in definition.lead.capability_ids:
                scoped(db, m.Capability, capability_id, actor)
            for category_id in definition.lead.category_ids:
                scoped(db, m.Category, category_id, actor)
            from .policies import condition_references
            # No tags here: a session keeps its duty type as it was, outside the rules' tag resolution (with_tags).
            condition_references(db, actor, definition.lead.conditions, tags=False)
    if kind == "programme_memberships":
        person_access(db, actor, value.person_id)
        programme = scoped(db, m.Programme, value.programme_id, actor)
        require(programme.starts_on <= value.starts_on and (programme.ends_on is None or value.ends_on <= programme.ends_on),
                "MEMBERSHIP_WINDOW", "Membership must lie within the programme dates")
        others = db.scalars(select(m.ProgrammeMembership).where(m.ProgrammeMembership.department_id == actor.department_id,
            m.ProgrammeMembership.person_id == value.person_id, m.ProgrammeMembership.programme_id == value.programme_id))
        require(not any(r.id != existing_id and range_overlaps(r.starts_on, r.ends_on, value.starts_on, value.ends_on) for r in others),
                "DUPLICATE_RELATIONSHIP", "Programme membership overlaps an existing record", 409)
    if kind == "cohorts":
        programme = scoped(db, m.Programme, value.programme_id, actor)
        require(programme.starts_on <= value.starts_on and (programme.ends_on is None or value.ends_on <= programme.ends_on),
                "COHORT_WINDOW", "Cohort must lie within the programme's effective period")
    if kind == "rotations":
        if value.rotation_type_id:
            typ = scoped(db, m.RotationType, value.rotation_type_id, actor)
            require(typ.active and typ.starts_on <= value.starts_on and (typ.ends_on is None or value.ends_on <= typ.ends_on),
                    "ROTATION_TYPE_WINDOW", "Rotation must lie within its active rotation type dates")
        person_access(db, actor, value.person_id)
        if value.mentor_id:
            person_access(db, actor, value.mentor_id)
        cohort = scoped(db, m.Cohort, value.cohort_id, actor)
        if value.service_id:
            scoped(db, m.Service, value.service_id, actor)
        require(cohort.starts_on <= value.starts_on and value.ends_on <= cohort.ends_on,
                "ROTATION_WINDOW", "Rotation must lie within its cohort window")
        rotation_membership(db, actor, value.person_id, cohort.programme_id, value.starts_on, value.ends_on, rotation_context)
        overlap = (any(range_overlaps(a, b, value.starts_on, value.ends_on)
                       for a, b in rotation_context["taken"].get(value.person_id, ())) if rotation_context is not None else
                   db.scalar(select(m.Rotation.id).where(m.Rotation.department_id == actor.department_id,
                       m.Rotation.person_id == value.person_id, m.Rotation.id != (existing_id or ""),
                       m.Rotation.starts_on < value.ends_on, m.Rotation.ends_on > value.starts_on).limit(1)) is not None)
        require(not overlap, "ROTATION_OVERLAP", "This trainee already has a rotation during these dates", 409)
        from .availability import rotation_leave_conflicts, require_rotation_leave
        conflicts = rotation_leave_conflicts(db, actor, rotation=value, rotation_context=rotation_context)
        require_rotation_leave(conflicts)
        return conflicts
    if kind == "rotation_templates":
        if value.programme_id:
            scoped(db, m.Programme, value.programme_id, actor)
        for cls, ids in ((m.RotationType, {b.rotation_type_id for b in value.blocks} - {None}),
                         (m.Service, {b.service_id for b in value.blocks} - {None})):
            known = set(db.scalars(select(cls.id).where(cls.department_id == actor.department_id, cls.id.in_(ids)))) if ids else set()
            known -= {row.id for row in db.deleted if isinstance(row, cls)}
            require(known == ids, "NOT_FOUND", "Record not found", 404)
    if kind == "recurring_series":
        people = set(value.definition.default_person_ids)
        known_people = (people & series_context['people'] if series_context is not None else
            set(db.scalars(select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id,
                                                   m.Person.id.in_(people)))) if people else set())
        known_people -= {row.id for row in db.deleted if isinstance(row, m.Person)}
        require(known_people == people, 'NOT_FOUND', 'Person not found', 404)
        typ = scoped(db, m.AssignmentType, value.assignment_type_id, actor)
        s.validate_attributes(s.TypeDefinition.model_validate(typ.definition), value.definition.attributes)
        scoped(db, m.Location, value.location_id, actor)
        for service_id in value.definition.service_ids:
            scoped(db, m.Service, service_id, actor)
        require(value.ends_on is None or (value.ends_on - value.starts_on).days <= 366, "SERIES_TOO_LONG", "A dated series version is bounded to one year")
        expand(value.definition, value.starts_on, value.ends_on)


def write_catalogue(db, actor, command, update=False, *, preview_only=False, resolved_occurrence=None,
                    series_context=None, permission=None):
    payload = CatalogueWrite.model_validate(command.payload)
    require(payload.catalogue in CATALOGUES, "UNKNOWN_CATALOGUE", "Unsupported catalogue")
    kind = payload.catalogue
    require(payload.members is None or kind == "tags", "TAG_MEMBERS_ONLY", "Only a tag can include member changes")
    score_check_add = permission == 'score_check.add_missing'
    require(permission is None or (score_check_add and not update and kind in {'people', 'services'}),
            'FORBIDDEN', 'This permission only creates people or services from a score-check gap', 403)
    if score_check_add:
        require_permission(actor, permission)
    if kind == "events":
        from . import events
        return events.write(db, actor, command, payload, update)
    if kind in PROGRAMME_CATALOGUES:
        require_role(actor, "admin", "coordinator", "programme_lead")
    elif kind in {"people", "department_content", "tags"} and not score_check_add:
        require_role(actor, "admin", "coordinator")
    elif not score_check_add:
        require_role(actor, "admin")
    if db.info.get("bulk") and kind != "people":
        db.flush()
    clear_owner = "owner_identity_id" in payload.model_fields_set
    if clear_owner:
        require(kind == "programmes" and update, "OWNER_CLEAR_UPDATE_ONLY", "Only an existing programme's owner can be cleared")
        require_role(actor, "admin", "coordinator")
    row = scoped(db, CATALOGUES[kind], command.target_id, actor) if update else None
    if kind == 'recurring_series' and row and isinstance(payload.value.get('definition'), dict):
        definition = payload.value['definition']
        payload.value = {**payload.value, 'definition': {
            'usual_assignees_fixed': row.definition.get('usual_assignees_fixed', False), **definition}}
    value = s.CATALOGUE_SCHEMAS[kind].model_validate(payload.value)
    require(not (db.info.get("bulk") and getattr(value, "notify", False)),
            "BULK_EXTERNAL_EFFECT", "Announcements that notify people must be sent individually")
    if kind == 'event_types':
        from . import events
        events.check_type(db, actor, value)
    if kind in PROGRAMME_CATALOGUES:
        if row:
            programme_write_access(db, actor, kind, row)
        if kind != "programmes" or row:
            programme_write_access(db, actor, kind, row if kind == "programmes" else value)
    if kind == 'categories' and row and 'short_code' not in payload.value:
        value = value.model_copy(update={'short_code': row.short_code})
    if kind == 'assignment_types' and row and 'short_code' not in payload.value:
        value = value.model_copy(update={'short_code': row.short_code or ''})
    if row and kind in {'tags', 'rotation_types', 'leave_types', 'categories'}:
        fields = ('sort_order',) if kind == 'categories' else ('short_name',)
        value = value.model_copy(update={key: getattr(row, key) for key in fields if key not in payload.value})
    if kind == 'people' and row:
        value = value.model_copy(update={key: getattr(row, key) for key in ('rota_name', 'rota_aliases', 'work_email', 'work_phone', 'work_extension', 'pager', 'given_name', 'family_name')
                                        if key not in payload.value})
    if kind == 'people':
        inactive_from = value.inactive_from
        if row and not row.active and 'inactive_from' not in payload.value:
            inactive_from = row.inactive_from
        value = value.model_copy(update={'inactive_from': person_inactive_from_transition(
            db, actor, row, value.active, inactive_from)})
    if kind == 'event_types' and row and 'also_email' not in payload.value:
        value = value.model_copy(update={'also_email': row.also_email})
    if kind == 'rotation_types' and row and 'leave_policy' not in payload.value:
        value = value.model_copy(update={'leave_policy': row.leave_policy})
    if kind == 'department_content':
        if row:
            value = value.model_copy(update={key: getattr(row, key) for key in
                ('also_email', 'email_sender', 'email_sender_identity_id') if key not in payload.value})
        from .email_messages import check_sender
        check_sender(db, actor, value, saved=record(row) if row else None)
    previous = None
    if row:
        expected(row, command.expected_revision)
        from .catalogue_dates import guard_dates
        guard_dates(db, actor, kind, row, value)
        if kind == "programme_calendar_entries":
            from .scheduling_facts import guard_calendar_edit
            guard_calendar_edit(db, actor, row, value)
        # The values before this edit, so the person can put them back (Undo) through this same checked write.
        before = record(row)
        previous = {k: before[k] for k in s.CATALOGUE_SCHEMAS[kind].model_fields if k in before}
        if kind == 'recurring_series':
            previous['definition'] = {'usual_assignees_fixed': False, **previous['definition']}
        if kind == 'date_sets':
            # Undo must distinguish clearing metadata from an older client omitting it.
            previous['entries'] = [{'start_year': None, 'fixed_duration': False, **entry}
                                   for entry in previous['entries']]
    if kind == 'assignment_types':
        old = row.definition.get('lead') if row else None
        old = s.TeamLead.model_validate(old).model_dump(mode='json') if old else None
        new = value.definition.lead.model_dump(mode='json') if value.definition.lead else None
        require(old == new, 'USE_RULE_CATALOGUE', 'Change team leads in Rules', 422)
    rotation_conflicts = validate_references(db, actor, kind, value, row.id if row else None) or []
    overlap_preview = None
    if kind == 'recurring_series':
        from .series_conflicts import preview, pending_write
        from .leave_disclosure import overlap_labels
        series_context = series_context or db.info.get('bulk', {}).get('series_overlaps')
        overlap_preview = pending_write(series_context, value, row.id if row else None, command.expected_revision)
        if overlap_preview is None:
            require(series_context is None, 'STALE_PREVIEW', 'The pending series definitions changed; preview again', 409)
            overlap_preview = preview(db, actor, value, row.id if row else None, disclose=False)
        acknowledgement = (series_context or {}).get('acknowledgement') or payload.overlap_acknowledgement
        require(preview_only or resolved_occurrence or (series_context or {}).get('preview_only') or not overlap_preview['conflicts']
                or acknowledgement == overlap_preview['conflict_token'],
                'SERIES_OVERLAP_ACKNOWLEDGEMENT',
                'Review and acknowledge every conflicting occurrence before saving this recurrence', 409,
                json_value(overlap_labels(db, actor, overlap_preview)))
    if kind == 'date_sets':
        from .date_sets import check_write
        check_write(db, actor, value, row)
    if kind == 'categories':
        code = rank_code(value.model_dump())
        # Legacy collisions remain readable/editable; only introducing or changing a code may add a collision.
        if code and (row is None or code != rank_code(record(row))):
            clash = next((other for other in db.scalars(select(m.Category).where(
                m.Category.department_id == actor.department_id, m.Category.id != (row.id if row else '')))
                if rank_code(record(other)) == code), None)
            require(clash is None, 'RANK_CODE_TAKEN', f'Rank short code {code} is already used in this department', 422)
    if kind == "people" and value.short_code:
        candidates = list(db.scalars(select(m.Person).where(m.Person.organisation_id == actor.organisation_id,
            m.Person.short_code == value.short_code, m.Person.id != (row.id if row else ""))))
        if db.info.get('bulk'):
            candidates.extend(person for person in set(db.new) | set(db.dirty) if isinstance(person, m.Person))
        clash = next((person for person in candidates if person not in db.deleted and person is not row
                      and person.organisation_id == actor.organisation_id and person.short_code == value.short_code), None)
        require(clash is None, "SHORT_CODE_TAKEN", f"Short code {value.short_code} is already used by another person", 409)
    if kind == "assignment_types" and value.short_code and value.active and \
            (db.get(m.Department, actor.department_id).display_settings or {}).get("avoid_duplicate_codes", True):
        candidates = list(db.scalars(select(m.AssignmentType).where(
            m.AssignmentType.department_id == actor.department_id, m.AssignmentType.active.is_(True),
            m.AssignmentType.short_code == value.short_code, m.AssignmentType.id != (row.id if row else ""))))
        if db.info.get("bulk"):
            candidates.extend(other for other in set(db.new) | set(db.dirty) if isinstance(other, m.AssignmentType))
        clash = next((other for other in candidates if other not in db.deleted and other is not row
                      and other.department_id == actor.department_id and other.active and
                      other.short_code == value.short_code), None)
        require(clash is None, "DUTY_CODE_TAKEN",
                f"Duty short code {value.short_code} is already used by an active duty type in this department", 422)
    changes = {"added": [], "removed": []}
    add, remove = set(), set()
    if payload.members:
        require(update or not payload.members.remove, "TAG_MEMBER_REMOVE_ON_CREATE", "A new tag has no members to remove")
        add, remove = tags.checked_changes(db, actor, row, payload.members, value)
    if kind == "tags":
        tags.check_write(db, actor, value, row, remove)
        if row and remove:
            changes = tags.apply_members(db, actor, row, set(), remove, payload.reason)
    old_name = row.name if row is not None and kind == "tags" else None
    values = value.model_dump()
    mentor_id = values.pop('mentor_id') if kind == 'rotations' else None
    notify = values.pop("notify", False)  # an action on saving, not a stored field
    for key in ("definition", "audience", "default_audience", "entries", "dial", "visibility", "format"):
        if key in values:
            values[key] = json_value(values[key])
    if row:
        if clear_owner:
            row.owner_identity_id = None
        for key, val in values.items():
            setattr(row, key, val)
        row.revision += 1
    else:
        if kind == "people":
            values["organisation_id"] = actor.organisation_id
        else:
            values["department_id"] = actor.department_id
        if kind == "programmes" and not actor.permits("admin", "coordinator"):
            values["owner_identity_id"] = actor.identity_id
        row = CATALOGUES[kind](**values)
        db.add(row)
    if kind == 'people':
        from .contacts import LEGACY_FIELDS, sync_legacy
        db.flush()
        sync_legacy(db, actor, row, LEGACY_FIELDS & payload.value.keys(), payload.reason)
    affected = 1
    if kind == 'rotations':
        affected += sync_rotation_attachment(db, actor, row, mentor_id, payload.reason)
    if overlap_preview is not None and not preview_only and not (series_context or {}).get('preview_only'):
        from .series_conflicts import cancel_actions
        db.flush()
        series_conflicts = [{**conflict, 'series_id': row.id,
                      'shift_id': f"series:{row.id}:{conflict['occurrence_on']}"}
                     if conflict['series_id'] is None else conflict for conflict in overlap_preview['conflicts']]
        overlap_preview['conflicts'] = cancel_actions(db, actor, series_conflicts)
    history(db, actor, kind, row, payload.reason, snapshot_extra=({
        'overlap_conflicts': json_value(overlap_preview['conflicts']),
        'overlap_conflict_token': overlap_preview['conflict_token'],
        'overlap_acknowledged': bool(overlap_preview['conflicts'] and not resolved_occurrence),
        **({'resolved_occurrence': str(resolved_occurrence)} if resolved_occurrence else {})
    } if overlap_preview is not None else None))
    if kind == 'assignment_types' and row.definition.get('lead') and (not update or not before['definition'].get('lead')):
        from .scoring_defaults import seed as seed_scoring_defaults
        seed_scoring_defaults(db, actor.department_id, types=[row], workload=False)
    if kind == "people":
        affected += tags.sync_person(db, actor, row, payload.reason, created=not update)
    if kind == "tags":
        tags.renamed(db, actor, row, old_name, payload.reason)
        if add:
            changes["added"] = tags.apply_members(db, actor, row, add, set(), payload.reason, created=not update)["added"]
        affected += len(changes['added']) + len(changes['removed'])
    if kind == "department_content":
        row.reminder_empty_checked_on = None
        db.flush()
        receipt_state = _announcement_state(db, row) if db.info.get('bulk') else {}
        announcements.sync_receipts(db, row)
        announcements.send(db, actor, row, notify=notify)
        if db.info.get('bulk'):
            affected += sum(receipt_state.get(key) != value for key, value in _announcement_state(db, row).items())
        return {"record": record(row), "previous_value": previous, "affected_count": affected,
                "message": "Announcement saved." + (" It is sent to the inbox of everyone it is for." if notify and row.active else "")}
    bump_inputs(db, actor)
    conflict_details = {}
    if overlap_preview is not None:
        conflict_details = {'overlap_conflicts': overlap_preview['conflicts'],
                            'conflicts': overlap_preview['conflicts'], 'conflict_token': overlap_preview['conflict_token']}
    elif rotation_conflicts:
        conflict_details = {'rotation_leave_conflicts': rotation_conflicts, 'conflicts': rotation_conflicts,
                            'warnings': list(dict.fromkeys(f['message'] for f in rotation_conflicts))}
    undo = {}
    if kind == 'contact_types':
        undo = {'undo': {'kind': 'catalogue.update', 'target_id': row.id, 'expected_revision': row.revision,
                'payload': {'catalogue': kind, 'value': previous or {**value.model_dump(mode='json'), 'active': False},
                            'reason': payload.reason}}}
    return {"record": record(row), "previous_value": previous, "affected_count": affected, **undo, **(changes if kind == "tags" else {}),
            **conflict_details,
            "message": "Configuration saved. Existing publications are preserved; current compliance is evaluated separately."
                       + (' Warning: ' + ' '.join(dict.fromkeys(f['message'] for f in rotation_conflicts)) if rotation_conflicts else '')}


def _announcement_state(db, row):
    """Receipt and inbox writes use SQL, outside the bulk history queue."""
    receipts, notes = m.AnnouncementReceipt, m.Notification
    outboxes = select(m.Outbox.id).where(m.Outbox.department_id == row.department_id,
        m.Outbox.kind == 'announcement', m.Outbox.payload['content_id'].as_string() == row.id)
    return {
        **{(receipts, ident): values for ident, *values in db.execute(select(receipts.id, receipts.revision,
            receipts.delivered_at, receipts.read_at).where(receipts.content_id == row.id))},
        **{(notes, ident): values for ident, *values in db.execute(select(notes.id, notes.source_kind, notes.source_id)
            .where(notes.outbox_id.in_(outboxes)))}
    }


def sync_rotation_attachments(db, actor, changes, reason):
    """Stage owned, exclusive attachments together before checking their final overlap state."""
    staged = []
    for rotation, mentor_id in changes:
        link = rotation.attachment_link
        attachment = link.mentorship if link else None
        if not mentor_id or not link or link.synced_revision != attachment.revision:
            continue
        shared = db.scalar(select(m.RotationAttachment.rotation_id).where(
            m.RotationAttachment.mentorship_id == attachment.id, m.RotationAttachment.rotation_id != rotation.id).limit(1))
        if shared:
            continue
        value = s.MentorshipInput(person_id=rotation.person_id, mentor_id=mentor_id,
            starts_on=rotation.starts_on, ends_on=rotation.ends_on,
            service_ids=[rotation.service_id] if rotation.service_id else [])
        if all(getattr(attachment, key) == val for key, val in value.model_dump().items()):
            continue
        staged.append((attachment, link, value))
    for attachment, link, value in staged:
        for key, val in value.model_dump().items():
            setattr(attachment, key, val)
        attachment.revision += 1
        link.synced_revision = attachment.revision
    db.flush()
    for attachment, _, value in staged:
        validate_references(db, actor, 'mentorships', value, attachment.id)
    history_many(db, actor, [('mentorships', attachment, reason) for attachment, _, _ in staged])
    return 2 * len(staged) + sum(sync_rotation_attachment(db, actor, rotation, mentor_id, reason)
                                 for rotation, mentor_id in changes)


def sync_rotation_attachment(db, actor, rotation, mentor_id, reason):
    """The rotation owns only the attachment revision it wrote; independent edits stay independent."""
    db.flush()
    link = rotation.attachment_link
    if not mentor_id and not link:
        return 0
    attachment = link.mentorship if link else None
    owned = bool(link and link.synced_revision == attachment.revision)
    shared = bool(attachment and db.scalar(
        select(m.RotationAttachment.rotation_id).where(m.RotationAttachment.mentorship_id == attachment.id,
                                                      m.RotationAttachment.rotation_id != rotation.id).limit(1)))
    if mentor_id:
        value = s.MentorshipInput(person_id=rotation.person_id, mentor_id=mentor_id,
            starts_on=rotation.starts_on, ends_on=rotation.ends_on,
            service_ids=[rotation.service_id] if rotation.service_id else [])
        if owned and all(getattr(attachment, key) == val for key, val in value.model_dump().items()):
            return 0
        # A separately maintained attachment keeps its scope and strength, including on an Undo.
        if attachment and not owned and attachment.person_id == value.person_id and attachment.mentor_id == mentor_id:
            return 0
        # A shared row cannot be rewritten for one rotation. Relink, or report the existing overlap conflict.
        owned = owned and not shared
        if not owned:
            programme = select(m.Cohort.programme_id).where(m.Cohort.id == rotation.cohort_id,
                m.Cohort.department_id == actor.department_id).scalar_subquery()
            matching = [r for r in db.scalars(select(m.Mentorship).where(
                m.Mentorship.department_id == actor.department_id, m.Mentorship.person_id == value.person_id,
                m.Mentorship.mentor_id == mentor_id, m.Mentorship.starts_on == value.starts_on,
                m.Mentorship.ends_on == value.ends_on,
                or_(m.Mentorship.programme_id.is_(None), m.Mentorship.programme_id == programme)).order_by(m.Mentorship.id))
                if not r.assignment_type_ids
                and (not r.service_ids or value.service_ids and set(value.service_ids) <= set(r.service_ids))]
            attachment = min(matching, key=lambda r: (not r.service_ids, len(r.service_ids or []), r.id), default=None)
        if attachment and not owned:
            provenance = db.scalar(select(m.RotationAttachment).where(
                m.RotationAttachment.mentorship_id == attachment.id,
                m.RotationAttachment.synced_revision == attachment.revision).limit(1))
            synced_revision = attachment.revision if provenance else None
            if link:
                link.mentorship_id, link.synced_revision = attachment.id, synced_revision
            else:
                db.add(m.RotationAttachment(rotation_id=rotation.id, mentorship_id=attachment.id,
                                           synced_revision=synced_revision))
            affected = 1
        else:
            if link and not owned:
                # Detach through the same ownership-aware path before validating the replacement.
                sync_rotation_attachment(db, actor, rotation, None, reason)
                link = None
            validate_references(db, actor, 'mentorships', value, attachment.id if owned else None)
            if owned:
                for key, val in value.model_dump().items():
                    setattr(attachment, key, val)
                attachment.revision += 1
            else:
                attachment = m.Mentorship(department_id=actor.department_id, **value.model_dump())
                db.add(attachment)
            history(db, actor, 'mentorships', attachment, reason)
            if not link:
                link = m.RotationAttachment(rotation_id=rotation.id, mentorship_id=attachment.id)
                db.add(link)
            link.mentorship_id, link.synced_revision = attachment.id, attachment.revision
            affected = 2
    elif link:
        db.delete(link)
        db.flush()
        affected = 1
        if owned and not shared:
            removed, _ = remove_created(db, actor, m.Mentorship, 'mentorships.remove',
                                       [{'id': attachment.id, 'revision': attachment.revision}], reason, own=False)
            affected += removed
    db.flush()
    db.expire(rotation, ['attachment_link'])
    return affected


CatalogueBulk = s.CatalogueBulk


def undo_programme_create(db, actor, item, reason):
    row = scoped(db, m.Programme, item.id, actor)
    programme_access(db, actor, row.id)
    expected(row, item.revision)
    creator = db.scalar(select(m.DefinitionHistory.actor_id).where(
        m.DefinitionHistory.department_id == actor.department_id, m.DefinitionHistory.kind == "programmes",
        m.DefinitionHistory.object_id == row.id, m.DefinitionHistory.object_revision == 1))
    require(row.revision == 1 and creator == actor.identity_id,
            "PROGRAMME_CREATE_UNDO", "Only your unchanged programme creation can be undone", 409)
    require(actor.permits("admin", "coordinator") or row.owner_identity_id == actor.identity_id,
            "FORBIDDEN", "This programme creation is outside your ownership", 403)
    # Do not erase training records or leave live or historical references dangling.
    for cls in (m.Cohort, m.ProgrammeMembership, m.Mentorship, m.ProgrammeCalendarEntry, m.RotationTemplate):
        require(db.scalar(select(cls.id).where(cls.programme_id == row.id).limit(1)) is None,
                "PROGRAMME_IN_USE", "This programme has training records; its creation cannot be undone", 409)
    for cls, condition in ((m.TagMember, (m.TagMember.kind == "programmes") & (m.TagMember.member_id == row.id)),
                           (m.SourceMapping, (m.SourceMapping.target_kind == "programmes") & (m.SourceMapping.target_id == row.id))):
        require(db.scalar(select(cls.id).where(condition).limit(1)) is None,
                "PROGRAMME_IN_USE", "This programme is referenced; its creation cannot be undone", 409)
    for cls, column in ((m.Policy, m.Policy.definition), (m.AssignmentType, m.AssignmentType.definition),
                        (m.DepartmentContent, m.DepartmentContent.audience), (m.Publication, m.Publication.snapshot),
                        (m.DefinitionHistory, m.DefinitionHistory.snapshot)):
        query = select(cls.id).where(cast(column, Text).contains(row.id))
        if cls is m.DefinitionHistory:
            query = query.where(or_(cls.kind != "programmes", cls.object_id != row.id))
        require(db.scalar(query.limit(1)) is None, "PROGRAMME_IN_USE",
                "This programme is referenced by configuration or evidence; its creation cannot be undone", 409)
    removed_ids = []
    removed, kept = remove_created(db, actor, m.Programme, "programmes", [item.model_dump()], reason, removed_ids=removed_ids)
    # Tombstone-only receipts contain no stale owned record and remain safely retryable after removal.
    return {"records": [], "previous_values": [], "removed_ids": removed_ids, "removed": removed, "kept": kept,
            "message": "Programme creation undone."}


def bulk_commands(db, actor, payload):
    fields = s.CATALOGUE_SCHEMAS[payload.catalogue].model_fields
    in_definition = payload.catalogue == 'assignment_types'
    commands = []
    for item in payload.items:
        row = scoped(db, CATALOGUES[payload.catalogue], item.id, actor)
        if payload.catalogue in PROGRAMME_CATALOGUES:
            programme_write_access(db, actor, payload.catalogue, row)
        current = record(row)
        value = {k: current[k] for k in fields if k in current}
        value.update(payload.set)
        if payload.catalogue == 'people' and row.active and 'inactive_from' not in payload.set:
            value.pop('inactive_from', None)
        if payload.active is not None:
            value['active'] = payload.active
        if payload.colour is not None and in_definition:
            value['definition'] = {**value['definition'], 'colour': payload.colour}
        elif payload.colour is not None:
            value['colour'] = payload.colour
        if payload.add_tags or payload.remove_tags:
            tags = [tag for tag in value.get('tags') or [] if tag not in payload.remove_tags]
            value['tags'] = tags + [tag for tag in payload.add_tags if tag not in tags]
        commands.append(s.Command(kind='catalogue.update', payload={'catalogue': payload.catalogue,
            'value': value, 'reason': payload.reason, 'overlap_acknowledgement': payload.overlap_acknowledgement},
            target_id=item.id, expected_revision=item.revision))
    return commands


@batch_input_invalidations
def bulk_catalogue(db, actor, command):
    """One change applied to many records through the same checked write as a single edit, all or nothing."""
    payload = CatalogueBulk.model_validate(command.payload)
    require(payload.catalogue in CATALOGUES, "UNKNOWN_CATALOGUE", "Unsupported catalogue")
    if (payload.catalogue == "programmes" and len(payload.items) == 1 and payload.items[0].revision == 1
            and payload.active is False and not payload.set and payload.colour is None
            and not payload.add_tags and not payload.remove_tags and payload.reason == "Undo"):
        # Compatibility: the current client sends no source receipt or structured creation-Undo flag.
        require(command.target_id is None and command.expected_revision is None,
                "PROGRAMME_UNDO_ENVELOPE", "Creation Undo uses only the item id and revision")
        return undo_programme_create(db, actor, payload.items[0], payload.reason)
    schema = s.CATALOGUE_SCHEMAS[payload.catalogue]
    fields = schema.model_fields
    require(payload.active is not None or payload.colour is not None or payload.add_tags or payload.remove_tags or payload.set,
            "NOTHING_TO_CHANGE", "Choose a change to apply")
    require(all(k in fields and k not in {"id", "revision", "department_id"} for k in payload.set),
            "UNSUPPORTED_CHANGE", "These records have no such field")
    in_definition = payload.catalogue == "assignment_types"
    require(payload.colour is None or "colour" in fields or in_definition, "UNSUPPORTED_CHANGE", "These records have no colour")
    require(not (payload.add_tags or payload.remove_tags) or "tags" in fields, "UNSUPPORTED_CHANGE", "These records have no tags")
    require(payload.active is None or "active" in fields, "UNSUPPORTED_CHANGE", "These records cannot be switched off")
    commands = bulk_commands(db, actor, payload)
    series_context = db.info.get('bulk', {}).get('series_overlaps')
    if payload.catalogue == 'recurring_series' and series_context is None:
        from .series_conflicts import prepare_writes, write_item
        series_context = prepare_writes(db, actor, [write_item(db, actor, part) for part in commands],
                                        payload.overlap_acknowledgement)
    saved, previous, affected = [], [], 0
    for single in commands:
        written = write_catalogue(db, actor, single, update=True, series_context=series_context)
        affected += written.get('affected_count', 1)
        saved.append(written["record"])
        previous.append(written["previous_value"])
    undo = {'item_kind': 'catalogue.update', 'reason': payload.reason,
        **({'undo_of': db.info['receipt_id']} if db.info.get('receipt_id') else {}),
        'items': [{'target_id': row['id'], 'expected_revision': row['revision'],
            'payload': {'catalogue': payload.catalogue, 'value': before, 'reason': payload.reason}}
            for row, before in zip(saved, previous)]}
    if payload.catalogue == 'recurring_series':
        from .bulk import _series_undo_acknowledgement
        undo.update(_series_undo_acknowledgement(db, actor, undo))
    return {"records": saved, "previous_values": previous, "affected_count": affected,
            "undo": {"kind": "bulk.apply", "payload": undo},
            "message": f"{len(saved)} record{'s' if len(saved) != 1 else ''} updated."}


def bulk_membership(db, actor, command):
    """One rank for many people in one audited step. Each person goes through the same checks as a single record;
    anyone who already holds that rank over those dates is skipped and reported, not an error."""
    from types import SimpleNamespace
    require_role(actor, "admin", "coordinator")
    value = s.MembershipBulkInput.model_validate(command.payload)
    scoped(db, m.Category, value.category_id, actor)
    # Ending other ranks only makes sense for a move with no end: a temporary rank would leave people with none after it.
    require(not (value.end_other_ranks and value.ends_on), "RANK_MOVE_NEEDS_OPEN_END",
            "To end their other ranks, give the new rank without a last day", 422)
    added, skipped, partial, later = [], [], [], []
    ended, ended_rows = 0, []
    for person_id in value.person_ids:
        person_access(db, actor, person_id)
    by_person, states = _relationship_state(db, actor, m.Membership, value.person_ids, 'category_id',
                                           [value.category_id], value.starts_on, value.ends_on)
    for person_id in value.person_ids:
        mine = by_person.get(person_id, [])
        state = states.get((person_id, value.category_id))
        if state:
            (skipped if state == 'covered' else partial).append(person_id)
            continue
        if value.end_other_ranks and any(r.category_id != value.category_id and r.starts_on >= value.starts_on for r in mine):
            later.append(person_id)  # another rank already starts on or after that day: not changed automatically
            continue
        if value.end_other_ranks:
            for r in mine:
                if r.category_id != value.category_id and r.starts_on < value.starts_on and (r.ends_on is None or r.ends_on > value.starts_on):
                    ended_rows.append({"id": r.id, "ends_on": r.ends_on.isoformat() if r.ends_on else None, "revision": r.revision + 1})
                    r.ends_on, r.revision = value.starts_on, r.revision + 1
                    history(db, actor, "membership.end", r, value.reason)
                    ended += 1
        single = SimpleNamespace(kind="membership.create", payload={"person_id": person_id, "category_id": value.category_id,
                                 "starts_on": value.starts_on.isoformat(), "ends_on": value.ends_on.isoformat() if value.ends_on else None})
        added.append(relationship(db, actor, single)["record"]["id"])
    return {"added": len(added), "already_had_it": len(skipped), "partly_had_it": len(partial), "needs_review": len(later),
            "other_ranks_ended": ended, "skipped_person_ids": partial + later,
            "created_ids": added, "ended": ended_rows,
            "message": f"{len(added)} given the rank" + (f"; {len(skipped)} already had it" if skipped else "")
                       + (f"; {len(partial)} had it for part of the period (left unchanged)" if partial else "")
                       + (f"; {len(later)} have another rank starting later (left unchanged)" if later else "")
                       + (f"; {ended} earlier ranks ended" if ended else "") + "."}


def _track_relationship(db, row):
    groups = db.info.get('bulk', {}).get('relationship_rows', {}).get(type(row))
    if groups is not None:
        groups.setdefault(row.person_id, []).append(row)


def _clear_relationship_rows(db, *_):
    if bulk := db.info.get('bulk'):
        bulk.pop('relationship_rows', None)


def _relationship_state(db, actor, cls, person_ids, ref_key, ref_ids, starts_on, ends_on, *,
                        existing_id=None, service_id=None):
    """One live-row and interval check for single, bulk, programme and import relationship writes."""
    people, references = set(person_ids), set(ref_ids)
    bulk = db.info.get('bulk')
    if bulk is None:
        rows = tags._pending_rows(db, cls, select(cls).where(cls.department_id == actor.department_id,
            cls.person_id.in_(people)), lambda row: row.department_id == actor.department_id and row.person_id in people)
    else:
        if not db.info.get('relationship_events'):
            event.listen(db, 'after_attach', _track_relationship)
            # Flushes retain these live rows; attachments and per-read deletion checks keep the index current.
            event.listen(db, 'after_soft_rollback', _clear_relationship_rows)
            db.info['relationship_events'] = True
        relationships = bulk.setdefault('relationship_rows', {})
        if cls not in relationships:
            grouped = {}
            for row in tags._pending_rows(db, cls, select(cls).where(cls.department_id == actor.department_id),
                                         lambda row: row.department_id == actor.department_id):
                grouped.setdefault(row.person_id, []).append(row)
            relationships[cls] = grouped
        rows = [row for person_id in people for row in relationships[cls].get(person_id, [])]
    by_person, states = {}, {}
    for row in rows:
        if row not in db or row in db.deleted or row.department_id != actor.department_id or row.person_id not in people:
            continue
        if getattr(row, 'withdrawn', False):
            continue
        by_person.setdefault(row.person_id, []).append(row)
        ref_id = getattr(row, ref_key)
        if ref_id not in references or existing_id is not None and row.id == existing_id:
            continue
        if cls is m.CapabilityGrant and (row.revoked or row.service_id != service_id):
            continue
        if range_overlaps(row.starts_on, row.ends_on, starts_on, ends_on):
            key = (row.person_id, ref_id)
            states.setdefault(key, 'overlap')
            if row.starts_on <= starts_on and (row.ends_on is None or ends_on is not None and row.ends_on >= ends_on):
                states[key] = 'covered'
    return by_person, states


def _relationship_value(db, actor, kind, payload, existing_id=None, reason=None, *,
                        overlap_message="An overlapping record already defines this relationship"):
    if kind == "membership":
        require_role(actor, "admin", "coordinator")
        value = s.MembershipInput.model_validate(payload)
        cls, ref_cls, ref_key = m.Membership, m.Category, "category_id"
    elif kind == "eligibility":
        # Service eligibility is a clinical permission, not a label-derived permission.
        require_role(actor, "credentialer")
        value = s.EligibilityInput.model_validate(payload)
        independence.ensure(db, actor, "skills.self", actor.person_id != value.person_id,
                            "Service eligibility cannot be self-attested", reason or "Service eligibility for themselves", value.person_id)
        cls, ref_cls, ref_key = m.Eligibility, m.Service, "service_id"
    else:
        require_role(actor, "credentialer")
        value = s.GrantInput.model_validate(payload)
        independence.ensure(db, actor, "skills.self", actor.person_id != value.person_id,
                            "Clinical capability evidence cannot be self-attested", reason or value.evidence, value.person_id)
        cls, ref_cls, ref_key = m.CapabilityGrant, m.Capability, "capability_id"
    person = person_access(db, actor, value.person_id)
    reference = scoped(db, ref_cls, getattr(value, ref_key), actor)
    bulk = db.info.get('bulk')
    if bulk is not None:
        references = bulk.setdefault('relationship_references', {})
        references[(m.Person, person.id)] = person
        references[(ref_cls, reference.id)] = reference
    if isinstance(value, s.GrantInput) and value.service_id:
        service = scoped(db, m.Service, value.service_id, actor)
        if bulk is not None:
            references[(m.Service, service.id)] = service
    _, states = _relationship_state(db, actor, cls, [value.person_id], ref_key, [getattr(value, ref_key)],
        value.starts_on, value.ends_on, existing_id=existing_id, service_id=getattr(value, 'service_id', None))
    require(not states, "DUPLICATE_RELATIONSHIP", overlap_message, 409)
    return cls, value


def relationship(db, actor, command):
    cls, value = _relationship_value(db, actor, command.kind.split('.')[0], command.payload)
    values = value.model_dump()
    if cls is m.CapabilityGrant:
        values["granted_by"] = actor.identity_id
    row = cls(department_id=actor.department_id, **values)
    db.add(row)
    if not db.info.get("bulk"):
        db.flush()
    history(db, actor, command.kind, row, getattr(value, "evidence", "Authorised relationship creation"))
    bump_inputs(db, actor)
    return {"record": record(row)}


def update_relationship_dates(db, actor, command):
    kind = command.kind.split('.')[0]
    cls, schema = {'membership': (m.Membership, s.MembershipInput), 'eligibility': (m.Eligibility, s.EligibilityInput),
                   'grant': (m.CapabilityGrant, s.GrantInput)}[kind]
    require_role(actor, *(('admin', 'coordinator') if cls is m.Membership else ('credentialer',)))
    row = scoped(db, cls, command.target_id, actor)
    expected(row, command.expected_revision)
    patch = s.RelationshipUpdateDates.model_validate(command.payload)
    changes = patch.model_dump(include={'starts_on', 'ends_on'}, exclude_unset=True)
    require(bool(changes), 'NOTHING_TO_CHANGE', 'Choose a change to apply')
    if cls is m.CapabilityGrant:
        require(not row.revoked, 'WORKFLOW_STATE', 'This skill has been revoked', 409)
    values = {key: getattr(row, key) for key in schema.model_fields}
    _, value = _relationship_value(db, actor, kind, {**values, **changes}, row.id, patch.reason)
    previous = json_value({'starts_on': row.starts_on, 'ends_on': row.ends_on})
    if value.starts_on != row.starts_on or value.ends_on != row.ends_on:
        row.starts_on, row.ends_on = value.starts_on, value.ends_on
        row.revision += 1
        history(db, actor, command.kind, row, patch.reason)
        bump_inputs(db, actor)
    from .credential_impact import affected
    impact = affected(db, actor, row, notify=True) if cls is m.Eligibility else None
    return {'record': record(row), 'previous_value': previous,
            **({'published_impact': impact} if impact is not None else {}),
            'undo': {'kind': command.kind, 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {**previous, 'reason': patch.reason}}}


def relationship_shortening_problem(row, payload):
    """Preview confirmation for removing any of the relationship's currently recorded date range."""
    patch = s.RelationshipUpdateDates.model_validate({'starts_on': row.starts_on, 'ends_on': row.ends_on, **payload})
    changes = patch.model_dump(include={'starts_on', 'ends_on'}, exclude_unset=True)
    start, end = changes.get('starts_on', row.starts_on), changes.get('ends_on', row.ends_on)
    if start > row.starts_on or end is not None and (row.ends_on is None or end < row.ends_on):
        # As the app shows a period: "1 Oct 2026 – 31 Oct 2026" (the stored end is the day after the last day).
        def period(first, after):
            day = lambda d: f'{d.day} {d:%b %Y}'
            return f'{day(first)} – {day(after - timedelta(days=1))}' if after else f'{day(first)} onwards'
        return Problem(409, 'WOULD_SHORTEN',
            f'This shortens {period(row.starts_on, row.ends_on)} to {period(start, end)}',
            {'before': json_value({'starts_on': row.starts_on, 'ends_on': row.ends_on}),
             'after': json_value({'starts_on': start, 'ends_on': end})})
    return None


def bulk_access(db, actor, command):
    """Credentialer: the same dated clinics or skills for many people, one checked record per pair. A pair already
    covered for the whole period is skipped; a partly overlapping one is reported for review, never changed."""
    from types import SimpleNamespace
    require_role(actor, "credentialer")
    value = s.StaffAccessBulkInput.model_validate(command.payload)
    clinics = command.kind == "eligibility.bulk_create"
    require(bool(value.service_ids) == clinics, "UNSUPPORTED_CHANGE", "Choose clinics to authorise or skills to grant")
    cls, key, items = (m.Eligibility, "service_id", value.service_ids) if clinics else (m.CapabilityGrant, "capability_id", value.capability_ids)
    for item in items:
        scoped(db, m.Service if clinics else m.Capability, item, actor)
    created, covered, overlapping, own = [], 0, [], []
    for person_id in value.person_ids:
        person_access(db, actor, person_id)
    _, states = _relationship_state(db, actor, cls, value.person_ids, key, items, value.starts_on, value.ends_on)
    for person_id in value.person_ids:
        if person_id == actor.person_id:
            own.append(person_id)  # never self-attested: another credentialer does their own
            continue
        for item in items:
            state = states.get((person_id, item))
            if state == 'covered':
                covered += 1
                continue
            if state == 'overlap':
                overlapping.append({"person_id": person_id, key: item})
                continue
            payload = {"person_id": person_id, key: item, "starts_on": value.starts_on.isoformat(),
                       "ends_on": value.ends_on.isoformat() if value.ends_on else None}
            if not clinics:
                payload["evidence"] = value.evidence
            row = relationship(db, actor, SimpleNamespace(kind="eligibility.create" if clinics else "grant.create", payload=payload))["record"]
            created.append({"id": row["id"], "revision": row["revision"]})
    noun = ("clinic permission", "clinic permissions") if clinics else ("skill", "skills")
    return {"created": created, "already_current": covered, "overlapping": overlapping, "own_person_ids": own,
            "message": f"{len(created)} {noun[len(created) != 1]} added" + (f"; {covered} already current" if covered else "")
                       + (f"; {len(overlapping)} overlap an existing record (left unchanged)" if overlapping else "")
                       + ("; your own need another credentialer" if own else "") + "."}


def remove_created(db, actor, cls, kind, items, reason, own=True, *, removed_ids=None, affected_counts=None):
    """Undo of a creation: remove records that nobody has changed since (with `own`, only ones this person created).
    Anything changed meanwhile is kept and counted, never overwritten. The removed record stays in the definition history."""
    ids = {item["id"] for item in items}
    bulk = db.info.get('bulk')
    if bulk:
        loaded = (db.get(cls, ident) for ident in ids)
    else:
        loaded = db.scalars(select(cls).where(cls.id.in_(ids))) if ids else []
    rows = {row.id: row for row in loaded if row is not None}
    # Keep the referenced cohorts alive for the same shared authorization checks on every rotation.
    _cohorts = list(db.scalars(select(m.Cohort).where(m.Cohort.id.in_({row.cohort_id for row in rows.values()})))) if cls is m.Rotation and rows else []
    creators = {}
    if own and rows and not bulk:
        for object_id, actor_id in db.execute(select(m.DefinitionHistory.object_id, m.DefinitionHistory.actor_id).where(
                m.DefinitionHistory.object_id.in_(rows), m.DefinitionHistory.object_revision == 1)):
            creators.setdefault(object_id, actor_id)
    removed, kept, removed_rows = 0, 0, []
    for item in items:
        row = rows.get(item["id"])
        if row is None:
            continue
        row = scoped(db, cls, row.id, actor)
        if cls in {m.Rotation, m.Programme}:
            programme_write_access(db, actor, "rotations" if cls is m.Rotation else "programmes", row)
        mine = (_creation_actor(db, row) if bulk else creators.get(row.id)) if own else None
        if row.revision != item["revision"] or (own and mine != actor.identity_id):
            kept += 1
            continue
        removed_rows.append(row)
        removed += 1
    if removed_rows:
        reference_cache = {'creation_undo_targets': {cls: set(rows)}} if bulk is None else None
        # Preflight the whole removal before mutating rows, with one reference read per table even outside bulk.apply.
        with db.no_autoflush:
            for row in removed_rows:
                _require_unreferenced(db, row, reference_cache=reference_cache)
        for row in removed_rows:
            row.revision += 1
            if removed_ids is not None:
                removed_ids.append(row.id)
        history_many(db, actor, [(kind, row, reason) for row in removed_rows])
        for row in removed_rows:
            affected = 1
            if cls is m.Rotation:
                affected += sync_rotation_attachment(db, actor, row, None, reason)
            if affected_counts is not None:
                affected_counts.append(affected)
            db.delete(row)
    if not bulk:
        db.flush()
    if removed:
        bump_inputs(db, actor)
    return removed, kept


def prepare_created_undo(db, actor, items, value):
    """Freeze the batch's deletion candidates; reference checks still run at each item's turn."""
    from .bulk import _commands
    targets = defaultdict(set)
    for item in items:
        kind, payload, _ = _commands(item, value)
        if kind == 'catalogue.undo_create' and payload.get('catalogue') in CATALOGUES:
            targets[CATALOGUES[payload['catalogue']]].add(item.get('target_id'))
        elif kind in {'membership.undo_create', 'eligibility.undo_create', 'grant.undo_create'}:
            cls = {'membership.undo_create': m.Membership, 'eligibility.undo_create': m.Eligibility,
                   'grant.undo_create': m.CapabilityGrant}[kind]
            targets[cls].update(ref.get('id') for ref in payload.get('items', []))
    db.info['created_undo_targets'] = {cls: ids - {None} for cls, ids in targets.items()}


def _creation_actor(db, row):
    bulk = db.info.get('bulk')
    if bulk is None:
        return db.scalar(select(m.DefinitionHistory.actor_id).where(m.DefinitionHistory.object_id == row.id,
            m.DefinitionHistory.object_revision == 1).limit(1))
    creators = bulk.setdefault('creation_actors', {})
    if row.id not in creators:
        ids = db.info.get('created_undo_targets', {}).get(type(row), {row.id}) - creators.keys()
        ids.add(row.id)
        creators.update(dict.fromkeys(ids))
        for ident, actor_id in db.execute(select(m.DefinitionHistory.object_id, m.DefinitionHistory.actor_id).where(
                m.DefinitionHistory.object_id.in_(ids), m.DefinitionHistory.object_revision == 1)):
            if creators[ident] is None:
                creators[ident] = actor_id
    return creators[row.id]


@cache
def _referencing_tables(cls):
    tables = [(table, tuple(fk.parent for fk in table.foreign_keys if fk.column.table is cls.__table__), {})
              for table in m.Base.metadata.tables.values() if any(
                  fk.column.table is cls.__table__ for fk in table.foreign_keys)]
    tables.extend((m.TagMember.__table__, (m.TagMember.__table__.c.member_id,), {"kind": kind})
                  for kind, model in tags.MEMBER_CLASSES.items() if model is cls)
    tables.extend((m.SourceMapping.__table__, (m.SourceMapping.__table__.c.target_id,), {"target_kind": kind})
                  for kind, model in CATALOGUES.items() if model is cls)
    return tuple(tables)


def _clear_reference_cache(db, *_):
    if bulk := db.info.get('bulk'):
        bulk.pop('creation_references', None)
        bulk.pop('creation_json_references', None)


@cache
def _json_reference_columns():
    # Current definitions and retained operational evidence, excluding receipts/history of the creation itself.
    classes = {*CATALOGUES.values(), m.Policy, m.PolicySeedLine, m.TableView, m.PlannerView, m.Department, m.Workspace,
               m.Shift, m.Assignment, m.RequestRecord, m.SchedulingFact, m.EventOccurrence, m.ReviewRoute,
               m.CalendarFeed, m.CatalogueConversion, m.RuleSourceBinding, m.Publication}
    return {cls: tuple(column for column in cls.__table__.columns if isinstance(column.type, JSON))
            for cls in sorted(classes, key=lambda cls: cls.__tablename__)
            if any(isinstance(column.type, JSON) for column in cls.__table__.columns)}


def _json_strings(value):
    todo, found = [value], set()
    while todo:
        part = todo.pop()
        if isinstance(part, dict):
            todo.extend(part.keys())
            todo.extend(part.values())
        elif isinstance(part, list):
            todo.extend(part)
        elif isinstance(part, str):
            found.add(part)
    return found


def _require_json_unreferenced(db, row, reference_cache=None):
    columns = _json_reference_columns()
    bulk = reference_cache if reference_cache is not None else db.info.get('bulk')
    references = bulk.get('creation_json_references') if bulk is not None else None
    if references is None:
        references = defaultdict(set)
        if bulk is not None:
            bulk['creation_json_references'] = references
    targets = bulk.get('creation_undo_targets', db.info.get('created_undo_targets', {})) if bulk is not None else {}
    missing = ({row.id} | set().union(*targets.values())) - references.keys()
    if missing:
        ids = set(missing)
        references.update({ident: set() for ident in ids})
        queries = []
        for cls, fields in columns.items():
            for column in fields:
                query = select(literal(cls.__tablename__), cls.id, column)
                if len(ids) == 1:
                    query = query.where(cast(column, Text).contains(json.dumps(next(iter(ids)))[1:-1], autoescape=True))
                queries.append(query)
        # Scan batch history once; per-target LIKE predicates repeatedly scan and decompress every snapshot.
        with db.no_autoflush, db.execute(union_all(*queries).execution_options(
                yield_per=25, include_withdrawn=True)) as result:
            for table, ident, value in result:
                for target in _json_strings(value) & ids:
                    references[target].add((table, ident))
    changed = [obj for obj in set(db.new) | set(db.dirty) | set(db.deleted) if type(obj) in columns]
    changed_keys = {(obj.__tablename__, obj.id) for obj in changed}
    require(not references.get(row.id, set()) - changed_keys, 'INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE, 409)
    require(not any(row.id in _json_strings(getattr(obj, column.key))
                    for obj in changed if obj not in db.deleted for column in columns[type(obj)]),
            'INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE, 409)


def _creation_owned_tag_members(db, actor, row):
    """Only initial creation's immutable tag provenance permits removing its side effects."""
    if not isinstance(row, (m.Person, m.Tag)):
        return []
    person = isinstance(row, m.Person)
    members = tags.members(db, department_id=actor.department_id, **({"kind": "people"} if person else {}))
    target_field = 'member_id' if person else 'tag_id'
    mine = [member for member in members if getattr(member, target_field) == row.id]
    if not mine:
        return []
    targets = db.info.get('created_undo_targets', {}).get(type(row), {row.id}) | {row.id}
    ids = targets | {member.id for member in members if getattr(member, target_field) in targets}
    bulk = db.info.get('bulk')
    versions = bulk.setdefault('creation_tag_provenance', {}) if bulk is not None else {}
    missing = ids - versions.keys()
    if missing:
        versions.update(dict.fromkeys(missing))
        with db.no_autoflush:
            for version in db.scalars(select(m.DefinitionHistory).where(
                    m.DefinitionHistory.object_id.in_(missing), m.DefinitionHistory.object_revision == 1)):
                versions[version.object_id] = version
    for version in db.new:
        if isinstance(version, m.DefinitionHistory) and version.object_id in ids and version.object_revision == 1:
            versions[version.object_id] = version
    for kind, queued_row, reason, snapshot in (bulk or {}).get('history', []):
        if queued_row.id in ids and snapshot.get('revision') == 1:
            versions[queued_row.id] = SimpleNamespace(kind=kind, snapshot=snapshot, actor_id=actor.identity_id,
                department_id=actor.department_id, batch_id=bulk.get('batch_id'))
    creation = versions.get(row.id)
    if creation is None or creation.kind != ('people' if person else 'tags') or creation.actor_id != actor.identity_id:
        return []
    return [member for member in mine if member.revision == 1 and (version := versions.get(member.id))
            and version.kind == 'tag.member_add' and version.actor_id == creation.actor_id
            and version.department_id == creation.department_id == actor.department_id
            and version.batch_id == creation.batch_id
            and version.snapshot.get('created_with_person_id' if person else 'created_with_tag_id') == row.id
            and all(version.snapshot.get(key) == getattr(member, key)
                    for key in ('id', 'department_id', 'tag_id', 'kind', 'member_id', 'revision'))]


def _require_unreferenced(db, row, *, owned_tag_members=(), reference_cache=None):
    bulk = reference_cache if reference_cache is not None else db.info.get('bulk')
    pending = defaultdict(list)
    for obj in set(db.new) | set(db.dirty) | set(db.deleted):
        pending[obj.__table__].append(obj)
    # The rotation lifecycle removes its own link and preserves independently maintained mentorships.
    tables = tuple(entry for entry in _referencing_tables(type(row))
                   if not (isinstance(row, m.Rotation) and entry[0] is m.RotationAttachment.__table__))
    references = None
    if bulk is not None:
        if reference_cache is None and not db.info.get('creation_reference_events'):
            event.listen(db, 'after_flush', _clear_reference_cache)
            event.listen(db, 'after_soft_rollback', _clear_reference_cache)
            db.info['creation_reference_events'] = True
        references = bulk.setdefault('creation_references', {}).setdefault(type(row), {})
        if row.id not in references:
            targets = bulk.get('creation_undo_targets', db.info.get('created_undo_targets', {}))
            ids = targets.get(type(row), {row.id}) - references.keys()
            ids.add(row.id)
            references.update({ident: defaultdict(set) for ident in ids})
            with db.no_autoflush:
                for table, columns, scope in tables:
                    keys = tuple(table.primary_key.columns)
                    for result in db.execute(select(*keys, *columns).where(or_(*(column.in_(ids) for column in columns)),
                            *(table.c[key] == value for key, value in scope.items()))):
                        for ident in result[len(keys):]:
                            if ident in ids:
                                references[ident][table].add(tuple(result[:len(keys)]))
    for table, columns, scope in tables:
        ignored = {member.id for member in owned_tag_members} if table is m.TagMember.__table__ else set()
        changed = pending[table]
        require(not any(obj not in db.deleted and getattr(obj, 'id', None) not in ignored
                        and all(getattr(obj, key) == value for key, value in scope.items())
                        and any(getattr(obj, column.key) == row.id for column in columns)
                        for obj in changed), 'INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE, 409)
        if references is not None:
            changed_keys = {inspect(obj).identity or tuple(getattr(obj, key.key) for key in table.primary_key.columns)
                            for obj in changed}
            require(not references[row.id][table] - changed_keys - {(ident,) for ident in ignored},
                    'INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE, 409)
            continue
        query = select(1).select_from(table).where(or_(*(column == row.id for column in columns)),
            *(table.c[key] == value for key, value in scope.items()))
        if 'id' in table.c and (known := [obj.id for obj in changed if obj.id] + list(ignored)):
            query = query.where(table.c.id.not_in(known))
        require(db.scalar(query.limit(1)) is None, 'INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE, 409)
    _require_json_unreferenced(db, row, reference_cache)


def undo_created(db, actor, command):
    membership = command.kind == 'membership.undo_create'
    require_role(actor, *(('admin', 'coordinator') if membership else ('credentialer',)))
    value = s.CreatedUndo.model_validate(command.payload)
    cls = m.Membership if membership else m.Eligibility if command.kind == "eligibility.undo_create" else m.CapabilityGrant
    if membership:
        for item in value.items:
            row = scoped(db, cls, item.id, actor)
            expected(row, item.revision)
            require(row.revision == 1, 'STALE_REVISION', 'This record changed after creation; undo its later changes first', 409)
            creator = _creation_actor(db, row)
            require(creator == actor.identity_id, 'FORBIDDEN', 'Only the person who created this record can undo it', 403)
            _require_unreferenced(db, row)
    removed_ids = []
    removed, kept = remove_created(db, actor, cls, command.kind, [i.model_dump() for i in value.items], value.reason, removed_ids=removed_ids)
    return {"removed": removed, "kept": kept, "removed_ids": removed_ids,
            "message": f"{removed} removed" + (f"; {kept} changed since and kept" if kept else "") + "."}


def undo_catalogue_created(db, actor, command, *, permission=None):
    class Undo(s.Schema):
        catalogue: str
        reason: s.Reason
    value = Undo.model_validate(command.payload)
    require(value.catalogue in CATALOGUES, 'UNKNOWN_CATALOGUE', 'Unsupported catalogue')
    score_check_add = permission == 'score_check.add_missing'
    require(permission is None or (score_check_add and value.catalogue in {'people', 'services'}),
            'FORBIDDEN', 'This permission only undoes people or services from a score-check gap', 403)
    if score_check_add:
        require_permission(actor, permission)
    else:
        from apps.api.commands import command_roles
        require_role(actor, *command_roles(command))
    cls = CATALOGUES[value.catalogue]
    row = scoped(db, cls, command.target_id, actor)
    if value.catalogue in PROGRAMME_CATALOGUES:
        programme_write_access(db, actor, value.catalogue, row)
    expected(row, command.expected_revision)
    require(row.revision == 1, 'STALE_REVISION', 'This record changed after creation; undo its later changes first', 409)
    creator = _creation_actor(db, row)
    require(creator == actor.identity_id, 'FORBIDDEN', 'Only the person who created this record can undo it', 403)
    if value.catalogue == 'contact_types':
        return write_catalogue(db, actor, s.Command(kind='catalogue.update', target_id=row.id,
            expected_revision=row.revision, payload={'catalogue': value.catalogue, 'reason': value.reason,
                'value': {**{key: record(row)[key] for key in s.ContactTypeInput.model_fields}, 'active': False}}), update=True)
    if isinstance(row, m.Tag):
        require(not tags.usage(db, actor.department_id, [row], include_workspace=True, include_retired=True)[row.id]['rules'],
                'INTEGRITY_CONFLICT', INTEGRITY_CONFLICT_MESSAGE, 409)
    owned_tags = _creation_owned_tag_members(db, actor, row)
    _require_unreferenced(db, row, owned_tag_members=owned_tags)
    if isinstance(row, m.Tag):
        tags.apply_members(db, actor, row, set(), {(member.kind, member.member_id) for member in owned_tags}, value.reason)
    else:
        for member in owned_tags:
            tags.remove_member(db, actor, member, value.reason)
    affected_counts = []
    removed, kept = remove_created(db, actor, cls, command.kind,
                                   [{'id': row.id, 'revision': row.revision}], value.reason, affected_counts=affected_counts)
    return {'removed': removed, 'kept': kept, 'removed_ids': [row.id] if removed else [],
            'affected_count': sum(affected_counts) + len(owned_tags)}


def programme_eligibility(db, actor, command):
    """Credentialer-reviewed eligibility for the exact dated programme roster."""
    require_role(actor, "credentialer")
    value = s.ProgrammeEligibilityInput.model_validate(command.payload)
    programme = scoped(db, m.Programme, value.programme_id, actor)
    require(programme.starts_on <= value.starts_on and (programme.ends_on is None or value.ends_on <= programme.ends_on),
            'PROGRAMME_WINDOW', 'The selected dates must be within the programme dates')
    for service_id in value.service_ids:
        scoped(db, m.Service, service_id, actor)
    members = {r.person_id for r in programme_memberships(db, actor, {value.programme_id})
               if r.starts_on <= value.starts_on and (r.ends_on is None or r.ends_on >= value.ends_on)}
    require(members == set(value.person_ids), 'PROGRAMME_ROSTER_CHANGED',
            'The programme roster changed. Review the people and dates again.', 409)
    require(len(members) * len(value.service_ids) <= 1000, 'ELIGIBILITY_BATCH_LIMIT',
            'Choose fewer people or services per change', 409)
    for person_id in members:
        person_access(db, actor, person_id)
    independence.ensure(db, actor, "skills.self", actor.person_id not in members,
                        "Service eligibility cannot be self-attested", value.reason, value.programme_id)
    _, states = _relationship_state(db, actor, m.Eligibility, members, 'service_id', value.service_ids,
                                    value.starts_on, value.ends_on)
    created = skipped = 0
    created_ids = []
    for person_id in sorted(members):
        for service_id in value.service_ids:
            state = states.get((person_id, service_id))
            if state == 'covered':
                skipped += 1
                continue
            require(state != 'overlap',
                    'ELIGIBILITY_OVERLAP', 'A person already has a partially overlapping permission. Review that record first.', 409)
            row = m.Eligibility(department_id=actor.department_id, person_id=person_id,
                                service_id=service_id, starts_on=value.starts_on, ends_on=value.ends_on)
            db.add(row)
            db.flush()
            history(db, actor, command.kind, row, value.reason)
            created += 1
            created_ids.append(row.id)
    if created:
        bump_inputs(db, actor)
    return {'people_count': len(members), 'created_count': created, 'already_current_count': skipped, 'created_ids': created_ids}


def end_relationship(db, actor, command):
    class End(s.Schema):
        ends_on: s.date
        reason: s.Reason
    class Revoke(s.RelationshipUpdateDates):
        ends_on: s.date | None
    value = (Revoke if command.kind == 'grant.revoke' else End).model_validate(command.payload)
    cls = {"membership.end": m.Membership, "eligibility.end": m.Eligibility, "grant.revoke": m.CapabilityGrant}[command.kind]
    require_role(actor, *(('admin', 'coordinator') if cls is m.Membership else ('credentialer',)))
    row = scoped(db, cls, command.target_id, actor)
    expected(row, command.expected_revision)
    if cls is m.CapabilityGrant:
        require(not row.revoked, 'WORKFLOW_STATE', 'This skill has already been revoked', 409)
    starts_on = value.starts_on if cls is m.CapabilityGrant and value.starts_on is not None else row.starts_on
    require(value.ends_on is None or value.ends_on > starts_on, "INVALID_INTERVAL", "End must follow start")
    if cls is not m.CapabilityGrant and row.ends_on is not None and value.ends_on > row.ends_on:
        schema = s.MembershipInput if cls is m.Membership else s.EligibilityInput
        _relationship_value(db, actor, command.kind.split('.')[0],
            {**{key: getattr(row, key) for key in schema.model_fields}, 'ends_on': value.ends_on}, row.id, value.reason)
    previous = json_value({'starts_on': row.starts_on, 'ends_on': row.ends_on})
    row.ends_on = value.ends_on
    if cls is m.CapabilityGrant:
        # Revocation records known invalid evidence immediately; it is not a future expiry.
        row.starts_on = starts_on
        row.revoked = True
    row.revision += 1
    history(db, actor, command.kind, row, value.reason)
    bump_inputs(db, actor)
    reverse = 'grant.restore' if cls is m.CapabilityGrant else command.kind.split('.')[0] + '.update_dates'
    from .credential_impact import affected
    impact = affected(db, actor, row, notify=True) if cls is m.Eligibility else None
    return {"record": record(row), 'previous_value': previous,
            **({'published_impact': impact} if impact is not None else {}),
            'undo': {'kind': reverse, 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {**previous, 'reason': value.reason}}}


def withdraw_records(db, actor, kind, rows, reason, *, withdrawn):
    """One retained-row lifecycle for dated relationships and rotations, including their history and Undo."""
    for row in rows:
        row.withdrawn = withdrawn
        row.revision += 1
        if isinstance(row, m.Rotation):
            db.expire(row, ['attachment_link'])
    if rows:
        history_many(db, actor, [(kind, row, reason) for row in rows])
        db.flush()  # Later bulk commands must see the live flags in Core reads too.
        bump_inputs(db, actor)


def withdraw_relationship(db, actor, command):
    """Withdraw an upcoming relationship without deleting its identity or frozen evidence; Undo restores it."""
    value = s.Decision.model_validate(command.payload)
    family, action = command.kind.split('.')
    cls, schema = {'membership': (m.Membership, s.MembershipInput),
                   'eligibility': (m.Eligibility, s.EligibilityInput),
                   'programme_membership': (m.ProgrammeMembership, s.ProgrammeMembershipInput)}[family]
    permission = 'catalogue.programmes' if cls is m.ProgrammeMembership else family + '.withdraw'
    require_permission(actor, permission)
    restoring = action == 'restore'
    row = scoped(db, cls, command.target_id, actor, include_withdrawn=restoring)
    if cls is m.ProgrammeMembership:
        programme_write_access(db, actor, 'programme_memberships', row)
    expected(row, command.expected_revision)
    values = json_value({key: getattr(row, key) for key in schema.model_fields})
    if restoring:
        require(row.withdrawn, 'RELATIONSHIP_NOT_WITHDRAWN', 'This record is already present; reload its current revision', 409)
        if cls is m.ProgrammeMembership:
            validate_references(db, actor, 'programme_memberships', schema.model_validate(values), row.id)
        else:
            _relationship_value(db, actor, family, values, row.id, value.reason)
    else:
        today = independence.today(db, actor.department_id)
        require(row.starts_on > today, 'WITHDRAWAL_ALREADY_STARTED', 'This record has started; end it instead', 409)
    withdraw_records(db, actor, command.kind, [row], value.reason, withdrawn=not restoring)
    from .credential_impact import affected
    impact = affected(db, actor, row, notify=True) if cls is m.Eligibility else None
    return {**({'restored_id': row.id} if restoring else {'removed_id': row.id}),
            'record': record(row), 'previous_value': values,
            **({'published_impact': impact} if impact is not None else {}),
            'undo': {'kind': family + ('.withdraw' if restoring else '.restore'), 'target_id': row.id,
                     'expected_revision': row.revision, 'payload': {'reason': value.reason}}}


def restore_grant(db, actor, command):
    require_role(actor, 'credentialer')
    row = scoped(db, m.CapabilityGrant, command.target_id, actor)
    expected(row, command.expected_revision)
    require(row.revoked, 'WORKFLOW_STATE', 'This skill has not been revoked', 409)
    patch = s.RelationshipUpdateDates.model_validate(command.payload)
    values = {key: getattr(row, key) for key in s.GrantInput.model_fields}
    _, value = _relationship_value(db, actor, 'grant',
        {**values, **patch.model_dump(include={'starts_on', 'ends_on'}, exclude_unset=True)}, row.id, patch.reason)
    previous = json_value({'starts_on': row.starts_on, 'ends_on': row.ends_on})
    row.starts_on, row.ends_on, row.revoked = value.starts_on, value.ends_on, False
    row.revision += 1
    history(db, actor, command.kind, row, patch.reason)
    bump_inputs(db, actor)
    return {'record': record(row), 'previous_value': previous,
            'undo': {'kind': 'grant.revoke', 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {**previous, 'reason': patch.reason}}}


def extend_eligibility(db, actor, command):
    """Credentialer: a clinic permission runs longer, to a later date or until changed (empty end). Undo: End it on
    its earlier date. Refused if another record for the same person and clinic would overlap."""
    class Extend(s.Schema):
        ends_on: s.date | None = None
        reason: s.Reason
    value = Extend.model_validate(command.payload)
    require_role(actor, "credentialer")
    row = scoped(db, m.Eligibility, command.target_id, actor)
    expected(row, command.expected_revision)
    previous = json_value({'starts_on': row.starts_on, 'ends_on': row.ends_on})
    require(row.ends_on is not None and (value.ends_on is None or value.ends_on > row.ends_on), "INVALID_INTERVAL",
            "Choose a later end, or none (until changed)")
    _relationship_value(db, actor, 'eligibility',
        {**{key: getattr(row, key) for key in s.EligibilityInput.model_fields}, 'ends_on': value.ends_on}, row.id, value.reason,
        overlap_message="A later record already covers part of this period")
    row.ends_on = value.ends_on
    row.revision += 1
    history(db, actor, "eligibility.extend", row, value.reason)
    bump_inputs(db, actor)
    return {"record": record(row), 'previous_value': previous,
            'undo': {'kind': 'eligibility.update_dates', 'target_id': row.id, 'expected_revision': row.revision,
                     'payload': {**previous, 'reason': value.reason}}}


def eligibility_gaps(db, department_id):
    """Clinics that nobody may work at after a date while a mandatory staffing level in force still requires people
    there after it: the permissions that end on that last date (department data, e.g. a trial month's permissions).
    Each: {id, revision, person_id, service_id, starts_on, ends_on}. Opening them (until changed) is a credentialer's
    call. (A clinic a target or a preference merely counts is not a gap: rotators' dated permissions end with them.)"""
    rows = list(db.scalars(select(m.Eligibility).where(m.Eligibility.department_id == department_id)))
    from datetime import date
    from types import SimpleNamespace
    from packages.domain.conditions import active_rules, with_tags
    from apps.api.snapshots import scheduling_inputs, tag_data
    data = {**scheduling_inputs(db, [department_id]), "tags": tag_data(db, [department_id]),
            "policies": [record(p) for p in db.scalars(select(m.Policy).where(m.Policy.department_id == department_id))]}
    # Approved retired destination rows retain lineage; only active resolved minimum-cover copies bind.
    with_tags(data)
    rules = [SimpleNamespace(definition=c["definition"], ends_on=date.fromisoformat(str(c["ends_on"])) if c.get("ends_on") else None)
             for c in active_rules(data) if c["kind"] == "minimum_cover" and c["definition"].get("severity", "mandatory") == "mandatory"]

    def clinics(p):
        d = p.definition
        scope = d.get("scope") or {}
        found = set(scope.get("service_ids") or []) | ({scope["service_id"]} if scope.get("service_id") else set())
        return found | {c for r in d.get("per_person") or [] for c in r.get("service_ids") or []}
    out = []
    for service_id in {r.service_id for r in rows}:
        here = [r for r in rows if r.service_id == service_id]
        if any(r.ends_on is None for r in here):
            continue
        last = max(r.ends_on for r in here)
        if any(service_id in clinics(p) and (p.ends_on is None or p.ends_on > last) for p in rules):
            out += [{"id": r.id, "revision": r.revision, "person_id": r.person_id, "service_id": r.service_id,
                     "starts_on": r.starts_on, "ends_on": r.ends_on} for r in here if r.ends_on == last]
    return sorted(out, key=lambda r: (r["service_id"], r["person_id"]))


def closure(db, actor, command):
    require_role(actor, "admin", "coordinator")
    value = s.ClosureInput.model_validate(command.payload)
    scoped(db, m.Location, value.location_id, actor)
    require((value.end - value.start).days <= 366, "INTERVAL_TOO_LONG", "Closure intervals are bounded to one year")
    row = m.LocationClosure(department_id=actor.department_id, **value.model_dump())
    db.add(row)
    db.flush()
    bump_inputs(db, actor)
    return {"record": record(row)}
