"""A restarted owner fences its previous attempt without expiring another replica's live lease."""
import pytest
from apps.api import models as m
from apps.api.db import retry_write, transaction
from apps.api.serialization import job_record
from apps.worker import main as worker
from tests.test_fix_list_performance import backend
from tests.test_data_safety import session_limits_database


def test_worker_owner_migration_preserves_existing_rows(session_limits_database):
    from alembic import command
    from sqlalchemy import MetaData, inspect
    from apps.api.db import make_engine
    from scripts import manage
    from tests.migration_support import historical_seed, without_installation_admin_backfill
    url = session_limits_database
    config = manage.alembic_config(url)
    command.upgrade(config, '0128_workspace_read_projections')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            historical_seed(db)
        old = MetaData()
        old.reflect(bind=engine)

        def snapshot():
            with engine.connect() as db:
                return {name: list(db.execute(table.select().order_by(*table.primary_key.columns)))
                        for name, table in old.tables.items() if name != 'alembic_version'}

        before = snapshot()
        command.upgrade(config, 'head')
        assert without_installation_admin_backfill(snapshot()) == before
        assert next(c for c in inspect(engine).get_columns('jobs') if c['name'] == 'worker_id')['nullable']
        assert 'ix_jobs_worker_lease' in {index['name'] for index in inspect(engine).get_indexes('jobs')}
    finally:
        engine.dispose()


@pytest.mark.parametrize('other_owner', [None, 'another-worker'])
def test_startup_recovers_only_owned_jobs_immediately_and_fences_previous_attempt(backend, monkeypatch, other_owner):
    h = backend
    monkeypatch.setattr(worker, 'WORKER_ID', 'restarting-replica')
    w = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': w['id'], 'kind': 'validation'},
                    revision=w['revision'])['job']
    engine = h.app.state.engine
    old = retry_write(engine, worker.claim_job)
    assert old['id'] == job['id']
    with transaction(engine, write=True) as db:
        owned = db.get(m.Job, job['id'])
        assert owned.worker_id == worker.WORKER_ID
        foreign = m.Job(kind='validation', workspace_id=w['id'], department_id=owned.department_id,
            requested_by=owned.requested_by, input_fingerprint=owned.input_fingerprint, input_snapshot={},
            status='running', started_at=m.now(), heartbeat_at=m.now(), attempts=1, worker_id=other_owner)
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
        assert 'worker_id' not in job_record(owned)
    assert retry_write(engine, worker.claim_job) is None
    resumed = retry_write(engine, lambda db: worker.claim_job(db, recovering_worker=worker.WORKER_ID))
    assert resumed['id'] == old['id'] and resumed['attempt'] == old['attempt'] + 1
    assert not retry_write(engine, lambda db: worker.heartbeat_job(db, old, {}))
    assert not retry_write(engine, lambda db: worker.finish_job(db, old, {'valid': True}, None))
    with transaction(engine) as db:
        foreign = db.get(m.Job, foreign_id)
        assert foreign.status == 'running' and foreign.attempts == 1 and foreign.worker_id == other_owner


def test_main_recovers_owned_work_once_per_successful_start(monkeypatch):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr('sys.argv', ['worker'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker, 'make_engine', lambda *a, **k: object())
    monkeypatch.setattr('apps.worker.score_check.run_loop', lambda *args: None)
    calls = []

    def run(_engine, **kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise SystemExit

    monkeypatch.setattr(worker, 'process_one', run)
    monkeypatch.setattr(worker.time, 'sleep', lambda _seconds: None)
    with pytest.raises(SystemExit):
        worker.main()
    assert [call['recover_owned'] for call in calls] == [True, False]
    assert [call['solver_workers'] for call in calls] == [1, 1]
