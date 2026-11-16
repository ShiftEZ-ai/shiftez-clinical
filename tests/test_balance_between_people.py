"""Across-line fairness uses one audience while retaining dated line scopes and histories."""
from copy import deepcopy
from uuid import UUID

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from packages.contracts.schemas import PolicyDefinition
from packages.domain import fairness as f
from packages.domain.cp_sat_goals import add_balance_goals
from packages.domain.cp_sat_quotas import add_quotas
from packages.domain.score import schedule_score
from tests.test_engine_fairness_review import A, B, POLICY, T, duty, policy as primitive_policy, world
from tests.test_rule_catalogue_api import approve, propose


def catalogue(people=(A, B), *, balance=True, metric='duties', period='rota', amounts=None, levels=('medium', 'medium')):
    lines = [{'id': str(UUID(int=20 + i)), 'who': {'conditions': [
        {'field': 'person_ids', 'values': [pid]}]},
        'numbers': {('target' if amounts else 'apply'): {'value': amounts[i] if amounts else None, 'level': levels[i]}}}
        for i, pid in enumerate(people)]
    definition = PolicyDefinition.model_validate({'balance_between_people': balance,
        'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': lines,
                 'settings': {'balance_goal': {'metric': metric, 'period': period, 'comparison': 'pooled' if balance else 'within_group',
                                               'aim': 'target' if amounts else 'equal_share'}}}}).model_dump(mode='json')
    return {'id': POLICY, 'name': 'Synthetic across-line fairness', 'revision': 1,
            'department_id': world()['workspace']['department_id'], 'status': 'active', 'active': True,
            'starts_on': '2020-01-01', 'ends_on': None, 'definition': definition}


def reports(data):
    found = []
    result = f.evaluate_goals(data, lambda code, message, **detail: found.append({'code': code, **detail}))
    return result, found


@pytest.mark.parametrize('invalid', [None, 0, 1, 'false', 'true'])
def test_balance_switch_is_strict_and_additive(invalid):
    definition = {'severity': 'advisory', 'rule': {'kind': 'balance_goal', 'metric': 'duties'}}
    assert PolicyDefinition.model_validate(definition).balance_between_people is True
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({**definition, 'balance_between_people': invalid})


@pytest.mark.parametrize('balance', [True, False])
def test_named_person_lines_compare_together_by_default_or_separately(balance):
    data = world([duty(i, i, [A]) for i in range(1, 5)])
    data['policies'] = [catalogue(balance=balance)]
    raw = f.balance_goals(data)
    assert len(raw) == 2 and len({f.goal_identity(goal) for goal in raw}) == 2
    result, findings = reports(data)
    assert len(result) == (1 if balance else 2)
    rows = {row['person_id']: row for report in result for row in report['people']}
    assert {pid: row['expected'] for pid, row in rows.items()} == ({A: 2, B: 2} if balance else {A: 4, B: 0})
    assert {finding['person'] for finding in findings} == ({A, B} if balance else set())
    score = schedule_score(data, {'findings': [], 'coverage': [], 'goals': result})
    assert score['components']['fairness']['value'] == pytest.approx(0.375 if balance else 1)


def test_grouped_goals_preserve_each_lines_history_targets_and_strength():
    data = world([duty(1, 15, [A])], first='2026-10-11', last='2026-10-18')
    data['policies'] = [catalogue(period='month', amounts=(2, 4), levels=('critical', 'low'))]
    raw = f.balance_goals(data)
    data['fairness_history'] = {f.goal_identity(goal): {'people': {pid: {'value': prior, 'available': 10}}}
                                for goal, pid, prior in zip(raw, (A, B), (3, 0))}
    data['fairness_history'][f.goal_identity(f.comparison_goal(raw))] = {'people': {
        A: {'value': 3, 'available': 10}, B: {'value': 0, 'available': 10}}}
    [report] = f.goal_reports(data)
    rows = {row['person_id']: row for row in report['people']}
    assert (rows[A]['prior'], rows[A]['planned'], rows[A]['expected']) == (3, 1, 1.1)
    assert rows[B]['expected'] == 2.19
    assert rows[A]['strengths'] == ['critical'] and rows[B]['strengths'] == ['low']
    weights = f.report_weights(report, {'critical': 30, 'low': 1})
    assert weights == {A: 30, B: 1}
    assert report['score'] == round(100 * f.share_value(report['people'], weights), 1)


def test_goal_groups_separate_metrics_periods_and_count_buckets():
    data = world([])
    policy = catalogue()
    lines = policy['definition']['rule']['lines']
    lines[1]['settings'] = {'balance_goal': {'metric': 'hours'}}
    data['policies'] = [policy]
    assert len(f.goal_groups(data)) == 2
    lines[1]['settings'] = {'balance_goal': {'metric': 'duties', 'period': 'month'}}
    assert len(f.goal_groups(data)) == 2
    lines[1].pop('settings')
    for line in lines:
        line['where'] = {'conditions': [{'field': 'service_ids', 'values': [A, B], 'count': 'each'}]}
    assert len(f.goal_groups(data)) == 2
    assert {len(group) for group in f.goal_groups(data)} == {2}


@pytest.mark.parametrize('balance', [True, False])
def test_solver_balances_named_lines_using_the_same_audience(balance):
    shifts = [duty(i, i) for i in range(1, 5)]
    data = world(shifts)
    data['policies'] = [catalogue(balance=balance, levels=('high', 'low'))]
    model = cp_model.CpModel()
    choices = {(shift['id'], pid): model.new_bool_var('attendance') for shift in shifts for pid in (A, B)}
    participants = {(shift['id'], pid): {'person_id': pid, 'start': shift['start'], 'end': shift['end']}
                    for shift in shifts for pid in (A, B)}
    for (sid, pid), variable in choices.items():
        model.add(variable == (pid == A))
    terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']),
                                 {'low': 1, 'medium': 3, 'high': 10, 'critical': 30})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert (solver.objective_value > 0) is balance


def test_overlapping_where_lines_count_a_duty_once_in_report_and_solver():
    data = world([duty(1, 1, [A], services=[A])])
    policy = catalogue()
    lines = policy['definition']['rule']['lines']
    lines[0]['settings'] = {'balance_goal': {'assignment_type_ids': [T]}}
    extra = deepcopy(lines[0])
    extra.update(id=str(UUID(int=24)), settings={'balance_goal': {'service_ids': [A]}})
    lines.append(extra)
    data['policies'] = [policy]
    [report] = f.goal_reports(data)
    assert next(row['planned'] for row in report['people'] if row['person_id'] == A) == 1
    model = cp_model.CpModel()
    shift = data['shifts'][0]
    participant = shift['participants'][0]
    variable = model.new_bool_var('attendance')
    model.add(variable == 1)
    terms, _ = add_balance_goals(model, data, {(shift['id'], A): variable}, {(shift['id'], A): participant},
                                 {}, {}, set(data['focus_ids']), {'medium': 3})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    counted = [solver.value(model.get_int_var_from_proto_index(index)) for index, variable in enumerate(model.proto.variables)
               if variable.name == 'goal_planned']
    assert counted == [1, 0]


def test_different_weekend_definitions_keep_their_own_counts_when_lines_reorder():
    data = world([duty(1, 5, [B])])  # Monday
    policy = catalogue(metric='weekends')
    lines = policy['definition']['rule']['lines']
    lines[1]['settings'] = {'balance_goal': {'weekend_start': 0, 'weekend_days': 2}}
    data['policies'] = [policy]
    before = f.goal_reports(data)
    lines.reverse()
    after = f.goal_reports(data)
    counts = lambda reports: {row['person_id']: row['planned'] for report in reports for row in report['people']}
    assert counts(before) == counts(after) == {A: 0, B: 1}


def test_comparison_source_lookup_skips_unrelated_named_people(monkeypatch):
    data = world([duty(1, 1, [A])])
    base = primitive_policy({'metric': 'duties'})
    sources = [{**base, 'definition': {**base['definition'], 'scope': {'person_id': str(UUID(int=i))}}}
               for i in range(1, 1001)]
    comparison = f.comparison_goal(sources)
    assert len(f.goal_sources(comparison, A)) == len(f.goal_sources(comparison, B)) == 1
    visits = []
    monkeypatch.setattr('packages.domain.validation.policy_applies',
                        lambda *args: visits.append(args[1]) or True)
    shift = data['shifts'][0]
    assert f.goal_applies(data, comparison, A, shift, shift['start'], shift['end'])
    assert len(visits) == 1


@pytest.mark.parametrize('matching_first', [True, False])
def test_primitive_goal_what_filter_is_checked_for_each_same_day_session(matching_first):
    shifts = [duty(i + 1, 1, [A], services=[A] if (i == 0) == matching_first else [B]) for i in range(2)]
    data = world(shifts)
    data['policies'] = [primitive_policy({'metric': 'duties', 'service_ids': [A]}, {'person_id': A})]
    model = cp_model.CpModel()
    choices = {(shift['id'], A): model.new_constant(1) for shift in shifts}
    participants = {(shift['id'], A): shift['participants'][0] for shift in shifts}
    terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), {'medium': 3})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert [solver.value(model.get_int_var_from_proto_index(index)) for index, variable in enumerate(model.proto.variables)
            if variable.name == 'goal_planned'] == [1]


def test_quota_target_groups_ignore_line_and_strength_but_keep_period_metric_bucket():
    row = {'policy_id': POLICY, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties',
           'line': 0, 'line_id': A, 'slot': 'target', 'comparison': 'target', 'strength': 'high'}
    other = {**row, 'line': 1, 'line_id': B, 'strength': 'low'}
    assert f.quota_target_key(row) == f.quota_target_key(other)
    assert f.quota_target_key({**row, 'balance_between_people': False}) != f.quota_target_key({**other, 'balance_between_people': False})
    for change in ({'metric': 'hours'}, {'starts_on': '2026-10-02'}, {'count_bucket': [['where.0', A]]}):
        assert f.quota_target_key(row) != f.quota_target_key({**row, **change})


def test_quota_targets_keep_each_line_strength_in_the_rules_component():
    data = world([duty(1, 1, [A])])
    data['policies'] = [catalogue()]
    rows = [{'policy_id': POLICY, 'policy_name': 'Quota target', 'person_id': pid,
             'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'duties', 'line': i,
             'line_id': pid, 'slot': 'target', 'comparison': 'target', 'amount': 2, 'actual': actual,
             'severity': 'advisory', 'strength': level, 'status': 'above_target' if actual else 'below_target'}
            for i, (pid, actual, level) in enumerate(((A, 3, 'high'), (B, 0, 'low')))]
    validation = {'findings': [], 'coverage': [], 'quota_report': {'rows': rows}}
    grouped = schedule_score(data, validation)['components']['rules']['value']
    assert grouped == pytest.approx(5 / 11, abs=0.0001)
    rows[0]['strength'], rows[1]['strength'] = 'low', 'high'
    changed = schedule_score(data, validation)['components']['rules']['value']
    assert changed == pytest.approx(0.5 / 11, abs=0.0001)
    for row in rows:
        row['balance_between_people'] = False
    assert schedule_score(data, validation)['components']['rules']['value'] == changed


@pytest.mark.parametrize('balance', [True, False])
def test_api_round_trip_dashboard_and_publication_trend_use_grouped_goals(h, balance):
    a, b, _ = h.seed['people']
    definition = catalogue((a, b), balance=balance)['definition']
    rule = definition['rule']
    proposed = propose(h, rule['id'], rule['lines'], settings=rule['settings'], balance_between_people=balance)['record']
    assert proposed['definition']['balance_between_people'] is balance
    saved = approve(h, proposed)
    assert saved['definition']['balance_between_people'] is balance
    w = h.workspace()
    for day in (4, 5, 6, 7):
        w = h.shift(w, f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00', people=[a])['workspace']
    dashboard = h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'])
    goals = [goal for goal in dashboard['goals'] if goal['policy_id'] == saved['id']]
    assert len(goals) == (1 if balance else 2)
    assert {row['expected'] for goal in goals for row in goal['people']} == ({2} if balance else {0, 4})
    h.publish(w)
    dashboard = h.get('coordinator', '/api/fairness/goals')
    goals = [goal for goal in dashboard['goals'] if goal['policy_id'] == saved['id']]
    assert all(goal['trend'] for goal in goals)
    mine = h.get('staff', '/api/fairness/goals')
    assert all(row['person_id'] == a for goal in mine['goals'] for row in goal['people'])
    assert all(goal['score'] is None and goal['trend'] == [] for goal in mine['goals'])


@pytest.mark.parametrize('balance', [True, False])
@pytest.mark.parametrize('amounts', [(2, 2), (2.25, 1.75)])
def test_quota_targets_share_dashboard_validation_score_and_solver_weights(h, balance, amounts):
    from fractions import Fraction
    from math import floor

    from packages.domain.cp_sat_score import ScoreModel, SCORE_SCALE
    from packages.domain.validation import evaluate

    a, b, _ = h.seed['people']
    definition = catalogue((a, b), balance=balance, amounts=amounts, levels=('high', 'low'))['definition']
    definition['rule'].update(id='duties_per_month', settings={'block_quota': {
        'starts_on': '2026-11-01', 'ends_on': '2026-12-01', 'window_alignment': 'calendar_month'}})
    rule = definition['rule']
    saved = approve(h, propose(h, rule['id'], rule['lines'], settings=rule['settings'], balance_between_people=balance)['record'])
    # A complete month after policy activation preserves fractional targets; partial windows prorate.
    w = h.workspace(start='2026-11-01', end='2026-12-01')
    for day in (4, 5, 6, 7):
        w = h.shift(w, f'2026-11-{day:02}T08:00:00+03:00', f'2026-11-{day:02}T12:00:00+03:00', people=[a])['workspace']
    data = h.snapshot(w)
    validation = evaluate(data)
    quota_rows = [row for row in validation['quota_report']['rows'] if row['policy_id'] == saved['id']]
    assert {row['person_id']: row['amount'] for row in quota_rows} == dict(zip((a, b), amounts))
    assert not any(row.get('proration') for row in quota_rows)
    goals = [goal for goal in validation['goals'] if goal['policy_id'] == saved['id']]
    assert len(goals) == (1 if balance else 2) and all(goal['quota_target'] for goal in goals)
    assert len([finding for finding in validation['findings'] if finding['code'].startswith('QUOTA_')]) == 2
    dashboard = h.get('coordinator', '/api/fairness/goals?workspace_id=' + w['id'])
    cards = [goal for goal in dashboard['goals'] if goal['policy_id'] == saved['id']]
    assert {card['goal_id']: card['score'] for card in cards} == {goal['goal_id']: goal['score'] for goal in goals}
    score = schedule_score(data, validation)
    fallback = schedule_score(data, {**validation, 'goals': [goal for goal in validation['goals'] if not goal.get('quota_target')]})
    assert score['components'] == fallback['components']
    target_a, target_b = (Fraction(str(amount)) for amount in amounts)
    # Fractional targets give partial credit, weighted by each line's strength.
    expected = (10 * min(1, abs(4 - target_a) / max(target_a, 1))
                + min(1, target_b / max(target_b, 1))) / 11
    assert score['components']['rules']['value'] == round(float(1 - expected), 4)
    model = cp_model.CpModel()
    choose_a = model.new_bool_var('choose_person_a_for_all_duties')
    by_person = {a: [], b: []}
    for shift in data['shifts']:
        for pid, variable in ((a, choose_a), (b, 1 - choose_a)):
            participant = {**shift['participants'][0], 'person_id': pid}
            by_person[pid].append((shift, participant, None, None, variable))
    items = []
    assert add_quotas(model, data, by_person, lambda *_: pytest.fail('Legacy quota penalty used'), score_items=items) == []
    loss = ScoreModel(model).mean([(weight, value) for weight, value, _ in items])
    model.minimize(loss)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.value(loss) == floor(SCORE_SCALE * expected)
    if amounts == (2.25, 1.75):
        assert solver.value(choose_a) == 1
    assert not any(variable.name == 'quota_share' for variable in model.proto.variables)


def test_quota_share_terms_aggregate_each_person_without_changing_line_obligations(h):
    from packages.domain.validation import evaluate

    a, b, _ = h.seed['people']
    definition = catalogue((a, b), amounts=(2, 2), levels=('high', 'medium'))['definition']
    lines = definition['rule']['lines']
    lines[0]['where'] = {'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}
    extra = deepcopy(lines[0])
    extra.update(id=str(UUID(int=24)), where={'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][1]]}]})
    extra['numbers']['target']['level'] = 'low'
    lines.append(extra)
    settings = {'block_quota': {'starts_on': '2026-11-01', 'ends_on': '2026-12-01', 'window_alignment': 'calendar_month'}}
    saved = approve(h, propose(h, 'duties_per_month', lines, settings=settings)['record'])
    # This share comparison uses complete monthly obligations rather than a prorated one-week rota.
    w = h.workspace(start='2026-11-01', end='2026-12-01')
    for day in range(4, 10):
        w = h.shift(w, f'2026-11-{day:02}T08:00:00+03:00', f'2026-11-{day:02}T12:00:00+03:00',
                    people=[a if day < 8 else b], service=0 if day < 8 else 1)['workspace']
    data = h.snapshot(w)
    validation = evaluate(data)
    quota_rows = [row for row in validation['quota_report']['rows'] if row['policy_id'] == saved['id']]
    assert len(quota_rows) == 3 and all(row['amount'] == 2 and not row.get('proration') for row in quota_rows)
    [report] = [goal for goal in validation['goals'] if goal['policy_id'] == saved['id']]
    assert report['score'] == 100
    assert {row['person_id']: (row['actual'], row['expected']) for row in report['people']} == {a: (4, 4), b: (2, 2)}
    assert len([finding for finding in validation['findings'] if finding['code'].startswith('QUOTA_')]) == 2
    model = cp_model.CpModel()
    by_person = {a: [], b: []}
    for shift in data['shifts']:
        for participant in shift['participants']:
            by_person[participant['person_id']].append((shift, participant, None, None, model.new_constant(1)))
    penalties = []
    def penalise(expression, policy, worst=1, bound=1):
        strengths = {'high': 10, 'medium': 3, 'low': 1}
        weight = policy.get('_fairness_weight', strengths[policy['definition']['strength']])
        scaled = model.new_int_var(0, 10**9, 'penalty')
        model.add(scaled * worst >= 1000 * expression)
        penalties.append(weight * scaled)
    assert add_quotas(model, data, by_person, penalise) == []
    model.minimize(sum(penalties))
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    shares = [solver.value(model.get_int_var_from_proto_index(index)) for index, variable in enumerate(model.proto.variables)
              if variable.name == 'quota_share']
    assert shares and set(shares) == {0}
    assert solver.objective_value > 0  # individual per-line targets are still obligations


@pytest.mark.parametrize('lead_off', [False, True], ids=['department-leads', 'lead-line-off'])
def test_overlapping_goal_history_counts_published_duties_once_and_keeps_source_keys(h, lead_off):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.snapshots import domain_snapshot
    from .test_team_lead import anyone_leads, disable_lead_balance, lead_goal

    a, b, _ = h.seed['people']
    definition = catalogue((a, b), period='month')['definition']
    lines = definition['rule']['lines']
    lines[0]['settings'] = {'balance_goal': {'assignment_type_ids': [h.seed['type']]}}
    extra = deepcopy(lines[0])
    extra.update(id=str(UUID(int=24)), settings={'balance_goal': {'service_ids': [h.seed['services'][0]]}})
    lines.append(extra)
    saved = approve(h, propose(h, 'fair_share', lines, settings=definition['rule']['settings'])['record'])
    w = h.shift(h.workspace(), people=[a])['workspace']
    h.publish(w)
    typ = anyone_leads(h)
    workspace = h.shift(h.workspace(start='2026-10-11', end='2026-10-18'), typ=typ,
                        start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00', people=[])['workspace']
    with transaction(h.app.state.engine) as db:
        later = domain_snapshot(db, db.get(m.Workspace, workspace['id']))
    lead_key = f.goal_identity(lead_goal(later, typ))
    if lead_off:
        disable_lead_balance(later, typ)
    sources = [goal for goal in f.balance_goals(later) if goal['id'] == saved['id']
               and goal['definition']['rule']['metric'] == 'duties']
    assert len(sources) == 3
    assert all(f.goal_identity(source) in later['fairness_history'] for source in sources)
    [report] = [goal for goal in f.goal_reports(later) if goal['policy_id'] == saved['id'] and goal['metric'] == 'duties']
    assert next(row['prior'] for row in report['people'] if row['person_id'] == a) == 1
    assert report['goal_id'] in later['fairness_history']
    if lead_off:
        assert f.goal_people(later, lead_goal(later, typ)) == {'who': [], 'partners': []}
        assert lead_key in later['fairness_history']


def test_real_solver_quota_targets_use_each_line_strength_once(h):
    from packages.domain.cp_sat import generate

    a, b, _ = h.seed['people']
    lines = catalogue((a, b), amounts=(2, 2), levels=('high', 'medium'))['definition']['rule']['lines']
    lines[0]['where'] = {'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][0]]}]}
    extra = deepcopy(lines[0])
    extra.update(id=str(UUID(int=24)), where={'conditions': [{'field': 'service_ids', 'values': [h.seed['services'][1]]}]})
    extra['numbers']['target']['level'] = 'low'
    lines.append(extra)
    settings = {'block_quota': {'starts_on': '2026-11-01', 'ends_on': '2026-12-01', 'window_alignment': 'calendar_month'}}
    approve(h, propose(h, 'duties_per_month', lines, settings=settings)['record'])
    # The expected per-line score is for full monthly targets, unchanged by partial-window proration.
    w = h.workspace(start='2026-11-01', end='2026-12-01')
    for day in range(4, 9):
        w = h.shift(w, f'2026-11-{day:02}T08:00:00+03:00', f'2026-11-{day:02}T12:00:00+03:00',
                    people=[a if day < 7 else b], service=0 if day < 7 else 1)['workspace']
    data = h.snapshot(w)
    for shift in data['shifts']:
        shift['locked'] = True
    # The high line is half missed, the low line fully missed and the medium line met: loss=(10*.5+1)/14.
    result = generate(data, time_budget_seconds=5, workers=1, deterministic=True)
    assert result['status'] == 'candidate_feasible'
    assert [part['name'] for part in result['objectives']] == ['coverage', 'weighted']
    assert result['objective_components']['rules']['value'] == pytest.approx(4 / 7, abs=0.0001)
    assert result['compiled_component_values']['rules'] == pytest.approx(4 / 7, abs=0.000001)


@pytest.mark.parametrize('amount', [27.6786, 1e-9])
def test_department_decimal_quota_share_has_linear_bounds_and_preserves_critical_weights(monkeypatch, amount):
    from packages.domain import cp_sat_quotas as quotas
    from packages.domain.time import aware

    people = [str(UUID(int=2000 + i)) for i in range(150)]
    shifts = [duty(i, i % 31 + 1) for i in range(531)]
    data = world(shifts)
    data['people'] = {pid: {'display_name': pid, 'tags': []} for pid in people}
    data['memberships'] = [{'person_id': pid, 'department_id': data['workspace']['department_id'], 'starts_on': '2020-01-01'}
                           for pid in people]
    definition = PolicyDefinition.model_validate({'severity': 'advisory', 'strength': 'critical', 'rule': {
        'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'metric': 'workload_credit',
        'comparison': 'target', 'amount': amount}}, context={'historical': True}).model_dump(mode='json')
    policy = {**primitive_policy({'metric': 'duties'}), 'name': 'Synthetic decimal quota',
              'revision': 1, 'definition': definition}
    data['policies'] = [policy]
    rows = [{'policy_id': policy['id'], 'person_id': pid, 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
             'metric': 'workload_credit', 'comparison': 'target', 'amount': amount * (2 if amount == 1e-9 and index >= 75 else 1),
             'severity': 'advisory', 'strength': 'critical'} for index, pid in enumerate(people)]
    monkeypatch.setattr(quotas, 'evaluate_quotas', lambda *args: {'complete': True, 'rows': rows})
    monkeypatch.setattr(quotas, 'quota_contribution', lambda data, policy, pid, shift, participant, members, a, b: {
        'duty': 1, 'presence': [(aware(shift['start']), aware(shift['end']))]})
    for shift in shifts:
        shift['type_snapshot']['definition']['workload_credit_per_hour'] = 1
    model = cp_model.CpModel()
    by_person = {pid: [(shift, {}, None, None, model.new_constant(int(pid != people[-1]))) for shift in shifts]
                 for pid in people}
    penalties = []
    assert add_quotas(model, data, by_person, lambda expression, policy, *args: penalties.append(expression)) == []
    # About150 reporting rows, not any product of people*possible-session soft-share constraints.
    assert len(model.proto.variables) < 900 and len(model.proto.constraints) < 900
    assert model.validate() == ''
    model.minimize(sum(penalties))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    shares = [solver.value(model.get_int_var_from_proto_index(index)) / 1000
              for index, variable in enumerate(model.proto.variables) if variable.name == 'quota_share']
    assert len(shares) == 150 and shares[-1] > 0  # critical strength enters before quantisation
    actuals = [2124] * 149 + [0]
    if amount == 1e-9:
        assert any(variable.name == 'quota_share_quantity' for variable in model.proto.variables)
    for actual, share, row in zip(actuals, shares, rows):
        exact = abs(actual / sum(actuals) - row['amount'] / sum(row['amount'] for row in rows))
        assert abs(share - exact) < .002


def test_large_grouped_historical_goal_uses_derived_worst_person_bounds():
    data = world([], first='2027-10-01', last='2027-11-01')
    policy = catalogue((A,) * 10, metric='hours', period='month', amounts=(0,) * 10, levels=('critical',) * 10)
    policy['definition']['rule']['settings']['balance_goal'].update(period='custom', starts_on='2025-11-01', ends_on='2027-11-01')
    for index, line in enumerate(policy['definition']['rule']['lines']):
        line['settings'] = {'balance_goal': {'service_ids': [str(UUID(int=3000 + index))]}}
    data['policies'] = [policy]
    sources = f.balance_goals(data)
    data['fairness_history'] = {f.goal_identity(source): {'people': {A: {'value': 400, 'available': 700}}}
                                for source in sources}
    data['fairness_history'][f.goal_identity(f.comparison_goal(sources))] = {
        'people': {A: {'value': 4000, 'available': 700}}}
    model = cp_model.CpModel()
    terms, upper_bound = add_balance_goals(model, data, {}, {}, {}, {}, set(), {'critical': 30})
    model.minimize(sum(terms))
    assert model.validate() == ''
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 5
    solver.parameters.num_search_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert [solver.value(model.get_int_var_from_proto_index(index)) for index, variable in enumerate(model.proto.variables)
            if variable.name == 'goal_worst_off'] == [1_200_000_000]
    assert upper_bound >= solver.objective_value


@pytest.mark.parametrize('full_window,expected_amounts,expected_score', [
    (True, (2.25, 1.75), 0.202), (False, (1, 1), 0.0)])
def test_monthly_target_scores_use_the_same_full_or_partial_amounts(h, full_window, expected_amounts, expected_score):
    from packages.domain.validation import evaluate

    a, b, _ = h.seed['people']
    definition = catalogue((a, b), amounts=(2.25, 1.75), levels=('high', 'low'))['definition']
    rule = definition['rule']
    rule.update(id='duties_per_month', settings={'block_quota': {
        'starts_on': '2026-11-01', 'ends_on': '2026-12-01', 'window_alignment': 'calendar_month'}})
    saved = approve(h, propose(h, rule['id'], rule['lines'], settings=rule['settings'])['record'])
    workspace = h.workspace(start='2026-11-01', end='2026-12-01') if full_window else h.workspace(start='2026-11-01', end='2026-11-08')
    for day in (4, 5, 6, 7):
        workspace = h.shift(workspace, f'2026-11-{day:02}T08:00:00+03:00',
                            f'2026-11-{day:02}T12:00:00+03:00', people=[a])['workspace']
    data = h.snapshot(workspace)
    checked = evaluate(data)
    rows = [row for row in checked['quota_report']['rows'] if row['policy_id'] == saved['id']]
    assert {row['person_id']: row['amount'] for row in rows} == dict(zip((a, b), expected_amounts))
    assert schedule_score(data, checked)['components']['rules']['value'] == expected_score
    if full_window:
        assert not any(row.get('proration') for row in rows)
    else:
        assert all((row['proration']['working_days'], row['proration']['full_working_days']) == (5, 22)
                   for row in rows)
