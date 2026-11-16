"""New rota › How should the draft fit? (schedule.ts overlapChoice), from the real compiled client in Node: an option
the server marks unavailable never states a date range of its own (maintenance DEV: "Start after it" said "The draft runs
1 Mar 2027 – 20 Feb 2027" when the published rota already covered every day); the server's reason says why."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS, FAKE_DOM

PUB = {'id': 'p1', 'name': 'February rota', 'relation': 'conflict', 'starts_on': '2027-02-01', 'ends_on': '2027-03-01',
       'overlap': {'starts_on': '2027-02-14', 'ends_on': '2027-02-21'}}
WHY = 'The published rota already covers every day of this one'


def text(client_modules, options):
    o = {'starts_on': '2027-02-14', 'ends_on': '2027-02-21', 'publications': [PUB], 'drafts': [], 'conflict': True,
         'options': options}
    script = (GLOBALS + FAKE_DOM + "N.prototype.addEventListener=function(){};Object.defineProperty(N.prototype,'style',{get(){return this._s||(this._s={setProperty(){}});}});"
              "const cl=Object.getOwnPropertyDescriptor(N.prototype,'classList');Object.defineProperty(N.prototype,'classList',{get(){const c=cl.get.call(this);c.toggle=(k,on)=>{(on??!c.contains(k))?c.add(k):c.remove(k);};return c;}});"
              f"const m=await import({json.dumps((client_modules / 'schedule.js').as_uri())});"
              "const o=JSON.parse(process.argv[1]);console.log(JSON.stringify(m.overlapChoice(o,'UTC',()=>{}).root.textContent));")
    out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps(o)],
                         capture_output=True, text=True, check=True, timeout=15)
    return json.loads(out.stdout)


def test_an_unavailable_start_after_states_no_reversed_range_only_the_reason(client_modules):
    said = text(client_modules, [
        {'choice': 'start_after', 'available': False, 'default': True, 'starts_on': '2027-03-01', 'ends_on': '2027-02-21', 'why': WHY},
        {'choice': 'replace_from', 'available': False, 'starts_on': '2027-02-14', 'ends_on': '2027-02-21', 'why': 'x'}])
    assert 'The draft runs' not in said and '1 Mar 2027' not in said
    assert WHY in said


def test_an_available_start_after_states_its_range_in_order(client_modules):
    said = text(client_modules, [
        {'choice': 'start_after', 'available': True, 'default': True, 'starts_on': '2027-03-01', 'ends_on': '2027-03-08'}])
    assert 'The draft runs 1 Mar 2027 – 7 Mar 2027' in said
