"""Every attendance assignment remains possible and has its actual pair deficit."""
import pytest
from ortools.sat.python import cp_model

from packages.domain.cp_sat_goals import _pair_deficit


@pytest.mark.parametrize('opportunities', [0, 1, 2, 3])
@pytest.mark.parametrize('need', [1, 2, 3, 5, 100000])
def test_pair_deficit_matches_every_attendance_assignment(opportunities, need):
    model = cp_model.CpModel()
    pairs = [(model.new_bool_var(f'a{i}'), model.new_bool_var(f'b{i}')) for i in range(opportunities)]
    deficit = _pair_deficit(model, pairs, need)
    found = {}

    class Oracle(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self):
            attendance = tuple(self.value(v) for pair in pairs for v in pair)
            actual = sum(attendance[i] * attendance[i + 1] for i in range(0, len(attendance), 2))
            assert attendance not in found
            found[attendance] = self.value(deficit)
            assert found[attendance] == max(0, need - actual)

    solver = cp_model.CpSolver()
    solver.parameters.enumerate_all_solutions = True
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.max_deterministic_time = 2
    assert solver.solve(model, Oracle()) == cp_model.OPTIMAL
    assert len(found) == 4 ** opportunities
