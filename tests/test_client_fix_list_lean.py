"""Lean Fix list (PERF 5dca7e1b5 + LEAD-UIUX, 8 Nov 2026): fix_list.ts readFixList opens on `view=lean` only; a
collapsed row's facts (Safety, New, the line under the sentence, the action's label) come from the lean row alone; a
cause's evidence is one `view=detail` read through api.ts keptRead keyed [rota, revision, fingerprint, cause], so a second
Show evidence or the list read again asks nothing; a stale detail (409) reads the lean list again, then the detail once;
the footer and the panel hold the one lean summary. The real compiled modules in Node, fetch stubbed."""
import json
import subprocess

from .test_client_autoschedule_live import GLOBALS

SERVER = """const sent=[];let R=1;
const lean=id=>({id,level:id==='c1'?'must':'could',sentence:id==='c1'?'Monday clinic needs a consultant':'Workload is uneven',
  shift_ids:['s1'],safety:id==='c1',new:id==='c1',detail_line:'1 of 2 consultants',where:{label:'Monday clinic AM'},
  // c2 as the lean view may send it without repeats: its keys only in session_findings, its scope without shift_ids.
  finding_keys:id==='c1'?['k-'+id]:[],session_findings:{s1:'k-'+id},
  action:{kind:'choose_cover',label:'Choose cover',available:true,unavailable_reason:null,
    scope:id==='c1'?{classification:'duties_during_approved_leave',shift_ids:['s1']}:{classification:null,dates:{starts_on:'2026-10-04',ends_on:'2026-10-10'}}},fixes:[]});
const reply=(body,status=200)=>new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
globalThis.fetch=async p=>{const u=new URL(p,'http://x'),q=u.searchParams,view=q.get('view')||'full';sent.push(view+(q.get('cause_id')?':'+q.get('cause_id')+'@'+q.get('revision'):''));
  const head={workspace_id:'w',revision:R,fingerprint:'f'+R};
  if(view==='lean')return reply({...head,summary:{must:1,could:1,checking:false,places_filled:3,places_total:4,places_open:1,checks_must:2,
    checks_could:1,fairness_checks:0,acknowledged:0,submit_blocked:true},causes:[lean('c1'),lean('c2')]});
  if(view==='detail'){if(Number(q.get('revision'))!==R)return reply({error:{code:'STALE_REVISION',message:'The Fix list changed'}},409);
    const id=q.get('cause_id');return reply({...head,cause:{...lean(id),evidence:[{explanation:'Why '+id+' at '+R}],rule_lines:[{rule_id:'r1',line:0}]}});}
  return reply({error:{code:'NOT_EXPECTED',message:view}},500);};"""


def test_open_is_lean_only_and_each_cause_detail_is_read_once(client_modules):
    fix = (client_modules / 'fix_list.js').as_uri()
    script = (GLOBALS + "globalThis.localStorage={getItem:()=>null,setItem(){},removeItem(){}};" + SERVER +
              f"const m=await import({json.dumps(fix)});const out={{}};"
              "const l=await m.readFixList('w');out.open=sent.splice(0);"
              # A collapsed row's facts, from the lean row alone (before any detail).
              "const c1=l.causes[0];out.row={safety:c1.safety,new:c1.new,line:m.fixLine(c1),label:m.actionLabel(c1),evidence:c1.evidence.length};"
              # The footer's held list and the panel's are the one lean read: equal counts by construction.
              "const c2=l.causes[1];out.restored={shifts:c2.action.scope.shift_ids,keys:c2.finding_keys,from:c2.action.scope.starts_on};"
              "out.counts={held:m.lastFixList('w')===l&&m.heldFixList('w',l.summary)===l,must:l.summary.must,could:l.summary.could};"
              # Show evidence twice (at once, then again): one detail read; then nothing left to load.
              "await Promise.all([m.loadDetail(c1),m.loadDetail(c1)]);out.expand=sent.splice(0);out.again=m.loadDetail(c1);"
              "out.evidence=c1.evidence.map(e=>e.explanation);"
              # The list read again at the same revision: its row's detail is kept (keptRead), only the lean list is asked.
              "const l2=await m.readFixList('w');await m.loadDetail(l2.causes[0]);out.rebuilt=sent.splice(0);"
              # The rota moved on: the detail is refused (409), the lean list is read again, then the detail once.
              "R=2;await m.loadDetail(l.causes[1]);out.stale=sent.splice(0);out.staleEvidence=l.causes[1].evidence.map(e=>e.explanation);"
              "console.log(JSON.stringify(out));")
    run = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out['open'] == ['lean']  # opening reads the lean view only
    assert out['row'] == {'safety': True, 'new': True, 'line': 'Monday clinic AM · 1 of 2 consultants',
                          'label': 'Find cover / replace', 'evidence': 0}
    assert out['restored'] == {'shifts': ['s1'], 'keys': ['k-c2'], 'from': '2026-10-04'}  # repeats the lean view may leave out
    assert out['counts'] == {'held': True, 'must': 1, 'could': 1}
    assert out['expand'] == ['detail:c1@1'] and out['again'] is None and out['evidence'] == ['Why c1 at 1']
    assert out['rebuilt'] == ['lean']  # no second detail read for the same cause at the same revision
    assert out['stale'] == ['detail:c2@1', 'lean', 'detail:c2@2'] and out['staleEvidence'] == ['Why c2 at 2']
