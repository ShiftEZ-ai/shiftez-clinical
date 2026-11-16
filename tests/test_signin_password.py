"""Identifier aliases, connection throttling and bounded remembered sessions."""
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from apps.api import auth, identity_store, main, models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import auth_configuration
from packages.contracts.schemas import AuthSettings, LoginInput
from .conftest import TEST_PASSWORD
from .test_bulk_pending_privacy import DATABASES
from .test_password_login import PASSWORD, USERNAME, client_for, production, provision


def attempt(h, settings, identifier=USERNAME, password=PASSWORD, peer="testclient", **values):
    return auth.password_login(h.app.state.engine,
                               LoginInput(username=identifier, password=password, **values), peer, settings)


def provider(identity_id, mode="both"):
    return AuthSettings(mode=mode, institutional_enabled=True, issuer="https://idp.example.test",
                        client_id="synthetic-client", allowed_hosts=["idp.example.test"],
                        institutional_bindings=[{"identity_id": identity_id, "issuer": "https://idp.example.test",
                                                 "subject": "synthetic-subject"}])


@pytest.mark.parametrize("identifier", [USERNAME.upper(), "MANAGED@EXAMPLE.TEST", "  managed@example.test  "])
def test_password_aliases_verify_the_canonical_managed_username(h, tmp_path, monkeypatch, identifier):
    settings, _, identity_id = provision(h, tmp_path)
    verified = []
    original = identity_store.AutheliaUsersFile.verify_credential

    def verify(store, username, password):
        verified.append(username)
        return original(store, username, password)

    monkeypatch.setattr(identity_store.AutheliaUsersFile, "verify_credential", verify)
    result = attempt(h, settings, identifier)
    assert not isinstance(result, Problem)
    assert verified == [USERNAME]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Session, result["session_id"]).identity_id == identity_id


def test_identifier_email_matching_uses_casefold_and_ambiguous_emails_fail_closed(h, tmp_path):
    settings, _, identity_id = provision(h, tmp_path)
    with transaction(h.app.state.engine, write=True) as db:
        account = db.scalar(select(m.Account).where(m.Account.identity_id == identity_id))
        account.email = "Straße@example.test"
        assert auth.resolve_identifier(db, "STRASSE@EXAMPLE.TEST", settings)[1].id == identity_id
        db.add(m.Account(identity_id=h.seed["identities"]["approver"], username="another.person",
                         email="STRASSE@EXAMPLE.TEST", accepted_at=m.now()))
        db.flush()
        assert auth.resolve_identifier(db, "strasse@example.test", settings) == (None, None)
        assert auth.resolve_identifier(db, USERNAME, settings)[1].id == identity_id
    result = attempt(h, settings, "strasse@example.test")
    assert isinstance(result, Problem) and result.code == "INVALID_LOGIN"


@pytest.mark.parametrize("identifier", [USERNAME, "managed@example.test", "unknown@example.test"])
def test_password_and_unknown_identifiers_return_the_same_identify_response(h, tmp_path, identifier):
    settings, _, _ = provision(h, tmp_path)
    with client_for(h, settings) as client:
        response = client.post("/auth/identify", json={"identifier": identifier})
    assert response.status_code == 200
    assert response.json() == {"method": "password"}
    assert response.headers["cache-control"] == "no-store"


def test_identify_uses_only_explicit_institutional_binding_and_preserves_local_admin(h, tmp_path, monkeypatch):
    settings, _, identity_id = provision(h, tmp_path)
    config = [provider(identity_id)]
    monkeypatch.setattr(auth_configuration, "current", lambda *_: config[0])
    identify = lambda value: auth.identifier_method(h.app.state.engine, value, "testclient", settings)
    assert identify("MANAGED@EXAMPLE.TEST") == {"method": "institutional"}
    assert identify("unknown@example.test") == {"method": "password"}
    config[0] = provider(h.seed["identities"]["admin"], "institutional_only")
    assert identify("admin") == {"method": "password"}


def test_local_account_collision_cannot_route_to_institutional_identity(h, tmp_path):
    settings, _, local_id = provision(h, tmp_path, issuer="local-development", subject=USERNAME)
    with transaction(h.app.state.engine, write=True) as db:
        local = db.get(m.Identity, local_id)
        institutional = m.Identity(issuer="https://idp.example.test", subject=USERNAME,
                                   display_name="Institutional twin (test)", person_id=local.person_id)
        db.add(institutional)
        db.flush()
        db.add(m.RoleBinding(identity_id=institutional.id, department_id=h.seed["department_id"], role="admin",
                             starts_on=m.now().date()))
        db.add(m.AuthConfiguration(id="environment", data=provider(institutional.id).model_dump(mode="json")))
    with client_for(h, settings) as client:
        identified = client.post("/auth/identify", json={"identifier": USERNAME})
        assert identified.status_code == 200 and identified.json() == {"method": "password"}
        signed_in = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert signed_in.status_code == 200, signed_in.text
        me = client.get("/api/me")
        assert me.status_code == 200 and "admin" not in me.json()["roles"]
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(client.cookies["rota_session"])))
            assert session.identity_id == local_id
            assert db.get(m.Identity, institutional.id).person_id == local.person_id


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("state", ["active", "disabled", "revoked", "future", "expired", "roleless", "unbound",
                                  "wrong_issuer", "off", "ambiguous"])
def test_identify_routes_bound_provider_identity_without_managed_account(request, database, state, tmp_path, monkeypatch):
    h = request.getfixturevalue(database)
    settings, _, identity_id = provision(h, tmp_path, issuer="https://idp.example.test", subject="Hospital.User@example.test")
    if database == "pg":
        settings = production(settings, monkeypatch)
    config = provider(identity_id)
    if state == "unbound":
        config.institutional_bindings = []
    elif state == "wrong_issuer":
        config.institutional_bindings[0].issuer = "https://another-idp.example.test"
    elif state == "off":
        config.institutional_enabled, config.mode = False, "password_only"
    with transaction(h.app.state.engine, write=True) as db:
        db.delete(db.scalar(select(m.Account).where(m.Account.identity_id == identity_id)))
        identity = db.get(m.Identity, identity_id)
        identity.disabled = state == "disabled"
        grant = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id))
        if state == "roleless":
            db.delete(grant)
        elif state == "revoked":
            grant.revoked = True
        elif state == "future":
            grant.starts_on = m.now().date() + timedelta(days=1)
        elif state == "expired":
            grant.ends_on = m.now().date()
        elif state == "ambiguous":
            db.add(m.Identity(issuer=identity.issuer, subject=identity.subject.upper(), display_name="Other (test)"))
        db.add(m.AuthConfiguration(id="environment", data=config.model_dump(mode="json")))
    with client_for(h, settings) as client:
        response = client.post("/auth/identify", json={"identifier": "  HOSPITAL.USER@EXAMPLE.TEST  "})
        assert response.status_code == 200
        assert response.json() == {"method": "institutional" if state == "active" else "password"}
        assert response.headers["cache-control"] == "no-store"
        assert not response.cookies
        unknown = client.post("/auth/identify", json={"identifier": "unknown@example.test"})
        assert unknown.status_code == 200 and unknown.json() == {"method": "password"}
    with transaction(h.app.state.engine) as db:
        assert auth.resolve_identifier(db, "Hospital.User@example.test", settings) == (None, None)


@pytest.mark.parametrize("has_account", [False, True])
def test_identify_known_and_unknown_names_share_query_and_store_work(h, tmp_path, monkeypatch, has_account):
    settings, _, identity_id = provision(h, tmp_path, issuer="https://idp.example.test", subject="Hospital.User@example.test")
    with transaction(h.app.state.engine, write=True) as db:
        if not has_account:
            db.delete(db.scalar(select(m.Account).where(m.Account.identity_id == identity_id)))
        db.add(m.AuthConfiguration(id="environment", data=provider(identity_id).model_dump(mode="json")))
    reads, queries = [], []
    original = identity_store.AutheliaUsersFile.has_credential

    def has_credential(store, username):
        reads.append(True)
        return original(store, username)

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    monkeypatch.setattr(identity_store.AutheliaUsersFile, "has_credential", has_credential)
    event.listen(h.app.state.engine, "before_cursor_execute", query)
    try:
        with client_for(h, settings) as client:
            known = client.post("/auth/identify", json={"identifier": "managed@example.test" if has_account
                                                     else "Hospital.User@example.test"})
            known_queries, queries[:] = queries[:], []
            unknown = client.post("/auth/identify", json={"identifier": "unknown@example.test"})
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", query)
    assert known.json() == {"method": "institutional"} and unknown.json() == {"method": "password"}
    assert known.status_code == unknown.status_code == 200
    assert set(known.json()) == set(unknown.json()) == {"method"}
    assert known_queries == queries and reads == [True, True]


def test_identify_has_a_shared_address_limit_across_rotating_names(h):
    settings = replace(h.settings, password_login_address_limit=1)
    assert auth.identifier_method(h.app.state.engine, "unknown-one", "one-peer", settings) == {"method": "password"}
    with pytest.raises(Problem) as blocked:
        auth.identifier_method(h.app.state.engine, "unknown-two", "one-peer", settings)
    assert blocked.value.status == 429
    assert auth.identifier_method(h.app.state.engine, "unknown-two", "another-peer", settings) == {"method": "password"}


@pytest.mark.parametrize("identifier", [USERNAME, "unknown@example.test"])
def test_rejected_passwords_pay_one_hash_and_share_the_generic_error(h, tmp_path, monkeypatch, identifier):
    settings, _, _ = provision(h, tmp_path)
    verified = []
    original = type(identity_store.HASHER).verify

    def verify(hasher, stored, password):
        verified.append(True)
        return original(hasher, stored, password)

    monkeypatch.setattr(type(identity_store.HASHER), "verify", verify)
    result = attempt(h, settings, identifier, PASSWORD + " incorrect")
    assert isinstance(result, Problem)
    assert (result.status, result.code, result.message) == (401, "INVALID_LOGIN", "Unable to sign in with these credentials")
    assert verified == [True]


def test_wrong_password_known_and_unknown_names_share_select_work(h, tmp_path):
    settings, _, _ = provision(h, tmp_path)
    queries = []

    def query(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(h.app.state.engine, "before_cursor_execute", query)
    try:
        known = attempt(h, settings, password=PASSWORD + " incorrect")
        known_queries, queries[:] = queries[:], []
        unknown = attempt(h, settings, "unknown@example.test", PASSWORD + " incorrect")
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", query)
    assert isinstance(known, Problem) and isinstance(unknown, Problem)
    assert known.code == unknown.code == "INVALID_LOGIN"
    assert known_queries == queries


def test_password_configuration_is_rechecked_after_hashing(h, tmp_path, monkeypatch):
    settings, _, identity_id = provision(h, tmp_path)
    config = [AuthSettings()]
    monkeypatch.setattr(auth_configuration, "current", lambda *_: config[0])
    original = identity_store.AutheliaUsersFile.verify_credential

    def verify(store, username, password):
        credential = original(store, username, password)
        config[0] = provider(identity_id, "institutional_only")
        return credential

    monkeypatch.setattr(identity_store.AutheliaUsersFile, "verify_credential", verify)
    result = attempt(h, settings)
    assert isinstance(result, Problem) and result.code == "INVALID_LOGIN"
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Session)) == 0


def test_both_mode_rejects_password_for_an_explicitly_institutional_account(h, tmp_path, monkeypatch):
    settings, _, identity_id = provision(h, tmp_path)
    monkeypatch.setattr(auth_configuration, "current", lambda *_: provider(identity_id))
    result = attempt(h, settings)
    assert isinstance(result, Problem)
    assert (result.status, result.code, result.message) == (401, "INVALID_LOGIN", "Unable to sign in with these credentials")
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Session)) == 0


def test_institutional_only_rejects_ordinary_local_password(h, tmp_path):
    settings, _, identity_id = provision(h, tmp_path)
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id="environment", data=provider(identity_id, "institutional_only").model_dump(mode="json")))
    with client_for(h, settings) as client:
        response = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert response.status_code == 401 and response.json()["error"]["code"] == "INVALID_LOGIN"
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Session)) == 0


def test_progressive_delay_is_canonical_per_connection_and_resets_after_success(h, tmp_path, monkeypatch):
    settings, _, _ = provision(h, tmp_path)
    settings = replace(settings, password_login_account_limit=100, password_login_progressive_delay=True)
    moment = [m.now().replace(hour=1, minute=0, second=0, microsecond=0)]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    for delay in (1, 2, 4, 8, 16, 32, 60, 60):
        failure = attempt(h, settings, "MANAGED@EXAMPLE.TEST", PASSWORD + " incorrect")
        assert isinstance(failure, Problem) and failure.status == 401
        blocked = attempt(h, settings, USERNAME)
        assert isinstance(blocked, Problem) and blocked.status == 429 and blocked.details["retry_after"] == delay
        moment[0] += timedelta(seconds=delay)
    assert not isinstance(attempt(h, settings, peer="another-network"), Problem)
    assert not isinstance(attempt(h, settings), Problem)
    assert attempt(h, settings, password=PASSWORD + " incorrect").status == 401
    assert attempt(h, settings).details["retry_after"] == 1


def test_progressive_delay_keeps_address_spray_backstop(h):
    settings = replace(h.settings, password_login_address_limit=1)
    assert attempt(h, settings, "unknown-one").status == 401
    assert attempt(h, settings, "unknown-two").status == 429


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("environment", ["development", "production"])
def test_identifier_aliases_share_wave4_consecutive_state_across_addresses(request, tmp_path, monkeypatch, database, environment):
    h = request.getfixturevalue(database)
    settings, _, _ = provision(h, tmp_path)
    settings = replace(settings, environment=environment, password_login_progressive_delay=True,
                       fmuc_data_key="01" * 32 if environment == "production" else "")
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    key = auth.login_account_bucket_keys(USERNAME, settings)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=key, hits=99, expires_at=moment[0] + timedelta(days=30)))
    assert attempt(h, settings, "MANAGED@EXAMPLE.TEST", PASSWORD + " wrong", peer="first-address").status == 401
    for identifier, peer in ((USERNAME, "second-address"), ("managed@example.test", "third-address")):
        blocked = attempt(h, settings, identifier, peer=peer)
        assert blocked.status == 429 and blocked.details["retry_after"] == 60
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 100
    moment[0] += timedelta(seconds=60)
    assert not isinstance(attempt(h, settings, USERNAME, peer="fourth-address"), Problem)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RateLimit, key).hits == 0


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("environment", ["development", "test"])
def test_keyless_progressive_password_login_survives_api_restart(request, database, environment, tmp_path, monkeypatch):
    h = request.getfixturevalue(database)
    settings, _, _ = provision(h, tmp_path)
    settings = replace(settings, environment=environment, password_login_progressive_delay=True, fmuc_data_key="")
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    with client_for(h, settings) as client:
        response = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD + " wrong"})
        assert response.status_code == 401 and response.json()["error"]["code"] == "INVALID_LOGIN"
    with client_for(h, replace(settings)) as restarted:
        blocked = restarted.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert blocked.status_code == 429 and blocked.json()["error"]["details"]["retry_after"] == 1
        moment[0] += timedelta(seconds=1)
        signed_in = restarted.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert signed_in.status_code == 200, signed_in.text


def test_login_api_uses_wave4_delay_for_username_and_email_aliases(h, tmp_path, monkeypatch):
    settings, _, _ = provision(h, tmp_path)
    settings = replace(settings, password_login_progressive_delay=True)
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    key = auth.login_account_bucket_keys(USERNAME, settings)[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=key, hits=99, expires_at=moment + timedelta(days=30)))
    for peer, identifier, expected in (("192.0.2.1", "managed@example.test", 401), ("192.0.2.2", USERNAME, 429)):
        with TestClient(main.create_app(settings, h.app.state.engine), base_url=settings.public_origin,
                        client=(peer, 12345)) as client:
            response = client.post("/auth/login", headers={"origin": settings.public_origin},
                                   json={"username": identifier, "password": PASSWORD + " wrong"})
            assert response.status_code == expected
            if expected == 429:
                assert response.headers["retry-after"] == "60"
                assert response.json()["error"]["details"]["retry_after"] == 60


def test_managed_passwords_are_available_by_default_in_production(h):
    settings = replace(h.settings, environment="production", identity_users_file="/managed/users.yml",
                       password_login_enabled=False)
    assert auth.password_login_enabled(settings)
    assert not auth.password_login_enabled(replace(settings, identity_users_file=""))


@pytest.mark.parametrize("environment", ["development", "test", "production"])
@pytest.mark.parametrize("progressive", [False, True])
def test_password_api_key_requirement_depends_on_environment_and_progressive_mode(h, tmp_path, monkeypatch, environment, progressive):
    path = tmp_path / "users.yml"
    path.write_text('{"users": {}}', encoding="utf-8")
    settings = replace(h.settings, environment=environment, identity_users_file=str(path),
                       password_login_enabled=False, password_login_progressive_delay=progressive, fmuc_data_key="")
    if environment == "production":
        settings = replace(production(replace(settings, database_url="postgresql+psycopg://synthetic@localhost/disposable"),
                                      monkeypatch), fmuc_data_key="")
    if environment == "production" and progressive:
        with pytest.raises(RuntimeError, match="FMUC_DATA_KEY is required for progressive password sign-in"):
            main.create_app(settings, h.app.state.engine)
        return
    app = main.create_app(settings, h.app.state.engine)
    assert app.state.settings is settings
    assert settings.fmuc_data_key == ""


@pytest.mark.parametrize("idle", [None, 12])
@pytest.mark.parametrize("unlimited_max", [False, True])
def test_remembered_sessions_use_the_longer_bound_instead_of_idle_policy(h, idle, unlimited_max):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.session_idle_unlimited = idle is None
        department.session_idle_minutes = idle
        department.session_max_unlimited = unlimited_max
        department.session_max_hours = 4
    result = attempt(h, h.settings, "staff", TEST_PASSWORD, remember_me=True)
    assert not isinstance(result, Problem)
    with transaction(h.app.state.engine) as db:
        session = db.get(m.Session, result["session_id"])
        created = auth.aware(session.created_at)
        expected = created + timedelta(days=30)
        assert session.remember_me and not session.expiry_unlimited
        assert auth.aware(session.expires_at) == expected
        assert auth.aware(result["cookie_expires_at"]) == created + timedelta(days=30)
        assert auth.session_deadline(session, (None, None)) <= created + timedelta(days=30)


def test_ordinary_sessions_keep_existing_limits(h):
    with transaction(h.app.state.engine, write=True) as db:
        department = db.get(m.Department, h.seed["department_id"])
        department.session_idle_unlimited, department.session_max_unlimited = True, False
        department.session_max_hours = 4
    result = attempt(h, h.settings, "staff", TEST_PASSWORD)
    with transaction(h.app.state.engine) as db:
        session = db.get(m.Session, result["session_id"])
        assert not session.remember_me
        assert auth.aware(session.expires_at) == auth.aware(session.created_at) + timedelta(hours=4)


def test_remember_deadline_survives_policy_relaxation_and_keep_alive(h, monkeypatch):
    result = attempt(h, h.settings, "staff", TEST_PASSWORD, remember_me=True)
    with transaction(h.app.state.engine, write=True) as db:
        session = db.get(m.Session, result["session_id"])
        created = auth.aware(session.created_at)
        auth.reconcile_session_limits(db, session.department_id, (60, 8), (None, None))
        assert not session.expiry_unlimited
        assert auth.aware(session.expires_at) == created + timedelta(days=30)
        department = db.get(m.Department, session.department_id)
        department.session_idle_unlimited = department.session_max_unlimited = True
    monkeypatch.setattr(auth, "now", lambda: created + timedelta(days=31))
    auth.keep_alive(h.app.state.engine, result["token"], h.settings)
    with transaction(h.app.state.engine) as db:
        session = db.get(m.Session, result["session_id"])
        assert not auth.session_active(session, limits=(None, None))
        assert auth.aware(session.expires_at) == created + timedelta(days=30)


def test_refresh_requires_csrf_rotates_both_secrets_and_keeps_original_deadline(h):
    with client_for(h, h.settings) as client:
        response = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD, "remember_me": True})
        assert response.status_code == 200
        token, csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(token)))
            before = (session.id, session.created_at, session.expires_at)
        assert client.post("/auth/refresh").status_code == 403
        refreshed = client.post("/auth/refresh", headers={"x-csrf-token": csrf})
        assert refreshed.status_code == 200
        rotated, rotated_csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
        assert rotated != token and rotated_csrf != csrf
        with transaction(h.app.state.engine) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(rotated)))
            assert (session.id, session.created_at, session.expires_at) == before
            with pytest.raises(Problem) as old_token:
                auth.authenticate(db, token, settings=h.settings)
            assert old_token.value.code == "SESSION_EXPIRED"
            with pytest.raises(Problem) as old_csrf:
                auth.authenticate(db, rotated, csrf, write=True, settings=h.settings)
            assert old_csrf.value.code == "CSRF_FAILED"
            assert auth.authenticate(db, rotated, rotated_csrf, write=True, settings=h.settings).identity_id == session.identity_id


def test_inflight_api_response_does_not_clear_concurrently_refreshed_cookies(h, monkeypatch):
    with client_for(h, h.settings) as client:
        response = client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD, "remember_me": True})
        assert response.status_code == 200
        token, csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
        original = main.authenticate
        overlapping = False
        rotated = []

        def authenticate(db, offered, *args, **kwargs):
            nonlocal overlapping
            actor = original(db, offered, *args, **kwargs)
            if offered == token and not overlapping:
                # The API route authenticated; rotate before that earlier response reaches the browser.
                overlapping = True
                refreshed = client.post("/auth/refresh", headers={"x-csrf-token": csrf})
                assert refreshed.status_code == 200
                rotated.append((client.cookies["rota_session"], client.cookies["rota_csrf"]))
            return actor

        monkeypatch.setattr(main, "authenticate", authenticate)
        late = client.get("/api/me")
        assert late.status_code == 200
        assert rotated and "set-cookie" not in late.headers
        assert (client.cookies["rota_session"], client.cookies["rota_csrf"]) == rotated[0]
        assert client.get("/api/me").status_code == 200


@pytest.mark.parametrize("revoked", [False, True])
def test_expired_or_revoked_sessions_cannot_refresh(h, revoked):
    with client_for(h, h.settings) as client:
        assert client.post("/auth/login", json={"username": "staff", "password": TEST_PASSWORD}).status_code == 200
        token, csrf = client.cookies["rota_session"], client.cookies["rota_csrf"]
        with transaction(h.app.state.engine, write=True) as db:
            session = db.scalar(select(m.Session).where(m.Session.token_hash == auth.digest(token)))
            if revoked:
                session.revoked = True
            else:
                session.expires_at = m.now() - timedelta(seconds=1)
        response = client.post("/auth/refresh", headers={"x-csrf-token": csrf})
        assert response.status_code == 401 and "set-cookie" not in response.headers
