import {request} from './api.js';
import type {LeadContext,LeadContextInput,StaffingSupervisionPlan} from './generated.js';

/** A named association from the checked plan or the recorded assignment; never inferred from rank or leadership. */
export const supervisedBy=(row:{supervision?:StaffingSupervisionPlan|null;supervisor_person_id?:string|null;[key:string]:unknown},name:(id:string)=>string=()=>'')=>{
  const plan=row.supervision,id=plan?.proposal?.supervisor_person_id||row.supervisor_person_id;
  return id?plan?.choices.find(x=>x.supervisor_person_id===id)?.display_name||name(id):'';
};

/** Supervision teams on one session (SSOT for every Schedule view, the session panel and the legacy shift card).
 *  A team is only what is recorded: a participant whose `supervisor_person_id` names someone on the same session sits
 *  beneath that person. Rank, mentorship and team lead never imply supervision, and sessions are never merged. */
type Paired={person_id?:string;supervisor_person_id?:string|null;[key:string]:unknown};
export type TeamEntry<T extends Paired>={p:T;
  /** 0: heads a team or stands alone; 1+: beneath their named supervisor. */depth:number;
  /** Names a supervisor who is not on this session (validation still reports it; the view says it). */missing:boolean;
  /** Distinct trainees beneath this participant. */trainees:number};
/** Supervises on this session (the S mark, every view): recorded as its supervisor, or anyone on the session names them
 *  as theirs (the tree hangs them beneath), whatever their own participation says. */
export const supervises=(p:Paired,participants:readonly Paired[])=>p.participation==='supervisor'||
  participants.some(x=>x.person_id!==p.person_id&&x.supervisor_person_id===p.person_id);
/** The session's participants in team order: each supervisor once, their named trainees right beneath them. Partial
 *  attendance rows of one person stay separate entries (their own times); a supervisor with two rows heads the team once.
 *  `roster` is the whole session when `participants` is only what a person display draws (drawnPeople): absence is
 *  judged on the session, never on the drawing, so a hidden supervisor is never said to be missing. */
export function teamOrder<T extends Paired>(participants:readonly T[],roster:readonly Paired[]=participants):TeamEntry<T>[]{
  const here=new Set(participants.map(p=>p.person_id)),onSession=new Set(roster.map(p=>p.person_id));
  const under=(p:T)=>!!p.supervisor_person_id&&p.supervisor_person_id!==p.person_id&&here.has(p.supervisor_person_id);
  const out:TeamEntry<T>[]=[],seen=new Set<T>(),headed=new Set<string>();
  const visit=(p:T,depth:number)=>{if(seen.has(p))return;seen.add(p);
    const kids=headed.has(p.person_id!)?[]:participants.filter(x=>x!==p&&x.supervisor_person_id===p.person_id&&under(x));headed.add(p.person_id!);
    out.push({p,depth,missing:!!p.supervisor_person_id&&p.supervisor_person_id!==p.person_id&&!onSession.has(p.supervisor_person_id),trainees:new Set(kids.map(k=>k.person_id)).size});
    for(const k of kids)visit(k,depth+1);};
  for(const p of participants)if(!under(p))visit(p,0);
  // A recorded cycle (A names B, B names A) has no head: each is still listed once.
  for(const p of participants)visit(p,0);
  return out;
}
/** One line for a session's people in team order: "Dr A (supervising B, C) · D · E (with Dr X, not on this session)".
 *  `label` adds a participant's own marks (TL, times). Accessible text keeps every relationship in words. */
export const teamText=<T extends Paired>(participants:readonly T[],nameOf:(id:string)=>string,label:(p:T)=>string=()=>'',roster:readonly Paired[]=participants):string=>teamParts(participants,nameOf,label,roster).join(' · ');
/** teamOrder split into teams: each head (depth 0) with everyone beneath it, each person once (a Week/Month pill). */
export function teamGroups<T extends Paired>(participants:readonly T[],roster:readonly Paired[]=participants):TeamEntry<T>[][]{
  const out:TeamEntry<T>[][]=[];
  for(const e of teamOrder(participants,roster))if(e.depth&&out.length)out[out.length-1].push(e);else out.push([e]);
  return out;
}
/** teamText's parts: one per team or unpaired person (a List row's pills). A head whose supervisor is on the session
 *  but not drawn (a person display) is said to be with them, never "not on this session". */
export function teamParts<T extends Paired>(participants:readonly T[],nameOf:(id:string)=>string,label:(p:T)=>string=()=>'',roster:readonly Paired[]=participants):string[]{
  const said=(e:TeamEntry<T>)=>nameOf(e.p.person_id!)+label(e.p),drawn=new Set(participants.map(p=>p.person_id));
  return teamGroups(participants,roster).map(([e,...kids])=>{const sup=e.p.supervisor_person_id;
    return said(e)+(kids.length?` (supervising ${kids.map(said).join(', ')})`:'')+(e.missing?` (with ${nameOf(sup!)}, not on this session)`:sup&&sup!==e.p.person_id&&!drawn.has(sup)?` (with ${nameOf(sup)})`:'');});
}
/** The supervision team one assignment belongs to (People › Group by Supervision team, CA-03): its named supervisor
 *  when they are on the same session, itself when trainees name it there, otherwise none (''). Team lead is not
 *  supervision. Read per assignment, so a trainee with different supervisors on different days is in each team. */
export function teamOf(participants:readonly Paired[],p:Paired):string{
  if(p.supervisor_person_id&&p.supervisor_person_id!==p.person_id&&participants.some(x=>x.person_id===p.supervisor_person_id))return p.supervisor_person_id;
  return participants.some(x=>x!==p&&x.supervisor_person_id===p.person_id)?p.person_id||'':'';
}

/** Who a person display keeps on one session (PlannerViewSettings.person_display; display only, the session and its
 *  checks keep everyone): `duty` everyone, `person` the person's own rows, `team` the person with their recorded
 *  same-session supervision team (teamOf: their named supervisor and that supervisor's trainees here, or their own
 *  trainees), else the person alone. A session the person is not on is kept whole (it matched another way). */
export function shownPeople<T extends Paired>(participants:readonly T[],person:string,display:'person'|'team'|'duty'):T[]{
  const mine=participants.filter(p=>p.person_id===person);
  if(display==='duty'||!mine.length)return [...participants];
  if(display==='person')return mine;
  const teams=new Set(mine.map(p=>teamOf(participants,p)).filter(Boolean));
  return participants.filter(p=>p.person_id===person||teams.has(teamOf(participants,p)));
}

// ---- Lead context (TL-03): the server's dated answer to "who may lead this duty" -------------------------------------
/** Read-only; the server resolves the duty type, rules and dates (rotation, cohort and programme boundaries included),
 *  so the client never evaluates lead conditions itself. Saving still goes through the validated shift edit. */
// Only identical questions in flight share one answer: a settled answer is never reused, because who may lead also
// depends on rules, people and calendars that change without a new rota revision (the server knows; the client cannot).
const leadAsking=new Map<string,Promise<LeadContext>>();
export function leadContext(workspaceId:string,revision:number,input:LeadContextInput):Promise<LeadContext>{
  const key=`${workspaceId}:${revision}:${JSON.stringify(input)}`;
  let hit=leadAsking.get(key);
  if(!hit){hit=request<LeadContext>(`/api/workspaces/${encodeURIComponent(workspaceId)}/lead-context`,{method:'POST',body:JSON.stringify(input)})
    .finally(()=>leadAsking.delete(key));leadAsking.set(key,hit);}
  return hit;
}
/** `failed` keeps the caught error itself, so its notice can offer Report this problem for an unexpected fault. */
export type LeadState={state:'pending'}|{state:'failed';error:Error}|{state:'stale'}|{state:'ready';context:LeadContext};
/** Whether an answer still describes the draft: one for an older rota revision is stale and enables nothing. */
export const leadState=(context:LeadContext,revision:number):LeadState=>context.workspace_revision===revision?{state:'ready',context}:{state:'stale'};
/** A person's lead permission in a ready context: null when they were not asked about. */
export function canLead(s:LeadState,personId:string):boolean|null{
  if(s.state!=='ready')return null;
  const p=s.context.people.find(x=>x.person_id===personId);return p?p.can_lead:null;
}
/** Whether this duty asks for a team lead at all (some enabled segment), and its fewest/most. */
export function leadNeed(c:LeadContext){
  const on=c.segments.filter(x=>x.enabled);
  return {enabled:on.length>0,minimum:Math.max(0,...on.map(x=>x.minimum)),maximum:!on.length||on.some(x=>x.maximum==null)?null:Math.min(...on.map(x=>x.maximum!)),
    wholeDuty:on.some(x=>x.continuity==='whole_duty'),conflict:on.some(x=>x.contradictory_bounds||x.empty_pool)};
}
