"""The deployed Uvicorn boundary passes only the edge's canonical client address to login limits."""
from .clock_support import freeze_clock
import secrets
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from apps.api import auth, models as m
from apps.api.db import transaction
from apps.api.rate_limit import counter_key

EDGE = "192.0.2.18"
USERNAME = "missing-person"


def fill_pair_delay(h, peer):
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.RateLimit(key_hash=auth.digest("login-delay:" + auth.login_keys(USERNAME, peer)[2]),
                           hits=0, expires_at=auth.now() + timedelta(seconds=60)))


def attempt(client, forwarded):
    return client.post("/auth/login", headers={"x-forwarded-for": forwarded},
                       json={"username": USERNAME, "password": secrets.token_urlsafe(32)})


def pin_clock(monkeypatch):
    moment = m.now()
    monkeypatch.setattr(auth, "now", lambda: moment)
    freeze_clock(monkeypatch, lambda: moment)


def pair_count(h, peer):
    key, _ = counter_key("login-pair", auth.login_keys(USERNAME, peer)[2],
                         h.settings.password_login_window_seconds, auth.now())
    with transaction(h.app.state.engine) as db:
        return db.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == key)) or 0


def test_untrusted_private_peer_cannot_rotate_forwarded_addresses_to_evade_login_limit(h, monkeypatch):
    pin_clock(monkeypatch)
    peer = "192.0.2.19"  # Another service on the edge's network is not the edge.
    fill_pair_delay(h, peer)
    with TestClient(ProxyHeadersMiddleware(h.app, trusted_hosts=EDGE), base_url=h.settings.public_origin,
                    client=(peer, 12345)) as client:
        client.headers["origin"] = h.settings.public_origin
        assert attempt(client, "198.51.100.41").status_code == 429
        assert attempt(client, "198.51.100.42").status_code == 429
    assert pair_count(h, "198.51.100.41") == pair_count(h, "198.51.100.42") == 0


def test_trusted_edge_keeps_clients_independent_and_ignores_forged_leftmost_chain_address(h, monkeypatch):
    pin_clock(monkeypatch)
    fill_pair_delay(h, "198.51.100.41")
    with TestClient(ProxyHeadersMiddleware(h.app, trusted_hosts=EDGE), base_url=h.settings.public_origin,
                    client=(EDGE, 12345)) as client:
        client.headers["origin"] = h.settings.public_origin
        assert attempt(client, "198.51.100.41").status_code == 429
        assert attempt(client, "198.51.100.42").status_code == 401
        assert attempt(client, "203.0.113.1, 198.51.100.41").status_code == 429
    assert pair_count(h, "198.51.100.42") == 1
