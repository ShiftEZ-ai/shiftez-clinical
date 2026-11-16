"""Monitor credentials are private; incoming evidence is bounded, scoped and idempotent."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import logging
from pathlib import Path
import secrets

from alembic import command
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import authenticate, digest
from apps.api.db import make_engine, transaction
from apps.api.main import BodyLimitMiddleware
from apps.api.services import monitors
from apps.api.settings import Settings
from migrations import safety
from packages.contracts import schemas as s
from scripts import manage
from .clock_support import freeze_clock
from .migration_support import historical_seed
from .test_feedback_storage import snapshot

REASON = 'Configure a synthetic external monitor'
INTEGRATION = '/api/system/monitor-integration'
EVENTS = '/api/system/monitor-events'
STATUS = '/api/system/status-files'
EVENT = {'source': 'synthetic', 'state': 'down', 'event_id': 'synthetic-event-1', 'message': 'Test evidence'}


def configure(h):
    response = h.client('operator').put(INTEGRATION, json={'rotate': True, 'reason': REASON})
    assert response.status_code == 200, response.text
    assert response.json()['configured'] is True
    return response.json()['token']


def external(h):
    return TestClient(h.app, base_url=h.settings.public_origin)


def post(client, token, payload=None):
    return client.post(EVENTS, json=EVENT if payload is None else payload,
                       headers={'authorization': 'Bearer ' + token})


def test_monitor_migration_is_additive_and_preserves_existing_rows_and_audit(tmp_path, monkeypatch):
    # The historical schema predates event types; seeding them is covered by the events migration tests.
    monkeypatch.setattr('apps.api.services.event_defaults.seed_department', lambda *args: None)
    path = tmp_path / 'before-monitors.db'
    url = 'sqlite:///' + str(path)
    command.upgrade(manage.alembic_config(url), '0070_open_ended_attachments')
    engine = make_engine(url)
    try:
        with transaction(engine, write=True) as db:
            seed = historical_seed(db)
            db.add(m.AuditEvent(department_id=seed['department_id'], actor_id=seed['identities']['staff'],
                                action='test.evidence', reason='Synthetic evidence before monitors',
                                detail={'revision': 1}, correlation_id='synthetic-monitor-migration'))
    finally:
        engine.dispose()
    before = snapshot(path)
    command.upgrade(manage.alembic_config(url), '0076_monitor_events')
    after = snapshot(path)
    added = {'monitor_integrations', 'monitor_incidents'}
    assert set(after) == set(before) | added
    assert {name: after[name] for name in before} == before
    assert all(after[name] == [] for name in added)
    migration = Path('migrations/versions/0076_monitor_events.py')
    assert safety.findings(migration) == [] and safety.unreviewed(migration) == []


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_system_monitor_permissions_follow_operations_roles(h, role, tmp_path):
    object.__setattr__(h.settings, 'status_directory', str(tmp_path))
    account = role if role in h.seed['identities'] else 'staff'
    if account != role:
        with transaction(h.app.state.engine, write=True) as db:
            [binding] = db.scalars(select(m.RoleBinding).where(
                m.RoleBinding.identity_id == h.seed['identities'][account],
                m.RoleBinding.department_id == h.seed['department_id']))
            binding.role = role
    client = h.client(account)
    reads = 200 if role in s.PERMISSIONS['system.operations'] else 403
    edits = 200 if role in s.PERMISSIONS['integrations.monitor'] else 403
    assert client.get(EVENTS).status_code == reads
    assert client.get(STATUS).status_code == reads
    assert client.get(INTEGRATION).status_code == edits
    assert client.put(INTEGRATION, json={'token': None, 'reason': REASON}).status_code == edits


def test_monitor_endpoints_use_the_permission_map(h, monkeypatch, tmp_path):
    object.__setattr__(h.settings, 'status_directory', str(tmp_path))
    monkeypatch.setitem(s.PERMISSIONS, 'integrations.monitor', ('admin',))
    monkeypatch.setitem(s.PERMISSIONS, 'system.operations', ('admin',))
    for role, status in (('admin', 200), ('operator', 403)):
        client = h.client(role)
        for path in (EVENTS, STATUS, INTEGRATION):
            assert client.get(path).status_code == status
        assert client.put(INTEGRATION, json={'token': None, 'reason': REASON}).status_code == status


def test_operations_reads_require_a_session_and_token_changes_require_csrf(h):
    with external(h) as client:
        for path in (EVENTS, STATUS, INTEGRATION):
            assert client.get(path).status_code == 401
    client = h.client('operator')
    assert client.put(INTEGRATION, json={'token': None, 'reason': REASON},
                      headers={'x-csrf-token': ''}).status_code == 403
    assert client.put(INTEGRATION, json={'token': None, 'reason': REASON},
                      headers={'origin': 'https://untrusted.example'}).status_code == 403


def test_monitor_token_is_generated_once_stored_only_as_a_digest_and_rotated_or_disabled(h, monkeypatch):
    assert h.get('operator', INTEGRATION) == {'configured': False}
    generated = []
    generate = secrets.token_urlsafe

    def observed(size):
        generated.append(size)
        return generate(size)

    monkeypatch.setattr(monitors.secrets, 'token_urlsafe', observed)
    token = configure(h)
    with transaction(h.app.state.engine) as db:
        row = db.get(m.MonitorIntegration, h.seed['department_id'])
        assert set(row.__table__.columns.keys()) == {'department_id', 'token_hash'}
        assert row.token_hash == digest(token) and token not in row.token_hash
    assert h.get('operator', INTEGRATION) == {'configured': True}
    new_token = configure(h)
    assert new_token != token and generated == [32, 32]
    with external(h) as client:
        assert post(client, token).status_code == 403
        assert post(client, new_token).status_code == 200
        response = h.client('operator').put(INTEGRATION, json={'token': None, 'reason': REASON})
        assert response.status_code == 200 and response.json() == {'configured': False}
        assert h.get('operator', INTEGRATION) == {'configured': False}
        assert post(client, new_token).status_code == 403
    with transaction(h.app.state.engine) as db:
        row = db.get(m.MonitorIntegration, h.seed['department_id'])
        assert row.token_hash is None
        changes = list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == 'monitor.token_changed')))
        assert len(changes) == 3
        assert sum(change.detail == {'configured': True} for change in changes) == 2
        assert sum(change.detail == {'configured': False} for change in changes) == 1
        assert all(change.reason == REASON and change.actor_id == h.seed['identities']['operator'] for change in changes)
        audit = json.dumps([{'reason': change.reason, 'detail': change.detail} for change in changes])
        assert token not in audit and new_token not in audit


@pytest.mark.parametrize('action', [{}, {'token': ''}, {'token': 'x' * 43}, {'rotate': False},
                                   {'rotate': True, 'token': None}, {'rotate': None}])
def test_token_inputs_require_rotation_or_explicit_disable(h, action):
    response = h.client('operator').put(INTEGRATION, json={**action, 'reason': REASON})
    assert response.status_code == 422


def test_webhook_accepts_only_bearer_token_and_uses_constant_time_comparison(h, monkeypatch):
    token = configure(h)
    compare = secrets.compare_digest
    comparisons = []

    def observed(left, right):
        comparisons.append((left, right))
        return compare(left, right)

    monkeypatch.setattr(monitors.secrets, 'compare_digest', observed)
    with external(h) as client:
        assert client.post(EVENTS, json=EVENT).status_code == 401
        assert post(client, 'invalid-token').status_code == 403
        assert comparisons and all(len(left) == len(right) == 64 for left, right in comparisons)
        comparisons.clear()
        assert client.post(EVENTS, json=EVENT, headers={'authorization': 'Basic ' + token}).status_code == 403
        assert post(client, token).status_code == 200
        assert comparisons and all(len(left) == len(right) == 64 for left, right in comparisons)
        # Only the exact webhook has the origin exception.
        assert client.post('/api/commands', json={}).status_code == 403
        assert client.post(EVENTS + '/unexpected', json=EVENT).status_code == 403


def test_receipt_is_idempotent_and_incident_preserves_provider_evidence(h):
    token = configure(h)
    with external(h) as client:
        assert post(client, token).json() == {'accepted': True, 'duplicate': False}
        assert post(client, token, dict(reversed(list(EVENT.items())))).json() == {'accepted': True, 'duplicate': True}
        assert post(client, token, {**EVENT, 'state': 'up'}).status_code == 409
        without_id = {'source': 'another-provider', 'state': 'up', 'detail': {'time': 'synthetic'}}
        assert post(client, token, without_id).json()['duplicate'] is False
        assert post(client, token, without_id).json()['duplicate'] is True
    rows = h.get('auditor', EVENTS)['incidents']
    assert len(rows) == 2
    stored = next(row for row in rows if row['source'] == EVENT['source'])
    assert stored['state'] == 'down' and stored['raw_payload'] == EVENT
    assert stored['received_at'] and stored['id'] and token not in json.dumps(rows)


@pytest.mark.parametrize('status,state', [('offline', 'down'), ('online', 'up')])
def test_official_hetrixtools_uptime_payload_is_stored_as_an_incident(h, status, state):
    token = configure(h)
    payload = {'monitor_id': 'synthetic-monitor-id', 'monitor_status': status, 'timestamp': 1791000000,
               'monitor_name': 'Synthetic Rota readiness', 'monitor_target': 'https://example.invalid/health/service',
               'monitor_type': 'Website Monitor', 'monitor_errors': []}
    with external(h) as client:
        response = post(client, token, payload)
        assert response.status_code == 200, response.text
    [row] = h.get('operator', EVENTS)['incidents']
    assert row['source'] == 'hetrixtools' and row['state'] == state and row['raw_payload'] == payload


@pytest.mark.parametrize('monitor_status', ['down', 'offline', {'provider': 'synthetic'}])
def test_explicit_generic_source_and_state_preserve_provider_fields(h, monitor_status):
    token = configure(h)
    payload = {'source': 'kuma', 'state': 'down', 'event_id': 'generic-provider-event',
               'monitor_status': monitor_status}
    with external(h) as client:
        response = post(client, token, payload)
        assert response.status_code == 200, response.text
        assert response.json() == {'accepted': True, 'duplicate': False}
        assert post(client, token, payload).json() == {'accepted': True, 'duplicate': True}
    [row] = h.get('operator', EVENTS)['incidents']
    assert row['source'] == 'kuma' and row['state'] == 'down' and row['raw_payload'] == payload


@pytest.mark.parametrize('body', [b'{', b'[]', b'{"source":"a","state":"unknown"}',
    b'{"source":"a","state":"up","number":NaN}',
    b'{"source":"a","state":"up","detail":{"number":1e999}}',
    b'{"monitor_status":[],"monitor_id":"x","timestamp":1}',
    b'{"monitor_status":{},"monitor_id":"x","timestamp":1}',
    b'{"monitor_status":"offline","monitor_id":"x","timestamp":true}'])
def test_invalid_webhook_evidence_is_rejected_without_storage(h, body):
    token = configure(h)
    with external(h) as client:
        response = client.post(EVENTS, content=body, headers={'authorization': 'Bearer ' + token,
                                                              'content-type': 'application/json'})
        assert response.status_code == 422, response.text
    assert h.get('operator', EVENTS)['incidents'] == []


def test_declared_and_streamed_body_size_are_capped_before_storage(h):
    token = configure(h)
    with external(h) as client:
        response = post(client, token, {**EVENT, 'detail': 'x' * s.MONITOR_LIMITS['body_bytes']})
        assert response.status_code == 413
    sent = []
    incoming = iter([{'type': 'http.request', 'body': b'x' * 40000, 'more_body': True},
                     {'type': 'http.request', 'body': b'x' * 40000, 'more_body': False}])

    async def receive():
        return next(incoming)

    async def send(message):
        sent.append(message)

    async def downstream(scope, receive, send):
        pytest.fail('Oversized streamed evidence reached the application')

    scope = {'type': 'http', 'path': EVENTS, 'method': 'POST', 'headers': []}
    asyncio.run(BodyLimitMiddleware(downstream, h.settings.max_body_bytes)(scope, receive, send))
    assert sent[0]['status'] == 413
    assert h.get('operator', EVENTS)['incidents'] == []


def test_event_rate_limit_is_shared_across_clients_and_resets_with_its_window(h, monkeypatch):
    token = configure(h)
    object.__setattr__(h.settings, 'monitor_events_per_minute', 2)
    moment = freeze_clock(monkeypatch)
    with external(h) as first, external(h) as second:
        assert post(first, token).status_code == 200 and post(second, token).status_code == 200
        response = post(first, token)
        assert response.status_code == 429 and 1 <= int(response.headers['retry-after']) <= 60
        freeze_clock(monkeypatch, moment + timedelta(seconds=61))
        assert post(second, token).status_code == 200
    assert len(h.get('operator', EVENTS)['incidents']) == 1


def test_invalid_tokens_cannot_bypass_the_network_rate_limit(h, monkeypatch):
    configure(h)
    freeze_clock(monkeypatch)
    with external(h) as client:
        for attempt in range(601):
            response = post(client, 'invalid-' + str(attempt))
            if response.status_code == 429:
                break
            assert response.status_code == 403
        else:
            pytest.fail('Invalid token attempts had no bounded per-network ceiling')
    assert int(response.headers['retry-after']) >= 1


@pytest.mark.postgres
def test_concurrent_postgres_retries_store_one_incident(pg):
    token = configure(pg)
    with external(pg) as client, ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(client, token), range(2)))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert sorted(response.json()['duplicate'] for response in responses) == [False, True]
    assert len(pg.get('operator', EVENTS)['incidents']) == 1


def test_monitor_tokens_and_incidents_are_department_scoped(h):
    token = configure(h)
    session_token = h.client('operator').cookies['rota_session']
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        other = m.Department(organisation_id=department.organisation_id, name='Synthetic other department')
        db.add(other)
        db.flush()
        other_id = other.id
        actor = authenticate(db, session_token)
        other_actor = replace(actor, department_id=other_id)
        result = monitors.change_token(db, other_actor, s.MonitorTokenInput(rotate=True, reason=REASON), 'synthetic')
        other_token = result['token']
        assert other_token != token
    with external(h) as client:
        assert post(client, token).status_code == 200
        assert post(client, other_token).status_code == 200
    assert len(h.get('operator', EVENTS)['incidents']) == 1
    with transaction(h.app.state.engine) as db:
        other_actor = replace(authenticate(db, session_token), department_id=other_id)
        assert len(monitors.incidents(db, other_actor, 0)['incidents']) == 1
        assert {row.department_id for row in db.scalars(select(m.MonitorIncident))} == {other_id, h.seed['department_id']}


def test_incidents_are_paged_without_cross_department_or_extra_evidence(h):
    with transaction(h.app.state.engine, write=True) as db:
        for index in range(s.MONITOR_LIMITS['page_size'] + 1):
            db.add(m.MonitorIncident(department_id=h.seed['department_id'], source='synthetic', state='up',
                                    raw_payload={'index': index}, event_key=str(index),
                                    received_at=m.now() + timedelta(seconds=index)))
    first = h.get('auditor', EVENTS)
    second = h.get('auditor', EVENTS + '?offset=' + str(first['next_offset']))
    assert len(first['incidents']) == s.MONITOR_LIMITS['page_size'] and len(second['incidents']) == 1
    assert second['next_offset'] is None
    assert len({row['id'] for row in first['incidents'] + second['incidents']}) == s.MONITOR_LIMITS['page_size'] + 1
    assert h.client('operator').get(EVENTS + '?offset=-1').status_code == 422


def test_host_status_reads_only_the_bounded_health_and_last_requested_alert_lines(h, tmp_path):
    object.__setattr__(h.settings, 'status_directory', str(tmp_path))
    assert h.get('operator', STATUS) == {'health': None, 'alerts': []}
    (tmp_path / 'health.txt').write_bytes(b'ready\n')
    (tmp_path / 'alerts.log').write_text(''.join(f'alert-{i}\n' for i in range(210)), encoding='utf-8')
    assert h.get('operator', STATUS + '?lines=2') == {'health': 'ready\n', 'alerts': ['alert-208', 'alert-209']}
    assert len(h.get('auditor', STATUS)['alerts']) == 50
    assert len(h.get('auditor', STATUS + '?lines=200')['alerts']) == 200
    for value in ('0', '201', 'nonnumeric'):
        assert h.client('operator').get(STATUS + '?lines=' + value).status_code == 422
    limit = s.MONITOR_LIMITS['status_bytes']
    (tmp_path / 'health.txt').write_bytes(b'x' * (limit + 100))
    (tmp_path / 'alerts.log').write_bytes(b'partial' + b'x' * limit + b'\nlast-alert\n')
    result = h.get('operator', STATUS)
    assert len(result['health'].encode()) == limit and result['alerts'] == ['last-alert']


def test_host_status_refuses_a_symlink_even_where_os_symlinks_are_unavailable(h, tmp_path, monkeypatch):
    from pathlib import Path
    object.__setattr__(h.settings, 'status_directory', str(tmp_path))
    original = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda path: path.name == 'health.txt' or original(path))
    response = h.client('operator').get(STATUS)
    assert response.status_code == 503 and response.json()['error']['code'] == 'STATUS_UNAVAILABLE'


def test_monitor_settings_have_defaults_and_read_deployment_values(monkeypatch, tmp_path):
    for name in ('FMUC_WORKER_HEARTBEAT_MAX_AGE_SECONDS', 'FMUC_MONITOR_EVENTS_PER_MINUTE',
                 'FMUC_STATUS_DIRECTORY'):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings.from_env()
    assert defaults.worker_heartbeat_max_age_seconds == 180 and defaults.monitor_events_per_minute == 60
    assert defaults.status_directory == '/data/status'
    for name, value in {'FMUC_WORKER_HEARTBEAT_MAX_AGE_SECONDS': '240', 'FMUC_MONITOR_EVENTS_PER_MINUTE': '42',
                        'FMUC_STATUS_DIRECTORY': str(tmp_path)}.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    settings.check()
    assert settings.worker_heartbeat_max_age_seconds == 240 and settings.monitor_events_per_minute == 42
    assert settings.status_directory == str(tmp_path)


@pytest.mark.parametrize('values,match', [({'worker_heartbeat_max_age_seconds': 0}, 'HEARTBEAT'),
    ({'worker_heartbeat_max_age_seconds': 86401}, 'HEARTBEAT'), ({'monitor_events_per_minute': 0}, 'MONITOR_EVENTS'),
    ({'monitor_events_per_minute': 10001}, 'MONITOR_EVENTS'), ({'status_directory': 'relative'}, 'STATUS_DIRECTORY')])
def test_monitor_deployment_settings_reject_unsafe_values(values, match):
    with pytest.raises(RuntimeError, match=match):
        Settings(environment='test', **values).check()


def test_tampered_monitor_digest_fails_closed(h):
    token = configure(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.MonitorIntegration, h.seed['department_id'])
        row.token_hash = digest('different-token')
    with external(h) as client:
        response = post(client, token)
        assert response.status_code == 403 and token not in response.text
    assert h.get('operator', EVENTS)['incidents'] == []


def test_request_logs_exclude_rotated_tokens_and_bearer_headers(h, caplog):
    with caplog.at_level(logging.INFO, logger='rota.api'):
        token = configure(h)
        with external(h) as client:
            assert post(client, token).status_code == 200
            assert post(client, token + '-invalid').status_code == 403
        assert h.get('operator', INTEGRATION) == {'configured': True}
    assert any(record.message == 'http_request' and record.route == EVENTS for record in caplog.records)
    assert token not in caplog.text
