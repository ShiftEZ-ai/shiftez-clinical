"""Real Chromium, real HTTP API and migrated database. No network mocks."""
from contextlib import contextmanager
import gc
import json
import os
import re
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit, urlunsplit
import httpx
import pytest
from playwright.sync_api import sync_playwright, expect, Error as PlaywrightError
from .conftest import TEST_PASSWORD
from .governance_support import identity
from packages.contracts.schemas import ROLE_NAMES
from packages.domain.time import now as clock_now

ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.browser
# A busy CI runner (2 CPUs shared by test workers, browsers and the API) has stalled API answers ~5 s, Playwright's
# default for expect: assertions wait up to 15 s, a loaded page up to 30 s. A state that does arrive passes as soon as
# it does; only a failing assertion waits longer.
ACTION_MS=15000  # one wait for an assertion and for an action (click, fill): a failing action no longer waits twice as long
expect.set_options(timeout=ACTION_MS)
PAGE_LOAD_MS=30000
FIX_PILL = '.app-footer .health-pill:not(.score-pill)'


def fix_pill(page, tone):
    """The draft/published health control in its expected state, separate from the score."""
    return page.locator(f'{FIX_PILL}.{tone}')


def _live_server_event(log_path,stage,*,backend,pid,exitcode=None,reason=None,
                       last_status=None,last_error=None,thread_alive=None):
    """A few fixed, credential-free startup facts, without inherited logging locks."""
    event={"message":"api_startup","stage":stage,"backend":backend,"pid":pid,"writer_pid":os.getpid(),"exitcode":exitcode,
           "reason":reason,"last_status":last_status,"last_error":last_error,"thread_alive":thread_alive}
    try:
        with log_path.open("a",encoding="utf-8") as stream:
            stream.write(json.dumps(event)+"\n")
    except Exception:
        pass  # Diagnostics must never replace the readiness failure or prevent cleanup.


def _wait_for_live_server(origin,alive,log_path,*,process=None):
    deadline=time.monotonic()+30
    last_status=last_error=None
    def event(stage,reason=None,running=None):
        _live_server_event(log_path,stage,backend="process" if process is not None else "thread",
            pid=process.pid if process is not None else os.getpid(),
            exitcode=process.exitcode if process is not None else None,reason=reason,
            last_status=last_status,last_error=last_error,thread_alive=running if process is None else None)
    event("readiness_wait")
    while True:
        running=alive()
        if not running or time.monotonic()>deadline:
            reason=("child_exited" if process is not None else "thread_exited") if not running else "readiness_timeout"
            event("failed",reason,running)
            raise RuntimeError(f"Real API failed to become ready ({reason}; see captured log)")
        try:
            last_status=httpx.get(origin+"/health/ready",timeout=2).status_code
            last_error=None
            if last_status==200:
                event("ready",running=running)
                return
        except httpx.HTTPError as exc:
            last_status=None
            last_error=type(exc).__name__[:80]
        time.sleep(.05)


def _observe_keep_alive_pool(original, keep_alive, logger):
    """Diagnostic only: scalar boundaries around the existing session-activity handoff."""
    serial = 0
    async def observed(function, *args, **kwargs):
        nonlocal serial
        if function is not keep_alive:
            return await original(function, *args, **kwargs)
        serial += 1
        span = serial
        def emit(stage, completed=None):
            try:
                logger.info("keep_alive_boundary", extra={"span": span, "stage": stage,
                    "monotonic_ns": time.monotonic_ns(), "completed": completed})
            except Exception:
                pass  # Observation must not replace the original outcome.
        def invoke():
            emit("worker_enter")
            completed = False
            try:
                result = function(*args, **kwargs)
                completed = True
                return result
            finally:
                emit("worker_exit", completed)
        emit("queued")
        completed = False
        try:
            result = await original(invoke)
            completed = True
            return result
        finally:
            emit("await_exit", completed)
    return observed


def _live_server_env(h,origin):
    return {"ROTA_ENV":"test","DATABASE_URL":h.settings.database_url,"PUBLIC_ORIGIN":origin,
            "ROTA_SQLITE_SYNCHRONOUS":h.settings.sqlite_synchronous,**getattr(h,"server_env",{})}


@pytest.fixture
def live_server(h,monkeypatch,tmp_path,request):
    """The real API over real HTTP (uvicorn) in its own process, as a deployment runs it, forked from this test worker
    (which has already imported everything) instead of started as a new interpreter: no seconds of start-up per test.
    Where fork does not exist (Windows) it is served from a thread of this worker instead.
    PW_API_DELAY_MS adds independent async API delays without blocking Playwright's dispatcher."""
    import multiprocessing
    import logging
    import threading
    import uvicorn
    from apps.api.main import create_app
    from apps.api.logging_setup import JsonFormatter
    from apps.api.settings import Settings
    # Keep each attempt outside pytest's retry basetemp: a recovered failure still needs its first server log.
    log_dir=Path(os.getenv("PW_API_LOG_DIR",str(tmp_path)))
    log_dir.mkdir(parents=True,exist_ok=True)
    log_path=log_dir/(re.sub(r"[^\w.-]+","_",request.node.nodeid)[-150:]+"-"+uuid.uuid4().hex[:8]+".log")
    handler=logging.FileHandler(log_path,encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    def log_origin(record):
        record.test_process_pid = os.getpid()
        record.test_native_thread_id = threading.get_native_id()
        return True
    handler.addFilter(log_origin)
    logging.getLogger().addHandler(handler)
    def close_log():
        logging.getLogger().removeHandler(handler)
        logging.getLogger("uvicorn").removeHandler(handler)
        handler.close()
    request.addfinalizer(close_log)
    sock=socket.socket();sock.bind(("127.0.0.1",0))
    origin=f"http://127.0.0.1:{sock.getsockname()[1]}"
    for key,value in _live_server_env(h,origin).items():
        monkeypatch.setenv(key,value)
    forked="fork" in multiprocessing.get_all_start_methods()
    backend="process" if forked else "thread"
    def server():
        _live_server_event(log_path,"before_create_app",backend=backend,pid=os.getpid())
        app=create_app(Settings.from_env())
        if forked:
            from apps.api import main as api_module
            diagnostic_log = logging.getLogger("rota.test_server")
            api_module.run_in_threadpool = _observe_keep_alive_pool(
                api_module.run_in_threadpool, api_module.keep_alive, diagnostic_log)
            diagnostic_log.info("browser_diagnostic_binding", extra={"database_backend": app.state.engine.dialect.name})
        _live_server_event(log_path,"app_constructed",backend=backend,pid=os.getpid())
        if delay_ms:=os.getenv("PW_API_DELAY_MS"):
            import asyncio
            import random
            delay=int(delay_ms)/1000
            @app.middleware("http")
            async def delay_api(request,call_next):
                if request.url.path.startswith("/api/"):
                    await asyncio.sleep(random.random()*delay)
                return await call_next(request)
        instance=uvicorn.Server(uvicorn.Config(app,log_level="warning",access_log=False,timeout_graceful_shutdown=1))
        _live_server_event(log_path,"server_start",backend=backend,pid=os.getpid())
        logging.getLogger("uvicorn").addHandler(handler)
        logging.getLogger("rota.test_server").info("api_server_start",extra={"pid":os.getpid()})
        return instance
    process=None
    if forked:
        process=multiprocessing.get_context("fork").Process(target=lambda:server().run(sockets=[sock]),daemon=True)
        process.start()
        alive=process.is_alive
        def stop():
            process.terminate();process.join(15)
            if process.is_alive():process.kill();process.join()
    else:
        instance=server()
        thread=threading.Thread(target=instance.run,kwargs={"sockets":[sock]},name="live-server",daemon=True)
        thread.start()
        alive=thread.is_alive
        def stop():
            instance.should_exit=True;thread.join(15)
            instance.config.app.state.engine.dispose()
    try:
        _wait_for_live_server(origin,alive,log_path,process=process)
        yield origin
    finally:
        stop()
        sock.close()

def _browser_boundary(stage, completed=None):
    try:
        import logging
        logging.getLogger("rota.test_browser").info("browser_lifecycle", extra={
            "stage": stage, "completed": completed, "monotonic_ns": time.monotonic_ns()})
    except Exception:
        pass  # Observation must not replace context cleanup or its original error.


@pytest.fixture
def browser(chromium,request):
    """One Chromium per worker session; every test still gets fresh contexts, and any it leaves open are closed.
    PW_TRACE_DIR and PW_SCREENSHOT_DIR hold trace archives and failure screenshots."""
    folder=os.getenv("PW_TRACE_DIR")
    def disconnected(_):
        _browser_boundary("browser_disconnected")
    raw=chromium.ensure()
    chromium.last_test=request.node.nodeid
    raw.on("disconnected", disconnected)
    request.addfinalizer(lambda: raw.remove_listener("disconnected", disconnected))
    # A route a test holds unanswered leaves Playwright's route coroutine suspended. Collected by the GC on another thread
    # with a running asyncio loop (e.g. a TestClient portal), its `finally` sends from there and the main thread reads the reply
    # before its callback exists: KeyError '<call id>' in Connection.dispatch kills the driver connection (PR #642, G62).
    # Cyclic GC therefore runs only here, on Playwright's own thread, after the test's contexts are closed.
    gc_was_enabled=gc.isenabled()
    gc.disable()
    request.addfinalizer(lambda:(gc.collect(),gc_was_enabled and gc.enable()))
    clocked=Clocked(raw)
    traced=Traced(clocked,Path(folder) if folder else None,re.sub(r"[^\w.-]+","_",request.node.nodeid)[-150:],request.node)
    traced.harness=request.getfixturevalue('h') if 'h' in request.fixturenames else None
    yield traced
    failed=any(getattr(request.node,"rep_"+when,None) and getattr(request.node,"rep_"+when).failed for when in ("setup","call"))
    for context in list(traced.open):traced.finish(context,keep=failed)
    if chromium.alive():  # a dead connection cannot close; the next test's relaunch stops it with its contexts
        for context in raw.contexts:context.close()


PHONE_CONTINUE_ALL="try{sessionStorage.getItem('fmuc.phone-continue')||sessionStorage.setItem('fmuc.phone-continue','[\"*\"]')}catch(e){}"


class Clocked:
    """The worker's Browser whose every page reads the test calendar (conftest TEST_NOW) as the server does: Date and
    "today" start from the pinned clock and time flows from there.
    The app's service worker is blocked: it installs about a second after the first load and then takes control of the
    page, and a request that goes through it is not seen by page.route (Playwright's documented limit), so a test that
    routes /api/... would race the worker's activation (test_directory_failure_shows_retry_and_recovers did)."""
    def __init__(self,browser):
        self._browser=browser

    def __getattr__(self,name):
        return getattr(self._browser,name)

    def new_context(self,phone_notice=False,**options):
        options.setdefault("service_workers","block")
        context=self._browser.new_context(**options)
        context.set_default_timeout(ACTION_MS);context.set_default_navigation_timeout(PAGE_LOAD_MS)
        context.add_init_script('window.importBundle = ' + IMPORT_BUNDLE)
        context.clock.install(time=clock_now())
        # Phone scope (DESIGN.md): every other test opens all pages on a phone as before ("Continue anyway" for all, '*'),
        # so the operator screens' phone layouts stay tested; the phone-scope tests pass phone_notice=True.
        if not phone_notice:context.add_init_script(PHONE_CONTINUE_ALL)
        # No worker serves jobs beside the test server: a heartbeat a test's own process_one wrote is not one, so the
        # app's rota checks stay synchronous. A test of the job-backed check routes /health/service itself (page routes win).
        context.route("**/health/service",lambda route:route.fulfill(status=503,json={"status":"unavailable"}))
        return context


def bundle_file(name):
    """The glob for this build's copy of a web bundle file: the server serves the bundle under /assets/<version>/
    (packages/web_assets.py), and the app loads only that copy, so route and wait on it, never on /assets/<name>."""
    return f"**/assets/*/{name}"


IMPORT_BUNDLE = """name => import(new URL(name, document.querySelector('script[type="module"]').src).href)"""


def pending_request_url(url):
    """Keep the request location without credentials, query values or bearer tokens in paths."""
    parts=urlsplit(url)
    path=re.sub(r"^/(invite|reset|calendar|api/account-links|api/commands)/[^/]+",r"/\1/[redacted]",parts.path)
    return urlunsplit((parts.scheme,parts.netloc.rsplit("@",1)[-1],path,"",""))


class Traced:
    """The worker's Browser with pending-request evidence and optional traces. Evidence is saved before context.close
    aborts requests, when an exception is propagating (a test's `finally: context.close()`) or a test's setup/call failed;
    otherwise discarded unwritten. Focused retry traces are also saved when the test passes."""
    def __init__(self,browser,folder,test,node):
        self._browser,self.test,self.node,self.open=browser,test,node,{}
        trace_mode = os.getenv("PW_TRACE_MODE")
        self.trace_folder = folder if trace_mode != "off" else None
        self.keep_traces = trace_mode == "on"
        screenshot_folder = os.getenv("PW_SCREENSHOT_DIR")
        self.screenshot_folder = Path(screenshot_folder) if screenshot_folder else None
        if self.screenshot_folder is None and folder is not None:
            self.screenshot_folder = folder.parent / ((folder.name or "trace") + "-screenshots")

    def __getattr__(self,name):
        return getattr(self._browser,name)

    def new_context(self,**options):
        context=self._browser.new_context(**options)
        pending={}
        def started(request):pending[request]=time.monotonic()
        context.on("request",started)
        for event in ("requestfinished","requestfailed"):
            context.on(event,lambda request:pending.pop(request,None))
        context.on("close",lambda _:(_browser_boundary("context_closed"),self.finish(context,keep=False)))
        if self.trace_folder:
            context.tracing.start(snapshots=True,sources=False)
        close=context.close
        def close_keeping_trace(*args,**kwargs):
            failed=any(getattr(self.node,"rep_"+when,None) and getattr(self.node,"rep_"+when).failed for when in ("setup","call"))
            self.finish(context,keep=sys.exc_info()[0] is not None or failed)
            _browser_boundary("context_close_requested")
            # A route callback still running when the context closes (a route_fetch whose request the close aborts)
            # raises "Request context disposed", which Playwright reports at this worker's NEXT call: the next test's
            # sign-in. Every test's routes are dropped first, their in-flight errors ignored (Playwright's remedy).
            for owner in (*getattr(context,"pages",()),context):
                try:getattr(owner,"unroute_all",lambda **_:None)(behavior="ignoreErrors")
                except PlaywrightError:pass  # the page or context is already gone
            completed = False
            try:
                result = close(*args,**kwargs)
                completed = True
                return result
            finally:
                _browser_boundary("context_close_returned", completed)
        context.close=close_keeping_trace
        self.open[context]=pending
        return context

    def finish(self,context,keep):
        if context not in self.open:return
        pending=self.open.pop(context)
        try:
            if keep:
                captured=time.monotonic()
                evidence=json.dumps([{"method":request.method,"url":pending_request_url(request.url),
                    "age_ms":round((captured-started)*1000,2)} for request,started in pending.items()],indent=2)
                self.node.user_properties.append(("pending_browser_requests",evidence))
                self.node.add_report_section("call","pending browser requests",evidence)
                if folder:=os.getenv("PW_API_LOG_DIR"):
                    Path(folder).mkdir(parents=True,exist_ok=True)
                    (Path(folder)/f"{self.test}-{uuid.uuid4().hex[:8]}.pending-requests.json").write_text(evidence,encoding="utf-8")
        except Exception as exc:  # diagnostics must not hide the test failure or prevent trace capture
            print(f"Pending requests not saved: {exc!r}")
        pending.clear()
        if not self.trace_folder and not (keep and self.screenshot_folder):
            return
        name = f"{self.test}-{uuid.uuid4().hex[:6]}"
        if self.trace_folder:
            try:
                # Save the trace first so a stuck screenshot cannot hold it up.
                if keep or self.keep_traces:
                    self.trace_folder.mkdir(parents=True, exist_ok=True)
                    context.tracing.stop(path=str(self.trace_folder / f"{name}.zip"))
                else:
                    context.tracing.stop()
            except Exception as exc:  # a crashed browser must not hide the test's own failure
                print(f"Trace not saved: {type(exc).__name__}")
        if not (keep and self.screenshot_folder):
            return
        try:
            self.screenshot_folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Screenshot directory not created: {type(exc).__name__}")
            return
        for n,page in enumerate(context.pages):
            try:page.screenshot(path=str(self.screenshot_folder/f"{name}-page{n+1}.png"),full_page=True,timeout=5000)
            except Exception as exc:print(f"Screenshot not saved: {type(exc).__name__}")


def new_context(browser,viewport=None,**options):
    """A fresh browser context in the department's locale and time zone."""
    return browser.new_context(viewport=viewport or {"width":1440,"height":1000}, locale="en-GB", timezone_id="Asia/Riyadh",**options)


class Held:
    """Requests held by URL pattern (`with holding(page) as held`): `held.hold(pattern)` keeps every matching request
    unanswered, `held[pattern]` lists them, `held.release(pattern)` lets them (and later ones) through."""
    def __init__(self,page):
        self.page,self.routes=page,{}

    def hold(self,pattern):
        routes=self.routes.setdefault(pattern,[])
        self.page.route(pattern,lambda route:routes.append(route))  # a lambda: Playwright tags the handler object

    def __getitem__(self,pattern):
        return self.routes.get(pattern,[])

    def release(self,pattern):
        # Removing the last interceptor also resumes held routes in Playwright 1.63/1.64.
        for route in self.routes.pop(pattern,[]):route.continue_()
        self.page.unroute(pattern)


@contextmanager
def holding(page):
    """Holds requests for a test (Held); whatever is still held is released on the way out, so a failed assertion
    never leaves a request (and the page waiting on it) hanging until the context closes."""
    held=Held(page)
    try:
        yield held
    finally:
        for pattern in list(held.routes):
            try:held.release(pattern)
            except PlaywrightError:pass  # the page or context is already gone


def route_fetch(route,**options):
    """route.fetch() for a test that holds or edits a real API answer, asked once more if the connection dropped
    ("socket hang up": the driver reused a keep-alive socket the in-thread server had just closed, so the request
    never ran). Never re-sent: a command, which might have run before the drop. Asked unconditionally: a read the page
    repeats with If-None-Match would come back 304, with no body to hold or edit."""
    options.setdefault('headers',{k:v for k,v in route.request.headers.items() if k!='if-none-match'})
    try:
        return route.fetch(**options)
    except PlaywrightError as exc:
        if not re.search(r"socket hang up|ECONNRESET",str(exc)) or "/api/commands" in route.request.url:raise
        return route.fetch(**options)


class _Loose(dict):
    """A faked Fix list's dicts: a field a test leaves out reads as None (the server always sends it)."""
    def __missing__(self,key):
        return None


def fix_list_reply(route,body):
    """A faked Fix list answered per view as the server does: lean through the server's own projection
    (rota_summary.lean_fix_list, so a field it drops is dropped here too), detail (the one cause asked for), else the
    full object."""
    from urllib.parse import parse_qs,urlsplit
    from apps.api.services.rota_summary import lean_fix_list
    q=parse_qs(urlsplit(route.request.url).query);view=(q.get('view') or ['full'])[0]
    if view=='detail':
        found=[c for c in body['causes'] if c['id']==(q.get('cause_id') or [''])[0]]
        if not found:
            return route.fulfill(status=404,json={'error':{'code':'NOT_FOUND','message':'Cause not found'}})
        return route.fulfill(json={**{k:body.get(k) for k in ('workspace_id','revision','fingerprint')},'cause':found[0]})
    if view=='lean':
        loose=lambda c:_Loose(c,action=_Loose(c.get('action') or {}),fixes=[_Loose(f) for f in c.get('fixes') or []])
        return route.fulfill(json=lean_fix_list(_Loose(body,summary=_Loose(body.get('summary') or {}),causes=[loose(c) for c in body['causes']])))
    return route.fulfill(json=body)


def open_app(context,origin):
    """The app's first page. PW_CPU_THROTTLE=4: it runs as on a 4x slower CPU (a busy CI runner) without loading this machine."""
    page=context.new_page()
    if os.getenv("PW_CPU_THROTTLE"):context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate",{"rate":float(os.environ["PW_CPU_THROTTLE"])})
    # App readiness is asserted by the caller; unrelated resources need not finish loading first.
    page.goto(origin,wait_until="domcontentloaded")
    return page


def no_opening_hours(h):
    """A department with no opening hours yet: the demo seed's "Demo departmental working days" calendar (#517, the
    working days for prorated targets) is unlinked and removed, as before setup sets opening hours."""
    from apps.api.db import transaction
    from apps.api import models as m
    with transaction(h.app.state.engine) as db:
        for department in db.query(m.Department):
            department.operating_calendar_id=None
        db.flush()
        db.query(m.OperatingCalendar).filter_by(name='Demo departmental working days').delete()


def sign_in(browser,origin,role,viewport=None,**options):
    """A signed-in page for `role`: signs in through the real login endpoint (the cookies the form would get) and opens
    the app. The form itself is exercised by sign_in_with_form and the account and session tests."""
    context=new_context(browser,viewport,**options)
    from tests.browser_seed import authenticated
    cookies=getattr(getattr(browser,'harness',None),'browser_auth',{}).get(role,{})
    if not authenticated(context,origin,cookies):
        reply=context.request.post(origin+"/auth/login",data={"username":role,"password":TEST_PASSWORD},headers={"origin":origin})
        assert reply.ok,reply.text()
    return _signed_in(context,open_app(context,origin))


def sign_in_with_form(browser,origin,role,viewport=None):
    """Sign in by typing into the sign-in form, as a person does."""
    context=new_context(browser,viewport)
    page=open_app(context,origin)
    fill_sign_in(page,role)
    return _signed_in(context,page)


def fill_sign_in(page,username):
    """The sign-in form, identifier first: username or email, Continue, then the password."""
    page.get_by_label("Username or email", exact=True).fill(username)
    page.get_by_role("button", name="Continue", exact=True).click()
    page.get_by_label("Password",exact=True).fill(TEST_PASSWORD)
    page.get_by_role("button",name="Sign in",exact=True).click()


# What the page says about its fonts and rendering when fonts_ready times out (the cause is not yet known: a font
# request, a failed face, layout work or a starved renderer). Also logged to the console, so the trace keeps it.
FONT_DIAGNOSTICS=r"""async () => {
  const frame = () => new Promise(r => { const late = setTimeout(() => r(false), 1000); requestAnimationFrame(() => { clearTimeout(late); r(true); }); });
  const fonts = performance.getEntriesByType('resource').filter(e => /\.(woff2?|ttf|otf)(\?|$)/.test(e.name));
  const preloaded = [...document.querySelectorAll('link[rel=preload][as=font]')].map(l => l.href);
  const state = {fonts_status: document.fonts.status,
    ready_settled: await Promise.race([document.fonts.ready.then(() => true), new Promise(r => setTimeout(() => r(false), 0))]),
    faces: [...document.fonts].map(f => ({family: f.family, style: f.style, weight: f.weight, status: f.status})),
    font_requests: fonts.map(e => ({url: e.name.replace(location.origin, ''), ms: Math.round(e.duration),
      status: e.responseStatus, bytes: e.transferSize})),
    font_requests_pending: preloaded.filter(u => !fonts.some(e => e.name === u)).map(u => u.replace(location.origin, '')),
    ready_state: document.readyState, visibility: document.visibilityState, hash: location.hash,
    loaded_view: document.querySelector('#main')?.getAttribute('data-loaded-view') ?? null,
    animation_frame: await frame()};
  console.warn('fonts_ready diagnostics ' + JSON.stringify(state));
  return JSON.stringify(state);
}"""


def fonts_ready(page):
    """The self-hosted Geist faces have loaded: text measured before then uses the runner's fallback font (font-display:
    swap), whose metrics differ by host. Layout checks call this before measuring."""
    try:
        page.wait_for_function("() => document.fonts.ready.then(() => { const faces = [...document.fonts];"
                               " return faces.some(f => f.family.replace(/\"/g, '') === 'Geist' && f.status === 'loaded')"
                               " && !faces.some(f => f.status === 'loading' || f.status === 'error'); })",timeout=15000)
    except PlaywrightError as exc:
        # Bounded: an unresponsive renderer must not hang the diagnostics too.
        try:state=page.wait_for_function(FONT_DIAGNOSTICS,timeout=5000).json_value()
        except PlaywrightError as again:state=f"unavailable, the page did not answer within 5 s ({type(again).__name__})"
        raise AssertionError(f"Fonts not ready within 15 s. Page state: {state}") from exc


def _signed_in(context,page):
    # The first catalogue load can take several seconds on a loaded PostgreSQL host.
    expect(page.get_by_role("button",name=re.compile(r"^Account menu, "))).to_be_visible(timeout=20000)
    # The landing page has finished rendering: a navigation right after sign-in is not overtaken by it.
    expect(page.locator("#main[data-loaded-view]")).to_have_count(1,timeout=PAGE_LOAD_MS)
    # Existing journeys fill whole forms; step-by-step mode has its own test.
    page.evaluate("localStorage.setItem('rota.prefs', JSON.stringify({forms:'all'}))")
    return context,page


# Old address -> the address it opens now (mirrors the string entries of ROUTE_ALIASES in apps/web/src/routes.ts; checked
# by tests/test_browser_page_unification.py). Tests may still navigate by an old name; it lands on its new page.
REDIRECTS={
    'overview':'home','today':'home',
    'availability':'requests','swaps':'requests?type=swap',
    'directory':'people',
    'fairness':'insights/fairness','reports':'insights/workload',
    'setup':'configuration/setup',
    'governance':'access/routes',
    'data':'system/imports','operations':'system/operations','audit':'system/audit',
}
# Old schedule pages are function aliases in the same map: they land on the one Schedule page.
SCHEDULE_ALIASES={"planner","timeline","calendar","published"}


def loaded_page(key):
    key=REDIRECTS.get(key,key);name=key.split("?")[0].split("/")[0]
    return "schedule" if name in SCHEDULE_ALIASES else name


def expect_loaded(page,key,timeout=PAGE_LOAD_MS):
    """The page of an address (#page/tab?query) has loaded; for a tab of a merged page, that tab."""
    name=loaded_page(key);path=REDIRECTS.get(key,key).split("?")[0];tab=path.partition("/")[2]
    expect(page.locator("#main")).to_have_attribute("data-loaded-view",name,timeout=timeout)
    if tab and name!="schedule":expect(page.locator("#main")).to_have_attribute("data-loaded-tab",tab.split("/")[0],timeout=timeout)


def open_rule(page,name):
    """Open a rule's sheet from its row on the Rules page (the row reads a sentence; the rule's name ends the line under it)."""
    page.locator(".rule-item").filter(has_text=name).locator(".rl-text").click()
    sheet=page.locator(".dock-panel.rule-sheet")
    expect(sheet).to_be_visible()
    return sheet


def whole_list(page):
    """Requests opens on Waiting for you for anyone who decides requests (QA-039); this opens the whole list instead."""
    page.locator('#main .dt-saved').click()
    page.get_by_role('menu', name='Saved views').get_by_role('menuitemradio', name=re.compile('^All requests')).click()
    expect(page.locator('#main .dt-saved')).to_contain_text('All requests')
    # Its rows are the whole list's once its read has answered (table_view.ts: a server-read filter marks the list busy).
    expect(page.locator('#main .requests .dt-body')).to_have_attribute('aria-busy', 'false')


def whole_list_if_waiting(page):
    """Requests as opened: a decider's opens on Waiting for you (QA-039, 998bbca16), which then holds only what still
    waits for them; this switches such a list to the whole list, and leaves any other reader's list as it is."""
    expect(page.locator('#main[data-loaded-view="requests"]')).to_have_count(1)
    saved = page.locator('#main .dt-saved')
    if saved.count() and 'Waiting for you' in saved.first.inner_text():
        whole_list(page)


# A draft session's person chip (planner_week.ts). The published chips around a draft (.sd-greychip) carry data-person
# too, so a person reads the same everywhere (highlight, leaving marks): a check of a draft chip uses this, and a check
# of a published one says .sd-greychip.
DRAFT_CHIP = '.pchip[data-person]:not(.sd-greychip)'


def draft_chip(person='', word=False):
    """CSS for the draft chip of `person` (any draft chip when unset); `word` matches one id in a space-separated list."""
    return f'.pchip[data-person{"~" if word else ""}="{person}"]:not(.sd-greychip)' if person else DRAFT_CHIP


def navigate(page,key,*,alert=False):
    loaded=loaded_page(key)
    # Moving within the Schedule keeps data-loaded-view="schedule": clear it so the wait below sees the new render.
    # The app renders on the hashchange event, a task after the address changes: a render already under way (a reload's
    # first render) can set data-loaded-view in between, so wait until the app has taken the change (its render then
    # owns the flag) or 2 s have passed.
    page.evaluate("""([key,loaded]) => new Promise(done => {const main=document.querySelector('#main');
        if(location.hash==='#'+key){done();return;}
        if(loaded==='schedule'&&main&&main.dataset.loadedView==='schedule')main.removeAttribute('data-loaded-view');
        const taken=()=>setTimeout(done);addEventListener('hashchange',taken,{once:true});setTimeout(done,2000);
        location.hash=key;})""",[key,loaded])
    expect_loaded(page,key)
    if not alert:
        expect(page.locator("#main [role=alert]")).to_have_count(0)


def restart_app(page):
    """Start the app anew once a test's routes exist, until its page has loaded. sign_in's landing page (Home) has
    already asked for the app's shared reads (the rota list for the sidebar counts, the inbox, the catalogues); a page
    opened next joins such a read while it is on its way (api.ts) instead of asking again, so it would never pass
    through routes added after it."""
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#main[data-loaded-view]")).to_have_count(1,timeout=PAGE_LOAD_MS)  # the same boot wait as sign_in


def open_rota(page,workspace_id):
    """Put a rota on the schedule (an old planner link: it becomes the editable draft and its days come into view;
    a published one shows as the published layer)."""
    navigate(page,"planner/"+workspace_id)


def new_draft(page):
    """New draft: the toolbar primary while no draft is open, otherwise from the draft switcher."""
    button=page.locator(".planner-toolbar").get_by_role("button",name="New rota",exact=True)
    if button.count():
        button.click()
    else:
        page.get_by_role("button",name=re.compile(r"^(Switch draft · |Choose a draft)")).click()
        page.get_by_role("dialog",name="Drafts",exact=True).get_by_role("button",name="New rota",exact=True).click()
    expect(page.get_by_role("dialog",name="New rota",exact=True)).to_be_visible()


def open_config(page, section):
    """Configuration › one section of its left navigation (settings pages carry no count)."""
    navigate(page, 'configuration')
    shown = page.locator('.config-pane').first.element_handle()
    page.get_by_role('navigation', name='Configuration sections').get_by_role('button', name=section, exact=True).click()
    # The section is drawn afresh even when it is the one shown: wait for the new pane, not the old one's heading.
    page.wait_for_function('pane => !pane.isConnected', arg=shown)
    expect(page.locator('.config-pane .section-head h2').first).to_have_text(section)


def department_tab(page, name):
    """Configuration › Department settings: open one of its tabs (General, Swaps, Announcements, Advanced)."""
    tab = page.locator('.config-pane').get_by_role('tablist', name='Department settings').get_by_role('tab', name=name, exact=True)
    tab.click()
    expect(tab).to_have_attribute('aria-selected', 'true')


# Each built-in Schedule view as View settings chooses it: a layout over a range (Year opens from its own link).
SCHEDULE_VIEWS={"Year": None, "Month": ("Calendar", "Month"), "Week": ("Calendar", "Week"), "Day": ("Calendar", "Day"),
                "People": ("People", "2 weeks"), "Coverage": ("Coverage", "Week"), "List": ("List", "Month")}


def keyboard_focus(locator):
    """Focus a control as a keyboard user reaches it. Help on focus follows keyboard input only (SW3-C-16: focus the app
    sets at load or hands back after a tapped popover shows no help), and a test's .focus() is indistinguishable from the
    app's, so a key press comes first."""
    locator.page.keyboard.press('Shift')
    locator.focus()


def view_settings(scope, row=None):
    """The toolbar's View settings popover, on its main page or (with `row`) on that setting's page."""
    panel = toolbar(scope, "View settings")
    back = panel.get_by_role("button", name=re.compile("^Back to "))
    while back.count():
        back.first.click()
    if row:
        panel.locator(".vs-row").filter(has=panel.page.locator(".vs-row-name", has_text=re.compile(rf"^{re.escape(row)}$"))).click()
        expect(panel.locator(".vs-back strong")).to_have_text(re.compile(rf"^{re.escape(row)}"))
    return panel


def choose_radio(panel, group, name):
    """A single choice in View settings: a row of radio buttons, or (Rows, Columns) the shared dropdown."""
    if group in ("Rows", "Columns"):
        box = panel.get_by_label(group, exact=True)
        box.select_option(label=name)
        expect(panel.get_by_label(group, exact=True).locator("option:checked")).to_have_text(name)
        return
    radio = panel.get_by_role("radiogroup", name=group, exact=True).get_by_role("radio", name=name, exact=True)
    if radio.get_attribute("aria-checked") != "true":
        radio.click()
    expect(radio).to_have_attribute("aria-checked", "true")


# The Calendar's renderers by their former layout names: (rows, what a person's cell shows).
GRID_LAYOUTS = {"Calendar": ("Duties", None), "Timeline": ("People", "Duty codes"), "People": ("People", "Duties and hours")}


def choose_layout(panel, name):
    """View settings › Layout: Calendar (with its rows and cells; Timeline and People are its people rows), Coverage or List."""
    if name not in GRID_LAYOUTS:
        choose_radio(panel, "Layout", name)
        return
    rows, cells = GRID_LAYOUTS[name]
    choose_radio(panel, "Layout", "Calendar")
    choose_radio(panel, "Rows", rows)
    if cells:
        choose_radio(panel, "Cells show", cells)


def pick_view(page, name):
    """Schedule views (Year … List) are a layout and range in View settings; other names are saved views."""
    bar = page.locator(".planner-toolbar")
    if name in SCHEDULE_VIEWS:
        if name == "Year":
            view_settings(bar).get_by_role("button", name="Open the year overview", exact=True).click()
        else:
            panel = view_settings(bar)
            layout, days = SCHEDULE_VIEWS[name]
            choose_layout(panel, layout)
            choose_radio(panel, "Range", days)
        close_popover(page)
        expect(page.locator("#schedule-view")).to_have_attribute("aria-label", f"{name} view")
    else:
        toolbar(bar, "Saved views").get_by_role("menuitemradio", name=name, exact=True).click()
        close_popover(page)
        expect(bar.locator("button.dt-saved")).to_have_attribute("aria-label", re.compile(rf"^Saved views: {re.escape(name)}"))


def session_row(page, text=""):
    """A session in the schedule's List view (one row per session, with its status)."""
    pick_view(page, "List")
    rows = page.locator("tr.dt-open")
    return (rows.filter(has_text=text) if text else rows).first


def open_session(page, text=""):
    """The session panel opens from the session's row in the List view (one session panel for every view, S-09)."""
    session_row(page, text).locator("td strong").first.click()
    expect(page.locator(".wk-panel .wk-session")).to_be_visible()


def session_action(page, name):
    """One of the session panel's ⋯ actions (Close session, Prepare automatic cover, Find staffing…)."""
    page.locator(".wk-panel").get_by_role("button", name="More session actions", exact=True).click()
    page.get_by_role("menuitem", name=name, exact=True).click()


def wait_run_created(page, seconds=30):
    """Until the auto-schedule run just pressed exists on the server. The panel shows the run's progress at once, before
    job.create has answered; its Stop (or Cancel) appears only once the job exists and the panel has read it."""
    expect(page.locator('#auto-panel [data-run-stop]').first).to_be_attached(timeout=seconds * 1000)


def wait_until(page, done, failure, seconds=30):
    """Until `done()` is true: a state set by a Playwright event callback (a route, a response). Each call into the page
    lets Playwright deliver pending events; no fixed wait, and never without an end (a state that cannot arrive fails
    here with `failure` instead of hanging the shard). `failure` may be a function, read when the wait gives up, so the
    message says the state as it is then."""
    deadline = time.monotonic() + seconds
    while not done() and time.monotonic() < deadline:
        try:
            page.evaluate("0")
        except PlaywrightError as exc:  # the state awaited may be a navigation (a reload): its page answers again after it
            if 'Execution context was destroyed' not in str(exc):
                raise
    assert done(), failure() if callable(failure) else failure


def wait_held(page, held, seconds=30, count=1):
    """Until a route callback has put `count` intercepted requests in `held`."""
    wait_until(page, lambda: len(held) >= count, "the request was never intercepted", seconds)


def reconnect(page):
    """The connection dropped and came back: the app asks for the pulse and, having been offline, refreshes the page
    in the background (app.ts onPulse). `online` alone reads nothing while the pulse has not moved (60315a5de)."""
    page.evaluate("dispatchEvent(new Event('offline')); dispatchEvent(new Event('online'))")


# A JavaScript function expression: `(COUNT_WORK)(() => draw())` runs a synchronous redraw and returns the work it did,
# counted rather than timed (wall-clock on a shared runner is noise): the elements it built and every read that forces a
# layout (each one lays out the whole page being drawn), with its time kept for the receipt.
COUNT_WORK = """run => {const count={created:0,layout:[]},undo=[];
  const wrap=(o,k,create)=>{const d=Object.getOwnPropertyDescriptor(o,k);if(!d)throw new Error('COUNT_WORK cannot count '+k);const f=d.get||d.value;
    const w=function(...a){if(create)count.created++;else count.layout.push(k);return f.apply(this,a);};
    Object.defineProperty(o,k,d.get?{...d,get:w}:{...d,value:w});undo.push(()=>Object.defineProperty(o,k,d));};
  wrap(Document.prototype,'createElement',true);
  for(const k of ['getBoundingClientRect','getClientRects','clientWidth','clientHeight','scrollWidth','scrollHeight','scrollTop','scrollLeft'])wrap(Element.prototype,k);
  for(const k of ['offsetWidth','offsetHeight','offsetTop','offsetLeft','innerText','focus'])wrap(HTMLElement.prototype,k);
  wrap(window,'getComputedStyle');
  const t=performance.now();
  try{run();}finally{for(const u of undo)u();}
  return {...count,ms:performance.now()-t};}"""


def press_escape(page):
    """Escape for the panel, preview or dialog, after a click. The pointer still rests on the clicked control, and once
    its hover help shows (280 ms later, sooner or later on a busy runner) the first Escape only dismisses that help
    (WCAG 1.4.13, dom.ts enhanceTooltips). So the pointer leaves the page first: off the viewport it rests on no other
    control whose help could open meanwhile (the phone menu button sits at 0,0), help still waiting out its delay is
    dropped as the pointer leaves, and help already shown closes within its 120 ms grace."""
    page.mouse.move(-1, -1)
    expect(page.get_by_role("tooltip")).to_have_count(0)
    page.keyboard.press("Escape")


def toolbar(scope, name):
    """Open a collection toolbar popover (Filter, Group, Sort, View, Saved views) and return it; an open one is reused."""
    page = getattr(scope, "page", scope)
    button = scope.locator("button.dt-tool").and_(scope.get_by_role("button", name=re.compile(rf"^{re.escape(name)}($|:| \()")))
    if button.get_attribute("aria-expanded") != "true":
        button.click()
    panel = page.get_by_role("dialog", name=name, exact=True)
    expect(panel).to_be_visible()
    return panel


def show_day(iso):
    """A day as the shared date field shows it: "Sun 4 Oct 2026"."""
    from datetime import date
    d=date.fromisoformat(iso[:10])
    return f"{d:%a} {d.day} {d:%b} {d.year}"


def expect_when(scope,label,value):
    """A date-and-time field (date_picker.dateTimeInput) shows `value` ("2026-10-04T09:00"): the day in the labelled
    date part, the 24-hour time in "<label> time"."""
    expect(scope.get_by_label(label,exact=True)).to_have_value(show_day(value))
    expect(scope.get_by_label(label+" time",exact=True)).to_have_value(value[11:16])


def close_popover(page):
    """Escape closes the open toolbar popover (and any picker inside it)."""
    for _ in range(3):
        if not page.locator(".popover").count():
            return
        page.keyboard.press("Escape")
    expect(page.locator(".popover")).to_have_count(0)


def set_layout(scope, name):
    """Choose a layout (Table/Cards/Board, or the planner's Calendar/People/Coverage/List) in the View settings popover."""
    panel = toolbar(scope, "View settings")
    radio = panel.get_by_role("radiogroup", name="Layout", exact=True).get_by_role("radio", name=name, exact=True)
    radio.click()
    expect(radio).to_have_attribute("aria-checked", "true")
    close_popover(getattr(scope, "page", scope))


def choose(scope, tool, menu, item):
    """Pick one entry of a toolbar menu, e.g. choose(page, "Group", "Group by", "Duty"). A list's Sort with keys already is
    a sort list (Sort › property, direction, + Add sort): its first key changes, and "No sorting" deletes the list."""
    panel = toolbar(scope, tool)
    rows = panel.locator(".dt-sort-row")
    if tool == "Sort" and rows.count():
        if item == "No sorting":
            wipe = panel.get_by_role("button", name="Delete sort", exact=True)
            wipe.click()
            panel.locator(".cond-delete.armed").click()  # the same button confirms
        else:
            rows.first.get_by_label("Sort by", exact=True).select_option(label=item)
    elif tool == "Group" and panel.locator(".dt-group-list .dt-sort-row").count():
        # Active grouping is a two-level editor; an ungrouped collection still starts with the menu.
        if item == "No grouping":
            panel.get_by_role("button", name="Remove grouping", exact=True).click()
        else:
            if menu == "Then by" and not panel.get_by_label("Then by", exact=True).count():
                panel.get_by_role("button", name="+ Add subgroup", exact=True).click()
            field = panel.get_by_label(menu, exact=True)
            clean = lambda text: re.sub(r"\s*(?:\(default\)|default)$", "", text).strip()
            options = field.locator("option")
            value = next(options.nth(i).get_attribute("value") for i in range(options.count())
                         if clean(options.nth(i).inner_text()) == clean(item))
            field.select_option(value)
    elif not (tool == "Sort" and item == "No sorting" and not panel.get_by_role("menuitemradio", name=item, exact=True).count()):
        panel.get_by_role("menu", name=menu, exact=True).get_by_role("menuitemradio", name=item, exact=True).click()
    close_popover(getattr(scope, "page", scope))


def sort_direction(scope, direction, row=0):
    """Set a sort key's direction ("Ascending" / "Descending") in the open or reopened Sort popover."""
    toolbar(scope, "Sort").locator(".dt-sort-row").nth(row).get_by_label("Direction", exact=True).select_option(label=direction)


def filter_by(scope, label, names):
    """Tick values in a table's Filter popover (the shared condition builder): the property's row gains them, or a new
    "Where <label> is any of <names>" row is added."""
    panel = toolbar(scope, "Filter")
    page = panel.page
    lines = panel.locator(".cond-line:not(.dt-as-of)")
    for i in range(lines.count()):
        line = lines.nth(i)
        if line.get_by_label("Filter on", exact=True).evaluate("s=>s.selectedOptions[0].textContent") == label and "Choose" not in line.locator(".pick-button").inner_text():
            line.locator(".pick-button").click()
            for name in names:
                page.locator(".picker").get_by_role("option", name=name, exact=True).check()
            break
    else:
        condition(panel, label, names)
    close_popover(page)


def planner_filter(page):
    """The Schedule's Filter popover: the shared condition builder (one "Where <field> is any of" line per field) under
    the two Only … switches."""
    return toolbar(page.locator(".planner-toolbar"), "Filter")


def filter_line(pop, field):
    """The builder line that filters on `field` (None when there is none)."""
    lines = pop.locator(".cond-line:not(.dt-as-of)")
    for i in range(lines.count()):
        line = lines.nth(i)
        if line.get_by_label("Filter on", exact=True).evaluate("s=>s.selectedOptions[0].textContent") == field:
            return line
    return None


def filter_pick(pop, field, *names, only=False):
    """Choose values (by name) of one Schedule filter field in the shared condition builder; `only` clears its other
    values first (with no names, the field filters nothing)."""
    line = filter_line(pop, field)
    if line is None:
        if names:
            condition(pop, field, names)
        return
    line.locator(".pick-button").click()
    picker = pop.page.locator(".picker")
    expect(picker).to_be_visible()
    if only:
        chosen = picker.get_by_role("option", checked=True)
        while chosen.count():
            chosen.first.click()  # untick (a re-drawn option no longer matches `checked`)
    for name in names:
        picker.get_by_role("option", name=name, exact=True).check()
    pop.page.keyboard.press("Escape")
    expect(picker).to_have_count(0)


def planner_tool(page, item):
    """Run an entry of the schedule's "Rota tools" menu (Rules & targets, recurring series…). Side by side lives in
    View settings › Layers (SR-05); Export… is the Print or export dialog, Print sheet… its Published sheet… (SR-11)."""
    if item in ("Compare rotas", "Compare side by side"):
        view_settings(page.locator(".planner-toolbar"), "Layers").get_by_role("radiogroup", name="Show drafts").get_by_role("radio", name="Side by side", exact=True).click()
        page.keyboard.press("Escape")
        return
    if item in ("Export…", "Print sheet…"):
        planner_tool(page, "Print or export…")
        if item == "Print sheet…":
            page.get_by_role("dialog", name="Print or export").get_by_role("button", name="Published sheet…", exact=True).click()
        return
    # With no draft open the published-schedule entries are "Schedule tools", in the same top-bar place (S-18).
    name = "Rota tools" if page.get_by_role("button", name="Rota tools", exact=True).count() else "Schedule tools"
    page.get_by_role("button", name=name, exact=True).click()
    # An item may carry a hint after its name (e.g. a count): match the name at the start.
    page.get_by_role("menu", name=name, exact=True).get_by_role("menuitem", name=re.compile(rf"^{re.escape(item)}( |$)")).first.click()


def show_names(page, value):
    """How the schedule names people: View settings › Properties › Person label (the shared editor, view_settings.ts
    labelBody) with only this name format and nothing added but the title (Staff number: no name, the number alone)."""
    name = {"full": "Full name", "short": "F. Lastname", "code": "Short code", "number": "No name"}[value]
    panel = view_settings(page.locator(".planner-toolbar"), "Properties")
    panel.locator(".vs-row", has_text="Person label").click()
    expect(panel.locator(".vs-back strong")).to_have_text("Person label")
    keep = {"title", "number"} if value == "number" else {"title"}
    if value == "number" and not panel.locator('.vs-prop[data-part="number"]').count():
        panel.get_by_label("Add to the name", exact=True).select_option("number")
        expect(panel.locator('.vs-prop[data-part="number"]')).to_have_count(1)
    panel.get_by_role("radiogroup", name="Name", exact=True).get_by_role("radio", name=name, exact=True).click()
    while True:
        extra = [p for p in panel.locator(".vs-prop[data-part]").evaluate_all("ns=>ns.map(n=>n.dataset.part)") if p not in keep]
        if not extra:
            break
        panel.locator(f'.vs-prop[data-part="{extra[0]}"] .vs-eye').click()
        expect(panel.locator(f'.vs-prop[data-part="{extra[0]}"]')).to_have_count(0)
    close_popover(page)


def footer_item(page, selector, text):
    """An item of the one-line footer (owner rule: overflow behind "+N"): inline where it fits, else read in the "+N"
    key's popover (dom.ts fitFooter marks it .footer-over). Returns the item where it is shown."""
    footer = page.locator('.app-footer')
    item = footer.locator(selector).filter(has_text=text).first
    expect(item).to_be_attached()
    if not item.evaluate("n => !!n.closest('.footer-over')"):
        expect(item).to_be_visible()
        return item
    footer.locator('.footer-key').click()
    shown = page.locator('.popover.footer-key-pop').locator(selector).filter(has_text=text).first
    expect(shown).to_be_visible()
    return shown


def footer_working(page, text, phone=False):
    """A footer working item ("Checking the rota · 0:04 · Cancel"): where it is shown (inline, or behind the "+N" key on
    a one-line phone footer that has no room). Returns the footer or the key's popover holding it. `phone` is kept for
    callers; where the item sits is the footer's to decide, so both are read the same way."""
    footer_item(page, '[role=status]', text)
    pop = page.locator('.popover.footer-key-pop')
    return pop if pop.count() else page.locator('.app-footer')


def minutes_words(n):
    """A whole number of minutes as the app writes durations (dom.ts duration): "7 min", "1 h", "2 h 5 min"."""
    h, m = divmod(int(n), 60)
    return f'{h} h {m} min' if h and m else f'{h} h' if h else f'{m} min'


def auto_setup(panel, fields=True):
    """The Auto-schedule run card's setup (D76): from a run or its result, the setup line's Change (it opens the fields);
    on setup, `fields` opens the dates, clinics and people under the line."""
    body = panel.locator('.auto-body')
    expect(body).to_have_attribute('data-state', re.compile('^(setup|run)$'))
    change = panel.locator('.rl-done').get_by_role('button', name='Change', exact=True)
    if body.get_attribute('data-state') == 'run' or fields and not panel.locator('.auto-fill').count():
        change.click()
    expect(body).to_have_attribute('data-state', 'setup')


def auto_result(panel):
    """The run card's run or result (D76): from its setup, Show run or Back to the result."""
    body = panel.locator('.auto-body')
    expect(body).to_have_attribute('data-state', re.compile('^(setup|run)$'))
    if body.get_attribute('data-state') == 'setup':
        panel.get_by_role('button', name=re.compile('^(Show run|Back to the result)$')).click()
    expect(body).to_have_attribute('data-state', 'run')


def open_scheduling(page):
    """The auto-schedule panel opens from the planner header and stays open while the page re-renders."""
    button=page.get_by_role("button",name=re.compile(r"^Auto-schedule(?: · proposal ready)?$"))
    if button.get_attribute("aria-expanded")!="true":
        button.click()
    expect(page.locator("#auto-panel")).to_be_visible()


def fill_catalogue(page,name):
    dialog=page.get_by_role("dialog")
    dialog.get_by_label("Name",exact=True).fill(name)
    dialog.get_by_label("Effective from",exact=True).fill("2026-10-01")
    dialog.get_by_label("Change reason",exact=True).fill("Fictional browser acceptance configuration")
    return dialog


def add_filter(scope,group=False):
    """+ Add filter rule ▾ in the shared condition builder: a rule, or (top level) a group with one rule."""
    page=getattr(scope,"page",scope)
    scope.get_by_role("button",name="+ Add filter rule",exact=True).first.click()
    page.get_by_role("menu",name="Add",exact=True).get_by_role("menuitem",name="Add filter group" if group else "Add filter rule",exact=True).click()


def row_action(row,item):
    """A filter row's ⋯ menu: Duplicate, Turn into group, Remove (a group's: Duplicate, Remove group)."""
    row.locator(":scope > .cond-menu-cell .cond-row-menu").click()
    menu=row.page.locator(".popover[role=menu]")  # the row's menu is the popup itself (dom.ts menu())
    menu.get_by_role("menuitem",name=item,exact=True).click()
    expect(menu).to_have_count(0)


def condition(scope,field,names,op="any"):
    """Add "Where <field> is any of / none of <names>" in the shared condition builder (an empty line is reused)."""
    line=scope.locator(".cond-line").last
    if "Choose" not in line.locator(".pick-button").inner_text():
        add_filter(scope)
        line=scope.locator(".cond-line").last
    line.scroll_into_view_if_needed()  # the list opens beside its line
    chooser=line.get_by_label("Filter on",exact=True)
    if chooser.evaluate("s=>s.selectedOptions[0].textContent")!=field:
        chooser.select_option(label=field)
        expect(scope.locator(".picker")).to_have_count(1)
    else:
        line.locator(".pick-button").click()
    for name in names:
        scope.locator(".picker").get_by_role("option",name=name,exact=True).check()
    scope.page.keyboard.press("Escape")
    expect(scope.locator(".picker")).to_have_count(0)
    line.get_by_label("Operator",exact=True).select_option(op)


def pick(scope,label,names,checked=True):
    """Tick (or untick) names in a multiple-choice field: the shared picker for longer lists, plain checkboxes for
    short ones. The picker opens in the top layer, so it is found on the page rather than inside the scope."""
    field=scope.locator(".pick-field, fieldset.multi-field").filter(has_text=label).first
    trigger=field.locator(".pick-button")
    if trigger.count():
        trigger.click()
        picker=scope.page.locator(".picker")
        for name in names:
            picker.get_by_role("option",name=name,exact=True).set_checked(checked)
        scope.page.keyboard.press("Escape")
        expect(picker).to_have_count(0)
    else:
        for name in names:
            field.get_by_label(name,exact=True).set_checked(checked)


def form_submit(dialog):
    """A form dialog's own submit button (its words depend on what the form creates)."""
    return dialog.locator(".form-footer button[type=submit]")


def save_dialog(dialog,label=None):
    """Save a form dialog: its own submit (a catalogue form's is "Add <thing>", "Save version"…) unless `label` names one."""
    # A configuration form works out its impact by itself as it is filled in: wait until it is shown.
    if dialog.get_by_role("heading",name="Review configuration consequences",exact=True).count():
        expect(dialog.get_by_text(re.compile(r"active rotas? checked\.|Configuration checked\.|records will be saved\.")).first).to_be_visible(timeout=20000)
    draft=dialog.get_by_role("button",name="Calculate draft impact",exact=True)
    def submit():
        if draft.count():
            draft.click()
            expect(dialog.get_by_text("Not saved yet: review what this changes",exact=False)).to_be_visible(timeout=20000)
        (dialog.locator(".form-footer button[type=submit]") if label is None else dialog.get_by_role("button",name=label,exact=True)).click()
        armed=dialog.locator("button.armed")  # irreversible (publish): the same button asks "Confirm?" once more
        if armed.count():
            armed.click()
    submit()
    # D-077: a draft edit saves on one press (the impact shown is optional), so there is no second Save to retry.
    expect(dialog).not_to_be_visible(timeout=60000)


def decide(page,label):
    """A governed step. Submitting and publishing go through one dialog, named after the step that happens: a draft's
    "Submit for review…" (primary "Submit for review"), an approved rota's "Publish…" (primary "Publish", a same-button
    confirm). Both have an optional audit reason."""
    if label in {"Submit for review","Publish rota"}:
        submit=label=="Submit for review"
        page.get_by_role("button",name="Submit for review…" if submit else "Publish…",exact=True).click()
        dialog=page.get_by_role("dialog",name=re.compile("^Submit .* for review$" if submit else "^Publish "))
        # ME-25: the optional note (shown to the approver when submitting).
        dialog.get_by_label("Note for the approver (optional)" if submit else "Note (optional)",exact=True).fill("Independent UI acceptance review")
        save_dialog(dialog,"Submit for review" if label=="Submit for review" else "Publish")
        return
    page.get_by_role("button",name=label,exact=True).click()
    dialog=page.get_by_role("dialog")
    # An independent rota approval takes an optional Note (BE-9); a self-approval and other decisions need a reason.
    dialog.get_by_label(re.compile("^(Note|Reason)$")).fill("Independent UI acceptance review")
    save_dialog(dialog,label)


SMOKE_PATHS={"admin":["overview","people","configuration","rules","programmes","governance","inbox","access"],"coordinator":["overview","setup","planner","published","people","rules","availability","programmes","reports","data","governance","swaps","inbox"],"approver":["overview","planner","published","rules","availability","reports","governance","swaps","inbox"],"credentialer":["people","inbox"],"staff":["published","availability","reports","swaps","inbox"],"operator":["operations","audit","inbox"],"auditor":["planner","published","reports","operations","audit"],"programme_lead":["published","people","programmes","fairness","inbox","access"]}


def test_every_permitted_surface_loads_without_javascript_errors(browser,live_server,h):
    scripts=[]
    assert set(SMOKE_PATHS)==set(ROLE_NAMES)
    identity(h,'programme_lead',['programme_lead'])
    paths={role:list(surfaces) for role,surfaces in SMOKE_PATHS.items()}
    for role,surfaces in paths.items():
        if role in {'admin','coordinator','approver','credentialer','auditor'}:
            surfaces.append('directory')
        if role in {'admin','coordinator','approver','auditor','staff'}:
            surfaces.append('today')
        context,page=sign_in(browser,live_server,role)
        page.on("pageerror",lambda error:scripts.append(str(error)))
        navigate(page,"home")
        expect(page.get_by_role("heading",name="Needs you",exact=True)).to_be_visible()
        for surface in surfaces:
            navigate(page,surface)
            page.wait_for_timeout(70)
            assert not page.get_by_text("Unable to load this workspace",exact=False).count()
        context.close()
    assert not scripts


def test_no_code_category_location_type_publish_second_account_and_absence(browser,live_server,h,tmp_path):
    suffix=uuid.uuid4().hex[:8]
    category="Visiting clinical staff "+suffix
    location="Satellite Clinic B "+suffix
    type_name="Extended-hours review "+suffix
    contexts=[]; errors=[]
    admin_context,admin=sign_in(browser,live_server,"admin");contexts.append(admin_context)
    admin.on("pageerror",lambda error:errors.append(str(error)))
    navigate(admin,"configuration")
    admin.get_by_role("button",name="Ranks",exact=True).click()
    admin.get_by_role("button",name="New rank",exact=True).click()
    save_dialog(fill_catalogue(admin,category))
    admin.get_by_role("button",name="Locations",exact=True).click()
    admin.get_by_role("button",name="New location",exact=True).click()
    dialog=fill_catalogue(admin,location);dialog.get_by_label("Location type",exact=True).select_option(label="Outpatient room")
    save_dialog(dialog)
    admin.get_by_role("button",name="Duty types",exact=True).click()
    admin.get_by_role("button",name="New duty type",exact=True).click()
    dialog=fill_catalogue(admin,type_name)
    dialog.get_by_label(category,exact=True).first.check()
    dialog.get_by_label("Requirement to minute",exact=True).fill("120")
    dialog.get_by_role("button",name="Add staffing interval",exact=True).click()
    dialog.get_by_label("Requirement from minute",exact=True).nth(1).fill("120")
    dialog.get_by_label("Minimum staff",exact=True).nth(1).fill("1")
    dialog.get_by_role("button",name="Add custom field",exact=True).click()
    dialog.get_by_label("Field key",exact=True).fill("review_reference")
    dialog.get_by_label("Field label",exact=True).fill("Review reference")
    dialog.get_by_label("Required",exact=True).check()
    dialog.get_by_label("Include in exports",exact=True).check()
    save_dialog(dialog)
    coordinator_context,coordinator=sign_in(browser,live_server,"coordinator");contexts.append(coordinator_context)
    navigate(coordinator,"people")
    coordinator.get_by_role("button",name=re.compile("^More actions for Staff 01\\(test\\)")).click()
    coordinator.get_by_role("menuitem",name="Add rank",exact=True).click()
    dialog=coordinator.get_by_role("dialog")
    expect(dialog.locator(".relationship-person")).to_contain_text("Staff 01(test)")  # SW3-D-14: the row's person, fixed
    dialog.get_by_label("Rank",exact=True).select_option(label=category)
    dialog.get_by_label("From",exact=True).fill("2026-10-01")
    save_dialog(dialog,"Add rank")
    navigate(coordinator,"planner")
    new_draft(coordinator)
    dialog=coordinator.get_by_role("dialog")
    dialog.get_by_label("Rota name",exact=True).fill("No-code UI acceptance "+suffix)
    dialog.get_by_label("First day",exact=True).fill("2026-10-04")
    dialog.get_by_label("Last day",exact=True).fill("2026-10-10")
    save_dialog(dialog,"Create draft")
    coordinator.locator("#main").get_by_role("button", name="Add session", exact=True).click()  # the toolbar's, or an empty week's card's (SX-14: one shows)
    dialog=coordinator.locator(".dock-panel")
    dialog.get_by_label("Duty type",exact=True).select_option(label=type_name)
    dialog.get_by_label("Location",exact=True).select_option(label=location)
    dialog.get_by_label("Session start",exact=True).fill("2026-10-04T22:00")
    dialog.get_by_label("Session end",exact=True).fill("2026-10-05T02:00")
    dialog.get_by_label("Continuity clinic",exact=True).check()
    dialog.get_by_label("Review reference",exact=True).fill("Fresh-config-browser-evidence")
    dialog.get_by_role("button",name="Add participant",exact=True).click()
    expect(dialog.get_by_text("Whole session for everyone · 4 Oct 2026, 22:00 – 5 Oct 2026, 02:00",exact=True)).to_be_visible()
    dialog.get_by_label("Participant",exact=True).select_option(label="Staff 01(test)")
    dialog.get_by_label("Overtime — Staff 01(test)",exact=True).check()
    save_dialog(dialog,"Save draft session")
    expect(coordinator.locator('main').get_by_text(type_name,exact=True).locator('visible=true').first).to_be_visible()
    decide(coordinator,"Submit for review")
    approver_context,approver=sign_in(browser,live_server,"approver");contexts.append(approver_context)
    navigate(approver,"planner");decide(approver,"Approve rota")
    # A second account is already open before publication; polling must discover it.
    staff_context,staff=sign_in(browser,live_server,"staff");contexts.append(staff_context)
    navigate(staff,"schedule/week/2026-10-04")  # the published days: the timeline opens on today
    coordinator.reload();expect(coordinator.get_by_role("button",name="Publish…",exact=True)).to_be_visible(timeout=15000)
    decide(coordinator,"Publish rota")
    expect(staff.get_by_text(type_name,exact=True).first).to_be_visible(timeout=16000)
    evidence=tmp_path/"browser-evidence";evidence.mkdir()
    staff.screenshot(path=str(evidence/"staff-published-desktop.png"),full_page=True)
    navigate(coordinator,"planner");coordinator.screenshot(path=str(evidence/"coordinator-planner.png"),full_page=True)
    expect(coordinator.locator('.review-bar')).not_to_contain_text('Needs a check')
    navigate(staff,"insights/workload")
    # Staff see their own workload as one line, not a one-row table.
    expect(staff.locator(".workload-mine")).to_have_text("1 duty · 4 scheduled hours · 1 overtime (4 h)")
    pub=h.get("staff","/api/publications")["publications"][0]
    report=h.get("staff","/api/reports/"+pub["id"])
    assert report["rows"][0]["scheduled_hours"]==4 and report["rows"][0]["planned_overtime_assignments"]==1
    response=h.client("staff").get("/api/reports/"+pub["id"]+"?format=assignments")
    assert "Fresh-config-browser-evidence" in response.text and type_name in response.text
    navigate(coordinator,"requests")
    coordinator.get_by_role("button",name="New request",exact=True).click()
    dialog=coordinator.get_by_role("dialog")
    dialog.get_by_role("radio",name=re.compile("^Leave or absence")).check()
    pick(dialog,"People",["Staff 01(test)"])
    dialog.get_by_label("All day").uncheck()
    dialog.get_by_label("Unavailable from",exact=True).fill("2026-10-05T00:00")
    dialog.get_by_label("Unavailable until",exact=True).fill("2026-10-05T01:00")
    dialog.get_by_label("Type", exact=True).select_option(label="Annual")
    dialog.get_by_label("Reason", exact=True).fill("Private synthetic evidence, not patient data")
    save_dialog(dialog,"Record leave")
    navigate(staff,"schedule/week/2026-10-04")
    expect(staff.locator(".pchip.gap").first).to_be_visible(timeout=16000)  # open place on the read-only week grid
    assert "Private synthetic evidence" not in staff.locator("body").inner_text()
    staff.set_viewport_size({"width":390,"height":844});staff.screenshot(path=str(evidence/"staff-mobile-absence.png"),full_page=True)
    assert staff.evaluate("document.documentElement.scrollWidth<=window.innerWidth+2")
    staff.get_by_role("button",name=re.compile(r"^Account menu, ")).click()
    staff.get_by_role("menuitem",name="Sign out",exact=True).click()
    expect(staff.get_by_label("Username or email", exact=True)).to_be_visible()
    assert type_name not in staff.locator("body").inner_text()
    # Sign-out leaves only the sign-in page's remembered account (who, never a password or session) and when this
    # device signed out (for its other tabs, QA-129).
    assert sorted(staff.evaluate("[...Object.keys(localStorage),...Object.keys(sessionStorage)]"))==["fmuc.signed-out-at","rota.account"]
    assert set(staff.evaluate("JSON.parse(localStorage.getItem('rota.account'))"))=={"identity","name","username","at"}  # at: when, so it expires (SEC-10)
    assert not errors
    for context in contexts:context.close()
