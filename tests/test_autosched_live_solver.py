"""Incumbent telemetry uses the same assignment materialization and saved score as final proposals."""
import pytest
from packages.domain import cp_sat, score
from packages.domain.solver_control import SolverControl
from tests.scheduler_departments import apply


@pytest.mark.parametrize('different_footer_settings', [False, True])
def test_live_score_arrives_before_search_returns_and_matches_the_saved_footer(h, monkeypatch, different_footer_settings):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    if different_footer_settings:
        data['generation_options'] = {'scoring': {'weights': dict.fromkeys(score.COMPONENTS, 1)}}
        data['generation_defaults'] = {'scoring': {'weights': {name: int(name == 'coverage') for name in score.COMPONENTS}}}
    updates, during_search = [], []
    solve = SolverControl.solve

    def progress(update):
        updates.append(update)

    def searching(control, solver, model, tier):
        status = solve(control, solver, model, tier)
        if isinstance(tier, int) and control.improvements_found and not during_search:
            during_search.extend(row for row in updates if row.get('best_score'))
            assert during_search, control.score_metrics
        return status

    monkeypatch.setattr(SolverControl, 'solve', searching)
    result = cp_sat.generate(data, 3, progress=progress)
    assert result['status'] == 'candidate_feasible', result
    assert during_search and all(row['stage'] == 'improving' for row in during_search)
    assert result['live_score_metrics']['published'] >= 1 and result['live_score_metrics']['unavailable'] == 0
    assert during_search[-1]['best_score'] == score.progress_score(result['summary']['score_saved'])
    assert [row for row in updates if row.get('best_score')][-1]['best_score'] == score.progress_score(
        result['summary']['score_saved'])
    draft = apply(data, result)
    from packages.domain.validation import evaluate
    assert during_search[-1]['best_score'] == score.progress_score(score.schedule_score(
        draft, evaluate(draft), data.get('generation_defaults')))


def test_score_progress_rejects_stale_incumbents_and_throttles_updates(monkeypatch):
    clock, updates = [100.0], []
    monkeypatch.setattr('packages.domain.solver_control.time.monotonic', lambda: clock[0])
    control = SolverControl(updates.append)
    control.improvements_found = 2
    value = {'total': 75.0, 'components': dict.fromkeys(score.COMPONENTS, None)}
    assert not control.publish_score(1, value)
    assert control.publish_score(2, value)
    control.improvements_found = 3
    clock[0] += 1.99
    assert not control.publish_score(3, value)
    clock[0] += .01
    assert control.publish_score(3, value)
    assert len(updates) == 2 and all(row['best_score'] == value for row in updates)


def test_final_score_progress_ignores_stale_sequence_and_throttle(monkeypatch):
    clock, updates = [100.0], []
    monkeypatch.setattr('packages.domain.solver_control.time.monotonic', lambda: clock[0])
    control = SolverControl(updates.append)
    value = {'total': 75.0, 'components': dict.fromkeys(score.COMPONENTS, None)}
    control.improvements_found = 2
    assert control.publish_score(2, value)
    control.improvements_found = 3
    clock[0] += .01
    assert control.publish_score(1, value, final=True)
    assert len(updates) == 2 and control.best_score == value['total']
