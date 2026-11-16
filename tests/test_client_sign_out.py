"""maintenance, truthful sign-out (the real compiled api.ts in Node, no DOM): logout() forgets everything kept for the account
before the server answers, sends one request however often it is asked, says ended only on a 2xx or a 401 (the session
already gone), and says not ended on a refusal or after SIGN_OUT_TIMEOUT_MS without an answer. app.ts signOut is the one
path every Sign out control takes, and it forgets the page and the device's keys before it waits."""
import json
import re
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / 'apps/web/src/app.ts'

# A fake clock (the timeout fires on demand), a cached read to forget and a logout the test answers when it chooses.
SETUP = ("globalThis.document={cookie:''};globalThis.window=globalThis;const timers=[];const realSet=setTimeout;"
         "globalThis.setTimeout=(f,ms)=>{if(ms===m.SIGN_OUT_TIMEOUT_MS){timers.push(f);return 0;}return realSet(f,ms);};"
         "const sent=[];let answer=null;"
         "let renew=200;const tick=()=>new Promise(r=>realSet(r,0));"
         "globalThis.fetch=(p,o)=>{const h=new Headers(o.headers),tag=h.get('If-None-Match');sent.push({p,method:o.method||'GET',tag,csrf:h.get('X-CSRF-Token')});"
         "if(p==='/auth/refresh'){if(!renew)return new Promise(()=>{});if(renew===200)document.cookie='rota_csrf=fresh';return Promise.resolve(new Response('{}',{status:renew}));}"
         "if(p!=='/auth/logout')return Promise.resolve(new Response(JSON.stringify({x:1}),{status:200,headers:{ETag:'\"v1\"'}}));"
         "return new Promise((ok,fail)=>{answer=ok;o.signal?.addEventListener('abort',()=>fail(new DOMException('aborted','AbortError')));});};")


def run(client_modules, body):
    script = (f"const m=await import({json.dumps((client_modules / 'api.js').as_uri())});{SETUP}"
              f"const out=await (async()=>{{{body}}})();console.log(JSON.stringify(out));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_everything_kept_is_forgotten_before_the_server_answers(client_modules):
    got = run(client_modules, "let forgot=0;m.onSessionReset(()=>forgot++);await m.get('/api/catalogues');const key=m.sessionKey();"
                              "const p=m.logout();const at={forgot,changed:m.sessionKey()!==key,signingOut:m.signingOutNow()};"
                              "await m.get('/api/catalogues');answer(new Response('{}',{status:200}));await p;"
                              "return {at,reread:sent[2].tag};")
    assert got['at'] == {'forgot': 1, 'changed': True, 'signingOut': True}
    assert got['reread'] is None  # the kept reply (its ETag) went with the session: the next read is in full


def test_one_request_however_often_it_is_asked(client_modules):
    got = run(client_modules, "const a=m.logout(),b=m.logout();answer(new Response('{}',{status:200}));"
                              "const [x,y]=await Promise.all([a,b]);return {same:a===b,x,y,posts:sent.filter(s=>s.p==='/auth/logout').length};")
    assert got == {'same': True, 'x': {'ended': True}, 'y': {'ended': True}, 'posts': 1}


def test_ended_only_when_the_server_says_so(client_modules):
    got = run(client_modules, """const results=[];
      for(const [status,body] of [[200,'{"end_session_url":"https://idp.example/end"}'],[401,'{"error":{"code":"NOT_SIGNED_IN","message":"x"}}'],
                                   [500,'{"error":{"code":"INTERNAL_ERROR","message":"x"}}']]){
        const p=m.logout();answer(new Response(body||null,{status}));results.push(await p);}
      return results;""")
    assert got == [{'ended': True, 'endSession': 'https://idp.example/end'}, {'ended': True},
                   {'ended': False}]


def test_no_answer_in_time_is_not_a_sign_out_and_a_retry_sends_again(client_modules):
    got = run(client_modules, "const p=m.logout();timers.shift()();const first=await p;const after=m.signingOutNow();"
                              "const q=m.logout();answer(new Response('{}',{status:200}));"
                              "return {first,after,second:await q,posts:sent.filter(s=>s.p==='/auth/logout').length,limit:m.SIGN_OUT_TIMEOUT_MS};")
    assert got == {'first': {'ended': False}, 'after': False, 'second': {'ended': True}, 'posts': 2, 'limit': 20000}


def test_every_sign_out_control_takes_the_one_path_and_it_forgets_first():
    src = APP.read_text(encoding='utf-8')
    assert src.count('logout()') == 1, 'app.ts calls logout() only from signOut'
    body = re.search(r'async function signOut\(tellTabs=true\)\{(.*?)\n\}\n', src, re.S).group(1)
    wait = body.index('logout()')
    for forget in ('forgetSession()', 'signedOut()', 'clearPrefs()', 'forgetPhoneContinue()', 'clearTableViews()',
                   'forgetRecent()', 'forgetPanelWidths()', 'forgetDeviceMemory()', 'history.replaceState(', 'root.replaceChildren('):
        assert -1 < body.index(forget) < wait, f'{forget} runs before the server is asked'
    assert body.index('if(answer.ended){await showLogin()') > wait  # sign-in only after the server's word
    for control in re.findall(r"'Sign out'[^\n]{0,120}", src):
        assert 'signOut()' in control, control
    assert "new BroadcastChannel('fmuc-session')" in src and "postMessage('signing-out')" in src
    assert body.index('localStorage.setItem(SIGNED_OUT_AT') < wait  # a tab restored from the page cache can tell
    restore = src[src.index("window.addEventListener('pageshow'"):][:900]
    assert 'if(!me){location.reload();return;}' in restore and 'signedOutSince()' in restore and "fetch('/api/me'" in restore


STALE = "document.cookie='rota_csrf=stale';const csrf=()=>sent.filter(s=>s.p==='/auth/logout').map(s=>s.csrf);"
CSRF = "new Response(JSON.stringify({error:{code:'CSRF_FAILED',message:'x'}}),{status:403})"


def test_a_stale_csrf_token_is_renewed_and_the_sign_out_sent_again(client_modules):
    got = run(client_modules, STALE + f"const p=m.logout();answer({CSRF});await tick();await tick();"
                              "answer(new Response('{}',{status:200}));return {r:await p,csrf:csrf()};")
    assert got == {'r': {'ended': True}, 'csrf': ['stale', 'fresh']}


def test_a_failed_renewal_says_not_signed_out_and_try_again_renews_first(client_modules):
    got = run(client_modules, STALE + f"renew=401;const p=m.logout();answer({CSRF});const first=await p;"
                              f"renew=200;const q=m.logout();answer({CSRF});await tick();await tick();"
                              "answer(new Response('{}',{status:200}));"
                              "return {first,second:await q,csrf:csrf(),renewals:sent.filter(s=>s.p==='/auth/refresh').length};")
    assert got == {'first': {'ended': False}, 'second': {'ended': True}, 'csrf': ['stale', 'stale', 'fresh'], 'renewals': 2}


def test_the_time_limit_covers_a_renewal_that_never_answers(client_modules):
    got = run(client_modules, STALE + f"renew=0;const p=m.logout();answer({CSRF});await tick();timers.shift()();"
                              "return {r:await p,signingOut:m.signingOutNow()};")
    assert got == {'r': {'ended': False}, 'signingOut': False}
