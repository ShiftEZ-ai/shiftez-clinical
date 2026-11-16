"""Named supervision is an opt-in Fair share measure, never a capacity constraint."""
from collections import Counter
from copy import deepcopy
from datetime import date

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from apps.api.services import fairness as service
from packages.contracts.schemas import BalanceGoal, FairnessViewSettings, PolicyDefinition
from packages.domain import fairness as f
from packages.domain.cp_sat import supervised_goal
from packages.domain.cp_sat_goals import add_balance_goals, add_score_goals
from packages.domain.cp_sat_score import SCORE_SCALE
from packages.domain.score import schedule_score
from tests.test_balance_between_people import catalogue
from tests.test_engine_fairness_review import A, B, DEPT, duty, policy, uid, world
from tests.test_weighted_goals import STRENGTHS, assert_losses, report_losses


SUPERVISORS = (A, B, uid(11), uid(12))
TRAINEE, CAPABILITY, RANK = uid(13), uid(14), uid(15)


def supervision_world(assignments=None):
    assignments = assignments or [A] * 8
    shifts = [duty(i, i, [*SUPERVISORS, TRAINEE]) for i in range(1, len(assignments) + 1)]
    data = world(shifts)
    data['people'] = {pid: {'display_name': pid, 'tags': []} for pid in (*SUPERVISORS, TRAINEE)}
    data['memberships'] = [{'person_id': pid, 'department_id': DEPT, 'category_id': RANK,
                            'starts_on': '2020-01-01'} for pid in data['people']]
    data['categories'] = {RANK: {}}
    data['capabilities'] = {CAPABILITY: {}}
    data['grants'] = [{'person_id': pid, 'department_id': DEPT, 'service_id': None,
                       'capability_id': CAPABILITY, 'starts_on': '2020-01-01'} for pid in SUPERVISORS]
    for shift, supervisor in zip(shifts, assignments):
        shift['type_snapshot']['definition']['supervision'] = {
            'capability_id': CAPABILITY, 'trainee_category_ids': [], 'trainees_per_supervisor': 1,
            'require_pairing': True}
        for participant in shift['participants']:
            participant['participation'] = 'trainee' if participant['person_id'] == TRAINEE else 'supervisor'
            if participant['person_id'] == TRAINEE:
                participant['supervisor_person_id'] = supervisor
    data['policies'] = [policy({'metric': 'supervision', 'share_by': 'equal',
                                'tolerance_units': 0, 'tolerance_percent': 0})]
    return data


def test_counts_are_the_supervisor_side_of_existing_ledger_and_count_each_trainee():
    data = supervision_world()
    shift = data['shifts'][0]
    shift['participants'].append({**shift['participants'][-1], 'person_id': uid(16)})
    values, pairs = f.planned(data, data['policies'][0])
    ledger = Counter()
    for _, supervisor, key, _, amount in f.ledger_facts(data):
        if key == 'supervised':
            ledger[supervisor] += amount
    assert values == {A: 9}
    assert {pid: value for pid, value in values.items() if value} == ledger
    assert pairs == {}
    assert not any(key.startswith('supervision') for _, _, key, _, _ in f.ledger_facts(data))


def test_contracts_use_existing_advisory_catalogue_and_supported_share_bases():
    assert BalanceGoal(kind='balance_goal', metric='supervision').share_by == 'supervisable_sessions'
    assert FairnessViewSettings(metric='supervision').metric == 'supervision'
    data = supervision_world()
    data['policies'] = [catalogue(people=SUPERVISORS, metric='supervision', levels=('medium',) * 4)]
    goals = f.balance_goals(data)
    assert len(goals) == 4
    assert all(g['definition']['severity'] == 'advisory' and g['definition']['strength'] == 'medium' for g in goals)
    assert all(supervised_goal(g) for g in goals)
    assert len(f.goal_reports(data)) == 1
    for setting in ({'share_by': 'offered'}, {'distribution': 'period_spread'}):
        with pytest.raises(ValidationError):
            BalanceGoal(kind='balance_goal', metric='supervision', **setting)
    with pytest.raises(ValidationError):
        PolicyDefinition.model_validate({'severity': 'mandatory', 'rule': {'kind': 'balance_goal', 'metric': 'supervision'}})


def test_report_score_and_findings_use_supervisor_load_and_exclude_unqualified_people():
    data = supervision_world()
    assert f.goal_people(data, data['policies'][0]) == {'who': sorted(SUPERVISORS), 'partners': []}
    findings = []
    [report] = f.evaluate_goals(data, lambda code, message, **detail: findings.append((code, message, detail)))
    assert report['unit'] == 'trainee sessions supervised'
    assert {r['person_id']: r['expected'] for r in report['people']} == dict.fromkeys(SUPERVISORS, 2)
    assert {code for code, _, _ in findings} == {'BALANCE_GOAL'}
    assert all(detail['severity'] == 'warning' and 'trainee sessions supervised' in message
               for _, message, detail in findings)
    score = schedule_score(data, {'findings': [], 'coverage': [], 'goals': [report]})
    assert score['components']['fairness']['value'] < 1
    assert score['why'] and score['publishable']
    balanced = supervision_world([pid for pid in SUPERVISORS for _ in range(2)])
    assert f.goal_reports(balanced)[0]['score'] == 100


@pytest.mark.parametrize('exact_score', [False, True])
@pytest.mark.parametrize('fixed', [False, True])
def test_solver_spreads_eight_trainee_sessions_or_keeps_fixed_skew_feasible(exact_score, fixed):
    data = supervision_world()
    model = cp_model.CpModel()
    participants = {(shift['id'], p['person_id']): p for shift in data['shifts'] for p in shift['participants']}
    choices = {key: model.new_constant(1) for key in participants}
    pairings = {}
    for shift in data['shifts']:
        options = []
        for supervisor in SUPERVISORS:
            pair = model.new_bool_var('pair')
            pairings[shift['id'], TRAINEE, supervisor] = pair
            options.append(pair)
            if fixed:
                model.add(pair == int(supervisor == A))
        model.add_exactly_one(options)
    args = (model, data, choices, participants, {}, pairings, set(data['focus_ids']), STRENGTHS)
    if exact_score:
        terms = add_score_goals(*args)
        model.minimize(sum(loss for _, loss in terms))
    else:
        terms, _ = add_balance_goals(*args)
        model.minimize(sum(terms))
    assert model.validate() == ''
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    counts = Counter(supervisor for (_, _, supervisor), paired in pairings.items() if solver.value(paired))
    assert counts == ({A: 8} if fixed else dict.fromkeys(SUPERVISORS, 2))
    if exact_score:
        resolved = deepcopy(data)
        for shift in resolved['shifts']:
            shift['participants'][-1]['supervisor_person_id'] = next(
                supervisor for supervisor in SUPERVISORS if solver.value(pairings[shift['id'], TRAINEE, supervisor]))
        assert_losses([(weight, solver.value(loss) / SCORE_SCALE) for weight, loss in terms], report_losses(resolved))


def test_existing_fixed_pairings_and_history_match_exact_report_score():
    data = supervision_world()
    goal = data['policies'][0]
    data['fairness_history'] = {f.goal_identity(goal): {'people': {B: {'value': 4, 'available': 7}}}}
    model = cp_model.CpModel()
    participants = {(shift['id'], p['person_id']): p for shift in data['shifts'] for p in shift['participants']}
    choices = {key: model.new_constant(1) for key in participants}
    terms = add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert_losses([(weight, solver.value(loss) / SCORE_SCALE) for weight, loss in terms], report_losses(data))


def test_publication_history_uses_named_supervision_and_clinic_union_once():
    data = supervision_world()
    for shift in data['shifts']:
        shift['service_ids'] = [uid(40), uid(41)]
    keys = f.keys_for({'metric': 'supervision', 'service_ids': [uid(40), uid(41)]})
    rows = service.publication_facts(None, DEPT, keys, date(2026, 10, 1), date(2026, 11, 1), SUPERVISORS,
                                      snapshots=[('publication', {'timezone': 'UTC', 'shifts': data['shifts']})])
    assert sum(row[-1] for row in rows if row[1] == A) == 8
    stored_keys, person = service.ledger_measure(keys)
    assert stored_keys == ['supervised|service:' + uid(40), 'supervised|service:' + uid(41)]
    assert person.key == 'other_person_id'


def test_overnight_supervision_keeps_trainee_day_in_ledger_report_and_snapshot():
    data = supervision_world([A])
    shift = data['shifts'][0]
    shift.update(start='2026-09-30T22:00:00+00:00', end='2026-10-01T04:00:00+00:00')
    for p in shift['participants']:
        p.update(start=shift['start'], end=shift['end'])
    shift['participants'][-1]['start'] = '2026-10-01T01:00:00+00:00'
    goal = data['policies'][0]
    assert f.goal_people(data, goal)['who'] == sorted(SUPERVISORS)
    values, _ = f.planned(data, goal)
    assert values == {A: 1}
    assert [(pid, day, value) for _, pid, key, day, value in f.ledger_facts(data) if key == 'supervised'] == [
        (A, date(2026, 10, 1), 1)]
    rows = service.publication_facts(None, DEPT, ['supervision'], date(2026, 10, 1), date(2026, 11, 1), SUPERVISORS,
                                    snapshots=[('publication', {'timezone': 'UTC', 'shifts': data['shifts']})])
    assert rows == [('publication', A, None, date(2026, 10, 1), 1)]


def test_greedy_marginal_cost_credits_the_supervisor_not_the_trainee():
    from packages.domain.fairness_distribution import people_marginal_cost

    data = supervision_world([A] * 4)
    shift = data['shifts'][-1]
    trainee = shift['participants'].pop()
    goal = data['policies'][0]
    cost = {pid: people_marginal_cost(data, goal, shift, {**trainee, 'supervisor_person_id': pid})
            for pid in (A, B)}
    assert cost[B] < cost[A]


def test_available_days_is_not_an_assigned_duty_denominator():
    data = supervision_world([B] * 6 + [A] * 2)
    for shift in data['shifts'][:6]:
        shift['participants'] = [p for p in shift['participants'] if p['person_id'] != A]
    goal = policy({'metric': 'supervision', 'share_by': 'available_days',
                   'tolerance_percent': 0, 'tolerance_units': 0}, {'person_ids': [A, B]})
    [a, b] = sorted(f.goal_report(data, goal)['people'], key=lambda row: row['person_id'])
    assert a['available_days'] == b['available_days'] == 31
    assert a['expected'] == b['expected'] == 4  # A works two duties, B works eight: availability alone cannot express that.
    assert (a['actual'], b['actual']) == (2, 6)
