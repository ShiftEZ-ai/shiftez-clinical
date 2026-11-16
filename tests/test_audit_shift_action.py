"""A department-sized session action is bounded, atomic, and one undoable edit."""
import uuid

import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning
from packages.contracts.schemas import ShiftAction
from tests.test_backend_scale import measure, scale_department


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_department_session_action_limit_budget_and_atomic_undo(request, database, monkeypatch, record_property):
    h = request.getfixturevalue(database)
    workspace, data, _, _ = scale_department(h)
    original = h.detail(workspace)['shifts']
    ids = [shift['id'] for shift in original[:500]]
    assert len(ids) == 500 and len(original) >= 500 and len(data['people']) >= 150
    client = h.client('coordinator')
    calls = []
    evaluate = planning.evaluate

    # Staged commands re-run their handler in rolled-back preparation passes (dca06b33a); the rota is evaluated once.
    def checked(*args, **kwargs):
        calls.append(1)
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(planning, 'evaluate', checked)

    def close(selected, status=200):
        response = client.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
            'kind': 'shift.action', 'expected_revision': workspace['revision'],
            'payload': {'workspace_id': workspace['id'], 'shift_ids': selected,
                        'action': 'cancel', 'reason': 'Close selected department sessions'}})
        assert response.status_code == status, response.text
        return response.json()

    close(ids + [str(uuid.uuid4())], status=422)
    assert calls == [] and h.detail(workspace)['shifts'] == original
    with transaction(h.app.state.engine) as db:
        changes_before = db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id']))
    result, metrics = measure(h, 'department-session-action', lambda: close(ids))
    record_property('department_session_action', metrics)
    assert len(calls) == 1
    assert sum(metrics['sql'].values()) <= 3 * len(ids) + 150, metrics
    assert all(shift['cancelled'] == (shift['id'] in ids) for shift in h.detail(result['workspace'])['shifts'])
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.ChangeSet).where(m.ChangeSet.workspace_id == workspace['id'])) == changes_before + 1
    workspace = h.command('coordinator', 'workspace.undo', {'reason': 'Undo department closure'},
                          target=workspace['id'], revision=result['workspace']['revision'])['workspace']
    assert h.detail(workspace)['shifts'] == original
    workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [ids[-1]],
                          'action': 'lock', 'reason': 'Keep this session locked'}, revision=workspace['revision'])['workspace']
    locked = h.detail(workspace)['shifts']
    assert close(ids, status=409)['error']['code'] == 'ASSIGNMENT_LOCKED'
    assert h.detail(workspace)['shifts'] == locked


def test_session_action_contract_accepts_a_week_and_has_a_hard_ceiling():
    limit = ShiftAction.model_json_schema()['properties']['shift_ids']['maxItems']
    assert limit == 500
    payload = {'workspace_id': str(uuid.uuid4()), 'action': 'cancel', 'reason': 'Close a selected week'}
    assert len(ShiftAction.model_validate({**payload, 'shift_ids': [str(uuid.uuid4()) for _ in range(112)]}).shift_ids) == 112
