"""Opt-in Where is everyone projection: bounded SQL aggregation, never a domain snapshot."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy.orm import load_only
from sqlalchemy import and_, cast, Date, func, literal, or_, select, union_all

from apps.api import models as m
from apps.api.errors import require
from packages.contracts import schemas as s
from packages.domain import conditions
from packages.domain.recurrence import expand, stored_series_definition, MAX_PREVIEW_DAYS
from packages.domain.time import aware, local_midnight, now
from packages.domain.whereabouts import DAY, project, short_label
from . import schedule
from .leave_disclosure import leave_wording
from .workspace_access import shared_clause


def options(bucket, source, hide, hide_leave_types):
    require(bucket in {'day', 'week', 'month'}, 'TIMELINE_BUCKET', 'Choose day, week or month', 422)
    sources = set((source if source is not None else 'published,draft,usual').split(','))
    hidden = set(filter(None, (hide or '').split(',')))
    leave_ids = set(filter(None, (hide_leave_types or '').split(',')))
    require(bool(sources) and sources <= {'published', 'draft', 'usual'}, 'TIMELINE_SOURCE', 'Choose supported sources', 422)
    require(hidden <= {'leave', 'academic', 'rotation', 'work'}, 'TIMELINE_HIDE', 'Choose supported layers', 422)
    require(len(leave_ids) <= 200, 'TIMELINE_HIDE', 'Choose up to 200 leave types', 422)
    for identifier in leave_ids:
        try:
            s.TypeAdapter(s.ID).validate_python(identifier)
        except (ValueError, TypeError):
            require(False, 'TIMELINE_HIDE', 'Choose valid leave types', 422)
    return sources, hidden, leave_ids


def catalogue(db, department_id):
    clinics = {row.id: {'id': row.id, 'label': row.name, 'short_name': short_label(row.name), 'colour': row.colour or None}
               for row in db.scalars(select(m.Service).where(m.Service.department_id == department_id))}
    tags = {row.id: {'id': row.id, 'label': row.name, 'short_name': short_label(row.name, row.short_name),
                     'colour': row.colour or None}
            for row in db.scalars(select(m.Tag).where(m.Tag.department_id == department_id, m.Tag.active.is_(True)))
            if 'services' in row.labels}
    members = defaultdict(list)
    for clinic_id, tag_id in db.execute(select(m.TagMember.member_id, m.TagMember.tag_id).where(
            m.TagMember.department_id == department_id, m.TagMember.kind == 'services', m.TagMember.tag_id.in_(tags))):
        if clinic_id in clinics:
            members[clinic_id].append(tag_id)
    return clinics, clinics | tags, members, set(tags)


def rosters(db, actor, start, end, zone, sources, workspace_id):
    """Publication effective ranges share the Schedule owner; drafts use one latest shared rota per day."""
    pubs = schedule.current_publications(db, [actor.department_id], start, end, light=True)
    W = m.Workspace
    drafts = list(db.scalars(select(W).where(W.department_id == actor.department_id,
        W.status.in_(schedule.LIVE), W.archived_at.is_(None), shared_clause(W),
        ~W.id.in_(select(m.PublicationChangeList.workspace_id)), W.starts_on < end, W.ends_on > start,
        W.id == workspace_id if workspace_id else True).options(load_only(
            W.id, W.name, W.starts_on, W.ends_on, W.created_at)).order_by(W.created_at.desc(), W.id.desc()))) if {'draft', 'usual'} & sources else []
    if workspace_id:
        require(bool(drafts) and actor.permits(*s.TEAM_READER_ROLES), 'NOT_FOUND', 'Shared draft rota not found', 404)
    # Rota names have the same swap-participant privacy as Schedule.
    from .common import rota_names
    workspace_ids = {p.workspace_id for p in pubs} | {w.id for w in drafts}
    labels = dict(db.execute(select(W.id, W.name).where(W.id.in_(workspace_ids))).all()) if workspace_ids else {}
    names = rota_names(db, actor.roles, actor.person_id, labels)
    roster, selected = {}, {}
    blocked_months = set()
    for pub in pubs:
        day = max(start, pub.starts_on)
        while day < min(end, schedule.until(pub)):
            blocked_months.add((day.year, day.month))
            if 'published' in sources:
                roster[day] = ('published', names.get(pub.workspace_id))
                selected[day] = pub.workspace_id
            day += DAY
    for draft in drafts:
        day = max(start, draft.starts_on)
        while day < min(end, draft.ends_on):
            blocked_months.add((day.year, day.month))
            if day not in selected and 'draft' in sources and actor.permits(*s.TEAM_READER_ROLES):
                roster[day] = ('draft', names.get(draft.id))
                selected[day] = draft.id
            day += DAY
    return pubs, drafts, roster, selected, blocked_months


def assignments(db, ids, start, end, zone, selected, tag_ids, closed=()):
    """One SQL statement returns daily clinic and area COUNT(DISTINCT assignment), not assignment payloads.

    A multi-clinic session contributes once per area, even if two of its clinics share that tag.
    PostgreSQL converts to department local date before grouping; SQLite is the test adapter.
    """
    if not ids or not selected:
        return {}
    A, S, C, T = m.Assignment, m.Shift, m.ShiftService, m.TagMember
    local = (cast(func.timezone(zone, S.start), Date) if db.bind.dialect.name == 'postgresql' else
             func.date(S.start, f'{int(local_midnight(start, zone).utcoffset().total_seconds()):+d} seconds'))
    base = select(A.id.label('assignment'), A.person_id.label('person'), local.label('day'),
                  S.workspace_id.label('workspace'), C.service_id.label('clinic')).join(S, S.id == A.shift_id).join(
                  C, C.shift_id == S.id).where(A.person_id.in_(ids), S.workspace_id.in_(set(selected.values())),
                  S.cancelled.is_(False), ~S.id.in_(closed), S.start >= local_midnight(start, zone), S.start < local_midnight(end, zone)).cte('work')
    clinic = select(base.c.person, base.c.day, base.c.workspace, literal('clinic').label('kind'),
                    base.c.clinic.label('identifier'), func.count(func.distinct(base.c.assignment)).label('count')).group_by(
                    base.c.person, base.c.day, base.c.workspace, base.c.clinic)
    area_id = func.coalesce(T.tag_id, base.c.clinic)
    area = select(base.c.person, base.c.day, base.c.workspace, literal('area').label('kind'), area_id.label('identifier'),
                  func.count(func.distinct(base.c.assignment)).label('count')).select_from(base.outerjoin(T,
                  and_(T.member_id == base.c.clinic, T.kind == 'services', T.tag_id.in_(tag_ids)))).group_by(
                  base.c.person, base.c.day, base.c.workspace, area_id)
    duties = select(base.c.person, base.c.day, base.c.workspace, literal('duty').label('kind'),
                    literal('').label('identifier'), func.count(func.distinct(base.c.assignment)).label('count')).group_by(
                    base.c.person, base.c.day, base.c.workspace)
    work = defaultdict(lambda: defaultdict(lambda: {'areas': Counter(), 'clinics': Counter(), 'duties': 0}))
    for row in db.execute(union_all(clinic, area, duties)):
        day = row.day if isinstance(row.day, date) else date.fromisoformat(row.day)
        if selected.get(day) == row.workspace:
            if row.kind == 'duty':
                work[row.person][day]['duties'] += row.count
            else:
                work[row.person][day]['clinics' if row.kind == 'clinic' else 'areas'][row.identifier] += row.count
    return work


def usual(db, actor, ids, start, end, zone, blocked, members, work, roster):
    """Expand each bounded recurring series once, never query per person or occurrence."""
    today = now().astimezone(ZoneInfo(zone)).date()
    rows = db.scalars(select(m.RecurringSeries).where(m.RecurringSeries.department_id == actor.department_id,
        m.RecurringSeries.starts_on < end, or_(m.RecurringSeries.ends_on.is_(None), m.RecurringSeries.ends_on > start)))
    for series in rows:
        definition = stored_series_definition(series.definition)
        people = set(definition.default_person_ids) & ids
        if not people:
            continue
        first, last = max(start, series.starts_on), min(end, series.ends_on or end)
        while first < last:
            stop = min(last, first + timedelta(days=MAX_PREVIEW_DAYS))
            for occurrence in expand(definition, first, stop):
                day = aware(occurrence['start']).astimezone(ZoneInfo(zone)).date()
                if (day.year, day.month) <= (today.year, today.month) or (day.year, day.month) in blocked:
                    continue
                roster.setdefault(day, ('usual', None))
                area_ids = {tag for clinic_id in definition.service_ids for tag in members.get(clinic_id, [clinic_id])}
                for pid in people:
                    current = work.setdefault(pid, {}).setdefault(day, {'areas': Counter(), 'clinics': Counter(), 'duties': 0})
                    current['duties'] += 1
                    current['clinics'].update(definition.service_ids)
                    current['areas'].update(area_ids)
            first = stop


def spans(db, actor, ids, data, start, end, hidden, hidden_leave):
    from .timeline import _dates
    from . import leave_types
    zone = ZoneInfo(data['organisation']['timezone'])
    result = defaultdict(list)
    department = db.get(m.Department, actor.department_id)
    types = {row.id: row for row in db.scalars(select(m.LeaveType).where(m.LeaveType.department_id == actor.department_id))}
    if 'leave' not in hidden or 'academic' not in hidden:
        for row in leave_types.names(db, actor.department_id, data['availability']):
            if row['person_id'] not in ids or row.get('pattern_id'):
                continue
            protected = row['kind'] == 'protected_time'
            if protected and 'academic' in hidden or not protected and 'leave' in hidden:
                continue
            if not protected and row['kind'] not in leave_types.LEAVE_KINDS:
                continue
            wording = leave_wording(department, actor, SimpleNamespace(**row), row['leave_type_name'])
            identifier = wording.get('leave_type_id')
            if identifier and identifier in hidden_leave:
                continue
            leave = types.get(identifier)
            label = wording['label']
            first, last = _dates(row, zone)
            short = short_label(label, leave.short_name if leave else '', academic=protected)
            result[row['person_id']].append({'id': row['id'], 'starts_on': first, 'ends_on': last,
                'kind': 'protected' if protected else 'leave', 'label': label, 'short_label': short,
                'leave_type_id': identifier, 'leave_type': wording.get('leave_type_name'),
                'leave_short_name': short if identifier else None, 'leave_colour': leave.colour or None if leave else None,
                'status': 'approved' if row['kind'] == 'approved_leave' else 'recorded'})
    if 'rotation' not in hidden:
        for row in data['rotations']:
            if row['person_id'] not in ids:
                continue
            rotation = data['rotation_types'].get(row.get('rotation_type_id'), {})
            service = data['services'].get(row.get('service_id'), {})
            label = rotation.get('name') or service.get('name') or 'Rotation'
            first, last = _dates(row, zone)
            result[row['person_id']].append({'id': row['id'], 'starts_on': first, 'ends_on': last,
                'kind': 'home' if row.get('department_available', True) else 'away', 'label': label,
                'short_label': short_label(label, rotation.get('short_name', '')), 'rotation_type_id': row.get('rotation_type_id')})
    if 'academic' not in hidden:
        members = defaultdict(list)
        for row in data['programme_memberships']:
            members[row['programme_id']].append(row)
        for entry in data.get('programme_calendar_entries', []):
            if entry['kind'] != 'academic_block':
                continue
            a, b = _dates(entry, zone)
            for member in members[entry['programme_id']]:
                lo, hi = _dates(member, zone)
                if member['person_id'] in ids and max(a, lo) < min(b, hi):
                    result[member['person_id']].append({'id': entry['id'], 'starts_on': max(a, lo), 'ends_on': min(b, hi),
                        'kind': 'academic', 'label': entry['name'], 'short_label': short_label(entry['name'], academic=True)})
        # Use the existing catalogue resolver and restriction clock, including weekday and interval semantics.
        from packages.domain.rule_lines import fact_policy
        from packages.domain.validation import restricted_intervals, scope_intervals
        data.setdefault('policies', [])
        for fact in data['scheduling_facts']:
            if fact['kind'] != 'protected_time' or fact['status'] != 'active' or not fact['on']:
                continue
            candidates = {fact['person_id']} if fact.get('person_id') else ids
            windows = {pid: [(start, end)] for pid in candidates & ids}
            if fact.get('calendar_entry_id'):
                entry = next((row for row in data.get('programme_calendar_entries', []) if row['id'] == fact['calendar_entry_id']), None)
                windows = defaultdict(list)
                if entry:
                    ea, eb = _dates(entry, zone)
                    for member in members[entry['programme_id']]:
                        ma, mb = _dates(member, zone)
                        lo, hi = max(start, ea, ma), min(end, eb, mb)
                        if member['person_id'] in candidates & ids and lo < hi:
                            windows[member['person_id']].append((lo, hi))
            for policy in conditions.active_rules([fact_policy(fact)], context=data):
                for pid, dates in windows.items():
                    for first, last in dates:
                        a, b = local_midnight(first, str(zone)), local_midnight(last, str(zone))
                        scope = policy['definition']['scope']
                        # Person-side reach intentionally ignores clocks; intersect the shared clock first.
                        windows = [(ca, cb) for lo, hi in restricted_intervals(data, policy, a, b, pid)
                                   for ca, cb in scope_intervals(data, scope, pid, lo, hi)]
                        for lo, hi in windows:
                            for sa, sb, matched in conditions.scope_segments(data, policy['definition']['scope'], pid, None, lo, hi):
                                if not matched:
                                    continue
                                first_day = sa.astimezone(zone).date()
                                last_day = (sb - timedelta(microseconds=1)).astimezone(zone).date() + DAY
                                label = fact.get('name') or 'Protected time'
                                result[pid].append({'id': fact['id'], 'starts_on': first_day, 'ends_on': last_day,
                                    'kind': 'protected', 'label': label, 'short_label': short_label(label, academic=True)})
    # Rank membership is the source of truth for whether somebody is in the department.  Keep the
    # complement server-owned so every client gets the same start, leave and internal-gap semantics.
    memberships = defaultdict(list)
    for row in data.get('memberships') or ():
        if row.get('department_id') == actor.department_id and row.get('person_id') in ids:
            memberships[row['person_id']].append(_dates(row, zone))
    labels = {'not_started': 'Not started yet', 'left': 'Left the department', 'gap': 'Not in the department'}
    for pid, intervals in memberships.items():
        intervals = sorted((first, last) for first, last in intervals if first < last)
        if not intervals:
            continue
        covered = []
        for first, last in intervals:
            if last <= start or first >= end:
                continue
            first, last = max(first, start), min(last, end)
            if covered and first <= covered[-1][1]:
                covered[-1] = (covered[-1][0], max(covered[-1][1], last))
            else:
                covered.append((first, last))
        gaps = []
        if covered:
            first, last = covered[0][0], covered[-1][1]
            if start < first:
                reason = 'gap' if any(end_on <= first for _, end_on in intervals) else 'not_started'
                gaps.append((start, first, reason))
            for (_, prior_end), (next_start, _) in zip(covered, covered[1:]):
                if prior_end < next_start:
                    gaps.append((prior_end, next_start, 'gap'))
            if last < end:
                reason = 'gap' if any(start_on >= last for start_on, _ in intervals) else 'left'
                gaps.append((last, end, reason))
        else:
            prior = any(end_on <= start for _, end_on in intervals)
            future = any(start_on >= end for start_on, _ in intervals)
            if prior or future:
                reason = 'gap' if prior and future else 'left' if prior else 'not_started'
                gaps.append((start, end, reason))
        for first, last, reason in gaps:
            result[pid].append({'id': f'outside:{reason}:{first.isoformat()}', 'starts_on': first, 'ends_on': last,
                'kind': 'outside', 'reason': reason, 'label': labels[reason], 'short_label': 'Out'})
    return result


def read_work(db, actor, ids, start, end, zone, sources, workspace_id=None):
    clinics, areas, members, tag_ids = catalogue(db, actor.department_id)
    pubs, drafts, roster, selected, blocked = rosters(db, actor, start, end, zone, sources, workspace_id)
    coverage = db.scalars(select(m.Publication.validation['coverage']).where(m.Publication.id.in_([p.id for p in pubs]))) if pubs else []
    closed = {row['shift_id'] for rows in coverage for row in rows or [] if row.get('status') == 'closed'}
    work = assignments(db, ids, start, end, zone, selected, tag_ids, closed)
    if 'usual' in sources:
        usual(db, actor, ids, start, end, zone, blocked, members, work, roster)
    return work, areas, clinics, roster


def projection(db, actor, ids, data, start, end, bucket, sources, hidden, hidden_leave, workspace_id=None, preloaded=None):
    work, areas, clinics, roster = preloaded or read_work(db, actor, ids, start, end, data['organisation']['timezone'], sources, workspace_id)
    facts = spans(db, actor, ids, data, start, end, hidden, hidden_leave)
    rules = s.WhereaboutsSettings.model_validate((db.get(m.Department, actor.department_id).display_settings or {}).get('whereabouts') or {})
    return {pid: project(start, end, bucket, work.get(pid, {}), facts[pid], areas, clinics, roster, show_work='work' not in hidden,
                         area_share=rules.area_share, max_areas=rules.max_areas)
            for pid in ids}
