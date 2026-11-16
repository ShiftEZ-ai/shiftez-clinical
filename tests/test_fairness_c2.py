"""Fair share line dials use one resolved audience and preserve the total."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from packages.contracts.schemas import PolicyDefinition, ScoringSettings
from packages.domain.fairness import expected_shares, goal_reports
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from tests.test_engine_fairness_review import A, B, policy


@pytest.mark.parametrize('factor', [0, 0.1, 0.8, 1])
def test_desired_cover_dial_is_shared_by_score_and_compiled_objective(factor):
    from ortools.sat.python import cp_model
    from packages.domain.cp_sat_score import coverage_component, SCORE_SCALE
    from tests.test_schedule_score import coverage, data, shift

    model = cp_model.CpModel()
    loss, present = coverage_component(model, [(1, 1, (2,), 60_000_000)], factor)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
    assert solver.value(present) == 1
    snapshot = data([shift('duty', 4, ['a'])])
    checked = {'coverage': [coverage('duty', 4, 1, 1)], 'findings': [{
        'code': 'COVERAGE_TARGET', 'severity': 'warning', 'rule_id': None, 'shift_id': 'duty',
        'start': snapshot['shifts'][0]['start'], 'end': snapshot['shifts'][0]['end'],
        'detail': {'target': 2, 'available': 1}}]}
    scored = schedule_score(snapshot, checked, {'scoring': {'desired_cover_factor': factor}})
    assert scored['components']['coverage']['value'] == pytest.approx(1 - factor)
    assert solver.value(loss) / SCORE_SCALE == pytest.approx(factor, abs=0.000002)


def test_fair_share_single_session_excludes_people_who_cannot_work_it(h):
    from packages.domain.cp_sat import generate

    a, b, c = h.seed['people']
    snapshot = h.snapshot(h.shift(h.workspace())['workspace'])
    snapshot['people'][b]['active'] = snapshot['people'][c]['active'] = False
    goal = policy({'metric': 'duties', 'period': 'rota'})
    goal['department_id'] = snapshot['workspace']['department_id']
    snapshot['policies'].append(goal)
    original = deepcopy(snapshot)
    report = next(r for r in goal_reports(snapshot) if r['metric'] == 'duties')
    assert [(r['person_id'], r['expected']) for r in report['people']] == [(a, 1)]
    validation = evaluate(snapshot)
    assert not [f for f in validation['findings'] if f['code'] == 'BALANCE_GOAL']
    assert schedule_score(snapshot, validation)['components']['fairness']['value'] == 1
    result = generate(snapshot, 10, deterministic=True)
    assert result['status'] == 'candidate_feasible'
    report = next(r for r in result['validation']['goals'] if r['metric'] == 'duties')
    assert {r['person_id'] for r in report['people']} == {a}
    assert snapshot == original


@pytest.mark.parametrize('adjustments,expected', [
    ({A: {'mode': 'percent', 'value': 50}}, {A: 2, B: 4}),
    ({A: {'mode': 'amount', 'value': 3}}, {A: 4, B: 2}),
    ({A: {'mode': 'amount', 'value': -10}}, {A: 0, B: 6}),
    ({pid: {'mode': 'amount', 'value': -10} for pid in (A, B)}, {A: 3, B: 3}),
])
def test_share_adjustments_preserve_total_and_never_go_negative(adjustments, expected):
    shares = expected_shares(6, {A: 7, B: 7}, adjustments)
    assert shares == expected and sum(shares.values()) == 6


def test_scoring_administration_has_technical_bounds_instead_of_percentage_caps():
    settings = ScoringSettings.model_validate({'weights': {'fairness': 1000},
        'strengths': {'low': 1, 'medium': 101, 'high': 1000, 'critical': 10000}, 'wish_debt_cap': 9})
    assert settings.weights.fairness == 1000 and settings.wish_debt_cap == 9
    rule = PolicyDefinition.model_validate({'severity': 'advisory',
        'rule': {'kind': 'allocation_preference', 'priority': 1000}})
    assert rule.rule.priority == 1000
    for bad in ({'weights': {'fairness': 1_000_001}}, {'wish_debt_cap': 1_000_001},
                {'preferences_mean_weight': 0, 'preferences_worst_weight': 0}):
        with pytest.raises(ValidationError):
            ScoringSettings.model_validate(bad)


def test_share_dials_do_not_silently_change_explicit_targets_or_pair_minima():
    with pytest.raises(ValidationError, match='shared totals'):
        PolicyDefinition.model_validate({'severity': 'advisory', 'rule': {'kind': 'balance_goal',
            'metric': 'duties', 'aim': 'target', 'amount': 5, 'share_adjustment': {'value': 50}}})


@pytest.mark.parametrize('measure', ['duties', 'reserved_hours'])
def test_seeded_department_workload_matches_the_c1_formula(measure, h):
    """Freeze the former C1 budget formula before replacing it with a visible Fair share line."""
    from tests.test_fairness import leave, session

    a, b, c = h.seed['people']
    leave(h, c, 4, 11)
    w = h.workspace()
    for day, pid in enumerate((a, a, a, b), start=4):
        w = session(h, w, day, [pid])
    snapshot = h.snapshot(w)
    snapshot['generation_options'] = {'balance_measure': measure}
    # C1: demand four duties, two eligible people, indivisible grid one duty. A is one above two.
    demand, loads, limit = 4, (3, 1), 2
    excess = [max(0, 2 * (load - limit)) for load in loads]
    before = (1 - sum(excess) / (2 * demand)) * (1 - max(excess) / demand / 2)
    report = next(r for r in goal_reports(snapshot) if r['metric'] == 'workload')
    assert report['value'] == before == 0.5625
    assert {r['person_id'] for r in report['people']} == {a, b}
    validation = evaluate(snapshot)
    after = schedule_score(snapshot, validation, {'balance_measure': measure})
    assert after['components']['fairness']['value'] == before
    assert not [f for f in validation['findings'] if f['code'] == 'BALANCE_GOAL']
    assert sum(item['points'] for item in after['why']) == pytest.approx(100 - after['score'], abs=0.11)


@pytest.mark.parametrize('singleton', [False, True])
def test_seeded_workload_person_standing_has_complete_source_metadata(h, singleton):
    from tests.test_fairness import leave

    person = h.seed['people'][0]
    if singleton:
        for other in h.seed['people'][1:]:
            leave(h, other, 4, 11)
    workspace = h.shift(h.workspace())['workspace']
    internal = next(goal for goal in goal_reports(h.snapshot(workspace)) if goal['metric'] == 'workload')
    assert internal['score_present'] is not singleton
    assert {'workload_spread', 'value', 'score_present'} <= internal.keys()
    result = h.get('coordinator', f'/api/people/{person}/fairness?workspace_id=' + workspace['id'])
    [workload] = [goal for goal in result['goals'] if goal['metric'] == 'workload']
    assert {'workload_spread', 'value', 'score_present'}.isdisjoint(workload)
    assert workload['line_id']
    assert all(source['goal_id'] and source['line_id']
               for row in workload['people'] for source in row['sources'])
    [row] = workload['people']
    original = next(row for row in internal['people'] if row['person_id'] == person)
    assert row['person_id'] == person
    assert (row['planned'], row['actual'], row['expected'], row['sources']) == (
        original['planned'], original['actual'], original['expected'], original['sources'])
    assert workload['score'] == internal['score']


def test_reduced_candidate_check_keeps_target_findings_without_department_workload(h):
    person = h.seed['people'][0]
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'target', 'amount': 10},
                    {'person_id': person}, severity='advisory')
    snapshot = h.snapshot(h.shift(h.workspace())['workspace'])
    checked = evaluate({**snapshot, 'person_rule_subjects': {person}})
    assert [g['policy_id'] for g in checked['goals']] == [goal['id']]
    assert any(f['code'] == 'BALANCE_GOAL' and f['rule_id'] == goal['id'] for f in checked['findings'])
    assert any(g['metric'] == 'workload' for g in evaluate(snapshot)['goals'])


def test_empty_next_rota_preserves_only_historically_eligible_goal_audience(h):
    from tests.test_fairness import leave

    a, b, c = h.seed['people']
    leave(h, c, 4, 31)
    goal = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'month'}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[a])['workspace']
    h.publish(workspace)
    later = h.snapshot(h.workspace(start='2026-10-11', end='2026-10-18'))
    report = next(report for report in goal_reports(later) if report['policy_id'] == goal['id'])
    rows = {row['person_id']: row for row in report['people']}
    assert set(rows) == {a, b}
    assert rows[a]['prior'] == 1 and rows[b]['prior'] == 0


def test_empty_next_rota_keeps_pair_deficits_without_historical_exposure(h):
    from tests.test_fairness import leave

    a, b, c = h.seed['people']
    leave(h, c, 4, 31)
    goal = h.policy({'kind': 'balance_goal', 'metric': 'pair_exposure', 'aim': 'min_per_pair',
                     'amount': 1, 'period': 'month', 'partners': {
                         'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [a, b]}]}},
                    severity='advisory')
    h.publish(h.shift(h.workspace(), people=[a])['workspace'])
    later = h.snapshot(h.workspace(start='2026-10-11', end='2026-10-18'))
    assert later['fairness_history'][goal['id']]['pairs'] == {a: {}, b: {}}
    validation = evaluate(later)
    report = next(report for report in validation['goals'] if report['policy_id'] == goal['id'])
    assert {(row['person_id'], row['partner_id']) for row in report['pairs']} == {(a, b), (b, a)}
    assert all(row['actual'] == 0 and row['status'] == 'below' for row in report['pairs'])
    assert report['score'] == 0
    deficits = [finding for finding in validation['findings']
                if finding['code'] == 'BALANCE_PAIR' and finding['rule_id'] == goal['id']]
    assert {finding['person_id'] for finding in deficits} == {a, b}
    assert len(deficits) == 2


def test_score_reuses_matching_validation_goal_reports(h, monkeypatch):
    snapshot = h.snapshot(h.shift(h.workspace())['workspace'])
    validation = evaluate(snapshot)

    def unexpected_report(*args, **kwargs):
        pytest.fail('An unchanged validated snapshot must reuse its goal reports')

    monkeypatch.setattr('packages.domain.score.goal_reports', unexpected_report)
    assert schedule_score(snapshot, validation)['components']['fairness']['value'] == 1


def test_score_recomputes_goal_reports_when_workload_measure_changes(h, monkeypatch):
    from packages.domain import score

    snapshot = h.snapshot(h.shift(h.workspace())['workspace'])
    validation = evaluate(snapshot)
    called = []
    reports = score.goal_reports

    def observed_report(data):
        called.append(data['_score_options']['balance_measure'])
        return reports(data)

    monkeypatch.setattr(score, 'goal_reports', observed_report)
    schedule_score(snapshot, validation, {'balance_measure': 'duties'})
    assert called == ['duties']


def _dated_off_fairness(h, off_first):
    from tests.test_fairness import leave, session
    from tests.test_rule_catalogue_guard import catalogue, line

    a, b, c = h.seed['people']
    leave(h, c, 4, 18)
    workspace = h.workspace(start='2026-10-04', end='2026-10-18')
    for day in (5, 12):
        workspace = session(h, workspace, day, [a, b])
    general = line(numbers={'apply': {'value': None, 'level': 'medium'}})
    disabled = line(who={'conditions': [{'field': 'person_ids', 'values': [a]}]},
                    starts_on='2026-10-10', numbers={'apply': {'value': None, 'level': 'off'}})
    source = catalogue('fair_share', *([disabled, general] if off_first else [general, disabled]),
        settings={'balance_goal': {'metric': 'duties', 'period': 'rota', 'aim': 'equal_share',
                                  'share_by': 'available_days', 'tolerance_percent': 0, 'tolerance_units': 0}})
    source['department_id'] = h.seed['department_id']
    source['definition']['rule']['catalogue_version'] = 2
    snapshot = h.snapshot(workspace)
    snapshot['policies'] = [source]
    return snapshot, a, b


@pytest.mark.parametrize('off_first', [False, True])
@pytest.mark.parametrize('legacy', [False, True])
def test_dated_off_fair_share_preserves_precedence_without_counting_disabled_sources(h, off_first, legacy):
    from datetime import date
    from packages.domain import fairness as f

    snapshot, a, b = _dated_off_fairness(h, off_first)
    if legacy:
        snapshot['fairness_semantics'] = 1
    resolved = f.balance_goals(snapshot)
    assert {p['definition']['strength'] for p in resolved} == {'medium', 'off'}
    [group] = f.goal_groups(snapshot)
    assert [p['definition']['strength'] for p in group] == ['medium']
    [report] = f.goal_reports(snapshot)
    rows = {row['person_id']: row for row in report['people']}
    if not legacy:
        assert set(rows) == {b}
        assert (rows[b]['planned'], rows[b]['available_days'], rows[b]['expected']) == (2, 14, 2)
        assert f.planned(snapshot, f.comparison_goal(resolved))[0] == {b: 2}
        assert schedule_score(snapshot, evaluate(snapshot))['components']['fairness']['value'] == 1
        return
    assert set(rows) == {a, b}
    assert (rows[a]['planned'], rows[a]['available_days']) == (1, 6)
    assert (rows[b]['planned'], rows[b]['available_days']) == (2, 14)
    assert (rows[a]['expected'], rows[b]['expected']) == (0.9, 2.1)
    # Explicit comparison callers also share the enabled-source interval/count predicate.
    comparison = f.comparison_goal(resolved)
    assert f.goal_intervals(snapshot, comparison, a, date(2026, 10, 4), date(2026, 10, 18)) == [
        (date(2026, 10, 4), date(2026, 10, 10))]
    assert f.planned(snapshot, comparison)[0] == {a: 1, b: 2}
    assert all(source['definition']['strength'] != 'off' for source in f.goal_sources(comparison, a))
    score = schedule_score(snapshot, evaluate(snapshot))['components']['fairness']['value']
    # Zero-tolerance count goals accept the attainable floor/ceiling band (F4).
    assert score == 1
    without_disabled_duty = deepcopy(snapshot)
    without_disabled_duty['shifts'][1]['participants'] = [
        p for p in without_disabled_duty['shifts'][1]['participants'] if p['person_id'] != a]
    assert schedule_score(without_disabled_duty, evaluate(without_disabled_duty))['components']['fairness']['value'] == score


@pytest.mark.parametrize('off_first', [False, True])
def test_compiled_fair_share_ignores_dated_off_attendance_and_keeps_public_score(h, off_first):
    from ortools.sat.python import cp_model
    from packages.domain import fairness as f
    from packages.domain.cp_sat_goals import add_score_goals
    from packages.domain.cp_sat_score import SCORE_SCALE

    snapshot, a, _ = _dated_off_fairness(h, off_first)
    expected = schedule_score(snapshot, evaluate(snapshot))['components']['fairness']['value']
    for disabled_attendance in (0, 1):
        model = cp_model.CpModel()
        participants = {(s['id'], p['person_id']): p for s in snapshot['shifts'] for p in s['participants']}
        choices = {key: model.new_bool_var('attendance') for key in participants}
        for (sid, pid), choice in choices.items():
            model.add(choice == (disabled_attendance if sid == snapshot['shifts'][1]['id'] and pid == a else 1))
        presence = []
        terms = add_score_goals(model, snapshot, choices, participants, {}, {}, set(snapshot['focus_ids']),
                                f.strengths_of(snapshot), presence=presence)
        assert len(terms) == len(presence) == 1
        _, loss = terms[0]
        model.minimize(loss)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 2
        assert solver.solve(model) == cp_model.OPTIMAL, model.validate()
        assert solver.value(presence[0]) == 1
        assert 1 - solver.value(loss) / SCORE_SCALE == pytest.approx(expected, abs=0.00005)


def test_off_only_fair_share_has_no_comparison_or_score(h):
    from packages.domain import fairness as f

    snapshot, _, _ = _dated_off_fairness(h, False)
    snapshot['policies'][0]['definition']['rule']['lines'] = [
        line for line in snapshot['policies'][0]['definition']['rule']['lines'] if line['numbers']['apply']['level'] == 'off']
    assert f.goal_groups(snapshot) == [] and f.goal_reports(snapshot) == []
    assert schedule_score(snapshot, evaluate(snapshot))['components']['fairness']['value'] is None
