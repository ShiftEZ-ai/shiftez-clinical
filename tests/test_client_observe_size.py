"""loading.ts observeSize, the client's one ResizeObserver (real compiled module in Node, observer stubbed): a box that was
on the page and has left is let go, including one hidden (0x0) the whole time, which the observer never reports; a box
built off-page and not yet mounted is kept for when it is shown; a stopped watch lets its box go."""
import json
import subprocess


def test_observe_size_lets_go_of_boxes_that_left_the_page(client_modules):
    loading = (client_modules / 'loading.js').as_uri()
    script = (
        "const frames=[];globalThis.requestAnimationFrame=f=>{frames.push(f);return frames.length;};"
        "const flush=()=>{while(frames.length)frames.shift()(0);};"
        "const watched=new Set();globalThis.ResizeObserver=class{observe(e){watched.add(e);}unobserve(e){watched.delete(e);}};"
        "const box=on=>({isConnected:on});"
        f"const m=await import({json.dumps(loading)});"
        # Hidden on the page (never reported), then removed while hidden: let go at the next frame.
        "const hidden=box(true);m.observeSize(hidden,()=>{});flush();const out={hiddenOn:watched.has(hidden)};"
        "hidden.isConnected=false;m.observeSize(box(true),()=>{});flush();out.hiddenGone=!watched.has(hidden);"
        # Built off-page, not mounted yet: kept; mounted then removed: let go.
        "const later=box(false);m.observeSize(later,()=>{});flush();out.offPageKept=watched.has(later);"
        "later.isConnected=true;m.observeSize(box(true),()=>{});flush();later.isConnected=false;m.observeSize(box(true),()=>{});flush();"
        "out.mountedThenGone=!watched.has(later);"
        # A stopped watch lets its box go; a box still on the page stays.
        "const kept=box(true),stop=m.observeSize(kept,()=>{});flush();out.onPage=watched.has(kept);stop();out.stopped=!watched.has(kept);"
        "console.log(JSON.stringify(out));")
    run = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {
        'hiddenOn': True, 'hiddenGone': True, 'offPageKept': True, 'mountedThenGone': True, 'onPage': True, 'stopped': True}


def test_observe_size_never_keeps_a_box_alive_by_itself(client_modules):
    """SC-01 (test_browser_schedule_memory): a box built off-page and hidden (the closed Auto-schedule panel's fields) that
    leaves before a frame saw it mounted is never let go by the sweep; the module must not be what keeps it, and its old
    page, alive. The stubbed observer holds nothing, as Chromium's does not."""
    loading = (client_modules / 'loading.js').as_uri()
    script = (
        "const frames=[];globalThis.requestAnimationFrame=f=>{frames.push(f);return frames.length;};"
        "const flush=()=>{while(frames.length)frames.shift()(0);};"
        "globalThis.ResizeObserver=class{observe(){}unobserve(){}};"
        f"const m=await import({json.dumps(loading)});"
        "const ref=(()=>{const box={isConnected:false};m.observeSize(box,()=>{});flush();return new WeakRef(box);})();"
        "for(let i=0;i<5&&ref.deref();i++){await new Promise(r=>setTimeout(r,0));gc();}"
        "console.log(JSON.stringify({released:!ref.deref()}));")
    run = subprocess.run(['node', '--expose-gc', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {'released': True}


def test_a_kept_stop_never_keeps_its_box_alive(client_modules):
    """HEAP (live refresh memory): a stop function kept after its box left the page (a hook of a page left behind, such
    as the Schedule's scroll put-back or a fitted row) holds the box weakly, so it never keeps that page alive; called
    while the box lives, it still lets the box go."""
    loading = (client_modules / 'loading.js').as_uri()
    script = (
        "const frames=[];globalThis.requestAnimationFrame=f=>{frames.push(f);return frames.length;};"
        "const flush=()=>{while(frames.length)frames.shift()(0);};"
        "const watched=new Set();let report;globalThis.ResizeObserver=class{constructor(f){report=f;}"
        "observe(e){watched.add(new WeakRef(e));}unobserve(e){for(const r of watched)if(r.deref()===e)watched.delete(r);}};"
        f"const m=await import({json.dumps(loading)});"
        "const stops=[];const ref=(()=>{const box={isConnected:true};stops.push(m.observeSize(box,()=>{}));flush();"
        "box.isConnected=false;m.observeSize({isConnected:true},()=>{});flush();return new WeakRef(box);})();"
        "for(let i=0;i<5&&ref.deref();i++){await new Promise(setImmediate);gc();}"
        "const live={isConnected:true},calls=[];"
        "const stop=m.observeSize(live,()=>calls.push('first')),again=m.observeSize(live,()=>calls.push('second'));flush();"
        "const resize=n=>{report([{target:live,contentRect:{width:n,height:n}}]);flush();};"
        "resize(1);stops.forEach(s=>s());stop();resize(2);const afterFirstStop=[...calls];"
        "const observedAfterFirstStop=[...watched].some(r=>r.deref()===live);"
        "again();const observedAfterLastStop=[...watched].some(r=>r.deref()===live);resize(3);"
        "console.log(JSON.stringify({released:!ref.deref(),afterFirstStop,calls,observedAfterFirstStop,observedAfterLastStop}));")
    run = subprocess.run(['node', '--expose-gc', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {
        'released': True, 'afterFirstStop': ['first', 'second', 'second'], 'calls': ['first', 'second', 'second'],
        'observedAfterFirstStop': True, 'observedAfterLastStop': False}
