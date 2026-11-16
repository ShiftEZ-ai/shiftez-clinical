"""Delegated mailbox consent never returns or persists plaintext token material."""
import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import logging
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from apps.api import data_crypto, models as m
from apps.api.auth import authenticate, digest
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.logging_setup import CallbackSecretsFilter, JsonFormatter
from apps.api.services import mailboxes
from apps.api.settings import Settings
from packages.contracts import schemas as s
from packages.domain.time import aware
from .governance_support import identity


REFRESH = "synthetic-private-refresh-token"
ACCESS = "synthetic-private-access-token"
CLIENT_SECRET = "synthetic-client-secret"
TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"


@pytest.mark.parametrize('cloud, authority, graph', [
    ('global', 'https://login.microsoftonline.com', 'https://graph.microsoft.com'),
    ('us_gov', 'https://login.microsoftonline.us', 'https://graph.microsoft.us'),
    ('china', 'https://login.chinacloudapi.cn', 'https://microsoftgraph.chinacloudapi.cn'),
])
def test_microsoft_secrets_only_go_to_contract_cloud_endpoints(h, mailbox, monkeypatch, cloud, authority, graph):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, mailbox.department_id)
        department.email_settings = {**department.email_settings, 'm365_cloud': cloud}
    with transaction(h.app.state.engine, write=True) as db:
        reply = mailboxes.start(db, mailbox, h.settings)
    url = urlsplit(reply['authorization_url'])
    assert f'{url.scheme}://{url.netloc}' == authority
    query = parse_qs(url.query)
    assert query['scope'] == ['offline_access ' + graph + '/Mail.Send']
    def inspect(url, kwargs):
        assert url == authority + f'/{TENANT}/oauth2/v2.0/token'
        assert kwargs['data']['client_secret'] == CLIENT_SECRET
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
    tokens(monkeypatch, inspect=inspect)
    with transaction(h.app.state.engine, write=True) as db:
        assert mailboxes.callback(db, mailbox, h.settings, query['state'][0], 'synthetic-code') == {'connected': True}
        assert mailboxes.access_token(db, mailbox.identity_id, mailbox.department_id, h.settings) == ACCESS


@pytest.mark.parametrize('field, value', [
    ('m365_authority_url', 'https://capture.example.test'),
    ('m365_graph_url', 'https://capture.example.test/v1.0'),
    ('m365_cloud', 'https://capture.example.test'),
])
def test_admin_cannot_send_secrets_to_an_unlisted_microsoft_endpoint(h, mailbox, monkeypatch, field, value):
    query = connect(h, mailbox, monkeypatch)
    monkeypatch.setattr(mailboxes.httpx, 'post', lambda *args, **kwargs: pytest.fail('Must not send a client secret'))
    current = h.get('admin', '/api/email/settings')
    h.command('admin', 'email.settings', {'value': {**current['value'], field: value},
        'reason': 'Synthetic endpoint redirection'}, h.seed['department_id'], current['revision'], status=422)
    assert h.get('admin', '/api/email/settings')['value'] == current['value']
    # Also fail closed for unsupported settings already in storage, before either token grant.
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, mailbox.department_id)
        department.email_settings = {**department.email_settings, field: value}
        with pytest.raises(ValueError):
            mailboxes.callback(db, mailbox, h.settings, query['state'][0], 'synthetic-code')
        with pytest.raises(ValueError):
            mailboxes.access_token(db, mailbox.identity_id, mailbox.department_id, h.settings)


@pytest.mark.parametrize('value', [
    {'m365_authority_url': 'https://login.microsoftonline.com', 'm365_graph_url': 'https://graph.microsoft.com/v1.0'},
    {'m365_authority_url': 'https://login.microsoftonline.com/', 'm365_graph_url': 'https://graph.microsoft.com/v1.0/'},
    {'m365_authority_url': 'https://login.microsoftonline.us', 'm365_graph_url': 'https://graph.microsoft.com/v1.0'},
    {'m365_authority_url': 'https://login.microsoftonline.com', 'm365_graph_url': 'https://graph.microsoft.com/v1.0',
     'm365_cloud': 'china'},
])
def test_free_form_microsoft_endpoint_fields_fail_closed(value):
    with pytest.raises(ValueError):
        s.EmailSettings.model_validate(value)


@pytest.fixture
def mailbox(h, monkeypatch):
    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    monkeypatch.setenv("FMUC_DATA_KEY", key)
    object.__setattr__(h.settings, "fmuc_data_key", key)
    monkeypatch.setenv("FMUC_M365_CLIENT_SECRET", CLIENT_SECRET)
    client = h.client("coordinator")
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies["rota_session"])
        db.get(m.Department, actor.department_id).email_settings = {
            "m365_enabled": True, "m365_tenant_id": TENANT, "m365_client_id": CLIENT}
    return actor


def begin(h, actor):
    with transaction(h.app.state.engine, write=True) as db:
        reply = mailboxes.start(db, actor, h.settings)
    return parse_qs(urlsplit(reply["authorization_url"]).query)


def tokens(monkeypatch, result=None, status=200, inspect=None):
    def post(url, **kwargs):
        if inspect:
            inspect(url, kwargs)
        return httpx.Response(status, json=result or {"access_token": ACCESS, "refresh_token": REFRESH},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(mailboxes.httpx, "post", post)


def connect(h, actor, monkeypatch):
    query = begin(h, actor)
    tokens(monkeypatch)
    with transaction(h.app.state.engine, write=True) as db:
        assert mailboxes.callback(db, actor, h.settings, query["state"][0], "synthetic-code") == {"connected": True}
    return query


def test_shared_encryption_binds_secret_to_owner_and_detects_tampering():
    settings = Settings(fmuc_data_key=base64.urlsafe_b64encode(bytes(range(32))).decode())
    encrypted = data_crypto.encrypt_secret(REFRESH, "department:owner", settings)
    assert encrypted != data_crypto.encrypt_secret(REFRESH, "department:owner", settings)
    assert REFRESH not in encrypted
    assert data_crypto.decrypt_secret(encrypted, "department:owner", settings) == REFRESH
    for wrong, context in ((encrypted, "another-owner"), ("invalid", "department:owner")):
        with pytest.raises(RuntimeError, match="cannot be decrypted") as problem:
            data_crypto.decrypt_secret(wrong, context, settings)
        assert REFRESH not in str(problem.value)
    raw = bytearray(base64.urlsafe_b64decode(encrypted))
    raw[-1] ^= 1
    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        data_crypto.decrypt_secret(base64.urlsafe_b64encode(raw).decode(), "department:owner", settings)


@pytest.mark.parametrize("key", ["", "private-invalid-key", base64.b64encode(b"too-short").decode()])
def test_shared_encryption_refuses_invalid_keys_without_echoing_them(key):
    with pytest.raises(RuntimeError, match="32-byte key") as problem:
        data_crypto.encrypt_secret(REFRESH, "owner", Settings(fmuc_data_key=key))
    assert REFRESH not in str(problem.value)
    if key:
        assert key not in str(problem.value)


def test_start_is_session_bound_and_pkce_verifier_is_encrypted(h, mailbox):
    query = begin(h, mailbox)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["offline_access https://graph.microsoft.com/Mail.Send"]
    assert query["prompt"] == ["select_account"]
    assert query["redirect_uri"] == [h.settings.public_origin + "/api/email/microsoft/callback"]
    assert query["code_challenge_method"] == ["S256"]
    with transaction(h.app.state.engine) as db:
        flow = db.scalar(select(m.MailboxOAuthFlow))
        assert flow.identity_id == mailbox.identity_id and flow.session_id == mailbox.session_id
        assert flow.department_id == mailbox.department_id and flow.state_hash == digest(query["state"][0])
        verifier = data_crypto.decrypt_secret(flow.encrypted_verifier,
            mailboxes._context("oauth-verifier", mailbox.identity_id, mailbox.department_id), h.settings)
        assert verifier not in flow.encrypted_verifier
        assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()]
        assert query["state"][0] != flow.state_hash


def test_start_suggests_only_the_authenticated_actors_account(h, mailbox):
    with transaction(h.app.state.engine, write=True) as db:
        account = db.scalar(select(m.Account).where(m.Account.identity_id == mailbox.identity_id))
        if account is None:
            account = m.Account(identity_id=mailbox.identity_id, username="synthetic-mail-sender")
            db.add(account)
        account.email = "synthetic-organiser@example.org"
    assert begin(h, mailbox)["login_hint"] == ["synthetic-organiser@example.org"]


def test_callback_and_rotation_keep_tokens_out_of_safe_results_and_logs(h, mailbox, monkeypatch, caplog):
    query = begin(h, mailbox)
    def inspect(url, kwargs):
        assert f"/{TENANT}/oauth2/v2.0/token" in url
        assert kwargs["follow_redirects"] is False
        values = kwargs["data"]
        assert values["client_secret"] == CLIENT_SECRET and values["client_id"] == CLIENT
        assert values["grant_type"] == "authorization_code" and values["code"] == "synthetic-code"
        assert len(values["code_verifier"]) >= 43
    tokens(monkeypatch, inspect=inspect)
    with transaction(h.app.state.engine, write=True) as db:
        reply = mailboxes.callback(db, mailbox, h.settings, query["state"][0], "synthetic-code")
        assert reply == {"connected": True}
        assert mailboxes.status(db, mailbox) == {"connected": True}
        connection = db.scalar(select(m.MailboxConnection))
        assert connection.identity_id == mailbox.identity_id
        assert REFRESH not in connection.encrypted_refresh_token
        encrypted = connection.encrypted_refresh_token
    def refreshed(url, kwargs):
        assert kwargs["data"]["grant_type"] == "refresh_token"
        assert kwargs["data"]["refresh_token"] == REFRESH
    tokens(monkeypatch, {"access_token": ACCESS, "refresh_token": "synthetic-rotated-refresh"}, inspect=refreshed)
    with transaction(h.app.state.engine, write=True) as db:
        assert mailboxes.access_token(db, mailbox.identity_id, mailbox.department_id, h.settings) == ACCESS
        connection = db.scalar(select(m.MailboxConnection))
        assert connection.encrypted_refresh_token != encrypted
        assert data_crypto.decrypt_secret(connection.encrypted_refresh_token,
            mailboxes._context("mailbox", mailbox.identity_id, mailbox.department_id), h.settings) == "synthetic-rotated-refresh"
    for secret in (REFRESH, ACCESS, CLIENT_SECRET, "synthetic-rotated-refresh"):
        assert secret not in caplog.text


@pytest.mark.parametrize("invalid", ["used", "expired", "other_session", "configuration_changed", "unknown_state"])
def test_callback_rejects_stale_or_unbound_state_before_contacting_provider(h, mailbox, monkeypatch, invalid):
    query = begin(h, mailbox)
    actor = mailbox
    with transaction(h.app.state.engine, write=True) as db:
        flow = db.scalar(select(m.MailboxOAuthFlow))
        if invalid == "used":
            flow.used = True
        elif invalid == "expired":
            flow.expires_at = m.now() - timedelta(seconds=1)
        elif invalid == "other_session":
            session = m.Session(identity_id=mailbox.identity_id, department_id=mailbox.department_id,
                token_hash=digest("another-session"), csrf_hash=digest("another-csrf"), expires_at=m.now() + timedelta(hours=1))
            db.add(session)
            db.flush()
            actor = replace(mailbox, session_id=session.id)
        elif invalid == "configuration_changed":
            department = db.get(m.Department, mailbox.department_id)
            department.email_settings = {**department.email_settings, "m365_client_id": TENANT}
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid OAuth state must not contact Microsoft")
    monkeypatch.setattr(mailboxes.httpx, "post", forbidden)
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as problem:
            mailboxes.callback(db, actor, h.settings,
                "unknown-state" if invalid == "unknown_state" else query["state"][0], "synthetic-code")
        assert problem.value.code == "MAILBOX_STATE"


def test_provider_failure_is_generic_and_consumes_the_flow(h, mailbox, monkeypatch, caplog):
    query = begin(h, mailbox)
    tokens(monkeypatch, {"error": "invalid_grant", "error_description": REFRESH + CLIENT_SECRET}, 400)
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem) as problem:
            mailboxes.callback(db, mailbox, h.settings, query["state"][0], "synthetic-code")
        assert problem.value.code == "MAILBOX_UNAVAILABLE"
        assert problem.value.details == {"draft_available": True}
        assert REFRESH not in str(problem.value) and CLIENT_SECRET not in str(problem.value)
    with transaction(h.app.state.engine, write=True) as db:
        assert db.scalar(select(m.MailboxOAuthFlow)).used
        assert mailboxes.status(db, mailbox) == {"connected": False}
        with pytest.raises(Problem) as replay:
            mailboxes.callback(db, mailbox, h.settings, query["state"][0], "synthetic-code")
        assert replay.value.code == "MAILBOX_STATE"
    assert REFRESH not in caplog.text and CLIENT_SECRET not in caplog.text


def test_callback_requires_offline_consent(h, mailbox, monkeypatch):
    query = begin(h, mailbox)
    tokens(monkeypatch, {"access_token": ACCESS})
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem, match="offline"):
            mailboxes.callback(db, mailbox, h.settings, query["state"][0], "synthetic-code")
        assert mailboxes.status(db, mailbox) == {"connected": False}


@pytest.mark.parametrize("disabled", ["channel", "identity", "role", "client"])
def test_refresh_never_sends_using_disabled_or_changed_authority(h, mailbox, monkeypatch, disabled):
    connect(h, mailbox, monkeypatch)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, mailbox.department_id)
        if disabled == "channel":
            department.email_settings = {**department.email_settings, "m365_enabled": False}
        elif disabled == "identity":
            db.get(m.Identity, mailbox.identity_id).disabled = True
        elif disabled == "role":
            for role in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == mailbox.identity_id)):
                role.revoked = True
        else:
            department.email_settings = {**department.email_settings, "m365_client_id": TENANT}
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled or changed authority must not contact Microsoft")
    monkeypatch.setattr(mailboxes.httpx, "post", forbidden)
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem):
            mailboxes.access_token(db, mailbox.identity_id, mailbox.department_id, h.settings)
        if disabled in {"identity", "role"}:
            assert db.scalar(select(m.MailboxConnection)) is None


def test_disconnect_removes_tokens_and_pending_sign_in(h, mailbox, monkeypatch):
    connect(h, mailbox, monkeypatch)
    begin(h, mailbox)
    with transaction(h.app.state.engine, write=True) as db:
        assert mailboxes.disconnect(db, mailbox) == {"connected": False}
        assert db.scalar(select(m.MailboxConnection)) is None
        assert db.scalar(select(m.MailboxOAuthFlow)) is None


@pytest.mark.parametrize("revocation", ["identity", "role"])
def test_prune_drops_consent_when_access_ends(h, mailbox, monkeypatch, revocation):
    connect(h, mailbox, monkeypatch)
    begin(h, mailbox)
    with transaction(h.app.state.engine, write=True) as db:
        if revocation == "identity":
            db.get(m.Identity, mailbox.identity_id).disabled = True
        else:
            for role in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == mailbox.identity_id)):
                role.ends_on = m.now().date()
        db.flush()
        assert mailboxes.prune(db) == 1
        db.flush()
        assert db.scalar(select(m.MailboxConnection)) is None
        assert db.scalar(select(m.MailboxOAuthFlow)) is None


def test_missing_crypto_secret_or_client_secret_never_starts_consent(h, mailbox, monkeypatch):
    for name in ("FMUC_DATA_KEY", "FMUC_M365_CLIENT_SECRET"):
        with monkeypatch.context() as missing:
            missing.delenv(name)
            original_key = h.settings.fmuc_data_key
            if name == "FMUC_DATA_KEY":
                object.__setattr__(h.settings, "fmuc_data_key", "")
            with transaction(h.app.state.engine, write=True) as db:
                with pytest.raises(Problem, match="email draft"):
                    mailboxes.start(db, mailbox, h.settings)
                assert db.scalar(select(m.MailboxOAuthFlow)) is None
            object.__setattr__(h.settings, "fmuc_data_key", original_key)


@pytest.mark.parametrize("path", ["/auth/callback", "/api/email/microsoft/callback"])
def test_callback_credentials_are_redacted_from_access_logs(path):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1", "GET", path + "?code=private-code&state=private-state", "1.1", 200), None)
    assert CallbackSecretsFilter().filter(record)
    assert len(record.args) == 5
    assert path + "?[redacted]" in record.getMessage()
    assert "private-code" not in JsonFormatter().format(record)
    assert "private-state" not in JsonFormatter().format(record)


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_mailbox_http_access_is_editor_only(h, mailbox, role):
    if role not in h.seed["identities"]:
        identity(h, role, [role])
    editor = role in {"admin", "coordinator"}
    assert h.get(role, "/api/email/microsoft", status=200 if editor else 403).get("connected") is (False if editor else None)
    result = h.command(role, "email.microsoft.connect", {"reason": "Connect own synthetic sending mailbox"},
        status=200 if editor else 403)
    if editor:
        assert result["authorization_url"].startswith("https://login.microsoftonline.com/")
    for secret in (REFRESH, ACCESS, CLIENT_SECRET):
        assert secret not in str(result)


def test_mailbox_http_round_trip_has_no_tokens_and_disconnects(h, mailbox, monkeypatch, caplog):
    started = h.command("coordinator", "email.microsoft.connect", {"reason": "Connect own synthetic mailbox"})
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    tokens(monkeypatch)
    response = h.client("coordinator").get("/api/email/microsoft/callback", params={"state": state, "code": "synthetic-code"})
    assert response.status_code == 200 and response.json() == {"connected": True}
    assert response.headers["cache-control"] == "no-store"
    assert h.get("coordinator", "/api/email/microsoft") == {"connected": True}
    repeated = h.client("coordinator").get("/api/email/microsoft/callback", params={"state": state, "code": "synthetic-code"})
    assert repeated.status_code == 401 and repeated.json()["error"]["code"] == "MAILBOX_STATE"
    result = h.command("coordinator", "email.microsoft.disconnect", {"reason": "Disconnect own synthetic mailbox"})
    assert result["connected"] is False
    assert h.get("coordinator", "/api/email/microsoft") == {"connected": False}
    for secret in (REFRESH, ACCESS, CLIENT_SECRET):
        assert secret not in str(response.json()) + str(result) + caplog.text


def test_mailbox_http_denied_consent_consumes_state_without_remote_exchange(h, mailbox, monkeypatch):
    started = h.command("coordinator", "email.microsoft.connect", {"reason": "Connect own synthetic mailbox"})
    state = parse_qs(urlsplit(started["authorization_url"]).query)["state"][0]
    def forbidden(*args, **kwargs):
        pytest.fail("Denied Microsoft consent must not exchange an authorization code")
    monkeypatch.setattr(mailboxes.httpx, "post", forbidden)
    response = h.client("coordinator").get("/api/email/microsoft/callback", params={"state": state, "error": "access_denied"})
    assert response.status_code == 422 and response.json()["draft_available"] is True
    assert response.json()["error"]["code"] == "MAILBOX_UNAVAILABLE"
    with transaction(h.app.state.engine) as db:
        flow = db.scalar(select(m.MailboxOAuthFlow))
        assert flow.used and flow.encrypted_verifier == ""
    assert h.get("coordinator", "/api/email/microsoft") == {"connected": False}


def test_mailbox_http_callback_cannot_connect_in_another_authenticated_session(h, mailbox, monkeypatch):
    query = begin(h, mailbox)
    def forbidden(*args, **kwargs):
        pytest.fail("Wrong Rota actor must not exchange a code")
    monkeypatch.setattr(mailboxes.httpx, "post", forbidden)
    response = h.client("admin").get("/api/email/microsoft/callback",
        params={"state": query["state"][0], "code": "synthetic-code"})
    assert response.status_code == 401 and response.json()["error"]["code"] == "MAILBOX_STATE"
    assert h.get("coordinator", "/api/email/microsoft") == {"connected": False}


def test_raw_record_serialization_excludes_mailbox_credential_fields(h, mailbox, monkeypatch):
    from apps.api.serialization import record
    connect(h, mailbox, monkeypatch)
    with transaction(h.app.state.engine) as db:
        connection = record(db.scalar(select(m.MailboxConnection)))
        flow = record(db.scalar(select(m.MailboxOAuthFlow)))
        assert "encrypted_refresh_token" not in connection
        assert "encrypted_verifier" not in flow and "state_hash" not in flow


def test_security_cleanup_removes_expired_flow_before_referenced_session(h, mailbox, monkeypatch):
    from apps.worker import main as worker
    connect(h, mailbox, monkeypatch)
    begin(h, mailbox)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Session, mailbox.session_id).expires_at = m.now() - timedelta(days=worker.EXPIRED_SESSION_DAYS + 1)
    worker._pruned.pop(h.app.state.engine, None)
    assert worker.prune_security_state(h.app.state.engine) >= 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Session, mailbox.session_id) is None
        assert db.scalar(select(m.MailboxOAuthFlow)) is None
        # Offline consent survives ordinary session expiry while the editor's department access remains active.
        assert db.scalar(select(m.MailboxConnection)) is not None


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_unlimited_sessions_connect_and_callback_without_losing_live_oauth_flows(request, database, monkeypatch):
    h = request.getfixturevalue(database)
    key = base64.urlsafe_b64encode(bytes(range(32))).decode()
    monkeypatch.setenv('FMUC_DATA_KEY', key)
    object.__setattr__(h.settings, 'fmuc_data_key', key)
    monkeypatch.setenv('FMUC_M365_CLIENT_SECRET', CLIENT_SECRET)
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed['department_id'])
        department.session_idle_unlimited = department.session_max_unlimited = True
        department.email_settings = {'m365_enabled': True, 'm365_tenant_id': TENANT, 'm365_client_id': CLIENT}
    client = h.client('coordinator')
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, client.cookies['rota_session'], settings=h.settings)
        session = db.get(m.Session, actor.session_id)
        assert session.expiry_unlimited and aware(session.expires_at) <= m.now()
    started = h.command('coordinator', 'email.microsoft.connect', {'reason': 'Connect an unlimited session'})
    state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
    with transaction(h.app.state.engine, write=True) as db:
        flow = db.scalar(select(m.MailboxOAuthFlow))
        flow_id = flow.id
        assert mailboxes.prune(db) == 0
        db.flush()
        assert db.get(m.MailboxOAuthFlow, flow_id) is not None
    tokens(monkeypatch)
    response = client.get('/api/email/microsoft/callback', params={'state': state, 'code': 'synthetic-code'})
    assert response.status_code == 200 and response.json() == {'connected': True}
    assert h.get('coordinator', '/api/email/microsoft') == {'connected': True}
    begin(h, actor)
    with transaction(h.app.state.engine, write=True) as db:
        flow = db.scalar(select(m.MailboxOAuthFlow))
        flow.expires_at = m.now() - timedelta(seconds=1)
        mailboxes.prune(db)
        db.flush()
        assert db.scalar(select(m.MailboxOAuthFlow)) is None
        assert db.scalar(select(m.MailboxConnection)) is not None


@pytest.mark.parametrize('state', ['expired', 'revoked-unlimited'])
def test_ended_sessions_cannot_connect_or_callback_and_pending_flows_are_pruned(h, mailbox, monkeypatch, state):
    query = begin(h, mailbox)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.get(m.Session, mailbox.session_id)
        session.expires_at = m.now() - timedelta(seconds=1)
        session.expiry_unlimited = state == 'revoked-unlimited'
        session.revoked = state == 'revoked-unlimited'
    monkeypatch.setattr(mailboxes.httpx, 'post', lambda *args, **kwargs: pytest.fail('Ended sessions must not contact Microsoft'))
    with transaction(h.app.state.engine, write=True) as db:
        for operation in (lambda: mailboxes.start(db, mailbox, h.settings),
                          lambda: mailboxes.callback(db, mailbox, h.settings, query['state'][0], 'synthetic-code')):
            with pytest.raises(Problem) as stopped:
                operation()
            assert stopped.value.code == 'SESSION_EXPIRED'
        mailboxes.prune(db)
        db.flush()
        assert db.scalar(select(m.MailboxOAuthFlow)) is None
