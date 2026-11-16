"""maintenance: a command whose outcome is unknown (a dropped response, a gateway's 502–504, an unreadable reply) is never
"Not saved". The real compiled api.ts in Node (no DOM): the footer hears "Checking whether it saved…" (rota-save-checking),
the same command is sent again with the SAME Idempotency-Key until a real answer, and a confirmed save is "Saved"
(rota-saved, replayed, so the page reads the rota again). A JSON refusal is "Not saved"; still unknown after the bound is
SAVE_UNCONFIRMED. No new key is ever minted for an unconfirmed body: not by a retry, not after a reload."""
import json
import subprocess
import pytest

# A fictional server: a command is committed once per Idempotency-Key and its receipt replayed (apps/api/commands.py).
# `answer(n,key)` returns how the n-th request is answered: 'drop' (committed, response lost), 'html502' (not reached),
# 'html503', 'html200' (a 200 with a proxy's page), 'hang' (no answer: committed, nothing comes back until aborted),
# 'slow' (a 200 after 12 s), 'refuse' (a JSON 422), 'stale' (a JSON 409 STALE_REVISION, nothing committed), 'busy' (the server's own 503: not saved), or 'ok'. Timers run on a virtual clock (Date.now too):
# the earliest is fired 20 ms of real time after the last one, so an answer that does come always beats its timeout.
# Backoff delays (`delays`) and command timeouts (`timeouts`) are recorded.
HARNESS = """globalThis.document={cookie:'rota_csrf=fictional',createElement:()=>({classList:{contains:()=>false,add(){},remove(){}},setAttribute(){}})};
const store=new Map();globalThis.sessionStorage={getItem:k=>store.has(k)?store.get(k):null,setItem:(k,v)=>store.set(k,String(v)),removeItem:k=>store.delete(k)};
const realSetTimeout=setTimeout,delays=[],timeouts=[],BACKOFF=[1000,2000,4000,8000,15000,30000];
let clock=1e6,seq=0,pumping=false;const timers=new Map();Date.now=()=>clock;
const pump=()=>{pumping=false;let next=null;for(const [id,t] of timers)if(!next||t.at<next[1].at)next=[id,t];if(!next)return;
  timers.delete(next[0]);clock=Math.max(clock,next[1].at);next[1].run();if(timers.size&&!pumping){pumping=true;realSetTimeout(pump,20);}};
globalThis.setTimeout=(fn,ms=0,...args)=>{if(ms>=1000)(BACKOFF.includes(ms)?delays:timeouts).push(ms);
  const id=++seq;timers.set(id,{at:clock+ms,run:()=>fn(...args)});if(!pumping){pumping=true;realSetTimeout(pump,20);}return id;};
globalThis.clearTimeout=id=>{timers.delete(id);};
const committed=new Map(),keys=[],receipts=[];let n=0;
globalThis.fetch=async(path,options)=>{
  // The receipt read (GET /api/commands/{key}): committed, or RECEIPT_NOT_FOUND. Not a send: not counted in keys.
  if(String(path).startsWith('/api/commands/')){const k=decodeURIComponent(String(path).slice(14));receipts.push(k);
    return committed.has(k)?new Response(JSON.stringify({status:'committed',result:committed.get(k)}),{headers:{'Content-Type':'application/json'}})
      :new Response(JSON.stringify({error:{code:'RECEIPT_NOT_FOUND',message:'No committed receipt was found for this operation key'}}),{status:404,headers:{'Content-Type':'application/json'}});}
  const key=new Headers(options.headers).get('Idempotency-Key');keys.push(key);const how=answer(++n,key);
  if(how==='html502'||how==='html503')return new Response('<html>Bad gateway</html>',{status:how==='html502'?502:503,headers:{'Content-Type':'text/html'}});
  if(how==='slow')await new Promise(done=>setTimeout(done,12000));  // a slow answer that succeeds (past the old 10 s alarm)
  if(how==='html200')return new Response('<html>Signed out by the proxy</html>',{status:200,headers:{'Content-Type':'text/html'}});
  if(how==='hang'){if(!committed.has(key))committed.set(key,{workspace:{id:'ws-1',revision:committed.size+2,content_revision:1}});
    return new Promise((_,fail)=>options.signal.addEventListener('abort',()=>fail(new DOMException('Aborted','AbortError'))));}
  if(how==='stale')return new Response(JSON.stringify({error:{code:'STALE_REVISION',message:'Another session changed this record; reload and reconcile'}}),{status:409,headers:{'Content-Type':'application/json'}});
  if(how==='refuse')return new Response(JSON.stringify({error:{code:'SCHEMA_INVALID',message:'Choose a person.'}}),{status:422});
  if(how==='busy')return new Response(JSON.stringify({error:{code:'CATALOGUE_BUSY',message:'The catalogue is busy. Try again.'}}),{status:503});
  if(how==='timeout503')return new Response(JSON.stringify({error:{code:'COMMAND_WRITE_TIMEOUT',message:'The system is busy, so this change was not saved. Try again.'}}),{status:503});
  if(how==='uncoded503')return new Response(JSON.stringify({error:{message:'Unavailable'}}),{status:503,headers:{'Content-Type':'application/json'}});
  if(!committed.has(key))committed.set(key,{workspace:{id:'ws-1',revision:committed.size+2,content_revision:1}});
  if(how==='drop')throw new TypeError('Response lost after the commit');
  return new Response(JSON.stringify(committed.get(key)),{headers:{'Content-Type':'application/json'}});};
const url=%s;const m=await import(url);
const events=[];globalThis.window=Object.assign(new EventTarget(),{setTimeout,clearTimeout});
for(const t of ['rota-save-checking','rota-saved','rota-save-failed'])window.addEventListener(t,e=>events.push({t,code:e.detail?.code,replayed:e.detail?.replayed}));
const assign=api=>api.command('shift.update',{assignments:['person-a']},'shift-1',3).then(r=>({ok:r}),e=>({code:e.code,message:e.message}));
"""


def run(client_modules, answer, body):
    script = f"let answer={answer};" + HARNESS % json.dumps((client_modules / 'api.js').as_uri()) + body
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_dropped_assign_response_is_checked_then_saved_with_the_same_key(client_modules):
    got = run(client_modules, "(n)=>n===1?'drop':'ok'",
              "const r=await assign(m);console.log(JSON.stringify({r,keys,writes:committed.size,events,checking:m.confirmingNow()}));")
    assert got['r'] == {'ok': {'workspace': {'id': 'ws-1', 'revision': 2, 'content_revision': 1}}}
    assert len(got['keys']) == 2 and got['keys'][0] == got['keys'][1] and got['writes'] == 1
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-saved', 'replayed': True}]
    assert got['checking'] is False


def test_a_502_or_an_unreadable_reply_is_replayed_with_the_same_key_until_saved(client_modules):
    got = run(client_modules, "(n)=>n===1?'html502':n===2?'html503':'ok'",
              "const r=await assign(m);console.log(JSON.stringify({r:!!r.ok,keys,writes:committed.size,events,delays}));")
    assert got['r'] and len(got['keys']) == 3 and len(set(got['keys'])) == 1 and got['writes'] == 1
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-saved', 'replayed': True}]
    assert got['delays'] == [1000, 2000]  # bounded backoff, never "Reload before making changes"


def test_a_json_refusal_is_not_saved_with_the_servers_message(client_modules):
    busy = run(client_modules, "(n)=>'busy'", "const r=await assign(m);console.log(JSON.stringify({r,keys,events}));")
    # A 503 the contract classes DEFINITE (UNAVAILABLE_OUTCOME) is said at once with the server's words: never replayed.
    assert busy['r'] == {'code': 'CATALOGUE_BUSY', 'message': 'The catalogue is busy. Try again.'} and len(busy['keys']) == 1
    assert [e['t'] for e in busy['events']] == ['rota-save-failed']
    got = run(client_modules, "(n)=>'refuse'",
              "const r=await assign(m);const again=await assign(m);"
              "console.log(JSON.stringify({r,keys,events,stored:sessionStorage.getItem('fmuc.pending-commands')}));")
    assert got['r'] == {'code': 'SCHEMA_INVALID', 'message': 'Choose a person.'}
    assert [e['t'] for e in got['events']] == ['rota-save-failed', 'rota-save-failed']
    assert got['events'][0]['code'] == 'SCHEMA_INVALID'
    assert got['keys'][0] != got['keys'][1]  # a real refusal releases its key: the next try is a new command
    assert got['stored'] is None


@pytest.mark.parametrize('how', ['timeout503', 'uncoded503'])
def test_an_unknown_503_is_confirmed_with_the_same_key_never_said_not_saved(client_modules, how):
    """UNAVAILABLE_OUTCOME: COMMAND_WRITE_TIMEOUT is UNKNOWN (a commit-phase failure can follow the save), as is a 503
    with no code: the same command is replayed with the same key, and the server's "not saved" sentence is never shown."""
    got = run(client_modules, f"(n)=>n===1?'{how}':'ok'",
              "const r=await assign(m);console.log(JSON.stringify({r:!!r.ok,keys,events}));")
    assert got['r'] and len(got['keys']) == 2 and len(set(got['keys'])) == 1
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-saved', 'replayed': True}]


def test_unknown_past_the_bound_is_unconfirmed_and_never_mints_a_new_key(client_modules):
    got = run(client_modules, "(n)=>n<=20?'html503':'ok'",
              "const r=await assign(m);const retry=await assign(m);"
              # A reload: a fresh api.ts in the same tab (sessionStorage kept) sends the same command with the same key.
              "const reloaded=await import(url+'?reload');const after=await assign(reloaded);"
              "console.log(JSON.stringify({r,retry:retry.code,after:!!after.ok,tries:keys.length,keys:[...new Set(keys)],events,writes:committed.size,"
              "stored:sessionStorage.getItem('fmuc.pending-commands')}));")
    assert got['r'] == {'code': 'SAVE_UNCONFIRMED',
                        'message': 'Couldn’t confirm whether it saved. Check the rota before trying again.'}
    assert got['retry'] == 'SAVE_UNCONFIRMED' and got['tries'] == 21  # each: the first send and nine replays in the window
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-save-failed', 'code': 'SAVE_UNCONFIRMED'},
                             {'t': 'rota-save-checking'}, {'t': 'rota-save-failed', 'code': 'SAVE_UNCONFIRMED'},
                             {'t': 'rota-saved', 'replayed': False}]
    assert got['after'] and got['writes'] == 1 and got['stored'] is None
    assert len(got['keys']) == 1  # one key for this body: the person's retry and the reloaded tab reused it


def test_the_unconfirmed_words_name_a_rota_only_for_a_rota_command(client_modules):
    got = run(client_modules, "(n)=>'ok'", "console.log(JSON.stringify([m.unconfirmedWords('shift.update'),m.unconfirmedWords('person.update')]));")
    assert got == ['Couldn’t confirm whether it saved. Check the rota before trying again.',
                   'Couldn’t confirm whether it saved. Check it before trying again.']


def test_a_hanging_assign_is_abandoned_at_the_timeout_then_confirmed_with_the_same_key(client_modules):
    got = run(client_modules, "(n)=>n===1?'hang':'ok'",
              "const r=await assign(m);console.log(JSON.stringify({r:!!r.ok,keys,writes:committed.size,events,timeouts,checking:m.confirmingNow()}));")
    assert got['r'] and len(got['keys']) == 2 and got['keys'][0] == got['keys'][1] and got['writes'] == 1
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-saved', 'replayed': True}]  # never "Saving…" forever
    assert got['timeouts'] == [150000, 20000] and got['checking'] is False  # the first try, then a short replay


def test_a_200_with_an_unreadable_body_is_unknown_not_saved(client_modules):
    got = run(client_modules, "(n)=>n===1?'html200':'ok'",
              "const r=await assign(m);console.log(JSON.stringify({r,keys,writes:committed.size,events}));")
    assert got['r'] == {'ok': {'workspace': {'id': 'ws-1', 'revision': 2, 'content_revision': 1}}}  # the server's answer, not the page
    assert len(got['keys']) == 2 and got['keys'][0] == got['keys'][1] and got['writes'] == 1
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-saved', 'replayed': True}]


def test_a_server_that_never_answers_is_unconfirmed_within_the_window(client_modules):
    """The first try waits COMMAND_TIMEOUT_MS (above the server's 120 s write budget); each replay REPLAY_TIMEOUT_MS; the
    whole confirmation ends CONFIRM_WINDOW_MS after the first try: never 23 minutes of "Checking whether it saved…"."""
    got = run(client_modules, "(n)=>'hang'",
              "const start=clock;const r=await assign(m);console.log(JSON.stringify({r:r.code,keys:[...new Set(keys)],sends:keys.length,"
              "events,timeouts,checking:m.confirmingNow(),took:clock-start,limits:[m.COMMAND_TIMEOUT_MS,m.REPLAY_TIMEOUT_MS,m.CONFIRM_WINDOW_MS]}));")
    first, replay, window = got['limits']
    assert (first, replay, window) == (150000, 20000, 180000)
    assert got['r'] == 'SAVE_UNCONFIRMED' and got['checking'] is False and len(got['keys']) == 1 and got['sends'] > 2
    assert got['timeouts'][0] == first and all(t <= replay for t in got['timeouts'][1:])
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-save-failed', 'code': 'SAVE_UNCONFIRMED'}]
    return locals()


@pytest.mark.wall_benchmark
def test_a_server_that_never_answers_stays_within_confirmation_window(client_modules):
    state = test_a_server_that_never_answers_is_unconfirmed_within_the_window(client_modules)
    first, _, window = state['got']['limits']
    assert state['got']['took'] <= first + window


def test_a_slow_200_is_saved_with_no_busy_or_outage_signal(client_modules):
    """maintenance: slowness that ends in success is never an alarm: no server-health change ("Server busy" was the footer's
    word for a read past 10 s), no Checking, no failure; the save state alone said Saving… meanwhile."""
    got = run(client_modules, "(n)=>'slow'",
              "const health=[];window.addEventListener('server-health',e=>health.push(e.detail));"
              "const r=await assign(m);const read=await m.request('/api/me');"
              "console.log(JSON.stringify({r:!!r.ok,read:!!read.workspace,health,now:m.serverHealth(),events,keys:keys.length}));")
    assert got['r'] and got['read'] and got['keys'] == 2  # one save, one read: nothing resent
    assert got['health'] == [] and got['now'] == 'ok'
    assert got['events'] == [{'t': 'rota-saved', 'replayed': False}]


def test_a_slow_background_read_says_updating_and_never_touches_the_connection(client_modules):
    """LEAD-UIUX (supersedes D-1): a background read (a refresh after a save, a live update) still waiting after 10 s
    says "Updating…" (api.ts updatingNow, UPDATING_WORDS), clears when its 200 lands, and is never a connection state.
    An opening page's own reads keep their skeleton's "Still loading · 0:18" instead: no "Updating…" for them."""
    got = run(client_modules, "(n)=>'slow'",
              "const health=[],updating=[];window.addEventListener('server-health',e=>health.push(e.detail));"
              "window.addEventListener('reads-updating',e=>updating.push(e.detail));"
              "let mid=null;setTimeout(()=>{mid=m.updatingNow();},11000);"
              "const read=await m.background('/api/rota-refresh');const after=m.updatingNow();"
              "m.openingPage(true);let opening=null;setTimeout(()=>{opening=m.updatingNow();},11000);"
              "await m.get('/api/page-open');m.openingPage(false);"
              "console.log(JSON.stringify({read:!!read.workspace,mid,after,opening,updating,health,now:m.serverHealth(),words:m.UPDATING_WORDS}));")
    assert got['read'] and got['words'] == 'Updating…'
    assert got['mid'] is True and got['after'] is False and got['updating'] == [True, False]  # shown at 10 s, gone on the 200
    assert got['opening'] is False  # the opening page's read: its own skeleton speaks
    assert got['health'] == [] and got['now'] == 'ok'


def test_updating_never_repeats_what_the_footer_already_says(client_modules):
    """Never what is on screen already: a rota check's status poll is said by "Checking the rota · m:ss", and a read
    slow while a save is on its way is under "Saving…": neither says "Updating…"."""
    got = run(client_modules, "(n)=>'slow'",
              "const footer=()=>{const save=m.confirmingNow()||m.ownSavingNow();return {save:save?m.savingWords():'',updating:m.updatingNow()};};"
              "const states=[];for(const t of ['reads-updating','rota-saving','rota-save-checking','rota-save-settled','rota-saved'])"
              "window.addEventListener(t,()=>states.push({t,...footer()}));"
              "let poll=null;setTimeout(()=>{poll=m.updatingNow();},11000);await m.background('/api/jobs/job-1/status');"
              "let during=null;setTimeout(()=>{during=m.updatingNow();},11000);"
              "await Promise.all([assign(m),m.background('/api/rota-refresh')]);"
              "console.log(JSON.stringify({poll,during,states,after:m.updatingNow()}));")
    assert got['poll'] is False and got['during'] is False and got['after'] is False
    assert got['states'] and all(not sample['save'] or not sample['updating'] for sample in got['states'])


def test_a_background_check_job_is_not_the_persons_save(client_modules):
    """maintenance: an effect check a review panel starts (job.create kind feasibility) or a rota check (validation) is not
    "Saving…": only the person's own change (an Assign) drives the save state."""
    got = run(client_modules, "(n)=>'slow'",
              "const own=[];const at5=()=>new Promise(r=>setTimeout(()=>{own.push(m.ownSavingNow());r();},5000));"
              "const check=m.command('job.create',{workspace_id:'ws-1',kind:'feasibility',rule_changes:[{policy_id:'p-1',expected_revision:2}]},undefined,3);"
              "await at5();await check;"
              "const rota=m.command('job.create',{workspace_id:'ws-1',kind:'validation',automatic:true},undefined,3);await at5();await rota;"
              "const mine=assign(m);await at5();await mine;"
              # An auto-schedule run and a job's release are not saves; applying the run's proposal is.
              "const run=m.command('job.create',{workspace_id:'ws-1',kind:'generation'},undefined,3);await at5();await run;"
              "const release=m.command('job.release',{},'job-1');await at5();await release;"
              "const apply=m.command('generation.apply',{reason:'Apply'},'job-1',3);await at5();await apply;"
              "console.log(JSON.stringify({own,saved:events.filter(e=>e.t==='rota-saved').length,"
              "auto:[m.automatic('job.create',{kind:'feasibility'}),m.automatic('job.create',{kind:'validation'}),m.automatic('shift.update',{}),"
              "m.automatic('job.create',{kind:'generation'}),m.automatic('job.cancel',{}),m.automatic('job.attach',{}),m.automatic('generation.apply',{}),m.automatic('workspace.baseline.refresh.preview',{})]}));")
    assert got['own'] == [False, False, True, False, False, True]  # "Saving…" only for the Assign and the Apply
    assert got['auto'] == [True, True, False, True, True, True, False, True] and got['saved'] == 6


def test_a_replay_refused_stale_after_the_first_try_committed_is_saved_from_its_receipt(client_modules):
    """D-139: the first try committed late (no answer by the timeout); its replay was refused STALE_REVISION (the rota had
    moved on, by that very commit or an Undo after it). The receipt is read and says committed: saved (Undone, Session
    added), never "Not saved · Another session changed this record"."""
    got = run(client_modules, "(n)=>n===1?'hang':'stale'",
              "const r=await assign(m);console.log(JSON.stringify({r,keys,receipts,writes:committed.size,events,checking:m.confirmingNow(),"
              "stored:sessionStorage.getItem('fmuc.pending-commands')}));")
    assert got['r'] == {'ok': {'workspace': {'id': 'ws-1', 'revision': 2, 'content_revision': 1}}}
    assert len(got['keys']) == 2 and len(set(got['keys'])) == 1 and got['receipts'] == got['keys'][:1] and got['writes'] == 1
    assert got['events'] == [{'t': 'rota-save-checking'}, {'t': 'rota-saved', 'replayed': True}]
    assert got['checking'] is False and got['stored'] is None


def test_a_replay_refused_with_no_receipt_is_not_saved_once_and_never_blocks_the_next_save(client_modules):
    """D-139: nothing committed (no receipt): the refusal is said once and its key released; it is never sent again, and
    a save on another rota goes through at once with its own key."""
    got = run(client_modules, "(n)=>n===1?'html502':n===2?'stale':'ok'",
              "const r=await assign(m);"
              "const other=await m.command('shift.create',{workspace_id:'ws-2'},'ws-2',1).then(x=>({ok:!!x}),e=>({code:e.code}));"
              "console.log(JSON.stringify({r:r.code,other,keys,receipts,events,stored:sessionStorage.getItem('fmuc.pending-commands')}));")
    assert got['r'] == 'STALE_REVISION' and got['other'] == {'ok': True}
    assert len(got['keys']) == 3 and got['keys'][0] == got['keys'][1] != got['keys'][2]  # sent, replayed once, then the other save
    assert got['receipts'] == got['keys'][:1]
    assert [e['t'] for e in got['events']] == ['rota-save-checking', 'rota-save-failed', 'rota-saved']
    assert got['events'][1]['code'] == 'STALE_REVISION' and got['stored'] is None


def test_a_first_try_refused_stale_reads_no_receipt(client_modules):
    """Only a replay can have been overtaken by its own commit: a first try's refusal is the answer, nothing is read."""
    got = run(client_modules, "(n)=>'stale'", "const r=await assign(m);console.log(JSON.stringify({r:r.code,keys,receipts}));")
    assert got['r'] == 'STALE_REVISION' and len(got['keys']) == 1 and got['receipts'] == []


def test_a_reload_sends_nothing_by_itself(client_modules):
    """D-139: the tab keeps only a body digest → key map (sessionStorage), never a command: loading the page with keys kept
    from an unconfirmed save (an older build's or another rota's) sends nothing; only the same command, pressed again,
    reuses its key."""
    got = run(client_modules, "(n)=>'ok'",
              "sessionStorage.setItem('fmuc.pending-commands',JSON.stringify([['0'.repeat(64),'kept-key-from-an-older-build']]));"
              "const reloaded=await import(url+'?reload');const sentOnLoad=keys.length;"
              "const r=await assign(reloaded);console.log(JSON.stringify({r:!!r.ok,sentOnLoad,keys,receipts}));")
    assert got['sentOnLoad'] == 0 and got['r'] and len(got['keys']) == 1 and got['keys'][0] != 'kept-key-from-an-older-build' and got['receipts'] == []
