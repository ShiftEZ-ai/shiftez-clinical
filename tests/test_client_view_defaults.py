"""Set as my / department default (table_view.ts `viewActions`, the one owner of those menu actions) is offered only for
a target the server can store: a saved view's id, or `null`, which the server reads as the FIRST built-in. Another
built-in shown (e.g. a print preset after Standard) must be saved as a view first, or its default would open the first."""
import json
import subprocess

import pytest


@pytest.fixture(scope='module')
def actions(client_modules):
    def call(builtins_on, saved=(), active=''):
        script = (f"const b=await import({json.dumps((client_modules / 'bulk.js').as_uri())});"
                  "b.setBulkContext({conditionFields:()=>[],choices:()=>undefined,permits:()=>true,labelOf:(_,id)=>id});"
                  f"const t=await import({json.dumps((client_modules / 'table_view.js').as_uri())});"
                  "const [on,saved,activeId]=JSON.parse(process.argv[1]);"
                  "const a=t.viewActions({key:'k',noun:'rows',saved,activeId,current:()=>({}),dirty:()=>false,done:()=>{},"
                  "builtins:on.map((v,i)=>({label:'View '+i,on:v,apply:()=>{}}))});"
                  "console.log(JSON.stringify([!!a.makeMine,!!a.makeDepartment]));")
        out = subprocess.run(['node', '--input-type=module', '-e', script, json.dumps([builtins_on, list(saved), active])],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)
    return call


def test_only_the_first_built_in_or_a_saved_view_can_become_a_default(actions):
    assert actions([True, False, False]) == [True, True]      # the first built-in: `null` names it
    assert actions([False, False, True]) == [False, False]    # a later built-in: save it as a view first
    assert actions([False, False, False]) == [False, False]   # changed away from every built-in, nothing saved
    shared = {'id': 'v1', 'revision': 1, 'name': 'Handout copy', 'definition': {}, 'scope': 'department'}
    assert actions([False, False, True], [shared], 'v1') == [True, True]  # a saved view: its own id
    assert actions([]) == [True, True]                         # no built-ins: `null` is the list's one default view


def test_a_saved_view_shown_before_the_saved_views_load_is_the_one_set_as_default(actions, client_modules):
    """Restore 9e917cf70: the address names a saved view (`activeId`) while the saved views are still loading. Set as
    default is offered for it, and it sends that view's id: never `null`, which the server reads as the first built-in."""
    assert actions([False, False, True], [], 'v1') == [True, True]  # a later built-in drawn meanwhile does not hide it
    script = (f"const b=await import({json.dumps((client_modules / 'bulk.js').as_uri())});"
              "b.setBulkContext({conditionFields:()=>[],choices:()=>undefined,permits:()=>true,labelOf:(_,id)=>id});"
              f"const t=await import({json.dumps((client_modules / 'table_view.js').as_uri())});"
              # Just enough of a page for api.ts command(): the CSRF cookie, save events and the pending-command store.
              "globalThis.document={cookie:'rota_csrf=fictional'};globalThis.window=Object.assign(new EventTarget(),{setTimeout,clearTimeout});"
              "const kept=new Map();globalThis.sessionStorage={getItem:k=>kept.get(k)??null,setItem:(k,v)=>kept.set(k,String(v)),removeItem:k=>kept.delete(k)};"
              "const sent=[];globalThis.fetch=async(_,o)=>{sent.push(JSON.parse(o.body));"
              "return new Response(JSON.stringify({defaults:{}}),{status:200,headers:{'Content-Type':'application/json'}});};"
              "const a=t.viewActions({key:'k',noun:'rows',saved:[],activeId:'v1',current:()=>({}),dirty:()=>false,done:()=>{},"
              "builtins:[{label:'Standard',on:true,apply:()=>{}}]});"
              "await a.makeMine().catch(()=>{});"  # the message after the save needs a real page; the command is what counts
              "console.log(JSON.stringify(sent.map(c=>[c.kind,c.payload])));process.exit(0);")
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [['table_view.set_default', {'table_key': 'k', 'scope': 'personal', 'view_id': 'v1'}]]
