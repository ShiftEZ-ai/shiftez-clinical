"""Browser callbacks redirect safely while retaining correlated server diagnostics and authentication audit."""
from datetime import timedelta
import logging
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select

from apps.api import main, models as m, oidc
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.logging_setup import JsonFormatter
from packages.contracts.schemas import AuthSettings
from .test_signin_oidc import isolated_keys, provider, signing_material  # noqa: F401  shared synthetic IdP fixtures

SENSITIVE = "synthetic-provider-detail-and-token-do-not-disclose"
pytestmark = pytest.mark.usefixtures("public_oidc_dns")


@pytest.fixture
def callback(h, monkeypatch):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id="environment", data=AuthSettings(mode="both", institutional_enabled=True,
            issuer="https://idp.test", client_id="rota", allowed_hosts=["idp.test"],
            institutional_bindings=[{"identity_id": h.seed["identities"]["staff"], "issuer": "https://idp.test", "subject": "hospital-subject"}]
        ).model_dump(mode="json")))
    monkeypatch.setattr(oidc, "document", lambda settings: {"authorization_endpoint": "https://idp.test/authorize"})
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        started = client.get("/auth/start", params={"next": "#planner"}, follow_redirects=False)
        assert started.status_code == 303
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        yield client, state


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_callback_defers_multi_department_session_to_browser_bound_typed_redemption(h, callback, monkeypatch):
    client, state = callback
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Second allowed department (test)')
        db.add(department)
        db.flush()
        db.add(m.RoleBinding(identity_id=h.seed['identities']['staff'], department_id=department.id,
                             role='staff', starts_on=m.now().date() - timedelta(days=1)))
        selected = department.id
    exchanged = []
    def exchange(settings, flow, code):
        exchanged.append(flow['id'])
        return {'sub': 'hospital-subject', 'roles': ['admin']}, None
    monkeypatch.setattr(oidc, 'exchange', exchange)
    response = client.get('/auth/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
    assert response.status_code == 303 and parse_qs(urlparse(response.headers['location']).query) == {'signin_choice': [state]}
    assert 'rota_session' not in client.cookies and 'rota_csrf' not in client.cookies
    assert not any(value.startswith(('rota_session=', 'rota_csrf=')) for value in response.headers.get_list('set-cookie'))
    choices = client.get('/auth/oidc/departments', params={'continuation': state})
    assert choices.status_code == 200 and set(choices.json()) == {'expires_at', 'departments'}
    assert {d['id'] for d in choices.json()['departments']} == {selected, h.seed['department_id']}
    assert choices.headers['cache-control'] == 'no-store'
    body = {'continuation': state, 'department_id': selected}
    assert client.post('/auth/oidc/department', json=body).status_code == 403  # no Origin, even pre-authentication
    assert client.post('/auth/oidc/department', json={**body, 'identity_id': h.seed['identities']['admin']},
                       headers={'origin': h.settings.public_origin}).status_code == 422
    response = client.post('/auth/oidc/department', json=body, headers={'origin': h.settings.public_origin})
    assert response.status_code == 200 and set(response.json()) == {'expires_at', 'display_name', 'return_to'}
    assert response.json()['return_to'] == '/#planner'
    assert client.cookies['rota_session'] and client.cookies['rota_csrf']
    me = client.get('/api/me').json()
    assert me['identity_id'] == h.seed['identities']['staff'] and me['department']['id'] == selected
    assert me['roles'] == ['staff']
    assert client.post('/auth/oidc/department', json=body, headers={'origin': h.settings.public_origin}).status_code == 401
    replay = client.get('/auth/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
    assert replay.headers['location'] == '/?signin_error=expired' and len(exchanged) == 1


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_pending_department_choice_cancel_is_same_origin_and_does_not_reopen_provider_exchange(h, callback, monkeypatch):
    client, state = callback
    with transaction(h.app.state.engine, write=True) as db:
        department = m.Department(organisation_id=h.seed['organisation_id'], name='Second department for cancel (test)')
        db.add(department)
        db.flush()
        db.add(m.RoleBinding(identity_id=h.seed['identities']['staff'], department_id=department.id,
                             role='staff', starts_on=m.now().date() - timedelta(days=1)))
    monkeypatch.setattr(oidc, 'exchange', lambda *args: ({'sub': 'hospital-subject'}, None))
    response = client.get('/auth/callback', params={'state': state, 'code': 'synthetic-code'}, follow_redirects=False)
    assert 'signin_choice=' in response.headers['location']
    body = {'continuation': state}
    assert client.post('/auth/oidc/cancel', json=body).status_code == 403
    assert client.get('/auth/oidc/departments', params=body).status_code == 200
    response = client.post('/auth/oidc/cancel', json=body, headers={'origin': h.settings.public_origin})
    assert response.status_code == 200 and response.json() == {'cancelled': True}
    assert 'rota_oidc_browser' not in client.cookies and 'rota_oidc_return' not in client.cookies
    assert client.get('/auth/oidc/departments', params=body).status_code == 401
    with transaction(h.app.state.engine) as db:
        flow = db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(state)))
        assert flow.used and flow.verified_identity_id == h.seed['identities']['staff'] and flow.completed_session_id is None
        assert db.scalar(select(m.Session).where(m.Session.auth_method == 'oidc')) is None


def assert_failure(h, response, code, caplog):
    assert response.status_code == 303 and response.headers["location"] == "/?signin_error=" + code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert not response.content
    assert SENSITIVE not in str(response.headers)
    assert not any(value.startswith(("rota_session=", "rota_csrf=")) for value in response.headers.get_list("set-cookie"))
    reference = response.headers["x-correlation-id"]
    diagnostics = [record for record in caplog.records if record.message == "oidc_callback_failed"]
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.correlation_id == reference and diagnostic.signin_error == code
    assert diagnostic.exception_type and diagnostic.phase and diagnostic.traceback
    assert SENSITIVE not in "\n".join(JsonFormatter().format(record) for record in caplog.records)
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuthLoginEvent).where(m.AuthLoginEvent.correlation_id == reference))
        assert event is not None and event.outcome == diagnostic.error_code and event.session_id is None
        assert db.scalar(select(m.Session).where(m.Session.auth_method == "oidc")) is None
    return diagnostic


@pytest.mark.parametrize(("failure", "public", "outcome"), [
    ("denied", "provider_denied", "OIDC_PROVIDER_DENIED"),
    ("provider_error", "failed", "OIDC_PROVIDER_ERROR"),
    ("state_mismatch", "expired", "OIDC_STATE"),
    ("missing_state", "expired", "OIDC_STATE"),
    ("expired_state", "expired", "OIDC_STATE"),
    ("used_state", "expired", "OIDC_STATE"),
    ("missing_browser", "expired", "OIDC_STATE"),
    ("missing_code", "failed", "OIDC_CALLBACK"),
    ("exchange", "failed", "OIDC_CALLBACK_FAILED"),
    ("nonce", "failed", "OIDC_NONCE"),
    ("issuer", "failed", "OIDC_TOKEN"),
    ("configuration", "config", "AUTH_CONFIG_CHANGED"),
    ("key", "config", "AUTH_KEY_UNAVAILABLE"),
    ("disabled", "config", "OIDC_UNCONFIGURED"),
])
def test_callback_errors_redirect_and_retain_safe_diagnostics_and_audit(h, callback, monkeypatch, caplog,
                                                                       failure, public, outcome):
    client, state = callback
    params = {"state": state, "code": SENSITIVE, "error_description": SENSITIVE, "id_token": SENSITIVE}
    if failure in {"denied", "provider_error"}:
        params.pop("code")
        params["error"] = "access_denied" if failure == "denied" else SENSITIVE
    elif failure == "state_mismatch":
        params.update(state=SENSITIVE, error="access_denied")
    elif failure == "missing_state":
        params.pop("state")
    elif failure == "missing_browser":
        client.cookies.delete("rota_oidc_browser")
    elif failure == "missing_code":
        params.pop("code")
    elif failure in {"expired_state", "used_state"}:
        with transaction(h.app.state.engine, write=True) as db:
            flow = db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(state)))
            if failure == "expired_state":
                flow.expires_at = m.now() - timedelta(seconds=1)
            else:
                flow.used = True
    elif failure in {"configuration", "key", "disabled"}:
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.AuthConfiguration, "environment")
            if failure == "configuration":
                row.revision += 1
            elif failure == "key":
                row.secret_ciphertext = "not-a-valid-encrypted-envelope"
            else:
                row.data = row.data | {"mode": "password_only", "institutional_enabled": False}

    def reject_exchange(*args):
        if failure == "exchange":
            raise httpx.ConnectError(SENSITIVE)
        if failure in {"nonce", "issuer"}:
            raise Problem(401, outcome, SENSITIVE)
        pytest.fail("Callback rejection must not exchange a code")
    monkeypatch.setattr(oidc, "exchange", reject_exchange)
    with caplog.at_level(logging.INFO, logger="rota.api"):
        response = client.get("/auth/callback", params=params, follow_redirects=False)
    assert assert_failure(h, response, public, caplog).error_code == outcome
    assert "rota_oidc_return" not in client.cookies
    with transaction(h.app.state.engine) as db:
        flow = db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(state)))
        assert flow.used == (failure in {"denied", "provider_error", "missing_code", "exchange", "nonce", "issuer", "used_state"})


@pytest.mark.parametrize("mutation", ["issuer", "nonce"])
def test_verified_token_rejections_use_safe_browser_redirect(h, provider, caplog, mutation):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id="environment", data=AuthSettings(mode="both", institutional_enabled=True,
            issuer=provider.settings.oidc_issuer, discovery_url=provider.settings.oidc_discovery_url,
            client_id=provider.settings.oidc_client_id, allowed_hosts=["idp.test"],
            institutional_bindings=[{"identity_id": h.seed["identities"]["staff"], "issuer": provider.settings.oidc_issuer, "subject": "hospital-subject"}]
        ).model_dump(mode="json")))
    provider.config["token_endpoint_auth_methods_supported"] = ["none"]
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        started = client.get("/auth/start", follow_redirects=False)
        params = parse_qs(urlparse(started.headers["location"]).query)
        claims = {"nonce": params["nonce"][0]}
        claims["iss" if mutation == "issuer" else "nonce"] = SENSITIVE
        provider.tokens = {"id_token": provider.token(**claims)}
        with caplog.at_level(logging.INFO, logger="rota.api"):
            response = client.get("/auth/callback", params={"state": params["state"][0], "code": SENSITIVE}, follow_redirects=False)
        diagnostic = assert_failure(h, response, "failed", caplog)
        assert diagnostic.error_code == ("OIDC_TOKEN" if mutation == "issuer" else "OIDC_NONCE")
        if mutation == "issuer":
            assert diagnostic.cause_type == "InvalidIssuerError" and diagnostic.cause_traceback
        assert provider.tokens["id_token"] not in "\n".join(JsonFormatter().format(record) for record in caplog.records)


def test_denied_callback_does_not_cancel_other_flows_or_destroy_an_existing_session(h, callback):
    client, state = callback
    existing = h.client("coordinator")
    token = existing.cookies["rota_session"]
    csrf = existing.cookies["rota_csrf"]
    client.cookies.set("rota_session", token)
    client.cookies.set("rota_csrf", csrf)
    started = client.get("/auth/start", follow_redirects=False)
    other_state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    browser = client.cookies["rota_oidc_browser"]
    response = client.get("/auth/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/?signin_error=provider_denied"
    assert client.cookies["rota_session"] == token and client.cookies["rota_csrf"] == csrf
    assert client.cookies["rota_oidc_browser"] == browser
    assert client.get("/api/me").status_code == 200
    with transaction(h.app.state.engine) as db:
        other = db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(other_state)))
        assert not other.used
    replay = client.get("/auth/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert replay.status_code == 303 and replay.headers["location"] == "/?signin_error=expired"


def test_callback_audit_outage_still_returns_safe_redirect(h, callback, monkeypatch, caplog):
    from sqlalchemy.exc import SQLAlchemyError
    client, state = callback
    def unavailable(*args, **kwargs):
        raise SQLAlchemyError(SENSITIVE)
    monkeypatch.setattr(main, "login_event", unavailable)
    with caplog.at_level(logging.WARNING, logger="rota.api"):
        response = client.get("/auth/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/?signin_error=provider_denied"
    reference = response.headers["x-correlation-id"]
    assert any(record.message == "login_audit_unavailable" and record.correlation_id == reference for record in caplog.records)
    assert SENSITIVE not in "\n".join(JsonFormatter().format(record) for record in caplog.records)
