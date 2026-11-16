"""Retaining old runs never expands the jobs panel beyond its existing history bound."""
from datetime import datetime, timedelta, timezone
import re

import pytest
from sqlalchemy import event, func, insert, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.conditional_reads import revision_etag
from apps.api.version import build
from apps.worker.main import process_one


@pytest.fixture(params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def jobs_backend(request):
    return request.getfixturevalue(request.param)


def test_jobs_history_keeps_the_latest_hundred_runs_without_loading_proposals(h):
    workspace = h.workspace()
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{
            'id': f'retained-run-{index:03}', 'department_id': h.seed['department_id'],
            'workspace_id': workspace['id'], 'requested_by': h.seed['identities']['coordinator'],
            'kind': 'generation', 'status': 'succeeded', 'input_fingerprint': 'retained',
            'created_at': first + timedelta(minutes=index),
            'input_snapshot': {'generation_engine': 'cp_sat'},
            'result': {'status': 'candidate_feasible', 'candidate_evidence': [
                {'person_id': 'synthetic', 'exclusions': []}]},
        } for index in range(105)])
    h.client('coordinator')
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if 'FROM jobs' in statement:
            statements.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', capture)
    try:
        response = h.client('coordinator').get('/api/jobs?workspace_id=' + workspace['id'])
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture)
    assert response.status_code == 200
    jobs = response.json()['jobs']
    assert [job['id'] for job in jobs] == [f'retained-run-{index:03}' for index in range(104, 4, -1)]
    expected = {'status': 'candidate_feasible', 'generation_outcome': {
        'status': 'UNKNOWN', 'candidate': 'none', 'strict_status': None, 'optimality_proven': False}}
    assert all(job['outcome'] == 'complete' and job['result'] == expected for job in jobs)
    job_reads = [statement for statement in statements if 'FROM jobs' in statement]
    assert len(job_reads) <= 2  # measured 2 on the 200 path: one stamp and one page statement
    assert not any(re.search(r'jobs\.(?:input_snapshot|result|result_summary)\b', statement) for statement in job_reads)

    with transaction(h.app.state.engine) as db:
        department = db.get(m.Department, h.seed['department_id'])
        organisation = db.get(m.Organisation, department.organisation_id)
        stamp = db.execute(select(func.count(m.Job.id), func.coalesce(func.sum(m.Job.revision), 0),
            func.max(m.Job.revision), func.max(m.Job.created_at)).where(
                m.Job.department_id == h.seed['department_id'])).one()
    me = h.get('coordinator', '/api/me')
    expected_tag = revision_etag('jobs-list', h.seed['department_id'], h.seed['identities']['coordinator'],
        me['person_id'], me['roles'], workspace['id'], None, 100, 0,
        (department.revision, organisation.revision, organisation.input_revision,
         organisation.availability_revision, h.settings.generation_engine, build()['release'], tuple(stamp)))
    assert response.headers['etag'] == expected_tag

    cached_statements = []
    def capture_cached(_connection, _cursor, statement, _parameters, _context, _many):
        if 'FROM jobs' in statement:
            cached_statements.append(statement)

    event.listen(h.app.state.engine, 'before_cursor_execute', capture_cached)
    try:
        cached = h.client('coordinator').get('/api/jobs?workspace_id=' + workspace['id'],
            headers={'If-None-Match': response.headers['etag']})
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', capture_cached)
    assert cached.status_code == 304
    assert len(cached_statements) == 1
    assert not re.search(r'\bSELECT\s+(?:DISTINCT\s+)?jobs\.', cached_statements[0], re.I)
    assert cached.headers['etag'] == response.headers['etag']


def test_jobs_list_etag_tracks_worker_create_and_finish(jobs_backend):
    """A 304 is the only empty list body, and worker writes move the workspace tag."""
    h = jobs_backend
    workspace = h.shift(h.workspace(), people=[])['workspace']
    client = h.client('coordinator')
    path = '/api/jobs?workspace_id=' + workspace['id']

    empty = client.get(path)
    assert empty.status_code == 200 and empty.json()['jobs'] == []

    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'generation'},
                    revision=workspace['revision'])['job']
    fresh = client.get(path)
    assert fresh.status_code == 200 and fresh.headers['etag'] != empty.headers['etag']
    listed = next(row for row in fresh.json()['jobs'] if row['id'] == job['id'])
    assert listed['status'] == 'queued'

    assert process_one(h.app.state.engine)
    changed = client.get(path, headers={'If-None-Match': fresh.headers['etag']})
    assert changed.status_code == 200 and changed.headers['etag'] != fresh.headers['etag']
    listed = next(row for row in changed.json()['jobs'] if row['id'] == job['id'])
    detail_response = client.get('/api/jobs/' + job['id'])
    assert detail_response.status_code == 200
    detail = detail_response.json()['job']
    assert {key: listed.get(key) for key in ('id', 'workspace_id', 'kind', 'status', 'revision', 'planning_context')} == {
        key: detail.get(key) for key in ('id', 'workspace_id', 'kind', 'status', 'revision', 'planning_context')}
    assert listed['status'] == detail['status'] == 'succeeded'

    unchanged = client.get(path, headers={'If-None-Match': changed.headers['etag']})
    assert (unchanged.status_code, unchanged.content, unchanged.headers['etag']) == (304, b'', changed.headers['etag'])


def test_jobs_can_be_read_in_one_state_for_operations(h):
    """SW3-D-24: Operations opens the jobs behind its failed count (newest first); an unknown state is refused."""
    workspace = h.workspace()
    first = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(insert(m.Job), [{
            'id': f'state-run-{index}', 'department_id': h.seed['department_id'], 'workspace_id': workspace['id'],
            'requested_by': h.seed['identities']['coordinator'], 'kind': 'generation', 'input_fingerprint': 'state',
            'status': 'failed' if index % 2 else 'succeeded', 'created_at': first + timedelta(minutes=index),
            'input_snapshot': {'generation_engine': 'cp_sat'}} for index in range(6)])
    failed = h.get('coordinator', '/api/jobs?status=failed')['jobs']
    assert [job['id'] for job in failed] == ['state-run-5', 'state-run-3', 'state-run-1']
    assert h.get('coordinator', '/api/jobs?status=exploded', status=422)['error']['code'] == 'SCHEMA_INVALID'
