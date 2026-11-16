"""Successive priorities start from a complete feasible incumbent."""
from ortools.sat.python import cp_model

from packages.domain.cp_sat import generate
from packages.domain.solver_control import SolverControl
from tests.scheduler_departments import department


def test_widening_and_replacing_priority_bounds_preserves_a_feasible_hint(h, monkeypatch):
    data, _ = department(h, 7, people=10, days=7, rules='full', leave_rate=.05)
    data['generation_options'] = {'shortlist_width': 1}
    original = SolverControl.solve
    checked = []

    def inspect(self, solver, model, tier):
        if tier is not None and self.first_solution_seconds is not None:
            hinted = model.clone()
            hint = hinted.proto.solution_hint
            assert sorted(hint.vars) == list(range(len(hinted.proto.variables)))
            hinted.minimize(0)
            witness = cp_model.CpSolver()
            witness.parameters.num_workers = 1
            witness.parameters.max_time_in_seconds = 2
            witness.parameters.fix_variables_to_their_hinted_value = True
            assert witness.solve(hinted) == cp_model.OPTIMAL, (dict(self.context), witness.response_stats())
            checked.append(dict(self.context))
        return original(self, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', inspect)
    result = generate(data, 30, workers=1)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert any(row.get('candidates') == 'shortlist' for row in checked), checked
    assert any(row.get('candidates') == 'all' for row in checked), checked
