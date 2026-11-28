"""Queued jobs graduate from yielding to the existing bounded writer wait."""
import logging
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from apps.api import models as m
from apps.api.db import DatabaseWriteDeferred, transaction
from apps.api.settings import Settings
from apps.worker import main as worker
from tests.clock_support import freeze_clock
from tests.test_worker_write_timeouts import generation_job, simulated_clock


def test_guarded_claim_reports_a_zero_row_sqlite_update(h, caplog):
    workspace = h.workspace()
    job_id = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'validation'},
                       revision=workspace['revision'])['job']['id']
    engine = h.app.state.engine
    row = select(m.Job.__table__).where(m.Job.id == job_id)
    with transaction(engine, write=True) as db:
        # SQLite accepts a timestamp without fractional seconds. Its ORM read binds
        # the value back with fractional seconds, which does not match the stored text.
        db.execute(text('UPDATE jobs SET started_at = :stamp, heartbeat_at = :stamp WHERE id = :id'),
                   {'stamp': '2026-10-04 08:00:00', 'id': job_id})
        before = db.execute(row).one()
    assert (before.status, before.attempts, before.worker_id) == ('queued', 0, None)

    with caplog.at_level(logging.WARNING, logger='rota.worker'):
        assert worker.claim_pending(engine, kinds={'validation'}) is None

    warning, = (record for record in caplog.records if record.message == 'job_claim_guard_mismatch')
    assert (warning.job_id, warning.job_kind, warning.attempts) == (job_id, 'validation', 0)
    with transaction(engine) as db:
        assert db.execute(row).one() == before


@pytest.mark.parametrize('queued_seconds,threshold,waits', [(19, 20, False), (20, 20, True), (21, 20, True),
                                                          (6, 7, False), (7, 7, True)])
def test_busy_writer_defers_then_waits_for_overdue_claim(h, monkeypatch, queued_seconds, threshold, waits):
    freeze_clock(monkeypatch)
    job_id = generation_job(h)
    simulated_clock(monkeypatch)
    callback = lambda: None
    engine = h.app.state.engine.execution_options(rota_defer_busy_writer=True, rota_write_wait_callback=callback)
    settings = h.settings if threshold == 20 else replace(h.settings, worker_claim_max_defer_seconds=threshold)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job_id).created_at = m.now() - timedelta(seconds=queued_seconds)
    claim_pending = worker.claim_pending
    calls = []

    def busy_writer(actual, **kwargs):
        if actual.get_execution_options().get('rota_defer_busy_writer'):
            raise DatabaseWriteDeferred()
        assert actual.get_execution_options()['rota_write_wait_callback'] is callback
        calls.append('bounded_wait')
        return claim_pending(actual, **kwargs)

    def maintenance(actual, **kwargs):
        assert actual.get_execution_options()['rota_defer_busy_writer'] is True
        calls.append('housekeeping')
        if not waits:
            raise DatabaseWriteDeferred()

    def solve(*args, **kwargs):
        calls.append('solve')
        return {'status': 'candidate_feasible'}

    monkeypatch.setattr(worker, 'claim_pending', busy_writer)
    monkeypatch.setattr(worker, 'prune_security_state', maintenance)
    if waits:
        assert worker.process_one(engine, settings=settings, scheduler=solve)
        assert calls.index('bounded_wait') < calls.index('solve') < calls.index('housekeeping')
    else:
        with pytest.raises(DatabaseWriteDeferred):
            worker.process_one(engine, settings=settings, scheduler=solve)
        assert calls == ['housekeeping']
    assert engine.get_execution_options()['rota_defer_busy_writer'] is True
    with transaction(h.app.state.engine) as db:
        job = db.get(m.Job, job_id)
        assert job.status == ('succeeded' if waits else 'queued')
        assert job.attempts == (1 if waits else 0)


def test_every_deferral_logs_oldest_job_and_queue_age(h, monkeypatch, caplog):
    job_id = generation_job(h)
    generation_job(h)
    wall_start = m.now()
    clock = simulated_clock(monkeypatch)
    monotonic_start = clock[0]
    freeze_clock(monkeypatch, lambda: wall_start + timedelta(seconds=clock[0] - monotonic_start))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, job_id).created_at = m.now() - timedelta(seconds=7)
    monkeypatch.setattr('sys.argv', ['worker'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker.Settings, 'from_env', lambda: h.settings)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: h.app.state.engine)
    monkeypatch.setattr(worker.threading.Thread, 'start', lambda self: None)

    def busy(*args, **kwargs):
        raise DatabaseWriteDeferred()

    pauses = []
    advance = worker.time.sleep

    def pause(seconds):
        pauses.append(seconds)
        advance(seconds)
        if len(pauses) == 2:
            raise SystemExit('finished deferrals')

    monkeypatch.setattr(worker, 'prune_security_state', busy)
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with caplog.at_level(logging.INFO, logger='rota.worker'), pytest.raises(SystemExit, match='finished deferrals'):
        worker.main()
    records = [record for record in caplog.records if record.message == 'worker_pass_deferred']
    assert len(records) == 2
    assert all(record.levelno == logging.INFO and record.job_id == job_id for record in records)
    assert [record.queued_seconds for record in records] == [7, 9]


def test_deferral_is_logged_when_queue_age_is_temporarily_unavailable(monkeypatch, caplog):
    monkeypatch.setattr('sys.argv', ['worker', '--once'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker.Settings, 'from_env', lambda: Settings(environment='test'))
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: object())

    def busy(*args, **kwargs):
        raise DatabaseWriteDeferred()

    def disconnect(engine):
        raise OperationalError('SELECT 1', {}, Exception('database system is in recovery mode'))

    monkeypatch.setattr(worker, 'process_one', busy)
    monkeypatch.setattr(worker, 'oldest_queued_job', disconnect)
    with caplog.at_level(logging.INFO, logger='rota.worker'):
        worker.main()
    record, = [record for record in caplog.records if record.message == 'worker_pass_deferred']
    assert record.job_id is None and record.queued_seconds is None


@pytest.mark.parametrize('value', [1, 20, 600])
def test_claim_defer_setting_accepts_bounded_environment_values(monkeypatch, value):
    monkeypatch.setenv('ROTA_WORKER_CLAIM_MAX_DEFER_SECONDS', str(value))
    settings = Settings.from_env()
    settings.check(require_authentication=False)
    assert settings.worker_claim_max_defer_seconds == value


@pytest.mark.parametrize('value', [0, -1, 601])
def test_claim_defer_setting_rejects_out_of_range_values(value):
    with pytest.raises(RuntimeError, match='ROTA_WORKER_CLAIM_MAX_DEFER_SECONDS'):
        replace(Settings(environment='test'), worker_claim_max_defer_seconds=value).check(require_authentication=False)


def test_claim_defer_setting_defaults_to_twenty_seconds():
    assert Settings().worker_claim_max_defer_seconds == 20


@pytest.mark.parametrize('value', ['invalid', '1.5'])
def test_claim_defer_setting_rejects_invalid_environment_values(monkeypatch, value):
    monkeypatch.setenv('ROTA_WORKER_CLAIM_MAX_DEFER_SECONDS', value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_queue_age_ignores_finished_and_disabled_jobs_and_clamps_future_dates(h, monkeypatch):
    freeze_clock(monkeypatch)
    job_id = generation_job(h)
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        job = db.get(m.Job, job_id)
        job.status = 'succeeded'
    assert worker.oldest_queued_job(engine) == {'job_id': None, 'queued_seconds': 0}
    with transaction(engine, write=True) as db:
        job = db.get(m.Job, job_id)
        job.status, job.created_at = 'queued', m.now() + timedelta(seconds=1)
    assert worker.oldest_queued_job(engine) == {'job_id': job_id, 'queued_seconds': 0}
    with transaction(engine, write=True) as db:
        job = db.get(m.Job, job_id)
        db.get(m.Workspace, job.workspace_id).origin = 'auto'
        db.get(m.Department, job.department_id).score_check_enabled = False
    assert worker.oldest_queued_job(engine) == {'job_id': None, 'queued_seconds': 0}


@pytest.mark.postgres
def test_busy_postgres_writer_defers_before_threshold_and_waits_after(pg, monkeypatch, caplog):
    freeze_clock(monkeypatch)
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    job_id = generation_job(pg)
    base = pg.app.state.engine
    monkeypatch.setattr('sys.argv', ['worker', '--once'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker.Settings, 'from_env', lambda: pg.settings)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: base)
    with transaction(base, write=True) as db:
        db.get(m.Job, job_id).created_at = m.now() - timedelta(seconds=19)
    with transaction(base, write=True), caplog.at_level(logging.INFO, logger='rota.worker'):
        worker.main()
    records = [record for record in caplog.records if record.message == 'worker_pass_deferred']
    assert len(records) == 1 and records[0].job_id == job_id and records[0].queued_seconds == 19
    with transaction(base, write=True) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'queued' and row.attempts == 0
        row.created_at = m.now() - timedelta(seconds=21)
    waiting = Event()
    engine = base.execution_options(rota_defer_busy_writer=True, rota_write_wait_callback=waiting.set)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(base, write=True):
            result = pool.submit(worker.process_one, engine, settings=pg.settings,
                                 scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible'})
            assert waiting.wait(10), 'Overdue claim did not enter the bounded writer wait'
            assert not result.done()
        assert result.result(timeout=10)
    with transaction(base) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.attempts == 1
