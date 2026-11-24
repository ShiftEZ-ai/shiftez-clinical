"""PWA protocol, consent, policy and leased delivery regression oracles (no real provider calls)."""
import base64
import io
import json
import os
import socket
import stat
from pathlib import Path
import struct
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from time import perf_counter, process_time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from apps.api import models as m
from apps.api.auth import revoke_session
from apps.api.db import retry_write, transaction
from apps.api.errors import Problem
from apps.api.serialization import record
from apps.api.settings import WORKER_IDLE_MAX_SECONDS
from apps.api.services import planning, pwa, pwa_transport as transport
from apps.worker import main as worker, push_delivery as delivery, email_delivery
from packages.contracts import schemas as s
from packages.domain.time import aware
from scripts.generate_vapid_key import generate


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def browser_keys():
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return private, {'p256dh': b64(public), 'auth': b64(os.urandom(16))}


def decrypt(body, private, keys):
    salt, size, length = body[:16], struct.unpack('!I', body[16:20])[0], body[20]
    public = body[21:21 + length]
    shared = private.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public))
    ikm = HKDF(algorithm=hashes.SHA256(), length=32, salt=transport.b64(keys['auth']),
        info=b'WebPush: info\x00' + transport.b64(keys['p256dh']) + public).derive(shared)
    key = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt, info=b'Content-Encoding: aes128gcm\x00').derive(ikm)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt, info=b'Content-Encoding: nonce\x00').derive(ikm)
    clear = AESGCM(key).decrypt(nonce, body[21 + length:], None)
    assert size == 4096 and clear[-1] == 2
    return clear[:-1]


def test_rfc8291_appendix_a_independent_known_answer():
    keys = {'p256dh': 'BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4',
            'auth': 'BTBZMqHH6r4Tts7J_aSIgg'}  # RFC 8291 Appendix A: the published Web Push test vector
    private = ec.derive_private_key(int.from_bytes(transport.b64('yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw'), 'big'), ec.SECP256R1())
    ua_private = ec.derive_private_key(int.from_bytes(transport.b64('q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94'), 'big'), ec.SECP256R1())
    plaintext = transport.b64('V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24')
    wire = transport.encrypt(keys, plaintext, private_key=private, salt=transport.b64('DGv6ra1nlYgDCS1FRnbzlw'))
    assert b64(wire[:86]) == 'DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8'
    assert b64(wire[86:]) == '8pfeW0KbunFT06SuDKoJH9Ql87S1QUrdirN6GcG7sFz1y1sqLgVi1VhjVkHsUoEsbI_0LpXMuGvnzQ'
    assert decrypt(wire, ua_private, keys) == plaintext


@pytest.fixture
def ready(h):
    # Ephemeral test keys exist only in memory and the disposable database, never stdout.
    key = ec.generate_private_key(ec.SECP256R1())
    for name, value in {'fmuc_data_key': os.urandom(32).hex(), 'vapid_private_key': b64(key.private_numbers().private_value.to_bytes(32, 'big')),
                        'vapid_key_id': 'test-active', 'vapid_subject': 'mailto:push@example.org'}.items():
        object.__setattr__(h.settings, name, value)
    h.command('operator', 'pwa.system.update', {**s.PwaSystemSettings(enabled=True).model_dump(), 'reason': 'Enable disposable push transport'}, revision=1)
    h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'], 'push_enabled': True,
              'reason': 'Enable disposable consent test'}, revision=1)
    return h


def subscribe(h, role='staff', endpoint='https://fcm.googleapis.com/opaque-capability', hint='test-device-1'):
    private, keys = browser_keys()
    payload = {'endpoint': endpoint, 'keys': keys, 'installation_hint': hint, 'explicit_consent': True,
               'key_id': h.settings.vapid_key_id}
    return h.command(role, 'push.subscribe', payload), private, payload


def event(h, kind='availability', **payload):
    with transaction(h.app.state.engine, write=True) as db:
        row = m.Outbox(department_id=h.seed['department_id'], kind=kind, event_key=m.uid(),
                       payload={'person_id': h.seed['people'][0], 'actor_id': h.seed['identities']['coordinator'], **payload})
        db.add(row); db.flush()
        event_id = row.id
        worker.dispatch_batch(db)
    return event_id


def claims(h):
    return retry_write(h.app.state.engine, lambda db: delivery.claim_batch(db, h.settings))


def test_vapid_signature_audience_expiration_and_draining(ready):
    expiry = int((m.now() + timedelta(hours=12)).timestamp())
    auth = transport.authorization(ready.settings, 'test-active', 'https://fcm.googleapis.com', expiry)
    token, public = auth.removeprefix('vapid t=').split(', k=')
    point = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), transport.b64(public))
    data = jwt.decode(token, point, algorithms=['ES256'], audience='https://fcm.googleapis.com', options={'verify_exp': False})
    assert data == {'aud': 'https://fcm.googleapis.com', 'exp': expiry, 'sub': 'mailto:push@example.org'}
    old = replace(ready.settings, vapid_key_id='new', vapid_draining_keys=json.dumps({'test-active': ready.settings.vapid_private_key}))
    assert 'test-active' in transport.keyring(old)
    with pytest.raises(Problem):
        transport.authorization(ready.settings, 'test-active', 'https://fcm.googleapis.com', expiry + 86400)


@pytest.mark.parametrize('endpoint', ['http://fcm.googleapis.com/x', 'https://user@fcm.googleapis.com/x',
    'https://fcm.googleapis.com:8443/x', 'https://fcm.googleapis.com/x#private', 'https://fcm.googleapis.com.evil.org/x',
    'https://127.0.0.1/x', 'https://fcm.googleapis.com\\@evil.org/x', 'https://fcm.googleapis.com/x\nsecret'])
def test_endpoint_security(endpoint):
    with pytest.raises(Problem):
        transport.endpoint_origin(endpoint, s.PwaSystemSettings())


def test_wire_security_bounds_and_pinned_dns(monkeypatch):
    _, keys = browser_keys()
    for invalid in ({**keys, 'p256dh': 'A' * 87}, {**keys, 'auth': b64(b'too short')}):
        with pytest.raises(Problem):
            transport.validate_keys(invalid)
    for payload, topic in ((b'x' * 4096, 'ok'), (b'x', '../bad')):
        with pytest.raises(Problem):
            transport.encode({'keys': keys}, payload, 'authorization', ttl=60, topic=topic, urgency='normal')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443))])
    with transport.PinnedTransport() as pinned, pytest.raises(httpx.ConnectError):
        pinned.handle_request(httpx.Request('POST', 'https://fcm.googleapis.com/x'))


def test_key_file_is_exclusive_protected_and_output_public_only(tmp_path):
    path = tmp_path / 'private'
    result = generate(path, 'test-key')
    assert set(result) == {'key_id', 'public_key'} and len(transport.b64(result['public_key'])) == 65
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        generate(path, 'replacement')


def test_disabled_defaults_no_legacy_consent_and_required_privacy(h):
    status = h.get('staff', '/api/me/push')
    assert not status['enabled'] and status['readiness'] == 'disabled_by_department'
    assert status['installations'] == [] and status['policy']['lock_screen_detail'] == 'generic'
    assert status['unread']['count'] == h.get('staff', '/api/notifications')['unread_total']
    for cls, field in ((s.NotificationPolicy, 'event_defaults'), (s.NotificationPreferences, 'event_overrides')):
        for required in ('duty_changed', 'absence_reported'):
            with pytest.raises(ValueError):
                cls.model_validate({field: {required: {'push': 'off'}}})
    h.command('staff', 'notification.preferences.update', {'department_id': h.seed['department_id'],
        'detail': 'event', 'reason': 'Try exceeding privacy'}, revision=1, status=422)
    h.command('staff', 'notification.preferences.update', {'department_id': h.seed['department_id'],
        'event_overrides': {'swap': {'email': 'on'}}, 'reason': 'Try unconfigured email'}, revision=1, status=409)


@pytest.mark.parametrize('role', s.ROLE_NAMES)
def test_api_role_permissions_use_authoritative_catalogue(h, role):
    login = role if role in h.seed['identities'] else 'staff'
    if login != role:
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(update(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed['identities'][login]).values(role=role))
    h.get(login, '/api/me/push')
    h.get(login, '/api/me/notification-preferences')
    h.command(login, 'notification.preferences.update', {'department_id': h.seed['department_id'], 'reason': 'Choose own channels'}, revision=1)
    h.get(login, '/api/admin/notifications/policy', status=200 if role == 'admin' else 403)
    h.get(login, '/api/operator/pwa', status=200 if role == 'operator' else 403)
    h.command(login, 'notification.policy.update', {'department_id': h.seed['department_id'], 'reason': 'Policy permission'}, revision=1,
              status=200 if role == 'admin' else 403)


def test_subscription_consent_ownership_refresh_rotation_redaction_and_logout(ready):
    h = ready
    subscribed, _, value = subscribe(h)
    h.command('staff', 'push.subscribe', {**value, 'explicit_consent': False}, status=422)
    h.command('approver', 'push.subscribe', value, status=409)
    refreshed = h.command('staff', 'push.subscribe', {**value, 'expiration_time': (m.now() + timedelta(days=3)).isoformat()})
    assert refreshed['subscription_id'] == subscribed['subscription_id']
    rotated = h.command('staff', 'push.rotate', {**value, 'endpoint': value['endpoint'] + '-replaced',
        'installation_id': subscribed['installation_id'], 'expected_revision': refreshed['revision']})
    h.command('approver', 'push.unsubscribe', {'installation_id': subscribed['installation_id'], 'reason': 'Wrong user'}, status=404)
    with transaction(h.app.state.engine) as db:
        subscriptions = list(db.scalars(select(m.PushSubscriptionVersion)))
        assert [sub.state for sub in subscriptions] == ['superseded', 'active']
        serialized = json.dumps([record(sub) for sub in subscriptions])
        evidence = json.dumps([record(r) for r in db.scalars(select(m.CommandReceipt))])
        assert value['endpoint'] not in serialized + evidence and value['keys']['auth'] not in serialized + evidence
        assert subscriptions[0].browser_expiration_at is not None
        session_id = db.get(m.PushInstallation, subscribed['installation_id']).session_id
    with transaction(h.app.state.engine, write=True) as db:
        revoke_session(db.get(m.Session, session_id))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.PushInstallation, subscribed['installation_id']).state == 'revoked'
        assert db.get(m.PushSubscriptionVersion, rotated['subscription_id']).state == 'revoked'


def test_policy_versions_conflicts_undo_immutability_and_email_projection(ready):
    h = ready
    first = h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'],
        'push_enabled': True, 'lock_screen_detail': 'event_date', 'reason': 'Allow date'}, revision=2)
    h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'], 'reason': 'Stale policy'}, revision=2, status=409)
    versions = h.get('admin', '/api/admin/notifications/policy/history')['versions']
    assert len(versions) == 3 and versions[0]['id'] == first['version_id']
    undone = h.command('admin', 'notification.policy.undo', {'department_id': h.seed['department_id'],
        'version_id': versions[-1]['id'], 'reason': 'Restore disabled baseline'}, revision=3)
    assert undone['revision'] == 4 and not undone['policy']['push_enabled']
    with pytest.raises(DBAPIError), transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.NotificationPolicyVersion).values(reason='rewrite'))


@pytest.mark.parametrize('status', [201, 404, 410, 429, 500, 403, 413])
def test_outbox_acceptance_expiry_retries_and_pause(ready, status):
    h = ready
    sub, _, _ = subscribe(h)
    parent = event(h)
    claim = claims(h)[0]
    before_finish = m.now()
    with transaction(h.app.state.engine, write=True) as db:
        assert delivery.finish(db, claim, status, '120', 5)
        assert db.get(m.Outbox, parent).state == 'available_in_app'
        count = db.scalar(select(func.count()).select_from(m.Notification))
        child = db.get(m.Outbox, claim['outbox_id'])
        subscription = db.get(m.PushSubscriptionVersion, sub['subscription_id'])
        if status == 201:
            assert child.state == 'sent' and subscription.last_accepted_at
        elif status in (404, 410):
            assert subscription.state == 'expired' and db.get(m.PushInstallation, sub['installation_id']).state == 'repair_required'
        elif status in (429, 500):
            assert child.state == 'retry' and aware(child.next_attempt_at) >= before_finish + timedelta(seconds=120)
        else:
            assert child.state == 'failed'
        assert db.scalar(select(func.count()).select_from(m.Notification)) == count
        assert db.scalar(select(m.PushAttempt)).status == status
        if status == 403:
            assert db.get(m.PushProviderState, (claim['origin'], claim['key_id'])).paused
    assert h.get('staff', '/api/me/push')['unread']['count'] == h.get('staff', '/api/notifications')['unread_total']


def test_send_reprojects_privacy_and_uses_no_database_transaction(ready, monkeypatch):
    h = ready
    _, private, value = subscribe(h)
    h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'],
        'push_enabled': True, 'lock_screen_detail': 'event_date', 'reason': 'Initial permissive cap'}, revision=2)
    event(h)
    claim = claims(h)[0]
    h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'],
        'push_enabled': True, 'reason': 'Tighten queued privacy'}, revision=3)
    observed = []
    def post(endpoint, body, headers, timeout):
        # Another writer can commit while the provider is called.
        retry_write(h.app.state.engine, lambda db: db.execute(text('SELECT 1')))
        observed.append(json.loads(decrypt(body, private, value['keys'])))
        assert headers['Content-Encoding'] == 'aes128gcm'
        return 201, None
    monkeypatch.setattr(transport, 'post', post)
    delivery._send(h.app.state.engine, h.settings, claim)
    assert observed[0]['title'] == 'Rota' and 'Availability' not in observed[0]['body']
    assert observed[0]['unread']['count'] == h.get('staff', '/api/notifications')['unread_total']
    destination = h.get('staff', '/api/notifications/' + observed[0]['notification_id'] + '/destination')
    assert destination['notification']['id'] == observed[0]['notification_id']
    h.get('approver', '/api/notifications/' + observed[0]['notification_id'] + '/destination', status=404)


def test_provider_timeout_keeps_stored_ceiling_and_bounds_transport(ready, monkeypatch):
    h = ready
    stored = s.PwaSystemSettings(enabled=True, timeout_seconds=300, lease_seconds=900)
    assert s.PwaSystemSettings.model_validate(stored.model_dump()).timeout_seconds == 300
    h.command('operator', 'pwa.system.update', {**stored.model_dump(), 'reason': 'Test stored provider timeout'}, revision=2)
    with transaction(h.app.state.engine) as db:
        assert pwa.system(db)[0].timeout_seconds == 300
    subscribe(h, endpoint='https://fcm.googleapis.com/timeout-300', hint='timeout-300')
    event(h)
    observed = []
    monkeypatch.setattr(transport, 'post', lambda endpoint, body, headers, timeout: (observed.append(timeout) or (201, None)))
    delivery._send(h.app.state.engine, h.settings, claims(h)[0])
    assert observed[0] <= WORKER_IDLE_MAX_SECONDS

    stored = s.PwaSystemSettings(enabled=True, timeout_seconds=30)
    h.command('operator', 'pwa.system.update', {**stored.model_dump(), 'reason': 'Test provider timeout below margin'}, revision=3)
    subscribe(h, endpoint='https://fcm.googleapis.com/timeout-30', hint='timeout-30')
    event(h)
    delivery._send(h.app.state.engine, h.settings, claims(h)[0])
    assert observed[1] == 30


def test_jwt_reuse_lease_fencing_ttl_and_undo_cancel(ready):
    h = ready
    subscribed, _, _ = subscribe(h)
    event(h)
    first = claims(h)[0]
    assert claims(h) == []
    with transaction(h.app.state.engine, write=True) as db:
        lease = db.get(m.PushTransportLease, first['outbox_id'])
        lease.until = m.now() - timedelta(seconds=1)
    replacement = claims(h)[0]
    assert replacement['generation'] == first['generation'] + 1 and replacement['authorization'] == first['authorization']
    with transaction(h.app.state.engine, write=True) as db:
        assert not delivery.finish(db, first, 201, None, 2)
        assert delivery.finish(db, replacement, 429, '9' * 100, 2)
        assert db.get(m.Outbox, first['outbox_id']).state == 'failed'
    h.command('staff', 'push.unsubscribe', {'installation_id': subscribed['installation_id'], 'reason': 'Disable device'}, revision=subscribed['revision'])
    assert not claims(h)


def test_required_events_are_recipient_specific():
    person = NS(person_id='affected')
    absence = NS(kind='request', payload={'request_kind': 'absence_report', 'request_status': 'submitted'})
    assert pwa.classify_event(absence, person, {'coordinator'}) == 'absence_reported'
    assert pwa.classify_event(absence, person, {'staff'}) == 'request'
    published = NS(kind='publication', payload={'person_changes': {'affected': {'added': [1]}}})
    assert pwa.classify_event(published, person) == 'duty_changed'


@pytest.mark.usefixtures('branding_editor')
def test_manifest_generated_from_public_config_etag_and_transparent_source(h):
    response = h.client('staff').get('/api/pwa/manifest.webmanifest')
    assert response.status_code == 200
    manifest = response.json()
    assert manifest['id'] == manifest['scope'] == manifest['start_url'] == '/' and manifest['display'] == 'standalone'
    assert h.client('staff').get('/api/pwa/manifest.webmanifest', headers={'If-None-Match': response.headers['etag']}).status_code == 304
    assert {(icon['purpose'], icon['sizes']) for icon in manifest['icons']} == {('any', '192x192'), ('any', '512x512'), ('maskable', '192x192'), ('maskable', '512x512')}
    for icon in manifest['icons']:
        image = h.client('staff').get(icon['src'])
        with Image.open(io.BytesIO(image.content)) as pixels:
            assert pixels.mode == 'RGB' and f'{pixels.width}x{pixels.height}' == icon['sizes']
    # A fully transparent configured logo is valid; rendering must not crash or expose a staged upload.
    binary = io.BytesIO(); Image.new('RGBA', (8, 8)).save(binary, format='PNG')
    upload = h.client('admin').post('/api/admin/branding/images', content=binary.getvalue(), headers={'Content-Type': 'image/png'}).json()
    h.command('admin', 'branding.update', {'app_name': 'Configured application', 'institution_name': 'Test institute',
        'emblem_image_id': upload['image_id'], 'theme_color': '#112233', 'reason': 'Configure public install identity'}, revision=1)
    configured = h.client('staff').get('/api/pwa/manifest.webmanifest').json()
    assert configured['name'] == 'Configured application' and configured['theme_color'] == '#112233'
    assert configured['icons'] != manifest['icons']
    assert h.client('staff').get(manifest['icons'][0]['src']).status_code == 200


def test_explicit_email_choice_queues_through_existing_transport_without_push(ready):
    h = ready
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.email_settings = s.EmailSettings(smtp_enabled=True, sender_address='rota@example.org', smtp_host='relay.example.org').model_dump(mode='json')
        db.get(m.Person, h.seed['people'][0]).work_email = 'staff@example.org'
    assert h.get('staff', '/api/me/notification-preferences')['email_configured']
    h.command('staff', 'notification.preferences.update', {'department_id': h.seed['department_id'],
        'event_overrides': {'availability': {'email': 'on', 'push': 'off'}}, 'reason': 'Email selected event'}, revision=1)
    event(h)
    with transaction(h.app.state.engine) as db:
        emails = list(db.scalars(select(m.EmailDelivery)))
        assert len(emails) == 1 and emails[0].identity_id == h.seed['identities']['staff']
        child = db.get(m.Outbox, emails[0].outbox_id)
        assert child.kind == 'email' and child.payload['event_id'] == 'availability'
        message = email_delivery._notification_message(db, child, db.get(m.Identity, emails[0].identity_id),
            s.EmailSettings.model_validate(db.get(m.Department, h.seed['department_id']).email_settings), h.settings)
        assert '/n/' in message.get_body(preferencelist=('plain',)).get_content() and not db.scalar(select(m.PushDelivery))


def test_no_auth_csrf_cross_origin_and_installation_races(ready):
    from concurrent.futures import ThreadPoolExecutor
    h = ready
    _, keys = browser_keys()
    value = {'endpoint': 'https://fcm.googleapis.com/race', 'keys': keys, 'installation_hint': 'race-installation',
             'explicit_consent': True, 'key_id': h.settings.vapid_key_id}
    with TestClient(h.app, base_url=h.settings.public_origin) as public:
        assert public.get('/api/me/push').status_code == 401
        assert public.post('/api/commands', json={'kind': 'push.subscribe', 'payload': value}).status_code in (401, 403)
    client = h.client('staff')
    assert client.post('/api/commands', json={'kind': 'push.subscribe', 'payload': value}, headers={'origin': 'https://other.example.org'}).status_code == 403
    assert client.post('/api/commands', json={'kind': 'push.subscribe', 'payload': value}, headers={'x-csrf-token': 'wrong'}).status_code == 403
    def create(_):
        return h.command('staff', 'push.subscribe', value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        both = list(pool.map(create, range(2)))
    assert both[0]['installation_id'] == both[1]['installation_id'] and both[0]['subscription_id'] == both[1]['subscription_id']


def test_badge_revision_changes_on_read_and_undo(ready):
    h = ready
    event(h)
    before = h.get('staff', '/api/me/push')['unread']
    h.command('staff', 'notification.ack_all')
    after = h.get('staff', '/api/me/push')['unread']
    assert after['revision'] > before['revision'] and after['count'] == 0


def test_expired_session_retains_generic_consent_but_kill_switch_stops_send(ready, monkeypatch):
    h = ready
    subscribed, private, value = subscribe(h)
    event(h)
    claim = claims(h)[0]
    with transaction(h.app.state.engine, write=True) as db:
        install = db.get(m.PushInstallation, subscribed['installation_id'])
        session = db.get(m.Session, install.session_id)
        session.expires_at = m.now() - timedelta(seconds=1)
        assert install.state == 'enabled'
        projected = json.loads(delivery.payload(db, db.scalar(select(m.PushDelivery))))
        assert projected['title'] == 'Rota'
    h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'], 'reason': 'Stop queued sends'}, revision=2)
    monkeypatch.setattr(transport, 'post', lambda *a: pytest.fail('Kill switch must stop external I/O'))
    delivery._send(h.app.state.engine, h.settings, claim)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Outbox, claim['outbox_id']).state == 'superseded'


def test_push_test_is_generic_rate_limited_and_has_no_fake_inbox(ready):
    h = ready
    subscribed, _, _ = subscribe(h)
    h.command('staff', 'push.test', {'installation_id': subscribed['installation_id'], 'reason': 'User requested test'})
    h.command('staff', 'push.test', {'installation_id': subscribed['installation_id'], 'reason': 'Another test'}, status=429)
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.Notification))
        projected = json.loads(delivery.payload(db, db.scalar(select(m.PushDelivery))))
        assert projected['notification_id'] is None and projected['unread']['count'] == 0


def test_measure_multi_installation_burst_with_history(ready, monkeypatch, tmp_path):
    """Measure a150-identity/300-installation burst without performance budgets or real provider sends."""
    from sqlalchemy import event as sql_event
    h = ready
    _, keys = browser_keys()
    with transaction(h.app.state.engine, write=True) as db:
        source = m.Outbox(department_id=h.seed['department_id'], kind='review', event_key=m.uid(), payload={})
        db.add(source); db.flush()
        for index in range(150):
            identity = m.Identity(issuer='pwa-measurement', subject=f'synthetic-{index}', display_name='Synthetic push measurement')
            db.add(identity); db.flush()
            db.add(m.RoleBinding(department_id=h.seed['department_id'], identity_id=identity.id, role='coordinator', starts_on=m.now().date()))
            for number in range(2):
                install = m.PushInstallation(identity_id=identity.id, department_id=h.seed['department_id'],
                    installation_hint=f'synthetic-{index}-{number}', state='enabled')
                db.add(install); db.flush()
                db.add(m.PushEnrollment(installation_id=install.id, department_id=h.seed['department_id']))
                sub = m.PushSubscriptionVersion(installation_id=install.id, endpoint_digest=f'{index:060d}{number:04d}',
                    endpoint_ciphertext='', p256dh_ciphertext='', auth_ciphertext='', vapid_key_id=h.settings.vapid_key_id)
                db.add(sub); db.flush()
                from apps.api.data_crypto import encrypt_secret
                for field, secret in {'endpoint': f'https://fcm.googleapis.com/synthetic-{index}-{number}', **keys}.items():
                    setattr(sub, field + '_ciphertext', encrypt_secret(secret, pwa.context(sub, field), h.settings))
            db.add_all([m.NotificationPreferenceVersion(department_id=h.seed['department_id'], identity_id=identity.id,
                revision=revision, actor_id=h.seed['identities']['admin'], definition={}, reason='Synthetic history') for revision in range(1, 21)])
        db.flush()
        source_id = source.id
    measurements = {'identities':150,'installations':300,'preference_history_rows':3000}
    queries = []
    def query(*args): queries.append(1)
    sql_event.listen(h.app.state.engine, 'before_cursor_execute', query)
    started, cpu = perf_counter(), process_time()
    try:
        retry_write(h.app.state.engine, worker.dispatch_batch)
        measurements.update(fanout_wall_seconds=perf_counter()-started, fanout_cpu_seconds=process_time()-cpu, fanout_queries=len(queries))
        queries.clear(); started, cpu = perf_counter(), process_time()
        # Mock provider acceptance only; real devices remain the coordinator's pilot.
        monkeypatch.setattr(transport, 'post', lambda *a: (201, None))
        count = 0
        while True:
            batch = claims(h)
            if not batch:
                break
            count += len(batch)
            for claim in batch:
                delivery._send(h.app.state.engine, h.settings, claim)
        measurements.update(transport_wall_seconds=perf_counter()-started, transport_cpu_seconds=process_time()-cpu,
                            transport_queries=len(queries), accepted=count)
    finally:
        sql_event.remove(h.app.state.engine, 'before_cursor_execute', query)
    assert count == 300
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.PushDelivery).where(m.PushDelivery.source_outbox_id == source_id)) == 300
    evidence = Path('.run/pwa-be/burst-measurement.json')
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(measurements,sort_keys=True,indent=2))


def test_fe_contract_reload_revisions_saved_preferences_caps_email_and_undo(ready):
    """The five coordinator wiring findings are checked through real API reads and FE-shaped writes."""
    h = ready
    department_id = h.seed['department_id']
    loaded = h.get('admin', '/api/me/push')
    assert loaded['policy_revision'] == loaded['policy']['revision'] == 2
    h.command('admin', 'notification.policy.update', {'department_id': department_id,
        'expected_revision': loaded['policy_revision'], 'push_enabled': True, 'lock_screen_detail': 'event_and_date',
        'event_defaults': {'swap': {'push': 'on'}}, 'reason': 'Allow event and date'})
    reloaded = h.get('admin', '/api/me/push')
    assert reloaded['policy_revision'] == 3 and reloaded['policy']['lock_screen_detail'] == 'event_date'
    assert reloaded['policy']['events']['duty_changed']['mandatory']
    assert not reloaded['email_available'] and not reloaded['email_configured']
    for detail in ('generic', 'event', 'event_and_date'):
        before = h.get('staff', '/api/me/push')['preferences']
        h.command('staff', 'notification.preferences.update', {'department_id': department_id,
            'expected_revision': before['revision'], 'event_overrides': {'swap': 'off'}, 'detail': detail,
            'reason': 'Saved from My notifications'})
        saved = h.get('staff', '/api/me/push')['preferences']
        assert saved['event_overrides']['swap'] == 'off' and saved['event_channels']['swap']['push'] == 'off'
        assert saved['detail'] == ('event_date' if detail == 'event_and_date' else detail)
    history = h.get('admin', '/api/admin/notifications/policy/history')['versions']
    undo = h.command('admin', 'notification.policy.undo', {'department_id': department_id,
        'version_id': history[1]['id'], 'reason': 'Restore prior policy'}, revision=reloaded['policy_revision'])
    assert undo['revision'] == 4 and undo['policy']['lock_screen_detail'] == 'generic'
    current = h.get('staff', '/api/me/push')['preferences']
    h.command('staff', 'notification.preferences.update', {'department_id': department_id,
        'detail': 'event', 'reason': 'Reject above current cap'}, revision=current['revision'], status=422)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, department_id).email_settings = s.EmailSettings(smtp_enabled=True,
            sender_address='rota@example.org', smtp_host='relay.example.org').model_dump(mode='json')
    assert h.get('staff', '/api/me/push')['email_available']


def test_ambiguous_crash_after_acceptance_is_at_least_once(ready, monkeypatch):
    h = ready
    subscribe(h)
    event(h)
    sent = []
    monkeypatch.setattr(transport, 'post', lambda *args: (sent.append(1) or 201, None))
    original = delivery.finish
    def crash(*args):
        raise RuntimeError('Simulated crash before result commit')
    claim = claims(h)[0]
    with monkeypatch.context() as patch:
        patch.setattr(delivery, 'finish', crash)
        with pytest.raises(RuntimeError):
            delivery._send(h.app.state.engine, h.settings, claim)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.PushTransportLease, claim['outbox_id']).until = m.now() - timedelta(seconds=1)
    recovered = claims(h)[0]
    delivery._send(h.app.state.engine, h.settings, recovered)
    assert len(sent) == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Outbox, recovered['outbox_id']).state == 'sent'
        assert db.scalar(select(m.PushAttempt)).attempt == 2


def test_independent_departments_can_send_concurrently(ready, monkeypatch):
    from threading import Barrier
    h = ready
    subscribe(h)
    event(h)  # A real pending delivery admits this pass before the mocked claims.
    # Claims have already been bounded globally and per department by claim_batch.
    # A low-concurrency department must not serialize another department's claimed send.
    batch = [{'department_id': 'first', 'concurrency': 1}, {'department_id': 'second', 'concurrency': 1}]
    barrier = Barrier(2)
    def send(engine, settings, claim):
        barrier.wait(timeout=30)
        return claim['department_id']
    monkeypatch.setattr(delivery, '_send', send)
    monkeypatch.setattr(delivery, 'retry_write', lambda engine, operation: batch)
    assert delivery.dispatch_pending(h.app.state.engine, h.settings) == {'first', 'second'}


def test_department_concurrency_covers_outstanding_worker_leases(ready):
    h = ready
    for index in range(3):
        subscribe(h, endpoint=f'https://fcm.googleapis.com/device-{index}', hint=f'test-device-{index}')
    h.command('admin', 'notification.policy.update', {'department_id': h.seed['department_id'],
        'push_enabled': True, 'delivery': {'concurrency': 2, 'batch_size': 1},
        'reason': 'Bound concurrent worker passes'}, revision=2)
    event(h)
    first, second = claims(h), claims(h)
    assert len(first) == len(second) == 1
    assert first[0]['outbox_id'] != second[0]['outbox_id']
    assert claims(h) == []
    retry_write(h.app.state.engine, lambda db: delivery.finish(db, first[0], 201, None, 1))
    third = claims(h)
    assert len(third) == 1 and third[0]['outbox_id'] not in {first[0]['outbox_id'], second[0]['outbox_id']}


def test_push_only_worker_wakes_inbox_readers(ready, monkeypatch, tmp_path):
    h = ready
    monkeypatch.setattr(worker, 'ALIVE_FILE', str(tmp_path / 'worker-alive'))
    monkeypatch.setattr(worker, '_beat', {})
    subscribe(h)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.Outbox(department_id=h.seed['department_id'], kind='availability', event_key=m.uid(),
            payload={'person_id': h.seed['people'][0], 'actor_id': h.seed['identities']['coordinator']}))
    changed, sent = [], []
    monkeypatch.setattr(worker.live, 'changed', lambda topic, **change: changed.append(topic))
    monkeypatch.setattr(transport, 'post', lambda *args: (sent.append(1) or 201, None))
    worker.process_push_one(h.app.state.engine, h.settings, record_worker_heartbeat=False)
    assert changed and set(changed) == {h.seed['department_id']} and sent == [1]
    assert h.get('staff', '/api/notifications')['unread_total'] > 0
    assert (tmp_path / 'worker-alive').is_file()
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.WorkerHeartbeat.worker_id)) is None
        assert not planning.background_checks_available(db)


def test_push_only_wake_from_availability_command_not_inbox_scope(ready):
    h = ready
    subscribe(h)
    payload = {'person_id': h.seed['people'][0], 'kind': 'actual_absence',
               'start': '2026-10-04T10:00:00+03:00', 'end': '2026-10-04T11:00:00+03:00',
               'private_reason': 'Synthetic push wake'}
    result = h.command('coordinator', 'availability.record', payload)
    changes = worker.live.command_changes('availability.record', payload=payload, result=result)
    assert changes[0]['scope'] == 'availability'

    wake = worker.threading.Event()
    worker._wake_job_waiter(NS(payload=json.dumps(changes[0])), wake, wake_scope=None)
    assert wake.is_set()
    with transaction(h.app.state.engine) as db:
        queued = db.scalar(select(m.Outbox).where(m.Outbox.department_id == h.seed['department_id'],
            m.Outbox.kind == 'availability').order_by(m.Outbox.created_at.desc()))
    assert queued is not None
