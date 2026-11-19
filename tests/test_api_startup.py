"""Application startup and shutdown run around context-managed test clients."""
import asyncio
from dataclasses import replace
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from apps.api import live
from apps.api.main import create_app
from apps.api.services import read_projection_warming


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_first_home_uses_warm_session_selects(h):
    cookies = h.client('coordinator').cookies
    app = create_app(h.settings)
    statements = []

    @event.listens_for(app.state.engine, 'before_cursor_execute')
    def capture(_connection, _cursor, sql, _parameters, context, _many):
        statements.append((sql, context.cache_hit.name))

    try:
        with TestClient(app, base_url=h.settings.public_origin, cookies=cookies) as client:
            assert all(sql.split()[0] in {'SELECT', 'SET'} for sql, _ in statements)
            for _ in range(2):
                statements.clear()
                assert client.get('/api/home').status_code == 200
                session_lookups = [hit for sql, hit in statements
                                   if sql.startswith('SELECT') and 'WHERE sessions.token_hash =' in sql]
                assert session_lookups == ['CACHE_HIT', 'CACHE_HIT']
    finally:
        app.state.engine.dispose()


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_readiness_waits_for_warmup(h, monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        entered, release, started = asyncio.Event(), Event(), asyncio.Event()
        finish = asyncio.Event()
        warm_process = read_projection_warming.warm_process

        def held(engine):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(60)
            warm_process(engine)

        async def serve():
            async with h.app.router.lifespan_context(h.app):
                started.set()
                await finish.wait()

        monkeypatch.setattr(read_projection_warming, 'warm_process', held)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url=h.settings.public_origin) as client:
            server = asyncio.create_task(serve())
            try:
                await asyncio.wait_for(entered.wait(), 60)
                assert not started.is_set()
                assert (await client.get('/health/ready')).status_code == 503
                assert (await client.get('/health/live')).status_code == 200
                release.set()
                await asyncio.wait_for(started.wait(), 60)
                assert (await client.get('/health/ready')).status_code == 200
            finally:
                release.set()
                finish.set()
                await server

    asyncio.run(scenario())


@pytest.mark.parametrize('h', ['sqlite', 'postgresql'], indirect=True)
def test_failed_warmup_is_logged_and_refuses_requests(h, monkeypatch, caplog):
    def fail(engine):
        raise RuntimeError('Synthetic warm-up failure')

    monkeypatch.setattr(read_projection_warming, 'warm_process', fail)
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        for path in ('/health/ready', '/health/service', '/api/home'):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {'status': 'unavailable'}
        assert client.get('/health/live').status_code == 200
    assert [(entry.levelname, entry.error_code) for entry in caplog.records
            if entry.message == 'api_process_warmup_failed'] == [('ERROR', 'RuntimeError')]


def test_live_updates_start_before_requests_and_stop_on_exit(h, monkeypatch):
    events = []
    attach, listen, detach = live.attach, live.listen, live.detach

    def attached(loop):
        assert loop is asyncio.get_running_loop()
        events.append(("attach", loop))
        attach(loop)

    def listening(database_url):
        assert live._loop is events[0][1]
        events.append(("listen", database_url))
        listen(database_url)

    def detached(loop):
        assert loop is asyncio.get_running_loop()
        events.append(("detach", loop))
        detach(loop)

    monkeypatch.setattr(live, "attach", attached)
    monkeypatch.setattr(live, "listen", listening)
    monkeypatch.setattr(live, "detach", detached)

    assert events == []
    with TestClient(h.app, base_url=h.settings.public_origin) as client:
        loop = events[0][1]
        assert events == [("attach", loop), ("listen", h.settings.database_url)]
        assert live._loop is loop
        assert client.get("/health/live").status_code == 200
        assert events == [("attach", loop), ("listen", h.settings.database_url)]

    assert events == [("attach", loop), ("listen", h.settings.database_url), ("detach", loop)]
    assert live._loop is None


def test_live_updates_stop_when_lifespan_exits_with_an_exception(h):
    async def interrupted():
        async with h.app.router.lifespan_context(h.app):
            assert live._loop is asyncio.get_running_loop()
            raise RuntimeError("Interrupted application lifespan")

    with pytest.raises(RuntimeError, match="Interrupted application lifespan"):
        asyncio.run(interrupted())
    assert live._loop is None


def test_two_apps_start_with_empty_history_and_reject_retired_listener_callbacks(h):
    async def scenario():
        loop = asyncio.get_running_loop()
        first = create_app(replace(h.settings, live_history_topics=1), engine=h.app.state.engine)
        second = create_app(h.settings, engine=h.app.state.engine)
        # Startup also discards stale state left by an interrupted/previous application.
        live._bump('before-start', {'scope': 'rules', 'id': None})
        async with first.router.lifespan_context(first):
            assert live.sequence() == 0
            retired = live._stop
            live._bump('old', {'scope': 'rules', 'id': None})
            live._bump('evict-old', {'scope': 'jobs', 'id': None})
            assert live._evicted_version > 0
            waiter = asyncio.create_task(live.wait('pending', live.version('pending'), 60))
            ready = loop.create_future()
            loop.call_soon(ready.set_result, None)
            await ready
            assert live._events and live._waiters
            live.changed('queued-before-shutdown', scope='rules')
        assert retired.is_set()
        assert live.sequence() == live._evicted_version == 0
        assert not live._changes and not live._versions and not live._events and not live._waiters
        async with second.router.lifespan_context(second):
            assert live._stop is not retired and not live._stop.is_set() and retired.is_set()
            assert live._history_topics == h.settings.live_history_topics
            # A late PG thread and already queued callbacks both belong to the retired app, even on this same loop.
            live._schedule(loop, retired, live.receive, 'late-old-listener')
            live.changed('new-app', scope='workspace')
            await asyncio.wait_for(waiter, 1)
            ready = loop.create_future()
            loop.call_soon(ready.set_result, None)
            await ready
            assert live.sequence() == 1 and set(live._changes) == {'new-app'}
            assert live.changes_since('new-app', 0) == [{'scope': 'workspace', 'id': None}]
        assert live.sequence() == 0 and live._stop.is_set() and live._loop is None
    asyncio.run(scenario())
