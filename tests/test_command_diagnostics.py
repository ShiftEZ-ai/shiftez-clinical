"""Command stages remain visible before a response, without recording private input."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from threading import get_native_id
import uuid

import httpx
import pytest
from anyio.to_thread import current_default_thread_limiter
from sqlalchemy import text
from sqlalchemy.engine import Connection

from apps.api import db, main, live
from apps.api.commands import logged_command_kind
from apps.api.settings import Settings


@pytest.fixture
def write_probe(tmp_path):
    engine = db.make_engine("sqlite:///" + str(tmp_path / "phases.db"), settings=Settings(environment="test"))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE phase_probe (value text)"))
    yield engine
    engine.dispose()


def test_write_phases_separate_operation_commit_and_cleanup_without_private_values(write_probe, caplog, monkeypatch):
    original_commit = write_probe.dialect.do_commit
    commit_boundaries = []

    def commit(connection):
        phases = [r for r in caplog.records if r.msg.startswith("write_phase_")]
        assert (phases[-1].msg, phases[-1].phase) == ("write_phase_started", "commit")
        original_commit(connection)
        commit_boundaries.append(len(phases))

    monkeypatch.setattr(write_probe.dialect, "do_commit", commit)
    secret = "private synthetic name and reason"

    def operation(session):
        for _ in range(3):
            session.execute(text("INSERT INTO phase_probe VALUES (:value)"), {"value": secret})

    with caplog.at_level(logging.INFO, logger="rota.db"):
        db.retry_write(write_probe, operation, context={"correlation_id": "phase-test", "command_kind": "workspace.create"})
    phases = [r for r in caplog.records if r.msg.startswith("write_phase_")]
    assert [(r.msg, r.phase) for r in phases] == [
        (event, phase) for phase in ("operation", "flush", "commit", "cleanup")
        for event in ("write_phase_started", "write_phase_completed")]
    assert commit_boundaries == [5]
    assert all(r.pid == os.getpid() and r.native_thread_id == get_native_id() for r in phases)
    assert all(r.correlation_id == "phase-test" and r.command_kind == "workspace.create" for r in phases)
    assert all(r.phase_wait_ms >= 0 for r in phases if r.msg == "write_phase_completed")
    assert caplog.records[-1].msg == "write_completed"
    logged = json.dumps([vars(r) for r in caplog.records], default=str)
    assert all(value not in logged for value in (secret, "INSERT INTO", "phase_probe", str(write_probe.url)))
    with write_probe.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM phase_probe")) == 3
        assert "rota_write" not in connection.info


@pytest.mark.parametrize("phase", ["operation", "flush", "commit"])
def test_write_phase_failure_reports_the_stage_and_rolls_back(write_probe, caplog, monkeypatch, phase):
    secret = "private synthetic exception and value"

    def fail(*args, **kwargs):
        raise RuntimeError(secret)

    def operation(session):
        session.execute(text("INSERT INTO phase_probe VALUES (:value)"), {"value": secret})
        if phase == "operation":
            fail()
        elif phase == "flush":
            patch.setattr(session, "flush", fail)

    with monkeypatch.context() as patch, caplog.at_level(logging.INFO, logger="rota.db"):
        if phase == "commit":
            patch.setattr(write_probe.dialect, "do_commit", fail)
        with pytest.raises(RuntimeError, match=secret):
            db.retry_write(write_probe, operation)
    failed = next(r for r in caplog.records if r.msg == "write_phase_failed")
    transaction = next(r for r in caplog.records if r.msg == "write_transaction_failed")
    assert failed.phase == transaction.phase == phase
    assert failed.error_code == transaction.error_code == "RuntimeError"
    assert failed.phase_wait_ms >= 0 and transaction.phase_wait_ms >= 0
    assert not any(r.msg == "write_phase_completed" and r.phase == phase for r in caplog.records)
    assert [(r.msg, r.phase) for r in caplog.records if getattr(r, "phase", None) == "cleanup"] == [
        ("write_phase_started", "cleanup"), ("write_phase_completed", "cleanup")]
    assert not any(r.msg == "write_completed" for r in caplog.records)
    logged = json.dumps([vars(r) for r in caplog.records], default=str)
    assert secret not in logged and "INSERT INTO" not in logged
    with write_probe.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM phase_probe")) == 0
        assert "rota_write" not in connection.info
        assert connection.scalar(text("PRAGMA busy_timeout")) == Settings().command_lock_timeout_ms
    db.retry_write(write_probe, lambda session: session.execute(text("INSERT INTO phase_probe VALUES ('saved')")))


@pytest.mark.parametrize("resource", [Connection, db.Session], ids=["connection", "session"])
def test_cleanup_failure_is_distinct_from_a_successful_commit(write_probe, caplog, monkeypatch, resource):
    original_close = resource.close
    secret = "private synthetic cleanup failure"

    def close(connection):
        original_close(connection)
        raise RuntimeError(secret)

    with monkeypatch.context() as patch, caplog.at_level(logging.INFO, logger="rota.db"):
        patch.setattr(resource, "close", close)
        with pytest.raises(RuntimeError, match=secret):
            db.retry_write(write_probe, lambda session: session.execute(text("INSERT INTO phase_probe VALUES ('saved')")))
    failed = [r for r in caplog.records if r.msg == "write_phase_failed"]
    assert len(failed) == 1 and failed[0].phase == "cleanup" and failed[0].error_code == "RuntimeError"
    assert any(r.msg == "write_phase_completed" and r.phase == "commit" for r in caplog.records)
    assert not any(r.msg == "write_completed" for r in caplog.records)
    assert secret not in json.dumps([vars(r) for r in caplog.records], default=str)
    with write_probe.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM phase_probe")) == 1


def test_read_transactions_do_not_emit_write_phases(write_probe, caplog):
    with caplog.at_level(logging.INFO, logger="rota.db"):
        with db.transaction(write_probe) as session:
            assert session.scalar(text("SELECT count(*) FROM phase_probe")) == 0
        with pytest.raises(ValueError):
            with db.transaction(write_probe):
                raise ValueError("synthetic read failure")
    assert not any(r.msg.startswith("write_") for r in caplog.records)


@pytest.mark.parametrize("valid", [True, False])
def test_command_entry_and_write_events_include_safe_kind(h, caplog, valid):
    client = h.client("coordinator")
    payload = {"name": "Private synthetic rota name"}
    if valid:
        payload.update(starts_on="2026-10-04", ends_on="2026-10-11")
    with caplog.at_level(logging.INFO):
        response = client.post("/api/commands?private=hidden-query", headers={"idempotency-key": str(uuid.uuid4())},
                               json={"kind": "workspace.create", "payload": payload})
    assert response.status_code == (200 if valid else 422)
    records = [r for r in caplog.records if getattr(r, "correlation_id", None) == response.headers["x-correlation-id"]]
    messages = [r.message for r in records]
    if valid:
        assert messages[:4] == ["http_request_started", "command_dispatch_queued", "command_handler_started", "write_attempt_started"]
        assert "write_completed" in messages
        assert records[2].threadpool_wait_ms >= 0
        assert all(r.command_kind == "workspace.create" for r in records[1:-1])
    else:
        # Preparation validates the workspace payload before the serialized writer starts.
        assert messages == ["http_request_started", "command_dispatch_queued", "command_handler_started", "http_request"]
        assert not any(message.startswith("write_") for message in messages)
        assert all(r.command_kind == "workspace.create" for r in records[1:-1])
    assert messages[-1] == "http_request"
    assert records[0].path == "/api/commands" and records[0].method == "POST"
    assert 0 <= records[0].threadpool_borrowed <= records[0].threadpool_total
    text = json.dumps([vars(r) for r in records], default=str)
    assert all(secret not in text for secret in ["Private synthetic rota name", "hidden-query", *client.cookies.values()])


@pytest.mark.parametrize("kind", ["Private arbitrary command kind", "workspace.CREATE", "workspace.create\n"])
def test_free_text_or_unexpected_command_characters_are_redacted(kind):
    assert logged_command_kind(kind) == "unknown"


def test_logged_command_kind_accepts_short_dotted_identifiers_without_a_registry():
    for kind in ("person.workload.propose", "future.command", "a.b.c.d", "a." + "a" * 62):
        assert logged_command_kind(kind) == kind
    for kind in ("workspace", "a.b.c.d.e", "a." + "a" * 63, "a._b", "a.b2", "a.b-c", "a.b c"):
        assert logged_command_kind(kind) == "unknown"


@pytest.mark.parametrize("path,template", [
    ("/api/account-links/private-token", "/api/account-links/{token}"),
    ("/invite/private-token", "/invite/{token}"),
    ("/reset/private-token", "/reset/{token}"),
    ("/calendar/private-token.ics", "/calendar/{token}.ics"),
    ("/api/commands/private-token", "/api/commands/{operation_key}"),
    ("/not-a-route/private-token", "unmatched"),
])
def test_request_entry_redacts_path_tokens_and_queries(h, caplog, path, template):
    with caplog.at_level(logging.INFO):
        h.client("coordinator").get(path + "?secret=private-query")
    entries = [r for r in caplog.records if r.message == "http_request_started"]
    assert entries[-1].path == template
    assert "private-token" not in json.dumps(vars(entries[-1]), default=str)
    assert "private-query" not in json.dumps(vars(entries[-1]), default=str)


def test_command_dispatch_reports_wait_for_a_thread_token(h, caplog, monkeypatch):
    monkeypatch.setattr(main, "execute", lambda *args: {"saved": True})

    async def scenario():
        limiter = current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        queued = asyncio.Event()
        original = main.log.info

        def record(message, *args, **kwargs):
            original(message, *args, **kwargs)
            if message == "command_dispatch_queued":
                queued.set()

        monkeypatch.setattr(main.log, "info", record)
        pending = None
        await limiter.acquire()
        held = True
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=h.settings.public_origin,
                                         headers={"origin": h.settings.public_origin}) as client:
                pending = asyncio.create_task(client.post("/api/commands", json={"kind": "notification.ack_all"}))
                await asyncio.wait_for(queued.wait(), 5)
                assert not pending.done()
                assert not any(r.message == "command_handler_started" for r in caplog.records)
                limiter.release()
                held = False
                response = await asyncio.wait_for(pending, 5)
                assert response.status_code == 200 and response.json() == {"saved": True}
        finally:
            if held:
                limiter.release()
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            limiter.total_tokens = previous

    # A reused browser-test worker may already have Playwright's event loop on its test thread.
    with caplog.at_level(logging.INFO), ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, scenario()).result()
    entry = next(r for r in caplog.records if r.message == "http_request_started")
    assert entry.threadpool_borrowed == entry.threadpool_total == 1
    handler = next(r for r in caplog.records if r.message == "command_handler_started")
    assert handler.threadpool_wait_ms >= 0


def test_live_streams_release_thread_tokens_while_commands_run(h, caplog, monkeypatch):
    client = h.client("coordinator")
    monkeypatch.setattr(live, "_events", {})
    # This checks dispatch capacity; notification persistence has its own command tests.
    monkeypatch.setattr(main, "execute", lambda *args: {"saved": True})

    async def scenario():
        limiter = current_default_thread_limiter()
        previous = limiter.total_tokens
        limiter.total_tokens = 1
        tasks, stops = [], []
        try:
            # Four live streams with one token would starve a command if each held a worker.
            for _ in range(4):
                started, stopped = asyncio.Event(), asyncio.Event()
                stops.append(stopped)

                async def receive(stop=stopped, messages=iter([{"type": "http.request", "body": b""}])):
                    if message := next(messages, None):
                        return message
                    await stop.wait()
                    return {"type": "http.disconnect"}

                async def send(message, ready=started):
                    if message["type"] == "http.response.body" and b"event: pulse" in message.get("body", b""):
                        ready.set()

                scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
                         "method": "GET", "scheme": "http", "path": "/api/events", "raw_path": b"/api/events",
                         "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1234), "server": ("localhost", 80),
                         "headers": [(b"host", b"localhost"), (b"cookie", ("rota_session=" + client.cookies["rota_session"]).encode())]}
                tasks.append(asyncio.create_task(h.app(scope, receive, send)))
                await asyncio.wait_for(started.wait(), 5)
            assert all(not task.done() for task in tasks)
            assert limiter.borrowed_tokens == 0
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=h.settings.public_origin,
                                         cookies=client.cookies, headers={"origin": h.settings.public_origin,
                                         "x-csrf-token": client.cookies["rota_csrf"], "idempotency-key": str(uuid.uuid4())}) as command_client:
                response = await asyncio.wait_for(command_client.post("/api/commands", json={"kind": "notification.ack_all"}), 5)
                assert response.status_code == 200, response.text
                assert response.json() == {"saved": True}
            assert all(not task.done() for task in tasks)
        finally:
            for stopped in stops:
                stopped.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            limiter.total_tokens = previous

    with caplog.at_level(logging.INFO), ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, scenario()).result()
