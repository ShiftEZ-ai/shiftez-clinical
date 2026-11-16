"""Tiny exhaustive AA/ZZ rotas catch solver constraints that reject valid work.

The oracle deliberately uses plain assignment enumeration and interval arithmetic,
not the solver's candidate preparation or constraint helpers.
"""
import copy
from datetime import datetime
from itertools import product
from random import Random

from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate


def _allowed(assignment, shifts, people, case):
    if assignment[0] != people[0]:  # The existing AA morning clinic is fixed.
        return False
    for index, person in enumerate(assignment):
        if (person, index) in case['leave']:
            return False
        if index == 2 and (person not in case['ucc_eligible']
                           or case['restrict_monday'] and person == people[1]):
            return False
    for person in people:
        work = [shifts[i] for i, assigned in enumerate(assignment) if assigned == person]
        if case['weekly_max'] and len(work) > case['weekly_max']:
            return False
        for earlier, later in zip(work, work[1:]):
            end = datetime.fromisoformat(earlier['end'])
            start = datetime.fromisoformat(later['start'])
            if start < end or case['rest_hours'] and (start - end).total_seconds() < case['rest_hours'] * 3600:
                return False
            if (case['forbid_next_day'] and earlier['assignment_type_id'] != later['assignment_type_id']
                    and (start.date() - datetime.fromisoformat(earlier['start']).date()).days == 1):
                return False
    return True


def _witness(data, assignment):
    witness = copy.deepcopy(data)
    template = witness['shifts'][0]['participants'][0]
    for index, (shift, person) in enumerate(zip(witness['shifts'], assignment)):
        shift['participants'] = [{**template, 'id': f'oracle-{index}', 'shift_id': shift['id'],
                                  'person_id': person, 'start': shift['start'], 'end': shift['end'],
                                  'fixed': index == 0}]
    return witness


def test_small_fm_clinic_and_ucc_rule_combinations_match_exhaustive_oracle(h):
    people = h.seed['people']
    fm_type = h.configure('assignment_types', {'name': 'AA clinic oracle', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    ucc_type = h.configure('assignment_types', {'name': 'ZZ shift oracle', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    policies = {
        'rest': h.policy({'kind': 'min_rest', 'hours': 12})['id'],
        'maximum': h.policy({'kind': 'max_duties', 'maximum': 2, 'window': 'week'})['id'],
        'restriction': h.policy({'kind': 'restriction', 'weekdays': [0], 'start_time': '00:00',
                                  'end_time': '24:00'}, {'person_id': people[1]})['id'],
        'transition': h.policy({'kind': 'forbidden_transition', 'from_type_ids': [fm_type],
                                 'to_type_ids': [ucc_type]})['id'],
    }
    w = h.shift(h.workspace(), typ=fm_type, people=[{'person_id': people[0],
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'fixed': True}])['workspace']
    w = h.shift(w, typ=fm_type, start='2026-10-04T13:00:00+03:00',
                end='2026-10-04T17:00:00+03:00', people=[])['workspace']
    w = h.shift(w, typ=ucc_type, service=1, location=1,
                start='2026-10-05T08:00:00+03:00', end='2026-10-05T16:00:00+03:00', people=[])['workspace']
    baseline = h.snapshot(w)
    assert len(baseline['shifts']) == 3
    rng = Random(621)
    cases = [
        (0, 0, (), (0, 1, 2), False, False),
        (24, 1, (), (0,), True, True),
        (2, 2, ((people[2], 2),), (0, 1, 2), False, True),
        (16, 1, ((people[1], 1),), (1, 2), True, False),
    ] + [
        (rng.choice((0, 2, 16, 24)), rng.choice((0, 1, 2)),
         tuple((people[p], i) for p in range(3) for i in (1, 2) if rng.randrange(5) == 0),
         tuple(p for p in range(3) if rng.randrange(4) != 0),
         bool(rng.randrange(2)), bool(rng.randrange(2)))
        for _ in range(24)
    ]
    outcomes = set()
    for number, (rest, maximum, leave, eligible, restrict, transition) in enumerate(cases):
        case = {'rest_hours': rest, 'weekly_max': maximum, 'leave': set(leave),
                'ucc_eligible': {people[i] for i in eligible}, 'restrict_monday': restrict,
                'forbid_next_day': transition}
        data = copy.deepcopy(baseline)
        for policy in data['policies']:
            if policy['id'] == policies['rest']:
                policy['active'] = rest > 0
                policy['definition']['rule']['hours'] = rest or 12
            elif policy['id'] == policies['maximum']:
                policy['active'] = maximum > 0
                policy['definition']['rule']['maximum'] = maximum or 2
            elif policy['id'] == policies['restriction']:
                policy['active'] = restrict
            elif policy['id'] == policies['transition']:
                policy['active'] = transition
        data['eligibilities'] = [row for row in data['eligibilities'] if
            row['service_id'] != h.seed['services'][1] or row['person_id'] in case['ucc_eligible']]
        for index, (person, shift_index) in enumerate(leave):
            shift = data['shifts'][shift_index]
            data['availability'].append({'id': f'leave-{number}-{index}', 'person_id': person,
                'kind': 'recorded_leave', 'start': shift['start'], 'end': shift['end']})
        assignments = product(people, repeat=3)
        valid = [choice for choice in assignments if _allowed(choice, data['shifts'], people, case)]
        outcomes.add(bool(valid))
        # Check the independent oracle against the canonical meaning for each tiny possibility.
        for choice in product(people, repeat=3):
            if choice[0] != people[0]:
                continue
            assert evaluate(_witness(data, choice))['valid'] == (choice in valid), (number, case, choice)
        result = generate(data, 5, feasibility_only=True)
        assert result['status'] == ('candidate_feasible' if valid else 'requirements_conflict'), (number, case, result)
        if valid:
            assert result['validation']['valid'], (number, case, result)
    assert outcomes == {False, True}

    # A soft ZZ preference may choose among valid assignments, never change feasibility.
    h.policy({'kind': 'allocation_preference', 'priority': 9},
             {'person_id': people[2], 'service_id': h.seed['services'][1]}, severity='advisory')
    preferred = h.snapshot(w)
    preferred_cases = {'rest_hours': 12, 'weekly_max': 2, 'leave': set(),
                       'ucc_eligible': set(people), 'restrict_monday': True,
                       'forbid_next_day': True}
    valid = [choice for choice in product(people, repeat=3)
             if _allowed(choice, preferred['shifts'], people, preferred_cases)]
    assert valid and any(choice[2] == people[2] for choice in valid)
    result = generate(preferred, 5)
    assert result['status'] == 'candidate_feasible' and result['optimality_proven'], result
    assigned_ucc = next(row['participant']['person_id'] for row in result['additions']
                        if row['shift_id'] == preferred['shifts'][2]['id'])
    assert int(assigned_ucc == people[2]) == max(int(choice[2] == people[2]) for choice in valid)
