"""maintenance: cold reads, after-edit currency, and final-write preparation boundaries."""
import os
import subprocess
import sys

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import defer

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import planning, rota_summary
from apps.api.services.read_projection_warming import warm_all, warm_one
from tests.clock_support import freeze_clock
from tests.test_fix_list_performance import backend


def open_session(h):
    typ = h.configure('assignment_types', {'name': 'Editable open session', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}],
                       'fields': [{'key': 'note', 'label': 'Note', 'kind': 'text'}]}})['record']
    return h.shift(h.workspace(), people=[], typ=typ['id'])


def test_worker_prewarms_canonical_reads_for_all_visible_audiences(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    w = h.shift(h.workspace(), people=[])['workspace']
    h.policy({'kind': 'minimum_cover', 'minimum': 2})
    assert warm_one(h.app.state.engine)
    assert not warm_one(h.app.state.engine)
    paths = ['/api/workspaces', '/api/home', '/api/home/rotas?start=2026-10-01&end=2026-11-01',
             f'/api/workspaces/{w["id"]}/fix-list']
    before = {(role, path): h.get(role, path) for role in ('coordinator', 'approver') for path in paths}
    def forbidden(*a, **kw):
        pytest.fail('A prewarmed read rebuilt canonical scheduling work')
    monkeypatch.setattr(snapshots, 'domain_snapshot', forbidden)
    monkeypatch.setattr(rota_summary, 'domain_snapshot', forbidden)
    monkeypatch.setattr(rota_summary, 'evaluate', forbidden)
    for (role, path), expected in before.items():
        assert h.get(role, path) == expected


def test_compact_check_updates_when_validation_has_no_revision_change(backend):
    from apps.api.services.workspace_reads import stored_checks, check_projection
    h = backend
    w = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, w['id'])
        revision = row.revision
        row.validation = {**row.validation, 'evaluated_at': '2026-10-05T01:00:00Z'}
    with transaction(h.app.state.engine) as db:
        row = db.scalar(select(m.Workspace).options(defer(m.Workspace.validation)).where(m.Workspace.id == w['id']))
        assert row.revision == revision
        projected = stored_checks(db, [row])[row.id]
        assert projected['evaluated_at'] == '2026-10-05T01:00:00Z'
        assert 'validation' not in row.__dict__
        assert projected == check_projection(row, row.validation)


def test_after_one_shift_edit_first_read_needs_no_snapshot_or_evaluation(backend, monkeypatch):
    h = backend
    freeze_clock(monkeypatch)
    # Keep an existing gap; assignment edits may not introduce a new mandatory finding.
    saved = open_session(h)
    w, shift = saved['workspace'], saved['shifts'][0]
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'], participants=[])
    payload['attributes'] = {'note': 'Updated open session'}
    changed = h.command('coordinator', 'shift.update', payload, revision=w['revision'])
    expected = planning.health(type('Workspace', (), {'validation': changed['validation'], 'accepted_issues': []})())
    def forbidden(*a, **kw):
        pytest.fail('An ordinary edit left canonical read work for its first reader')
    monkeypatch.setattr(snapshots, 'domain_snapshot', forbidden)
    monkeypatch.setattr(rota_summary, 'domain_snapshot', forbidden)
    monkeypatch.setattr(rota_summary, 'evaluate', forbidden)
    listed = h.get('coordinator', '/api/workspaces')['workspaces'][0]
    assert listed['health'] == {**expected, 'fresh': True}
    assert h.get('coordinator', f'/api/workspaces/{w["id"]}/fix-list')['summary']['must']
    h.get('coordinator', '/api/home')


def test_preparation_reuse_still_reacquires_under_final_writer_lock(h, monkeypatch):
    saved = open_session(h)
    w, shift = saved['workspace'], saved['shifts'][0]
    calls, stamps, original = [], {False: [], True: []}, snapshots.preparation_stamp
    def counted(db, *a, **kw):
        value = original(db, *a, **kw)
        writer = bool(db.connection().info.get('rota_write'))
        calls.append(writer)
        stamps[writer].append(value)
        return value
    monkeypatch.setattr(snapshots, 'preparation_stamp', counted)
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'], participants=[])
    payload['attributes'] = {'note': 'Updated open session'}
    h.command('coordinator', 'shift.update', payload, revision=w['revision'])
    assert False in calls and True in calls
    # The detached graph replaces full reacquisition; its complete watermark stays authoritative in both transactions.
    assert calls.count(False) <= 2 and calls.count(True) <= 2, calls
    assert stamps[False] == stamps[True]


def test_input_change_during_preparation_refuses_without_saving(h, monkeypatch):
    saved = open_session(h)
    w, shift = saved['workspace'], saved['shifts'][0]
    original, changed = planning.evaluate, []
    def evaluate(*a, **kw):
        result = original(*a, **kw)
        if not changed:
            changed.append(True)
            with transaction(h.app.state.engine, write=True) as db:
                db.get(m.Location, shift['location_id']).capacity += 1
        return result
    monkeypatch.setattr(planning, 'evaluate', evaluate)
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=w['id'], shift_id=shift['id'], participants=[])
    payload['attributes'] = {'note': 'Updated open session'}
    reply = h.command('coordinator', 'shift.update', payload, revision=w['revision'], status=409)
    assert reply['error']['code'] == 'STALE_INPUTS'
    assert h.detail(w)['workspace']['revision'] == w['revision']


@pytest.mark.parametrize('changes', [0, 1000])
def test_status_reads_are_metadata_only_and_full_result_stays_available(backend, changes):
    from packages.domain.generation_outcome import generation_outcome
    h = backend
    w = h.shift(h.workspace())['workspace']
    result = {'status': 'candidate_feasible', 'additions': [{'shift_id': 's', 'participant': {'person_id': str(i)}} for i in range(changes)],
              'validation': {'valid': True, 'error_count': 0, 'warning_count': 0, 'findings': []},
              'reconciliation': {'status': 'current', 'validation': {'findings': [{'detail': str(i)} for i in range(1000)]}}}
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=w['id'], kind='generation', status='succeeded',
                    requested_by=h.seed['identities']['coordinator'], input_fingerprint='old',
                    input_snapshot={'generation_engine': 'cp_sat'}, result=result)
        db.add(job)
        db.flush()
        jid = job.id
    statements = []
    def sql(_c, _cu, statement, _p, _ctx, _m):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        status = h.get('coordinator', f'/api/jobs/{jid}/status')['job']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert status['status'] == 'succeeded' and status['result'] == {
        'status': 'candidate_feasible', 'change_count': changes,
        'generation_outcome': generation_outcome(result),
        'validation': {'valid': True, 'error_count': 0, 'warning_count': 0}}
    history = h.get('coordinator', f'/api/jobs?workspace_id={w["id"]}')['jobs']
    assert next(row for row in history if row['id'] == jid)['result']['change_count'] == changes
    job_reads = [s for s in statements if 'FROM jobs' in s]
    assert len(job_reads) <= 2  # One scoped job row and one indexed scalar projection, independent of evidence size.
    assert all('jobs.result,' not in s and 'jobs.input_snapshot,' not in s for s in job_reads)
    assert 'jobs.read_metadata' not in job_reads[0]  # The other SELECT extracts scalar JSON paths.
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(m.Job.__table__.update().where(m.Job.id == jid).values(read_metadata=None))
    statements.clear()
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        legacy = h.get('coordinator', f'/api/jobs/{jid}/status')['job']
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert legacy['result'] is None and legacy['outcome'] is None
    assert all('jobs.result,' not in s and 'jobs.input_snapshot,' not in s for s in statements if 'FROM jobs' in s)
    assert h.get('coordinator', f'/api/jobs/{jid}')['job']['result']['additions'] == result['additions']
    h.get('approver', f'/api/jobs/{jid}/status', status=403)


def test_deployment_warmer_starts_before_partial_worker_cursor_and_bounds_discovery(backend, monkeypatch):
    h = backend
    workspaces = [h.workspace('Warm cycle ' + str(i)) for i in range(3)]
    assert warm_all(h.app.state.engine) == 3
    with transaction(h.app.state.engine) as db:
        cursor = db.get(m.ReadProjectionWarmCursor, 'workspaces')
        if cursor is None:
            cursor = m.ReadProjectionWarmCursor(key='workspaces')
            db.add(cursor)
        cursor.after_id = max(w['id'] for w in workspaces)
        C = m.WorkspaceReadProjection
        db.execute(C.__table__.delete().where(C.kind == 'warm'))
    assert warm_all(h.app.state.engine) == 3
    with transaction(h.app.state.engine) as db:
        assert set(db.scalars(select(m.WorkspaceReadProjection.workspace_id).where(
            m.WorkspaceReadProjection.kind == 'warm'))) == {w['id'] for w in workspaces}
    def forbidden(*a, **kw):
        pytest.fail('An unchanged warming pass performed scheduling work')
    monkeypatch.setattr(snapshots, 'domain_snapshot', forbidden)
    monkeypatch.setattr(rota_summary, 'evaluate', forbidden)
    statements = []
    def sql(_c, _cu, statement, _p, _ctx, _m):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        assert not warm_one(h.app.state.engine)
        assert not warm_one(h.app.state.engine)
        assert not warm_one(h.app.state.engine)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    discovery = [s for s in statements if 'FROM workspaces' in s]
    assert len(discovery) == 3 and all('LIMIT' in s for s in discovery)
    assert not any(s.lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'} for s in statements)


def test_standalone_writer_updates_compact_check_without_api_startup(backend):
    h = backend
    w = h.shift(h.workspace())['workspace']
    script = """
import os
from sqlalchemy import select
from apps.api import models as m
from apps.api.db import make_engine, transaction
engine = make_engine(os.environ['BE17_STANDALONE_URL'])
with transaction(engine, write=True) as db:
    row = db.get(m.Workspace, os.environ['BE17_STANDALONE_WORKSPACE'])
    row.validation = {**row.validation, 'evaluated_at': '2026-10-05T02:00:00Z'}
with transaction(engine) as db:
    row = db.scalar(select(m.WorkspaceReadProjection).where(
        m.WorkspaceReadProjection.workspace_id == os.environ['BE17_STANDALONE_WORKSPACE'],
        m.WorkspaceReadProjection.kind == 'stored_check'))
    assert row.value['evaluated_at'] == '2026-10-05T02:00:00Z'
engine.dispose()
"""
    # Credentials remain in the child environment, never command arguments or captured output.
    env = dict(os.environ, BE17_STANDALONE_URL=h.settings.database_url, BE17_STANDALONE_WORKSPACE=w['id'],
               ROTA_SQLITE_SYNCHRONOUS=h.settings.sqlite_synchronous)
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_writer_snapshot_memo_isolates_mutations_and_preserves_types(h):
    from copy import deepcopy
    w = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        workspace = db.get(m.Workspace, w['id'])
        first = snapshots.domain_snapshot(db, workspace)
        expected = deepcopy(first)
        first['shifts'][0]['participants'].clear()
        first['people'].clear()
        second = snapshots.domain_snapshot(db, workspace)
        assert second == expected
        second['shifts'].clear()
        assert snapshots.domain_snapshot(db, workspace) == expected


@pytest.mark.parametrize('path', ['/api/home', '/api/home/rotas?start=2026-10-01&end=2026-11-01'])
def test_home_reads_do_not_hydrate_one_revision_per_rota(backend, path):
    h = backend
    for i in range(3):
        h.workspace('Bounded Home ' + str(i))
    warm_all(h.app.state.engine)
    statements = []
    def sql(_c, _cu, statement, _p, _ctx, _m):
        statements.append(statement)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql)
    try:
        h.get('coordinator', path)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql)
    assert not [s for s in statements if s.startswith('SELECT workspaces.revision ')], statements


@pytest.mark.parametrize('snapshot, expected', [({}, 'legacy'), ({'generation_engine': None}, None),
                                             ({'generation_engine': 'cp_sat'}, 'cp_sat')])
def test_status_preserves_unknown_engine_instead_of_claiming_legacy(backend, snapshot, expected):
    h = backend
    w = h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        job = m.Job(department_id=h.seed['department_id'], workspace_id=w['id'], kind='generation', status='queued',
                    requested_by=h.seed['identities']['coordinator'], input_fingerprint='old', input_snapshot=snapshot)
        db.add(job)
        db.flush()
        jid = job.id
    assert h.get('coordinator', f'/api/jobs/{jid}/status')['job']['generation_engine'] == expected
