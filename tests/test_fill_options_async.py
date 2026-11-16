"""Durable async fill-options jobs retain checked pages and fingerprint-bound assignment."""
import uuid
import pytest
from fastapi.testclient import TestClient

from packages.contracts import schemas as s
from apps.worker.main import process_one
from apps.api.services import staffing
from tests.conftest import TEST_PASSWORD


PATH = '/api/planner/fill-options'


def enqueue(h, workspace, shift_id, request_id=None):
    payload = {'workspace_id': workspace['id'], 'shift_id': shift_id, 'expected_revision': workspace['revision']}
    if request_id:
        payload['request_id'] = request_id
    response = h.client('coordinator').post(PATH + '?async_options=true', json=payload)
    assert response.status_code == 202, response.text
    return response.json()


def test_async_fill_options_finishes_with_exact_envelope_and_status_fallback(h):
    workspace = h.shift(h.workspace('Async fill options'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    assert queued['job']['kind'] == 'fill_options'
    assert h.get('coordinator', '/api/jobs/' + queued['job']['id'] + '/status')['job']['status'] == 'queued'

    assert process_one(h.app.state.engine)
    detail = h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']
    result = detail['result']
    assert result['provisional_page'] is False
    assert result['source_fingerprint'] == queued['source_fingerprint']
    assert result['workspace_revision'] == workspace['revision']
    assert result['shift_id'] == shift['id']
    assert result['candidates']
    status = h.get('coordinator', '/api/jobs/' + queued['job']['id'] + '/status')['job']
    assert status['result']['provisional_page'] is False
    assert status['result']['source_fingerprint'] == result['source_fingerprint']


def test_async_fill_options_cancellation_uses_generic_job_cancel(h):
    workspace = h.shift(h.workspace('Async fill options cancellation'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    cancelled = h.command('coordinator', 'job.cancel', {'reason': 'Stop candidate check'}, queued['job']['id'], queued['job']['revision'])
    assert cancelled['job']['status'] == 'cancelled'
    assert process_one(h.app.state.engine) is False
    assert h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']['status'] == 'cancelled'


def test_fill_options_attach_tokens_coalesce_and_release_per_consumer(h):
    workspace = h.shift(h.workspace('Async fill options consumers'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    first, second = enqueue(h, workspace, shift['id']), enqueue(h, workspace, shift['id'])
    assert first['consumer_token'] and second['consumer_token'] != first['consumer_token']
    assert first['job']['id'] == second['job']['id'] and first['joined'] is False and second['joined'] is True
    attached = h.command('coordinator', 'job.attach', {
        'workspace_id': workspace['id'], 'shift_id': shift['id'], 'expected_revision': workspace['revision']},
        first['job']['id'])
    assert attached['job']['id'] == first['job']['id'] and attached['consumer_token'] not in {
        first['consumer_token'], second['consumer_token']}

    released = h.command('coordinator', 'job.release', {'token': first['consumer_token']}, first['job']['id'])
    assert released['released'] is True and released['cancelled'] is False and released['job']['status'] == 'queued'
    assert h.get('coordinator', '/api/jobs/' + first['job']['id'])['job']['status'] == 'queued'

    still_running = h.command('coordinator', 'job.release', {'token': second['consumer_token']}, first['job']['id'])
    assert still_running['released'] is True and still_running['cancelled'] is False and still_running['job']['status'] == 'queued'
    cancelled = h.command('coordinator', 'job.release', {'token': attached['consumer_token']}, first['job']['id'])
    assert cancelled['released'] is True and cancelled['cancelled'] is True and cancelled['job']['status'] == 'cancelled'


def test_fill_options_release_is_idempotent(h):
    workspace = h.shift(h.workspace('Async fill options idempotent release'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    first = h.command('coordinator', 'job.release', {'token': queued['consumer_token']})
    second = h.command('coordinator', 'job.release', {'token': queued['consumer_token']}, queued['job']['id'])
    assert first['cancelled'] is True and second['released'] is False and second['cancelled'] is True


def test_fill_options_release_requires_the_issuing_session(h):
    workspace = h.shift(h.workspace('Async fill options session ownership'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    other = TestClient(h.app, base_url=h.settings.public_origin)
    other.headers['origin'] = h.settings.public_origin
    try:
        login = other.post('/auth/login', json={'username': 'coordinator', 'password': TEST_PASSWORD})
        assert login.status_code == 200, login.text
        other.headers['x-csrf-token'] = other.cookies['rota_csrf']
        response = other.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
            'kind': 'job.release', 'target_id': queued['job']['id'], 'payload': {'token': queued['consumer_token']}})
        assert response.status_code == 403 and response.json()['error']['code'] == 'FORBIDDEN'
    finally:
        other.close()
    assert h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']['status'] == 'queued'


def test_fill_options_release_after_completion_is_harmless(h):
    workspace = h.shift(h.workspace('Async fill options finished release'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    assert process_one(h.app.state.engine)
    first = h.command('coordinator', 'job.release', {'token': queued['consumer_token']}, queued['job']['id'])
    second = h.command('coordinator', 'job.release', {'token': queued['consumer_token']}, queued['job']['id'])
    assert first['released'] is True and first['cancelled'] is False and first['job']['status'] == 'succeeded'
    assert second['released'] is False and second['cancelled'] is False and second['job']['status'] == 'succeeded'


def test_async_fill_options_fails_closed_when_revision_changes(h):
    workspace = h.shift(h.workspace('Async fill options stale'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])
    assert process_one(h.app.state.engine)
    detail = h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']
    assert detail['status'] == 'failed' and detail['error_code'] == 'STALE_INPUTS'


def test_async_fill_options_fingerprint_is_accepted_by_assign(h):
    workspace = h.shift(h.workspace('Async fill options assignment'), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    queued = enqueue(h, workspace, shift['id'])
    assert process_one(h.app.state.engine)
    result = h.get('coordinator', '/api/jobs/' + queued['job']['id'])['job']['result']
    candidate = next(row for row in result['candidates'] if row['status'] == 'ok')
    payload = {key: shift[key] for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')}
    payload['workspace_id'] = workspace['id']
    payload['shift_id'] = shift['id']
    payload['participants'] = [{**result['slot'], 'person_id': candidate['person_id']}]
    saved = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
        'kind': 'shift.update', 'expected_revision': workspace['revision'],
        'expected_fingerprint': result['source_fingerprint'], 'payload': payload})
    assert saved.status_code == 200, saved.text


def test_shared_hard_overlap_exclusion_skips_full_trial(h, monkeypatch):
    workspace = h.shift(h.workspace('Async fill options exclusions'), people=[h.seed['people'][0]])['workspace']
    workspace = h.shift(workspace, people=[])['workspace']
    source = h.snapshot(workspace)
    busy = h.seed['people'][0]
    target = next(row for row in source['shifts'] if not row['participants'])
    monkeypatch.setattr(staffing, 'evaluate', lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('hard overlap exclusions must not enter full candidate trials')))
    [row], _ = staffing.check_people(source, target, None, {busy})
    assert row['status'] == 'blocked'
    assert row['findings'][0]['code'] == 'TIME_OVERLAP'


def test_fill_options_reports_first_page_and_honours_generic_cancellation(monkeypatch):
    from types import SimpleNamespace
    from packages.domain.solver_control import Cancelled
    from tests.test_schedule_score_scale import department

    data = department()
    data['fingerprint'] = 'synthetic-fill-options-progress'
    data['shifts'][0]['locked'] = False
    workspace = SimpleNamespace(**data['workspace'], status='draft', revision=1)
    db = SimpleNamespace(scalar=lambda *args: None)
    actor = SimpleNamespace(department_id=workspace.department_id, permits=lambda *roles: True)
    monkeypatch.setattr(staffing, 'scoped', lambda *args: workspace)
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *args: data)
    from apps.api.services import published_changes
    monkeypatch.setattr(published_changes, 'validation_scope', lambda *args, **kwargs: None)
    pages = []
    with pytest.raises(Cancelled):
        staffing.fill_options(db, actor, s.FillOptionsInput(
            workspace_id=workspace.id, expected_revision=workspace.revision, shift_id=data['shifts'][0]['id']),
            progress=pages.append, cancelled=lambda: bool(pages))
    assert pages and pages[0]['evaluated_count'] == 20 and pages[0]['total'] == 148
    assert len(pages[0]['candidates']) == 20 and pages[0]['provisional_page'] is True
