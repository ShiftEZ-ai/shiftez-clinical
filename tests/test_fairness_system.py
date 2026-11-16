"""Every Fair share measure changes decisions through the production score compiler."""
from copy import deepcopy

import pytest
from ortools.sat.python import cp_model

from packages.domain import fairness as f
from packages.domain.cp_sat_goals import add_score_goals
from packages.domain.cp_sat_score import SCORE_SCALE
from tests.test_engine_fairness_review import A, B, DEPT, duty, policy, uid, world
from tests.test_supervision_balance import supervision_world, SUPERVISORS, TRAINEE
from tests.test_weighted_goals import STRENGTHS, assert_losses, report_losses


@pytest.mark.parametrize('metric', ['duties', 'hours', 'session_units', 'weekends', 'overtime_duties',
                                  'overtime_hours', 'lead_turns', 'pair_exposure', 'workload', 'supervision'])
def test_each_metric_improves_decisions_and_matches_report_score(metric):
    data = world([duty(i, day, [A, B]) for i, day in enumerate((2, 9, 16, 23), 1)])
    rule = {'metric': metric, 'share_by': 'equal', 'tolerance_percent': 0, 'tolerance_units': 0}
    if metric == 'workload':
        rule['aim'] = 'workload_spread'
    if metric == 'pair_exposure':
        rule.pop('tolerance_percent')
        rule.pop('tolerance_units')
        rule.update(aim='min_per_pair', amount=2,
                    partners={'conditions': [{'field': 'person_ids', 'values': [A, B]}]})
        data['people'][TRAINEE] = {'display_name': 'Trainee', 'tags': []}
        data['memberships'].append({'person_id': TRAINEE, 'department_id': DEPT, 'starts_on': '2020-01-01'})
        for shift in data['shifts']:
            shift['participants'].append({**shift['participants'][0], 'person_id': TRAINEE})
    data['policies'] = [policy(rule, {'person_ids': [TRAINEE]} if metric == 'pair_exposure' else {})]
    if metric == 'supervision':
        data = supervision_world()
    if metric == 'lead_turns':
        for shift in data['shifts']:
            shift['type_snapshot']['definition']['lead'] = {
                'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': []}
    for person in data['people'].values():
        person['active'] = True
    if metric != 'supervision':
        rank = uid(80)
        data['categories'] = {rank: {}}
        for row in data['memberships']:
            row['category_id'] = rank

    def solve(enabled):
        model = cp_model.CpModel()
        choices, participants, overtime, leads, pairings, decisions = {}, {}, {}, {}, {}, []
        for shift in data['shifts']:
            options = []
            for participant in shift['participants']:
                pid, sid = participant['person_id'], shift['id']
                key = sid, pid
                participants[key] = {**participant, 'overtime': metric.startswith('overtime_')}
                fixed = metric in ('lead_turns', 'supervision') or pid == TRAINEE
                choices[key] = model.new_constant(1) if fixed else model.new_bool_var('attend')
                if metric == 'lead_turns':
                    leads[key] = model.new_bool_var('lead')
                    options.append(leads[key])
                elif metric == 'supervision' and pid in SUPERVISORS:
                    pairings[sid, TRAINEE, pid] = model.new_bool_var('supervise')
                    options.append(pairings[sid, TRAINEE, pid])
                elif not fixed:
                    options.append(choices[key])
                if metric.startswith('overtime_'):
                    overtime[key] = choices[key]
            model.add_exactly_one(options)
            decisions.append(options)
        terms = add_score_goals(model, data, choices, participants, overtime, pairings,
                                set(data['focus_ids']), STRENGTHS, leads)
        # Fix the otherwise arbitrary off-goal tie to a reproducible skew. Every seat remains equally eligible.
        tie = sum(options[0] for options in decisions)
        model.minimize(sum(loss for _, loss in terms) * (len(decisions) + 1) - tie if enabled else -tie)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 30
        status = solver.solve(model)
        assert status == cp_model.OPTIMAL, solver.status_name(status)
        resolved = deepcopy(data)
        for shift in resolved['shifts']:
            sid = shift['id']
            shift['participants'] = [deepcopy(participants[sid, p['person_id']]) for p in shift['participants']
                                     if solver.value(choices[sid, p['person_id']])]
            for p in shift['participants']:
                if leads:
                    p['team_lead'] = bool(solver.value(leads[sid, p['person_id']]))
                if pairings and p['person_id'] == TRAINEE:
                    p['supervisor_person_id'] = next(pid for pid in SUPERVISORS
                        if solver.value(pairings[sid, TRAINEE, pid]))
        assert_losses([(weight, solver.value(loss) / SCORE_SCALE) for weight, loss in terms], report_losses(resolved))
        return f.goal_reports(resolved)[0]

    off, on = solve(False), solve(True)
    assert f.report_value(on, STRENGTHS) > f.report_value(off, STRENGTHS)
    assert f.report_value(on, STRENGTHS) == pytest.approx(1)


def test_no_configured_goal_has_no_hidden_workload_score():
    data = world([duty(1, 1, [A])])
    data['generation_options'] = {'balance_measure': 'reserved_hours'}
    model = cp_model.CpModel()
    participants = {(s['id'], p['person_id']): p for s in data['shifts'] for p in s['participants']}
    choices = {key: model.new_constant(1) for key in participants}
    assert f.goal_reports(data) == []
    assert add_score_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), STRENGTHS) == []


def test_lead_promotion_uses_grouped_weighted_report_without_counting_attendance_twice():
    from packages.domain.fairness_distribution import greedy_goals, marginal_priority
    from tests.test_balance_between_people import catalogue

    data = world([duty(i, i, [A, B]) for i in range(1, 5)])
    for shift in data['shifts']:
        shift['type_snapshot']['definition']['lead'] = {
            'minimum': 1, 'maximum': 1, 'capability_ids': [], 'category_ids': []}
    for shift in data['shifts'][:-1]:
        shift['participants'][0]['team_lead'] = True
    goal = catalogue(metric='lead_turns', levels=('high', 'low'))
    goal['definition']['rule']['settings']['balance_goal'].update(tolerance_percent=0, tolerance_units=0)
    data.update(policies=[goal], _distribution_cache={}, _distribution_revision=0, _allocation_strengths=STRENGTHS)
    data['_distribution_goals'] = greedy_goals(data)
    shift = data['shifts'][-1]
    [before] = f.goal_reports(data)
    weights = f.report_weights(before, STRENGTHS)
    priorities = {}
    for participant in shift['participants']:
        trial = deepcopy(data)
        trial.pop('_distribution_cache')
        trial['shifts'][-1]['participants'][shift['participants'].index(participant)]['team_lead'] = True
        [after] = f.goal_reports(trial)
        priorities[participant['person_id']] = marginal_priority(data, shift, {**participant, 'team_lead': True})
        assert priorities[participant['person_id']] == pytest.approx(
            (f.report_value(after, STRENGTHS) - f.report_value(before, STRENGTHS))
            * sum(weights.values()) / len(weights) / STRENGTHS['medium'])
    assert priorities[B] > priorities[A]


def test_holidays_are_a_date_set_scope_and_history_carries_into_a_longer_period():
    from datetime import date
    from tests.test_date_sets import data as dates, entry
    from tests.test_balance_between_people import catalogue

    data = world([duty(1, 5, [A]), duty(2, 6, [B]), duty(3, 7, [A])])
    calendar = uid(90)
    data['date_sets'] = {calendar: {**dates(entry(repeat='once', first_day='2026-10-05',
        last_day='2026-10-06'))['date_sets']['holidays'], 'id': calendar}}
    record = catalogue(period='year')
    for line in record['definition']['rule']['lines']:
        line['where'] = {'conditions': [{'field': 'date_set_ids', 'values': [calendar]}]}
    data['policies'] = [record]
    goal = f.comparison_goal(f.balance_goals(data))
    data['fairness_history'] = {f.goal_identity(goal): {'people': {B: {'value': 3, 'available': 20}}}}
    [report] = f.goal_reports(data)
    rows = {row['person_id']: row for row in report['people']}
    assert rows[A]['planned'] == rows[B]['planned'] == 1
    assert rows[B]['prior'] == 3 and rows[B]['actual'] == 4
    assert f.period_bounds(goal['definition']['rule'], date(2026, 10, 1), date(2026, 11, 1))[0] == date(2026, 1, 1)
    from apps.api.services.fairness import publication_facts, precise_history
    from tests.test_weighted_goals import solver_losses
    assert_losses(solver_losses(data), report_losses(data))
    assert precise_history(goal, {A: [(date(2026, 1, 1), date(2026, 11, 1))]})
    facts = publication_facts(None, DEPT, ['duties'], date(2026, 1, 1), date(2026, 11, 1), [A, B],
        snapshots=[('publication', {'timezone': 'UTC', 'shifts': data['shifts']})], policy=goal, data=data)
    assert sum(row[-1] for row in facts) == 2


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('metric', ['duties', 'overtime_duties', 'overtime_hours'])
def test_greedy_respects_grouped_lines_and_actual_overtime(h, metric):
    from packages.domain.generation import generate
    from tests.test_balance_between_people import catalogue

    first, second = sorted(h.seed['people'][:2])
    overtime = metric.startswith('overtime_')
    typ = h.configure('assignment_types', {'name': 'Fairness diagnostic', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}], 'extra_cover': overtime}})['record']['id']
    if overtime:
        h.policy({'kind': 'allocation_overtime'}, severity='advisory')
    w = h.workspace(start='2026-10-05', end='2026-10-12')
    for day, pid, counted in ((5, first, True), (6, first, True), (7, second, False), (8, second, False)):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        w = h.shift(w, start=start, end=end, typ=typ if counted else h.seed['type'],
                    people=[{'person_id': pid, 'start': start, 'end': end, 'overtime': overtime and counted}])['workspace']
    w = h.shift(w, start='2026-10-09T08:00:00+03:00', end='2026-10-09T12:00:00+03:00',
                typ=typ, people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: data['people'][pid] for pid in (first, second)}
    goal = catalogue(people=(first, second), metric=metric)
    goal['department_id'] = data['workspace']['department_id']
    goal['definition']['rule']['settings']['balance_goal'].update(
        optimize_greedy=True, share_by='equal', tolerance_percent=0, tolerance_units=0,
        assignment_type_ids=[typ])
    data['policies'].append(goal)
    data['generation_options'] = {'mode': 'fill_gaps'}
    result = generate(data, 20)
    assert result['status'] == 'candidate_feasible', result
    assert result['additions'][0]['participant']['person_id'] == second
    assert bool(result['additions'][0]['participant']['overtime']) == overtime
