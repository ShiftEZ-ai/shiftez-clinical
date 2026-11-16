/** Draft Snapshot words and order, one place for New rota and Refresh draft baseline. Pure, for Node tests. */
import type {WorkspaceBaselineScope} from './generated.js';

export type BaselineSource=NonNullable<WorkspaceBaselineScope['sources']>[number];
/** The groups a new draft can freeze, by name. People and calendars stay live in every draft (the server overlays them
 *  and never reports them as changes), so they are not offered. */
export const BASELINE_SOURCES:[BaselineSource,string][]=[
  ['duties','Duties and places'],
  ['rules','Rules and adjustments'],
  ['score','Score and fairness'],
  ['auto','Auto-schedule settings'],
  ['receipts','Scheduler version'],
  ['display','Display defaults'],
];
export const baselineSourceLabel=(source:string)=>BASELINE_SOURCES.find(s=>s[0]===source)?.[1]||source;
/** A refresh row's state (workspace_baselines._diff) in words; a conflict on mandatory safety rules says so. */
const CHANGE:Record<string,string>={department:'Department changed',draft:'Changed in this draft',same:'Same change in both',conflict:'Conflict'};
export const changeLabel=(c:{status:string;safety?:boolean})=>c.status==='conflict'&&c.safety?'Conflict · mandatory safety':CHANGE[c.status]||c.status;
export const SAFETY_KEEP='mandatory safety rules stay with the department';
/** A safety conflict has one outcome: said as its decision, never a picker with one usable value. */
export const SAFETY_DECIDED=`Use department · ${SAFETY_KEEP}`;
export const UNDECIDED='Choose an outcome for each conflict';
/** What Refresh draft baseline offers: nothing to apply without a department change; a decision for each conflict. */
export function refreshPlan(changes:{status:string}[]):{empty:string;decide:boolean;apply:boolean}{
  if(!changes.some(c=>c.status!=='draft'))return {empty:'No department changes since this draft was created',decide:false,apply:false};
  return {empty:'',decide:changes.some(c=>c.status==='conflict'),apply:true};
}
/** Uncovered dates said as warnings beside New rota's dates; the rest fold into one quiet line. */
export const GAPS_SHOWN=2;
/** Days in no rota, nearest to the chosen days [from, to) first (ISO days, `to` exclusive); ties by start. */
export function nearestGaps<G extends {starts_on:string;ends_on:string}>(gaps:G[],from:string,to:string):G[]{
  const away=(g:G)=>g.ends_on<=from?Date.parse(from)-Date.parse(g.ends_on):Math.max(0,Date.parse(g.starts_on)-Date.parse(to));
  return [...gaps].sort((x,y)=>away(x)-away(y)||x.starts_on.localeCompare(y.starts_on));
}
