"""PERF async fill-options (46f346af7, d4c1b4991, 7add70607) + LEAD-UIUX, 8 Nov 2026: candidates.ts askCandidates and
askRegistry, the one way the Fix list and the session panel ask who can work a session as a job, with its wait, POST
and release passed in (here fakes; in the app api.ts awaitJob, request and job.release). Provisional pages arrive
merged and assignable, the final envelope replaces them; stopping (another session, a close, a sign-out) releases this
consumer's token once and never sends job.cancel, so a job other tabs or people share is cancelled only by its last
consumer; the Assign is bound to the exact fingerprint. Real compiled module in Node (logic only, no DOM)."""
import json
import subprocess

SCRIPT = """const m=await import(%s);const a=JSON.parse(process.argv[1]);const log={pages:[],released:[],posted:[]};
const deps={post:async b=>{log.posted.push(b);return a.queued;},
  wait:async (job,o)=>{for(const p of a.progress){if(o.stop())return null;o.seen({...job,status:'running',progress:{result:p}});}
    return o.stop()?null:a.last;},
  release:(job,token)=>{log.released.push([job.id,token]);}};
let n=0;const stopped=()=>a.stopAfter!=null&&++n>a.stopAfter;
try{const r=await m.askCandidates(a.body||{workspace_id:'w'},deps,{onPage:rows=>log.pages.push(rows.candidates.map(c=>c.person_id)),stopped});
  log.final={provisional:r.provisional_page,people:r.candidates.map(c=>c.person_id),fingerprint:r.source_fingerprint,
    assign:m.assignFingerprint(r.candidates[0],r)};}
catch(e){log.error=e.code;}
console.log(JSON.stringify(log));"""
TOKEN = 'j1.' + 'x' * 43


def ask(client_modules, progress, last, stop_after=None, queued=None, body=None):
    script = SCRIPT % json.dumps((client_modules / 'candidates.js').as_uri())
    arg = {'queued': queued or {'job': {'id': 'j1', 'status': 'queued'}, 'source_fingerprint': 'f1', 'consumer_token': TOKEN},
           'progress': progress, 'last': last, 'stopAfter': stop_after, 'body': body}
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(arg)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def page(people, provisional):
    return {'provisional_page': provisional, 'source_fingerprint': 'f1', 'workspace_revision': 4,
            'candidates': [{'person_id': p, 'status': 'ok'} for p in people]}


def test_provisional_pages_then_the_final_envelope(client_modules):
    final = {'status': 'succeeded', 'result': page(['p1', 'p2', 'p3'], False)}
    log = ask(client_modules, [page(['p1'], True), page(['p2'], True)], final)
    assert log['pages'] == [['p1'], ['p1', 'p2']]  # each page merged with the rows before it, each row assignable
    assert log['final'] == {'provisional': False, 'people': ['p1', 'p2', 'p3'], 'fingerprint': 'f1', 'assign': 'f1'}
    assert log['released'] == []  # an answer in: nothing to release


def test_stopping_releases_its_own_token_once(client_modules):
    log = ask(client_modules, [page(['p1'], True), page(['p2'], True)], {'status': 'succeeded', 'result': page(['p1'], False)},
              stop_after=1)
    assert log['error'] == 'STOPPED' and log['released'] == [['j1', TOKEN]]  # its own share, never job.cancel


def test_a_stale_job_fails_closed_and_an_old_server_answers_at_once(client_modules):
    log = ask(client_modules, [], {'status': 'failed', 'error_code': 'STALE_INPUTS'})
    assert log['error'] == 'STALE_INPUTS' and 'final' not in log and log['released'] == []
    # A server without the async path answers with the envelope itself: taken as final.
    log = ask(client_modules, [], None, queued=page(['p9'], False))
    assert log['final']['people'] == ['p9'] and log['final']['provisional'] is False


def test_the_assign_is_bound_to_the_candidates_fingerprint(client_modules):
    log = ask(client_modules, [], {'status': 'succeeded', 'result': page(['p1'], False)})
    assert log['final']['assign'] == 'f1'  # sent as expected_fingerprint (a supervised trainee's own trial would win)


def test_assign_sends_the_checked_stamp_and_the_workspace_revision(client_modules):
    script = f"""const planner=await import({json.dumps((client_modules / 'planner_week.js').as_uri())});
globalThis.window=Object.assign(new EventTarget(),{{setTimeout,clearTimeout}});
globalThis.document={{cookie:'',querySelectorAll:()=>[],createElement:()=>({{setAttribute(){{}},
  classList:{{contains:()=>false,add(){{}},remove(){{}}}}}})}};
const sent=[];
globalThis.fetch=async(path,options)=>{{
  sent.push(JSON.parse(options.body));
  return new Response('{{"validation":{{"error_count":0}}}}',{{headers:{{'Content-Type':'application/json'}}}});
}};
const w={{id:'w1',revision:4}},shift={{id:'s1',participants:[]}};
const result={{slot:{{start:'start',end:'end'}},source_fingerprint:'legacy',assignment_stamp:'assign:base'}};
for(const candidate of [
  {{person_id:'p1'}},
  {{person_id:'p2',assignment_stamp:'assign:fresh',source_fingerprint:'fresh'}},
  {{person_id:'p3',source_fingerprint:'fresh-legacy'}},
])await planner.assignCandidate(w,shift,candidate,result);
console.log(JSON.stringify(sent.map(row=>[row.kind,row.expected_revision,row.expected_fingerprint])));"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True,
                         text=True, timeout=15)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        ['shift.update', 4, 'assign:base'],
        ['shift.update', 4, 'assign:fresh'],
        ['shift.update', 4, 'fresh-legacy'],
    ]


def test_the_panels_options_go_with_the_same_ask(client_modules):
    # The session panel's replace and chosen supervisor are fields of the one request (FillOptionsInput), on the job too;
    # its search filters the answer on the client.
    body = {'workspace_id': 'w', 'shift_id': 's', 'expected_revision': 4, 'replace_assignment_id': 'a1', 'supervisor_person_id': 'p7'}
    log = ask(client_modules, [], {'status': 'succeeded', 'result': page(['p1'], False)}, body=body)
    assert log['posted'] == [body] and log['final']['people'] == ['p1']


# A fake server for one coalesced job (7add70607): each ask gets its own consumer token; job.release removes it; only
# the last release cancels the job; `finish()` lets it succeed for whoever still waits.
REGISTRY = """const m=await import(%s);const out={};
function server(){const s={tokens:new Set(),n:0,cancelled:false,released:[],waits:[],ready:[]};
  s.started=n=>s.waits.length>=n?Promise.resolve():new Promise(done=>s.ready.push({n,done}));
  const settle=()=>{for(const w of s.waits)if(w.x.stop()||s.cancelled)w.done(null);};
  s.finish=()=>{for(const w of s.waits)w.done(w.x.stop()?null:{status:'succeeded',result:{provisional_page:false,candidates:[{person_id:'p1',status:'ok'}]}});};
  s.deps={post:async()=>{const t='shared.'+'x'.repeat(43)+(++s.n);s.tokens.add(t);return {job:{id:'shared',status:'queued'},consumer_token:t};},
    wait:(job,x)=>new Promise(done=>{s.waits.push({x,done});for(const r of s.ready)if(s.waits.length>=r.n)r.done();settle();}),
    release:(job,t)=>{s.released.push(t);s.tokens.delete(t);if(!s.tokens.size)s.cancelled=true;settle();}};
  return s;}
const settle=p=>p.then(r=>({ok:r.candidates.length}),e=>({error:e.code}));
// 1. Two consumers (the Fix list's box, the session panel): the panel closing releases only its share.
{const s=server(),asks=m.askRegistry((b,o)=>m.askCandidates(b,s.deps,o));
 const box=asks.held('fix','shared',()=>({s:1})),panel=asks.held('panel','shared',()=>({s:1}));
 const boxResult=settle(box.final),panelResult=settle(panel.final);await s.started(2);
 asks.stop('panel');out.one={cancelled:s.cancelled,released:s.released.length};s.finish();
 out.one.box=await boxResult;out.one.panel=await panelResult;}
// 2. Both stop: the last release cancels the job.
{const s=server(),asks=m.askRegistry((b,o)=>m.askCandidates(b,s.deps,o));
 const box=asks.held('fix','shared',()=>({s:1})),panel=asks.held('panel','shared',()=>({s:1}));
 const boxResult=settle(box.final),panelResult=settle(panel.final);await s.started(2);
 asks.stop('panel');asks.stop('fix');out.last={cancelled:s.cancelled,released:s.released.length};
 out.last.box=await boxResult;out.last.panel=await panelResult;}
// 3. Sign-out: every ask released and forgotten.
{const s=server(),asks=m.askRegistry((b,o)=>m.askCandidates(b,s.deps,o));
 const box=settle(asks.held('fix','shared',()=>({s:1})).final),panel=settle(asks.held('panel','shared',()=>({s:1})).final);await s.started(2);
 asks.reset();out.reset={released:s.released.length,cancelled:s.cancelled,held:[asks.has('shared')],box:await box,panel:await panel};}
// 4. Choosing another session on one surface releases the one before, once (its own stop releases nothing more).
{const s=server(),next=server(),asks=m.askRegistry((b,o)=>m.askCandidates(b,b.s===1?s.deps:next.deps,o));
 const a=settle(asks.held('fix','k1',()=>({s:1})).final);await s.started(1);
 const b=settle(asks.held('fix','k2',()=>({s:2})).final);await next.started(1);
 out.next={first:await a,released:s.released.length};next.finish();out.next.second=await b;}
console.log(JSON.stringify(out));"""


def test_consumers_release_their_share_and_the_last_release_cancels(client_modules):
    script = REGISTRY % json.dumps((client_modules / 'candidates.js').as_uri())
    out = json.loads(subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True,
                                    check=True, timeout=15).stdout)
    # One releasing never ends the other's job: the box still gets its result.
    assert out['one'] == {'cancelled': False, 'released': 1, 'box': {'ok': 1}, 'panel': {'error': 'STOPPED'}}
    # The last consumer's release cancels the job.
    assert out['last'] == {'cancelled': True, 'released': 2, 'box': {'error': 'STOPPED'}, 'panel': {'error': 'STOPPED'}}
    # Sign-out releases every share and forgets every ask.
    assert out['reset'] == {'released': 2, 'cancelled': True, 'held': [False],
                            'box': {'error': 'STOPPED'}, 'panel': {'error': 'STOPPED'}}
    # Another session on the same surface: the first is released exactly once.
    assert out['next'] == {'first': {'error': 'STOPPED'}, 'released': 1, 'second': {'ok': 1}}
