"""Disposable templates preserve real migrations, independent databases and fail-closed cleanup."""
from contextlib import closing
import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from . import conftest


@pytest.mark.parametrize('name,template', [
    ('postgres', None),
    ('rota_test_not_random', None),
    ('rota_test_' + 'a' * 32, 'postgres'),
    ('rota_test_' + 'a' * 32, 'rota_template_; DROP DATABASE postgres'),
])
def test_postgres_template_rejects_unowned_database_names_before_connecting(monkeypatch, name, template):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setenv('ROTA_ALLOW_DISPOSABLE_TEST_DB', 'yes')
    monkeypatch.setenv('TEST_POSTGRES_URL', 'postgresql+psycopg://example.invalid/disposable')
    monkeypatch.setattr('sqlalchemy.create_engine', lambda *args, **kwargs: pytest.fail('Must refuse before connecting'))
    with pytest.raises(ValueError, match='not owned'):
        with conftest.postgres_database(conftest.disposable_postgres_url(), name, template):
            pytest.fail('Must not yield an unowned database')


@pytest.mark.parametrize('environment,allowed', [('production', 'yes'), ('test', 'no')])
def test_postgres_template_cached_seed_never_bypasses_disposable_authorization(monkeypatch, environment, allowed):
    monkeypatch.setenv('ROTA_ENV', environment)
    monkeypatch.setenv('ROTA_ALLOW_DISPOSABLE_TEST_DB', allowed)
    monkeypatch.setenv('TEST_POSTGRES_URL', 'postgresql+psycopg://example.invalid/disposable')
    monkeypatch.setattr('sqlalchemy.create_engine', lambda *args, **kwargs: pytest.fail('Must refuse before connecting'))
    seed = ('postgresql+psycopg://example.invalid/rota_template_' + 'a' * 32, {})
    with pytest.raises(pytest.fail.Exception, match='never run|requires TEST_POSTGRES_URL'):
        next(conftest.postgres_harness('postgresql+psycopg://example.invalid/disposable', seed))


def test_postgres_template_create_failure_never_drops_an_existing_database(monkeypatch):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setenv('ROTA_ALLOW_DISPOSABLE_TEST_DB', 'yes')
    monkeypatch.setenv('TEST_POSTGRES_URL', 'postgresql+psycopg://example.invalid/disposable')
    admin = MagicMock()
    execute = admin.connect.return_value.__enter__.return_value.execute
    execute.side_effect = RuntimeError('Synthetic create failure')
    monkeypatch.setattr('sqlalchemy.create_engine', lambda *args, **kwargs: admin)
    name = 'rota_test_' + uuid.uuid4().hex
    template = 'rota_template_' + uuid.uuid4().hex
    with pytest.raises(RuntimeError, match='Synthetic create failure'):
        with conftest.postgres_database(conftest.disposable_postgres_url(), name, template):
            pytest.fail('A failed CREATE must not yield')
    assert [str(call.args[0]) for call in execute.call_args_list] == ['CREATE DATABASE ' + name + ' TEMPLATE ' + template]
    admin.dispose.assert_called_once()


@pytest.mark.postgres
def test_postgres_template_clones_keep_seed_and_isolate_changes(pg, postgres_worker_url, postgres_template, monkeypatch):
    monkeypatch.setattr(conftest, 'migrate', lambda url: pytest.fail('Clones must not repeat migrations'))
    template_url, seed = postgres_template
    template_name = make_url(template_url).database
    first_name = make_url(pg.settings.database_url).database
    pg.workspace()
    pg.seed['people'].append('only-this-harness')
    admin = create_engine(postgres_worker_url, hide_parameters=True)
    try:
        with closing(conftest.postgres_harness(postgres_worker_url, postgres_template)) as provision:
            other = next(provision)
            other_name = make_url(other.settings.database_url).database
            assert len({first_name, other_name, template_name, make_url(postgres_worker_url).database}) == 4
            assert other.seed == seed
            assert 'only-this-harness' not in other.seed['people']
            assert other.seed is not seed
            with pg.app.state.engine.connect() as first, other.app.state.engine.connect() as second:
                assert first.scalar(text('SELECT count(*) FROM workspaces')) == 1
                assert second.scalar(text('SELECT count(*) FROM workspaces')) == 0
                assert first.scalar(text('SELECT current_schema()')) == second.scalar(text('SELECT current_schema()'))
                key = uuid.uuid4().int % (2 ** 31)
                first.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
                assert second.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': key})
            assert other.client('admin').get('/api/me').status_code == 200
        with admin.connect() as db:
            assert not db.scalar(text('SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)'), {'name': other_name})
            assert db.scalar(text('SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)'), {'name': template_name})
    finally:
        admin.dispose()


@pytest.mark.postgres
def test_postgres_template_migration_failure_drops_owned_template(postgres_worker_url, monkeypatch):
    attempted = []
    def broken_migration(url):
        attempted.append(make_url(url).database)
        raise RuntimeError('Synthetic migration failure')
    monkeypatch.setattr(conftest, 'migrate', broken_migration)
    with pytest.raises(RuntimeError, match='Synthetic migration failure'):
        next(conftest.postgres_template.__wrapped__(postgres_worker_url))
    assert len(attempted) == 1
    admin = create_engine(postgres_worker_url, hide_parameters=True)
    try:
        with admin.connect() as db:
            assert not db.scalar(text('SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)'), {'name': attempted[0]})
    finally:
        admin.dispose()


@pytest.mark.postgres
def test_postgres_template_harness_failure_drops_owned_clone(postgres_worker_url, postgres_template, monkeypatch):
    attempted = []
    def broken_harness(url, seed):
        attempted.append(make_url(url).database)
        raise RuntimeError('Synthetic harness failure')
    monkeypatch.setattr(conftest, 'Harness', broken_harness)
    with pytest.raises(RuntimeError, match='Synthetic harness failure'):
        next(conftest.postgres_harness(postgres_worker_url, postgres_template))
    assert len(attempted) == 1
    admin = create_engine(postgres_worker_url, hide_parameters=True)
    try:
        with admin.connect() as db:
            assert not db.scalar(text('SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=:name)'), {'name': attempted[0]})
    finally:
        admin.dispose()


@pytest.mark.postgres
def test_postgres_template_scale_harness_uses_seeded_clone(scale_harness):
    harness = scale_harness('pg')
    with harness.app.state.engine.connect() as db:
        assert db.scalar(text('SELECT count(*) FROM departments')) == 1
        assert db.scalar(text('SELECT count(*) FROM workspaces')) == 0
    assert harness.client('coordinator').get('/api/me').status_code == 200


def test_xdist_controller_prepares_one_seed_and_owns_its_lifetime(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setenv('ROTA_ALLOW_DISPOSABLE_TEST_DB', 'yes')
    monkeypatch.setenv('TEST_POSTGRES_URL', 'postgresql+psycopg://example.invalid/disposable')
    events, cleanups = [], []
    @contextmanager
    def template(url):
        events.append('created')
        try:
            yield 'postgresql+psycopg://example.invalid/rota_template_' + 'a' * 32, {'people': ['synthetic']}
        finally:
            events.append('dropped')
    monkeypatch.setattr(conftest, 'postgres_template', template)
    config = SimpleNamespace(add_cleanup=cleanups.append)
    nodes = [SimpleNamespace(config=config, workerinput={}) for _ in range(4)]
    for node in nodes:
        conftest.pytest_configure_node(node)
    assert events == ['created'] and len(cleanups) == 1
    assert all(node.workerinput == nodes[0].workerinput for node in nodes)
    cleanups[0]()
    assert events == ['created', 'dropped']


@pytest.mark.parametrize('environment,allowed', [('production', 'yes'), ('test', 'no')])
def test_shared_worker_template_rechecks_disposable_authorization(monkeypatch, environment, allowed):
    from types import SimpleNamespace
    monkeypatch.setenv('ROTA_ENV', environment)
    monkeypatch.setenv('ROTA_ALLOW_DISPOSABLE_TEST_DB', allowed)
    monkeypatch.setenv('TEST_POSTGRES_URL', 'postgresql+psycopg://example.invalid/disposable')
    monkeypatch.setattr('sqlalchemy.create_engine', lambda *args, **kwargs: pytest.fail('Must refuse before connecting'))
    request = SimpleNamespace(config=SimpleNamespace(workerinput={'rota_postgres_template': (
        'postgresql+psycopg://example.invalid/rota_template_' + 'a' * 32, {})}))
    with pytest.raises(pytest.fail.Exception, match='never run|requires TEST_POSTGRES_URL'):
        next(conftest.worker_postgres_template.__wrapped__('postgresql+psycopg://example.invalid/disposable', request))
