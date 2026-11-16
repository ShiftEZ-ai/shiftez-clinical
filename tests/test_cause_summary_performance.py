"""The cause panel receives its complete evidence without duplicate summary projections."""
import json
import pytest
from tests.test_backend_scale import scale_department, measure
from tests.test_performance_budget import python_budget

pytestmark = pytest.mark.postgres


def test_cause_summary_matches_full_at_scale(pg, request):
    workspace, *_ = scale_department(pg, assigned=False)
    pg.command('coordinator', 'workspace.validate', {'reason': 'Synthetic payload budget'}, workspace['id'], workspace['revision'])
    client = pg.client('coordinator')
    path = f'/api/workspaces/{workspace["id"]}/summary'
    full, before = measure(pg, 'batch4-summary-full', lambda: client.get(path))
    compact, after = measure(pg, 'batch4-summary-causes', lambda: client.get(path + '?view=causes'))
    assert full.status_code == compact.status_code == 200
    assert compact.json() == {key: full.json()[key] for key in compact.json()}
    assert set(compact.json()) == {'workspace_id', 'revision', 'fingerprint', 'observations', 'repair_tasks', 'cause_groups'}
    assert compact.json()['cause_groups'] and compact.json()['repair_tasks']
    print('summary bytes', len(full.content), len(compact.content))
    request.node.user_properties.append(('cause_payload', json.dumps({
        'full_bytes': len(full.content), 'cause_bytes': len(compact.content), 'before': before, 'after': after})))
    assert len(compact.content) < len(full.content) * .4
    assert sum(after['sql'].values()) <= 35, after
    assert client.get(path + '?view=invalid').status_code == 422
    assert pg.client('staff').get(path + '?view=causes').status_code == 403
    return locals()


@pytest.mark.wall_benchmark
def test_cause_summary_wall_budget(pg, request):
    state = test_cause_summary_matches_full_at_scale(pg, request)
    assert state['after']['wall_seconds'] < python_budget(3), state['after']
