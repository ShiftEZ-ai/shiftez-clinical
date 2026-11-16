"""SQLite durability is explicit on every connection; OFF needs disposable-test consent."""
import os
from types import SimpleNamespace

import pytest

from apps.api.db import make_engine
from apps.api.main import create_app
from apps.api.settings import Settings
from tests.conftest import Harness


@pytest.mark.parametrize("mode", ["FULL", "NORMAL", "OFF"])
def test_live_server_environment_uses_harness_synchronous(monkeypatch, mode):
    from tests.test_browser import _live_server_env

    monkeypatch.setenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", "yes")
    monkeypatch.delenv("ROTA_SQLITE_SYNCHRONOUS", raising=False)
    harness = SimpleNamespace(settings=Settings(environment="test", sqlite_synchronous=mode))
    for key, value in _live_server_env(harness, "http://127.0.0.1:8000").items():
        monkeypatch.setenv(key, value)
    child_settings = Settings.from_env()
    assert child_settings.sqlite_synchronous == harness.settings.sqlite_synchronous
    assert os.environ["ROTA_ALLOW_DISPOSABLE_TEST_DB"] == "yes"
    child_settings.check_sqlite_synchronous()


@pytest.mark.parametrize("mode, expected", [("FULL", 2), ("NORMAL", 1), ("OFF", 0)])
@pytest.mark.parametrize("from_environment", [False, True], ids=["settings", "environment"])
def test_synchronous_applies_to_every_connection(tmp_path, monkeypatch, mode, expected, from_environment):
    monkeypatch.setenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", "yes")
    monkeypatch.setenv("ROTA_SQLITE_SYNCHRONOUS", mode)
    url = "sqlite:///" + str(tmp_path / "durability.db")
    settings = Settings.from_env() if from_environment else Settings(sqlite_synchronous=mode)
    settings.check()
    assert settings.sqlite_synchronous == mode
    engine = make_engine(url, **({} if from_environment else {"settings": settings}))
    try:
        with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == expected
                assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    finally:
        engine.dispose()


def test_default_synchronous_is_full(tmp_path, monkeypatch):
    monkeypatch.delenv("ROTA_SQLITE_SYNCHRONOUS", raising=False)
    monkeypatch.delenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", raising=False)
    assert Settings().sqlite_synchronous == Settings.from_env().sqlite_synchronous == "FULL"
    engine = make_engine("sqlite:///" + str(tmp_path / "default.db"))
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("allowance", [None, "no", "true", "YES"])
@pytest.mark.parametrize("from_environment", [False, True], ids=["settings", "environment"])
def test_off_refuses_startup_and_direct_engine_without_exact_allowance(tmp_path, monkeypatch, allowance, from_environment):
    if allowance is None:
        monkeypatch.delenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", raising=False)
    else:
        monkeypatch.setenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", allowance)
    monkeypatch.setenv("ROTA_SQLITE_SYNCHRONOUS", "OFF")
    settings = Settings.from_env() if from_environment else Settings(sqlite_synchronous="OFF")
    with pytest.raises(RuntimeError, match="OFF requires ROTA_ALLOW_DISPOSABLE_TEST_DB=yes"):
        create_app(settings)
    path = tmp_path / "refused" / "database.db"
    with pytest.raises(RuntimeError, match="OFF requires ROTA_ALLOW_DISPOSABLE_TEST_DB=yes"):
        make_engine("sqlite:///" + str(path), **({} if from_environment else {"settings": settings}))
    assert not path.parent.exists()


@pytest.mark.parametrize("mode", ["", "full", "EXTRA", "OFF; PRAGMA foreign_keys=OFF"])
@pytest.mark.parametrize("from_environment", [False, True], ids=["settings", "environment"])
def test_invalid_synchronous_is_refused_before_connecting(tmp_path, monkeypatch, mode, from_environment):
    monkeypatch.setenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", "yes")
    monkeypatch.setenv("ROTA_SQLITE_SYNCHRONOUS", mode)
    settings = Settings.from_env() if from_environment else Settings(sqlite_synchronous=mode)
    with pytest.raises(RuntimeError, match="ROTA_SQLITE_SYNCHRONOUS must be FULL, NORMAL or OFF"):
        create_app(settings)
    path = tmp_path / "refused" / "database.db"
    with pytest.raises(RuntimeError, match="ROTA_SQLITE_SYNCHRONOUS must be FULL, NORMAL or OFF"):
        make_engine("sqlite:///" + str(path), **({} if from_environment else {"settings": settings}))
    assert not path.parent.exists()


def test_postgres_does_not_require_sqlite_disposable_allowance(monkeypatch):
    monkeypatch.delenv("ROTA_ALLOW_DISPOSABLE_TEST_DB", raising=False)
    settings = Settings(environment="test", database_url="postgresql+psycopg://localhost/unused",
                        sqlite_synchronous="OFF")
    settings.check()
    engine = make_engine(settings.database_url, settings=settings)
    engine.dispose()


@pytest.mark.parametrize("allowance, override, expected", [
    (None, None, "FULL"),
    ("yes", None, "OFF"),
    (None, "NORMAL", "NORMAL"),
    ("yes", "NORMAL", "NORMAL"),
])
def test_harness_uses_configured_synchronous(tmp_path, monkeypatch, allowance, override, expected):
    for name, value in (("ROTA_ALLOW_DISPOSABLE_TEST_DB", allowance), ("ROTA_SQLITE_SYNCHRONOUS", override)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    harness = Harness("sqlite:///" + str(tmp_path / "harness.db"), {})
    try:
        assert harness.settings.sqlite_synchronous == expected
        with harness.app.state.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar() == {"FULL": 2, "NORMAL": 1, "OFF": 0}[expected]
    finally:
        harness.app.state.engine.dispose()
