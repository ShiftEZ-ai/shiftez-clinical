"""app.ts behaviours run from the compiled app.js in Node: app.js boots the whole app on import, so (as in
test_perf_frontend_audit.py) the functions under test are cut from the compiled source and run with controlled I/O.
A cut that no longer matches fails loudly (the regex finds nothing), never silently."""
import json
import re
import subprocess


def cut(source, pattern):
    found = re.findall(pattern, source, re.S)
    assert len(found) == 1, f'{pattern!r} matched {len(found)} times in the compiled app.js'
    return found[0]


def node(script):
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stdout + out.stderr
    return json.loads(out.stdout)


def test_model_proven_stop_hides_continue_search(client_modules):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    resumable = cut(source, r'const resumable\s*=\s*(.*?);')
    got = node(r"""
const check=reason=>{
  const editable=true,cpSat=true,currentDraft=true,stale=false,discarded=false,canContinue=false;
  const latest={currency:'',status:'succeeded'},result={resume_available:true,status:'candidate_feasible',search_stop:{reason}};
  return (""" + resumable + r""");
};
console.log(JSON.stringify({optimal:check('optimal'),model:check('model_proven'),plateau:check('plateau')}));""")
    assert got == {'optimal': False, 'model': False, 'plateau': True}


def test_your_own_stop_or_add_time_on_a_queued_run_is_read_back_within_3_s(client_modules):
    """Restore ee3c5da9f (schedulingPanel): a queued run is read back 3 → 10 → 30 s apart. Your own Stop and keep best
    or Add time restarts that back-off, so its outcome is read 3 s later, not after a 30 s wait. The poll step (the
    queued count and the delay it chooses) and both confirm handlers are the compiled app's own; time never passes."""
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    count = cut(source, r"\n\s*(queuedPolls = running\?\.status === 'queued' \? queuedPolls \+ 1 : 0;)")
    delay = cut(source, r"\n\s*(const watched = \(!target\.hidden \|\| searching\) && !document\.hidden, delay = .*?;)\n")
    stop = cut(source, r"(async \(reason\) => \{ await command\('job\.stop_keep_best'.*?await load\(\); \})")
    add = cut(source, r"(async \(reason\) => \{ await command\('job\.add_time'.*?await load\(\); \})")
    got = node(r"""
let queuedPolls=0;const target={hidden:false},document={hidden:false},searching=false,streamSeen=0,more={value:()=>15};
const running={id:'run-1',status:'queued'},sent=[],delays=[];
const command=async kind=>{sent.push(kind);};
// One poll of the open panel, as schedulingPanel's load: count the queued reads, then choose when to read next.
async function load(){""" + count + delay + r"""delays.push(delay);}
const stop=""" + stop + ",addTime=" + add + r""";
await load();await load();await load();       // queued for three reads: backed off to 30 s
await stop('Fictional reason');              // your own Stop and keep best
await load();await load();await load();
await addTime('Fictional reason');           // your own Add time
console.log(JSON.stringify({sent,delays}));""")
    assert got['sent'] == ['job.stop_keep_best', 'job.add_time']
    assert got['delays'] == [3000, 10000, 30000, 3000, 10000, 30000, 30000, 3000], got


def test_a_live_update_waits_while_your_command_is_on_its_way_then_the_owed_redraw_runs(client_modules):
    """Restore df743f4d7 (readerBusy, onPulse): a live update that would redraw the page arrives while one of your own
    commands is still on its way; the page is not redrawn under it (an approval panel would close before the activation
    commits). The update is kept, and the redraw it owes runs once the command has finished: the 10 s live tick
    replays it. onPulse, readerBusy, deferPulse/mergePulse and the tick are the compiled app's own."""
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    merge = cut(source, r'\n(function mergePulse\(older, newer\) \{.*?\n\})\n')
    defer = cut(source, r'\n(const deferPulse = \(pulse\) => \{.*?\};)\n')
    busy = cut(source, r'\n(const readerBusy = \(\) => .*?;)\n')
    on_pulse = cut(source, r'\n(async function onPulse\(given\) \{.*?\n\})\n')
    tick = cut(source, r'\n(window\.setInterval\(\(\) => \{\n\s*if \(!me \|\| document\.visibilityState.*?\}, 10_000\);)\n')
    got = node(f"const {{withoutOwnCheck}}=await import({json.dumps((client_modules / 'edit_delta.js').as_uri())});" + r"""
let me={identity_id:'i',roles:['coordinator'],organisation:{input_revision:1,availability_revision:1}};
let polling=false,deferredPulse=null,seenActivity=5,catalogues={},inboxPulseKey='',inboxPending=null,burst=null,burstTimer=0,lastPoll=0,ownSaves={activity:NaN,n:0},plannerMemo=null;
const view='people',routeParts=[],opsBusy=false,connected=true,redrawOwed=false,refreshSkipped=false,workspaceId='',
  autoChecking=false,checkJob=null,autoCheckKey='',renderSerial=0,stream={},streamResume=0,streamSeen=Date.now(),
  checkSettled=new Map(),BADGE_SOURCES={},paths={people:{live:['people']},home:{live:[]},schedule:{live:['workspace']}};
let saving=true;const savingNow=()=>saving,editingNow=()=>false;  // your command is on its way; nothing else is open
const document={visibilityState:'visible',querySelector:()=>null,querySelectorAll:()=>[]};
const refreshes=[],refresh=async(_,scopes)=>{refreshes.push([...scopes]);};
const nothing=()=>{},sessionKey=()=>1,resuming=()=>false,sessionChanged=()=>false,background=async()=>{throw new Error('no read expected');};
const noticeNewVersion=nothing,inboxPulse=nothing,remoteEdit=nothing,applyRemoteEdit=nothing,forgetSchedule=nothing,
  loadNavBadges=nothing,boot=nothing,showConnection=nothing,flushPulse=nothing,redraw=nothing,renderPage=nothing,listen=nothing,
  applyDriftInputs=nothing;
let liveTick;const window={setInterval:fn=>{liveTick=fn;}};
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
function liveNeeds(c){return paths[view].live.includes(c.scope);}
""" + merge + defer + busy + on_pulse + tick + r"""
// Someone else's change to People (the page on screen) arrives while your command is on its way.
await onPulse({activity:5,input_revision:2,availability_revision:1,roles:['coordinator'],changes:[{scope:'people',id:null}]});
const during=refreshes.length,kept=!!deferredPulse;
liveTick();await settle();const stillSaving=refreshes.length;   // the next tick, the command still on its way
saving=false;liveTick();await settle();                          // the command has finished: the next tick
console.log(JSON.stringify({during,kept,stillSaving,after:refreshes,left:deferredPulse}));""")
    assert (got['during'], got['kept'], got['stillSaving']) == (0, True, 0), got  # no redraw under the command
    assert got['after'] == [['people']] and got['left'] is None, got             # the owed redraw ran, once


def test_save_refresh_does_not_rebuild_the_page_opened_during_its_reads(client_modules):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    refresh = cut(source, r'\n(async function refreshOnce\(stale.*?\n\})\n')
    got = node(r"""
let me={}, page='schedule/', release, refreshForeground=page, refreshScopes, refreshChanges=[],
  accessRead, inPlace=true, refreshSkipped, refreshFailed, redrawOwed=false, foregroundRender=false;
const savedKinds=new Set(['workspace.publish']), routeParts=[], draws=[];
const pageStep=()=>page, sessionKey=()=>1, view='insights';
const forgetReads=()=>{}, readAfresh=()=>{}, applyDisplay=()=>{}, showConnection=()=>{}, loadNavBadges=()=>{};
const ME_SCOPES=[], LIST_SCOPES=[], loadCatalogues=async()=>{}, readerBusy=()=>false;
const background=()=>new Promise(resolve=>{release=()=>resolve(me);});
const renderPage=async()=>draws.push([page,foregroundRender]);
const sessionChanged=()=>false, document={querySelector:()=>null};
const notify=message=>{throw new Error(message);};
""" + refresh + r"""
const pending=refreshOnce();
page='insights/workload';
release();
await pending;
refreshForeground=page;
const samePage=refreshOnce();
release();
await samePage;
console.log(JSON.stringify(draws));""")
    assert got == [['insights/workload', True]]


def test_schedule_does_not_choose_its_opening_rota_from_a_kept_list(client_modules):
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    read_list = cut(source, r'\n\s*(const readList = .*?\n\s*const page = .*?;)\n')
    got = node(r"""
const planner=true, memo=null, listAhead=null, workspaceListOffset=0;
const get=async()=>({workspaces:[{status:'draft'}]});
const background=async()=>({workspaces:[{status:'submitted'}]});
async function read(workspaceId='',draftNone=false,sourcePublication=''){
""" + read_list + r"""
  return page.workspaces[0].status;
}
console.log(JSON.stringify([await read(),await read('selected'),await read('',true),await read('',false,'version')]));
""")
    assert got == ['submitted', 'draft', 'draft', 'draft']


def test_a_failed_refresh_toasts_only_when_the_page_shows_nothing_of_its_own(client_modules):
    """A refresh failing over rows keeps their stale banner, over a page that did not load its own Retry: no toast,
    so a pulse after pulse does not repeat one. Only a page with neither says the refresh failed."""
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    refresh = cut(source, r'\n(async function refreshOnce\(stale.*?\n\})\n')
    got = node(r"""
let me={}, refreshForeground=null, refreshScopes, refreshChanges=[], accessRead, inPlace=true, refreshSkipped,
  refreshFailed=false, redrawOwed=false, foregroundRender=false, pageFailed=false, banner=null;
const savedKinds=new Set(), routeParts=[], toasts=[];
const pageStep=()=>'people/', sessionKey=()=>1, view='people';
const forgetReads=()=>{}, readAfresh=()=>{}, applyDisplay=()=>{}, showConnection=()=>{}, loadNavBadges=()=>{};
const ME_SCOPES=[], LIST_SCOPES=[], loadCatalogues=async()=>{}, readerBusy=()=>false, renderPage=async()=>{};
const background=async()=>{throw new Error('Connection lost.');};
const sessionChanged=()=>false, document={querySelector:()=>banner};
const notify=message=>toasts.push(message);
""" + refresh + r"""
const runs=[];
for(const [failed,shown] of [[false,null],[false,{}],[true,null],[true,null]]){
  pageFailed=failed;banner=shown;toasts.length=0;await refreshOnce();runs.push([toasts.length,refreshFailed]);}
console.log(JSON.stringify(runs));""")
    assert got == [[1, True], [0, True], [0, True], [0, True]]


def test_a_start_whose_lists_fail_says_the_page_did_not_load(client_modules):
    """Signed in with the frame drawn, People, Requests or Rules whose lists fail at start-up say only that the page did
    not load, in the page with Retry; before the frame (the account unread) and on the Schedule, the start-up screen."""
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    start = cut(source, r'\n(function startFailed\(.*?\n\})\n')
    got = node(r"""
let view='people', pageFailed=false, shown=null;
const calls=[], boot=async()=>calls.push('boot');
const bootFailure=(e,retry,signedIn)=>calls.push(['start-up screen',signedIn]);
const el=(tag,attrs,...children)=>children[0];
const readFailed=(name,e,retry)=>({name,message:e.message,retry:typeof retry});
const failedPageName=()=>view==='people'?'People':'the schedule';
const document={querySelector:()=>({replaceChildren:node=>{shown=node;}})};
""" + start + r"""
const e=new Error('Connection lost.'),runs=[];
for(const [page,framed] of [['people',false],['people',true],['schedule',true]]){
  view=page;pageFailed=false;shown=null;calls.length=0;startFailed(e,framed);runs.push([calls.slice(),shown,pageFailed]);}
console.log(JSON.stringify(runs));""")
    assert got == [
        [[['start-up screen', True]], None, False],
        [[], {'name': 'People', 'message': 'Connection lost.', 'retry': 'function'}, True],
        [[['start-up screen', True]], None, False],
    ]
