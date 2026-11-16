"""Retained history uses both recurring-occurrence access paths and audit ordering indexes."""
from datetime import date, datetime, timedelta, timezone
import importlib
import json
from pathlib import Path

from alembic import command
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import func, insert, inspect, select

from apps.api import models as m
from apps.api.db import make_engine, transaction
from migrations import safety
from scripts.manage import alembic_config, migrate
from tests.migration_support import historical_seed
from tests.test_audit_worker_indexes import measure
from tests.test_performance_budget import python_budget


MIGRATION = importlib.import_module('migrations.versions.0119_backend_hotpath_indexes')
INDEX_NAMES = {'ix_shifts_series_occurrence', 'ix_shifts_occurrence_series', 'ix_audit_events_department_at',
               'ix_audit_events_department_actor'}


def test_single_additive_migration_preserves_retained_evidence(tmp_path):
    graph = ScriptDirectory('migrations')
    head, = graph.get_heads()
    assert MIGRATION.revision in {r.revision for r in graph.iterate_revisions(head, 'base')}
    assert MIGRATION.down_revision == '0118_shift_occurrence_index'
    path = Path(MIGRATION.__file__)
    assert safety.analyze(path)['class'] == 'additive'
    assert not safety.needs_confirmation(path)
    url = 'sqlite:///' + str(tmp_path / 'upgrade.db')
    command.upgrade(alembic_config(url), MIGRATION.down_revision)
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            event = m.AuditEvent(department_id=seed['department_id'], actor_id=seed['identities']['admin'],
                action='synthetic.retained', reason='Retained synthetic evidence', detail={}, correlation_id='synthetic')
            db.add(event)
            db.flush()
            event_id = event.id
        with engine.connect() as db:
            before = db.execute(select(m.AuditEvent.__table__)).all()
        migrate(url)
        with engine.connect() as db:
            assert db.execute(select(m.AuditEvent.__table__)).all() == before
            assert db.scalar(select(m.AuditEvent.id)) == event_id
            assert db.scalar(select(m.Department.catalogue_revision)) == 1
            assert INDEX_NAMES <= {index['name'] for table in ('shifts', 'audit_events')
                                   for index in inspect(db).get_indexes(table)}
    finally:
        engine.dispose()


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def indexed_history(request, scale_harness):
    h = scale_harness(request.param)
    start = date(2023, 1, 1)
    actor = h.seed['identities']['coordinator']
    with h.app.state.engine.begin() as db:
        db.execute(insert(m.Workspace), [{'id': f'history-rota-{n}', 'department_id': h.seed['department_id'],
            'name': f'Synthetic retained rota {n}', 'starts_on': start + timedelta(days=n * 30),
            'ends_on': start + timedelta(days=n * 30 + 30), 'created_by': actor, 'last_edited_by': actor}
            for n in range(36)])
        db.execute(insert(m.RecurringSeries), [{'id': f'history-series-{n}', 'department_id': h.seed['department_id'],
            'name': f'Synthetic recurring series {n}', 'assignment_type_id': h.seed['type'],
            'location_id': h.seed['locations'][0], 'definition': {}, 'starts_on': start} for n in range(100)])
        for first in range(0, 18_000, 1000):
            rows = []
            for n in range(first, first + 1000):
                day = start + timedelta(days=n // 500 * 30 + n % 500 // 100)
                at = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
                rows.append({'id': f'history-shift-{n}', 'workspace_id': f'history-rota-{n // 500}',
                    'assignment_type_id': h.seed['type'], 'type_revision': 1, 'type_snapshot': {},
                    'location_id': h.seed['locations'][0], 'start': at, 'end': at + timedelta(hours=4),
                    'series_id': f'history-series-{n % 100}', 'series_revision': 1,
                    'series_snapshot': {}, 'occurrence_on': day})
            db.execute(insert(m.Shift), rows)
        for first in range(0, 50_000, 1000):
            db.execute(insert(m.AuditEvent), [{'id': f'history-audit-{n:06}', 'department_id': h.seed['department_id'],
                'actor_id': actor, 'action': 'synthetic.retained', 'reason': 'Synthetic retained evidence', 'detail': {},
                'correlation_id': 'synthetic', 'created_at': datetime(2023, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=n)}
                for n in range(first, first + 1000)])
        lo = start + timedelta(days=900)
        queries = {
            'series_date': select(m.Shift.id).where(m.Shift.series_id.in_(['history-series-2', 'history-series-3']),
                m.Shift.occurrence_on.in_([lo, lo + timedelta(days=1)])),
            'date_window': select(m.Shift.id).where(m.Shift.series_id.is_not(None),
                m.Shift.occurrence_on >= lo, m.Shift.occurrence_on < lo + timedelta(days=1)),
            'audit': select(m.AuditEvent.id).where(m.AuditEvent.department_id == h.seed['department_id'])
                .order_by(m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc()).limit(101),
            'audit_actors': select(m.Identity.id).where(select(m.AuditEvent.id).where(
                m.AuditEvent.department_id == h.seed['department_id'], m.AuditEvent.actor_id == m.Identity.id).exists())}
        indexes = [i for table in (m.Shift.__table__, m.AuditEvent.__table__) for i in table.indexes if i.name in INDEX_NAMES]
        assert {i.name for i in indexes} == INDEX_NAMES
        for index in indexes:
            index.drop(db)
        before = measure(db, queries)
        for index in indexes:
            index.create(db)
        if db.dialect.name == 'postgresql':
            from sqlalchemy import text
            db.execute(text('ANALYZE shifts'))
            db.execute(text('ANALYZE audit_events'))
        after = measure(db, queries)
        assert db.scalar(select(func.count()).select_from(m.Shift)) == 18_000
    print('backend retained-history indexes', request.param, json.dumps({'before': before, 'after': after}, default=str))
    return h, before, after


def test_occurrence_and_audit_queries_use_bounded_index_work(indexed_history):
    h, before, after = indexed_history
    for name, measured in after.items():
        plan = json.dumps(measured['plan'])
        expected = ('ix_audit_events_department_at' if name == 'audit' else
                    'ix_audit_events_department_actor' if name == 'audit_actors' else 'ix_shifts_')
        assert expected in plan
        if h.app.state.engine.dialect.name == 'sqlite':
            assert measured['vm_steps'] < 5000, (name, measured)
            if name == 'date_window':
                # Dev's occurrence-only index already bounds this query; the new indexes must not regress it.
                assert measured['vm_steps'] <= before[name]['vm_steps'], (name, before[name], measured)
            else:
                assert measured['vm_steps'] * 10 < before[name]['vm_steps'], (name, before[name], measured)
        else:
            if name == 'audit_actors':
                def nodes(node):
                    yield node
                    for child in node.get('Plans', []):
                        yield from nodes(child)

                audit_nodes = [node for node in nodes(measured['plan'])
                               if node.get('Relation Name') == 'audit_events']
                # A scan of the few identities is cheap; retained audit history must use a keyed probe.
                assert audit_nodes and all(node.get('Index Name') == expected
                    and all(column in node.get('Index Cond', '') for column in ('department_id', 'actor_id'))
                    for node in audit_nodes), (name, measured)
            else:
                assert 'Seq Scan' not in plan, (name, measured)
            assert measured['blocks'] < 150, (name, measured)


@pytest.mark.wall_benchmark
def test_retained_history_query_time_budget(indexed_history):
    _, _, after = indexed_history
    assert all(measured['mean_ms'] < python_budget(20) for measured in after.values()), after
