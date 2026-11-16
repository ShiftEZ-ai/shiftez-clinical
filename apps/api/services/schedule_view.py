"""Windowed reads of the schedule (Year, Month and Week views): day summaries, one dot per source per day, and the
published sessions of a window. Built from Shift/Assignment rows and stored checks, never from snapshot JSON; what is in
force comes from services/schedule.py."""
import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import false, or_, select
from apps.api import models as m
from apps.api.snapshots import shift_records, workspace_shifts, project_series_shifts, frozen_series_states
from apps.api.services import schedule
from apps.api.services.events import schedule_occurrences
from apps.api.services.date_sets import data as date_set_data
from packages.contracts.schemas import PERMISSIONS
from packages.contracts import schemas as s
from packages.domain import conditions
from packages.domain.date_sets import resolve_date_sets
from packages.domain.time import aware, local_midnight
from packages.domain.recurrence import inactive
from packages.domain.supervision_teams import own_and_paired
from packages.domain.overlap import evaluate_overlaps, overlap_dates, per_session_overlaps
from apps.api.services.common import rota_names

# shortcut: per-process memo of lightweight source facts, 64 source versions, oldest dropped first; a shared cache if the API
# runs many workers and Year views stay slow.
_summaries = {}


def date_markers(db, department_id, start, end):
    """Named department dates, including days without anyone's scheduled duties."""
    result = {}
    for entry in resolve_date_sets({'date_sets': date_set_data(db, [department_id])}, start,
                                  end - timedelta(days=1), errors={}):
        first = max(start, date.fromisoformat(entry['first_day']))
        last = min(end - timedelta(days=1), date.fromisoformat(entry['last_day']))
        marker = {key: entry[key] for key in ('set_id', 'set_name', 'entry_id', 'name')}
        for offset in range((last - first).days + 1):
            result.setdefault((first + timedelta(days=offset)).isoformat(), []).append(marker)
    return dict(sorted(result.items()))


def etag(*parts):
    return '"' + hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()[:32] + '"'


def sources(db, actor, start, end, drafts=True, exclude=()):
    """The sources meeting [start, end), light rows only (no sessions, no checks): current publications in force (staff:
    the ones they are on) and, for planners, rotas still being planned. Returns (timezone, [(source, row, version)])."""
    planner = actor.permits(*PERMISSIONS['schedule.read_team'])
    zone = schedule.zone_of(db, actor.department_id)
    pubs = [p for p in schedule.current_publications(db, [actor.department_id], start, end, light=True)
            if p.id not in exclude and p.workspace_id not in exclude]
    if not planner:
        PP = m.PublicationPerson
        mine = set(db.scalars(select(PP.publication_id).where(PP.person_id == actor.person_id, PP.publication_id.in_(
            [p.id for p in pubs])))) if actor.person_id and pubs else set()
        pubs = [p for p in pubs if p.id in mine]
    W = m.Workspace
    columns = (W.id, W.name, W.status, W.starts_on, W.ends_on, W.base_publication_id, W.replaces_publication_id,
               W.revision, W.content_revision, W.validation["evaluated_at"].as_string().label("evaluated_at"))
    owners = {r.id: r for r in db.execute(select(*columns).where(W.id.in_([p.workspace_id for p in pubs])))} if pubs else {}
    rows = []
    names = rota_names(db, actor.roles, actor.person_id, {w.id: w.name for w in owners.values()})
    for p in pubs:
        w = owners[p.workspace_id]
        rows.append(({"id": p.id, "kind": "published", "workspace_id": p.workspace_id, "name": names[w.id], "status": "published",
                      "starts_on": p.starts_on.isoformat(), "ends_on": p.ends_on.isoformat(),
                      "in_force_until": schedule.until(p).isoformat(), "sequence": p.sequence, "checked": True,
                      "base_publication_id": w.base_publication_id, "replaces_publication_id": w.replaces_publication_id},
                     p, (p.sequence, p.effective_until)))
    if planner and drafts:
        org = db.get(m.Organisation, actor.organisation_id)
        live = list(db.execute(select(*columns).where(W.department_id == actor.department_id, W.archived_at.is_(None),
                                                       W.status.in_(schedule.LIVE), W.starts_on < end, W.ends_on > start)
                               .order_by(W.starts_on, W.name)))
        names = rota_names(db, actor.roles, actor.person_id, {w.id: w.name for w in live})
        for w in live:
            rows.append(({"id": w.id, "kind": "draft", "workspace_id": w.id, "name": names[w.id], "status": w.status,
                          "starts_on": w.starts_on.isoformat(), "ends_on": w.ends_on.isoformat(), "in_force_until": None,
                          "sequence": None, "checked": w.evaluated_at is not None,
                          "base_publication_id": w.base_publication_id, "replaces_publication_id": w.replaces_publication_id},
                         w, (w.revision, w.content_revision, w.evaluated_at, org.input_revision, org.availability_revision)))
    return zone, rows


def summary(db, kind, row, zone):
    """Read source facts once; viewer and collection filters are applied after the shared memo."""
    S, A = m.Shift, m.Assignment
    clauses = (schedule.in_force_clause(row, zone) if kind == 'published' else S.workspace_id == row.id,
               S.cancelled.is_(False))
    projection = or_(*(S.series_snapshot['definition'][key].as_boolean().is_(True)
                       for key in ('usual_assignees_fixed', 'close_when_all_usual_unavailable'))).label('project')
    found = list(db.execute(select(S.id, S.start, S.end, S.assignment_type_id,
        S.type_snapshot['name'].as_string().label('name'),
        S.type_snapshot['short_code'].as_string().label('short_code'),
        S.type_snapshot['definition']['colour'].as_string().label('colour'), projection)
        .where(*clauses).order_by(S.start, S.id)))
    model = m.Publication if kind == 'published' else m.Workspace
    from packages.domain.rota_summary import normalized_check, rota_summary
    check = normalized_check(db.scalar(select(model.validation).where(model.id == row.id))) or {}
    if any(r.project for r in found):
        if kind == 'published':
            closed = {r['shift_id'] for r in check.get('coverage', []) if r['status'] == 'closed'}
        else:
            workspace = db.get(m.Workspace, row.id)
            closed = {r['id'] for r in project_series_shifts(db, workspace, workspace_shifts(db, row.id)) if r.get('closed')}
        found = [r for r in found if r.id not in closed]
    ids = {r.id for r in found}
    people = {}
    for person_id, shift_id in db.execute(select(A.person_id, A.shift_id).join(S).where(*clauses)):
        if shift_id in ids:
            people.setdefault(person_id, set()).add(shift_id)
    return {'rows': found, 'day_of': {r.id: aware(r.start).astimezone(ZoneInfo(zone)).date().isoformat() for r in found},
            'people': people, 'sessions': rota_summary(check)['sessions'],
            'findings': [{key: finding.get(key) for key in ('shift_id', 'start', 'severity', 'message')}
                         for finding in check.get('findings', [])]}


def _day_summary(source, zone, person_id=None, allowed_ids=None):
    """Project only this viewer's permitted sessions; staff responses never include source check facts."""
    mine = source['people'].get(person_id, set()) if person_id else None
    found = [r for r in source['rows'] if (mine is None or r.id in mine)
             and (allowed_ids is None or r.id in allowed_ids)]
    day_of = {r.id: source['day_of'][r.id] for r in found}
    days = {}
    for r in found:
        entry = days.setdefault(day_of[r.id], {"sessions": 0, "open": 0, "errors": 0, "warnings": 0, "issue": None})
        entry["sessions"] += 1
        if person_id:
            entry.setdefault("duties", []).append({
                "shift_id": r.id, "name": r.name, "short_code": r.short_code or '', "colour": r.colour or '',
                "assignment_type_id": r.assignment_type_id, "start": aware(r.start).isoformat(), "end": aware(r.end).isoformat()})
    if person_id or not days:
        return days
    for session in source['sessions']:
        if session['shift_id'] in day_of:
            entry = days[day_of[session['shift_id']]]
            entry['open'] += int(session['has_gap'])  # legacy gap-session field
            for field in ('places_needed', 'places_filled', 'places_open'):
                entry[field] = entry.get(field, 0) + session[field]
    issues = {}
    for f in source['findings']:
        d = (day_of.get(f['shift_id']) if f['shift_id'] else
             aware(f['start']).astimezone(ZoneInfo(zone)).date().isoformat() if f['start'] else None)
        if d not in days:
            continue
        if f.get("severity") == "error":
            days[d]["errors"] += 1
            issues.setdefault(d, Counter())[f.get("message")] += 1
        else:
            days[d]["warnings"] += 1
    for d, counts in issues.items():
        days[d]["issue"] = counts.most_common(1)[0][0]
    return days


def _collection_context(db, actor, collection_filter, zone):
    from . import collection_filters
    from .condition_context import evaluation_context, required_tokens
    from .training import led_programmes
    fields = {field['key'] for field in s.table_condition_definitions('schedule')}
    sorts = collection_filters.sort_keys(collection_filter, fields)
    if collection_filter is None or not collection_filter.conditions and not sorts:
        return {'tree': None, 'zone': zone, 'signature': collection_filter.model_dump(mode='json') if collection_filter else None}
    context = evaluation_context(db, actor, as_of=collection_filter.as_of, table_key='schedule',
                                 tokens=required_tokens(collection_filter.conditions))
    tree = collection_filters.validate(collection_filter.conditions, fields, context, table_key='schedule')
    org = db.get(m.Organisation, actor.organisation_id)
    programmes = led_programmes(db, actor)
    scope = sorted(db.scalars(programmes)) if programmes is not None else None
    used = (collection_filter.conditions.fields() if collection_filter.conditions else set()) | {field for field, _ in sorts}
    return {'tree': tree, 'fields': used, 'zone': zone, 'context': context,
            'data': {'organisation': {'timezone': zone}, 'locations': {}},
            'signature': [collection_filter.model_dump(mode='json'), context.today, context.as_of, context.values,
                          sorted(actor.roles), scope, org.input_revision, org.availability_revision]}


def _prepare_collection_data(db, actor, shifts, filtering):
    if not filtering.get('fields'):
        return
    from apps.api.auth import CATALOGUE_READ_ROLES
    from .bulk_selection import _page_context
    people = {participant['person_id'] for shift in shifts for participant in shift['participants']}
    # Paired identity visibility grants no access to that person's private rank, programme or tags.
    if not actor.permits(*CATALOGUE_READ_ROLES):
        people &= {actor.person_id}
    services = {service_id for shift in shifts for service_id in shift['service_ids']}
    data, _, _ = _page_context(db, actor, filtering['context'].as_of, people, services,
                               filtering['fields'] & set(s.CONDITION_FIELDS), scoped=True)
    filtering['data'] = data
    if filtering['tree']:
        filtering['tree'] = conditions.tag_scope(data, {'conditions': filtering['tree']})['conditions']


def _schedule_holds(row, data, person_id=None):
    start = aware(row['start'])
    session = conditions.rule_holds(data, None, row, start, start + timedelta(microseconds=1))
    people = data.setdefault('_schedule_person_holds', {})
    person_values = {}

    def person(pid):
        key = row['id'], pid
        if key not in people:
            people[key] = conditions.rule_holds(data, pid, row, start, start + timedelta(microseconds=1))
        return people[key]

    def holds(field):
        if field in conditions.SESSION_FIELDS:
            return session(field)
        if field in conditions.PERSON_FIELDS:
            if person_id is not None:
                return person(person_id)(field) or set()
            if field not in person_values:
                person_values[field] = set().union(*(person(participant['person_id'])(field) or set()
                    for participant in row['participants']))
            return person_values[field]
        if field == 'consultation_mode':
            return {data.get('services', {}).get(service_id, {}).get('mode') or 'unspecified'
                    for service_id in row['service_ids']}
        if field == 'weekday':
            return {str(start.astimezone(ZoneInfo(data['organisation']['timezone'])).weekday())}
        if field == 'rota_ids':
            return {value for value in (row.get('workspace_id'), row.get('publication_id')) if value}
        if field == 'source':
            return {row.get('publication_id') or row['workspace_id']}
        return row.get(field) if field in {'start', 'end'} else {row[field]} if row.get(field) else set()

    return holds


def _filter_shifts(shifts, filtering, collection_filter):
    from .collection_filters import sort_keys
    if filtering['tree']:
        shifts = [row for row in shifts if conditions.evaluate_candidates(filtering['tree'],
            _schedule_holds(row, filtering['data']),
            (_schedule_holds(row, filtering['data'], participant['person_id']) for participant in row['participants']) if row['participants'] else (),
            filtering['context'])]
    for field, direction in reversed(sort_keys(collection_filter, {field['key'] for field in s.table_condition_definitions('schedule')})):
        def value(row):
            if field in {'start', 'end', 'date'}:
                instant = aware(row['start' if field == 'date' else field])
                return instant.astimezone(ZoneInfo(filtering['zone'])).date().toordinal() if field == 'date' else instant.timestamp()
            raw = (sorted(_schedule_holds(row, filtering['data'])(field)) if field in {'category_ids', 'programme_ids', 'tags', 'weekday', 'consultation_mode'} else
                   row.get('publication_id') or row.get('workspace_id') if field == 'source' else
                   row.get('workspace_id') if field == 'rota_ids' else
                   sorted(participant['person_id'] for participant in row['participants']) if field == 'person_ids' else
                   row.get(field.removesuffix('s')) if field in {'location_ids', 'assignment_type_ids'} else row.get(field))
            return json.dumps(raw, sort_keys=True).lower() if raw is not None else None
        populated = [row for row in shifts if value(row) is not None]
        shifts = sorted(populated, key=value, reverse=direction == 'desc') + [row for row in shifts if value(row) is None]
    return shifts


def operational_sources(db, actor, rows, zone, start, end, *, shifts=None, flagged=False):
    from .published_operational import project
    pubs = [row for source, row, _ in rows if source['kind'] == 'published']
    if not pubs:
        return rows
    if shifts is None:
        shifts = published_shifts(db, pubs, zone, start, end,
                                 person_id=None if actor.permits(*PERMISSIONS['schedule.read_team']) else actor.person_id)
    projections = project(db, actor, pubs, shifts, zone, start, end, flagged=flagged)
    return [({**source, 'operational': projections[source['id']]} if source['kind'] == 'published' else source, row, version)
            for source, row, version in rows]


def days(db, actor, start, end, if_none_match=None, collection_filter=None):
    """Day summaries of every source in [start, end). Returns (ETag, body), body None when the tag matches."""
    planner = actor.permits(*PERMISSIONS['schedule.read_team'])
    zone, rows = sources(db, actor, start, end)
    rows = operational_sources(db, actor, rows, zone, start, end)
    markers = date_markers(db, actor.department_id, start, end)
    events = schedule_occurrences(db, actor, start, end)
    filtering = _collection_context(db, actor, collection_filter, zone)
    tag = etag("days", start, end, actor.identity_id, actor.person_id, planner, zone,
               [(s["id"], v, (s.get("operational") or {}).get("fingerprint")) for s, _, v in rows], events, markers, filtering['signature'])
    if if_none_match == tag:
        return tag, None
    first, last = start.isoformat(), end.isoformat()
    result, allowed_by_source = {}, None
    if filtering['tree']:
        person = None if planner else actor.person_id
        shifts = published_shifts(db, [row for source, row, _ in rows if source['kind'] == 'published'],
                                  zone, start, end, person_id=person)
        for source, row, _ in rows:
            if source['kind'] == 'draft':
                workspace = db.get(m.Workspace, row.id)
                shifts += project_series_shifts(db, workspace, workspace_shifts(db, row.id))
        if person:
            shifts = [staff_shift(shift, person) for shift in shifts]
        _prepare_collection_data(db, actor, shifts, filtering)
        allowed_by_source = {}
        for shift in _filter_shifts(shifts, filtering, collection_filter):
            allowed_by_source.setdefault(shift.get('publication_id') or shift['workspace_id'], set()).add(shift['id'])
    for source, row, version in rows:
        person = None if planner else actor.person_id
        key = (source['id'], version, zone)
        found = _summaries.get(key)
        if found is None:
            found = summary(db, source['kind'], row, zone)
            if len(_summaries) >= 64:
                _summaries.pop(next(iter(_summaries)), None)
            _summaries[key] = found
        allowed = allowed_by_source.get(source['id'], set()) if allowed_by_source is not None else None
        for d, counts in _day_summary(found, zone, person, allowed).items():
            if first <= d < last:
                result.setdefault(d, []).append({"source": source["id"], **counts})
    return tag, {"starts_on": first, "ends_on": last, "timezone": zone, "sources": [s for s, _, _ in rows],
                 "days": dict(sorted(result.items())), "events": events, "date_set_markers": markers}


def published_shifts(db, pubs, zone, start, end, before=None, person_id=None, *, shift_ids=None):
    """The sessions of these publications in force and overlapping [start, end), from Shift rows (never snapshots), each
    with its `publication_id`. `before`: {publication id: day} ends a publication earlier (a replacement being shown);
    `person_id`: only that person's sessions, closures left out (staff). One reader for every windowed view."""
    if not pubs:
        return []
    S, A = m.Shift, m.Assignment
    lo = aware(start) if isinstance(start, datetime) else local_midnight(start, zone)
    hi = aware(end) if isinstance(end, datetime) else local_midnight(end, zone)
    query = select(S).where(or_(*[schedule.in_force_clause(p, zone, (before or {}).get(p.id)) for p in pubs]),
                            S.start < hi, S.end > lo).order_by(S.start, S.id)
    if person_id is not None:
        query = query.where(S.cancelled.is_(False), S.id.in_(select(A.shift_id).where(A.person_id == person_id)))
    if shift_ids is not None:
        query = query.where(S.id.in_(shift_ids))
    owner = {p.workspace_id: p.id for p in pubs}
    rows = shift_records(db, db.scalars(query))
    by_workspace = {}
    for r in rows:
        by_workspace.setdefault(r['workspace_id'], []).append(r)
    frozen = {r['id']: r for p in pubs for r in frozen_series_states(by_workspace.get(p.workspace_id, []), p)}
    return [{**frozen[r['id']], "publication_id": owner[r["workspace_id"]]} for r in rows
            if person_id is None or not inactive(frozen[r['id']])]


def staff_shift(shift, person_id):
    """Project one published session through the same staff privacy boundary for every reader."""
    participants = own_and_paired(shift['participants'], person_id)
    definition = shift['type_snapshot']['definition']
    fields = [field for field in definition.get('fields', []) if field['visibility'] == 'staff']
    allowed = {field['key'] for field in fields}
    result = {**shift, 'participants': participants,
              'attributes': {key: value for key, value in shift['attributes'].items() if key in allowed},
              'type_snapshot': {**shift['type_snapshot'], 'definition': {**definition, 'fields': fields}}}
    # Template provenance includes planner-only attributes and other usual people's identities.
    result.pop('series_snapshot', None)
    result.pop('template_skipped', None)
    if 'usual_assignee_unavailability' in result:
        visible = {participant['person_id'] for participant in participants}
        result['usual_assignee_unavailability'] = [row for row in result['usual_assignee_unavailability']
                                                  if row['person_id'] in visible]
    if 'double_booking_conflicts' in result:
        from .leave_disclosure import staff_overlap_labels
        result['double_booking_conflicts'] = staff_overlap_labels([
            row for row in result['double_booking_conflicts'] if row['person_id'] == person_id])
    return result


def session_overlap_flags(db, actor, shifts, zone, *, team, people=None, services=None):
    """Current derived flags for published readers, with their own audience boundary."""
    if not shifts:
        return shifts
    shifts = [{key: value for key, value in row.items() if key not in {'double_booking_conflicts', 'absence_reports'}} for row in shifts]
    person_ids = {p['person_id'] for row in shifts for p in row['participants']}
    person_ids |= {pid for row in shifts for pid in
                   (row.get('series_snapshot') or {}).get('definition', {}).get('default_person_ids', [])}
    if people is None:
        people = {pid: {'id': pid, 'display_name': name} for pid, name in db.execute(select(
            m.Person.id, m.Person.display_name).where(m.Person.id.in_(person_ids)))}
    if services is None:
        service_ids = {sid for row in shifts for sid in row['service_ids']}
        services = {sid: {'id': sid, 'name': name} for sid, name in db.execute(select(
            m.Service.id, m.Service.name).where(m.Service.id.in_(service_ids)))}
    context = {'organisation': {'timezone': zone}, 'shifts': shifts, 'people': people, 'services': services}
    if person_ids:
        from .availability_patterns import expand
        lo, hi = min(aware(row['start']) for row in shifts), max(aware(row['end']) for row in shifts)
        departments = list(db.scalars(select(m.Department.id).where(m.Department.organisation_id == actor.organisation_id)))
        context['availability'] = [dict(row) for row in db.execute(select(
            m.Availability.id, m.Availability.person_id, m.Availability.start, m.Availability.end).where(
            m.Availability.person_id.in_(person_ids), m.Availability.start < hi, m.Availability.end > lo,
            m.Availability.retracted.is_(False), m.Availability.counts_as_unavailable.is_(True))).mappings()]
        context['availability'] += expand(db, departments, list(person_ids), lo, hi, zone)
        first = lo.astimezone(ZoneInfo(zone)).date()
        last = (hi - timedelta(microseconds=1)).astimezone(ZoneInfo(zone)).date()
        context['rotations'] = [dict(row) for row in db.execute(select(
            m.Rotation.id, m.Rotation.person_id, m.Rotation.starts_on, m.Rotation.ends_on,
            m.Rotation.department_available).where(m.Rotation.department_id.in_(departments),
            m.Rotation.person_id.in_(person_ids), m.Rotation.department_available.is_(False),
            m.Rotation.starts_on <= last, m.Rotation.ends_on > first)).mappings()]
    conflicts = per_session_overlaps(context, evaluate_overlaps(context))
    if not team:
        conflicts = {sid: [row for row in rows if row['person_id'] == actor.person_id] for sid, rows in conflicts.items()}
    shifts = [{**shift, 'double_booking_conflicts': conflicts[shift['id']]} if conflicts.get(shift['id']) else shift
              for shift in shifts]
    # A submitted absence report exists before approval creates Availability, so
    # it must be projected from the request itself.  Team readers see the
    # assignment; staff readers are limited to their own assignment.
    report_keys = {(shift['id'], participant['person_id'], participant['id'], participant['start'], participant['end'])
                  for shift in shifts for participant in shift['participants']
                  if team or participant['person_id'] == actor.person_id}
    if report_keys:
        person_ids = {person_id for _, person_id, *_ in report_keys}
        lo = min(aware(start) for *_, start, _ in report_keys)
        hi = max(aware(end) for *_, _, end in report_keys)
        reports = list(db.scalars(select(m.RequestRecord).where(
            m.RequestRecord.department_id == actor.department_id,
            m.RequestRecord.kind == 'absence_report',
            m.RequestRecord.status.in_(('submitted', 'approved')),
            m.RequestRecord.person_id.in_(person_ids),
            m.RequestRecord.start < hi, m.RequestRecord.end > lo)))
        by_assignment = {(row.person_id, aware(row.start), aware(row.end)): row for row in reports}
        absence = {}
        for shift_id, person_id, assignment_id, start, end in report_keys:
            row = by_assignment.get((person_id, aware(start), aware(end)))
            if row is not None:
                absence.setdefault(shift_id, []).append({'request_id': row.id, 'person_id': person_id,
                    'assignment_id': assignment_id, 'status': row.status})
        shifts = [{**shift, 'absence_reports': sorted(absence.get(shift['id'], []),
                                                       key=lambda row: (row['status'], row['person_id'], row['assignment_id']))}
                  for shift in shifts]
    else:
        shifts = [{**shift, 'absence_reports': []} for shift in shifts]
    from .series_conflicts import session_actions
    shifts = session_actions(db, actor, shifts, disclose=team)
    # Closure uses complete usual-assignee evidence before provenance and other people's fields are redacted.
    return shifts if team else [staff_shift(shift, actor.person_id) for shift in shifts]


def publication_session_flags(db, actor, publication, shifts, zone):
    """Enrich a publication response without changing its issued snapshot or check."""
    if not shifts:
        return shifts
    team = actor.permits(*PERMISSIONS['publications.read_team'])
    ids = {row['id'] for row in shifts}
    if not team:
        # Public snapshots already removed provenance; restore only the internal inputs needed for legacy closure.
        definitions = dict(db.execute(select(m.Shift.id, m.Shift.series_snapshot).where(m.Shift.id.in_(ids))).all())
        shifts = [{**row, 'series_snapshot': definitions.get(row['id'])} for row in shifts]
    peers = []
    if publication.current:
        lo, hi = min(aware(row['start']) for row in shifts), max(aware(row['end']) for row in shifts)
        start, end = overlap_dates(lo, hi, zone)
        publications = [row for row in schedule.current_publications(db, [actor.department_id], start, end, light=True)
                        if row.workspace_id != publication.workspace_id]
        peers = published_shifts(db, publications, zone, lo, hi, person_id=None if team else actor.person_id)
    projected = session_overlap_flags(db, actor, shifts + peers, zone, team=team)
    return [row for row in projected if row['id'] in ids]


def sessions(db, actor, start, end, exclude=(), if_none_match=None, collection_filter=None):
    """Published sessions in force overlapping [start, end), with their stored coverage and findings (planners) and the
    people, places and clinics they name. Staff: only their own duties. Returns (ETag, body), body None when unchanged."""
    planner = actor.permits(*PERMISSIONS['schedule.read_team'])
    zone, rows = sources(db, actor, start, end, drafts=False, exclude=exclude)
    markers = date_markers(db, actor.department_id, start, end)
    events = schedule_occurrences(db, actor, start, end)
    # Published duties are frozen, while names, pager numbers and places are read from current catalogues.
    organisation = db.get(m.Organisation, actor.organisation_id)
    department = db.get(m.Department, actor.department_id)
    filtering = _collection_context(db, actor, collection_filter, zone)
    shifts = published_shifts(db, [row for _, row, _ in rows], zone, start, end, person_id=None if planner else actor.person_id)
    coverage, findings = [], []
    visible_ids = {shift['id'] for shift in shifts}
    if shifts:
        lo, hi = min(aware(shift['start']) for shift in shifts), max(aware(shift['end']) for shift in shifts)
        first, last = overlap_dates(lo, hi, zone)
        _, peers = sources(db, actor, first, last, drafts=False, exclude=exclude)
        shifts += [shift for shift in published_shifts(db, [row for _, row, _ in peers], zone, lo, hi,
            person_id=None if planner else actor.person_id) if shift['id'] not in visible_ids]
    shifts = [shift for shift in session_overlap_flags(db, actor, shifts, zone, team=planner)
              if shift['id'] in visible_ids]
    # Source warnings and the rendered duties share one current overlap evaluation, before display filtering.
    rows = operational_sources(db, actor, rows, zone, start, end, shifts=shifts, flagged=True)
    tag = etag("sessions", start, end, actor.identity_id, actor.person_id, sorted(actor.roles), planner, zone, organisation.input_revision,
               organisation.availability_revision, department.leave_reason_display,
               m.now().astimezone(ZoneInfo(zone)).date().isoformat(),
               sorted(exclude), [(s["id"], v, (s.get("operational") or {}).get("fingerprint")) for s, _, v in rows], events, markers, filtering['signature'])
    if if_none_match == tag:
        return tag, None
    _prepare_collection_data(db, actor, shifts, filtering)
    shifts = _filter_shifts(shifts, filtering, collection_filter)
    from .people import duty_ranks
    shifts = duty_ranks(db, actor.department_id, shifts, zone)
    if planner:
        ids = {s["id"] for s in shifts}
        P = m.Publication
        for check in db.scalars(select(P.validation).where(P.id.in_({s["publication_id"] for s in shifts}))):
            coverage += [r for r in (check or {}).get("coverage", []) if r.get("shift_id") in ids]
            findings += [f for f in (check or {}).get("findings", []) if f.get("shift_id") in ids]
        from packages.domain.rota_summary import rota_summary
        summary = rota_summary({'coverage': coverage, 'findings': findings})
        by_session = {row['shift_id']: row for row in summary['sessions']}
        shifts = [{**row, 'staffing': {'on_duty': len({p['person_id'] for p in row['participants']}),
                    'places_open': by_session.get(row['id'], {}).get('places_open', 0)}} for row in shifts]
    person_ids = {p["person_id"] for s in shifts for p in s["participants"]}
    location_ids = {s["location_id"] for s in shifts}
    service_ids = {i for s in shifts for i in s["service_ids"]}
    Pe, L, Sv = m.Person, m.Location, m.Service
    from .people import visible_people
    entries = [{"id": i, "display_name": n, "short_code": c, "pager": g} for i, n, c, g in db.execute(
        select(Pe.id, Pe.display_name, Pe.short_code, Pe.pager).where(Pe.id.in_(person_ids)))] if person_ids else []
    people = {p['id']: p for p in visible_people(db, actor, entries)}
    # Schedule labels expose only the authorized pager; personal mobiles never enter print/export projections.
    for person in people.values():
        person.pop('contacts', None)
        for field in ('work_phone', 'work_email', 'work_extension'):
            person.pop(field, None)
    locations = {i: {"id": i, "name": n} for i, n in db.execute(select(L.id, L.name).where(L.id.in_(location_ids)))} if location_ids else {}
    services = {i: {"id": i, "name": n} for i, n in db.execute(select(Sv.id, Sv.name).where(Sv.id.in_(service_ids)))} if service_ids else {}
    return tag, {"starts_on": start.isoformat(), "ends_on": end.isoformat(), "timezone": zone, "sources": [s for s, _, _ in rows],
                 "shifts": shifts, "coverage": coverage, "findings": findings, "events": events,
                 **({'summary': summary['issued_counts']} if planner else {}),
                 "people": people, "locations": locations, "services": services, "date_set_markers": markers}
