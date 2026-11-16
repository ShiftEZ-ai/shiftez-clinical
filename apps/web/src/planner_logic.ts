/** Pure planner projection functions. Server records remain the only authority. */
import {labelOf,toLocal,WEEKEND_DAYS} from './dom.js';
import type {Row,Catalogues} from './types.js';
import {PLANNER_LAYER_DEFAULTS,type PlannerViewDefinition,type PlannerLayers} from './generated.js';

/** The Schedule's Layers before anyone changes them (the contract's PLANNER_LAYER_DEFAULTS): a draft shown (which one
 *  the Schedule chooses), leave and wishes shown, everyone's duties, one draft to edit. */
export const LAYER_DEFAULTS:Required<PlannerLayers>=PLANNER_LAYER_DEFAULTS;
/** Layers as a saved view keeps them (PlannerViewSettings.layers): null when all are at their defaults, so a view saved
 *  before layers were kept compares equal. Whether a draft shows, never which rota or publication. */
export function savedLayers(l:PlannerLayers|null|undefined):Required<PlannerLayers>|null{
  const v={draft:l?.draft??LAYER_DEFAULTS.draft,leave:l?.leave??LAYER_DEFAULTS.leave,mine:l?.mine??LAYER_DEFAULTS.mine,mode:l?.mode??LAYER_DEFAULTS.mode};
  return (Object.keys(v) as (keyof typeof v)[]).every(k=>v[k]===LAYER_DEFAULTS[k])?null:v;
}

export const isoDay=(date:Date)=>date.toISOString().slice(0,10);
// Configured weekend: Friday and Saturday (JS getUTCDay 5 and 6).
/** A department weekend day (Configuration › Department settings). */
export const isWeekend=(day:string)=>WEEKEND_DAYS.includes((new Date(day+'T12:00:00Z').getUTCDay()+6)%7);
export const addDays=(day:string,count:number)=>{const d=new Date(day+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+count);return isoDay(d);};
/** A new grant for [from, until) (until exclusive; null: no end) against existing grants of the same role: 'covered' when
 *  one spans the whole period, 'overlap' when one meets part of it, '' when the dates are free. Revoked grants never count. */
export const grantOverlap=(grants:Row[],from:string,until:string|null):'covered'|'overlap'|''=>{const held=grants.filter(b=>!b.revoked);
  return held.some(b=>b.starts_on<=from&&(!b.ends_on||!!until&&b.ends_on>=until))?'covered'
    :held.some(b=>(!b.ends_on||from<b.ends_on)&&(!until||b.starts_on<until))?'overlap':'';};
/** Whether dated memberships [starts_on, ends_on) together cover the whole [start, end): adjacent and overlapping rows join,
 *  a gap does not. The server's catalogue.membership_covers (calendar confirmed_members, rotations), on ISO dates. */
export function membershipCovers(rows:{starts_on:string;ends_on:string}[],start:string,end:string):boolean{
  let covered=start;
  for(const r of [...rows].sort((a,b)=>a.starts_on.localeCompare(b.starts_on)||a.ends_on.localeCompare(b.ends_on))){
    if(r.starts_on>covered)break;
    if(r.ends_on>covered)covered=r.ends_on;}
  return covered>=end;
}
const monthNumber=(day:string)=>Number(day.slice(0,4))*12+Number(day.slice(5,7))-1;

export function touchesDay(shift:Row,day:string,zone:string):boolean{
  // Convert end minus one millisecond so a midnight-exclusive end does not add a day.
  const first=toLocal(shift.start,zone).slice(0,10);
  const last=toLocal(new Date(Date.parse(shift.end)-1).toISOString(),zone).slice(0,10);
  return first<=day&&day<=last;
}

/** How much of the rota one view shows. Every layout pages through the rota in these steps. */
export type PlanRange='day'|'week'|'2weeks'|'month'|'rota';
/** How an empty range is named: "this month/week/day"; a multi-unit or custom range is "this period", the whole rota "this rota". */
export const rangeWords=(unit:string)=>['month','week','day'].includes(unit)?{where:`this ${unit}`,other:`another ${unit}`}
  :{where:unit==='rota'?'in this rota':'in this period',other:'another period'};
const UNIT:Record<PlanRange,string>={day:'day',week:'week','2weeks':'2 weeks',month:'month',rota:'rota'};

/** The days one page of a view covers, always trimmed to the rota (end date exclusive). Weeks start on the department's first weekday. */
export function calendarPeriod(workspace:Row,range:PlanRange,page:number,weekStart=6){
  const inRota=(day:string)=>day>=workspace.starts_on&&day<workspace.ends_on,unit=UNIT[range]||'week';
  const span=(start:string,length:number)=>Array.from({length},(_,index)=>addDays(start,index)).filter(inRota);
  if(range==='rota')return {page:0,has_previous:false,has_next:false,label:workspace.starts_on,days:span(workspace.starts_on,Math.max(1,Math.round((Date.parse(workspace.ends_on)-Date.parse(workspace.starts_on))/86400000))),unit};
  if(range==='month'){
    const max=monthNumber(addDays(workspace.ends_on,-1))-monthNumber(workspace.starts_on);
    const bounded=Math.min(max,Math.max(0,page));
    const first=new Date(workspace.starts_on.slice(0,7)+'-01T12:00:00Z');first.setUTCMonth(first.getUTCMonth()+bounded);
    const next=new Date(first);next.setUTCMonth(next.getUTCMonth()+1);
    return {page:bounded,has_previous:bounded>0,has_next:bounded<max,label:isoDay(first).slice(0,7),days:span(isoDay(first),Math.round((next.getTime()-first.getTime())/86400000)),unit};
  }
  const length=range==='day'?1:range==='2weeks'?14:7;
  // Days start on the rota's first day; weeks on the department's first weekday (Sunday here), trimmed to the rota.
  const lead=range==='day'?0:(new Date(workspace.starts_on+'T12:00:00Z').getUTCDay()-((weekStart+1)%7)+7)%7,aligned=addDays(workspace.starts_on,-lead);
  const max=Math.max(0,Math.ceil((Date.parse(workspace.ends_on)-Date.parse(aligned))/(length*86400000))-1);
  const bounded=Math.min(max,Math.max(0,page)),days=span(addDays(aligned,bounded*length),length);
  return {page:bounded,has_previous:bounded>0,has_next:bounded<max,label:days[0],days,unit};
}
/** The page of a range that shows a day, so switching Week → Month keeps the same dates in view. */
export function pageOf(workspace:Row,range:PlanRange,day:string,weekStart=6){
  for(let page=0;page<1000;page++){const p=calendarPeriod(workspace,range,page,weekStart);if(!p.has_next||(p.days.at(-1)||'')>=day)return p.page;}
  return 0;
}

export function filterShifts(shifts:Row[],catalogues:Catalogues,filters:PlannerViewDefinition):Row[]{
  const label=(kind:string,id:string)=>labelOf((catalogues[kind]||[]).find(row=>row.id===id));
  const search=(filters.search||'').toLocaleLowerCase();
  return shifts.filter(shift=>(!filters.location_id||shift.location_id===filters.location_id)
    &&(!filters.assignment_type_id||shift.assignment_type_id===filters.assignment_type_id)
    &&(!filters.person_id||shift.participants.some((p:Row)=>p.person_id===filters.person_id))
    &&(!search||[shift.type_snapshot.name,label('locations',shift.location_id),
      ...shift.service_ids.map((id:string)=>label('services',id)),
      ...shift.participants.map((p:Row)=>label('people',p.person_id))].join(' ').toLocaleLowerCase().includes(search)));
}

/** A session nobody works: closed by a coordinator (cancelled), or closed because every usual assignee of its recurring
 *  session is away for all of it (`closed`, derived by the server: packages/domain/recurrence.py series_state). */
export const isClosed=(s:Row)=>!!(s.cancelled||s.closed);
/** Who is absent from a session (SW3-C-02): leave or rotation findings on it, and the viewer's own leave approved after
 *  publication (cover being arranged). The one answer for every layout, its panel and the CSV drawn from the List.
 *  `findings`: the session's own, or any (others are skipped). */
export const ABSENCE_CODES=['UNAVAILABLE','EXTERNAL_ROTATION'];
export const absentFrom=(s:Row,findings:readonly object[],me?:string|null)=>{
  const out=new Set<string>((findings as Row[]).filter(f=>f.shift_id===s.id&&ABSENCE_CODES.includes(String(f.code))).map(f=>String(f.person_id)));
  if(s.leave_pending&&me)out.add(me);
  // G-02: a published session's own flags: approved absence (its live conflicts) and a reported Can't make it.
  for(const c of (s.double_booking_conflicts||[]) as Row[])if(ABSENCE_CODES.includes(String(c.code)))out.add(String(c.person_id));
  for(const r of (s.absence_reports||[]) as Row[])out.add(String(r.person_id));
  return out;};
/** Why a session is closed, in words ("Closed: every usual assignee is away · Dr A (on leave)"); '' when open. */
export function closureText(s:Row,name:(id:string)=>string):string{
  if(s.cancelled)return s.cancellation_reason||'Closed';
  if(!s.closed)return '';
  const away=((s.usual_assignee_unavailability||[]) as Row[]).map(r=>`${name(r.person_id)} (${((r.codes||[]) as string[]).map(c=>c==='EXTERNAL_ROTATION'?'on rotation':'unavailable').join(', ')})`);
  return 'Closed: every usual assignee is away'+(away.length?' · '+away.join(', '):'');
}
/** Whether a session of this duty type needs a clinic: the server's rule (planning.py SERVICE_REQUIRED, a staffed duty),
 *  read from the type's definition (TypeDefinition.purpose, "staffed" when unset, as before a type is chosen). */
export const needsClinic=(type:Row|null|undefined)=>(type?.definition?.purpose||'staffed')==='staffed';
