"""Proposal retention uses the shared, deadline-bounded infeasibility confirmation."""
import copy

import pytest
from ortools.sat.python import cp_model

from packages.domain.proposal_reconciliation import retain_bundles
from packages.domain.solver_control import SolverControl, TimeLimit


@pytest.fixture
def retention(h):
    current = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    shift = current['shifts'][0]
    result = {'additions': [{'shift_id': shift['id'], 'participant': {
        'person_id': h.seed['people'][0], 'start': shift['start'], 'end': shift['end']}}], 'removals': []}
    bundles = {'addition:0': {'shift_id': shift['id'], 'addition_indexes': [0], 'removal_indexes': []}}
    return current, result, bundles


@pytest.mark.parametrize('phase', ['baseline', 'maximize'])
@pytest.mark.parametrize('confirmed', [cp_model.FEASIBLE, cp_model.OPTIMAL])
def test_retention_confirms_false_presolve_infeasibility(retention, monkeypatch, phase, confirmed):
    native, calls = cp_model.CpSolver.solve, []
    false_call = 0 if phase == 'baseline' else 1

    def solve(solver, model, callback=None):
        calls.append((solver.parameters.cp_model_presolve, solver.parameters.max_time_in_seconds))
        assert solver.parameters.num_search_workers == 1
        if len(calls) == false_call + 1:
            impossible = model.clone()
            impossible.add(False)
            return native(solver, impossible, callback)
        status = native(solver, model, callback)
        assert status == cp_model.OPTIMAL
        return confirmed if not solver.parameters.cp_model_presolve else status

    monkeypatch.setattr(cp_model.CpSolver, 'solve', solve)
    before = copy.deepcopy(retention)
    selected, optimal, status = retain_bundles(*retention)
    assert retention == before
    assert status != 'INFEASIBLE'
    assert [presolve for presolve, _ in calls] == (
        [True, False] if phase == 'baseline' and confirmed == cp_model.FEASIBLE else
        [True, False, True] if phase == 'baseline' else [True, True, False])
    assert 0 < calls[false_call + 1][1] <= calls[false_call][1]
    if phase == 'baseline' and confirmed == cp_model.FEASIBLE:
        assert (selected, optimal, status) == ([], False, 'FEASIBLE')
    else:
        assert selected == ['addition:0']
        assert optimal == (confirmed == cp_model.OPTIMAL)
        assert status == confirmed.name


@pytest.mark.parametrize('phase', ['baseline', 'maximize'])
def test_retention_deadline_commits_nothing(retention, monkeypatch, phase):
    original, calls = SolverControl.solve, []

    def solve(control, solver, model, tier):
        calls.append(tier)
        assert tier == 'diagnostics'
        if len(calls) == (1 if phase == 'baseline' else 2):
            raise TimeLimit()
        return original(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    assert retain_bundles(*retention) == ([], False, 'UNKNOWN')
