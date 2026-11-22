"""Hand-written preference arithmetic and a 27-rota independent oracle."""
from copy import deepcopy
from fractions import Fraction
from itertools import product

from packages.domain.score import schedule_score
from packages.domain.validation import evaluate, finding_observations
from tests.scheduler_departments import _id, apply


ASSERTIONS = {'pressure', 'counterfactual', 'boundary', 'validator', 'seed_robustness',
              'component_maths', 'ablation', 'soft_hard', 'soft_priority', 'oracle'}


def _options(data, weight, stability):
    data['generation_options'] = {'mode': 'rebalance', 'shortlist_width': 0,
        'scoring': {'weights': {'coverage': 0, 'rules': 0, 'fairness': 0,
                                'preferences': weight, 'stability': stability}}}
    return data


def _assignment(data, ids):
    shifts = {s['id']: s for s in data['shifts']}
    result = []
    for key in ('s0', 's1', 's2'):
        participants = shifts[ids[key]]['participants']
        assert len(participants) == 1
        result.append(participants[0]['person_id'])
    return tuple(result)


def _witness(data, ids, assignment):
    witness = deepcopy(data)
    shifts = {s['id']: s for s in witness['shifts']}
    for key, pid in zip(('s0', 's1', 's2'), assignment):
        shift = shifts[ids[key]]
        template = shift['participants'][0]
        shift['participants'] = [{**template, 'id': _id('golden-preference', key, pid),
                                  'person_id': pid, 'fixed': False}]
    return witness


def _score(data, baseline):
    return schedule_score(data, evaluate(data), options=data['generation_options'], baseline=baseline)


def _preference(assignment, people, priorities):
    # Three hand-reviewed people: A/B have an equally weighted zero-loss rule and priority item; C only misses priority.
    a, b, c = people
    ceiling = max(priorities)
    sat = {a: Fraction(1, 2) + Fraction(priorities[0], 2 * ceiling),
           b: Fraction(1, 2) + Fraction(priorities[1], 2 * ceiling), c: Fraction(0)}
    values = [sat[pid] for pid in set(assignment)]
    return Fraction(7, 10) * sum(values) / len(values) + Fraction(3, 10) * min(values)


def _oracle(people, weight, priorities, stability, hard=False):
    possibilities = []
    for choice in product(people, repeat=3):
        if hard and choice[0] == people[0]:
            continue  # Sunday protection, independently specified rather than asking the validator.
        stable = Fraction(choice.count(people[1]), 3)
        value = (weight * _preference(choice, people, priorities) + stability * stable) / (weight + stability)
        possibilities.append((value, choice))
    best = max(value for value, _ in possibilities)
    return {choice for value, choice in possibilities if value == best}


def check_preference(data, ids, spec):
    from tests.golden_checks import solve

    people = tuple(ids[f'p{i}'] for i in range(3))
    a, b, c = people
    baseline = {(sid, b): None for sid in (ids['s0'], ids['s1'], ids['s2'])}
    expected = spec['expect']
    ablation, maths, priority = expected['ablation'], expected['component_maths'], expected['soft_priority']
    off_weight, on_weight, stable = ablation['off'], ablation['on'], ablation['fixed_stability_weight']
    priorities, swapped_priorities = priority['priorities'], priority['swapped_priorities']
    assert data['generation_options']['scoring']['weights']['preferences'] == on_weight
    assert data['generation_options']['scoring']['weights']['stability'] == stable
    hard = _options(deepcopy(data), on_weight, stable)
    safety = next(p for p in hard['policies'] if p['id'] == ids['safety'])
    assert safety['definition']['severity'] == 'mandatory'
    # Safety kinds are always mandatory. Omit the rule entirely in the preference-only counterfactual.
    data = {**hard, 'policies': [p for p in hard['policies'] if p['id'] != safety['id']]}
    assert [next(p for p in data['policies'] if p['id'] == ids[key])['definition']['rule']['priority']
            for key in ('pref', 'prefer_backup')] == priorities

    oracle = expected['oracle']
    assert len(people) == oracle['people']
    assert _oracle(people, off_weight, priorities, stable) == {tuple(ids[key] for key in oracle['weight_zero_optimum'])}
    assert _oracle(people, on_weight, priorities, stable) == {tuple(ids[key] for key in oracle['base_optimum'])}
    assert _oracle(people, on_weight, priorities, stable, hard=True) == {tuple(ids[key] for key in choice) for choice in oracle['mandatory_optima']}

    for name in ('at_limit', 'one_over'):
        boundary = expected['boundary'][name]
        choice = tuple(ids[key] for key in boundary['assignment'])
        witness = _witness(data, ids, choice)
        assert evaluate(witness)['valid']
        actual = _score(witness, baseline)['components']['preferences']['value']
        assert actual == boundary['preferences'], (name, actual)
        assert actual == round(float(_preference(choice, people, priorities)), 4)
        assert actual == maths['all_preferred' if name == 'at_limit' else 'one_miss']
        if name == 'one_over':
            assert _preference(choice, people, priorities) == Fraction(maths['one_miss_exact'])
    all_backup = _witness(data, ids, (b, b, b))
    assert _score(all_backup, baseline)['components']['preferences']['value'] == maths['all_backup']
    all_alternative = _witness(data, ids, (c, c, c))
    assert _score(all_alternative, baseline)['components']['preferences']['value'] == maths['all_alternative']

    broken = _witness(hard, ids, (a, a, a))
    witness = expected['validator']
    assert any(f['code'] == witness['code'] and f['person_id'] == ids[witness['person']]
               and f['shift_id'] == ids[witness['session']] and f['rule_id'] == ids[witness['rule']]
               for f in finding_observations(evaluate(broken)))

    # Every one of the 27 explicit choices has independently known feasibility and preference arithmetic.
    possibilities = list(product(people, repeat=oracle['sessions']))
    assert len(possibilities) == oracle['enumerated_rotas']
    for choice in possibilities:
        plain = _witness(data, ids, choice)
        assert evaluate(plain)['valid']
        assert _score(plain, baseline)['components']['preferences']['value'] == round(float(_preference(choice, people, priorities)), 4)
        assert evaluate(_witness(hard, ids, choice))['valid'] == (choice[0] != a)

    bound = {key: set() for key in expected['binding_rules']}
    sessions = [ids[key] for key in ('s0', 's1', 's2')]
    assert len(sessions) == oracle['sessions']
    for seed in expected['seed_robustness']['seeds']:
        off = _options(deepcopy(data), off_weight, stable)
        result_off, result_on = solve(off, seed), solve(data, seed)
        for result in (result_off, result_on):
            assert result['status'] == 'candidate_feasible' and result['optimality_proven'], result
        off_choice = _assignment(apply(off, result_off), ids)
        on_choice = _assignment(apply(data, result_on), ids)
        assert off_choice in _oracle(people, off_weight, priorities, stable)
        assert on_choice in _oracle(people, on_weight, priorities, stable)
        bound['pref'].update(sid for sid, before, after in zip(sessions, off_choice, on_choice) if before != after)
        moved = on_choice.count(a) - off_choice.count(a)
        assert moved >= ablation['minimum_shift'], 'scenario not sensitive'
        assert _score(apply(data, result_on), baseline)['components']['preferences']['value'] > _score(apply(data, result_off), baseline)['components']['preferences']['value']

        result_hard = solve(hard, seed)
        assert result_hard['status'] == 'candidate_feasible' and result_hard['optimality_proven'], result_hard
        legal = apply(hard, result_hard)
        hard_choice = _assignment(legal, ids)
        assert hard_choice in _oracle(people, on_weight, priorities, stable, hard=True)
        assert (hard_choice[0] != a) == expected['soft_hard']['mandatory_sunday_protection_wins']
        bound['safety'].update(sid for sid, before, after in zip(sessions, on_choice, hard_choice) if before != after and sid == ids['s0'])
        assert evaluate(legal)['valid']
        penalty = _score(legal, baseline)
        assert penalty['components']['preferences']['value'] == expected['soft_hard']['preference_component']
        assert penalty['components']['preferences']['lost'] > expected['soft_hard']['visible_penalty_above']
        assert any(row['component'] == 'preferences' and row['points'] > 0 for row in penalty['contributions'])

        swapped = deepcopy(data)
        for key, value in zip(('pref', 'prefer_backup'), swapped_priorities):
            next(p for p in swapped['policies'] if p['id'] == ids[key])['definition']['rule']['priority'] = value
        result_swapped = solve(swapped, seed)
        assert result_swapped['status'] == 'candidate_feasible' and result_swapped['optimality_proven'], result_swapped
        swapped_choice = _assignment(apply(swapped, result_swapped), ids)
        assert swapped_choice in _oracle(people, on_weight, swapped_priorities, stable)
        bound['prefer_backup'].update(sid for sid, before, after in zip(sessions, on_choice, swapped_choice) if before != after)
        assert swapped_choice.count(b) - on_choice.count(b) >= priority['minimum_shift'], 'scenario not sensitive'

    assert all(bound.values()), 'scenario not sensitive'
    return {'assertions': ASSERTIONS, 'bindings': {key: len(moments) for key, moments in bound.items()}}
