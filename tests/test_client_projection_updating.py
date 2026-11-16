"""maintenance: a read whose projection is being rebuilt (the rule catalogue after an approval) answers 202
{state:'updating'}. api.ts never treats it as data: the read stays pending, waits on the job (live jobs change, then
its lean status) or, without a job, on the live catalogue change / Retry-After, then reads once and resolves with the
200. No older kept answer stands in meanwhile, and concurrent readers share one wait. The real compiled api.ts in
Node, fetch and timers stubbed."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS

UPDATING = json.dumps({'state': 'updating', 'projection': 'rule_catalogue', 'stamp': 's2', 'retry_after_seconds': 2})


def run(client_modules, body):
    api = (client_modules/'api.js').as_uri()
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        # Timers the test fires by hand: no real waits.
        "const timers=[];globalThis.setTimeout=globalThis.window.setTimeout=(f,ms)=>{timers.push({f,ms});return timers.length;};"
        "globalThis.clearTimeout=globalThis.window.clearTimeout=id=>{if(timers[id-1])timers[id-1].f=null;};"
        "const fire=()=>{const due=timers.splice(0);due.forEach(t=>t.f?.());return due.filter(t=>t.f).map(t=>t.ms);};"
        "const tick=()=>new Promise(r=>setImmediate(r));const settle=async()=>{for(let i=0;i<20;i++)await tick();};"
        f"const UPDATING={json.dumps(UPDATING)};"
        "const sent=[];let replies=[];globalThis.fetch=async p=>{sent.push(p);const r=replies.shift();"
        "return typeof r==='function'?r(p):r;};"
        "const busy=(job)=>new Response(JSON.stringify({...JSON.parse(UPDATING),job_id:job}),{status:202,headers:{'Retry-After':'2'}});"
        "const ok=(data,tag)=>new Response(JSON.stringify(data),{status:200,headers:tag?{ETag:tag}:{}});"
        f"const api=await import({json.dumps(api)});const out={{}};"
        + body + "console.log(JSON.stringify(out));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_updating_with_a_job_waits_for_the_job_then_reads_once(client_modules):
    got = run(client_modules,
        "const C='/api/rules/catalogue?version=2';"
        "replies=[busy('j1'),ok({job:{id:'j1',status:'succeeded'}}),ok({rules:2},'\"b\"')];"
        "let resolved=null;const p=api.request(C).then(d=>{resolved=d;});await settle();"
        "out.pendingAfter202=resolved===null;out.readsBeforeJob=sent.length;"
        # The live jobs change wakes the wait at once: the lean status, then one re-read.
        "dispatchEvent(new CustomEvent('jobs-changed',{detail:['j1']}));await p;"
        "out.sent=sent;out.data=resolved;")
    assert got == {'pendingAfter202': True, 'readsBeforeJob': 1,
                   'sent': ['/api/rules/catalogue?version=2', '/api/jobs/j1/status', '/api/rules/catalogue?version=2'],
                   'data': {'rules': 2}}


def test_updating_without_a_job_waits_on_the_catalogue_change_or_retry_after(client_modules):
    got = run(client_modules,
        "const C='/api/rules/catalogue';"
        # Retry-After: no re-read before it; the waits back off (2 s, 2 s, 4 s …), never faster than 2 s.
        "replies=[busy(null),busy(null),busy(null),ok({rules:3})];let resolved=null;"
        "const p=api.request(C).then(d=>{resolved=d;});await settle();out.first=sent.length;"
        "out.waits=[];for(let i=0;i<3;i++){out.waits.push(...fire());await settle();}await p;"
        "out.reads=sent.length;out.data=resolved;"
        # The live catalogue change ends the wait at once.
        "replies=[busy(null),ok({rules:4})];const q=api.request(C);await settle();"
        "dispatchEvent(new Event('catalogue-changed'));out.live=await q;out.liveReads=sent.length-out.reads;")
    assert got['first'] == 1
    assert got['waits'] == [2000, 2000, 4000]
    assert got['reads'] == 4 and got['data'] == {'rules': 3}
    assert got['live'] == {'rules': 4} and got['liveReads'] == 2


def test_an_old_kept_answer_is_never_returned_while_updating(client_modules):
    got = run(client_modules,
        "const C='/api/rules/catalogue?version=2',DROP={scopes:['rules','catalogue'],cap:4};"
        "const tags=[];const f0=globalThis.fetch;globalThis.fetch=(p,o)=>{tags.push(new Headers(o?.headers).get('If-None-Match'));return f0(p,o);};"
        "const r=()=>api.keptRead(C,()=>api.get(C),DROP);"
        "replies=[ok({rules:1},'\"a\"')];out.old=await r().p;"
        # Opening the page again later (keptReads, after fresh_ms): the old reply is shown at once and checked; the check
        # meets the rebuild. Then every reader waits: the keptRead answer is dropped, a page opened meanwhile is pending.
        "const now=Date.now;Date.now=()=>now()+60_000;"
        "replies=[busy(null),ok({rules:2},'\"b\"')];let redrawn=false;"
        "await api.keptReads(async()=>{await api.get(C);},()=>{redrawn=true;});await settle();"
        "const k=r();out.keptWhileUpdating=k.done?k.data:'pending';"
        "let opened=null;api.keptReads(async()=>{opened=await api.get(C);},()=>{});await settle();out.openedWhileUpdating=opened;"
        # Retry-After passes: one re-read, without the old ETag (a 304 can never bring the old rules back).
        "fire();out.kept=await k.p;await settle();out.opened=opened;out.redrawn=redrawn;out.tags=tags;out.sent=sent.length;")
    assert got['old'] == {'rules': 1}
    assert got['keptWhileUpdating'] == 'pending' and got['openedWhileUpdating'] is None
    assert got['kept'] == {'rules': 2} and got['opened'] == {'rules': 2} and got['redrawn'] is True
    assert got['tags'] == [None, '"a"', None] and got['sent'] == 3


def test_two_readers_share_one_wait(client_modules):
    got = run(client_modules,
        "const C='/api/rules/catalogue?version=2',DROP={scopes:['catalogue'],cap:4};"
        "const r=()=>api.keptRead(C,()=>api.get(C),DROP);"
        "replies=[busy('j2'),ok({job:{id:'j2',status:'succeeded'}}),ok({rules:5})];"
        "const a=r(),b=r(),c=api.get(C);await settle();out.same=a===b;"
        "dispatchEvent(new CustomEvent('jobs-changed',{detail:['j2']}));"
        "out.data=await Promise.all([a.p,b.p,c]);out.sent=sent;")
    assert got['same'] is True
    assert got['data'] == [{'rules': 5}] * 3
    assert got['sent'] == ['/api/rules/catalogue?version=2', '/api/jobs/j2/status', '/api/rules/catalogue?version=2']


def test_a_decision_meeting_the_rebuild_is_refused_never_data_nor_resent(client_modules):
    got = run(client_modules,
        "replies=[busy(null)];try{await api.request('/api/rules/catalogue/x/check',{method:'POST',body:'{}'});out.data=true;}"
        "catch(e){out.code=e.code;out.message=e.message;}out.sent=sent.length;")
    assert got == {'code': 'RULES_UPDATING', 'message': 'Rules are updating… Try again in a moment.', 'sent': 1}
