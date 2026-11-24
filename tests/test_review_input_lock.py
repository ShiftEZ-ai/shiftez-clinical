"""A review never stamps a concurrent input-counter write as already checked."""
from concurrent.futures import Future, ThreadPoolExecutor
import sqlite3
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from apps.api import models as m, snapshots
from apps.api.auth import authenticate
from apps.api.services import planning
from packages.contracts.schemas import Command


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
@pytest.mark.parametrize('action', ['submit', 'approve'])
def test_review_serializes_counter_writer_before_stamping(h, action, monkeypatch):
    workspace = h.shift(h.workspace())['workspace']
    if action == 'approve':
        workspace = h.lifecycle(workspace, 'submit')['workspace']
    role = 'coordinator' if action == 'submit' else 'approver'
    cookies = h.client(role).cookies
    engine = h.app.state.engine
    postgres = engine.dialect.name == 'postgresql'
    organisation_id = h.seed['organisation_id']
    org = m.Organisation.__table__
    bump = update(org).where(org.c.id == organisation_id).values(
        input_revision=org.c.input_revision + 1, availability_revision=org.c.availability_revision + 1
    ).returning(org.c.input_revision, org.c.availability_revision)
    command = Command(kind=f'workspace.{action}', target_id=workspace['id'],
                      expected_revision=workspace['revision'], payload={'reason': 'Synthetic review'})
    writer_pid, sqlite_busy = Future(), Event()
    lock_review_inputs = planning._lock_review_check_inputs

    def write_counters():
        with Session(engine) as db:
            if postgres:
                writer_pid.set_result(db.scalar(text('SELECT pg_backend_pid()')))
            else:
                # SQLite exposes write contention as SQLITE_BUSY with a zero wait budget.
                wait_budget = db.scalar(text('PRAGMA busy_timeout'))
                db.execute(text('PRAGMA busy_timeout=0'))
                try:
                    with pytest.raises(OperationalError) as caught:
                        db.execute(bump)
                    assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
                finally:
                    db.execute(text(f'PRAGMA busy_timeout={wait_budget}'))
                db.rollback()
                sqlite_busy.set()
            revisions = db.execute(bump).one()
            db.commit()
            return tuple(revisions)

    def pause_after_lock(db, actor, data):
        nonlocal writer
        organisation = lock_review_inputs(db, actor, data)
        writer = pool.submit(write_counters)
        if postgres:
            pid = writer_pid.result(timeout=10)
            deadline = monotonic() + 10
            while not db.scalar(text('SELECT pg_backend_pid() = ANY(pg_blocking_pids(:pid))'), {'pid': pid}):
                assert not writer.done(), 'Counter writer finished before review committed'
                assert monotonic() < deadline, 'Counter writer never waited for the review lock'
        else:
            assert sqlite_busy.wait(10), 'Counter writer did not report SQLite contention'
        assert not writer.done()
        return organisation

    writer = None
    monkeypatch.setattr(planning, '_lock_review_check_inputs', pause_after_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as db:
            if not postgres:
                # Match the SQLite write transaction without using the shared writer wrapper.
                db.execute(text('BEGIN IMMEDIATE'))
            actor = authenticate(db, cookies['rota_session'], cookies['rota_csrf'], write=True, settings=h.settings)
            result = planning.lifecycle(db, actor, command)
            db.commit()
        assert writer is not None
        written_input, written_availability = writer.result(timeout=10)

    assert result['workspace']['status'] == ('submitted' if action == 'submit' else 'approved')
    with Session(engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        hint = row.validation['input_manifest']['dirty_hint']
        assert (hint['input_revision'], hint['availability_revision']) == (written_input - 1, written_availability - 1)
        assert row.id not in snapshots.cheap_fingerprints(db, [row], checks={row.id: row.validation})
