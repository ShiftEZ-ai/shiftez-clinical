import uuid

import pytest
from sqlalchemy import event

from apps.api import workspace_baselines


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_empty_draft_reads_and_verifies_each_captured_baseline_once(h, monkeypatch):
    client = h.client('coordinator')
    verified, selected = [], []
    verify = workspace_baselines.verify

    def count_verify(row):
        verified.append(row.id)
        return verify(row)

    def count_reads(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().lower().startswith('select ') and 'from workspace_baselines' in statement.lower():
            selected.append(1)

    monkeypatch.setattr(workspace_baselines, 'verify', count_verify)
    engine = h.app.state.engine
    event.listen(engine, 'before_cursor_execute', count_reads)
    try:
        response = client.post('/api/commands', headers={'idempotency-key': str(uuid.uuid4())}, json={
            'kind': 'workspace.create',
            'payload': {'name': 'Empty draft', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01'},
        })
    finally:
        event.remove(engine, 'before_cursor_execute', count_reads)

    assert response.status_code == 200
    result = response.json()
    assert result['setup'] == {'created': 0, 'skipped': 0}
    assert result['snapshot_hash'] == result['workspace']['baseline']['hash'] == result['baseline']['hash']
    # The preparation and the final writer have separate transactions and verify independently.
    assert (len(selected), len(verified)) == (2, 2)
    assert result['baseline']['id'] == verified[-1]
