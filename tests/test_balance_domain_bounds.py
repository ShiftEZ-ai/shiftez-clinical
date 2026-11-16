"""Compile-only bounds contracts; no scheduler search or timing qualification.

The public fixtures below also feed the external, pinned-source whole-model
comparison. These permanent regressions need neither Git history nor that file.
"""
import copy
from itertools import product
from unittest.mock import patch

import pytest
from ortools.sat.python import cp_model, cp_model_helper

from packages.domain import cp_sat
from packages.domain.cp_sat_score import SCORE_SCALE, ScoreModel
from packages.domain.score_normalization import workload_limit, workload_normalization


BOUND_FAMILIES = {'score_workload'}
SCENARIOS = ('zero', 'single', 'sparse', 'named', 'fixed', 'kept', 'locked',
             'published', 'scoped', 'protected_overlap', 'segments', 'overtime', 'shared')


@pytest.fixture(autouse=True)
def no_search(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Balance-domain tests must return before any search')

    monkeypatch.setattr(cp_model.CpSolver, 'solve', forbidden)
    monkeypatch.setattr(cp_model.CpSolver, 'Solve', forbidden)


def _snapshot(h, scenario, balance_measure='reserved_hours'):
    """Real API snapshots, with explicit raw shapes for protected/scoped inputs."""
    assert scenario in SCENARIOS
    definition = {'requirements': [{'minimum': 0, 'target': 1, 'maximum': 1}]}
    if scenario == 'shared':
        definition.update(allow_shared_services=True, shared_duty_reason='Synthetic bounds regression')
    typ = h.configure('assignment_types', {'name': 'Balance domain bounds',
        'starts_on': '2020-01-01', 'definition': definition})['record']['id']
    if scenario == 'named':
        h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'rota',
                  'aim': 'equal_share'}, severity='advisory')
    if scenario == 'overtime':
        h.policy({'kind': 'allocation_overtime'}, severity='advisory')
    workspace = h.workspace()
    if scenario != 'zero':
        first = h.seed['people'][0]
        assigned = scenario in {'fixed', 'kept', 'locked', 'published', 'scoped',
                               'protected_overlap', 'segments', 'overtime'}
        for day, end in ((4, 12), (6, 16)):
            if scenario == 'protected_overlap':
                day, end = 4, 12
            start_at = f'2026-10-{day:02}T08:00:00+03:00'
            end_at = f'2026-10-{day:02}T{end:02}:00:00+03:00'
            participant = {'person_id': first, 'start': start_at, 'end': end_at,
                           'fixed': scenario in {'fixed', 'protected_overlap'},
                           'overtime': scenario == 'overtime'}
            if scenario == 'segments':
                participant['segments'] = [
                    {'start': start_at, 'end': f'2026-10-{day:02}T10:00:00+03:00'},
                    {'start': f'2026-10-{day:02}T11:00:00+03:00', 'end': end_at}]
            workspace = h.shift(workspace, typ=typ, start=start_at, end=end_at,
                                people=[participant] if assigned else [])['workspace']
        if scenario == 'scoped':
            workspace = h.shift(workspace, typ=typ, start='2026-10-07T08:00:00+03:00',
                end='2026-10-07T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    count = 0 if scenario == 'zero' else 1 if scenario in {'single', 'protected_overlap'} else 2
    ids = h.seed['people'][:count]
    data['people'] = {pid: data['people'][pid] for pid in ids}
    data['generation_options'] = {'balance_measure': balance_measure,
        'mode': 'fill_gaps' if scenario in {'kept', 'segments'} else 'rebalance',
        'objective_order': ['preferences', 'balance', 'targets', 'changes'],
        'scoring': {'tolerance': {'targets': 7, 'preferences': 11, 'changes': 13, 'balance': 17}}}
    if scenario == 'locked':
        for shift in data['shifts']:
            shift['locked'] = True
    if scenario == 'published':
        # The compiler's supported publication-derived snapshot shape: the
        # publication reference makes every existing assignment uneditable.
        data['workspace']['base_publication_id'] = '00000000-0000-4000-8000-000000000999'
    if scenario == 'scoped':
        outside, kept, selected = sorted(data['shifts'], key=lambda row: row['start'])
        data['focus_ids'] = [kept['id'], selected['id']]
        data['generation_session_ids'] = [selected['id']]
        assert outside['id'] not in data['focus_ids']
    if scenario == 'shared':
        for shift in data['shifts']:
            shift['service_ids'] = list(h.seed['services'][:2])
    return data


def _flatten(expression):
    if isinstance(expression, int):
        return {'variable_indices': [], 'coefficients': [], 'offset': expression}
    flattened = cp_model_helper.FlatIntExpr(expression)
    assert flattened.ok
    return {'variable_indices': [var.index for var in flattened.vars],
            'coefficients': list(flattened.coeffs), 'offset': flattened.offset}


def _capture(generator, data, feasibility=False):
    """Capture complete ordered tiers before any search, without canonicalizing."""
    captured, workloads = [], []
    original_var = ScoreModel.var

    def record_var(score, expression, low=None, high=None, name='score'):
        result = original_var(score, expression, low, high, name)
        if name == 'score_workload':
            workloads.append(score.bounds(result))
        return result

    def capture(model, tiers=()):
        assert not captured
        assert model.validate() == ''
        rows = []
        for name, terms, tolerance in tiers:
            objective_model = model.clone()
            objective_model.minimize(sum(terms))
            rows.append({'name': name, 'tolerance': tolerance,
                'terms': [_flatten(term) for term in terms],
                'aggregate': _flatten(sum(terms)),
                'objective_proto': str(objective_model.proto.objective)})
        captured.append({'model': model.clone(), 'tiers': rows})

    arguments = {'feasibility_only': True, '_on_model': capture} if feasibility else {'_on_objectives': capture}
    with patch.object(ScoreModel, 'var', record_var):
        result = generator(copy.deepcopy(data), 5, 1, **arguments)
    assert result['status'] == 'model_ready', result
    assert result['optimality_proven'] is False
    assert len(captured) == 1
    return {**captured[0], 'result': result, 'workloads': workloads}


def _domains(model, name):
    return [tuple(var.domain) for var in model.proto.variables if var.name == name]


@pytest.mark.parametrize('balance_measure,load_upper', [('reserved_hours', 3), ('duties', 2)])
@pytest.mark.parametrize('scenario,count', [('zero', 0), ('single', 1), ('sparse', 2)])
def test_sparse_zero_and_single_load_bounds(h, scenario, count, balance_measure, load_upper):
    data = _snapshot(h, scenario, balance_measure)
    captured = _capture(cp_sat.generate, data)
    # A fixed eligible audience of fewer than two has no built-in sharing objective.
    assert captured['workloads'] == ([(0, load_upper)] * count if count >= 2 else [])
    assert [row['name'] for row in captured['tiers']] == ['coverage', 'weighted']
    assert [row['tolerance'] for row in captured['tiers']] == [0, 0]


@pytest.mark.parametrize('balance_measure,demand,limit', [
    ('reserved_hours', 12 * 3_600_000_000, 8 * 3_600_000_000), ('duties', 2, 1)])
def test_named_goals_keep_fixed_demand_normalization(h, monkeypatch, balance_measure, demand, limit):
    from packages.domain import cp_sat_goals
    from packages.domain.fairness import goal_groups
    data = _snapshot(h, 'named', balance_measure)
    groups = goal_groups(data)
    assert sorted((group[0]['definition']['rule']['metric'], group[0]['definition']['rule']['aim'])
                  for group in groups) == [('duties', 'equal_share'), ('workload', 'workload_spread')]
    assert all(len(group) == 1 for group in groups)
    original = cp_sat_goals.add_score_goals
    named = []

    def observed(model, *args, **kwargs):
        terms = original(model, *args, **kwargs)
        named.extend((weight, ScoreModel(model).bounds(loss)) for weight, loss in terms)
        return terms

    monkeypatch.setattr(cp_sat_goals, 'add_score_goals', observed)
    captured = _capture(cp_sat.generate, data)
    audience, budget = workload_normalization(data, balance_measure)
    assert len(audience) == 2 and budget == demand
    assert workload_limit(data, balance_measure, audience, budget) == limit
    # C2 exposes the former built-in workload as one goal alongside the named duty goal.
    # Both use unit weight and bounded normalized losses; the fixed demand budget is unchanged.
    assert len(named) == len(groups) == 2
    for weight, bounds in named:
        assert weight == 1
        assert 0 <= bounds[0] <= bounds[1] <= SCORE_SCALE
        assert bounds[1] > 0
    weighted = next(row for row in captured['tiers'] if row['name'] == 'weighted')
    assert weighted['terms'] and all(term['coefficients'] for term in weighted['terms'])


@pytest.mark.parametrize('scenario', ['fixed', 'kept', 'locked', 'published', 'scoped',
                                     'segments', 'overtime', 'shared'])
@pytest.mark.parametrize('balance_measure', ['reserved_hours', 'duties'])
def test_assignment_shapes_keep_whole_duty_loads(h, scenario, balance_measure):
    data = _snapshot(h, scenario, balance_measure)
    captured = _capture(cp_sat.generate, data)
    model = captured['model']
    upper = 3 if balance_measure == 'reserved_hours' else 2
    # Keep whole focused envelopes once, including booked breaks, regular/overtime
    # choices and shared clinics. Constants keep their actual load instead of a loose domain.
    expected = []
    for pid in sorted(data['people']):
        first = pid == h.seed['people'][0]
        if scenario == 'scoped':
            expected.append((upper - 1, upper) if first else (0, 1))
        elif scenario in {'locked', 'published'}:
            expected.append((upper, upper) if first else (0, 0))
        elif scenario in {'fixed', 'kept', 'segments'} and first:
            expected.append((upper, upper))
        else:
            expected.append((0, upper))
    assert captured['workloads'] == expected
    if scenario == 'scoped':
        assert 'loosened_constraints' in captured['result']
    if scenario == 'overtime':
        assert any(var.name == 'overtime' for var in model.proto.variables)
    if scenario == 'shared':
        assert all(len(shift['service_ids']) == 2 for shift in data['shifts'])
    if scenario == 'segments':
        assert all(shift['participants'][0]['segments'] for shift in data['shifts'])


def test_protected_overlapping_fixed_sum_keeps_no_worse_repair(h):
    data = _snapshot(h, 'protected_overlap')
    captured = _capture(cp_sat.generate, data)
    model = captured['model']
    assert captured['result']['loosened_constraints'] > 0
    assert captured['workloads'] == []  # One eligible person has no sharing score to repair.
    [one_index] = [index for index, var in enumerate(model.proto.variables) if tuple(var.domain) == (1, 1)]
    assert tuple(model.proto.variables[one_index].domain) == (1, 1)
    # Both overlapping assignments use this fixed-one literal. The existing hard
    # at-most-one conflict is replaced by an empty conjunction, preserving both.
    repaired = [constraint for constraint in model.proto.constraints
                if constraint.has_bool_and() and not constraint.bool_and.literals
                and not constraint.enforcement_literal]
    assert len(repaired) == 1
    assert not any(constraint.has_at_most_one()
                   and list(constraint.at_most_one.literals).count(one_index) > 1
                   for constraint in model.proto.constraints)


def _toy_repair(tightened):
    """Small actual keep_no_worse model with protected fixed sum above B."""
    model = cp_model.CpModel()
    choice = model.new_bool_var('optional_work')
    first = model.new_int_var(0, 1, 'reserved_workload')
    second = model.new_int_var(0, 1 if tightened else 4, 'reserved_workload')
    model.add(first == 2)
    model.add(second == choice)
    total = model.new_int_var(0, 2 if tightened else 5, 'total_workload')
    model.add(total == first + second)
    for load in (first, second):
        deviation = model.new_int_var(0, 1 if tightened else 8, 'workload_deviation')
        model.add_abs_equality(deviation, 2 * load - total)
    assert cp_sat.keep_no_worse(model, set()) == 1
    assert model.validate() == ''
    return model


def _toy_feasible(model):
    """Independent integer enumeration; deliberately never calls CP-SAT."""
    def linear(expression, vector):
        return expression.offset + sum(coefficient * vector[index]
            for index, coefficient in zip(expression.vars, expression.coeffs))

    accepted = set()
    # Materialize native repeated fields before indexing: their negative-index
    # behavior is not Python sequence indexing, and can silently empty ranges.
    domains = [tuple(var.domain) for var in model.proto.variables]
    assert all(len(domain) == 2 and domain[0] <= domain[1] for domain in domains)
    ranges = [range(low, high + 1) for low, high in domains]
    assert all(ranges)
    for vector in product(*ranges):
        for constraint in model.proto.constraints:
            assert not constraint.enforcement_literal
            if constraint.has_linear():
                value = sum(coefficient * vector[index]
                    for index, coefficient in zip(constraint.linear.vars, constraint.linear.coeffs))
                domain = list(constraint.linear.domain)
                valid = any(low <= value <= high for low, high in zip(domain[::2], domain[1::2]))
            else:
                assert constraint.has_lin_max()
                valid = linear(constraint.lin_max.target, vector) == max(
                    linear(expression, vector) for expression in constraint.lin_max.exprs)
            if not valid:
                break
        else:
            accepted.add(vector)
    return accepted


def test_fixed_sum_repair_preserves_complete_feasible_vectors_by_enumeration():
    original, tightened = _toy_repair(False), _toy_repair(True)
    assert [str(constraint) for constraint in original.proto.constraints] == [
        str(constraint) for constraint in tightened.proto.constraints]
    expected = {(0, 1, 0, 1, 1, 1), (1, 1, 1, 2, 0, 0)}
    assert _toy_feasible(original) == _toy_feasible(tightened) == expected
    # Both deviations, and therefore the complete balance objective for every
    # feasible vector, are unchanged. Fixed work above B stays capped at B.
    assert {sum(vector[-2:]) for vector in expected} == {0, 2}
