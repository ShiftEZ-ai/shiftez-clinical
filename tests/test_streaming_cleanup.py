"""Interrupted streaming must release generator-owned transactions before returning."""
import asyncio
from threading import Event

import anyio
import pytest
from sqlalchemy import create_engine, text
from starlette.requests import ClientDisconnect

from apps.api import models as m
from apps.api.db import WRITE_LOCK_KEY, transaction
from apps.api.streaming import ClosingStreamingResponse
from packages.contracts import schemas as s


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("outcome", ["complete", "send_failure", "cancelled"])
def test_stream_closes_iterator_on_every_exit(kind, outcome):
    closed = []

    def sync_rows():
        try:
            yield "first"
            yield "second"
        finally:
            closed.append(True)

    async def async_rows():
        try:
            yield "first"
            yield "second"
        finally:
            await anyio.lowlevel.checkpoint()
            closed.append(True)

    async def run():
        response = ClosingStreamingResponse(sync_rows() if kind == "sync" else async_rows())
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("more_body"):
                if outcome == "send_failure":
                    raise OSError("Synthetic disconnected download")
                if outcome == "cancelled":
                    scope.cancel()
                    await anyio.lowlevel.checkpoint()

        with anyio.CancelScope() as scope:
            if outcome == "send_failure":
                with pytest.raises(ClientDisconnect):
                    await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
            else:
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert closed == [True]

    anyio.run(run)


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_disconnect_listener_closes_iterator(kind):
    closed = []

    def sync_rows():
        try:
            yield "first"
        finally:
            closed.append(True)

    async def async_rows():
        try:
            yield "first"
        finally:
            await anyio.lowlevel.checkpoint()
            closed.append(True)

    async def run():
        sent = anyio.Event()
        response = ClosingStreamingResponse(sync_rows() if kind == "sync" else async_rows())
        async def receive():
            await sent.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body":
                sent.set()
                await anyio.sleep_forever()
        await response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send)
        assert closed == [True]

    anyio.run(run)


def test_cancelled_sync_stream_waits_for_running_iterator_before_closing():
    entered, release, closed = Event(), Event(), Event()

    def rows():
        try:
            entered.set()
            assert release.wait(10), "Test did not release the synthetic worker"
            yield "first"
        finally:
            closed.set()

    async def run():
        response = ClosingStreamingResponse(rows())
        async def receive():
            return {"type": "http.disconnect"}
        async def send(_message):
            pass
        task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        assert await anyio.to_thread.run_sync(entered.wait, 10)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()

    anyio.run(run)


def test_stream_accepts_async_iterators_without_a_close_method():
    class Empty:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def run():
        async def send(_message):
            pass
        async def receive():
            return {"type": "http.disconnect"}
        await ClosingStreamingResponse(Empty())(
            {"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    anyio.run(run)


@pytest.mark.postgres
def test_interrupted_audit_csv_releases_postgres_read_transaction(pg):
    client = pg.client("auditor")
    # One full chunk makes the audit generator suspend inside its transaction.
    with transaction(pg.app.state.engine, write=True) as session:
        session.add_all(m.AuditEvent(department_id=pg.seed["department_id"], actor_id=pg.seed["identities"]["coordinator"],
            action="synthetic.stream", reason="Synthetic interrupted audit download", detail={}, correlation_id="stream-cleanup")
            for _ in range(1001))
    route = next(route for route in pg.app.routes if getattr(route, "path", None) == "/api/audit.csv")
    from starlette.requests import Request
    # Build the actual route's streaming body; request authentication uses its session cookie.
    request = Request({"type": "http", "path": "/api/audit.csv", "headers": [(b"cookie", "; ".join(
        f"{key}={value}" for key, value in client.cookies.items()).encode())], "state": {}})
    response = route.endpoint(request, {"start": None, "end": None, "person_id": None, "action": [],
        "actor_id": None, "target_id": None, "self_approved": False, "q": "", "collection_filter": s.CollectionFilter()})

    async def run():
        async def receive():
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body" and message.get("more_body"):
                raise OSError("Synthetic disconnected audit download")
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    anyio.run(run)
    observer = create_engine(pg.settings.database_url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    try:
        with observer.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND state LIKE 'idle in transaction%'")) == 0
            assert connection.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND granted AND classid = 0 AND objid = :key AND database = "
                "(SELECT oid FROM pg_database WHERE datname = current_database())"), {"key": WRITE_LOCK_KEY}) == 0
        assert pg.app.state.engine.pool.checkedout() == 0
    finally:
        observer.dispose()
