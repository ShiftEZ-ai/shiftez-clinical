"""Durable planner preview enqueue, worker rollback and stale apply guards."""
import uuid

from apps.worker.main import process_one


def command_for(workspace, shift_id):
    return {
        'kind': 'shift.action',
        'expected_revision': workspace['revision'],
        'payload': {'workspace_id': workspace['id'], 'shift_ids': [shift_id],
                    'action': 'cancel', 'reason': 'Async preview test'},
    }


def enqueue(h, workspace, *, command=None):
    command = command or command_for(workspace, h.detail(workspace)['shifts'][0]['id'])
    response = h.client('coordinator').post('/api/planner/preview?async_preview=true', json=command)
    assert response.status_code == 202, response.text
    return response.json(), command


def test_async_preview_enqueues_without_running_mutation(h, monkeypatch):
    from apps.api.services import planner_tools

    workspace = h.shift(h.workspace('Async preview enqueue'))['workspace']
    monkeypatch.setattr(planner_tools, 'preview', lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('preview must run in the worker')))
    queued, command = enqueue(h, workspace)
    assert queued['job']['kind'] == 'planner_preview'
    assert queued['job']['status'] == 'queued'
    assert queued['job']['planner_command_kind'] == command['kind']
    assert queued['source_fingerprint'] == queued['job']['planner_source_fingerprint']


def test_async_preview_duplicate_joins_active_job(h):
    workspace = h.shift(h.workspace('Async preview duplicate'))['workspace']
    first, command = enqueue(h, workspace)
    second, _ = enqueue(h, workspace, command=command)
    assert second['joined'] is True
    assert second['job']['id'] == first['job']['id']


def test_async_preview_worker_rolls_back_and_apply_is_receipt_bound(h):
    workspace = h.shift(h.workspace('Async preview worker'))['workspace']
    before = h.detail(workspace)
    queued, command = enqueue(h, workspace)
    assert process_one(h.app.state.engine)
    detail = h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']
    assert detail['status'] == 'succeeded'
    assert detail['result']['preview'] is True and detail['result']['committed'] is False
    assert h.detail(workspace) == before
    missing_receipt = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
        **command, 'expected_fingerprint': queued['source_fingerprint']})
    assert missing_receipt.status_code == 409 and missing_receipt.json()['error']['code'] == 'STALE_PREVIEW'
    applied = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
        **command, 'preview_job_id': queued['job']['id'], 'expected_fingerprint': queued['source_fingerprint']})
    assert applied.status_code == 200, applied.text
    assert h.detail(workspace)['shifts'][0]['cancelled'] is True
    stale = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
        **command, 'preview_job_id': queued['job']['id'], 'expected_fingerprint': queued['source_fingerprint']})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'STALE_PREVIEW'


def test_async_preview_detail_does_not_expose_stored_command(h):
    workspace = h.shift(h.workspace('Async preview redaction'))['workspace']
    queued, _ = enqueue(h, workspace)
    detail = h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']
    assert 'planner_command' not in detail.get('input_snapshot', {})


def test_async_undo_preview_binds_apply_to_revision_fingerprint_and_job(h):
    import uuid

    first, second = h.seed['people'][:2]
    workspace = h.shift(h.workspace('Async undo preview'), people=[first])['workspace']
    row = h.detail(workspace)['shifts'][0]
    payload = {key: row[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload.update(workspace_id=workspace['id'], shift_id=row['id'],
                   participants=[{'person_id': second, 'start': row['start'], 'end': row['end']}])
    edited = h.command('coordinator', 'shift.update', payload, revision=workspace['revision'])['workspace']
    command = {'kind': 'workspace.undo', 'target_id': edited['id'], 'expected_revision': edited['revision'],
               'payload': {'reason': 'Async undo preview test'}}
    queued = h.client('coordinator').post('/api/planner/preview?async_preview=true', json=command)
    assert queued.status_code == 202, queued.text
    queued = queued.json()
    assert queued['job']['kind'] == 'planner_preview'
    assert process_one(h.app.state.engine)
    job = h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']
    assert job['status'] == 'succeeded' and job['result']['preview'] is True
    stale_without_receipt = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={**command, 'expected_fingerprint': queued['source_fingerprint']})
    assert stale_without_receipt.status_code == 409
    assert stale_without_receipt.json()['error']['code'] == 'STALE_PREVIEW'
    applied = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={**command, 'preview_job_id': queued['job']['id'], 'expected_fingerprint': queued['source_fingerprint']})
    assert applied.status_code == 200, applied.text
    assert h.detail(edited)['shifts'][0]['participants'][0]['person_id'] == first
    undone = applied.json()['workspace']
    redo = {'kind': 'workspace.redo', 'target_id': undone['id'], 'expected_revision': undone['revision'],
            'payload': {'reason': 'Async redo preview test'}}
    queued_redo = h.client('coordinator').post('/api/planner/preview?async_preview=true', json=redo)
    assert queued_redo.status_code == 202, queued_redo.text
    queued_redo = queued_redo.json()
    assert process_one(h.app.state.engine)
    redone = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())},
        json={**redo, 'preview_job_id': queued_redo['job']['id'], 'expected_fingerprint': queued_redo['source_fingerprint']})
    assert redone.status_code == 200, redone.text
    assert h.detail(edited)['shifts'][0]['participants'][0]['person_id'] == second
