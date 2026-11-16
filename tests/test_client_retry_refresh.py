"""UX-RETRY-ROOT: a Retry is never dead behind a hung refresh. Run as the real compiled loading.ts `flight` (the app
refresh's single flight, app.ts refresh/retryRefresh) in Node: a pulse joins the run on its way; a Retry abandons a hung
run and starts exactly one new one; a second Retry while that one waits does not stack; a Retry past RETRY_AFTER replaces
a hung Retry too, and the abandoned runs are told they are stale."""
import json
import subprocess


def test_retry_starts_exactly_one_new_run_behind_a_hung_refresh_and_pulses_still_join(client_modules):
    script = f"""let t=0;Object.defineProperty(globalThis,'performance',{{value:{{now:()=>t}},configurable:true}});
// The Retry button's bound (RETRY_AFTER) is held here and fired by hand: the test does not wait 15 s.
const long=[],realTimeout=setTimeout;globalThis.setTimeout=(f,ms)=>ms>=1000?long.push(f):realTimeout(f,ms);
const {{flight,RETRY_AFTER}}=await import({json.dumps((client_modules/'loading.js').as_uri())});
const runs=[];let answer='hang';
// Each run is one refresh: it sends one request; 'hang' never answers.
const f=flight(stale=>{{const r={{stale}};runs.push(r);
  return answer==='hang'?new Promise(()=>{{}}):new Promise(done=>{{r.done=done;setTimeout(done,0);}});}});
const out={{}};const tick=()=>new Promise(r=>setTimeout(r,5));
const first=f.join();await tick();out.first=runs.length;                     // the refresh that hangs
const pulse=f.join();await tick();out.pulseJoins=pulse===first&&runs.length===1;  // a pulse joins, sends nothing
const retry=f.retry();await tick();out.retry=runs.length;out.firstStale=runs[0].stale();  // exactly one new run
f.retry();await tick();out.secondRetry=runs.length;                          // waits: joins, no stacking
t+=RETRY_AFTER+1;answer='ok';
let settled=false;void retry.then(()=>{{settled=true;}});long.forEach(f=>f());await tick();out.buttonFreed=settled;  // the button re-enables
await f.retry();out.late=runs.length;out.hungRetryStale=runs[1].stale();     // past the bound: replaces the hung Retry
const after=f.join();await after;out.joinAfter=runs.length;                  // nothing running: a pulse starts one
console.log(JSON.stringify(out));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {'first': 1, 'pulseJoins': True, 'retry': 2, 'firstStale': True,
                                      'secondRetry': 2, 'buttonFreed': True, 'late': 3, 'hungRetryStale': True,
                                      'joinAfter': 4}
