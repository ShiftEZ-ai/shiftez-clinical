"""Ledger batching keeps domain values and the publication transaction on both databases."""
from collections import Counter
from datetime import date
from sqlite3 import SQLITE_LIMIT_VARIABLE_NUMBER

import pytest
from sqlalchemy import Text, cast, event, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.fairness import rebuild
from apps.api.snapshots import domain_snapshot
from packages.domain.fairness import balance_goals, goal_identity, goal_report, ledger_facts
from packages.domain.validation import ancestor_ids
from tests.test_fairness import at, goal, ledger, session


def test_ledger_metrics_follow_a_manual_oracle_and_calculate_duration_once(monkeypatch):
    from packages.domain import fairness

    person = {'person_id': 'person', 'start': '2026-10-04T08:00:00+03:00',
              'end': '2026-10-04T12:30:00+03:00', 'overtime': True, 'team_lead': True}
    shift = {'id': 'shift', 'start': person['start'], 'end': person['end'],
             'cancelled': False, 'assignment_type_id': 'type', 'service_ids': ['clinic'],
             'location_id': 'place', 'type_snapshot': {'definition': {'session_units': 1.25}}, 'participants': [person]}
    oracle = {'duties': 1, 'hours': 4.5, 'session_units': 1.25, 'overtime_duties': 1,
              'overtime_hours': 4.5, 'lead_turns': 1, 'days_worked': 1}
    assert {key: fairness.contribution(key, shift, person) for key in oracle} == oracle
    ordinary = {**person, 'overtime': False, 'team_lead': False}
    assert fairness._contributions(shift, ordinary) == {**oracle, 'overtime_duties': 0, 'overtime_hours': 0, 'lead_turns': 0}
    assert fairness.contribution('session_units', {**shift, 'type_snapshot': {'definition': {}}}, person) == 1
    with pytest.raises(KeyError, match='start'):
        fairness.contribution('duties', shift, {'end': person['end']})
    with pytest.raises(KeyError, match='unknown'):
        fairness.contribution('unknown', shift, person)

    calls, real = [], fairness.duration_hours
    monkeypatch.setattr(fairness, 'duration_hours', lambda a, b: calls.append((a, b)) or real(a, b))
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'workspace': {
        'department_id': 'department', 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'},
        'focus_ids': ['shift'], 'shifts': [shift], 'memberships': []}
    expected = Counter(('person', None, key + suffix, date(2026, 10, 4), value)
                       for key, value in oracle.items() for suffix in ('', '|type:type', '|service:clinic', '|location:place'))
    assert Counter(fairness.ledger_facts(data)) == expected
    assert calls == [(person['start'], person['end'])]


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_batched_ledger_preserves_facts_goals_and_rolls_back(database, request):
    h = request.getfixturevalue(database)
    a, b, _ = h.seed['people']
    policy = goal(h, metric='hours', aim='target', amount=2.5, period='rota', scope={'person_ids': [a, b]})
    workspace = session(h, h.workspace(), 4, [a, b])
    workspace = session(h, workspace, 5, [a], overtime=[a])
    publication_id = h.publish(workspace)['publication_id']

    with transaction(h.app.state.engine) as db:
        data = domain_snapshot(db, db.get(m.Workspace, workspace['id']))
    goals = balance_goals(data)
    [target] = [p for p in goals if p['id'] == policy['id']]
    assert {p['definition']['rule']['metric'] for p in goals} == {'workload', 'hours'}
    key, day = 'goal:' + goal_identity(target), date.fromisoformat(workspace['starts_on'])
    expected = [(*row, None) for row in ledger_facts(data)]
    for goal_policy in goals:
        report = goal_report(data, goal_policy)
        goal_key = 'goal:' + goal_identity(goal_policy)
        expected += [(None, None, goal_key, day, report['score'], 100.0)]
        expected += [(p['person_id'], None, goal_key, day, p['actual'], p['expected']) for p in report['people']]
    rows = ledger(h, publication_id=publication_id)
    assert Counter(rows) == Counter(expected)
    assert {r[5] for r in rows if r[2] == key and r[0] is not None} == {2.5}
    assert any(r[0] == a and r[1] == b and r[2] == 'pair' and r[5] is None for r in rows)

    with transaction(h.app.state.engine, write=True) as db:
        assert rebuild(db, db.get(m.Publication, publication_id)) == len(expected)
    assert Counter(ledger(h, publication_id=publication_id)) == Counter(expected)

    # A later publication step can fail after COPY succeeds: its delete and new rows must both roll back.
    with transaction(h.app.state.engine) as db:
        original_ids = set(db.scalars(select(m.FairnessLedger.id).where(m.FairnessLedger.publication_id == publication_id)))
    with pytest.raises(RuntimeError, match='Synthetic failure after ledger write'):
        with transaction(h.app.state.engine, write=True) as db:
            assert 0 < rebuild(db, db.get(m.Publication, publication_id), own_shifts=[]) < len(expected)
            assert db.scalar(select(m.FairnessLedger.id).where(m.FairnessLedger.publication_id == publication_id,
                m.FairnessLedger.metric_key == 'duties')) is None
            raise RuntimeError('Synthetic failure after ledger write')
    with transaction(h.app.state.engine) as db:
        assert set(db.scalars(select(m.FairnessLedger.id).where(m.FairnessLedger.publication_id == publication_id))) == original_ids
    assert Counter(ledger(h, publication_id=publication_id)) == Counter(expected)


def test_ledger_retains_individual_comparison_and_quota_goal_receipts(h):
    from packages.domain import fairness
    from tests.test_rule_catalogue_api import approve, propose
    from tests.test_rule_catalogue_guard import line

    a, b, _ = h.seed['people']
    lines = [line(who={'conditions': [{'field': 'person_ids', 'values': [pid]}]},
                  numbers={'target': {'value': amount, 'level': level}})
             for pid, amount, level in ((a, 2, 'high'), (b, 4, 'low'))]
    approve(h, propose(h, 'fair_share', lines, settings={'balance_goal': {
        'metric': 'duties', 'aim': 'target', 'period': 'rota', 'comparison': 'pooled'}})['record'])
    h.policy({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 2,
              'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, {'person_ids': [a]}, severity='advisory')
    workspace = session(h, h.workspace(), 4, [a, b])
    data = h.snapshot(workspace)
    sources = balance_goals(data)
    comparisons = fairness.goal_reports(data) + fairness.quota_goal_reports(data)
    assert any(report.get('source_goal_ids') for report in comparisons)
    assert any(report.get('quota_target') for report in comparisons)
    day = date.fromisoformat(workspace['starts_on'])
    expected = [(*row, None) for row in ledger_facts(data)]
    for policy in sources:
        report = goal_report(data, policy)
        key = 'goal:' + goal_identity(policy)
        expected.append((None, None, key, day, report['score'], 100.0))
        expected += [(row['person_id'], None, key, day, row['actual'], row['expected'])
                     for row in report['people']]
    expected += [(None, None, 'goal:' + report['goal_id'], day, report['score'], 100.0)
                 for report in comparisons if report.get('source_goal_ids') or report.get('quota_target')]
    publication_id = h.publish(workspace)['publication_id']
    assert Counter(ledger(h, publication_id=publication_id)) == Counter(expected)
    with transaction(h.app.state.engine, write=True) as db:
        assert rebuild(db, db.get(m.Publication, publication_id)) == len(expected)
    assert Counter(ledger(h, publication_id=publication_id)) == Counter(expected)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_earlier_publication_supplies_new_catalogue_to_later_rebuild(database, request):
    h = request.getfixturevalue(database)
    person, other, _ = h.seed['people']
    policy = goal(h, scope={'person_ids': [person, other]})
    later = session(h, h.workspace('Later rota', '2026-10-11', '2026-10-18'), 11, [person])
    later_id = h.publish(later)['publication_id']
    assignment_type = h.configure('assignment_types', {'name': 'Added duty', 'starts_on': '2020-01-01',
                                                        'definition': {}})['record']
    location_type = h.get('admin', '/api/catalogues')['location_types'][0]['id']
    location = h.configure('locations', {'name': 'Added room', 'starts_on': '2020-01-01',
                                         'location_type_id': location_type})['record']
    service = h.configure('services', {'name': 'Added service', 'starts_on': '2020-01-01'})['record']
    h.command('credentialer', 'eligibility.create', {'person_id': other, 'service_id': service['id'],
                                                      'starts_on': '2020-01-01'})
    baseline = select(m.WorkspaceBaseline.payload_hash, cast(m.WorkspaceBaseline.canonical_payload, Text)).where(
        m.WorkspaceBaseline.workspace_id == later['id']).order_by(m.WorkspaceBaseline.workspace_revision)
    publication = select(m.Publication.fingerprint, cast(m.Publication.snapshot, Text)).where(m.Publication.id == later_id)
    with transaction(h.app.state.engine) as db:
        stored_baselines = db.execute(baseline).all()
        stored_publication = db.execute(publication).one()
    assert stored_baselines and all(assignment_type['id'] not in payload for _, payload in stored_baselines)

    earlier = h.workspace('Earlier rota', '2026-10-04', '2026-10-11')
    start, end = at(4)
    earlier = h.command('coordinator', 'shift.create', {
        'workspace_id': earlier['id'], 'assignment_type_id': assignment_type['id'], 'location_id': location['id'],
        'service_ids': [service['id']], 'start': start, 'end': end,
        'participants': [{'person_id': other, 'start': start, 'end': end}],
    }, revision=earlier['revision'])['workspace']
    h.publish(earlier)
    for key, row, fields in (
        ('assignment_types', assignment_type, ('definition',)),
        ('locations', location, ('location_type_id',)),
        ('services', service, ()),
    ):
        h.configure(key, {'name': 'Renamed ' + row['name'], 'starts_on': row['starts_on'],
                          **{field: row[field] for field in fields}}, row['id'], row['revision'])

    with transaction(h.app.state.engine) as db:
        assert db.execute(baseline).all() == stored_baselines
        assert db.execute(publication).one() == stored_publication
        data = domain_snapshot(db, db.get(m.Workspace, later['id']))
    for key, row in (('types', assignment_type), ('locations', location), ('services', service)):
        assert data[key][row['id']]['name'] == row['name']
    assert (other, None, 'goal:' + policy['id'], date(2026, 10, 11), 1.0) in [
        row[:5] for row in ledger(h, publication_id=later_id)]


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_new_neighbour_location_ancestry_preserves_existing_ledger_rows(database, request):
    h = request.getfixturevalue(database)
    person = h.seed['people'][0]
    goal(h, period='rota')
    later = session(h, h.workspace('Later rota', '2026-10-11', '2026-10-18'), 11, [person])
    later_id = h.publish(later)['publication_id']
    original = Counter(ledger(h, publication_id=later_id))
    assert original

    location_type = h.get('admin', '/api/catalogues')['location_types'][0]['id']
    place = {'starts_on': '2020-01-01', 'location_type_id': location_type}
    parent = h.configure('locations', {**place, 'name': 'Added suite'})['record']['id']
    child = h.configure('locations', {**place, 'name': 'Added room', 'parent_id': parent})['record']['id']
    earlier = h.workspace('Earlier rota', '2026-10-04', '2026-10-11')
    start, end = at(4)
    earlier = h.command('coordinator', 'shift.create', {
        'workspace_id': earlier['id'], 'assignment_type_id': h.seed['type'], 'location_id': child,
        'service_ids': [h.seed['services'][0]], 'start': start, 'end': end,
        'participants': [{'person_id': person, 'start': start, 'end': end}],
    }, revision=earlier['revision'])['workspace']
    h.publish(earlier)

    assert Counter(ledger(h, publication_id=later_id)) == original
    with transaction(h.app.state.engine) as db:
        data = domain_snapshot(db, db.get(m.Workspace, later['id']))
    assert ancestor_ids(data, child) == [child, parent]


@pytest.mark.parametrize('variable_limit', [None, 8000])
def test_sqlite_ledger_batches_keep_transaction_and_cursor_guards(h, monkeypatch, variable_limit):
    from apps.api.services import fairness as service
    people = h.seed['people']
    person = people[0]
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    publication_id = h.publish(workspace)['publication_id']
    goal_receipts = [row for row in ledger(h, publication_id=publication_id) if row[2].startswith('goal:')]
    facts = [(people[n % 3], people[(n + 1) % 3] if n // 9 % 2 else None,
              f'synthetic:{n // 18}', date(2026, 10, 4 + n // 3 % 3), float(n)) for n in range(5001)]
    # Seeded workload goals keep their canonical receipts alongside the synthetic metric facts.
    expected = [(*row, None) for row in facts] + goal_receipts
    monkeypatch.setattr(service.f, 'ledger_facts', lambda _: facts)
    batches, completed, bound_order = [], [], []

    def before(connection, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO fairness_ledger'):
            assert connection.in_transaction() and connection.info['rota_write']
            assert not many and len(parameters) % 8 == 0
            assert len(parameters) <= connection.connection.driver_connection.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)
            batches.append(len(parameters) // 8)
            bound_order.extend((parameters[i + 4], parameters[i + 5], parameters[i + 2] or '', parameters[i + 3] or '')
                               for i in range(0, len(parameters), 8))

    def after(connection, cursor, statement, parameters, context, many):
        if statement.startswith('INSERT INTO fairness_ledger'):
            completed.append(len(parameters) // 8)

    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', before)
    event.listen(engine, 'after_cursor_execute', after)
    try:
        with transaction(engine, write=True) as db:
            raw = db.connection().connection.driver_connection
            previous = raw.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)
            if variable_limit is not None:
                raw.setlimit(SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
            maximum_rows = min(5000, raw.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER) // 8)
            try:
                assert rebuild(db, db.get(m.Publication, publication_id)) == len(expected)
            finally:
                raw.setlimit(SQLITE_LIMIT_VARIABLE_NUMBER, previous)
    finally:
        event.remove(engine, 'before_cursor_execute', before)
        event.remove(engine, 'after_cursor_execute', after)
    assert batches == completed and sum(batches) == len(expected)
    assert len(batches) == (len(expected) + maximum_rows - 1) // maximum_rows
    assert all(size == maximum_rows for size in batches[:-1]) and 0 < batches[-1] <= maximum_rows
    assert bound_order == sorted((key, day.isoformat(), pid or '', other or '') for pid, other, key, day, _, _ in expected)
    assert Counter(ledger(h, publication_id=publication_id)) == Counter(expected)
    with transaction(engine) as db:
        defaults = db.execute(select(m.FairnessLedger.wish_points_submitted, m.FairnessLedger.wish_points_granted).where(
                                     m.FairnessLedger.publication_id == publication_id)).all()
        assert set(defaults) == {(None, None)}
