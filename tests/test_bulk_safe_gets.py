"""Cookie-authenticated bulk reads do not require write CSRF tokens."""
import uuid

import pytest
from fastapi.testclient import TestClient

from apps.api import models as m
from apps.api.db import transaction
from .clock_support import freeze_clock
from .test_bulk import people_change


@pytest.mark.parametrize('path', ['/api/bulk/collections', '/api/bulk/settings',
                                  '/api/bulk/staged', '/api/contracts'])
def test_bulk_and_condition_metadata_gets_require_a_session_but_not_csrf(h, path):
    client = h.client('coordinator')
    del client.headers['x-csrf-token']
    assert 'x-csrf-token' not in client.headers
    response = client.get(path)
    assert response.status_code == 200, response.text
    with TestClient(h.app, base_url=h.settings.public_origin) as anonymous:
        response = anonymous.get(path)
        assert response.status_code == 401, response.text
        assert response.json()['error']['code'] == 'AUTH_REQUIRED'


@pytest.mark.parametrize('action', ['preview', 'resolve', 'rows', 'reopen'])
def test_bulk_posts_still_require_csrf(h, action):
    payload = people_change(h)
    if action == 'preview':
        path, body = '/api/commands/preview', {'kind': 'bulk.apply', 'payload': payload}
    elif action == 'resolve':
        path, body = '/api/bulk/resolve', {'collection': 'people'}
    elif action == 'rows':
        path, body = '/api/bulk/rows', {'selection': {'collection': 'people'}, 'limit': 1}
    else:
        path, body = f'/api/bulk/staged/{uuid.uuid4()}/reopen', None
    client = h.client('coordinator')
    del client.headers['x-csrf-token']
    response = client.post(path, json=body)
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'CSRF_FAILED'


def test_bulk_collection_get_without_csrf_keeps_the_session_rate_limit(h, monkeypatch):
    freeze_clock(monkeypatch)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed['department_id']).bulk_settings = {
            'rate_bulk_preview_per_minute': 1}
    client = h.client('coordinator')
    del client.headers['x-csrf-token']
    response = client.get('/api/bulk/collections')
    assert response.status_code == 200, response.text
    response = client.get('/api/bulk/collections')
    assert response.status_code == 429, response.text
    assert response.json()['error']['code'] == 'RATE_LIMITED'
