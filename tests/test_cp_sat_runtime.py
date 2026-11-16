"""Linux dependency qualification; application rule parity is tested separately."""
from ortools.sat.python import cp_model


def test_cp_sat_can_reassign_an_earlier_choice_to_cover_both_days():
    model = cp_model.CpModel()
    flexible_first = model.new_bool_var('flexible_first')
    restricted_first = model.new_bool_var('restricted_first')
    flexible_second = model.new_bool_var('flexible_second')
    model.add(flexible_first + restricted_first == 1)
    model.add(flexible_second == 1)
    model.add(flexible_first + flexible_second <= 1)
    # An existing assignment is a hint, not permission to leave day two empty.
    model.add_hint(flexible_first, 1)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_workers = 1
    assert not model.validate()
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(flexible_first) == 0
    assert solver.value(restricted_first) == solver.value(flexible_second) == 1
