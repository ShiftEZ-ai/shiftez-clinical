"""Independent witnesses for rolling Fair share and session-local supervision."""
from collections import Counter
from copy import deepcopy

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import SpreadEvenly
from packages.domain.validation import evaluate, finding_observations
from tests.golden_checks import disabled, feasible, place, shift, solve
from tests.golden_safety import policy


def check_fair_share(data, ids, spec):
    expected = spec['expect']
    rule_id = ids['share']
    people = {ids[name]: name for name in expected['people']}

    def comparison(snapshot):
        goals = [row for row in evaluate(snapshot).get('goals', []) if row['policy_id'] == rule_id]
        return sorted((row['period_start'], row['period_end'],
                       sorted((people[p['person_id']], p['planned'], p['expected']) for p in row['people']))
                      for row in goals)

    def assert_comparison(snapshot, key):
        rows = expected[key]
        actual = comparison(snapshot)
        assert actual == sorted((r['start'], r['end'], [tuple(person) for person in r['rows']]) for r in rows)
        return len(actual)

    def variant(**updates):
        result = deepcopy(data)
        policy(result, rule_id)['definition']['rule']['settings']['balance_goal'].update(updates)
        return result

    pooled_count = assert_comparison(data, 'pooled')
    separate = variant(comparison='within_group')
    separate_count = assert_comparison(separate, 'within_group')
    assert_comparison(variant(period_months=None, period_days=32), 'day_window')

    for seed in expected['seed_robustness']['seeds']:
        for snapshot, key in ((data, 'pooled'), (separate, 'within_group')):
            candidate = feasible(snapshot, solve(snapshot, seed))
            [assignment] = shift(candidate, ids['choice'])['participants']
            assert people[assignment['person_id']] == expected['solver_choice'][key]
    return {'assertions': {'pooled', 'within_group', 'day_window', 'solver_choice', 'seed_robustness'},
            'bindings': {'share': separate_count - pooled_count}}


def check_spread(data, ids, spec):
    expected = spec['expect']
    observed = expected['session_loads']
    supervisors = [ids[name] for name in observed['supervisors']]
    trainees = [ids[name] for name in observed['trainees']]
    duties = [ids[name] for name in observed['duties']]
    rule_id = ids['spread']
    rule = policy(data, rule_id)['definition']['rule']
    assert (rule['holder'], rule['within'], rule['mode'], rule['maximum_gap']) == (
        observed['holder'], observed['within'], observed['mode'], observed['maximum_gap'])

    def loads(snapshot, duty_id):
        participants = shift(snapshot, duty_id)['participants']
        assert {p['person_id'] for p in participants if p['participation'] == 'supervisor'} == set(supervisors)
        members = [p for p in participants if p['participation'] == 'trainee']
        assert {p['person_id'] for p in members} == set(trainees)
        assigned = Counter(p['supervisor_person_id'] for p in members)
        assert assigned.keys() <= set(supervisors)
        return sorted(assigned[pid] for pid in supervisors)

    broken = deepcopy(data)
    for duty_id, supervisor in zip(duties, supervisors, strict=True):
        place(broken, duty_id, [{'person_id': pid, 'participation': 'supervisor'} for pid in supervisors] +
              [{'person_id': pid, 'participation': 'trainee', 'supervisor_person_id': supervisor} for pid in trainees])
        assert loads(broken, duty_id) == observed['skewed']
    checked = evaluate(broken)
    findings = [f for f in finding_observations(checked) if f.get('rule_id') == rule_id and f['severity'] == 'error']
    assert {(f['shift_id'], f['code'], f['detail']['reason']) for f in findings} == {
        (duty_id, observed['code'], 'imbalance') for duty_id in duties}
    assert evaluate(disabled(broken, rule_id))['valid']

    allowed_gap = expected['permitted_gap']
    with pytest.raises(ValidationError, match='Levelled spread uses a maximum gap'):
        SpreadEvenly.model_validate({**rule, 'maximum_gap': allowed_gap})
    permissive = deepcopy(broken)
    updated = SpreadEvenly.model_validate({**rule, 'mode': 'maximum_gap', 'maximum_gap': allowed_gap})
    policy(permissive, rule_id)['definition']['rule'] = updated.model_dump(mode='json')
    assert evaluate(permissive)['valid']

    for seed in expected['seed_robustness']['seeds']:
        draft = feasible(data, solve(data, seed))
        assert all(loads(draft, duty_id) == observed['levelled'] for duty_id in duties)
    return {'assertions': {'session_loads', 'permitted_gap', 'seed_robustness'},
            'bindings': {'spread': len({f['shift_id'] for f in findings})}}
