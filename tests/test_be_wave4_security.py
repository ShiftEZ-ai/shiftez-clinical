"""Audit security fixes and reviewed deployment policies; synthetic local fixtures only."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import socket

import pytest
from fastapi.testclient import TestClient

from .clock_support import freeze_clock
from apps.api import auth, models as m
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.main import create_app
from apps.api.settings import Settings
from apps.worker import email_delivery as email
from packages.contracts.schemas import LoginInput
from .conftest import TEST_PASSWORD
from .test_accounts import acc, anonymous, invite, token_of, PASSWORD  # noqa: F401


def test_invite_link_limit_is_per_link_not_shared_hospital_address(acc, monkeypatch):
    freeze_clock(monkeypatch)
    with anonymous(acc) as client:
        for index in range(11):
            token = token_of(invite(acc, username=f"new.person.{index}"))
            assert client.get(f"/api/account-links/{token}").status_code == 200
            assert client.post(f"/api/account-links/{token}", json={"password": PASSWORD}).status_code == 200
        for _ in range(10):
            assert client.post("/api/account-links/invalid-link", json={"password": PASSWORD}).status_code == 404
        assert client.post("/api/account-links/invalid-link", json={"password": PASSWORD}).status_code == 429
        assert client.post("/api/account-links/another-link", json={"password": PASSWORD}).status_code == 404


def test_link_reads_have_separate_per_link_and_high_address_backstops(h, monkeypatch):
    freeze_clock(monkeypatch)
    from apps.api.rate_limit import hit
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        for index in range(61):
            assert client.get(f"/api/account-links/invalid-{index}").status_code == 404
        for _ in range(59):
            assert client.get("/api/account-links/invalid-0").status_code == 404
        assert client.get("/api/account-links/invalid-0").status_code == 429
        with transaction(h.app.state.engine, write=True) as db:
            for _ in range(1000):
                hit(db.connection(), "account-link-address", auth.login_digest("peer:testclient", h.settings), 900, m.now())
        assert client.get("/api/account-links/unique-link").status_code == 429


def test_security_policy_settings_follow_owner_defaults_and_load_from_environment(monkeypatch):
    defaults = Settings(environment="test")
    assert defaults.password_login_progressive_delay
    assert defaults.smtp_private_host_allowlist == ""
    assert (defaults.db_pool_size, defaults.db_max_overflow) == (5, 10)
    monkeypatch.delenv("ROTA_PASSWORD_LOGIN_PROGRESSIVE_DELAY", raising=False)
    assert Settings.from_env().password_login_progressive_delay
    for key, value in {"ROTA_PASSWORD_LOGIN_PROGRESSIVE_DELAY": "false",
                       "FMUC_SMTP_PRIVATE_HOST_ALLOWLIST": "relay.test",
                       "ROTA_DB_POOL_SIZE": "3", "ROTA_DB_MAX_OVERFLOW": "0"}.items():
        monkeypatch.setenv(key, value)
    configured = Settings.from_env()
    configured.check()
    assert not configured.password_login_progressive_delay  # Explicit fixed-window rollback only.
    assert configured.smtp_private_host_allowlist == "relay.test"
    assert (configured.db_pool_size, configured.db_max_overflow) == (3, 0)


@pytest.mark.parametrize("name,low,high", [("db_pool_size", 1, 100),
                                           ("db_max_overflow", 0, 100)])
def test_security_and_pool_limits_are_bounded(name, low, high):
    for value in (low, high):
        replace(Settings(environment="test"), **{name: value}).check()
    for value in (low - 1, high + 1):
        with pytest.raises(RuntimeError, match="ROTA_" + name.upper()):
            replace(Settings(environment="test"), **{name: value}).check()




def test_progressive_login_delay_is_per_pair_and_resets_after_success(h, monkeypatch):
    # Keep the complete delay sequence inside one fixed window, even late in a long CI shard.
    moment = [m.now().replace(minute=0, second=0, microsecond=0)]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    settings = replace(h.settings, password_login_pair_limit=8, password_login_account_limit=100)
    def attempt(password="wrong password", peer="first-network"):
        return auth.password_login(h.app.state.engine, LoginInput(username="staff", password=password), peer, settings)
    for delay in (1, 2, 4, 8, 16, 32, 60, 60, 60, 60):
        failed = attempt()
        assert isinstance(failed, Problem) and failed.status == 401
        blocked = attempt()
        assert isinstance(blocked, Problem) and blocked.status == 429 and blocked.details["retry_after"] == delay
        assert "this connection" in blocked.message
        moment[0] += timedelta(seconds=delay)
    assert not isinstance(attempt(TEST_PASSWORD, "another-network"), Problem)
    assert not isinstance(attempt(TEST_PASSWORD), Problem)
    assert attempt().status == 401
    assert attempt().details["retry_after"] == 1


def test_progressive_pair_delay_restarts_in_the_next_fixed_window(h, monkeypatch):
    settings = replace(h.settings, password_login_account_limit=100)
    end = m.now().replace(minute=0, second=0, microsecond=0) + timedelta(seconds=settings.password_login_window_seconds)
    moment = [end - timedelta(seconds=3)]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    data = LoginInput(username="staff", password="wrong password")
    def attempt():
        return auth.password_login(h.app.state.engine, data, "same-network", settings)
    assert attempt().status == 401
    assert attempt().details["retry_after"] == 1
    moment[0] += timedelta(seconds=1)
    assert attempt().status == 401
    assert attempt().details["retry_after"] == 2
    moment[0] = end
    assert attempt().status == 401
    assert attempt().details["retry_after"] == 1


@pytest.mark.parametrize("username", ["staff", "unknown-person"])
def test_progressive_rotating_addresses_share_one_second_account_ceiling(h, monkeypatch, username):
    from sqlalchemy import select
    from apps.api.rate_limit import counter_key
    from packages.domain.time import aware
    moment = [m.now()]
    monkeypatch.setattr(auth, "now", lambda: moment[0])
    settings = replace(h.settings, password_login_account_limit=3)
    verified = []
    original = auth.PASSWORDS.verify
    def verify(*args):
        verified.append(True)
        return original(*args)
    monkeypatch.setattr(auth, "PASSWORDS", SimpleNamespace(verify=verify))
    def attempt(peer, password="wrong password", name=username):
        return auth.password_login(h.app.state.engine, LoginInput(username=name, password=password), peer, settings)
    for index in range(3):
        assert attempt(f"attacker-{index}").status == 401
    account = auth.login_keys(username, "unused", settings)[0]
    delay_key = auth.login_account_bucket_keys(username, settings)[1]
    with transaction(h.app.state.engine) as db:
        deadline = aware(db.get(m.RateLimit, delay_key).expires_at)
    assert deadline == moment[0] + timedelta(seconds=1)
    for index in range(12):
        blocked = attempt(f"rotating-{index}", TEST_PASSWORD)
        assert blocked.status == 429 and blocked.details["retry_after"] == 1
        assert blocked.message == "Too many sign-in attempts for this account; try again in 1 second"
    with transaction(h.app.state.engine) as db:
        assert aware(db.get(m.RateLimit, delay_key).expires_at) == deadline
        key, _ = counter_key("login-account", account, settings.password_login_window_seconds, moment[0])
        assert db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == key)) == 3
    assert len(verified) == 3  # Rejected guesses never reach the verifier.
    assert not isinstance(attempt("shared-hospital", TEST_PASSWORD, "approver"), Problem)
    moment[0] = deadline
    result = attempt("victim-network", TEST_PASSWORD)
    if username == "unknown-person":
        assert isinstance(result, Problem) and result.status == 401
    else:
        assert not isinstance(result, Problem)


def test_progressive_login_retains_shared_address_spray_backstop(h, monkeypatch):
    freeze_clock(monkeypatch)
    settings = replace(h.settings, password_login_address_limit=1)
    for username, status in (("unknown-one", 401), ("unknown-two", 429)):
        result = auth.password_login(h.app.state.engine, LoginInput(username=username, password="invalid password"),
                                     "shared-address", settings)
        assert isinstance(result, Problem) and result.status == status


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["192.0.2.11"], ["169.254.169.254"], ["::1"],
                                       ["::ffff:127.0.0.1"], ["224.0.0.1"], ["ff02::1"], ["8.8.8.8", "192.0.2.21"]])
def test_private_smtp_addresses_and_mixed_dns_are_refused_before_connect(monkeypatch, addresses):
    relay = object.__new__(email.RestrictedSMTP)
    relay.allowed_hosts = set()
    records = [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 587))
               for address in addresses]
    monkeypatch.setattr(email.socket, "getaddrinfo", lambda *args, **kwargs: records)
    monkeypatch.setattr(email.socket, "socket", lambda *args: pytest.fail("Refused host must not connect"))
    with pytest.raises(email.DeliveryError) as rejected:
        relay._get_socket("relay.test", 587, 3)
    assert rejected.value.code == "SMTP_UNCONFIGURED" and rejected.value.draft


@pytest.mark.parametrize("address,allowlist", [("8.8.8.8", set()), ("192.0.2.11", {"relay.test"})])
def test_smtp_connects_to_pinned_address_and_allowlist_is_exact(monkeypatch, address, allowlist):
    relay = object.__new__(email.RestrictedSMTP)
    relay.allowed_hosts = allowlist
    connections = []
    sock = SimpleNamespace(settimeout=lambda timeout: None, connect=lambda target: connections.append(target))
    monkeypatch.setattr(email.socket, "getaddrinfo", lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 587))])
    monkeypatch.setattr(email.socket, "socket", lambda *args: sock)
    assert relay._get_socket("relay.test", 587, 3) is sock
    assert connections == [(address, 587)]
    if allowlist:
        with pytest.raises(email.DeliveryError):
            relay._get_socket("other-relay.test", 587, 3)


def test_revocation_purges_a_legacy_oidc_token_without_rewriting_other_sessions():
    row = SimpleNamespace(revoked=False, revision=1, expiry_unlimited=False, oidc_id_token="legacy-private-claims")
    auth.revoke_session(row)
    assert row.revoked and row.revision == 2 and row.oidc_id_token is None


def test_enabled_smtp_guard_is_used_by_delivery_and_preserves_tls_hostname(monkeypatch):
    config = SimpleNamespace(sender_address='sender@example.test', smtp_username='', smtp_host='relay.test',
                             smtp_port=587, timeout_seconds=3, smtp_starttls=False)
    monkeypatch.setattr(email.socket, 'getaddrinfo', lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.0.2.11', 587))])
    with pytest.raises(email.DeliveryError) as rejected:
        email.send_smtp(None, None, None, config, Settings(environment='test'))
    assert rejected.value.code == 'SMTP_UNCONFIGURED'
    relay = object.__new__(email.RestrictedSMTP)
    relay._host, relay.sock = 'relay.test', object()
    relay.ehlo_or_helo_if_needed = lambda: None
    relay.has_extn = lambda name: name == 'starttls'
    relay.docmd = lambda command: (220, b'ready')
    seen = []
    context = SimpleNamespace(wrap_socket=lambda sock, *, server_hostname: seen.append(server_hostname))
    relay.starttls(context=context)
    assert seen == ['relay.test']


@pytest.mark.parametrize('environment', ['test', 'production'])
def test_cross_origin_and_transport_security_headers(h, monkeypatch, environment, tmp_path):
    from apps.api.settings import TEST_ONLY_FLAGS
    for flag in TEST_ONLY_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    users = tmp_path / 'users.yml'
    users.write_text('{"users": {}}', encoding='utf-8')
    public_origin = 'https://rota.test'
    settings = replace(h.settings, environment=environment, identity_users_file=str(users), fmuc_data_key='01' * 32,
                       database_url='postgresql+psycopg://synthetic@localhost/unused',
                       public_origin=public_origin, forwarded_allow_ips='192.0.2.17',
                       oidc_issuer=public_origin + "/authelia", oidc_client_id='rota-test', oidc_client_secret='synthetic',
                       oidc_redirect_uri='https://rota.test/auth/callback', oidc_allowed_hosts='idp.test')
    with TestClient(create_app(settings, h.app.state.engine), base_url=settings.public_origin) as client:
        response = client.get('/')
    assert response.headers['cross-origin-opener-policy'] == 'same-origin'
    assert response.headers['cross-origin-resource-policy'] == 'same-origin'
    assert response.headers['x-permitted-cross-domain-policies'] == 'none'
    assert ('strict-transport-security' in response.headers) is (environment == 'production')
    if environment == 'production':
        assert response.headers['strict-transport-security'] == 'max-age=63072000; includeSubDomains'




def test_owner_keeps_unlimited_sessions_available_without_an_absolute_cap(h, monkeypatch):
    me = h.get('admin', '/api/me')
    saved = h.command('admin', 'department.sessions', {'idle_minutes': 60, 'max_hours': 24,
        'idle_unlimited': True, 'max_unlimited': True}, me['department']['id'], me['department']['revision'])
    assert saved['sessions']['idle_unlimited'] and saved['sessions']['max_unlimited']
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers['origin'] = h.settings.public_origin
        signed = client.post('/auth/login', json={'username': 'staff', 'password': TEST_PASSWORD})
        assert signed.status_code == 200 and signed.json()['expires_at'] is None
        future = m.now() + timedelta(days=3650)
        monkeypatch.setattr(auth, 'now', lambda: future)
        assert client.get('/api/me').status_code == 200


def test_default_managed_sign_in_returns_pair_delay_and_retry_after(h, tmp_path, monkeypatch):
    from .test_password_login import provision, client_for, post_login
    moment = [m.now()]
    monkeypatch.setattr(auth, 'now', lambda: moment[0])
    settings, _, _ = provision(h, tmp_path)
    settings = replace(settings, password_login_progressive_delay=Settings().password_login_progressive_delay)
    with client_for(h, settings) as client:
        assert post_login(client, password='wrong password').status_code == 401
        blocked = post_login(client)
        assert blocked.status_code == 429 and blocked.headers['retry-after'] == '1'
        assert blocked.json()['error']['message'] == 'Too many sign-in attempts for this account from this connection; try again in 1 second'
        moment[0] += timedelta(seconds=1)
        assert post_login(client).status_code == 200
