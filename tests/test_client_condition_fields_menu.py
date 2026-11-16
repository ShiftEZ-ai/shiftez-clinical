"""D-057 for the shared filter builder: a property's values arriving (a lazy read, setFields) while + Add filter rule's
menu is open must not redraw the builder away from under it (dom.ts closes a menu whose button is gone), so the person's
next click lands. With nothing open, the rows redraw with the new values. Real compiled conditions.ts in Node."""
import json
import subprocess

from .test_client_toast_save import FAKE_DOM
from .test_client_menu_keep import SELECTORS


def test_new_field_values_keep_an_open_add_menu_and_redraw_once_it_is_closed(client_modules):
    script = FAKE_DOM + SELECTORS + f"""
// The fake DOM has no form-control classes; dom.ts only wraps their checkValidity.
for(const k of ['HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','HTMLFormElement','HTMLFieldSetElement'])globalThis[k]??=class{{checkValidity(){{return true;}}}};
const dom=await import({json.dumps((client_modules / 'dom.js').as_uri())});
const {{conditionBuilder}}=await import({json.dumps((client_modules / 'conditions.js').as_uri())});
const field=values=>({{key:'tag',label:'Tag',choices:values.map(v=>({{value:v,label:v}}))}});
const b=conditionBuilder([field(['A'])]);body.append(b.root);
const adder=()=>b.root.querySelector('.cond-add');
const first=adder();first.click();await frames();
const menu=()=>body.querySelector('[role=menu]');
const out={{opened:!!menu()}};
b.setFields([field(['A','B'])]);await frames();
out.whileOpen={{open:!!menu(),same:adder()===first}};
[...menu().querySelectorAll('[role=menuitem]')].find(x=>x.textContent.includes('Add filter rule')).click();await frames();
out.added=b.root.querySelectorAll('.cond-row').length;
b.setFields([field(['A','B','C'])]);
out.redrawn=adder()!==first;
console.log(JSON.stringify(out));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got['opened']
    assert got['whileOpen'] == {'open': True, 'same': True}  # the open menu and its button survive the new values
    assert got['added'] == 2  # its item still acts
    assert got['redrawn']  # with nothing open, new values redraw the rows


def test_an_edit_in_a_builder_select_is_announced_once(client_modules):
    """maintenance (444ac3e): the builder's Filter on, Operator and And/Or selects announced their edit twice (their own bubbling
    change plus a second one dispatched on the builder), so every consumer applied it twice: on the Schedule two redraws,
    the second reading the rota list, the draft, its jobs and sessions again. Each edit is now one change on the builder."""
    script = FAKE_DOM + SELECTORS + f"""
for(const k of ['HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','HTMLFormElement','HTMLFieldSetElement'])globalThis[k]??=class{{checkValidity(){{return true;}}}};
// A change bubbles as in a browser: its path (the target and its ancestors) is fixed before any listener runs.
const own=EventTarget.prototype.dispatchEvent;
Node.prototype.dispatchEvent=function(e){{const path=[];for(let n=this.parentElement;n;n=n.parentElement)path.push(n);
  const r=own.call(this,e);if(e.bubbles)for(const n of path)own.call(n,new Event(e.type));return r;}};
await import({json.dumps((client_modules / 'dom.js').as_uri())});
const {{conditionBuilder}}=await import({json.dumps((client_modules / 'conditions.js').as_uri())});
const tag={{key:'tag',label:'Tag',choices:[{{value:'A',label:'A'}},{{value:'B',label:'B'}}]}};
const b=conditionBuilder([tag],{{match:'all',conditions:[{{field:'tag',op:'any',values:['A']}},{{field:'tag',op:'any',values:['B']}}]}});
body.append(b.root);let n=0;b.root.addEventListener('change',()=>n++);
const pick=name=>b.root.all().filter(x=>x.tagName==='SELECT'&&x.getAttribute('name')===name)[0];
const edit=(name,value)=>{{const s=pick(name);s.value=value;n=0;s.dispatchEvent(new Event('change',{{bubbles:true}}));return n;}};
const out={{operator:edit('Operator','none'),joiner:edit('Joiner','any'),field:edit('Filter on','tag')}};
out.read=b.read();
console.log(JSON.stringify(out));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert {k: got[k] for k in ('operator', 'joiner', 'field')} == {'operator': 1, 'joiner': 1, 'field': 1}
    assert got['read']['match'] == 'any'  # the edits took effect
