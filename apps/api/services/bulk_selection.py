"""Bounded, department-scoped collection reads shared by bulk preview, apply and the Sheet view."""
from collections import Counter, OrderedDict
from datetime import date, timedelta
import hashlib
import json
from threading import RLock
from time import monotonic
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from sqlalchemy import Date, DateTime, String, and_, case, false, func, literal, or_, select, union_all

from apps.api import models as m
from apps.api.auth import CATALOGUE_READ_ROLES, require_catalogue_read, require_role
from apps.api.errors import Problem, require
from apps.api.serialization import json_value, record
from packages.contracts import schemas as s
from packages.domain import conditions
from packages.domain.time import aware, local_midnight
from packages.domain.validation import ancestor_ids, index_person_rows
from .common import CATALOGUES
from .independence import today
from .staff_import import match_key, match_people

MAX_CANDIDATES = s.BULK_LIMITS["resolve_candidates"]
READERS = CATALOGUE_READ_ROLES
MODELS = {**CATALOGUES, "eligibilities": m.Eligibility, "grants": m.CapabilityGrant,
          "memberships": m.Membership, "availability": m.Availability, "requests": m.RequestRecord,
          "availability_patterns": m.AvailabilityPattern, "wishes": m.StaffWish, "swaps": m.SwapRequest,
          "scheduling_facts": m.SchedulingFact, "policies": m.Policy, "policy_lines": m.Policy,
          "exceptions": m.PolicyException, "sessions": m.Shift, "roles": m.RoleBinding,
          "identities": m.Identity, "inbox": m.Notification, "outbox": m.Outbox,
          "imports": m.ImportBatch, "history": m.AuditEvent, "feedback": m.FeedbackReport,
          "closures": m.LocationClosure, "issues": m.Workspace}
PRIVATE = ("private_reason", "password_hash", "rows", "screenshot", "screenshot_media_type", "payload")
# Cache only page identities, never personal record values or ORM objects. Writes always resolve live.
_ROWS_CACHE = WeakKeyDictionary()
_ROWS_CACHE_LOCK = RLock()
_ROWS_CACHE_ENTRIES = 32
_ROWS_CACHE_PER_USER = 4
_ROWS_CACHE_SECONDS = 60


def _value(value):
    return value.model_dump(mode="json", exclude_none=True, exclude_unset=True) if hasattr(value, "model_dump") else value


def _read_column(cls, name):
    return cls.attachment_mentor_id.label(name) if cls is m.Rotation and name == "mentor_id" else getattr(cls, name)


def _read_columns(cls, excluded):
    columns = [_read_column(cls, c.key) for c in cls.__mapper__.column_attrs if c.key not in excluded]
    if cls in {m.RoleBinding, m.Notification}:
        columns.append(m.Identity.person_id)
    return columns


def _private_columns(actor, collection):
    # Requests hide draft rota identifiers from everyone outside the planning team.
    from .contacts import LEGACY_FIELDS
    # Contact values and their role-dependent predicates belong to the shared person/contact reader.
    return set(PRIVATE) | ({'details'} if collection == 'requests' else set()) | (LEGACY_FIELDS if collection == 'people' else set()) | (
        {"workspace_id"} if collection == "requests" and not actor.permits("coordinator", "approver") else set())


def _people_query(actor):
    # A person in another department of the organisation must not appear in pasted-name diagnostics.
    identities = select(m.Identity.id).where(m.Identity.person_id == m.Person.id).correlate(m.Person)
    return select(m.Person).where(m.Person.organisation_id == actor.organisation_id, or_(
        select(m.DefinitionHistory.id).where(m.DefinitionHistory.department_id == actor.department_id,
            m.DefinitionHistory.kind == "people", m.DefinitionHistory.object_id == m.Person.id).exists(),
        select(m.Membership.id).where(m.Membership.department_id == actor.department_id,
            m.Membership.person_id == m.Person.id).exists(),
        select(m.RoleBinding.id).where(m.RoleBinding.department_id == actor.department_id,
            m.RoleBinding.identity_id.in_(identities)).exists(),
        select(m.Session.id).where(m.Session.department_id == actor.department_id,
            m.Session.identity_id.in_(identities)).exists()))


def collection_readers(collection):
    """The rows guard and generated record-type picker share these read permissions."""
    if collection in {"sessions", "issues"}:
        return s.PERMISSIONS["schedule.read_team"]
    if collection == "requests":
        return ("staff", "coordinator", "approver", "programme_lead")
    if collection in {"availability", "swaps"}:
        return ("staff", "coordinator", "approver")
    if collection in {"availability_patterns", "feedback", "inbox"}:
        return s.ROLE_NAMES
    if collection == "wishes":
        return ("staff", "coordinator", "approver", "programme_lead", "auditor")
    if collection in {"identities", "roles"}:
        return ("admin", *s.PERMISSIONS["access.system_roles"])
    if collection in {"department_content", "events"}:
        return ("admin", "coordinator")
    if collection in {"history", "outbox"}:
        return s.PERMISSIONS["audit.read" if collection == "history" else "system.operations"]
    if collection == "imports":
        return ("coordinator",)
    if collection == "scheduling_facts":
        from .scheduling_facts import READERS as FACT_READERS
        return FACT_READERS
    if collection in s.CATALOGUE_READS:
        return s.PERMISSIONS[s.CATALOGUE_READS[collection]['permission']]
    return READERS


def _base_query(db, actor, collection):
    if collection in s.CATALOGUE_READS:
        require_catalogue_read(actor, collection)
    if collection == "events":
        from .events import require_definition_access
        require_definition_access(actor)
    if collection not in {"availability_patterns", "feedback", "inbox"}:
        require_role(actor, *collection_readers(collection))
    cls = MODELS[collection]
    query = select(cls)
    # Counts wrap these queries in a Core UNION; carry live predicates into it explicitly.
    if issubclass(cls, m.Withdrawable):
        query = query.where(cls.withdrawn.is_(False))
    elif cls is m.Mentorship:
        from apps.api.db import live_mentorship_clause
        query = query.where(live_mentorship_clause())
    if cls is m.Person:
        query = _people_query(actor)
    elif cls is m.Identity:
        from apps.api.settings import Settings
        from .access import sign_in_identities
        settings = db.get_bind().get_execution_options().get("rota_write_settings") or Settings()
        query = query.where(sign_in_identities(actor, settings))
    elif cls is m.Shift:
        query = query.join(m.Workspace).where(m.Workspace.department_id == actor.department_id)
    elif cls is m.Notification:
        query = query.join(m.Outbox).join(m.Identity, cls.identity_id == m.Identity.id).where(
            m.Outbox.department_id == actor.department_id, cls.identity_id == actor.identity_id)
    elif cls is m.RoleBinding:
        from .access import SWAP_AUTOMATION_ISSUER
        query = query.join(m.Identity).where(cls.department_id == actor.department_id, m.Identity.issuer != SWAP_AUTOMATION_ISSUER)
    else:
        query = query.where(cls.department_id == actor.department_id)
    if cls is m.AuditEvent:
        from .audit_log import read_scope
        query = query.where(read_scope(actor))
    if collection in CATALOGUES:
        from .training import catalogue_scope
        query = query.where(catalogue_scope(db, actor, collection, cls))
    if collection in {"availability", "requests", "swaps"}:
        if collection == 'availability':
            from .availability import visible_record_clause
            query = query.where(visible_record_clause(cls))
        if not actor.permits("coordinator", "approver"):
            if not actor.person_id:
                # An unlinked identity is not the null recipient of an unclaimed open offer.
                own = false()
            else:
                own = (or_(cls.proposer_person_id == actor.person_id, cls.recipient_person_id == actor.person_id)
                       if collection == "swaps" else cls.person_id == actor.person_id)
            if collection == "requests" and actor.permits("programme_lead"):
                from .training import request_scope
                own = or_(own, request_scope(db, actor))
            query = query.where(own)
    elif collection == "availability_patterns":
        if not actor.permits("coordinator", "approver"):
            query = query.where(cls.person_id == actor.person_id)
    elif collection == "wishes":
        if not actor.permits("coordinator", "approver", "programme_lead", "auditor"):
            query = query.where(cls.person_id == actor.person_id if actor.person_id else false())
    elif collection == "feedback":
        if not actor.permits(*s.PERMISSIONS["feedback.read_all"]):
            query = query.where(cls.reporter_identity_id == actor.identity_id)
    elif collection == "scheduling_facts":
        if actor.permits("programme_lead") and not actor.permits("admin", "coordinator", "approver", "auditor"):
            from .scheduling_facts import scoped_query
            query = query.where(cls.id.in_(scoped_query(db, actor).with_only_columns(cls.id)))
    return query


def service_locations(db, department_id):
    """The shared clinic footprint from duties and recurring templates."""
    locations = {}
    for service_id, location_id in db.execute(select(m.ShiftService.service_id, m.Shift.location_id).join(
            m.Shift, m.Shift.id == m.ShiftService.shift_id).join(m.Workspace).where(
            m.Workspace.department_id == department_id).distinct()):
        locations.setdefault(service_id, set()).add(location_id)
    for row in db.scalars(select(m.RecurringSeries).where(m.RecurringSeries.department_id == department_id)):
        for service_id in row.definition.get("service_ids", []):
            locations.setdefault(service_id, set()).add(row.location_id)
    return locations



def _context(db, actor, day, person_ids, *, fields=None, service_ids=()):
    if fields is not None and fields <= {"roles", "person_ids", "category_ids", "programme_ids", "tags",
                                         "service_ids", "location_ids", "assignment_type_ids", "date_set_ids"}:
        from apps.api.snapshots import tag_data
        data, start, end = _page_context(db, actor, day, person_ids, set(service_ids), fields - {"tags"})
        data["tags"] = tag_data(db, [actor.department_id])
        if "date_set_ids" in fields:
            data["date_sets"] = {r.id: record(r) for r in db.scalars(select(m.DateSet).where(m.DateSet.department_id == actor.department_id))}
        return conditions.with_tags(data), start, end
    from .policies import people_data
    data, start, end = people_data(db, actor, day)
    data["_bulk_day"] = day
    data["_bulk_department"] = actor.department_id
    data["people"].update({p.id: record(p) for p in db.scalars(select(m.Person).where(
        m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(person_ids)))})
    data["locations"] = {r.id: record(r) for r in db.scalars(select(m.Location).where(m.Location.department_id == actor.department_id))}
    data["services"] = {r.id: record(r) for r in db.scalars(select(m.Service).where(m.Service.department_id == actor.department_id))}
    data["date_sets"] = {r.id: record(r) for r in db.scalars(select(m.DateSet).where(m.DateSet.department_id == actor.department_id))}
    data["_bulk_service_locations"] = service_locations(db, actor.department_id)
    data["_person_rows"] = index_person_rows(data)
    return data, start, end


def _row_person(row, collection):
    return (row["id"] if collection == "people" else
            row.get("reporter_person_id") if collection == "feedback" else row.get("person_id"))


def _linked_people(row, collection):
    person = _row_person(row, collection)
    return ({person} if person else set()) | _parent_ids(row, "person_id") | (
        {p for p in (row.get("proposer_person_id"), row.get("recipient_person_id")) if p} if collection == "swaps" else set())


def _record_key(row, collection):
    return (row["policy_id"], row["id"]) if collection == "policy_lines" else row["id"]


def _person_column(cls):
    return (m.Identity.person_id if cls in {m.RoleBinding, m.Notification} else
            cls.reporter_person_id if cls is m.FeedbackReport else getattr(cls, "person_id", None))


def _literal_search(column, text):
    pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return column.ilike(pattern, escape="\\")


def _linked_name_matches(db, actor, lines, records, collection):
    people = {pid for row in records for pid in _linked_people(row, collection)}
    result = match_people(db, actor, lines, people=list(db.scalars(_people_query(actor).where(m.Person.id.in_(people)))))
    by_person = {row["line"]: [row["id"]] for row in result["matches"]}
    by_person.update({row["line"]: [p["id"] for p in row["candidates"]] for row in result["ambiguous"]})
    by_id, index = {}, {}
    if collection in {"identities", "roles", "inbox"}:
        ids = {row["id"] if collection == "identities" else row["identity_id"] for row in records}
        by_id = dict(db.execute(select(m.Identity.id, m.Identity.display_name).where(m.Identity.id.in_(ids))).all())
    for row in records:
        for name in (row.get("name"), row.get("short_code"),
                     by_id.get(row["id"] if collection == "identities" else row.get("identity_id"))):
            if name:
                index.setdefault(match_key(name), {})[_record_key(row, collection)] = row
    ambiguous = {row["line"] for row in result["ambiguous"]}
    matches, selected, count = [], set(), 0
    for line in lines:
        people = set(by_person.get(line, []))
        hits = [_record_key(row, collection) for row in records if _linked_people(row, collection) & people]
        if hits and line not in ambiguous:
            selected.update(hits)
        elif not people and (own := list(index.get(match_key(line), {}).values())):
            identities = {row.get("identity_id", row["id"]) for row in own}
            hits = [_record_key(row, collection) for row in own]
            result["unmatched"] = [value for value in result["unmatched"] if value != line]
            if len(identities) == 1:
                selected.update(hits)
            else:
                result["ambiguous"].append({"line": line, "candidates": [
                    {"id": identity_id, "name": by_id.get(identity_id, next((row.get("name") for row in own
                        if row["id"] == identity_id), None))} for identity_id in sorted(identities)]})
        count += len(hits)
        require(count <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 matching record IDs; narrow the pasted list", 422)
        matches.append({"line": line, "ids": hits})
    return result, matches, selected


def _person_roles(db, actor, day):
    roles = {}
    for person, role in db.execute(select(m.Identity.person_id, m.RoleBinding.role).join(m.RoleBinding).where(
            m.RoleBinding.department_id == actor.department_id, m.RoleBinding.revoked.is_(False),
            m.RoleBinding.starts_on <= day, or_(m.RoleBinding.ends_on.is_(None), m.RoleBinding.ends_on > day))):
        if person:
            roles.setdefault(person, set()).add(role)
    return roles


def _line_people(db, actor, records, day, *, context=None, fields=(), target_people=None):
    if context is None:
        people = sorted(target_people) if target_people is not None else list(
            db.scalars(_people_query(actor).with_only_columns(m.Person.id).limit(MAX_CANDIDATES + 1)))
        require(len(people) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        services = [] if target_people is not None else list(
            db.scalars(_base_query(db, actor, "services").with_only_columns(m.Service.id).limit(MAX_CANDIDATES + 1)))
        require(len(services) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        scope_fields = set().union(*(s.Conditions.model_validate(row["who"]).fields()
            for row in records if row.get("who"))) if target_people is not None else set().union(*(
            s.Conditions.model_validate(row[key]).fields()
            for row in records for key in ("who", "where") if row.get(key)))
        context = _context(db, actor, day, people, fields=scope_fields | set(fields), service_ids=services) \
            if scope_fields or fields or target_people is None else ({"_bulk_line_candidates": people,
                "_bulk_line_services": services}, None, None)
        context[0]["_bulk_line_candidates"] = people
        context[0]["_bulk_line_services"] = services
    data, start, end = context
    if all("person_ids" in row and "service_ids" in row for row in records):
        return context
    roles = _person_roles(db, actor, day) if any("roles" in s.Conditions.model_validate(row["who"]).fields()
        for row in records if row.get("who")) else {}

    def clinic_matches(where, sid):
        held = _held(data, start, end, {"id": sid}, "services", roles)
        # Duty/person attributes are unknown when projecting a clinic, as in canonical rule scopes.
        return conditions.evaluate(where, lambda field: held(field) if field in {"service_ids", "location_ids", "tags"} else None)

    for row in records:
        who = conditions.tag_scope(data, {"conditions": row["who"]})["conditions"] if row.get("who") else None
        row["person_ids"] = [pid for pid in sorted(data["_bulk_line_candidates"]) if not who or not who.get("conditions") or
            conditions.evaluate(who, _held(data, start, end, {"id": pid}, "people", roles))]
        if target_people is not None:
            row["service_ids"] = []
            continue
        where = conditions.tag_scope(data, {"conditions": row["where"]})["conditions"] if row.get("where") else None
        row["service_ids"] = [sid for sid in sorted(data["_bulk_line_services"]) if not where or not where.get("conditions") or
            clinic_matches(where, sid)]
    return context


def _choices(value):
    if value is None or value == "":
        return set()
    result = set()
    for item in value if isinstance(value, (list, tuple, set, frozenset)) else (value,):
        if isinstance(item, bool):
            result.update((item, "true" if item else "false", "Yes" if item else "No"))
        elif isinstance(item, (dict, list)):
            result.add(json.dumps(json_value(item), ensure_ascii=False, separators=(",", ":")))
        else:
            result.add(item)
    return result



def _held(data, start, end, row, collection, roles, *, fields=None):
    fields = s.CONDITION_FIELDS if fields is None else fields
    person_id = _row_person(row, collection)
    person_holds = data.setdefault("_bulk_person_holds", {})
    if person_id not in person_holds:
        person_holds[person_id] = conditions.rule_holds(data, person_id, None, start, end)
    held = person_holds[person_id]
    own = {field: set(row.get(field) or []) for field in fields if isinstance(row.get(field), list)}
    if collection == "recurring_series":
        own["service_ids"] = set(row.get("definition", {}).get("service_ids", []))
        own["person_ids"] = set(row.get("definition", {}).get("default_person_ids", []))
    if collection in {"sessions", "recurring_series"}:
        from packages.domain.recurrence import usual_assignee_ids
        own["usual_assignee_ids"] = set(usual_assignee_ids(row if collection == "sessions" else {"series_snapshot": row}))
    if collection in {"swaps", "sessions", "recurring_series"}:
        for field in set(fields) & conditions.PERSON_FIELDS:
            own[field] = set().union(*(_held(data, start, end, {"id": pid}, "people", roles)(field)
                for pid in _linked_people(row, collection)))
    if collection == "scheduling_facts" and (row.get("data") or {}).get("programme_id"):
        own["programme_ids"] = {row["data"]["programme_id"]}
    if collection == "policy_lines":
        for field in fields:
            values = _condition_values(row.get("where"), field)
            if field in conditions.PERSON_FIELDS:
                values = set().union(*(_held(data, start, end, {"id": pid}, "people", roles)(field)
                                      for pid in row.get("person_ids", [])))
            elif field in {"service_ids", "location_ids"}:
                values = set().union(*(_held(data, start, end, {"id": sid}, "services", roles)(field)
                    for sid in row.get("service_ids", [])))
            if values:
                own[field] = {v.lower() for v in values} if field == "tags" else values
            elif field in conditions.PERSON_FIELDS:
                own[field] = set()
    for field in s.CONDITION_FIELD_DEFINITIONS:
        key = field["key"]
        if key not in fields or key == "roles":
            continue
        singular = key.removesuffix("s")
        if row.get(singular):
            own[key] = {row[singular]}
        if collection == field.get("kind"):
            own[key] = {row["id"]}
    if "location_ids" in fields and (row.get("location_id") or collection == "locations"):
        own["location_ids"] = set(ancestor_ids(data, row["id"] if collection == "locations" else row["location_id"]))
    elif "location_ids" in fields and (row.get("service_id") or collection == "services"):
        own["location_ids"] = {ancestor for location in data["_bulk_service_locations"].get(
            row["id"] if collection == "services" else row["service_id"], ()) for ancestor in ancestor_ids(data, location)}
    if "tags" in fields and collection != "people" and collection in s.TAG_KINDS:
        own["tags"] = {tag["name"].lower() for tag in data.get("tags", {}).values()
                       if row["id"] in tag.get("members", {}).get(collection, [])}

    def holds(field):
        metadata = s.condition_field_definition(field, collection) or {}
        source_field = metadata.get("operand_field") or field
        related = s.BULK_COLLECTIONS.get(collection, {}).get("related_columns", {}).get(field)
        if related and related.get("collection") == "people" and person_id:
            if source_field in conditions.PERSON_FIELDS:
                canonical = {person_id} if source_field == "person_ids" else held(source_field) or set()
                display = data["people"].get(person_id, {}).get(related["field"])
                return _choices(canonical) | _choices(display) if metadata.get("display_values") else canonical
            source = related["field"]
            return (held(source) or set()) if source in conditions.PERSON_FIELDS else _choices(data["people"].get(person_id, {}).get(source))
        if metadata.get("value_kind") in {"date", "text", "number"}:
            if source_field == "date":
                return {aware(row["start"]).astimezone(ZoneInfo(data["organisation"]["timezone"])).date() if row.get("start") else data["_bulk_day"]}
            if source_field in {"first_day", "last_day"}:
                return _choices(row.get(field, row.get(source_field, row.get("starts_on" if source_field == "first_day" else "ends_on"))))
            if collection == "policy_lines" and metadata.get("value_kind") == "number" and source_field not in row:
                value = row.get("numbers", {}).get(source_field)
                return _choices(value.get("value") if isinstance(value, dict) else value)
            return _choices(row.get(field, row.get(source_field, data["people"].get(person_id, {}).get(source_field))))
        if source_field != field and metadata.get("display_values") and field in row:
            if related and related.get("collection") == "services" and related["field"] == "tags":
                return _choices(row[field])
            return _choices(holds(source_field)) | _choices(row[field])
        if field == "status":
            status = row.get(field) or _row_dated_status(row, data["_bulk_day"], data["organisation"]["timezone"])
            return {str(status).casefold(), str(status).title()} if status else set()
        if field not in s.CONDITION_FIELDS and field in row:
            return _choices(row[field])
        if source_field != field:
            return holds(source_field)
        if field == "department_ids":
            return {data["_bulk_department"]}
        if field == "rota_ids":
            return {row["workspace_id"]} if row.get("workspace_id") else set()
        if field in own:
            return own[field]
        if field == "roles":
            role_key = row["id"] if collection == "identities" else row.get("identity_id") if collection == "roles" else person_id
            return roles.get(role_key, set())
        if field == "date_set_ids":
            from packages.domain.date_sets import matching_date_sets
            return matching_date_sets(data, data["_bulk_day"])
        person_values = data.setdefault("_bulk_person_values", {})
        key = (person_id, field)
        if key not in person_values:
            person_values[key] = held(field)
        result = person_values[key]
        return result if result is not None else set()
    from packages.domain.date_sets import matching_date_sets
    holds.date_sets = lambda ids, names: matching_date_sets(data, data["_bulk_day"], ids, names)
    holds.row = row
    return holds


def _parent_ids(row, field):
    value = row.get(field)
    if value is None and field and field.endswith("_id"):
        value = row.get(field + "s")
    if value is None:
        value = (row.get("definition") or {}).get(field)
        if value is None and field.endswith("_id"):
            value = (row.get("definition") or {}).get(field + "s", [])
        if not value and field == "person_id":
            value = (row.get("definition") or {}).get("default_person_ids", [])
    return set(value if isinstance(value, list) else [value] if value else [])


def _status_column(cls, day, zone):
    ended = [getattr(cls, flag).is_(True) for flag in ("revoked", "retracted") if hasattr(cls, flag)]
    if hasattr(cls, "status"):
        status = cls.status
    elif cls is m.Identity:
        status = case((cls.disabled.is_(True), "disabled"), else_="enabled")
    elif cls is m.Notification:
        status = case((cls.read_at.is_(None), "unread"), else_="read")
    elif cls is m.Outbox:
        status = cls.state
    elif hasattr(cls, "starts_on"):
        status = case((cls.starts_on > day, "future"), (cls.ends_on <= day, "ended"), else_="current")
    elif hasattr(cls, "start"):
        start, end = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
        status = case((cls.start >= end, "future"), (cls.end <= start, "ended"), else_="current")
    elif hasattr(cls, "active"):
        status = case((cls.active.is_(True), "active"), else_="inactive")
    else:
        status = literal(None, type_=String)
    if not hasattr(cls, "status") and hasattr(cls, "active") and hasattr(cls, "starts_on"):
        status = case((cls.active.is_(False), "inactive"), else_=status)
    if hasattr(cls, "cancelled"):
        status = case((cls.cancelled.is_(True), "cancelled"), else_=status)
    return case((or_(*ended), "ended"), else_=status) if ended else status


def _status_values(collection, values):
    choices = {option["value"] for option in s.BULK_COLLECTIONS[collection]["status"]["options"]}
    values = [str(v).casefold() for v in values]
    if "current" in choices:
        values = ["current" if v == "active" else v for v in values]
    require(bool(choices) and set(values) <= choices, "INVALID_FILTER", "Choose a valid status filter")
    return values


def _status_tree(collection, tree):
    for child in tree["conditions"]:
        if "conditions" in child:
            _status_tree(collection, child)
        elif child["field"] == "status" and child.get("op") not in {"empty", "not_empty"}:
            values = _status_values(collection, conditions.absolute_values(child) or [])
            if child.get("value") is None:
                child["values"] = values
            else:
                operand = child["value"]
                absolute = operand.get("value") if isinstance(operand, dict) else operand
                child["value"] = {"kind": "absolute", "value": values if isinstance(absolute, list) else values[0]}


def _project_records(db, actor, collection, source):
    records = [json_value(dict(row)) for row in source]
    if collection in {"policies", "policy_lines"}:
        from .scoring_defaults import seeded_rows
        records = seeded_rows(db, records)
    if collection == "wishes":
        from .wishes import outcome_reasons
        for row in records:
            row["outcome_reasons"] = outcome_reasons(actor, row.get("outcome_reasons"))
    if collection == "events":
        from .events import event_tag_ids
        for department in {row["department_id"] for row in records}:
            tags = event_tag_ids(db, department, [row["id"] for row in records if row["department_id"] == department])
            for row in records:
                if row["id"] in tags:
                    row["tag_ids"] = tags[row["id"]]
    if collection == "issues":
        from .issues import issue_key
        from packages.domain.rota_summary import normalized_check
        return [{**finding, "id": str(uuid5(NAMESPACE_URL, row["id"] + issue_key(finding))), "issue_key": issue_key(finding),
                 "workspace_id": row["id"], "revision": row["revision"], "status": row.get("status")}
                for row in records for finding in (normalized_check(row["validation"]) or {}).get("findings", [])]
    if collection == "sessions":
        by_id = {row["id"]: row for row in records}
        for row in records:
            row.update(service_ids=[], person_ids=[], participants=[])
        for shift_id, service_id in db.execute(select(m.ShiftService.shift_id, m.ShiftService.service_id).where(m.ShiftService.shift_id.in_(by_id))):
            by_id[shift_id]["service_ids"].append(service_id)
        for assignment in db.execute(select(m.Assignment.__table__).where(m.Assignment.shift_id.in_(by_id))).mappings():
            by_id[assignment["shift_id"]]["person_ids"].append(assignment["person_id"])
            by_id[assignment["shift_id"]]["participants"].append(json_value(dict(assignment)))
    if collection == "policy_lines":
        return [{**line, "id": line["id"], "line_id": line["id"], "policy_id": row["id"],
                 "revision": row["revision"], "name": row["name"], "status": row["status"],
                 "catalogue_id": row["definition"]["rule"].get("id")}
                for row in records for line in row["definition"].get("rule", {}).get("lines", [])]
    return records


def _matched_lines(matches, records, field, collection=None):
    if not matches:
        return []
    by_target = {}
    for row in records:
        key = _record_key(row, collection) if field == "id" else row.get(field)
        by_target.setdefault(key, []).append(row["id"])
    total = 0
    for match in matches:
        total += sum(len(by_target.get(target_id, [])) for target_id in match["ids"])
        require(total <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT",
                "More than 5,000 matching record IDs; narrow the pasted list", 422)
    return [{"line": match["line"], "ids": [record_id for target_id in match["ids"]
            for record_id in by_target.get(target_id, [])]} for match in matches]


def _row_dated_status(row, day, zone=None):
    if row.get("revoked") or row.get("status") == "retired":
        return "Ended"
    if "starts_on" in row or "ends_on" in row:
        starts = date.fromisoformat(row["starts_on"]) if isinstance(row.get("starts_on"), str) else row.get("starts_on")
        ends = date.fromisoformat(row["ends_on"]) if isinstance(row.get("ends_on"), str) else row.get("ends_on")
        return "Future" if starts and starts > day else "Ended" if ends and ends <= day else "Current"
    if zone and (row.get("start") or row.get("end")):
        start, end = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
        return "Future" if row.get("start") and aware(row["start"]) >= end else "Ended" if (
            row.get("end") and aware(row["end"]) <= start) else "Current"
    return None



def _records(db, actor, selection, depth=0, *, sample_only=False):
    selection = _value(selection)
    collection = selection.get("collection")
    require(collection in s.BULK_COLLECTIONS and collection in MODELS, "BULK_COLLECTION", "Choose a supported collection")
    cls, registry = MODELS[collection], s.BULK_COLLECTIONS[collection]
    query = _base_query(db, actor, collection)
    where = selection.get("where") or {}
    included_ids = set(selection.get("include_ids", []))
    explicit_only = "include_ids" in selection and not (selection.get("parent") or selection.get("names")
        or where.get("conditions") or where.get("filters") or where.get("q")
        or (where.get("search_options") or {}).get("reference"))
    day = date.fromisoformat(where["as_of"]) if where.get("as_of") else None
    tree = s.CollectionConditions.model_validate(where["conditions"], context={"table_key": collection}).model_dump(mode="json") if where.get("conditions") else None
    tree_fields = s.CollectionConditions.model_validate(tree, context={"table_key": collection}).fields() if tree else set()
    if "status" in tree_fields:
        _status_tree(collection, tree)
    if collection == "policy_lines":
        filters = where.get("filters") or {}
        # A line ID survives replacement; without a version/status binding it denotes the active version.
        if (explicit_only or included_ids or "id" in filters or "id" in tree_fields) and not (
                {"policy_id", "status"} & (filters.keys() | tree_fields)) and (
                selection.get("parent", {}).get("collection") != "policies"):
            query = query.where(cls.status == "active")
    private = _private_columns(actor, collection)
    condition_fields = (_column_names(collection, actor) | set(s.CONDITION_FIELDS)) - private
    if "workspace_id" in private:
        condition_fields.discard("rota_ids")
    condition_fields |= {entry["key"] for entry in s.table_condition_definitions(collection)
                         if entry.get("operand_field") in condition_fields and entry["key"] not in private}
    require(tree_fields <= condition_fields and set(where.get("filters") or {}) <= condition_fields,
            "INVALID_FILTER", "Choose readable collection fields")

    parent, parent_field = None, None
    if selection.get("parent"):
        require(depth == 0, "BULK_PARENT_DEPTH", "Choose at most one parent level")
        parent_spec = selection["parent"]
        parent_selection = {**(parent_spec.get("selection") or {}), "collection": parent_spec["collection"]}
        parent = _records(db, actor, parent_selection, depth=depth + 1)
        parent_field = registry.get("parent_of", {}).get(parent_spec["collection"])
        require(bool(parent_field), "BULK_PARENT", "This collection does not have that parent")
        parent_ids = {row["id"] for row in parent["records"]}
        if hasattr(cls, parent_field) and collection != "policy_lines":
            query = query.where(getattr(cls, parent_field).in_(parent_ids))
        elif collection == "policy_lines" and parent_field == "policy_id":
            query = query.where(cls.id.in_(parent_ids))
    else:
        parent_ids = set()

    deferred = {}
    for field, value in (where.get("filters") or {}).items():
        values = value if isinstance(value, list) else [value]
        if field == "status":
            require(all(isinstance(v, str) for v in values), "INVALID_FILTER", "Choose valid status values")
            values = _status_values(collection, values)
        if collection == "policy_lines":
            require(field in registry.get("fields", {}) or field in {"id", "policy_id", "status", "name", "revision"}
                    or field in registry.get("related_columns", {}) or field in s.CONDITION_FIELDS,
                    "INVALID_FILTER", f"Unknown collection filter: {field}")
            if isinstance(value, dict):
                require(field in {"starts_on", "ends_on"} and bool(value) and set(value) <= {"gte", "gt", "lte", "lt"}
                        and all(isinstance(v, str) for v in value.values()), "INVALID_FILTER", "Date ranges use gte, gt, lte or lt")
            else:
                require(all(isinstance(v, (str, int, float, bool)) or v is None for v in values), "INVALID_FILTER", "Choose valid filter values")
            if field == "policy_id":
                query = query.where(cls.id.in_(values))
            deferred[field] = value if isinstance(value, dict) else values
            if field in {"starts_on", "ends_on"}:
                try:
                    deferred[field] = ({op: date.fromisoformat(bound).isoformat() for op, bound in value.items()}
                                       if isinstance(value, dict) else [date.fromisoformat(bound).isoformat() for bound in values])
                except (TypeError, ValueError):
                    require(False, "INVALID_FILTER", "Choose dates for this filter")
        elif collection == "issues" and field == "id":
            require(all(isinstance(v, str) for v in values), "INVALID_FILTER", "Choose valid filter values")
            deferred[field] = values
        elif field == "status":
            day = day or today(db, actor.department_id)
            query = query.where(_status_column(cls, day, db.get(m.Organisation, actor.organisation_id).timezone).in_(values))
        elif field in cls.__table__.columns and field not in private:
            column = _read_column(cls, field)
            convert = aware if isinstance(column.type, DateTime) else date.fromisoformat if isinstance(column.type, Date) else lambda v: v
            if isinstance(value, dict):
                require(bool(value) and set(value) <= {"gte", "gt", "lte", "lt"} and isinstance(column.type, (Date, DateTime)),
                        "INVALID_FILTER", "Date ranges use gte, gt, lte or lt")
                for op, bound in value.items():
                    require(isinstance(bound, str), "INVALID_FILTER", "Choose a date for the range")
                    bound = convert(bound)
                    query = query.where({"gte": column.__ge__, "gt": column.__gt__, "lte": column.__le__, "lt": column.__lt__}[op](bound))
            elif isinstance(column.type, (String, Date, DateTime)) or field == "active":
                require(all(isinstance(v, (str, int, float, bool)) or v is None for v in values), "INVALID_FILTER", "Choose valid filter values")
                if isinstance(column.type, (Date, DateTime)):
                    require(all(isinstance(v, str) for v in values), "INVALID_FILTER", "Choose dates for this filter")
                    values = [convert(v) for v in values]
                query = query.where(column.in_(values))
            else:
                require(all(isinstance(v, (str, int, float, bool)) or v is None for v in values), "INVALID_FILTER", "Choose valid filter values")
                deferred[field] = values
        else:
            require(field in registry.get("related_columns", {}) or field in registry.get("columns", {}) or field in s.CONDITION_FIELDS,
                    "INVALID_FILTER", f"Unknown collection filter: {field}")
            deferred[field] = values

    names_result = {"matches": [], "unmatched": [], "ambiguous": []}
    name_matches, named_ids = [], set()
    linked_names = collection in {"identities", "roles", "inbox", "feedback", "sessions", "recurring_series", "policy_lines", "swaps", "issues"}
    name_field = "person_id" if collection != "people" and hasattr(cls, "person_id") else "id"
    if selection.get("names") and not linked_names:
        if collection == "people" or hasattr(cls, "person_id"):
            people_query = _people_query(actor)
            if collection == "requests" and actor.permits("programme_lead") and not actor.permits("coordinator", "approver"):
                people_query = people_query.where(m.Person.id.in_(
                    _base_query(db, actor, collection).with_only_columns(cls.person_id)))
            elif collection in {"availability", "requests", "availability_patterns", "wishes"} and not actor.permits("coordinator", "approver", *(('programme_lead', 'auditor') if collection == "wishes" else ())):
                people_query = people_query.where(m.Person.id == actor.person_id)
            visible_people = list(db.scalars(people_query.order_by(m.Person.id).limit(MAX_CANDIDATES + 1)))
            require(len(visible_people) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
            names_result = match_people(db, actor, selection["names"], people=visible_people)
            named_ids = {r["id"] for r in names_result["matches"]}
            by_line = {r["line"]: [r["id"]] for r in names_result["matches"]}
            by_line.update({r["line"]: [p["id"] for p in r["candidates"]] for r in names_result["ambiguous"]})
            name_matches = [{"line": line, "ids": by_line.get(line, [])} for line in selection["names"]]
            query = query.where(getattr(cls, name_field).in_({pid for match in name_matches for pid in match["ids"]}))

    from . import search as semantic_search
    search_options = s.SearchOptions.model_validate(where['search_options']) if where.get('search_options') is not None else None
    semantic_projection = search_options is not None and collection in {'policy_lines', 'issues'}
    semantic_found = semantic_search.matching_entities(db, actor,
        semantic_search.registry()[collection]['reference_kinds'], where.get('q'), search_options) if semantic_projection and search_options.scope != 'self' else {}
    if semantic_projection and collection == 'policy_lines' and semantic_found.get('tags'):
        semantic_found['_tagged_policies'] = set(db.scalars(select(m.TagMember.member_id).where(
            m.TagMember.department_id == actor.department_id, m.TagMember.kind == 'policies',
            m.TagMember.tag_id.in_(semantic_found['tags']))))
    if search_options is not None and not semantic_projection:
        query = query.where(semantic_search.predicate(db, actor, collection, where.get('q'), search_options, as_of=day))
    deferred_search = search_options is None and collection in {"policy_lines", "recurring_series", "issues", "swaps"}
    if search_options is None and where.get("q") and not deferred_search:
        text = where["q"].strip().casefold()
        fields = [getattr(cls, key) for key in ("name", "display_name", "short_code", "staff_number") if hasattr(cls, key)]
        searches = [_literal_search(column, text) for column in fields]
        person_column = _person_column(cls)
        if person_column is not None:
            matched = select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id,
                or_(*(_literal_search(column, text) for column in (m.Person.display_name, m.Person.short_code, m.Person.staff_number))))
            searches.append(person_column.in_(matched))
        if cls in {m.RoleBinding, m.Notification}:
            searches.append(_literal_search(m.Identity.display_name, text))
        if cls is m.Shift:
            searches.append(cls.id.in_(select(m.Assignment.shift_id).where(m.Assignment.person_id.in_(
                select(m.Person.id).where(m.Person.organisation_id == actor.organisation_id,
                    or_(*(_literal_search(column, text) for column in (m.Person.display_name, m.Person.short_code, m.Person.staff_number))))))))
        if hasattr(cls, "service_id"):
            searches.append(cls.service_id.in_(select(m.Service.id).where(m.Service.department_id == actor.department_id,
                                                                          _literal_search(m.Service.name, text))))
        require(bool(searches), "INVALID_FILTER", "This collection does not support text search")
        query = query.where(or_(*searches))

    # Explicit ticks add to the matches, never to the department or permission scope.
    if collection not in {"policy_lines", "issues"}:
        if explicit_only:
            query = query.where(cls.id.in_(included_ids))
        elif included_ids:
            query = _base_query(db, actor, collection).where(or_(
                cls.id.in_(query.with_only_columns(cls.id)), cls.id.in_(included_ids)))
    if selection.get("exclude_ids") and collection not in {"policy_lines", "issues"}:
        query = query.where(cls.id.not_in(selection["exclude_ids"]))
    day = day or today(db, actor.department_id)
    columns = _read_columns(cls, private | {"status"})
    columns.append(_status_column(cls, day, db.get(m.Organisation, actor.organisation_id).timezone).label("status"))
    source = list(db.execute(query.with_only_columns(*columns).order_by(cls.id).limit(MAX_CANDIDATES + 1)).mappings())
    require(len(source) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
    if collection in {"wishes", "policies"}:
        source = _project_records(db, actor, collection, source)
    if not tree and not deferred and not parent_field and collection not in {"policy_lines", "issues", "sessions", "events"} \
            and not (where.get("q") and deferred_search) \
            and (not selection.get("names") or not linked_names and (collection == "people" or hasattr(cls, "person_id"))):
        records = [dict(row) for row in source]
        matches = _matched_lines(name_matches, records, name_field, collection)
        if name_matches:
            records = [r for r in records if r.get(name_field) in named_ids or r["id"] in included_ids]
        # Resolve exposes only its sample; the remaining rows contribute only IDs/revisions to the token.
        count = s.BULK_LIMITS["sample_rows"] if sample_only else len(records)
        records[:count] = [json_value(row) for row in records[:count]]
        return {"collection": collection, "records": records, "matches": matches, "unmatched": names_result["unmatched"],
                "ambiguous": names_result["ambiguous"], "by_parent": {}}
    plain = collection not in {"policy_lines", "issues", "sessions", "events"}
    records = [dict(row) for row in source] if plain else _project_records(db, actor, collection, source)
    status_bound = "status" in deferred or "status" in tree_fields
    if collection == "policy_lines" and included_ids and "policy_id" in deferred and parent_field != "policy_id":
        # Bound versions win; other owners retain explicit statuses, or default to active.
        unbound_ids = included_ids - {row["id"] for row in records}
        if unbound_ids:
            owner_query = _base_query(db, actor, collection)
            if not status_bound:
                owner_query = owner_query.where(cls.status == "active")
            owners = list(db.execute(owner_query
                .with_only_columns(*columns).order_by(cls.id).limit(MAX_CANDIDATES + 1)).mappings())
            require(len(owners) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
            records.extend(row for row in _project_records(db, actor, collection, owners) if row["id"] in unbound_ids)
    included_lines = None
    version_bindings = set()
    if collection == "policy_lines" and included_ids and ("policy_id" in deferred or "policy_id" in tree_fields):
        version_bindings = set(deferred.get("policy_id", ())) | _condition_values(tree, "policy_id", ops={"any", "all", "is"})
        bound_lines = {row["id"] for row in records if row["policy_id"] in version_bindings}
        included_lines = {_record_key(row, collection) for row in records if row["id"] in included_ids and (
            row["policy_id"] in version_bindings or (status_bound or row["status"] == "active") and row["id"] not in bound_lines)}
    if collection == "policy_lines" and "status" in deferred:
        records = [row for row in records if _matches_filter(
            row["status"], deferred["status"], row=row, field="status", day=day)]
    line_context = None
    if collection in {"policy_lines", "issues"}:
        if explicit_only:
            records = [r for r in records if r["id"] in included_ids]
        elif "id" in deferred:
            record_ids = set(map(str, deferred["id"])) | included_ids
            records = [r for r in records if r["id"] in record_ids]
        records = [r for r in records if r["id"] not in selection.get("exclude_ids", [])]
        require(len(records) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
    if selection.get("names") and linked_names:
        if collection == "policy_lines":
            line_context = _line_people(db, actor, records, day, fields=set(s.CONDITION_FIELDS))
        names_result, name_matches, named_ids = _linked_name_matches(db, actor, selection["names"], records, collection)
        name_field = "id"
    elif selection.get("names") and collection != "people" and not hasattr(cls, "person_id"):
        index = {}
        for row in records:
            for key in ("name", "short_code"):
                if row.get(key):
                    index.setdefault(match_key(row[key]), {})[row["id"]] = row
        for line in selection["names"]:
            hits = list(index.get(match_key(line), {}).values())
            name_matches.append({"line": line, "ids": [r["id"] for r in hits]})
            if len(hits) == 1:
                named_ids.add(hits[0]["id"])
            elif hits:
                names_result["ambiguous"].append({"line": line, "candidates": [{"id": r["id"], "name": r.get("name")} for r in hits]})
            else:
                names_result["unmatched"].append(line)
        candidate_ids = {rid for match in name_matches for rid in match["ids"]} | included_ids
        records = [r for r in records if r["id"] in candidate_ids]
    needs_context = tree or deferred or parent_field and not hasattr(cls, parent_field) or collection == "policy_lines" and where.get("q") and search_options is None
    if needs_context:
        day = day or today(db, actor.department_id)
    people = {pid for row in records for pid in _linked_people(row, collection)}
    fields = (tree_fields | set(deferred)) & set(s.CONDITION_FIELDS)
    fields |= {(s.condition_field_definition(field, collection) or {}).get("operand_field", field)
               for field in tree_fields | set(deferred)} & set(s.CONDITION_FIELDS)
    fields |= {spec["field"] for key, spec in registry.get("related_columns", {}).items() if key in set(deferred) | tree_fields}
    if parent_field == "tag_id" or parent_field and collection == "policy_lines":
        fields = set(s.CONDITION_FIELDS)
    services = {sid for row in records for sid in _parent_ids(row, "service_id")}
    if collection == "services":
        services.update(row["id"] for row in records)
    context = (line_context or _line_people(db, actor, records, day, fields=fields) if collection == "policy_lines" else
               _context(db, actor, day, people, fields=fields, service_ids=services)) if needs_context else None
    evaluation = None
    if tree:
        from .condition_context import validate_tree
        evaluation = validate_tree(db, actor, tree, as_of=day, workspace_id=where.get("workspace_id"),
                                   context="selection", collection=collection)
    roles = {}
    if "roles" in fields:
        role_key = m.Identity.id if collection in {"identities", "roles"} else m.Identity.person_id
        for key, role in db.execute(select(role_key, m.RoleBinding.role).select_from(m.Identity).join(m.RoleBinding).where(
                m.RoleBinding.department_id == actor.department_id, m.RoleBinding.revoked.is_(False),
                m.RoleBinding.starts_on <= day, or_(m.RoleBinding.ends_on.is_(None), m.RoleBinding.ends_on > day))):
            if key is not None:
                roles.setdefault(key, set()).add(role)
    related = _related(db, actor, collection, records, day, context) if deferred or tree_fields & registry.get('related_columns', {}).keys() else {}
    search_people = {}
    if where.get("q") and deferred_search:
        linked = {pid for row in records for pid in _linked_people(row, collection)}
        search_people = {row.id: record(row) for row in db.scalars(_people_query(actor).where(m.Person.id.in_(linked)))}
    kept, matched_records, by_parent = [], [], Counter()
    resolved_tree = conditions.tag_scope(context[0], {"conditions": tree})["conditions"] if tree else None
    related_aliases = {key: spec["field"] for key, spec in registry.get("related_columns", {}).items()}
    person_aliases = {key: spec["field"] for key, spec in registry.get("related_columns", {}).items()
                      if spec["collection"] == "people"}
    condition_filters = [{"field": field, "values": [str(value) for value in values]}
                         for field, values in deferred.items() if not isinstance(values, dict) and (
                             field in s.CONDITION_FIELDS and (s.condition_field_definition(field, collection) or {}).get(
                                 "value_kind") not in {"date", "text", "number"} or field in related_aliases)]
    if condition_filters:
        flat_tree = conditions.tag_scope(context[0], {"conditions": {"conditions": condition_filters}})["conditions"]
        resolved_tree = {"conditions": ([resolved_tree] if resolved_tree else []) + [flat_tree]}
    def combined(person_holds, witness=None, person_id=None):
        def holds(field):
            metadata = s.condition_field_definition(field, collection) or {}
            canonical = metadata.get("operand_field") or field
            if witness is not None and canonical in conditions.PERSON_FIELDS:
                values = witness(canonical)
                if metadata.get("display_values") and field in person_aliases:
                    display = _person_column_value(*context, person_id, person_aliases[field], roles)
                    return _choices(values) | _choices(display)
                return values
            if person_id is not None and field in person_aliases:
                return _choices(_person_column_value(*context, person_id, person_aliases[field], roles))
            return person_holds(field)
        if person_holds is not None:
            holds.date_sets = person_holds.date_sets
        holds.row = condition_row
        return holds

    def match_line(line, source):
        if related_aliases.get(line["field"]) == "tags":
            alias = line["field"]
            canonical_line = {**line, "field": "tags"}
            tagged = conditions.tag_scope(context[0], {"conditions": {"conditions": [canonical_line]}})["conditions"]
            line = tagged["conditions"][0]
            alias_source = source

            def tag_holds(field):
                values = alias_source(alias if field == "tags" else field)
                return {str(value).lower() for value in values} if field == "tags" else values
            tag_holds.row = condition_row
            source = tag_holds
        return conditions.evaluate(line, source, evaluation, row=condition_row)

    for row in records:
        if collection == "policy_lines":
            target = row.get("numbers", {}).get("target")
            row["target"] = target.get("value") if isinstance(target, dict) else target
        included = row["id"] in included_ids and (included_lines is None or _record_key(row, collection) in included_lines)
        _own_columns(row, collection)
        condition_row = {**row, **related.get(_record_key(row, collection), {})}
        domain_holds = _held(*context, condition_row, collection, roles, fields=fields) if context else None

        holds = combined(domain_holds)
        parents = _parent_ids(row, parent_field) & parent_ids if parent_field else set()
        if parent_field == "tag_id" and collection != "policy_lines":
            parents = {tid for tid in parent_ids if row["id"] in context[0].get("tags", {}).get(tid, {}).get("members", {}).get(collection, [])}
        if parent_field and collection == "policy_lines" and parent_field == "person_id":
            parents = set(row["person_ids"]) & parent_ids
        if parent_field and collection == "policy_lines" and parent_field == "service_id":
            parents = set(row["service_ids"]) & parent_ids
        if parent_field and collection == "policy_lines" and parent_field == "tag_id":
            parents = {tid for tid in parent_ids if tid in _condition_values(row.get("who"), "tags")
                       or tid in _condition_values(row.get("where"), "tags")}
        if parent_field and not parents and not included:
            continue
        if not included and semantic_projection and not semantic_search.projected_matches(
                row, collection, where.get('q'), search_options, semantic_found):
            continue
        if not included and deferred_search and where.get("q"):
            values = [row, *(search_people.get(pid, {}) for pid in _linked_people(row, collection))]
            if match_key(where["q"]) not in match_key(" ".join(str(value.get(key) or "") for value in values
                    for key in ("name", "display_name", "short_code", "staff_number", "note", "catalogue_id", "message"))):
                continue
        # Bound line versions satisfy the complete predicate; unbound owners retain the explicit overlay.
        bound_line = collection == "policy_lines" and (status_bound or row["policy_id"] in version_bindings)
        if resolved_tree and (not included or bound_line):
            candidates = (_linked_people(row, collection) if collection in {"sessions", "recurring_series", "swaps", "policy_lines"}
                          and (fields & conditions.PERSON_FIELDS or (tree_fields | deferred.keys()) & person_aliases.keys()) else ())

            witnesses = (combined(domain_holds, _held(*context, {"id": pid}, "people", roles), pid) for pid in candidates) if candidates else ()
            person_fields = set(person_aliases) | {field for field in tree_fields | deferred.keys()
                if (s.condition_field_definition(field, collection) or {}).get('operand_field', field) in conditions.PERSON_FIELDS}
            matches = conditions.evaluate_candidates(resolved_tree, holds, witnesses, evaluation,
                row=condition_row, person_fields=person_fields, match_line=match_line)
            if not matches:
                continue
        if (not included or bound_line) and any(not _matches_filter(
                related.get(_record_key(row, collection), {}).get(field, list(holds(field)) if field in s.CONDITION_FIELDS else row.get(field)),
                values, row=row, field=field, day=day, zone=context[0]["organisation"]["timezone"]) for field, values in deferred.items()
                if field not in {line["field"] for line in condition_filters} and not (
                    included and (field == 'id' or field == 'policy_id' and row['policy_id'] not in version_bindings))):
            continue
        if name_matches:
            matched_records.append(row)
            if (_record_key(row, collection) if name_field == "id" else row.get(name_field)) not in named_ids and not included:
                continue
        kept.append(json_value(row) if plain else row)
        by_parent.update(parents)
    return {"collection": collection, "records": kept,
            "matches": _matched_lines(name_matches, matched_records, name_field, collection), "unmatched": names_result["unmatched"],
            "ambiguous": names_result["ambiguous"], "by_parent": dict(by_parent)}


def resolve_targets(db, actor, selection, *, sample_only=False):
    result = _records(db, actor, selection, sample_only=sample_only)
    org = db.get(m.Organisation, actor.organisation_id)
    binding = {"department": actor.department_id, "actor": actor.identity_id,
               "selection": _value(selection), "inputs": [org.input_revision, org.availability_revision],
               "records": sorted((r.get("policy_id", ""), r["id"], r["revision"]) for r in result["records"])}
    result["token"] = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return result


def resolve(db, actor, selection):
    selection = _value(selection)
    if _simple_parent_count(selection):
        fast = _resolve_simple_parent_count(db, actor, selection)
        if fast is not None:
            return fast
    result = resolve_targets(db, actor, selection, sample_only=True)
    return {"count": len(result["records"]), "token": result["token"], "sample": result["records"][:s.BULK_LIMITS["sample_rows"]],
            **{key: result[key] for key in ("matches", "unmatched", "ambiguous", "by_parent")}}


def _simple_parent_count(selection):
    """Recognize the unfiltered child counts used by Show their? without broadening filters."""
    where = selection.get("where") or {}
    return bool(selection.get("parent")) and not any(key in selection for key in
        ("include_ids", "exclude_ids", "names")) and not any(key in where for key in
        ("filters", "conditions", "q", "search_options"))


def _resolve_simple_parent_count(db, actor, selection):
    """Count a direct parent relation with one bounded identity query and no row projection/context scan."""
    parent_spec = selection["parent"]
    parent_selection = {**(parent_spec.get("selection") or {}), "collection": parent_spec["collection"]}
    parent = _records(db, actor, parent_selection)
    parent_ids = {row["id"] for row in parent["records"]}
    collection = selection["collection"]
    parent_field = s.BULK_COLLECTIONS[collection].get("parent_of", {}).get(parent_spec["collection"])
    cls = MODELS.get(collection)
    org = db.get(m.Organisation, actor.organisation_id)
    day_value = (selection.get("where") or {}).get("as_of")
    day = date.fromisoformat(day_value) if day_value else today(db, actor.department_id)
    if collection == "policy_lines" and parent_field == "person_id":
        query = _base_query(db, actor, collection)
        columns = _read_columns(cls, _private_columns(actor, collection) | {"status"})
        columns.append(_status_column(cls, day, org.timezone).label("status"))
        source = list(db.execute(query.with_only_columns(*columns).order_by(cls.id).limit(MAX_CANDIDATES + 1)).mappings())
        require(len(source) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        records = _project_records(db, actor, collection, source)
        _line_people(db, actor, records, day, fields=(), target_people=parent_ids)
        records = [row for row in records if set(row.get("person_ids", ())) & parent_ids]
        require(len(records) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        refs = [(row["id"], row["revision"]) for row in records]
        by_parent = {person: sum(person in row.get("person_ids", ()) for row in records) for person in parent_ids}
        sample = records[:s.BULK_LIMITS["sample_rows"]]
    elif collection == "recurring_series" and parent_field == "person_id":
        query = _base_query(db, actor, collection)
        source = list(db.execute(query.with_only_columns(cls.id, cls.revision, cls.definition)
            .order_by(cls.id).limit(MAX_CANDIDATES + 1)).mappings())
        matched = [row for row in source if _parent_ids(row, parent_field) & parent_ids]
        require(len(source) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        refs = [(row["id"], row["revision"]) for row in matched]
        by_parent = {person: sum(person in _parent_ids(row, parent_field) for row in matched) for person in parent_ids}
        sample_ids = [row["id"] for row in matched[:s.BULK_LIMITS["sample_rows"]]]
        sample = []
        if sample_ids:
            columns = _read_columns(cls, _private_columns(actor, collection) | {"status"})
            columns.append(_status_column(cls, day, org.timezone).label("status"))
            source = db.execute(query.where(cls.id.in_(sample_ids)).with_only_columns(*columns)
                .order_by(cls.id)).mappings()
            sample = _project_records(db, actor, collection, source)
    else:
        if not parent_ids or not parent_field or cls is None or not hasattr(cls, parent_field):
            return None
        query = _base_query(db, actor, collection).where(getattr(cls, parent_field).in_(parent_ids))
        refs = list(db.execute(query.with_only_columns(cls.id, cls.revision).order_by(cls.id).limit(MAX_CANDIDATES + 1)))
        require(len(refs) <= MAX_CANDIDATES, "BULK_SELECTION_LIMIT", "More than 5,000 candidates; narrow the filter", 422)
        by_parent = dict(db.execute(query.with_only_columns(getattr(cls, parent_field), func.count()).group_by(
            getattr(cls, parent_field))).all())
        sample_ids = [row[0] for row in refs[:s.BULK_LIMITS["sample_rows"]]]
        sample = []
        if sample_ids:
            columns = _read_columns(cls, _private_columns(actor, collection) | {"status"})
            columns.append(_status_column(cls, day, org.timezone).label("status"))
            source = db.execute(query.where(cls.id.in_(sample_ids)).with_only_columns(*columns).order_by(cls.id)).mappings()
            sample = _project_records(db, actor, collection, source)
    binding = {"department": actor.department_id, "actor": actor.identity_id,
               "selection": _value(selection), "inputs": [org.input_revision, org.availability_revision],
               "records": [(row[0], row[1]) if not isinstance(row, dict) else (row["id"], row["revision"]) for row in refs]}
    token = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return {"count": len(refs), "token": token, "sample": sample,
            "matches": [], "unmatched": [], "ambiguous": [],
            "by_parent": {key: count for key, count in by_parent.items()}}


def _page_context(db, actor, day, people, services, fields, *, scoped=False):
    from .training import catalogue_scope
    zone = db.get(m.Organisation, actor.organisation_id).timezone
    data = {"organisation": {"timezone": zone}, "_bulk_day": day, "_bulk_department": actor.department_id,
            "locations": {}, "tags": {},
            "people": {}, "services": {}, "_bulk_service_locations": {}}
    if people:
        data["people"] = {p.id: record(p) for p in db.scalars(select(m.Person).where(
            m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(people)))}
    if services:
        data["services"] = {r.id: record(r) for r in db.scalars(select(m.Service).where(
            m.Service.department_id == actor.department_id, m.Service.id.in_(services)))}
    for field, name, cls, foreign, definitions, definition_cls in (
        ("category_ids", "memberships", m.Membership, "category_id", "categories", m.Category),
        ("programme_ids", "programme_memberships", m.ProgrammeMembership, "programme_id", "programmes", m.Programme),
    ):
        if field not in fields:
            continue
        data[name] = [record(r) for r in db.scalars(select(cls).where(
            cls.department_id == actor.department_id, cls.person_id.in_(people),
            catalogue_scope(db, actor, name, cls) if scoped else True))]
        ids = {r[foreign] for r in data[name]}
        data[definitions] = {r.id: record(r) for r in db.scalars(select(definition_cls).where(
            definition_cls.department_id == actor.department_id, definition_cls.id.in_(ids),
            catalogue_scope(db, actor, definitions, definition_cls) if scoped else True))}
    if "tags" in fields:
        for tag_id, name, kind, member in db.execute(select(m.Tag.id, m.Tag.name, m.TagMember.kind, m.TagMember.member_id).join(
                m.TagMember, m.TagMember.tag_id == m.Tag.id).where(m.Tag.department_id == actor.department_id,
                m.TagMember.department_id == actor.department_id, or_(
                    and_(m.TagMember.kind == "people", m.TagMember.member_id.in_(people)),
                    and_(m.TagMember.kind == "services", m.TagMember.member_id.in_(services))))):
            data["tags"].setdefault(tag_id, {"name": name, "members": {}})["members"].setdefault(kind, []).append(member)
        conditions.with_tags(data)
    if "location_ids" in fields:
        data["locations"] = {r.id: record(r) for r in db.scalars(select(m.Location).where(m.Location.department_id == actor.department_id))}
    if "location_ids" in fields and services:
        locations = data["_bulk_service_locations"]
        for sid, lid in db.execute(select(m.ShiftService.service_id, m.Shift.location_id).join(
                m.Shift, m.Shift.id == m.ShiftService.shift_id).join(m.Workspace).where(
                m.Workspace.department_id == actor.department_id, m.ShiftService.service_id.in_(services)).distinct()):
            locations.setdefault(sid, set()).add(lid)
        for location, member_services in db.execute(select(m.RecurringSeries.location_id, m.RecurringSeries.definition["service_ids"]).where(
                m.RecurringSeries.department_id == actor.department_id)):
            for sid in services.intersection(member_services or []):
                locations.setdefault(sid, set()).add(location)
    data["_person_rows"] = index_person_rows(data)
    return data, local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)


def _person_column_value(data, start, end, person_id, field, roles=None):
    if field in conditions.PERSON_FIELDS:
        return sorted(_held(data, start, end, {"id": person_id}, "people", roles or {})(field))
    return data["people"].get(person_id, {}).get(field)


def _related(db, actor, collection, records, day, context=None, columns=None):
    specs = s.BULK_COLLECTIONS[collection].get("related_columns", {})
    if columns is not None:
        specs = {key: spec for key, spec in specs.items() if key in columns}
    if not specs or not records:
        return {}
    if collection == "policy_lines":
        context = _line_people(db, actor, records, day, context=context, fields={spec["field"] for spec in specs.values()})
    people = {pid for row in records for pid in _parent_ids(row, "person_id")}
    if collection == "people":
        people.update(row["id"] for row in records)
    services = {sid for row in records for sid in _parent_ids(row, "service_id")}
    if collection == "services":
        services.update(row["id"] for row in records)
    data, start, end = context or _page_context(db, actor, day, people, services, {spec["field"] for spec in specs.values()})
    result = {_record_key(r, collection): {} for r in records}
    for row in records:
        for key, spec in specs.items():
            target, field = spec.get("collection"), spec.get("field")
            values = []
            if target == "people":
                person_ids = {row["id"]} if collection == "people" else _parent_ids(row, "person_id")
                for pid in sorted(person_ids):
                    values.append(_person_column_value(data, start, end, pid, field))
            elif target == "services":
                service_ids = {row["id"]} if collection == "services" else _parent_ids(row, "service_id")
                for sid in sorted(service_ids):
                    service = data["services"].get(sid, {})
                    value = service.get(field)
                    if field == "tags":
                        value = [t["name"] for t in data.get("tags", {}).values() if sid in t.get("members", {}).get("services", [])]
                    elif field == "location_ids":
                        value = sorted(_held(data, start, end, {"id": sid}, "services", {},
                                             fields={"location_ids"})("location_ids"))
                    values.append(value)
            if len(values) == 1:
                result[_record_key(row, collection)][key] = values[0]
            elif values:
                result[_record_key(row, collection)][key] = [item for v in values for item in (v if isinstance(v, list) else [v])]
            else:
                result[_record_key(row, collection)][key] = None
    return result


def _condition_values(tree, field, *, ops=None):
    return {value for child in (tree or {}).get("conditions", [])
            for value in (_condition_values(child, field, ops=ops) if "conditions" in child else
                          conditions.absolute_values(child) or [] if child["field"] == field and (
                              ops is None or child.get("op", "any") in ops) else []) if isinstance(value, str)}


def _matches_filter(value, expected, *, row, field, day, zone=None):
    if isinstance(expected, dict):
        if value is None:
            return False
        values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
        return any(actual is not None and all(
            {"gte": actual >= bound, "gt": actual > bound, "lte": actual <= bound, "lt": actual < bound}[op]
            for op, bound in expected.items()) for actual in values)
    if field == "status":
        return str(value).casefold() in {str(v).casefold() for v in expected}
    return bool(set(map(str, expected)) & set(map(str, value if isinstance(value, (list, tuple, set, frozenset)) else [value])))




def _sort_value(value):
    if isinstance(value, (int, float)):
        return (0, value)
    return (1, match_key(json.dumps(value, ensure_ascii=False)))


def _rows_key(db, actor, selection, sort, group, day):
    org = db.get(m.Organisation, actor.organisation_id)
    sources = []
    cacheable = True
    workspace_dependent = False
    current = selection
    while current:
        collection = current.get("collection")
        require(collection in s.BULK_COLLECTIONS and collection in MODELS, "BULK_COLLECTION", "Choose a supported collection")
        # Rechecking replaces findings without changing the workspace revision.
        if collection == "issues":
            return None
        cls = MODELS[collection]
        workspace_dependent |= collection == "sessions"
        # Reapply scope even on a hit; exact identities catch replacements with unchanged counts/revision sums.
        # Some canonical read states change without a record revision (passive inbox reads).
        columns = [cls.id, cls.revision, _status_column(cls, day, org.timezone)]
        if cls is m.Rotation:
            columns.append(cls.attachment_mentor_id)
        if cls in {m.RoleBinding, m.Notification}:
            columns.append(m.Identity.person_id)
            columns.append(m.Identity.display_name)
        source = list(db.execute(_base_query(db, actor, collection).with_only_columns(*columns)
            .order_by(cls.id).limit(MAX_CANDIDATES + 1)))
        cacheable &= len(source) <= MAX_CANDIDATES
        sources.append((collection, [tuple(row) for row in source]))
        parent = current.get("parent")
        current = {**parent["selection"], "collection": parent["collection"]} if parent else None
    trees = json.dumps(selection, sort_keys=True, default=str)
    if '"roles"' in trees:
        source = list(db.execute(select(m.RoleBinding.id, m.RoleBinding.revision).where(
            m.RoleBinding.department_id == actor.department_id).order_by(m.RoleBinding.id).limit(MAX_CANDIDATES + 1)))
        cacheable &= len(source) <= MAX_CANDIDATES
        sources.append([tuple(row) for row in source])
    # Assignments and derived clinic locations also follow edits that revise only the workspace.
    dependencies = json.dumps([selection, sort, group], sort_keys=True, default=str)
    if workspace_dependent or '"location_ids"' in dependencies or '"service.location_ids"' in dependencies:
        source = list(db.execute(select(m.Workspace.id, m.Workspace.revision).where(
            m.Workspace.department_id == actor.department_id).order_by(m.Workspace.id).limit(MAX_CANDIDATES + 1)))
        cacheable &= len(source) <= MAX_CANDIDATES
        sources.append(("workspaces", [tuple(row) for row in source]))
    # A wider source is still filterable; resolve it live rather than retain unbounded dependency metadata.
    if not cacheable:
        return None
    binding = {"selection": selection, "sort": sort, "group": group, "roles": sorted(actor.roles),
               "person": actor.person_id, "day": str(day), "today": str(today(db, actor.department_id)), "timezone": org.timezone,
               "inputs": [org.input_revision, org.availability_revision, org.revision], "sources": sources}
    return (actor.identity_id, actor.department_id, hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest())


def _cached_rows(db, key, entry=None):
    if key is None:
        return None
    engine = db.get_bind().engine
    now = monotonic()
    with _ROWS_CACHE_LOCK:
        cache = _ROWS_CACHE.setdefault(engine, OrderedDict())
        for old in [k for k, value in cache.items() if value["expires"] <= now]:
            del cache[old]
        if entry is not None:
            for old in [k for k in cache if k[:2] == key[:2] and k != key][:-(_ROWS_CACHE_PER_USER - 1)]:
                del cache[old]
            cache[key] = {**entry, "expires": now + _ROWS_CACHE_SECONDS}
            cache.move_to_end(key)
            while len(cache) > _ROWS_CACHE_ENTRIES:
                cache.popitem(last=False)
        elif key in cache:
            cache.move_to_end(key)
            return cache[key]


def _row_ref(row, collection):
    return (row.get("policy_id") if collection == "policy_lines" else
            row.get("workspace_id") if collection == "issues" else row["id"], row["id"], row["revision"])


def _load_page(db, actor, collection, refs, day):
    cls = MODELS[collection]
    ids = {ref[0] for ref in refs}
    if not ids:
        return []
    private = _private_columns(actor, collection)
    columns = _read_columns(cls, private | {"status"})
    query = _base_query(db, actor, collection).where(cls.id.in_(ids)).with_only_columns(*columns,
        _status_column(cls, day, db.get(m.Organisation, actor.organisation_id).timezone).label("status"))
    found = {_row_ref(row, collection): row for row in _project_records(db, actor, collection, db.execute(query).mappings())}
    return [found[ref] for ref in refs if ref in found]


def _current_refs(db, actor, collection, refs):
    cls = MODELS[collection]
    expected = {(source, revision) for source, _, revision in refs}
    current = set(db.execute(_base_query(db, actor, collection).where(cls.id.in_({r[0] for r in refs}))
        .with_only_columns(cls.id, cls.revision))) if refs else set()
    return current == expected


def _own_columns(row, collection):
    if collection in {"policies", "scheduling_facts"}:
        definition = row.get("data") or row.get("definition") or {}
        rule = definition.get("rule", definition)
        for name in s.BULK_COLLECTIONS[collection].get("columns", {}):
            if name not in row:
                row[name] = rule.get(name, definition.get(name))
        if collection == "policies":
            row["catalogue_id"] = rule.get("id")
    return row


def _column_names(collection, actor):
    registry = s.BULK_COLLECTIONS[collection]
    allowed = (set(MODELS[collection].__table__.columns.keys()) | set(registry.get("columns", {}))
               | set(registry.get("fields", {})) | set(registry.get("related_columns", {})) | {"status"}) - _private_columns(actor, collection)
    if collection == "policy_lines":
        allowed |= set(registry.get("fields", {})) | {"policy_id", "line_id", "catalogue_id", "who", "where", "numbers"}
    if collection == "issues":
        allowed |= {"workspace_id", "issue_key", "code", "message", "person_id", "shift_id", "rule_id", "severity"}
    if collection == "sessions":
        allowed |= {"service_ids", "person_ids"}
    if MODELS[collection] in {m.RoleBinding, m.Notification}:
        allowed.add("person_id")
    return allowed


def rows(db, actor, payload):
    payload = _value(payload)
    selection = _value(payload["selection"])
    collection = selection.get("collection")
    require(collection in s.BULK_COLLECTIONS and collection in MODELS, "BULK_COLLECTION", "Choose a supported collection")
    registry = s.BULK_COLLECTIONS[collection]
    columns = payload.get("columns") or []
    allowed = _column_names(collection, actor)
    require(set(columns) <= allowed, "BULK_COLUMNS", "Choose columns from this collection")
    where = selection.get("where") or {}
    day = date.fromisoformat(where["as_of"]) if where.get("as_of") else today(db, actor.department_id)
    sort = payload.get("sort") or []
    if isinstance(sort, str):
        sort = [{"field": sort}]
    if isinstance(sort, dict):
        sort = [sort]
    group = payload.get("group")
    if isinstance(group, dict):
        group = group.get("field")
    require(not group or group in allowed, "BULK_GROUP", "Choose a grouping column from this collection")
    ordering = ([{"field": group}] if group else []) + sort
    for entry in ordering:
        field = entry.get("field", entry.get("key"))
        require(field in allowed, "BULK_SORT", "Choose a sorting column from this collection")
    offset, limit = payload.get("offset", 0), payload.get("limit", 100)
    require(isinstance(offset, int) and 0 <= offset <= MAX_CANDIDATES and isinstance(limit, int) and 1 <= limit <= MAX_CANDIDATES,
            "BULK_PAGE", "Choose a page of up to 5,000 rows")
    key = _rows_key(db, actor, selection, sort, group, day)
    cached = _cached_rows(db, key)
    if cached and not _current_refs(db, actor, collection, cached["refs"]):
        cached = None
    if cached is None:
        result = resolve_targets(db, actor, selection)
        records = [_own_columns(row, collection) for row in result["records"]]
        sort_columns = {entry.get("field", entry.get("key")) for entry in ordering}
        if "status" in sort_columns:
            records = [_own_columns(row, collection) for row in _load_page(db, actor, collection, [_row_ref(r, collection) for r in records], day)]
        related = _related(db, actor, collection, records, day, columns=sort_columns)
        records = [{**row, **related.get(_record_key(row, collection), {})} for row in records]
        for entry in reversed(ordering):
            field = entry.get("field", entry.get("key"))
            records.sort(key=lambda r: (r.get(field) is None, _sort_value(r.get(field))),
                         reverse=entry.get("direction", "asc") == "desc")
        groups = {}
        if group:
            for index, row in enumerate(records):
                value = json.dumps(row.get(group), sort_keys=True)
                groups.setdefault(value, [index, 0])[1] += 1
        cached = {"refs": tuple(_row_ref(row, collection) for row in records), "token": result["token"],
                  "groups": tuple(tuple(value) for value in groups.values())}
        _cached_rows(db, key, cached)
    refs = cached["refs"]
    page_refs = refs[offset:offset + limit]
    group_refs = [refs[index] for index, _ in cached["groups"]]
    requested = list(dict.fromkeys([*page_refs, *group_refs]))
    loaded = _load_page(db, actor, collection, requested, day)
    require(len(loaded) == len(requested), "BULK_ROWS_STALE", "Records changed while this page loaded; refresh it", 409)
    related = _related(db, actor, collection, loaded, day, columns=set(columns) | ({group} if group else set()) if columns else None)
    found = {}
    for row in loaded:
        _own_columns(row, collection)
        found[_row_ref(row, collection)] = {**row, **related.get(_record_key(row, collection), {})}
    page = [found[ref] for ref in page_refs if ref in found]
    identity_columns = ("id", "revision", "status", "policy_id", "line_id") if collection == "policy_lines" else ("id", "revision", "status")
    return {"rows": [{name: row.get(name) for name in dict.fromkeys((*identity_columns, *columns))} if columns else row for row in page],
            "count": len(refs), "total": len(refs), "token": cached["token"], "offset": offset, "limit": limit,
            "has_more": offset + len(page) < len(refs),
            "groups": [{"value": found.get(refs[index], {}).get(group), "count": count} for index, count in cached["groups"]]}


def collections(db, actor):
    """Counts use the same per-collection permission and row scope as resolve; zero is meaningful."""
    counts, queries = {}, []
    for collection in s.BULK_COLLECTIONS:
        if collection not in MODELS:
            continue
        try:
            query = _base_query(db, actor, collection)
        except Problem as error:
            if error.status != 403:
                raise
            continue
        cls = MODELS[collection]
        if collection == "policy_lines":
            source = db.execute(query.with_only_columns(cls.id, cls.revision, cls.name, cls.status, cls.definition)).mappings()
            counts[collection] = {"count": len(_project_records(db, actor, collection, source))}
        elif collection == "issues":
            from packages.domain.rota_summary import normalized_check
            counts[collection] = {"count": sum(len((normalized_check(check) or {}).get("findings", []))
                for check in db.scalars(query.with_only_columns(cls.validation)))}
        else:
            queries.append(select(literal(collection), func.count()).select_from(query.with_only_columns(cls.id).subquery()))
    if queries:
        counts.update({collection: {"count": count} for collection, count in db.execute(union_all(*queries))})
    return {"collections": counts}
