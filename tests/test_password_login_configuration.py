"""Password sign-in trusts one managed identity source and bounded operator settings."""
from dataclasses import replace
import json

import pytest

from apps.api.settings import Settings, TEST_ONLY_FLAGS


@pytest.fixture
def production(tmp_path, monkeypatch):
    for flag in TEST_ONLY_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    path = tmp_path / "users.yml"
    path.write_text(json.dumps({"users": {}}), encoding="utf-8")
    return Settings(environment="production", database_url="postgresql+psycopg://synthetic@db.invalid/rota",
                    public_origin="https://rota.test", identity_users_file=str(path), password_login_enabled=True,
                    oidc_issuer="https://rota.test/authelia", oidc_client_id="synthetic-client",
                    oidc_client_secret="synthetic-test-only", oidc_redirect_uri="https://rota.test/auth/callback",
                    oidc_allowed_hosts="rota.test", forwarded_allow_ips="192.0.2.17", fmuc_data_key="01" * 32)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("issuer", ["https://hospital.test/oidc", "https://rota.test/authelia/other",
                                   "https://rota.test.attacker.test/authelia"])
def test_external_oidc_without_users_file_still_requires_managed_password_store(production, enabled, issuer):
    with pytest.raises(RuntimeError, match="managed identity store"):
        replace(production, password_login_enabled=enabled, identity_users_file="", oidc_issuer=issuer).check()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("issuer", ["https://hospital.test/oidc", "https://rota.test/authelia/other",
                                   "https://rota.test.attacker.test/authelia"])
def test_production_refuses_users_file_with_any_external_issuer(production, enabled, issuer):
    with pytest.raises(RuntimeError, match="cannot be combined with an external OIDC issuer"):
        replace(production, password_login_enabled=enabled, oidc_issuer=issuer).check()


@pytest.mark.parametrize("enabled", [False, True])
def test_production_requires_managed_passwords_alongside_any_legacy_provider(production, enabled):
    replace(production, password_login_enabled=enabled).check()
    with pytest.raises(RuntimeError, match="managed identity store"):
        replace(production, password_login_enabled=enabled, identity_users_file="").check()


def test_password_only_production_and_worker_remain_valid(production):
    settings = replace(production, oidc_issuer="", oidc_client_id="", oidc_client_secret="",
                       oidc_redirect_uri="", oidc_allowed_hosts="")
    settings.check()
    replace(settings, identity_users_file="", password_login_enabled=False).check(require_authentication=False)
    with pytest.raises(RuntimeError, match="HTTPS"):
        replace(settings, identity_users_file="", public_origin="http://rota.test").check(require_authentication=False)


@pytest.mark.parametrize("environment", ["test", "development"])
def test_nonproduction_identity_fixtures_can_use_an_external_issuer(production, environment):
    replace(production, environment=environment, oidc_issuer="https://hospital.test/oidc").check()


def test_password_login_limits_load_from_environment_and_stay_off_by_default(monkeypatch):
    monkeypatch.delenv("ROTA_PASSWORD_LOGIN_ENABLED", raising=False)
    values = {"password_login_account_limit": 12, "password_login_pair_limit": 60, "password_login_address_limit": 4000,
              "password_login_window_seconds": 1800, "password_login_audit_retention_days": 90}
    for name, value in values.items():
        monkeypatch.setenv("ROTA_" + name.upper(), str(value))
    settings = Settings.from_env()
    assert not settings.password_login_enabled
    assert settings.password_login_progressive_delay
    assert {name: getattr(settings, name) for name in values} == values


@pytest.mark.parametrize(("name", "minimum", "maximum"), [
    ("password_login_account_limit", 1, 1000), ("password_login_pair_limit", 1, 1000),
    ("password_login_address_limit", 1, 100000),
    ("password_login_window_seconds", 60, 86400), ("password_login_audit_retention_days", 30, 3650),
])
def test_password_login_limits_require_positive_bounded_values(name, minimum, maximum):
    for value in (minimum, maximum):
        replace(Settings(environment="test"), **{name: value}).check()
    for value in (0, minimum - 1, maximum + 1):
        with pytest.raises(RuntimeError, match="ROTA_" + name.upper()):
            replace(Settings(environment="test"), **{name: value}).check()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("progressive", [False, True])
def test_production_progressive_password_startup_requires_the_key(production, h, enabled, progressive):
    from apps.api.main import create_app
    settings = replace(production, fmuc_data_key="", password_login_enabled=enabled,
                       password_login_progressive_delay=progressive)
    if progressive:
        with pytest.raises(RuntimeError, match="FMUC_DATA_KEY is required for progressive password sign-in"):
            create_app(settings, h.app.state.engine)
    else:
        assert create_app(settings, h.app.state.engine).state.settings is settings
    replace(settings, identity_users_file="").check(require_authentication=False)


def test_signin_bucket_key_uses_the_existing_environment_secret(monkeypatch):
    from apps.api import auth
    monkeypatch.setenv("FMUC_DATA_KEY", "01" * 32)
    settings = Settings.from_env()
    assert settings.fmuc_data_key == "01" * 32
    assert auth.login_account_bucket_keys("Staff", settings) == auth.login_account_bucket_keys("staff", settings)
    assert auth.login_account_bucket_keys("Straße", settings) == auth.login_account_bucket_keys("STRASSE", settings)
    assert any(auth.login_account_bucket_keys(f"probe-{i}", settings) != auth.login_account_bucket_keys(
        f"probe-{i}", replace(settings, fmuc_data_key="02" * 32)) for i in range(10))
    import base64
    encoded = base64.b64encode(bytes.fromhex("01" * 32)).decode()
    assert auth.login_account_bucket_keys("staff", settings) == auth.login_account_bucket_keys(
        "staff", replace(settings, fmuc_data_key=encoded))
    assert "01" * 32 not in repr(settings)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_keyless_signin_buckets_are_stable_in_nonproduction(environment):
    from apps.api import auth
    settings = Settings(environment=environment)
    assert auth.login_account_bucket_keys("Straße", settings) == auth.login_account_bucket_keys(
        "STRASSE", Settings(environment="test"))
    assert auth.login_account_bucket_keys("unknown-person", settings) == auth.login_account_bucket_keys(
        "unknown-person", Settings(environment=environment))


@pytest.mark.parametrize("enabled", [False, True])
def test_production_keyless_bucket_derivation_fails_closed(production, enabled):
    from apps.api import auth
    from apps.api.errors import Problem
    settings = replace(production, fmuc_data_key="", password_login_enabled=enabled)
    with pytest.raises(Problem) as refusal:
        auth.login_account_bucket_keys("victim@example.test", settings)
    assert (refusal.value.status, refusal.value.code) == (503, "LOGIN_UNCONFIGURED")
    assert "victim" not in refusal.value.message
