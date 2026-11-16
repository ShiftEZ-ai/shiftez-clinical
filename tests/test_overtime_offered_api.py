"""Offered-hour projections share the domain basis across insights, workload and exports."""
import csv
import io
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.auth import authenticate
from apps.api.db import transaction
from apps.api.services.fairness import department_data
from apps.api.snapshots import scheduling_inputs
from packages.domain.fairness import balance_goals, goal_identity, goal_report
from packages.domain.time import aware
from tests.test_scheduling_facts_api import decide


def offer(h, person, first, last, start, end, previous=None):
    row = h.command('coordinator', 'person.overtime.propose', {
        'kind': 'overtime_offer', 'person_id': person, 'catalogue_id': 'overtime_offers',
        'line': {'id': previous['data']['line']['id'] if previous else str(uuid.uuid4()),
                 'starts_on': first, 'ends_on': last, 'start_time': start, 'end_time': end,
                 'numbers': {'apply': {'value': None, 'level': 'medium'}},
                 'settings': {'allocation_overtime': {'volunteer_required': False}}},
        **({'replaces_id': previous['id'], 'expected_revision': previous['revision']} if previous else {}),
        'reason': 'Synthetic dated overtime offer'})['fact']
    return decide(h, row)['fact']


def absence(h, person, start, end, **extra):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Availability(department_id=h.seed['department_id'], person_id=person,
            start=aware(start), end=aware(end), kind='recorded_leave',
            private_reason='Synthetic absence', recorded_by=h.seed['identities']['coordinator'], **extra))


@pytest.mark.parametrize('metric', ['overtime_duties', 'overtime_hours'])
def test_rota_only_personal_target_does_not_break_overtime_distribution(h, metric):
    person = h.seed['people'][0]
    own = h.workspace()
    other = h.workspace(name='Another synthetic rota', start='2026-10-11', end='2026-10-18')
    spec = {'kind': 'balance_goal', 'metric': metric, 'aim': 'target', 'amount': 10,
            'period': 'custom', 'starts_on': own['starts_on'], 'ends_on': own['ends_on']}
    goal = h.policy(spec, severity='advisory', workspace_id=own['id'])
    target = h.command('coordinator', 'person.workload.propose', {
        'kind': 'target', 'person_id': person, 'catalogue_id': 'fair_share',
        'goal_ref': {'policy_id': goal['id'], 'revision': goal['revision']},
        'line': {'id': str(uuid.uuid4()), 'numbers': {'target': {'value': 7, 'level': 'medium'}},
                 'settings': {'balance_goal': {k: v for k, v in spec.items() if k not in ('kind', 'amount')}}},
        'reason': 'Synthetic personal target for one rota'})['fact']
    decide(h, target)
    offer(h, person, own['starts_on'], own['ends_on'], '08:00', '16:00')
    for workspace, expected in ((own, 56), (other, 0)):
        distribution = h.get('coordinator', '/api/fairness/distribution?metric=' + metric
                             + '&starts_on=' + workspace['starts_on'] + '&ends_on=' + workspace['ends_on'])
        if workspace == own:
            assert next(r for r in distribution['people'] if r['person_id'] == person)['overtime_offered_hours'] == expected
        else:
            assert distribution['people'] == []
    first, last = date.fromisoformat(own['starts_on']), date.fromisoformat(own['ends_on'])
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'])
        for workspace_id in (own['id'], other['id'], None):
            data = department_data(db, actor, first, last, include_offers=True, workspace_id=workspace_id)
            goals = [p for p in balance_goals(data) if p['id'] == goal['id']]
            if workspace_id == own['id']:
                [bound] = goals
                assert bound['definition']['rule']['person_amounts'] == {person: 7}
            else:
                assert not goals


def test_offer_only_distribution_qualifies_offered_people(h):
    eligible, inactive = h.seed['people'][:2]
    offer(h, eligible, '2026-10-04', '2026-10-05', '08:00', '16:00')
    offer(h, inactive, '2026-10-04', '2026-10-05', '08:00', '16:00')
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, inactive).active = False
    rows = h.get('coordinator', '/api/fairness/distribution?metric=overtime_hours'
                 '&starts_on=2026-10-01&ends_on=2026-11-01')['people']
    ids = {row['person_id'] for row in rows}
    assert eligible in ids and inactive not in ids


@pytest.mark.parametrize('metric,expected', [('overtime_duties', (1, 2)), ('overtime_hours', (4, 8))])
def test_dashboard_workload_and_csv_use_dated_union_less_unavailability(h, metric, expected):
    a, b, c = h.seed['people']
    offer(h, a, '2026-10-04', '2026-10-05', '08:00', '12:00')
    offer(h, a, '2026-10-04', '2026-10-05', '10:00', '14:00')
    offer(h, b, '2026-10-04', '2026-10-05', '08:00', '16:00')
    offer(h, a, '2026-11-04', '2026-11-05', '00:00', '23:00')
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    workspace = h.shift(h.workspace(), start, end,
        people=[{'person_id': p, 'start': start, 'end': end, 'overtime': True} for p in (a, b, c)])['workspace']
    publication = h.publish(workspace)['publication_id']
    report_url = '/api/reports/' + publication
    issued = h.get('coordinator', report_url)
    assert issued['metric_version'] == 'scheduled-v3'
    absence(h, a, '2026-10-04T10:00:00+03:00', '2026-10-04T12:00:00+03:00')
    absence(h, b, '2026-10-04T08:00:00+03:00', '2026-10-04T16:00:00+03:00', counts_as_unavailable=False)
    absence(h, b, '2026-10-04T08:00:00+03:00', '2026-10-04T16:00:00+03:00', retracted=True)

    url = '/api/fairness/distribution?metric=' + metric + '&starts_on=2026-10-01&ends_on=2026-11-01'
    distribution = h.get('coordinator', url)
    rows = {row['person_id']: row for row in distribution['people']}
    assert distribution['share_by'] == 'offered'
    assert [rows[p]['overtime_offered_hours'] for p in (a, b, c)] == [4, 8, 0]
    assert [rows[p]['expected'] for p in (a, b, c)] == [*expected, 0]
    assert rows[c]['status'] == 'within'
    assert [row['person_id'] for row in h.get('staff', url)['people']] == [a]

    person = h.get('staff', '/api/people/' + a + '/workload?on=2026-10-06')
    assert all(period['totals']['overtime_offered_hours'] == 4 for period in person['periods'])
    report = h.get('coordinator', report_url)
    assert report['rows'] == issued['rows']
    assert {r['person_id']: r['overtime_offered_hours'] for r in report['rows']} == {a: 6, b: 8, c: 0}
    text = h.client('coordinator').get(report_url + '?format=csv').text
    exported = list(csv.DictReader(io.StringIO(text.split('\n', 1)[1])))
    assert {r['person_id']: float(r['overtime_offered_hours']) for r in exported} == {a: 6, b: 8, c: 0}
    own = h.get('staff', report_url)
    assert [(r['person_id'], r['overtime_offered_hours']) for r in own['rows']] == [(a, 6)]
    snapshot = h.get('staff', '/api/publications/' + publication)['snapshot']
    assert set(snapshot['overtime_offered_intervals']) == {a}


@pytest.mark.parametrize('version', ['scheduled-v1', 'scheduled-v2'])
def test_legacy_publications_keep_issued_report_rows_and_csv_columns(h, monkeypatch, version):
    from apps.api.services import planning
    from packages.domain.reports import report_csv, workload, workload_breakdown

    original = planning.publication_snapshot

    def legacy_snapshot(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshot['metric_version'] = version
        del snapshot['overtime_offered_intervals']
        return snapshot

    monkeypatch.setattr(planning, 'publication_snapshot', legacy_snapshot)
    person = h.seed['people'][0]
    offer(h, person, '2026-10-04', '2026-10-05', '08:00', '16:00')
    publication = h.publish(h.shift(h.workspace())['workspace'])['publication_id']
    snapshot = h.get('coordinator', '/api/publications/' + publication)['snapshot']
    for grouping in (None, 'week', 'month'):
        expected = workload_breakdown(snapshot, grouping=grouping) if grouping else workload(snapshot)
        url = '/api/reports/' + publication + ('?grouping=' + grouping if grouping else '')
        for role in ('coordinator', 'staff'):
            report = h.get(role, url)
            assert report['rows'] == expected['rows']
            assert all('overtime_offered_hours' not in row for row in report['rows'])
            csv_text = h.client(role).get(url + ('&' if grouping else '?') + 'format=csv').text
            assert csv_text.splitlines()[1:] == report_csv(expected, publication, 1).splitlines()[1:]
            assert 'overtime_offered_hours' not in csv_text


@pytest.mark.parametrize('grouping,offered_on,period_start', [
    ('week', '2026-10-20', '2026-10-18'),
    ('month', '2026-11-04', '2026-11-01'),
])
def test_grouped_reports_include_offer_only_periods_and_preserve_access(h, grouping, offered_on, period_start):
    a, b = h.seed['people'][:2]
    offer(h, a, '2026-10-04', '2026-10-05', '08:00', '16:00')
    last = (date.fromisoformat(offered_on) + timedelta(days=1)).isoformat()
    offer(h, a, offered_on, last, '08:00', '16:00')
    offer(h, b, offered_on, last, '08:00', '12:00')
    workspace = h.shift(h.workspace(start='2026-10-01', end='2026-12-01'), people=[a, b])['workspace']
    publication = h.publish(workspace)['publication_id']
    url = '/api/reports/' + publication
    measures = ('assignments', 'scheduled_hours', 'workload_credit', 'training_credit',
                'planned_overtime_assignments', 'planned_overtime_hours', 'reserved_hours', 'off_cover_hours', 'lead_turns')
    for role, query, expected in (('coordinator', '?', {a: 16, b: 4}),
                                  ('coordinator', '?person_id=' + a + '&', {a: 16}),
                                  ('staff', '?', {a: 16})):
        total = h.get(role, url + query)
        report = h.get(role, url + query + 'grouping=' + grouping)
        assert {r['person_id']: r['overtime_offered_hours'] for r in total['rows']} == expected
        assert set(r['person_id'] for r in report['rows']) == set(expected)
        assert {pid: sum(r['overtime_offered_hours'] for r in report['rows'] if r['person_id'] == pid)
                for pid in expected} == expected
        offer_only = [r for r in report['rows'] if r['period_start'] == period_start]
        assert {r['person_id']: r['overtime_offered_hours'] for r in offer_only} == {
            pid: 8 if pid == a else 4 for pid in expected}
        assert all(r[key] == 0 for r in offer_only for key in measures)
        response = h.client(role).get(url + query + 'grouping=' + grouping + '&format=csv')
        assert response.status_code == 200
        exported = list(csv.DictReader(io.StringIO(response.text.split('\n', 1)[1])))
        assert {pid: sum(float(r['overtime_offered_hours']) for r in exported if r['person_id'] == pid)
                for pid in expected} == expected
        assert set(r['person_id'] for r in exported) == set(expected)
        assert [(r['period_start'], r['person_id'], float(r['overtime_offered_hours'])) for r in exported] == [
            (r['period_start'], r['person_id'], r['overtime_offered_hours']) for r in report['rows']]
        assert all(float(r[key]) == 0 for r in exported if r['period_start'] == period_start for key in measures)
    h.get('staff', url + '?grouping=' + grouping + '&person_id=' + b, status=403)
    assert h.client('staff').get(url + '?grouping=' + grouping + '&person_id=' + b + '&format=csv').status_code == 403


def test_grouped_exports_clip_offers_at_each_month_boundary(h):
    person = h.seed['people'][0]
    offer(h, person, '2026-10-31', '2026-11-01', '20:00', '04:00')
    workspace = h.shift(h.workspace(start='2026-10-31', end='2026-11-02'),
        '2026-10-31T22:00:00+03:00', '2026-11-01T02:00:00+03:00', people=[person])['workspace']
    publication = h.publish(workspace)['publication_id']
    report = h.get('staff', '/api/reports/' + publication + '?grouping=month')
    # The shared overnight recurrence includes 00-04 and 20-24 on the one valid date.
    assert [r['overtime_offered_hours'] for r in report['rows']] == [8, 0]
    absence(h, person, '2026-10-31T20:00:00+03:00', '2026-11-01T04:00:00+03:00')
    assert h.get('staff', '/api/reports/' + publication + '?grouping=month')['rows'] == report['rows']


def test_replaced_publication_reports_clip_offers_to_in_force_days(h):
    person = h.seed['people'][0]
    for first, last in [('2026-10-04', '2026-10-05'), ('2026-10-20', '2026-10-21')]:
        offer(h, person, first, last, '08:00', '16:00')
    workspace = h.shift(h.workspace(start='2026-10-01', end='2026-11-01'), people=[person])['workspace']
    publication = h.publish(workspace)['publication_id']
    url = '/api/reports/' + publication
    assert h.get('staff', url)['rows'][0]['overtime_offered_hours'] == 16
    with transaction(h.app.state.engine) as db:
        snapshot = db.get(m.Publication, publication).snapshot
    replacement = h.command('coordinator', 'workspace.create', {
        'name': 'Synthetic replacement', 'starts_on': '2026-10-15', 'ends_on': '2026-11-01',
        'replaces_publication_id': publication})['workspace']
    replacement = h.shift(replacement, '2026-10-20T08:00:00+03:00', '2026-10-20T12:00:00+03:00',
                          people=[person])['workspace']
    h.publish(replacement)
    for query in ('', '?grouping=month'):
        report = h.get('staff', url + query)
        assert [(r['person_id'], r['overtime_offered_hours']) for r in report['rows']] == [(person, 8)]
        response = h.client('staff').get(url + query + ('&' if query else '?') + 'format=csv')
        assert response.status_code == 200
        rows = list(csv.DictReader(io.StringIO(response.text.split('\n', 1)[1])))
        assert [(r['person_id'], float(r['overtime_offered_hours'])) for r in rows] == [(person, 8)]
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Publication, publication)
        assert stored.effective_until == date(2026, 10, 15)
        assert stored.snapshot == snapshot


def test_quarter_history_reads_past_offers_and_past_leave_outside_neighbour_window(h):
    a, b, _ = h.seed['people']
    offer(h, a, '2026-10-04', '2026-10-05', '08:00', '16:00')
    offer(h, b, '2026-10-04', '2026-10-05', '08:00', '12:00')
    offer(h, a, '2026-12-15', '2026-12-16', '08:00', '10:00')
    offer(h, b, '2026-12-15', '2026-12-16', '08:00', '18:00')
    absence(h, a, '2026-10-04T10:00:00+03:00', '2026-10-04T12:00:00+03:00')
    policy = h.policy({'kind': 'balance_goal', 'metric': 'overtime_duties', 'aim': 'equal_share',
                       'period': 'quarter', 'share_by': 'offered'}, scope={'person_ids': [a, b]}, severity='advisory')
    workspace = h.workspace(start='2026-12-14', end='2026-12-21')
    # A matching duty establishes the goal's eligible audience without adding overtime work.
    workspace = h.shift(workspace, '2026-12-15T08:00:00+03:00',
                        '2026-12-15T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    goal = next(p for p in balance_goals(data) if p['id'] == policy['id'])
    historical = data['fairness_history'][goal_identity(goal)]['people']
    assert {p: historical[p]['offered'] for p in (a, b)} == {a: 6, b: 4}
    report = goal_report(data, goal)
    assert {r['person_id']: r['overtime_offered_hours'] for r in report['people']} == {a: 8, b: 14}
    dashboard = h.get('coordinator', '/api/fairness/goals?workspace_id=' + workspace['id'])
    goal = next(g for g in dashboard['goals'] if g['policy_id'] == policy['id'])
    assert {r['person_id']: r['overtime_offered_hours'] for r in goal['people']} == {a: 8, b: 14}


def test_replaced_offer_keeps_its_old_validity_for_quarter_history_and_exports(h):
    person = h.seed['people'][0]
    old = offer(h, person, '2026-10-04', '2026-10-05', '08:00', '16:00')
    publication = h.publish(h.shift(h.workspace(), people=[person])['workspace'])['publication_id']
    url = '/api/reports/' + publication
    issued = h.get('staff', url)
    csv_text = h.client('staff').get(url + '?format=csv').text
    offer(h, person, '2026-12-15', '2026-12-16', '08:00', '10:00', previous=old)
    policy = h.policy({'kind': 'balance_goal', 'metric': 'overtime_hours', 'period': 'quarter', 'share_by': 'offered'},
                      scope={'person_ids': [person]}, severity='advisory')
    workspace = h.workspace(start='2026-12-14', end='2026-12-21')
    data = h.snapshot(workspace)
    goal = next(p for p in balance_goals(data) if p['id'] == policy['id'])
    assert data['fairness_history'][goal_identity(goal)]['people'][person]['offered'] == 8
    assert goal_report(data, goal)['people'][0]['overtime_offered_hours'] == 10
    report = h.get('staff', '/api/reports/' + publication)
    assert report['rows'] == issued['rows']
    assert h.client('staff').get(url + '?format=csv').text == csv_text
    assert report['rows'][0]['overtime_offered_hours'] == 8


def test_retirement_history_is_opt_in_for_inputs_and_snapshots(h):
    person = h.seed['people'][0]
    row = offer(h, person, '2026-10-04', '2026-10-20', '08:00', '16:00')
    decide(h, row, 'retire')
    workspace = h.workspace(start='2026-10-04', end='2026-10-20')
    history_queries = []

    def queried(_connection, _cursor, statement, _parameters, _context, _many):
        if 'definition_history' in statement.lower():
            history_queries.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', queried)
    try:
        with transaction(h.app.state.engine) as db:
            facts = scheduling_inputs(db, [h.seed['department_id']])['scheduling_facts']
        snapshot = h.snapshot(workspace)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', queried)
    assert not history_queries
    assert 'retired_at' not in next(r for r in facts if r['id'] == row['id'])
    assert 'retired_at' not in next(r for r in snapshot['scheduling_facts'] if r['id'] == row['id'])
    with transaction(h.app.state.engine) as db:
        facts = scheduling_inputs(db, [h.seed['department_id']], include_offer_history=True)['scheduling_facts']
    assert next(r for r in facts if r['id'] == row['id'])['retired_at']


def test_retirement_metadata_uses_latest_transition_and_publication_time(h, monkeypatch):
    from apps.api.services import common
    from packages.domain.fairness import offered_amounts

    person = h.seed['people'][0]
    row = offer(h, person, '2026-10-04', '2026-10-20', '08:00', '16:00')
    h.policy({'kind': 'balance_goal', 'metric': 'overtime_hours', 'share_by': 'offered'},
             scope={'person_ids': [person]}, severity='advisory')
    clock = {'at': aware('2026-10-06T06:00:00+00:00')}
    original = common.history_many

    def record_at(db, actor, entries):
        original(db, actor, entries)
        for pending in db.new:
            if isinstance(pending, m.DefinitionHistory) and pending.kind == 'scheduling_fact':
                pending.created_at = clock['at']

    # Stamp synthetic evidence before INSERT, keeping audit immutability and auth time intact.
    monkeypatch.setattr(common, 'history_many', record_at)
    row = decide(h, row, 'retire')['fact']
    workspace = h.workspace(start='2026-10-04', end='2026-10-20')
    data = h.snapshot(workspace)
    assert offered_amounts(data, date(2026, 10, 4), date(2026, 10, 20), [person])[person] == 17
    dashboard = h.get('coordinator', '/api/fairness/distribution?metric=overtime_hours&starts_on=2026-10-04&ends_on=2026-10-20')
    assert next(r for r in dashboard['people'] if r['person_id'] == person)['overtime_offered_hours'] == 17
    clock['at'] = aware('2026-10-07T06:00:00+00:00')
    row = decide(h, row, 'undo')['fact']
    clock['at'] = aware('2026-10-08T06:00:00+00:00')
    decide(h, row, 'retire')
    with transaction(h.app.state.engine) as db:
        current = scheduling_inputs(db, [h.seed['department_id']], include_offer_history=True)['scheduling_facts']
        historical = scheduling_inputs(db, [h.seed['department_id']], aware('2026-10-06T12:00:00+00:00'),
                                       include_offer_history=True)['scheduling_facts']
    assert next(r for r in current if r['id'] == row['id'])['retired_at'].startswith('2026-10-08')
    assert next(r for r in historical if r['id'] == row['id'])['retired_at'].startswith('2026-10-06')
