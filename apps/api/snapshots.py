"""Build version-bound pure domain inputs from canonical relational records."""
import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Mapping
from packages.contracts.schemas import GENERATION_RUNTIME_OPTIONS
from datetime import date, timedelta
from operator import itemgetter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from sqlalchemy import false, func, or_, select, tuple_
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import defer, object_session
from . import models as m
from .serialization import json_value, record, records_many, rows_many, shift_link_rows
from packages.domain.conditions import with_tags
from packages.domain.time import aware, local_midnight
from packages.domain.recurrence import SERIES_STATE_FIELDS, with_series_state
from packages.domain.overlap import overlap_dates
from packages.domain.serialization import json_default
from packages.domain.validation import ENGINE_VERSION
from .settings import Settings, quota_policy_limit


class DomainSnapshot(dict):
    """A snapshot mapping with read-only diagnostics kept outside its input keys."""
    def __init__(self, *args, read_quality=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_quality = read_quality if read_quality is not None else {}


# One registry owns the tables that can change a prepared snapshot's inputs.
# ``shape`` describes how the row is projected by ``_snapshot_inputs``; rows
# with ``shape=None`` are read by the live domain-snapshot stages below.
# ``scope`` is used to build the bounded source predicates for the shared
# projection.  The revision listener and its structural guard consume this
# same registry, so a new source cannot silently escape invalidation.
SNAPSHOT_INPUT_SOURCE_SPECS = (
    # Shared department catalogue and governed records.
    ('operating_calendars', m.OperatingCalendar, 'general', 'department', 'keyed', frozenset()),
    ('resource_pools', m.ResourcePool, 'general', 'department', 'keyed', frozenset()),
    ('categories', m.Category, 'general', 'department', 'keyed', frozenset()),
    ('capabilities', m.Capability, 'general', 'department', 'keyed', frozenset()),
    ('location_types', m.LocationType, 'general', 'department', 'keyed', frozenset()),
    ('locations', m.Location, 'general', 'department', 'keyed', frozenset()),
    ('services', m.Service, 'general', 'department', 'keyed', frozenset()),
    ('types', m.AssignmentType, 'general', 'department', 'keyed', frozenset()),
    ('programmes', m.Programme, 'general', 'department', 'keyed', frozenset()),
    ('rotation_types', m.RotationType, 'general', 'department', 'keyed', frozenset()),
    ('cohorts', m.Cohort, 'general', 'department', 'keyed', frozenset()),
    ('memberships', m.Membership, 'general', 'department', 'listed', frozenset()),
    ('eligibilities', m.Eligibility, 'general', 'department', 'listed', frozenset()),
    ('grants', m.CapabilityGrant, 'general', 'department', 'listed', frozenset()),
    ('closures', m.LocationClosure, 'general', 'department', 'listed', frozenset()),
    ('policies', m.Policy, 'general', 'department', 'listed', frozenset()),
    ('exceptions', m.PolicyException, 'general', 'department', 'listed', frozenset()),
    ('recurring_series', m.RecurringSeries, 'general', 'department', 'listed', frozenset()),
    ('programme_memberships', m.ProgrammeMembership, 'general', 'department', 'listed', frozenset()),
    ('mentorships', m.Mentorship, 'general', 'department', 'listed', frozenset()),
    ('scheduling_facts', m.SchedulingFact, 'general', 'department', 'scheduling', frozenset()),
    ('catalogue_conversions', m.CatalogueConversion, 'general', 'department', 'scheduling', frozenset()),
    ('type_lead_ownership', m.TypeLeadOwnership, 'general', 'department', 'scheduling', frozenset()),
    ('policy_seed_lines', m.PolicySeedLine, 'general', 'department', 'scheduling', frozenset()),
    ('source_bindings', m.RuleSourceBinding, 'general', 'department', 'scheduling', frozenset()),
    ('date_sets', m.DateSet, 'general', 'department', 'date_set', frozenset()),
    ('tags', m.Tag, 'general', 'department', 'tag', frozenset()),
    ('tag_members', m.TagMember, 'general', 'department', 'tag_member', frozenset()),
    ('people', m.Person, 'general', 'organisation', 'people', frozenset()),
    ('departments', m.Department, 'general', 'organisation', 'departments', frozenset({'revision', 'catalogue_revision', 'events_checked_on',
        *(f'generation_defaults.{key}' for key in GENERATION_RUNTIME_OPTIONS)})),
    ('programme_calendar_entries', m.ProgrammeCalendarEntry, 'general', 'department', None, frozenset()),
    ('rotations', m.Rotation, 'general', 'department', None, frozenset()),
    # Other rotas read only these lifecycle/input fields for external schedule
    # and wish-budget context; all writer metadata remains excluded.
    ('workspaces', m.Workspace, 'general', 'department', None, frozenset({
        'access_version_id', 'name', 'origin', 'content_revision', 'created_by', 'last_edited_by',
        'base_publication_id', 'submitted_fingerprint', 'submitted_at', 'review_snapshot', 'urgency',
        'urgency_reason', 'target_overrides', 'rule_adjustments', 'accepted_issues', 'archived_by',
        'archive_reason', 'archive_state', 'return_note', 'replaces_publication_id',
        'id', 'revision', 'created_at', 'department_id', 'validation'})),
    ('planning_group_versions', m.PlanningGroupVersion, 'general', 'department', None, frozenset()),
    # Live stages that are part of the domain input even though they are not
    # copied into the shared catalogue projection above.
    ('organisation', m.Organisation, 'general', 'organisation', None, frozenset({'revision', 'input_revision', 'availability_revision'})),
    ('publications', m.Publication, 'general', 'department', None, frozenset()),
    ('staff_wishes', m.StaffWish, 'general', 'department', None, frozenset()),
    ('wish_overrides', m.WishOverride, 'general', 'department', None, frozenset()),
    ('rotation_attachments', m.RotationAttachment, 'general', None, None, frozenset()),
    ('fairness_ledger', m.FairnessLedger, 'general', 'department', None, frozenset()),
    ('wish_ledger', m.WishLedger, 'general', 'department', None, frozenset()),
    # Leave/absence and protected-time inputs use the separate availability
    # revision so consumers can invalidate the narrower availability layer.
    ('availability', m.Availability, 'availability', 'department', None, frozenset()),
    ('requests', m.RequestRecord, 'availability', 'department', None, frozenset()),
    ('publication_changes', m.PublicationChangeList, 'availability', 'department', None, frozenset()),
    ('role_bindings', m.RoleBinding, 'availability', 'department', None, frozenset()),
    ('identities', m.Identity, 'availability', 'person', None, frozenset({
        'revision', 'created_at', 'issuer', 'subject', 'subject_normalized', 'display_name', 'password_hash', 'last_sign_in_at'})),
    ('availability_patterns', m.AvailabilityPattern, 'availability', 'department', None, frozenset()),
    ('events', m.Event, 'availability', 'department', None, frozenset()),
    ('event_occurrences', m.EventOccurrence, 'availability', 'department', None, frozenset()),
    ('announcement_receipts', m.AnnouncementReceipt, 'availability', None, None, frozenset({'revision', 'delivered_at', 'read_at', 'acknowledged_at'})),
)


def snapshot_input_specs():
    """Return the canonical snapshot-input registry for guards and writers."""
    return SNAPSHOT_INPUT_SOURCE_SPECS


def snapshot_input_classes():
    return {cls for _, cls, *_ in SNAPSHOT_INPUT_SOURCE_SPECS}


def snapshot_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def session_key(row):
    """Session demand, independent of assigned people and cloned record IDs."""
    value = {key: row[key] for key in ('assignment_type_id', 'type_revision', 'type_snapshot',
                                      'location_id', 'attributes', 'cancelled')}
    value['service_ids'] = sorted(row['service_ids'])
    value.update({key: aware(row[key]).isoformat() for key in ('start', 'end')})
    return snapshot_hash(value)


def session_match_groups(rows, published):
    """Exact demand groups on both sides; callers may only map singleton pairs."""
    from packages.domain.recurrence import inactive
    groups, prior = defaultdict(list), defaultdict(list)
    for row in rows:
        groups[session_key(row)].append(row)
    for old in published:
        if not inactive(old):
            prior[session_key(old)].append(old)
    for key, old in prior.items():
        yield old, groups.get(key, [])


def mapped_score_baseline(rows, published, details=None):
    from packages.domain.score import flags
    if published is None:
        return None
    baseline, matched, unmatched, ambiguous = {}, 0, 0, 0
    for old, choices in session_match_groups(rows, published):
        if not choices:
            unmatched += len(old)
        elif len(choices) != 1 or len(old) != 1:
            ambiguous += len(old)
        else:
            matched += 1
            baseline.update({(choices[0]['id'], p['person_id']): flags(p) for p in old[0]['participants']})
    if details is not None:
        details.update(state='partial' if matched and (unmatched or ambiguous) else 'available' if matched else 'unavailable',
                       matched_sessions=matched, unmatched_sessions=unmatched, ambiguous_sessions=ambiguous)
    return baseline if matched else None


def publication_score_baseline(db, workspace, rows, own_publication=None):
    """Freeze the named publication's assignments as scoring evidence, never as draft assignments."""
    if workspace.status in ('published', 'superseded'):
        publication = own_publication or db.scalar(select(m.Publication).where(m.Publication.workspace_id == workspace.id))
        # Historical publications predating this input retain their original scoring semantics.
        return json_value(publication.snapshot.get('score_baseline')) if publication else None
    if workspace.origin not in ('import', 'auto') or not workspace.base_publication_id:
        return None
    publication = db.get(m.Publication, workspace.base_publication_id)
    details = {}
    # The retained snapshot is immutable; later effective-range trimming cannot change this baseline.
    baseline = mapped_score_baseline(rows, publication.snapshot['shifts'], details)
    return {'publication_id': publication.id, **details, 'assignments': None if baseline is None else [
        {'shift_id': sid, 'person_id': pid, 'team_lead': lead, 'overtime': overtime}
        for (sid, pid), (lead, overtime) in sorted(baseline.items())]}


def workspace_shifts(db, workspace_id: str) -> list[dict]:
    """Every session with its clinics and people, acquired in bounded batches."""
    return shift_records(db, db.scalars(select(m.Shift).where(m.Shift.workspace_id == workspace_id).order_by(m.Shift.start, m.Shift.id)))


def shift_records(db, rows, *, include_participants=True) -> list[dict]:
    """Attach clinics and optionally people in batches to ORM sessions or selected session fields."""
    rows = list(rows)
    ids = [row['id'] if isinstance(row, Mapping) else row.id for row in rows]
    workspaces = {row.get('workspace_id') if isinstance(row, Mapping) else row.workspace_id for row in rows}
    # Link visibility is evaluated again in SQL, within the scope of the selected rows.
    options = {'shift_workspace_id': next(iter(workspaces))} if len(workspaces) == 1 else None
    services, people = {i: [] for i in ids}, {i: [] for i in ids}
    for chunk in (ids[k:k + 1000] for k in range(0, len(ids), 1000)):
        for shift_id, service_id, assignment in shift_link_rows(db, chunk, include_participants=include_participants,
                                                               execution_options=options):
            if assignment is None:
                services[shift_id].append(service_id)
            else:
                people[shift_id].append(assignment)
    by_person = itemgetter('person_id')
    for assignments in people.values():
        assignments.sort(key=by_person)
    result = []
    for row, sid in zip(rows, ids):
        if isinstance(row, RowMapping):
            # Selected columns already own their decoded JSON. Format only the typed dates.
            shift = {key: json_default(value) if isinstance(value, date) else value for key, value in row.items()}
        elif isinstance(row, Mapping):
            shift = json_value(dict(row))
        else:
            shift = record(row)
        shift.update(service_ids=sorted(services[sid]), participants=people[sid])
        result.append(shift)
    return result


def shift_record(db, row) -> dict:
    result = record(row)
    result["service_ids"] = sorted(db.scalars(select(m.ShiftService.service_id).where(m.ShiftService.shift_id == row.id)))
    result["participants"] = [record(p) for p in db.scalars(select(m.Assignment).where(m.Assignment.shift_id == row.id).order_by(m.Assignment.person_id))]
    return result


def frozen_series_states(rows, publication):
    if not any((row.get('series_snapshot') or {}).get('definition', {}).get(key) for row in rows
               for key in ('usual_assignees_fixed', 'close_when_all_usual_unavailable')):
        return rows
    frozen = {row['id']: row for row in publication.snapshot.get('shifts', []) if row.get('series_state_frozen')}
    return [{**row, **{key: frozen[row['id']].get(key) for key in SERIES_STATE_FIELDS}, 'series_state_frozen': True}
            if row['id'] in frozen else row for row in rows]


def project_series_shifts(db, workspace, rows, *, external_out=None, shared_inputs=None, window=None):
    if not rows:
        return rows
    from packages.domain.overlap import _conflict, evaluate_overlaps, per_session_overlaps
    # Recurrence closure and reservations need absence/rotation facts, never rules, wishes or fairness.
    data = overlap_projection_inputs(db, workspace, rows, window=window)
    focus = set(data['focus_ids'])
    found = evaluate_overlaps(data, include_fixed=True)
    if found:
        people = {row['person_id'] for row in found}
        services = {sid for row in found for key in ('clinic_ids', 'other_clinic_ids') for sid in row[key]}
        data['people'] = {pid: {'id': pid, 'display_name': name} for pid, name in db.execute(
            select(m.Person.id, m.Person.display_name).where(m.Person.id.in_(people)))}
        data['services'] = {sid: {'id': sid, 'name': name} for sid, name in db.execute(
            select(m.Service.id, m.Service.name).where(m.Service.id.in_(services)))}
        shifts = {row['id']: row for row in data['shifts']}
        found = [_conflict(data, row['person_id'], shifts[row['shift_id']], shifts.get(row['other_shift_id']),
            aware(row['start']), aware(row['end']), row['code'], row['availability_id']) for row in found]
    conflicts = per_session_overlaps(data, found)
    if external_out is not None and focus and not data.get('quota_context', {}).get('limit_exceeded'):
        lo, hi = local_midnight(workspace.starts_on, data['organisation']['timezone']), local_midnight(
            workspace.ends_on, data['organisation']['timezone'])
        external_out.append([row for row in data['shifts'] if row['workspace_id'] != workspace.id
                             and aware(row['start']) < hi and aware(row['end']) > lo])
    return [{**row, 'double_booking_conflicts': conflicts[row['id']]} if conflicts.get(row['id']) else row
            for row in data['shifts'] if row['id'] in focus]


def overlap_projection_inputs(db, workspace, rows, *, window=None):
    from types import SimpleNamespace
    from .services.availability_patterns import expand
    from .services.schedule import in_force_shifts
    org = db.get(m.Organisation, db.get(m.Department, workspace.department_id).organisation_id)
    departments = list(db.scalars(select(m.Department.id).where(m.Department.organisation_id == org.id)))
    if workspace.status == 'published':
        publication = db.execute(select(m.Publication.effective_until, m.Publication.snapshot['shifts']).where(
            m.Publication.workspace_id == workspace.id)).first()
        if publication is not None:
            rows = in_force_shifts(SimpleNamespace(effective_until=publication.effective_until), rows, org.timezone)
            rows = frozen_series_states(rows, SimpleNamespace(snapshot={'shifts': publication[1] or []}))
    data = {'organisation': {'timezone': org.timezone}, 'shifts': rows, 'focus_ids': [row['id'] for row in rows]}
    if not rows:
        return data
    # Wishes reuse this official-work context even on rota days without an own session.
    lo = min(window[0] if window else local_midnight(workspace.starts_on, org.timezone), min(aware(row['start']) for row in rows))
    hi = max(window[1] if window else local_midnight(workspace.ends_on, org.timezone), max(aware(row['end']) for row in rows))
    publications, query = external_schedule(db, workspace, departments, lo, hi, org.timezone)
    external = shift_records(db, db.scalars(query)) if publications else []
    by_workspace = {}
    for row in external:
        by_workspace.setdefault(row['workspace_id'], []).append(row)
    frozen_ids = {wid for wid, shifts in by_workspace.items() if any(
        (row.get('series_snapshot') or {}).get('definition', {}).get(key)
        for row in shifts for key in ('usual_assignees_fixed', 'close_when_all_usual_unavailable'))}
    evidence = dict(db.execute(select(m.Publication.workspace_id, m.Publication.snapshot['shifts']).where(
        m.Publication.id.in_([pub.id for pub in publications if pub.workspace_id in frozen_ids]))).all()) if frozen_ids else {}
    frozen = {row['id']: row for wid in frozen_ids for row in frozen_series_states(
        by_workspace[wid], SimpleNamespace(snapshot={'shifts': evidence.get(wid) or []}))}
    data['shifts'] = rows + [frozen.get(row['id'], row) for row in external]
    people = {p['person_id'] for row in data['shifts'] for p in row['participants']}
    people |= {pid for row in data['shifts'] for pid in
               (row.get('series_snapshot') or {}).get('definition', {}).get('default_person_ids', [])}
    lo = min(lo, min(aware(row['start']) for row in data['shifts']))
    hi = max(hi, max(aware(row['end']) for row in data['shifts']))
    data['availability'] = [record(row, exclude=('private_reason',)) for row in db.scalars(select(m.Availability).where(
        m.Availability.person_id.in_(people), m.Availability.start < hi, m.Availability.end > lo,
        m.Availability.retracted.is_(False), m.Availability.counts_as_unavailable.is_(True)))]
    data['availability'] += expand(db, departments, list(people), lo, hi, org.timezone)
    data['rotations'] = [record(row) for row in db.scalars(select(m.Rotation).where(
        m.Rotation.department_id.in_(departments), m.Rotation.person_id.in_(people)))]
    from .services.published_changes import staged_availability
    data['availability'] += staged_availability(db, workspace)
    return with_series_state(data)


def inactive_series_shift_ids(db, selected_ids):
    """Draft absence never closes a place; publications retain their issued closure status."""
    from packages.domain.recurrence import inactive
    S = m.Shift
    selected = (S.id.in_(selected_ids),
        S.series_snapshot['definition']['close_when_all_usual_unavailable'].as_boolean().is_(True))
    # D38 leaves every draft occurrence open through absence. Only publications can retain an issued closure.
    if not db.scalar(select(S.id).join(m.Workspace, m.Workspace.id == S.workspace_id).where(
            *selected, m.Workspace.status == 'published').limit(1)):
        return set(db.scalars(select(S.id).where(*selected, S.cancelled.is_(True))))
    rows = db.scalars(select(S).where(S.id.in_(selected_ids),
        S.series_snapshot['definition']['close_when_all_usual_unavailable'].as_boolean().is_(True)))
    by_workspace = {}
    for row in shift_records(db, rows):
        by_workspace.setdefault(row['workspace_id'], []).append(row)
    workspaces = {w.id: w for w in db.scalars(select(m.Workspace).where(m.Workspace.id.in_(by_workspace)))} if by_workspace else {}
    if not workspaces:
        return set()
    from types import SimpleNamespace
    from .services.availability_patterns import expand
    from .services.schedule import in_force_shifts
    D = m.Department
    departments = dict(db.execute(select(D.id, D.organisation_id).where(D.organisation_id.in_(
        select(D.organisation_id).where(D.id.in_({w.department_id for w in workspaces.values()}))))).all())
    organisations = {org.id: org for org in db.scalars(select(m.Organisation).where(
        m.Organisation.id.in_({departments[w.department_id] for w in workspaces.values()})))}
    publications = {wid: (until, shifts) for wid, until, shifts in db.execute(select(
        m.Publication.workspace_id, m.Publication.effective_until, m.Publication.snapshot['shifts']).where(
            m.Publication.workspace_id.in_([w.id for w in workspaces.values() if w.status == 'published'])))}
    grouped = {}
    for wid, shifts in by_workspace.items():
        org = organisations[departments[workspaces[wid].department_id]]
        if wid in publications:
            until, frozen = publications[wid]
            shifts = frozen_series_states(in_force_shifts(SimpleNamespace(effective_until=until), shifts, org.timezone),
                SimpleNamespace(snapshot={'shifts': frozen or []}))
        grouped.setdefault(org.id, []).extend(shifts)
    inactive_ids = set()
    for oid, shifts in grouped.items():
        if not shifts:
            continue
        org = organisations[oid]
        dept_ids = [did for did, key in departments.items() if key == oid]
        people = {pid for row in shifts for pid in
            (row.get('series_snapshot') or {}).get('definition', {}).get('default_person_ids', [])}
        lo, hi = min(aware(row['start']) for row in shifts), max(aware(row['end']) for row in shifts)
        absence = [record(row, exclude=('private_reason',)) for row in db.scalars(select(m.Availability).where(
            m.Availability.person_id.in_(people), m.Availability.start < hi, m.Availability.end > lo,
            m.Availability.retracted.is_(False), m.Availability.counts_as_unavailable.is_(True)))]
        absence += expand(db, dept_ids, list(people), lo, hi, org.timezone)
        rotations = [record(row) for row in db.scalars(select(m.Rotation).where(
            m.Rotation.department_id.in_(dept_ids), m.Rotation.person_id.in_(people)))]
        data = with_series_state({'organisation': {'timezone': org.timezone}, 'shifts': shifts,
                                  'availability': absence, 'rotations': rotations})
        inactive_ids.update(row['id'] for row in data['shifts'] if inactive(row))
    return inactive_ids


def workspace_content(db, workspace) -> dict:
    return {"workspace": record(workspace, exclude=("validation", "submitted_fingerprint")),
            "shifts": workspace_shifts(db, workspace.id)}


def precondition_snapshot(db, workspace):
    """Acquire current preview inputs once per unchanged rota in this transaction."""
    held = db.info.get('_precondition_snapshot') or {}
    if (held.get('workspace_id'), held.get('content_revision')) == (workspace.id, workspace.content_revision):
        return held['data']
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    inputs = snapshot_inputs(db, organisation)
    content = workspace_content(db, workspace)
    data = domain_snapshot(db, workspace, shared_inputs=inputs, own_shifts=content['shifts'])
    db.info['_precondition_snapshot'] = {'workspace_id': workspace.id, 'content_revision': workspace.content_revision,
                                         'inputs': inputs, 'data': data, 'content': content,
                                         'context': (department, organisation)}
    return data


def take_precondition(db, workspace):
    held = db.info.pop('_precondition_snapshot', None) or {}
    return held if (held.get('workspace_id'), held.get('content_revision')) == (workspace.id, workspace.content_revision) else {}


def fingerprint(db, workspace, *, content_revision=None) -> str:
    data = domain_snapshot(db, workspace, shared_inputs=_fingerprint_shared_inputs(db, workspace))
    if content_revision is None:
        return data['fingerprint']
    from packages.domain.input_manifest import manifest
    return manifest(data, content_revision=content_revision)['token']


def _baseline_hint(db, workspace):
    if not hasattr(db, 'scalar'):
        return [None, None]
    memo = db.info.setdefault('_baseline_hints', {})
    baseline_revision = workspace.revision
    key = (workspace.id, baseline_revision)
    if key in memo:
        return memo[key]
    from .workspace_baselines import latest
    row = latest(db, workspace.id, include_payload=False)
    if row is None:
        return [None, None]
    hint = [row.id, row.payload_hash]
    memo[key] = hint
    return hint


def _verified_baseline(db, workspace):
    """Load and integrity-check one draft baseline once in this request/session."""
    from .workspace_baselines import baseline_rows, latest as latest_baseline, verify_in_transaction

    rows = baseline_rows(db)
    row_key = (workspace.id, workspace.revision)
    baseline = rows.get(row_key)
    if row_key not in rows or (baseline is not None and object_session(baseline) is not db):
        baseline = rows[row_key] = latest_baseline(db, workspace.id)
    return baseline, verify_in_transaction(db, baseline)


def _baseline_inputs_view(payload):
    """Copy only top-level branch containers before applying a draft overlay.

    Nested records stay shared and read-only until ``json_value`` creates the
    snapshot graph; copying those 14 MB records here would erase the gain.
    """
    # shortcut: nested records are shared read-only until json_value; upgrade to a frozen structure if a pre-json_value writer appears.
    return {key: (dict(value) if isinstance(value, dict) else list(value) if isinstance(value, list) else value)
            for key, value in payload.items()}


DRAFT_POLICY_BASIS = 'live_mandatory_with_resolved_zero_limits'


def draft_policy_hint(workspace):
    """Identify the draft rule basis for receipts made before the live overlay."""
    return {'draft_policy_basis': DRAFT_POLICY_BASIS} if getattr(workspace, 'status', None) == 'draft' else {}


def fingerprints(db, workspaces, *, checks=None, rebuild=None, reload=True):
    """One shared acquisition per organisation for a visible batch; never authority across transactions.

    With `rebuild`, a rota it rejects is never rebuilt: it gets its current stored manifest or
    durable projection, or is omitted so the caller treats it as unconfirmed (not fresh).
    """
    from packages.domain.input_manifest import digest, toolchain
    workspaces = list(workspaces)
    if not workspaces:
        return {}
    # Load deferred columns once for the batch, instead of one SELECT per column per rota.
    if reload:
        workspaces = list(db.scalars(select(m.Workspace).options(defer(m.Workspace.validation))
            .where(m.Workspace.id.in_([w.id for w in workspaces]))))
    if checks is None:
        from .services.workspace_reads import stored_checks
        checks = stored_checks(db, workspaces)
    from .services.read_projections import cached as cached_reads, input_stamp, CANONICAL_AUDIENCE
    shared, result, pending, department_revisions = {}, {}, {}, {}
    current_toolchain = digest(toolchain(quota_policy_limit()))
    for workspace in workspaces:
        department = db.get(m.Department, workspace.department_id)
        org = db.get(m.Organisation, department.organisation_id)
        revisions = department_revisions.get(org.id)
        if revisions is None:
            revisions = department_revisions[org.id] = sorted(
                [row.id, row.revision, row.catalogue_revision]
                for row in db.scalars(select(m.Department).where(m.Department.organisation_id == org.id)))
        cached = checks[workspace.id].get('input_manifest') or {}
        if (currency_match(cached.get('dirty_hint'), {'input_revision': org.input_revision,
                'availability_revision': org.availability_revision, 'content_revision': workspace.content_revision,
                'department_revisions': revisions, 'baseline': _baseline_hint(db, workspace), **draft_policy_hint(workspace)}
                ) and cached.get('components', {}).get('toolchain') == current_toolchain):
            result[workspace.id] = cached['token']
            continue
        pending[workspace.id] = workspace
    skipped = [wid for wid, workspace in pending.items() if rebuild is not None and not rebuild(workspace)]
    if skipped:
        # Collection reads must not rebuild many drafts per request; the worker warms these rows.
        C = m.WorkspaceReadProjection
        stored = db.execute(select(C.workspace_id, C.stamp, C.value).where(C.workspace_id.in_(skipped),
            C.audience == CANONICAL_AUDIENCE, C.kind == 'fingerprint')).all()
        result.update({wid: value for wid, stamp, value in stored if stamp == input_stamp(db, pending[wid])})
        for wid in skipped:
            del pending[wid]
    def build(wid):
        workspace = pending[wid]
        department = db.get(m.Department, workspace.department_id)
        org = db.get(m.Organisation, department.organisation_id)
        if org.id not in shared:
            shared[org.id] = _fingerprint_shared_inputs(db, workspace)
        data = domain_snapshot(db, workspace, shared_inputs=shared[org.id])
        # Only a single-rota check can reuse this payload; lists must release each full snapshot.
        if len(workspaces) == 1:
            db.info.setdefault('read_snapshots', {})[wid] = data
        return data['fingerprint']
    result.update(cached_reads(db, pending, 'fingerprint',
        {wid: input_stamp(db, workspace) for wid, workspace in pending.items()}, build))
    return result


def _fingerprint_shared_inputs(db, workspace):
    """Build the one shared input projection used by single and batch fingerprints."""
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    return prepared_snapshot_inputs(db, organisation)


def cheap_fingerprints(db, workspaces, *, checks):
    """Return currencies proven by stored dirty hints without rebuilding snapshots.

    Collection and landing reads must never rebuild a department snapshot merely
    to label a row as fresh. Missing or stale hints are omitted so callers can
    conservatively show unknown/outdated and let an authoritative read resolve it.
    """
    from packages.domain.input_manifest import digest, toolchain

    current_toolchain = digest(toolchain(quota_policy_limit()))
    departments = {}
    revision_stamps = {}
    result = {}
    for workspace in workspaces:
        check = (checks or {}).get(workspace.id) or {}
        manifest = check.get('input_manifest') or {}
        hint = manifest.get('dirty_hint') or {}
        department = departments.get(workspace.department_id)
        if department is None:
            department = departments[workspace.department_id] = db.get(m.Department, workspace.department_id)
        organisation_id = department.organisation_id
        if organisation_id not in revision_stamps:
            # Read current revisions even when the request retains the settings rows.
            rows = db.execute(select(m.Department.id, m.Department.revision, m.Department.catalogue_revision,
                m.Organisation.input_revision, m.Organisation.availability_revision)
                .join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
                .where(m.Department.organisation_id == organisation_id).order_by(m.Department.id)).all()
            revision_stamps[organisation_id] = {
                'input_revision': rows[0].input_revision, 'availability_revision': rows[0].availability_revision,
                'department_revisions': [list(row[:3]) for row in rows]}
        if (currency_match(hint, {**revision_stamps[organisation_id], 'content_revision': workspace.content_revision,
                      'baseline': _baseline_hint(db, workspace), **draft_policy_hint(workspace)})
                and manifest.get('components', {}).get('toolchain') == current_toolchain
                and manifest.get('token')):
            result[workspace.id] = manifest['token']
    return result


def feasibility_fingerprint(db, workspace):
    """Reads (e.g. every rule-review poll) reuse the stored token for unchanged inputs; writers always rebuild."""
    return feasibility_fingerprints(db, [workspace])[workspace.id]


def feasibility_fingerprints(db, workspaces, *, shared_inputs=None):
    """Read feasibility currencies for a batch of rotas from one immutable input snapshot."""
    if not workspaces:
        return {}
    from packages.domain.input_manifest import feasibility_token
    from .services.read_projections import cached, input_stamp
    rows = {workspace.id: workspace for workspace in workspaces}
    stamps = {wid: input_stamp(db, workspace) for wid, workspace in rows.items()}
    def build_many(ids):
        nonlocal shared_inputs
        if shared_inputs is None:
            org = db.get(m.Organisation, db.get(m.Department, workspaces[0].department_id).organisation_id)
            shared_inputs = activation_shared_inputs(db, org)
        # Feasibility currency hashes only content, mandatory inputs and the
        # compiler toolchain. Keep the fairness horizon (it determines which
        # dated leave rows are mandatory) but skip the expensive ledger,
        # wishes and score work that the token ignores.
        values = {}
        # A miss is deliberately evaluated one rota at a time. Keep only the
        # token; a department-scale activation must not retain one full graph
        # per affected rota until the batch finishes.
        for wid in ids:
            snapshot = domain_snapshot(db, rows[wid], shared_inputs=shared_inputs,
                                       include_fairness=True, include_objectives=False,
                                       include_fairness_history=False)
            try:
                values[wid] = feasibility_token(snapshot)
            finally:
                snapshot.clear()
                del snapshot
        return values

    return cached(db, rows, 'feasibility_fingerprint', stamps, build_many=build_many)


def tag_data(db, department_ids, *, acquired=None) -> dict:
    """The departments' tags with their members, as conditions.with_tags reads them: {id: {id, name, labels, members: {kind: [ids]}}}."""
    if acquired is None:
        acquired = records_many(db, {'tags': (m.Tag, m.Tag.department_id.in_(department_ids)),
            'tag_members': (m.TagMember, m.TagMember.department_id.in_(department_ids))})
    tags = {t['id']: {"id": t['id'], "name": t['name'], "labels": list(t['labels'] or []), "members": {}}
            for t in acquired['tags']}
    for member in sorted(acquired['tag_members'], key=lambda row: row['member_id']):
        tags[member['tag_id']]["members"].setdefault(member['kind'], []).append(member['member_id'])
    return tags


def policies_as_of(db, policies, when, *, keep_catalogue_lineage=False):
    """Active rules at `when`; approved retired catalogue versions may supply reconstruction lineage only."""
    ids = [p["id"] for p in policies]
    H = m.DefinitionHistory
    # Rank metadata before loading JSON: publication reconstruction needs one version per policy,
    # never every historical definition. The ordering matches the former chronological overwrite.
    latest = select(H.id, func.row_number().over(partition_by=H.object_id,
        order_by=(H.created_at.desc(), H.object_revision.desc())).label('position')).where(
            H.kind == 'policy', H.object_id.in_(ids), H.created_at <= when).subquery()
    then = {ident: snapshot for ident, snapshot in db.execute(select(H.object_id, H.snapshot)
        .join(latest, H.id == latest.c.id).where(latest.c.position == 1))}
    out = []
    for policy in policies:
        version = then.get(policy["id"], {})
        if version.get("status") == "active" or (keep_catalogue_lineage and version.get("status") == "retired"
                and version.get("approved_by") and version.get("definition", {}).get("rule", {}).get("kind") == "catalogue"):
            out.append(version)
    return out


def exceptions_as_of(db, exceptions, publication):
    """Publication-time versions; legacy approvals need evidence in the issued check, never an assumed date."""
    used = {(f.get('detail') or {}).get('exception_id') for f in (publication.validation or {}).get('findings', [])
            if f.get('code') == 'RULE_EXCEPTION'} - {None}
    versions, retained = {}, {}
    for row in db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'exception',
            m.DefinitionHistory.object_id.in_([e['id'] for e in exceptions])).order_by(
                m.DefinitionHistory.created_at, m.DefinitionHistory.object_revision)):
        if aware(row.created_at) <= aware(publication.created_at):
            versions[row.object_id] = row.snapshot
        elif row.reason == 'Exception retained before ending' and row.snapshot.get('status') == 'approved':
            retained.setdefault(row.object_id, row.snapshot)
    out = []
    for exception in exceptions:
        version = versions.get(exception['id'])
        if version is None and exception['id'] in used:
            version = retained.get(exception['id'], exception)
            if version.get('status') != 'approved' or not version.get('approved_by'):
                version = None
        if version is not None:
            out.append(version)
    return out


SCHEDULING_ORDER = {'scheduling_facts': (m.SchedulingFact.created_at, m.SchedulingFact.seq)}


def scheduling_sources(department_ids, when=None):
    sources = {name: (cls, cls.department_id.in_(department_ids)) for name, cls, _category, _scope, shape, _exclusions
               in SNAPSHOT_INPUT_SOURCE_SPECS if shape == 'scheduling'}
    if when is None:
        active = select(m.CatalogueConversion.id).where(m.CatalogueConversion.department_id.in_(department_ids),
                                                       m.CatalogueConversion.status == 'active')
        sources['source_bindings'] = (m.RuleSourceBinding, m.RuleSourceBinding.department_id.in_(department_ids)
                                     & m.RuleSourceBinding.migration_proposal_id.in_(active))
    else:
        for name in ('type_lead_ownership', 'policy_seed_lines'):
            cls, where = sources[name]
            sources[name] = cls, where & (cls.created_at <= when)
    return sources


def scheduling_inputs(db, department_ids, when=None, *, include_offer_history=False, acquired=None):
    """Approved fact versions and frozen bindings, batched and publication-time aware."""
    if acquired is None:
        acquired = records_many(db, scheduling_sources(department_ids, when), order_by=SCHEDULING_ORDER)
    facts = acquired['scheduling_facts']
    conversions = acquired['catalogue_conversions']
    if when is not None:
        versions = {}
        for row in db.scalars(select(m.DefinitionHistory).where(
                m.DefinitionHistory.department_id.in_(department_ids),
                m.DefinitionHistory.kind.in_(("scheduling_fact", "catalogue_conversion")),
                m.DefinitionHistory.created_at <= when).order_by(
                    m.DefinitionHistory.created_at, m.DefinitionHistory.object_revision)):
            versions[row.object_id] = row.snapshot
        facts = [versions[r["id"]] for r in facts if r["id"] in versions]
        conversions = [versions[r["id"]] for r in conversions if r["id"] in versions]
    if include_offer_history:
        facts = _offer_history(db, facts, when)
    active = {r["id"] for r in conversions if r["status"] == "active"}
    bindings = acquired.get('source_bindings')
    if bindings is None:
        # Historical conversion status owns the binding selection, not today's status.
        bindings = [record(row) for row in db.scalars(select(m.RuleSourceBinding).where(
            m.RuleSourceBinding.department_id.in_(department_ids), m.RuleSourceBinding.migration_proposal_id.in_(active)))]
    def retained(name):
        return sorted(acquired[name], key=lambda row: row['id'])
    # Retired approved versions retain replacement links; the domain emits only the active descendant.
    return {"type_lead_ownership": retained('type_lead_ownership'),
            "scheduling_facts": [r for r in facts if r["status"] in ("active", "retired") and r.get("approved_by")],
            "policy_seed_lines": retained('policy_seed_lines'),
            "source_bindings": [r['data'] for r in sorted(bindings, key=lambda row: row['id'])
                                if r['migration_proposal_id'] in active]}


def _offer_history(db, facts, when=None):
    """Retirement cutoffs are needed only for fairness's historical offered windows."""
    retired_ids = [r['id'] for r in facts if r['kind'] == 'overtime_offer' and r['status'] == 'retired' and r.get('approved_by')]
    if retired_ids:
        query = select(m.DefinitionHistory).where(m.DefinitionHistory.kind == 'scheduling_fact',
                                                 m.DefinitionHistory.object_id.in_(retired_ids))
        if when is not None:
            query = query.where(m.DefinitionHistory.created_at <= when)
        states, retired_at = {}, {}
        for row in db.scalars(query.order_by(m.DefinitionHistory.created_at, m.DefinitionHistory.object_revision)):
            status = row.snapshot.get('status')
            if status == 'retired' and states.get(row.object_id) != 'retired':
                retired_at[row.object_id] = aware(row.created_at).isoformat()
            states[row.object_id] = status
        facts = [{**r, 'retired_at': retired_at[r['id']]} if states.get(r['id']) == 'retired' else r for r in facts]
    return facts


def _unchanged_external_shifts(db, workspace, previous, current, lo, hi, limit):
    """Reuse unchanged published context within a write; own rows and every derived check stay fresh."""
    if previous is None or current['workspace']['status'] != 'draft':
        return None
    keys = ('id', 'department_id', 'status', 'starts_on', 'ends_on', 'base_publication_id', 'replaces_publication_id')
    before, after = previous['workspace'], current['workspace']
    zone = current['organisation']['timezone']
    # An own-session edit advances its content revision, but cannot change the published context under the write lock.
    from packages.domain.input_manifest import manifest
    expected = manifest(previous)['token']
    dirty = ('input_revision', 'availability_revision')
    if (previous.get('fingerprint') != expected
            or any(previous['organisation'].get(k) != current['organisation'].get(k) for k in dirty)
            or any(before.get(key) != after.get(key) for key in keys)
            or previous['organisation']['timezone'] != zone
            or previous.get('quota_policy_limit') != current['quota_policy_limit']):
        return None
    old_quota, new_quota = previous.get('quota_context'), current.get('quota_context')
    if bool(old_quota) != bool(new_quota):
        return None
    if old_quota:
        # An incomplete/capped source cannot establish that its external rows are complete.
        if (not old_quota.get('complete') or old_quota.get('limit_exceeded')
                or not new_quota.get('complete') or new_quota.get('limit_exceeded')
                or (old_quota.get('start'), old_quota.get('end')) != (lo.isoformat(), hi.isoformat())):
            return None
    elif (local_midnight(date.fromisoformat(str(before['starts_on'])), zone) - timedelta(days=32),
          local_midnight(date.fromisoformat(str(before['ends_on'])), zone) + timedelta(days=32)) != (lo, hi):
        return None
    focus = set(previous['focus_ids'])
    external = [row for row in previous['shifts'] if row['id'] not in focus]
    # Preserve the fresh query's ordered row limit; its completeness flags are calculated below as usual.
    return external if limit is None else external[:limit]


def snapshot_inputs(db, org):
    return _snapshot_inputs(db, org)


HINT_DATE_UNRESOLVED = "__unresolved__"


def currency_match(left, right):
    """Compare compact currency only when both hints have a resolved date state."""
    if any(isinstance(value, Mapping) and value.get('event_local_date') == HINT_DATE_UNRESOLVED
           for value in (left, right)):
        return False
    return left == right


def input_current(recorded, current):
    """Absent currency is unknown; only equal semantic fingerprints establish current inputs."""
    return None if recorded is None or current is None else recorded == current


def receipt_currencies(db, workspaces, candidates, build):
    """Hints prove unchanged inputs; every other retained receipt needs an authoritative fingerprint."""
    from .serialization import effective_input_fingerprint
    from packages.domain.input_manifest import digest, toolchain
    current_toolchain = digest(toolchain(quota_policy_limit()))
    currencies, pending = {}, []
    for workspace in workspaces:
        expected = currency_hint(db, workspace)
        retained = False
        for job in candidates.get(workspace.id, []):
            projection = job.read_metadata or {'input_snapshot': {}, 'result': None}
            recorded = effective_input_fingerprint(job, projection=projection)
            if recorded is None:
                continue
            retained = True
            snapshot = projection.get('input_snapshot') or {}
            if (currency_match(snapshot.get('currency_hint'), expected)
                    and (job.kind != 'generation' or (snapshot.get('input_manifest') or {}).get(
                        'components', {}).get('toolchain') == current_toolchain)):
                currencies[workspace.id] = recorded
                break
        if retained and workspace.id not in currencies:
            pending.append(workspace)
    if pending:
        currencies.update(build(db, pending))
    return currencies


def snapshot_department_ids(db, organisation_id):
    cache = db.info.setdefault('_snapshot_department_ids', {})
    if organisation_id not in cache:
        cache[organisation_id] = list(db.scalars(
            select(m.Department.id).where(m.Department.organisation_id == organisation_id)
            .order_by(m.Department.id)))
    return cache[organisation_id]


def snapshot_window(workspace, timezone):
    return (local_midnight(workspace.starts_on, timezone) - timedelta(days=32),
            local_midnight(workspace.ends_on, timezone) + timedelta(days=32))


def event_local_date_for_hint(db, workspace, organisation, *, now=None):
    """Return the local date only when recurring blocking-event facts depend on today.

    ``blocking_occurrences`` clamps recurring projections to ``m.now()``'s local
    date and drops starts before that instant.  Stored occurrences do not use the
    clock, so an unaffected draft keeps the same compact hint across midnight.
    """
    timezone = (organisation.get('timezone') if isinstance(organisation, Mapping)
                else getattr(organisation, 'timezone', None)) if organisation is not None else None
    try:
        local_zone = ZoneInfo(timezone) if timezone else None
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return HINT_DATE_UNRESOLVED
    if local_zone is None:
        return HINT_DATE_UNRESOLVED
    now = now or m.now()
    today = now.astimezone(local_zone).date()
    try:
        lo, hi = snapshot_window(workspace, timezone)
    except (AttributeError, TypeError, ValueError):
        # A malformed draft window cannot prove that the clock is irrelevant.
        return today.isoformat()
    last = (hi - timedelta(microseconds=1)).astimezone(local_zone).date()
    if not lo.astimezone(local_zone).date() <= today <= last:
        return None
    revision = (organisation.get('input_revision'), organisation.get('availability_revision')) \
        if isinstance(organisation, Mapping) else (getattr(organisation, 'input_revision', None),
                                                    getattr(organisation, 'availability_revision', None))
    department = db.get(m.Department, workspace.department_id)
    department_ids = snapshot_department_ids(db, department.organisation_id)
    key = (tuple(department_ids), workspace.starts_on, workspace.ends_on, timezone, today, revision)
    cache = db.info.setdefault('_hint_blocking_event_dates', {})
    if key not in cache:
        from .services import events
        event_where = events.blocking_sources(department_ids, lo, hi)['events'][1]
        cache[key] = db.scalar(select(m.Event.id).where(event_where, m.Event.repeat.is_not(None)).limit(1)) is not None
    return today.isoformat() if cache[key] else None


def hint_with_event_local_date(db, workspace, hint, organisation):
    value = event_local_date_for_hint(db, workspace, organisation)
    if value == HINT_DATE_UNRESOLVED:
        return {**hint, 'event_local_date': HINT_DATE_UNRESOLVED}
    return {**hint, **({'event_local_date': value} if value is not None else {})}


def currency_hint(db, workspace, *, mandatory_rules=True):
    """Return the compact source currency stored with a feasibility receipt.

    Activation admission only needs to prove that the receipt was built from
    the same versioned inputs.  Keeping this relational hint separate from a
    domain snapshot avoids loading every scheduling row just to compare a
    receipt that already carries its input fingerprint.
    """
    from packages.domain.input_manifest import FORMAT
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    current = hint_with_event_local_date(db, workspace, {
        'input_manifest_format': FORMAT,
        'input_revision': organisation.input_revision,
        'availability_revision': organisation.availability_revision,
        'content_revision': workspace.content_revision,
        'baseline': _baseline_hint(db, workspace),
        **(draft_policy_hint(workspace) if mandatory_rules else {}),
    }, organisation)
    return current


def activation_shared_inputs(db, org):
    """Reuse one department input projection across activation read phases.

    Preparation may validate legacy receipt currencies before calculating policy
    impact. Both phases must share the same immutable catalogue; rebuilding it
    doubles the largest allocation. The final writer reacquires current inputs.
    """
    # A serialized writer must reacquire its own current inputs; never reuse a
    # preparation memo after the read transaction has closed.
    if db.connection().info.get('rota_write'):
        return snapshot_inputs(db, org)
    memo = db.info.setdefault('_activation_shared_inputs', {})
    department_revisions = tuple(db.execute(select(m.Department.id, m.Department.revision,
        m.Department.catalogue_revision).where(m.Department.organisation_id == org.id)
        .order_by(m.Department.id)).all())
    key = (org.id, org.revision, org.input_revision, org.availability_revision, department_revisions)
    if key not in memo:
        memo[key] = snapshot_inputs(db, org)
    return memo[key]


def prepared_snapshot_inputs(db, org):
    """Reuse frozen preparation inputs; independently reacquire them in the final writer."""
    state = db.info.get('_planning_preparation')
    if state is None or state['replay']:
        return snapshot_inputs(db, org)
    token = (org.id, org.revision, org.input_revision, org.availability_revision,
             tuple(db.execute(select(m.Department.id, m.Department.revision, m.Department.catalogue_revision)
                              .where(m.Department.organisation_id == org.id)
                              .order_by(m.Department.id)).all()))
    import pickle
    if state['input_tokens'].get(org.id) == token and org.id in state['inputs']:
        return pickle.loads(state['inputs'][org.id])
    value = snapshot_inputs(db, org)
    state['inputs'][org.id] = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    state['input_tokens'][org.id] = token
    return value


def _snapshot_inputs(db, org):
    """Shared current records for a read-only batch of snapshots in one transaction."""
    department_ids = snapshot_department_ids(db, org.id)
    result = {"organisation": record(org)}
    keyed = {name: cls for name, cls, _category, _scope, shape, _exclusions in SNAPSHOT_INPUT_SOURCE_SPECS if shape == 'keyed'}
    listed = {name: cls for name, cls, _category, _scope, shape, _exclusions in SNAPSHOT_INPUT_SOURCE_SPECS if shape == 'listed'}
    sources = {name: (cls, cls.department_id.in_(department_ids)) for name, cls, _category, scope, shape, _exclusions
               in SNAPSHOT_INPUT_SOURCE_SPECS if scope == 'department' and shape in {'keyed', 'listed'}}
    sources['people'] = (m.Person, m.Person.organisation_id == org.id)
    sources['departments'] = (m.Department, m.Department.organisation_id == org.id)
    sources.update(scheduling_sources(department_ids))
    sources.update({name: (cls, cls.department_id.in_(department_ids)) for name, cls, _category, scope, shape, _exclusions
                    in SNAPSHOT_INPUT_SOURCE_SPECS if scope == 'department' and shape in {'date_set', 'tag', 'tag_member'}})
    # A pager number is a contact detail, not a scheduling input: the rota snapshot leaves it out.
    acquired = records_many(db, sources, exclude={'people': ('pager',)}, order_by=SCHEDULING_ORDER)
    result.update({name: {row['id']: row for row in acquired[name]} for name in [*keyed, 'people']})
    # Department rows carry the captured score/auto/display settings used by
    # draft baselines; keep them in the shared payload as keyed records.
    result['departments'] = {row['id']: row for row in acquired['departments']}
    result.update({name: acquired[name] for name in listed})
    result['department_ids'] = [row['id'] for row in acquired['departments']]
    # Canonical mentors resolve through ORM attachment relationships, not the legacy mentor column.
    result['rotations'] = [record(row) for row in db.scalars(select(m.Rotation).where(
        m.Rotation.department_id.in_(department_ids)))]
    result.update(scheduling_inputs(db, result['department_ids'], acquired=acquired))
    from .services.date_sets import data as date_set_data
    result['date_sets'] = date_set_data(db, result['department_ids'], acquired=acquired['date_sets'])
    result['tags'] = tag_data(db, result['department_ids'], acquired=acquired)
    return result


def external_schedule(db, workspace, department_ids, lo, hi, zone):
    """In-force context for a draft, excluding its own publication and the version it amends."""
    from .services.schedule import current_publications, in_force_clause
    publications = [p for p in current_publications(db, department_ids, *overlap_dates(lo, hi, zone), light=True)
                    if p.workspace_id != workspace.id and p.id != workspace.base_publication_id]
    clauses = [in_force_clause(p, zone, workspace.starts_on if p.id == workspace.replaces_publication_id else None)
               for p in publications]
    query = select(m.Shift).where(or_(*clauses) if clauses else false(),
                                 m.Shift.start < hi, m.Shift.end > lo).order_by(m.Shift.start, m.Shift.id)
    return publications, query


def _snapshot_external_schedule(db, workspace, department_ids, lo, hi, zone, limit, previous, current):
    # A shift update writes only its own draft. Each preparation transaction holds a consistent
    # read of issued sessions, even while the edited shift and workspace revision change.
    import pickle
    preparation = db.info.get('_planning_preparation') or {}
    shared = (preparation.get('kind') == 'shift.update' and not preparation['replay'] and previous is None)
    if shared:
        memo = db.info.setdefault('_prepared_external_schedule', {})
        key = (workspace.id, workspace.base_publication_id, workspace.replaces_publication_id, workspace.starts_on,
               tuple(department_ids), lo, hi, zone, limit)
        if key in memo:
            publications, rows = memo[key]
            return publications, pickle.loads(rows)
    publications, query = external_schedule(db, workspace, department_ids, lo, hi, zone)
    external = _unchanged_external_shifts(db, workspace, previous, current, lo, hi, limit)
    if external is None:
        external = shift_records(db, db.scalars(query if limit is None else query.limit(limit)))
    if shared:
        memo[key] = publications, pickle.dumps(external, protocol=pickle.HIGHEST_PROTOCOL)
    return publications, external


def _domain_snapshot(db, workspace, operational: bool = False, policy_overrides: list[dict] | None = None, *, wish_debt_cap: float | None = None,
                    lead_balance: str | None = None, fact_overrides: list[dict] | None = None,
                    current_scheduling: bool = False, mandatory_rules: bool = True, current_mandatory: bool = False,
                    own_shifts: list[dict] | None = None,
                    external_snapshot: dict | None = None, shared_inputs: dict | None = None,
                    scope_interval_cache: dict | None = None, include_fairness: bool = True,
                    include_objectives: bool = True, include_fairness_history: bool = True,
                    rule_map_bindings: bool = False) -> dict:
    department = db.get(m.Department, workspace.department_id)
    org = db.get(m.Organisation, department.organisation_id)
    draft_rules = draft_policy_hint(workspace) if mandatory_rules and not current_scheduling else {}
    mandatory_overlay_changed = False
    baseline = None
    if shared_inputs is None and not current_scheduling:
        shared_inputs = _fingerprint_shared_inputs(db, workspace)
    live_inputs = shared_inputs
    if not current_scheduling:
        from .workspace_baselines import LIVE_VALIDITY_SOURCES, projection as baseline_projection
        baseline, verified_inputs = _verified_baseline(db, workspace)
        if baseline is not None:
            # Keep the verified payload memo immutable; overlay work receives
            # private top-level branch containers while json_value copies records.
            saved_inputs = _baseline_inputs_view(verified_inputs)
            validity_baseline = {key: saved_inputs.get(key) or {} for key in LIVE_VALIDITY_SOURCES}
            # Drafts keep the captured department inputs even when callers prepared a live shared-input batch.
            # A draft may add a local duty after capture. Keep only the referenced
            # catalogue rows needed to interpret that local content. The captured
            # policy/configuration is the basis for the overlay below.
            own_ids = list(db.scalars(select(m.Shift.id).where(m.Shift.workspace_id == workspace.id)))
            if own_ids:
                refs = db.execute(select(m.Shift.assignment_type_id, m.Shift.location_id)
                                  .where(m.Shift.id.in_(own_ids))).all()
                type_ids = {row[0] for row in refs}
                location_ids = {row[1] for row in refs}
                service_ids = set(db.scalars(select(m.ShiftService.service_id).where(m.ShiftService.shift_id.in_(own_ids))))
                # Baselines freeze governed inputs, not only people already assigned in a draft.
                # The prepared phase payload already contains every organisation person.
                current_people = (live_inputs or {}).get('people') or {}
                people_ids = set(current_people)
                catalogue_inputs = {
                    key: {row.id: record(row) for row in db.scalars(select(cls).where(cls.id.in_(ids)))}
                    for key, cls, ids in (("types", m.AssignmentType, type_ids), ("locations", m.Location, location_ids),
                                          ("services", m.Service, service_ids)) if ids
                }
                if people_ids and workspace.status not in {"draft", "submitted", "approved"}:
                    catalogue_inputs['people'] = {ident: json_value(row) for ident, row in current_people.items()}
                for key, ids in (("types", type_ids), ("locations", location_ids), ("services", service_ids), ("people", people_ids)):
                    saved = dict(saved_inputs.get(key) or {})
                    current = catalogue_inputs.get(key) or {}
                    saved.update({ident: current[ident] for ident in ids if ident in current})
                    saved_inputs[key] = saved
                shared_inputs = saved_inputs
            else:
                shared_inputs = saved_inputs
            from .workspace_baselines import live_safety_inputs
            shared_inputs = live_safety_inputs(db, workspace, shared_inputs, current=live_inputs,
                                              include_mandatory=bool(draft_rules) or current_mandatory,
                                              validity_baseline=validity_baseline)
            mandatory_overlay_changed = shared_inputs.pop('_mandatory_overlay_changed', False)
            if rule_map_bindings and workspace.status in {"draft", "submitted", "approved"}:
                from packages.domain.rule_lines import seeded_policies

                captured = seeded_policies(saved_inputs.get("policies") or [], saved_inputs.get("policy_seed_lines") or [])
                effective = live_safety_inputs(db, workspace, saved_inputs, current=live_inputs,
                                              include_mandatory=True, validity_baseline=validity_baseline)["policies"]
                if captured != effective:
                    shared_inputs['_rule_map_policies'] = (captured, effective)
    # Overrides, tag expansion and evaluator caches must never change the inputs of another rota.
    result = json_value(shared_inputs) if shared_inputs is not None else prepared_snapshot_inputs(db, org)
    read_quality = {}
    result = DomainSnapshot(result, read_quality=read_quality)
    if draft_rules:
        result['_draft_policy_basis'] = draft_rules['draft_policy_basis']
    if current_mandatory and mandatory_overlay_changed:
        result['_mandatory_overlay_changed'] = True
    if result['organisation']['id'] != org.id:
        raise ValueError('Snapshot inputs belong to another organisation')
    department_ids = result.pop('department_ids')
    result.update({"workspace": record(workspace, exclude=("validation",)), "department": record(department),
                   "operational": operational,
                   "quota_policy_limit": quota_policy_limit()})
    department_settings = result['department']
    if baseline is not None:
        from .workspace_baselines import frozen_department_settings
        department_settings = {**department_settings, **frozen_department_settings(
            result, workspace.department_id, baseline.included_sources)}
        result["baseline"] = baseline_projection(baseline)
    if scope_interval_cache is not None:
        result['_scope_interval_cache'] = scope_interval_cache
    # A published rota keeps the rules it was approved under: rules activated, changed or retired later
    # apply to drafts and future rotas, never retroactively.
    own_publication = None
    if workspace.status == "published":
        pub = db.scalar(select(m.Publication).where(m.Publication.workspace_id == workspace.id))
        if pub is not None:
            own_publication = pub
    stored_policies = {p['id']: p for p in result['policies']}
    if baseline is not None and workspace.status in ('draft', 'submitted', 'approved'):
        # Explicit rota adjustments admit only their captured approved revision, leaving other frozen rules alone.
        admitted = {row['policy_id']: row['policy_revision'] for row in workspace.rule_adjustments or []
                    if row['policy_id'] not in stored_policies}
        if admitted:
            from .errors import require
            H = m.DefinitionHistory
            snapshots = [snapshot for snapshot in db.scalars(select(H.snapshot).where(
                H.department_id == workspace.department_id, H.kind == 'policy',
                tuple_(H.object_id, H.object_revision).in_(list(admitted.items()))).order_by(H.object_id))
                if snapshot.get('status') == 'active' and snapshot.get('approved_by')]
            require({snapshot['id'] for snapshot in snapshots} == set(admitted), 'BOUND_PREVIEW_CONTEXT',
                    'The approved rule version for this adjustment is unavailable', 409)
            result['policies'] += snapshots
    from packages.domain.fairness import SEMANTICS as fairness_semantics
    result['fairness_semantics'] = (own_publication.snapshot.get('fairness_semantics', 1)
        if own_publication and not current_scheduling else fairness_semantics)
    if own_publication and not current_scheduling:
        # D53 publications predating captured defaults used all-history; never reinterpret issued evidence.
        result['fairness_default_window'] = own_publication.snapshot.get('fairness_default_window', {'period': 'all_history'})
    if own_publication and not current_scheduling:
        frozen_department = own_publication.snapshot.get('department') or {}
        calendar_id = frozen_department.get('operating_calendar_id')
        result['department']['operating_calendar_id'] = calendar_id
        if 'weekend_days' in frozen_department:
            result['department']['weekend_days'] = json_value(frozen_department['weekend_days'])
        if calendar_id:
            calendar = own_publication.snapshot.get('operating_calendars', {}).get(calendar_id)
            if calendar is None:
                result['operating_calendars'].pop(calendar_id, None)
            else:
                result['operating_calendars'][calendar_id] = json_value(calendar)
        from .services.date_sets import data as date_set_data
        result['date_sets'] = (own_publication.snapshot.get('date_sets')
                              if 'date_sets' in own_publication.snapshot
                              else date_set_data(db, department_ids, own_publication.created_at))
        result.update(scheduling_inputs(db, department_ids, own_publication.created_at))
        result["policies"] = policies_as_of(db, result["policies"], own_publication.created_at,
            keep_catalogue_lineage=bool(result["source_bindings"]))
        result["exceptions"] = exceptions_as_of(db, result["exceptions"], own_publication)
        if 'leadership' in own_publication.snapshot:
            evidence = own_publication.snapshot['leadership']
            require_evidence = evidence.get('workspace_id') == workspace.id and evidence.get('format') == 1
            if not require_evidence:
                raise ValueError('Publication leadership evidence does not match this rota')
            result['type_lead_ownership'] = json_value(evidence['type_lead_ownership'])
            result['policies'] = [p for p in result['policies'] if p['definition']['rule']['kind'] != 'team_lead_requirement'
                                  and p['definition']['rule'].get('id') != 'team_leads'] + json_value(evidence['policies'])

    overridden_ids = set()
    if policy_overrides:
        from .errors import require
        replacements, replaced, simulated = {}, set(), set()
        current_policies = {p['id']: p for p in ((live_inputs or {}).get('policies') or result['policies'])}
        current_catalogues = defaultdict(list)
        for policy in current_policies.values():
            rule = policy['definition']['rule']
            if rule['kind'] == 'catalogue':
                current_catalogues[(policy['department_id'], rule['id'], rule.get('catalogue_version', 1))].append(policy)
        for value in policy_overrides:
            policy = value
            rule = policy.get('definition', {}).get('rule') or {}
            bound = rule.get('catalogue_version') == 2 and any(line.get('source_bindings') for line in rule.get('lines') or [])
            if bound and policy.get('status') == 'active':
                references = {pin['policy_id'] for line in rule.get('lines') or []
                              for pin in (line.get('source_bindings') or {}).values()}
                available = {binding.get('data', binding)['source_policy_id'] for binding in result['source_bindings']}
                predecessors = set(policy['definition'].get('merged_ids') or []) | {policy['definition'].get('replaces_id')}
                if baseline is not None and not own_publication:
                    # Explicit previews need this catalogue's live lineage; unrelated rules stay captured.
                    related = current_catalogues[(policy['department_id'], rule['id'], rule['catalogue_version'])]
                    related_ids = {p['id'] for p in related}
                    from packages.contracts.schemas import NEVER_OVERRIDDEN, safety_rule
                    from packages.domain.rule_lines import _safety_number
                    captured_safety = []
                    safety_source_ids = {pin['policy_id'] for p in related
                        for line in p['definition']['rule'].get('lines') or []
                        for pin in (line.get('source_bindings') or {}).values()
                        if pin.get('primitive_kind') in NEVER_OVERRIDDEN}
                    for captured in result['policies']:
                        if captured['id'] not in related_ids | safety_source_ids:
                            continue
                        definition = captured.get('definition') or {}
                        rule = definition.get('rule') or {}
                        if rule.get('kind') == 'catalogue':
                            lines = [line for line in rule.get('lines') or [] if _safety_number({
                                'catalogue_id': rule.get('id'),
                                'catalogue_version': rule.get('catalogue_version', 1),
                                'line': line,
                            })]
                            if not lines:
                                continue
                            preserved = json_value(captured)
                            preserved['definition'] = {**preserved['definition'],
                                'rule': {**preserved['definition']['rule'], 'lines': lines}}
                        elif safety_rule(definition):
                            preserved = json_value(captured)
                        else:
                            continue
                        preserved['id'] = str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f'fmuc:bound-preview:{workspace.id}:{captured["id"]}:{captured.get("revision", 1)}'))
                        preserved['_source_policy_id'] = captured['id']
                        captured_safety.append(preserved)
                    sources = references | {pin['policy_id'] for p in related
                        for line in p['definition']['rule'].get('lines') or []
                        for pin in (line.get('source_bindings') or {}).values()}
                    result['policies'] = [p for p in result['policies'] if p['id'] not in related_ids]
                    result['policies'] += captured_safety + json_value(related)
                    result['source_bindings'] = [binding for binding in result['source_bindings']
                        if binding.get('data', binding)['source_policy_id'] not in sources] + json_value([
                            binding for binding in (live_inputs or {}).get('source_bindings', [])
                            if binding.get('data', binding)['source_policy_id'] in sources])
                    available = {binding.get('data', binding)['source_policy_id'] for binding in result['source_bindings']}
                    require(references <= available, 'BOUND_PREVIEW_CONTEXT',
                            'This bound target has no matching source context; check it on a current draft', 409)
                require(not own_publication or current_scheduling or
                        references <= available and (predecessors - {None}) <= {p['id'] for p in result['policies']},
                        'BOUND_PREVIEW_CONTEXT', 'This bound target has no matching publication context; check it on a current draft', 409)
                saved = current_policies.get(policy['id']) or stored_policies.get(policy['id'])
                if saved and saved['status'] == 'proposed':
                    policy = {**policy, 'revision': policy['revision'] + 1}
                    simulated.update(predecessors - {None})
            if policy.get('status') == 'active':
                replaced.update(policy['definition'].get('merged_ids') or [])
                if policy['definition'].get('replaces_id'):
                    replaced.add(policy['definition']['replaces_id'])
            manifest = policy.get('definition', {}).get('legacy_lead_conversion')
            if manifest and policy.get('status') == 'active':
                from .services.leadership import ownership_id
                existing = {row['assignment_type_id'] for row in result['type_lead_ownership']}
                result['type_lead_ownership'] += [
                    {'id': ownership_id(policy['id'], source['type_id']), 'department_id': policy['department_id'],
                     'assignment_type_id': source['type_id'], 'source_type_revision': source['type_revision'],
                     'source_history_id': source['source_history_id'], 'source_lead_hash': source['lead_hash'],
                     'initial_policy_id': policy['id'], 'initial_line_id': source['destination_line_id'],
                     'effective_on': manifest['effective_on']}
                    for source in manifest['sources'] if source['type_id'] not in existing]
            replacements[policy['id']] = policy
        overridden_ids = set(replacements)
        result['policies'] = [({**p, 'status': 'retired', 'active': False,
                               'revision': p['revision'] + (p['id'] in simulated)} if p['id'] in replaced and p['status'] == 'active' else p)
                              for p in result['policies'] if p['id'] not in replacements] + list(replacements.values())
    if fact_overrides:
        overrides = {r["id"]: r for r in fact_overrides}
        replaced = {r.get("replaces_id") for r in fact_overrides}
        result["scheduling_facts"] = [({**r, "status": "retired"} if r["id"] in replaced else r)
            for r in result["scheduling_facts"] if r["id"] not in overrides] + list(overrides.values())
    from packages.domain.rule_lines import bound_policies
    # This rota's adjustments over the department rules (switch off, or another number).
    adjustments = [r for r in (workspace.rule_adjustments or []) if r['policy_id'] not in overridden_ids]

    def resolve(policies, quality):
        policies = bound_policies(policies, result, read_quality=quality)
        if adjustments:
            from .services.planning import adjusted_rules
            policies = [q for p in policies if (q := adjusted_rules(p, adjustments)) is not None]
        # Rules added for one rota apply to that rota only.
        return [p for p in policies if (p.get("definition") or {}).get("workspace_id") in (None, workspace.id)]

    if '_rule_map_policies' in result:
        result['_rule_map_policies'] = [resolve(policies, {}) for policies in result['_rule_map_policies']]
    result["policies"] = resolve(result["policies"], read_quality)
    result["_catalogue_v2_materialized"] = True
    # Tags a rule names read as their members today: whoever is tagged now is included.
    with_tags(result)
    lo, hi = snapshot_window(workspace, org.timezone)
    from packages.domain.quotas import applicable_policies, period_bounds, MAX_SHIFTS, MAX_ROWS
    policy_limit = result["quota_policy_limit"]
    quota_policies = applicable_policies(result)
    if quota_policies:
        # A whole block, not just the neighbouring rest window, must be loaded.
        for policy in quota_policies[:MAX_ROWS]:
            try:
                a, b = period_bounds(policy, org.timezone)
            except ValueError:
                continue  # The evaluator emits the explicit ambiguous-time finding.
            lo, hi = min(lo, a), max(hi, b)
        result["quota_context"] = {"start": lo.isoformat(), "end": hi.isoformat(),
                                   "complete": True, "limit_exceeded": len({p['id'] for p in quota_policies}) > policy_limit or len(quota_policies) > MAX_ROWS}
    if include_fairness:
        from .services.fairness import prepare_history_window
        prepare_history_window(db, result)
    availability_lo = lo
    if include_fairness:
        from packages.domain.conditions import active_rules
        from packages.domain.fairness import history_windows
        for policy in active_rules(result['policies'], overrides=False, context=result, read_quality=read_quality):
            rule = policy['definition']['rule']
            if rule['kind'] == 'balance_goal' and rule['metric'] in ('overtime_duties', 'overtime_hours'):
                for first, _ in history_windows(result, policy, preload=True).values():
                    availability_lo = min(availability_lo, local_midnight(first, org.timezone))
    from .services import availability_patterns, events
    window_inputs = rows_many(db, {'availability': (m.Availability,
        m.Availability.person_id.in_(result["people"]) & (m.Availability.start < hi)
        & (m.Availability.end > availability_lo) & m.Availability.retracted.is_(False)),
        'patterns': availability_patterns.snapshot_source(department_ids, list(result['people'])),
        **events.blocking_sources(department_ids, lo, hi)})
    # Draft baselines freeze governed rules/configuration, but approved leave and
    # recorded absence remain current person facts and must be read at run time.
    result['rotation_leave_availability'] = [record(r, exclude=("private_reason",))
                                             for r in window_inputs['availability']]
    result['availability'] = [row for row in result['rotation_leave_availability'] if row['counts_as_unavailable']]
    # Approved weekly patterns count as ordinary unavailability for the same window.
    weekly = availability_patterns.expand(db, department_ids, list(result["people"]), availability_lo, hi, org.timezone,
                                         acquired=window_inputs['patterns'])
    result["availability"] += weekly
    from packages.domain.rule_lines import fact_policy
    facts = events.protected_facts(db, department_ids, list(result["people"]), lo, hi, org.timezone, acquired=window_inputs)
    result["scheduling_facts"] += facts
    result["policies"] += [fact_policy(fact) for fact in facts]
    # Staff wishes on the rota's days, each weighted by its share of the points budget and any catch-up for declined wishes.
    from .services.wishes import snapshot_wishes
    from .services.schedule import in_force_shifts
    from packages.contracts.schemas import GenerationOptions
    scoring = GenerationOptions.model_validate(department_settings.get('generation_defaults') or {}).scoring
    own = own_shifts
    if own is None:
        edited = db.info.get('_prepared_shift_edit')
        own = edited[1] if edited is not None and edited[0] == workspace.id else workspace_shifts(db, workspace.id)
    if own_publication is not None and own_publication.effective_until:
        # A published rota partly replaced by a later one: only the days it is still in force.
        own = in_force_shifts(own_publication, own, org.timezone)
        result["workspace"]["ends_on"] = own_publication.effective_until.isoformat()
    result["wishes"] = (snapshot_wishes(db, [workspace.department_id], workspace,
                                       own_publication.effective_until if own_publication is not None else None,
                                       wish_debt_cap=wish_debt_cap, scoring_settings=scoring)
                        if include_objectives else [])
    result['wishes'] = [{key: value for key, value in wish.items() if key != 'outcome_reasons'}
                        for wish in result['wishes']]
    score_context = {}
    if include_objectives and wish_debt_cap is not None and wish_debt_cap != scoring.wish_debt_cap:
        wishes = snapshot_wishes(db, [workspace.department_id], workspace,
                                 own_publication.effective_until if own_publication is not None else None,
                                 scoring_settings=scoring)
        wishes = [{key: value for key, value in wish.items() if key != 'outcome_reasons'} for wish in wishes]
        if wishes != result["wishes"]:
            score_context["wishes"] = wishes
    result["focus_ids"] = [s["id"] for s in own]
    publications, external = _snapshot_external_schedule(
        db, workspace, department_ids, lo, hi, org.timezone, MAX_SHIFTS + 1 if quota_policies else None,
        external_snapshot, result)
    if own_publication is not None:
        own = frozen_series_states(own, own_publication)
    published = {p.workspace_id: p for p in publications}
    by_workspace = {}
    for row in external:
        by_workspace.setdefault(row['workspace_id'], []).append(row)
    projected = {row['id']: row for workspace_id, rows in by_workspace.items()
                 for row in (frozen_series_states(rows, published[workspace_id]) if workspace_id in published else rows)}
    external = [projected[row['id']] for row in external]
    result["shifts"] = own + external
    if baseline is not None:
        # Use issued type facts for neighbours so later edits cannot reinterpret their duties.
        for shift in external:
            type_id = shift['assignment_type_id']
            if type_id not in result['types']:
                issued = shift.get('type_snapshot') or {}
                source = issued if issued.get('department_id') else live_inputs['types'][type_id]
                result['types'][type_id] = json_value(source)
    from .services.published_changes import staged_availability
    result['availability'] += staged_availability(db, workspace)
    # All snapshot consumers below are evaluation entry points (fairness, rule-map,
    # feasibility fingerprints, activation receipts and population checks).  Build
    # the person-row views once after every source list is final; keeping this index
    # on the immutable snapshot avoids a people x rows scan in conditions.
    # Resolver/trial snapshots opt in separately because they can mutate source rows.
    from packages.domain.validation import index_person_rows
    result['_person_rows'] = index_person_rows(result)
    result = with_series_state(result)
    focus = set(result['focus_ids'])
    baseline = (publication_score_baseline(db, workspace,
        [row for row in result['shifts'] if row['id'] in focus], own_publication) if include_objectives else None)
    if baseline is not None:
        result['score_baseline'] = baseline
    if quota_policies:
        used = {row["workspace_id"] for row in external}
        result["quota_context"]["limit_exceeded"] |= len(result["shifts"]) > MAX_SHIFTS
        result["quota_context"]["publications"] = [
            {"publication_id": p.id, "sequence": p.sequence, "workspace_id": p.workspace_id}
            for p in publications if p.department_id == workspace.department_id and p.workspace_id in used]
    from .services.planning_population import applied_population
    population = applied_population(db, workspace, result)
    if population is not ...:
        result['applied_planning_population'] = population
    # Balance goals read each person's history from the fairness ledger (earlier publications).
    from packages.domain.fairness import balance_goals
    result["fairness_strengths"] = scoring.strengths.model_dump()
    result['generation_options'] = json_value(department_settings.get('generation_defaults') or {})
    goals = balance_goals(result) if include_fairness else []
    if goals and include_fairness_history:
        from .services.fairness import history
        if any(g['definition']['rule']['metric'] in ('overtime_duties', 'overtime_hours') for g in goals):
            result['scheduling_facts'] = _offer_history(db, result['scheduling_facts'],
                own_publication.created_at if own_publication and not current_scheduling else None)
        result["fairness_history"] = history(db, result, goals)
    if score_context:
        result["score_context"] = score_context
    from packages.domain.input_manifest import manifest
    result['input_manifest'] = manifest(result)
    result['fingerprint'] = result['input_manifest']['token']
    return result


def preparation_stamp(db, workspace):
    """Complete bounded source currency, checked again under the serialized writer."""
    from .services.planning import validation_stamp
    from packages.domain.input_manifest import digest, toolchain
    department = db.get(m.Department, workspace.department_id)
    organisation = db.get(m.Organisation, department.organisation_id)
    # Retain weak-identity-map parents for the entire pass.
    db.info.setdefault('_preparation_parents', {}).update({department.id: department, organisation.id: organisation})
    revisions = list(db.execute(select(m.Department.id, m.Department.revision, m.Department.catalogue_revision)
                    .where(m.Department.organisation_id == organisation.id).order_by(m.Department.id)))
    return {**currency_hint(db, workspace), 'validation_stamp': validation_stamp(db, workspace),
            'department_revisions': [list(row) for row in revisions],
            'toolchain': digest(toolchain(quota_policy_limit()))}


def domain_snapshot(db, workspace, *args, **kwargs):
    """Reuse detached preparation inputs only after checking current relational currency."""
    import pickle
    state = db.info.get('_planning_preparation')
    writer = db.connection().info.get('rota_write')
    supported = not args and all(key in {'shared_inputs', 'own_shifts', 'mandatory_rules', 'current_mandatory',
                                         'include_fairness', 'include_objectives', 'include_fairness_history'} or value is None
                                 for key, value in kwargs.items())
    if supported and state and state.get('kind') in {'shift.update', 'workspace.undo', 'workspace.redo'}:
        db.flush()
        index = state['next_snapshot']
        state['next_snapshot'] += 1
        stamp = preparation_stamp(db, workspace)
        key = snapshot_hash({'workspace': record(workspace, exclude=('validation',)), 'options': kwargs,
                             'stamp': stamp})
        cached = state['prepared_snapshots'].get(index)
        if cached and cached[0] == key and currency_match(stamp, stamp):
            return pickle.loads(cached[1])
        from .errors import require
        require(not state['replay'], 'STALE_INPUTS',
                'The rota or its scheduling inputs changed during preparation; refresh and apply the edit again',
                409, {'recoverable': True})
        value = _domain_snapshot(db, workspace, *args, **kwargs)
        state['prepared_snapshots'][index] = (key, pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
        return value
    if not supported or not (writer or state and not state['replay']):
        return _domain_snapshot(db, workspace, *args, **kwargs)
    db.flush()
    key = snapshot_hash({'workspace': record(workspace, exclude=('validation',)), 'options': kwargs})
    if state and not state['replay']:
        # Reuse across rollback rounds at the same check position. Later checks may
        # follow assignment writes that leave the workspace row unchanged.
        cache, epoch = state['snapshots'], state['next_check']
    else:
        cache, epoch = db.info.setdefault('write_domain_snapshots', {}), writer.get('snapshot_epoch', 0)
        # A new write epoch can never reuse an earlier snapshot. Drop its pickle
        # buffers immediately instead of retaining every full copy until commit.
        if cache and next(iter(cache))[0] != epoch:
            cache.clear()
    key = (epoch, key)
    if key not in cache:
        value = _domain_snapshot(db, workspace, *args, **kwargs)
        # Bytes are produced here only; preserve graph types/aliases without repeated Python deep copies.
        cache[key] = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        return value
    return pickle.loads(cache[key])


def publication_snapshot(db, workspace, evaluation: dict | None = None, *, own_shifts: list[dict] | None = None,
                         data: dict | None = None) -> dict:
    result = {"workspace": record(workspace, exclude=("validation", "submitted_fingerprint")),
              "shifts": workspace_shifts(db, workspace.id) if own_shifts is None else own_shifts}
    org = db.get(m.Organisation, db.get(m.Department, workspace.department_id).organisation_id)
    result["timezone"] = org.timezone
    result['week_start'] = org.week_start
    from packages.domain.patterns import wish_days, wish_outcomes, wish_worked
    if data is None:
        data = domain_snapshot(db, workspace, own_shifts=own_shifts)
    from packages.domain.supervision_teams import with_duty_ranks
    result['shifts'] = with_duty_ranks(data, result['shifts'])
    if 'score_baseline' in data:
        result['score_baseline'] = json_value(data['score_baseline'])
    from packages.domain.fairness import SEMANTICS as fairness_semantics
    result['fairness_semantics'] = data.get('fairness_semantics', fairness_semantics)
    from packages.contracts.schemas import DEFAULT_FAIR_SHARE_WINDOW
    result['fairness_default_window'] = json_value(data.get('fairness_default_window', DEFAULT_FAIR_SHARE_WINDOW))
    result['fairness_history_start'] = data.get('fairness_history_start')
    # Frozen dated qualifications let future supervision shares read issued opportunities.
    result['fairness_qualification'] = json_value({key: data.get(key, []) for key in
        ('memberships', 'grants', 'rotations', 'programme_memberships', 'eligibilities')})
    result['leadership'] = {'format': 1, 'workspace_id': workspace.id,
        'type_lead_ownership': json_value(data.get('type_lead_ownership', [])),
        'policies': json_value([p for p in data['policies'] if p['definition']['rule']['kind'] == 'team_lead_requirement'
            or p['definition']['rule'].get('id') == 'team_leads'])}
    result['department'] = {key: data['department'][key] for key in ('id', 'operating_calendar_id', 'weekend_days')}
    if evaluation is None:
        from packages.domain.validation import evaluate
        evaluation = evaluate(data)
    result['wishes'] = data['wishes']
    # Outcomes and their explanations must use the same final checked official-work context.
    worked = wish_worked(data)
    outcomes = wish_outcomes(data, worked)
    if result['wishes']:
        evaluated_days = {}
        for wish in result['wishes']:
            evaluated_days.setdefault(wish['person_id'], set()).update(wish_days(wish, data))
        # A later replacement trims the original official-work facts, never today's live context.
        result['wish_worked'] = {pid: {day.isoformat(): sorted(types)
                                      for day, types in worked.get(pid, {}).items() if day in days}
                                 for pid, days in evaluated_days.items()}
    receipt = (evaluation or {}).get('wish_receipt')
    if receipt is None:
        from packages.domain.wish_reasons import declined_wishes
        receipt = declined_wishes(data, evaluation)
    reasons = {row['wish_id']: row['reasons'] for row in receipt}
    result['wishes'] = [{**wish, 'outcome': 'granted' if outcomes[wish['id']][0] == 1 else
                        'declined' if outcomes[wish['id']][0] == 0 else 'partly',
                        'outcome_reasons': reasons.get(wish['id'], []) if outcomes[wish['id']][0] < 1 else []}
                       for wish in result['wishes']]
    result["availability_revision"] = org.availability_revision
    result["input_revision"] = org.input_revision
    person_ids = {a["person_id"] for s in result["shifts"] for a in s["participants"]}
    result["people"] = {pid: {"id": pid, "display_name": name} for pid, name in db.execute(
        select(m.Person.id, m.Person.display_name).where(m.Person.id.in_(person_ids)))}
    result["locations"] = {r.id: record(r) for r in db.scalars(select(m.Location).where(
        m.Location.id.in_({s['location_id'] for s in result['shifts']})))}
    service_ids = {sid for s in result["shifts"] for sid in s["service_ids"]}
    result["services"] = {r.id: record(r) for r in db.scalars(select(m.Service).where(m.Service.id.in_(service_ids)))}
    location_ids = set(result["locations"])
    for location_id in list(location_ids):
        row = db.get(m.Location, location_id)
        while row and row.parent_id and row.parent_id not in result["locations"]:
            row = db.get(m.Location, row.parent_id)
            result["locations"][row.id] = record(row)
    pool_ids = {d["resource_pool_id"] for shift in result["shifts"]
                for d in shift["type_snapshot"]["definition"].get("resources", [])}
    result["resource_pools"] = {pid: record(db.get(m.ResourcePool, pid)) for pid in pool_ids}
    calendar_ids = {r["calendar_id"] for r in list(result["locations"].values()) + list(result["resource_pools"].values()) + list(result["services"].values()) if r.get("calendar_id")}
    result["operating_calendars"] = {cid: record(db.get(m.OperatingCalendar, cid)) for cid in calendar_ids}
    department_calendar_id = result['department']['operating_calendar_id']
    if department_calendar_id in data.get('operating_calendars', {}):
        result['operating_calendars'][department_calendar_id] = json_value(data['operating_calendars'][department_calendar_id])
    from .services.date_sets import data as date_set_data
    result['date_sets'] = date_set_data(db, [workspace.department_id])
    from packages.domain.fairness import offered_intervals
    from packages.domain.reports import METRIC_VERSION
    result["metric_version"] = METRIC_VERSION
    states = {row['id']: row for row in data['shifts']}
    result['shifts'] = [{**row, **{key: states[row['id']].get(key) for key in SERIES_STATE_FIELDS},
                         'series_state_frozen': True} if 'closed' in states.get(row['id'], {}) else row for row in result['shifts']]
    result['overtime_offered_intervals'] = json_value(offered_intervals(
        {**data, 'scheduling_facts': _offer_history(db, data['scheduling_facts'])},
        workspace.starts_on, workspace.ends_on, person_ids))
    if evaluation.get("quota_report"):
        import copy
        result["quota_report"] = copy.deepcopy(evaluation["quota_report"])
        result["quota_report"]["source_state"] = workspace.status
    return result
