"""D59 notYet: a control held again with a new reason (the rota check went from waiting to running) says the new words
everywhere at once: its tip, its description and the message a press shows, with one press listener, never two
messages. Real compiled dom.ts in Node with a small fake DOM."""
import json
import subprocess

from .test_client_menu_keep import SELECTORS
from .test_client_toast_save import FAKE_DOM

AFTER = r"""
globalThis.HTMLInputElement=class extends N{};
N.prototype.after=function(...ns){const p=this.parentElement;for(const n of ns){const a=p._adopt(n);p.kids.splice(p.kids.indexOf(this)+1,0,a);}};
"""


def test_a_held_control_held_again_says_only_its_new_reason(client_modules):
    script = FAKE_DOM + SELECTORS + AFTER + f"""
const dom=await import({json.dumps((client_modules / 'dom.js').as_uri())});
const b=document.createElement('button');body.append(b);let ran=0;
dom.notYet(b,'Waiting to check');dom.notYet(b,'Checking the rota');
// The control's own action (Node's EventTarget has no capture phase: added after the hold, as the browser orders it).
b.addEventListener('click',()=>ran++);
const described=()=>(b.getAttribute('aria-describedby')||'').split(' ').map(id=>body.querySelector('#'+id)?.textContent).join(' ');
b.click();
const toasts=body.querySelectorAll('.toast').map(t=>t.textContent);
console.log(JSON.stringify({{held:b.getAttribute('aria-disabled'),tip:b.dataset.tip,described:described(),toasts,ran}}));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got == {'held': 'true', 'tip': 'Checking the rota', 'described': 'Checking the rota',
                   'toasts': ['Checking the rota'], 'ran': 0}
