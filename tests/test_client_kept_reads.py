"""maintenance/118: api.ts keptRead, the one place a page keeps its answers across rebuilds (the real compiled api.ts in Node,
fetch stubbed). Identical keys share one request, in flight and answered; distinct keys never do; a failed read is not
kept; a sign-out, the person's own save and a live change the group cannot ignore drop them; the cap drops the oldest;
`queueBehindInFlight` waits for the read on its way instead of stacking another. Consumers: test_client_fairness_reads.py
and test_client_timeline_reads.py."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS


def test_kept_reads_share_identical_keys_and_forget_on_reset_save_and_scope(client_modules):
    api = (client_modules/'api.js').as_uri()
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "const sent=[];let answer;let slow=new Promise(r=>{answer=r;});let fail=false;"
        "globalThis.fetch=async p=>{sent.push(p);await slow;if(fail)return new Response('{}',{status:500});"
        "return new Response(JSON.stringify({p}),{status:200});};"
        f"const api=await import({json.dumps(api)});"
        "const KEEP={keepScopes:['inbox'],cap:2},DROP={scopes:['rules'],cap:8},Q={keepScopes:[],cap:4,queueBehindInFlight:true};"
        "const r=(url,o=DROP)=>api.keptRead(url,s=>api.request(url,{signal:s}),o);const n=()=>sent.length;const out={};"
        # Concurrent identical keys: one request; distinct keys: their own.
        "const a=[r('/a'),r('/a'),r('/b')];out.inFlight=n();out.same=a[0]===a[1];answer();await Promise.all(a.map(x=>x.p));"
        "out.data=a[0].data;await r('/a').p;out.answered=n();"
        # Scopes: DROP ignores what it does not name; KEEP keeps only what it names; null drops every group.
        "await r('/k',KEEP).p;const base=n();"
        "api.forgetReads(new Set(['inbox']));await r('/a').p;await r('/k',KEEP).p;out.unrelated=n()-base;"
        "api.forgetReads(new Set(['people']));await r('/a').p;await r('/k',KEEP).p;out.people=n()-base;"
        "api.forgetReads(new Set(['rules']));await r('/a').p;out.rules=n()-base;"
        "api.forgetReads(null);await r('/a').p;await r('/k',KEEP).p;out.unnamed=n()-base;"
        # The person's own save forgets them; the app's automatic check does not.
        "dispatchEvent(new CustomEvent('rota-saved',{detail:{kind:'workspace.validate'}}));await r('/a').p;out.automatic=n()-base;"
        "dispatchEvent(new CustomEvent('rota-saved',{detail:{kind:'people.update'}}));await r('/a').p;out.saved=n()-base;"
        # Sign-out forgets them.
        "api.clearSessionState();await r('/a').p;out.signedOut=n()-base;"
        # Cap: the oldest goes first.
        "await r('/k1',KEEP).p;await r('/k2',KEEP).p;await r('/k3',KEEP).p;const c=n();await r('/k3',KEEP).p;await r('/k1',KEEP).p;out.capped=n()-c;"
        # A failed read is not kept: the next asks again.
        "fail=true;await r('/f').p.catch(()=>{});fail=false;await r('/f').p;out.failedAgain=sent.filter(p=>p==='/f').length;"
        # Queue: dropped while on its way, three asks wait for it and read once together.
        "slow=new Promise(x=>{answer=x;});const q0=r('/q',Q);api.forgetReads(null);const qs=[r('/q',Q),r('/q',Q),r('/q',Q)];"
        "out.queuedBefore=sent.filter(p=>p==='/q').length;answer();await q0.p;await Promise.all(qs.map(x=>x.p));"
        "out.queuedAfter=sent.filter(p=>p==='/q').length;"
        # Without the queue a dropped read on its way is asked again at once.
        "slow=new Promise(x=>{answer=x;});const d0=r('/d');api.forgetReads(null);const d1=r('/d');"
        "out.unqueued=sent.filter(p=>p==='/d').length;answer();await Promise.all([d0.p,d1.p]);"
        # Leaving the page: a read it does not abort is still shared by the next page; a page read it aborted (get) is
        # asked again for the newer page, which gets the answer, never PAGE_LEFT.
        "slow=new Promise(x=>{answer=x;});const l0=r('/left');api.leavePage();const l1=r('/left');"
        "const g=u=>api.keptRead(u,()=>api.get(u),DROP);const p0=g('/page');p0.p.catch(()=>{});api.leavePage();const p1=g('/page');"
        "answer();await l0.p;out.leftData=(await l1.p).p;out.pageData=(await p1.p).p;"
        "out.leftSent=sent.filter(p=>p==='/left').length;out.pageSent=sent.filter(p=>p==='/page').length;"
        "console.log(JSON.stringify(out));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {'inFlight': 2, 'same': True, 'data': {'p': '/a'}, 'answered': 2,
                   'unrelated': 0, 'people': 1, 'rules': 2, 'unnamed': 4, 'automatic': 4, 'saved': 5, 'signedOut': 6,
                   'capped': 1, 'failedAgain': 2, 'queuedBefore': 1, 'queuedAfter': 2, 'unqueued': 2,
                   'leftData': '/left', 'pageData': '/page', 'leftSent': 1, 'pageSent': 2}


def test_a_rotas_explainer_is_dropped_by_a_change_to_that_rota_only(client_modules):
    """The rota route says whether each rule holds in that rota: a change to it (its id), a publication or a rule change
    reads it again; another rota's change keeps it. The department route ignores rota changes."""
    api, explained = ((client_modules/name).as_uri() for name in ('api.js', 'rules_explained.js'))
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "const sent=[];globalThis.fetch=async p=>{sent.push(p);return new Response(JSON.stringify({rules:[],conflicts:[]}),{status:200});};"
        f"const api=await import({json.dumps(api)}),x=await import({json.dumps(explained)});"
        "const A=()=>x.rotaSentences('w-a','w-a:3::f'),B=()=>x.rotaSentences('w-b','w-b:7::f'),D=()=>x.departmentRules('2026-11-08');"
        "const ask=async()=>{await A();await B();await D();};const of=u=>sent.filter(p=>p.includes(u)).length;"
        "const forget=(scope,id)=>api.forgetReads(new Set([scope]),[{scope,id}]);const out={};"
        "await Promise.all([A(),A(),B(),D(),D()]);await ask();out.first=[of('/w-a/'),of('/w-b/'),of('group_by')];"
        "forget('workspace','w-b');await ask();out.otherRota=[of('/w-a/'),of('/w-b/'),of('group_by')];"
        "forget('workspace',null);await ask();out.someRota=[of('/w-a/'),of('/w-b/'),of('group_by')];"
        "api.forgetReads(new Set(['workspace']));await ask();out.unsaid=[of('/w-a/'),of('/w-b/'),of('group_by')];"
        "forget('publication','p1');await ask();out.published=[of('/w-a/'),of('/w-b/'),of('group_by')];"
        "forget('rules','r1');await ask();out.rules=[of('/w-a/'),of('/w-b/'),of('group_by')];"
        "console.log(JSON.stringify(out));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {'first': [1, 1, 1], 'otherRota': [1, 2, 1], 'someRota': [2, 3, 1], 'unsaid': [3, 4, 1],
                   'published': [4, 5, 1], 'rules': [5, 6, 2]}


def test_any_live_change_stops_every_kept_reply_answering_a_page_opening(client_modules):
    """maintenance/maintenance (maintenance review): a page opened again answers from the replies this tab holds (keptReads). A reply carries more
    than the page that read it shows (/api/catalogues holds people and settings), so ANY live change (app.ts onPulse →
    forgetReads), whatever its scope, makes the next opening ask the server, with the kept ETag (a conditional read);
    a reply on its way when the change lands is not shown as current either. Until a change, openings use memory."""
    api = (client_modules/'api.js').as_uri()
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "const sent=[];let gate=null;globalThis.fetch=async(p,o)=>{const tag=new Headers(o?.headers).get('If-None-Match');sent.push([p,tag]);"
        "if(gate)await gate;if(tag)return new Response(null,{status:304,headers:{ETag:tag}});"
        "return new Response(JSON.stringify({p}),{status:200,headers:{ETag:'\"'+p+'\"'}});};"
        f"const api=await import({json.dumps(api)});"
        "const open=async p=>{let got;await api.keptReads(async()=>{got=await api.get(p);},()=>{});return got;};"
        "const of=p=>sent.filter(s=>s[0]===p);const both=async()=>{await open('/api/catalogues');await open('/api/home');};"
        "const counts=()=>[of('/api/catalogues').length,of('/api/home').length];const out={};"
        "await both();out.first=counts();await both();out.kept=counts();"
        # Read on Home, a people change: the catalogues reply is asked again, conditionally.
        "api.forgetReads(new Set(['people']),[{scope:'people',id:'p1'}]);await both();out.people=counts();"
        "out.conditional=of('/api/catalogues').at(-1)[1];out.keptAgain=(await both(),counts());"
        "api.forgetReads(null);await both();out.unnamed=counts();"
        # A change while a read is on its way: its answer is not kept as current.
        "let release;gate=new Promise(r=>{release=r;});api.forgetReads(null);const flying=open('/api/home');"
        "api.forgetReads(new Set(['workspace']));release();await flying;gate=null;await open('/api/home');out.inFlight=of('/api/home').length;"
        "out.data=await open('/api/catalogues');"
        "console.log(JSON.stringify(out));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {'first': [1, 1], 'kept': [1, 1], 'people': [2, 2], 'conditional': '"/api/catalogues"',
                   'keptAgain': [2, 2], 'unnamed': [3, 3], 'inFlight': 5, 'data': {'p': '/api/catalogues'}}
