"""Form answers that arrive late (forms.ts), from the real compiled client in Node with the small fake DOM of
test_client_toast_save.py plus the form controls a dialog needs. Requests are held and answered by the test; settling
is a fixed number of event-loop turns, never a fixed time."""
import json
import subprocess

from tests.test_client_toast_save import FAKE_DOM

# Form controls (value, validity) and the page around a dialog; `held` answers the test chooses, `json` a reply.
FORM_DOM = FAKE_DOM + r"""
const VAL={get(){return this._v??this.attrs.value??'';},set(v){this._v=String(v);},configurable:true};Object.defineProperty(N.prototype,'value',VAL);
for(const k of ['HTMLInputElement','HTMLSelectElement','HTMLTextAreaElement','HTMLFormElement','HTMLFieldSetElement','HTMLButtonElement','HTMLDialogElement']){
  globalThis[k]=class extends N{};Object.defineProperty(globalThis[k].prototype,'value',VAL);}
for(const k of ['setCustomValidity','scrollIntoView','setSelectionRange','blur'])N.prototype[k]=function(){};
N.prototype.checkValidity=N.prototype.reportValidity=()=>true;N.prototype.getClientRects=()=>[];
N.prototype.showModal=function(){this.open=true;this.attrs.open='';};N.prototype.close=function(){this.open=false;delete this.attrs.open;};
N.prototype.insertBefore=function(n,ref){if(!ref)return this.appendChild(n);ref.before(n);return n;};
Object.defineProperty(N.prototype,'validity',{get(){return {valid:true};}});
globalThis.CSS={escape:s=>s};globalThis.addEventListener=()=>{};globalThis.removeEventListener=()=>{};
globalThis.ResizeObserver=class{observe(){}disconnect(){}};globalThis.getComputedStyle=()=>({getPropertyValue:()=>''});
document.documentElement=Object.assign(document.createElement('html'),{clientWidth:1440,clientHeight:900});
const json=b=>new Response(JSON.stringify(b),{headers:{'Content-Type':'application/json'}});
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));};
const failures=[];process.on('unhandledRejection',e=>failures.push(String(e)));
const ctx={me:{organisation:{timezone:'UTC',week_start:6},department:{name:'Dept'}},catalogues:{},ready:async()=>{},refresh:async()=>{},
  navigate(){},setWorkspace(){},permits:()=>true};
"""


def run(client_modules, body):
    script = FORM_DOM + body.replace('FORMS', json.dumps((client_modules / 'forms.js').as_uri()))
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_new_rota_drops_a_late_overlap_answer_once_the_dates_are_incomplete(client_modules):
    """Restore c962d1906: the overlap answer for whole dates arrives after the last day was cleared; it no longer names
    this rota, so nothing is taken from it: no notes, no suggested name (it used to rename the rota from the earlier
    dates' answer, then fail on the empty day)."""
    got = run(client_modules, r"""
globalThis.window=Object.assign(new EventTarget(),{setTimeout,clearTimeout,innerHeight:900,innerWidth:1440,scrollY:0});
const held=[];globalThis.fetch=path=>String(path).includes('/api/schedule/overlaps')?new Promise(answer=>held.push(answer))
  :Promise.resolve(json({workspaces:[],coverage_gaps:[],has_more:false,previous_workspace_id:null}));
const f=await import(FORMS);
const ask=async clear=>{
  await f.workspaceForm(ctx);await settle();
  const dlg=[...document.querySelectorAll('dialog')].at(-1),dates=dlg.querySelectorAll('input').filter(i=>/^\d{4}-\d{2}-\d{2}$/.test(i.value));
  if(clear){dates[1].value='';dates[1].dispatchEvent(new Event('change'));await settle();}
  held.at(-1)(json({publications:[],drafts:[{name:'Other draft'}],suggested_name:'Rota from the answer'}));await settle();
  return {dates:dates.length,drawn:dlg.textContent.includes('Also being planned for these days: Other draft'),
    named:dlg.querySelectorAll('input').some(i=>i.value==='Rota from the answer')};};
const whole=await ask(false),cleared=await ask(true);
console.log(JSON.stringify({whole,cleared,failures}));process.exit(0);""")
    assert got['whole'] == {'dates': 2, 'drawn': True, 'named': True}, got  # the same answer for dates still whole is drawn
    assert got['cleared'] == {'dates': 2, 'drawn': False, 'named': False}, got
    assert got['failures'] == []


def test_check_against_rotas_keeps_its_answer_over_a_pending_debounced_check(client_modules):
    """Restore f66f296bb (legacy policyForm): an edit schedules a debounced rule check; "Check against rotas" pressed
    before it runs replaces it, so the debounce never starts a second check that would discard the press's answer.
    One check is sent and its answer opens the rota review (which reads the rotas being planned)."""
    got = run(client_modules, r"""
const timers=new Map();let seq=0;  // window timers fire only when the test says the interval has passed
globalThis.window=Object.assign(new EventTarget(),{setTimeout:(fn,ms)=>{timers.set(++seq,{fn,ms});return seq;},clearTimeout:id=>timers.delete(id),
  innerHeight:900,innerWidth:1440,scrollY:0});
const paths=[],checks=[];globalThis.fetch=path=>{path=String(path);paths.push(path);
  if(path.includes('/api/policies/check'))return new Promise(answer=>checks.push(answer));
  return Promise.resolve(json(path.includes('/api/policies/capabilities')?{rule_kinds:['max_shifts'],always_mandatory_kinds:[]}:{workspaces:[]}));};
const f=await import(FORMS);
await f.policyForm(ctx);await settle();
const dlg=document.querySelector('dialog'),form=dlg.querySelector('form');
form.dispatchEvent(new Event('input'));const pending=[...timers.values()].some(t=>t.ms>0);
dlg.querySelectorAll('button').find(b=>b.textContent==='Check against rotas').click();await settle();
for(const [id,t] of [...timers])if(t.ms>0){timers.delete(id);t.fn();}  // the debounce interval passes
await settle();
checks[0](json({compatible:true,issues:[],people_now:3}));await settle();
const after=paths.slice(paths.findIndex(p=>p.includes('/api/policies/check')));
console.log(JSON.stringify({pending,checks:checks.length,review:after.includes('/api/workspaces?limit=100&current_only=true'),failures}));
process.exit(0);""")
    assert got == {'pending': True, 'checks': 1, 'review': True, 'failures': []}, got
