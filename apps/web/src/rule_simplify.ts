/** Rules › Simplify rules: the department's rules move to one rule per idea
 *  in ONE reviewed proposal — before → after, the server's proof that behaviour is unchanged, its blockers, and what
 *  each rule becomes. A coordinator or admin proposes; another approver activates; Undo brings the previous rules back
 *  exactly. Nothing here decides equivalence: the page shows what the server previewed and recorded. */
import {el,button,armed,readFailed,alive,compact,relabel,confirmAction,empty,formatDate,labelOf,note,notify,plural,ruleFacts,statusBadge,CAPABILITY} from './dom.js';
import {fullNameOf} from './person_label.js';
import {get,command,sessionChanged,WITHDRAWN,awaitJob,keptRead,type KeptReadOptions} from './api.js';
import {skeleton} from './loading.js';
import {dockPanel,closeDock} from './side_panel.js';
import {dataTable} from './table_view.js';
import {RULE_CATALOGUE,RULE_CATALOGUE_V2,RULE_FAMILIES} from './generated.js';
import {catalogueVersion,type Options} from './rule_catalogue.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';
import {mayDecide} from './independence.js';

const PREVIEW=[CAPABILITY.COORDINATOR,CAPABILITY.ADMIN,CAPABILITY.APPROVER,CAPABILITY.AUDITOR],READ=[...PREVIEW,CAPABILITY.PROGRAMME_LEAD];
const FACT_WORDS:Record<string,[string,string]>={workload:['personal quota','personal quotas'],target:['personal target','personal targets'],
  overtime_offer:['overtime offer','overtime offers'],protected_time:['protected time','protected times']};
/** The comparisons the proof makes, in words (strict_proof's keys). */
const PROOF_WORDS:Record<string,string>={expanded:'rule obligations',findings:'rule checks',quota:'workload counts',fairness:'fairness',coverage:'cover',
  validation:'other check results',score:'schedule score',model:'scheduling model'};
/** The preview answers at once; the strict proof runs as a background job (rule.catalogue.check). */
const PREVIEW_READ='/api/rules/catalogue/migration?version=2&background=true';
const CHECKERS=[CAPABILITY.COORDINATOR,CAPABILITY.APPROVER];
const day=(x:unknown)=>x?formatDate(String(x).slice(0,10)):'';
/** Where a blocker's action goes: the Rules page's own (set by the header button, which the Rules page always draws).
 *  shortcut: module state for the one Rules page; pass them through simplifyPanel if another page opens it first. */
let go:Partial<Options>={};
const entryName=(id:string)=>(RULE_CATALOGUE_V2.find(e=>e.id===id)||RULE_CATALOGUE.find(e=>e.id===id))?.name||id;
/** "2|Duties per week|Night rule": a rule's place in the catalogue's family order, then the entry and its own name. */
const familyOrder=(id:string,name:string)=>{const e=RULE_CATALOGUE_V2.find(x=>x.id===id)||RULE_CATALOGUE.find(x=>x.id===id);
  const f=RULE_FAMILIES.findIndex(([k])=>k===e?.family);return `${String(f<0?99:f).padStart(2,'0')}|${entryName(id)}|${name}`.toLowerCase();};

// The simplifications and each one's record, kept across rebuilds of the Rules page (api.ts keptRead; maintenance): the
// header button, its panel and a retired rule's source share one read until a rule, list, setting or access change.
const CONVERSIONS:KeptReadOptions={scopes:['rules','catalogue','settings','access'],cap:8};
const keptGet=<T>(url:string)=>keptRead(url,()=>get<T>(url),CONVERSIONS).p;
/** The department's simplifications (`history`: withdrawn and undone ones too). */
export const conversions=(history=true)=>keptGet<{records:Row[]}>('/api/rules/catalogue/conversions'+(history?'?include_history=true':''));
/** One simplification with its plan and proof. */
export const conversion=(id:string)=>keptGet<{record:Row}>('/api/rules/catalogue/conversions/'+encodeURIComponent(id));

/** The header button: in a department still on the old rules (for those who may read the preview), or while a
 *  simplification waits for approval or is in force (it carries the Undo). Hidden while rule changes are being drafted:
 *  propose or discard them first (and a phone header keeps room for Propose). */
export function simplifyButton(ctx:Context,refresh:()=>Promise<void>,o:Partial<Options>={}):(HTMLElement&{drafting:(on:boolean)=>void})|null{
  go=o;
  if(!READ.some(key=>ctx.permits(key)))return null;
  let shown=catalogueVersion(ctx)===1&&PREVIEW.some(key=>ctx.permits(key)),drafting=false;
  const b=compact(button('Simplify rules',()=>simplifyPanel(ctx,refresh),'secondary'),'sparkle'),sync=()=>{b.hidden=!shown||drafting;};sync();
  void conversions().then(r=>{
    if(r.records.some(x=>x.status==='proposed'))relabel(b,'Simplify rules · waiting for approval');shown||=r.records.length>0;sync();}).catch(()=>undefined);
  return Object.assign(b,{drafting:(on:boolean)=>{drafting=on;sync();}});
}

export function simplifyPanel(ctx:Context,refresh:()=>Promise<void>,conversionId?:string){
  dockPanel({key:'simplify-rules',className:'rs-panel',title:'Simplify rules',sub:'One rule per idea, in one reviewed proposal',width:760,build:({body})=>{
    body.append(skeleton('lines','Checking the department’s rules…'));
    const draw=()=>load(ctx,conversionId).then(x=>{if(body.isConnected)body.replaceChildren(...view(ctx,x,refresh,draw));}).catch(err=>{
      if(!sessionChanged(err)&&body.isConnected)body.replaceChildren(note((err as Error).message,'error',err),
        button('Try again',()=>simplifyPanel(ctx,refresh,conversionId),'secondary small'));});
    void draw();
  }});
}

type Loaded={record:Row|null;plan:Row|null;records:Row[];history:boolean};
async function load(ctx:Context,conversionId?:string):Promise<Loaded>{
  const {records}=await conversions();
  // One at a time: the waiting proposal, else the one in force; otherwise a fresh preview (its proof checked in the background).
  const current=records.find(r=>r.status==='proposed')||records.find(r=>r.status==='active'),id=conversionId||current?.id;
  if(id){const {record}=await conversion(id);
    return {record,records,history:!!conversionId&&conversionId!==current?.id,plan:{...record.plan,proof:record.proof,plan_hash:record.plan_hash}};}
  if(!PREVIEW.some(key=>ctx.permits(key))||catalogueVersion(ctx)===2)return {record:null,plan:null,records,history:false};
  const plan=await get<Row>(PREVIEW_READ);
  return {record:null,plan:plan.bindings?.length||plan.blocked?.length?plan:null,records,history:false};
}

function view(ctx:Context,{record,plan,records,history}:Loaded,refresh:()=>Promise<void>,redraw:()=>Promise<void>):Node[]{
  const past=proposalHistory(ctx,records,refresh);
  if(!plan)return [catalogueVersion(ctx)===2||PREVIEW.some(key=>ctx.permits(key))?empty('Nothing to simplify',catalogueVersion(ctx)===2?'The rules are already one rule per idea.':'Every rule is already one rule per idea.',null,'check')
    :empty('No proposal waiting','A coordinator or admin proposes simplifying the rules; it then waits here for review.',null,'check'),...(past?[past]:[])];
  const mine=record?.proposed_by===ctx.me.identity_id,state=record?.status||'preview';
  const counts=(plan.counts||{}) as Row,facts=Object.entries((counts.facts||{}) as Record<string,number>);
  const proof=plan.proof as Row|null,blocked=(plan.blocked||[]) as Row[],identical=proof?.identical===true;
  // A preview's proof is the server's background check of exactly this plan (proof_state); a proposal carries its own.
  const proofState=record?'recorded':String(plan.proof_state||(proof?identical?'passed':'different':'not_checked'));
  const said:Record<string,()=>string>={preview:()=>'A preview: nothing has changed. One proposal covers the whole department; it applies once another person approves it.',
    proposed:()=>`Proposed ${day(record?.created_at)}${mine?' by you':''}. The current rules apply until another person activates it.`,
    active:()=>`In force since ${day(record?.approved_at||record?.updated_at)}. Undo brings back the previous rules exactly.`,
    declined:()=>'Declined. The proposal did not change the department’s rules.',retired:()=>'Undone. The previous rules were restored; this proposal is kept for history.'};
  const out:Node[]=[el('div',{class:'rs-state'},statusBadge(state==='preview'?'draft':state,state==='preview'?'Preview':state==='retired'?'Undone':undefined),el('span',{class:'muted'},said[state]?.()||''))];
  if(history)out.push(button('View current proposal',()=>simplifyPanel(ctx,refresh),'secondary small'));
  if(record?.reason)out.push(el('p',{class:'muted'},el('strong',{},'Proposal reason: '),String(record.reason)));
  out.push(ruleFacts([
    ['Rules',`${counts.current_sources??'—'} → ${counts.target_rules??'—'}`],
    ['Moves to people',facts.length?facts.map(([k,n])=>plural(n,...(FACT_WORDS[k]||['record','records']))).join(' · '):'Nothing'],
    ['Hidden from the list',[counts.empty_types?plural(counts.empty_types,'empty rule'):'',counts.expired_lines?plural(counts.expired_lines,'ended line'):''].filter(Boolean).join(' · ')||'Nothing'],
    ['Kept for history',counts.retired_sources?plural(counts.retired_sources,'retired rule'):'Every replaced rule, retired']]));
  // Behaviour: exactly what the server's strict proof says, never inferred here.
  if(blocked.length)out.push(blockers(ctx,blocked));
  else if(['not_checked','queued','running','failed','cancelled'].includes(proofState))out.push(proofCheck(ctx,plan,proofState,redraw));
  else if(!proof)out.push(note('The behaviour check did not finish. Preview again before proposing.','error'));
  else if(identical)out.push(note((proof.rotas||[]).length?`Behaviour unchanged on ${plural(proof.rotas.length,'rota')} being planned: ${Object.values(PROOF_WORDS).join(', ')} are identical.`
    :'Behaviour unchanged: every rule obligation is identical. No rota is being planned, so there is no schedule to compare.','success'));
  else{const differ=[...new Set(((proof.rotas||[]) as Row[]).flatMap(r=>Object.keys(PROOF_WORDS).filter(k=>r[k]?.equal===false)))].map(k=>PROOF_WORDS[k]);
    out.push(note(differ.length||proof.expanded_equal===false?`Behaviour would change${differ.length?` (${differ.join(', ')})`:''}. It cannot be proposed as it is.`
      :'The behaviour check is incomplete. It cannot be proposed until the server confirms equivalence.','error'));}
  if(record&&proof)out.push(el('small',{class:'muted'},'This is the equivalence check recorded for this proposal. Activation rechecks the exact inputs.'));
  out.push(wasNow(plan));
  // Propose only on a fresh passing proof of this exact plan (the server's proof_state is for its plan_hash).
  const ready=!blocked.length&&identical&&(!!record||proofState==='passed');
  out.push(actions(ctx,record,plan,state,mine,ready,proofState,refresh));
  if(past)out.push(past);
  return out;
}

/** The background behaviour check of this preview: started for those who may (one job per plan; the server reuses it),
 *  its progress read every few seconds while the panel is open, and the preview drawn again when it ends. */
function proofCheck(ctx:Context,plan:Row,state:string,redraw:()=>Promise<void>):HTMLElement{
  const can=CHECKERS.some(key=>ctx.permits(key)),box=el('section',{class:'notice info rs-proof','aria-busy':'true'});
  const start=async()=>(await command<{job:Row}>('rule.catalogue.check',{version:2,plan_hash:plan.plan_hash,reason:'Check that simplifying the rules keeps behaviour unchanged'})).job;
  const paint=(job:Row|null)=>{
    const p:Row=job?.progress||{},done=Number(p.completed_rotas)||0,total=Number(p.total_rotas)||0,counting=job?.status==='running'&&total>0;
    box.replaceChildren(el('p',{role:'status'},el('strong',{},'Checking behaviour is unchanged…')),  // the progress bar says how far, never a fixed estimate
      el('progress',{max:String(total||1),...(counting?{value:String(done)}:{}),'aria-label':'Behaviour check progress'}),
      el('small',{class:'muted'},`${counting?`${done} of ${plural(total,'rota')} checked`:'Waiting to start'} · you can keep working; Propose turns on when it passes`));};
  const failed=(why:string)=>{box.removeAttribute('aria-busy');box.className='notice error rs-proof';
    box.replaceChildren(el('p',{},why),...(can?[button('Check again',()=>{paint(null);box.className='notice info rs-proof';void start().then(poll).catch(e=>{if(!sessionChanged(e))failed((e as Error).message);});},'secondary small')]:[]));};
  const poll=async(job:Row)=>{paint(job);if(await awaitJob(job,{every:3000,seen:paint,stop:()=>!box.isConnected})&&box.isConnected)await redraw();};
  if(state==='failed'||state==='cancelled')failed(state==='cancelled'?'The behaviour check was cancelled. Propose stays off until a check passes.'
    :`The behaviour check could not finish${plan.proof_job?.error_code==='STALE_INPUTS'?': the rules or rotas changed while it ran':''}. Propose stays off until a check passes.`);
  else if(state==='not_checked'&&!can){box.removeAttribute('aria-busy');box.append(el('p',{},'The behaviour check has not run for this preview. A coordinator or approver starts it by opening Simplify rules; Propose turns on when it passes.'));}
  else{paint(plan.proof_job||null);void (state==='not_checked'?start():Promise.resolve(plan.proof_job as Row)).then(poll).catch(e=>{if(!sessionChanged(e))failed((e as Error).message);});}
  return box;
}

/** What stops the move, in plain words, each with the way to clear it: the server names the exact proposals, people and
 *  lines (proposal_ids, person_ids, line_ids). Propose stays off until a fresh preview has none. */
function blockers(ctx:Context,blocked:Row[]):HTMLElement{
  const open=(label:string,act:(()=>void)|undefined,aria?:string)=>act?button(label,()=>{closeDock();act();},'secondary small',aria?{'aria-label':aria}:{}):'';
  const policy=(id:string)=>(ctx.catalogues.policies||[]).find(p=>p.id===id);
  const rule=(b:Row)=>open('Open rule',b.rule_id&&go.openRule?()=>go.openRule!(policy(b.rule_id)||{id:b.rule_id,revision:0}):undefined);
  const person=(id:string)=>fullNameOf((ctx.catalogues.people||[]).find(p=>p.id===id))||'A person';
  const item=(b:Row):HTMLElement[]=>{
    const reason=String(b.reason||'').replace(/^Not in the catalogue yet: /,''),name=String(b.name||'');
    if(b.kind==='proposal'){
      const ids=(b.proposal_ids||[]) as string[];
      return [el('li',{class:'rs-blocker'},el('div',{},el('strong',{},'Rule changes are waiting for approval'),
        el('small',{class:'muted'},'Approve, decline or withdraw each one first.')),
        ...ids.length?[el('ul',{class:'rs-blocker-list'},...ids.map(id=>{const p=policy(id),entry=p?.kind==='catalogue'?String(p.definition?.rule?.id||''):'',label=entry?entryName(entry):p?labelOf(p):'A waiting rule change';
          return el('li',{},el('span',{},label),open('Open proposal',entry&&go.navigate?()=>go.navigate!('rules/'+entry):go.openRule?()=>go.openRule!(p||{id,revision:0}):undefined,`Open proposal: ${label}`));}))]:[])];}
    const people=(b.person_ids||[]) as string[],lines=(b.line_ids||[]) as string[];
    if(people.length||lines.length){
      // A catalogue rule's rows are its lines; an older rule's rows are its people.
      const source=policy(b.rule_id),own=(source?.definition?.rule?.lines||[]) as Row[],byLine=source?.kind==='catalogue';
      const rows=byLine?lines.map(id=>({label:`Line ${own.findIndex(l=>l.id===id)+1}`,go:()=>go.openLine!(b.rule_id,{lineId:id})}))
        :people.map(id=>({label:person(id),go:()=>go.openLine!(b.rule_id,{personId:id})}));
      return [el('li',{class:'rs-blocker'},el('div',{},el('strong',{},name),
        el('small',{class:'muted'},`${rows.length===1?(byLine?'A line has':'A person has'):(byLine?'Some lines have':'Some people have')} no number of their own and ${rows.length===1?'takes':'take'} the rule’s usual one. Give ${rows.length===1?(byLine?'it':'them'):'each'} a number, then preview again.`),
        el('ul',{class:'rs-blocker-list'},...rows.map(r=>el('li',{},el('span',{},r.label),open('Open row',go.openLine?r.go:undefined,`Open row: ${r.label}`))))),rule(b))];}
    if(/person exceptions/.test(reason))return [el('li',{class:'rs-blocker'},el('div',{},el('strong',{},name),
      el('small',{class:'muted'},'It has current person exceptions. End them or let them expire, then preview again.')),open('Open exceptions',go.exceptions))];
    return [el('li',{class:'rs-blocker'},el('div',{},el('strong',{},name||'The department'),el('small',{class:'muted'},reason.replace(/^./,c=>c.toUpperCase()))),rule(b))];};
  return el('section',{class:'notice warning rs-blockers',role:'note'},
    el('strong',{},`${plural(blocked.length,'thing stops','things stop')} the move. Propose is off until ${blocked.length===1?'it is':'they are'} cleared.`),
    el('ul',{class:'rs-blocker-items'},...blocked.flatMap(item)));
}

function proposalHistory(ctx:Context,records:Row[],refresh:()=>Promise<void>):HTMLElement|null{
  if(!records.length)return null;
  return el('details',{class:'rs-map'},el('summary',{},'Proposal history'),dataTable<Row>({id:'simplify-rule-history',noun:['proposal','proposals'],
    rows:records,rowId:r=>r.id,pageSize:25,searchLabel:'Search proposals',searchPlaceholder:'Status or proposal reason',
    onOpen:r=>simplifyPanel(ctx,refresh,r.id),columns:[
      {key:'created',label:'Proposed',text:r=>day(r.created_at),cell:r=>day(r.created_at)},
      {key:'status',label:'Status',text:r=>r.status==='retired'?'Undone':r.status,cell:r=>statusBadge(r.status,r.status==='retired'?'Undone':undefined)},
      {key:'reason',label:'Proposal reason',text:r=>String(r.reason||''),cell:r=>String(r.reason||'')}]}).root);
}

/** Was → now: each rule in force and what it becomes (catalogue rules and person records), searchable at department scale. */
function wasNow(plan:Row):HTMLElement{
  const targets=new Map(((plan.targets||[]) as Row[]).map(t=>[t.row.id,t.catalogue_id])),facts=new Map(((plan.scheduling_facts||[]) as Row[]).map(f=>[f.id,f.kind]));
  const now=(r:Row)=>{const d=(r.destinations||[]) as Row[],rules=new Map<string,number>(),people=new Map<string,number>();
    for(const x of d){if(x.kind==='policy'){const n=entryName(targets.get(x.id)||'');rules.set(n,(rules.get(n)||0)+1);}
      else{const k=facts.get(x.id)||'';people.set(k,(people.get(k)||0)+1);}}
    return [...[...rules].map(([n,c])=>`${n} · ${plural(c,'line')}`),...[...people].map(([k,c])=>plural(c,...(FACT_WORDS[k]||['person record','person records'])))].join(' · ')||'—';};
  const rows=(plan.report||[]) as Row[];
  return el('section',{class:'rs-map'},el('h3',{},'What each rule becomes'),dataTable<Row>({id:'simplify-rules',noun:['rule','rules'],rows,rowId:r=>r.rule_id,pageSize:25,
    searchLabel:'Search rules',searchPlaceholder:'Rule or what it becomes',empty:['No rules in force','There is nothing to move.'],
    columns:[{key:'was',label:'Rule now',text:r=>`${r.name} ${entryName(r.catalogue_id)}`,cell:r=>el('span',{class:'ph-what'},String(r.name),el('small',{class:'muted'},entryName(r.catalogue_id)))},
      {key:'now',label:'Becomes',text:now,cell:now},
      // A clear order: by family (the catalogue's order), then the rule it becomes, then its own name.
      {key:'order',label:'Family, then name',hidden:true,cell:()=>'',sort:r=>familyOrder(String(r.catalogue_id),String(r.name))}],
    defaults:{sort:['order','asc']}}).root);
}

/** A stored proposal's complete census, read fresh (never a kept read) with details: the server serves it only when it
 *  matches the hash recorded with the proposal, so what is shown is what is decided; a mismatch is its refusal. */
function evidencePanel(record:Row){
  dockPanel({key:'simplify-evidence',className:'rs-panel',title:'Full evidence',sub:`Proposal of ${day(record.created_at)}`,width:760,build:({body})=>{
    body.append(skeleton('lines','Loading the full evidence…'));
    void get<{record:Row}>(`/api/rules/catalogue/conversions/${encodeURIComponent(String(record.id))}?details=true`).then(({record:full})=>{
      if(!alive(body))return;
      const census=(full.plan?.census||{}) as Row,counts=(census.counts||{}) as Record<string,number>,sources=(census.sources||[]) as Row[];
      const head=el('tr',{},...['Rule','Revision','Status','Becomes','Classification','Lines'].map(h=>el('th',{scope:'col'},h)));
      const rows=sources.map(s=>el('tr',{},el('td',{},entryName(String(s.catalogue_id||s.kind||''))),el('td',{class:'num'},String(s.source_revision??'')),
        el('td',{},String(s.status||'')),el('td',{},s.target_catalogue_id?entryName(String(s.target_catalogue_id)):'—'),el('td',{},String(s.classification||'')),
        el('td',{class:'num'},String((s.line_ids||[]).length))));
      body.replaceChildren(ruleFacts([['Rules',String(counts.sources??sources.length)],['Lines',String(counts.lines??'—')],
          ['Kept for history',String(counts.history_sources??'—')],['Evidence hash',String(census.hash||'—')]]),
        el('table',{class:'dt-table'},el('thead',{},head),el('tbody',{},...rows)));
    }).catch(err=>{if(!sessionChanged(err)&&alive(body))body.replaceChildren(readFailed('the full evidence',err,()=>evidencePanel(record)));});
  }});
}
function actions(ctx:Context,record:Row|null,plan:Row,state:string,mine:boolean,ready:boolean,proofState:string,refresh:()=>Promise<void>):HTMLElement{
  const can=(...keys:string[])=>ctx.permits(...keys);
  const done=async(message:string)=>{closeDock();notify(message);await refresh();};
  const decide=(kind:string,title:string,text:string,label:string,message:string,final=false)=>confirmAction(title,text,async reason=>{
    await command(kind,{reason},record!.id,record!.revision);await done(message);},label,undefined,false,final);
  const buttons:HTMLElement[]=[];
  // The proposal's full evidence, bound to its stored hash, one action from where it is decided.
  if(record)buttons.push(button('Full evidence',()=>evidencePanel(record),'secondary'));
  const checking=['not_checked','queued','running'].includes(proofState);
  if(state==='preview'&&!ready&&!checking)buttons.push(button('Preview again',()=>simplifyPanel(ctx,refresh),'secondary'));
  if(state==='preview'&&can(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN))buttons.push(button('Propose',()=>confirmAction('Simplify rules','One proposal for the whole department. The current rules apply until another authorised person activates it.',async reason=>{
    await command('rule.migrate_to_catalogue',{version:2,plan_hash:plan.plan_hash,reason});await done('Proposed; it applies once another authorised person activates it.');},'Propose'),'primary',{disabled:!ready,why:(plan.blocked||[]).length?'Clear what stops the move first, then preview again.':checking?'Wait for the behaviour check: Propose turns on when it passes.':'The behaviour check must confirm nothing changes first.'}));
  const decides=mayDecide(ctx.me,'rule.activate',mine);
  if(state==='proposed'&&can(CAPABILITY.APPROVER)&&decides)buttons.push(button('Activate',()=>decide('rule.catalogue.activate','Activate the simplified rules',
    'Your independent approval activates the complete proposal. Rechecks the exact inputs, then replaces the department’s rules in one step. Undo brings them back.','Activate','Rules simplified.'),'primary',{disabled:!ready,why:checking?'Wait for the behaviour check.':'Clear what stops the move first.'}));
  if(state==='proposed'&&can(CAPABILITY.APPROVER,CAPABILITY.COORDINATOR,CAPABILITY.ADMIN))buttons.push(mine?armed(button('Withdraw',async()=>{await command('rule.catalogue.decline',{reason:WITHDRAWN},record!.id,record!.revision);await done('Proposal withdrawn.');},'secondary'),'Withdraw?')
    :button('Decline',()=>decide('rule.catalogue.decline','Decline the simplification','The current rules stay; the proposal is kept in the history.','Decline','Declined.'),'secondary'));
  if(state==='active'&&can(CAPABILITY.APPROVER))buttons.push(button('Undo',()=>decide('rule.catalogue.undo','Undo the simplification',
    'The previous rules apply again exactly as before. The simplified rules and person records are kept in the history.','Undo','The previous rules apply again.',true),'secondary'));
  const who=state==='preview'?'Coordinators and admins propose; another approver activates.':state==='proposed'?(mine&&!decides?'Another approver activates it: you proposed it.':mine?'You proposed it; your self-approval permission lets you activate it with a reason.':'An approver who did not propose it activates it.'):state==='active'?'An approver can undo it.':'';
  return el('footer',{class:'rs-actions'},el('span',{class:'muted'},who),el('span',{class:'grow'}),...buttons);
}
