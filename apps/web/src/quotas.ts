import {el, note, badge, icon, dateUnits, formatDate,emptyLine} from './dom.js';
import {addDays} from './planner_logic.js';
import {prorated} from './findings.js';
import {dataTable} from './table_view.js';

export interface QuotaRow {
  policy_id:string; policy_revision:number; policy_name:string; person_id:string; display_name:string;
  starts_on:string; ends_on:string; metric:string; comparison:string; amount:number; actual:number;
  target_calculation?:{default_amount:number;reference_days:number;department_days:number;calculated_amount:number;rounding:string;override_amount?:number;override_reason?:string};
  /** The server's working-day proration of this amount (quotas.py), shown as it was calculated. */
  proration?:{configured_amount:number;amount:number;working_days:number;full_working_days:number}|null;
  difference:number; status:string; severity:string; overtime:string;
}
export interface QuotaReport {metric_version:string; timezone:string; basis:string; complete:boolean; rows:QuotaRow[]; empty_policy_ids?:string[]}
export const quotaUnits:Record<string,string>={duties:'Duties',session_units:'Session units',scheduled_hours:'Planned presence hours',reserved_hours:'Reserved hours',workload_credit:'Workload credits',training_credit:'Planned educational credits',lead_turns:'Team lead turns'};

/** Same server-issued rows in planning, review and immutable publication reports. `id` keeps each place's list view
 *  (filters, sort, saved views) its own. */
export function quotaPanel(report:QuotaReport, fresh=true, id='quotas'):HTMLElement {
  // Nothing to show: one line instead of an empty table and its filters.
  if(!report.rows.length&&report.complete&&fresh&&!report.empty_policy_ids?.length)
    return el('section',{class:'panel'},el('h3',{},'Targets and limits'),emptyLine('No workload targets or limits apply to this period.'));
  const panel=el('section',{class:'panel'},el('h3',{class:'quota-head'},'Targets and limits'),el('button',{type:'button',class:'icon-button bare','aria-label':'How this is calculated','data-help':'','data-tip':report.basis+' Times in '+report.timezone+'.'},icon('help')));
  if(!fresh)panel.append(note('This calculation is stale. Revalidate against current inputs before making a scheduling decision.','warning'));
  if(!report.complete)panel.append(note('This calculation is incomplete. Targets and limits cannot yet be confirmed.','error'));
  if(report.empty_policy_ids?.length)panel.append(note(report.empty_policy_ids.length===1?'One target or limit applies to nobody in its period, so it was not checked.':`${report.empty_policy_ids.length} targets or limits apply to nobody in their period, so they were not checked.`,'warning'));
  const result=(r:QuotaRow)=>r.status.replaceAll('_',' ');
  const calc=(r:QuotaRow)=>r.target_calculation?`${r.target_calculation.default_amount} per ${r.target_calculation.reference_days} days × ${r.target_calculation.department_days} departmental days = ${r.target_calculation.calculated_amount}${r.target_calculation.override_amount!==undefined?' → adjusted to '+r.target_calculation.override_amount+(r.target_calculation.override_reason?' · '+r.target_calculation.override_reason:''):''}`:r.proration?`${r.proration.configured_amount} prorated by working days`:'Fixed amount';
  const rows=report.rows.map((r,i)=>({...r,key:String(i)}));
  panel.append(dataTable<QuotaRow&{key:string}>({id,noun:['commitment','commitments'],rows,rowId:r=>r.key,searchLabel:'Search commitments',searchPlaceholder:'Person or rule',
    columns:[
      {key:'person',label:'Person',search:'related',text:r=>r.display_name,cell:r=>r.display_name},
      {key:'rule',label:'Rule / version',search:'related',text:r=>r.policy_name,cell:r=>`${r.policy_name} · v${r.policy_revision}`},
      // Stored end-exclusive; shown through the included final day.
      {key:'block',label:'Period',sort:r=>r.starts_on,cell:r=>dateUnits([formatDate(r.starts_on,'UTC'),'– '+formatDate(addDays(r.ends_on,-1),'UTC')])},
      {key:'measure',label:'Measure',cell:r=>quotaUnits[r.metric]||r.metric},
      {key:'kind',label:'Kind',cell:r=>r.comparison},
      {key:'calc',label:'Target calculation',cell:calc},
      {key:'amount',label:'Amount',num:true,sort:r=>r.amount,cell:r=>`${r.amount}${prorated(r.proration)}`},
      {key:'actual',label:'Scheduled amount',num:true,sort:r=>r.actual,cell:r=>String(r.actual)},
      {key:'difference',label:'Difference',num:true,sort:r=>r.difference,cell:r=>String(r.difference)},
      {key:'status',label:'Result',facet:result,sort:result,cell:r=>badge(result(r),!fresh||!report.complete?'warn':r.status==='met'?'ok':r.severity==='mandatory'&&r.status!=='excepted'?'err':'warn')},
      {key:'overtime',label:'Overtime',cell:r=>r.overtime},
    ]}).root);
  return panel;
}
