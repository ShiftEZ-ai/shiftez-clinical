"""Independent workers keep real PostgreSQL locking inside their owned databases."""
from contextlib import closing
import os
import re
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from . import conftest


@pytest.mark.parametrize("environment,url,allowed,message",[
    ("production","postgresql+psycopg://example.invalid/disposable","yes","never run"),
    ("test","","yes","requires TEST_POSTGRES_URL"),
    ("test","postgresql+psycopg://example.invalid/disposable","no","requires TEST_POSTGRES_URL"),
    ("test","postgresql://example.invalid/disposable","yes","pinned psycopg"),
])
def test_worker_database_refuses_unsafe_configuration_before_connecting(monkeypatch,environment,url,allowed,message):
    monkeypatch.setenv("ROTA_ENV",environment)
    monkeypatch.setenv("TEST_POSTGRES_URL",url)
    monkeypatch.setenv("ROTA_ALLOW_DISPOSABLE_TEST_DB",allowed)
    def refuse_connection(*args,**kwargs):
        raise AssertionError("Safety checks must run before connecting")
    monkeypatch.setattr("sqlalchemy.create_engine",refuse_connection)
    with pytest.raises(pytest.fail.Exception,match=message):
        next(conftest.postgres_worker_url.__wrapped__())


def test_worker_url_preserves_connection_options_and_cleanup_targets_only_owned_database(monkeypatch):
    original="postgresql+psycopg://synthetic:private@example.invalid/disposable?options=-capplication_name%3Dfixture&sslmode=require"
    monkeypatch.setenv("ROTA_ENV","test")
    monkeypatch.setenv("TEST_POSTGRES_URL",original)
    monkeypatch.setenv("ROTA_ALLOW_DISPOSABLE_TEST_DB","yes")
    engine=MagicMock()
    monkeypatch.setattr("sqlalchemy.create_engine",lambda *args,**kwargs:engine)
    with closing(conftest.postgres_worker_url.__wrapped__()) as provision:
        worker=make_url(next(provision))
        assert re.fullmatch(r"rota_worker_[0-9a-f]{32}",worker.database)
        assert worker.set(database="disposable")==make_url(original)
        assert os.environ["TEST_POSTGRES_URL"]==original
    statements=[str(call.args[0]) for call in engine.connect.return_value.__enter__.return_value.execute.call_args_list]
    assert statements==["CREATE DATABASE "+worker.database,"DROP DATABASE "+worker.database+" WITH (FORCE)"]
    engine.dispose.assert_called_once()


@pytest.mark.postgres
def test_worker_database_has_independent_advisory_locks_and_per_test_schema(pg,postgres_worker_url):
    source=create_engine(conftest.disposable_postgres_url())
    key=uuid.uuid4().int%(2**31)
    try:
        with source.begin() as holder,pg.app.state.engine.begin() as worker:
            holder.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":key})
            assert worker.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"),{"key":key})
            assert worker.scalar(text("SELECT current_database()"))==make_url(pg.settings.database_url).database
            assert make_url(pg.settings.database_url).database!=make_url(postgres_worker_url).database
            assert re.fullmatch(r"rota_test_[0-9a-f]{32}",worker.scalar(text("SELECT current_schema()")))
            assert worker.scalar(text("SELECT current_database()"))!=holder.scalar(text("SELECT current_database()"))
    finally:
        source.dispose()
