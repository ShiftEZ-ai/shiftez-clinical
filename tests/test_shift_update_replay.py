"""Check the detached edit against independent reads at replay and after commit."""
from uuid import uuid4

import pytest

from apps.api import models as m, snapshots
from apps.api.db import transaction
from apps.api.services import planning
from tests.clock_support import freeze_clock
from tests.test_attachments import people, setup
from tests.test_shift_update_currency import edit_payload


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('supervised', [False, True])
def test_edit_replay_uses_current_session_records_and_stores_fresh_check(h, monkeypatch, supervised):
    freeze_clock(monkeypatch)
    if supervised:
        trainee, assistant, mentor, duty_type = setup(h)
        participants = people(trainee, assistant, mentor)
        workspace = h.shift(h.workspace(), typ=duty_type, people=participants[1:])['workspace']
    else:
        workspace = h.shift(h.workspace(), people=h.seed['people'][:2])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    payload = edit_payload(workspace, shift)
    payload['participants'] = participants if supervised else payload['participants'][:-1]

    domain_snapshot = planning.domain_snapshot
    after_edit = []

    def inspect_replay(db, row, *args, **kwargs):
        data = domain_snapshot(db, row, *args, **kwargs)
        preparation = db.info.get('_planning_preparation') or {}
        if preparation.get('replay') and db.connection().info.get('rota_write') and data['focus_ids']:
            focus = set(data['focus_ids'])
            prepared_rows = [item for item in data['shifts'] if item['id'] in focus]
            assert prepared_rows == snapshots.workspace_shifts(db, row.id)
            if row.revision == workspace['revision'] + 1:
                after_edit.extend(prepared_rows)
        return data

    monkeypatch.setattr(planning, 'domain_snapshot', inspect_replay)
    response = h.client('coordinator').post('/api/commands', headers={'idempotency-key': str(uuid4())}, json={
        'kind': 'shift.update', 'payload': payload, 'expected_revision': workspace['revision']})
    assert response.status_code == 200, response.text
    assert [row['id'] for row in after_edit] == [shift['id']]
    if supervised:
        assert next(row['supervisor_person_id'] for row in after_edit[0]['participants']
                    if row['person_id'] == trainee) == mentor

    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        stored = row.validation
        current = planning.revalidate(db, row, store=False, include_internal=True)
    edit_receipts = {'fix_list_delta', 'fix_list_state'}
    assert {key: value for key, value in stored.items() if key not in edit_receipts} == {
        key: value for key, value in current.items() if key not in edit_receipts}
