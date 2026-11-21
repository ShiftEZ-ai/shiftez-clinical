"""Managed-password sign-in uses existing account identities and the existing users file."""
from .clock_support import freeze_clock
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import json
import logging
import secrets
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select

from apps.api import auth, identity_store, models as m, oidc
from apps.api.auth import aware, digest, login_keys
from apps.api.db import transaction
from apps.api.rate_limit import counter_key, hit
from apps.api.main import create_app
from apps.api.settings import Settings, TEST_ONLY_FLAGS
from migrations.safety import unreviewed
from packages.contracts.schemas import AuthSettings

USERNAME = "managed.person"
PASSWORD = secrets.token_urlsafe(30)
INVALID = {"code": "INVALID_LOGIN", "message": "Unable to sign in with these credentials", "details": None}


def provision(h, tmp_path, *, issuer="https://idp.test", subject=None):
    """Attach a managed account to an existing person and role binding, as invitation/OIDC do."""
    path = tmp_path / "users.yml"
    path.write_text(json.dumps({"users": {}}), encoding="utf-8")
    # Fixed-window rollback cases below retain their original counter assertions.
    settings = replace(h.settings, identity_users_file=str(path), password_login_enabled=True,
                       password_login_progressive_delay=False)
    users = identity_store.store_for(settings)
    users.create(USERNAME, "Managed Person (test)", "managed@example.test")
    users.set_password(USERNAME, PASSWORD)
    identity_id = h.seed["identities"]["staff"]
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, identity_id)
        identity.issuer = issuer
        identity.subject = subject or str(uuid.uuid4())
        identity.password_hash = None
        db.add(m.Account(identity_id=identity_id, username=USERNAME, email="managed@example.test", accepted_at=m.now()))
    return settings, users, identity_id


def client_for(h, settings):
    client = TestClient(create_app(settings, h.app.state.engine), base_url=settings.public_origin)
    client.headers["origin"] = settings.public_origin
    return client


@pytest.fixture
def managed(h, tmp_path):
    settings, users, identity_id = provision(h, tmp_path)
    with client_for(h, settings) as client:
        yield client, settings, users, identity_id


def post_login(client, username=USERNAME, password=PASSWORD, **kwargs):
    return client.post("/auth/login", json={"username": username, "password": password}, **kwargs)


def pin_login_clock(monkeypatch):
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    freeze_clock(monkeypatch, lambda: moment)
    return moment


def fill_failure_budget(h, settings, scope, count, *, username=USERNAME, peer="testclient"):
    keys = login_keys(username, peer, settings)
    subject = keys[0] if scope == "login-account" else keys[2]
    with transaction(h.app.state.engine, write=True) as db:
        for _ in range(count):
            hit(db.connection(), scope, subject, settings.password_login_window_seconds, auth.now())


def failure_counts(h, settings, *, username=USERNAME, peer="testclient"):
    keys = login_keys(username, peer, settings)
    with transaction(h.app.state.engine) as db:
        return [db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == counter_key(
            scope, subject, settings.password_login_window_seconds, auth.now())[0])) or 0
                for scope, subject in (("login-account", keys[0]), ("login-pair", keys[2]))]


def production(settings, monkeypatch):
    # pg creates its disposable schema before the production app refuses these flags.
    for flag in TEST_ONLY_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    return replace(settings, environment="production", public_origin="https://rota.test", deployment="production",
                   forwarded_allow_ips="192.0.2.18", fmuc_data_key="01" * 32)


def institutional(h, settings):
    config = AuthSettings(mode="both", institutional_enabled=True, issuer="https://idp.test",
                          client_id="synthetic-client", allowed_hosts=["idp.test"])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id="environment", data=config.model_dump(mode="json")))
    return settings


@pytest.mark.parametrize(("environment", "issuer", "subject"), [
    ("test", "https://idp.test", "pending:" + USERNAME),
    ("development", "https://another-idp.test", "synthetic-provider-subject"),
    ("test", "local-development", USERNAME),
])
def test_password_login_reuses_existing_identity_and_department_session_limits(h, tmp_path, environment, issuer, subject):
    settings, _, identity_id = provision(h, tmp_path, issuer=issuer, subject=subject)
    settings = replace(settings, environment=environment)
    with transaction(h.app.state.engine, write=True) as db:
        count = db.scalar(select(func.count()).select_from(m.Identity))
        identity = db.get(m.Identity, identity_id)
        person_id, display_name = identity.person_id, identity.display_name
        department = db.scalars(select(m.Department)).first()
        department.session_idle_minutes, department.session_max_hours = 17, 2
    with client_for(h, settings) as client:
        started = m.now()
        response = post_login(client)
        completed = m.now()
        assert response.status_code == 200, response.text
        assert response.json() == {"expires_at": response.json()["expires_at"], "display_name": display_name}
        assert response.headers["cache-control"] == "no-store"
        token = client.cookies["rota_session"]
        assert client.get("/api/me").json()["account"] == {"username": USERNAME}
        with transaction(h.app.state.engine) as db:
            assert db.scalar(select(func.count()).select_from(m.Identity)) == count
            identity = db.get(m.Identity, identity_id)
            assert (identity.issuer, identity.subject, identity.person_id, identity.password_hash) == (issuer, subject, person_id, None)
            session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(token)))
            assert session.identity_id == identity_id and session.auth_method == "password" and session.oidc_id_token is None
            assert started <= aware(identity.last_sign_in_at) <= completed
            assert datetime.fromisoformat(response.json()["expires_at"]) == aware(identity.last_sign_in_at) + timedelta(minutes=17)
            event = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == "auth.sign_in")).one()
            assert event.actor_id == identity_id and event.target_id == session.id and event.reason == "Signed in"
            assert event.correlation_id == response.headers["x-correlation-id"]
            assert event.detail == {"person_id": person_id, "identity_id": identity_id, "session_id": session.id,
                                    "device_label": session.device_label, "network_label": session.network_label, "method": "password"}
            assert db.scalar(select(func.count()).select_from(m.LoginAttempt)) == 0
        assert failure_counts(h, settings) == [0, 0]


@pytest.mark.postgres
@pytest.mark.parametrize("provider", [False, True])
def test_managed_password_login_and_logout_in_production(pg, tmp_path, monkeypatch, provider):
    settings, _, identity_id = provision(pg, tmp_path, subject=str(uuid.uuid4()) if provider else "pending:" + USERNAME)
    settings = production(settings, monkeypatch)
    if provider:
        settings = institutional(pg, settings)
    monkeypatch.setattr(oidc, "document", lambda settings: pytest.fail("Password authentication must not contact an OIDC provider"))
    with client_for(pg, settings) as client:
        config = client.get("/auth/config").json()
        assert config["password_login"] is True and config["development_login"] is True
        assert config["institutional_login"] is provider
        response = post_login(client)
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"expires_at", "display_name"}
        cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "__Host-rota")
        assert cookie.secure and cookie.path == "/" and not cookie.domain_specified and cookie.has_nonstandard_attr("HttpOnly")
        assert all("SameSite=lax" in value and "Secure" in value and "Path=/" in value
                   for value in response.headers.get_list("set-cookie"))
        assert client.get("/api/me").status_code == 200
        client.headers["x-csrf-token"] = client.cookies["rota_csrf"]
        assert client.post("/auth/logout", headers={"x-csrf-token": "wrong"}).status_code == 403
        ended = client.post("/auth/logout")
        assert ended.status_code == 200 and ended.json() == {"signed_out": True}
        assert not any("oidc_reauthenticate" in cookie.name for cookie in client.cookies.jar)
        assert "__Host-rota" not in client.cookies and "rota_csrf" not in client.cookies
        assert client.get("/api/me").status_code == 401
        # Legacy fixture hashes must remain unusable in production, even with a configured store.
        refused = post_login(client, "coordinator", "test-password-not-a-secret-0001")
        assert refused.status_code == 401 and refused.json()["error"] == INVALID
        with transaction(pg.app.state.engine) as db:
            session = db.scalars(select(m.Session).where(m.Session.identity_id == identity_id)).one()
            assert session.revoked and session.auth_method == "password"


@pytest.mark.parametrize("failure", ["wrong_password", "unknown", "disabled", "unaccepted", "store_disabled"])
def test_login_failures_are_generic_and_attempts_are_committed(h, managed, failure, monkeypatch):
    pin_login_clock(monkeypatch)
    client, settings, users, identity_id = managed
    username, password = USERNAME, PASSWORD
    if failure == "wrong_password":
        password += " wrong"
    elif failure == "unknown":
        username = "unknown.person"
    elif failure == "store_disabled":
        users.set_disabled(USERNAME, True)
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if failure == "disabled":
                db.get(m.Identity, identity_id).disabled = True
            else:
                db.scalar(select(m.Account).where(m.Account.identity_id == identity_id)).accepted_at = None
    response = post_login(client, username, password)
    assert response.status_code == 401 and response.json()["error"] == INVALID
    assert "rota_session" not in client.cookies
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.LoginAttempt)) == 0
        assert db.scalar(select(func.count()).select_from(m.Session)) == 0
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action == "auth.sign_in")) == 0
    assert failure_counts(h, settings, username=username) == [1, 1]


def test_unknown_username_still_performs_store_dummy_password_verification(managed, monkeypatch):
    client, _, _, _ = managed
    verified = []
    verify = type(identity_store.HASHER).verify

    def record_verify(hasher, stored, password):
        if hasher is identity_store.HASHER:
            verified.append(stored)
        return verify(hasher, stored, password)

    monkeypatch.setattr(type(identity_store.HASHER), "verify", record_verify)
    assert post_login(client, "unknown.person").status_code == 401
    assert verified == [identity_store._DUMMY]


def test_successful_managed_login_does_not_reopen_users_file_for_non_admin_probe(h, tmp_path, monkeypatch):
    settings, _, _ = provision(h, tmp_path)
    settings = institutional(h, settings)
    monkeypatch.setattr(identity_store.AutheliaUsersFile, 'has_credential',
                        lambda *_: pytest.fail('ordinary login must not reopen the users file for admin probing'))
    with client_for(h, settings) as client:
        assert post_login(client).status_code == 200


@pytest.mark.parametrize(("scope", "attempts"), [("login-account", 7), ("login-pair", 39)])
def test_account_and_pair_rate_limits_use_hashed_casefolded_keys(h, managed, scope, attempts, monkeypatch):
    pin_login_clock(monkeypatch)
    client, settings, _, _ = managed
    fill_failure_budget(h, settings, scope, attempts)
    wrong = post_login(client, USERNAME.upper(), PASSWORD + " wrong")
    assert wrong.status_code == 401 and wrong.json()["error"] == INVALID
    blocked = post_login(client)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "LOGIN_RATE_LIMIT"
    with transaction(h.app.state.engine) as db:
        stored = list(db.scalars(select(m.RateLimit)))
        assert all(len(row.key_hash) == 64 and USERNAME not in row.key_hash for row in stored)
    assert failure_counts(h, settings) == ([8, 1] if scope == "login-account" else [1, 40])


@pytest.mark.parametrize("failure", ["missing", "corrupt", "unconfigured"])
def test_managed_account_cannot_fall_back_to_a_legacy_password(h, managed, failure):
    _, settings, users, identity_id = managed
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, identity_id)
        identity.issuer, identity.subject = "local-development", USERNAME
        identity.password_hash = db.get(m.Identity, h.seed["identities"]["coordinator"]).password_hash
    if failure == "unconfigured":
        settings = replace(settings, identity_users_file="")
    with client_for(h, settings) as client:
        # Startup checks require an existing file; its later loss/corruption must fail closed.
        if failure == "missing":
            users.path.unlink()
        elif failure == "corrupt":
            users.path.write_text("invalid users file", encoding="utf-8")
        response = post_login(client, password="test-password-not-a-secret-0001")
        assert response.status_code == 401 and response.json()["error"] == INVALID
        assert "rota_session" not in client.cookies


@pytest.mark.parametrize("environment", ["development", "test"])
@pytest.mark.parametrize("configured", [False, True])
def test_nonproduction_config_keeps_password_login_for_test_identities(h, tmp_path, environment, configured):
    settings = provision(h, tmp_path)[0] if configured else h.settings
    settings = replace(settings, environment=environment, oidc_issuer="https://legacy-idp.test", fmuc_data_key="01" * 32)
    with client_for(h, settings) as client:
        config = client.get("/auth/config").json()
    assert config["password_login"] is True and config["development_login"] is True
    assert config["institutional_login"] is False


def test_production_requires_managed_password_store_even_with_legacy_oidc_environment(monkeypatch):
    settings = production(Settings(database_url="postgresql+psycopg://synthetic@localhost/disposable",
                                   oidc_issuer="https://idp.test", oidc_client_id="synthetic-client",
                                   oidc_client_secret="synthetic-test-only"), monkeypatch)
    with pytest.raises(RuntimeError, match="managed identity store"):
        settings.check()


def test_password_login_requires_same_origin_and_does_not_log_credentials(h, managed, caplog):
    client, _, _, _ = managed
    for origin in ("", "https://attacker.invalid"):
        response = post_login(client, headers={"origin": origin})
        assert response.status_code == 403 and response.json()["error"]["code"] == "ORIGIN_REJECTED"
        # F-D1: the refusal names the configured address, so the page can send the person there.
        assert response.json()["error"]["details"]["canonical_origin"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.LoginAttempt)) == 0
    with caplog.at_level(logging.INFO, logger="rota.api"):
        assert post_login(client, password=PASSWORD + " wrong").status_code == 401
        assert post_login(client).status_code == 200
    recorded = repr([record.__dict__ for record in caplog.records])
    assert USERNAME not in recorded and PASSWORD not in recorded


def test_production_passwords_require_store_and_legacy_oidc_values_do_not_configure_provider(tmp_path, monkeypatch):
    path = tmp_path / "users.yml"
    path.write_text('{"users": {}}', encoding="utf-8")
    settings = production(Settings(database_url="postgresql+psycopg://synthetic@localhost/disposable",
                                   identity_users_file=str(path), password_login_enabled=True), monkeypatch)
    settings = replace(settings, oidc_issuer=settings.public_origin + "/authelia")
    settings.check()  # Managed-password-only production is valid.
    with pytest.raises(RuntimeError):
        replace(settings, identity_users_file="").check()
    # Provider validation belongs to the database-backed AuthSettings contract.
    replace(settings, oidc_redirect_uri="https://another.test/auth/callback").check()


def test_authentication_method_migration_is_additive(h):
    migration = Path(__file__).resolve().parents[1] / "migrations/versions/0108_password_login.py"
    assert unreviewed(migration) == []
    column = next(column for column in inspect(h.app.state.engine).get_columns("sessions") if column["name"] == "auth_method")
    assert column["nullable"] and column["default"] is None


@pytest.mark.postgres
def test_production_managed_password_login_is_available_without_legacy_opt_in(pg, tmp_path, monkeypatch):
    settings, _, identity_id = provision(pg, tmp_path)
    settings = production(replace(settings, password_login_enabled=False), monkeypatch)
    with client_for(pg, settings) as client:
        assert client.get("/auth/config").json()["password_login"] is True
        assert post_login(client).status_code == 200
    with transaction(pg.app.state.engine, write=True) as db:
        db.get(m.Identity, identity_id).issuer = "local-development"
    with client_for(pg, settings) as client:
        assert post_login(client).status_code == 200  # A managed store account may retain this issuer label.
    monkeypatch.setenv("ROTA_PASSWORD_LOGIN_ENABLED", "true")
    assert Settings.from_env().password_login_enabled
    for value in ("false", "TRUE", "1", "yes", ""):
        monkeypatch.setenv("ROTA_PASSWORD_LOGIN_ENABLED", value)
        assert not Settings.from_env().password_login_enabled
    monkeypatch.delenv("ROTA_PASSWORD_LOGIN_ENABLED")
    assert not Settings.from_env().password_login_enabled


def test_login_preserves_password_exactly_and_rotates_the_presented_session(h, managed):
    client, _, users, _ = managed
    password = " \t" + secrets.token_urlsafe(30) + "\N{ARABIC LETTER MEEM} "
    users.set_password(USERNAME, password)
    for offered in (password.strip(), password.swapcase()):
        response = post_login(client, password=offered)
        assert response.status_code == 401 and response.json()["error"] == INVALID
    assert post_login(client, password=password).status_code == 200
    previous, csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
    assert post_login(client, password=password).status_code == 200
    assert previous != client.cookies["rota_session"] and csrf != client.cookies["rota_csrf"]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Session).where(m.Session.token_hash == digest(previous))).revoked
        assert [event.outcome for event in db.scalars(select(m.AuthLoginEvent).order_by(m.AuthLoginEvent.created_at, m.AuthLoginEvent.id))].count("success") == 2


@pytest.mark.parametrize("state", ["unknown", "unaccepted", "disabled", "store_disabled", "storeless", "malformed"])
def test_ineligible_logins_each_pay_one_argon2_verification(h, managed, state, monkeypatch):
    client, settings, users, identity_id = managed
    username = "unknown.person" if state == "unknown" else USERNAME
    if state == "storeless":
        settings = replace(settings, identity_users_file="")
    elif state == "store_disabled":
        users.set_disabled(USERNAME, True)
    elif state == "malformed":
        data = json.loads(users.path.read_text(encoding="utf-8"))
        data["users"][USERNAME]["password"] = "corrupt hash"
        users.path.write_text(json.dumps(data), encoding="utf-8")
    elif state in {"unaccepted", "disabled"}:
        with transaction(h.app.state.engine, write=True) as db:
            if state == "disabled":
                db.get(m.Identity, identity_id).disabled = True
            else:
                db.scalar(select(m.Account).where(m.Account.identity_id == identity_id)).accepted_at = None
    verified = []
    original = type(identity_store.HASHER).verify
    def record(hasher, stored, password):
        verified.append(stored)
        return original(hasher, stored, password)
    monkeypatch.setattr(type(identity_store.HASHER), "verify", record)
    with client_for(h, settings) as client:
        response = post_login(client, username)
        assert response.status_code == 401 and response.json()["error"] == INVALID
    assert len(verified) == 1


def test_failure_audit_includes_rate_limit_csrf_schema_and_session_issuance(h, managed, monkeypatch):
    pin_login_clock(monkeypatch)
    client, settings, _, identity_id = managed
    assert post_login(client, headers={"origin": "null"}).status_code == 403
    assert client.post("/auth/login", json={"username": USERNAME, "password": ""}).status_code == 422
    assert client.post("/auth/login", data={"username": USERNAME, "password": PASSWORD}).status_code == 422
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id)):
            binding.revoked = True
    response = post_login(client)
    assert response.status_code == 401 and response.json()["error"] == INVALID
    assert failure_counts(h, settings) == [1, 1]
    fill_failure_budget(h, settings, "login-account", 7)
    blocked = post_login(client)
    assert blocked.status_code == 429 and 1 <= int(blocked.headers["retry-after"]) <= 900
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuthLoginEvent)))
        assert len(events) == 4  # The two malformed bodies share one immutable first-rejection event.
        assert {event.outcome for event in events} == {"ORIGIN_REJECTED", "SCHEMA_INVALID", "INVALID_LOGIN", "LOGIN_RATE_LIMIT"}
        assert all(len(event.account_hash) == len(event.peer_hash) == 64 and event.correlation_id for event in events)
        assert all(event.session_id is None for event in events)
        assert PASSWORD not in repr([vars(event) for event in events]) and USERNAME not in repr([vars(event) for event in events])
        assert db.scalar(select(func.count()).select_from(m.Session)) == 0


def test_login_lockout_expires_without_blocked_requests_extending_it(h, managed, monkeypatch):
    client, settings, _, _ = managed
    moment = pin_login_clock(monkeypatch)
    fill_failure_budget(h, settings, "login-account", 8)
    assert post_login(client).status_code == 429
    assert failure_counts(h, settings) == [8, 0]
    monkeypatch.setattr(auth, "now", lambda: moment + timedelta(seconds=settings.password_login_window_seconds))
    freeze_clock(monkeypatch, auth.now)
    assert post_login(client).status_code == 200


def test_password_login_preserves_unlimited_session_policy(h, managed):
    client, _, _, _ = managed
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.session_idle_unlimited = department.session_max_unlimited = True
    response = post_login(client)
    assert response.status_code == 200 and response.json()["expires_at"] is None


@pytest.mark.postgres
def test_password_only_production_invitation_acceptance_and_login_keep_identity_and_roles(pg, tmp_path, monkeypatch):
    from apps.api.auth import authenticate
    from apps.api.services import accounts
    from packages.contracts.schemas import AccountInvite
    token = pg.client("admin").cookies["rota_session"]
    path = tmp_path / "users.yml"
    path.write_text(json.dumps({"users": {}}), encoding="utf-8")
    settings = production(replace(pg.settings, identity_users_file=str(path), password_login_enabled=True,
                                  password_login_progressive_delay=True), monkeypatch)
    username, password = "invited." + secrets.token_hex(6), secrets.token_urlsafe(30)
    with transaction(pg.app.state.engine, write=True) as db:
        actor = authenticate(db, token, settings=pg.settings)
        result = accounts.invite(db, actor, AccountInvite(username=username, email=username + "@example.test",
                                 display_name="Invited Person (test)", roles=["staff"], reason="Synthetic password-login invitation"), settings)
        identity_id = result["record"]["id"]
        link_token = db.info["one_time_link"].rsplit("/", 1)[1]
        count = db.scalar(select(func.count()).select_from(m.Identity))
    with client_for(pg, settings) as client:
        assert client.get("/auth/config").json()["institutional_login"] is False
        assert post_login(client, username, password).status_code == 401
        assert client.post("/api/account-links/" + link_token, json={"password": password}).status_code == 200
        delay_key = digest("login-delay:" + login_keys(username, "testclient", settings)[2])
        with transaction(pg.app.state.engine) as db:
            deadline = aware(db.get(m.RateLimit, delay_key).expires_at)
        monkeypatch.setattr(auth, "now", lambda: deadline)
        assert post_login(client, username, password).status_code == 200
        me = client.get("/api/me").json()
        assert me["roles"] == ["staff"] and me["account"] == {"username": username}
        assert client.get("/api/access").status_code == 403
        with transaction(pg.app.state.engine) as db:
            assert db.scalar(select(func.count()).select_from(m.Identity)) == count
            identity = db.get(m.Identity, identity_id)
            assert identity.issuer == "local-development" and identity.password_hash is None
            session = db.scalar(select(m.Session).where(m.Session.token_hash == digest(client.cookies["__Host-rota"])))
            assert session.identity_id == identity_id and session.auth_method == "password"
        refused = post_login(client, "coordinator", "test-password-not-a-secret-0001")
        assert refused.status_code == 401 and refused.json()["error"] == INVALID


@pytest.mark.postgres
def test_password_only_production_worker_starts_without_api_users_file(pg, monkeypatch):
    import sys
    from apps.worker import main as worker
    settings = production(replace(pg.settings, password_login_enabled=False, identity_users_file=""), monkeypatch)
    with pytest.raises(RuntimeError, match="managed identity store"):
        settings.check()
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(worker, "make_engine", lambda *args, **kwargs: pg.app.state.engine)
    monkeypatch.setattr(worker, "ALIVE_FILE", "")
    monkeypatch.delenv("FMUC_HEARTBEAT_APP_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["rota-worker", "--once"])
    worker.main()  # Real worker pass on the disposable database; no provider or users-file mount.
    with transaction(pg.app.state.engine) as db:
        assert db.get(m.WorkerHeartbeat, worker.WORKER_ID).last_seen_at
    for invalid in (replace(settings, database_url="sqlite:///unsafe.db"), replace(settings, public_origin="http://rota.test")):
        with pytest.raises(RuntimeError):
            invalid.check(require_authentication=False)


def test_rejected_and_throttled_requests_aggregate_without_new_immutable_rows(h, managed, monkeypatch):
    moment = pin_login_clock(monkeypatch)
    client, settings, _, _ = managed
    for _ in range(12):
        assert post_login(client, headers={"origin": "null"}).status_code == 403
        assert client.post("/auth/login", json={"username": USERNAME, "password": ""}).status_code == 422
    fill_failure_budget(h, settings, "login-account", settings.password_login_account_limit)
    for _ in range(12):
        assert post_login(client).status_code == 429
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuthLoginEvent)))
        assert {event.outcome for event in events} == {"ORIGIN_REJECTED", "SCHEMA_INVALID", "LOGIN_RATE_LIMIT"}
        assert len(events) == 3
        for outcome in ("ORIGIN_REJECTED", "SCHEMA_INVALID", "LOGIN_RATE_LIMIT"):
            key, _ = counter_key("login-audit:" + outcome, "rejected", settings.password_login_window_seconds, moment)
            assert db.get(m.RateLimit, key).hits == 12
    later = moment + timedelta(seconds=settings.password_login_window_seconds)
    monkeypatch.setattr(auth, "now", lambda: later)
    freeze_clock(monkeypatch, auth.now)
    assert post_login(client, headers={"origin": "null"}).status_code == 403
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.AuthLoginEvent)) == 4


def test_unknown_username_spray_aggregates_but_known_first_failures_and_successes_remain(h, managed, monkeypatch):
    pin_login_clock(monkeypatch)
    client, settings, _, _ = managed
    for _ in range(10):
        assert post_login(client, "unknown-" + secrets.token_hex(6)).status_code == 401
    for _ in range(3):
        assert post_login(client, password=PASSWORD + " wrong").status_code == 401
    assert post_login(client).status_code == post_login(client).status_code == 200
    with transaction(h.app.state.engine) as db:
        events = list(db.scalars(select(m.AuthLoginEvent)))
        assert len(events) == 4  # First unknown aggregate, first known failure, and every success.
        assert [event.outcome for event in events].count("success") == 2
        known = [event for event in events if event.outcome == "INVALID_LOGIN" and event.account_hash == login_keys(USERNAME, "testclient", settings)[0]]
        assert len(known) == 1
        assert db.scalar(select(func.count()).select_from(m.LoginAttempt)) == 0
    assert failure_counts(h, settings) == [3, 3]


def test_one_hospital_address_cannot_lock_every_account(h, managed, monkeypatch):
    pin_login_clock(monkeypatch)
    client, settings, _, _ = managed
    for i in range(45):
        fill_failure_budget(h, settings, "login-pair", settings.password_login_pair_limit, username=f"another-{i}")
    assert post_login(client).status_code == 200
    assert failure_counts(h, settings) == [0, 0]


@pytest.mark.postgres
def test_parallel_password_burst_obeys_configured_failure_limit(pg, tmp_path, monkeypatch):
    pin_login_clock(monkeypatch)
    settings, _, _ = provision(pg, tmp_path)
    settings = replace(settings, password_login_account_limit=3, password_login_pair_limit=20)
    verified = []
    original = identity_store.AutheliaUsersFile.verify_credential
    def verify(users, username, password):
        verified.append(username)
        return original(users, username, password)
    monkeypatch.setattr(identity_store.AutheliaUsersFile, "verify_credential", verify)
    with client_for(pg, settings) as client:
        with ThreadPoolExecutor(max_workers=6) as executor:
            statuses = list(executor.map(lambda _: post_login(client, password=PASSWORD + " wrong").status_code, range(9)))
    assert sorted(statuses) == [401] * 3 + [429] * 6
    assert verified == [USERNAME] * 3
    assert failure_counts(pg, settings) == [3, 3]


def test_correct_password_department_selection_does_not_consume_failure_budget(h, managed, monkeypatch):
    pin_login_clock(monkeypatch)
    client, settings, _, identity_id = managed
    with transaction(h.app.state.engine, write=True) as db:
        original = db.get(m.Department, h.seed["department_id"])
        other = m.Department(organisation_id=original.organisation_id, name="Second Department (test)")
        db.add(other)
        db.flush()
        db.add(m.RoleBinding(identity_id=identity_id, department_id=other.id, role="staff", starts_on=m.now().date()))
    for _ in range(settings.password_login_account_limit + 1):
        response = post_login(client)
        assert response.status_code == 409 and response.json()["error"]["code"] == "DEPARTMENT_REQUIRED"
    assert failure_counts(h, settings) == [0, 0]
    assert client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD,
                                           "department_id": h.seed["department_id"]}).status_code == 200


@pytest.mark.parametrize("window", [60, 900, 3600])
def test_configured_password_failure_window_and_pair_limit(h, managed, monkeypatch, window):
    moment = pin_login_clock(monkeypatch)
    _, settings, _, _ = managed
    settings = replace(settings, password_login_account_limit=20, password_login_pair_limit=1,
                       password_login_window_seconds=window)
    with client_for(h, settings) as client:
        assert post_login(client, password=PASSWORD + " wrong").status_code == 401
        blocked = post_login(client)
        assert blocked.status_code == 429 and 1 <= int(blocked.headers["retry-after"]) <= window
        later = moment + timedelta(seconds=window)
        monkeypatch.setattr(auth, "now", lambda: later)
        freeze_clock(monkeypatch, auth.now)
        assert post_login(client).status_code == 200


def test_stored_sign_in_hashes_are_keyed_not_plain_sha256(h, managed, monkeypatch):
    # #930: account names and client addresses are low in entropy, so a plain SHA-256 of them reverses.
    import hashlib
    pin_login_clock(monkeypatch)
    client, settings, _, _ = managed
    assert post_login(client, password=PASSWORD + " wrong").status_code == 401
    plain = lambda value: hashlib.sha256(value.encode()).hexdigest()
    with transaction(h.app.state.engine) as db:
        event = db.scalars(select(m.AuthLoginEvent).where(m.AuthLoginEvent.outcome == "INVALID_LOGIN")).one()
        keys = login_keys(USERNAME, "testclient", settings)
        assert (event.account_hash, event.peer_hash) == keys[:2]
        assert event.account_hash != plain("account:" + USERNAME) and event.peer_hash != plain("peer:testclient")
        assert keys[2] != plain(f"pair:{len(USERNAME)}:{USERNAME}testclient")
        counters = set(db.scalars(select(m.RateLimit.key_hash)))
    window = settings.password_login_window_seconds
    for scope, subject in (("login-account", "account:" + USERNAME), ("login-address", "peer:testclient")):
        assert counter_key(scope, plain(subject), window, auth.now())[0] not in counters
    assert counter_key("login-account", keys[0], window, auth.now())[0] in counters
    other = replace(settings, fmuc_data_key="02" * 32)
    assert login_keys(USERNAME, "testclient", other)[0] != keys[0]


def test_keyless_production_never_stores_a_sign_in_hash(h):
    from apps.api.errors import Problem
    with pytest.raises(Problem) as refused:
        auth.login_digest("peer:testclient", replace(h.settings, environment="production", fmuc_data_key=""))
    assert refused.value.code == "LOGIN_UNCONFIGURED"
