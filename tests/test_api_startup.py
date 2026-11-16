"""Application startup and shutdown run around context-managed test clients."""
import asyncio
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from apps.api import live
from apps.api.main import create_app


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
