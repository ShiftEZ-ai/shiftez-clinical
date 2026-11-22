"""Bounded, metadata-only PostgreSQL lock diagnostics."""
from dataclasses import replace
import json
import logging

from fastapi.testclient import TestClient
from sqlalchemy import event, text
import pytest

from apps.api import db
from apps.api.main import create_app
from apps.api.settings import Settings
from .test_password_login import PASSWORD, provision


@pytest.fixture
def diagnostic_engine(pg, monkeypatch):
    settings = replace(pg.settings, command_lock_timeout_ms=80, command_statement_timeout_ms=150,
                       command_write_budget_ms=500, command_lock_diagnostic_timeout_ms=37,
                       command_lock_diagnostic_interval_seconds=10)
    engine = db.make_engine(settings.database_url, settings=settings)
    monkeypatch.setattr(db, "_lock_diagnostic_last_at", None)
    monkeypatch.setattr(db, "_lock_diagnostic_suppressed", 0)
    try:
        yield settings, engine
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_lock_diagnostic_rate_limit_logs_suppression(diagnostic_engine, caplog, monkeypatch):
    settings, engine = diagnostic_engine
    scans = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "pg_stat_activity" in statement:
            scans.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with caplog.at_level(logging.WARNING, logger="rota.db"):
            for _ in range(2):
                with pytest.raises(db.DatabaseWriteTimeout):
                    with db.transaction(engine):
                        raise db.DatabaseWriteTimeout()
            monkeypatch.setattr(db, "_lock_diagnostic_last_at",
                                db.time.monotonic() - settings.command_lock_diagnostic_interval_seconds - 1)
            with pytest.raises(db.DatabaseWriteTimeout):
                with db.transaction(engine):
                    raise db.DatabaseWriteTimeout()
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(scans) == 2
    assert len([record for record in caplog.records if record.message == "write_timeout_diagnostic"]) == 3
    suppressed = [record for record in caplog.records if record.message == "write_lock_diagnostic_suppressed"]
    assert len(suppressed) == 1 and suppressed[0].count == 1


@pytest.mark.postgres
def test_lock_diagnostic_sets_local_statement_timeout(diagnostic_engine):
    settings, engine = diagnostic_engine
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if "statement_timeout" in statement:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with db.transaction(engine, write=True, serialize=False) as session:
            prior_timeout = session.scalar(text("SHOW statement_timeout"))
            db._advisory_blockers(session, settings)
            assert session.scalar(text("SHOW statement_timeout")) == prior_timeout
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert any("SET LOCAL statement_timeout = '37ms'" in statement for statement in statements)


def test_lock_diagnostic_interval_is_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("ROTA_COMMAND_LOCK_DIAGNOSTIC_INTERVAL_SECONDS", "17")
    assert Settings.from_env().command_lock_diagnostic_interval_seconds == 17
    for invalid in (0, 3601):
        with pytest.raises(RuntimeError, match="ROTA_COMMAND_LOCK_DIAGNOSTIC_INTERVAL_SECONDS"):
            replace(Settings(environment="test"), command_lock_diagnostic_interval_seconds=invalid).check()


def test_lock_diagnostic_timeout_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setenv("ROTA_COMMAND_LOCK_DIAGNOSTIC_TIMEOUT_MS", "321")
    assert Settings.from_env().command_lock_diagnostic_timeout_ms == 321
    for invalid in (0, 5001):
        with pytest.raises(RuntimeError, match="ROTA_COMMAND_LOCK_DIAGNOSTIC_TIMEOUT_MS"):
            replace(Settings(environment="test"), command_lock_diagnostic_timeout_ms=invalid).check()


@pytest.mark.postgres
@pytest.mark.parametrize("contention", ("skip_locked", "timeout"))
def test_password_login_row_lock_logs_metadata_and_relation(pg, tmp_path, monkeypatch, caplog, contention):
    base_settings, _, identity_id = provision(pg, tmp_path)
    # The lock wait stays tight (80 ms): a sign-in that waits on the locked row still fails. The statement
    # timeout and write budget are generous, so a busy shared database cannot fail the skip-locked path.
    settings = replace(base_settings, command_lock_timeout_ms=80, command_statement_timeout_ms=5000,
                       command_write_budget_ms=10000, command_lock_diagnostic_interval_seconds=1,
                       command_lock_diagnostic_timeout_ms=37,
                       password_login_progressive_delay=False)
    engine = db.make_engine(settings.database_url, settings=settings)
    monkeypatch.setattr(db, "_lock_diagnostic_last_at", None)
    monkeypatch.setattr(db, "_lock_diagnostic_suppressed", 0)
    diagnostic_connections = []

    def capture_diagnostic(connection, _cursor, statement, _parameters, _context, _many):
        if "pg_stat_activity" in statement:
            diagnostic_connections.append(bool(connection.info.get("rota_write")))

    event.listen(engine, "before_cursor_execute", capture_diagnostic)
    holder = engine.connect()
    holder.execute(text("BEGIN"))
    holder_pid = holder.scalar(text("SELECT pg_backend_pid()"))
    holder.execute(text("SELECT id FROM accounts WHERE identity_id = :identity_id FOR UPDATE"),
                   {"identity_id": identity_id})
    try:
        with caplog.at_level(logging.WARNING, logger="rota.db"):
            if contention == "skip_locked":
                with TestClient(create_app(settings, engine), base_url=settings.public_origin) as client:
                    client.headers["origin"] = settings.public_origin
                    response = client.post("/auth/login", json={"username": "managed.person", "password": PASSWORD})
                assert response.status_code == 409, response.text
                assert response.json()["error"]["code"] == "SIGN_IN_BUSY"
            else:
                with pytest.raises(db.DatabaseWriteTimeout):
                    db.retry_write(engine, lambda session: session.execute(
                        text("SELECT id FROM accounts WHERE identity_id = :identity_id FOR UPDATE"),
                        {"identity_id": identity_id}), serialize=False,
                        context={"lock_diagnostic": "password_login", "lock_relations": ("accounts", "identities")})
        diagnostic = next(record for record in caplog.records
                           if record.message == "password_login_row_lock_diagnostic")
        blocker = next(blocker for blocker in diagnostic.blockers if blocker["relation"] == "accounts")
        assert blocker["pid"] == holder_pid
        assert {"pid", "state", "wait_event_type", "wait_event", "query_id", "statement_kind",
                "transaction_ms", "relation"} <= blocker.keys()
        assert "query" not in blocker and PASSWORD not in json.dumps(vars(diagnostic), default=str)
    finally:
        event.remove(engine, "before_cursor_execute", capture_diagnostic)
        holder.rollback()
        holder.close()
        engine.dispose()
    assert diagnostic_connections == [True]


@pytest.mark.postgres
def test_plain_reader_is_not_reported_as_account_row_lock(pg, tmp_path, monkeypatch):
    base_settings, _, _ = provision(pg, tmp_path)
    settings = replace(base_settings, command_lock_diagnostic_timeout_ms=37,
                       command_lock_diagnostic_interval_seconds=1)
    engine = db.make_engine(settings.database_url, settings=settings)
    monkeypatch.setattr(db, "_lock_diagnostic_last_at", None)
    monkeypatch.setattr(db, "_lock_diagnostic_suppressed", 0)
    reader = engine.connect()
    reader.execute(text("BEGIN"))
    reader_pid = reader.scalar(text("SELECT pg_backend_pid()"))
    reader.execute(text("SELECT id FROM accounts LIMIT 1"))
    try:
        with db.transaction(engine) as session:
            blockers = db._row_lock_blockers(session, settings, ("accounts",))
        assert not any(row["pid"] == reader_pid and row["relation"] == "accounts" for row in blockers)
    finally:
        reader.rollback()
        reader.close()
        engine.dispose()


@pytest.mark.postgres
def test_gone_row_lock_blocker_is_an_empty_diagnostic(diagnostic_engine, caplog):
    settings, engine = diagnostic_engine
    with caplog.at_level(logging.WARNING, logger="rota.db"):
        with db.transaction(engine) as session:
            assert db._row_lock_blockers(session, settings, ("identities",)) == []
    assert not any(record.message == "write_lock_diagnostics_unavailable" for record in caplog.records)
