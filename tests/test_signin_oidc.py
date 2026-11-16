"""Institutional sign-in protocol and account boundaries; all provider traffic is synthetic."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric import ec, rsa
import httpx
import jwt
import pytest
from sqlalchemy import select

from apps.api import models as m, oidc
from apps.api.auth import current_roles, digest
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.settings import Settings


@pytest.fixture(autouse=True)
def isolated_keys():
    with oidc._JWKS_LOCK:
        oidc._JWKS_CACHE.clear()
    yield
    with oidc._JWKS_LOCK:
        oidc._JWKS_CACHE.clear()


@pytest.fixture(scope="module")
def signing_material():
    return {"RS256": rsa.generate_private_key(public_exponent=65537, key_size=2048),
            "ES256": ec.generate_private_key(ec.SECP256R1())}


def public_key(key, algorithm, kid):
    converter = jwt.algorithms.RSAAlgorithm if algorithm == "RS256" else jwt.algorithms.ECAlgorithm
    return {**json.loads(converter.to_jwk(key.public_key())), "kid": kid, "alg": algorithm, "use": "sig"}


@pytest.fixture
def provider(monkeypatch, signing_material, public_oidc_dns):
    settings = SimpleNamespace(**asdict(Settings(environment="test")))
    settings.oidc_issuer = "https://idp.test/tenant"
    settings.oidc_discovery_url = "https://idp.test/metadata"
    settings.oidc_client_id = "synthetic-client"
    settings.oidc_client_secret = "synthetic-client-secret"
    settings.oidc_redirect_uri = settings.public_origin + "/auth/callback"
    settings.oidc_allowed_hosts = "idp.test"
    settings.oidc_scopes = ["openid", "profile", "email"]
    settings.oidc_username_claim = "hospital_username"
    settings.oidc_email_claim = "email"
    settings.oidc_groups_claim = "hospital_groups"
    settings.oidc_allowed_domains = []
    settings.auth_configuration_revision = 7
    settings.institutional_bindings = []
    moment = int(datetime.now(timezone.utc).timestamp())  # real clock: PyJWT validates token lifetimes against system UTC.
    value = SimpleNamespace(settings=settings, signing_material=signing_material,
        claims={"iss": settings.oidc_issuer, "aud": settings.oidc_client_id, "sub": "hospital-subject",
                "nonce": "expected-nonce", "iat": moment, "exp": moment + 120},
        config={"issuer": settings.oidc_issuer, "authorization_endpoint": "https://idp.test/authorize",
                "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/keys",
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"]},
        keys=[public_key(signing_material["RS256"], "RS256", "key-a")], requests=[], wire_requests=[], userinfo={}, tokens=None,
        client_factory=httpx.Client)

    def token(algorithm="RS256", kid="key-a", **claims):
        headers = {} if kid is None else {"kid": kid}
        return jwt.encode({**value.claims, **claims}, signing_material[algorithm], algorithm=algorithm, headers=headers)

    value.token = token
    value.tokens = {"id_token": token()}

    def handle(request):
        value.wire_requests.append(request)
        request = httpx.Request(request.method, request.url.copy_with(host=request.headers["host"]),
                                headers=request.headers, content=request.content)
        value.requests.append(request)
        if request.url == settings.oidc_discovery_url:
            return httpx.Response(200, json=value.config)
        if request.url == value.config["token_endpoint"]:
            return httpx.Response(200, json=value.tokens)
        if request.url == value.config["jwks_uri"]:
            return httpx.Response(200, json={"keys": value.keys})
        if request.url == value.config.get("userinfo_endpoint"):
            return httpx.Response(200, json=value.userinfo)
        raise AssertionError("Unexpected provider request")

    client = httpx.Client
    monkeypatch.setattr(oidc.httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    return value


def exchange(provider):
    return oidc.exchange(provider.settings, {"nonce": "expected-nonce", "verifier": "one-use-verifier"}, "one-use-code")


def key_requests(provider):
    return [request for request in provider.requests if request.url == provider.config["jwks_uri"]]


def test_configurable_discovery_requires_exact_issuer_and_https_allowlist(provider):
    assert oidc.document(provider.settings)["issuer"] == provider.settings.oidc_issuer
    assert str(provider.requests[0].url) == provider.settings.oidc_discovery_url
    provider.config["issuer"] += "/other"
    with pytest.raises(Problem, match="Unexpected provider issuer"):
        oidc.document(provider.settings)
    provider.settings.oidc_discovery_url = "https://other.test/metadata"
    with pytest.raises(Problem, match="allowlist"):
        oidc.document(provider.settings)


@pytest.mark.parametrize(("name", "value"), [
    ("code_challenge_methods_supported", "S256"), ("code_challenge_methods_supported", ["plain"]),
    ("response_types_supported", ["token"]), ("grant_types_supported", ["implicit"]),
    ("id_token_signing_alg_values_supported", ["HS256"]),
    ("id_token_signing_alg_values_supported", [{}]),
])
def test_discovery_rejects_incompatible_or_malformed_protocol_metadata(provider, name, value):
    provider.config[name] = value
    with pytest.raises(Problem):
        oidc.document(provider.settings)


@pytest.mark.parametrize("url", ["https://idp.test:0/keys", "https://idp.test:65536/keys",
                                "https://idp.test\\@other.test/keys", "https://idp.test/keys\n"])
def test_endpoint_rejects_ambiguous_ports_and_characters(provider, url):
    with pytest.raises(Problem):
        oidc.allowed_url(provider.settings, url)


@pytest.mark.parametrize(("secret", "methods", "expected"), [
    ("synthetic-secret", ["client_secret_post", "client_secret_basic"], "basic"),
    ("synthetic-secret", ["client_secret_post"], "post"),
    ("", ["none"], "public"),
])
def test_exchange_uses_the_supported_client_method(provider, secret, methods, expected):
    provider.settings.oidc_client_secret = secret
    provider.config["token_endpoint_auth_methods_supported"] = methods
    assert exchange(provider)[0]["sub"] == "hospital-subject"
    request = next(request for request in provider.requests if request.method == "POST")
    data = parse_qs(request.content.decode())
    assert data["code_verifier"] == ["one-use-verifier"]
    assert data["redirect_uri"] == [provider.settings.public_origin + "/auth/callback"]
    assert data["grant_type"] == ["authorization_code"]
    if expected == "basic":
        assert request.headers["authorization"].startswith("Basic ")
        assert "client_secret" not in data
    else:
        assert "authorization" not in request.headers
        assert data.get("client_secret") == ([secret] if expected == "post" else None)


def test_basic_client_credentials_are_form_encoded_before_base64(provider):
    provider.settings.oidc_client_id = "client:id +/\u00e9"
    provider.settings.oidc_client_secret = "secret: +/\u00e9"
    provider.claims["aud"] = provider.settings.oidc_client_id
    provider.tokens = {"id_token": provider.token()}
    exchange(provider)
    request = next(request for request in provider.requests if request.method == "POST")
    encoded = request.headers["authorization"].removeprefix("Basic ")
    assert base64.b64decode(encoded).decode() == "client%3Aid+%2B%2F%C3%A9:secret%3A+%2B%2F%C3%A9"


@pytest.mark.parametrize(("secret", "methods"), [("", ["client_secret_basic"]),
    ("synthetic-secret", ["none"]), ("synthetic-secret", ["private_key_jwt"]), ("", "none")])
def test_unsupported_client_method_fails_before_token_request(provider, secret, methods):
    provider.settings.oidc_client_secret = secret
    provider.config["token_endpoint_auth_methods_supported"] = methods
    with pytest.raises(Problem):
        exchange(provider)
    assert not any(request.method == "POST" for request in provider.requests)


@pytest.mark.parametrize("algorithm", ["RS256", "ES256"])
def test_asymmetric_signatures_and_unique_key_without_kid(provider, algorithm):
    provider.keys = [public_key(provider.signing_material[algorithm], algorithm, "key-a")]
    provider.tokens = {"id_token": provider.token(algorithm, kid=None)}
    assert exchange(provider)[0]["sub"] == "hospital-subject"


@pytest.mark.parametrize("claims", [{"nonce": 5}, {"nonce": "non-ascii-\u00e9"}, {"sub": ""},
    {"sub": "x" * 256}, {"sub": "non-ascii-\u00e9"}, {"iat": "123"}, {"exp": True}, {"exp": float("nan")}])
def test_malformed_authenticated_claims_are_rejected(provider, claims):
    provider.tokens = {"id_token": provider.token(**claims)}
    with pytest.raises(Problem):
        exchange(provider)


@pytest.mark.parametrize("tokens", [[], {}, {"id_token": None}, {"id_token": "not-a-jwt"}])
def test_malformed_token_responses_are_rejected(provider, tokens):
    provider.tokens = tokens
    with pytest.raises(Problem):
        exchange(provider)


@pytest.mark.parametrize("mutation", ["symmetric", "wrong_type", "wrong_algorithm", "encrypt_only", "duplicate"])
def test_signing_keys_cannot_cross_algorithm_or_usage_boundaries(provider, mutation):
    if mutation == "symmetric":
        provider.tokens = {"id_token": jwt.encode(provider.claims, "synthetic-hmac-key" * 2, algorithm="HS256")}
    elif mutation == "wrong_type":
        provider.keys[0]["kty"] = "oct"
    elif mutation == "wrong_algorithm":
        provider.keys[0]["alg"] = "HS256"
    elif mutation == "encrypt_only":
        provider.keys[0]["key_ops"] = ["encrypt"]
    else:
        provider.keys *= 2
    with pytest.raises(Problem):
        exchange(provider)


def test_jwks_cache_expires_and_unknown_kid_refresh_is_bounded(provider, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(oidc.time, "monotonic", lambda: clock[0])
    assert exchange(provider)[0]["sub"] == "hospital-subject"
    exchange(provider)
    assert len(key_requests(provider)) == 1
    provider.keys = [public_key(provider.signing_material["RS256"], "RS256", "key-b")]
    provider.tokens = {"id_token": provider.token(kid="key-b")}
    assert exchange(provider)[0]["sub"] == "hospital-subject"
    assert len(key_requests(provider)) == 2
    for kid in ("unknown-a", "unknown-b"):
        provider.tokens = {"id_token": provider.token(kid=kid)}
        with pytest.raises(Problem):
            exchange(provider)
    assert len(key_requests(provider)) == 2
    clock[0] += oidc.JWKS_REFRESH_SECONDS + 1
    with pytest.raises(Problem):
        exchange(provider)
    assert len(key_requests(provider)) == 3
    clock[0] += oidc.JWKS_TTL_SECONDS + 1
    provider.tokens = {"id_token": provider.token(kid="key-b")}
    exchange(provider)
    assert len(key_requests(provider)) == 4


def test_jwks_cache_has_bounded_entries_and_configuration_isolation(provider):
    for revision in range(oidc.JWKS_CACHE_LIMIT + 2):
        provider.settings.auth_configuration_revision = revision
        oidc.signing_keys(provider.settings, provider.config["jwks_uri"])
    assert len(oidc._JWKS_CACHE) == oidc.JWKS_CACHE_LIMIT
    assert len(key_requests(provider)) == oidc.JWKS_CACHE_LIMIT + 2


def test_refreshed_key_removal_never_falls_back_to_the_previous_cached_key(provider):
    exchange(provider)
    provider.keys = []
    provider.tokens = {"id_token": provider.token(kid="new-key")}
    with pytest.raises(Problem):
        exchange(provider)
    provider.tokens = {"id_token": provider.token()}
    with pytest.raises(Problem):
        exchange(provider)
    assert len(key_requests(provider)) == 2


@pytest.mark.parametrize("keys", [[{"kty": "oct", "k": "synthetic"}],
    [{"kty": "RSA", "n": "!", "e": "AQAB"}], [{"kty": "RSA", "use": "enc"}]])
def test_connection_key_projection_contains_only_usable_asymmetric_signing_material(provider, keys):
    provider.keys = keys
    assert oidc.signing_keys(provider.settings, provider.config["jwks_uri"]) == {"keys": []}


def test_weak_rsa_keys_are_not_usable_for_identity_signatures(provider):
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    provider.keys = [public_key(weak, "RS256", "weak")]
    assert oidc.signing_keys(provider.settings, provider.config["jwks_uri"]) == {"keys": []}


def test_concurrent_key_reads_fetch_once(provider):
    start = Barrier(4)

    def read():
        start.wait(timeout=5)
        return oidc.signing_keys(provider.settings, provider.config["jwks_uri"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: read(), range(4)))
    assert all(result["keys"][0]["kid"] == "key-a" for result in results)
    assert len(key_requests(provider)) == 1


def test_userinfo_requires_same_subject_and_matching_verified_email(provider):
    provider.settings.oidc_allowed_domains = ["hospital.test"]
    provider.config["userinfo_endpoint"] = "https://idp.test/userinfo"
    provider.tokens = {"id_token": provider.token(email="person@hospital.test"), "access_token": "synthetic-access"}
    provider.userinfo = {"sub": "other-subject", "email": "person@hospital.test", "email_verified": True}
    assert "email_verified" not in exchange(provider)[0]
    provider.userinfo["sub"] = "hospital-subject"
    provider.userinfo["email"] = "different@hospital.test"
    assert "email_verified" not in exchange(provider)[0]
    provider.userinfo.update(email="person@hospital.test", hospital_username="person", hospital_groups=["admin"])
    claims, _ = exchange(provider)
    assert claims["email_verified"] is True and claims["hospital_username"] == "person"
    assert claims["hospital_groups"] == ["admin"]


def test_flow_persists_remember_me_revision_and_actual_pkce(h, provider):
    provider.settings.public_origin = h.settings.public_origin
    provider.settings.oidc_redirect_uri = h.settings.public_origin + "/auth/callback"
    provider.config["authorization_endpoint"] += "?tenant=clinical"
    with transaction(h.app.state.engine, write=True) as db:
        location, browser = oidc.start_flow(db, provider.settings, provider.config, remember_me=True)
    params = parse_qs(urlparse(location).query)
    assert params["tenant"] == ["clinical"] and params["scope"] == ["openid profile email"]
    with transaction(h.app.state.engine, write=True) as db:
        flow = oidc.consume_flow(db, params["state"][0], browser)
        saved = db.get(m.OIDCFlow, flow["id"])
        assert saved.state_hash == digest(params["state"][0]) and saved.browser_hash == digest(browser)
        assert saved.remember_me and saved.config_revision == 7
    challenge = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert params["code_challenge"] == [challenge]
    assert flow["remember_me"] is True and flow["config_revision"] == 7
    with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
        oidc.consume_flow(db, params["state"][0], browser)


def bind_staff(h, provider):
    identity_id = h.seed["identities"]["staff"]
    provider.settings.institutional_bindings = [
        {"identity_id": identity_id, "issuer": provider.settings.oidc_issuer, "subject": "hospital-subject"}]
    return identity_id


def pending_department_choice(h, provider, *, force_login=False):
    identity_id = bind_staff(h, provider)
    with transaction(h.app.state.engine, write=True) as db:
        second = m.Department(organisation_id=h.seed['organisation_id'], name='Other allowed department (test)')
        foreign = m.Department(organisation_id=h.seed['organisation_id'], name='Foreign department (test)')
        db.add_all([second, foreign])
        db.flush()
        db.add(m.RoleBinding(identity_id=identity_id, department_id=second.id, role='staff', starts_on=datetime(2020, 1, 1).date()))
        second_id, foreign_id = second.id, foreign.id
        location, browser = oidc.start_flow(db, provider.settings, provider.config, remember_me=True, force_login=force_login)
    state = parse_qs(urlparse(location).query)['state'][0]
    with transaction(h.app.state.engine, write=True) as db:
        flow = oidc.consume_flow(db, state, browser)
    with transaction(h.app.state.engine, write=True) as db:
        result = oidc.finish_flow(db, provider.settings, provider.claims, flow_id=flow['id'])
        assert result == {'department_required': True}
        saved = db.get(m.OIDCFlow, flow['id'])
        assert saved.used and saved.verified_identity_id == identity_id and saved.completed_session_id is None
        assert db.scalar(select(m.Session).where(m.Session.auth_method == 'oidc')) is None
    return state, browser, flow['id'], second_id, foreign_id


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_verified_department_choice_is_browser_bound_one_use_and_uses_current_grants(request, database, provider):
    h = request.getfixturevalue(database)
    state, browser, flow_id, selected, foreign = pending_department_choice(h, provider, force_login=True)
    with transaction(h.app.state.engine) as db:
        choices = oidc.department_choices(db, provider.settings, state, browser)
        assert set(choices) == {'expires_at', 'departments'}
        assert {d['id'] for d in choices['departments']} == {h.seed['department_id'], selected}
        assert all(set(d) == {'id', 'name'} for d in choices['departments'])
        grants = [(r.id, r.role, r.revoked) for r in db.scalars(select(m.RoleBinding).order_by(m.RoleBinding.id))]
    with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
        oidc.choose_department(db, provider.settings, state, browser, foreign)
    with transaction(h.app.state.engine, write=True) as db:
        values = oidc.choose_department(db, provider.settings, state, browser, selected, reauthenticate=True,
                                        correlation_id='synthetic-department-redemption')
        session = db.get(m.Session, values['session_id'])
        assert session.identity_id == h.seed['identities']['staff'] and session.department_id == selected
        assert session.remember_me and session.auth_method == 'oidc'
        assert current_roles(db, session.identity_id, selected) == frozenset({'staff'})
        assert db.get(m.OIDCFlow, flow_id).completed_session_id == session.id
        assert db.scalar(select(m.AuditEvent).where(m.AuditEvent.correlation_id == 'synthetic-department-redemption',
                                                  m.AuditEvent.action == 'auth.sign_in')) is not None
        assert [(r.id, r.role, r.revoked) for r in db.scalars(select(m.RoleBinding).order_by(m.RoleBinding.id))] == grants
    for operation in ('consume', 'finish', 'choose'):
        with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
            if operation == 'consume':
                oidc.consume_flow(db, state, browser)
            elif operation == 'finish':
                oidc.finish_flow(db, provider.settings, provider.claims, flow_id=flow_id)
            else:
                oidc.choose_department(db, provider.settings, state, browser, selected)


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('invalid', ['browser', 'configuration', 'expired', 'cancelled', 'disabled',
                                    'empty_rights', 'changed_rights', 'reauthentication'])
def test_pending_verified_choice_rechecks_invalidation_before_any_session(request, database, provider, invalid):
    h = request.getfixturevalue(database)
    state, browser, flow_id, selected, _ = pending_department_choice(h, provider)
    with transaction(h.app.state.engine, write=True) as db:
        flow = db.get(m.OIDCFlow, flow_id)
        if invalid == 'expired':
            flow.expires_at = m.now() - timedelta(seconds=1)
        elif invalid == 'cancelled':
            oidc.cancel_flow(db, browser, continuation=state)
        elif invalid == 'disabled':
            db.get(m.Identity, flow.verified_identity_id).disabled = True
        elif invalid in {'empty_rights', 'changed_rights'}:
            for grant in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == flow.verified_identity_id)):
                if invalid == 'empty_rights' or grant.department_id == selected:
                    grant.revoked = True
    if invalid == 'configuration':
        provider.settings.auth_configuration_revision += 1
    if invalid == 'changed_rights':
        with transaction(h.app.state.engine) as db:
            assert {d['id'] for d in oidc.department_choices(db, provider.settings, state, browser)['departments']} == {h.seed['department_id']}
    with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
        oidc.choose_department(db, provider.settings, state, 'foreign-browser' if invalid == 'browser' else browser,
                               selected, reauthenticate=invalid == 'reauthentication')
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Session).where(m.Session.auth_method == 'oidc')) is None
        flow = db.get(m.OIDCFlow, flow_id)
        assert flow.used and flow.completed_session_id is None


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_concurrent_department_redemption_creates_one_session_and_cancel_revokes_it(request, database, provider):
    from apps.api.db import retry_write
    h = request.getfixturevalue(database)
    state, browser, flow_id, selected, _ = pending_department_choice(h, provider)
    start = Barrier(2)
    def redeem(_):
        start.wait(timeout=10)
        try:
            return retry_write(h.app.state.engine, lambda db: oidc.choose_department(db, provider.settings, state, browser, selected))
        except Problem as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(redeem, range(2)))
    assert sum(isinstance(value, dict) for value in outcomes) == 1 and 'OIDC_STATE' in outcomes
    with transaction(h.app.state.engine, write=True) as db:
        sessions = list(db.scalars(select(m.Session).where(m.Session.auth_method == 'oidc')))
        assert len(sessions) == 1 and db.get(m.OIDCFlow, flow_id).completed_session_id == sessions[0].id
        oidc.cancel_flow(db, browser, continuation=state)
        assert sessions[0].revoked and db.get(m.OIDCFlow, flow_id).used


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_failed_department_session_issue_rolls_back_without_resetting_exchange(request, database, provider, monkeypatch):
    h = request.getfixturevalue(database)
    state, browser, flow_id, selected, _ = pending_department_choice(h, provider)
    original = oidc.issue_session
    def fail_after_issue(*args, **kwargs):
        original(*args, **kwargs)
        raise Problem(409, 'SYNTHETIC_SESSION_FAILURE', 'Synthetic session transaction rollback')
    monkeypatch.setattr(oidc, 'issue_session', fail_after_issue)
    with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
        oidc.choose_department(db, provider.settings, state, browser, selected)
    with transaction(h.app.state.engine) as db:
        flow = db.get(m.OIDCFlow, flow_id)
        assert flow.used and flow.verified_identity_id and flow.completed_session_id is None
        assert db.scalar(select(m.Session).where(m.Session.auth_method == 'oidc')) is None


def test_explicit_subject_binding_preserves_local_identity_and_never_grants_claim_roles(h, provider):
    identity_id = bind_staff(h, provider)
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, identity_id)
        previous = identity.issuer, identity.subject, identity.password_hash, identity.person_id
        flow = m.OIDCFlow(state_hash=digest("state"), browser_hash=digest("browser"), nonce="nonce", verifier="verifier",
                          used=True, remember_me=True, config_revision=7, expires_at=m.now() + timedelta(minutes=5))
        db.add(flow)
        db.flush()
        values = oidc.finish_flow(db, provider.settings, {**provider.claims, "hospital_groups": ["admin"], "roles": ["admin"]}, flow_id=flow.id)
        session = db.get(m.Session, values["session_id"])
        assert session.identity_id == identity_id and session.remember_me and session.auth_method == "oidc"
        assert "admin" not in current_roles(db, identity_id, session.department_id)
        assert (identity.issuer, identity.subject, identity.password_hash, identity.person_id) == previous


def test_runtime_never_falls_back_to_issuer_subject_username_or_email_matching(h, provider):
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, h.seed["identities"]["admin"])
        identity.issuer, identity.subject = provider.settings.oidc_issuer, provider.claims["sub"]
    with pytest.raises(Problem, match="add your hospital sign-in"), transaction(h.app.state.engine, write=True) as db:
        oidc.finish_flow(db, provider.settings, {**provider.claims, "preferred_username": "admin", "email": "admin@hospital.test"})


@pytest.mark.parametrize(("email", "verified"), [("person@hospital.test", False), ("person@hospital.test", "true"),
    ("person@hospital.test.attacker.test", True), ("person@other.test", True), (None, True), ("person@@hospital.test", True)])
def test_email_domain_filter_requires_exact_verified_address(h, provider, email, verified):
    bind_staff(h, provider)
    provider.settings.oidc_allowed_domains = ["hospital.test"]
    with pytest.raises(Problem, match="verified institutional email"), transaction(h.app.state.engine, write=True) as db:
        oidc.finish_flow(db, provider.settings, {**provider.claims, "email": email, "email_verified": verified})


def test_email_domain_filter_uses_configured_claim_case_insensitively(h, provider):
    bind_staff(h, provider)
    provider.settings.oidc_email_claim = "hospital_email"
    provider.settings.oidc_allowed_domains = ["hospital.test"]
    with transaction(h.app.state.engine, write=True) as db:
        assert oidc.finish_flow(db, provider.settings, {**provider.claims, "hospital_email": "person@HOSPITAL.TEST", "email_verified": True})["token"]


@pytest.mark.parametrize("state", ["stale_configuration", "expired", "disabled", "missing_binding", "empty_rights"])
def test_invalidated_flows_and_disabled_bindings_cannot_issue_sessions(h, provider, state):
    identity_id = bind_staff(h, provider)
    with transaction(h.app.state.engine, write=True) as db:
        flow = m.OIDCFlow(state_hash=digest("state"), browser_hash=digest("browser"), nonce="nonce", verifier="verifier",
                          used=True, config_revision=6 if state == "stale_configuration" else 7,
                          expires_at=m.now() + timedelta(minutes=-1 if state == "expired" else 5))
        db.add(flow)
        db.flush()
        flow_id = flow.id
        if state == "disabled":
            db.get(m.Identity, identity_id).disabled = True
        if state == "missing_binding":
            provider.settings.institutional_bindings[0]["identity_id"] = "no-such-identity"
        if state == "empty_rights":
            for grant in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity_id)):
                grant.revoked = True
    with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
        oidc.finish_flow(db, provider.settings, provider.claims, flow_id=flow_id)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.OIDCFlow, flow_id).completed_session_id is None


def test_exact_callback_is_checked_before_authorization_or_exchange(h, provider):
    provider.settings.oidc_redirect_uri = "https://other.test/auth/callback"
    with pytest.raises(Problem), transaction(h.app.state.engine, write=True) as db:
        oidc.start_flow(db, provider.settings, provider.config)
    with pytest.raises(Problem):
        exchange(provider)
    assert not any(request.method == "POST" for request in provider.requests)


def test_oidc_logout_identifies_client_without_sending_identity_claims(provider):
    provider.config["end_session_endpoint"] = "https://idp.test/logout?tenant=clinical"
    location = oidc.logout_url(provider.settings, "oidc", "browser-bound-state")
    assert parse_qs(urlparse(location).query) == {
        "tenant": ["clinical"], "client_id": [provider.settings.oidc_client_id],
        "post_logout_redirect_uri": [provider.settings.public_origin + "/"], "state": ["browser-bound-state"]}
    assert provider.tokens["id_token"] not in location
    assert provider.requests and all(request.url == provider.settings.oidc_discovery_url for request in provider.requests)


@pytest.mark.parametrize("method", ["password", None, "", "unsupported"])
def test_non_oidc_logout_does_not_contact_provider(provider, method):
    assert oidc.logout_url(provider.settings, method, "state") is None
    assert provider.requests == []
