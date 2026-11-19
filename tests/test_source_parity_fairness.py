"""Configured Fair share dimensions share one evaluator across history, score and both generators."""
import copy
import math
from datetime import date, timedelta
from fractions import Fraction
import time
from unittest.mock import patch

import pytest
from ortools.sat.python import cp_model

from apps.api.services import fairness as service
from packages.domain import fairness as f
from packages.domain.cp_sat import generate as cp_generate
from packages.domain.cp_sat_goals import add_balance_goals
from packages.domain.fairness_distribution import counted_groups, distribution_plan, loss, marginal_cost, numeric_groups, value
from packages.domain.generation import generate as greedy_generate
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from tests.test_engine_fairness_review import A, B, DEPT, T, duty, policy, uid, world


def tree(field, values):
    return {'match': 'all', 'conditions': [{'field': field, 'values': values}]}


def configured(data, distribution, **settings):
    goal = policy({'metric': 'duties', 'distribution': distribution, **settings})
    data['policies'] = [goal]
    return goal


def test_generic_share_uses_nested_conditions_and_frozen_published_history():
    selected, other = uid(20), uid(21)
    data = world([duty(1, 15, [A], [other])], '2026-10-15', '2026-10-22')
    goal = configured(data, 'share', period='month', target_share=.5, within=tree('service_ids', [selected]))
    published = world([duty(2, 5, [A], [selected])])
    with patch.object(service, 'published_snapshots', return_value=[('publication', {'shifts': published['shifts']})]):
        data['fairness_history'] = service.history(None, data, [goal])
    [row] = f.goal_report(data, goal)['people']
    assert (row['prior'], row['planned'], row['actual'], row['expected'], row['cost']) == (1, 0, 1, 1, 0)
    assert f.goal_report(data, goal)['history'] is True
    data['shifts'][0]['service_ids'] = [selected]
    [row] = f.goal_report(data, goal)['people']
    assert row['actual'] == 2 and row['expected'] == 1 and row['cost'] == pytest.approx(1 / 21)
    data['fairness_history'] = {}
    data['shifts'][0]['service_ids'] = [other]
    assert f.goal_report(data, goal)['people'][0]['status'] == 'below'


def test_greedy_history_does_not_recount_nonfocus_publication_context():
    selected = uid(20)
    old, current = duty(1, 5, [A], [selected]), duty(2, 15, [], [selected])
    data = world([old, current], '2026-10-15', '2026-10-22')
    data['focus_ids'] = [current['id']]
    goal = configured(data, 'share', period='month', within=tree('service_ids', [selected]))
    data['fairness_history'] = {f.goal_identity(goal): {'distribution': [
        {'person_id': A, 'bucket': '', 'partner_id': None, 'total': 1, 'selected': 1}]}}
    participant = {'person_id': A, 'start': current['start'], 'end': current['end'], 'overtime': False}
    assert marginal_cost(data, goal, current, participant, [(old, old['participants'][0])]) == marginal_cost(
        data, goal, current, participant, [])


@pytest.mark.parametrize(('week_start', 'buckets'), [
    (0, {'2026-10-05': 7, '2026-10-12': 7}),
    (2, {'2026-09-30': 7, '2026-10-07': 7, '2026-10-14': 5}),
])
def test_period_spread_ignores_fixed_weeks_before_rota(week_start, buckets):
    data = world([duty(1, 5, [A]), duty(2, 12, [A])], '2026-10-05', '2026-10-19')
    data['organisation']['week_start'] = week_start
    goal = configured(data, 'period_spread')
    assert goal['definition']['rule']['period'] == 'rolling'
    assert goal['definition']['rule']['period_months'] == 12
    retained = {'person_id': A, 'bucket': min(buckets), 'total': 1}
    data['fairness_history'] = {f.goal_identity(goal): {'distribution': [
        {'person_id': A, 'bucket': '2026-09-21', 'total': 100}, retained]}}
    plans = distribution_plan(data, goal)
    person = [plan for plan in plans if plan['person_id'] == A]
    assert {plan['bucket'] for plan in person} == set(buckets)
    assert sum(value(plan['actual']) for plan in person) == 3
    assert {plan['bucket']: value(plan['expected']) for plan in person} == {
        bucket: Fraction(3 * days, sum(buckets.values())) for bucket, days in buckets.items()}
    assert {value(plan['denominator']) for plan in person} == {sum(buckets.values())}
    row = next(row for row in f.goal_report(data, goal)['people'] if row['person_id'] == A)
    assert (row['prior'], row['planned'], row['actual']) == (1, 2, 3)
    data['fairness_history'][f.goal_identity(goal)]['distribution'] = [retained]
    assert distribution_plan(data, goal) == plans


def test_period_spread_keeps_a_single_week_bucket():
    data = world([duty(1, 5, [A]), duty(2, 6, [A])], '2026-10-05', '2026-10-12')
    goal = configured(data, 'period_spread')
    [plan] = [plan for plan in distribution_plan(data, goal) if plan['person_id'] == A]
    assert plan['bucket'] == '2026-10-05'
    assert value(plan['actual']) == value(plan['expected']) == 2
    assert value(plan['denominator']) == 7
    assert loss(plan) == 0


def test_week_spread_and_pair_limit_use_department_calendar_and_shared_marginal_cost():
    data = world([duty(1, 5, [A]), duty(2, 6, [A])], '2026-10-05', '2026-10-19')
    goal = configured(data, 'period_spread')
    [row] = f.goal_report(data, goal)['people'][:1]
    assert row['actual'] == row['expected'] == 2 and row['cost'] == pytest.approx(1 / 7)
    later = duty(3, 12)
    participation = {'person_id': A, 'start': later['start'], 'end': later['end'], 'overtime': False}
    history = [(s, s['participants'][0]) for s in data['shifts']]
    assert marginal_cost(data, goal, later, participation, history) < 0
    data['organisation']['week_start'] = 2
    assert {plan['bucket'] for plan in distribution_plan(data, goal)} == {'2026-09-30', '2026-10-07', '2026-10-14'}
    data = world([duty(1, 5, [A, B]), duty(2, 6, [A, B]), duty(3, 12, [A, B])])
    goal = configured(data, 'pair_limit', metric='pair_exposure', aim='target', amount=1,
                      partners=tree('person_ids', [B]))
    rows = {row['person_id']: row for row in f.goal_report(data, goal)['people']}
    assert rows[A]['cost'] == 1
    assert [b['cost'] for b in rows[A]['buckets']] == [1, 0]


def test_distribution_solver_compiles_the_evaluators_exact_deviations():
    data = world([duty(1, 5), duty(2, 6)], '2026-10-05', '2026-10-19')
    goal = configured(data, 'period_spread')
    model = cp_model.CpModel()
    choices, participants = {}, {}
    for shift in data['shifts']:
        for pid in (A, B):
            choices[shift['id'], pid] = model.new_bool_var('attendance')
            participants[shift['id'], pid] = {'person_id': pid, 'start': shift['start'], 'end': shift['end'], 'overtime': False}
        model.add(sum(choices[shift['id'], pid] for pid in (A, B)) == 1)
    terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), {'low': 1, 'medium': 3, 'high': 9})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    assert solver.solve(model) == cp_model.OPTIMAL
    assert not model.validate()
    for shift in data['shifts']:
        shift['participants'] = [participants[shift['id'], pid] for pid in (A, B) if solver.value(choices[shift['id'], pid])]
    assert sum(loss(plan) for plan in distribution_plan(data, goal)) == 2
    canonical = sum(math.ceil(1000 * loss(plan) / value(plan['denominator'])) * 3
                    for plan in distribution_plan(data, goal))
    assert solver.objective_value == canonical


def test_supervision_planning_uses_candidate_pairing_instead_of_previous_name():
    third = uid(30)
    shift = duty(1, 5, [A, B, third])
    shift['participants'][0]['supervisor_person_id'] = B
    data = world([shift])
    data['people'][third] = {'display_name': 'Other', 'tags': []}
    data['memberships'].append({'person_id': third, 'department_id': DEPT, 'starts_on': '2020-01-01'})
    goal = configured(data, 'pair_limit', metric='pair_exposure', aim='target', amount=1,
                      pairing='supervised', partners=tree('person_ids', [B, third]))
    groups = numeric_groups(counted_groups(data, goal, [(shift, shift['participants'][0], 1, {B: 0, third: 1})]))
    assert {row['partner_id']: row['total'] for row in groups} == {B: 0, third: 1}


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate], ids=['greedy', 'cp-sat'])
@pytest.mark.parametrize('distribution', ['share', 'period_spread'])
def test_each_engine_uses_configured_distribution_when_filling_a_gap(h, engine, distribution):
    first, second = h.seed['people'][:2]
    settings = ({'within': tree('service_ids', [h.seed['services'][1]]), 'target_share': .5}
                if distribution == 'share' else {})
    h.policy({'kind': 'balance_goal', 'metric': 'duties', 'distribution': distribution, **settings}, severity='advisory')
    duty = h.configure('assignment_types', {'name': 'One-place distribution clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']
    workspace = h.workspace(start='2026-10-05', end='2026-10-19')
    rows = ((5, 1, first), (12, 0, second), (13, 1, None)) if distribution == 'share' else ((5, 0, first), (12, 0, second), (13, 0, None))
    for day, clinic, person in rows:
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00', end=f'2026-10-{day:02}T12:00:00+03:00',
                            typ=duty['id'], service=clinic, people=[person] if person else [])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['generation_options'] = {'mode': 'fill_gaps'}
    original = copy.deepcopy(data)
    result = engine(data, 20)
    assert result['status'] == 'candidate_feasible', result
    assert len(result['additions']) == 1
    expected = second if distribution == 'share' else first
    assert result['additions'][0]['participant']['person_id'] == expected
    assert data == original


def test_numeric_weight_changes_score_without_changing_validation_or_default_goals():
    data = world([duty(1, 5, [A]), duty(2, 6, [A]), duty(3, 7, [B])], '2026-10-05', '2026-10-19')
    goal = configured(data, 'period_spread')
    # Relative weights need a second goal; C2 exposes the former built-in workload goal as data.
    default = {**policy({'metric': 'workload', 'aim': 'workload_spread'}), 'id': uid(6100)}
    data['policies'].append(default)
    default_report = f.goal_report(data, default)
    low = schedule_score(data, {'goals': [f.goal_report(data, goal), default_report], 'findings': []})
    goal['definition']['rule']['weight'] = 3
    high = schedule_score(data, {'goals': [f.goal_report(data, goal), default_report], 'findings': []})
    assert f.goal_report(data, default) == default_report
    assert f.goal_report(data, goal)['weight'] == 3
    assert high['components']['fairness']['value'] != low['components']['fairness']['value']
    attributed = [item for item in high['contributions'] if item.get('rule_id') == goal['id']]
    assert attributed and all(item['rule_weight'] == 9 for item in attributed)
    assert next(item for item in high['rule_weights'] if item['rule_id'] == goal['id'])['weight'] == 9
    old = policy({'metric': 'duties'})
    explicit = copy.deepcopy(old)
    explicit['definition']['rule'].update(distribution='people', weight=1)
    assert f.goal_report(data, old) == f.goal_report(data, explicit)


@pytest.mark.parametrize('engine', [greedy_generate, cp_generate], ids=['greedy', 'cp-sat'])
def test_each_engine_avoids_repeated_named_supervision(h, engine):
    trainee = h.seed['people'][0]
    first, second = sorted(h.seed['people'][1:3])
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'balance_goal', 'distribution': 'pair_limit', 'metric': 'pair_exposure',
              'aim': 'target', 'amount': 1, 'pairing': 'supervised',
              'partners': tree('person_ids', [first, second])}, {'person_id': trainee}, 'advisory')
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'target', 'amount': 2}, {'person_id': trainee}, 'advisory')
    typ = h.configure('assignment_types', {'name': 'Synthetic supervised duty', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 2,
            'require_pairing': True}}})['record']
    workspace = h.workspace()
    for day in (5, 6):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        people = [{'person_id': pid, 'start': start, 'end': end, 'participation': 'supervisor'} for pid in (first, second)]
        if day == 5:
            people.append({'person_id': trainee, 'start': start, 'end': end,
                           'participation': 'trainee', 'supervisor_person_id': first, 'fixed': True})
        workspace = h.shift(workspace, start=start, end=end, typ=typ['id'], people=people)['workspace']
    data = h.snapshot(workspace)
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = engine(data, 20)
    assert result['status'] == 'candidate_feasible', result
    [added] = [row['participant'] for row in result['additions'] if row['participant']['person_id'] == trainee]
    assert added['supervisor_person_id'] == second


@pytest.mark.parametrize('metric', ['duties', 'weekends'], ids=['selected-duty-type', 'weekends'])
def test_existing_balance_dimensions_opt_into_greedy_without_changing_its_default(h, metric):
    first, second = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Selected synthetic duties', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    rule = {'kind': 'balance_goal', 'metric': metric, 'optimize_greedy': True}
    rule.update({'assignment_type_ids': [typ]} if metric == 'duties' else {'weekend_start': 0, 'weekend_days': 1})
    record = h.policy(rule, severity='advisory')
    workspace = h.workspace(start='2026-10-05', end='2026-11-03')
    for week in range(4):
        for offset, pid, selected in ((week * 7, first, True), (week * 7 + 1, second, False)):
            day = (date(2026, 10, 5) + timedelta(days=offset)).isoformat()
            workspace = h.shift(workspace, start=day + 'T08:00:00+03:00', end=day + 'T12:00:00+03:00',
                                typ=typ if selected else h.seed['type'], people=[pid])['workspace']
    workspace = h.shift(workspace, start='2026-11-02T08:00:00+03:00', end='2026-11-02T12:00:00+03:00',
                        typ=typ, people=[])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = greedy_generate(data, 20)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'][0]['participant']['person_id'] == second
    goal = next(p for p in data['policies'] if p['id'] == record['id'])['definition']['rule']
    goal['optimize_greedy'] = False
    neutral = greedy_generate(data, 20)
    del goal['optimize_greedy']
    assert greedy_generate(data, 20)['additions'] == neutral['additions']


@pytest.fixture(scope='module', params=['share', 'period_spread', 'pair_limit'])
def department_distribution_plan(request):
    distribution = request.param
    people = [uid(1000 + n) for n in range(150)]
    shifts = [duty(n, (date(2026, 10, 5) + timedelta(days=n % 28)).isoformat(),
                   [people[n % 140], people[140 + n % 10]] if distribution == 'pair_limit' else [people[n % len(people)]])
              for n in range(500)]
    if distribution == 'pair_limit':
        for n, shift in enumerate(shifts):
            candidates = [people[(n + offset) % 140] for offset in range(10)] + people[140:]
            shift['participants'] = [{'person_id': pid, 'start': shift['start'], 'end': shift['end'],
                                      'overtime': False} for pid in candidates]
    data = world(shifts, '2026-10-05', '2026-11-02')
    data['people'] = {pid: {'display_name': str(n), 'tags': []} for n, pid in enumerate(people)}
    data['memberships'] = [{'person_id': pid, 'department_id': DEPT, 'starts_on': '2020-01-01'} for pid in people]
    settings = ({'within': tree('person_ids', people[:10])} if distribution == 'share' else
                {'metric': 'pair_exposure', 'aim': 'target', 'amount': 1, 'partners': tree('person_ids', people[140:])}
                if distribution == 'pair_limit' else {})
    goal = configured(data, distribution, **settings)
    if distribution == 'pair_limit':
        goal['definition']['scope']['person_ids'] = people[:140]
    model = cp_model.CpModel()
    choices = {(shift['id'], p['person_id']): model.new_bool_var('attendance') for shift in shifts for p in shift['participants']}
    participants = {(shift['id'], p['person_id']): p for shift in shifts for p in shift['participants']}
    started = time.process_time()
    terms, bound = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), {'low': 1, 'medium': 3, 'high': 9})
    elapsed = time.process_time() - started
    print('fairness distribution budget', distribution, {'cpu_seconds': elapsed,
          'variables': len(model.proto.variables), 'constraints': len(model.proto.constraints)})
    return distribution, model, terms, bound, elapsed


def test_distribution_planning_has_department_scale_model_budget(department_distribution_plan):
    distribution, model, terms, bound, _ = department_distribution_plan
    assert bound > 0 and terms and not model.validate()
    maximum = 150000 if distribution == 'pair_limit' else 5000
    assert len(model.proto.variables) < maximum and len(model.proto.constraints) < maximum


@pytest.mark.cpu_budget
def test_distribution_planning_has_department_scale_cpu_budget(department_distribution_plan):
    distribution, _, _, _, elapsed = department_distribution_plan
    assert elapsed < (45 if distribution == 'pair_limit' else 15), elapsed


def test_dense_present_pair_product_fails_with_configured_diagnostic(h):
    first, second = h.seed['people'][:2]
    h.policy({'kind': 'balance_goal', 'metric': 'pair_exposure', 'distribution': 'pair_limit',
              'aim': 'target', 'amount': 1, 'partners': tree('person_ids', [second])},
             {'person_id': first}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    data['generation_options'] = {'pair_limit_max_opportunities': 1}
    result = cp_generate(data, 20)
    assert result['status'] == 'invalid_inputs', result
    assert result['issues'][0]['code'] == 'FAIRNESS_PAIR_BUDGET'
    assert result['issues'][0]['maximum'] == 1
    assert not result['additions']
    data['generation_options']['pair_limit_max_opportunities'] = 10
    assert cp_generate(data, 20)['status'] == 'candidate_feasible'
