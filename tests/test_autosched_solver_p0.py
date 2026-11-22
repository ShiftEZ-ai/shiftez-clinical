"""Completion expiry keeps only resumable decisions until a checked candidate is available."""
from copy import deepcopy
from weakref import ref

import pytest
from ortools.sat.python import cp_model

from packages.domain import cp_sat
from packages.domain.solver_control import SolverControl
from tests.test_solver_search_budget import store_checkpoints


@pytest.mark.parametrize('deterministic', [False, True])
def test_score_completion_expiry_is_bounded_reported_and_resumes_to_public_validation(h, monkeypatch, deterministic):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    snapshot = h.snapshot(workspace)
    snapshot['shifts'][0]['type_snapshot']['definition']['requirements'][0].update(minimum=0, target=1, maximum=1)
    snapshot['generation_options'] = {'shortlist_width': 0, 'diagnostic_seconds': 2}
    saved, updates, completions = [], [], []
    allowance = 100 if deterministic else 2
    solve = SolverControl.solve

    def expire(control, solver, model, tier):
        if tier is None:
            assert solver.parameters.max_time_in_seconds == allowance
            assert updates[-1]['stage'] in {'finding', 'improving', 'searching'}
            completions.append(True)
            control.completion_budget = allowance
            control.completion_spent = allowance
            control.last_solve_deterministic_time = 0.0
            return cp_model.UNKNOWN
        assert not completions, 'Search continued after no score-completion allowance remained'
        return solve(control, solver, model, tier)

    with monkeypatch.context() as patch:
        patch.setattr(SolverControl, 'solve', expire)
        partial = cp_sat.generate(snapshot, 2, progress=updates.append, checkpoint=store_checkpoints(saved),
                                  deterministic=deterministic)
    assert completions and partial['status'] == 'needs_more_time'
    assert partial['score_completion_seconds'] == allowance
    assert not partial['additions'] and not partial['removals'] and 'validation' not in partial
    assert saved[0]['has_solution'] and saved[0]['hint']
    assert saved[0]['result']['status'] == 'candidate_with_gaps' and saved[0]['result']['checkpointed']
    initial_hint = deepcopy(saved[0]['hint'])

    def rebuilt(*args, **kwargs):
        raise AssertionError('A completion timeout discarded its prepared model')

    resumed_tiers = []

    def resume(control, solver, model, tier):
        if not resumed_tiers:
            assert tier is None, 'Continuation searched before completing its saved decisions'
            assert [list(model.proto.variables[index].domain) for index in range(len(initial_hint))] == [
                [value, value] for value in initial_hint]
        resumed_tiers.append(tier)
        return solve(control, solver, model, tier)

    monkeypatch.setattr(cp_sat, '_prepare', rebuilt)
    monkeypatch.setattr(SolverControl, 'solve', resume)
    resumed = cp_sat.generate(snapshot, 2, resume_state=saved[0], checkpoint=store_checkpoints(saved),
                              deterministic=deterministic)
    assert initial_hint and resumed_tiers and resumed['preparation_reused']
    assert resumed['status'] == 'candidate_feasible' and resumed['validation']['valid']
    assert resumed['score_completion_seconds'] >= 0


@pytest.mark.parametrize('later_result', ['solve', 'unknown', 'error'])
def test_completed_solver_is_released_before_next_search_without_changing_results(h, monkeypatch, later_result):
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'day'}, severity='advisory')
    snapshot = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    snapshot['shifts'][0]['type_snapshot']['definition']['requirements'][0].update(minimum=0, target=1, maximum=1)
    snapshot['generation_options'] = {'shortlist_width': 0, 'diagnostic_seconds': 2}
    solve = SolverControl.solve

    def run(require_release):
        completed, calls = [], []

        def observe(control, solver, model, tier):
            name = control.context['objective_names'][tier] if tier is not None else None
            calls.append(name)
            if name in {'desired_coverage', 'weighted'}:
                assert len(completed) == tier
                if require_release:
                    assert all(previous() is None for previous in completed), (
                        'Unused score-completion solver survived into the next search')
            if name == 'weighted':
                if later_result == 'unknown':
                    return cp_model.UNKNOWN
                if later_result == 'error':
                    raise RuntimeError('later search failed')
            status = solve(control, solver, model, tier)
            if tier is None:
                completed.append(ref(solver))
            return status

        with monkeypatch.context() as patch:
            patch.setattr(SolverControl, 'solve', observe)
            if later_result == 'error':
                with pytest.raises(RuntimeError, match='later search failed'):
                    cp_sat.generate(snapshot, 2)
                result = None
            else:
                result = cp_sat.generate(snapshot, 2)
        # Desired cover is now a separate search with its own score completion before weighted search.
        assert calls == ['coverage', None, 'desired_coverage', None, 'weighted']
        return result

    expected = run(False)
    actual = run(True)
    if later_result == 'error':
        assert actual is expected is None
    else:
        assert actual['status'] == expected['status'] == 'candidate_feasible'
        assert actual['validation']['valid']
        assert len(actual['additions']) == 1 and not actual['removals']
        for key in ('additions', 'removals', 'validation', 'objectives', 'weighted_status',
                    'optimality_proven', 'compiled_component_values', 'objective_components'):
            assert actual[key] == expected[key], key
        assert actual['summary']['score_saved'] == expected['summary']['score_saved']
