"""Shape rules (runs, days off, weekend shape, succession, together/apart): the validator reports each breach as a
warning, the solver avoids them, and the score counts them under preferences (packages/domain/patterns.py)."""
import copy

import pytest

from packages.domain.cp_sat import generate
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate

DAY = '2026-10-{:02}T{}:00+03:00'  # 4 October 2026 is a Sunday; Friday 9 and Saturday 10 are a weekend (Fri-Sat)


def duty(h, name):
    return h.configure('assignment_types', {'name': name, 'starts_on': '2020-01-01',
                                            'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']


def rota(h, plan, start='2026-10-04', end='2026-10-12'):
    """plan: [(day, type, [people], (from, to))] -> the draft's validation."""
    w = h.workspace(start=start, end=end)
    result = None
    for day, typ, people, (a, b) in plan:
        result = h.shift(w, typ=typ, start=DAY.format(day, a), end=DAY.format(day, b), people=people)
        w = result['workspace']
    return w, result['validation']


def codes(validation, rule_id):
    return sorted({f['code'] for f in validation['findings'] if f['rule_id'] == rule_id})


AM, PM = ('08:00', '12:00'), ('14:00', '18:00')


@pytest.mark.parametrize('rule,days,expected', [
    ({'kind': 'run_length', 'minimum': 2}, [4], ['RUN_TOO_SHORT']),
    ({'kind': 'run_length', 'minimum': 2}, [4, 5], []),
    ({'kind': 'run_length', 'maximum': 2}, [4, 5, 6], ['RUN_TOO_LONG']),
    ({'kind': 'days_off_run', 'minimum': 2}, [4, 6], ['DAYS_OFF_SHORT']),
    ({'kind': 'days_off_run', 'minimum': 2}, [4, 7], []),
    ({'kind': 'weekend_shape', 'shape': 'complete', 'weekend_start': 4, 'weekend_days': 2}, [9], ['WEEKEND_SPLIT']),
    ({'kind': 'weekend_shape', 'shape': 'complete', 'weekend_start': 4, 'weekend_days': 2}, [9, 10], []),
    ({'kind': 'weekend_shape', 'shape': 'complete', 'weekend_start': 4, 'weekend_days': 2}, [8, 11], []),
])
def test_day_patterns_are_reported_as_advisory_warnings(h, rule, days, expected):
    policy = h.policy(rule, severity='advisory')
    person = h.seed['people'][0]
    _, validation = rota(h, [(d, None, [person], AM) for d in days])
    assert codes(validation, policy['id']) == expected
    assert validation['valid']  # advisory: never blocking
    assert all(f['severity'] == 'warning' and f['person_id'] == person for f in validation['findings'] if f['rule_id'] == policy['id'])


def test_types_and_people_patterns(h):
    early, late = duty(h, 'Early'), duty(h, 'Late')
    a, b = h.seed['people'][:2]
    cases = [
        ({'kind': 'weekend_shape', 'shape': 'identical_types'}, [(9, early, [a], AM), (10, late, [a], PM)], 'WEEKEND_TYPES_DIFFER'),
        ({'kind': 'shift_succession', 'mode': 'forward', 'type_order': [early, late]}, [(4, late, [a], PM), (5, early, [a], AM)], 'SHIFT_ORDER'),
        ({'kind': 'shift_succession', 'mode': 'avoid', 'from_type_ids': [late], 'to_type_ids': [early], 'within_days': 2},
         [(4, late, [a], PM), (6, early, [a], AM)], 'SHIFT_SUCCESSION'),
        ({'kind': 'shift_succession', 'mode': 'rest_after_series', 'from_type_ids': [late], 'series_length': 2, 'rest_days': 2},
         [(4, late, [a], PM), (5, late, [a], PM), (7, early, [a], AM)], 'REST_AFTER_SERIES'),
        ({'kind': 'together_apart', 'mode': 'apart', 'person_ids': [a, b]}, [(4, early, [a], AM), (4, late, [b], PM)], 'WORK_APART'),
        ({'kind': 'together_apart', 'mode': 'together', 'person_ids': [a, b]}, [(4, early, [a], AM)], 'WORK_TOGETHER'),
    ]
    for rule, plan, code in cases:
        policy = h.policy(rule, severity='advisory')
        w, validation = rota(h, plan)
        assert codes(validation, policy['id']) == [code], (rule, validation['findings'])
        # The same breach costs preference points, not rule points, and says why.
        score = schedule_score(h.snapshot(w), validation)
        assert score['components']['preferences']['lost'] > 0 and score['components']['rules']['value'] in (None, 1.0)
        assert any(policy['name'] in line['text'] for line in score['why'])
        h.command('coordinator', 'workspace.archive', {'reason': 'Next case'}, w['id'], w['revision'])
        h.command('approver', 'policy.retire', {'reason': 'Next case'}, policy['id'], policy['revision'])
    # A compliant pattern is quiet.
    policy = h.policy({'kind': 'shift_succession', 'mode': 'forward', 'type_order': [early, late]}, severity='advisory')
    _, validation = rota(h, [(4, early, [a], AM), (5, late, [a], PM)])
    assert codes(validation, policy['id']) == []


def test_shape_rules_are_advisory_only(h):
    response = h.command('coordinator', 'policy.create', {'name': 'Runs', 'owner': 'Owner', 'source': 'Department decision', 'starts_on': '2020-01-01',
        'definition': {'rule': {'kind': 'run_length', 'minimum': 2}, 'scope': {}, 'severity': 'mandatory'}}, status=422)
    assert 'advisory' in str(response)


# Solver: tiny rotas where the configured score weights make avoiding the pattern worthwhile.
def solve(h, plan, people=2, wishes=()):
    w, _ = rota(h, plan)
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in h.seed['people'][:people]}
    data['wishes'] = list(wishes)
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result.get('status')
    worked = {}
    for addition in result['additions']:
        shift = next(s for s in data['shifts'] if s['id'] == addition['shift_id'])
        worked.setdefault(addition['participant']['person_id'], []).append((int(shift['start'][8:10]), shift['assignment_type_id']))
    return worked, result


def advisory(h, rule):
    return h.policy(rule, severity='advisory')


def empty_plan(days, typ=None, slot=AM):
    return [(d, typ, [], slot) for d in days]


@pytest.mark.parametrize('rule,days', [
    ({'kind': 'run_length', 'minimum': 2}, [4, 5, 6, 7]),
    ({'kind': 'days_off_run', 'minimum': 2}, [4, 5, 6, 7, 8, 9]),
    ({'kind': 'weekend_shape', 'shape': 'complete'}, [9, 10]),
])
def test_solver_follows_day_patterns(h, rule, days):
    advisory(h, rule)
    worked, result = solve(h, empty_plan(days))
    # These candidates can satisfy the shape while retaining a good workload score.
    assert not [f for f in result['validation']['findings'] if (f.get('detail') or {}).get('kind') == rule['kind']], worked


def test_solver_follows_type_and_people_patterns(h):
    early, late = duty(h, 'Early'), duty(h, 'Late')
    a, b = h.seed['people'][:2]
    # Same duty all weekend.
    policy = advisory(h, {'kind': 'weekend_shape', 'shape': 'identical_types'})
    worked, _ = solve(h, empty_plan([9, 10], early) + empty_plan([9, 10], late))
    assert all(len({t for _, t in mine}) == 1 for mine in worked.values()), worked
    h.command('approver', 'policy.retire', {'reason': 'Next case'}, policy['id'], policy['revision'])
    # Forward rotation: never late then early.
    policy = advisory(h, {'kind': 'shift_succession', 'mode': 'forward', 'type_order': [early, late]})
    worked, result = solve(h, empty_plan([4, 5], early, AM) + empty_plan([4, 5], late, PM))
    assert not [f for f in result['validation']['findings'] if f['code'] == 'SHIFT_ORDER'], worked
    h.command('approver', 'policy.retire', {'reason': 'Next case'}, policy['id'], policy['revision'])
    # Together and apart: two days, two places a day, three candidates.
    for mode in ('together', 'apart'):
        policy = advisory(h, {'kind': 'together_apart', 'mode': mode, 'person_ids': [a, b]})
        worked, result = solve(h, empty_plan([4, 5], early, AM) + empty_plan([4, 5], late, PM), people=3)
        first, second = ({d for d, _ in worked.get(pid, [])} for pid in (a, b))
        # A named person need not work: together means equal worked days, apart means disjoint days.
        assert (first == second if mode == 'together' else first.isdisjoint(second)), (mode, worked)
        assert not codes(result['validation'], policy['id'])
        h.command('approver', 'policy.retire', {'reason': 'Next case'}, policy['id'], policy['revision'])


def test_solver_rests_after_a_series(h):
    late = duty(h, 'Late series')
    advisory(h, {'kind': 'shift_succession', 'mode': 'rest_after_series', 'from_type_ids': [late], 'series_length': 1, 'rest_days': 1})
    early = duty(h, 'Early after')
    worked, result = solve(h, empty_plan([4], late, PM) + empty_plan([5], early, AM))
    assert not [f for f in result['validation']['findings'] if f['code'] == 'REST_AFTER_SERIES'], worked
    assert len(worked) == 2  # the late person does not take the next morning
