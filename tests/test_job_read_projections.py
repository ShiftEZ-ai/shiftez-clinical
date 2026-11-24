"""Large retained results never cross the read boundary before projection/paging."""
import copy
from datetime import timedelta
import json
import re

import pytest
from sqlalchemy import Text, cast, event, insert, null, select, update
from sqlalchemy.orm import defer

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import (effective_input_fingerprint, job_detail_result, job_read_metadata,
                                    job_result_projection, job_result_summary)
from apps.api.services.job_reads import full_job_result
from apps.worker import main as worker
from packages.contracts.schemas import ROLE_NAMES
from packages.domain import feasibility
from tests.governance_support import identity


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def both(request):
    return request.getfixturevalue(request.param)


def test_job_metadata_preserves_explicit_null_engine_without_null_rule_bindings():
    # run_context is always recorded (None when it cannot be recovered), so a detail read never reloads the frozen input.
    assert job_read_metadata({'generation_engine': None, 'bindings': None, 'compiler_version': None}, None) == {
        'input_snapshot': {'generation_engine': None, 'run_context': None}, 'result': None}


def test_effective_fingerprint_does_not_touch_unloaded_fallback(h):
    workspace = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), {'id': 'unloaded-fingerprint', 'department_id': h.seed['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'generation', 'status': 'succeeded', 'input_fingerprint': 'frozen',
            'input_snapshot': {'generation_engine': 'cp_sat'}, 'result': None})
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Job).where(m.Job.id == 'unloaded-fingerprint').options(
            defer(m.Job.input_fingerprint, raiseload=True)))
        assert effective_input_fingerprint(row, projection={
            'input_snapshot': {'generation_engine': 'cp_sat'}, 'result': None}) is None


def test_job_projections_preserve_inputs_results_and_full_detail(both):
    h = both
    workspace = h.workspace()
    candidates = [{'person_id': str(index), 'shift_id': 'clinic', 'exclusions': []} for index in range(2000)]
    check = {'status': 'candidate_feasible', 'candidate_evidence': candidates,
             'summary': {'required_places': 2, 'detail': 'retained score detail' * 1000},
             'findings': [{'message': 'duplicate finding'}],
             'validation': {'valid': True, 'findings': []}, 'diagnostics': []}
    raw = {'status': 'feasible', 'checks': {'baseline': check, 'proposed': copy.deepcopy(check)}}
    snapshot = {'compiler_version': 'test-version', 'bindings': [], 'unused': ['frozen source'] * 2000}
    original = copy.deepcopy(raw)
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=workspace['id'],
                    requested_by=h.seed['identities']['coordinator'], kind='feasibility', status='succeeded',
                    input_fingerprint='test-fingerprint', input_snapshot=snapshot, result=raw)
        db.add(job)
        db.flush()
        job_id = job.id
        assert job.read_metadata == job_read_metadata(snapshot, raw)
        assert job.result_summary == job_result_projection('feasibility', raw)

    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        history = h.get('coordinator', '/api/jobs?workspace_id=' + workspace['id'])['jobs']
        detail = h.get('approver', '/api/jobs/' + job_id)['job']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert history[0]['id'] == job_id
    assert detail['result'] == job_detail_result('feasibility', raw)
    # A deterministic I/O budget applies equally to covered/slower CI hosts: zero raw JSON projections.
    job_reads = [sql for sql in statements if 'FROM jobs' in sql]
    assert job_reads and not any(re.search(r'jobs\.(?:input_snapshot|result)\b', sql) for sql in job_reads)
    assert not {'read_metadata', 'result_summary'}.intersection(detail)
    full = h.get('approver', '/api/jobs/' + job_id + '?full=true')['job']
    assert full['result'] == job_result_summary(raw)
    assert len(json.dumps(detail)) < len(json.dumps(full)) / 2
    with transaction(h.app.state.engine, write=True) as db:
        job = db.get(m.Job, job_id)
        assert job.result == original and job.input_snapshot == snapshot
        job.result = {**job.result, 'status': 'inconclusive'}
        job.input_snapshot = {**job.input_snapshot, 'compiler_version': 'new-version'}
    with transaction(h.app.state.engine) as db:
        job = db.get(m.Job, job_id)
        assert job.read_metadata == job_read_metadata(job.input_snapshot, job.result)
        assert job.result_summary == job_result_projection(job.kind, job.result)
    assert h.get('approver', '/api/jobs/' + job_id)['job']['result']['status'] == 'inconclusive'


def test_evidence_sql_page_matches_python_filters_counts_and_reason_deduplication(both):
    h = both
    workspace = h.workspace()
    rows = [{'person_id': str(index), 'shift_id': 'other' if index % 3 == 0 else 'clinic',
             'exclusions': [{'code': 'RESTRICTION'}, {'code': 'RESTRICTION'}, {'code': 'UNAVAILABLE'}]
                           if index % 2 else []} for index in range(2000)]
    diagnostics = [{'shift_id': 'clinic', 'candidates': [
        {'person_id': 'implicit', 'exclusions': []},
        {'person_id': 'explicit', 'shift_id': 'clinic', 'exclusions': [{'code': 'RESTRICTION'}]},
        {'person_id': 'null-shift', 'shift_id': None, 'exclusions': []},
        {'person_id': 'other', 'shift_id': 'other', 'exclusions': []}]}]
    raw = {'checks': {'proposed': {'candidate_evidence': rows, 'diagnostics': diagnostics}}}
    # Bulk insert uses the same projections as worker/API ORM writers.
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{'id': 'paged-evidence', 'department_id': h.seed['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'feasibility', 'status': 'succeeded', 'input_fingerprint': 'frozen',
            'input_snapshot': {}, 'result': raw}])
    selected = [row for row in rows if row['shift_id'] == 'clinic']
    statements = []
    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        page = h.get('approver', '/api/jobs/paged-evidence/evidence?phase=proposed&shift_id=clinic&offset=100&limit=10')
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    # SQLite and PG may scan stored arrays, but the API never decodes full inputs/results before slicing.
    assert not any(re.search(r'(?:SELECT|,)\s*jobs\.(?:input_snapshot|result)\s*(?:,|\nFROM)', sql) for sql in statements)
    assert page['candidates'] == selected[100:110] and len(page['candidates']) == 10
    assert page['total'] == len(selected) and page['eligible'] == sum(not row['exclusions'] for row in selected)
    assert page['reasons'] == {code: sum(bool(row['exclusions']) for row in selected)
                               for code in ('RESTRICTION', 'UNAVAILABLE')}
    diagnostic = h.get('approver', '/api/jobs/paged-evidence/evidence?phase=proposed&diagnostic_index=0&shift_id=clinic')
    assert [row['person_id'] for row in diagnostic['candidates']] == ['implicit', 'explicit']
    assert diagnostic['reasons'] == {'RESTRICTION': 1} and diagnostic['eligible'] == 1
    missing = h.get('approver', '/api/jobs/paged-evidence/evidence?phase=proposed&diagnostic_index=1', status=404)
    assert missing['error']['code'] == 'EVIDENCE_NOT_FOUND'
    empty = h.get('approver', '/api/jobs/paged-evidence/evidence?phase=baseline&offset=2000')
    assert empty['candidates'] == [] and empty['total'] == 0 and empty['reasons'] == {}
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, 'paged-evidence')
        assert stored.result == raw
        assert stored.read_metadata == job_read_metadata({}, raw)


def test_projection_backfill_only_adds_read_fields_and_preserves_raw_evidence(both):
    import importlib
    h = both
    workspace = h.workspace()
    snapshot = {'bindings': [], 'compiler_version': 'frozen', 'unprojected': ['original input']}
    raw = {'checks': {'proposed': {'candidate_evidence': [{'person_id': 'synthetic', 'exclusions': []}],
                                  'summary': {'required_places': 1}, 'findings': []}}}
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{'id': 'backfill-source', 'department_id': h.seed['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'feasibility', 'status': 'succeeded', 'input_fingerprint': 'frozen',
            'input_snapshot': snapshot, 'result': raw}])
        db.execute(update(m.Job).where(m.Job.id == 'backfill-source').values(read_metadata=null(), result_summary=null()))
    projection = select(cast(m.Job.input_snapshot, Text), cast(m.Job.result, Text)).where(m.Job.id == 'backfill-source')
    with h.app.state.engine.begin() as connection:
        before = tuple(connection.execute(projection).one())
        migration = importlib.import_module('migrations.versions.0117_backend_read_indexes')
        migration.backfill(connection)
        assert tuple(connection.execute(projection).one()) == before
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, 'backfill-source')
        assert stored.read_metadata == job_read_metadata(snapshot, raw)
        assert stored.result_summary == job_result_projection('feasibility', raw)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Job).where(m.Job.id == 'backfill-source').values(read_metadata=null(), result_summary=null()))
    page = h.get('approver', '/api/jobs/backfill-source/evidence?phase=proposed')
    assert page['total'] == page['eligible'] == 1 and page['candidates'] == raw['checks']['proposed']['candidate_evidence']
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Job, 'backfill-source').progress = {'stage': 'ready'}
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.Job, 'backfill-source')
        assert stored.read_metadata == job_read_metadata(snapshot, raw)
        assert stored.result_summary == job_result_projection('feasibility', raw)
        assert stored.input_snapshot == snapshot and stored.result == raw


@pytest.mark.parametrize('retained', ['read_metadata', 'result_summary'])
def test_projection_backfill_preserves_populated_fields_and_is_repeatable(both, retained):
    import importlib
    h = both
    workspace = h.workspace()
    snapshot, raw = {'compiler_version': 'retained-input'}, {'status': 'candidate_feasible', 'candidate_evidence': []}
    sentinel = {'retained': 'Existing projection must not be recomputed'}
    missing = 'result_summary' if retained == 'read_metadata' else 'read_metadata'
    with h.app.state.engine.begin() as connection:
        connection.execute(insert(m.Job), {'id': 'partial-backfill', 'department_id': h.seed['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'generation', 'status': 'succeeded', 'input_fingerprint': 'retained',
            'input_snapshot': snapshot, 'result': raw})
        connection.execute(update(m.Job).where(m.Job.id == 'partial-backfill').values({retained: sentinel, missing: null()}))
        preserved = select(cast(m.Job.input_snapshot, Text), cast(m.Job.result, Text),
                           cast(getattr(m.Job, retained), Text)).where(m.Job.id == 'partial-backfill')
        before = tuple(connection.execute(preserved).one())
        migration = importlib.import_module('migrations.versions.0117_backend_read_indexes')
        migration.backfill(connection)
        migration.backfill(connection)
        assert tuple(connection.execute(preserved).one()) == before
        expected = job_result_projection('generation', raw) if missing == 'result_summary' else job_read_metadata(snapshot, raw)
        assert connection.scalar(select(getattr(m.Job, missing)).where(m.Job.id == 'partial-backfill')) == expected


@pytest.mark.postgres
@pytest.mark.parametrize('pending', [False, True])
def test_evidence_counts_and_page_share_one_read_committed_snapshot(pg, pending):
    h = pg
    workspace = h.workspace()
    old = [] if pending else [{'person_id': 'old-' + str(index), 'exclusions': []} for index in range(3)]
    new = [{'person_id': 'new-' + str(index), 'exclusions': [{'code': 'RESTRICTION'}]} for index in range(7)]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Job(id='concurrent-evidence', department_id=h.seed['department_id'], workspace_id=workspace['id'],
            requested_by=h.seed['identities']['coordinator'], kind='generation', status='running',
            input_fingerprint='frozen', input_snapshot={}, result=None if pending else {'candidate_evidence': old}))
    with h.app.state.engine.connect() as connection:
        assert connection.get_isolation_level() == 'READ COMMITTED'
    h.client('coordinator')
    updated = []
    def checkpoint_after_select(_connection, _cursor, statement, _parameters, _context, _many):
        if not updated and 'jobs.result_summary' in statement and statement.lstrip().startswith(('SELECT', 'WITH')):
            updated.append(True)
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Job, 'concurrent-evidence').result = {'candidate_evidence': new}
    event.listen(h.app.state.engine, 'after_cursor_execute', checkpoint_after_select)
    try:
        page = h.get('coordinator', '/api/jobs/concurrent-evidence/evidence?limit=2')
    finally:
        event.remove(h.app.state.engine, 'after_cursor_execute', checkpoint_after_select)
    assert updated
    assert page['total'] == page['eligible'] == len(old) and page['reasons'] == {}
    assert page['candidates'] == old[:2]
    latest = h.get('coordinator', '/api/jobs/concurrent-evidence/evidence?limit=2')
    assert latest['candidates'] == new[:2] and latest['total'] == 7 and latest['eligible'] == 0
    assert latest['reasons'] == {'RESTRICTION': 7}


@pytest.mark.parametrize('change', ['none', 'inputs', 'cancel', 'lost', 'access'])
def test_feasibility_staging_keeps_evidence_private_and_fences_the_attempt(both, monkeypatch, change):
    h = both
    workspace = h.workspace()
    engine = h.app.state.engine
    original_write = worker.retry_write
    with transaction(engine, write=True) as db:
        for row in db.scalars(select(m.Job).where(m.Job.status == 'queued')):
            row.status = 'cancelled'
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility'},
                    revision=workspace['revision'])['job']
    path = '/api/jobs/' + job['id']
    row = {'person_id': h.seed['people'][0], 'shift_id': 'candidate-duty', 'exclusions': []}
    phase = {'status': 'candidate_feasible', 'candidate_evidence': [row],
             'diagnostics': [{'shift_id': row['shift_id'], 'candidates': [row]}]}
    result = {'status': 'feasible', 'checks': {'baseline': phase, 'proposed': phase},
              'full_feasibility_established': True}
    monkeypatch.setattr(feasibility, 'check', lambda *args, **kwargs: result)

    def before_finish(engine, operation, **options):
        if (options.get('context') or {}).get('job_phase') == 'finish':
            with transaction(engine) as db:
                stored = db.get(m.Job, job['id'])
                assert stored.status == 'running' and stored.result is None and stored.result_summary is None
                assert db.get(m.JobFeasibilityResult, (job['id'], 1)).payload == result
            for endpoint in (path, path + '?full=true', path + '/status?events=true'):
                running = h.get('coordinator', endpoint)['job']
                assert running['status'] == 'running' and running['result'] is None
            evidence = h.get('coordinator', path + '/evidence?phase=baseline')
            assert evidence['total'] == 0 and evidence['candidates'] == []
            h.get('coordinator', path + '/evidence?phase=baseline&diagnostic_index=0', status=404)
            if change == 'inputs':
                h.shift(workspace, people=[])
            elif change == 'lost':
                raise SystemExit('Worker stopped after staging')
            elif change == 'cancel':
                cancelled = h.command('coordinator', 'job.cancel', {'reason': 'Cancel the synthetic run'},
                                      target=job['id'])['job']
                assert cancelled['status'] == 'cancelled' and cancelled['result'] is None
            elif change == 'access':
                with transaction(engine, write=True) as db:
                    db.get(m.Identity, h.seed['identities']['coordinator']).disabled = True
        return original_write(engine, operation, **options)

    monkeypatch.setattr(worker, 'retry_write', before_finish)
    if change == 'lost':
        with pytest.raises(SystemExit, match='after staging'):
            worker.process_one(engine, settings=h.settings)
        with transaction(engine, write=True) as db:
            stored = db.get(m.Job, job['id'])
            stored.heartbeat_at = stored.started_at - timedelta(seconds=worker.JOB_LEASE_SECONDS + 1)
        fresh = {**result, 'status': 'inconclusive'}

        def after_reclaim(engine, operation, **options):
            if (options.get('context') or {}).get('job_phase') == 'finish':
                assert not original_write(engine, lambda db: worker.finish_job(db,
                    {'id': job['id'], 'attempt': 1}, result, None))
                assert h.get('coordinator', path + '?full=true')['job']['result'] is None
                assert h.get('coordinator', path + '/evidence?phase=baseline')['candidates'] == []
            return original_write(engine, operation, **options)

        monkeypatch.setattr(worker, 'retry_write', after_reclaim)
        monkeypatch.setattr(feasibility, 'check', lambda *args, **kwargs: fresh)
        assert worker.process_one(engine, settings=h.settings)
        with transaction(engine) as db:
            stored = db.get(m.Job, job['id'])
            assert stored.attempts == 2 and full_job_result(db, stored) == fresh
        assert h.get('coordinator', path)['job']['result']['status'] == 'inconclusive'
    else:
        assert worker.process_one(engine, settings=h.settings)

    published = change in {'none', 'inputs', 'lost'}
    with transaction(engine, write=True) as db:
        stored = db.get(m.Job, job['id'])
        assert stored.status == ('succeeded' if published else 'cancelled' if change == 'cancel' else 'failed')
        assert stored.error_code == ('ACCESS_REVOKED' if change == 'access' else None)
        worker.prune_abandoned_feasibility_results(db, limit=10)
        assert (db.get(m.JobFeasibilityResult, (job['id'], 1)) is not None) == (change in {'none', 'inputs'})
        assert (full_job_result(db, stored) is not None) == published
    if change in {'none', 'inputs'}:
        detail = h.get('coordinator', path)['job']
        assert detail['check_stale'] is (change == 'inputs')
        assert detail['result']['full_feasibility_established']
        assert h.get('coordinator', path + '/evidence?phase=baseline')['candidates'] == [row]
    if change == 'none':
        endpoints = (path, path + '?full=true', path + '/status?events=true',
                     path + '/evidence?phase=baseline', path + '/evidence?phase=proposed&diagnostic_index=0',
                     '/api/jobs?workspace_id=' + workspace['id'])
        for role in ROLE_NAMES:
            if role not in h.seed['identities']:
                identity(h, role, [role])

        def read_responses():
            responses = {}
            for role in ROLE_NAMES:
                for endpoint in endpoints:
                    response = h.client(role).get(endpoint)
                    payload = response.content if response.is_success else {
                        key: value for key, value in response.json().items() if key != 'correlation_id'}
                    responses[role, endpoint] = response.status_code, payload, response.headers.get('etag')
            return responses

        staged_responses = read_responses()
        with transaction(engine, write=True) as db:
            stored = db.get(m.Job, job['id'])
            db.delete(db.get(m.JobFeasibilityResult, (job['id'], 1)))
            stored.result = result
        assert read_responses() == staged_responses
