"""Changes to a published rota, from the real compiled published_changes.ts in Node (logic only): the send button's name
for the change-list rule (D12: urgent alone, routine all together)."""
import json
import subprocess


def run(client_modules, body):
    script = f"const m=await import({json.dumps((client_modules/'published_changes.js').as_uri())});console.log(JSON.stringify({body}));"
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_the_send_button_is_named_after_its_step(client_modules):
    assert run(client_modules, "[m.sendLabel('urgent',3),m.sendLabel('routine',1),m.sendLabel('routine',3)]") == \
        ['Send for urgent approval', 'Send for approval', 'Send 3 changes for approval']


def staged(client_modules, row, d):
    return run(client_modules, f'm.stagedRow({json.dumps(row)},{d})')


FULL = {'shift_id': 's', 'status': 'covered', 'required': 1, 'scheduled': 1, 'available_eligible': 1,
        'places_needed': 1, 'places_filled': 1, 'places_open': 0}


def test_a_staged_absence_opens_the_place_the_grid_reads(client_modules):
    """SX-04: the check's rows carry their own places (packages/domain/coverage.py) and the grid reads those first
    (planner_week.ts coverOf), so a staged absence opens one there too; putting someone on fills it again."""
    got = staged(client_modules, FULL, -1)
    assert (got['available_eligible'], got['scheduled'], got['places_filled'], got['places_open']) == (0, 0, 0, 1)
    back = staged(client_modules, got, 1)
    assert (back['places_filled'], back['places_open']) == (1, 0)


def test_a_spare_person_leaving_opens_nothing_and_other_shortfalls_stay(client_modules):
    got = staged(client_modules, {**FULL, 'scheduled': 2, 'available_eligible': 2}, -1)
    assert (got['available_eligible'], got['places_filled'], got['places_open']) == (1, 1, 0)
    # A supervision shortfall the server counted (headcount met) is not filled by one more person.
    got = staged(client_modules, {**FULL, 'status': 'gap', 'places_needed': 2, 'places_open': 1}, 1)
    assert (got['places_filled'], got['places_open']) == (1, 1)
    # An older check without its own places keeps the headcount alone.
    old = staged(client_modules, {k: v for k, v in FULL.items() if not k.startswith('places_')}, -1)
    assert old['available_eligible'] == 0 and 'places_open' not in old


def test_reading_an_empty_change_list_redraws_nothing_and_a_live_change_still_draws(client_modules):
    """Restore e425f0912: only a live change draws anything (chip, marks), so reading a published rota's EMPTY change list
    never redraws the Schedule under a warning or menu just opened. The read that finds a change still draws."""
    script = f"""const set=changes=>({{workspace:null,changes,urgent_route:{{active:false,approver_count:0}}}});
globalThis.fetch=async path=>new Response(JSON.stringify(set(String(path).includes('pub-live')?[{{id:'c1',status:'draft'}}]:[])),
  {{status:200,headers:{{'Content-Type':'application/json'}}}});
const m=await import({json.dumps((client_modules/'published_changes.js').as_uri())});
let empty=0;m.loadChanges(['pub-empty'],()=>{{empty++;}});
// The second read is answered after the first: once its change is drawn, the empty list's read has settled too.
await new Promise(drawn=>m.loadChanges(['pub-live'],drawn));
console.log(JSON.stringify({{empty,live:'drawn'}}));process.exit(0);"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {'empty': 0, 'live': 'drawn'}
