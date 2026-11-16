"""Search budgets are independent of preparation and mandatory final checks."""
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from packages.domain import cp_sat, solver_control, solver_summary, score, wish_reasons
from packages.domain.solver_control import SolverControl, Cancelled, TimeLimit
from packages.domain.validation import capabilities_for, categories_for
from packages.domain.time import aware
from tests.scheduler_departments import department
from tests.test_performance_budget import python_budget


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: now[0])
    return now


def _single_session_candidates(h):
    data, info = department(h, 7, people=500, days=2, rules='none', leave_rate=0)
    shift = min((s for s in data['shifts'] if s['assignment_type_id'] == info['types']['am']), key=lambda s: s['start'])
    data['shifts'], data['focus_ids'] = [shift], [shift['id']]
    assert len(data['people']) == 500
    return data


@pytest.mark.parametrize('interruption', ['cancel', 'elapsed_preparation'])
def test_generation_preparation_remains_cancellable_without_spending_search_budget(h, clock, monkeypatch, interruption):
    data = _single_session_candidates(h)
    before = deepcopy(data)
    stopped, visited = threading.Event(), []
    original = cp_sat.candidate_evidence

    def candidate(view, pid, shift):
        report = original(view, pid, shift)
        visited.append(pid)
        if len(visited) == 1:
            if interruption == 'cancel':
                stopped.set()
            else:
                clock[0] += 45
        return report

    original_solve = SolverControl.solve

    def search(*args, **kwargs):
        assert interruption != 'cancel', 'Cancellation must stop before native search'
        return original_solve(*args, **kwargs)

    monkeypatch.setattr(cp_sat, 'candidate_evidence', candidate)
    monkeypatch.setattr(SolverControl, 'solve', search)
    result = cp_sat.generate(data, 45, cancelled=stopped.is_set)
    if interruption == 'cancel':
        assert visited == [min(data['people'])]
        assert result['status'] == 'cancelled'
        assert result['additions'] == result['removals'] == []
        assert result['phase_timings']['search_seconds'] == 0
        assert not {'candidate_evidence', 'diagnostics', 'validation', 'summary'} & result.keys()
    else:
        assert len(visited) > 1
        assert result['status'] == 'candidate_feasible' and result['validation']['valid']
        assert result['phase_timings']['preparation_seconds'] == pytest.approx(45)
    assert data == before


@pytest.mark.parametrize('interruption', ['cancel', 'deadline'])
@pytest.mark.parametrize('path', ['direct', 'precomputed', 'capability'])
def test_shortage_interrupts_between_candidates_in_one_large_session(h, clock, monkeypatch, interruption, path):
    import packages.domain.candidate_evidence as evidence_module

    data = _single_session_candidates(h)
    [shift] = data['shifts']
    stopped, visited = threading.Event(), []
    control = SolverControl(cancelled=stopped.is_set)
    control.deadline = control.started + 45

    def observed(pid):
        visited.append(pid)
        if len(visited) == 1:
            if interruption == 'cancel':
                stopped.set()
            else:
                clock[0] = control.deadline

    evidence = []
    if path == 'direct':
        original = evidence_module.candidate_evidence

        def candidate(view, pid, duty):
            report = original(view, pid, duty)
            observed(pid)
            return report

        monkeypatch.setattr(evidence_module, 'candidate_evidence', candidate)
    else:
        def unexpected_candidate(*args, **kwargs):
            pytest.fail('Precomputed candidate evidence must be reused')

        monkeypatch.setattr(evidence_module, 'candidate_evidence', unexpected_candidate)

        class ObservedEvidence(dict):
            def __getitem__(self, key):
                value = super().__getitem__(key)
                if key == 'exclusions':
                    observed(self['person_id'])
                return value

        evidence = [{'person_id': pid, 'shift_id': shift['id'], 'exclusions': []} for pid in data['people']]
        if path == 'precomputed':
            evidence = [ObservedEvidence(row) for row in evidence]
        else:
            capability = h.seed['capability']
            shift['type_snapshot']['definition']['requirements'][0]['capabilities'] = [
                {'capability_id': capability, 'minimum': 1}]
            original = evidence_module.capabilities_for

            def capabilities(view, pid, duty, start, end):
                result = original(view, pid, duty, start, end)
                observed(pid)
                return result

            monkeypatch.setattr(evidence_module, 'capabilities_for', capabilities)

    before = deepcopy(data)
    before_evidence = [{key: deepcopy(value) for key, value in row.items()} for row in evidence]
    data['_check_budget'] = control.check
    with pytest.raises(Cancelled if interruption == 'cancel' else TimeLimit):
        evidence_module.direct_coverage_shortages(data, evidence, {shift['id']})
    assert visited == [next(iter(data['people']))]
    assert {key: value for key, value in data.items() if key != '_check_budget'} == before
    assert [{key: value for key, value in row.items()} for row in evidence] == before_evidence


def test_each_solve_uses_budget_minus_preparation_and_final_check_reserve(clock):
    control = SolverControl()
    control.deadline = control.started + 45
    control.reserve_seconds = 5
    clock[0] += 7
    limits = []
    solver = SimpleNamespace(parameters=SimpleNamespace(max_time_in_seconds=100))

    def solve(model, callback):
        limits.append(solver.parameters.max_time_in_seconds)
        clock[0] += 4
        return cp_model.FEASIBLE

    solver.solve = solve
    solver.stop_search = lambda: None
    assert control.solve(solver, None, 0) == cp_model.FEASIBLE
    solver.parameters.max_time_in_seconds = 100
    assert control.solve(solver, None, 1) == cp_model.FEASIBLE
    solver.parameters.max_time_in_seconds = 3
    assert control.solve(solver, None, 2) == cp_model.FEASIBLE
    assert limits == pytest.approx([45 - 7 - 5, 45 - 11 - 5, 3])

    clock[0] = control.deadline - control.reserve_seconds
    assert control.solve(solver, None, 3) == cp_model.UNKNOWN
    assert len(limits) == 3  # Do not start a solve that would consume the final-check reserve.
    clock[0] = control.deadline
    with pytest.raises(TimeLimit):
        control.check()


def test_deadline_interrupts_search_without_an_incumbent(clock):
    control = SolverControl()
    control.deadline = control.started + 45
    control.reserve_seconds = 5
    interrupted = threading.Event()

    class Searching:
        parameters = SimpleNamespace(max_time_in_seconds=100)

        def solve(self, model, callback):
            clock[0] = control.deadline - control.reserve_seconds
            assert interrupted.wait(2), 'Deadline did not interrupt search without a solution callback'
            return cp_model.UNKNOWN

        def stop_search(self):
            interrupted.set()

    assert control.solve(Searching(), None, 0) == cp_model.UNKNOWN
    assert control.first_solution_seconds is None


@pytest.mark.parametrize('configured_seconds', [300, 600])
@pytest.mark.parametrize('deterministic', [False, True])
def test_generation_uses_existing_options_budget(clock, monkeypatch, configured_seconds, deterministic):
    observed = []

    def generate(snapshot, budget, workers, control, options, *args):
        observed.append((budget, control.deadline))
        clock[0] += configured_seconds + 1 if deterministic else 7
        return {'status': 'model_ready', 'additions': [], 'removals': [], 'optimality_proven': False}

    monkeypatch.setattr(cp_sat, '_generate', generate)
    result = cp_sat.generate({'fingerprint': 'budget-source', 'generation_options': {'search_seconds': configured_seconds}},
                             deterministic=deterministic)
    assert observed == [(configured_seconds, None)]
    assert result['status'] == 'model_ready'



def test_score_completion_preserves_candidate_after_search_budget_is_spent(h, clock, monkeypatch):
    data, _ = department(h, 7, people=10, days=7, rules='full', leave_rate=.05)
    data['generation_options'] = {'shortlist_width': 1}
    controls, completed = [], []
    original = SolverControl.solve

    def solve(control, solver, model, tier):
        controls.append(control)
        if tier is None:
            completed.append(control.search_budget)
            control.search_spent = control.search_budget
            clock[0] += 45
        return original(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = cp_sat.generate(data, 45)
    assert completed == [45]
    assert all(control is controls[0] for control in controls)
    assert result['status'] == 'candidate_feasible'
    assert result['validation']['valid'] and result['additions']


@pytest.mark.parametrize('deterministic', [False, True])
def test_shortlist_preserves_mode_specific_budgets_and_full_candidate_fallback(h, clock, monkeypatch, deterministic):
    data, _ = department(h, 7, people=10, days=7, rules='full', leave_rate=.05)
    data['generation_options'] = {'shortlist_width': 1}
    attempts = []

    def solve(control, solver, model, tier):
        # No incumbent exists: even a previous expensive native startup must not suppress fallback search.
        control.incumbent_latency_seconds = 100
        budget = solver.parameters.max_deterministic_time if deterministic else solver.parameters.max_time_in_seconds
        attempts.append((control.context['candidates'], list(solver.parameters.subsolvers), budget))
        control.last_solve_deterministic_time = budget if deterministic else 0
        if not deterministic:
            clock[0] += budget
            control.search_spent += budget
        return cp_model.UNKNOWN

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = cp_sat.generate(data, 45, deterministic=deterministic)
    expected = [('shortlist', ['core']), ('all', ['core']), ('all', [])]
    if deterministic:
        expected.insert(1, ('shortlist', []))
    assert [(candidates, subsolvers) for candidates, subsolvers, _ in attempts] == expected
    assert [budget for _, _, budget in attempts] == pytest.approx([11.25] * 4 if deterministic else [22.5, 11.25, 11.25])
    assert result['status'] == 'needs_more_time'
    assert result['additions'] == result['removals'] == []
    assert not result['optimality_proven']


def test_shortlist_infeasibility_still_searches_every_candidate(h, clock, monkeypatch):
    data, _ = department(h, 7, people=10, days=7, rules='full', leave_rate=.05)
    data['generation_options'] = {'shortlist_width': 1}
    original, attempts = SolverControl.solve, []

    def solve(control, solver, model, tier):
        if tier is not None:
            attempts.append(control.context['candidates'])
            if control.context['candidates'] == 'shortlist':
                return cp_model.INFEASIBLE
        return original(control, solver, model, tier)

    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = cp_sat.generate(data, 45)
    assert attempts[0] == 'shortlist' and 'all' in attempts
    assert result['status'] == 'candidate_feasible' and result['validation']['valid']
    assert result['additions']


def test_arithmetic_completion_keeps_public_search_progress_and_plateau_state(clock):
    updates = []
    control = SolverControl(updates.append)
    control.deadline = control.started + 45
    control.reserve_seconds = 5
    control.stalled, control.stall_seconds = True, 1
    control.first_solution_seconds = 3
    model, solver = cp_model.CpModel(), cp_model.CpSolver()
    model.new_bool_var('derived')
    solver.parameters.max_time_in_seconds = 100
    assert control.solve(solver, model, None) in (cp_model.FEASIBLE, cp_model.OPTIMAL)
    assert solver.parameters.max_time_in_seconds == 40
    assert control.stalled and control.stall_seconds == 1
    assert control.first_solution_seconds == 3
    assert not updates


@pytest.mark.parametrize('case', ['short_slice', 'short_global_remaining', 'ample', 'deterministic', 'cancelled'])
def test_optional_search_admission_preserves_checked_incumbent(h, clock, monkeypatch, case):
    data, _ = department(h, 7, people=10, days=7, rules='full', leave_rate=.05)
    data['generation_options'] = {'shortlist_width': 1}
    before = deepcopy(data)
    deterministic = case == 'deterministic'
    controls, completions, improvements, scores, trimmed, cancelled = [], [], [], [], [], []
    original_solve, original_score = SolverControl.solve, score.schedule_score

    def solve(control, solver, model, tier):
        controls.append(control)
        if tier is not None and completions:
            improvements.append((control.context['candidates'], tier))
        status = original_solve(control, solver, model, tier)
        if tier is None and status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            completions.append(True)
            # Model a completed first incumbent whose observed initial search cost is four seconds.
            control.incumbent_latency_seconds = 4 if not deterministic else 1000
            if not deterministic and len(completions) == 1:
                # Six seconds globally gives the shortlist's next tier a slice below the observed four.
                remaining = 6 if case == 'short_slice' else 30
                control.search_spent = control.search_budget - remaining
        return status

    def progress(update):
        if case in {'short_global_remaining', 'cancelled'} and completions and not trimmed and update['stage'] == 'improving':
            control = controls[-1]
            # Time can pass after allocating a generous per-solve slice and before entering native search.
            control.search_spent = control.search_budget - 1
            trimmed.append(True)
            if case == 'cancelled':
                cancelled.append(True)

    def scoring(*args, **kwargs):
        result = original_score(*args, **kwargs)
        scores.append(result)
        return result

    monkeypatch.setattr(SolverControl, 'solve', solve)
    monkeypatch.setattr(score, 'schedule_score', scoring)
    result = cp_sat.generate(data, 45, deterministic=deterministic, progress=progress, cancelled=lambda: bool(cancelled))
    assert completions, 'Required arithmetic completion must still produce a full-model incumbent'
    if case == 'cancelled':
        assert result['status'] == 'cancelled'
        assert result['additions'] == result['removals'] == []
        assert 'validation' not in result and 'summary' not in result
        assert not result['optimality_proven']
        assert data == before
        return
    if case == 'short_slice':
        # The three-second shortlist slice is too short; widening still receives its six-second slice.
        assert improvements and all(candidates == 'all' for candidates, _ in improvements)
    else:
        assert bool(improvements) == (case in {'ample', 'deterministic'})
    if case == 'short_global_remaining':
        assert result['weighted_status'] == 'FEASIBLE'
        assert not result['optimality_proven']
    assert result['status'] == 'candidate_feasible'
    assert result['validation']['valid'] and result['validation']['error_count'] == 0
    assert result['additions'] and len(scores) == 3  # Before, canonical objective components, saved proposal.
    assert result['summary']['score_before'] == scores[0]
    assert result['summary']['score_saved'] == scores[2]
    assert all(control.deadline is None for control in controls)
    assert data == before


def test_validation_reserve_scales_measured_cost_with_projected_assignments():
    def shift(existing, requirements):
        return {'participants': [{} for _ in range(existing)],
                'type_snapshot': {'definition': {'requirements': requirements}}}

    data = {'shifts': [shift(2, [{'minimum': 1, 'target': 3, 'maximum': 4}]),
                       shift(0, [{'minimum': 1, 'target': 2}, {'minimum': 1}]),
                       shift(5, [{'minimum': 1}])]}
    # Three sessions, seven existing assignments, twelve projected assignments; three measured passes.
    assert cp_sat.validation_reserve(data, 2) == pytest.approx(3 * 2 * (3 + 12) / (3 + 7))
    assert cp_sat.validation_reserve(data, 4) == 2 * cp_sat.validation_reserve(data, 2)
    assert cp_sat.validation_reserve({'shifts': []}, 2) == 0


def test_before_score_is_measured_before_search_and_reused_for_public_summary(h, clock, monkeypatch):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    scores, reserves = [], []
    original_score, original_solve = score.schedule_score, SolverControl.solve

    def scoring(snapshot, *args, **kwargs):
        result = original_score(snapshot, *args, **kwargs)
        scores.append((sum(len(s['participants']) for s in snapshot['shifts']), result))
        clock[0] += .25
        return result

    def solve(control, *args, **kwargs):
        assert scores, 'The saved before score must be measured before any search'
        reserves.append(control.reserve_seconds)
        return original_solve(control, *args, **kwargs)

    monkeypatch.setattr(score, 'schedule_score', scoring)
    monkeypatch.setattr(SolverControl, 'solve', solve)
    result = cp_sat.generate(data, 45)
    assert result['status'] == 'candidate_feasible'
    assert scores[0][0] == 0 and all(count > 0 for count, _ in scores[1:])
    assert len(scores) == 3  # Before, objective components, saved proposal; no repeated before score.
    assert result['summary']['score_before'] == scores[0][1]
    assert reserves and all(value == pytest.approx(2 * cp_sat.validation_reserve(data, .25) / 3) for value in reserves)


def test_run_fact_caches_preserve_dated_authority_and_service_department_scope():
    data = {'organisation': {'timezone': 'UTC'},
            'types': {'a': {'department_id': 'a'}, 'b': {'department_id': 'b'}},
            'capabilities': {'skill': {}}, 'categories': {'rank': {}},
            'grants': [{'person_id': 'person', 'department_id': 'a', 'service_id': 'clinic',
                        'capability_id': 'skill', 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}],
            'memberships': [{'person_id': 'person', 'category_id': 'rank',
                             'starts_on': '2026-10-04', 'ends_on': '2026-10-05'}]}
    cached = {**data, '_categories_cache': {}, '_capabilities_cache': {}}
    for day, typ, services, expected in ((4, 'a', ['clinic'], {'skill'}), (4, 'a', ['other'], set()),
                                        (4, 'a', ['clinic', 'other'], set()), (4, 'b', ['clinic'], set()),
                                        (5, 'a', ['clinic'], set()), (4, 'a', ['clinic'], {'skill'})):
        start, end = aware(f'2026-10-{day:02}T08:00:00+00:00'), aware(f'2026-10-{day:02}T12:00:00+00:00')
        shift = {'assignment_type_id': typ, 'service_ids': services}
        assert capabilities_for(cached, 'person', shift, start, end) == capabilities_for(data, 'person', shift, start, end) == expected
        assert categories_for(cached, 'person', start, end) == categories_for(data, 'person', start, end) == ({'rank'} if day == 4 else set())


@pytest.mark.parametrize('phase', ['checking', 'preparation', 'search', 'validation', 'summary', 'scoring', 'wish_reasons'])
def test_elapsed_wall_time_in_any_generation_phase_keeps_checked_proposal(h, clock, monkeypatch, phase):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    triggered = []
    original_evaluate, original_summary = cp_sat.evaluate, solver_summary.summarise
    original_score = score.schedule_score
    original_reasons = wish_reasons.declined_wishes

    def expire():
        triggered.append(phase)
        clock[0] += 45

    def progress(update):
        if (phase == 'checking' and update['stage'] == 'checking'
                or phase == 'preparation' and update['stage'] == 'preparing'
                or phase == 'search' and update['stage'] == 'finding'):
            expire()

    def evaluate(snapshot, **kwargs):
        validation = original_evaluate(snapshot, **kwargs)
        if phase == 'validation' and any(shift['participants'] for shift in snapshot['shifts']):
            expire()
        return validation

    def summarise(*args, **kwargs):
        summary = original_summary(*args, **kwargs)
        if phase == 'summary':
            expire()
        return summary

    def scoring(*args, **kwargs):
        result = original_score(*args, **kwargs)
        if phase == 'scoring' and any(shift['participants'] for shift in args[0]['shifts']):
            expire()
        return result

    def reasons(*args, **kwargs):
        result = original_reasons(*args, **kwargs)
        if phase == 'wish_reasons':
            expire()
        return result

    monkeypatch.setattr(wish_reasons, 'declined_wishes', reasons)
    monkeypatch.setattr(cp_sat, 'evaluate', evaluate)
    monkeypatch.setattr(solver_summary, 'summarise', summarise)
    monkeypatch.setattr(score, 'schedule_score', scoring)
    result = cp_sat.generate(data, 45, progress=progress)
    assert triggered, phase
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['valid'] and result['summary']
    assert result['additions'] and not result['removals']
    assert all(not shift['participants'] for shift in data['shifts'])


def test_declined_wish_explanations_observe_generation_cancellation():
    def cancelled():
        raise Cancelled()

    data = {'organisation': {'timezone': 'UTC'}, 'shifts': [], 'wishes': [], '_check_budget': cancelled}
    with pytest.raises(Cancelled):
        wish_reasons.declined_wishes(data)


@pytest.fixture
def department_generation_wall_measurement(h, request):
    data, _ = department(h, 7, people=150, days=28, rules='full', leave_rate=0.05, clinics=9)
    assert len(data['people']) >= 150 and len(data['shifts']) == 532
    budget = 45
    began, cpu = time.perf_counter(), time.process_time()
    progress = []
    result = cp_sat.generate(data, budget, workers=2, progress=progress.append)
    timings = {'wall_seconds': time.perf_counter() - began, 'cpu_seconds': time.process_time() - cpu,
               **result['phase_timings'], 'status': result['status'], 'budget_seconds': budget,
               'input_fingerprint': data['fingerprint'], 'weighted_status': result.get('weighted_status'),
               'model_size': result.get('model_size'), 'last_progress': progress[-1] if progress else None}
    request.node.user_properties.append(('generation_wall_budget', timings))
    print('department generation wall budget', timings)
    return result, timings


def test_department_generation_reports_search_budget_separately(department_generation_wall_measurement):
    result, timings = department_generation_wall_measurement
    if result['status'] == 'candidate_feasible':
        assert result['validation']['error_count'] == 0, timings
        assert result['additions'], timings
    else:
        # Exhausted search keeps the immutable inputs available for continuation.
        assert result['status'] == 'needs_more_time', timings
        assert result['additions'] == result['removals'] == []
        assert 'validation' not in result and not result['optimality_proven']


@pytest.mark.wall_benchmark
def test_department_generation_search_cpu_budget(department_generation_wall_measurement):
    result, timings = department_generation_wall_measurement
    # One second covers cooperative checkpoints and solver shutdown; tracing scales only that Python allowance.
    assert timings['search_seconds'] <= timings['budget_seconds'] + python_budget(1), timings


def test_no_overtime_offer_skips_candidate_permission_resolution(h, monkeypatch):
    h.policy({'kind': 'allocation_preference', 'priority': 5},
             {'person_id': h.seed['people'][0]}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']

    def unexpected(*args, **kwargs):
        pytest.fail('An allocation preference cannot permit overtime without an overtime-offer rule')

    monkeypatch.setattr(cp_sat, 'allocation_overtime_allowed', unexpected)
    result = cp_sat.generate(h.snapshot(workspace), 10, deterministic=True)
    assert result['status'] == 'candidate_feasible', result
    assert result['validation']['error_count'] == 0
    assert result['additions'] and all(not row['participant']['overtime'] for row in result['additions'])
