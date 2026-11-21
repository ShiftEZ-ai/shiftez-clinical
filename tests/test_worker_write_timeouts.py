"""A busy writer must not discard a solve or its computed result within its lease."""
import threading
import time
import logging
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from apps.api import db as database, models as m
from apps.api.db import DatabaseWriteTimeout, transaction
from apps.worker import main as worker


@pytest.mark.parametrize('lease_seconds,heartbeat_seconds,pool_timeout,starts', [
    (600, 3, 5, True),
    (600, 3, 596, True),
    (600, 3, 597, False),
    (600, 3, 598, False),
    (600, 10, 589, True),
    (600, 10, 590, False),
    (1200, 3, 1000, True),
    (1200, 3, 1196, True),
    (1200, 3, 1197, False),
])
def test_worker_startup_requires_positive_job_lease_budget(monkeypatch, lease_seconds, heartbeat_seconds, pool_timeout, starts):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setenv('ROTA_DB_POOL_TIMEOUT_SECONDS', str(pool_timeout))
    monkeypatch.setenv('ROTA_SOLVER_WORKERS', '1')
    monkeypatch.setattr(worker, 'JOB_LEASE_SECONDS', lease_seconds)
    monkeypatch.setattr(worker, 'JOB_HEARTBEAT_SECONDS', heartbeat_seconds)
    monkeypatch.setattr('sys.argv', ['worker', '--once'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    calls = []
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: calls.append('engine'))
    monkeypatch.setattr(worker, 'process_one', lambda *args, **kwargs: calls.append('job'))

    if starts:
        worker.main()
    else:
        with pytest.raises(RuntimeError, match='ROTA_DB_POOL_TIMEOUT_SECONDS.*positive.*lease'):
            worker.main()
    assert calls == (['engine', 'job'] if starts else [])


def generation_job(h):
    workspace = h.workspace()
    return h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                     revision=workspace['revision'])['job']['id']


def test_worker_loop_backs_off_repeated_write_timeouts(monkeypatch, caplog):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr('sys.argv', ['worker'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    engine = object()
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: engine)
    attempts, sleeps = [], []

    def process(actual, **kwargs):
        assert actual is engine
        attempts.append(actual)
        if len(attempts) <= 2:
            raise DatabaseWriteTimeout()

    def pause(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise SystemExit('test finished after recovery')

    monkeypatch.setattr(worker, 'process_one', process)
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit, match='after recovery'):
        worker.main()
    assert len(attempts) == 3 and sleeps == [2, 4, 8]
    assert sum(record.message == 'worker_pass_write_timeout' for record in caplog.records) == 2


def test_worker_loop_survives_transient_database_recovery(monkeypatch, caplog):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr(sys, 'argv', ['worker'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    engine = object()
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: engine)
    attempts, sleeps = [], []

    def process(actual, **kwargs):
        assert actual is engine
        attempts.append(actual)
        if len(attempts) == 1:
            from sqlalchemy.exc import OperationalError
            raise OperationalError('SELECT 1', {}, Exception('database system is in recovery mode'))

    def pause(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise SystemExit('test finished after recovery')

    monkeypatch.setattr(worker, 'process_one', process)
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit, match='after recovery'):
        worker.main()
    assert len(attempts) == 2 and sleeps == [2, 4]
    assert any(record.message == 'worker_pass_database_error' for record in caplog.records)


def test_worker_loop_does_not_swallow_nontransient_database_errors(monkeypatch):
    monkeypatch.setenv('ROTA_ENV', 'test')
    monkeypatch.setattr(sys, 'argv', ['worker', '--once'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: object())
    from sqlalchemy.exc import SQLAlchemyError
    monkeypatch.setattr(worker, 'process_one', lambda *args, **kwargs: (_ for _ in ()).throw(SQLAlchemyError('synthetic bug')))
    with pytest.raises(SQLAlchemyError, match='synthetic bug'):
        worker.main()


@pytest.mark.parametrize('once', [False, True])
def test_worker_loop_rolls_back_dispatch_timeout_and_recovers(h, monkeypatch, caplog, once):
    engine = h.app.state.engine
    monkeypatch.setattr('sys.argv', ['worker', '--once'] if once else ['worker'])
    monkeypatch.setattr(worker, 'configure_logging', lambda: None)
    monkeypatch.setattr(worker.Settings, 'from_env', lambda: h.settings)
    monkeypatch.setattr(worker, 'make_engine', lambda *args, **kwargs: engine)
    dispatch = worker.dispatch_batch
    attempts, sleeps = [], []
    with transaction(engine) as db:
        original = db.get(m.Department, h.seed['department_id']).name

    def busy_dispatch(db):
        attempts.append('dispatch')
        if len(attempts) == 1:
            db.get(m.Department, h.seed['department_id']).name = 'uncommitted timeout'
            db.flush()
            raise DatabaseWriteTimeout()
        assert db.get(m.Department, h.seed['department_id']).name == original
        return dispatch(db)

    def pause(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise SystemExit('test finished after recovery')

    monkeypatch.setattr(worker, 'dispatch_batch', busy_dispatch)
    monkeypatch.setattr(worker, 'dispatch_pending', lambda engine: worker.retry_write(engine, worker.dispatch_batch))
    monkeypatch.setattr(worker.time, 'sleep', pause)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(DatabaseWriteTimeout if once else SystemExit):
            worker.main()
    assert attempts == ['dispatch'] * (1 if once else 2)
    assert sleeps == ([] if once else [2, 4])
    assert any(record.message == 'worker_pass_write_timeout' and record.error_code == 'DatabaseWriteTimeout'
               for record in caplog.records)
    assert 'uncommitted timeout' not in caplog.text
    with transaction(engine) as db:
        assert db.get(m.Department, h.seed['department_id']).name == original


@pytest.mark.parametrize('failure', [DatabaseWriteTimeout, RuntimeError])
def test_heartbeat_write_timeouts_keep_solving_and_recover(h, monkeypatch, failure):
    job_id = generation_job(h)
    recovered = threading.Event()
    heartbeat_job = worker.heartbeat_job
    attempts = []
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)

    def busy_heartbeat(db, claim, progress):
        attempts.append(claim['attempt'])
        if len(attempts) <= 2:
            raise failure()
        alive = heartbeat_job(db, claim, progress)
        recovered.set()
        return alive

    monkeypatch.setattr(worker, 'heartbeat_job', busy_heartbeat)
    expected = {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    def solve(snapshot, *, progress, cancelled):
        progress({'stage': 'finding'})
        assert recovered.wait(5), 'Heartbeat did not recover from write contention'
        assert not cancelled(), 'Temporary contention cancelled the running solve'
        progress({'stage': 'ready'})
        return expected

    assert worker.process_one(h.app.state.engine, scheduler=solve)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.error_code is None
        assert row.result == expected and row.progress['stage'] == 'ready'
        assert len(attempts) >= 3


@pytest.mark.parametrize('engine', ['injected', 'greedy'])
def test_heartbeat_timeouts_cancel_only_near_lease_expiry(h, monkeypatch, engine):
    job_id = generation_job(h)
    attempts = []
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)
    monkeypatch.setattr(worker, 'JOB_LEASE_SECONDS', h.settings.db_pool_timeout_seconds + 1)

    def busy_heartbeat(db, claim, progress):
        attempts.append(time.monotonic())
        raise DatabaseWriteTimeout()

    monkeypatch.setattr(worker, 'heartbeat_job', busy_heartbeat)

    def solve(snapshot, *, progress, cancelled):
        deadline = time.monotonic() + 5
        tick = threading.Event()
        while not cancelled() and time.monotonic() < deadline:
            tick.wait(.01)
        assert cancelled(), 'An expired lease did not stop the solve'
        assert len(attempts) > 1
        return {'status': 'cancelled'}

    monkeypatch.setattr(worker, 'generate', lambda snapshot, *, cancelled: solve(
        snapshot, progress=lambda row: None, cancelled=cancelled))
    assert worker.process_one(h.app.state.engine, scheduler=solve if engine == 'injected' else None)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'running' and row.result is None and row.error_code is None


def test_worker_status_heartbeat_forwards_lease_deadline(monkeypatch):
    engine = object()
    deadlines = []

    def write(engine, operation, *, deadline, serialize):
        deadlines.append(deadline)
        assert serialize is False  # liveness is not a scheduling input; it never joins the write queue

    monkeypatch.setattr(worker, 'retry_write', write)
    monkeypatch.setattr(worker, '_beat', {})
    monkeypatch.setattr(worker, 'ALIVE_FILE', '')
    worker.heartbeat(engine, deadline=123.0)
    assert deadlines == [123.0]


def simulated_clock(monkeypatch):
    clock = [time.monotonic()]

    def advance(seconds):
        clock[0] += seconds

    clock_source = SimpleNamespace(monotonic=lambda: clock[0], sleep=advance)
    monkeypatch.setattr(worker, 'time', clock_source)
    monkeypatch.setattr(database, 'time', clock_source)
    return clock


def test_waiting_to_claim_does_not_consume_new_job_lease(h, monkeypatch):
    assert_waiting_to_claim_preserves_lease(h, monkeypatch)


def test_waiting_to_claim_does_not_consume_new_job_lease_on_postgres(pg, monkeypatch):
    assert_waiting_to_claim_preserves_lease(pg, monkeypatch)


def assert_waiting_to_claim_preserves_lease(h, monkeypatch):
    job_id = generation_job(h)
    clock = simulated_clock(monkeypatch)
    finish_attempts = []
    retry_write = worker.retry_write
    lease_seconds = h.settings.db_pool_timeout_seconds + worker.GENERATION_HEARTBEAT_SECONDS + 1
    monkeypatch.setattr(worker, 'JOB_LEASE_SECONDS', lease_seconds)

    def write(engine, operation, **kwargs):
        if operation is worker.claim_job:
            clock[0] += lease_seconds * 2
        if (kwargs.get('context') or {}).get('job_phase') == 'finish':
            finish_attempts.append(clock[0])
            if len(finish_attempts) == 1:
                raise DatabaseWriteTimeout()
        return retry_write(engine, operation, **kwargs)

    monkeypatch.setattr(worker, 'retry_write', write)
    assert worker.process_one(h.app.state.engine, scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible'})
    assert len(finish_attempts) >= 2
    assert finish_attempts[1] > finish_attempts[0]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.result['status'] == 'candidate_feasible'


def test_completion_deadline_reserves_configured_pool_wait(h, monkeypatch):
    assert_completion_deadline_reserves_pool_wait(h, monkeypatch)


def test_completion_deadline_reserves_configured_pool_wait_on_postgres(pg, monkeypatch):
    assert_completion_deadline_reserves_pool_wait(pg, monkeypatch)


def assert_completion_deadline_reserves_pool_wait(h, monkeypatch):
    generation_job(h)
    engine = h.app.state.engine
    engine.update_execution_options(rota_write_settings=replace(h.settings, db_pool_timeout_seconds=60))
    clock = simulated_clock(monkeypatch)[0]
    deadlines = []
    retry_write = worker.retry_write
    monkeypatch.setattr(worker, 'JOB_LEASE_SECONDS', 65)

    def record_deadline(engine, operation, **kwargs):
        if kwargs.get('deadline') is not None:
            assert not deadlines, 'Unexpected retry of a lease-bound write while measuring the completion deadline'
            deadlines.append(kwargs['deadline'])
        return retry_write(engine, operation, **kwargs)

    monkeypatch.setattr(worker, 'retry_write', record_deadline)
    assert worker.process_one(engine, scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible'})
    # Subtracting the lease margin from a monotonic float can lose sub-microsecond precision.
    assert deadlines == pytest.approx([clock + 4], rel=0, abs=1e-6)


def test_finish_job_retries_same_result_and_preserves_final_progress(h, monkeypatch):
    job_id = generation_job(h)
    finish_job = worker.finish_job
    results, delays, solves = [], [], []
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=delays.append))

    def busy_finish(db, claim, result, error, *, progress=None, error_detail=None):
        results.append(result)
        if len(results) <= 2:
            raise DatabaseWriteTimeout()
        return finish_job(db, claim, result, error, progress=progress, error_detail=error_detail)

    monkeypatch.setattr(worker, 'finish_job', busy_finish)
    expected = {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    def solve(snapshot, *, progress, cancelled):
        solves.append(snapshot)
        progress({'stage': 'ready'})
        return expected

    assert worker.process_one(h.app.state.engine, scheduler=solve)
    assert len(solves) == 1 and len(results) == 3
    assert all(result is expected for result in results)
    assert delays == [.1, .2]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.result == expected
        assert row.error_code is None and row.progress['stage'] == 'ready'


def test_finish_job_stops_retrying_before_lease_expiry(h, monkeypatch, caplog):
    job_id = generation_job(h)
    clock = [time.monotonic()]
    attempts, delays = [], []

    def advance(seconds):
        delays.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(worker, 'JOB_LEASE_SECONDS', h.settings.db_pool_timeout_seconds + worker.GENERATION_HEARTBEAT_SECONDS + 1)
    clock_source = SimpleNamespace(monotonic=lambda: clock[0], sleep=advance)
    monkeypatch.setattr(worker, 'time', clock_source)
    monkeypatch.setattr(database, 'time', clock_source)  # The absolute write deadline uses this same clock.

    def busy_finish(db, claim, result, error, *, progress=None, error_detail=None):
        attempts.append(clock[0])
        clock[0] += .25
        raise DatabaseWriteTimeout()

    monkeypatch.setattr(worker, 'finish_job', busy_finish)
    assert worker.process_one(h.app.state.engine, scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible'})
    assert len(attempts) == 3 and len(delays) == 2
    assert 'job_result_write_timeout' in caplog.text
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'running' and row.result is None and row.error_code is None


@pytest.mark.parametrize('ownership_change', ['cancelled', 'new_attempt'])
def test_finish_retry_does_not_overwrite_lost_ownership(h, monkeypatch, ownership_change):
    job_id = generation_job(h)
    finish_job = worker.finish_job
    attempts = []
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=lambda seconds: None))

    def cancel_before_retry(db, claim, result, error, *, progress=None, error_detail=None):
        attempts.append(claim['attempt'])
        if len(attempts) == 1:
            raise DatabaseWriteTimeout()
        row = db.get(m.Job, job_id)
        if ownership_change == 'cancelled':
            row.status = 'cancelled'
        else:
            row.attempts += 1
        return finish_job(db, claim, result, error, progress=progress, error_detail=error_detail)

    monkeypatch.setattr(worker, 'finish_job', cancel_before_retry)
    assert worker.process_one(h.app.state.engine, scheduler=lambda *args, **kwargs: {'status': 'candidate_feasible'})
    assert len(attempts) == 2
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == ('cancelled' if ownership_change == 'cancelled' else 'running')
        assert row.result is None


@pytest.mark.postgres
def test_worker_liveness_never_queues_behind_the_global_write_lock(pg):
    """SW3-D-21: a heartbeat waiting behind slow commands made a healthy worker look dead."""
    from sqlalchemy import text
    engine = pg.app.state.engine
    worker._beat.clear()
    with transaction(engine, write=True) as holder:
        assert holder.scalar(text('SELECT count(*) FROM pg_locks WHERE locktype = :t AND objid = :k AND granted AND pid = pg_backend_pid()'),
                             {'t': 'advisory', 'k': database.WRITE_LOCK_KEY}) == 1
        worker.heartbeat(engine)
    with transaction(engine) as db:
        row = db.get(m.WorkerHeartbeat, worker.WORKER_ID)
        assert row is not None and row.passes >= 1
