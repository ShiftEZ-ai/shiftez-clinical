"""reviewed authentication-event retention leaves current and clinical evidence intact."""
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.db import transaction
from apps.worker import main as worker


@pytest.fixture(autouse=True)
def database_wall_clock(monkeypatch):
    from packages.domain import time as domain_time
    # Match PostgreSQL/SQLite's independent security-retention clock.
    monkeypatch.setattr(domain_time, '_offset', timedelta(0))


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def audit_store(request):
    return request.getfixturevalue(request.param)


def add_event(db, days, outcome='credentials_rejected'):
    event = m.AuthLoginEvent(account_hash='a' * 64, peer_hash='b' * 64, outcome=outcome,
                            correlation_id='synthetic-retention', created_at=m.now() - timedelta(days=days))
    db.add(event)
    db.flush()
    return event.id


def expired_security_state(db, seed):
    moment = m.now()
    session = m.Session(identity_id=seed['identities']['coordinator'], department_id=seed['department_id'],
                        token_hash='c' * 64, csrf_hash='d' * 64, expires_at=moment - timedelta(days=60))
    attempt = m.LoginAttempt(key_hash='e' * 64, succeeded=False, created_at=moment - timedelta(days=2))
    limit = m.RateLimit(key_hash='f' * 64, hits=4000, expires_at=moment - timedelta(days=1))
    db.add_all([session, attempt, limit])
    db.flush()
    return session.id, attempt.id, limit.key_hash


def assert_security_state_removed(engine, ids):
    with transaction(engine) as db:
        for model, key in zip((m.Session, m.LoginAttempt, m.RateLimit), ids):
            assert db.get(model, key) is None


def test_audit_prune_failure_does_not_rollback_security_housekeeping(audit_store, monkeypatch):
    from sqlalchemy import event
    engine = audit_store.app.state.engine
    with transaction(engine, write=True) as db:
        ids = expired_security_state(db, audit_store.seed)
        retained = add_event(db, 40)

    def reject_audit_prune(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT prune_auth_login_events') or statement.startswith('DELETE FROM auth_login_events'):
            raise RuntimeError('Synthetic audit pruning outage')

    monkeypatch.setattr(worker, '_pruned', {})
    event.listen(engine, 'before_cursor_execute', reject_audit_prune)
    try:
        with pytest.raises(RuntimeError, match='audit pruning outage'):
            worker.prune_security_state(engine, settings=audit_store.settings)
    finally:
        event.remove(engine, 'before_cursor_execute', reject_audit_prune)
    assert_security_state_removed(engine, ids)
    with transaction(engine) as db:
        assert db.get(m.AuthLoginEvent, retained)
    assert engine not in worker._pruned
    assert worker.prune_security_state(engine, settings=audit_store.settings) == 1


def test_authentication_retention_is_configured_bounded_and_preserves_other_evidence(audit_store, monkeypatch):
    engine = audit_store.app.state.engine
    with transaction(engine, write=True) as db:
        expired = [add_event(db, age, outcome) for age, outcome in (
            (40, 'success'), (35, 'credentials_rejected'), (31, 'credentials_rejected'))]
        recent = [add_event(db, 29), add_event(db, 0, 'success')]
        other_evidence = db.execute(select(m.AuditEvent)).scalars().all()
        audit_ids = {event.id for event in other_evidence}
        db.add(m.RateLimit(key_hash='e' * 64, hits=4000, expires_at=m.now() - timedelta(days=1)))
        db.add(m.RateLimit(key_hash='f' * 64, hits=4000, expires_at=m.now() + timedelta(days=1)))
    settings = replace(audit_store.settings, password_login_audit_retention_days=30)
    monkeypatch.setattr(worker, '_pruned', {})
    assert worker.prune_security_state(engine, batch=2, settings=settings) == 3  # two events, one expired counter
    assert worker.prune_security_state(engine, batch=2, settings=settings) == 0
    with transaction(engine) as db:
        assert set(db.scalars(select(m.AuthLoginEvent.id))) == {expired[-1], *recent}
        assert set(db.scalars(select(m.AuditEvent.id))) == audit_ids
        assert set(db.scalars(select(m.RateLimit.key_hash))) == {'f' * 64}
    monkeypatch.setattr(worker, '_pruned', {})
    assert worker.prune_security_state(engine, settings=replace(settings, password_login_audit_retention_days=365)) == 0
    monkeypatch.setattr(worker, '_pruned', {})
    assert worker.prune_security_state(engine, settings=settings) == 1
    with transaction(engine) as db:
        assert set(db.scalars(select(m.AuthLoginEvent.id))) == set(recent)
        assert set(db.scalars(select(m.AuditEvent.id))) == audit_ids


@pytest.mark.parametrize('days,batch', [(0, 1), (1, 1), (29, 1), (3651, 1), (30, 0), (30, 501)])
def test_worker_retention_rejects_unsafe_bounds_before_mutation(h, days, batch):
    settings = replace(h.settings, password_login_audit_retention_days=days)
    with transaction(h.app.state.engine, write=True) as db:
        event_id = add_event(db, 4000)
    with pytest.raises(ValueError, match='retention bounds'):
        worker.prune_security_state(h.app.state.engine, batch=batch, settings=settings)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.AuthLoginEvent, event_id)


def test_sqlite_retention_guard_always_blocks_updates_and_current_deletion(h):
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        old, protected, current = add_event(db, 31), add_event(db, 29), add_event(db, 0)
    for statement, event_id in (("UPDATE auth_login_events SET outcome='forged' WHERE id=:id", old),
                                ("UPDATE auth_login_events SET outcome='forged' WHERE id=:id", current),
                                ('DELETE FROM auth_login_events WHERE id=:id', current),
                                ('DELETE FROM auth_login_events WHERE id=:id', protected)):
        with pytest.raises(DBAPIError, match='immutable evidence'), transaction(engine, write=True) as db:
            db.execute(text(statement), {'id': event_id})
    with transaction(engine, write=True) as db:
        assert db.execute(text('DELETE FROM auth_login_events WHERE id=:id'), {'id': old}).rowcount == 1


def test_worker_passes_its_explicit_retention_setting(h, monkeypatch):
    settings = replace(h.settings, password_login_audit_retention_days=91)
    seen = []
    monkeypatch.setattr(worker, 'prune_security_state', lambda engine, *, settings: seen.append(settings))
    monkeypatch.setattr(worker, 'prune_feedback_screenshots', lambda engine: 0)
    monkeypatch.setattr(worker, 'dispatch_batch', lambda db: set())
    monkeypatch.setattr(worker, 'claim_job', lambda db: None)
    monkeypatch.setattr(worker, 'heartbeat', lambda engine: None)
    worker.process_one(h.app.state.engine, settings=settings)
    assert seen == [settings]
