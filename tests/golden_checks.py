"""Small acceptance witnesses using the canonical solver, validator and score."""
import copy
from datetime import datetime, timedelta
from itertools import combinations
from random import Random
from unittest.mock import patch

from ortools.sat.python import cp_model

from packages.contracts.schemas import ParticipantInput
from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate, finding_observations
from tests.scheduler_departments import apply, _id


def solve(data, seed):
    """Vary both input order and the actual CP-SAT seed, with one worker."""
    data = copy.deepcopy(data)
    rng = Random(seed)
    for key in ('shifts', 'policies', 'memberships', 'eligibilities', 'grants', 'availability'):
        rng.shuffle(data[key])
    items = list(data['people'].items())
    rng.shuffle(items)
    data['people'] = dict(items)
    constructor = cp_model.CpSolver

    def seeded_solver():
        solver = constructor()
        solver.parameters.random_seed = seed
        return solver

    # Test-only injection: the production API does not expose a random-seed setting.
    with patch('packages.domain.cp_sat.cp_model.CpSolver', seeded_solver):
        return generate(data, 5, workers=1)


def shift(data, id_):
    return next(row for row in data['shifts'] if row['id'] == id_)


def place(data, sid, people):
    """Hand-built rota, independent of any solver result."""
    row = shift(data, sid)
    row['participants'] = []
    for person in people:
        fields = {'person_id': person} if isinstance(person, str) else person
        participant = ParticipantInput.model_validate({'start': row['start'], 'end': row['end'], **fields})
        row['participants'].append(dict(id=_id('golden-witness', sid, participant.person_id),
            shift_id=sid, **participant.model_dump(mode='json')))


def finding(data, code, sid, pid, rule):
    return [row for row in evaluate(data)['findings'] if
            (row['code'], row['shift_id'], row['person_id'], row.get('rule_id')) == (code, sid, pid, rule)]


def expected_findings(data, ids, expected, rule):
    return finding(data, expected['code'], ids[expected['session']],
                   ids[expected['person']] if expected['person'] else None, ids[rule])


def disabled(data, rid):
    result = copy.deepcopy(data)
    for row in result['policies']:
        if row['id'] == rid:
            row['active'] = False
    return result


def feasible(data, result):
    assert result['status'] == 'candidate_feasible', result
    draft = apply(data, result)
    assert evaluate(draft)['valid'], evaluate(draft)
    return draft


def retime(data, sid, start):
    row = shift(data, sid)
    duration = datetime.fromisoformat(row['end']) - datetime.fromisoformat(row['start'])
    row['start'] = start
    row['end'] = (datetime.fromisoformat(start) + duration).isoformat()
    for participant in row['participants']:
        participant.update(start=row['start'], end=row['end'])


def check_rest(data, ids, spec):
    sid, pid, rid = ids['early'], ids['p0'], ids['rest']
    expected = spec['expect']
    broken = copy.deepcopy(data)
    place(broken, sid, [pid])
    retime(broken, sid, expected['boundary']['one_over_start'])
    assert expected_findings(broken, ids, expected['validator'], 'rest')
    at = copy.deepcopy(broken)
    retime(at, sid, expected['boundary']['at_start'])
    assert evaluate(at)['valid'], evaluate(at)
    assert not finding(at, 'MIN_REST', sid, pid, rid)
    assert (datetime.fromisoformat(shift(at, sid)['start']) -
            datetime.fromisoformat(shift(at, ids['night'])['end'])) == timedelta(hours=expected['boundary']['hours'])
    assert datetime.fromisoformat(shift(at, sid)['start']) - datetime.fromisoformat(shift(broken, sid)['start']) == timedelta(minutes=1)
    bound = set()
    for seed in expected['seed_robustness']['seeds']:
        on = feasible(data, solve(data, seed))
        assert pid not in {p['person_id'] for p in shift(on, sid)['participants']}
        off_data = disabled(data, rid)
        off = feasible(off_data, solve(off_data, seed))
        # Re-enable only for the independent validator, after obtaining the off-rule candidate.
        off['policies'] = copy.deepcopy(data['policies'])
        hits = expected_findings(off, ids, expected['counterfactual'], 'rest')
        assert hits, 'scenario not sensitive'
        bound.update((f['person_id'], f['shift_id']) for f in hits)
        assert shift(on, ids['night'])['participants'] == shift(data, ids['night'])['participants']
    return {'assertions': {'counterfactual', 'boundary', 'validator', 'seed_robustness'},
            'bindings': {'rest': len(bound)}}


def check_cover(data, ids, spec):
    sid, rid = ids['cover_shift'], ids['cover']
    at = copy.deepcopy(data)
    place(at, sid, [ids[key] for key in spec['expect']['boundary']['at_people']])
    assert len(shift(at, sid)['participants']) == spec['expect']['boundary']['minimum']
    assert evaluate(at)['valid'], evaluate(at)
    broken = copy.deepcopy(data)
    place(broken, sid, [ids[key] for key in spec['expect']['boundary']['one_over_people']])
    assert len(shift(at, sid)['participants']) - len(shift(broken, sid)['participants']) == 1
    assert expected_findings(broken, ids, spec['expect']['validator'], 'cover')
    bound = set()
    for seed in spec['expect']['seed_robustness']['seeds']:
        on = feasible(data, solve(data, seed))
        people = {p['person_id'] for p in shift(on, sid)['participants']}
        assert len(people) >= spec['expect']['boundary']['minimum']
        assert people <= {ids['p0'], ids['p1'], ids['p2']}
        off_data = disabled(data, rid)
        off = feasible(off_data, solve(off_data, seed))
        off['policies'] = copy.deepcopy(data['policies'])
        hits = expected_findings(off, ids, spec['expect']['counterfactual'], 'cover')
        assert hits, 'scenario not sensitive'
        bound.update((f['shift_id'], f['start'], f['end']) for f in hits)
    return {'assertions': {'counterfactual', 'boundary', 'validator', 'seed_robustness'},
            'bindings': {'cover': len(bound)}}


def check_interaction(data, ids, spec):
    sid, night = ids['early'], ids['night']
    data = copy.deepcopy(data)
    broken = copy.deepcopy(data)
    place(broken, sid, [ids['p0'], ids['p2']])
    expected = spec['expect']
    assert expected_findings(broken, ids, expected['validator'], 'rest')
    shortage = copy.deepcopy(data)
    place(shortage, sid, [ids['p2']])
    assert finding(shortage, expected['validator']['cover_code'],
                   ids[expected['validator']['session']], expected['validator']['cover_person'], ids['cover'])
    at = copy.deepcopy(broken)
    retime(at, sid, spec['expect']['boundary']['at_start'])
    assert evaluate(at)['valid'], evaluate(at)
    assert len(shift(at, sid)['participants']) == expected['boundary']['cover_at']
    assert len(shift(at, sid)['participants']) - len(shift(shortage, sid)['participants']) == expected['boundary']['cover_one_over_shortage']
    assert (datetime.fromisoformat(shift(at, sid)['start']) -
            datetime.fromisoformat(shift(at, night)['end'])) == timedelta(hours=expected['boundary']['hours'])
    over = copy.deepcopy(at)
    retime(over, sid, spec['expect']['boundary']['one_over_start'])
    assert finding(over, 'MIN_REST', sid, ids['p0'], ids['rest'])
    oracle = expected['oracle']
    audience = [ids[key] for key in oracle['people']]
    all_staffings = {frozenset(people) for size in range(len(audience) + 1)
                     for people in combinations(audience, size)}
    declared = [frozenset(ids[key] for key in case['people']) for case in oracle['cases']]
    assert len(declared) == len(all_staffings) == oracle['enumerated_staffings']
    assert set(declared) == all_staffings
    assert shift(data, ids[oracle['locked_session']])['locked']
    for case in oracle['cases']:
        witness = copy.deepcopy(data)
        place(witness, ids[oracle['session']], [ids[key] for key in case['people']])
        checked = evaluate(witness)
        assert not checked['valid'], (case, checked)
        actual = {(f['code'], f['person_id'], f['shift_id'], f['rule_id'])
                  for f in finding_observations(checked) if f.get('rule_id') in {ids['rest'], ids['cover']}}
        wanted = {(f['code'], ids[f['person']] if f['person'] else None,
                   ids[f['session']], ids[f['rule']]) for f in case['findings']}
        assert actual == wanted, (case, actual, wanted)
        assert shift(witness, ids[oracle['locked_session']]) == shift(data, ids[oracle['locked_session']])
    bound = {'rest': set(), 'cover': set(), 'lock': set()}
    for seed in spec['expect']['seed_robustness']['seeds']:
        frozen = copy.deepcopy(shift(data, night))
        on = solve(data, seed)
        assert on['status'] == expected['counterfactual']['rule_on'], on
        explanation = next(row for row in on['diagnostics'] if row['code'] == expected['explanation']['code'])
        named = {row['rule_id'] for row in explanation['requirements'] if row['kind'] == 'policy'}
        assert {ids[key] for key in expected['explanation']['rules']} <= named
        assert explanation['proven'] and not explanation['implementation_error']
        assert not on['additions'] and not on['removals'], on
        assert shift(data, night) == frozen
        for key in ('rest', 'cover'):
            off_data = disabled(data, ids[key])
            result = solve(off_data, seed)
            assert result['status'] == expected['counterfactual'][key + '_off'], result
            off = feasible(off_data, result)
            assert shift(off, night) == frozen
            off['policies'] = copy.deepcopy(data['policies'])
            hits = [f for f in evaluate(off)['findings'] if f.get('rule_id') == ids[key]]
            assert hits, 'scenario not sensitive'
            bound[key].update((f['shift_id'], f['person_id']) for f in hits)
        unlocked = copy.deepcopy(data)
        shift(unlocked, night)['locked'] = False
        result = solve(unlocked, seed)
        assert result['status'] == expected['counterfactual']['lock_off'], result
        candidate = feasible(unlocked, result)
        before = {p['id'] for p in frozen['participants']}
        after = {p['id'] for p in shift(candidate, night)['participants']}
        assert before - after, 'scenario not sensitive'
        bound['lock'].update(before - after)
    return {'assertions': {'counterfactual', 'boundary', 'validator', 'seed_robustness', 'oracle', 'explanation'},
            'bindings': {key: len(value) for key, value in bound.items()}}


def check_allocation_cover(data, ids, spec):
    from packages.domain.score import schedule_score

    expected = spec['expect']
    overfilled = copy.deepcopy(data)
    for duty in expected['staffing']:
        place(overfilled, ids[duty], [ids['p0'], ids['p1']])
    original_score = schedule_score(data, evaluate(data))['components']['preferences']['value']
    overfilled_score = schedule_score(overfilled, evaluate(overfilled))['components']['preferences']['value']
    assert (overfilled_score > original_score) == expected['pressure']['overfilled_preference_score_is_higher']

    supervision = expected['staffing_with_supervision']
    # Let the solver move the supervisor while the trainee's required pairing stays fixed.
    rebalance = {**data, 'generation_options': {**data['generation_options'], 'mode': 'rebalance'}}
    seeds = expected['seed_robustness']['seeds']
    for seed in seeds:
        result = solve(data, seed)
        assert result['solver_status'] == 'OPTIMAL', result
        draft = feasible(data, result)
        for duty, people in expected['staffing'].items():
            assert [p['person_id'] for p in shift(draft, ids[duty])['participants']] == [ids[p] for p in people]
        draft = feasible(rebalance, solve(rebalance, seed))
        for duty, people in expected['staffing'].items():
            assert len(shift(draft, ids[duty])['participants']) == len(people)
        supervised = {p['person_id']: p for p in shift(draft, ids[supervision['duty']])['participants']}
        trainee, supervisor = ids[supervision['trainee']], ids[supervision['supervisor']]
        assert supervised.keys() == {trainee, supervisor}
        assert supervised[trainee]['supervisor_person_id'] == supervisor
    return {'assertions': {'staffing', 'pressure', 'seed_robustness', 'staffing_with_supervision'},
            'bindings': {'balance': len(expected['staffing'])}}


def check_scenario(data, ids, spec):
    if spec['id'] == 'A-FAIR-01':
        from tests.golden_atlas import check_fair_share
        return check_fair_share(data, ids, spec)
    if spec['id'] == 'A-SPREAD-01':
        from tests.golden_atlas import check_spread
        return check_spread(data, ids, spec)
    if 'safety' in spec['expect']:
        from tests.golden_safety import check_safety
        return check_safety(data, ids, spec)
    if spec['id'] == 'A-LEAD-01':
        from tests.golden_leadership import check_leadership
        return check_leadership(data, ids, spec)
    if spec['id'] == 'A-PREF-01':
        from tests.golden_preference import check_preference
        return check_preference(data, ids, spec)
    return {'A-REST-01': check_rest, 'A-COVER-01': check_cover, 'A-ALLOCATION-COVER-01': check_allocation_cover,
            'I-LOCK-REST-COVER-01': check_interaction}[spec['id']](data, ids, spec)
