"""Replies to a signed-in session carry their server time; identity routes and unauthenticated replies never do."""
import re

import pytest

TIMING = re.compile(r'app;dur=\d+\.\d, db;dur=\d+\.\d')


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_signed_in_replies_report_application_and_database_time(h):
    client = h.client('coordinator')
    for path in ('/api/me', '/api/workspaces?limit=10&offset=0', '/api/workspaces/not-a-rota'):
        response = client.get(path)
        timing = response.headers.get('server-timing', '')
        assert TIMING.fullmatch(timing), (path, response.status_code, timing)
        app, db = (float(part.split('=')[1]) for part in timing.split(', '))
        assert 0 <= db <= app + 1, (path, timing)


@pytest.mark.parametrize('h', ['sqlite'], indirect=True)
def test_identity_routes_and_unauthenticated_replies_never_report_server_time(h):
    client = h.client('coordinator')
    anonymous = type(client)(h.app, base_url=h.settings.public_origin)
    anonymous.headers['origin'] = h.settings.public_origin
    responses = [
        anonymous.get('/api/me'),
        anonymous.get('/api/workspaces?limit=10&offset=0'),
        anonymous.get('/health/live'),
        anonymous.get('/health/ready'),
        anonymous.get('/auth/config'),
        anonymous.post('/auth/identify', json={'identifier': 'nobody@example.test'}),
        anonymous.post('/auth/login', json={'username': 'nobody', 'password': 'not-a-password'}),
        anonymous.get('/auth/start'),
        anonymous.get('/api/account-links/not-a-token'),
        anonymous.post('/api/account-links/not-a-token', json={}),
        anonymous.get('/invite/not-a-token'),
        anonymous.get('/reset/not-a-token'),
        anonymous.get('/n/not-a-token'),
        anonymous.get('/calendar/not-a-token.ics'),
        client.get('/auth/config'),
        client.post('/auth/refresh'),
        client.post('/api/account/password', json={'current_password': 'not-it', 'new_password': 'Another-long-pass-1'}),
        client.post('/auth/logout'),
    ]
    for response in responses:
        assert 'server-timing' not in response.headers, (response.request.method, response.request.url.path, response.status_code)
