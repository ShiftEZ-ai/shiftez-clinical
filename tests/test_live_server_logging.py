"""The real HTTP harness keeps API evidence without request bodies or credentials."""
import json
from types import SimpleNamespace

import httpx
import pytest

from .conftest import TEST_PASSWORD
from . import test_browser as browser_harness
from .test_browser import live_server  # noqa: F401


@pytest.fixture(autouse=True)
def log_dir(monkeypatch,tmp_path):
    folder=tmp_path/"api-logs"
    monkeypatch.setenv("PW_API_LOG_DIR",str(folder))
    return folder


def test_live_server_keeps_sanitized_api_logs(live_server,log_dir):
    with httpx.Client(base_url=live_server,headers={"origin":live_server},timeout=30) as client:
        response=client.post("/auth/login",json={"username":"coordinator","password":TEST_PASSWORD})
        assert response.status_code==200,response.text
        token=client.cookies["rota_csrf"]
        response=client.post("/api/commands",headers={"x-csrf-token":token,"idempotency-key":"log-capture-test"},
            json={"kind":"workspace.create","payload":{"name":"Private synthetic request body","starts_on":"2026-10-04","ends_on":"2026-10-11"}})
        assert response.status_code==200,response.text
        cookies=list(client.cookies.values())
    paths=list(log_dir.glob("*.log"))
    assert len(paths)==1
    content=paths[0].read_text(encoding="utf-8")
    lines=[json.loads(line) for line in content.splitlines()]
    assert any(line["message"]=="api_server_start" and line["pid"] for line in lines)
    stages=[line["stage"] for line in lines if line["message"]=="api_startup"]
    assert all(stage in stages for stage in ("before_create_app","app_constructed","server_start","ready"))
    assert stages.index("before_create_app")<stages.index("app_constructed")<stages.index("server_start")<stages.index("ready")
    assert any(line["message"]=="http_request" and line["route"]=="/api/commands" and line["status"]==200 for line in lines)
    events=[line for line in lines if line.get("correlation_id")==response.headers["x-correlation-id"]]
    assert events[0]["message"]=="http_request_started" and events[0]["path"]=="/api/commands"
    assert events[0]["threadpool_total"]>=events[0]["threadpool_borrowed"]>=0
    for name in ("command_dispatch_queued","command_handler_started","write_attempt_started","write_completed"):
        assert next(line for line in events if line["message"]==name)["command_kind"]=="workspace.create"
    assert all(secret not in content for secret in [TEST_PASSWORD,token,*cookies,"Private synthetic request body"])


def test_readiness_success_retains_http_timeout_and_poll(monkeypatch,tmp_path):
    ticks=iter([0,0,.05])
    pauses,calls=[],[]
    monkeypatch.setattr(browser_harness,"time",SimpleNamespace(monotonic=lambda:next(ticks),sleep=pauses.append))
    statuses=iter([503,200])
    def get(url,*,timeout):
        calls.append((url,timeout))
        return SimpleNamespace(status_code=next(statuses))
    monkeypatch.setattr(browser_harness.httpx,"get",get)
    path=tmp_path/"startup.log"
    browser_harness._wait_for_live_server("http://127.0.0.1:1234",lambda:True,path,
        process=SimpleNamespace(pid=123,exitcode=None))
    assert calls==[("http://127.0.0.1:1234/health/ready",2)]*2
    assert pauses==[.05]
    events=[json.loads(line) for line in path.read_text().splitlines()]
    assert [e["stage"] for e in events]==["readiness_wait","ready"]
    assert events[-1]["last_status"]==200 and events[-1]["last_error"] is None
    assert events[-1]["pid"]==123 and events[-1]["exitcode"] is None


@pytest.mark.parametrize("backend",["process","thread"])
def test_readiness_exit_precedes_deadline_and_http(monkeypatch,tmp_path,backend):
    ticks=iter([0])  # A dead server short-circuits the deadline read, just as before.
    monkeypatch.setattr(browser_harness,"time",SimpleNamespace(monotonic=lambda:next(ticks)))
    monkeypatch.setattr(browser_harness.httpx,"get",lambda *a,**kw:pytest.fail("HTTP after server exit"))
    path=tmp_path/"startup.log"
    process=SimpleNamespace(pid=123,exitcode=-9) if backend=="process" else None
    reason="child_exited" if process is not None else "thread_exited"
    with pytest.raises(RuntimeError,match=reason):
        browser_harness._wait_for_live_server("http://127.0.0.1:1234",lambda:False,path,process=process)
    events=[json.loads(line) for line in path.read_text().splitlines()]
    assert len(events)==2
    failure=events[-1]
    assert failure["stage"]=="failed" and failure["reason"]==reason and failure["backend"]==backend
    assert failure["pid"]==(123 if process is not None else browser_harness.os.getpid())
    assert failure["exitcode"]==(-9 if process is not None else None)
    assert failure["thread_alive"]==(False if process is None else None)


@pytest.mark.parametrize("backend",["process","thread"])
@pytest.mark.parametrize("last_result",["status","exception"])
def test_readiness_deadline_and_private_last_outcome(monkeypatch,tmp_path,backend,last_result):
    ticks=iter([10,40,40.05])  # At exactly 30 s HTTP is still attempted; strictly later fails.
    pauses,calls=[],[]
    monkeypatch.setattr(browser_harness,"time",SimpleNamespace(monotonic=lambda:next(ticks),sleep=pauses.append))
    secret="private-password-cookie-body-token"
    def get(url,*,timeout):
        calls.append((url,timeout))
        if last_result=="exception":
            raise httpx.ConnectTimeout(secret,request=httpx.Request("GET",f"http://user:{secret}@private.invalid/?token={secret}"))
        return SimpleNamespace(status_code=503)
    monkeypatch.setattr(browser_harness.httpx,"get",get)
    path=tmp_path/"startup.log"
    process=SimpleNamespace(pid=123,exitcode=None) if backend=="process" else None
    with pytest.raises(RuntimeError,match="readiness_timeout") as failure:
        browser_harness._wait_for_live_server("http://private.invalid",lambda:True,path,process=process)
    assert calls==[("http://private.invalid/health/ready",2)] and pauses==[.05]
    content=path.read_text()
    assert secret not in content and "private.invalid" not in content and secret not in str(failure.value)
    events=[json.loads(line) for line in content.splitlines()]
    assert len(events)==2  # No record per poll or unbounded repeated exception details.
    last=events[-1]
    assert last["reason"]=="readiness_timeout" and last["backend"]==backend
    assert last["last_status"]==(503 if last_result=="status" else None)
    assert last["last_error"]==("ConnectTimeout" if last_result=="exception" else None)
    assert last["exitcode"] is None
    assert last["thread_alive"]==(True if process is None else None)


@pytest.mark.parametrize("running",[True,False])
def test_readiness_diagnostic_write_failure_cannot_replace_outcome(monkeypatch,tmp_path,running):
    ticks=iter([0,0])
    monkeypatch.setattr(browser_harness,"time",SimpleNamespace(monotonic=lambda:next(ticks)))
    monkeypatch.setattr(browser_harness.httpx,"get",lambda *a,**kw:SimpleNamespace(status_code=200))
    # Opening the directory as a log fails; the original success/failure must still be observed.
    if running:
        browser_harness._wait_for_live_server("http://127.0.0.1:1234",lambda:True,tmp_path)
    else:
        with pytest.raises(RuntimeError,match="thread_exited"):
            browser_harness._wait_for_live_server("http://127.0.0.1:1234",lambda:False,tmp_path)
