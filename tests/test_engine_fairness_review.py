"""Regressions for the independent engine review's fairness findings (4–6)."""
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest
from ortools.sat.python import cp_model

from apps.api.services import fairness as service
from packages.contracts.schemas import PolicyDefinition
from packages.domain import fairness as f
from packages.domain.cp_sat_goals import add_balance_goals


def uid(number):
    return str(UUID(int=number))


A, B, T, DEPT, POLICY = [uid(n) for n in range(1, 6)]


def duty(number, day, people=(), services=()):
    day = day if isinstance(day, str) else f'2026-10-{day:02}'
    start, end = day + 'T08:00:00+00:00', day + 'T12:00:00+00:00'
    return {'id': uid(100 + number), 'start': start, 'end': end, 'cancelled': False,
            'assignment_type_id': T, 'service_ids': list(services), 'location_id': None,
            'type_snapshot': {'name': 'Synthetic duty', 'definition': {'session_units': 2, 'requirements': [{'minimum': 1}]}},
            'participants': [{'person_id': pid, 'start': start, 'end': end, 'participation': 'regular',
                              'overtime': False, 'team_lead': False} for pid in people]}


def world(shifts=(), first='2026-10-01', last='2026-11-01'):
    return {'organisation': {'timezone': 'UTC', 'week_start': 0},
            'workspace': {'department_id': DEPT, 'starts_on': first, 'ends_on': last},
            'people': {pid: {'display_name': pid[-1], 'tags': []} for pid in (A, B)},
            'memberships': [{'person_id': pid, 'department_id': DEPT, 'starts_on': '2020-01-01'} for pid in (A, B)],
            'types': {T: {'department_id': DEPT}}, 'categories': {}, 'grants': [],
            'rotations': [], 'availability': [], 'policies': [], 'lead_balance': 'off',
            'shifts': list(shifts), 'focus_ids': [shift['id'] for shift in shifts]}


def policy(rule, scope=None):
    definition = PolicyDefinition.model_validate({'severity': 'advisory', 'scope': scope or {},
                                                  'rule': {'kind': 'balance_goal', **rule}},
                                                 context={'historical': True}).model_dump(mode='json')
    return {'id': POLICY, 'name': 'Synthetic fairness goal', 'department_id': DEPT, 'status': 'active',
            'active': True, 'starts_on': '2020-01-01', 'ends_on': None, 'definition': definition}


@pytest.mark.parametrize('metric', ['duties', 'hours'])
@pytest.mark.parametrize('aim', ['equal_share', 'max_min', 'target'])
def test_prepared_goal_tolerance_preserves_cost_status_and_explicit_zero(metric, aim):
    expected = 2.75
    tolerance = f.band(metric, expected)[2]
    for value in (0, expected - tolerance, expected, expected + tolerance, expected + tolerance + .125):
        gap = max(0.0, expected - value) if aim == 'max_min' else abs(value - expected)
        cost = min(1.0, max(0.0, gap - tolerance) / expected)
        status = 'within' if gap <= tolerance else 'above' if value > expected else 'below'
        for prepared in (None, tolerance):
            assert f.outside(metric, value, expected, aim, tolerance=prepared) == cost
            assert f.status(metric, value, expected, aim, tolerance=prepared) == status
        assert f.outside(metric, value, expected, aim) == cost
        assert f.status(metric, value, expected, aim) == status
    value = expected - .125
    assert f.outside(metric, value, expected, aim, tolerance=0) == .125 / expected
    assert f.status(metric, value, expected, aim, tolerance=0) == 'below'
    assert f.outside(metric, value, expected, aim, tolerance=None) == 0
    assert f.status(metric, value, expected, aim, tolerance=None) == 'within'


@pytest.mark.parametrize('aim', ['equal_share', 'max_min', 'target'])
def test_cost_only_ordinary_report_keeps_canonical_costs_and_source_weights(aim, monkeypatch):
    import json

    data = world([duty(1, 5, [A]), duty(2, 6, [B])], '2026-10-05', '2026-10-19')
    sources = [policy({'metric': 'duties', 'aim': aim, 'weight': weight,
                       **({'amount': 2.75} if aim == 'target' else {})}, {'person_ids': [person]})
               for person, weight in ((A, 2.375), (B, .625))]
    for index, (source, strength) in enumerate(zip(sources, ('high', 'medium'))):
        source['definition']['strength'] = strength
        source['template_line'] = {'id': uid(40 + index), 'line': str(index + 1), 'slot': 'apply'}
    goal = f.comparison_goal(sources)
    data['policies'] = sources
    data['fairness_history'] = {f.goal_identity(goal): {'people': {
        A: {'value': .375, 'available': 2}, B: {'value': 1.125, 'available': 3}}}}
    data['_distribution_cache'] = {}
    counts = ({A: 3.125, B: .4375}, {})
    public = f.goal_report(data, goal, counts)
    serialized = json.dumps(public)

    def display_used(*args, **kwargs):
        pytest.fail('Cost-only trials must not format public report fields or its display score')

    with monkeypatch.context() as patches:
        for name in ('round', 'status', 'share_value', 'report_weights'):
            patches.setattr(f, name, display_used, raising=False)
        projected = f.goal_report(data, goal, counts, _cost_only=True)
    assert projected == {**{key: value for key, value in public.items() if key != 'score'},
                         'people': [{key: row[key] for key in ('person_id', 'cost', 'sources')}
                                    for row in public['people']]}
    strengths = {'high': 9, 'medium': 3, 'low': 1}
    assert f.report_weights(projected, strengths) == f.report_weights(public, strengths)
    assert f.report_value(projected, strengths) == f.report_value(public, strengths)
    assert json.dumps(f.goal_report(data, goal, counts)) == serialized


def test_rolling_goal_counts_only_the_configured_last_days():
    data = world([duty(1, 1, [A]), duty(2, 30, [B])])
    goal = policy({'metric': 'duties', 'period': 'rolling', 'period_days': 7})
    report = f.goal_report(data, goal)
    assert {row['person_id']: row['planned'] for row in report['people']} == {A: 0, B: 1}
    assert f.period_bounds(goal['definition']['rule'], date(2026, 10, 1), date(2026, 11, 1)) == (
        date(2026, 10, 25), date(2026, 11, 1))
    assert {row['available_days'] for row in report['people']} == {7}


def test_custom_goal_excludes_duties_before_and_after_its_dates():
    data = world([duty(1, 1, [A]), duty(2, 15, [B]), duty(3, 30, [A])])
    goal = policy({'metric': 'duties', 'period': 'custom', 'starts_on': '2026-10-10', 'ends_on': '2026-10-20'})
    report = f.goal_report(data, goal)
    assert {row['person_id']: row['planned'] for row in report['people']} == {A: 0, B: 1}
    assert {row['available_days'] for row in report['people']} == {10}


@pytest.mark.parametrize(('ends_on', 'expected'), [
    ('2026-10-10', {B: (7, 1)}),
    ('2026-10-28', {A: (3, 0.6), B: (7, 1.4)}),
])
def test_rolling_goal_audience_and_available_days_match_dated_person_scope(ends_on, expected):
    programme = uid(30)
    data = world([duty(1, 27, [A]), duty(2, 30, [B])])
    data['programmes'] = {programme: {'active': True, 'starts_on': '2020-01-01'}}
    data['programme_memberships'] = [{'person_id': pid, 'programme_id': programme, 'starts_on': '2026-10-01',
                                     'ends_on': ends_on if pid == A else None} for pid in (A, B)]
    scope = {'conditions': {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [programme]}]}}
    goal = policy({'metric': 'duties', 'period': 'rolling', 'period_days': 7}, scope)
    report = f.goal_report(data, goal)
    assert {row['person_id']: (row['available_days'], row['expected']) for row in report['people']} == expected


def test_catalogue_goal_lines_keep_their_own_history():
    lines = [{'id': uid(20 + i), 'numbers': {'apply': {'value': None, 'level': 'medium'}},
              'settings': {'balance_goal': {'metric': metric, 'period': 'month'}}}
             for i, metric in enumerate(('duties', 'hours'))]
    catalogue = {'id': POLICY, 'name': 'Two fairness measures', 'kind': 'catalogue', 'revision': 1,
                 'department_id': DEPT, 'status': 'active', 'active': True, 'starts_on': '2020-01-01',
                 'ends_on': None, 'definition': {'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': lines}}}
    data = world([], first='2026-10-11', last='2026-10-18')
    data['policies'] = [catalogue]
    goals = f.balance_goals(data)
    ledger = f.ledger_facts(world([duty(1, 4, [A])], first='2026-10-01', last='2026-10-11'))

    db = SimpleNamespace(execute=lambda _query: [(key, pid, other, day, value)
                                                for pid, other, key, day, value in ledger])
    data['fairness_history'] = service.history(db, data, goals)
    reports = {goal['definition']['rule']['metric']: f.goal_report(data, goal) for goal in goals}
    assert {metric: next(row['prior'] for row in report['people'] if row['person_id'] == A)
            for metric, report in reports.items()} == {'duties': 1, 'hours': 4}
    assert len(data['fairness_history']) == 2
    assert {report['policy_id'] for report in reports.values()} == {POLICY}


def test_run_without_lead_balance_keeps_catalogue_fairness_history(h):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.snapshots import domain_snapshot
    from .test_rule_catalogue_api import approve, line, propose
    from .test_team_lead import anyone_leads, disable_lead_balance, lead_goal, week

    saved = approve(h, propose(h, 'fair_share', [line(
        numbers={'apply': {'value': None, 'level': 'medium'}},
        settings={'balance_goal': {'metric': 'duties', 'period': 'month'}})])['record'])
    first, second = h.seed['people'][:2]
    typ = anyone_leads(h)
    h.publish(week(h, typ, 'Published catalogue history', 4, lambda s, e: [
        {'person_id': first, 'start': s, 'end': e, 'team_lead': True}, second], days=2))
    workspace = week(h, typ, 'Run without lead balance', 11, lambda s, e: [first, second], days=2)
    with transaction(h.app.state.engine) as db:
        data = domain_snapshot(db, db.get(m.Workspace, workspace['id']))
    lead_key = f.goal_identity(lead_goal(data, typ))
    assert data['fairness_history'][lead_key]['people'][first]['value'] == 2
    disable_lead_balance(data, typ)
    [goal] = [goal for goal in f.balance_goals(data) if goal['definition']['rule']['metric'] == 'duties']
    key = f.goal_identity(goal)
    assert goal['id'] == saved['id'] and key != saved['id']
    assert data['fairness_history'][key]['people'][first]['value'] == 2
    report = f.goal_report(data, goal)
    assert report['history']
    assert next(row['prior'] for row in report['people'] if row['person_id'] == first) == 2
    assert f.goal_people(data, lead_goal(data, typ)) == {'who': [], 'partners': []}
    assert data['fairness_history'][lead_key]['people'][first]['value'] == 2


def test_goal_comparison_preserves_catalogue_lines_with_the_same_parent():
    from apps.api.services.planner_tools import score_detail
    from packages.domain.score import COMPONENTS

    score = {'score': 100, 'components': {component: {'value': 1} for component in COMPONENTS}}
    goals = [{'policy_id': POLICY, 'goal_id': POLICY + ':' + uid(20 + i) + ':apply', 'name': 'Fair share',
              'unit': metric, 'score': 70 + i} for i, metric in enumerate(('duties', 'h'))]
    before = SimpleNamespace(validation={'score': score, 'goals': goals})
    after = SimpleNamespace(validation={'score': score, 'goals': [{**goal, 'score': goal['score'] + 5} for goal in goals]})
    comparison = score_detail(None, before, after)['goals']
    assert len(comparison) == 2
    assert {row['unit']: row['delta'] for row in comparison} == {'duties': 5, 'h': 5}
    assert {row['policy_id'] for row in comparison} == {POLICY}


def test_fairness_intervals_split_at_catalogue_selector_source_dates():
    everyone, personal = uid(20), uid(21)
    settings = {'balance_goal': {'metric': 'duties', 'period': 'rota'}}
    data = world([duty(1, 5, [A]), duty(2, 12, [A])], '2026-10-04', '2026-10-18')
    data['policies'] = [{'id': POLICY, 'name': 'Dated fair share lines', 'kind': 'catalogue', 'revision': 1,
        'department_id': DEPT, 'status': 'active', 'active': True, 'starts_on': '2020-01-01', 'ends_on': None,
        'definition': {'rule': {'kind': 'catalogue', 'id': 'fair_share', 'lines': [
            {'id': everyone, 'numbers': {'apply': {'value': None, 'level': 'medium'}}, 'settings': settings},
            {'id': personal, 'numbers': {'apply': {'value': None, 'level': 'medium'}}, 'settings': settings,
             'starts_on': '2026-10-10', 'who': {'match': 'all', 'conditions': [
                 {'field': 'person_ids', 'op': 'any', 'values': [A]}]}},
        ]}}}]
    goal = next(goal for goal in f.balance_goals(data) if goal['template_line']['id'] == everyone)
    assert f.goal_intervals(data, goal, A, *f.rota_dates(data)) == [(date(2026, 10, 4), date(2026, 10, 10))]
    row = next(row for row in f.goal_report(data, goal)['people'] if row['person_id'] == A)
    assert (row['planned'], row['available_days']) == (1, 6)


@pytest.mark.parametrize(('metric', 'expected'), [('duties', 3), ('hours', 12), ('session_units', 6), ('pair_exposure', 3)])
def test_shared_and_separate_clinic_duties_have_the_same_counts_after_publication(h, metric, expected):
    a, b, _ = h.seed['people']
    clinics = h.seed['services'][:2]
    typ = h.configure('assignment_types', {'name': 'Shared synthetic clinic duty', 'starts_on': '2020-01-01',
        'definition': {'session_units': 2, 'requirements': [{'minimum': 2}], 'allow_shared_services': True,
                       'shared_duty_reason': 'Synthetic shared coverage'}})['record']['id']
    rule = {'kind': 'balance_goal', 'metric': metric, 'period': 'month', 'service_ids': clinics}
    if metric == 'pair_exposure':
        rule.update(aim='min_per_pair', amount=3,
                    partners={'match': 'all', 'conditions': [{'field': 'person_ids', 'op': 'any', 'values': [b]}]})
    saved = h.policy(rule, scope={'person_ids': [a]}, severity='advisory')
    workspace = h.workspace()
    for day, services in ((4, clinics), (5, clinics[:1]), (6, clinics[1:])):
        start, end = f'2026-10-{day:02}T08:00:00+03:00', f'2026-10-{day:02}T12:00:00+03:00'
        workspace = h.command('coordinator', 'shift.create', {
            'workspace_id': workspace['id'], 'assignment_type_id': typ, 'location_id': h.seed['locations'][0],
            'service_ids': services, 'start': start, 'end': end,
            'participants': [{'person_id': pid, 'start': start, 'end': end} for pid in (a, b)]},
            revision=workspace['revision'])['workspace']
    data = h.snapshot(workspace)
    goal = next(goal for goal in data['policies'] if goal['id'] == saved['id'])
    before = f.goal_report(data, goal)
    rows = before['pairs'] if metric == 'pair_exposure' else before['people']
    assert next(row['planned'] for row in rows if row['person_id'] == a) == expected
    h.publish(workspace)
    later = h.snapshot(h.workspace(start='2026-10-11', end='2026-10-18'))
    goal = next(goal for goal in later['policies'] if goal['id'] == saved['id'])
    after = f.goal_report(later, goal)
    rows = after['pairs'] if metric == 'pair_exposure' else after['people']
    assert next(row['prior'] for row in rows if row['person_id'] == a) == expected
    dashboard = h.get('coordinator', '/api/fairness/goals?workspace_id=' + later['workspace']['id'])
    card = next(goal for goal in dashboard['goals'] if goal['policy_id'] == saved['id'])
    rows = card['pairs'] if metric == 'pair_exposure' else card['people']
    assert next(row['prior'] for row in rows if row['person_id'] == a) == expected
    filters = '&starts_on=2026-10-01&ends_on=2026-11-01' + ''.join('&service_ids=' + clinic for clinic in clinics)
    if metric == 'pair_exposure':
        exposure = h.get('coordinator', '/api/fairness/exposure?' + filters[1:])
        row = next(i for i, person in enumerate(exposure['rows']) if person['person_id'] == a)
        column = next(i for i, person in enumerate(exposure['cols']) if person['person_id'] == b)
        assert exposure['cells'][row][column] == expected
    else:
        spread = h.get('coordinator', '/api/fairness/distribution?metric=' + metric + filters + '&group_by=service_ids')
        assert next(row['value'] for row in spread['people'] if row['person_id'] == a) == expected
        assert {group['key']: group['value'] for group in spread['groups']} == {clinic: 4 * expected / 3 for clinic in clinics}
    drill = h.get('coordinator', '/api/fairness/sessions?person_id=' + a + '&metric=' + metric + filters
                  + ('&other_person_id=' + b if metric == 'pair_exposure' else ''))
    [published] = drill['publications']
    assert published['value'] == expected and len(published['sessions']) == 3


def test_dated_fairness_scope_counts_the_same_work_and_days_before_and_after_publication(h):
    a = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Dated fairness audience', 'starts_on': '2020-01-01'})['record']['id']
    h.configure('programme_memberships', {'person_id': a, 'programme_id': programme, 'starts_on': '2026-10-05',
                                         'ends_on': '2026-10-07'}, role='coordinator')
    scope = {'conditions': {'match': 'all', 'conditions': [{'field': 'programme_ids', 'op': 'any', 'values': [programme]}]}}
    saved = h.policy({'kind': 'balance_goal', 'metric': 'duties', 'period': 'custom',
                      'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, scope=scope, severity='advisory')
    workspace = h.workspace()
    for day in (4, 5, 8):
        workspace = h.shift(workspace, start=f'2026-10-{day:02}T08:00:00+03:00',
                            end=f'2026-10-{day:02}T12:00:00+03:00', people=[a])['workspace']
    data = h.snapshot(workspace)
    goal = next(goal for goal in data['policies'] if goal['id'] == saved['id'])
    [before] = f.goal_report(data, goal)['people']
    assert (before['planned'], before['available_days']) == (1, 2)
    h.publish(workspace)
    data = h.snapshot(h.workspace(start='2026-10-11', end='2026-10-18'))
    goal = next(goal for goal in data['policies'] if goal['id'] == saved['id'])
    [after] = f.goal_report(data, goal)['people']
    assert (after['prior'], after['available_days']) == (1, 2)


@pytest.mark.parametrize(('rule', 'expected'), [
    ({'period': 'rota'}, ('2026-10-11', '2026-10-18')),
    ({'period': 'month'}, ('2026-10-01', '2026-11-01')),
    ({'period': 'quarter'}, ('2026-10-01', '2027-01-01')),
    ({'period': 'year'}, ('2026-01-01', '2027-01-01')),
    ({'period': 'rolling', 'period_days': 28}, ('2026-09-20', '2026-10-18')),
    ({'period': 'block'}, ('2026-10-01', '2026-11-01')),
    ({'period': 'custom', 'starts_on': '2026-09-01', 'ends_on': '2026-12-01'}, ('2026-09-01', '2026-12-01')),
])
def test_existing_period_boundaries(rule, expected):
    assert f.period_bounds(rule, date(2026, 10, 11), date(2026, 10, 18),
                           (date(2026, 10, 1), date(2026, 11, 1))) == tuple(map(date.fromisoformat, expected))


def test_available_day_prorating_keeps_leave_control():
    data = world([], first='2026-10-11', last='2026-10-18')
    data['availability'] = [{'person_id': A, 'kind': 'approved_leave', 'start': '2026-10-11T00:00:00+00:00',
                             'end': '2026-10-14T00:00:00+00:00'}]
    goal = policy({'metric': 'duties', 'period': 'rota'})
    rows = {row['person_id']: row for row in f.goal_report(data, goal, planned_values=({A: 2, B: 5}, {}))['people']}
    assert (rows[A]['available_days'], rows[B]['available_days']) == (4, 7)
    assert (rows[A]['expected'], rows[B]['expected']) == (2.55, 4.45)


@pytest.mark.parametrize(('first', 'last', 'rule', 'inside', 'outside', 'available', 'target'), [
    ('2026-10-28', '2026-11-04', {'period': 'month'}, '2026-10-30', '2026-11-02', 4, 31),
    ('2026-12-28', '2027-01-04', {'period': 'quarter'}, '2026-12-30', '2027-01-02', 4, 92),
    ('2026-12-28', '2027-01-04', {'period': 'year'}, '2026-12-30', '2027-01-02', 4, 365),
    ('2026-10-01', '2026-11-01', {'period': 'custom', 'starts_on': '2026-10-28', 'ends_on': '2026-11-07'},
     '2026-10-30', '2026-10-02', 4, 4),
    ('2026-10-01', '2026-11-01', {'period': 'custom', 'starts_on': '2026-09-20', 'ends_on': '2026-10-05'},
     '2026-10-02', '2026-10-30', 4, 15),
])
def test_straddling_periods_clip_planned_days_and_target_progress(first, last, rule, inside, outside, available, target):
    data = world([duty(1, inside, [A]), duty(2, outside, [A])], first, last)
    start, end = f.period_bounds(rule, date.fromisoformat(first), date.fromisoformat(last))
    goal = policy({'metric': 'duties', 'aim': 'target', 'amount': (end - start).days, **rule})
    row = next(row for row in f.goal_report(data, goal)['people'] if row['person_id'] == A)
    assert (row['planned'], row['available_days'], row['expected']) == (1, available, target)


@pytest.mark.parametrize(('start', 'end', 'expected_history', 'expected_target'), [
    ('2026-09-01', '2026-09-10', (date(2026, 9, 1), date(2026, 9, 10)), 9),
    ('2026-11-01', '2026-11-10', None, 0),
])
def test_disjoint_custom_periods_do_not_widen_into_the_rota(start, end, expected_history, expected_target):
    data = world([duty(1, 15, [A])])
    goal = policy({'metric': 'duties', 'aim': 'target', 'amount': 9, 'period': 'custom', 'starts_on': start, 'ends_on': end})
    report = f.goal_report(data, goal)
    assert all(row['planned'] == 0 and row['available_days'] == 0 and row['expected'] == expected_target
               for row in report['people'])
    assert f.period_bounds(goal['definition']['rule'], date(2026, 10, 1), date(2026, 11, 1)) == (
        date.fromisoformat(start), date.fromisoformat(end))
    assert f.history_windows(data, goal).get(A) == expected_history


def test_block_period_uses_each_persons_starting_rotation_boundaries():
    data = world([duty(1, 4, [A]), duty(2, 8, [A, B])], '2026-10-04', '2026-10-11')
    data['rotations'] = [
        {'person_id': A, 'starts_on': '2026-10-04', 'ends_on': '2026-10-06'},
        {'person_id': A, 'starts_on': '2026-10-06', 'ends_on': '2026-10-20'},
        {'person_id': B, 'starts_on': '2026-10-04', 'ends_on': '2026-10-09'},
    ]
    goal = policy({'metric': 'duties', 'period': 'block'})
    rows = {row['person_id']: row for row in f.goal_report(data, goal)['people']}
    assert (rows[A]['planned'], rows[B]['planned']) == (1, 1)
    assert (rows[A]['available_days'], rows[B]['available_days']) == (2, 5)


@pytest.mark.parametrize('metric', ['duties', 'hours', 'weekends', 'pair_exposure'])
def test_solver_goal_penalty_ignores_outside_period_contributions(metric):
    data = world([duty(1, 2, [A, B]), duty(2, 30, [A, B])])
    rule = {'metric': metric, 'period': 'rolling', 'period_days': 7, 'aim': 'target', 'amount': 2}
    if metric == 'pair_exposure':
        rule.update(aim='min_per_pair', partners={'match': 'all', 'conditions': [
            {'field': 'person_ids', 'op': 'any', 'values': [B]}]})
    goal = policy(rule, scope={'person_ids': [A]})
    data['policies'] = [goal]
    penalties = []
    for outside_selected in (False, True):
        model, choices, participants = cp_model.CpModel(), {}, {}
        for shift in data['shifts']:
            for participant in shift['participants']:
                key = shift['id'], participant['person_id']
                choices[key] = model.new_bool_var(str(key))
                participants[key] = participant
                model.add(choices[key] == (int(outside_selected) if shift == data['shifts'][0] else 1))
        terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), {'medium': 3})
        model.minimize(sum(terms))
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        solver.parameters.max_time_in_seconds = 1
        assert solver.solve(model) == cp_model.OPTIMAL
        penalties.append(solver.objective_value)
    assert penalties[0] == penalties[1]


def test_counted_solver_goals_keep_same_day_clinics_separate():
    clinics = [uid(40), uid(41)]
    data = world([duty(1, 15, [A], [clinics[0]]), duty(2, 15, [A], [clinics[1]])])
    data['policies'] = [policy({'metric': 'duties', 'period': 'rota', 'aim': 'target', 'amount': 1},
        scope={'person_ids': [A], 'conditions': {'conditions': [
            {'field': 'service_ids', 'op': 'any', 'values': clinics, 'count': 'each'}]}})]
    model, choices, participants = cp_model.CpModel(), {}, {}
    for shift in data['shifts']:
        [participant] = shift['participants']
        key = shift['id'], A
        choices[key] = model.new_bool_var(str(key))
        participants[key] = participant
        model.add(choices[key] == 1)
    terms, _ = add_balance_goals(model, data, choices, participants, {}, {}, set(data['focus_ids']), {'medium': 3})
    model.minimize(sum(terms))
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 1
    assert solver.solve(model) == cp_model.OPTIMAL
    assert solver.objective_value == 0
