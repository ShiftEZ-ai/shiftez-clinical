"""Worker polls stay bounded as immutable job and delivery history grows."""
from datetime import date, datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import re
import time

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import func, insert, select, text
from sqlalchemy.schema import CreateIndex

from apps.api import models as m
from apps.api.db import transaction


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
INDEX_NAMES = {
    'ix_jobs_status_created', 'ix_jobs_status_lease', 'ix_outbox_state_attempt',
    'ix_email_deliveries_sender_sent', 'ix_email_deliveries_identity_sent',
}


def test_worker_poll_migration_keeps_reserved_id_and_single_head():
    migration = importlib.import_module('migrations.versions.0115_worker_poll_indexes')
    assert [path.name for path in Path('migrations/versions').glob('0115_*.py')] == ['0115_worker_poll_indexes.py']
    assert migration.revision == '0115_worker_poll_indexes'
    assert migration.down_revision == '0114_request_decision_times'
    graph = ScriptDirectory('migrations')
    head, = graph.get_heads()
    assert migration.revision in {revision.revision for revision in graph.iterate_revisions(head, 'base')}


def polling_queries(department_id, sender_id):
    pending = select(m.Outbox).where(m.Outbox.state.in_(['queued', 'retry']),
        m.Outbox.next_attempt_at <= NOW).order_by(m.Outbox.created_at).limit(200)
    sent = select(m.EmailDelivery.sent_at).where(m.EmailDelivery.department_id == department_id,
        m.EmailDelivery.sent_at.is_not(None))
    return {
        'queued': select(m.Job).where(m.Job.status == 'queued').order_by(m.Job.created_at).limit(1),
        'expired': select(m.Job).where(m.Job.status == 'running',
            func.coalesce(m.Job.heartbeat_at, m.Job.started_at) < NOW - timedelta(minutes=5)),
        'running': select(m.Job.id).where(m.Job.status == 'running',
            m.Job.kind.in_(['generation', 'feasibility', 'catalogue_proof', 'swap_audience'])).limit(1),
        'outbox': pending.where(m.Outbox.kind != 'email',
            m.Outbox.payload['audience_pending'].as_boolean().is_not(True)),
        'email': pending.where(m.Outbox.kind == 'email').order_by(m.Outbox.id),
        'smtp': sent.where(m.EmailDelivery.sender_mode == 'smtp').order_by(m.EmailDelivery.sent_at.desc()).limit(1),
        'microsoft': sent.where(m.EmailDelivery.sender_mode == 'microsoft',
            m.EmailDelivery.sender_identity_id == sender_id).order_by(m.EmailDelivery.sent_at.desc()).limit(1),
    }


def history_rows(db, h, start, end, workspace, sender, other):
    """Insert in bounded batches; each delivery keeps its parent and identity foreign keys."""
    for first in range(start, end, 1000):
        numbers = range(first, min(end, first + 1000))
        outbox = [{'id': f'history-outbox-{n}', 'event_key': f'history-event-{n}',
            'department_id': h.seed['department_id'], 'kind': 'email', 'payload': {},
            'state': 'sent', 'created_at': NOW - timedelta(days=30), 'next_attempt_at': NOW} for n in numbers]
        db.execute(insert(m.Outbox), outbox)
        db.execute(insert(m.Job), [{'id': f'history-job-{n}', 'department_id': h.seed['department_id'],
            'kind': 'generation', 'workspace_id': workspace, 'requested_by': sender, 'status': 'succeeded',
            'input_fingerprint': 'synthetic', 'input_snapshot': {}, 'created_at': NOW - timedelta(days=30)}
            for n in numbers])
        db.execute(insert(m.EmailDelivery), [{'id': f'history-delivery-{n}',
            'department_id': h.seed['department_id'], 'outbox_id': f'history-outbox-{n}',
            'source_outbox_id': 'history-outbox-0', 'identity_id': sender,
            'sender_mode': 'smtp' if n % 2 else 'microsoft', 'sender_identity_id': other,
            'state': 'sent', 'sent_at': NOW - timedelta(days=30) + timedelta(seconds=n)} for n in numbers])


def measure(db, queries):
    measurements = {}
    for name, query in queries.items():
        sql = str(query.compile(dialect=db.dialect, compile_kwargs={'literal_binds': True}))
        if db.dialect.name == 'sqlite':
            plan = [row[-1] for row in db.execute(text('EXPLAIN QUERY PLAN ' + sql))]
            steps = []
            raw = db.connection.driver_connection
            raw.set_progress_handler(lambda: steps.append(1), 100)
            try:
                list(db.execute(query))
            finally:
                raw.set_progress_handler(None, 0)
            work = {'vm_steps': len(steps) * 100}
        else:
            plan = db.execute(text('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + sql)).scalar()[0]['Plan']
            work = {'blocks': plan.get('Shared Hit Blocks', 0) + plan.get('Shared Read Blocks', 0)}
        started = time.perf_counter()
        for _ in range(10):
            list(db.execute(query))
        measurements[name] = {'plan': plan, **work, 'mean_ms': (time.perf_counter() - started) * 100}
    return measurements


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def retained_history(request, scale_harness):
    h = scale_harness(request.param)
    with transaction(h.app.state.engine, write=True) as db:
        sender, other = list(db.scalars(select(m.Identity.id).order_by(m.Identity.id).limit(2)))
        workspace = m.Workspace(department_id=h.seed['department_id'], name='Synthetic history',
            starts_on=date(2029, 1, 1), ends_on=date(2029, 2, 1), created_by=sender, last_edited_by=sender)
        db.add(workspace)
        db.flush()
        workspace_id = workspace.id
    queries = polling_queries(h.seed['department_id'], sender)
    measurements = {}
    previous = 0
    for size in (0, 10_000, 100_000):
        with h.app.state.engine.begin() as db:
            history_rows(db, h, previous, size, workspace_id, sender, other)
            if db.dialect.name == 'postgresql':
                for table in ('jobs', 'outbox', 'email_deliveries'):
                    db.execute(text('ANALYZE ' + table))
            measurements[size] = measure(db, queries)
        previous = size
    print('worker polling history', request.param, json.dumps(measurements, default=str))
    return h, queries, measurements, workspace_id, sender


def test_idle_worker_polls_do_not_scan_retained_history(retained_history):
    h, _, measurements, _, _ = retained_history
    for name, measured in measurements[100_000].items():
        if h.app.state.engine.dialect.name == 'sqlite':
            assert measured['vm_steps'] < 5000, (name, measured)
        else:
            assert measured['blocks'] < 100, (name, measured)
            assert 'Seq Scan' not in json.dumps(measured['plan']), (name, measured)


def test_migration_and_models_define_the_same_polling_indexes(retained_history, monkeypatch):
    h, _, _, _, _ = retained_history
    migration = importlib.import_module('migrations.versions.0115_worker_poll_indexes')
    assert migration.down_revision == '0114_request_decision_times'
    recorded = {}
    monkeypatch.setattr(migration.op, 'create_index', lambda name, table, columns, **kw:
        recorded.update({name: (table, [str(c) for c in columns], kw)}))
    migration.upgrade()
    assert set(recorded) == INDEX_NAMES
    indexes = [index for table in (m.Job.__table__, m.Outbox.__table__, m.EmailDelivery.__table__)
               for index in table.indexes if index.name in INDEX_NAMES]
    assert {index.name for index in indexes} == INDEX_NAMES
    with h.app.state.engine.connect() as db:
        for index in indexes:
            name, expressions, options = recorded[index.name]
            assert name == index.table.name
            assert expressions == [str(expr).removeprefix(name + '.') for expr in index.expressions]
            for key, value in options.items():
                assert str(index.dialect_kwargs[key]) == str(value)
            expected = str(CreateIndex(index).compile(dialect=db.dialect))
            if db.dialect.name == 'sqlite':
                actual = db.execute(text('SELECT sql FROM sqlite_master WHERE name=:name'), {'name': index.name}).scalar()
            else:
                actual = db.execute(text('SELECT pg_get_indexdef(to_regclass(:name))'), {'name': index.name}).scalar()
                schema = db.execute(text('SELECT current_schema()')).scalar()
                actual = actual.replace(schema + '.', '').replace(' USING btree', '')
                actual = re.sub(r'WHERE \((.*)\)$', r'WHERE \1', actual)
            assert actual.lower() == expected.lower()


def test_indexes_preserve_pending_selection_and_lease_null_semantics(retained_history):
    h, queries, _, workspace, sender = retained_history
    with h.app.state.engine.begin() as db:
        jobs = [
            ('later', 'queued', None, None, NOW),
            ('first', 'queued', None, None, NOW - timedelta(days=1)),
            ('expired-fallback', 'running', None, NOW - timedelta(hours=1), NOW),
            ('expired-heartbeat', 'running', NOW - timedelta(hours=1), NOW, NOW),
            ('live-heartbeat', 'running', NOW, NOW - timedelta(hours=1), NOW),
            ('no-lease', 'running', None, None, NOW),
        ]
        db.execute(insert(m.Job), [{'id': name, 'department_id': h.seed['department_id'], 'kind': 'validation',
            'workspace_id': workspace, 'requested_by': sender, 'status': status, 'heartbeat_at': heartbeat,
            'started_at': started, 'created_at': created, 'input_snapshot': {}, 'input_fingerprint': 'synthetic'}
            for name, status, heartbeat, started, created in jobs])
        events = [('due', 'announcement', 'queued', NOW, {}), ('retry', 'announcement', 'retry', NOW, {}),
            ('email-due', 'email', 'queued', NOW, {}), ('future', 'announcement', 'queued', NOW + timedelta(days=1), {}),
            ('audience', 'announcement', 'queued', NOW, {'audience_pending': True})]
        db.execute(insert(m.Outbox), [{'id': name, 'event_key': name, 'department_id': h.seed['department_id'],
            'kind': kind, 'state': state, 'next_attempt_at': due, 'payload': payload}
            for name, kind, state, due, payload in events])
        assert [row.id for row in db.execute(queries['queued'])] == ['first']
        assert {row.id for row in db.execute(queries['expired'])} == {'expired-fallback', 'expired-heartbeat'}
        assert {row.id for row in db.execute(queries['outbox'])} == {'due', 'retry'}
        assert [row.id for row in db.execute(queries['email'])] == ['email-due']
        # Returning to retained history also proves state transitions remove work from both polling indexes.
        db.execute(m.Job.__table__.update().where(m.Job.id.in_([row[0] for row in jobs])).values(status='succeeded'))
        db.execute(m.Outbox.__table__.update().where(m.Outbox.id.in_([row[0] for row in events])).values(state='sent'))
        assert not list(db.scalars(queries['queued']))
        assert not list(db.scalars(queries['expired']))
        assert not list(db.scalars(queries['outbox']))
        assert not list(db.scalars(queries['email']))


@pytest.mark.wall_benchmark
def test_retained_history_polling_time_budget(retained_history):
    _, _, measurements, _, _ = retained_history
    # Work-count guards above are deterministic; this generous floor tolerates shared CI and coverage overhead.
    for name, measured in measurements[100_000].items():
        assert measured['mean_ms'] < max(20, measurements[0][name]['mean_ms'] * 20), (name, measured)


@pytest.fixture(scope='module')
def index_write_overhead(retained_history):
    h, _, _, workspace, sender = retained_history
    tables = (m.Job.__table__, m.Outbox.__table__, m.EmailDelivery.__table__)
    indexes = [index for table in tables for index in table.indexes if index.name in INDEX_NAMES]
    migration = importlib.import_module('migrations.versions.0115_worker_poll_indexes')
    with h.app.state.engine.begin() as db:
        before = [list(db.execute(select(table).order_by(table.c.id).limit(1))) for table in tables]
        counts = [db.scalar(select(func.count()).select_from(table)) for table in tables]
        other = db.scalar(select(m.Identity.id).where(m.Identity.id != sender).limit(1))

        def writes():
            savepoint = db.begin_nested()
            try:
                started = time.perf_counter()
                history_rows(db, h, 100_000, 101_000, workspace, sender, other)
                for table, prefix, values in ((m.Job.__table__, 'job', {'status': 'running', 'heartbeat_at': NOW}),
                        (m.Outbox.__table__, 'outbox', {'state': 'queued', 'next_attempt_at': NOW + timedelta(minutes=1)}),
                        (m.EmailDelivery.__table__, 'delivery', {'sent_at': NOW})):
                    ids = [f'history-{prefix}-{n}' for n in range(100_000, 101_000)]
                    db.execute(table.update().where(table.c.id.in_(ids)).values(**values))
                return time.perf_counter() - started
            finally:
                savepoint.rollback()

        indexed = writes()
        for index in indexes:
            index.drop(db)
        try:
            unindexed = writes()
        finally:
            with Operations.context(MigrationContext.configure(db)):
                migration.upgrade()
        after = [list(db.execute(select(table).order_by(table.c.id).limit(1))) for table in tables]
        after_counts = [db.scalar(select(func.count()).select_from(table)) for table in tables]
    print('worker index writes', h.app.state.engine.dialect.name,
          {'3000_rows_indexed_s': indexed, '3000_rows_unindexed_s': unindexed, 'ratio': indexed / unindexed})
    return before, after, counts, after_counts, indexed, unindexed


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_additive_indexes_preserve_evidence(request, database):
    """Small and deterministic: dropping the polling indexes and running migration 0115 again keeps every retained
    row (first rows and counts), and the indexes are back. The timed 100,000-row cycle is the nightly wall benchmark below."""
    h = request.getfixturevalue(database)
    tables = (m.Job.__table__, m.Outbox.__table__, m.EmailDelivery.__table__)
    indexes = [index for table in tables for index in table.indexes if index.name in INDEX_NAMES]
    migration = importlib.import_module('migrations.versions.0115_worker_poll_indexes')
    with h.app.state.engine.begin() as db:
        sender, other = list(db.scalars(select(m.Identity.id).order_by(m.Identity.id).limit(2)))
        workspace = m.Workspace.__table__
        db.execute(insert(workspace).values(id='history-workspace', department_id=h.seed['department_id'],
            name='Synthetic history', starts_on=date(2029, 1, 1), ends_on=date(2029, 2, 1), created_by=sender,
            last_edited_by=sender))
        history_rows(db, h, 0, 20, 'history-workspace', sender, other)
        snapshot = lambda: ([list(db.execute(select(table).order_by(table.c.id)))[:3] for table in tables],
                            [db.scalar(select(func.count()).select_from(table)) for table in tables])
        before = snapshot()
        for index in indexes:
            index.drop(db)
        with Operations.context(MigrationContext.configure(db)):
            migration.upgrade()
        assert snapshot() == before
        # Read from the catalogue: reflection skips expression indexes (ix_jobs_status_lease on SQLite).
        names = set(db.scalars(text("SELECT name FROM sqlite_master WHERE type = 'index'" if db.dialect.name == 'sqlite'
                                    else 'SELECT indexname FROM pg_indexes')))
        assert INDEX_NAMES <= names


@pytest.mark.wall_benchmark
def test_additive_indexes_have_bounded_write_overhead(index_write_overhead):
    before, after, counts, after_counts, indexed, unindexed = index_write_overhead
    assert before == after and counts == after_counts  # the timed cycle keeps the evidence too
    assert indexed < max(2, unindexed * 5)
