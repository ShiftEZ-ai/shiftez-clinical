"""Commands, exports and private calendar links have per-identity/per-link ceilings shared through the database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
import uuid
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, func, select, text
from apps.api import models as m
from apps.api import db
from apps.api.auth import digest
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.rate_limit import counter_key, enforce
from .clock_support import freeze_clock


def limit(h, **values):
    for name, value in values.items():
        object.__setattr__(h.settings, name, value)  # frozen dataclass; read per request


def post(h, role):
    return h.client(role).post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
                               json={"kind": "calendar_feed.revoke", "payload": {}})


def test_frozen_clock_keeps_imported_authentication_in_the_same_rate_window(monkeypatch):
    from apps.api.auth import now as authentication_now

    before = datetime(2026, 10, 3, 9, 30, 59, 600000, tzinfo=timezone.utc)
    clock = [before]
    monkeypatch.setattr(m, "clock_now", lambda: clock[0])
    assert freeze_clock(monkeypatch) == before
    clock[0] += timedelta(milliseconds=500)
    assert m.now() == authentication_now() == before
    assert counter_key("commands", "identity", 60, m.now()) == counter_key(
        "commands", "identity", 60, authentication_now())


def test_controlled_clock_advances_direct_and_imported_reads_together(monkeypatch):
    from apps.api.auth import now as authentication_now

    clock = [datetime(2026, 10, 3, 9, 30, 59, 600000, tzinfo=timezone.utc)]
    assert freeze_clock(monkeypatch, lambda: clock[0]) == clock[0]
    clock[0] += timedelta(milliseconds=500)
    assert m.now() == authentication_now() == clock[0]


@pytest.mark.parametrize("moment,ends", [
    (datetime(9999, 12, 30, 23, 59, 59, 999999, tzinfo=timezone.utc), datetime(9999, 12, 31, tzinfo=timezone.utc)),
    (datetime.max.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc)),
])
def test_counter_windows_support_maximum_session_dates_without_os_timestamp_limits(moment, ends):
    key, deadline = counter_key("synthetic", "subject", 900, moment)
    assert len(key) == 64 and deadline == ends


def test_commands_are_limited_per_identity_with_retry_after_and_a_fresh_window(h, monkeypatch):
    limit(h, rate_commands_per_minute=3)
    freeze_clock(monkeypatch)
    assert [post(h, "staff").status_code for _ in range(3)] == [200] * 3
    blocked = post(h, "staff")
    assert blocked.status_code == 429 and 1 <= int(blocked.headers["retry-after"]) <= 60
    assert blocked.json()["error"]["code"] == "RATE_LIMITED" and "wait a minute" in blocked.json()["error"]["message"]
    assert "correlation_id" in blocked.json()
    assert [post(h, "staff").status_code for _ in range(2)] == [429, 429]
    start = int(m.now().timestamp()) // 60 * 60
    key = digest(f"commands:{h.seed['identities']['staff']}:{start}")
    with h.app.state.engine.connect() as conn:
        assert conn.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == key)) == 6
    assert post(h, "coordinator").status_code != 429  # another identity is independent
    later = m.now() + timedelta(seconds=61)  # the pinned moment, a window later
    freeze_clock(monkeypatch, later)
    assert post(h, "staff").status_code == 200
    with h.app.state.engine.connect() as conn:  # the new window pruned the expired counters
        assert conn.scalar(select(func.count()).select_from(m.RateLimit).where(m.RateLimit.expires_at < later)) == 0


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
def test_refused_attempts_are_counted_without_touching_later_subjects(backend, request, monkeypatch):
    h = request.getfixturevalue("pg" if backend == "postgresql" else "h")
    moment = freeze_clock(monkeypatch)
    engine = h.app.state.engine
    enforce(engine, "test-counts", ["busy"], 1, 60, "Wait a minute")
    for _ in range(3):
        with pytest.raises(Problem) as refused:
            enforce(engine, "test-counts", ["busy", "other"], 1, 60, "Wait a minute")
        assert refused.value.status == 429 and refused.value.code == "RATE_LIMITED"
    start = int(moment.timestamp()) // 60 * 60
    with engine.connect() as conn:
        for subject, hits in (("busy", 4), ("other", None)):
            key = digest(f"test-counts:{subject}:{start}")
            assert conn.scalar(select(m.RateLimit.hits).where(m.RateLimit.key_hash == key)) == hits


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("method", ["get", "post"])
def test_account_link_address_limit_stops_fresh_tokens_creating_counters(backend, request, monkeypatch, method):
    h = request.getfixturevalue("pg" if backend == "postgresql" else "h")
    freeze_clock(monkeypatch)
    engine = h.app.state.engine
    with transaction(engine, write=True) as db:
        key, ends = counter_key("account-link-address", "testclient", 900, m.now())
        db.add(m.RateLimit(key_hash=key, hits=990, expires_at=ends))
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        client.headers["origin"] = h.settings.public_origin

        def guess():
            path = f"/api/account-links/{uuid.uuid4().hex}"
            return client.get(path) if method == "get" else client.post(path,
                json={"password": "Unused synthetic password 123"})

        assert [guess().status_code for _ in range(10)] == [404] * 10
        with engine.connect() as conn:
            rows = conn.scalar(select(func.count()).select_from(m.RateLimit))
        assert rows == 11  # One address counter and ten distinct token counters.
        for _ in range(5):
            refused = guess()
            assert refused.status_code == 429 and refused.json()["error"]["code"] == "RATE_LIMITED"
            assert 1 <= int(refused.headers["retry-after"]) <= 900
        with engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(m.RateLimit)) == rows
            assert conn.scalar(select(func.sum(m.RateLimit.hits))) == 1015


@pytest.mark.postgres
def test_postgres_rate_limit_does_not_wait_for_the_command_advisory_lock(pg, monkeypatch):
    freeze_clock(monkeypatch)
    engine = pg.app.state.engine
    with ThreadPoolExecutor(max_workers=1) as executor:
        with engine.begin() as holder:
            holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": db.WRITE_LOCK_KEY})
            pending = executor.submit(enforce, engine, "test-independent", ["subject"], 1, 60, "Wait a minute")
            assert pending.result(timeout=2) is None
            with pytest.raises(Problem) as refused:
                pending = executor.submit(enforce, engine, "test-independent", ["subject"], 1, 60, "Wait a minute")
                pending.result(timeout=2)
            assert refused.value.status == 429 and refused.value.code == "RATE_LIMITED"
            assert holder.scalar(select(m.RateLimit.hits)) == 2


def test_queued_sqlite_rate_limit_uses_the_window_after_acquiring_the_write_lock(h, monkeypatch):
    moment = freeze_clock(monkeypatch)
    engine = h.app.state.engine
    enforce(engine, "test-window", ["subject"], 1, 60, "Wait a minute")
    waiting = Event()

    def queued(connection, cursor, statement, parameters, context, many):
        state = connection.info.get("rota_write", {})
        if state.get("context", {}).get("rate_limit_scope") == "test-window":
            waiting.set()

    event.listen(engine, "before_cursor_execute", queued)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction(engine, write=True):
                pending = executor.submit(enforce, engine, "test-window", ["subject"], 1, 60, "Wait a minute")
                assert waiting.wait(10)
                freeze_clock(monkeypatch, moment + timedelta(seconds=61))
            pending.result(timeout=10)
        with engine.connect() as conn:
            assert conn.scalars(select(m.RateLimit.hits)).all() == [1]
    finally:
        event.remove(engine, "before_cursor_execute", queued)


def test_exports_are_limited_per_identity(h, monkeypatch):
    publication = h.publish(h.shift(h.workspace())["workspace"])["publication_id"]
    limit(h, rate_exports_per_minute=2)
    freeze_clock(monkeypatch)
    path = f"/api/reports/{publication}?format=csv"
    client = h.client("coordinator")
    assert [client.get(path).status_code for _ in range(2)] == [200, 200]
    assert client.get(path).status_code == 429 and client.get(path.replace("csv", "json")).status_code == 429
    assert h.client("approver").get(path).status_code == 200


def test_calendar_link_is_limited_per_link_and_per_address(h, monkeypatch):
    freeze_clock(monkeypatch)
    limit(h, rate_feed_per_hour=2, rate_feed_address_per_hour=4)  # the address counts every attempt, even one its link refuses
    path = h.command("staff", "calendar_feed.create", {})["path"]
    app = TestClient(h.app, base_url=h.settings.public_origin)
    assert [app.get(path).status_code for _ in range(2)] == [200, 200]
    blocked = app.get(path)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) <= 3600
    assert app.get("/calendar/another-link.ics").status_code == 404  # link limit is per link; address has 1 hit left
    assert app.get("/calendar/yet-another-link.ics").status_code == 429  # address ceiling reached


def test_defaults_are_generous(h):
    s = h.settings
    assert (s.rate_commands_per_minute, s.rate_exports_per_minute, s.rate_feed_per_hour) == (120, 30, 60)
    assert s.rate_feed_address_per_hour >= 600
    for _ in range(40):  # a busy person's burst of edits and exports stays well inside the defaults
        assert post(h, "staff").status_code == 200
