"""maintenance: in a tab still running an older bundle than the server's, a page that fails to draw, an error message, an
error notice or an uncaught error reloads once onto the new bundle. With something unsaved it offers "Reload to update"
instead. The loop guard (sessionStorage, keyed by the target bundle) stops a second reload for the same bundle.
stale_bundle.ts runs here in Node from the compiled client."""
import itertools
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'apps/web/src'
STORE = ("const store=new Map(),reloads=[];globalThis.sessionStorage={getItem:k=>store.has(k)?store.get(k):null,"
         "setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};globalThis.location={reload:()=>reloads.push(1)};")


@pytest.fixture(scope='module')
def run(client_modules):
    module = (client_modules / 'stale_bundle.js').as_uri()
    api = (client_modules / 'api.js').as_uri()

    def go(body, payload=None):
        script = f"{STORE}const m=await import({json.dumps(module)}),a=await import({json.dumps(api)});const input=JSON.parse(process.argv[1]);console.log(JSON.stringify((()=>{{{body}}})()));"
        out = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script, json.dumps(payload)],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return go


def expected(newer, failed, dirty, flagged):
    if not (newer and failed) or flagged:
        return 'none'
    return 'ask' if dirty else 'reload'


def test_the_decision_for_every_case(run):
    cases = list(itertools.product([True, False], repeat=4))
    got = run("return input.map(([newer,failed,dirty,flagged])=>m.staleChoice({own:'old',seen:newer?'new':'old',failed,dirty,"
              "reloadedFor:flagged?'new':''}));", cases)
    assert got == [expected(*c) for c in cases]


def test_unknown_builds_never_reload(run):
    # An API-only server sends no build; a bundle served without its directory has no identity of its own.
    assert run("return [m.staleChoice({own:'old',seen:'',failed:true,dirty:false,reloadedFor:''}),"
               "m.staleChoice({own:'',seen:'new',failed:true,dirty:false,reloadedFor:''})];") == ['none', 'none']


def test_the_guard_is_keyed_by_the_target_bundle(run):
    got = run("const before=m.reloadedFor();const did=m.reloadFor('new');"
              "return [before,did,reloads.length,m.reloadedFor(),"
              "m.staleChoice({own:'old',seen:'new',failed:true,dirty:false,reloadedFor:m.reloadedFor()}),"
              "m.staleChoice({own:'old',seen:'newer',failed:true,dirty:false,reloadedFor:m.reloadedFor()})];")
    assert got == ['', True, 1, 'new', 'none', 'reload']  # once for 'new'; a later bundle may reload again


def test_without_storage_it_never_reloads(run):
    assert run("globalThis.sessionStorage={getItem(){throw 1},setItem(){throw 1}};return [m.reloadFor('new'),reloads.length];") == [False, 0]


def test_updated_is_said_once_after_landing_on_the_bundle(run):
    assert run("sessionStorage.setItem('fmuc.stale-reload',m.OWN_BUILD);return [m.justUpdated(),m.justUpdated(),m.reloadedFor()===m.OWN_BUILD];") == [True, False, True]
    assert run("sessionStorage.setItem('fmuc.stale-reload','another');return m.justUpdated();") is False


# maintenance follow-up: an error message, an error notice or an uncaught error in an outdated tab takes the same step.
FAULT = ("const asked=[];let dirty=false;m.staleHooks({unsaved:()=>dirty,ask:()=>asked.push(1)});"
         "m.noticeServerBuild('newer-'+m.OWN_BUILD);")


def test_a_bug_or_server_fault_reloads_once(run):
    got = run(FAULT + "return [m.staleFault(new TypeError('x is undefined')),reloads.length,m.reloadedFor()===m.serverBuild,"
              "m.staleFault(new a.ApiError('BOOM','Server error',500)),reloads.length,asked.length];")
    assert got == ['reload', 1, True, 'none', 1, 0]  # the guard: once per bundle, whatever fails next


def test_with_something_unsaved_it_shows_the_update_bar_instead(run):
    assert run(FAULT + "dirty=true;return [m.staleFault(new TypeError('x')),reloads.length,asked.length];") == ['ask', 0, 1]


def test_refusals_and_lost_connections_are_not_the_bundles(run):
    got = run(FAULT + "return [m.staleFault(new a.ApiError('NETWORK_ERROR','Lost',0)),m.staleFault(new a.ApiError('INVALID','Name is required',422)),"
              "m.staleFault(new Error('declined')),m.staleFault(undefined),reloads.length,asked.length];")
    assert got == ['none', 'none', 'none', 'none', 0, 0]


def test_no_newer_build_known_never_reloads(run):
    assert run("return [m.staleFault(new TypeError('x')),reloads.length];") == ['none', 0]
    assert run("m.noticeServerBuild(m.OWN_BUILD);return [m.staleFault(new TypeError('x')),reloads.length];") == ['none', 0]


def test_every_error_path_reaches_the_one_step():
    stale = (SRC / 'stale_bundle.ts').read_text(encoding='utf-8')
    assert "window.addEventListener('error',e=>void staleFault(e.error));" in stale
    assert "window.addEventListener('unhandledrejection',e=>void staleFault(e.reason));" in stale
    dom = (SRC / 'dom.ts').read_text(encoding='utf-8')
    # notify (every error message: reportError, a button's failed action) and note (a section's error panel).
    assert "{recordError(message,'message shown');staleFault(error);" in dom
    assert "{recordError(message,'notice');staleFault(error);" in dom
    # One reload engine: only stale_bundle.ts decides or reloads.
    for path in SRC.glob('*.ts'):
        if path.name != 'stale_bundle.ts':
            text = path.read_text(encoding='utf-8')
            assert 'staleChoice(' not in text and 'fmuc.stale-reload' not in text, path.name


def test_the_failed_page_is_where_the_rule_runs():
    app = (SRC / 'app.ts').read_text(encoding='utf-8')
    assert "const update=updateAfter(!(e instanceof ApiError&&e.status===0));" in app
    assert "if(update==='reload')return;" in app
    assert "button('Reload to update'" in app
    # The dirty-close rule and the update bar are app.ts's, given to the one step.
    assert "staleHooks({unsaved:()=>unsavedNow()||!!document.querySelector('dialog[open]'),ask:()=>newVersion()});" in app
    # No new polling: the server's build comes from the pulse the app already reads.
    assert "if(!build)return;noticeServerBuild(build);" in app
    assert "seenActivity=p.activity;noticeNewVersion(p.build);" in app  # the start-up pulse it already reads
