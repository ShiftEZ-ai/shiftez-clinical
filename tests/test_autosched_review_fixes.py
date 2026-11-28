"""Public explanations and progress remain useful across score completion."""
from copy import deepcopy
import pytest

from packages.domain import cp_sat
from packages.domain import solver_resume
from packages.domain.rota_summary import rota_summary
from packages.domain.solver_progress import precheck_issues
from packages.domain.solver_control import SolverControl
from packages.domain.validation import evaluate
from tests.test_solver_search_budget import store_checkpoints


STAGE_PHASE = {**dict.fromkeys(('queued',), 0), **dict.fromkeys(('checking',), 1),
    **dict.fromkeys(('preparing',), 2), **dict.fromkeys(('finding', 'improving', 'searching'), 3),
    **dict.fromkeys(('scoring', 'validating', 'summarizing'), 4), 'ready': 5}


def assert_monotonic(updates):
    stages = [update['stage'] for update in updates]
    phases = [STAGE_PHASE[stage] for stage in stages]
    assert phases == sorted(phases), stages


def scoring_snapshot(h):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    data['shifts'][0]['type_snapshot']['definition']['requirements'][0].update(minimum=0, target=1, maximum=1)
    data['generation_options'] = {'shortlist_width': 0, 'diagnostic_seconds': 2}
    return data


def test_in_search_completions_preserve_search_stage_and_run_progress_is_monotonic(h, monkeypatch):
    updates, completions = [{'stage': 'queued'}], []
    solve = SolverControl.solve

    def observe(control, solver, model, tier):
        if tier is None:
            completions.append(control._stage)
        return solve(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', observe)
    result = cp_sat.generate(scoring_snapshot(h), 2, progress=updates.append)
    assert result['status'] == 'candidate_feasible', result
    assert completions and set(completions) <= {'finding', 'improving', 'searching'}
    assert updates[-1]['stage'] == 'ready'
    assert_monotonic(updates)


@pytest.mark.parametrize('stopped', [False, True])
def test_resume_progress_is_monotonic_and_only_final_completion_publishes_scoring(h, stopped):
    data, saved = scoring_snapshot(h), []
    first = cp_sat.generate(data, 2, checkpoint=store_checkpoints(saved))
    assert first['status'] == 'candidate_feasible' and saved[0]['has_solution']
    state = deepcopy(saved[0])
    state.pop('result', None)
    updates = [{'stage': 'queued'}]
    resumed = cp_sat.generate(data, 2, resume_state=state,
        search_control=lambda: {'stop': stopped}, progress=updates.append)
    assert resumed['status'] == 'candidate_feasible' and resumed['preparation_reused']
    assert any(update['stage'] == 'scoring' for update in updates) == stopped
    if stopped:
        assert not any(update['stage'] in {'finding', 'improving', 'searching'} for update in updates)
    assert_monotonic(updates)


def test_requirements_conflict_summary_preserves_canonical_causes_hints_and_groups(h):
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    data['eligibilities'] = []
    checked = evaluate(data)
    expected = rota_summary(checked)
    cause = next(finding for finding in expected['findings'] if finding['code'] == 'COVERAGE_GAP')
    assert cause['cause'] and cause['fix_hints'] and expected['cause_groups']
    result = cp_sat.generate(data, 2)
    assert result['status'] == 'requirements_conflict', result
    public = result['summary']['rota_summary']
    finding = next(finding for finding in public['findings'] if finding['code'] == 'COVERAGE_GAP')
    assert finding['cause'] == cause['cause']
    assert finding['fix_hints'] == cause['fix_hints']
    assert public['cause_groups'] == expected['cause_groups']


def test_resumed_precheck_issues_have_the_same_global_sort_and_cap_as_fresh_checks(monkeypatch):
    protected = [{'code': f'P_{index:03}'} for index in reversed(range(60))]
    outside = [{'code': f'O_{index:03}'} for index in reversed(range(30))]
    shortages = [{'code': f'U_{index:03}'} for index in reversed(range(10))]
    prepared = {'result': {'issues': protected, 'outside_scope_issues': outside},
        'data': {}, 'before_data': {}, 'shortages': shortages,
        'diagnostic_domains': {}, 'diagnostic_objectives': {}, 'objective_order': [], 'objectives': []}
    expected = precheck_issues(protected, 'protected')
    expected = precheck_issues(outside, 'outside_scope', previous=expected)
    expected = precheck_issues(shortages, 'unresolved', previous=expected)
    monkeypatch.setattr(solver_resume, 'restore', lambda *args, check=None: prepared)

    def search(snapshot, budget, workers, control, *args, **kwargs):
        control.stage('preparing')
        return {'status': 'needs_more_time'}

    monkeypatch.setattr(cp_sat, '_search', search)
    updates = []
    result = cp_sat.generate({'fingerprint': 'frozen-inputs'}, 2, resume_state={'hint': []}, progress=updates.append)
    assert result['status'] == 'needs_more_time'
    issues = updates[0]['precheck_issues']
    assert issues == expected and len(issues) == 50
    assert [(issue['classification'], issue['code']) for issue in issues] == sorted(
        (issue['classification'], issue['code']) for issue in issues)


def test_legacy_preparation_clears_stored_live_score_before_search(h, monkeypatch):
    from packages.contracts.schemas import GenerationOptions

    data = scoring_snapshot(h)
    prepared = cp_sat._prepare(data, 2, 1, SolverControl(),
        GenerationOptions.model_validate(data['generation_options']), False, False)
    prepared['score_program'].pop('goal_cost_checks')
    monkeypatch.setattr(solver_resume, 'restore', lambda *args, check=None: prepared)
    updates = []
    result = cp_sat.generate(data, 2, resume_state={'hint': []}, progress=updates.append)
    assert result['status'] == 'candidate_feasible' and result['preparation_reused']
    assert updates[0]['stage'] == 'preparing' and 'best_score' in updates[0]
    assert updates[0]['best_score'] is None
    assert all(update.get('best_score') is None for update in updates
               if update['stage'] in {'finding', 'improving', 'searching'})
