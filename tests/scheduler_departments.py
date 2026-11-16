"""Seeded synthetic departments for scheduler property and performance tests.

Rules and duty types go through the real commands (so they are validated and
stored exactly like production ones); people, leave and sessions are then scaled
up in the immutable snapshot, as the four-week department tests do.
"""
import copy
import uuid
from datetime import date, timedelta
from random import Random

ZONE = '+03:00'
START = date(2026, 10, 4)  # a Sunday; the demo organisation week starts on Sunday


def _id(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'scheduler-department:' + ':'.join(map(str, parts))))


def department(h, seed, people=20, days=7, rules='full', leave_rate=0.05, clinics=1):
    """Build a varied department snapshot. Returns (snapshot, info).

    `clinics`: parallel morning and afternoon clinics a day (one night cover), for department-sized session counts."""
    rng = Random(seed)
    s = h.seed
    trainee_rank, supervise = s['trainee_category'], s['supervisor_capability']
    minimum = {'am': rng.choice((1, 2)), 'pm': rng.choice((1, 2)), 'night': 1}
    types = {
        'am': h.configure('assignment_types', {'name': f'Morning clinic {seed}', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': minimum['am'], 'maximum': minimum['am'] + 1}]}})['record']['id'],
        'pm': h.configure('assignment_types', {'name': f'Afternoon clinic {seed}', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': minimum['pm'], 'target': minimum['pm'] + 1,
                                             'capabilities': [{'capability_id': s['capability'], 'minimum': 1}]}],
                           'supervision': {'trainee_category_ids': [trainee_rank], 'capability_id': supervise,
                                           'trainees_per_supervisor': 2}}})['record']['id'],
        'night': h.configure('assignment_types', {'name': f'Night cover {seed}', 'starts_on': '2020-01-01',
            'definition': {'requirements': [{'minimum': 1, 'maximum': 1}], 'allowed_category_ids': [s['category']]}})['record']['id'],
    }
    names = {}
    if rules != 'none':
        mandatory = [
            ('min_rest', {'kind': 'min_rest', 'hours': 11}, {}),
            ('one_a_day', {'kind': 'max_duties', 'maximum': 1, 'window': 'day'}, {}),
            ('week_duties', {'kind': 'max_duties', 'maximum': 5, 'window': 'week'}, {}),
            ('week_hours', {'kind': 'max_hours', 'hours': 40, 'window': 'week'}, {}),
            ('consecutive', {'kind': 'max_consecutive_days', 'maximum': 4}, {}),
            ('weekends', {'kind': 'max_consecutive_weekends', 'maximum': 1, 'weekend_start': 4, 'weekend_days': 2}, {}),
            ('night_then_morning', {'kind': 'forbidden_transition', 'from_type_ids': [types['night']],
                                    'to_type_ids': [types['am']]}, {}),
            ('travel', {'kind': 'travel_buffer', 'from_location_id': s['locations'][0],
                        'to_location_id': s['locations'][1], 'minutes': 90}, {}),
            ('no_fridays', {'kind': 'restriction', 'weekdays': [4], 'start_time': '00:00', 'end_time': '24:00'},
             {'person_id': s['people'][1]}),
            ('monthly_cap', {'kind': 'block_quota', 'starts_on': START.isoformat(),
                             'ends_on': (START + timedelta(days=days)).isoformat(), 'metric': 'duties',
                             'comparison': 'maximum', 'amount': max(4, days * 2 // 3)}, {}),
            ('supervision_cap', {'kind': 'supervision_limit', 'mode': 'capacity', 'maximum': 2}, {}),
            ('night_cap', {'kind': 'maximum_participants', 'maximum': 1}, {'assignment_type_id': types['night']}),
        ]
        advisory = [
            ('soft_week', {'kind': 'max_duties', 'maximum': 4, 'window': 'week'}, {}),
            ('soft_rest', {'kind': 'min_rest', 'hours': 16}, {}),
            ('prefer_seed', {'kind': 'allocation_preference', 'priority': 5}, {'person_id': s['people'][2]}),
        ]
        chosen = mandatory if rules == 'full' else rng.sample(mandatory, rng.randint(3, len(mandatory)))
        for key, rule, scope in chosen:
            names[key] = h.policy(rule, scope)['id']
        for key, rule, scope in advisory if rules == 'full' else rng.sample(advisory, rng.randint(0, 2)):
            names[key] = h.policy(rule, scope, severity='advisory')['id']
    w = h.workspace(start=START.isoformat(), end=(START + timedelta(days=days)).isoformat())
    templates = {}
    for key, (start, end, location, service) in {'am': ('08:00', '12:00', 0, 0), 'pm': ('13:00', '17:00', 1, 0),
                                                  'night': ('20:00', '08:00', 0, 1)}.items():
        next_day = START + timedelta(days=int(end < start))
        w = h.shift(w, typ=types[key], start=f'{START}T{start}:00{ZONE}', end=f'{next_day}T{end}:00{ZONE}',
                    location=location, service=service, people=[])['workspace']
    data = h.snapshot(w)
    for shift in data['shifts']:
        if shift['id'] not in data['focus_ids']:
            continue  # Published neighbour duties are context, not this department's templates.
        key = next(k for k, t in types.items() if t == shift['assignment_type_id'])
        templates[key] = shift
    for location in data['locations'].values():
        location['capacity'] = 1000
    source = s['people'][0]
    base = {k: [r for r in data[k] if r['person_id'] == source] for k in ('memberships', 'eligibilities', 'grants')}
    trainees = set()
    for n in range(3, people):
        pid = _id(seed, 'person', n)
        data['people'][pid] = {**data['people'][source], 'id': pid, 'display_name': f'Synthetic clinician {n}'}
        trainee = rng.random() < 0.25
        for row in base['memberships']:
            data['memberships'].append({**row, 'id': _id(seed, 'membership', n), 'person_id': pid,
                                        'category_id': trainee_rank if trainee else row['category_id']})
        for i, row in enumerate(base['eligibilities']):
            if i == 0 or rng.random() < 0.8:  # a few people are not eligible for the night service
                data['eligibilities'].append({**row, 'id': _id(seed, 'eligibility', n, i), 'person_id': pid})
        if not trainee:
            for i, row in enumerate(base['grants']):
                data['grants'].append({**row, 'id': _id(seed, 'grant', n, i), 'person_id': pid})
        else:
            trainees.add(pid)
    people_ids = list(data['people'])
    data['shifts'] = []
    for day in range(days):
        on = START + timedelta(days=day)
        for key, template in templates.items():
            for clinic in range(1 if key == 'night' else clinics):
                shift = copy.deepcopy(template)
                start, end = template['start'][11:], template['end'][11:]
                finish = on + timedelta(days=int(key == 'night'))
                shift.update(id=_id(seed, 'shift', day, key, *([clinic] if clinic else [])), start=f'{on}T{start}',
                             end=f'{finish}T{end}', participants=[], series_id=None, locked=False)
                data['shifts'].append(shift)
    # Leave: whole days, drawn per person.
    for pid in people_ids:
        for day in range(days):
            if rng.random() < leave_rate:
                on = START + timedelta(days=day)
                data['availability'].append({'id': _id(seed, 'leave', pid, day), 'person_id': pid, 'kind': 'recorded_leave',
                    'start': f'{on}T00:00:00{ZONE}', 'end': f'{on + timedelta(days=1)}T00:00:00{ZONE}'})
    # One person is away on an external rotation for the whole period.
    away = people_ids[-1]
    data.setdefault('rotations', []).append({'id': _id(seed, 'rotation'), 'person_id': away, 'rotation_type_id': None,
        'department_available': False, 'starts_on': START.isoformat(), 'ends_on': (START + timedelta(days=days)).isoformat()})
    data['focus_ids'] = [shift['id'] for shift in data['shifts']]
    data.pop('generation_session_ids', None)
    return data, {'types': types, 'policies': names, 'trainees': trainees, 'away': away, 'minimum': minimum}


def apply(data, result):
    """The draft after applying a proposal, built independently of the solver's copy."""
    draft = copy.deepcopy(data)
    removed = {(r['shift_id'], r['assignment_id']) for r in result['removals']}
    for shift in draft['shifts']:
        shift['participants'] = [p for p in shift['participants'] if (shift['id'], p['id']) not in removed]
        shift['participants'] += [a['participant'] for a in result['additions'] if a['shift_id'] == shift['id']]
    return draft


def assert_no_mandatory_single_addition(data, result):
    """Every admissible fill trial on a final mandatory gap leaves mandatory deficits non-decreasing."""
    from packages.domain import generation
    from packages.domain.finding_identity import finding_key
    from packages.domain.generation import (COVERAGE_CODES, Recheck, deficit_score,
                                             introduced_overflow,
                                             introduced_supervision_gaps, keep_assignment,
                                             own_duty_blocked)
    from packages.domain.generation import allocation_settings
    from packages.domain.generation_scope import population_allows
    from packages.domain.recurrence import series_candidate_allowed
    from packages.domain.time import aware, overlaps
    from packages.domain.validation import (categories_for, qualified, person_rows,
                                             restriction_blocks, rotation_blocks,
                                             trainee_intervals, participation_for,
                                             qualified_supervisor)

    final = apply(data, result)
    recheck = Recheck(final)
    baseline = recheck.result()
    before = deficit_score(baseline)
    if not any(before[:2]):
        return 0

    focus = set(final['focus_ids'])
    selected = set(final.get('generation_session_ids', focus))
    history = {pid: [] for pid in final['people']}
    for shift in final['shifts']:
        for participant in shift['participants']:
            history.setdefault(participant['person_id'], []).append((shift, participant))
    own_errors = {}
    tolerated = {finding_key(f) for f in generation.finding_observations(baseline)
                 if f['severity'] == 'error'}
    mandatory_shifts = {f.get('shift_id') for f in generation.finding_observations(baseline)
                        if f.get('shift_id') and (
                            f['code'] in COVERAGE_CODES and f['severity'] == 'error'
                            or f['code'] == 'QUOTA_BELOW_MINIMUM')}
    overflow_rules = {p['id']: p for p in final['policies']
                      if p['status'] == 'active' and p['definition']['rule'].get('overflow_maximum', 0)}
    checked = 0
    for shift in final['shifts']:
        if shift['id'] not in mandatory_shifts or shift['locked'] or shift['id'] not in focus & selected:
            continue
        a, b = aware(shift['start']), aware(shift['end'])
        definition = shift['type_snapshot']['definition']
        current_ids = {p['person_id'] for p in shift['participants']}
        for pid, person in final['people'].items():
            if (pid in current_ids or not person['active'] or not population_allows(final, shift, pid)
                    or not series_candidate_allowed(final, shift, pid)
                    or not qualified(final, pid, shift, definition, a, b)
                    or rotation_blocks(final, pid, a, b) or restriction_blocks(final, pid, shift, a, b)
                    or any(overlaps(a, b, aware(row['start']), aware(row['end']))
                           for row in person_rows(final, 'availability', pid))):
                continue
            displaced = [(other, prior) for other, prior in history[pid]
                         if overlaps(a, b, aware(prior['start']), aware(prior['end']))]
            if displaced and (any(other['locked'] or other['id'] not in focus & selected
                                  or keep_assignment(final, other, prior)
                                  for other, prior in displaced)):
                continue
            overtime_options = [False]
            if definition.get('extra_cover', False):
                overtime_options = []
            if allocation_settings(final, shift, pid, history=history[pid])[1]:
                overtime_options.append(True)
            for overtime in overtime_options:
                participant = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  final['fingerprint'] + shift['id'] + pid)),
                               "person_id": pid, "shift_id": shift['id'],
                               "start": shift['start'], "end": shift['end'],
                               "participation": participation_for(final, pid, shift, a, b),
                               "overtime": overtime, "revision": 1}
                intervals = trainee_intervals(final, shift, participant)
                pairings = [None]
                if intervals:
                    pairings += [p['person_id'] for p in shift['participants'] if all(
                        qualified_supervisor(final, shift, p, lo, hi) for lo, hi in intervals)]
                for supervisor_id in pairings:
                    if supervisor_id:
                        participant['supervisor_person_id'] = supervisor_id
                    for other, prior in displaced:
                        other['participants'].remove(prior)
                    if own_duty_blocked(final, shift, participant,
                                        [other for other, _ in history[pid]], own_errors):
                        for other, prior in displaced:
                            other['participants'].append(prior)
                        continue
                    shift['participants'].append(participant)
                    trial = recheck.trial([shift, *(other for other, _ in displaced)], [pid])
                    blockers = [f for f in generation.finding_observations(trial)
                                if f['severity'] == 'error'
                                and (f['code'] not in COVERAGE_CODES | {'QUOTA_BELOW_MINIMUM', 'ATTACHMENT_PAIRING'}
                                     or f['code'] == 'LEAD_GAP' and 'maximum' in (f.get('detail') or {}))
                                and finding_key(f) not in tolerated]
                    blockers.extend(introduced_supervision_gaps(baseline, trial))
                    overflow = introduced_overflow(baseline, trial)
                    if overflow and any(
                            not overflow_rules.get(f['rule_id'], {}).get('definition', {}).get('rule', {}).get(
                                'overflow_category_ids', [])
                            or not set(overflow_rules[f['rule_id']]['definition']['rule']['overflow_category_ids']).intersection(
                                categories_for(final, pid, aware(f['start']), aware(f['end'])))
                            for f in overflow):
                        blockers.extend(overflow)
                    shift['participants'].pop()
                    for other, prior in displaced:
                        other['participants'].append(prior)
                    if blockers:
                        continue
                    checked += 1
                    after = deficit_score(trial)
                    assert after[0] >= before[0] and after[1] >= before[1], {
                        'shift_id': shift['id'], 'person_id': pid,
                        'before': before, 'after': after,
                    }
    return checked
