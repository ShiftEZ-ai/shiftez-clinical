"""Internal summary trials and bounded score completion preserve public solver behaviour."""
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from packages.domain import generation, solver_control, solver_summary, validation, wish_reasons
from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.solver_control import Cancelled, SolverControl
from packages.domain.validation import evaluate


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    return now


@pytest.mark.parametrize('allowance', [float('inf'), float('nan'), -1])
def test_completion_requires_finite_nonnegative_allowance(allowance):
    control = SolverControl()
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=allowance))
    with pytest.raises(ValueError, match='finite nonnegative'):
        control.solve(solver, None, None)
    assert control.completion_spent == 0


def test_completion_allowance_is_cumulative_and_separate_from_stopped_search(clock):
    updates, saved, limits = [], [], []
    control = SolverControl(updates.append, search_control=lambda: {'stop': True}, checkpoint_solution=saved.append)
    control.search_budget = control.search_spent = 3
    control.stalled, control.stall_seconds, control.first_solution_seconds = True, 1, 2
    control.phase('validation')

    def complete(seconds):
        solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=1), stop_search=lambda: None)

        def solve(model, callback):
            limits.append(solver.parameters.max_time_in_seconds)
            clock[0] += seconds
            solver.response_proto = SimpleNamespace(deterministic_time=seconds)
            return cp_model.FEASIBLE

        solver.solve = solve
        return control.solve(solver, None, None)

    assert complete(.25) == cp_model.FEASIBLE
    assert control.last_solve_deterministic_time == .25
    assert complete(.75) == cp_model.FEASIBLE
    assert control.last_solve_deterministic_time == .75
    assert complete(1) == cp_model.UNKNOWN
    assert control.last_solve_deterministic_time == 0
    assert limits == [1, .75]
    assert control.completion_budget == control.completion_spent == 1
    assert control.search_spent == 3 and control.stopped
    assert control.timings()['validation_seconds'] == 1
    assert control.timings()['search_seconds'] == 3
    assert control.stalled and control.stall_seconds == 1 and control.first_solution_seconds == 2
    assert not updates and not saved


def test_completion_watcher_enforces_cap_without_solution_callbacks(clock):
    control = SolverControl(search_control=lambda: {'stop': True})
    control.search_budget = control.search_spent = 1
    interrupted = threading.Event()
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=1), stop_search=interrupted.set)

    def solve(model, callback):
        clock[0] += 1
        assert interrupted.wait(2), 'Completion allowance did not interrupt a solve without callbacks'
        return cp_model.UNKNOWN

    solver.solve = solve
    assert control.solve(solver, None, None) == cp_model.UNKNOWN
    assert control.completion_spent == 1 and control.search_spent == 1


def test_cancel_interrupts_completion_and_accounts_elapsed_time(clock):
    cancelled, interrupted = threading.Event(), threading.Event()
    control = SolverControl(cancelled=cancelled.is_set)
    control.search_budget = control.search_spent = 1
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=1), stop_search=interrupted.set)

    def solve(model, callback):
        clock[0] += .25
        cancelled.set()
        assert interrupted.wait(2), 'Cancellation did not interrupt score completion'
        return cp_model.UNKNOWN

    solver.solve = solve
    with pytest.raises(Cancelled):
        control.solve(solver, None, None)
    assert control.completion_spent == .25 and control.search_spent == 1


def test_zero_completion_allowance_does_not_start_native_solve():
    control = SolverControl()
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=0))
    assert control.solve(solver, None, None) == cp_model.UNKNOWN
    assert control.completion_spent == 0


def test_summary_trials_skip_decoration_without_changing_public_reasons(h, monkeypatch):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    pid = h.seed['people'][0]
    workspace = h.shift(workspace, start='2026-10-04T09:00:00+03:00',
                        end='2026-10-04T13:00:00+03:00', people=[pid])['workspace']
    source = h.snapshot(workspace)
    source['people'] = {pid: source['people'][pid]}
    public_validation = evaluate(source)
    public_before = deepcopy(public_validation)
    evidence = [candidate_evidence(source, pid, shift) for shift in source['shifts']]
    calls = []

    def rich(data, **kwargs):
        return evaluate(data, **{**kwargs, 'explain_findings': True})

    monkeypatch.setattr(solver_summary, 'evaluate', rich)
    expected = solver_summary.summarise(source, public_validation, evidence, set(source['focus_ids']))

    def internal(data, **kwargs):
        calls.append(kwargs.get('explain_findings'))
        return evaluate(data, **kwargs)

    monkeypatch.setattr(solver_summary, 'evaluate', internal)
    actual = solver_summary.summarise(source, public_validation, evidence, set(source['focus_ids']))
    assert calls and all(flag is False for flag in calls)
    assert actual == expected
    assert any(row['code'] == 'TIME_OVERLAP' for row in actual['open_places'][0]['people'])
    assert public_validation == public_before


def test_greedy_trials_skip_decoration_and_keep_public_validation(h, monkeypatch):
    source = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    original, calls = generation.evaluate, []

    def rich(data, **kwargs):
        return original(data, **{**kwargs, 'explain_findings': True})

    monkeypatch.setattr(generation, 'evaluate', rich)
    expected = generation.generate(source, 5)

    def internal(data, **kwargs):
        calls.append(kwargs.get('explain_findings', True))
        return original(data, **kwargs)

    monkeypatch.setattr(generation, 'evaluate', internal)
    actual = generation.generate(source, 5)
    assert False in calls and calls[-1] is True
    assert actual['status'] == expected['status']
    assert actual['additions'] == expected['additions']
    assert actual['validation'] == expected['validation']


def test_wish_trial_skips_decoration_without_changing_rule_reason(h, monkeypatch):
    from tests.test_staff_wishes import wish

    pid = h.seed['people'][0]
    rule = h.policy({'kind': 'min_rest', 'hours': 24})
    wish(h, '2026-10-05', kind='prefer_on')
    workspace = h.shift(h.workspace(), people=[pid])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    source = h.snapshot(workspace)
    public_validation = evaluate(source)
    original, calls = validation.evaluate, []

    def rich(data, **kwargs):
        return original(data, **{**kwargs, 'explain_findings': True})

    monkeypatch.setattr(validation, 'evaluate', rich)
    expected = wish_reasons.declined_wishes(source, public_validation)

    def internal(data, **kwargs):
        calls.append(kwargs.get('explain_findings'))
        return original(data, **kwargs)

    monkeypatch.setattr(validation, 'evaluate', internal)
    actual = wish_reasons.declined_wishes(source, public_validation)
    assert calls and all(flag is False for flag in calls)
    assert actual == expected
    reason = actual[0]['reasons'][0]
    assert reason['code'] == 'MIN_REST' and reason['rule_id'] == rule['id']
