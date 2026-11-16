"""Synthetic provider protocol fixtures; no deployment credentials or live IdP requests."""
from dataclasses import replace
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from apps.api import models as m, oidc
from apps.api.auth import digest
from apps.api.auth import issue_session
from apps.api.db import transaction
from apps.api.main import create_app
from apps.api.oidc import document as discover
from apps.api.settings import TEST_ONLY_FLAGS
from packages.contracts.schemas import AuthSettings

ID_TOKEN = jwt.encode({"iss": "https://idp.test", "aud": "rota-client", "sub": "staff"},
                      "synthetic-provider-key-for-mocked-exchange", algorithm="HS256")


def test_logout_retries_liveness_writes_with_a_bound_and_retains_audit(h, monkeypatch):
    from apps.api import main

    client = h.client("staff")
    token = client.cookies["rota_session"]
    operations, cleanup = [], []
    real_write = main.liveness_write

    def write_spy(*args, **kwargs):
        operations.append((kwargs.get('attempts', 3), kwargs.get('context')))
        return real_write(*args, **kwargs)

    monkeypatch.setattr(main, 'liveness_write', write_spy)
    monkeypatch.setattr(main, "retry_write", lambda *args, **kwargs: pytest.fail("logout entered retry_write"))
    monkeypatch.setattr(main, "_deferred_logout_cleanup", lambda *args: cleanup.append(args[1:]))

    response = client.post("/auth/logout")
    assert response.status_code == 200
    assert operations == [(3, {'route': 'auth.logout'})]
    assert cleanup and cleanup[0][1]
    assert "rota_session" not in client.cookies and "rota_csrf" not in client.cookies

    client.cookies.set("rota_session", token)
    assert client.get("/api/me").status_code == 401
    with transaction(h.app.state.engine) as session:
        current = session.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
        assert current.revoked
        assert session.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "auth.sign_out",
                                                          m.AuditEvent.target_id == current.id)) is not None


def test_logout_defers_push_installation_cleanup_until_after_revoke_commit(h):
    client = h.client("staff")
    token = client.cookies["rota_session"]
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
        installation = m.PushInstallation(identity_id=session.identity_id, department_id=session.department_id,
                                           session_id=session.id, installation_hint="logout-cleanup-test")
        db.add(installation)
        db.flush()
        installation_id = installation.id

    response = client.post("/auth/logout")
    assert response.status_code == 200
    with transaction(h.app.state.engine) as db:
        assert db.get(m.PushInstallation, installation_id).state == "revoked"


@pytest.mark.postgres
def test_logout_does_not_wait_for_global_writer_and_commits_while_held(pg):
    from apps.api import db

    client = pg.client("staff")
    token = client.cookies["rota_session"]
    with pg.app.state.engine.begin() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": db.WRITE_LOCK_KEY})
        started = time.monotonic()
        response = client.post("/auth/logout")
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert "rota_session" not in client.cookies and "rota_csrf" not in client.cookies
        assert client.get("/api/me").status_code == 401
        with transaction(pg.app.state.engine) as session:
            current = session.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
            assert current.revoked
            assert session.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "auth.sign_out",
                                                              m.AuditEvent.target_id == current.id)) is not None
    return locals()


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_logout_global_writer_wall_budget(pg):
    state = test_logout_does_not_wait_for_global_writer_and_commits_while_held(pg)
    assert state['elapsed'] < 2


@pytest.fixture
def institutional(h, monkeypatch, public_oidc_dns):
    settings = replace(h.settings, oidc_issuer="https://idp.test", oidc_client_id="rota-client",
                       oidc_client_secret="fixture-only", oidc_redirect_uri=h.settings.public_origin + "/auth/callback",
                       oidc_allowed_hosts="idp.test")
    config = {"authorization_endpoint": "https://idp.test/authorize", "end_session_endpoint": "https://idp.test/logout"}
    monkeypatch.setattr(oidc, "document", lambda settings: config)
    monkeypatch.setattr(oidc, "exchange", lambda settings, flow, code: ({"sub": "staff"}, ID_TOKEN))
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.scalar(select(m.Identity).where(m.Identity.subject == "staff"))
        identity.issuer = settings.oidc_issuer
        db.add(m.AuthConfiguration(id="environment", data=AuthSettings(mode="both", institutional_enabled=True,
            issuer="https://idp.test", client_id="rota-client", allowed_hosts=["idp.test"],
            institutional_bindings=[{"identity_id": identity.id, "issuer": settings.oidc_issuer, "subject": "staff"}]).model_dump(mode="json")))
    with TestClient(create_app(settings, h.app.state.engine), base_url=settings.public_origin) as client:
        client.headers["origin"] = settings.public_origin
        yield client, config, settings


def sign_in(client):
    started = client.get("/auth/start", follow_redirects=False)
    assert started.status_code == 303
    params = parse_qs(urlparse(started.headers["location"]).query)
    done = client.get("/auth/callback", params={"state": params["state"][0], "code": "synthetic-code"}, follow_redirects=False)
    assert done.status_code == 303
    client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
    return params


@pytest.mark.parametrize("change", [{"issuer": "https://other-provider.test", "allowed_hosts": ["other-provider.test"]},
                                  {"client_id": "other-client"}])
@pytest.mark.parametrize("unsafe_endpoint", [False, True])
def test_provider_switch_uses_current_metadata_without_storing_or_sending_tokens(h, institutional, monkeypatch,
                                                                              change, unsafe_endpoint):
    client, _, settings = institutional
    sign_in(client)
    token_hash = digest(client.cookies["rota_session"])
    issuer = change.get("issuer", settings.oidc_issuer)
    metadata_url = issuer + "/metadata"
    with transaction(h.app.state.engine, write=True) as db:
        assert db.scalar(select(m.Session).where(m.Session.token_hash == token_hash)).oidc_id_token is None
        row = db.get(m.AuthConfiguration, "environment")
        row.data = row.data | change | {"discovery_url": metadata_url}
        row.revision += 1
    config = {"issuer": issuer, "authorization_endpoint": issuer + "/authorize", "token_endpoint": issuer + "/token",
              "jwks_uri": issuer + "/keys", "code_challenge_methods_supported": ["S256"],
              "end_session_endpoint": "https://untrusted.test/logout" if unsafe_endpoint else issuer + "/logout"}
    requests = []
    def metadata(request):
        requests.append(request)
        return httpx.Response(200, json=config)
    client_factory = httpx.Client
    monkeypatch.setattr(oidc, "document", discover)
    monkeypatch.setattr(oidc.httpx, "Client", lambda **kwargs: client_factory(
        transport=httpx.MockTransport(metadata), **kwargs))
    response = client.post("/auth/logout")
    assert response.status_code == 200 and response.json()["signed_out"] is True
    assert requests and all(str(request.url.copy_with(host=request.headers["host"])) == metadata_url for request in requests)
    if unsafe_endpoint:
        assert response.json()["end_session_url"] is None
    else:
        location = urlparse(response.json()["end_session_url"])
        params = parse_qs(location.query)
        assert location._replace(query="").geturl() == config["end_session_endpoint"]
        assert params["client_id"] == [change.get("client_id", settings.oidc_client_id)]
        assert "id_token_hint" not in params
    assert "rota_session" not in client.cookies and client.cookies["rota_oidc_reauthenticate"] == "yes"
    assert ID_TOKEN not in response.text + str([(request.url, dict(request.headers), request.content) for request in requests])
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
        assert session.revoked and session.oidc_id_token is None


def test_reset_logout_restore_still_requires_fresh_provider_authentication(h, institutional):
    client, _, _ = institutional
    sign_in(client)
    admin = h.client("admin")
    path = "/api/admin/auth-config"
    decision = {"expected_revision": 1, "confirmed": True, "reason": "Synthetic provider reset"}
    assert admin.request("DELETE", path, json=decision).status_code == 200
    response = client.post("/auth/logout")
    assert response.status_code == 200 and response.json()["end_session_url"] is None
    assert client.cookies["rota_oidc_reauthenticate"] == "yes"
    assert admin.post(path + "/undo", json=decision | {"expected_revision": 2}).status_code == 200
    started = client.get("/auth/start", follow_redirects=False)
    assert started.status_code == 303
    assert parse_qs(urlparse(started.headers["location"]).query)["prompt"] == ["login"]


@pytest.mark.parametrize("legacy_null_method", [False, True], ids=["oidc", "legacy-null"])
def test_logout_uses_client_id_without_retaining_claims_and_browser_bound_return(h, institutional, legacy_null_method):
    client, config, settings = institutional
    assert "prompt" not in sign_in(client)
    token_hash = digest(client.cookies["rota_session"])
    assert ID_TOKEN not in client.get("/api/me").text
    assert ID_TOKEN not in str(client.cookies)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Session).where(m.Session.token_hash == token_hash)).oidc_id_token is None
    if legacy_null_method:
        with transaction(h.app.state.engine, write=True) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
            session.auth_method = None
            session.oidc_id_token = ID_TOKEN
    config["end_session_endpoint"] += "?provider_option=keep"
    ended = client.post("/auth/logout")
    assert ended.status_code == 200 and ended.json()["signed_out"] is True
    assert ended.headers["cache-control"] == "no-store"
    url = urlparse(ended.json()["end_session_url"])
    params = parse_qs(url.query)
    assert url.scheme == "https" and url.netloc == "idp.test" and url.path == "/logout"
    assert "id_token_hint" not in params
    assert params["client_id"] == [settings.oidc_client_id]
    assert params["post_logout_redirect_uri"] == [settings.public_origin + "/"]
    assert params["provider_option"] == ["keep"] and len(params["state"][0]) >= 32
    assert "rota_session" not in client.cookies and "rota_csrf" not in client.cookies
    with transaction(h.app.state.engine) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
        assert session.revoked and session.oidc_id_token is None
        assert ID_TOKEN not in str([event.detail for event in db.scalars(select(m.AuditEvent))])
    assert client.get("/api/me").status_code == 401
    assert client.get("/", params={"state": "wrong"}).status_code == 401
    returned = client.get("/", params={"state": params["state"][0]}, follow_redirects=False)
    assert returned.status_code == 303 and returned.headers["location"] == "/"
    assert "rota_oidc_logout" not in client.cookies
    assert client.get("/", params={"state": params["state"][0]}).status_code == 401


@pytest.mark.parametrize("failure", ["missing_endpoint", "unsafe_endpoint", "discovery_unavailable",
                                   "malformed_discovery", "malformed_endpoint"])
def test_logout_fallback_requires_fresh_login_until_success(h, institutional, monkeypatch, failure):
    client, config, settings = institutional
    sign_in(client)
    if failure == "missing_endpoint":
        config.pop("end_session_endpoint")
    elif failure == "unsafe_endpoint":
        config["end_session_endpoint"] = "https://attacker.invalid/logout"
    elif failure == "discovery_unavailable":
        def unavailable(settings):
            raise httpx.ConnectError("Synthetic unavailable provider")
        monkeypatch.setattr(oidc, "document", unavailable)
    elif failure == "malformed_discovery":
        # Exercise real discovery parsing rather than replacing its validation.
        client_factory = httpx.Client
        monkeypatch.setattr(oidc, "document", discover)
        monkeypatch.setattr(oidc.httpx, "Client", lambda **kwargs: client_factory(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])), **kwargs))
    elif failure == "malformed_endpoint":
        config["end_session_endpoint"] = {"url": "https://idp.test/logout"}
    ended = client.post("/auth/logout")
    assert ended.status_code == 200 and ended.json()["end_session_url"] is None
    assert client.get("/api/me").status_code == 401
    marker = next(cookie for cookie in client.cookies.jar if cookie.name == "rota_oidc_reauthenticate")
    assert marker.path == "/" and marker.has_nonstandard_attr("HttpOnly")
    monkeypatch.setattr(oidc, "document", lambda settings: config)
    # Abandoning an authorization attempt must not consume the requirement.
    for _ in range(2):
        started = client.get("/auth/start", follow_redirects=False)
        params = parse_qs(urlparse(started.headers["location"]).query)
        assert params["prompt"] == ["login"] and "max_age" not in params
        with transaction(h.app.state.engine) as db:
            flow = db.scalar(select(m.OIDCFlow).where(m.OIDCFlow.state_hash == digest(params["state"][0])))
            assert flow.force_login
    params = sign_in(client)
    assert params["prompt"] == ["login"] and "max_age" not in params
    assert client.cookies["rota_oidc_reauthenticate"] == "yes"
    assert sign_in(client)["prompt"] == ["login"]


def test_an_authorization_started_before_logout_cannot_clear_reauthentication(institutional):
    client, config, settings = institutional
    sign_in(client)
    started = client.get("/auth/start", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
    browser = client.cookies["rota_oidc_browser"]
    assert client.post("/auth/logout").status_code == 200
    assert "rota_oidc_browser" not in client.cookies
    # Even restoring the old browser binding must not let a held callback reuse the OP session.
    client.cookies.set("rota_oidc_browser", browser, domain=urlparse(settings.public_origin).hostname, path="/")
    callback = client.get("/auth/callback", params={"state": state, "code": "held-code"}, follow_redirects=False)
    assert callback.status_code == 303 and callback.headers["location"] == "/?signin_error=expired"
    assert client.cookies["rota_oidc_reauthenticate"] == "yes"
    assert client.get("/api/me").status_code == 401


def test_logout_cancels_all_concurrent_browser_flows(institutional):
    client, config, settings = institutional
    sign_in(client)
    browser = client.cookies["rota_oidc_browser"]
    states = []
    for _ in range(2):
        started = client.get("/auth/start", follow_redirects=False)
        states.append(parse_qs(urlparse(started.headers["location"]).query)["state"][0])
        assert client.cookies["rota_oidc_browser"] == browser
    assert states[0] != states[1]
    assert client.post("/auth/logout").status_code == 200
    client.cookies.set("rota_oidc_browser", browser, domain=urlparse(settings.public_origin).hostname, path="/")
    for state in states:
        response = client.get("/auth/callback", params={"state": state, "code": "held-code"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/?signin_error=expired"
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("logout_after", ["consume", "finish"])
def test_logout_cancels_a_callback_already_in_flight(h, institutional, monkeypatch, logout_after):
    client, config, settings = institutional
    sign_in(client)
    started = client.get("/auth/start", follow_redirects=False)
    state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]

    def sign_out():
        assert client.post("/auth/logout").status_code == 200
        assert client.cookies["rota_oidc_reauthenticate"] == "yes"

    if logout_after == "consume":
        def exchange_then_logout(settings, flow, code):
            sign_out()
            return {"sub": "staff"}, ID_TOKEN
        monkeypatch.setattr(oidc, "exchange", exchange_then_logout)
    else:
        # End the old app session after the new session commits but before its cookies reach the browser.
        from apps.api import main
        write = main.liveness_write
        def committed_then_logout(engine, operation, *, attempts=3, context=None, deadline=None):
            result = write(engine, operation, attempts=attempts, context=context, deadline=deadline)
            if context == {'route': 'auth.oidc_callback', 'phase': 'finish'} and isinstance(result, dict) and "session_id" in result:
                sign_out()
            return result
        monkeypatch.setattr(main, "liveness_write", committed_then_logout)
    callback = client.get("/auth/callback", params={"state": state, "code": "pending-code"}, follow_redirects=False)
    assert callback.status_code == 303
    if logout_after == "consume":
        assert callback.headers["location"] == "/?signin_error=expired"
    assert client.cookies["rota_oidc_reauthenticate"] == "yes"
    assert client.get("/api/me").status_code == 401
    next_sign_in = client.get("/auth/start", follow_redirects=False)
    assert parse_qs(urlparse(next_sign_in.headers["location"]).query)["prompt"] == ["login"]


def test_logout_still_requires_csrf_and_password_sign_out_does_not_call_provider(h, monkeypatch):
    client = h.client("staff")
    monkeypatch.setattr(oidc, "document", lambda settings: pytest.fail("Password logout must not contact the IdP"))
    assert client.post("/auth/logout", headers={"x-csrf-token": "wrong"}).status_code == 403
    assert client.get("/api/me").status_code == 200
    assert client.post("/auth/logout").json() == {"signed_out": True}
    assert "rota_oidc_reauthenticate" not in client.cookies
    assert client.get("/api/me").status_code == 401


def test_password_session_on_institutional_identity_does_not_call_provider(h, institutional, monkeypatch):
    client, _, _ = institutional
    sign_in(client)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(client.cookies["rota_session"])))
        session.auth_method = "password"
    monkeypatch.setattr(oidc, "document", lambda settings: pytest.fail("Password logout must not contact the IdP"))
    assert client.post("/auth/logout").json() == {"signed_out": True}
    assert "rota_oidc_reauthenticate" not in client.cookies
    assert client.get("/api/me").status_code == 401


def test_sign_out_other_devices_preserves_current_institutional_session(h, institutional):
    import uuid
    client, config, settings = institutional
    sign_in(client)
    token_hash = digest(client.cookies["rota_session"])
    with transaction(h.app.state.engine, write=True) as db:
        current = db.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
        other = issue_session(db, db.get(m.Identity, current.identity_id), current.department_id, settings)
        db.get(m.Session, other["session_id"]).oidc_id_token = "synthetic-legacy-device-id-token"
    response = client.post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
                           json={"kind": "account.end_other_sessions", "payload": {"reason": "End my other devices"}})
    assert response.status_code == 200 and response.json()["ended"] == 1
    assert client.get("/api/me").status_code == 200
    assert "rota_oidc_reauthenticate" not in client.cookies
    with transaction(h.app.state.engine) as db:
        current = db.scalar(select(m.Session).where(m.Session.token_hash == token_hash))
        assert not current.revoked and current.oidc_id_token is None
        ended = db.scalar(select(m.Session).where(m.Session.token_hash == digest(other["token"])))
        assert ended.revoked and ended.oidc_id_token is None
    assert "id_token_hint" not in parse_qs(urlparse(client.post("/auth/logout").json()["end_session_url"]).query)


@pytest.mark.parametrize("unlimited", [False, True])
def test_production_oidc_cookies_are_host_bound(h, institutional, monkeypatch, unlimited, tmp_path):
    from http.cookies import SimpleCookie
    from apps.api.auth import SESSION_COOKIE_MAX_AGE_SECONDS
    _, config, settings = institutional
    department = h.get("admin", "/api/me")["department"]
    h.command("admin", "department.sessions", {"idle_minutes": 5, "max_hours": 1,
              "idle_unlimited": unlimited, "max_unlimited": unlimited}, department["id"], department["revision"])
    origin = "https://rota.test"
    users = tmp_path / "users.json"
    users.write_text('{"users": {}}', encoding="utf-8")
    # Cookie policy probe only: no production database or provider is accessed.
    settings = replace(settings, environment="production", public_origin=origin,
                       oidc_redirect_uri=origin + "/auth/callback", database_url="postgresql+psycopg://fixture@localhost/fixture",
                       forwarded_allow_ips="192.0.2.18", identity_users_file=str(users), fmuc_data_key="01" * 32)
    from apps.api import live
    monkeypatch.setattr(live, "listen", lambda database_url: None)
    for flag in TEST_ONLY_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    with TestClient(create_app(settings, h.app.state.engine), base_url=origin) as client:
        client.headers["origin"] = origin
        client.cookies.set("rota_oidc_browser", "untrusted-legacy-binding", domain="rota.test", path="/auth")
        started = client.get("/auth/start", follow_redirects=False)
        assert started.status_code == 303
        state = parse_qs(urlparse(started.headers["location"]).query)["state"][0]
        browser = client.cookies["__Host-rota_oidc_browser"]
        assert browser != "untrusted-legacy-binding"
        done = client.get("/auth/callback", params={"state": state, "code": "synthetic-code"}, follow_redirects=False)
        assert done.status_code == 303
        cookies = SimpleCookie()
        for header in done.headers.get_list("set-cookie"):
            cookies.load(header)
        for name in ("__Host-rota", "rota_csrf"):
            cookie = cookies[name]
            assert cookie["secure"] and cookie["samesite"].lower() == "lax" and cookie["path"] == "/"
            assert not cookie["domain"] and bool(cookie["httponly"]) == (name == "__Host-rota")
            age = int(cookie["max-age"])
            if unlimited:
                assert age == SESSION_COOKIE_MAX_AGE_SECONDS
            else:
                assert 3595 <= age <= 3600
        ended = client.post("/auth/logout", headers={"x-csrf-token": client.cookies["rota_csrf"]})
        assert ended.status_code == 200
        for name in ("__Host-rota_oidc_reauthenticate", "__Host-rota_oidc_logout"):
            cookie = next(cookie for cookie in client.cookies.jar if cookie.name == name)
            assert cookie.secure and cookie.path == "/" and not cookie.domain_specified
            assert cookie.has_nonstandard_attr("HttpOnly")
