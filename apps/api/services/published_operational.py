"""Current absence warnings over issued duties; never a rewrite or a new publication check.

Availability expansion and conflict detection belong to schedule_view. This projection clips those reservations
through canonical planned presence and gives Home and published consumers the same deduplicated task facts.
"""
from collections import defaultdict
from zoneinfo import ZoneInfo

from sqlalchemy import and_, false, or_, select, tuple_
from sqlalchemy.orm import aliased

from apps.api import models as m
from apps.api.snapshots import session_match_groups, shift_records, snapshot_hash
from packages.contracts import schemas as s
from packages.domain.overlap import merged_windows, overlap_dates
from packages.domain.participation import presence_intervals
from packages.domain.recurrence import inactive
from packages.domain.time import aware, local_midnight
from . import schedule


def amendment_targets(db, publications, amendments, shifts):
    """Prove unique demand matches across each exact base/amendment, beyond the visible duties."""
    from .planning import chunks
    pairs = {(wid, aware(row['start'])) for row in shifts if row['publication_id'] in amendments
             for wid in (publications[row['publication_id']].workspace_id, amendments[row['publication_id']])}
    S = m.Shift
    by_workspace = defaultdict(list)
    for batch in chunks(sorted(pairs)):
        query = select(S.id, S.workspace_id, S.assignment_type_id, S.type_revision, S.type_snapshot,
            S.location_id, S.attributes, S.cancelled, S.start, S.end).where(
                S.workspace_id.in_({wid for wid, _ in batch}),
                tuple_(S.workspace_id, S.start).in_(batch)).execution_options(yield_per=1000)
        for page in db.execute(query).mappings().partitions():
            for row in shift_records(db, page, include_participants=False):
                by_workspace[row['workspace_id']].append(row)
    result = {}
    for pub_id, wid in amendments.items():
        for old, choices in session_match_groups(by_workspace[wid], by_workspace[publications[pub_id].workspace_id]):
            state = ('ambiguous' if len(old) > 1 or len(choices) > 1 else
                     'matched' if choices else 'changed_or_missing')
            target = choices[0]['id'] if state == 'matched' else None
            result.update({row['id']: (target, state) for row in old})
    return result


def project(db, actor, publications, shifts, zone, start, end, *, flagged=False):
    """One warning task per issued participant, with merged exact future absence/presence intersections."""
    from .schedule_view import session_overlap_flags
    team = actor.permits(*s.PERMISSIONS['schedule.read_team'])
    now = m.now()
    current = {p.id: p for p in publications if p.current and p.department_id == actor.department_id}
    shifts = [row for row in shifts if row.get('publication_id') in current and not inactive(row)]
    if not flagged:
        shifts = session_overlap_flags(db, actor, shifts, zone, team=team)
    amendments = {}
    if actor.permits('coordinator') and current:
        W = m.Workspace
        for base, wid in db.execute(select(W.base_publication_id, W.id).where(
                W.department_id == actor.department_id, W.base_publication_id.in_(current),
                W.status.in_(schedule.LIVE), W.archived_at.is_(None)).order_by(W.created_at, W.id)):
            amendments.setdefault(base, wid)
    warnings = {p.id: [] for p in publications}
    for shift in shifts:
        pub = current[shift['publication_id']]
        if aware(shift['start']) >= local_midnight(schedule.until(pub), zone):
            continue
        for participant in shift['participants']:
            pid = participant['person_id']
            if not team and pid != actor.person_id:
                continue
            spans = []
            for row in shift.get('double_booking_conflicts', []):
                if row['code'] != 'UNAVAILABLE' or row['person_id'] != pid:
                    continue
                for a, b in presence_intervals(participant):
                    lo, hi = max(a, aware(row['start'])), min(b, aware(row['end']))
                    if lo < hi and hi > now:
                        spans.append((lo, hi))
            for a, b in merged_windows(spans):
                action = {'key': 'amend_publication', 'publication_id': pub.id, 'workspace_id': pub.workspace_id,
                          'amendment_workspace_id': amendments.get(pub.id), 'shift_id': shift['id'],
                          'amendment_shift_id': None,
                          'amendment_shift_state': 'changed_or_missing' if pub.id in amendments else 'not_created',
                          'date': aware(shift['start']).astimezone(ZoneInfo(zone)).date().isoformat()} if actor.permits('coordinator') else None
                warnings[pub.id].append({'key': snapshot_hash([pub.id, shift['id'], pid]),
                    'publication_id': pub.id, 'workspace_id': pub.workspace_id, 'shift_id': shift['id'],
                    'assignment_id': participant['id'], 'person_id': pid,
                    'start': a.isoformat(), 'end': b.isoformat(), 'action': action})
    warned = {row['shift_id'] for rows in warnings.values() for row in rows if row['action']}
    targets = amendment_targets(db, current, amendments, [row for row in shifts if row['id'] in warned])
    for rows in warnings.values():
        for row in rows:
            if row['action'] and row['shift_id'] in targets:
                row['action']['amendment_shift_id'], row['action']['amendment_shift_state'] = targets[row['shift_id']]
    result = {}
    for pub in publications:
        rows = sorted(warnings[pub.id], key=lambda row: (row['start'], row['shift_id'], row['person_id']))
        payload = {'basis': 'current_absence_overlaps', 'starts_on': start.isoformat(), 'ends_on': end.isoformat(),
                   'conflicting_assignments': len({row['key'] for row in rows}),
                   'affected_sessions': len({row['shift_id'] for row in rows}), 'warnings': rows}
        result[pub.id] = s.PublishedOperationalSummary(**payload, as_of=now,
            fingerprint=snapshot_hash([pub.id, pub.current, schedule.until(pub).isoformat(), payload])).model_dump(mode='json')
    return result


def home_attention(db, actor, *, page_size=100, exclude_sessions=None):
    """Keyset pages over future in-force duties only; fixed-size Home sample, no snapshots or history scan."""
    from .schedule_view import published_shifts
    # These are coordinator-owned remediation tasks.  Staff and reviewers can still read
    # the published operational facts on Schedule/publication detail, but Home's
    # "Needs you" must contain only work they can act on: the amendment action is
    # coordinator-only (SW3-B-06), so the item is too.
    if not actor.permits('coordinator'):
        return None
    zone = schedule.zone_of(db, actor.department_id)
    now = m.now()
    first, _ = overlap_dates(now, now, zone)
    pubs = schedule.current_publications(db, [actor.department_id], first, light=True)
    if not pubs:
        return None
    last = max(schedule.until(p) for p in pubs)
    S, A = m.Shift, m.Assignment
    V, Pattern = m.Availability, m.AvailabilityPattern
    # Most future duties have no absence. Project only indexed person/interval intersections;
    # the canonical reader still decides exact presence, recurring absence and issued closure.
    absent = select(V.id).where(V.person_id == A.person_id, V.retracted.is_(False),
        V.counts_as_unavailable.is_(True), V.start < A.end, V.end > A.start, V.end > now).exists()
    recurring = select(Pattern.id).where(Pattern.person_id == A.person_id,
        Pattern.status == 'active', or_(Pattern.ends_on.is_(None), Pattern.ends_on > first)).exists()
    candidates = select(A.shift_id).where(A.end > now, or_(absent, recurring))
    query = select(S.id, S.start, S.end, S.workspace_id).where(or_(*(schedule.in_force_clause(p, zone) for p in pubs)), S.end > now,
        S.cancelled.is_(False), S.id.in_(candidates))
    if not actor.permits(*s.PERMISSIONS['schedule.read_team']):
        query = query.where(S.id.in_(candidates.where(A.person_id == actor.person_id)))
    if exclude_sessions is not None:
        query = query.where(S.id.not_in(exclude_sessions))
    total, sample, cursor = 0, [], None
    while True:
        page = query.where(or_(S.start > cursor[0], and_(S.start == cursor[0], S.id > cursor[1]))) if cursor else query
        rows = list(db.execute(page.order_by(S.start, S.id).limit(page_size)))
        if not rows:
            break
        cursor = (rows[-1].start, rows[-1].id)
        # Keep publication-time closure/retirement facts through the canonical reader.
        lo, hi = min(aware(r.start) for r in rows), max(aware(r.end) for r in rows)
        page_pubs = [p for p in pubs if p.workspace_id in {r.workspace_id for r in rows}]
        shifts = published_shifts(db, page_pubs, zone, lo, hi, shift_ids=[r.id for r in rows])
        projections = project(db, actor, page_pubs, shifts, zone, first, last)
        for summary in projections.values():
            warnings = summary['warnings']
            total += len({row['key'] for row in warnings})
            seen = {row['key'] for row in sample}
            for warning in warnings:
                if warning['key'] not in seen:
                    sample.append(warning)
                    seen.add(warning['key'])
            sample = sorted(sample, key=lambda row: (row['start'], row['key']))[:2]
    return s.PublishedOperationalAttention(count=total, sample=sample,
        action=sample[0]['action'] if sample else None).model_dump(mode='json') if total else None


def amendment_duplicate(db, department_id, now):
    """A cloned amendment duty already has a current published task; leave new draft duties in the draft query."""
    zone = schedule.zone_of(db, department_id)
    first, _ = overlap_dates(now, now, zone)
    pubs = schedule.current_publications(db, [department_id], first, light=True)
    if not pubs:
        return false()
    P, S, A = aliased(m.Publication), aliased(m.Shift), aliased(m.Assignment)
    return select(P.id).join(S, S.workspace_id == P.workspace_id).join(A, A.shift_id == S.id).where(
        P.id == m.Workspace.base_publication_id, P.current.is_(True), P.department_id == m.Workspace.department_id,
        or_(*(and_(P.id == pub.id, S.start < local_midnight(schedule.until(pub), zone)) for pub in pubs)),
        S.cancelled.is_(False), S.start == m.Shift.start, S.end == m.Shift.end,
        S.location_id == m.Shift.location_id, A.start == m.Assignment.start, A.end == m.Assignment.end,
        S.assignment_type_id == m.Shift.assignment_type_id, A.person_id == m.Assignment.person_id,
        A.start < m.RequestRecord.end, A.end > m.RequestRecord.start).exists()
