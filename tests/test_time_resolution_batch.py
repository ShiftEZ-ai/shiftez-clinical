"""A session save resolves its wall times in one authenticated request."""
import json
from collections import Counter

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s


def test_time_resolution_batch_matches_single_calls_with_one_authentication(h, record_property):
    client = h.client('coordinator')
    locals = [f'2026-10-{day:02d}T08:00' for day in range(4, 20)] + ['2026-10-04T24:00', '2026-10-04T08:00']
    queries = Counter()

    def count(conn, cursor, statement, parameters, context, executemany):
        queries[statement.split()[0].upper()] += 1

    event.listen(h.app.state.engine, 'before_cursor_execute', count)
    try:
        responses = [client.get('/api/time/resolve', params={'local': local}) for local in locals]
        before = dict(queries)
        queries.clear()
        response = client.post('/api/time/resolve', json={'locals': locals})
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', count)
    assert all(response.status_code == 200 for response in responses)
    assert response.status_code == 200, response.text
    body = s.TimeResolveBatchResult.model_validate(response.json())
    assert body.instants == [response.json()['instant'] for response in responses]
    assert body.timezone == responses[0].json()['timezone']
    assert queries['SELECT'] <= before['SELECT'] / len(locals)
    record_property('measurement', json.dumps({'before_requests': len(responses), 'before_sql': before,
                                             'after_requests': 1, 'after_sql': dict(queries)}))


@pytest.mark.parametrize('locals', [[], ['bad'], ['2026-02-30T08:00'], ['2026-10-04X08:00'],
                                  ['2026-10-04T25:00'], ['2026-10-04T08:00'] * 1001])
def test_time_resolution_batch_rejects_invalid_input(h, locals):
    response = h.client('coordinator').post('/api/time/resolve', json={'locals': locals})
    assert response.status_code in {400, 422}


def test_time_resolution_batch_uses_current_timezone_and_rejects_dst_ambiguity(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed['organisation_id']).timezone = 'America/New_York'
    client = h.client('coordinator')
    assert client.post('/api/time/resolve', json={'locals': ['2026-10-04T08:00']}).json() == {
        'instants': ['2026-10-04T12:00:00+00:00'], 'timezone': 'America/New_York'}
    for local in ['2026-11-01T01:30', '2026-03-08T02:30']:
        assert client.post('/api/time/resolve', json={'locals': [local]}).status_code == 422


def test_time_resolution_batch_requires_csrf(h):
    client = h.client('coordinator')
    saved = client.headers.pop('x-csrf-token')
    try:
        response = client.post('/api/time/resolve', json={'locals': ['2026-10-04T08:00']})
    finally:
        client.headers['x-csrf-token'] = saved
    assert response.status_code == 403
