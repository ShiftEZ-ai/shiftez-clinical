"""Recorded session teams and dated ranks, shared by duty readers and exports.

Ordering follows Planner's planner_teams.teamOrder; neither rank nor team leadership
implies a supervision relationship.
"""
from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from .time import aware, local_midnight
from .validation import categories_for, index_person_rows


def team_order(participants, roster=None):
    """Keep each attendance row, head each person once, and terminate recorded cycles."""
    here = {p['person_id'] for p in participants}
    on_session = {p['person_id'] for p in (participants if roster is None else roster)}
    children = defaultdict(list)
    roots = []
    for index, p in enumerate(participants):
        supervisor = p.get('supervisor_person_id')
        if supervisor and supervisor != p['person_id'] and supervisor in here:
            children[supervisor].append(index)
        else:
            roots.append(index)
    seen, headed, result = set(), set(), []
    for root in [*roots, *range(len(participants))]:
        stack = [(root, 0)]
        while stack:
            index, depth = stack.pop()
            if index in seen:
                continue
            seen.add(index)
            p = participants[index]
            supervisor = p.get('supervisor_person_id')
            kids = [] if p['person_id'] in headed else children[p['person_id']]
            headed.add(p['person_id'])
            result.append({'p': p, 'depth': depth,
                'missing': bool(supervisor and supervisor != p['person_id'] and supervisor not in on_session),
                'trainees': len({participants[i]['person_id'] for i in kids})})
            stack.extend((i, depth + 1) for i in reversed(kids))
    return result


def team_facts(participants):
    """One bounded session index for Home's own duty and its named pairs."""
    children = defaultdict(dict)
    ranks = {p['person_id']: p.get('rank', '') for p in participants}
    ordered = team_order(participants)
    for entry in ordered:
        p = entry['p']
        supervisor = p.get('supervisor_person_id')
        if supervisor and supervisor != p['person_id']:
            children[supervisor][p['person_id']] = None
    result = {}
    for entry in ordered:
        p = entry['p']
        pid, supervisor = p['person_id'], p.get('supervisor_person_id')
        paired = [pid, supervisor, *children[pid]]
        result[p['id']] = {'rank': p.get('rank', ''), 'trainee_ids': list(children[pid]),
            'supervisor_not_on_session': entry['missing'],
            'pair_ranks': {person: ranks[person] for person in paired if person in ranks}}
    return result


def with_duty_ranks(data, shifts):
    """Resolve only missing labels on the session's local day; preserve frozen issued labels."""
    context = {**data, '_person_rows': index_person_rows(data)}
    zone = data.get('timezone') or data['organisation']['timezone']
    context['organisation'] = {**data.get('organisation', {}), 'timezone': zone}
    labels = {}
    result = []
    for shift in shifts:
        day = aware(shift['start']).astimezone(ZoneInfo(zone)).date()
        start, end = local_midnight(day, zone), local_midnight(day + timedelta(days=1), zone)
        participants = []
        for p in shift['participants']:
            key = p['person_id'], day
            if 'rank' in p:
                participants.append(p)
                continue
            if key not in labels:
                ranks = [context['categories'][rid] for rid in categories_for(context, p['person_id'], start, end)]
                labels[key] = ' / '.join(sorted({r.get('short_code') or r['name'] for r in ranks}))
            participants.append({**p, 'rank': labels[key]})
        result.append({**shift, 'participants': participants})
    return result


def own_and_paired(participants, person_id):
    """What a staff member sees of a session: their own participation and the people paired with them on it (their named
    supervisor, the trainees who name them). The one rule for the published rota, the Schedule and exports."""
    if person_id is None:
        return []
    mine = next((p for p in participants if p["person_id"] == person_id), None)
    supervisor = (mine or {}).get("supervisor_person_id")
    return [{key: value for key, value in p.items() if key != 'override_reason'} for p in participants
            if p["person_id"] in (person_id, supervisor) or p.get("supervisor_person_id") == person_id]
