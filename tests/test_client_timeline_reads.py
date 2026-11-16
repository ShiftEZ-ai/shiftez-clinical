"""maintenance on People › Timeline: a rebuilt timeline (a live pulse, Back, a restyle) joins the read of its window that is
on its way, or draws its answer, instead of queueing another behind it; only a change that can alter it, or a sign-out,
asks again (the real compiled person_timeline.ts and api.ts in Node, fetch stubbed). Each `peopleTimeline()` is one
rebuild; `worksIn` asks the line's window through the same read as a drawing."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS

LINE = {'people': [{'id': 'p1', 'areas': [{'label': 'FM'}]}], 'layers': [], 'timezone': 'UTC', 'offset': 0, 'has_more': False}


def test_rebuilt_timelines_share_one_request_per_window_until_a_change(client_modules):
    api, timeline = ((client_modules/name).as_uri() for name in ('api.js', 'person_timeline.js'))
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "const sent=[];let answer;let slow=new Promise(r=>{answer=r;});"
        f"globalThis.fetch=async p=>{{sent.push(p);await slow;return new Response(JSON.stringify({json.dumps(LINE)}),{{status:200}});}};"
        f"const api=await import({json.dumps(api)}),pt=await import({json.dumps(timeline)});"
        # One rebuild: a new timeline asks for p1's areas and says when they landed.
        "const rebuild=()=>new Promise(done=>{const tl=pt.peopleTimeline({areasChanged:()=>done(tl.worksIn('p1'))});tl.worksIn('p1');});"
        "const n=()=>sent.length;"
        # Three rebuilds while the first read is on its way: one request, none queued behind it.
        "const first=[rebuild(),rebuild(),rebuild()];answer();const areas=await Promise.all(first);const inFlight=n();"
        # Answered: a rebuild reads nothing.
        "await rebuild();const answered=n();"
        # A refresh whose changes cannot alter a timeline keeps the answer; any other (or an unnamed one) reads again.
        "api.forgetReads(new Set(['inbox','feedback']));await rebuild();const unrelated=n();"
        "api.forgetReads(new Set(['inbox','workspace']));await rebuild();await rebuild();const changed=n();"
        "api.forgetReads(null);await rebuild();const unnamed=n();"
        # A save (api.ts command's rota-saved) forgets them before its live change arrives.
        "dispatchEvent(new CustomEvent('rota-saved',{detail:{kind:'people.update'}}));await rebuild();const saved=n();"
        # Sign-out forgets them (api.ts onSessionReset).
        "api.clearSessionState();await rebuild();const signedOut=n();"
        "console.log(JSON.stringify({areas,inFlight,answered,unrelated,changed,unnamed,saved,signedOut}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {'areas': [['FM']] * 3, 'inFlight': 1, 'answered': 1, 'unrelated': 1, 'changed': 2, 'unnamed': 3,
                   'saved': 4, 'signedOut': 5}


def test_works_in_redraws_only_when_its_answer_changes_what_the_list_says(client_modules):
    """Before its read Works in says nothing for anyone: an answer naming no areas changes no cell, so the list is not
    rebuilt under the reader (People, run 42863010208); an answer naming one does."""
    api, timeline = ((client_modules/name).as_uri() for name in ('api.js', 'person_timeline.js'))
    empty = {**LINE, 'people': [{'id': 'p1', 'areas': []}, {'id': 'p2'}]}
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "let line;globalThis.fetch=async()=>new Response(JSON.stringify(line),{status:200});"
        f"const api=await import({json.dumps(api)}),pt=await import({json.dumps(timeline)});"
        "const heard=async answer=>{line=answer;api.forgetReads(null);let n=0;const tl=pt.peopleTimeline({areasChanged:()=>{n++;}});"
        "tl.worksIn('p1');for(let i=0;i<50;i++)await new Promise(r=>setTimeout(r,0));return [n,tl.worksIn('p1')];};"
        f"const none=await heard({json.dumps(empty)}),named=await heard({json.dumps(LINE)});"
        "console.log(JSON.stringify({none,named}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {'none': [0, []], 'named': [1, ['FM']]}
