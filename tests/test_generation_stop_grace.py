"""Operator stop keeps canonical evidence within one finalization allowance."""
from copy import deepcopy
from datetime import timedelta
import threading
import time
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from packages.domain import cp_sat, solver_control, solver_summary
from packages.contracts.schemas import GenerationOptions
from tests.test_solver_search_budget import snapshot, store_checkpoints


@pytest.fixture
def stop_clock(monkeypatch):
    now = [100.0]
    # Patch this module's clock without freezing worker leases or Python thread waits.
    monkeypatch.setattr(solver_control, 'time', SimpleNamespace(
        monotonic=lambda: now[0], thread_time=time.thread_time))
    return now


@pytest.mark.parametrize('grace', [1, 15])
@pytest.mark.parametrize('deterministic', [False, True])
def test_mid_solve_stop_calls_callback_stop_and_keeps_incumbent(h, monkeypatch, stop_clock, grace, deterministic):
    data, update, kept, stops = snapshot(h), {}, [], []
    data['generation_options'] = {'generation_stop_grace_seconds': grace}
    native_solve = cp_model.CpSolver.solve

    def solve(solver, model, callback=None):
        solver.parameters.max_deterministic_time = .1
        if callback is not None:
            stop = callback.stop_search

            def stop_search():
                stops.append(True)
                stop()

            callback.stop_search = stop_search
        return native_solve(solver, model, callback)

    def progress(row):
        if row['stage'] == 'improving' and not update:
            kept.extend((p['shift_id'], p['person_id']) for p in row['solution']['pairs'])
            update['stop'] = True

    monkeypatch.setattr(cp_model.CpSolver, 'solve', solve)
    control = solver_control.SolverControl(progress, search_control=lambda: update)
    result = cp_sat.generate(data, 5, _control=control, deterministic=deterministic)
    assert stops, 'The solution callback must call StopSearch on operator stop'
    assert control.stop_deadline == stop_clock[0] + grace
    assert result['status'] == 'candidate_feasible' and result['validation']['valid']
    assert sorted((a['shift_id'], a['participant']['person_id']) for a in result['additions']) == sorted(kept)
    assert result['solver_status'] == 'FEASIBLE' and not result['optimality_proven']
    assert result['search_stop']['reason'] == 'operator_stop'


@pytest.mark.wall_benchmark
def test_mid_solve_stop_wall_grace(h, request):
    data, update = snapshot(h), {}
    grace = GenerationOptions().generation_stop_grace_seconds

    def progress(row):
        if row['stage'] == 'improving' and not update:
            update.update(stop=True, at=time.monotonic())

    cp_sat.generate(data, 5, progress=progress, search_control=lambda: update)
    elapsed = time.monotonic() - update['at']
    request.node.user_properties.append(('solver_stop_wall_seconds', elapsed))
    assert elapsed <= grace


@pytest.mark.parametrize('grace', [1, 10])
@pytest.mark.parametrize('stop_at', ['callback', 'between_tiers'])
def test_stop_after_proven_tier_keeps_incumbent_and_operator_reason(h, monkeypatch, grace, stop_at):
    h.policy({'kind': 'restriction'}, severity='advisory')
    data, update, solved, stops, kept = snapshot(h), {}, [], [], []
    data['generation_options'] = {'generation_stop_grace_seconds': grace}
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0].update(minimum=0, target=1, maximum=1)
    native_solve = cp_model.CpSolver.solve
    original_solve = solver_control.SolverControl.solve

    def solve(solver, model, callback=None):
        if callback is not None:
            stop_search = callback.stop_search

            def stop():
                stops.append(True)
                stop_search()

            callback.stop_search = stop
        status = native_solve(solver, model, callback)
        # Model a later search interrupted with an incumbent but without its optimality proof.
        return cp_model.FEASIBLE if update and stop_at == 'callback' else status

    def solve_tier(control, solver, model, tier):
        status = original_solve(control, solver, model, tier)
        if tier is not None:
            solved.append((tier, status))
        return status

    def progress(row):
        if row['stage'] == 'improving' and row.get('tier') == 0 and 'solution' in row:
            kept[:] = [(p['shift_id'], p['person_id']) for p in row['solution']['pairs']]
        if row['stage'] == 'improving' and row.get('tier') == 1:
            if ('objective_value' in row) == (stop_at == 'callback'):
                assert solved == [(0, cp_model.OPTIMAL)]
                update['stop'] = True

    monkeypatch.setattr(cp_model.CpSolver, 'solve', solve)
    monkeypatch.setattr(solver_control.SolverControl, 'solve', solve_tier)
    result = cp_sat.generate(data, 5, progress=progress, search_control=lambda: update)
    assert update['stop']
    assert bool(stops) == (stop_at == 'callback')
    assert result['status'] == 'candidate_feasible' and result['validation']['valid']
    assert len(result['additions']) == 1
    assert [(a['shift_id'], a['participant']['person_id']) for a in result['additions']] == kept
    assert result['solver_status'] == 'FEASIBLE' and not result['optimality_proven']
    assert result['objectives'][0]['proven_optimal']
    assert not result['objectives'][1]['proven_optimal']
    assert result['search_stop']['reason'] == result['stop_reason'] == 'operator_stop'


def test_stop_after_all_tiers_proven_keeps_optimal_reason(h):
    update = {}

    def progress(row):
        if row['stage'] == 'ready':
            update['stop'] = True

    result = cp_sat.generate(snapshot(h), 5, progress=progress, search_control=lambda: update)
    assert update['stop'] and result['optimality_proven']
    assert all(tier['proven_optimal'] for tier in result['objectives'])
    assert result['search_stop']['reason'] == result['stop_reason'] == 'optimal'


def test_stop_with_late_cancel_keeps_cancelled_semantics(h):
    update = {}

    def progress(row):
        if row['stage'] == 'ready':
            update.update(stop=True, cancel=True)

    result = cp_sat.generate(snapshot(h), 5, progress=progress, search_control=lambda: update,
                             cancelled=lambda: update.get('cancel', False))
    assert result['status'] == result['search_stop']['reason'] == 'cancelled'
    assert not result['optimality_proven'] and result['additions'] == []


@pytest.mark.parametrize('phase', ['summary', 'scoring'])
def test_stop_grace_expiry_keeps_checked_incumbent_without_score(h, monkeypatch, phase):
    data, update, saved, now = snapshot(h), {}, [], [100.0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    from packages.domain import score
    original = solver_summary.summarise if phase == 'summary' else score.schedule_score

    def progress(row):
        if row['stage'] == 'improving':
            update['stop'] = True

    def expire(*args, **kwargs):
        if update.get('stop'):
            now[0] += 10
        return original(*args, **kwargs)

    monkeypatch.setattr(solver_summary if phase == 'summary' else score,
                        'summarise' if phase == 'summary' else 'schedule_score', expire)
    result = cp_sat.generate(data, 5, progress=progress, search_control=lambda: update,
                             checkpoint=store_checkpoints(saved))
    assert result['status'] == 'candidate_feasible' and result['validation']['valid']
    assert result['additions'] and result['score'] is None
    assert result['score_completed'] is False and not result['optimality_proven']
    assert result['search_stop']['reason'] == 'operator_stop'
    after = deepcopy(data)
    for addition in result['additions']:
        next(s for s in after['shifts'] if s['id'] == addition['shift_id'])['participants'].append(addition['participant'])
    assert cp_sat.evaluate(after)['valid']


def test_stop_grace_expiry_in_canonical_validation_is_resumable_not_applyable(h, monkeypatch):
    data, update, saved, now = snapshot(h), {}, [], [100.0]
    original, stages = cp_sat.evaluate, []
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])

    def progress(row):
        stages.append(row['stage'])
        if row['stage'] == 'improving':
            update['stop'] = True

    def expire(data, **kwargs):
        if stages[-1] == 'validating':
            now[0] += 10
        return original(data, **kwargs)

    monkeypatch.setattr(cp_sat, 'evaluate', expire)
    result = cp_sat.generate(data, 5, progress=progress, search_control=lambda: update,
                             checkpoint=store_checkpoints(saved))
    assert result['status'] == 'needs_more_time' and result['solver_status'] == 'UNKNOWN'
    assert result['additions'] == result['removals'] == []
    assert 'validation' not in result and not result['optimality_proven']
    assert result['score'] is None and result['score_completed'] is False
    assert saved[0]['hint'] and saved[0]['checkpoint_assignments']


def test_stop_interrupts_completion_without_solution_callbacks(stop_clock):
    update, interrupted = {}, threading.Event()
    control = solver_control.SolverControl(search_control=lambda: update)
    control.configure_stop_conditions(GenerationOptions())
    control.search_budget = 60
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=1), stop_search=interrupted.set)

    def solve(model, callback):
        update['stop'] = True
        control.poll()
        assert not interrupted.wait(.2), 'Operator stop interrupted completion before its cap'
        stop_clock[0] += 1
        assert interrupted.wait(1), 'Completion ignored its cap without a new solution'
        return cp_model.UNKNOWN

    solver.solve = solve
    assert control.solve(solver, None, None) == cp_model.UNKNOWN
    assert control.stopped and control.stop_reason == 'operator_stop'
    assert control.completion_spent == control.completion_budget == 1


def test_stop_grace_interrupts_completion_without_solution_callbacks(stop_clock):
    interrupted = threading.Event()
    control = solver_control.SolverControl(search_control=lambda: {'stop': True})
    control.configure_stop_conditions(GenerationOptions(generation_stop_grace_seconds=1))
    control.search_budget = 60
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=60), stop_search=interrupted.set)

    def solve(model, callback):
        assert not interrupted.wait(.2), 'Operator stop interrupted completion before its grace deadline'
        stop_clock[0] += 1
        assert interrupted.wait(2), 'Stop grace did not interrupt score completion'
        return cp_model.UNKNOWN

    solver.solve = solve
    with pytest.raises(solver_control.StopGraceExpired):
        control.solve(solver, None, None)
    assert control.completion_spent == 1 < control.completion_budget


@pytest.mark.parametrize('value', [0, 16, True, 1.5, '5'])
def test_stop_grace_setting_is_validated(value):
    with pytest.raises(ValueError):
        GenerationOptions(generation_stop_grace_seconds=value)


def test_stop_deadline_is_shared_sticky_and_runtime_only(monkeypatch):
    from packages.domain.solver_resume import resume_key
    now, update = [100.0], {'stop': True}
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    control = solver_control.SolverControl(search_control=lambda: update)
    options = GenerationOptions(generation_stop_grace_seconds=3)
    control.configure_stop_conditions(options)
    control.search_budget = 60
    control.poll()
    assert control.stop_deadline == 103
    update.clear()
    now[0] = 102
    control.check()
    assert control.stop_deadline == 103 and control.remaining() == 0
    now[0] = 103
    with pytest.raises(solver_control.StopGraceExpired):
        control.check()
    assert resume_key({'fingerprint': 'synthetic'}, options, False, False) == resume_key(
        {'fingerprint': 'synthetic'}, GenerationOptions(), False, False)


@pytest.mark.parametrize('age,remaining', [(2, 1), (7, 0)])
def test_persisted_stop_request_age_consumes_the_same_grace(monkeypatch, age, remaining):
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: 100)
    instant = solver_control.utc_now()
    monkeypatch.setattr(solver_control, 'utc_now', lambda: instant)
    update = {'stop': True, 'stop_requested_at': (solver_control.utc_now() - timedelta(seconds=age)).isoformat()}
    control = solver_control.SolverControl(search_control=lambda: update)
    control.configure_stop_conditions(GenerationOptions(generation_stop_grace_seconds=3))
    control.search_budget = 60
    control.poll()
    assert control.stop_deadline == 100 + remaining
    update['stop_requested_at'] = solver_control.utc_now().isoformat()
    control.poll()
    assert control.stop_deadline == 100 + remaining  # A repeated request cannot replenish the allowance.


def test_stopped_worker_presentation_does_not_restart_validation_or_scoring(monkeypatch):
    from packages.domain import rota_summary
    monkeypatch.setattr(rota_summary, '_generation_fix_lists',
                        lambda *args: pytest.fail('Stopped worker restarted validation/scoring'))
    result = rota_summary.generation_fix_lists({}, {'stop_reason': 'operator_stop', 'score_completed': False})
    assert result['fix_list_fallback'] and result['score_delta'] is None


def test_stop_arriving_during_worker_presentation_interrupts_its_checks(monkeypatch):
    from packages.domain import rota_summary
    stopped = threading.Event()

    def project(source, result):
        source['_check_budget']()
        stopped.set()
        source['_check_budget']()
        pytest.fail('Presentation continued after operator stop')

    monkeypatch.setattr(rota_summary, '_generation_fix_lists', project)
    result = rota_summary.generation_fix_lists({}, {}, stopped=stopped.is_set)
    assert result['fix_list_fallback'] and '_fix_list_presentation_error' not in result


def test_worker_mid_solve_stop_reaches_succeeded_ready_within_grace(cp_sat_engine, monkeypatch, stop_clock):
    from apps.api import models as m
    from apps.api.db import transaction
    from packages.domain import rota_summary

    h, grace, update, kept, controls = cp_sat_engine, 5, {}, [], []
    workspace = h.shift(h.workspace(), people=[])['workspace']
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation',
        'generation_options': {'generation_stop_grace_seconds': grace}}, revision=workspace['revision'])['job']
    generate = cp_sat.generate

    def scheduler(data, **kwargs):
        report, search_control = kwargs['progress'], kwargs['search_control']

        def progress(row):
            report(row)
            if row['stage'] == 'improving' and not update:
                kept.extend((p['shift_id'], p['person_id']) for p in row['solution']['pairs'])
                current = h.get('coordinator', '/api/jobs/' + job['id'])['job']
                response = h.command('coordinator', 'job.stop_keep_best', {'reason': 'Local mid-solve regression'},
                                     job['id'], current['revision'])
                assert response['job']['search_control']['stop'] is True
                # Deliver the persisted command at the solver boundary; heartbeat cadence is a separate concern.
                update.update(response['job']['search_control'])

        control = solver_control.SolverControl(progress, kwargs['cancelled'],
            search_control=lambda: {**search_control(), **update})
        controls.append(control)
        return generate(data, **kwargs, _control=control)

    summarise = solver_summary.summarise

    def expire_summary(*args, **kwargs):
        control = controls[0]
        assert control.validated_result is not None and control.stopped
        assert control.stop_deadline == 100 + grace
        stop_clock[0] = control.stop_deadline
        return summarise(*args, **kwargs)

    monkeypatch.setattr(cp_sat, 'generate', scheduler)
    monkeypatch.setattr(solver_summary, 'summarise', expire_summary)
    monkeypatch.setattr(rota_summary, '_generation_fix_lists',
                        lambda *args: pytest.fail('Worker restarted display-only validation/scoring'))
    assert h.process_jobs()
    assert update['stop'] and controls[0].stopped
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job['id'])
        assert row.search_control['stop_requested_at']
        assert row.status == 'succeeded' and row.progress['stage'] == 'ready'
        assert row.result['status'] == 'candidate_feasible' and row.result['validation']['valid']
        assert row.result['additions'] and row.result['solver_status'] == 'FEASIBLE'
        assert row.result['search_stop']['reason'] == 'operator_stop'
        assert sorted((a['shift_id'], a['participant']['person_id']) for a in row.result['additions']) == sorted(kept)
        assert row.result['score'] is None and row.result['score_completed'] is False
        assert not row.result['optimality_proven']


@pytest.mark.parametrize('expire_in', [None, 'validation', 'summary'])
def test_coupled_projection_shares_the_same_stop_deadline(h, monkeypatch, stop_clock, expire_in):
    from packages.domain import group_generation
    from tests.test_generation_population import scope
    data, update = snapshot(h), {}
    data.update(planning_group_id='synthetic', lookahead='coupled',
        generation_scope=scope('person_ids', *data['people']),
        planning_population={s['id']: list(data['people']) for s in data['shifts']})

    def progress(row):
        if row['stage'] == 'improving':
            update['stop'] = True

    if expire_in is not None:
        original = group_generation.evaluate if expire_in == 'validation' else group_generation.summarise

        def expire(*args, **kwargs):
            stop_clock[0] = control.stop_deadline
            return original(*args, **kwargs)

        monkeypatch.setattr(group_generation, 'evaluate' if expire_in == 'validation' else 'summarise', expire)
    control = solver_control.SolverControl(progress, search_control=lambda: update)
    result = cp_sat.generate(data, 5, _control=control)
    assert update['stop'] and control.stopped
    assert control.stop_deadline == 100 + GenerationOptions().generation_stop_grace_seconds
    assert not result['optimality_proven']
    if expire_in == 'validation':
        assert result['status'] == 'needs_more_time' and 'validation' not in result
        assert result['additions'] == result['removals'] == []
    else:
        assert result['status'] == 'candidate_feasible' and result['validation']['valid']
        assert result['additions'] and result['planning_population'] == data['planning_population']
    if expire_in is not None:
        assert result['score'] is None and result['score_completed'] is False
