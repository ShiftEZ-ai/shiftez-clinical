"""A session edit replies with the rota check once, as a summary, and stores no copy of the findings in its receipt
(QA-Planner M21: about 0.9 MB per edit at 420 sessions). The reply also names the live-pulse value the change leads to,
so the page that made it does not refresh a second time (L17)."""
import uuid

from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import CHECK_SUMMARY
from tests.test_series_reconciliation import template


def _has_full_findings_list(value):
    if isinstance(value, dict):
        return any(key == 'findings' and isinstance(child, list) for key, child in value.items()) or any(
            _has_full_findings_list(child) for child in value.values())
    return isinstance(value, list) and any(_has_full_findings_list(child) for child in value)


def _post(h, kind, payload, target=None, revision=None, key=None, role='coordinator', status=200):
    response = h.client(role).post('/api/commands', headers={'idempotency-key': key or str(uuid.uuid4())},
        json={'kind': kind, 'payload': payload, 'target_id': target, 'expected_revision': revision})
    assert response.status_code == status, response.text
    return response.json()


def test_an_edit_reply_carries_the_check_once_as_a_summary(h):
    w = h.workspace()
    before = h.detail(w)['shifts']
    key = str(uuid.uuid4())
    payload = {'workspace_id': w['id'], 'assignment_type_id': h.seed['type'], 'location_id': h.seed['locations'][0],
               'service_ids': [h.seed['services'][0]], 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
               'participants': [], 'attributes': {}}
    reply = _post(h, 'shift.create', payload, revision=w['revision'], key=key)
    assert set(reply['validation']) <= set(CHECK_SUMMARY) and reply['validation']['error_count'] >= 1  # the open place
    assert 'validation' not in reply['workspace']
    # The full check is on the rota, the same evaluation.
    stored = h.get('coordinator', '/api/workspaces/' + w['id'])['workspace']['validation']
    assert stored['evaluated_at'] == reply['validation']['evaluated_at'] and stored['findings']
    assert reply['validation'] == {k: stored[k] for k in CHECK_SUMMARY if k in stored}
    assert stored['score'] is not None
    # The receipt keeps the summary, and a retry with the same key gets exactly the reply.
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        assert not _has_full_findings_list(receipt.result)
    assert _post(h, 'shift.create', payload, revision=w['revision'], key=key) == reply
    assert h.get('coordinator', '/api/commands/' + key)['result'] == reply
    # The reply names the pulse value this change leads to.
    assert reply['activity'] == h.get('coordinator', '/api/pulse')['activity']
    denied = _post(h, 'shift.create', payload, revision=reply['workspace']['revision'], role='staff', status=403)
    assert denied['error']['code'] == 'FORBIDDEN'
    undone = _post(h, 'workspace.undo', {'reason': 'Restore the draft before its single session edit'},
                   w['id'], reply['workspace']['revision'])
    detail = h.detail(w)
    assert detail['shifts'] == before and 'validation' not in undone['workspace']
    assert undone['validation'] == {k: detail['workspace']['validation'][k]
                                    for k in CHECK_SUMMARY if k in detail['workspace']['validation']}
    again = _post(h, 'workspace.undo', {'reason': 'A repeated Undo must not redo the edit'},
                 w['id'], undone['workspace']['revision'], status=409)
    assert again['error']['code'] == 'NO_UNDO' and h.detail(w)['shifts'] == before


def test_create_reply_retains_stored_validation_and_replays_one_result(h):
    series = template(h, days=(6,))
    payload = {'name': 'Compact recurring draft', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
               'series_ids': [series['id']]}
    key = str(uuid.uuid4())
    denied = _post(h, 'workspace.create', payload, role='staff', status=403)
    assert denied['error']['code'] == 'FORBIDDEN'
    reply = _post(h, 'workspace.create', payload, key=key)
    detail = h.detail(reply['workspace'])
    stored = detail['workspace']['validation']
    assert reply['setup']['created'] == 1 and len(detail['shifts']) == 1
    assert stored['findings'] and stored['score'] is not None and not stored['valid']
    assert 'validation' not in reply['workspace']
    assert reply['validation'] == {k: stored[k] for k in CHECK_SUMMARY if k in stored}
    assert _post(h, 'workspace.create', payload, key=key) == reply
    assert h.get('coordinator', '/api/commands/' + key)['result'] == reply
    assert h.detail(reply['workspace'])['shifts'] == detail['shifts']
    with transaction(h.app.state.engine) as db:
        receipt = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        assert not _has_full_findings_list(receipt.result)


def test_a_lifecycle_reply_summarises_the_check_on_its_rota(h):
    w = h.shift(h.workspace())['workspace']
    reply = _post(h, 'workspace.validate', {'reason': 'Check'}, w['id'], w['revision'])
    assert set(reply['validation']) <= set(CHECK_SUMMARY) and 'validation' not in reply['workspace']
