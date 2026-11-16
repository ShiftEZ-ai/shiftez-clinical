"""Bounded score minima retain constants through pattern breaches and live scoring."""
import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat_score import SCORE_SCALE, ScoreModel, begin_score_program
from tests.test_live_score_program import assert_program_matches


@pytest.mark.parametrize('fixed', [False, True])
@pytest.mark.parametrize('negated', [False, True])
def test_boolean_minimum_folds_absorbing_and_identity_constants(fixed, negated):
    model = cp_model.CpModel()
    flag = model.new_bool_var('duty')
    zero, one = (model.new_constant(0), model.new_constant(1)) if fixed else (0, 1)
    begin_score_program(model)
    q = ScoreModel(model)
    expression = 1 - flag if negated else flag
    before = len(model.proto.variables)
    assert q.minimum([expression, zero], 1) == 0
    remaining = q.minimum([one, expression], 1)
    assert len(model.proto.variables) == before and not model.proto.constraints
    for value in (0, 1):
        trial = model.clone()
        trial.add(trial.get_bool_var_from_proto_index(flag.index) == value)
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        assert solver.solve(trial) == cp_model.OPTIMAL
        assert solver.value(remaining) == (1 - value if negated else value)
    model.add(flag == 1)
    assert_program_matches(model, {'preferences': (SCORE_SCALE * remaining, 1)})


@pytest.mark.parametrize('values', [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_minimum_keeps_a_binding_constant_ceiling_and_records_exact_live_values(values):
    model = cp_model.CpModel()
    x, y = model.new_bool_var('x'), model.new_bool_var('y')
    begin_score_program(model)
    q = ScoreModel(model)
    capped = q.minimum([1, x + y], 2)
    conjunction = q.minimum([x, 1 - y, 1], 1)
    assert q.bounds(capped) == (0, 1)
    assert q.bounds(conjunction) == (0, 1)
    for flag, value in zip((x, y), values):
        model.add(flag == value)
    assert_program_matches(model, {'rules': (SCORE_SCALE * capped, 1),
                                   'preferences': (SCORE_SCALE * conjunction, 1)})
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(capped) == min(1, sum(values))
    assert solver.value(conjunction) == min(values[0], 1 - values[1])


def test_minimum_uses_tight_non_boolean_bounds_and_reuses_the_surviving_expression():
    model = cp_model.CpModel()
    x, y = model.new_int_var(2, 7, 'x'), model.new_int_var(4, 9, 'y')
    q = ScoreModel(model)
    result = q.minimum([x, y, 10])
    assert q.bounds(result) == (2, 7)
    assert q.minimum([10, x]) is x
    assert q.minimum([x, 2]) == 2
    assert len(model.proto.variables) == 3 and len(model.proto.constraints) == 1
