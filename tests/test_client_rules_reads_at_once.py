"""A Rules list reload sends its own reads in the burst beside the lists, not in waves after them (scale: 4–5 serial
waves made a 150-person reload take about 6 s). The real compiled rule_catalogue.ts page runs in Node with a small fake
DOM; fetch holds every request until the test answers the whole burst, so each request's wave is the number of bursts
answered before it was sent. The boot steps are app.ts boot()'s, in its order: the account, then the lists and the live
baseline (and readRulesAhead for a Rules list address), the contacts the lists withheld, then the page's draw."""
import json
import re
import subprocess

from .test_client_menu_keep import SELECTORS
from .test_client_reach_list_repaint import CLONE
from .test_client_toast_save import FAKE_DOM

CLIENT_DAY = '2026-11-20'
RULES = ['/api/policies/capabilities', '/api/catalogues?active_only=true&include=services&include=locations&include=assignment_types',
         '/api/rules/catalogue?version=2&include_hidden=true', '/api/table-views?table=rule-tree',
         '/api/rules/catalogue/conversions?include_history=true']


def reload(client_modules, server_day):
    """The waves of one Rules list reload: {wave: [paths sent in it]}."""
    modules = {name: (client_modules / f'{name}.js').as_uri() for name in ('api', 'dom', 'rule_catalogue')}
    script = FAKE_DOM + SELECTORS + CLONE + r"""
globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};globalThis.sessionStorage=globalThis.localStorage;
globalThis.getComputedStyle=()=>({getPropertyValue:()=>''});globalThis.ResizeObserver=class{observe(){}disconnect(){}};
// What the Rules list's toolbar reads beyond the fake DOM: attributes the browser reflects as properties.
globalThis.HTMLButtonElement=N;globalThis.HTMLInputElement=N;
for(const k of ['placeholder'])Object.defineProperty(N.prototype,k,{get(){return this.attrs[k]??'';},set(v){this.attrs[k]=String(v);}});
// The department's today on this client is fixed (UTC zone); the server's catalogue answers DAY.
const FIXED=Date.UTC(2026,10,20,12),RealDate=Date;globalThis.Date=class extends RealDate{constructor(...a){super(...(a.length?a:[FIXED]));}static now(){return FIXED;}};
const DAY='""" + server_day + r"""';
// Every request waits for the burst it belongs to; answer() answers all that are waiting: one wave.
let wave=1,waiting=[];const sent=[];
const reply=p=>p.startsWith('/api/me')?{identity_id:'i1',roles:['coordinator'],catalogue_version:2,
    organisation:{week_start:0,timezone:'UTC'},department:{id:'d1',display_settings:{rule_catalogue:true}}}
  :p.startsWith('/api/rules/catalogue/conversions')?{records:[]}
  :p.startsWith('/api/rules/catalogue')?{entries:[],families:[],day:DAY,catalogue_version:2,attention:[]}
  :p.startsWith('/api/rules-explained')?{questions:[],conflicts:[],people:null}
  :p.startsWith('/api/table-views')?{views:[]}
  :p.startsWith('/api/catalogues?active_only')?{services:[],locations:[],assignment_types:[]}
  :p.startsWith('/api/catalogues')?{people:[{id:'p1'}],_versions:{people:'1'}}
  :p.startsWith('/api/directory')?{people:[]}:{};
globalThis.fetch=async(path)=>{const p=String(path);sent.push({p,wave});await new Promise(r=>waiting.push(r));
  return new Response(JSON.stringify(reply(p)),{status:200,headers:{'Content-Type':'application/json'}});};
const settle=()=>new Promise(r=>setTimeout(r,20));
const answer=async()=>{await settle();const now=waiting;waiting=[];wave++;now.forEach(r=>r());await settle();};
const api=await import(""" + json.dumps(modules['api']) + r"""),dom=await import(""" + json.dumps(modules['dom']) + r""");
const rc=await import(""" + json.dumps(modules['rule_catalogue']) + r""");
// 1. The account.
const me=api.request('/api/me');await answer();const account=await me;dom.setWeekStart(0,'UTC');
const ctx={me:account,catalogues:{people:[],policies:[],exceptions:[]},permits:(...k)=>k.some(x=>dom.hasAccess(account.roles,x)),
  refresh:async()=>{},navigate(){},ready:async()=>{}};
// 2. The lists and the live baseline; a Rules list address starts its own reads beside them (absent before the fix).
const ahead=rc.readRulesAhead?.(ctx);
const lists=api.background('/api/catalogues?');api.background('/api/leave-types');api.background('/api/pulse');
await answer();
// 3. The lists withheld contacts: the directory, joined by id.
if((await lists).people.some(p=>!('work_phone' in p))){const d=api.background('/api/directory');await answer();await d;}
// 4. The page draws (a new page: the reads of the one left are aborted first), answering whatever it still asks.
api.leavePage();const target=document.createElement('div');body.append(target);
let done=false;rc.ruleCataloguePage(ctx,target,{parts:[],openTag(){},navigate(){},ahead}).then(()=>{done=true;},e=>{done=e;});
for(let i=0;i<8&&done!==true;i++){await settle();if(waiting.length)await answer();if(done&&done!==true)throw done;}
await settle();if(waiting.length)await answer();
console.log(JSON.stringify({done,sent}));process.exit(0);
"""
    out = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-4000:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got['done'] is True
    waves = {}
    for r in got['sent']:
        waves.setdefault(r['wave'], []).append(r['p'])
    return waves


def sent_in(waves, path):
    return [wave for wave, paths in sorted(waves.items()) for p in paths if p == path]


def test_a_rules_reload_reads_the_rules_beside_the_lists_and_each_read_once(client_modules):
    waves = reload(client_modules, CLIENT_DAY)
    # Every read the Rules list makes is sent once, in the burst beside the lists (wave 2); none waits for another.
    for path in [*RULES, f'/api/rules-explained?group_by=question&at={CLIENT_DAY}']:
        assert sent_in(waves, path) == [2], (path, waves)
    # The account, then the lists with the page's reads, then the contacts the lists withheld: three waves (base: six).
    assert sorted(waves) == [1, 2, 3], waves
    assert waves[3] == ['/api/directory'], waves


def test_a_catalogue_answering_another_day_still_draws_that_days_questions(client_modules):
    """Midnight between the client and the server: the questions shown are the catalogue's day, as before."""
    waves = reload(client_modules, '2026-11-21')
    assert sent_in(waves, '/api/rules-explained?group_by=question&at=2026-11-21') == [4], waves
    for path in RULES:
        assert sent_in(waves, path) == [2], (path, waves)


def test_boot_starts_the_rules_reads_before_it_waits_for_the_lists(client_modules):
    """app.ts wires it: boot starts readRulesAhead beside the lists (not after them), and the next draw takes it."""
    source = (client_modules / 'app.js').read_text(encoding='utf-8')
    boot = re.findall(r'\nasync function boot\(card\) \{(.*?)\n\}', source, re.S)
    assert len(boot) == 1
    start, lists = boot[0].find('readRulesAhead(ctx())'), boot[0].find('await Promise.all([loadCatalogues(')
    assert 0 <= start < lists, (start, lists)
    assert len(re.findall(r'ahead: rulesRead\b', source)) == 1
