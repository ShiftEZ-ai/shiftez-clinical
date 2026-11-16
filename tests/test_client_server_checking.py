"""D-062 (maintenance): whether a rota is being checked is the server's answer (api.ts serverChecking), never a worker probe
remembered from when the page opened; the probe is asked again when a job appears (reprobeWorkers) and only decides
what to offer when no check exists. The words are the one queued rule (rule_check_api.ts). Real compiled modules in Node."""
import json
import subprocess
from pathlib import Path

from .test_client_autoschedule_live import GLOBALS

ROOT = Path(__file__).parents[1]


def run(client_modules, body):
    script = (GLOBALS + "let up=false,asked=0;globalThis.fetch=async()=>{asked++;return {ok:up};};"
              f"const a=await import({json.dumps((client_modules / 'api.js').as_uri())});"
              f"const r=await import({json.dumps((client_modules / 'rule_check_api.js').as_uri())});const out={{}};" + body +
              "console.log(JSON.stringify(out));")
    done = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_servers_answer_decides_whatever_the_probe_said(client_modules):
    out = run(client_modules,
              # The page opened with no worker: the probe said so and is remembered.
              "await a.probeWorkers();await a.probeWorkers();out.before=[a.workersKnown(),asked];"
              # A worker starts; a save's reply then carries the server's own proof.
              "up=true;const reply={validation:{scope:'command_changes'},fix_list_delta:{summary:{checking:true}}};"
              "out.stale=a.workersKnown();"
              "out.checking=[a.serverChecking(reply.validation),a.serverChecking(null,reply.fix_list_delta.summary),"
              "a.serverChecking(null,null,{status:'queued'}),a.serverChecking(null,null,{status:'running'})];"
              "out.words=[r.checkingWords('queued'),r.checkingWords('running'),r.checkingWords()];"  # no job state: no claim
              # The job is new proof: the probe is asked again, once per proof, and now finds the worker.
              "await a.reprobeWorkers('job-1');await a.reprobeWorkers('job-1');out.after=[a.workersKnown(),asked];"
              # No check and no worker: nothing says checking, and the probe's answer is what the offer goes by.
              "up=false;await a.reprobeWorkers('job-2');"
              "out.none=[a.serverChecking({scope:'full'},{checking:false},{status:'succeeded'}),a.serverChecking(),a.workersKnown()];")
    assert out['before'] == [False, 1] and out['stale'] is False  # one probe, remembered: stale once the worker starts
    assert out['checking'] == [True, True, True, True]  # each of the server's proofs, despite the stale "no worker"
    assert out['words'] == ['Waiting to check', 'Checking the rota', '']
    assert out['after'] == [True, 2]  # refreshed by the job, not kept once per page; one request per proof
    assert out['none'] == [False, False, False]


# Master, 10 Nov 2026 (after maintenance): the reply's scope is not proof a worker runs the check (any heartbeat in the last 60 s
# sends it: a push-only worker, one that just died). The status is the check JOB's own, read as awaitJob reads it.
WAIT = ("const statuses=[];globalThis.fetch=async p=>String(p).includes('/status')"
        "?new Response(JSON.stringify({job:{id:'j1',status:statuses.shift()||'queued'}}),{status:200}):{ok:up};"
        "const said=[],follow=async(...next)=>{statuses.push(...next);"
        "const wait=a.awaitJob({id:'j1',status:'queued'},{every:1,seen:j=>said.push(r.checkingWords(j.status))});"
        # The worker's live announcement wakes the wait (as the stream does), once per status read.
        "for(let n=0;n<next.length;n++){await new Promise(d=>setTimeout(d,5));window.dispatchEvent(new CustomEvent('jobs-changed',{detail:['j1']}));}"
        "return (await wait).status;};")


def test_a_worker_started_after_the_page_opened_is_said_from_the_job_it_queued(client_modules):
    out = run(client_modules, WAIT +
              "await a.probeWorkers();up=true;out.probe=a.workersKnown();"
              "out.ended=await follow('queued','running','succeeded');out.said=said;")
    assert out['probe'] is False  # the page still remembers "no worker"
    # (a) queued → waiting words; (d) running → checking words; a finished check says nothing.
    assert out['said'] == ['Waiting to check', 'Checking the rota', ''] and out['ended'] == 'succeeded'


# maintenance: the bound is the server's heartbeat window, sent on the rota's summary with the workers' liveness; never a copy.
WORKERS = ("const live={worker_liveness:'running',worker_heartbeat_max_age_seconds:180},"
           "down={worker_liveness:'not_responding',worker_heartbeat_max_age_seconds:180},"
           "never={worker_liveness:'never_seen',worker_heartbeat_max_age_seconds:180},"
           "slow={worker_liveness:'running',worker_heartbeat_max_age_seconds:600};")


def test_a_queued_check_nobody_starts_reads_as_not_started_with_check_again(client_modules):
    out = run(client_modules, WORKERS +
              "const at=r.checkBoundMs(live);out.bound=[at,r.checkBoundMs(slow),r.checkBoundMs(down),r.checkBoundMs(never),r.checkBoundMs()];"
              "out.before=[r.checkingWords('queued',at-1,live),r.checkUnfinished('queued',at-1,live)];"
              "out.after=[r.checkingWords('queued',at,live),r.checkUnfinished('queued',at,live)];"
              # No worker answers: nothing can start it, so it has not started at once.
              "out.down=[down,never].map(w=>[r.checkingWords('queued',0,w),r.checkUnfinished('queued',0,w)]);"
              # The workers not known (Home, a summary): a queued check waits, however long.
              "out.unknown=[r.checkingWords('queued',at*100),r.checkUnfinished('queued',at*100)];"
              # The bound is about the queue only: a check that is running is never "not picked up".
              "out.running=[r.checkingWords('running',at*10,down),r.checkUnfinished('running',at*10,down)];out.again=r.CHECK_AGAIN;")
    # The summary's own window (seconds) is the bound; another setting moves it, nothing is copied in the client.
    assert out['bound'] == [180_000, 600_000, 0, 0, None]  # JSON has no Infinity: no bound while the workers are unknown
    assert out['before'] == ['Waiting to check', False]
    # DL-018: the person's view ("hasn't started"), not the system's ("not picked up"); no spinner, no "Checking"
    assert out['after'] == ["Check hasn't started", True] and out['again'] == 'Check again'
    assert out['down'] == [["Check hasn't started", True]] * 2
    assert out['unknown'] == ['Waiting to check', False]
    assert out['running'] == ['Checking the rota', False]


def test_a_failed_or_refused_check_stopped(client_modules):
    out = run(client_modules, WAIT +
              "out.ended=await follow('running','failed');out.said=said;"
              "out.unfinished=[r.checkUnfinished('failed'),r.checkUnfinished('succeeded'),r.checkUnfinished('cancelled')];"
              # A cancelled job has no words of its own: its caller decides whether it stopped (then worded as 'failed').
              "out.cancelled=r.checkingWords('cancelled');"
              # A refusal for the person's own access offers no Check again: the command's 403 or the worker's codes.
              "out.refused=[r.checkRefused('FORBIDDEN',403),r.checkRefused('ACCESS_REVOKED',409),r.checkRefused('FORBIDDEN',409),"
              "r.checkRefused('STALE_INPUTS',409),r.checkRefused('INTERNAL',500),r.checkRefused()];")
    # (c) a job that fails at lease or run (a refusal is a failed job with its error_code). DL-018: "Check stopped",
    # the verb of the line above the grid ("The check stopped after m:ss").
    assert out['said'] == ['Checking the rota', 'Check stopped'] and out['ended'] == 'failed'
    assert out['unfinished'] == [True, False, False]  # a person's own Cancel keeps its "Not checked"
    assert out['cancelled'] == ''
    assert out['refused'] == [True, True, True, False, False, False]


def test_a_stopped_check_job_is_worded_by_its_code_from_the_one_map(client_modules):
    """DL-018, one state one verb: the line above the grid says "The check stopped", with the code's existing words only
    when they add a fact the Check again beside it does not (Rule 1 on DL-018). The rule review's row (rule_review.ts)
    reads the same map and keeps its own words: the phrase is typed once."""
    out = run(client_modules,
              "out.access=[r.checkStoppedWords('ACCESS_REVOKED'),r.checkStoppedWords('FORBIDDEN'),r.checkStoppedWords('ANYTHING',403)];"
              "out.lease=r.checkStoppedWords('WORKER_LEASE_EXPIRED');"
              "out.unknown=[r.checkStoppedWords('CHECK_FAILED'),r.checkStoppedWords('INTERNAL',500),r.checkStoppedWords(),r.checkStoppedWords('constructor')];"
              "out.review=[r.checkCodeLabel('ACCESS_REVOKED'),r.checkCodeLabel('WORKER_LEASE_EXPIRED'),r.checkCodeLabel('FORBIDDEN'),r.checkCodeLabel()];")
    assert out['access'] == ['The check stopped · your role cannot run it'] * 3
    assert out['lease'] == 'The check stopped.'  # "run it again" is the Check again button beside it
    assert out['unknown'] == ['The check stopped.'] * 4  # never "could not finish": one verb for one state
    # The review's rows keep their words, and its reason is the banner's (one map, not two phrases).
    assert out['review'] == ['Check failed · your role cannot run it', 'Check interrupted · run it again', '', '']
    assert out['access'][0].endswith(out['review'][0].split(' · ')[1])
    sources = ''.join((ROOT / 'apps/web/src' / name).read_text(encoding='utf-8') for name in ('app.ts', 'rule_review.ts', 'rule_catalogue.ts'))
    assert 'your role cannot' not in sources and 'The check could not finish' not in sources


def test_the_scope_alone_claims_no_running_check(client_modules):
    out = run(client_modules,
              # (e) the scope says a job may exist to look at; with no job found there are no words to show.
              "const down={worker_liveness:'not_responding',worker_heartbeat_max_age_seconds:180};"
              "out.exists=a.serverChecking({scope:'command_changes'});out.words=r.checkingWords(undefined,1e9,down);"
              "out.unfinished=r.checkUnfinished(undefined,1e9,down);"
              # The rule review panels' one queued word is unchanged.
              "out.waiting=[r.waitingWord('queued'),r.waitingWord('running'),r.waitingWord()];")
    assert out['exists'] is True and out['words'] == '' and out['unfinished'] is False
    assert out['waiting'] == ['Waiting to check', '', '']


def test_the_page_takes_over_only_a_check_still_queued_at_the_bound_and_only_once(client_modules):
    """DL-018 + Master 10 Nov 2026: the one automatic fallback's decision (rule_check_api.ts checkTakeover). maintenance: the
    bound is the summary's heartbeat window while a worker runs; with no worker answering it is taken over at once."""
    out = run(client_modules, WORKERS +
              "const at=r.checkBoundMs(live);"
              "out.due=[r.checkTakeover('queued',at,false,live),r.checkTakeover('queued',at*3,false,live)];"
              "out.early=r.checkTakeover('queued',at-1,false,live);"
              # not_responding and never_seen: no wait for the window.
              "out.now=[r.checkTakeover('queued',0,false,down),r.checkTakeover('queued',0,false,never)];"
              # The workers not known (the summary read failed or is out): no take-over, it waits.
              "out.unknown=[r.checkTakeover('queued',at*100,false),r.checkTakeover('queued',at*100,false,null)];"
              # (a) one attempt: a job (or rota version) already taken over is never taken over again.
              "out.again=[r.checkTakeover('queued',at*3,true,live),r.checkTakeover('queued',0,true,down)];"
              # (c) a check that is running is being served, however long it runs; one that ended has nothing to take over.
              "out.served=['running','failed','succeeded','cancelled',undefined].map(s=>r.checkTakeover(s,at*10,false,down));"
              # (e) the cancel's audited reason is fixed and recognisable; the words after a failed take-over.
              "out.reason=r.CHECK_TAKEN_OVER;out.failed=r.CHECK_NOT_STARTED+' · '+r.CHECK_AGAIN;"
              "out.same=r.checkingWords('queued',0,down)===r.CHECK_NOT_STARTED;")
    assert out['due'] == [True, True] and out['early'] is False
    assert out['now'] == [True, True]
    assert out['unknown'] == [False, False]
    assert out['again'] == [False, False]
    assert out['served'] == [False] * 5
    assert out['reason'] == 'Check not started in time: run by the page'
    assert out['failed'] == "Check hasn't started · Check again" and out['same'] is True  # DL-018, said once


def test_a_check_set_aside_elsewhere_while_the_rota_has_a_current_check_stopped_nothing(client_modules):
    """DL-024: a cancelled check job (another person's Cancel, or a take-over from any page on the rota) is read against
    the rota's check first; a current one shows its result: no banner, no "Check stopped" pill."""
    out = run(client_modules, "out.stopped=r.checkEndedStopped('cancelled',true);out.succeeded=r.checkEndedStopped('succeeded',true);")
    assert out['stopped'] is False and out['succeeded'] is False


def test_a_check_set_aside_elsewhere_with_no_current_check_reads_as_stopped(client_modules):
    """DL-024: no new phrase. A cancelled check job that leaves the rota unchecked is the ordinary stopped check: the
    pill reads "Check stopped · Check again" and the banner "The check stopped." (no code: nothing to add)."""
    out = run(client_modules,
              "out.stopped=[r.checkEndedStopped('cancelled',false),r.checkEndedStopped('cancelled')];"
              "out.failed=[r.checkEndedStopped('failed',false),r.checkEndedStopped('failed',true)];"
              "out.pill=r.checkingWords('failed')+' · '+r.CHECK_AGAIN;out.banner=r.checkStoppedWords();")
    assert out['stopped'] == [True, True]  # the cancel's reason is not needed: the rota's own state decides
    assert out['failed'] == [True, True]  # a failed job stopped, whatever the rota holds
    assert out['pill'] == 'Check stopped · Check again' and out['banner'] == 'The check stopped.'


def test_cancel_is_offered_only_while_the_check_is_a_job_the_server_can_stop(client_modules):
    """DL-019: the first check's line and a re-check's line beside the kept counts are one line with one decision."""
    out = run(client_modules,
              "out.job=['queued','running'].map(s=>r.checkCancellable({status:s}));"
              "out.not=[null,undefined,...['succeeded','failed','cancelled'].map(s=>({status:s}))].map(j=>r.checkCancellable(j));")
    assert out['job'] == [True, True]
    assert out['not'] == [False] * 5  # the synchronous check (no job) and a job that ended


def test_a_failed_rota_read_after_a_set_aside_check_reads_as_stopped(client_modules):
    """DL-024 (Design Lead FIX): the rota read that decides a set-aside check never leaves it in no state: a failed read
    is "not current", so the page says the check stopped and offers Check again."""
    out = run(client_modules,
              "globalThis.fetch=async p=>{p=String(p);if(p.endsWith('/w-down'))throw new TypeError('offline');"
              "if(p.endsWith('/w-500'))return new Response('{}',{status:500});"
              "return new Response(JSON.stringify({workspace:{validation_current:p.endsWith('/w-ok')}}),{status:200});};"
              "out.read=[await r.rotaCheckCurrent('w-ok'),await r.rotaCheckCurrent('w-old'),"
              "await r.rotaCheckCurrent('w-down'),await r.rotaCheckCurrent('w-500')];"
              "out.stopped=r.checkEndedStopped('cancelled',await r.rotaCheckCurrent('w-down'));")
    assert out['read'] == [True, False, False, False]
    assert out['stopped'] is True


def test_the_take_over_stops_the_job_only_if_still_queued_and_follows_one_a_worker_started(client_modules):
    """maintenance job.cancel {only_if_queued: true}: the take-over's stop is one conditional command, never a status read first.
    cancelled: false means a worker started the job meanwhile: the page follows it (its words: Checking the rota) and
    runs no check of its own (app.ts takeOver returns before workspace.validate)."""
    out = run(client_modules,
              "const sent=[];let reply;globalThis.fetch=async(p,o={})=>{sent.push([String(p),o.method||'GET',o.body?JSON.parse(o.body):null]);"
              "return new Response(JSON.stringify(reply),{status:200,headers:{'Content-Type':'application/json'}});};"
              "reply={cancelled:false,job:{id:'j1',revision:3,status:'running'}};const started=await r.takeOverJob('j1');"
              "out.started=[started.cancelled,started.job.status,r.checkingWords(started.job.status)];out.first=sent.splice(0);"
              "reply={cancelled:true,job:{id:'j2',revision:4,status:'cancelled'}};const set=await r.takeOverJob('j2');"
              "out.set=[set.cancelled,set.job.status];out.second=sent.splice(0);")
    for sent, job in ((out['first'], 'j1'), (out['second'], 'j2')):
        assert [(path, method) for path, method, _ in sent] == [('/api/commands', 'POST')]  # one command, no pre-read
        body = sent[0][2]
        assert body['kind'] == 'job.cancel' and body['target_id'] == job
        assert body['payload'] == {'reason': 'Check not started in time: run by the page', 'only_if_queued': True}
    assert out['started'] == [False, 'running', 'Checking the rota']  # followed: no second check
    assert out['set'] == [True, 'cancelled']
    source = (ROOT / 'apps/web/src/app.ts').read_text(encoding='utf-8')
    take = source[source.index('function takeOver('):source.index('/** The joined job\'s wait.')]
    # Started meanwhile: the page returns before its own check; the status pre-read is gone.
    assert take.index('if(!set.cancelled)return;') < take.index("command('workspace.validate'")
    assert "/status'" not in take


def test_home_and_the_fix_list_word_a_check_from_its_jobs_status(client_modules):
    """maintenance: the Fix list's and Home's summaries carry check_status and check_queued_at (Home's state is truthful: its
    cache key holds the department's in-flight checks). They are worded by the one rule (checkingWords); whether a check
    is claimed at all is serverChecking's alone. No workers are known there, so a queued check waits."""
    out = run(client_modules,
              "const ago=ms=>new Date(Date.now()-ms).toISOString();"
              "out.words=[r.summaryCheckWords({checking:true,check_status:'queued',check_queued_at:ago(10*60_000)}),"
              "r.summaryCheckWords({checking:true,check_status:'running',check_queued_at:ago(5_000)}),"
              "r.summaryCheckWords({checking:true,check_status:'queued',check_queued_at:null})];"
              # Not claimed: the latest job ended (its status is still sent), or no summary.
              "out.none=[r.summaryCheckWords({checking:false,check_status:'failed',check_queued_at:ago(1)}),"
              "r.summaryCheckWords({checking:false,check_status:'succeeded'}),r.summaryCheckWords(null),r.summaryCheckWords()];")
    assert out['words'] == ['Waiting to check', 'Checking the rota', 'Waiting to check']
    assert out['none'] == ['', '', '', '']
    src = ROOT / 'apps/web/src'
    gate = (src / 'fix_list.ts').read_text(encoding='utf-8')
    home = (src / 'home.ts').read_text(encoding='utf-8')
    # The Fix list gate (page, panel, run card) and Home's draft row read the one wording; no second rule.
    assert "s.checking_words||summaryCheckWords(s)||" in gate
    assert "summaryCheckWords(fix)||(i.checked===false||unchecked?'Needs a check':'')" in home
    for text in (gate, home):
        assert '.check_status' not in text and 'Waiting to check' not in text  # read nowhere else


def test_the_footer_check_line_always_names_its_operation(client_modules):
    """D-085: the footer's check line (app.ts checkSaid) is never blank and its Cancel is never "Cancel: " with nothing
    after it, whatever status the check's job holds while the page still waits for its results."""
    import typing
    from packages.contracts.schemas import JobStatus
    statuses = [*typing.get_args(JobStatus), None]
    out = run(client_modules, f"out.said={json.dumps(statuses)}.map(s=>r.checkingWords(s)||r.checkingWords('running'));")
    assert all(out['said']), dict(zip(map(str, statuses), out['said']))
    app = (ROOT / 'apps/web/src/app.ts').read_text(encoding='utf-8')
    assert "const checkSaid=()=>checkingWords(checkJob?checkJob.status:checkSync?'running':'queued')||checkingWords('running');" in app
