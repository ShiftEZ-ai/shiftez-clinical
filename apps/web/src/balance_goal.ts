import {el,badge,field,select,multiPick,button,note,table,type Choice} from './dom.js';
import {working} from './loading.js';import {dateRange,lastDayIncluded} from './date_picker.js';
import {request} from './api.js';
import {conditionBuilder,type ConditionField} from './conditions.js';
import type {PersonAmount,BalanceGoal,FairnessViewSettings} from './generated.js';

/** The Balance goal sentence: "Count WHAT for WHO over PERIOD aiming for AIM, strength S" (who is the rule scope). */
/** Every measure a fair share goal or the Fairness page counts, named once (the contract's enums: a measure added there
 *  fails the build until it is named here). Sentences use them in lower case. */
export const METRIC_WORDS:Record<BalanceGoal['metric']|NonNullable<FairnessViewSettings['metric']>,string>={duties:'Duties',hours:'Hours',session_units:'Session units',
  weekends:'Weekends worked',overtime_duties:'Overtime duties',overtime_hours:'Overtime hours',lead_turns:'Team lead turns',supervision:'Trainee sessions supervised',
  pair_exposure:'Duties shared with a partner',workload:'Workload',wishes_granted:'Wish points granted',wishes_declined:'Wish points declined',wishes_submitted:'Wish points asked for'};
/** The measures the Balance goal sentence offers (workload has its own built-in goal). */
export const GOAL_METRICS:Choice[]=(['duties','hours','session_units','weekends','overtime_duties','overtime_hours','lead_turns','supervision','pair_exposure'] as const)
  .map(value=>({value,label:METRIC_WORDS[value].toLowerCase()}));
export const GOAL_PERIODS:Choice[]=[{value:'rota',label:'this rota'},{value:'rolling',label:'the last N days'},{value:'month',label:'the calendar month'},
  {value:'quarter',label:'the quarter'},{value:'year',label:'the year'},{value:'block',label:'each person’s rotation block'},{value:'custom',label:'these dates'}];
const AIMS:Choice[]=[{value:'equal_share',label:'an equal share'},{value:'target',label:'a target each'},{value:'max_min',label:'lifting whoever is furthest below their share'}];
const PAIR_AIM:Choice[]=[{value:'min_per_pair',label:'at least N with each partner'}];
/** Advisory strength, shared with the rule form. */
export const STRENGTHS:Choice[]=['low','medium','high','critical'].map(v=>({value:v,label:v[0].toUpperCase()+v.slice(1)}));
/** Offered hours share only overtime (the contract refuses it elsewhere); a new overtime goal suggests it. */
const SHARES:Choice[]=[{value:'available_days',label:'days available in the department'},{value:'equal',label:'equally per person'},{value:'offered',label:'overtime each person offered'}];
const OVERTIME=['overtime_duties','overtime_hours'];
const label=(list:Choice[],value:string)=>list.find(c=>c.value===value)?.label||value;

/** What a goal counts and over when, in words: "trainee sessions supervised over the calendar month". */
export const goalMeasure=(rule:Record<string,any>)=>`${goalMetric(rule.metric)} over ${goalOver(rule)}`;
export const goalMetric=(metric:string)=>(METRIC_WORDS[metric as keyof typeof METRIC_WORDS]||metric).toLowerCase();
const goalOver=(rule:Record<string,any>)=>rule.period==='rolling'?`the last ${rule.period_days} days`:rule.period==='custom'?`${rule.starts_on} – ${rule.ends_on}`:label(GOAL_PERIODS,rule.period||'rota');
/** One plain sentence for a saved goal (rule lists and summaries). */
export function goalSentence(rule:Record<string,any>,name:(kind:string,id:string)=>string):string{
  const what=goalMetric(rule.metric)+(rule.assignment_type_ids?.length?' of '+rule.assignment_type_ids.map((id:string)=>name('assignment_types',id)).join(', '):'')
    +(rule.service_ids?.length?' at '+rule.service_ids.map((id:string)=>name('services',id)).join(', '):'');
  const over=goalOver(rule);
  const own=Object.keys(rule.person_amounts||{}).length,others=own?` (${own} ${own===1?'person has':'people have'} their own)`:'';
  const aim=rule.aim==='min_per_pair'?`at least ${rule.amount} with each partner${others}`:rule.aim==='target'?`a target of ${rule.amount??'a set amount'} each${others}`
    :rule.aim==='max_min'?'lifting whoever is furthest below their share':`an equal share${rule.share_by==='equal'?'':rule.share_by==='offered'?' (pro rata to overtime offered)':' (pro rata to days available)'}`;
  return `Count ${what} over ${over}, aiming for ${aim}. History from published rotas counts.`;
}

/** The sentence builder. `strength` is the form's own strength select, kept in step both ways. `perPerson` makes the
 *  rule-template per-person table (forms.ts perPersonEditor) for own targets. */
export function goalBuilder(given:Record<string,any>,choices:{types:Choice[];services:Choice[];partners:ConditionField[]},strength:HTMLSelectElement,
  perPerson:(rows:PersonAmount[])=>{wrap:HTMLElement;read:()=>PersonAmount[]}){
  const g:Record<string,any>={metric:'duties',period:'month',aim:'equal_share',share_by:'available_days',pairing:'present',weekend_start:4,weekend_days:2,...given};
  if(!given.share_by&&OVERTIME.includes(g.metric))g.share_by='offered';
  const metric=select('Count',GOAL_METRICS,g.metric);
  const types=multiPick('Duty types',choices.types.map(c=>({...c,tone:'place'})),g.assignment_type_ids||[],undefined,'any duty type');
  const services=multiPick('Clinics',choices.services.map(c=>({...c,tone:'place'})),g.service_ids||[],undefined,'any clinic');
  const period=select('Over',GOAL_PERIODS,g.period);
  const days=field('Days','number',g.period_days||28,{min:1,max:366,step:1,required:true});
  const from=field('From','date',g.starts_on||''),until=field('Until (included)','date',g.ends_on||'');lastDayIncluded(until.input);const dates=dateRange('Dates',from,until);
  const aim=select('Aiming for',g.metric==='pair_exposure'?PAIR_AIM:AIMS,g.aim);
  const amount=field('Amount','number',g.amount??'',{min:0,max:100000,step:'any'});
  const own=perPerson(Object.entries(g.person_amounts||{}).map(([person_id,amount])=>({person_id,amount:Number(amount)})));
  const share=select('Share by',SHARES,g.share_by);
  const pairing=select('Together means',[{value:'present',label:'on the same session'},{value:'supervised',label:'named supervisor'}],g.pairing);
  const weekendStart=select('Weekend starts',['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'].map((l,i)=>({value:String(i),label:l})),String(g.weekend_start));
  const weekendDays=field('Weekend days','number',g.weekend_days,{min:1,max:3,step:1,required:true});
  const partners=conditionBuilder(choices.partners,g.partners||null);
  const ownStrength=select('Strength',STRENGTHS,strength.value||'medium');
  ownStrength.input.addEventListener('change',()=>{strength.value=ownStrength.input.value;strength.dispatchEvent(new Event('change',{bubbles:true}));});
  strength.addEventListener('change',()=>{if(ownStrength.input.value!==strength.value)ownStrength.input.value=strength.value;});
  const word=(text:string)=>el('span',{class:'goal-word'},text);
  const partnerRow=el('div',{class:'goal-line span-2'},word('with partners'),el('div',{class:'audience-conditions'},partners.root),pairing.wrap);
  const weekendRow=el('div',{class:'goal-line'},weekendStart.wrap,weekendDays.wrap);
  const root=el('div',{class:'goal-sentence span-2','data-goal-builder':''},
    el('div',{class:'goal-line'},metric.wrap,el('div',{class:'field'},el('span',{class:'pick-label'},'of duty types'),types.wrap),el('div',{class:'field'},el('span',{class:'pick-label'},'or at clinics'),services.wrap)),
    weekendRow,partnerRow,
    el('div',{class:'goal-line'},word('for the people in Scope, over'),period.wrap,days.wrap,dates),
    el('div',{class:'goal-line'},aim.wrap,amount.wrap,share.wrap,ownStrength.wrap),own.wrap,
    note('History from published rotas (the fairness ledger) counts toward the period. Shares are pro rata to days in the department, excluding leave and away rotations, or to the hours each person offered for overtime.'));
  const sync=()=>{
    const pair=metric.input.value==='pair_exposure';
    const wanted=pair?PAIR_AIM:AIMS;
    if(!wanted.some(c=>c.value===aim.input.value)){aim.input.replaceChildren(...wanted.map(c=>el('option',{value:c.value},c.label)));aim.input.value=wanted[0].value;}
    partnerRow.hidden=!pair;weekendRow.hidden=metric.input.value!=='weekends';
    days.wrap.hidden=period.input.value!=='rolling';days.input.disabled=days.wrap.hidden;
    dates.hidden=period.input.value!=='custom';from.input.required=until.input.required=!dates.hidden;
    amount.wrap.hidden=!['target','min_per_pair'].includes(aim.input.value);amount.input.required=!amount.wrap.hidden;own.wrap.hidden=amount.wrap.hidden;
    share.wrap.hidden=!['equal_share','max_min'].includes(aim.input.value);
    const overtime=OVERTIME.includes(metric.input.value),offered=share.input.querySelector<HTMLOptionElement>('option[value="offered"]')!;
    offered.hidden=offered.disabled=!overtime;if(!overtime&&share.input.value==='offered')share.input.value='available_days';
  };
  for(const input of [metric.input,period.input,aim.input])input.addEventListener('change',sync);
  metric.input.addEventListener('change',()=>{if(OVERTIME.includes(metric.input.value))share.input.value='offered';});
  sync();
  // A goal counts duty types or clinics, not both: choosing one closes the other.
  const trigger=(p:{wrap:HTMLElement})=>p.wrap.querySelector<HTMLButtonElement>('.pick-button')!;
  const close=(b:HTMLButtonElement,shut:boolean,tip:string)=>{b.disabled=shut;if(shut)b.dataset.tip=tip;else delete b.dataset.tip;};
  const exclusive=()=>{close(trigger(services),types.values().length>0,'Clear the duty types to count clinics instead');
    close(trigger(types),services.values().length>0,'Clear the clinics to count duty types instead');};
  for(const p of [types,services])p.wrap.addEventListener('change',exclusive);
  exclusive();
  const read=()=>{
    const m=metric.input.value,p=period.input.value,a=aim.input.value;
    return {kind:'balance_goal',metric:m,assignment_type_ids:types.values(),service_ids:services.values(),
      ...(m==='pair_exposure'?{partners:partners.read(),pairing:pairing.input.value}:{}),
      ...(m==='weekends'?{weekend_start:Number(weekendStart.input.value),weekend_days:Number(weekendDays.input.value)}:{}),
      period:p,...(p==='rolling'?{period_days:Number(days.input.value)}:{}),...(p==='custom'?{starts_on:from.input.value,ends_on:until.input.value}:{}),
      aim:a,...(['target','min_per_pair'].includes(a)?{amount:Number(amount.input.value),person_amounts:Object.fromEntries(own.read().map(r=>[r.person_id,r.amount]))}:{}),
      share_by:share.input.value};
  };
  return {root,read};
}

type PreviewRow={person_id:string;name:string;prior:number;planned:number;actual:number;expected:number;low:number;high:number|null;status:string;partner_name?:string;available_days?:number};

/** "Preview on current draft": each person's value now against their aim, on a chosen rota being planned. */
export function goalPreview(definition:()=>Record<string,unknown>,title:()=>string,workspaceId?:string){
  const pick=select('Draft rota',workspaceId?[{value:workspaceId,label:'This rota'}]:[],workspaceId||'',false,'Choose a draft rota');
  const out=el('div',{class:'goal-preview','aria-live':'polite'});
  let loaded=!!workspaceId;
  const load=async()=>{
    if(loaded)return;loaded=true;
    const list=await request<{workspaces:{id:string;name:string;status:string}[]}>('/api/workspaces?current_only=true&limit=100');
    for(const w of list.workspaces.filter(w=>w.status!=='published'))pick.input.append(el('option',{value:w.id},w.name));
  };
  pick.input.addEventListener('focus',()=>void load());pick.input.addEventListener('pointerdown',()=>void load());
  const run=async()=>{
    await load();
    if(!pick.input.value){const first=[...pick.input.options].find(o=>o.value);if(first)pick.input.value=first.value;}
    if(!pick.input.value){out.replaceChildren(note('No rota is being planned yet.'));return;}
    out.replaceChildren(working('Counting…'));
    try{
      const r=await request<{unit:string;score:number;people:PreviewRow[];pairs:PreviewRow[];history:boolean}>('/api/fairness/preview',{method:'POST',body:JSON.stringify({workspace_id:pick.input.value,definition:definition(),name:title()})});
      const status=(s:string)=>badge(({within:'On aim',below:'Below aim',above:'Above aim'} as Record<string,string>)[s]||s,s==='within'?'ok':'warn');
      const rows=r.pairs.length?r.pairs.map(p=>[p.name,p.partner_name||'',String(p.prior),String(p.planned),String(p.actual),`≥ ${p.expected}`,status(p.status)])
        :r.people.map(p=>[p.name,String(p.prior),String(p.planned),String(p.actual),`${p.expected} (${p.low}–${p.high??'…'})`,status(p.status)]);
      const head=r.pairs.length?['Person','Partner','History','This rota','Total','Aim','']:['Person','History','This rota','Total',`Aim (band) · ${r.unit}`,''];
      out.replaceChildren(el('p',{class:'goal-score'},`Goal score ${r.score} / 100`),table(head,rows,'Nobody matches this goal.'));
    }catch(error){out.replaceChildren(note((error as Error).message,'error',error));}
  };
  return el('div',{class:'span-2 goal-preview-box','data-step':'Preview'},el('div',{class:'goal-line'},pick.wrap,button('Preview on current draft',run,'secondary')),out);
}
