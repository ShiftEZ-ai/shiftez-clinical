import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from apps.api import models as m
from apps.api.db import transaction, retry_write
from apps.worker.main import claim_job, heartbeat_job, finish_job, process_one
from packages.domain.cp_sat import generate
from packages.domain.solver_control import SolverControl, Cancelled
from packages.domain import solver_control


def test_fair_share_plateau_extends_only_for_unmet_fairness_progress(monkeypatch):
    now = [20.0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    control = SolverControl()
    history = [(0, 1000), (4, 999), (8, 998), (12, 997)]
    assert control.on_plateau(history, 10)  # No goal: retain the ordinary 1% rule.
    control.fairness_plateau, control.fairness_stall_seconds = True, 15
    assert not control.on_plateau(history, 10, [(0, 100), (8, 99)])  # Small fairness gains count.
    assert control.on_plateau(history, 10, [(0, 100), (4, 99), (12, 99)])  # Preference gains do not.
    assert control.on_plateau(history, 10, [(0, 100), (12, 0)])  # Fully satisfied: ordinary plateau.
    assert not control.on_plateau(history, 10, [(0, 0), (12, 5)])  # Worsening reopens a repair window.
    assert control.on_plateau(history, 10, [(0, 100), (4, 99)])  # Expired extension falls back.
    assert not control.on_plateau([(0, 1000), (12, 900)], 10, [(0, 100)])  # Ordinary progress still counts.
    assert not control.on_plateau(history, -1, [(0, 0)])  # First incumbent gets the ordinary window.
    control.fairness_stall_seconds = 0
    assert not control.on_plateau(history, 10, [(0, 100)])
    assert control.on_plateau(history, 10, [(0, 0)])


@pytest.mark.parametrize('window, ended', [(5, 10), (0, 20)])
@pytest.mark.parametrize('losses, improving', [([100, 90, 80], True), ([100, 90, 95], False), ([100, 100, 100], False)])
def test_fairness_stop_tag_tracks_actual_fairness_not_just_weighted_improvements(monkeypatch, losses, improving, window, ended):
    now, current = [0.0], [0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(solver_control.cp_model, 'CpSolverSolutionCallback', type('Callback', (), {
        'objective_value': property(lambda self: 1000 - current[0]), 'best_objective_bound': 0,
        'value': lambda self, expression: losses[current[0]],
    }))
    control = SolverControl()
    control.fairness_plateau, control.fairness_stall_seconds, control.fairness_loss = True, window, 'canonical fairness loss'
    control.stall_seconds = 5
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=12), stop_search=lambda: None)
    def solve(model, callback):
        for i, at in enumerate((0, 4, 8)):
            current[0], now[0] = i, at
            callback.on_solution_callback()
        now[0] = ended
        return solver_control.cp_model.FEASIBLE
    solver.solve = solve
    assert control.solve(solver, None, 1) == solver_control.cp_model.FEASIBLE
    assert control.last_search['objective_improvement_age_seconds'] == ended - 8
    assert control.last_search['fairness_improving'] is (improving and window != 0)
    assert control.last_search['stop_reason'] == 'time_limit'


def test_incumbent_latency_keeps_largest_first_search_callback_sample(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    # Exercise the controller's callback without introducing native solver timing into this clock test.
    monkeypatch.setattr(solver_control.cp_model, 'CpSolverSolutionCallback', type('Callback', (), {
        'objective_value': 1, 'best_objective_bound': 0,
    }))
    control = SolverControl()
    control.deadline = 200
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=10), stop_search=lambda: None)

    def run(first, tier=0):
        def solve(model, callback):
            now[0] += first
            callback.on_solution_callback()
            now[0] += 5
            callback.on_solution_callback()  # Later improvements are not another startup sample.
            return solver_control.cp_model.FEASIBLE

        solver.solve = solve
        assert control.solve(solver, None, tier) == solver_control.cp_model.FEASIBLE

    run(2)
    assert control.incumbent_latency_seconds == 2
    assert control.first_solution_seconds == 2
    run(1)
    assert control.incumbent_latency_seconds == 2
    run(3)
    assert control.incumbent_latency_seconds == 3
    run(8, tier=None)  # Arithmetic completion does not change search admission or public first-solution time.
    assert control.incumbent_latency_seconds == 3
    assert control.first_solution_seconds == 2
    solver.solve = lambda model, callback: solver_control.cp_model.UNKNOWN
    assert control.solve(solver, None, 0) == solver_control.cp_model.UNKNOWN
    assert control.incumbent_latency_seconds == 3


def test_search_interrupt_does_not_require_solution_callbacks():
    cancelled = threading.Event()
    interrupted = threading.Event()
    class Searching:
        def solve(self, model, callback):
            cancelled.set()
            assert interrupted.wait(2), 'Search was not interrupted without a solution'
            return 0
        def stop_search(self):
            interrupted.set()
    with pytest.raises(Cancelled):
        SolverControl(cancelled=cancelled.is_set).solve(Searching(), None, 0)


def test_worker_cp_sat_progress_and_preparation_cancellation(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    stop = threading.Event()
    def report(update):
        if update['stage'] == 'preparing':
            stop.set()
    result = generate(data, progress=report, cancelled=stop.is_set)
    assert result['status'] == 'cancelled'
    assert result['weighted_status'] == 'UNKNOWN'
    assert not result['additions'] and not result['removals']
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']
    assert process_one(h.app.state.engine, scheduler=generate)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.result['status'] == 'candidate_feasible'
        assert row.progress['stage'] == 'ready'
        assert row.progress['shared_validation_passed']
        assert row.result['first_solution_seconds'] is not None


def test_live_heartbeat_prevents_lease_recovery_and_late_results(h):
    w = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']
    engine = h.app.state.engine
    claim = retry_write(engine, claim_job)
    with transaction(engine, write=True) as db:
        db.get(m.Job, job['id']).started_at = m.now() - timedelta(hours=1)
    assert retry_write(engine, lambda db: heartbeat_job(db, claim, {'stage': 'finding'}))
    assert retry_write(engine, claim_job) is None
    with transaction(engine, write=True) as db:
        db.get(m.Job, job['id']).status = 'cancelled'
    assert not retry_write(engine, lambda db: heartbeat_job(db, claim, {'stage': 'ready'}))
    retry_write(engine, lambda db: finish_job(db, claim, {'status': 'candidate_feasible'}, None))
    with transaction(engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'cancelled' and row.result is None


def test_running_worker_observes_operator_cancellation(h):
    w = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'generation'}, revision=w['revision'])['job']
    def waiting_scheduler(snapshot, *, progress, cancelled):
        progress({'stage': 'finding'})
        running = h.get('coordinator', '/api/jobs/' + job['id'])['job']
        assert running['revision'] > job['revision']
        h.command('operator', 'job.cancel', {'reason': 'Test interruption of running search'},
                  job['id'], job['revision'])
        # The worker watcher must detect the persisted cancellation independently.
        deadline = time.monotonic() + 8
        while not cancelled() and time.monotonic() < deadline:
            threading.Event().wait(.05)
        assert cancelled()
        return {'status': 'cancelled', 'additions': [], 'removals': []}
    process_one(h.app.state.engine, scheduler=waiting_scheduler)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.status == 'cancelled' and row.result is None
    assert h.detail(w)['workspace']['revision'] == w['revision']
    late = h.command('operator', 'job.cancel', {'reason': 'Confirmation arrived after cancellation'},
                     job['id'], job['revision'], status=409)
    assert late['error']['code'] == 'JOB_STATE'
