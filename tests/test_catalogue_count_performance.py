"""Setup counts use the same scoped catalogue rows without hydrating the deferred lists."""
import json
import pytest
from tests.test_backend_scale import scale_department, measure
from tests.test_performance_budget import python_budget

pytestmark = pytest.mark.postgres


def test_setup_counts_preserve_scoping_and_bound_payload(pg, request):
    scale_department(pg)
    client = pg.client('coordinator')
    path = '/api/catalogues?include=policies&include=operating_calendars'
    full = client.get('/api/catalogues')
    boot = client.get('/api/catalogues?boot=true').json()
    known = ','.join(boot['_versions'].values())
    deferred, before = measure(pg, 'batch4-catalogue-deferred', lambda: client.get('/api/catalogues', params={'known': known}))
    compact, after = measure(pg, 'batch4-catalogue-counts', lambda: client.get(path + '&counts_only=true'))
    assert compact.headers.get('etag')
    warm, warm_after = measure(pg, 'batch4-catalogue-counts-warm',
                               lambda: client.get(path + '&counts_only=true', headers={'If-None-Match': compact.headers['etag']}))
    assert full.status_code == compact.status_code == 200
    assert compact.json()['counts'] == {key: sum(row.get('active') is not False for row in full.json()[key])
                                        for key in ('policies', 'operating_calendars')}
    assert compact.json()['rules_in_force'] == client.get('/api/rules/catalogue').json()['catalogue_counts']['in_force']
    assert warm.status_code == 304 and not warm.content
    assert '_versions' not in compact.json()
    print('catalogue bytes: full, deferred, counts', len(full.content), len(deferred.content), len(compact.content))
    request.node.user_properties.append(('catalogue_payload', json.dumps({
        'full_bytes': len(full.content), 'deferred_bytes': len(deferred.content), 'count_bytes': len(compact.content),
        'before': before, 'after': after, 'warm_after': warm_after})))
    assert len(compact.content) < 200
    assert sum(after['sql'].values()) <= 20, after
    # Baseline at the 450-row fixture is 13 statements; conditional warm reads stay within baseline + 1.
    assert sum(warm_after['sql'].values()) <= 14, warm_after
    assert client.get('/api/catalogues?include=unknown&counts_only=true').status_code == 422
    assert pg.client('staff').get(path + '&counts_only=true').status_code == 403
    return locals()


@pytest.mark.wall_benchmark
def test_setup_counts_wall_budget(pg, request):
    state = test_setup_counts_preserve_scoping_and_bound_payload(pg, request)
    assert state['after']['wall_seconds'] < python_budget(3), state['after']
