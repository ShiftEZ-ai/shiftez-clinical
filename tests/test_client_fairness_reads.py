"""maintenance: Insights › Fairness asks the server once per URL however often the page is rebuilt (a live update, the check
after it opens from kept replies, a restyle) while its slow reads are on their way or answered; only a change that can
alter them, or a sign-out, asks again (the real compiled fairness.ts and api.ts in Node, fetch stubbed)."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS

DIST = '/api/fairness/distribution?starts_on=2026-10-01&ends_on=2026-11-01&metric=duties'
GOALS = '/api/fairness/goals?starts_on=2026-10-01&ends_on=2026-11-01'


def test_a_rebuilt_insights_page_shares_each_read_until_a_change_can_alter_it(client_modules):
    api, fairness = ((client_modules/name).as_uri() for name in ('api.js', 'fairness.js'))
    script = (
        GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};"
        "const sent=[];let answer;const slow=new Promise(r=>{answer=r;});"
        "globalThis.fetch=async p=>{sent.push(p);await slow;return new Response(JSON.stringify({people:[]}),{status:200});};"
        f"const api=await import({json.dumps(api)}),f=await import({json.dumps(fairness)});"
        f"const D={json.dumps(DIST)},G={json.dumps(GOALS)};const n=()=>sent.length;"
        # Three rebuilds while the first reads are still on their way: one request per URL.
        "const first=[f.fairnessRead(D),f.fairnessRead(G),f.fairnessRead(D),f.fairnessRead(D),f.fairnessRead(G)];"
        "const inFlight=n();answer();await Promise.all(first);"
        # Answered: a rebuild reads nothing.
        "await f.fairnessRead(D);await f.fairnessRead(G);const answered=n();"
        # A draft's change (and its auto-schedule job) moves only the reads that follow the rota in focus.
        "api.forgetReads(new Set(['workspace','jobs']));await f.fairnessRead(D);await f.fairnessRead(G);const draft=n();"
        # A publication (or anything unnamed) asks again for all of them.
        "api.forgetReads(new Set(['publication','workspace']));await f.fairnessRead(D);await f.fairnessRead(G);const published=n();"
        "api.forgetReads(null);await f.fairnessRead(D);const unnamed=n();"
        # Sign-out forgets them (api.ts onSessionReset).
        "api.clearSessionState();await f.fairnessRead(D);const signedOut=n();"
        "console.log(JSON.stringify({inFlight,answered,draft,published,unnamed,signedOut,"
        "goals:sent.filter(p=>p===G).length,dist:sent.filter(p=>p===D).length}));")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got == {'inFlight': 2, 'answered': 2, 'draft': 3, 'published': 5, 'unnamed': 6, 'signedOut': 7,
                   'goals': 3, 'dist': 4}
