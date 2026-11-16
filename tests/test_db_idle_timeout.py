"""Connection defaults and transaction-local opt-ins never change database roles."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, text

from apps.api.db import allow_long_idle, make_engine, transaction
from apps.api.settings import Settings


@pytest.mark.parametrize("minutes", [0, 5, 17, 35791])
def test_idle_timeout_setting_loads_environment_and_accepts_off(monkeypatch, minutes):
    monkeypatch.setenv("ROTA_DB_IDLE_TX_TIMEOUT_MINUTES", str(minutes))
    settings = Settings.from_env()
    assert settings.db_idle_tx_timeout_minutes == minutes
    settings.check()


@pytest.mark.parametrize("minutes", [-1, 35792])
def test_idle_timeout_setting_rejects_out_of_range(minutes):
    with pytest.raises(RuntimeError, match="ROTA_DB_IDLE_TX_TIMEOUT_MINUTES"):
        replace(Settings(environment="test"), db_idle_tx_timeout_minutes=minutes).check()


def test_idle_timeout_defaults_to_five(monkeypatch):
    monkeypatch.delenv("ROTA_DB_IDLE_TX_TIMEOUT_MINUTES", raising=False)
    assert Settings.from_env().db_idle_tx_timeout_minutes == 5


def test_connection_factory_reads_idle_setting_without_unrelated_app_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ROTA_DB_IDLE_TX_TIMEOUT_MINUTES", "17")
    monkeypatch.setenv("OIDC_CLIENT_SECRET_FILE", str(tmp_path / "absent-secret"))
    monkeypatch.setenv("ROTA_RATE_COMMANDS_PER_MINUTE", "not-an-integer")
    engine = make_engine("sqlite:///:memory:")
    try:
        assert engine.get_execution_options()["rota_write_settings"].db_idle_tx_timeout_minutes == 17
        with engine.connect() as db:
            assert db.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_allow_long_idle_emits_postgres_set_local():
    db = Mock()
    db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    allow_long_idle(db, 30)
    assert str(db.execute.call_args.args[0]) == "SET LOCAL idle_in_transaction_session_timeout = '30min'"
    db.execute.assert_called_once()


def test_sqlite_has_no_idle_timeout_sql():
    engine = make_engine("sqlite:///:memory:")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cursor, statement, *args: statements.append(statement))
    try:
        with transaction(engine) as db:
            allow_long_idle(db, 30)
            assert db.scalar(text("SELECT 1")) == 1
        assert statements == ["SELECT 1"]
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("minutes", [0, 5, 17])
def test_postgres_applies_environment_on_every_new_connection_and_restores_after_local(
        postgres_worker_url, monkeypatch, minutes):
    monkeypatch.setenv("ROTA_DB_IDLE_TX_TIMEOUT_MINUTES", str(minutes))
    engine = make_engine(postgres_worker_url)
    setting = text("SELECT current_setting('idle_in_transaction_session_timeout')::interval = make_interval(mins => :minutes)")
    try:
        # Concurrent checkouts require distinct DBAPI connections, both configured on connect.
        with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                assert connection.scalar(setting, {"minutes": minutes})
                connection.rollback()
                assert connection.scalar(setting, {"minutes": minutes})
        with transaction(engine) as db:
            allow_long_idle(db, 30)
            assert db.scalar(setting, {"minutes": 30})
        with transaction(engine) as db:
            assert db.scalar(setting, {"minutes": minutes})
        with pytest.raises(ValueError, match="rollback"):
            with transaction(engine) as db:
                allow_long_idle(db, 30)
                raise ValueError("rollback")
        with transaction(engine) as db:
            assert db.scalar(setting, {"minutes": minutes})
    finally:
        engine.dispose()
