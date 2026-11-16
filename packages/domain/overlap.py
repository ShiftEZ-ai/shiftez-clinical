"""Shared reservation conflicts for recurring previews, planner flags and rota checks."""
from collections import defaultdict
from datetime import date, timedelta
from heapq import heappop, heappush
from zoneinfo import ZoneInfo

from packages.contracts.schemas import MAX_DUTY_DURATION_MINUTES
from .recurrence import inactive, series_definition, usual_assignee_ids, with_series_state
from .time import aware, local_midnight

MAX_DUTY_DURATION = timedelta(minutes=MAX_DUTY_DURATION_MINUTES)


def overlap_envelope(start, end):
    """Bound possible peers by elapsed duty duration, before converting to local dates."""
    return aware(start) - MAX_DUTY_DURATION, aware(end) + MAX_DUTY_DURATION


def overlap_dates(start, end, zone):
    """Publication admission dates enclosing all possible peer starts in the department's zone."""
    start, end = overlap_envelope(start, end)
    tz = ZoneInfo(zone)
    return start.astimezone(tz).date(), end.astimezone(tz).date() + timedelta(days=1)


def merged_windows(windows):
    merged = []
    for start, stop in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(stop, merged[-1][1]))
        else:
            merged.append((start, stop))
    return merged


def _session(data, shift):
    if shift is None:
        return {'shift_id': None, 'workspace_id': None, 'series_id': None, 'occurrence_on': None, 'session_name': None,
                'clinic_ids': [], 'clinic_names': []}
    series = shift.get('series_snapshot') or {}
    typ = shift.get('type_snapshot') or data.get('types', {}).get(shift.get('assignment_type_id'), {})
    clinics = list(shift.get('service_ids', []))
    clinic_names = [data.get('services', {}).get(cid, {}).get('name', cid) for cid in clinics]
    name = series.get('name') or (typ.get('name') or shift['id']) + (f" ({', '.join(clinic_names)})" if clinic_names else '')
    return {'shift_id': shift['id'], 'workspace_id': shift.get('workspace_id'), 'series_id': shift.get('series_id'),
            'occurrence_on': str(shift['occurrence_on']) if shift.get('occurrence_on') else None,
            'session_name': name,
            'clinic_ids': clinics,
            'clinic_names': clinic_names}


def _conflict(data, pid, shift, other, a, b, code='TIME_OVERLAP', availability_id=None):
    zone = series_definition(shift).get('timezone') or data['organisation']['timezone']
    person = data.get('people', {}).get(pid, {})
    return {'code': code, 'date': str(shift.get('occurrence_on') or aware(shift['start']).astimezone(ZoneInfo(zone)).date()),
            'start': a.isoformat(), 'end': b.isoformat(), 'person_id': pid,
            'person_name': person.get('display_name') or person.get('name') or pid,
            **_session(data, shift), **{'other_' + key: value for key, value in _session(data, other).items()},
            'availability_id': availability_id}


def evaluate_overlaps(data, *, focus_ids=None, include_fixed=False, include_unavailability=True, commitments=None):
    """One row per pair, using full reserved envelopes, never off-cover gaps.

    An interval sweep visits only overlapping reservations/absence records; no
    database access or per-session scan of every other person or leave record.
    """
    if focus_ids is None:
        focus_ids = data['focus_ids'] if 'focus_ids' in data else (s['id'] for s in data['shifts'])
    focus = set(focus_ids)
    if not focus:
        return []
    if commitments is None:
        data = with_series_state(data)
        commitments = defaultdict(list)
        for shift in data['shifts']:
            if inactive(shift):
                continue
            assigned = set()
            for person in shift.get('participants', []):
                assigned.add(person['person_id'])
                commitments[person['person_id']].append((shift, person, aware(person['start']), aware(person['end'])))
            if include_fixed and series_definition(shift).get('usual_assignees_fixed'):
                for pid in usual_assignee_ids(shift) - assigned:
                    commitments[pid].append((shift, None, aware(shift['start']), aware(shift['end'])))
    subjects = data.get('person_rule_subjects')
    partial = data.get('_partial')
    if partial is not None and not partial.get('complete'):
        subjects = partial['people'] if subjects is None else set(subjects) & set(partial['people'])
    events = {pid: [(a, b, shift, None, None) for shift, _, a, b in rows]
              for pid, rows in commitments.items() if subjects is None or pid in subjects}
    if include_unavailability:
        for row in data.get('availability', []):
            if row['person_id'] in events:
                events[row['person_id']].append((aware(row['start']), aware(row['end']), None, 'UNAVAILABLE', row.get('id')))
        zone = data['organisation']['timezone']
        for row in data.get('rotations', []):
            if row.get('department_available', True) or row['person_id'] not in events:
                continue
            a = local_midnight(date.fromisoformat(str(row['starts_on'])), zone)
            b = (local_midnight(date.fromisoformat(str(row['ends_on'])), zone) if row.get('ends_on')
                 else max(event[1] for event in events[row['person_id']]))
            events[row['person_id']].append((a, b, None, 'EXTERNAL_ROTATION', row.get('id')))
    result = []
    for pid, rows in events.items():
        data.get('_check_budget', lambda: None)()
        active, ends = ({}, {}), []
        for n, event in enumerate(sorted(rows, key=lambda row: row[0])):
            a, b, shift, code, record_id = event
            if a >= b:
                continue
            while ends and ends[0][0] <= a:
                _, previous, kind = heappop(ends)
                active[kind].pop(previous, None)
            for c, d, other, _, _ in active[0].values():
                if shift is None:
                    if other['id'] in focus:
                        result.append(_conflict(data, pid, other, None, a, min(b, d), code, record_id))
                elif other['id'] != shift['id'] and (other['id'] in focus or shift['id'] in focus):
                    first, second = (other, shift) if other['id'] in focus else (shift, other)
                    result.append(_conflict(data, pid, first, second, a, min(b, d)))
            if shift is not None and shift['id'] in focus:
                for c, d, _, absence_code, absence_id in active[1].values():
                    result.append(_conflict(data, pid, shift, None, a, min(b, d), absence_code, absence_id))
            kind = int(shift is None)
            active[kind][n] = event
            heappush(ends, (b, n, kind))
    return result


def per_session_overlaps(data, conflicts=None, *, focus_ids=None):
    """Orient the same conflict to both sessions, or only to the requested projection rows."""
    result = defaultdict(list)
    focus = None if focus_ids is None else set(focus_ids)
    shifts = {shift['id']: shift for shift in data['shifts']}
    for row in evaluate_overlaps(data, include_fixed=True) if conflicts is None else conflicts:
        if focus is None or row['shift_id'] in focus:
            result[row['shift_id']].append(row)
        if row['other_shift_id'] is None or focus is not None and row['other_shift_id'] not in focus:
            continue
        shift, other = shifts[row['other_shift_id']], shifts[row['shift_id']]
        mirror = _conflict(data, row['person_id'], shift, other, aware(row['start']), aware(row['end']))
        result[shift['id']].append(mirror)
    return dict(result)
