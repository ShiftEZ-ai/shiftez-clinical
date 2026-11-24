"""Result retries never reload department-sized frozen input or prior evidence."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import re
import pytest

from sqlalchemy import event, select, text

from apps.api import models as m
from apps.api.db import WRITE_LOCK_KEY, transaction, retry_write
from apps.api.serialization import job_read_metadata, job_result_projection
from apps.api.services.job_reads import full_job_result
from apps.worker import main as worker
from apps.worker.main import finish_job
from packages.domain import feasibility
from tests.test_backend_scale import measure
from tests.test_performance_budget import python_budget

pytestmark = pytest.mark.postgres


def staged_finish(engine, claim, snapshot, result):
    retry_write(engine, lambda db: worker.stage_feasibility_result(db, claim, result), serialize=False)
    projection = job_read_metadata(snapshot, result), job_result_projection('feasibility', result)
    return lambda db: finish_job(db, claim, None, None, feasibility_projection=projection)


def test_department_feasibility_finish_has_bounded_reads(pg, request):
    workspace = pg.workspace()
    # PC13 retained 53,727 candidates per phase, 34 MB of input and 60 MB of output.
    rows = [{'person_id': f'{i % 163:036d}', 'shift_id': f'{i // 163:036d}',
             'start': '2027-06-01T08:00:00+00:00', 'end': '2027-06-01T12:00:00+00:00',
             'eligible_and_available': False,
             'boundary': 'Other assignments, rest, workload and supervision are not checked here.',
             'exclusions': [{'code': 'RESTRICTION', 'rule_id': f'{(i + k) % 1000:036d}', 'rule_revision': 1}
                            for k in range(3)]} for i in range(53727)]
    check = {'status': 'requirements_conflict', 'candidate_evidence': rows, 'diagnostics': []}
    result = {'status': 'requirements_conflict', 'checks': {'baseline': check, 'proposed': check}}
    with transaction(pg.app.state.engine, write=True) as db:
        job = m.Job(department_id=pg.seed['department_id'], workspace_id=workspace['id'],
            requested_by=pg.seed['identities']['coordinator'], kind='feasibility', status='running',
            attempts=1, worker_id=worker.WORKER_ID, input_fingerprint='frozen', input_snapshot={'frozen': rows})
        db.add(job)
        db.flush()
        claim = {'id': job.id, 'attempt': 1, 'input_fingerprint': job.input_fingerprint}
    finish = staged_finish(pg.app.state.engine, claim, {'frozen': rows}, result)
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(pg.app.state.engine, 'before_cursor_execute', capture)
    try:
        accepted, timing = measure(pg, 'feasibility-finish', lambda: retry_write(pg.app.state.engine, finish))
    finally:
        event.remove(pg.app.state.engine, 'before_cursor_execute', capture)
    request.node.user_properties.append(('feasibility_finish', json.dumps(timing)))
    assert accepted
    with transaction(pg.app.state.engine) as db:
        stored = db.get(m.Job, claim['id'])
        assert stored.status == 'succeeded' and full_job_result(db, stored) == result
        assert stored.input_snapshot == {'frozen': rows}
    job_reads = [sql for sql in statements if sql.lstrip().startswith('SELECT') and 'FROM jobs' in sql]
    assert job_reads and not any(re.search(r'jobs\.(?:input_snapshot|result)\b', sql) for sql in job_reads)
    assert sum(timing['sql'].values()) <= 15, timing
    return locals()


@pytest.mark.parametrize('retained_result', [False, True])
def test_feasibility_finish_without_manifest_never_selects_frozen_payloads(pg, retained_result):
    workspace = pg.workspace()
    snapshot = {'frozen': ['retained input'] * 2000}
    previous = {'status': 'unknown_time_limit', 'frozen': ['retained result'] * 2000} if retained_result else None
    result = {'status': 'feasible', 'checks': {}}
    with transaction(pg.app.state.engine, write=True) as db:
        job = m.Job(department_id=pg.seed['department_id'], workspace_id=workspace['id'],
            requested_by=pg.seed['identities']['coordinator'], kind='feasibility', status='running',
            attempts=1, worker_id=worker.WORKER_ID, input_fingerprint='frozen', input_snapshot=snapshot, result=previous)
        db.add(job)
        db.flush()
        claim = {'id': job.id, 'attempt': 1, 'input_fingerprint': job.input_fingerprint}
        assert job.read_metadata['input_snapshot']['run_context'] is None
        assert 'generation_manifest' not in (job.read_metadata['result'] or {})
    finish = staged_finish(pg.app.state.engine, claim, snapshot, result)
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(pg.app.state.engine, 'before_cursor_execute', capture)
    try:
        accepted = retry_write(pg.app.state.engine, finish)
    finally:
        event.remove(pg.app.state.engine, 'before_cursor_execute', capture)
    assert accepted
    job_reads = [sql for sql in statements if sql.lstrip().startswith('SELECT') and 'FROM jobs' in sql]
    assert job_reads and not any(re.search(r'jobs\.(?:input_snapshot|result)\b', sql) for sql in job_reads)
    with transaction(pg.app.state.engine) as db:
        stored = db.get(m.Job, claim['id'])
        assert stored.status == 'succeeded' and full_job_result(db, stored) == result
        assert stored.input_snapshot == snapshot
        assert stored.read_metadata['result'] == {'status': 'feasible'}


@pytest.mark.wall_benchmark
def test_department_feasibility_finish_wall_budget(pg, request):
    # Budget the serialized transaction while the proof is already staged.
    state = test_department_feasibility_finish_has_bounded_reads(pg, request)
    assert state['timing']['wall_seconds'] < python_budget(5), state['timing']


def test_feasibility_payload_write_leaves_the_save_writer_available(pg, monkeypatch, caplog):
    workspace = pg.workspace()
    other = pg.workspace('Independent draft', start='2026-12-06', end='2026-12-13')
    engine = pg.app.state.engine
    with transaction(engine, write=True) as db:
        for job in db.scalars(select(m.Job).where(m.Job.status == 'queued')):
            job.status = 'cancelled'
    pg.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility'},
               revision=workspace['revision'])
    monkeypatch.setattr(feasibility, 'check', lambda *a, **k: {
        'status': 'feasible',
        'checks': {'baseline': {'candidate_evidence': [
            {'person_id': 'candidate', 'shift_id': 'duty', 'exclusions': []}] * 20000}},
    })
    caplog.set_level('INFO', logger='rota.db')
    writing, resume = Event(), Event()
    write_phases = []

    def pause(connection, _cursor, statement, _parameters, _context, _many):
        if ('INSERT INTO job_feasibility_results' in statement
                or 'UPDATE jobs SET' in statement and 'result=' in statement):
            phase = connection.info['rota_write']['context'].get('job_phase')
            write_phases.append(phase)
            if not writing.is_set():
                writing.set()
                assert resume.wait(20)

    event.listen(engine, 'before_cursor_execute', pause)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            run = pool.submit(worker.process_one, engine, settings=pg.settings)
            try:
                assert writing.wait(20)
                # Probe without waiting: on the old path the worker already owns this lock.
                with engine.begin() as db:
                    available = db.scalar(text('SELECT pg_try_advisory_xact_lock(:key)'),
                                          {'key': WRITE_LOCK_KEY})
                assert available, 'The feasibility payload write holds the instance writer'
                assert pg.shift(other, start='2026-12-06T08:00:00+03:00',
                                end='2026-12-06T12:00:00+03:00', people=[])['workspace']
                acquired = next(record for record in caplog.records
                                if record.message == 'write_lock_acquired'
                                and getattr(record, 'command_kind', None) == 'shift.create')
                assert acquired.lock_wait_ms < 1000
            finally:
                resume.set()
            assert run.result(timeout=20)
        assert write_phases == ['stage_result']
    finally:
        event.remove(engine, 'before_cursor_execute', pause)
