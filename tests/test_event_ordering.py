"""Identical timestamps and reverse UUIDs must retain the database's insertion order."""
from datetime import date, datetime, timezone
from urllib.parse import urlencode
import uuid

import pytest
from sqlalchemy import Column, event, inspect, select, text

from apps.api import models as m
from apps.api.db import transaction
from tests.governance_support import swap_automation, transition
from tests.test_staff_swaps import source_pair, offered
from tests.test_postgres_roles import runtime  # noqa: F401


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)], ids=['sqlite', 'postgres'])
def both(request, monkeypatch):
    monkeypatch.setattr(m, 'clock_now', lambda: datetime(2026, 10, 3, 0, tzinfo=timezone.utc))
    return request.getfixturevalue(request.param)


@pytest.fixture
def reverse_event_ids():
    remaining = iter(range(100000, 0, -1))

    def assign(mapper, connection, target):
        target.id = str(uuid.UUID(int=next(remaining), version=4))

    event.listen(m.SwapEvent, 'before_insert', assign)
    try:
        yield
    finally:
        event.remove(m.SwapEvent, 'before_insert', assign)


def test_only_observed_tables_expose_query_only_insertion_order(both):
    from apps.api.serialization import record
    from apps.api.services.common import materialise_defaults

    with transaction(both.app.state.engine) as db:
        query = ("SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                 "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
                 "AND t.tgname LIKE '%_insertion_order'" if db.bind.dialect.name == 'postgresql' else
                 "SELECT tbl_name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_insertion_order'")
        observed = set(db.scalars(text(query)))
        ordered = {mapper.local_table.name for mapper in m.Base.registry.mappers
                   if issubclass(mapper.class_, m.InsertionOrdered)}
        assert ordered == observed and len(ordered) == 45
        for mapper in m.Base.registry.mappers:
            if mapper.class_ is m.RecordInsertion:
                continue
            assert all(isinstance(column, Column) for column in mapper.columns)
            assert 'seq' not in mapper.column_attrs
            if mapper.local_table.name not in observed:
                with pytest.raises(AttributeError):
                    select(mapper.class_).order_by(mapper.class_.seq)
            else:
                assert db.scalar(select(mapper.class_.id).where(mapper.class_.seq.is_(None)).limit(1)) is None
        pending = m.Service(department_id=both.seed['department_id'], name='Pending default fixture')
        materialise_defaults(pending)
        assert pending.id and pending.created_at and pending.revision == 1
        assert 'seq' not in record(pending) and 'seq' not in inspect(pending).attrs


def test_insertion_observers_roll_back_with_savepoint_and_transaction(both):
    engine = both.app.state.engine
    with transaction(engine) as db:
        before = list(db.execute(select(m.RecordInsertion.__table__).order_by(m.RecordInsertion.seq)))
    with pytest.raises(RuntimeError, match='Abort outer insertion'):
        with transaction(engine, write=True) as db:
            nested = db.begin_nested()
            row = m.Service(department_id=both.seed['department_id'], name='Rolled back savepoint',
                            starts_on=date(2026, 1, 1))
            db.add(row)
            db.flush()
            assert row.seq is not None
            nested.rollback()
            assert db.get(m.Service, row.id) is None
            assert list(db.execute(select(m.RecordInsertion.__table__).order_by(m.RecordInsertion.seq))) == before
            db.execute(m.Service.__table__.insert().values(id=m.uid(), department_id=both.seed['department_id'],
                name='Rolled back Core insert', starts_on=date(2026, 1, 1)))
            assert len(list(db.scalars(select(m.RecordInsertion.seq)))) == len(before) + 1
            raise RuntimeError('Abort outer insertion')
    with transaction(engine) as db:
        assert list(db.execute(select(m.RecordInsertion.__table__).order_by(m.RecordInsertion.seq))) == before
        assert db.scalar(select(m.Service.id).where(m.Service.name == 'Rolled back Core insert')) is None


def test_swap_latest_undo_and_thread_follow_insertion_order(both, reverse_event_ids):
    h = both
    swap_automation(h, prepare=False, publish=False)
    pid, first, _, _ = source_pair(h)
    row = offered(h, pid, first, kind='give')
    row = transition(h, row, 'accept', 'staff_two')['swap']
    row = transition(h, row, 'confirm')['swap']
    history = h.get('staff', '/api/swaps/' + row['id'])['events']
    assert len({item['created_at'] for item in history}) == 1
    assert [item['action'] for item in history] == ['offered', 'accept', 'confirm']
    assert [item['id'] for item in history] == sorted((item['id'] for item in history), reverse=True)
    reply = transition(h, row, 'undo')
    assert reply['swap']['status'] == 'counteroffered'
    assert reply['swap']['reopened_from_id'] == row['id']
    assert reply['undone']['status'] == 'withdrawn'
    history = h.get('staff', '/api/swaps/' + row['id'])['events']
    assert [item['action'] for item in history][-1] == 'undo'


@pytest.mark.parametrize('kind', ['prepare', 'publish'])
def test_swap_refusal_badge_uses_last_insert_in_detail_and_cached_list(both, kind):
    h = both
    swap_automation(h, prepare=False, publish=False)
    pid, first, _, _ = source_pair(h)
    row = offered(h, pid, first, kind='give')
    row = transition(h, row, 'accept', 'staff_two')['swap']
    row = transition(h, row, 'confirm')['swap']
    if kind == 'publish':
        row = transition(h, row, 'prepare', 'coordinator')['swap']
    ids = [str(uuid.UUID(int=value, version=4)) for value in (900, 100)]
    with transaction(h.app.state.engine, write=True) as db:
        for ident, code in zip(ids, ('FIRST_REFUSAL', 'LATEST_REFUSAL')):
            db.add(m.SwapEvent(id=ident, swap_id=row['id'], actor_id=h.seed['identities']['coordinator'],
                action=kind + '_refused', pair_hash='a' * 64, reason='Synthetic refusal', detail={'code': code}))
            db.flush()
    detail = h.get('coordinator', '/api/swaps/' + row['id'])
    shown = next(item for item in h.get('coordinator', '/api/swaps')['swaps'] if item['id'] == row['id'])
    assert detail['swap'][kind + '_refusal'] == shown[kind + '_refusal'] == {'code': 'LATEST_REFUSAL'}
    assert [item['id'] for item in detail['events'] if item['action'] == kind + '_refused'] == ids


def test_audit_raw_folded_cursors_and_person_activity_share_insertion_order(both):
    h = both
    # Authenticate before inserting the synthetic feed so login events cannot split its runs.
    h.client('admin')
    h.client('auditor')
    person, actor = h.seed['people'][0], h.seed['identities']['staff']
    ids = [str(uuid.UUID(int=value, version=4)) for value in (900, 700, 500, 300)]
    with transaction(h.app.state.engine, write=True) as db:
        # Core bulk inserts must be covered too: ORM-only hooks miss these writes.
        db.execute(m.AuditEvent.__table__.insert(), [dict(id=ident, department_id=h.seed['department_id'],
            actor_id=actor, action='auth.session_revoke', target_id=actor, reason='Run ' + str(i // 2),
            detail={'person_id': person, 'identity_id': actor, 'session_id': ident,
                    'device_label': None, 'network_label': None}, correlation_id='same-clock') for i, ident in enumerate(ids)])
    path = '/api/audit?action=auth&target_id=' + actor + '&limit=1'
    page, seen = h.get('auditor', path), []
    while True:
        seen.extend(item['id'] for item in page['events'])
        if not page['has_more']:
            break
        page = h.get('auditor', path + '&' + urlencode(page['next_cursor']))
    assert seen == ids[::-1]
    folded = h.get('auditor', path + '&fold=true')
    assert folded['events'][0]['count'] == 2
    expanded = h.get('auditor', path.replace('limit=1', 'limit=100') + '&run_id=' + folded['events'][0]['run_id'])
    assert [item['id'] for item in expanded['events']] == ids[:1:-1]
    second = h.get('auditor', path + '&fold=true&cursor=' + folded['next_cursor'])
    assert second['events'][0]['count'] == 2 and not second['has_more']
    activity = h.get('admin', f'/api/people/{person}/auth-activity')
    assert [item['id'] for item in activity['events'] if item['id'] in ids] == ids[::-1]
    assert len({item['created_at'] for item in expanded['events']}) == 1


def test_generation_resume_selection_and_pruning_agree_on_tied_jobs(both):
    from apps.api.services.generation_resume import prune_generation_resume, resume_current
    from apps.api.snapshots import fingerprint

    h = both
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        stamp = fingerprint(db, db.get(m.Workspace, workspace['id']))
        jobs = [m.Job(id=str(uuid.UUID(int=i, version=4)), kind='generation', status='succeeded',
            workspace_id=workspace['id'], department_id=h.seed['department_id'], requested_by=h.seed['identities']['coordinator'],
            input_fingerprint=stamp, input_snapshot={}, result={'status': 'needs_more_time'},
            resume_state={'synthetic': True}, resume_checkpoint={'synthetic': True}) for i in (900, 100)]
        db.add_all(jobs)
        db.flush()
        assert jobs[0].created_at == jobs[1].created_at
        assert not resume_current(db, jobs[0]) and resume_current(db, jobs[1])
        assert prune_generation_resume(db, days=7) == 1
        assert jobs[0].resume_state is None and jobs[1].resume_state == {'synthetic': True}


@pytest.mark.postgres
def test_runtime_cannot_rewrite_or_delete_insertion_order(runtime):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    runtime.workspace()
    for sql in ("UPDATE record_insertions SET seq=seq+100000", "DELETE FROM record_insertions"):
        with pytest.raises(DBAPIError) as error, runtime.app.state.engine.begin() as db:
            db.execute(text(sql))
        assert error.value.orig.sqlstate == '42501'
