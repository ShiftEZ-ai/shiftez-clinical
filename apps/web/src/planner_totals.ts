import {PLANNER_TOTAL_MEASURES,type PlannerTotal,type TimeOfDay} from './generated.js';
import {labelOf,toLocal,type Choice} from './dom.js';
import {addDays,isWeekend} from './planner_logic.js';
import {timeOfDayAt} from './times_of_day.js';
import type {QuotaRow} from './quotas.js';
import type {Row} from './types.js';

/** People › Totals (PlannerViewSettings.totals): the view's own list in its order, or Hours alone. */
export const totalsOf=(saved:readonly PlannerTotal[]|null|undefined):PlannerTotal[]=>
  saved?.length?saved.map(t=>({key:t.key,on:t.on!==false})):[{key:'hours',on:true}];
/** What a total can count: the department's times of day and the duty types. */
export interface TotalSource{times:readonly TimeOfDay[];types:readonly Row[]}
const MEASURES=PLANNER_TOTAL_MEASURES as Record<string,string>;
/** A total's column name; '' when its time of day or duty type no longer exists (kept in the view, not drawn). */
export function totalName(key:string,src:TotalSource):string{
  if(MEASURES[key])return MEASURES[key];
  if(key.startsWith('time:'))return src.times.find(t=>t.code===key.slice(5))?.name||'';
  const type=key.startsWith('type:')?src.types.find(t=>t.id===key.slice(5)):undefined;
  return type?labelOf(type):'';}
/** The group a total is added from (Add total). */
export const totalKind=(key:string)=>MEASURES[key]?'Measures':key.startsWith('time:')?'Times of day':'Duty types';
export const totalChoices=(src:TotalSource):Choice[]=>[...Object.keys(MEASURES),...src.times.map(t=>'time:'+t.code),...src.types.map(t=>'type:'+t.id)]
  .map(value=>({value,label:totalName(value,src),group:totalKind(value)}));

/** A person's count of one total over their duties in the view (cancelled sessions never count). */
export function totalCount(key:string,duties:readonly {shift:Row;p:Row}[],zone:string,times:readonly TimeOfDay[]):number{
  const live=duties.filter(x=>!x.shift.cancelled);
  if(key==='hours')return live.reduce((a,x)=>a+(Date.parse(x.p.end)-Date.parse(x.p.start))/3600000,0);
  if(key==='weekend_days')return new Set(live.map(x=>toLocal(x.shift.start,zone).slice(0,10)).filter(isWeekend)).size;
  if(key.startsWith('time:'))return live.filter(x=>timeOfDayAt(times,toLocal(x.shift.start,zone).slice(11,16)).code===key.slice(5)).length;
  if(key.startsWith('type:'))return live.filter(x=>x.shift.assignment_type_id===key.slice(5)).length;
  return live.length;}

const HOURS=new Set(['reserved_hours','scheduled_hours']);
/** The total a quota row measures: Hours, Duties, or one duty type's duties ('' for any other quota). */
export const quotaMeasure=(r:QuotaRow)=>{const ids=r.assignment_type_ids||[];
  return HOURS.has(r.metric)&&!ids.length?'hours':r.metric!=='duties'?'':!ids.length?'duties':ids.length===1?'type:'+ids[0]:'';};
/** The person's quota in the rota's rules (the server's quota report) that this total measures over exactly the view's
 *  days; a maximum or target before a minimum. Times of day and weekend days have no quota measure. */
export function quotaOf(key:string,rows:readonly QuotaRow[],pid:string,days:readonly string[]):QuotaRow|undefined{
  if(!days.length)return undefined;
  const until=addDays(days[days.length-1],1);
  // shortcut: a quota whose scope narrows the counted duties (a place, a time) still matches its measure; its own count is
  // shown with it, so the pair agrees. Upgrade when the report says which rows count every duty of the measure.
  const found=rows.filter(r=>r.person_id===pid&&r.starts_on===days[0]&&r.ends_on===until&&quotaMeasure(r)===key);
  return found.find(r=>r.comparison!=='minimum')||found[0];}
/** Over the quota's maximum or target, as the server's check says. */
export const quotaOver=(r:QuotaRow|undefined)=>r?.status==='above_maximum'||r?.status==='above_target';
/** A total's cell words: "12 h", "12 / 40 h", "3", "3 / 4". */
export const totalText=(key:string,n:number,target?:number)=>
  `${key==='hours'?Math.round(n):Math.round(n*100)/100}${target===undefined?'':' / '+target}${key==='hours'?' h':''}`;
