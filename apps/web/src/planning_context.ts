/** On-demand recorded facts and quota shortfalls from the server's planning context. */
import {request,onSessionReset,ApiError} from './api.js';
import {working} from './loading.js';
import {el,button,note,showDialog,plural,formatDate,formatTime,spanLabel,emptyLine} from './dom.js';
import type {Row} from './types.js';
import {TEAM_PROBLEM_WORDS} from './generated.js';
import type {PlanningExclusion,PlanningRecordReference,PeriodReadiness,PlanningContextInput,ReadinessShortfall,RequirementEvidence,FindingObservationSummary,FindingFixHint} from './generated.js';
import {dockPanel} from './side_panel.js';  // lists are docked panels (UX › controls C26)
import {measureWords} from './rule_catalogue.js';

/** Why someone is ruled out, keyed by the contract's typed codes (a new code fails the build here). */
export const EXCLUSION_WORDS:Record<PlanningExclusion['code'],string>={UNAVAILABLE:'Recorded unavailability',RESTRICTION:'Active scheduling restriction',
  ELIGIBILITY_MISSING:'Clinical eligibility missing',ELIGIBILITY_ENDED:'Clinical eligibility ended',MEMBERSHIP_MISSING:'Department membership missing',
  RANK_NOT_ALLOWED:'Rank not permitted',PERSON_INACTIVE:'Employee inactive',EXTERNAL_ROTATION:'External rotation',CAPABILITY_NOT_GRANTED:'Required clinical capability missing',
  DUTY_NOT_EDITABLE:'Duty is protected',USUAL_ASSIGNEES_FIXED:'Usual people are fixed on this duty',OVERTIME_NOT_ALLOWED:'Overtime not allowed',
  OUTSIDE_GENERATION_POPULATION:'Not in the people chosen for this run',SUPERVISION_UNAVAILABLE:TEAM_PROBLEM_WORDS.needs};
const RECORD_WORDS:Record<PlanningRecordReference['collection'],string>={memberships:'Membership',eligibilities:'Clinic eligibility',grants:'Capability',rotations:'Rotation',availability:'Absence'};

/** Whether a stored run still fits its rota. Only the server says an auto-schedule result is current
 *  (`result_stale===false`, one classifier for list, detail and Home: inputs, scheduler and rule source); null or absent
 *  is an older record it cannot confirm, never current. '' current · 'rota' its inputs changed (the rota's fingerprint as
 *  read now) · 'scheduler' stale with the same inputs (the scheduler or its rule source changed) · 'unknown' ·
 *  'superseded' a newer run of the same sessions replaced it (job.currency; Apply refuses it with RUN_SUPERSEDED).
 *  A run not compared yet (notCompared) never says "the rota changed": while queued or running it makes no claim ('',
 *  its progress shows), finished it is 'unknown' (never applicable). */
export type Currency=''|'rota'|'scheduler'|'unknown'|'superseded';
/** A deferred run not yet prepared has no effective input fingerprint (G1FP; currency 'preparing'): there is nothing to
 *  compare with the rota yet. The one check every input_fingerprint comparison asks first. */
export const notCompared=(job:Row)=>job.input_fingerprint==null||job.currency==='preparing';
/** A replaced run's state and its next step, said alike wherever it is listed (Recent runs, its Review, Home). */
export const SUPERSEDED={state:'Replaced by a newer run',next:'Review latest run'} as const;
export const currency=(job:Row,fingerprint:string|undefined):Currency=>job.currency==='superseded'?'superseded'
  :notCompared(job)?(['queued','running'].includes(job.status)?'':'unknown'):job.input_fingerprint!==fingerprint?'rota'
  :job.kind!=='generation'||job.result_stale===false?'':job.result_stale===true?'scheduler':'unknown';
type Name=(kind:string,id:string)=>string;
export const SHORTFALL_WORDS:Record<ReadinessShortfall['code'],string>={
  RECURRING_SESSIONS_ENDED:'Recurring sessions end too early',
  SUPERVISED_MINIMUM_SHORTFALL:'Too few supervised places',
  PERSON_MINIMUM_SHORTFALL:'Too few sessions for a personal minimum',
};
function shortfallLine(row:ReadinessShortfall,name:Name){
  const labels=(kind:string,ids:string[])=>ids.map(id=>name(kind,id)).join(', ');
  const numbers=`Needs ${row.required} ${measureWords(row.metric,row.required)} · at most ${row.possible} possible`;
  return el('li',{},[
    labels('people',row.person_ids),labels('assignment_types',row.assignment_type_ids),labels('recurring_series',row.series_ids),
    labels('policies',row.rule_ids),spanLabel(row.starts_on+'T00:00:00Z',row.ends_on+'T00:00:00Z','UTC'),numbers,
  ].filter(Boolean).join(' · '));
}
function shortfallGroups(rows:ReadinessShortfall[],name:Name){
  const groups=Object.keys(SHORTFALL_WORDS).flatMap(code=>{
    const matches=rows.filter(row=>row.code===code);
    return matches.length?[el('li',{},el('strong',{},SHORTFALL_WORDS[matches[0].code]),
      el('ul',{class:'why-list'},...matches.map(row=>shortfallLine(row,name))))]:[];
  });
  return groups.length?el('ul',{class:'why-list','aria-label':'Rota-wide shortfalls'},...groups):null;
}
// One read per identical input while it is in flight (a second press or redraw waits for the same answer).
const inflight=new Map<string,Promise<unknown>>();
onSessionReset(()=>inflight.clear());
function post<T>(path:string,body:object):Promise<T>{
  const key=path+JSON.stringify(body);let p=inflight.get(key) as Promise<T>|undefined;
  if(!p){p=request<T>(path,{method:'POST',body:JSON.stringify(body)}).finally(()=>{if(inflight.get(key)===p)inflight.delete(key);});inflight.set(key,p);}
  return p;
}
const span=(a:string,b:string,zone:string)=>`${formatDate(a,zone)} ${formatTime(a,zone)}–${formatTime(b,zone)}`;
const record=(r:PlanningRecordReference,zone:string)=>[RECORD_WORDS[r.collection],r.start&&r.end?span(r.start,r.end,zone)
  :r.starts_on?`${formatDate(r.starts_on)}${r.ends_on?' – '+formatDate(r.ends_on):''}`:r.ends_on?`until ${formatDate(r.ends_on)}`:''].filter(Boolean).join(' ');
const exclusion=(x:PlanningExclusion,name:Name,zone:string)=>[EXCLUSION_WORDS[x.code],x.service_id?name('services',x.service_id):'',x.rule_id?name('policies',x.rule_id):'',
  ...(x.related_records||[]).map(r=>record(r,zone))].filter(Boolean).join(' · ');
/** The server refused because its rota check is missing or old: the check is the coordinator's explicit action. */
const needsCheck=(e:unknown)=>e instanceof ApiError&&['CHECK_REQUIRED','STALE_INPUTS'].includes(e.code);

/** Auto-schedule › What to fill: quota shortfalls and what is recorded for assigned people. `input` is the
 *  run as set up now (or why it cannot be read); a reply for a setup changed since is not shown. */
export function readinessRegion(o:{workspace:string;input:()=>PlanningContextInput|string;name:Name;zone:string;check?:()=>void}){
  const out=el('div',{class:'auto-readiness','aria-live':'polite'});let asked='';
  const read=button('Check readiness',()=>void show(),'link-button small');
  const show=async()=>{const input=o.input();if(typeof input==='string'){out.replaceChildren(read,note(input,'warning'));return;}
    const key=JSON.stringify(input);asked=key;out.replaceChildren(read,working('Checking readiness…'));
    try{const r=await post<PeriodReadiness>(`/api/workspaces/${encodeURIComponent(o.workspace)}/readiness`,input);if(asked!==key)return;
      const shortfalls=shortfallGroups(r.shortfalls||[],o.name);
      if(!r.checked){out.replaceChildren(read,...(shortfalls?[shortfalls]:[]),note('Check the rota to see exclusions for the people already assigned.','info'),...(o.check?[button('Check rota',o.check,'secondary small')]:[]));return;}
      out.replaceChildren(read,...(shortfalls?[shortfalls]:[]),el('p',{},el('strong',{},`Recorded for the people already assigned · ${plural(r.sessions,'session')}`)),
        r.groups.length?el('ul',{class:'why-list'},...r.groups.map(g=>el('li',{},[plural(g.people,'person','people'),EXCLUSION_WORDS[g.code],g.service_id?o.name('services',g.service_id):'',
          o.name('assignment_types',g.assignment_type_id),span(g.start,g.end,o.zone)].filter(Boolean).join(' · ')))):emptyLine('Nothing recorded rules them out.'),
        // The limit of these facts changes what Run can promise, so it stays visible.
        el('p',{class:'hint'},'Exclusions cover people already assigned. This check cannot say whether every place can be filled.'));
    }catch(e){if(asked!==key)return;out.replaceChildren(read,needsCheck(e)?note('The rota changed since its check. Check the rota first.','info'):note((e as Error).message,'error',e),
      ...(needsCheck(e)&&o.check?[button('Check rota',o.check,'secondary small')]:[]));}};
  out.append(read);
  // A changed setup drops what was shown for the old one (and any answer still on its way).
  return {root:out,sync:()=>{const i=o.input();if(asked&&(typeof i==='string'||JSON.stringify(i)!==asked)){asked='';out.replaceChildren(read);}}};
}

/** One person on the duty's evidence list: the name, why they are ruled out, and only the fixes the server served for
 *  that person's own exclusions (each named and permitted there: planning_context.py exclusion), through the same
 *  `fixes` as the dialog's own and its close. */
function evidenceRow(c:RequirementEvidence['candidates'][number],o:{name:Name;zone:string;fixes:(hints:FindingFixHint[],close:()=>void)=>HTMLElement[]},close:()=>void):HTMLElement{
  const own=o.fixes(c.exclusions.flatMap(x=>x.fix_hints||[]),close);
  return el('li',{},el('strong',{},o.name('people',c.person_id)),c.retained?' · already on it':'',
    c.exclusions.length?' · '+c.exclusions.map(x=>exclusion(x,o.name,o.zone)).join('; '):' · nothing recorded rules them out',
    own.length?el('span',{class:'actions'},...own):'');
}
/** One requirement's people, as the rota check recorded them: who is ruled out for this one duty and the dated records
 *  behind it. Not who would resolve the requirement as a whole (scoped or pooled requirements may count others). */
export function requirementEvidence(o:{workspace:string;fingerprint:string;observation:FindingObservationSummary;name:Name;zone:string;
  fixes:(hints:FindingFixHint[],close:()=>void)=>HTMLElement[];check?:()=>void}){
  dockPanel({key:'duty-people',title:'People recorded for this duty',build:({body,close})=>{
    const status=el('p',{role:'status'}),rows=el('ul',{class:'why-list'}),nav=el('div',{class:'actions'}),fixes=el('div',{class:'actions'});
    body.append(el('p',{class:'hint'},'Eligibility for this one duty only: it does not show who would resolve the whole requirement.'),status,rows,nav,fixes);
    let offset=0;
    const load=async()=>{nav.querySelectorAll('button').forEach(b=>b.disabled=true);status.textContent='Reading…';
      try{const r=await post<RequirementEvidence>(`/api/workspaces/${encodeURIComponent(o.workspace)}/observations/${encodeURIComponent(o.observation.id)}/evidence`,
          {expected_fingerprint:o.fingerprint,...(o.observation.shift_id?{session_ids:[o.observation.shift_id]}:{}),offset,limit:50});
        if(!body.isConnected)return;
        status.textContent=r.total?`${offset+1}–${Math.min(offset+r.candidates.length,r.total)} of ${plural(r.total,'person','people')} checked for this duty`:'No one was checked for this duty.';
        rows.replaceChildren(...r.candidates.map(c=>evidenceRow(c,o,close)));
        nav.replaceChildren(...(offset?[button('Previous',()=>{offset=Math.max(0,offset-50);void load();},'secondary small')]:[]),...(r.has_more?[button('Next',()=>{offset+=50;void load();},'secondary small')]:[]));
        fixes.replaceChildren(...o.fixes(r.fix_hints,close));
      }catch(e){status.textContent='';
        rows.replaceChildren(el('li',{},e instanceof ApiError&&e.status===404?'This issue is no longer in the rota check.':needsCheck(e)?'The rota changed since its check. Check the rota first.':(e as Error).message));
        nav.replaceChildren(...(needsCheck(e)&&o.check?[button('Check rota',()=>{close();o.check!();},'secondary small')]:[]));}};
    void load();
  }});
}
