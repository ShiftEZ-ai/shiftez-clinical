"""One windowed, permission-scoped timeline projection for boards and person records."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from apps.api import models as m
from apps.api.auth import require_role
from apps.api.errors import require
from apps.api.serialization import record
from apps.api.snapshots import currency_match, domain_snapshot
from packages.contracts import schemas as s
from packages.domain import conditions
from packages.domain.recurrence import inactive
from packages.domain.time import aware, local_midnight
from packages.domain.validation import _qualification_key, effective_type_definition, evaluate, index_person_rows, qualified
from . import bulk_selection, leave_types, training
from .leave_disclosure import leave_wording, staff_overlap_labels


LAYERS = (
    ("rotation", "Rotations", "rotation", "rotations"),
    ("attachment", "Attachments", "attachment", "mentorships"),
    ("leave", "Leave", "leave", "availability"),
    ("time_away", "Time away", "time-away", "availability"),
    ("academic", "Academic time", "academic", "programme_calendar_entries"),
    ("protected_time", "Protected time", "protected", "scheduling_facts"),
    ("overtime_offer", "Overtime offers", "overtime", "scheduling_facts"),
    ("eligibility", "Eligibility", "eligibility", "eligibilities"),
    ("grant", "Skills", "grant", "grants"),
    ("programme", "Programme memberships", "programme", "programme_memberships"),
    ("rank", "Rank changes", "rank", "memberships"),
    ("pending", "Pending changes", "pending", "requests"),
    ("duty_density", "Published duties", "density", "publications"),
    ("conflict", "Conflicts", "conflict", "validation"),
)

LAYER_KEYS = tuple(row[0] for row in LAYERS)
# Eligibility is a high-cardinality rail and is opt-in.  The remaining descriptors stay available to the
# existing timeline layout; callers can narrow them further with `layers=`.
DEFAULT_LAYERS = frozenset(kind for kind in LAYER_KEYS if kind != "eligibility")
LAYER_ALIASES = {"rotations": "rotation", "findings": "conflict", "memberships": "programme"}


def requested_layers(value=None):
    """Normalize the comma-separated API value (or a direct iterable used by tests)."""
    if value is None:
        return DEFAULT_LAYERS
    if value == "none":
        return frozenset()
    values = value.split(",") if isinstance(value, str) else value
    result = {LAYER_ALIASES.get(str(item).strip(), str(item).strip()) for item in values if str(item).strip()}
    require(result <= set(LAYER_KEYS), "TIMELINE_LAYERS", "Choose supported timeline layers", 422)
    return frozenset(result)


def _dates(row, zone):
    if row.get("start"):
        return (aware(row["start"]).astimezone(zone).date(),
                (aware(row["end"]) - timedelta(microseconds=1)).astimezone(zone).date() + timedelta(days=1))
    return (date.fromisoformat(row["starts_on"]) if row.get("starts_on") else date.min,
            date.fromisoformat(row["ends_on"]) if row.get("ends_on") else date.max)


def _overlaps(row, start, end, zone):
    first, last = _dates(row, zone)
    return first < end and start < last


def _check_data(db, actor, start, end):
    # A transient read context reuses the complete quota/rest preload and the ordinary validator; it is never saved.
    workspace = m.Workspace(id=m.uid(), revision=1, created_at=m.now(), department_id=actor.department_id,
        name="Timeline check", starts_on=start, ends_on=end, status="draft", content_revision=1,
        created_by=actor.identity_id, last_edited_by=actor.identity_id, rule_adjustments=[], target_overrides=[])
    return domain_snapshot(db, workspace, own_shifts=[])


def prepare_conflict_check(data, subjects):
    """Request-local caches keep full validation while omitting the unused department workload score report."""
    return {**data, "person_rule_subjects": set(subjects), "quota_subjects": set(subjects),
            "_categories_cache": {}, "_capabilities_cache": {}, "_policy_applies_cache": {},
            "_policy_applies_people": True, "_workload_matches_cache": {}, "_distribution_cache": {}}


def _light_data(db, actor, start, end, person_ids, group_by, layers):
    """Read only the bounded records needed by the requested timeline layers.

    The full domain snapshot is reserved for writes and explicit checks.  Timeline reads use one batched query per
    source table and keep the same record projection as the old snapshot path.
    """
    dep = actor.department_id
    organisation = db.get(m.Organisation, actor.organisation_id)
    data = {"organisation": record(organisation), "programme_memberships": [], "rotations": [], "memberships": [],
            "mentorships": [], "eligibilities": [], "grants": [], "availability": [], "scheduling_facts": [],
            "shifts": [], "cohorts": {}, "rotation_types": {}, "services": {}, "programmes": {},
            "categories": {}, "capabilities": {}, "people": {}, "types": {}}

    def rows(cls, *, scope=None):
        query = select(cls).where(cls.department_id == dep,
            or_(cls.starts_on.is_(None), cls.starts_on < end),
            or_(cls.ends_on.is_(None), cls.ends_on > start))
        if person_ids is not None:
            query = query.where(scope if scope is not None else cls.person_id.in_(person_ids))
        if cls is m.Membership and person_ids is not None:
            # Availability needs the nearest historical boundaries to classify a visible rank gap without
            # reading one membership query per person. Rows overlapping the window are retained as before;
            # these two correlated extrema add at most one predecessor and successor per selected person.
            prior, future = aliased(m.Membership), aliased(m.Membership)
            previous_end = select(func.max(prior.ends_on)).where(
                prior.department_id == dep, prior.person_id == cls.person_id,
                prior.ends_on.is_not(None), prior.ends_on <= start).correlate(cls).scalar_subquery()
            next_start = select(func.min(future.starts_on)).where(
                future.department_id == dep, future.person_id == cls.person_id,
                future.starts_on >= end).correlate(cls).scalar_subquery()
            query = select(cls).where(cls.department_id == dep, cls.person_id.in_(person_ids),
                or_(
                    and_(or_(cls.starts_on.is_(None), cls.starts_on < end),
                         or_(cls.ends_on.is_(None), cls.ends_on > start)),
                    cls.ends_on == previous_end,
                    cls.starts_on == next_start,
                ))
        return [record(row) for row in db.scalars(query)]

    # Memberships are needed for lead scoping and group labels even when the membership layer is hidden.
    data["programme_memberships"] = rows(m.ProgrammeMembership)
    if group_by == "rank" or "rank" in layers:
        data["memberships"] = rows(m.Membership)
    if group_by == "programme" or {"rotation", "programme"} & layers:
        data["programmes"] = {row.id: record(row) for row in db.scalars(select(m.Programme).where(m.Programme.department_id == dep))}
    if group_by == "cohort" or {"rotation", "attachment", "conflict"} & layers:
        data["rotations"] = rows(m.Rotation)
        data["cohorts"] = {row.id: record(row) for row in db.scalars(select(m.Cohort).where(m.Cohort.department_id == dep))}
    if "rotation" in layers or "attachment" in layers:
        data["rotation_types"] = {row.id: record(row) for row in db.scalars(select(m.RotationType).where(m.RotationType.department_id == dep))}
    if "attachment" in layers:
        data["mentorships"] = rows(m.Mentorship, scope=or_(
            m.Mentorship.person_id.in_(person_ids), m.Mentorship.mentor_id.in_(person_ids)))
    if "eligibility" in layers:
        data["eligibilities"] = rows(m.Eligibility)
    if "grant" in layers:
        data["grants"] = rows(m.CapabilityGrant)
    if "rotation" in layers or "eligibility" in layers or "grant" in layers:
        data["services"] = {row.id: record(row) for row in db.scalars(select(m.Service).where(m.Service.department_id == dep))}
    if "grant" in layers:
        data["capabilities"] = {row.id: record(row) for row in db.scalars(select(m.Capability).where(m.Capability.department_id == dep))}
    if "rank" in layers or group_by == "rank":
        data["categories"] = {row.id: record(row) for row in db.scalars(select(m.Category).where(m.Category.department_id == dep))}
    if {"leave", "time_away", "protected_time"} & layers:
        lo, hi = local_midnight(start, organisation.timezone), local_midnight(end, organisation.timezone)
        availability = list(db.scalars(select(m.Availability).where(
            m.Availability.department_id == dep, m.Availability.person_id.in_(person_ids),
            m.Availability.retracted.is_(False), m.Availability.start < hi, m.Availability.end > lo)))
        report_ids = {row.request_id for row in availability if row.request_id}
        reports = {row.id: row for row in db.scalars(select(m.RequestRecord).where(
            m.RequestRecord.department_id == dep, m.RequestRecord.id.in_(report_ids),
            m.RequestRecord.kind == 'absence_report'))} if report_ids else {}
        data["availability"] = []
        for row in availability:
            value = record(row, exclude=("private_reason",)) | {"private_reason": ""}
            if report := reports.get(row.request_id):
                value.update(absence_report=True, absence_report_category=(report.details or {}).get('reporter_category'),
                             absence_report_note=(report.details or {}).get('note'), absence_report_reason=report.private_reason)
            data["availability"].append(value)
        if "time_away" in layers:
            from .availability_patterns import expand
            data["availability"] += expand(db, [dep], list(person_ids), lo, hi, organisation.timezone)
    if {"academic", "protected_time", "pending"} & layers:
        data["programme_calendar_entries"] = [record(row) for row in db.scalars(select(m.ProgrammeCalendarEntry).where(
            m.ProgrammeCalendarEntry.department_id == dep, m.ProgrammeCalendarEntry.starts_on < end,
            m.ProgrammeCalendarEntry.ends_on > start))]
    else:
        data["programme_calendar_entries"] = []
    if {"protected_time", "overtime_offer", "pending"} & layers:
        data["scheduling_facts"] = [record(row) for row in db.scalars(select(m.SchedulingFact).where(
            m.SchedulingFact.department_id == dep,
            m.SchedulingFact.status.in_(("active", "proposed") if "pending" in layers else ("active",)),
            or_(m.SchedulingFact.person_id.in_(person_ids), m.SchedulingFact.person_id.is_(None)),
            or_(m.SchedulingFact.starts_on.is_(None), m.SchedulingFact.starts_on < end),
            or_(m.SchedulingFact.ends_on.is_(None), m.SchedulingFact.ends_on > start)))]
        entries = {row["id"] for row in data["programme_calendar_entries"]}
        data["scheduling_facts"] = [row for row in data["scheduling_facts"] if
            not row.get("calendar_entry_id") or row["calendar_entry_id"] in entries]
        if "protected_time" in layers:
            from .events import protected_facts
            lo, hi = local_midnight(start, organisation.timezone), local_midnight(end, organisation.timezone)
            data["scheduling_facts"] += protected_facts(db, [dep], list(person_ids), lo, hi, organisation.timezone)
    if "duty_density" in layers:
        from .schedule import current_publications
        from .schedule_view import published_shifts
        publications = current_publications(db, [dep], start, end, light=True)
        data["shifts"] = published_shifts(db, publications, organisation.timezone, start, end,
            shift_ids=select(m.Assignment.shift_id).where(m.Assignment.person_id.in_(person_ids)))
        data["types"] = {row.id: record(row) for row in db.scalars(select(m.AssignmentType).where(m.AssignmentType.department_id == dep))}
    # Names for attachments and role labels are cheap and avoid a per-item lookup.
    related = {pid for row in data["mentorships"] for pid in (row["person_id"], row["mentor_id"])}
    related.update(row.get("mentor_id") for row in data["rotations"] if row.get("mentor_id"))
    data["people"] = {row.id: record(row) for row in db.scalars(select(m.Person).where(
        m.Person.organisation_id == actor.organisation_id, m.Person.id.in_(related)))} if related else {}
    from packages.domain.validation import index_person_rows
    data['_person_rows'] = index_person_rows(data)
    return data


def _stored_findings(db, actor, start, end, ids, zone):
    """Return only current, stored publication findings; never turn a read into a validator run."""
    if not ids:
        return []
    from .schedule import current_publications, until
    from apps.api.settings import quota_policy_limit
    from packages.domain.input_manifest import digest, toolchain
    organisation = db.get(m.Organisation, actor.organisation_id)
    current_toolchain = digest(toolchain(quota_policy_limit()))
    publications = current_publications(db, [actor.department_id], start, end, light=True)
    revisions = dict(db.execute(select(m.Workspace.id, m.Workspace.content_revision).where(
        m.Workspace.id.in_([p.workspace_id for p in publications]))).all()) if publications else {}
    checks = dict(db.execute(select(m.Publication.id, m.Publication.validation).where(
        m.Publication.id.in_([p.id for p in publications]))).all()) if publications else {}
    findings = []
    for publication in publications:
        check = checks[publication.id] or {}
        manifest = check.get("input_manifest") or {}
        dirty = manifest.get("dirty_hint") or {}
        current = {"input_revision": organisation.input_revision,
                   "availability_revision": organisation.availability_revision,
                   "content_revision": revisions.get(publication.workspace_id)}
        # Compare the publication counters; the manifest also carries draft-specific evidence.
        recorded = {key: value for key, value in dirty.items() if key in current}
        if (not currency_match(recorded, current)
                or manifest.get("components", {}).get("toolchain") != current_toolchain):
            continue
        # An amendment retains earlier findings as issued evidence. Its current
        # stamp covers the findings that the amendment actually checked.
        for finding in check.get("change_findings", check.get("findings", [])):
            if finding.get("person_id") not in ids:
                continue
            if finding.get("start") and finding.get("end"):
                first, last = aware(finding["start"]), aware(finding["end"])
                if (last <= local_midnight(max(start, publication.starts_on), str(zone))
                        or first >= local_midnight(min(end, until(publication)), str(zone))):
                    continue
            findings.append(finding)
    return findings


def _people(db, actor, data, start, end, person_ids, q, collection_filter, led, trainees_only, area_filter=None):
    trainees = training.trainees(actor.department_id, start, end)
    query = select(m.Person).where(m.Person.organisation_id == actor.organisation_id, or_(
        m.Person.id.in_(bulk_selection._people_query(actor).with_only_columns(m.Person.id)),
        m.Person.id.in_(trainees)))
    team = actor.permits(*s.TEAM_READER_ROLES)
    if not team:
        require(bool(actor.person_id), "PERSON_REQUIRED", "Your account is not linked to a person", 403)
        require(not person_ids or set(person_ids) <= {actor.person_id}, "FORBIDDEN", "Staff can view only their own timeline", 403)
        query = query.where(m.Person.id == actor.person_id)
    elif person_ids:
        query = query.where(m.Person.id.in_(person_ids))
    if trainees_only:
        query = query.where(m.Person.id.in_(trainees))
    if led is not None:
        allowed = {r["person_id"] for r in data["programme_memberships"] if r["department_id"] == actor.department_id
                   and r["programme_id"] in led and _overlaps(r, start, end, ZoneInfo(data["organisation"]["timezone"]))}
        query = query.where(m.Person.id.in_(allowed))
    if collection_filter and collection_filter.search_options is not None:
        from .search import predicate
        query = query.where(predicate(db, actor, 'people', q, collection_filter.search_options, as_of=collection_filter.as_of or start))
    elif q and q.strip():
        query = query.where(or_(*(bulk_selection._literal_search(column, q.strip()) for column in
                                 (m.Person.display_name, m.Person.short_code, m.Person.staff_number))))
    rows = [record(row) for row in db.scalars(query.order_by(m.Person.id).limit(bulk_selection.MAX_CANDIDATES + 1))]
    require(len(rows) <= bulk_selection.MAX_CANDIDATES, "TIMELINE_LIMIT", "Narrow the people filter", 422)
    if area_filter and collection_filter and collection_filter.conditions and 'works_in_area' in collection_filter.conditions.fields():
        values = area_filter({row['id'] for row in rows})
        for row in rows:
            row['works_in_area'] = list({identifier for work in values.get(row['id'], {}).values() for identifier in work['areas']})
    if not collection_filter or not collection_filter.conditions:
        return rows
    from .condition_context import validate_tree
    from .collection_filters import validate
    tree = collection_filter.conditions
    fields = tree.fields()
    validate(tree, bulk_selection._column_names("people", actor) | set(s.CONDITION_FIELDS) | {"works_in_area"}, table_key="people")
    day = collection_filter.as_of or start
    fields |= {(s.condition_field_definition(field, "people") or {}).get("operand_field", field) for field in fields}
    # Filter facts are independent of the displayed layers and bounded to the candidate people on the filter day.
    data = _light_data(db, actor, day, day + timedelta(days=1), {row["id"] for row in rows}, "none",
                       {"rank", "programme", "rotation", "grant", "eligibility"})
    data["people"] = {row["id"]: row for row in rows}
    from apps.api.snapshots import tag_data
    data["tags"] = tag_data(db, [actor.department_id])
    data["locations"] = {row.id: record(row) for row in db.scalars(select(m.Location).where(
        m.Location.department_id == actor.department_id))} if "location_ids" in fields else {}
    data["date_sets"] = {row.id: record(row) for row in db.scalars(select(m.DateSet).where(
        m.DateSet.department_id == actor.department_id))} if "date_set_ids" in fields else {}
    data["_bulk_day"], data["_bulk_department"] = day, actor.department_id
    data["_bulk_service_locations"] = bulk_selection.service_locations(db, actor.department_id) if "location_ids" in fields else {}
    conditions.with_tags(data)
    lo, hi = local_midnight(day, data["organisation"]["timezone"]), local_midnight(day + timedelta(days=1), data["organisation"]["timezone"])
    evaluation = validate_tree(db, actor, tree, as_of=day, context="selection", collection="people")
    related = bulk_selection._related(db, actor, "people", rows, day, (data, lo, hi), columns=fields)
    roles = bulk_selection._person_roles(db, actor, day) if "roles" in fields else {}
    resolved = conditions.tag_scope(data, {"conditions": tree.model_dump(mode="json")})["conditions"]
    return [row for row in rows if conditions.evaluate(resolved, bulk_selection._held(
        data, lo, hi, {**row, **related.get(row["id"], {})}, "people", roles, fields=fields), evaluation)]


def _evaluate_rota_eligibility(data, person_ids):
    """Evaluate each requested person against the rota's own active sessions once per qualification key."""
    focus_ids = set(data.get("focus_ids", ()))
    sessions = [shift for shift in data.get("shifts", ())
                if shift["id"] in focus_ids and not inactive(shift)]
    result = {person_id: None if not sessions else False for person_id in person_ids}
    if not sessions or not person_ids:
        return result, len(sessions)
    context = {**data,
               "_person_rows": data.get("_person_rows") or index_person_rows(data),
               "_qualification_cache": data.get("_qualification_cache") or {},
               "_categories_cache": data.get("_categories_cache") or {},
               "_capabilities_cache": data.get("_capabilities_cache") or {}}
    prepared = [(shift, aware(shift["start"]), aware(shift["end"]), effective_type_definition(context, shift))
                for shift in sessions]
    for person_id in person_ids:
        seen = set()
        for shift, start, end, definition in prepared:
            signature = _qualification_key(context, person_id, shift, definition, start, end)
            if signature in seen:
                continue
            seen.add(signature)
            if qualified(context, person_id, shift, definition, start, end):
                result[person_id] = True
                break
    return result, len(sessions)


def _rota_eligibility(db, workspace, person_ids):
    data = domain_snapshot(db, workspace, include_fairness=False, include_objectives=False)
    eligible, sessions = _evaluate_rota_eligibility(data, person_ids)
    return eligible, {"workspace_id": workspace.id, "sessions": sessions, "basis": "qualified"}


def list_data(db, actor, start, end, person_ids=None, q=None, group_by="none", sort_by="name",
              sort_direction="asc", offset=0, limit=150, collection_filter=None, trainees_only=False, layers=None,
              bucket=None, source=None, hide=None, hide_leave_types=None, workspace_id=None,
              eligible_workspace_id=None):
    require_role(actor, "staff", *s.TEAM_READER_ROLES)
    eligible_workspace = None
    if eligible_workspace_id is not None:
        require_role(actor, *s.TEAM_READER_ROLES)
        from .common import scoped
        eligible_workspace = scoped(db, m.Workspace, eligible_workspace_id, actor)
    require(0 < (end - start).days <= training.MAX_BOARD_DAYS, "TIMELINE_RANGE", "Choose up to two years", 422)
    require(group_by in {"none", "programme", "rank", "cohort"} and sort_by in {"name", "code"}
            and sort_direction in {"asc", "desc"}, "TIMELINE_ORDER", "Choose a supported grouping and order", 422)
    require(isinstance(offset, int) and offset >= 0 and isinstance(limit, int) and 1 <= limit <= 200,
            "TIMELINE_PAGE", "Choose a page of up to 200 people", 422)
    layers = requested_layers(layers)
    from . import whereabouts
    selected_sources, hidden, hidden_leave = whereabouts.options(bucket, source, hide, hide_leave_types) if bucket is not None else (None, None, None)
    require(bucket is not None or not (source or hide or hide_leave_types or workspace_id),
            'TIMELINE_BUCKET', 'Choose a bucket for the one-line projection', 422)
    preloaded = None

    def area_filter(ids):
        nonlocal preloaded
        require(bucket is not None, 'TIMELINE_BUCKET', 'Choose a bucket to filter by work area', 422)
        preloaded = whereabouts.read_work(db, actor, ids, start, end, str(zone), selected_sources, workspace_id)
        return preloaded[0]
    # A conflict read is a projection of the last stored publication check.  It does not need the full domain
    # snapshot; the explicit write/preview paths remain the only callers of the evaluator.
    data = _light_data(db, actor, start, end, set(person_ids) if person_ids else None, group_by, frozenset())
    zone = ZoneInfo(data["organisation"]["timezone"])
    led_query = training.led_programmes(db, actor) if bucket is None else None
    led = set(db.scalars(led_query)) if led_query is not None else None
    candidates = _people(db, actor, data, start, end, person_ids, q, collection_filter, led, trainees_only, area_filter=area_filter)
    department = db.get(m.Department, actor.department_id)
    dep = actor.department_id
    memberships = [r for r in data["programme_memberships"] if r["department_id"] == dep
                   and (led is None or r["programme_id"] in led) and _overlaps(r, start, end, zone)]
    rotations = [r for r in data["rotations"] if r["department_id"] == dep and _overlaps(r, start, end, zone)
                 and (led is None or data["cohorts"].get(r["cohort_id"], {}).get("programme_id") in led)]
    memberships_by_person, rotations_by_person, ranks_by_person = defaultdict(list), defaultdict(list), defaultdict(list)
    for rows, grouped in ((memberships, memberships_by_person), (rotations, rotations_by_person), (data["memberships"], ranks_by_person)):
        for row in rows:
            if row["department_id"] == dep and _overlaps(row, start, end, zone):
                grouped[row["person_id"]].append(row)

    def group(person):
        if group_by == "none":
            return None
        kind, key, grouped = (("programmes", "programme_id", memberships_by_person) if group_by == "programme" else
                           ("categories", "category_id", ranks_by_person) if group_by == "rank" else
                           ("cohorts", "cohort_id", rotations_by_person))
        values = sorted({data[kind].get(row[key], {}).get("name", row[key]) for row in grouped[person["id"]]})
        return ", ".join(values) or None

    for person in candidates:
        person["group"] = group(person)
    candidates.sort(key=lambda p: ((p["display_name"] if sort_by == "name" else p["short_code"]).casefold(), p["id"]),
                    reverse=sort_direction == "desc")
    if group_by == 'rank':
        rank_order = {row['name']: i for i, row in enumerate(sorted(data['categories'].values(),
            key=lambda row: (row.get('sort_order', 1000), row['name'], row['id'])))}
        candidates.sort(key=lambda p: (p['group'] is None, rank_order.get(p['group'], len(rank_order))))
    elif group_by != "none":
        candidates.sort(key=lambda p: (p["group"] is None, (p["group"] or "").casefold()))
    total = len(candidates)
    people = {p["id"]: {key: p[key] for key in ("id", "display_name", "short_code", "group")}
              | {"items": [], "duty_density": [], "conflicts": []} for p in candidates[offset:offset + limit]}
    ids = set(people)
    rota_eligibility = None
    if eligible_workspace is not None:
        eligibility, rota_eligibility = _rota_eligibility(db, eligible_workspace, ids)
        for person_id, value in eligibility.items():
            people[person_id]["rota_eligible"] = value
    # Related records are acquired only after filtering and paging, never for the whole organisation/history.
    projection_layers = set()
    if bucket:
        if 'rotation' not in hidden:
            projection_layers.add('rotation')
        if 'leave' not in hidden:
            projection_layers.add('leave')
        if 'academic' not in hidden:
            projection_layers.update(('academic', 'protected_time', 'rank'))
    data = _light_data(db, actor, start, end, ids, group_by, layers | projection_layers)
    if bucket:
        projected = whereabouts.projection(db, actor, ids, data, start, end, bucket, selected_sources,
                                          hidden, hidden_leave, workspace_id, preloaded)
        for pid in ids:
            people[pid].update(projected[pid])
    memberships = [r for r in data["programme_memberships"] if led is None or r["programme_id"] in led]
    rotations = [r for r in data["rotations"] if led is None or data["cohorts"].get(r["cohort_id"], {}).get("programme_id") in led]
    memberships_by_person = defaultdict(list)
    for row in memberships:
        memberships_by_person[row["person_id"]].append(row)
    notes, note_person_ids = training.note_summary(db, actor, ids)
    for pid, person in people.items():
        person["notes"] = notes.get(pid, 0)
    names = {kind: {} for kind in ("programmes", "cohorts", "rotation_types", "services", "categories", "capabilities", "people")}

    def name(kind, identifier, fallback):
        row = data.get(kind, {}).get(identifier)
        label = row.get("name", row.get("display_name", fallback)) if row else fallback
        if identifier:
            names[kind][identifier] = label
        return label

    lead_spans = {pid: sorted(_dates(row, zone) for row in rows) for pid, rows in memberships_by_person.items()}

    def in_scope(row):
        if led is None:
            return True
        first, last = _dates(row, zone)
        covered = first
        for lo, hi in lead_spans.get(row["person_id"], ()):
            if lo <= covered:
                covered = max(covered, hi)
        return covered >= last

    def append(row, kind, source, label, status="active", details=None, *, person_id=None):
        pid = person_id or row.get("person_id")
        if pid not in people or not _overlaps(row, start, end, zone):
            return
        first, last = _dates(row, zone)
        people[pid]["items"].append({"id": row["id"], "kind": kind, "source": source, "label": label,
            "starts_on": first.isoformat() if first != date.min else start.isoformat(),
            "ends_on": last.isoformat() if last != date.max else None,
            "start": row.get("start"), "end": row.get("end"), "status": status,
            "revision": row.get("revision"), "details": details or {}})

    for row in rotations if "rotation" in layers else ():
        if row["person_id"] not in ids:
            continue
        cohort = data["cohorts"].get(row["cohort_id"], {})
        label = name("rotation_types", row.get("rotation_type_id"), "") or name("services", row.get("service_id"), "Rotation")
        name("cohorts", row["cohort_id"], "Cohort")
        name("programmes", cohort.get("programme_id"), "Programme")
        if row.get("service_id"):
            name("services", row["service_id"], "Clinic")
        if row.get("mentor_id"):
            name("people", row["mentor_id"], "Supervisor")
        append(row, "rotation", "rotations", label, details={key: row.get(key) for key in
            ("rotation_type_id", "service_id", "cohort_id", "department_available", "mentor_id")})
    linked_attachments = set(db.scalars(select(m.RotationAttachment.mentorship_id).where(
        m.RotationAttachment.rotation_id.in_({row["id"] for row in rotations if row["person_id"] in ids})))) if led is not None and "attachment" in layers else set()
    for row in data["mentorships"] if "attachment" in layers else ():
        if row["department_id"] != dep or led is not None and row.get("programme_id") not in led and row["id"] not in linked_attachments:
            continue
        for pid, other in ((row["person_id"], row["mentor_id"]), (row["mentor_id"], row["person_id"])):
            if pid in ids:
                append(row, "attachment", "mentorships", name("people", other, "Attached person"), person_id=pid,
                       details={key: row.get(key) for key in ("person_id", "mentor_id", "programme_id", "assignment_type_ids", "service_ids", "strength")})
    for source, kind, foreign, definitions in (("memberships", "rank", "category_id", "categories"),
            ("eligibilities", "eligibility", "service_id", "services"), ("grants", "grant", "capability_id", "capabilities"),
            ("programme_memberships", "programme", "programme_id", "programmes")):
        if kind not in layers:
            continue
        for row in memberships if source == "programme_memberships" else data[source]:
            if row["department_id"] == dep and row["person_id"] in ids:
                append(row, kind, source, name(definitions, row[foreign], kind.capitalize()),
                    "revoked" if row.get("revoked") else "active", details={foreign: row[foreign],
                    **({"external": row["external"], "home_programme": row["home_programme"]}
                       if source == "programme_memberships" else {}),
                    **({"service_id": row.get("service_id")} if source == "grants" else {})})
    lo, hi = local_midnight(start, str(zone)), local_midnight(end, str(zone))
    if {"leave", "time_away", "protected_time"} & layers:
        availability = [row for row in data["availability"] if not row.get("pattern_id")]
        typed = leave_types.names(db, dep, availability)
        for row in typed:
            if not in_scope(row):
                continue
            kind = "protected_time" if row["kind"] == "protected_time" else "leave" if row["kind"] in leave_types.LEAVE_KINDS else "time_away"
            if kind not in layers:
                continue
            wording = leave_wording(department, actor, SimpleNamespace(**row), row["leave_type_name"])
            wording.pop("note", None)
            label = "Protected time" if kind == "protected_time" else wording.pop("label")
            append(row, kind, "availability", label, "recorded", {"availability_kind": row["kind"],
                "counts_as_unavailable": row["counts_as_unavailable"], **wording})
        if "time_away" in layers:
            for row in data["availability"]:
                if row.get("pattern_id") and row["department_id"] == dep and row["person_id"] in ids and in_scope(row):
                    append(row, "time_away", "availability_patterns", "Weekly unavailability", details={"pattern_id": row["pattern_id"]})
    entries = [row for row in data["programme_calendar_entries"] if
               row["department_id"] == dep and row["starts_on"] < end.isoformat() and row["ends_on"] > start.isoformat() and
               (led is None or row["programme_id"] in led)]
    entries_by_id = {row["id"]: row for row in entries}
    members_by_programme = defaultdict(list)
    for row in memberships:
        if row["person_id"] in ids:
            members_by_programme[row["programme_id"]].append(row)
    for entry in entries:
        if "academic" not in layers or entry["kind"] != "academic_block":
            continue
        spans = defaultdict(list)
        for member in members_by_programme[entry["programme_id"]]:
            first, last = max(entry["starts_on"], member["starts_on"]), min(entry["ends_on"], member["ends_on"])
            if first < last:
                spans[member["person_id"]].append((first, last))
        for pid, intervals in spans.items():
            for first, last in sorted(set(intervals)):
                append({**entry, "starts_on": first, "ends_on": last}, "academic", "programme_calendar_entries", entry["name"],
                       person_id=pid, details={"programme_id": entry["programme_id"], "calendar_entry_id": entry["id"]})
    from .scheduling_facts import FACT_LABELS, _personal_projection
    facts = [row for row in data["scheduling_facts"] if not row.get("event_occurrence_id")]
    for row in facts:
        if (row["status"] == "active" and (row["kind"] not in {"protected_time", "overtime_offer"} or not row["on"])
                or not _overlaps(row, start, end, zone) or row.get("calendar_entry_id") and row["calendar_entry_id"] not in entries_by_id):
            continue
        kind = row["kind"] if row["status"] == "active" else "pending"
        if kind not in layers and not (kind == "protected_time" and "protected_time" in layers):
            continue
        spans = [(row["person_id"], row.get("starts_on"), row.get("ends_on"))]
        if row.get("calendar_entry_id"):
            entry = entries_by_id[row["calendar_entry_id"]]
            spans = [(member["person_id"], max(row.get("starts_on") or start.isoformat(), member["starts_on"]),
                      min(row.get("ends_on") or end.isoformat(), member["ends_on"]))
                     for member in members_by_programme[entry["programme_id"]]
                     if not row["person_id"] or member["person_id"] == row["person_id"]]
        if led is not None:
            spans = [(pid, max(first or start.isoformat(), lo.isoformat()), min(last or end.isoformat(), hi.isoformat()))
                     for pid, first, last in spans for lo, hi in lead_spans.get(pid, ())]
        for pid, first, last in set(spans):
            if pid in ids and (not first or not last or first < last):
                line = row["data"].get("line", {})
                if not actor.permits(*s.TEAM_READER_ROLES):
                    line = _personal_projection(line, pid)
                if led is not None:
                    line = {**line, "starts_on": first, "ends_on": last}
                append({**row, "starts_on": first, "ends_on": last}, kind, "scheduling_facts",
                    ("Stop " if not row["on"] else "") + FACT_LABELS[row["kind"]], row["status"],
                    {"fact_kind": row["kind"], "on": row["on"], "calendar_entry_id": row["calendar_entry_id"],
                     "line": line}, person_id=pid)
    for fact in data["scheduling_facts"] if "protected_time" in layers else ():
        if fact.get("event_occurrence_id") and fact["department_id"] == dep:
            interval = fact["data"]["interval"]
            attendees = {pid for line in fact["data"]["line"]["who"]["conditions"] for pid in line["values"]}
            for pid in ids & attendees:
                row = {**fact, **interval, "person_id": pid}
                if in_scope(row):
                    append(row, "protected_time", "events", fact["name"], details={"event_occurrence_id": fact["event_occurrence_id"]})
    request_rows = list(db.scalars(select(m.RequestRecord).where(
        m.RequestRecord.department_id == dep, training.change_person_scope(ids), m.RequestRecord.status == "submitted",
        m.RequestRecord.start < hi, m.RequestRecord.end > lo,
        training.request_scope(db, actor) if led is not None else True))) if "pending" in layers else []
    requests = [record(row, exclude=("private_reason", "decision_reason")) for row in request_rows]
    from .availability import _decidable
    decidable = _decidable(db, actor, [("request", row) for row in request_rows])
    visible_rotation_ids = {r["id"] for r in rotations}
    rotation_people = training.change_rotation_people(db, actor, request_rows, ids)
    for row in request_rows:
        if row.kind == "rotation_change":
            for part in training.change_parts(row, rotation_people):
                append({"id": row.id, "revision": row.revision, "person_id": part["person_id"], **part["proposed"]},
                    "pending", "requests", "Rotation swap" if (row.details or {}).get("swap") else "Rotation change",
                    "submitted", {**{key: value for key, value in part.items() if key not in {"id", "revision", "person_id"}},
                                  "request_kind": row.kind, "can_decide": row.id in decidable})
    for row in requests:
        if row["kind"] == "rotation_change":
            continue
        append(row, "pending", "requests", "Leave request" if row["kind"] == "leave" else
            row["kind"].replace("_", " ").capitalize(), "submitted",
            {"request_kind": row["kind"], "rotation_id": row.get("rotation_id"),
             **({"availability_kind": row["details"]["availability_kind"]} if (row.get("details") or {}).get("availability_kind") else {})})

    density = defaultdict(Counter)
    if "duty_density" in layers:
        for shift in data["shifts"]:
            if data["types"].get(shift["assignment_type_id"], {}).get("department_id") != dep or inactive(shift):
                continue
            for participant in shift["participants"]:
                day = aware(participant["start"]).astimezone(zone).date()
                if participant["person_id"] in ids and start <= day < end:
                    density[participant["person_id"]][day.isoformat()] += 1
    findings = _stored_findings(db, actor, start, end, ids, zone) if "conflict" in layers else []
    from .availability import redact_rotation_leave_findings
    findings = redact_rotation_leave_findings(findings, rotation_ids=visible_rotation_ids,
        availability_ids={row["id"] for row in data["availability"] if in_scope(row)},
        request_ids={row["id"] for row in requests})
    if not actor.permits(*s.TEAM_READER_ROLES):
        findings = staff_overlap_labels(findings)
    for finding in findings:
        pid = finding.get("person_id")
        rotation_id = (finding.get("detail") or {}).get("rotation_id")
        if rotation_id and rotation_id not in visible_rotation_ids:
            continue
        if pid in ids and (not finding.get("start") or _overlaps(finding, start, end, zone)) and (
                led is None or not finding.get("start") or in_scope(finding)):
            people[pid]["conflicts"].append({**finding, "detail": finding.get("detail") or {}})
    for pid, person in people.items():
        person["items"].sort(key=lambda row: (row["starts_on"], row["kind"], row["id"]))
        person["duty_density"] = [{"day": day, "count": count} for day, count in sorted(density[pid].items())]
    counts = Counter(item["kind"] for person in people.values() for item in person["items"])
    counts["duty_density"] = sum(sum(density[pid].values()) for pid in people)
    counts["conflict"] = sum(len(person["conflicts"]) for person in people.values())
    return {"start": start.isoformat(), "end": end.isoformat(), "timezone": str(zone), "total": total,
        "offset": offset, "limit": limit, "has_more": offset + len(people) < total, "group_by": group_by,
        "sort_by": sort_by, "sort_direction": sort_direction, "names": names, "people": list(people.values()),
        "rota_eligibility": rota_eligibility,
        "choices": training.rotation_choices(db, actor), "can_note": actor.permits("programme_lead"),
        "note_person_ids": sorted(note_person_ids),
        "layers": [{"kind": kind, "label": label, "style_key": style, "source": source,
                    "count": counts[kind] if kind in layers else 0, "requested": kind in layers}
                   for kind, label, style, source in LAYERS],
        "can_plan": actor.permits("admin", "coordinator", "programme_lead")}
