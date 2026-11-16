"""Executed leadership witnesses: counts bind separately from continuity."""
import copy

from packages.domain.validation import evaluate, finding_observations
from tests.golden_checks import disabled, feasible, place, shift, solve


def check_leadership(data, ids, spec):
    expected = spec['expect']
    sid, rid = ids['lead_shift'], ids['lead']
    at = copy.deepcopy(data)
    place(at, sid, [ids['p0'], ids['p1']])
    duty = shift(at, sid)
    duty['participants'][0]['team_lead'] = True
    assert expected['boundary_min']['minimum'] == expected['boundary_max']['maximum'] == 1
    assert evaluate(at)['valid']

    def lead_errors(snapshot):
        return [f for f in finding_observations(evaluate(snapshot))
                if f['severity'] == 'error' and f.get('rule_id') == rid]

    missing = copy.deepcopy(at)
    shift(missing, sid)['participants'][0]['team_lead'] = False
    minimum = lead_errors(missing)
    assert minimum and {f['code'] for f in minimum} == {expected['boundary_min']['missing_code']}
    excess = copy.deepcopy(at)
    shift(excess, sid)['participants'][1]['team_lead'] = True
    maximum = lead_errors(excess)
    assert maximum and {f['code'] for f in maximum} == {expected['boundary_max']['excess_code']}

    handover = copy.deepcopy(excess)
    duty = shift(handover, sid)
    for p, (start, end) in zip(duty['participants'], expected['continuity']['gaps']):
        p['segments'] = [{'start': duty['start'], 'end': start}, {'start': end, 'end': duty['end']}]
    # The same cap in both witnesses isolates continuity from simultaneous headcount.
    policy = next(p for p in handover['policies'] if p['id'] == rid)
    for line in policy['definition']['rule']['lines']:
        line['numbers']['max']['value'] = expected['continuity']['maximum']
    whole = lead_errors(handover)
    assert {f['person_id'] for f in whole if f['code'] == expected['continuity']['whole_duty_code']} == {ids['p0'], ids['p1']}
    continuous = copy.deepcopy(handover)
    policy = next(p for p in continuous['policies'] if p['id'] == rid)
    for line in policy['definition']['rule']['lines']:
        line['settings']['team_lead_requirement']['continuity'] = 'continuous'
    assert evaluate(continuous)['valid'] is expected['continuity']['continuous_valid']
    assert evaluate(disabled(missing, rid))['valid'] is expected['counterfactual']['without_rule_valid']
    for seed in expected['seed_robustness']['seeds']:
        draft = feasible(data, solve(data, seed))
        flags = [p for p in shift(draft, sid)['participants'] if p.get('team_lead')]
        assert len(flags) == 1 and flags[0]['person_id'] in {ids['p0'], ids['p1']}
    return {'assertions': {'boundary_min', 'boundary_max', 'continuity', 'counterfactual', 'seed_robustness'},
            'bindings': {'lead': len(minimum) + len(maximum) + len(whole)}}
