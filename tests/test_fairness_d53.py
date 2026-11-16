"""Owner D53: historical measurement, explicit comparisons and assigned supervision opportunity shares."""
from copy import deepcopy
from datetime import date
from fractions import Fraction

import pytest
from ortools.sat.python import cp_model
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import BalanceGoal, GenerationOptions, DEFAULT_FAIR_SHARE_WINDOW
from packages.domain import fairness as f
from packages.domain.cp_sat_goals import _dynamic_share_aims, add_score_goals
from packages.domain.cp_sat_score import ScoreModel, SCORE_SCALE
from tests.test_engine_fairness_review import A, B, duty, uid, world
from tests.test_fairness_review_backend import catalogue, line
from tests.test_supervision_balance import supervision_world, SUPERVISORS, TRAINEE
from tests.test_weighted_goals import STRENGTHS, assert_losses, report_losses


def test_fairness_plateau_legacy_name_is_readable_but_only_one_setting_is_written():
    old = 'fair_share_stop_without_improvement_seconds'
    options = GenerationOptions.model_validate({old: 45})
    assert options.fairness_plateau_seconds == 45
    assert old not in options.model_dump() and options.model_dump()['fairness_plateau_seconds'] == 45
    assert GenerationOptions.model_validate({old: 45, 'fairness_plateau_seconds': 45}) == options
    from packages.domain.solver_resume import resume_key
    old_snapshot = {'fingerprint': 'same', 'generation_defaults': {old: 45}}
    new_snapshot = {'fingerprint': 'same', 'generation_defaults': {'fairness_plateau_seconds': 15}}
    assert resume_key(old_snapshot, options, False, False) == resume_key(new_snapshot, GenerationOptions(), False, False)
    with pytest.raises(ValueError, match='disagree'):
        GenerationOptions.model_validate({old: 45, 'fairness_plateau_seconds': 15})


def test_defaults_and_metric_specific_share_contract():
    default = BalanceGoal(kind='balance_goal', metric='duties')
    assert {key: getattr(default, key) for key in DEFAULT_FAIR_SHARE_WINDOW} == DEFAULT_FAIR_SHARE_WINDOW
    assert default.share_by == 'available_days'
    assert BalanceGoal.model_json_schema()['properties']['period']['x-default-window'] == DEFAULT_FAIR_SHARE_WINDOW
    assert BalanceGoal(kind='balance_goal', metric='supervision').share_by == 'supervisable_sessions'
    assert BalanceGoal(kind='balance_goal', metric='workload', aim='workload_spread').period == 'rota'
    assert GenerationOptions().fairness_plateau_seconds == 15
    for share in ('equal', 'available_days', 'supervisable_sessions'):
        assert BalanceGoal(kind='balance_goal', metric='supervision', share_by=share).share_by == share
    with pytest.raises(ValueError):
        BalanceGoal(kind='balance_goal', metric='duties', share_by='supervisable_sessions')
    for values in ({'period': 'since'}, {'period': 'since', 'starts_on': '2026-01-01', 'ends_on': '2027-01-01'}):
        with pytest.raises(ValueError):
            BalanceGoal(kind='balance_goal', metric='duties', **values)


def test_default_window_resolves_catalogue_lines_and_preserves_frozen_defaults():
    data = world([duty(1, 1, [A])])
    source = catalogue([line(1, [A])])
    source['definition']['rule']['settings']['balance_goal'].pop('period', None)
    data['policies'] = [source]
    before = deepcopy(source)
    [goal] = f.balance_goals(data)
    assert {key: goal['definition']['rule'][key] for key in DEFAULT_FAIR_SHARE_WINDOW} == DEFAULT_FAIR_SHARE_WINDOW
    data['fairness_default_window'] = {'period': 'all_history'}
    [frozen] = f.balance_goals(data)
    assert frozen['definition']['rule']['period'] == 'all_history'
    assert frozen['definition']['rule']['period_months'] is None
    data['fairness_semantics'] = 1
    [legacy] = f.balance_goals(data)
    assert legacy['definition']['rule']['period'] == 'rota'
    assert source == before


@pytest.mark.parametrize('window', [dict(period='all_history'), dict(period='rota'), dict(period='month'),
    dict(period='rolling', period_days=30), dict(period='rolling', period_months=3)])
def test_explicit_window_is_preserved(window):
    goal = BalanceGoal(kind='balance_goal', metric='duties', **window)
    assert all(getattr(goal, key) == value for key, value in window.items())
    if 'period_months' not in window:
        assert goal.period_months is None


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('captured', [True, False])
def test_publication_reader_keeps_its_captured_or_pre_d55_default(h, captured, monkeypatch):
    from tests.test_rule_catalogue_api import approve, propose, line as api_line
    from apps.api.services import planning
    if not captured:
        build_snapshot = planning.publication_snapshot
        def old_snapshot(*args, **kwargs):
            snapshot = build_snapshot(*args, **kwargs)
            snapshot.pop('fairness_default_window')
            return snapshot
        # Create old-format evidence at publication time; never update an issued snapshot.
        monkeypatch.setattr(planning, 'publication_snapshot', old_snapshot)
    person = h.seed['people'][1]
    row = api_line(who={'conditions': [{'field': 'person_ids', 'values': [person]}]},
        numbers={'apply': {'value': None, 'level': 'medium'}}, settings={'balance_goal': {'metric': 'duties'}})
    saved = approve(h, propose(h, 'fair_share', [row])['record'])
    workspace = h.workspace(start='2026-11-01', end='2026-12-01')
    workspace = h.shift(workspace, start='2026-11-05T08:00:00+03:00',
        end='2026-11-05T12:00:00+03:00', people=[person])['workspace']
    published = h.publish(workspace)
    with transaction(h.app.state.engine) as db:
        before = deepcopy(db.get(m.Publication, published['publication_id']).snapshot)
    data = h.snapshot(workspace)
    [goal] = [p for p in f.balance_goals(data) if p['id'] == saved['id']]
    assert goal['definition']['rule']['period'] == ('rolling' if captured else 'all_history')
    assert goal['definition']['rule']['period_months'] == (12 if captured else None)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, published['publication_id']).snapshot == before


def test_legacy_pooling_flag_is_readable_but_only_explicit_comparison_pools():
    data = world([duty(1, 1, [A])])
    source = catalogue([line(1, [A]), line(2, [B])])
    source['definition']['balance_between_people'] = True
    data['policies'] = [source]
    before = deepcopy(source)
    assert len(f.goal_reports(data)) == 2
    assert source == before
    data['fairness_semantics'] = 1
    assert len(f.goal_reports(data)) == 1
    data['fairness_semantics'] = f.SEMANTICS
    source['definition']['rule']['settings']['balance_goal']['comparison'] = 'pooled'
    assert len(f.goal_reports(data)) == 1


def test_supervisable_sessions_count_eligible_attendance_once_and_recompute_after_assignment():
    data = supervision_world()
    goal = data['policies'][0]
    goal['definition']['rule']['share_by'] = 'supervisable_sessions'
    for shift in data['shifts'][2:]:
        shift['participants'] = [p for p in shift['participants'] if p['person_id'] != B]
    amounts = f.supervisable_amounts(data, goal, SUPERVISORS)
    assert amounts == dict(zip(SUPERVISORS, (8, 2, 8, 8)))
    data['shifts'][0]['participants'].append({**data['shifts'][0]['participants'][-1], 'person_id': uid(999)})
    assert f.supervisable_amounts(data, goal, SUPERVISORS) == amounts
    data['shifts'][1]['participants'] = [p for p in data['shifts'][1]['participants'] if p['person_id'] != TRAINEE]
    assert f.supervisable_amounts(data, goal, SUPERVISORS)[B] == 1
    data['grants'] = [g for g in data['grants'] if g['person_id'] != B]
    assert f.supervisable_amounts(data, goal, SUPERVISORS)[B] == 0


@pytest.mark.parametrize('adjustments', [{}, {A: {'mode': 'percent', 'value': 50}},
    {A: {'mode': 'percent', 'value': 0}, B: {'mode': 'percent', 'value': 0}},
    {A: {'mode': 'amount', 'value': -10}}, {A: {'mode': 'percent', 'value': 50, '_amount': 0.5}}])
@pytest.mark.parametrize('amounts', [(0, 0), (1, 3), (4, 2)])
def test_symbolic_basis_matches_canonical_shares_and_adjustments(adjustments, amounts):
    model = cp_model.CpModel()
    basis = {pid: model.new_int_var(0, 10, 'opportunities') for pid in (A, B)}
    total = model.new_int_var(0, 20, 'total')
    aims = _dynamic_share_aims(ScoreModel(model), total, 1, basis,
                              {pid: {'share_adjustment': adjustments.get(pid)} for pid in basis})
    for pid, value in zip(basis, amounts):
        model.add(basis[pid] == value)
    model.add(total == 8)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert model.validate() == ''
    assert solver.solve(model) == cp_model.OPTIMAL
    expected = f.expected_shares(8, dict(zip(basis, amounts)), adjustments)
    assert {pid: Fraction(solver.value(n), solver.value(d)) for pid, (n, d) in aims.items()} == expected


def test_supervision_symbolic_attendance_and_history_match_compiler_report():
    data = supervision_world()
    goal = data['policies'][0]
    goal['definition']['rule']['share_by'] = 'supervisable_sessions'
    data['fairness_history'] = {f.goal_identity(goal): {'people': {B: {
        'value': 4, 'available': 30, 'supervisable_sessions': 12}}}}
    model = cp_model.CpModel()
    participants = {(s['id'], p['person_id']): p for s in data['shifts'] for p in s['participants']}
    choices = {key: model.new_bool_var('attendance') for key in participants}
    # Compile with genuinely symbolic opportunities, then fix attendance and compare the resolved score.
    terms = add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS)
    for key, choice in choices.items():
        present = key[1] != B or key[0] in data['focus_ids'][:2]
        model.add(choice == present)
    for s in data['shifts'][2:]:
        s['participants'] = [p for p in s['participants'] if p['person_id'] != B]
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert model.validate() == ''
    assert solver.solve(model) == cp_model.OPTIMAL
    assert_losses([(w, solver.value(loss) / SCORE_SCALE) for w, loss in terms], report_losses(data))
    report = f.goal_report(data, goal)
    row = next(r for r in report['people'] if r['person_id'] == B)
    assert row['supervisable_sessions'] == 14
    assert row['expected'] == pytest.approx(12 * 14 / 38, abs=0.005)


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('window', [dict(period='all_history'), dict(period='since', starts_on='2026-10-01'),
    dict(period='rolling', period_days=61), dict(period='rolling', period_months=12)])
def test_newly_activated_line_counts_last_month_without_rewriting_publication(h, window):
    from tests.test_rule_catalogue_api import approve, propose, line as api_line
    person = h.seed['people'][1]
    old = h.workspace(start='2026-10-01', end='2026-11-01')
    old = h.shift(old, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[person])['workspace']
    published = h.publish(old)
    with transaction(h.app.state.engine) as db:
        before = deepcopy(db.get(m.Publication, published['publication_id']).snapshot)
    assert before['fairness_default_window'] == DEFAULT_FAIR_SHARE_WINDOW
    row = api_line(who={'conditions': [{'field': 'person_ids', 'values': [person]}]},
        numbers={'apply': {'value': None, 'level': 'medium'}}, starts_on='2026-11-06',
        settings={'balance_goal': {'metric': 'duties', **window}})
    saved = approve(h, propose(h, 'fair_share', [row])['record'])
    nxt = h.workspace(start='2026-11-06', end='2026-12-01')
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if context.execution_options.get('yield_per') == 1 and 'FROM publications' in statement:
            statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        data = h.snapshot(nxt)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    [goal] = [p for p in f.balance_goals(data) if p['id'] == saved['id']]
    [report] = f.goal_reports(data, [[goal]])
    assert report['people'][0]['prior'] == 1
    assert report['people'][0]['available_days'] > report['people'][0]['rota_days']
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Publication, published['publication_id']).snapshot == before
    # History is one streamed statement, irrespective of goal count or publication count.
    assert len([s for s in statements if 'ORDER BY publications.starts_on, publications.sequence' in s]) == 1


def test_greedy_dynamic_share_recomputes_opportunities_for_the_complete_trial():
    from packages.domain.fairness_distribution import people_marginal_cost
    data = supervision_world([A] * 4)
    goal = data['policies'][0]
    goal['definition']['rule'].update(share_by='supervisable_sessions', optimize_greedy=True)
    # B can attend only the new trainee session; adding the trainee changes the denominator.
    for shift in data['shifts'][:-1]:
        shift['participants'] = [p for p in shift['participants'] if p['person_id'] != B]
    shift = data['shifts'][-1]
    trainee = shift['participants'].pop()
    for pid in (A, B):
        participant = {**trainee, 'supervisor_person_id': pid}
        trial = deepcopy(data)
        trial['shifts'][-1]['participants'].append(participant)
        expected = f.report_value(f.goal_report(data, goal), STRENGTHS) - f.report_value(f.goal_report(trial, goal), STRENGTHS)
        assert people_marginal_cost(data, goal, shift, participant) == pytest.approx(expected)
