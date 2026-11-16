"""DL-027: a person's profile has its own address, #people?open=<id> (the People page opens the hub from it, and clears
it on close), so a reload, a shared link and Back bring it back. Every person name goes through personLink; the hub
opener app.ts registers navigates to that address whenever People can show the person, and opens the hub in place
only for a reader People cannot show them to. Real compiled person_label.ts in Node; the opener's wiring in app.ts."""
import json
import subprocess
from pathlib import Path

from .test_client_toast_save import FAKE_DOM

APP = Path(__file__).resolve().parents[1] / 'apps/web/src/app.ts'


def test_a_person_name_goes_to_the_profiles_address(client_modules):
    script = FAKE_DOM + f"""
for(const k of ['HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','HTMLFormElement','HTMLFieldSetElement'])globalThis[k]??=class{{checkValidity(){{return true;}}}};
const m=await import({json.dumps((client_modules / 'person_label.js').as_uri())});
const went=[];const b=m.personLink('p 1','Alex Morgan',path=>went.push(path),'Alex Morgan');b.click();
const opened=[];m.onPersonOpen((id,label)=>opened.push([id,label]));m.personLink('p2','Alex Morgan',path=>went.push(path),'Alex Morgan').click();
console.log(JSON.stringify({{went,opened}}));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['went'] == ['people?open=p%201']  # the address, encoded
    assert got['opened'] == [['p2', 'Alex Morgan']]  # a registered opener decides


def test_the_registered_opener_navigates_to_the_address_when_people_can_show_them():
    source = APP.read_text(encoding='utf-8')
    opener = source[source.index('onPersonOpen((id,label)=>'):]
    opener = opener[:opener.index('as Row);});') + 11]
    assert "navigate('people?'+new URLSearchParams({open:id}))" in opener, opener
    assert "allowed('people')" in opener and 'personDetails(' in opener, opener
