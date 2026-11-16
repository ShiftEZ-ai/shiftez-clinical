"""Boundary regressions found while reviewing the engine fairness repair."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.api.services import fairness as service
from apps.api.services.planner_tools import score_detail
from packages.domain import fairness as f
from packages.domain.score import COMPONENTS
from tests.test_engine_fairness_review import A, DEPT, POLICY, duty, policy, uid, world


def read_history(data, goals, published):
    ledger = f.ledger_facts(published)
    db = SimpleNamespace(execute=lambda _query: [(key, pid, other, day, value)
                                                for pid, other, key, day, value in ledger])
    snapshots = [('synthetic-publication', {'timezone': published['organisation']['timezone'],
                                          'shifts': published['shifts']})]
    with patch.object(service, 'published_snapshots', return_value=snapshots):
        return service.history(db, data, goals)


@pytest.mark.parametrize('transition', ['join', 'leave'])
@pytest.mark.parametrize('nested', [False, True], ids=['flat', 'nested'])
def test_overnight_dated_fairness_scope_is_unchanged_by_publication(h, transition, nested):
    person = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Overnight fairness audience', 'starts_on': '2020-01-01'})['record']['id']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme,
        'starts_on': '2026-10-05' if transition == 'join' else '2020-01-01',
        'ends_on': '2026-10-07' if transition == 'join' else '2026-10-05'}, role='coordinator')
    scope = ({'conditions': {'match': 'all', 'conditions': [{'match': 'all', 'conditions': [
        {'field': 'programme_ids', 'op': 'any', 'values': [programme]}]}]}}
        if nested else {'programme_id': programme})
    saved = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'custom',
        'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, scope=scope, severity='advisory')
    workspace = h.shift(h.workspace(), start='2026-10-04T22:00:00+03:00',
                        end='2026-10-05T02:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    goal = next(goal for goal in data['policies'] if goal['id'] == saved['id'])
    [before] = f.goal_report(data, goal)['people']
    h.publish(workspace)
    later = h.snapshot(h.workspace(start='2026-10-11', end='2026-10-18'))
    goal = next(goal for goal in later['policies'] if goal['id'] == saved['id'])
    [after] = f.goal_report(later, goal)['people']
    assert after['prior'] == before['planned']


def inherited_goal_data():
    programme, personal = uid(30), uid(23)
    settings = {'balance_goal': {'metric': 'duties', 'period': 'month'}}
    data = world([], '2026-10-11', '2026-10-18')
    data['programmes'] = {programme: {'active': True, 'starts_on': '2020-01-01'}}
    data['programme_memberships'] = [{'person_id': A, 'programme_id': programme,
                                    'starts_on': '2026-10-10', 'ends_on': '2026-11-01'}]
    data['policies'] = [{'id': POLICY, 'name': 'Inherited fairness strength', 'kind': 'catalogue', 'revision': 1,
        'department_id': DEPT, 'status': 'active', 'active': True, 'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': [
            {'id': uid(21), 'numbers': {'apply': {'value': None, 'level': 'medium'}}, 'settings': settings},
            {'id': uid(22), 'numbers': {'apply': {'value': None, 'level': 'high'}}, 'settings': settings,
             'who': {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [programme]}]}},
            {'id': personal, 'numbers': {'apply': {'value': None, 'level': None}}, 'settings': settings,
             'who': {'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [A]}]}},
        ]}}}]
    goals = [goal for goal in f.balance_goals(data) if goal['template_line']['id'] == personal]
    assert {goal['definition']['strength'] for goal in goals} == {'medium', 'high'}
    return data, goals


def test_inherited_same_line_strength_copies_keep_distinct_history():
    data, goals = inherited_goal_data()
    published = world([duty(1, 4, [A]), duty(2, 5, [A]), duty(3, 10, [A])], '2026-10-01', '2026-10-11')
    data['fairness_history'] = read_history(data, goals, published)
    reports = [f.goal_report(data, goal) for goal in goals]
    comparison = f.comparison_goal(goals)
    assert set(data['fairness_history']) == {*(f.goal_identity(goal) for goal in goals), f.goal_identity(comparison)}
    assert data['fairness_history'][f.goal_identity(comparison)]['people'][A]['value'] == 3
    assert all(len('goal:' + report['goal_id']) <= 96 for report in reports)
    assert {report['strength']: report['people'][0]['prior'] for report in reports} == {'medium': 2, 'high': 1}


def test_comparison_keeps_inherited_same_line_strength_copies():
    data, goals = inherited_goal_data()
    published = world([duty(1, 4, [A]), duty(2, 5, [A]), duty(3, 10, [A])], '2026-10-01', '2026-10-11')
    data['fairness_history'] = read_history(data, goals, published)
    reports = [f.goal_report(data, goal) for goal in goals]
    score = {'score': 100, 'components': {component: {'value': 1} for component in COMPONENTS}}
    before = SimpleNamespace(validation={'score': score, 'goals': reports})
    after = SimpleNamespace(validation={'score': score, 'goals': [{**report, 'score': report['score'] - 5}
                                                                for report in reports]})
    comparison = score_detail(None, before, after)['goals']
    assert len(comparison) == 2
    assert all(row['delta'] == -5 for row in comparison)


@pytest.mark.parametrize('catalogue_line', [False, True], ids=['policy-dates', 'line-dates'])
def test_goal_effective_dates_clip_availability_planned_work_and_history(catalogue_line):
    data = world([duty(1, 4, [A]), duty(2, 5, [A]), duty(3, 7, [A])], '2026-10-04', '2026-10-08')
    rule = {'metric': 'duties', 'period': 'custom', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}
    goal = policy(rule)
    if catalogue_line:
        data['policies'] = [{**goal, 'kind': 'catalogue', 'revision': 1,
            'definition': {'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': [
                {'id': uid(20), 'starts_on': '2026-10-05', 'ends_on': '2026-10-10',
                 'numbers': {'apply': {'value': None, 'level': 'medium'}},
                 'settings': {'balance_goal': rule}},
            ]}}}]
        [goal] = f.balance_goals(data)
    else:
        goal.update(starts_on='2026-10-05', ends_on='2026-10-10')
    assert f.goal_intervals(data, goal, A, *f.rota_dates(data)) == [(date(2026, 10, 5), date(2026, 10, 8))]
    before = next(row for row in f.goal_report(data, goal)['people'] if row['person_id'] == A)
    assert (before['planned'], before['available_days']) == (2, 3)
    later = {**data, 'workspace': {**data['workspace'], 'starts_on': '2026-10-08', 'ends_on': '2026-10-11'},
             'shifts': [], 'focus_ids': []}
    later['fairness_history'] = read_history(later, [goal], data)
    after = next(row for row in f.goal_report(later, goal)['people'] if row['person_id'] == A)
    assert (after['prior'], after['available_days']) == (2, 5)
