import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import logging
import pytest
from apps.api.logging_setup import JsonFormatter


def capture():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("rota.api")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return stream, lambda: logger.removeHandler(handler)


def test_request_entry_and_response_with_correlation_and_no_secrets(h):
    client = h.client("coordinator")
    stream, stop = capture()
    try:
        response = client.get("/api/me", headers={"authorization": "Bearer hunter2-token"})
    finally:
        stop()
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["http_request_started", "http_request"]
    assert lines[0]["path"] == "/api/me" and lines[0]["method"] == "GET"
    assert lines[0]["correlation_id"] == response.headers["x-correlation-id"]
    line = lines[1]
    assert line["message"] == "http_request" and line["status"] == 200 and line["route"] == "/api/me"
    assert line["correlation_id"] == response.headers["x-correlation-id"]
    assert line["identity_id"] == h.seed["identities"]["coordinator"] and {"ts", "level", "logger", "duration_ms", "response_bytes", "db_time_ms"} <= line.keys()
    text = stream.getvalue()
    for secret in [*client.cookies.values(), client.headers["x-csrf-token"], "hunter2-token"]:
        assert secret not in text


def test_request_log_carries_refresh_correlation_without_body_or_cookie(h):
    stream, stop = capture()
    try:
        response = h.client("coordinator").get('/api/me', headers={'X-Refresh-Correlation-ID': 'refresh-42'})
    finally:
        stop()
    line = next(line for line in map(json.loads, stream.getvalue().splitlines()) if line.get('route') == '/api/me')
    assert response.status_code == 200 and line['refresh_correlation_id'] == 'refresh-42'
    assert line['response_bytes'] > 0 and line['db_time_ms'] >= 0


@pytest.mark.parametrize("message", ["kaboom", "No response returned."])
def test_unhandled_error_logs_the_reference_the_user_sees_with_a_stack(h, message):
    @h.app.get("/api/boom")
    def boom():
        raise RuntimeError(message)
    client = h.client("coordinator")
    stream, stop = capture()
    try:
        response = client.get("/api/boom")
    finally:
        stop()
    assert response.status_code == 500
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    reference = response.json()["correlation_id"]
    assert [x["message"] for x in lines] == ["http_request_started", "unhandled_error", "http_request"]
    assert all(x["correlation_id"] == reference for x in lines)
    assert "RuntimeError" in lines[1]["stack"] and all(x["level"] == "ERROR" for x in lines[1:])


def test_client_disconnect_while_receiving_body_is_info_not_server_error(h):
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/api/commands", "raw_path": b"/api/commands",
             "query_string": b"", "root_path": "", "client": ("127.0.0.1", 1234), "server": ("localhost", 80),
             "headers": [(b"host", b"localhost"), (b"origin", h.settings.public_origin.encode()),
                         (b"content-type", b"application/json")]}
    incoming = iter([{"type": "http.request", "body": b'{"kind":"workspace.check"', "more_body": True},
                     {"type": "http.disconnect"}])
    sent = []

    async def receive():
        return next(incoming)

    async def send(message):
        sent.append(message)

    stream, stop = capture()
    try:
        # A synchronous browser fixture may already own this worker thread's event loop.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(asyncio.run, h.app(scope, receive, send)).result()
    finally:
        stop()
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    assert [line["message"] for line in lines] == ["http_request_started", "client_aborted"]
    line = lines[1]
    assert line["message"] == "client_aborted" and line["level"] == "INFO" and line["status"] == 499
    assert line["method"] == "POST" and line["error_code"] == "CLIENT_ABORTED" and "stack" not in line
    assert sent[0]["status"] == 499
    assert dict(sent[0]["headers"])[b"x-correlation-id"].decode() == line["correlation_id"]
