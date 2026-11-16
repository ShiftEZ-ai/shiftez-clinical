"""Session imports check the completed draft once, regardless of batch size."""
import json
import os
from pathlib import Path

import pytest

from apps.api.services import planning
from tests.test_backend_scale import measure, post, scale_department
from tests.test_bulk_draft_session_creation import creation


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('count', [10, 500])
def test_bulk_session_creation_checks_the_draft_once(request, database, count, monkeypatch):
    h = request.getfixturevalue(database)
    workspace, _, _, _ = scale_department(h, assigned=False)
    h.client('coordinator')
    calls = []
    original = planning.revalidate

    def revalidate(*args, **kwargs):
        calls.append(args[1].id)
        return original(*args, **kwargs)

    monkeypatch.setattr(planning, 'revalidate', revalidate)
    result, metrics = measure(h, f'bulk-create-{count}', lambda: post(h, 'bulk.apply', {
        'items': [creation(h, workspace) for _ in range(count)], 'reason': 'Synthetic batch performance'}))
    metrics['revalidations'] = len(calls)
    metrics['reply_bytes'] = len(json.dumps(result, separators=(',', ':')).encode())
    if label := os.getenv('B5_MEASURE'):
        Path(f'.run/b5-{label}-{database}-{count}.json').write_text(json.dumps(metrics, indent=2) + '\n')
    assert result['counts']['changed'] == count
    detail = h.detail(workspace)
    assert len(detail['shifts']) == 768 + count
    assert len(calls) == 1, metrics
    assert sum(metrics['sql'].values()) <= 300, metrics
    assert sum(len(row['issues']) for row in result['items']) <= count * 3, metrics
    replies = [reply for item in result['items'] for reply in item['results']]
    for field in ('health', 'issues', 'fix_list', 'fix_list_delta'):
        assert sum(field in reply for reply in replies) == 1, field
    # Compact transport retains the full canonical cause identities and acknowledgement evidence.
    canonical_list = h.client('coordinator').get(f"/api/workspaces/{workspace['id']}/fix-list").json()
    saved = {cause['id']: cause for cause in canonical_list['causes']}
    for cause in replies[0]['fix_list']['causes']:
        canonical = saved.pop(cause['id'])
        assert cause['evidence_hash'] == canonical['evidence_hash']
        assert cause['finding_keys'] == canonical['finding_keys']
        assert len(cause['evidence']) == len(canonical['evidence'])
        for actual, expected in zip(cause['evidence'], canonical['evidence']):
            assert all(actual.get(key) == expected.get(key) for key in (
                'code', 'severity', 'shift_id', 'finding_key', 'sessions', 'detail', 'rule_name', 'reason'))
    assert not saved
    # The fixed allowance includes the 768-session draft's existing issue/impact metadata.
    assert metrics['reply_bytes'] <= 3_000_000 + count * 20_000, metrics
