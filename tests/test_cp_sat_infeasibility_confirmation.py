"""Presolve alone is never evidence that mandatory scheduling requirements conflict."""
from types import SimpleNamespace
import threading

import pytest
from ortools.sat.python import cp_model

from packages.domain import solver_control
from packages.domain.cp_sat import generate
from packages.domain.solver_control import Cancelled, SolverControl
from packages.domain.solver_diagnostics import explain_conflict


@pytest.mark.parametrize('confirmed', [cp_model.FEASIBLE, cp_model.UNKNOWN, cp_model.INFEASIBLE])
def test_generation_reports_only_confirmed_infeasibility(h, monkeypatch, confirmed):
    h.policy({'kind': 'restriction'}, severity='mandatory' if confirmed == cp_model.INFEASIBLE else 'advisory')
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    data['generation_options'] = {'shortlist_width': 0, 'on_conflict': 'report'}
    original, calls = SolverControl._solve_once, []

    def solve(control, solver, model, tier, *, extend_slice=True):
        if not isinstance(tier, int):
            return original(control, solver, model, tier, extend_slice=extend_slice)
        calls.append(solver.parameters.cp_model_presolve)
        if solver.parameters.cp_model_presolve:
            return cp_model.INFEASIBLE
        if confirmed == cp_model.UNKNOWN:
            return confirmed
        status = original(control, solver, model, tier, extend_slice=extend_slice)
        assert status == (cp_model.INFEASIBLE if confirmed == cp_model.INFEASIBLE else cp_model.OPTIMAL)
        return confirmed

    monkeypatch.setattr(SolverControl, '_solve_once', solve)
    result = generate(data, 5)
    assert calls[:2] == [True, False]
    assert result['weighted_status'] == result['solver_status'] == confirmed.name
    if confirmed == cp_model.INFEASIBLE:
        assert result['status'] == 'requirements_conflict'
        assert result['generation_outcome']['strict_status'] == 'INFEASIBLE'
    else:
        assert result['status'] != 'requirements_conflict'
        assert result['generation_outcome']['strict_status'] != 'INFEASIBLE'
        assert (result.get('conflict') or {}).get('strict_status') != 'INFEASIBLE'
        assert not result['optimality_proven']


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    return now


class ScriptedSolver:
    def __init__(self, clock, statuses, *, wall=5, deterministic=float('inf'), spent=(1, 2)):
        self.parameters = SimpleNamespace(max_time_in_seconds=wall,
            max_deterministic_time=deterministic, cp_model_presolve=True)
        self.clock, self.statuses, self.spent = clock, statuses, spent
        self.calls = []
        self.response_proto = SimpleNamespace(deterministic_time=0)

    def solve(self, model, callback):
        index = len(self.calls)
        self.calls.append(dict(vars(self.parameters)))
        self.clock[0] += 2 if index == 0 else 1
        self.response_proto = SimpleNamespace(deterministic_time=self.spent[index])
        return self.statuses[index]

    def stop_search(self):
        pass


@pytest.mark.parametrize('tier', [0, None, 'diagnostics'])
def test_false_infeasibility_recovers_the_actual_solution_and_deterministic_work(monkeypatch, tier):
    model = cp_model.CpModel()
    x = model.new_bool_var('required')
    model.add(x == 1)
    impossible = model.clone()
    impossible.add(x == 0)
    native = cp_model.CpSolver.solve
    calls, work = [], []

    def false_presolve(solver, actual, callback):
        calls.append(solver.parameters.cp_model_presolve)
        status = native(solver, impossible if len(calls) == 1 else actual, callback)
        work.append(solver.response_proto.deterministic_time)
        return status

    monkeypatch.setattr(cp_model.CpSolver, 'solve', false_presolve)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 2
    before = str(model.proto)
    control = SolverControl()
    assert control.solve(solver, model, tier) == cp_model.OPTIMAL
    assert calls == [True, False]
    assert solver.value(x) == 1
    assert solver.response_proto.status == cp_model.OPTIMAL
    assert control.last_solve_deterministic_time == sum(work)
    assert solver.parameters.cp_model_presolve and solver.parameters.max_time_in_seconds == 2
    assert str(model.proto) == before


def test_confirmed_assumption_conflict_preserves_the_final_core(monkeypatch):
    model = cp_model.CpModel()
    x, guard = model.new_bool_var('x'), model.new_bool_var('required')
    model.add(x == 1)
    model.add(x == 0).only_enforce_if(guard)
    model.add_assumption(guard)
    native, calls = cp_model.CpSolver.solve, []

    def observed(solver, actual, callback):
        calls.append(solver.parameters.cp_model_presolve)
        return native(solver, actual, callback)

    monkeypatch.setattr(cp_model.CpSolver, 'solve', observed)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 2
    assert SolverControl().solve(solver, model, 'diagnostics') == cp_model.INFEASIBLE
    assert calls == [True, False]
    assert solver.sufficient_assumptions_for_infeasibility() == [guard.index]


@pytest.mark.parametrize('confirmed', [cp_model.INFEASIBLE, cp_model.UNKNOWN, cp_model.MODEL_INVALID])
def test_confirmation_status_uses_only_the_remaining_original_slice(clock, confirmed):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, confirmed])
    control = SolverControl()
    assert control.solve(solver, None, 'diagnostics') == confirmed
    assert [row['cp_model_presolve'] for row in solver.calls] == [True, False]
    assert solver.calls[1]['max_time_in_seconds'] == 3
    assert solver.calls[1]['max_deterministic_time'] == float('inf')
    assert control.last_solve_deterministic_time == 3


@pytest.mark.parametrize('tier', [0, 'diagnostics'])
@pytest.mark.parametrize('elapsed', [0, 120])
def test_deterministic_confirmation_uses_only_remaining_work_units(clock, tier, elapsed):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, cp_model.INFEASIBLE], wall=2, deterministic=10)
    native = solver.solve

    def slow_solve(model, callback):
        clock[0] += elapsed
        return native(model, callback)

    solver.solve = slow_solve
    control = SolverControl()
    assert control.solve(solver, None, tier) == cp_model.INFEASIBLE
    assert solver.calls[1]['max_time_in_seconds'] == float('inf')
    assert solver.calls[1]['max_deterministic_time'] == 9
    assert control.last_solve_deterministic_time == 3
    assert solver.parameters.max_time_in_seconds == 2
    assert solver.parameters.max_deterministic_time == 10


@pytest.mark.parametrize('wall, elapsed, status, limits, spent, work', [
    (5, 0, cp_model.INFEASIBLE, [5, 3], 3, 3),
    (2, 0, cp_model.UNKNOWN, [2], 2, 1),
    (5, 120, cp_model.UNKNOWN, [5], 122, 1),
])
def test_deterministic_completion_confirmation_shares_remaining_wall_allowance(
        clock, wall, elapsed, status, limits, spent, work):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, cp_model.INFEASIBLE], wall=wall, deterministic=10)
    native = solver.solve

    def slow_solve(model, callback):
        clock[0] += elapsed
        return native(model, callback)

    solver.solve = slow_solve
    control = SolverControl()
    assert control.solve(solver, None, None) == status
    assert [call['max_time_in_seconds'] for call in solver.calls] == limits
    assert control.completion_budget == wall and control.completion_spent == spent
    assert control.last_solve_deterministic_time == work
    assert solver.parameters.max_time_in_seconds == wall
    assert solver.parameters.max_deterministic_time == 10
    if len(solver.calls) == 2:
        assert solver.calls[1]['max_deterministic_time'] == 9
        assert not solver.calls[1]['cp_model_presolve']


@pytest.mark.parametrize('wall, deterministic, spent', [(2, float('inf'), (1,)), (5, 1, (1,))])
def test_no_confirmation_budget_means_unknown(clock, wall, deterministic, spent):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE], wall=wall,
                            deterministic=deterministic, spent=spent)
    assert SolverControl().solve(solver, None, 0) == cp_model.UNKNOWN
    assert len(solver.calls) == 1


def test_confirmation_preserves_deadline_and_final_check_reserve(clock):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, cp_model.UNKNOWN])
    control = SolverControl()
    control.deadline = control.started + 4
    control.reserve_seconds = 1
    assert control.solve(solver, None, 0) == cp_model.UNKNOWN
    assert [row['max_time_in_seconds'] for row in solver.calls] == [3, 1]


def test_confirmation_charges_both_calls_to_the_live_search_budget(clock):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, cp_model.UNKNOWN])
    control = SolverControl()
    control.search_budget = 5
    assert control.solve(solver, None, 0) == cp_model.UNKNOWN
    assert control.search_spent == 3 and control.remaining() == 2
    assert len(solver.calls) == 2


def test_confirmation_does_not_consume_exhausted_search_budget_for_diagnostics(clock):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, cp_model.UNKNOWN])
    control = SolverControl()
    control.search_budget = control.search_spent = 5
    assert control.solve(solver, None, 'diagnostics') == cp_model.UNKNOWN
    assert control.search_spent == 5
    assert solver.calls[1]['max_time_in_seconds'] == 3


def test_unbounded_diagnostic_confirmation_is_capped(clock):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE, cp_model.UNKNOWN], wall=float('inf'))
    assert SolverControl().solve(solver, None, 'diagnostics') == cp_model.UNKNOWN
    assert solver.calls[1]['max_time_in_seconds'] == 10


def test_cancellation_after_the_first_solve_prevents_confirmation(clock):
    solver = ScriptedSolver(clock, [cp_model.INFEASIBLE])
    control = SolverControl(cancelled=lambda: bool(solver.calls))
    with pytest.raises(Cancelled):
        control.solve(solver, None, 0)
    assert len(solver.calls) == 1


def test_model_invalid_is_not_retried(clock):
    solver = ScriptedSolver(clock, [cp_model.MODEL_INVALID])
    assert SolverControl().solve(solver, None, 0) == cp_model.MODEL_INVALID
    assert len(solver.calls) == 1


def test_assumption_diagnostics_do_not_publish_a_false_conflict(monkeypatch):
    model = cp_model.CpModel()
    x = model.new_bool_var('x')
    model.add(x == 1).with_name('policy:cover')
    native, calls = cp_model.CpSolver.solve, []

    def false_presolve(solver, actual, callback):
        calls.append(solver.parameters.cp_model_presolve)
        if len(calls) == 1:
            impossible = actual.clone()
            impossible.add(x == 0)
            return native(solver, impossible, callback)
        return native(solver, actual, callback)

    monkeypatch.setattr(cp_model.CpSolver, 'solve', false_presolve)
    result = explain_conflict(model, {'fingerprint': 'frozen'}, SolverControl(), 2)
    assert calls == [True, False]
    assert result['implementation_error'] and not result['proven']
    assert result['code'] == 'DIAGNOSTIC_MODEL_ERROR'
    assert not result['requirements']


def test_live_budget_extension_does_not_renew_confirmation_cap(clock):
    update, interrupted, calls = {}, threading.Event(), []
    control = SolverControl(search_control=lambda: update)
    control.search_budget = 60
    solver = ScriptedSolver(clock, [], wall=60)

    def solve(model, callback):
        calls.append(solver.parameters.cp_model_presolve)
        solver.response_proto = SimpleNamespace(deterministic_time=0)
        if len(calls) == 1:
            return cp_model.INFEASIBLE
        update['minutes'] = 2
        clock[0] += 11
        assert interrupted.wait(2), 'Confirmation ignored its own ten-second cap'
        return cp_model.UNKNOWN

    solver.solve, solver.stop_search = solve, interrupted.set
    assert control.solve(solver, None, 0) == cp_model.UNKNOWN
    assert calls == [True, False]
    assert control.search_budget == 120 and control.search_spent == 11
