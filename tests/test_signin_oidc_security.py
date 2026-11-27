"""Public-network OIDC endpoints and issuer-scoped identity links; no live provider traffic."""
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from apps.api import models as m, oidc
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import auth_configuration as config
from packages.contracts.schemas import AuthSettings
from .test_signin_config import PATH, provider as configuration
from .test_signin_oidc import bind_staff, isolated_keys, provider, signing_material  # noqa: F401


UNSAFE_ADDRESSES = ["127.0.0.1", "192.0.2.10", "192.0.2.16", "192.0.2.20", "169.254.169.254",
                    "0.0.0.0", "192.0.2.14", "224.0.0.1", "::1", "::", "fe80::1", "fc00::1",
                    "ff02::1", "::ffff:127.0.0.1"]


def dns_addresses(monkeypatch, addresses):
    monkeypatch.setattr(oidc.socket, "getaddrinfo", lambda host, port, **kwargs: [
        (oidc.socket.AF_INET6 if ":" in address else oidc.socket.AF_INET, oidc.socket.SOCK_STREAM,
         6, "", (address, port)) for address in addresses])


@pytest.mark.parametrize("address", UNSAFE_ADDRESSES)
def test_allowlisted_endpoint_rejects_private_literal_and_dns_addresses(monkeypatch, address):
    dns_addresses(monkeypatch, [address])
    settings = SimpleNamespace(oidc_allowed_hosts="idp.test," + address)
    host = "[" + address + "]" if ":" in address else address
    for url in ("https://idp.test/metadata", "https://" + host + "/metadata"):
        with pytest.raises(Problem):
            oidc.allowed_url(settings, url)


def test_endpoint_rejects_mixed_public_private_dns_answers_and_resolution_failure(monkeypatch):
    settings = SimpleNamespace(oidc_allowed_hosts="idp.test")
    dns_addresses(monkeypatch, ["93.184.216.34", "169.254.169.254"])
    with pytest.raises(Problem):
        oidc.allowed_url(settings, "https://idp.test/metadata")

    def unavailable(*args, **kwargs):
        raise oidc.socket.gaierror("Synthetic unavailable DNS")

    monkeypatch.setattr(oidc.socket, "getaddrinfo", unavailable)
    with pytest.raises(Problem):
        oidc.allowed_url(settings, "https://idp.test/metadata")


@pytest.mark.parametrize("host", ["localhost", "postgres", "idp.localhost", "127.0.0.1", "192.0.2.10", "192.0.2.16",
                                 "192.0.2.20", "169.254.169.254", "0.0.0.0", "192.0.2.14", "224.0.0.1",
                                 "93.184.216.34", "-idp.test", "idp-.test", "idp.-test", "idp.test-", "idp..test",
                                 "0x5d.0xb8.0xd8.0x22", "a" * 64 + ".test"])
def test_provider_configuration_rejects_nonpublic_host_literals(host):
    with pytest.raises(ValidationError):
        AuthSettings(allowed_hosts=[host])
    with pytest.raises(Problem):
        oidc.allowed_url(SimpleNamespace(oidc_allowed_hosts=host), "https://" + host + "/metadata")


def test_provider_request_pins_dns_result_and_preserves_http_and_tls_hostname(provider, monkeypatch):
    lookups = []

    def rebinding(host, port, **kwargs):
        lookups.append(host)
        address = "93.184.216.34" if len(lookups) == 1 else "169.254.169.254"
        return [(oidc.socket.AF_INET, oidc.socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(oidc.socket, "getaddrinfo", rebinding)
    assert oidc._request_json(provider.settings, "get", provider.settings.oidc_discovery_url) == provider.config
    assert lookups == ["idp.test"]
    sent = provider.wire_requests[0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["host"] == "idp.test"
    assert sent.extensions["sni_hostname"] == "idp.test"
    assert sent.url.path == "/metadata"


def test_provider_request_falls_back_only_to_validated_addresses_with_the_same_host_and_sni(provider, monkeypatch):
    dns_addresses(monkeypatch, ["2606:4700:4700::1111", "93.184.216.34"])
    requests = []

    def receive(request):
        requests.append(request)
        if request.url.host == "2606:4700:4700::1111":
            raise httpx.ConnectError("Synthetic unavailable IPv6 route")
        return httpx.Response(200, json=provider.config)

    monkeypatch.setattr(oidc.httpx, "Client", lambda **kwargs: provider.client_factory(
        transport=httpx.MockTransport(receive), **kwargs))
    assert oidc._request_json(provider.settings, "get", "https://idp.test:8443/metadata") == provider.config
    assert [request.url.host for request in requests] == ["2606:4700:4700::1111", "93.184.216.34"]
    assert all(request.headers["host"] == "idp.test:8443"
               and request.extensions["sni_hostname"] == "idp.test" for request in requests)


@pytest.mark.parametrize("operation", ["discovery", "token", "keys", "userinfo", "test_connection"])
def test_all_provider_requests_and_connection_test_reject_private_dns_before_http(provider, monkeypatch, operation):
    dns_addresses(monkeypatch, ["169.254.169.254"])
    if operation == "test_connection":
        assert config.test_connection(provider.settings)["ok"] is False
    elif operation == "userinfo":
        assert oidc.userinfo(provider.settings, "https://idp.test/userinfo", "synthetic-token", "hospital-subject") == {}
    else:
        if operation == "token":
            monkeypatch.setattr(oidc, "document", lambda settings: provider.config)
        with pytest.raises(Problem):
            if operation == "discovery":
                oidc.document(provider.settings)
            elif operation == "token":
                oidc.exchange(provider.settings, {"verifier": "synthetic-verifier"}, "synthetic-code")
            else:
                oidc.signing_keys(provider.settings, provider.config["jwks_uri"])
    assert provider.wire_requests == []


def test_provider_request_ignores_proxy_environment_and_does_not_follow_redirects(provider, monkeypatch):
    client_factory = provider.client_factory
    requests = []

    def receive(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest/meta-data"})

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return client_factory(transport=httpx.MockTransport(receive), **kwargs)

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setattr(oidc.httpx, "Client", client)
    with pytest.raises(httpx.HTTPStatusError):
        oidc.document(provider.settings)
    assert len(requests) == 1


def test_flow_creation_does_not_resolve_dns_inside_the_write_transaction(h, provider, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Provider DNS must be checked with discovery before the write transaction")

    monkeypatch.setattr(oidc.socket, "getaddrinfo", forbidden)
    with transaction(h.app.state.engine, write=True) as db:
        location, _ = oidc.start_flow(db, provider.settings, provider.config)
    assert location.startswith(provider.config["authorization_endpoint"] + "?")


@pytest.mark.parametrize("stored_issuer", [None, "", "https://previous-provider.test"])
def test_subject_link_without_the_exact_current_issuer_cannot_authenticate(h, provider, stored_issuer):
    bind_staff(h, provider)
    binding = provider.settings.institutional_bindings[0]
    if stored_issuer is None:
        del binding["issuer"]
    else:
        binding["issuer"] = stored_issuer
    with pytest.raises(Problem, match="add your institutional sign-in"), transaction(h.app.state.engine, write=True) as db:
        oidc.finish_flow(db, provider.settings, provider.claims)
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Session)) == 0


def test_existing_ui_new_links_are_scoped_and_provider_switch_cannot_retarget_them(h):
    admin = h.client("admin")
    saved = admin.put(PATH, json=configuration(h))
    assert saved.status_code == 200
    link = saved.json()["institutional_bindings"][0]
    assert link["issuer"] == "https://idp.test"
    assert admin.put(PATH, json=configuration(h, expected_revision=1)).status_code == 200
    switched = configuration(h, expected_revision=2, issuer="https://replacement.test", allowed_hosts=["replacement.test"])
    denied = admin.put(PATH, json=switched)
    assert denied.status_code == 422 and denied.json()["error"]["code"] == "AUTH_BINDING_ISSUER"
    with transaction(h.app.state.engine) as db:
        settings = config.effective(db, h.settings)
        assert settings.oidc_issuer == "https://idp.test" and settings.auth_configuration_revision == 2
    switched["institutional_bindings"][0]["issuer"] = "https://replacement.test"
    assert admin.put(PATH, json=switched).status_code == 200


def test_legacy_unscoped_database_link_requires_explicit_reconfirmation(h):
    admin = h.client("admin")
    legacy = AuthSettings(
        mode="both", institutional_enabled=True, issuer="https://idp.test", client_id="rota-test", allowed_hosts=["idp.test"],
        institutional_bindings=[{"identity_id": h.seed["identities"]["staff"], "subject": "hospital-staff-subject"}])
    data = legacy.model_dump(mode="json")
    del data["institutional_bindings"][0]["issuer"]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.AuthConfiguration(id=config.SINGLETON, data=data))
    with transaction(h.app.state.engine, write=True) as db:
        settings = config.effective(db, h.settings)
        with pytest.raises(Problem, match="add your institutional sign-in"):
            oidc.finish_flow(db, settings, {"sub": "hospital-staff-subject"})
    assert admin.put(PATH, json=configuration(h, expected_revision=1)).status_code == 422
    confirmed = configuration(h, expected_revision=1)
    confirmed["institutional_bindings"][0]["issuer"] = "https://idp.test"
    assert admin.put(PATH, json=confirmed).status_code == 200
    undone = admin.post(PATH + "/undo", json={"expected_revision": 2, "confirmed": True,
                                             "reason": "Undo synthetic link confirmation"})
    assert undone.status_code == 200 and undone.json()["institutional_bindings"][0]["issuer"] == ""
    with transaction(h.app.state.engine, write=True) as db:
        with pytest.raises(Problem, match="add your institutional sign-in"):
            oidc.finish_flow(db, config.effective(db, h.settings), {"sub": "hospital-staff-subject"})
