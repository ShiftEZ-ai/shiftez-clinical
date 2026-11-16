""""Show their…" (bulk_children.ts showTheir), from the real compiled client in Node with the small fake DOM of
test_client_toast_save.py: the menu's counts are its own requests, so picking a list stops the ones still out (a browser
holds about six requests to one server at once; the list just picked must not queue behind them)."""
import json
import subprocess

from tests.test_client_toast_save import FAKE_DOM


def test_picking_a_list_stops_the_counts_still_out(client_modules):
    """Restore 7384ddbe8: every count request still unanswered is aborted when a list is picked."""
    script = FAKE_DOM + f"""
globalThis.CSS={{escape:s=>s}};globalThis.window=Object.assign(new EventTarget(),{{setTimeout,clearTimeout,innerHeight:900,innerWidth:1440,scrollY:0}});
globalThis.ResizeObserver=class{{observe(){{}}disconnect(){{}}}};globalThis.getComputedStyle=()=>({{getPropertyValue:()=>''}});
globalThis.addEventListener=()=>{{}};globalThis.removeEventListener=()=>{{}};
document.documentElement=Object.assign(document.createElement('html'),{{clientWidth:1440,clientHeight:900}});
// Counts never answer by themselves: each request waits until it is aborted.
const sent=[];globalThis.fetch=(path,o)=>{{sent.push(o.signal);return new Promise((_,fail)=>o.signal?.addEventListener('abort',()=>fail(new DOMException('Aborted','AbortError'))));}};
const b=await import({json.dumps((client_modules / 'bulk.js').as_uri())});
b.setBulkContext({{conditionFields:()=>[],choices:()=>undefined,permits:()=>true,labelOf:(_,id)=>id}});
const s=await import({json.dumps((client_modules / 'bulk_children.js').as_uri())});
const anchor=document.createElement('button');document.body.append(anchor);
s.showTheir(anchor,'people',['person','people'],['p1','p2'],'People');
const items=document.querySelectorAll('.menu-item');
const before=sent.map(x=>!!x?.aborted);
items[0].click();
console.log(JSON.stringify({{items:items.length,sent:sent.length,before,after:sent.map(x=>!!x?.aborted)}}));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['items'] > 1 and got['sent'] == got['items'], got  # one count per child list, all still out
    assert not any(got['before']), got
    assert all(got['after']), got
