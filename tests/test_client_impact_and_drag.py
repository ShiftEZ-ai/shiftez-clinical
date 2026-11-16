"""Impact findings are grouped by code AND explanation; a template placed on a
timeline ends block by block as the server computes it; every way out of a bar drag removes all its listeners."""
import json
import re
import subprocess
from pathlib import Path

from .test_client_dates_roles import run

SRC = Path(__file__).resolve().parents[1] / 'apps' / 'web' / 'src'


def run_with_listeners(client_modules, module, expression, payload):
    """As `run`, for a module that registers document listeners when it loads (timeline_drag's chip palette)."""
    script = (f"globalThis.document={{cookie:'',addEventListener(){{}}}};const m=await import({json.dumps((client_modules/module).as_uri())});"
              f"const input=JSON.parse(process.argv[1]);console.log(JSON.stringify({expression}));")
    result = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(payload)],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def test_one_code_with_different_explanations_stays_two_causes(client_modules):
    found = [{'code': 'COVER', 'message': 'Morning cover', 'rota': 'A'}, {'code': 'COVER', 'message': 'Night cover', 'rota': 'A'},
             {'code': 'COVER', 'message': 'Morning cover', 'rota': 'A'}, {'code': 'COVER', 'message': 'Morning cover', 'rota': 'B'}]
    plain = run(client_modules, 'configuration_impact.js', 'm.groupCauses(input).map(x=>[x.f.message,x.n])', found)
    assert plain == [['Morning cover', 3], ['Night cover', 1]]
    by_rota = run(client_modules, 'configuration_impact.js', 'm.groupCauses(input,f=>f.rota).map(x=>[x.f.rota,x.f.message,x.n])', found)
    assert by_rota == [['A', 'Morning cover', 2], ['A', 'Night cover', 1], ['B', 'Morning cover', 1]]


def test_a_mixed_unit_template_ends_where_the_server_ends_it(client_modules):
    # training.py _block_end: 2 months then 1 week from 1 Feb 2027 is 8 Apr 2027 (not 12 Apr from rounded weeks).
    drop = {'length': [9, 'weeks'], 'blocks': [[2, 'months'], [1, 'weeks']]}
    assert run_with_listeners(client_modules, 'timeline_drag.js', "m.dropEnd('2027-02-01',input)", drop) == '2027-04-08'
    assert run_with_listeners(client_modules, 'timeline_drag.js', "m.dropEnd('2027-01-31',input)", {'length': [1, 'months'], 'blocks': [[1, 'months'], [1, 'months']]}) == '2027-03-28'
    assert run_with_listeners(client_modules, 'timeline_drag.js', "m.dropEnd('2027-02-01',input)", {'length': [4, 'weeks']}) == '2027-03-01'


def test_every_way_out_of_a_bar_drag_removes_all_its_listeners():
    text = (SRC / 'timeline_drag.ts').read_text(encoding='utf-8')
    body = text[text.index('function pointerDrag('):text.index('function keyboard(')]
    stop = re.search(r"const stop=\(\)=>\{(.*?)\};", body, re.S).group(1)
    for listener in ("'pointermove',move", "'pointerup',up", "'keydown',esc,true"):
        assert f"removeEventListener({listener})" in stop, listener
    assert body.count('removeEventListener') == 3  # only in stop
    assert re.search(r"const up=\(u:PointerEvent\)=>\{stop\(\);", body) and re.search(r"const esc=.*?stop\(\);", body)


def test_short_milestone_labels_keep_the_24px_lane(client_modules):
    """A one-character milestone label 21 px from the next does not shorten its 24 px
    target's lane, so two openable milestones never share a lane with overlapping buttons (both densities)."""
    row = {'id': 'r', 'name': 'Rank', 'bars': [
        {'id': 'a', 'start': '2027-01-01', 'end': '2027-01-02', 'label': 'R', 'style': 'milestone', 'open': True},
        {'id': 'b', 'start': '2027-01-03', 'end': '2027-01-04', 'label': 'S', 'style': 'milestone', 'open': True}]}
    for compact in ('true', 'false'):
        # 30 days over 315 px: 10.5 px a day, so the two diamonds are 21 px apart.
        lanes = run_with_listeners(client_modules, 'timeline.js',
            f"(()=>{{const d=m.dayNumber('2027-01-01');return m.layoutRow(input,[d,d+30],315,{compact}).placed.map(p=>p.lane);}})()", row)
        assert lanes[0] != lanes[1], (compact, lanes)


def test_a_draft_edit_saves_on_one_press_without_running_the_preview():
    """D-077: Save in draftImpact (Add session, copy, swap, reconcile) sends the command at once. It never runs or waits
    for the optional preview, never refuses to ask for a second press, and sends a fingerprint only for the preview on
    screen for exactly these values. A refusal already shown is not sent again unchanged."""
    text = (SRC / 'draft_impact.ts').read_text(encoding='utf-8')
    save = text[text.rindex('  return async()=>{'):]
    assert 'calc(' not in save and 'await running' not in save and 'request(' not in save
    # Nothing refuses before the command is sent: a throw is allowed only in the reply's own handling (a server refusal
    # already said in the panel, e.g. Save without, ends silently so the form adds no second copy).
    assert 'throw new Error(' not in save[:save.index('await command(')]
    assert 'stillRefused(now)' in save
    assert re.search(r"seen=!running&&started===epoch&&!!fingerprint&&now===captured", save)
    assert 'seen?fingerprint:undefined' in save
    for gone in ('press Save again', 'exact current values', 'before saving', 'before you save'):
        assert gone not in text, gone
