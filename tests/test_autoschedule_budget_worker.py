"""Saved preparation and incumbent hints belong to the durable job, not a worker process."""
from datetime import datetime, timedelta
from dataclasses import replace
import threading
import gc
from types import SimpleNamespace
from weakref import ref

import pytest
from sqlalchemy import event, select

from apps.api import models as m
from apps.api.db import DatabaseWriteTimeout, make_engine, retry_write, transaction
from apps.worker import main as worker
from packages.domain import cp_sat
from packages.domain.solver_rules import COMPILER_VERSION
from packages.domain.worker_contract import JOB_LEASE_SECONDS
from packages.domain.solver_resume import decompress_state


def generation_job(h):
    workspace = h.workspace()
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job['id'])
        row.input_snapshot = {**row.input_snapshot, 'generation_engine': 'cp_sat', 'compiler_version': COMPILER_VERSION}
        row.search_control = {'minutes': 5, 'stop': False}
    return job['id']


def unscoped_generation_job(h):
    job_id = generation_job(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Job, job_id)
        row.input_snapshot = {key: value for key, value in row.input_snapshot.items()
                              if key not in {'generation_scope', 'generation_scope_source'}}
    return job_id


def expire(db, job_id, *, exhausted=False):
    row = db.get(m.Job, job_id)
    row.heartbeat_at = m.now() - timedelta(seconds=JOB_LEASE_SECONDS + 1)
    if exhausted:
        row.attempts = 3


def saved_state(row):
    state = decompress_state(row.resume_state)
    return {**state, **(row.resume_checkpoint or {})} if state is not None else None


@pytest.mark.parametrize('recovery', ['expired', 'owned'])
def test_checkpoint_survives_worker_restart_and_is_passed_to_the_recovered_attempt(h, monkeypatch, recovery):
    job_id = generation_job(h)
    engine = h.app.state.engine
    state = {'key': 'synthetic-input', 'model': 'synthetic-model', 'prepared': {'data': []}, 'hint': [0, 1]}

    def interrupted(snapshot, *, checkpoint, resume_state, **kwargs):
        assert resume_state is None
        checkpoint(state)
        with transaction(engine) as db:
            assert saved_state(db.get(m.Job, job_id)) == state
        raise SystemExit('Simulated worker death after durable checkpoint')

    monkeypatch.setattr(cp_sat, 'generate', interrupted)
    with pytest.raises(SystemExit, match='Simulated worker death'):
        worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    if recovery == 'expired':
        retry_write(engine, lambda db: expire(db, job_id))
    restarted = make_engine(h.settings.database_url)

    def resumed(snapshot, *, resume_state, checkpoint, search_control, **kwargs):
        assert resume_state['encoding'] == 'gzip' and 'model' not in resume_state
        assert decompress_state(resume_state) == state
        assert search_control() == {'minutes': 5, 'stop': False}
        checkpoint({'key': state['key'], 'hint': state['hint']})
        return {'status': 'needs_more_time', 'additions': [], 'removals': []}

    monkeypatch.setattr(cp_sat, 'generate', resumed)
    try:
        assert worker.process_one(restarted, recover_owned=recovery == 'owned')
        with transaction(restarted) as db:
            row = db.get(m.Job, job_id)
            assert row.status == 'succeeded' and row.attempts == 2
            assert row.result['status'] == 'needs_more_time'
            assert saved_state(row) == state
    finally:
        restarted.dispose()


def test_running_search_observes_added_time_and_stop_from_heartbeat(h, monkeypatch):
    job_id = generation_job(h)
    engine = h.app.state.engine
    renewed = threading.Condition()
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)
    def heartbeat(*args, **kwargs):
        with renewed:
            renewed.notify_all()
    monkeypatch.setattr(worker, 'heartbeat', heartbeat)

    def solving(snapshot, *, search_control, cancelled, **kwargs):
        assert search_control() == {'minutes': 5, 'stop': False}
        with transaction(engine, write=True) as db:
            db.get(m.Job, job_id).search_control = {'minutes': 10, 'stop': False}
        with renewed:
            assert renewed.wait_for(lambda: search_control() == {'minutes': 10, 'stop': False}, 5), 'Worker did not poll the updated search deadline'
        with transaction(engine, write=True) as db:
            db.get(m.Job, job_id).search_control = {'minutes': 10, 'stop': True}
        with renewed:
            assert renewed.wait_for(lambda: search_control() == {'minutes': 10, 'stop': True}, 5), 'Worker did not poll Stop and keep best'
        assert not cancelled(), 'Keep-best is a solver stop, not job cancellation'
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    monkeypatch.setattr(cp_sat, 'generate', solving)
    assert worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    with transaction(engine) as db:
        assert db.get(m.Job, job_id).result['status'] == 'candidate_feasible'


@pytest.mark.postgres
def test_heartbeat_commits_a_pending_checkpoint(pg, monkeypatch):
    job_id = unscoped_generation_job(pg)
    engine = pg.app.state.engine
    persisted = threading.Event()
    main_thread = threading.get_ident()
    checkpoint_job = worker.checkpoint_job
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)
    monkeypatch.setattr(worker, 'heartbeat', lambda *args, **kwargs: None)

    def tracked_checkpoint(db, claim, state, **kwargs):
        result = checkpoint_job(db, claim, state, **kwargs)
        if threading.get_ident() != main_thread and state.get('key') == 'heartbeat-checkpoint':
            persisted.set()
        return result

    monkeypatch.setattr(worker, 'checkpoint_job', tracked_checkpoint)

    def solving(snapshot, *, checkpoint, **kwargs):
        state = {'key': 'heartbeat-checkpoint', 'hint': [1], 'has_solution': True,
                 'search_elapsed_seconds': 1,
                 'result': {'status': 'candidate_feasible', 'input_fingerprint': snapshot['fingerprint'],
                            'additions': [], 'removals': []}}
        checkpoint(state)
        assert persisted.wait(5), 'Heartbeat did not persist the pending checkpoint'
        with transaction(engine) as db:
            row = db.get(m.Job, job_id)
            assert row.resume_checkpoint['key'] == state['key']
            assert row.resume_checkpoint['hint'] == state['hint']
        return state['result']

    monkeypatch.setattr(cp_sat, 'generate', solving)
    assert worker.process_one(engine, settings=replace(pg.settings, generation_engine='cp_sat'))


@pytest.mark.postgres
def test_checkpoint_and_heartbeat_do_not_invert_postgres_writer_locks(pg, monkeypatch):
    job_id = unscoped_generation_job(pg)
    engine = pg.app.state.engine.execution_options(
        rota_write_settings=replace(pg.settings, command_lock_timeout_ms=100, command_write_budget_ms=1_000))
    assert engine.dialect.name == 'postgresql'
    monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)
    monkeypatch.setattr(worker, 'heartbeat', lambda *args, **kwargs: None)
    monitor_ready = threading.Event()
    main_attempted = threading.Event()
    checkpoint_started = threading.Event()
    done = threading.Event()
    timed_out = threading.Event()
    errors = []
    worker_thread_id = []
    gate_lock = threading.Lock()
    gate_used = False
    main_seen = False
    heartbeat_job = worker.heartbeat_job
    retry_write = worker.retry_write

    def tracked_heartbeat_job(db, claim, progress):
        nonlocal gate_used
        result = heartbeat_job(db, claim, progress)
        if threading.get_ident() != worker_thread_id[0]:
            with gate_lock:
                first = not gate_used
                gate_used = True
            if first:
                monitor_ready.set()
                main_attempted.wait(.5)
        return result

    def tracked_retry_write(*args, **kwargs):
        nonlocal main_seen
        if (worker_thread_id and threading.get_ident() == worker_thread_id[0]
                and checkpoint_started.is_set() and not main_seen):
            main_seen = True
            assert monitor_ready.wait(5), 'Heartbeat did not reach its writer transaction'
            main_attempted.set()
        try:
            return retry_write(*args, **kwargs)
        except DatabaseWriteTimeout:
            timed_out.set()
            raise

    monkeypatch.setattr(worker, 'heartbeat_job', tracked_heartbeat_job)
    monkeypatch.setattr(worker, 'retry_write', tracked_retry_write)

    def solving(snapshot, *, checkpoint, **kwargs):
        assert monitor_ready.wait(5), 'Heartbeat did not reach its writer transaction'
        checkpoint_started.set()
        checkpoint({'key': 'lock-order', 'hint': [1]})
        return {'status': 'needs_more_time', 'additions': [], 'removals': []}

    monkeypatch.setattr(cp_sat, 'generate', solving)

    def run_worker():
        worker_thread_id.append(threading.get_ident())
        try:
            worker.process_one(engine, settings=replace(pg.settings, generation_engine='cp_sat'))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    assert done.wait(5), 'Checkpoint and heartbeat deadlocked'
    assert not errors
    assert gate_used and main_seen and not timed_out.is_set()
    with transaction(engine) as db:
        assert db.get(m.Job, job_id).status == 'succeeded'


@pytest.mark.parametrize('lost_ownership', ['new_attempt', 'cancelled', 'access_revoked'])
def test_checkpoint_rejects_a_worker_that_no_longer_owns_an_authorised_attempt(h, lost_ownership):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    state = {'key': 'synthetic-key', 'prepared': 'kept', 'hint': [1]}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job_id)
        if lost_ownership == 'new_attempt':
            row.attempts += 1
        elif lost_ownership == 'cancelled':
            row.status = 'cancelled'
        else:
            db.get(m.Identity, row.requested_by).disabled = True
    assert not retry_write(engine, lambda db: worker.checkpoint_job(db, claim, {'hint': [0]}))
    with transaction(engine) as db:
        assert saved_state(db.get(m.Job, job_id)) == (state if lost_ownership == 'new_attempt' else None)


@pytest.mark.parametrize('has_checkpoint', [True, False])
def test_exhausted_worker_recovery_keeps_saved_work_as_a_resumable_result(h, has_checkpoint):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    state = {'key': 'synthetic-key', 'prepared': 'kept', 'hint': [1]} if has_checkpoint else None
    if has_checkpoint:
        assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    retry_write(engine, lambda db: expire(db, job_id, exhausted=True))
    assert retry_write(engine, worker.claim_job) is None
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.error_code is None
        assert row.result['status'] == 'needs_more_time' and row.completed_at is not None
        assert saved_state(row) == state
    assert not retry_write(engine, lambda db: worker.checkpoint_job(db, claim, {'hint': [0]}))


def test_injected_scheduler_keeps_its_existing_signature(h):
    generation_job(h)

    def scheduler(snapshot, *, progress, cancelled):
        return {'status': 'candidate_feasible', 'additions': [], 'removals': []}

    assert worker.process_one(h.app.state.engine, scheduler=scheduler)


@pytest.mark.parametrize('exhausted', [False, True])
def test_recovery_returns_the_already_validated_proposal_without_another_search_attempt(h, exhausted):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'],
                'algorithm_version': COMPILER_VERSION, 'additions': [{'shift_id': 'synthetic'}],
                'removals': [], 'phase_timings': {'preparation_seconds': 12, 'search_seconds': 5}}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'synthetic-key', 'prepared': 'kept', 'hint': [1], 'result': proposal}))
    retry_write(engine, lambda db: expire(db, job_id, exhausted=exhausted))
    assert retry_write(engine, worker.claim_job) is None
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.result == proposal and row.status == 'succeeded'
        assert row.attempts == (3 if exhausted else 1)
        assert row.resume_state is None and row.resume_checkpoint is None


@pytest.mark.parametrize('invalid', ['fingerprint', 'permission'])
def test_first_recovery_does_not_return_an_unmatched_or_unauthorised_saved_proposal(h, invalid):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': (
        'another-input' if invalid == 'fingerprint' else claim['input']['fingerprint'])}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'synthetic-key', 'prepared': 'kept', 'hint': [1], 'result': proposal}))
    with transaction(engine, write=True) as db:
        expire(db, job_id)
        if invalid == 'permission':
            db.get(m.Identity, claim['requested_by']).disabled = True
    next_attempt = retry_write(engine, worker.claim_job)
    assert bool(next_attempt) == (invalid == 'fingerprint')
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.result == (proposal if invalid == 'permission' else None)
        assert row.status == ('running' if invalid == 'fingerprint' else 'failed')
        if invalid == 'permission':
            assert row.error_code == 'ACCESS_REVOKED'


@pytest.mark.parametrize('recovery', ['heartbeat', 'finish'])
def test_temporary_checkpoint_contention_keeps_solving_and_persists_pending_best(h, monkeypatch, recovery):
    job_id = generation_job(h)
    engine = h.app.state.engine
    persisted = threading.Event()
    checkpoint_job = worker.checkpoint_job
    writes = []
    state = {'key': 'synthetic-key', 'model': 'data', 'prepared': 'kept', 'hint': [1]}

    def busy_once(db, claim, saved, **kwargs):
        assert 'model' not in saved and 'prepared' not in saved
        writes.append(saved)
        if len(writes) == 1:
            raise DatabaseWriteTimeout()
        alive = checkpoint_job(db, claim, saved, **kwargs)
        persisted.set()
        return alive

    monkeypatch.setattr(worker, 'checkpoint_job', busy_once)
    if recovery == 'heartbeat':
        monkeypatch.setattr(worker, 'GENERATION_HEARTBEAT_SECONDS', .01)

    def solving(snapshot, *, checkpoint, cancelled, **kwargs):
        checkpoint(state)
        assert not cancelled(), 'Temporary checkpoint contention cancelled a useful search'
        if recovery == 'heartbeat':
            assert persisted.wait(5), 'Heartbeat did not retry the pending best solution'
        return {'status': 'needs_more_time', 'additions': [], 'removals': []}

    monkeypatch.setattr(cp_sat, 'generate', solving)
    assert worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.error_code is None
        assert saved_state(row) == state
    assert len(writes) == 2 and all(saved == {'key': state['key'], 'hint': state['hint']} for saved in writes)


def test_incumbent_checkpoint_does_not_read_or_rewrite_immutable_preparation(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    state = {'key': 'synthetic-key', 'model': 'large immutable model', 'prepared': {'data': [1, 2]},
             'hint': [0], 'has_solution': True}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(engine, 'before_cursor_execute', capture)
    improved = {**state, 'hint': [1]}
    try:
        assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, improved))
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    updates = [statement for statement in statements if statement.startswith('update jobs')]
    assert updates and all('resume_checkpoint' in statement and 'resume_state' not in statement for statement in updates)
    assert not any('resume_state' in statement or 'input_snapshot' in statement for statement in statements)
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.resume_state['encoding'] == 'gzip'
        assert decompress_state(row.resume_state) == {key: state[key] for key in ('key', 'model', 'prepared')}
        assert saved_state(row) == improved


def test_changed_preparation_key_replaces_model_and_checkpoint_together(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    first = {'key': 'old-key', 'model': 'old-model', 'prepared': {}, 'hint': [0]}
    replacement = {'key': 'new-key', 'model': 'new-model', 'prepared': {'data': 1}, 'hint': [1]}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, first))
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, replacement))
    with transaction(engine) as db:
        assert saved_state(db.get(m.Job, job_id)) == replacement


def test_native_worker_compresses_preparation_once_outside_write_transactions(h, monkeypatch):
    from packages.domain import solver_resume
    job_id = generation_job(h)
    engine = h.app.state.engine
    local = threading.local()
    writes = worker.retry_write
    compress = solver_resume.compress_state
    checkpoint_job = worker.checkpoint_job
    calls = []

    def tracked_write(engine, operation, *args, **kwargs):
        local.writing = True
        try:
            return writes(engine, operation, *args, **kwargs)
        finally:
            local.writing = False

    def compressed(state):
        assert not getattr(local, 'writing', False)
        calls.append(state['key'])
        return compress(state)

    def checked_checkpoint(db, claim, state, **kwargs):
        assert 'model' not in state and 'prepared' not in state
        return checkpoint_job(db, claim, state, **kwargs)

    def solving(snapshot, *, checkpoint, **kwargs):
        state = {'key': 'compressed-once', 'model': 'x' * 200_000, 'prepared': {'data': [1, 2]}, 'hint': []}
        checkpoint(state)
        checkpoint({**state, 'hint': [1], 'search_elapsed_seconds': 2})
        return {'status': 'needs_more_time', 'additions': [], 'removals': []}

    monkeypatch.setattr(worker, 'retry_write', tracked_write)
    monkeypatch.setattr(worker, 'checkpoint_job', checked_checkpoint)
    monkeypatch.setattr(solver_resume, 'compress_state', compressed)
    monkeypatch.setattr(cp_sat, 'generate', solving)
    assert worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    assert calls == ['compressed-once']
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert len(row.resume_state['data']) < 5000
        assert saved_state(row)['hint'] == [1]
        assert saved_state(row)['model'] == 'x' * 200_000


def test_completed_proposal_clears_private_resume_state_atomically(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    state = {'key': 'prepared', 'model': 'data', 'prepared': {}, 'hint': [1], 'result': proposal}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'running' and row.result == proposal and row.resume_state is not None
        assert 'result' not in row.resume_checkpoint
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, proposal, None))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.result == proposal
        assert row.resume_state is None and row.resume_checkpoint is None


def test_finish_keeps_new_result_when_prior_result_is_not_checkpointed(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    prior = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'],
             'additions': [], 'removals': []}
    with transaction(engine, write=True) as db:
        db.get(m.Job, job_id).result = prior
    new = {'status': 'requirements_conflict'}
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, new, None))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.result == new


def test_generation_result_metadata_retains_immutable_manifest(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'],
                'additions': [], 'removals': []}
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, proposal, None))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.result == proposal
        manifest = row.read_metadata['result']['generation_manifest']
        assert manifest['job']['id'] == job_id
        assert manifest['baseline']['hash'] == row.input_snapshot['baseline']['hash']
        assert manifest['result']['status'] == proposal['status']


@pytest.mark.parametrize('invalidate', ['new_run', 'inputs', 'ttl'])
def test_late_checkpoint_cannot_resurrect_invalidated_preparation(h, invalidate):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    state = {'key': 'kept', 'model': 'prepared', 'hint': []}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    workspace = h.detail(claim['input']['workspace']['id'])['workspace']
    if invalidate == 'new_run':
        assert retry_write(engine, lambda db: worker.finish_job(db, claim, {'status': 'needs_more_time'}, None))
        newest = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                           revision=workspace['revision'])['job']
        with transaction(engine) as db:
            assert db.get(m.Job, newest['id']).created_at > db.get(m.Job, job_id).created_at
        new_claim = retry_write(engine, worker.claim_job)
        assert new_claim['id'] == newest['id']
        assert retry_write(engine, lambda db: worker.checkpoint_job(db, new_claim, {'key': 'newest', 'model': 'data', 'hint': []}))
    elif invalidate == 'inputs':
        h.shift(workspace, people=[])
    else:
        with transaction(engine, write=True) as db:
            db.get(m.Job, job_id).created_at = m.now() - timedelta(days=8)
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state)) == (invalidate == 'inputs')
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        if invalidate == 'inputs':
            assert row.resume_state is not None and row.resume_checkpoint is not None
        else:
            assert row.resume_state is None and row.resume_checkpoint is None
        if invalidate == 'new_run':
            assert list(db.scalars(select(m.Job.id).where(m.Job.workspace_id == row.workspace_id,
                m.Job.resume_state.is_not(None)))) == [newest['id']]


@pytest.mark.parametrize('cache_name,sweep_name', [
    ('_pruned', 'prune_security_state'),
    ('_feedback_pruned', 'prune_feedback_screenshots'),
    ('_bulk_staged_pruned', 'prune_bulk_staged'),
    ('_generation_pruned', 'prune_generation_state'),
])
def test_housekeeping_throttle_follows_live_engines_not_recycled_ids(h, monkeypatch, cache_name, sweep_name):
    first, second = h.app.state.engine, make_engine(h.settings.database_url)
    elapsed, statements = [1000.0], []
    monkeypatch.setattr(worker, cache_name, type(getattr(worker, cache_name))())
    monkeypatch.setattr(worker, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    # Reproduce a recycled engine-ID slot without depending on allocator or collection order.
    monkeypatch.setattr(worker, 'id', lambda engine: 1, raising=False)
    sweep = getattr(worker, sweep_name)
    kwargs = {'settings': h.settings} if cache_name in {'_pruned', '_generation_pruned'} else {}

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(first, 'before_cursor_execute', capture)
    event.listen(second, 'before_cursor_execute', capture)
    try:
        sweep(first, **kwargs)
        assert statements
        statements.clear()
        assert sweep(first, **kwargs) == 0
        assert not statements
        sweep(second, **kwargs)
        assert statements, 'A different engine must run its own first sweep'
        statements.clear()
        elapsed[0] += 600
        sweep(first, **kwargs)
        assert statements, 'The same engine must resume sweeping after ten minutes'
    finally:
        event.remove(first, 'before_cursor_execute', capture)
        event.remove(second, 'before_cursor_execute', capture)
        second.dispose()
    released = ref(second)
    del second
    gc.collect()
    assert released() is None, 'Throttle state must not retain a discarded engine'


@pytest.mark.parametrize('instant', [
    '2026-10-03T20:59:59.999999+00:00', '2026-10-03T21:00:00+00:00',
    '2026-10-03T23:59:59.999999+00:00', '2026-10-04T00:00:00+00:00',
])
def test_resume_sweep_uses_configured_ttl_without_deleting_results(h, monkeypatch, instant):
    monkeypatch.setattr(m, 'clock_now', lambda: datetime.fromisoformat(instant))
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    state = {'key': 'kept', 'model': 'prepared', 'hint': []}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, state))
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, {'status': 'needs_more_time'}, None))
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job_id)
        row.created_at = row.completed_at = m.now() - timedelta(days=2)
    assert worker.prune_generation_state(engine, settings=replace(h.settings, generation_resume_days=1)) == 1
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.result == {'status': 'needs_more_time'}
        assert row.resume_state is None and row.resume_checkpoint is None


def test_crash_after_final_checkpoint_preserves_proposal_across_ttl_and_input_invalidation(h):
    from apps.api.services.generation_resume import clear_generation_resume
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'prepared', 'model': 'data', 'hint': [1], 'result': proposal}))
    with transaction(engine, write=True) as db:
        row = db.get(m.Job, job_id)
        row.created_at = m.now() - timedelta(days=8)
        db.get(m.Workspace, row.workspace_id).content_revision += 1
        clear_generation_resume(db, job_id=job_id)
        expire(db, job_id)
    worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.result == proposal
        assert row.resume_state is None and row.resume_checkpoint is None


@pytest.mark.parametrize('has_solution', [False, True])
def test_worker_restart_preserves_spent_search_budget_and_validates_saved_hint(h, monkeypatch, has_solution):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, {'stage': 'improving', 'search_elapsed_seconds': 290}))
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim, {
        'key': 'prepared', 'model': 'data', 'hint': [1] if has_solution else [], 'has_solution': has_solution,
        'search_elapsed_seconds': 300}))
    assert retry_write(engine, lambda db: worker.heartbeat_job(db, claim, {'stage': 'improving', 'search_elapsed_seconds': 295}))
    retry_write(engine, lambda db: expire(db, job_id))
    called = []

    def resumed(snapshot, *, resume_state, search_control, **kwargs):
        called.append(True)
        assert has_solution and decompress_state(resume_state)['hint'] == [1]
        assert search_control()['spent_seconds'] == 300
        return {'status': 'candidate_feasible', 'input_fingerprint': snapshot['fingerprint'], 'additions': [], 'removals': []}

    monkeypatch.setattr(cp_sat, 'generate', resumed)
    worker.process_one(engine, settings=replace(h.settings, generation_engine='cp_sat'))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.search_control['spent_seconds'] == 300
        assert row.result['status'] == ('candidate_feasible' if has_solution else 'needs_more_time')
        assert row.attempts == (2 if has_solution else 1)
    assert bool(called) == has_solution


@pytest.mark.parametrize('failure', ['permission', 'exception'])
def test_finish_failure_preserves_already_validated_proposal_evidence(h, failure):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'prepared', 'model': 'data', 'hint': [1], 'result': proposal}))
    if failure == 'permission':
        with transaction(engine, write=True) as db:
            db.get(m.Identity, claim['requested_by']).disabled = True
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, None, 'RuntimeError' if failure == 'exception' else None))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'failed' and row.result == proposal
        assert row.error_code == ('ACCESS_REVOKED' if failure == 'permission' else 'RuntimeError')
        assert row.resume_state is None and row.resume_checkpoint is None


@pytest.mark.parametrize(('error', 'stop_reason', 'kept'), [
    ('RuntimeError', None, False),
    (None, 'operator_stop', True),
])
def test_checkpointed_finish_retains_only_non_error_stops(h, error, stop_reason, kept):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'checkpointed': True,
                'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': [],
                **({'stop_reason': stop_reason} if stop_reason else {})}
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'prepared', 'model': 'data', 'hint': [1], 'has_solution': True, 'result': proposal}))
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, proposal, error))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert (row.resume_state is not None and row.resume_checkpoint is not None) is kept
        assert ('resume_available' in (row.result or {})) is kept


@pytest.mark.parametrize('invalidate', ['inputs', 'permission'])
def test_finish_keeps_incoming_validated_proposal_without_a_prior_final_checkpoint(h, invalidate):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    with transaction(engine, write=True) as db:
        if invalidate == 'inputs':
            db.get(m.Workspace, claim['input']['workspace']['id']).content_revision += 1
        else:
            db.get(m.Identity, claim['requested_by']).disabled = True
    assert retry_write(engine, lambda db: worker.finish_job(db, claim, proposal, None))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.result == proposal
        assert row.status == ('succeeded' if invalidate == 'inputs' else 'failed')
        assert row.error_code == (None if invalidate == 'inputs' else 'ACCESS_REVOKED')
        assert row.resume_state is None and row.resume_checkpoint is None


def test_final_checkpoint_keeps_validated_proposal_after_input_invalidation(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    with transaction(engine, write=True) as db:
        db.get(m.Workspace, claim['input']['workspace']['id']).content_revision += 1
    assert retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'prepared', 'model': 'data', 'hint': [1], 'result': proposal}))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'running' and row.result == proposal
        assert row.resume_state is not None and row.resume_checkpoint is not None
    retry_write(engine, lambda db: expire(db, job_id))
    assert retry_write(engine, worker.claim_job) is None
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'succeeded' and row.result == proposal


def test_revoked_final_checkpoint_preserves_proposal_if_worker_dies_before_finishing(h):
    job_id = generation_job(h)
    engine = h.app.state.engine
    claim = retry_write(engine, worker.claim_job)
    proposal = {'status': 'candidate_feasible', 'input_fingerprint': claim['input']['fingerprint'], 'additions': [], 'removals': []}
    with transaction(engine, write=True) as db:
        db.get(m.Identity, claim['requested_by']).disabled = True
    assert not retry_write(engine, lambda db: worker.checkpoint_job(db, claim,
        {'key': 'prepared', 'model': 'data', 'hint': [1], 'result': proposal}))
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.result == proposal and row.status == 'running'
        assert row.resume_state is None and row.resume_checkpoint is None
    retry_write(engine, lambda db: expire(db, job_id))
    assert retry_write(engine, worker.claim_job) is None
    with transaction(engine) as db:
        row = db.get(m.Job, job_id)
        assert row.status == 'failed' and row.error_code == 'ACCESS_REVOKED'
        assert row.result == proposal
