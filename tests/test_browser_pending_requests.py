"""Pending browser evidence survives the context.close in a failing test's finally block."""
import json
from types import SimpleNamespace

import pytest

from .test_browser import Traced, pending_request_url

pytestmark = pytest.mark.browser


class Context:
    def __init__(self):
        self.listeners={}
        self.pages=[]
        self.requests=[]
        self.tracing=SimpleNamespace(start=lambda **kwargs:None,stop=lambda **kwargs:None)

    def on(self,event,callback):
        self.listeners.setdefault(event,[]).append(callback)

    def emit(self,event,request):
        for callback in self.listeners.get(event,[]):callback(request)

    def close(self):
        # Real Playwright cancels unfinished requests as the context closes.
        for request in list(self.requests):self.emit("requestfailed",request)
        self.emit("close",self)

    def start(self,method,url):
        request=type("Request",(),{"method":method,"url":url})()
        self.requests.append(request)
        self.emit("request",request)
        return request


class Node:
    def __init__(self):
        self.user_properties=[]
        self.sections=[]

    def add_report_section(self,*section):self.sections.append(section)


@pytest.fixture
def tracked(monkeypatch,tmp_path):
    monkeypatch.setenv("PW_API_LOG_DIR",str(tmp_path))
    context=Context()
    node=Node()
    browser=Traced(SimpleNamespace(new_context=lambda **kwargs:context),None,"synthetic-test",node)
    return browser,context,node,tmp_path


@pytest.mark.parametrize("url,expected",[
    (('http://user:secret' + chr(64) + '127.0.0.1:8000/api/commands?name=Private#token'),"http://127.0.0.1:8000/api/commands"),
    ("http://localhost/auth/callback?code=secret&state=Private","http://localhost/auth/callback"),
    ("http://localhost/api/account-links/secret?name=Private","http://localhost/api/account-links/[redacted]"),
    ("http://localhost/api/commands/private-operation-key","http://localhost/api/commands/[redacted]"),
    ("http://localhost/invite/secret","http://localhost/invite/[redacted]"),
    ("http://localhost/reset/secret","http://localhost/reset/[redacted]"),
    ("http://localhost/calendar/secret.ics","http://localhost/calendar/[redacted]"),
])
def test_pending_request_urls_omit_secrets(url,expected):
    assert pending_request_url(url)==expected


def test_pending_requests_captured_before_failing_context_close(tracked,monkeypatch):
    browser,raw,node,folder=tracked
    clock=iter([10,11,12,15])
    monkeypatch.setattr("tests.test_browser.time",SimpleNamespace(monotonic=lambda:next(clock)))
    context=browser.new_context()
    done=context.start("GET","http://localhost/api/me")
    failed=context.start("GET","http://localhost/api/pulse")
    context.start("POST",('http://user:secret' + chr(64) + 'localhost/api/commands?name=Private#token'))
    context.emit("requestfinished",done)
    context.emit("requestfailed",failed)
    with pytest.raises(AssertionError,match="original failure"):
        try:raise AssertionError("original failure")
        finally:context.close()
    expected=[{"method":"POST","url":"http://localhost/api/commands","age_ms":3000}]
    assert json.loads(node.user_properties[0][1])==expected
    assert node.user_properties[0][0]=="pending_browser_requests"
    assert node.sections==[("call","pending browser requests",node.user_properties[0][1])]
    paths=list(folder.glob("*.pending-requests.json"))
    assert len(paths)==1
    assert json.loads(paths[0].read_text(encoding="utf-8"))==expected
    assert not browser.open


def test_successful_context_close_discards_pending_requests(tracked):
    browser,raw,node,folder=tracked
    context=browser.new_context()
    context.start("GET","http://localhost/api/events")
    context.close()
    assert not browser.open
    browser.finish(context,keep=True)
    assert not node.user_properties
    assert not node.sections
    assert not list(folder.iterdir())


@pytest.mark.parametrize("phase",["setup","call"])
@pytest.mark.parametrize("failed",[True,False])
def test_fixture_close_keeps_traces_only_for_failed_test_reports(tracked,phase,failed):
    browser,raw,node,folder=tracked
    stops=[]
    browser.folder=folder/"traces"
    raw.tracing.stop=lambda **kwargs:stops.append(kwargs)
    context=browser.new_context()
    context.start("POST","http://localhost/api/commands")
    setattr(node,"rep_"+phase,SimpleNamespace(failed=failed))
    # Yield-fixture teardown closes after pytest reported the failure, with no propagating exception.
    context.close()
    assert not browser.open
    assert len(stops)==1
    assert ("path" in stops[0]) is failed
    if failed:
        assert str(stops[0]["path"]).endswith(".zip")
        assert len(node.user_properties)==1
        assert json.loads(node.user_properties[0][1])[0]["method"]=="POST"
        assert len(list(folder.glob("*.pending-requests.json")))==1
    else:
        assert not node.user_properties
        assert not list(folder.iterdir())


def test_test_failure_captures_context_left_open_without_trace_folder(tracked):
    browser,raw,node,folder=tracked
    context=browser.new_context()
    context.start("POST","http://localhost/api/commands")
    browser.finish(context,keep=True)
    context.close()
    assert len(node.user_properties)==1
    assert len(list(folder.glob("*.pending-requests.json")))==1
    assert not browser.open


def test_artifact_error_preserves_failure_and_trace_capture(tracked,monkeypatch,tmp_path,capsys):
    browser,raw,node,folder=tracked
    stops=[]
    browser.folder=tmp_path/"traces"
    raw.tracing.stop=lambda **kwargs:stops.append(kwargs)
    context=browser.new_context()
    context.start("POST","http://localhost/api/commands")
    def denied(*args,**kwargs):raise OSError("synthetic artifact error")
    monkeypatch.setattr("tests.test_browser.Path.write_text",denied)
    with pytest.raises(AssertionError,match="original failure"):
        try:raise AssertionError("original failure")
        finally:context.close()
    assert "Pending requests not saved" in capsys.readouterr().out
    assert len(stops)==1 and str(stops[0]["path"]).endswith(".zip")
    assert not browser.open
