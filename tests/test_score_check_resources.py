"""Real resource boundaries and local-only hostile HTTP timing, with owned child cleanup."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import zlib

import pytest

from apps.api.services import score_check_isolation as isolation, score_check_provider as provider
from apps.api.settings import Settings
from .test_score_check_provider import Reply, _pdf


def configured(endpoint, timeout=3):
    settings = Settings(fmuc_data_key="17" * 32)
    profile = SimpleNamespace(id="resource-profile", department_id="resource-department", enabled=True,
        kind="openai_compatible", api_style="chat", base_url=endpoint, model="synthetic-model",
        reasoning="none", max_tokens=256, timeout_s=timeout, allow_private_endpoint=True)
    profile.key_ciphertext = provider.encrypt_key(profile, "synthetic-private-key", settings)
    source = provider.PreparedFile("synthetic-file", "a" * 64, "Synthetic private source", frozenset())
    return profile, settings, source


def capture_children(monkeypatch, script=None):
    real_popen, children = subprocess.Popen, []
    def launch(arguments, **kwargs):
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert "FMUC_DATA_KEY" not in kwargs["env"] and "DATABASE_URL" not in kwargs["env"]
        assert "synthetic-private-key" not in repr(arguments)
        if os.name == "nt":
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
            assert kwargs["startupinfo"].wShowWindow == 0
        if script is not None:
            code = script(arguments)
            arguments = arguments[:4] + ["-c", code]
        child = real_popen(arguments, **kwargs)
        child.kill = Mock(wraps=child.kill)
        children.append(child)
        return child
    monkeypatch.setattr(isolation.subprocess, "Popen", launch)
    return children


def assert_reaped(children):
    assert children and all(child.poll() is not None for child in children)
    assert all(child.stdin.closed and child.stdout.closed for child in children)
    names = {f"score-check-io-{child.pid}" for child in children}
    assert not any(thread.is_alive() and thread.name in names for thread in threading.enumerate())


def test_provider_child_import_avoids_compiling_unrelated_application_schemas():
    # Cold schema compilation consumes the same deadline as the real provider request.
    code = (f"import sys;sys.path.insert(0,{str(isolation.ROOT)!r})\n"
            "from apps.api.services import score_check_provider\n"
            "assert 'packages.contracts.schemas' not in sys.modules\n")
    subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', code], check=True,
                   capture_output=True, timeout=8, **isolation._hidden_options())


@contextmanager
def hostile_server(mode):
    stop, received, finished = threading.Event(), threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            try:
                self.rfile.read(int(self.headers.get("content-length", 0)))
                requests.append(self.path)
                received.set()
                if mode in {"valid", "repair"}:
                    result = {"ok": "invalid"} if mode == "repair" and len(requests) == 1 else {"ok": True}
                    data = json.dumps({"choices": [{"finish_reason": "stop", "message": {
                        "content": json.dumps(result)}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if mode == "headers":
                    stop.wait(10)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "1000000")
                self.end_headers()
                # Releasing stop cleans up the deliberately slow sender; these bytes reset inactivity timers.
                while not stop.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    stop.wait(0.15)
            except (OSError, ValueError):
                pass
            finally:
                finished.set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", received, requests
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        listener.join(timeout=2)
        if received.is_set():
            assert finished.wait(2)
        assert not listener.is_alive()


@pytest.mark.parametrize("mode", ["headers", "trickle"])
def test_real_attempt_deadline_interrupts_headers_and_trickle_without_retry(monkeypatch, mode, caplog):
    children = capture_children(monkeypatch)
    with hostile_server(mode) as (endpoint, received, requests):
        profile, settings, source = configured(endpoint, timeout=8)
        events = []
        with pytest.raises(provider.ProviderError) as error:
            provider.extract(profile, "Read synthetic source", [source], Reply, settings=settings, audit=events.append)
        assert received.is_set() and requests == ["/v1/chat/completions"]
    assert error.value.code == "SCORE_PROVIDER_UNAVAILABLE"
    assert [event["result"] for event in events] == ["started", "error"]
    assert "synthetic-private-key" not in str(error.value) + json.dumps(events) + caplog.text
    assert_reaped(children)
    assert all(child.returncode != 0 for child in children)


@pytest.mark.parametrize("mode,attempts", [("valid", 1), ("repair", 2)])
def test_real_isolated_reply_and_single_schema_repair_preserve_audits(monkeypatch, mode, attempts):
    children = capture_children(monkeypatch)
    with hostile_server(mode) as (endpoint, received, requests):
        profile, settings, source = configured(endpoint, timeout=8)
        events = []
        result, usage = provider.extract(profile, "Read synthetic source", [source], Reply,
                                         settings=settings, audit=events.append)
        assert result == {"ok": True} and usage == {
            "input_tokens": 3 * attempts, "output_tokens": 2 * attempts, "attempts": attempts}
        assert received.is_set() and len(requests) == attempts == len(children)
        assert [event["result"] for event in events] == (["started", "valid"] if attempts == 1 else
                                                       ["started", "invalid", "started", "valid"])
    assert_reaped(children)


@pytest.mark.parametrize("stage", ["dns", "bootstrap"])
def test_deadline_kills_stalled_dns_or_bootstrap_and_closes_all_ipc(monkeypatch, tmp_path, stage):
    marker = tmp_path / 'dns-entered'
    def stalled(arguments):
        setup = "import runpy, socket, sys, threading\nfrom pathlib import Path\n"
        setup += (f"def blocked(*args,**kwargs):\n Path({str(marker)!r}).write_text('entered')\n threading.Event().wait(60)\n"
                  "socket.getaddrinfo=blocked\n" if stage == "dns" else "threading.Event().wait(60)\n")
        return setup + f"sys.argv={arguments[4:]!r}\nrunpy.run_path({arguments[4]!r},run_name='__main__')"
    children = capture_children(monkeypatch, stalled)
    profile, settings, source = configured("http://127.0.0.1:1/v1", timeout=8 if stage == "dns" else 1)
    events = []
    with pytest.raises(provider.ProviderError) as error:
        provider.extract(profile, "Read synthetic source", [source], Reply, settings=settings, audit=events.append)
    assert error.value.code == "SCORE_PROVIDER_UNAVAILABLE"
    assert [event["result"] for event in events] == ["started", "error"]
    assert len(children) == 1
    if stage == "dns":
        assert marker.read_text() == 'entered'
    assert_reaped(children)
    assert children[0].returncode != 0
    if stage == "bootstrap":
        children[0].kill.assert_called_once()


def test_ipc_refuses_oversized_output_prefix_without_buffering_or_waiting(monkeypatch):
    def oversized(arguments):
        return ("import struct,sys,threading\n"
                f"sys.stdout.buffer.write(struct.pack('!I',{isolation.OUTPUT_BYTES + 1}));sys.stdout.buffer.flush()\n"
                "threading.Event().wait(60)")
    children = capture_children(monkeypatch, oversized)
    with pytest.raises(isolation.IsolationError, match='^resource$'):
        isolation.run("media", {}, timeout=3)
    assert_reaped(children)
    children[0].kill.assert_called_once()


def test_ipc_input_cap_prevents_launching_any_child(monkeypatch):
    children = capture_children(monkeypatch)
    monkeypatch.setattr(isolation, "INPUT_BYTES", 3)
    with pytest.raises(isolation.IsolationError, match="limit"):
        isolation.run("media", {"too": "large"}, timeout=1)
    assert children == []


def test_media_timeout_interrupts_blocked_input_and_reaps_child(monkeypatch):
    children = capture_children(monkeypatch, lambda arguments: "import threading;threading.Event().wait(60)")
    monkeypatch.setattr(isolation, "MEDIA_SECONDS", 1)
    with pytest.raises(provider.ProviderError) as error:
        provider.prepare_file("synthetic", "synthetic.pdf", b"%PDF-" + b" " * (1024 * 1024))
    assert error.value.code == "SCORE_INPUT_LIMIT"
    assert_reaped(children)
    children[0].kill.assert_called_once()


def test_os_memory_limit_rejects_allocation_before_native_parsers_are_imported():
    # One denied allocation proves enforcement without consuming the requested memory on the host.
    code = (f"import sys;sys.path.insert(0,{str(isolation.ROOT)!r})\n"
            "from apps.api.services.score_check_isolation import _memory_limit\n"
            "job=_memory_limit(64*1024*1024)\n"
            "try:\n value=bytearray(256*1024*1024)\nexcept MemoryError:\n print('bounded')\n")
    completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True,
        timeout=5, check=True, **isolation._hidden_options())
    assert completed.stdout.strip() == b"bounded" and completed.stderr == b""


def test_media_fails_closed_without_parsing_when_os_memory_limit_cannot_be_set(monkeypatch, tmp_path):
    marker = tmp_path / 'parser-entered'
    def unavailable(arguments):
        return ("import runpy,sys\nfrom pathlib import Path\n"
                f"sys.argv={arguments[4:]!r}\nchild=runpy.run_path({arguments[4]!r})\n"
                "def denied(*args):\n raise OSError('Synthetic limit setup refusal')\n"
                f"def forbidden(*args):\n Path({str(marker)!r}).write_text('entered')\n raise AssertionError\n"
                "main=child['_main']\nmain.__globals__['_memory_limit']=denied\n"
                "main.__globals__['_dispatch']=forbidden\nmain()\n")
    children = capture_children(monkeypatch, unavailable)
    with pytest.raises(provider.ProviderError) as error:
        provider.prepare_file('synthetic', 'synthetic.pdf', _pdf('Synthetic rota'))
    assert error.value.code == 'SCORE_INPUT_UNAVAILABLE'
    assert not marker.exists()
    assert_reaped(children)


def compressed_pdf():
    compressor, output = zlib.compressobj(), io.BytesIO()
    for _ in range(768):
        output.write(compressor.compress(b" " * (1024 * 1024)))
    output.write(compressor.compress(b"BT /F1 12 Tf 10 100 Td (Synthetic rota) Tj ET"))
    output.write(compressor.flush())
    stream = output.getvalue()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Filter /FlateDecode /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    content, offsets = b"%PDF-1.4\n", []
    for index, value in enumerate(objects, 1):
        offsets.append(len(content))
        content += str(index).encode() + b" 0 obj\n" + value + b"\nendobj\n"
    start = len(content)
    content += b"xref\n0 6\n0000000000 65535 f \n"
    content += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    return content + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF"


def test_small_pdf_with_large_decoded_stream_is_confined_to_os_memory_budget(monkeypatch):
    children = capture_children(monkeypatch)
    content = compressed_pdf()
    assert len(content) < 1024 * 1024
    # Native parsers may report refusal or abort at the OS limit before writing a reply.
    with pytest.raises(provider.ProviderError) as error:
        provider.prepare_file('synthetic', 'synthetic.pdf', content)
    assert error.value.code in {'SCORE_INPUT_LIMIT', 'SCORE_INPUT_INVALID'}
    assert_reaped(children)


@pytest.mark.parametrize("bound,value", [("MAX_PAGES", 0), ("MAX_IMAGE_PIXELS", 100)])
def test_pdf_page_and_pixel_preflight_precedes_pdfminer_expansion(monkeypatch, bound, value):
    import pdfplumber
    def forbidden(*args, **kwargs):
        pytest.fail("PDFMiner must not expand a document before page/pixel preflight")
    monkeypatch.setattr(pdfplumber, "open", forbidden)
    monkeypatch.setattr(provider, bound, value)
    with pytest.raises(provider.ProviderError) as error:
        provider._prepare_file("synthetic", "synthetic.pdf", _pdf("Synthetic rota"))
    assert error.value.code == "SCORE_INPUT_LIMIT"
