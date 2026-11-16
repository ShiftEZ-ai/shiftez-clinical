"""Real incumbent diffs through the existing solver progress callback."""
from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from packages.domain import solver_control
from packages.domain.cp_sat import generate
from packages.domain.solver_control import SolverControl
from packages.domain.time import aware


def test_diff_preserves_fixed_assignments_and_counts_peak_session_headcount_deficit():
    model = cp_model.CpModel()
    choices = {('session', person): model.new_bool_var(person) for person in ('fixed', 'old', 'new')}
    control = SolverControl()
    baseline = {('session', 'fixed'), ('session', 'old')}
    cover = sum(choices.values())
    control.live_solutions(choices, baseline, [('session', cover, 3), ('session', cover, 4)])
    event = control.live_event([1, 0, 1], 7, 2, 0)
    assert event['adds'] == [{'shift_id': 'session', 'person_id': 'new'}]
    assert event['removes'] == [{'shift_id': 'session', 'person_id': 'old'}]
    assert event['open_places'] == 2  # Peak deficit, rather than adding overlapping intervals.
    assert event['warnings'] is None  # Advisory rules have not been checked by shared validation yet.
    assert event['objective'] == 7 and event['bound'] == 2
    assert aware(event['at']).utcoffset().total_seconds() == 0
    assert not event['truncated'] and event['kind'] == 'improvement' and event['seq'] == 1


def test_live_coverage_keeps_integer_constants_for_sessions_without_candidates():
    control = SolverControl()
    control.live_solutions({}, set(), [('empty', 0, 3), ('constant', 2, 3)])
    event = control.live_event([], 0, 0, 0)
    assert event['open_places'] == 4
    assert event['adds'] == event['removes'] == []


@pytest.mark.parametrize('changes', [200, 201])
def test_large_diff_refetch_signal_uses_total_changed_pairs(changes):
    model = cp_model.CpModel()
    choices = {('session', str(person)): model.new_bool_var(str(person)) for person in range(changes)}
    baseline = {('session', str(person)) for person in range(100)}
    control = SolverControl()
    control.live_solutions(choices, baseline, [])
    solution = [int(person >= 100) for person in range(changes)]
    event = control.live_event(solution, 1, 0, 0)
    assert event['truncated'] is (changes > 200)
    if changes > 200:
        assert event['adds'] == event['removes'] == []
    else:
        assert len(event['adds']) == len(event['removes']) == 100
    assert control.live_solution == {'seq': event['seq'], 'pairs': [
        {'shift_id': 'session', 'person_id': str(person)} for person in sorted(range(100, changes), key=str)]}
    # Truncation still advances the baseline, so the next event is an incremental diff.
    unchanged = control.live_event(solution, 1, 0, 1)
    assert unchanged['adds'] == unchanged['removes'] == []
    assert not unchanged['truncated']


@pytest.mark.parametrize('rate', [1, 3, 8, 100])
def test_callback_caps_reads_and_emits_cumulative_diff_without_losing_checkpoints_or_count(monkeypatch, rate):
    clock, current, reads = [0.0], [[1, 0]], []
    monkeypatch.setattr(solver_control.time, 'monotonic', lambda: clock[0])

    class Callback:
        objective_value = 1
        best_objective_bound = 0

        @property
        def response_proto(self):
            reads.append(1)
            return SimpleNamespace(solution=current[0])

    monkeypatch.setattr(solver_control.cp_model, 'CpSolverSolutionCallback', Callback)
    model = cp_model.CpModel()
    choices = {('session', person): model.new_bool_var(person) for person in ('a', 'b')}
    progress, saved = [], []
    control = SolverControl(progress.append, checkpoint_solution=saved.append)
    control.live_solutions(choices, set(), [], rate)
    interval = 1 / min(rate, 8)

    def solve(model, callback):
        callback.on_solution_callback()
        current[0] = [0, 0]
        clock[0] = interval / 2
        callback.on_solution_callback()
        current[0] = [0, 1]
        clock[0] = interval
        callback.on_solution_callback()
        return cp_model.FEASIBLE

    solver = SimpleNamespace(solve=solve, stop_search=lambda: None)
    assert control.solve(solver, model, 0) == cp_model.FEASIBLE
    events = [event for update in progress for event in update.get('events', [])]
    assert len(events) == 2 and [event['seq'] for event in events] == [1, 2]
    assert events[1]['adds'] == [{'shift_id': 'session', 'person_id': 'b'}]
    assert events[1]['removes'] == [{'shift_id': 'session', 'person_id': 'a'}]
    assert len(saved) == len(reads) == control.improvements_found == 3
    assert [update['solution']['seq'] for update in progress if 'events' in update] == [1, 2]
    assert [update['improvements_found'] for update in progress] == [1, 2, 3]
    control.stage('ready')
    assert progress[-1]['improvements_found'] == 3
    # Without checkpointing, a suppressed event never reads the full native solution.
    clock[0] = 0
    reads.clear()
    control = SolverControl()
    control.live_solutions(choices, set(), [], rate)
    assert control.solve(solver, model, 0) == cp_model.FEASIBLE
    assert len(reads) == 2 and control.improvements_found == 3


def test_cp_sat_progress_exposes_actual_assignments_and_retains_locked_work(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    populated = next(shift for shift in data['shifts'] if shift['participants'])
    populated['locked'] = True
    baseline = {(shift['id'], person['person_id']) for shift in data['shifts'] for person in shift['participants']}
    progress = []
    result = generate(data, 10, feasibility_only=True, progress=progress.append)
    assert result['status'] == 'candidate_feasible', result
    events = [event for update in progress for event in update.get('events', [])]
    assert events and result['improvements_found'] >= len(events)
    selected = set(baseline)
    for event in events:
        assert not event['truncated']
        selected.difference_update((pair['shift_id'], pair['person_id']) for pair in event['removes'])
        selected.update((pair['shift_id'], pair['person_id']) for pair in event['adds'])
        assert baseline <= selected  # Protected duties never animate away.
    expected = baseline | {(pair['shift_id'], pair['participant']['person_id']) for pair in result['additions']}
    assert selected == expected
    assert events[-1]['open_places'] == 0 and events[-1]['warnings'] is None
