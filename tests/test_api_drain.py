"""API process draining ends only live streams and preserves current pulse state."""

import asyncio
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import httpx
import pytest
import uvicorn

from apps.api import live
from tests.conftest import TEST_PASSWORD


# Delivery measured 1.118 s exit on Linux, 2 CPU / 2 GiB, uvicorn 0.54.0, 10 Nov 2026.
DRAIN_EXIT_BUDGET_SECONDS = 5


def _pulse(response):
    assert response.status_code == 200, response.text
    assert ": connected" in response.text.splitlines()
    return json.loads(next(line[6:] for line in response.text.splitlines() if line.startswith("data: ")))


def test_reconnected_stream_starts_with_current_full_pulse(h):
    client = h.client("coordinator")
    first = _pulse(client.get("/api/events?once=true"))
    h.workspace("Changed while the previous stream was closed")
    second = _pulse(client.get("/api/events?once=true", headers={"Last-Event-ID": "ignored"}))

    # The route has no event IDs or replay cursor: every connection reads the current full pulse.
    assert first["changes"] is None and second["changes"] is None
    assert second["activity"] > first["activity"]


def test_shutdown_signal_chaining_wakes_stream_wait_and_restores_handlers(h):
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    calls = []
    cookies = dict(h.client("coordinator").cookies)

    def prior(sig, frame):
        calls.append(sig)

    signal.signal(signal.SIGTERM, prior)
    signal.signal(signal.SIGINT, prior)

    async def scenario():
        async with h.app.router.lifespan_context(h.app):
            assert signal.getsignal(signal.SIGTERM) is not prior
            assert signal.getsignal(signal.SIGINT) is not prior
            pending = asyncio.create_task(live.wait("shutdown-test", live.version("shutdown-test"), 60))

            transport = httpx.ASGITransport(app=h.app)
            async with httpx.AsyncClient(transport=transport, base_url=h.settings.public_origin) as client:
                assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/api/events")).status_code == 401
            assert not live.is_draining()

            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            await asyncio.wait_for(pending, 1)
            assert live.is_draining() and not live._stop.is_set()
            assert calls == [signal.SIGTERM]
            async with httpx.AsyncClient(transport=transport, base_url=h.settings.public_origin,
                                         cookies=cookies) as client:
                draining_stream = await client.get("/api/events")
            assert draining_stream.status_code == 200
            assert "event: pulse\n" in draining_stream.text
            assert "event: expired\n" not in draining_stream.text
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            assert calls == [signal.SIGTERM, signal.SIGINT]

        assert not live.is_draining()
        assert signal.getsignal(signal.SIGTERM) is prior
        assert signal.getsignal(signal.SIGINT) is prior

    try:
        asyncio.run(scenario())
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@pytest.mark.parametrize("previous", [signal.SIG_DFL, None, signal.SIG_IGN])
def test_shutdown_signal_preserves_non_callable_previous_handlers(monkeypatch, previous):
    from apps.api import main

    callbacks = []
    set_calls = []
    raised = []
    loop = type("Loop", (), {"call_soon_threadsafe": lambda self, callback: callbacks.append(callback)})()
    monkeypatch.setattr(signal, "signal", lambda sig, handler: set_calls.append((sig, handler)))
    monkeypatch.setattr(signal, "raise_signal", lambda sig: raised.append(sig))

    main._chain_shutdown_signal(signal.SIGTERM, None, previous, loop)

    assert callbacks == [live.begin_draining]
    if previous == signal.SIG_IGN:
        assert set_calls == [] and raised == []
    else:
        assert set_calls == [(signal.SIGTERM, signal.SIG_DFL)] and raised == [signal.SIGTERM]


def test_shutdown_signal_chains_callable_previous_handler(monkeypatch):
    from apps.api import main

    callbacks = []
    calls = []
    set_calls = []
    loop = type("Loop", (), {"call_soon_threadsafe": lambda self, callback: callbacks.append(callback)})()
    previous = lambda sig, frame: calls.append((sig, frame))
    monkeypatch.setattr(signal, "signal", lambda sig, handler: set_calls.append((sig, handler)))
    monkeypatch.setattr(signal, "raise_signal", lambda sig: pytest.fail("callable previous must not re-raise"))

    main._chain_shutdown_signal(signal.SIGINT, "frame", previous, loop)

    assert callbacks == [live.begin_draining]
    assert calls == [(signal.SIGINT, "frame")]
    assert set_calls == []


def _wait_for_http(origin, process, timeout=20):
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=origin, timeout=1, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"server exited before readiness: {process.returncode}")
            try:
                if client.get("/health/live").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            select.select([], [], [], min(0.05, max(0, deadline - time.monotonic())))
    raise AssertionError("server did not become ready before the deadline")


def _run_real_uvicorn_sigterm_drain(h):
    """Run the shared stream/slow-request scenario in a throwaway copy for before/after proof.

    Keep this test file, then run the unmodified base ``ee5e5c464`` source with
    ``git checkout ee5e5c464 -- apps/api/live.py apps/api/main.py`` and
    ``ROTA_DRAIN_EXPECTED=before python -m pytest -q -s tests/test_api_drain.py::test_real_uvicorn_sigterm_drains_events_without_cancelling_slow_request``.
    Restore the fixed source with ``git checkout HEAD -- apps/api/live.py apps/api/main.py``
    and run ``ROTA_DRAIN_EXPECTED=after python -m pytest -q -s tests/test_api_drain.py -k real_uvicorn_sigterm``.
    The fixed source must not be used for the ``before`` run. Before the fix, the
    graceful timeout cuts the open stream mid-response, so the consumer must record an error.
    After the fix the stream closes cleanly; the slow request completes in both cases.
    Both tests print DRAIN_EXIT_SECONDS; only the wall benchmark asserts the exit budget.
    """
    assert uvicorn.__version__ == "0.54.0", (
        "drain test assumes uvicorn 0.54.0 capture_signals ordering; inspect the upgraded server before changing this pin"
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    origin = f"http://127.0.0.1:{port}"
    script = textwrap.dedent(
        """
        import asyncio
        from fastapi.responses import StreamingResponse
        import uvicorn
        from apps.api.main import app

        @app.get('/__drain_test_slow')
        async def drain_test_slow():
            async def body():
                yield b'started\\n'
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                yield b'done\\n'
            return StreamingResponse(body(), media_type='text/plain')

        uvicorn.run(app, host='127.0.0.1', port=int(__import__('os').environ['DRAIN_TEST_PORT']),
                    log_level='warning', access_log=False, timeout_graceful_shutdown=15)
        """
    )
    environment = os.environ.copy()
    environment.update({
        "ROTA_ENV": "test",
        "DATABASE_URL": h.settings.database_url,
        "ROTA_SQLITE_SYNCHRONOUS": h.settings.sqlite_synchronous,
        "PUBLIC_ORIGIN": origin,
        "DRAIN_TEST_PORT": str(port),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
    })
    process = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                               env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    stream_ready = threading.Event()
    stream_done = threading.Event()
    slow_started = threading.Event()
    results = {}

    try:
        _wait_for_http(origin, process)
        with httpx.Client(base_url=origin, timeout=5, trust_env=False) as client:
            login = client.post("/auth/login", headers={"Origin": origin},
                                json={"username": "coordinator", "password": TEST_PASSWORD})
            assert login.status_code == 200, login.text
            cookies = dict(client.cookies)

        def consume_events():
            try:
                with httpx.Client(base_url=origin, cookies=cookies, timeout=None, trust_env=False) as client:
                    with client.stream("GET", "/api/events") as response:
                        response.raise_for_status()
                        for line in response.iter_lines():
                            if line == "event: pulse":
                                stream_ready.set()
                results["stream"] = "closed"
            except Exception as exc:  # surfaced in the assertion below
                results["stream_error"] = repr(exc)
            finally:
                stream_done.set()

        def consume_slow():
            try:
                with httpx.Client(base_url=origin, timeout=None, trust_env=False) as client:
                    with client.stream("GET", "/__drain_test_slow") as response:
                        response.raise_for_status()
                        lines = response.iter_lines()
                        assert next(lines) == "started"
                        slow_started.set()
                        results["slow"] = list(lines)
            except Exception as exc:  # surfaced in the assertion below
                results["slow_error"] = repr(exc)

        event_thread = threading.Thread(target=consume_events)
        slow_thread = threading.Thread(target=consume_slow)
        event_thread.start()
        assert stream_ready.wait(10), results
        slow_thread.start()
        assert slow_started.wait(10), results

        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        elapsed = time.monotonic() - started
        event_thread.join(5)
        slow_thread.join(5)
        print(f"DRAIN_EXIT_SECONDS={elapsed:.3f}")

        assert stream_done.is_set(), results
        assert results.get("slow") == ["done"], results
        return elapsed, results
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM graceful shutdown is not meaningful on Windows; run in python:3.13 Linux")
def test_real_uvicorn_sigterm_drains_events_without_cancelling_slow_request(h):
    """Check stream termination and slow-request completion without a timing budget."""
    _, results = _run_real_uvicorn_sigterm_drain(h)
    if os.getenv("ROTA_DRAIN_EXPECTED", "after") == "before":
        assert results.get("stream_error"), results
    else:
        assert results.get("stream") == "closed", results


@pytest.mark.wall_benchmark
@pytest.mark.skipif(os.name == "nt", reason="SIGTERM graceful shutdown is not meaningful on Windows; run in python:3.13 Linux")
def test_real_uvicorn_sigterm_drain_exit_wall_benchmark(h):
    """Assert the fixed server's exit time stays within Delivery's regression budget."""
    elapsed, _ = _run_real_uvicorn_sigterm_drain(h)
    assert elapsed < DRAIN_EXIT_BUDGET_SECONDS, (
        f"uvicorn drain ordering assumption failed: stream held shutdown for {elapsed:.3f}s; "
        f"expected <{DRAIN_EXIT_BUDGET_SECONDS}s"
    )
