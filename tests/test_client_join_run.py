"""Approve rota's review opens once per rota (#1041): the real compiled loading.js joinRun in Node. A start while one is
pending joins it (one run, one result); the run starts after the caller's draw; it says it is slow once, only past
SLOW_AFTER; its entry clears when it ends, a failed run included, so the next press runs again."""
import json
import subprocess


def node(client_modules, body):
    # A captured clock: the slow notice is checked by the delay it asks for, never by waiting.
    script = ("const timers=[],cleared=[];globalThis.setTimeout=(f,ms)=>{timers.push({f,ms});return timers.length;};"
              "globalThis.clearTimeout=t=>{if(t)cleared.push(t);};"
              f"const L=await import({json.dumps((client_modules / 'loading.js').as_uri())});{body}")
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_a_second_start_joins_the_pending_run_and_the_next_one_runs_again(client_modules):
    out = node(client_modules, (
        "const runs=new Map(),order=[];let runsStarted=0,release;const slow=[];"
        "const run=()=>{runsStarted++;order.push('run');return new Promise(r=>{release=r;});};"
        "const a=L.joinRun(runs,'r1',run,()=>slow.push('r1'));order.push('drawn');"
        "const b=L.joinRun(runs,'r1',run,()=>slow.push('again'));const pending=runs.has('r1');"
        "const other=L.joinRun(runs,'r2',()=>'other');"
        "await Promise.resolve();const same=a===b,started=runsStarted;"
        "timers[0].f();const delay=timers[0].ms;release('dialog');const [ra,rb]=await Promise.all([a,b]);await other;"
        "const cleared_=runs.size;const c=L.joinRun(runs,'r1',()=>{runsStarted++;throw new Error('read failed');});"
        "const failed=await c.then(()=>null,e=>e.message);"
        "console.log(JSON.stringify({same,started,pending,order,ra,rb,delay,slowAfter:L.SLOW_AFTER,slow,"
        "cleared:cleared_,timersCleared:cleared.length,failed,after:runs.size,total:runsStarted}));"))
    assert out['same'] is True and out['started'] == 1  # the second start joined: no second run
    assert out['pending'] is True
    assert out['order'] == ['drawn', 'run']  # the run starts after the caller's draw, which reads `runs`
    assert out['ra'] == out['rb'] == 'dialog'
    assert out['delay'] == out['slowAfter'] == 3000 and out['slow'] == ['r1']  # said once, by the first start only
    assert out['cleared'] == 0 and out['timersCleared'] >= 1  # ended: the entry and its slow notice are gone
    assert out['failed'] == 'read failed' and out['after'] == 0  # a failed run clears too: the next press runs
    assert out['total'] == 2
