"""Long personal days-off gaps keep bounded expressions and cooperative model preparation."""
from datetime import date, timedelta
from time import process_time
import threading

import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.cp_sat_score import fractional_scale
from packages.domain.cp_sat_patterns import add_patterns
from packages.domain.patterns import wish_outcomes
from packages.domain.solver_control import Cancelled, SolverControl
from packages.domain.time import aware
from tests.test_performance_budget import python_budget
from tests.test_staff_wishes import wish


def gap_input(length, limit=2_147_483_647, worked=()):
    start = date(2026, 10, 1)
    shifts = []
    for offset in range(length):
        day = (start + timedelta(days=offset)).isoformat()
        shifts.append({'id': f'gap-{offset}', 'assignment_type_id': 'type', 'cancelled': False,
                       'start': day + 'T08:00:00+03:00', 'end': day + 'T12:00:00+03:00',
                       'participants': [{'person_id': 'person', 'start': day + 'T08:00:00+03:00'}]
                       if offset in worked else []})
    return {'organisation': {'timezone': 'Asia/Riyadh'},
            'workspace': {'starts_on': start.isoformat(), 'ends_on': (start + timedelta(days=length)).isoformat()},
            'shifts': shifts, 'policies': [], 'wishes': [{'id': 'gap-wish', 'person_id': 'person',
                'kind': 'min_days_off_together', 'limit': limit, 'points': 3, 'weight': 0.3,
                'first_day': start.isoformat(), 'last_day': (start + timedelta(days=length - 1)).isoformat()}]}


def gap_model(data, *, check=None):
    model = cp_model.CpModel()
    flags = [model.new_bool_var(shift['id']) for shift in data['shifts']]
    rows = [(shift, None, aware(shift['start']), aware(shift['end']), flag)
            for shift, flag in zip(data['shifts'], flags)]
    terms, worst = add_patterns(model, data, {'person': rows}, {'medium': 3}, check=check)
    return model, flags, terms, worst


def expression_size(model):
    return sum(len(constraint.linear.vars) for constraint in model.proto.constraints if constraint.has_linear())


@pytest.fixture(scope='module')
def quarter_gap_preparation():
    month, *_ = gap_model(gap_input(30))
    started = process_time()
    quarter, *_ = gap_model(gap_input(90))
    elapsed = process_time() - started
    print('large days-off gap preparation', {'cpu_seconds': elapsed,
          'month_references': expression_size(month), 'quarter_references': expression_size(quarter),
          'constraints': len(quarter.proto.constraints), 'variables': len(quarter.proto.variables)})
    return month, quarter, elapsed


def test_quarter_large_gap_expression_growth_is_quadratic_and_preparation_bounded(quarter_gap_preparation):
    month, quarter, _ = quarter_gap_preparation
    assert expression_size(quarter) <= expression_size(month) * 12


@pytest.mark.cpu_budget
def test_quarter_large_gap_preparation_cpu_budget(quarter_gap_preparation):
    elapsed = quarter_gap_preparation[-1]
    assert elapsed < python_budget(1)


@pytest.mark.parametrize('limit', [2, 3, 90, 2_147_483_647])
@pytest.mark.parametrize('worked', [[], [0], [0, 1, 2], [0, 2], [0, 3, 8]])
def test_gap_cost_matches_canonical_short_and_large_limit_shapes(limit, worked):
    data = gap_input(12, limit, worked)
    model, flags, terms, worst = gap_model(data)
    for offset, flag in enumerate(flags):
        model.add(flag == int(offset in worked))
    scale, *factors = fractional_scale([1, *(factor for factor, _ in terms)])
    model.minimize(sum(factor * sum(costs) for factor, (_, costs) in zip(factors, terms)))
    assert not model.validate()
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    honoured = wish_outcomes(data)['gap-wish'][0]
    assert solver.objective_value / scale == pytest.approx((1 - honoured) * 900)
    assert worst == 900


@pytest.mark.parametrize('worked', [[0, 2], [0, 1, 2]])
@pytest.mark.parametrize('fixed', [[0], [2], [0, 2]])
def test_gap_prefixes_mix_fixed_work_and_sparse_variable_choices(worked, fixed):
    data = gap_input(12, 90, worked)
    model, rows, variables = cp_model.CpModel(), [], []
    for offset in sorted(set(fixed) | set(worked) | {5, 8}):
        shift = data['shifts'][offset]
        flag = model.new_constant(1) if offset in fixed else model.new_bool_var(shift['id'])
        rows.append((shift, None, aware(shift['start']), aware(shift['end']), flag))
        if offset not in fixed:
            variables.append((offset, flag))
    terms, _ = add_patterns(model, data, {'person': rows}, {'medium': 3})
    for offset, flag in variables:
        model.add(flag == int(offset in worked))
    scale, *factors = fractional_scale([1, *(factor for factor, _ in terms)])
    model.minimize(sum(factor * sum(costs) for factor, (_, costs) in zip(factors, terms)))
    assert not model.validate()
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.objective_value / scale == pytest.approx((1 - wish_outcomes(data)['gap-wish'][0]) * 900)


@pytest.mark.parametrize('fixed', [[10, 13], [10, 11, 13]])
def test_fixed_department_gap_breaches_stay_uncharged_with_variable_work_before_them(fixed):
    data = gap_input(14, 3, fixed)
    data['wishes'] = []
    data['types'] = {'type': {'department_id': 'department'}}
    data['policies'] = [{'id': 'department-gap', 'department_id': 'department', 'status': 'active',
                         'definition': {'rule': {'kind': 'days_off_run', 'minimum': 3},
                                        'scope': {}, 'severity': 'advisory'}}]
    model, rows = cp_model.CpModel(), []
    for offset in [0, *fixed]:
        shift = data['shifts'][offset]
        flag = model.new_bool_var('optional-earlier-work') if offset == 0 else model.new_constant(1)
        rows.append((shift, None, aware(shift['start']), aware(shift['end']), flag))
    assert add_patterns(model, data, {'person': rows}, {'medium': 3}) == ([], 0)


def test_quarter_gap_preparation_observes_cancellation_before_building_every_constraint():
    data = gap_input(90)
    model = cp_model.CpModel()
    rows = [(shift, None, aware(shift['start']), aware(shift['end']), model.new_bool_var(shift['id']))
            for shift in data['shifts']]
    control = SolverControl(cancelled=lambda: len(model.proto.constraints) >= 128)
    with pytest.raises(Cancelled):
        add_patterns(model, data, {'person': rows}, {'medium': 3}, check=control.check)
    assert 128 <= len(model.proto.constraints) < 500


def test_full_generation_wires_cancellation_into_pattern_preparation(h, monkeypatch):
    from packages.domain import cp_sat_score_preferences
    workspace = h.shift(h.workspace())['workspace']
    wish(h, '2026-10-04', last='2026-10-10', kind='min_days_off_together', limit=90)
    cancelled = threading.Event()
    real = cp_sat_score_preferences.add_patterns
    entered = []

    def stop_during_patterns(*args, **kwargs):
        entered.append(True)
        assert kwargs.get('check') is not None
        cancelled.set()
        return real(*args, **kwargs)

    monkeypatch.setattr(cp_sat_score_preferences, 'add_patterns', stop_during_patterns)
    result = generate(h.snapshot(workspace), 5, cancelled=cancelled.is_set)
    assert entered and result['status'] == 'cancelled'
    assert result['additions'] == result['removals'] == []
