/** People › a person › Quotas & offers: the person's own workload quotas,
 *  personal targets and overtime offers are data on the person, not copies of a rule. Each change is a proposal another
 *  person approves (person.workload.* / person.overtime.*), audited, with Undo; the server resolves what applies. */
import {el,button,confirmAction,field,formDialog,area,notify,select,multiPick,check,labelOf,statusBadge,formatDate,type Child,CAPABILITY} from './dom.js';
import {get,command,WITHDRAWN} from './api.js';
import {RULE_CATALOGUE,RULE_CATALOGUE_V2,type RuleCatalogueEntry,type RuleLine} from './generated.js';
import {namer,treeText,daysText,validText,numbersText,measureText,periodText,validEditor,daysEditor,countFields,newLine,numberFields,settingsPopover,wire,countingEndText,type Entry,type Inherited} from './rule_catalogue.js';
import {conditionBuilder,SESSION_FIELDS} from './conditions.js';
import {ruleConditionFields,catalogueEntryOf} from './forms.js';
import {ruleReview} from './rule_review.js';
import {GOAL_METRICS,GOAL_PERIODS} from './balance_goal.js';
import {profileSection,profileRow} from './person_profile.js';
import {levelWords} from './rule_view.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';
import {mayDecide} from './independence.js';

type Facts={active:Row[];proposed:Row[];history:Row[];effective:Row[];on:string};
const QUOTAS=['workload','target'],FAMILY:Record<string,string>={workload:'person.workload',target:'person.workload',overtime_offer:'person.overtime'};
/** The catalogue entry a fact's line belongs to (overtime offers keep the v1 entry). */
const entryOf=(f:Row):RuleCatalogueEntry|undefined=>catalogueEntryOf({kind:'catalogue',id:f.data.catalogue_id,catalogue_version:f.data.catalogue_version});
const WORKLOAD=RULE_CATALOGUE_V2.find(e=>e.id==='workload_limit')!;
const OVERTIME=RULE_CATALOGUE.find(e=>e.id==='overtime_offers')!;
const FAIR_SHARE=RULE_CATALOGUE_V2.find(e=>e.id==='fair_share')!;
const permits=(ctx:Context,...keys:string[])=>ctx.permits(...keys);
const inherited=(l:RuleLine):Inherited|undefined=>l.source_default?Object.fromEntries(Object.entries(l.source_default.numbers).map(([k,n])=>[k,{...n,from:'source'}])):undefined;

export async function personFacts(ctx:Context,p:Row,target:HTMLElement,redraw:()=>void):Promise<void>{
  const facts=await get<Facts>(`/api/people/${p.id}/scheduling-facts`),names=namer(ctx);
  facts.proposed=facts.proposed.filter(f=>FAMILY[f.kind]);facts.history=facts.history.filter(f=>FAMILY[f.kind]);
  const propose=permits(ctx,CAPABILITY.COORDINATOR,CAPABILITY.ADMIN),approve=permits(ctx,CAPABILITY.APPROVER);
  const after=(message:string)=>{notify(message);redraw();};
  const effective=(f:Row)=>facts.effective.some(g=>g.policy_id===f.id||Object.values(f.data.line.source_bindings||{}).some(value=>{
    const ref=value as Row;return ref.policy_id===g.policy_id&&(ref.line_id??null)===(g.template_line?.id??null);}));
  /** "≤ 5! duties per calendar week · Clinic A · Sun–Thu", an offer "Evening shift at Clinic A · Tue, Fri". */
  const what=(f:Row)=>{const l=f.data.line as RuleLine;
    if(f.kind==='overtime_offer')return `${treeText(l.where,names,'Any session')} · ${daysText(l)}`;
    const goal=l.settings?.balance_goal as Row|undefined;
    const over=goal?.period==='custom'?validText({...l,starts_on:goal.starts_on,ends_on:goal.ends_on}):goal?.period==='rolling'?`over the last ${goal.period_days} days`
      :goal?`over ${GOAL_PERIODS.find(x=>x.value===goal.period)?.label||goal.period||'this rota'}`:'';
    return numbersText(l,inherited(l),undefined,[measureText(l).toLowerCase()||GOAL_METRICS.find(x=>x.value===goal?.metric)?.label||String(goal?.metric||''),periodText(l)||over].filter(Boolean).join(' '))+(f.kind==='target'?' (personal target)':'');};
  const detail=(f:Row)=>{const l=f.data.line as RuleLine;
    const limits=(l.settings?.allocation_overtime?.required_policy_ids||[]) as string[];
    return [f.kind==='overtime_offer'?(l.numbers.apply?.level?levelWords(l.numbers.apply.level):'Strength from the rule'):'',
      f.kind==='overtime_offer'&&!f.request_id&&l.settings?.allocation_overtime?.volunteer_required?'Only when they volunteer':'',
      l.who?.conditions.length?`When ${treeText(l.who,names,'')}`:'',f.kind!=='overtime_offer'&&l.where?.conditions.length?treeText(l.where,names,''):'',
      f.kind!=='overtime_offer'?daysText(l):'',limits.length?`Needs ${limits.map(id=>labelOf(ctx.catalogues.policies?.find(r=>r.id===id))||'a required limit').join(', ')}`:'',
      f.kind==='overtime_offer'&&f.status==='active'&&f.on!==false&&!effective(f)?'Not effective today: review its dates, conditions and required limits.':'',
      ...((f.counting_attention||[]) as {counting_end:string;validity_end:string|null}[]).map(a=>'⚠ '+countingEndText(a)),l.note,f.reason].filter(Boolean).join(' · ');};
  const when=(f:Row)=>el('span',{},validText(f.data.line));
  const decide=(f:Row,action:string,title:string,text:string,label:string,said:string)=>confirmAction(title,text,async reason=>{
    await command(`${FAMILY[f.kind]}.${action}`,{reason},f.id,f.revision);after(said);},label);
  const source=(f:Row)=>{const ref=f.data.goal_ref||Object.values(f.data.line.source_bindings||{})[0] as Row|undefined;
    return ref&&permits(ctx,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)?button('Source',()=>ctx.navigate(`rules?source=${encodeURIComponent(ref.policy_id)}`),'ghost small',{'aria-label':`Source of ${what(f)}`}):'';};
  const row=(f:Row,state:Child,...actions:Child[])=>{const node=profileRow(what(f),detail(f),when(f),state,source(f),...actions);node.dataset.fact=f.id;node.tabIndex=-1;return node;};
  const mine=(f:Row)=>f.proposed_by===ctx.me.identity_id;
  const goalFor=(f:Row)=>facts.effective.find(g=>g.policy_id===f.data.goal_ref?.policy_id&&(g.template_line?.id??null)===(f.data.goal_ref?.line_id??null));
  const pendingFor=(f:Row)=>facts.proposed.some(x=>x.replaces_id===f.id);
  const laterFor=(f:Row)=>facts.active.concat(facts.proposed).some(x=>x.replaces_id===f.id);
  // `live` lets a volunteered request (several records) ask whether any of its records applies today.
  const state=(f:Row,live=()=>effective(f))=>f.on===false?statusBadge('off','Off'):f.ends_on&&f.ends_on<=facts.on?statusBadge('expired','Ended')
    :f.starts_on&&f.starts_on>facts.on?statusBadge('proposed','Scheduled'):f.kind==='overtime_offer'&&!live()?statusBadge('off','Not effective today'):statusBadge('active','Active');
  // Converted records are decided with the department simplification; a change replaces them through a proposal.
  const activeRow=(f:Row)=>row(f,state(f),
    propose&&!pendingFor(f)?button('Change…',()=>change(ctx,p,f,after,f.kind,goalFor(f)),'ghost small',{'aria-label':`Change ${what(f)}`}):'',
    approve&&!f.migration_proposal_id&&!pendingFor(f)?button('Undo',()=>decide(f,'undo','Undo this approval','The record stops applying. Its approved predecessor, if any, applies again.','Undo','Approval undone.'),'ghost small',{'aria-label':`Undo ${what(f)}`}):'',
    approve&&!f.migration_proposal_id&&!f.data.line.source_default&&!pendingFor(f)?button('Retire',()=>decide(f,'retire','Retire this record','It stops applying; it stays in the history.','Retire','Retired.'),'ghost small',{'aria-label':`Retire ${what(f)}`}):'');
  const waitingRow=(f:Row)=>row(f,statusBadge('proposed',f.replaces_id?'Change waiting':'Waiting'),
    approve&&mayDecide(ctx.me,'rule.activate',mine(f))?button('Approve',()=>ruleReview({...f,name:what(f)},true,true,async()=>redraw(),(kind,id)=>labelOf(ctx.catalogues[kind]?.find(r=>r.id===id)),ctx.me.organisation.timezone,
      false,[],what(f),detail(f),ctx,FAMILY[f.kind]),'secondary small',{'aria-label':`Approve ${what(f)}`}):'',
    mine(f)&&propose?button('Withdraw',async()=>{await command(`${FAMILY[f.kind]}.undo`,{reason:WITHDRAWN},f.id,f.revision);after('Proposal withdrawn.');},'ghost small',{'aria-label':`Withdraw ${what(f)}`})
      :permits(ctx,CAPABILITY.APPROVER,CAPABILITY.COORDINATOR,CAPABILITY.ADMIN)&&!mine(f)?button('Decline',()=>decide(f,'decline','Decline this change','Nothing changes; the proposal stays in the history.','Decline','Declined.'),'ghost small',{'aria-label':`Decline ${what(f)}`}):'');
  const historyRow=(f:Row)=>row(f,statusBadge(f.status),
    approve&&f.status==='retired'&&f.approved_by&&!f.migration_proposal_id&&!f.request_id&&!laterFor(f)&&(!f.replaces_id||facts.history.some(x=>x.id===f.replaces_id))
      ?button('Restore',()=>decide(f,'undo','Restore this record','It applies again, as approved before.','Restore','Restored.'),'ghost small',{'aria-label':`Restore ${what(f)}`}):'');
  const list=(rows:Row[],draw:(f:Row)=>HTMLElement,none:string)=>rows.length?el('ul',{class:'ph-rows'},...rows.map(draw)):el('p',{class:'muted'},none);
  const quotas=facts.active.filter(f=>QUOTAS.includes(f.kind)),offers=facts.active.filter(f=>f.kind==='overtime_offer'&&!f.request_id);
  // An approved Available for overtime request makes one record per overtime line (a date range with weekdays, or its
  // exact offer_dates when irregular): shown as one row, changed through the request.
  const byRequest=new Map<string,Row[]>();
  for(const f of facts.active)if(f.kind==='overtime_offer'&&f.request_id)byRequest.set(String(f.request_id),[...byRequest.get(String(f.request_id))||[],f]);
  const offered=(f:Row):string[]=>{if(f.data.offer_dates)return f.data.offer_dates;
    const out:string[]=[],weekdays=f.data.line.weekdays as number[]|undefined;  // Monday = 0, as the server stores them
    for(const d=new Date(`${f.starts_on}T00:00:00Z`);d.toISOString().slice(0,10)<f.ends_on;d.setUTCDate(d.getUTCDate()+1))
      if(!weekdays?.length||weekdays.includes((d.getUTCDay()+6)%7))out.push(d.toISOString().slice(0,10));
    return out;};
  // A day covered by several overtime lines makes one record per line: count the days, and name each line's scope once.
  const volunteered=[...byRequest].map(([id,rows])=>{
    const days=[...new Set(rows.flatMap(offered))].sort();
    const where=[...new Set(rows.map(f=>treeText(f.data.line.where,names,'Any session')))].join('; ');
    const span={...rows[0],starts_on:rows.map(f=>f.starts_on).sort()[0],ends_on:rows.map(f=>f.ends_on).sort().at(-1)};
    return profileRow(`Volunteered · ${days.length===1?'1 day':days.length+' days'}`,where,
      el('span',{},days.length>1?`${formatDate(days[0])} – ${formatDate(days[days.length-1])}`:formatDate(days[0])),state(span,()=>rows.some(f=>effective(f))),
      button('Open request',()=>ctx.navigate('requests?open='+encodeURIComponent(id)),'ghost small',{'aria-label':'Open the overtime request'}));});
  // The department's own workload rules still apply beside these (the server's resolved copies for this person).
  const own=new Set([...facts.active,...facts.proposed].map(f=>f.id)),sources=new Set(facts.active.flatMap(f=>Object.values(f.data.line.source_bindings||{}).map(v=>`${(v as Row).policy_id}|${(v as Row).line_id||''}`))),
    shared=[...new Set(facts.effective.filter(x=>!own.has(x.policy_id)&&!sources.has(`${x.policy_id}|${x.template_line?.id||''}`)
    &&['max_duties','max_hours','block_quota','allocation_overtime','balance_goal'].includes(x.definition?.rule?.kind)).map(x=>String(x.name)))];
  const goals=facts.effective.filter(g=>g.definition?.rule.kind==='balance_goal'&&g.definition.rule.aim==='target'&&!facts.active.concat(facts.proposed).some(f=>f.data.goal_ref?.policy_id===g.policy_id&&(f.data.goal_ref?.line_id??null)===(g.template_line?.id??null)));
  target.append(
    profileSection('Quotas & targets',propose?el('span',{class:'row-buttons'},button('Add quota',()=>change(ctx,p,null,after),'ghost small',{'aria-label':'Add a personal quota'}),
      ...(goals.length?[button('Add target',()=>addTarget(ctx,p,goals,after),'ghost small',{'aria-label':'Add a personal target'})]:[])):null,
      list(quotas,activeRow,'None. The department’s workload rules apply.'),
      shared.length?el('p',{class:'muted'},`Also applies: ${shared.join(', ')}.`):''),
    profileSection('Overtime offers',propose?button('Add offer',()=>change(ctx,p,null,after,'overtime_offer'),'ghost small',{'aria-label':'Add an overtime offer'}):null,offers.length||!volunteered.length?list(offers,activeRow,'No overtime offered.'):'',
      volunteered.length?el('ul',{class:'ph-rows'},...volunteered):''),
    ...(facts.proposed.length?[profileSection(`Waiting for approval (${facts.proposed.length})`,null,list(facts.proposed,waitingRow,''))]:[]),
    ...(facts.history.length?[profileSection('History',null,list(facts.history.slice().reverse(),historyRow,''))]:[]));
  const wanted=new URLSearchParams(location.hash.split('?')[1]||'').get('fact');
  if(wanted)requestAnimationFrame(()=>{const node=target.querySelector<HTMLElement>(`[data-fact="${CSS.escape(wanted)}"]`);node?.scrollIntoView({block:'nearest'});node?.focus({preventScroll:true});});
}

function addTarget(ctx:Context,p:Row,goals:Row[],after:(message:string)=>void){
  formDialog('Choose a target source','The personal amount keeps this approved Fair-share goal’s scope, period and strength.',form=>{
    const source=select('Fair-share goal',goals.map((g,i)=>({value:String(i),label:`${g.name} · ${String(g.definition.rule.metric).replaceAll('_',' ')}${g.template_line?` · line ${g.template_line.line+1}`:''}`})),goals.length===1?'0':'');form.append(source.wrap);
    return async()=>{change(ctx,p,null,after,'target',goals[Number(source.input.value)]);};
  },'Continue');
}

/** Change… (a replacement of `f`) or Add quota (`f` null): numbers with their strengths, counts for a new quota, days
 *  and dates, and the reason. One proposal; another person approves it. */
function change(ctx:Context,p:Row,f:Row|null,after:(message:string)=>void,kind='workload',goal?:Row){
  kind=f?.kind||kind;
  const e=f?entryOf(f):kind==='overtime_offer'?OVERTIME:kind==='target'?FAIR_SHARE:WORKLOAD;if(!e){notify('This record cannot be changed here.','warning');return;}
  const base:RuleLine=f?JSON.parse(JSON.stringify(f.data.line)):newLine(e);
  if(goal&&!f){base.numbers={};base.settings={balance_goal:Object.fromEntries(Object.entries(goal.definition.rule).filter(([k])=>!['kind','amount','person_amounts'].includes(k)))};}
  const bound=!!f&&Object.keys(f.data.line.source_bindings||{}).length>0;
  const linked=kind==='target'&&e.id==='fair_share'&&!bound;
  // A converted line keeps its numeric slots; a native one may set any count number. A target follows its goal's strength.
  const keys=kind==='overtime_offer'||bound?Object.keys(base.numbers):kind==='target'?['target']:['min','max','target'];
  formDialog(f?'Change this record':kind==='overtime_offer'?'Add an overtime offer':kind==='target'?'Add a personal target':'Add a personal quota',`${p.display_name||'This person'} · applies once another authorised person approves it.`,form=>{
    const reads:(()=>void)[]=[];
    const levels=linked?[base.numbers.target?.level||goal?.definition.strength||'medium']:e.levels;
    const numbers=numberFields({...e,levels,numbers:Object.fromEntries(keys.map(k=>[k,e.numbers[k]]))},base,true,reads,inherited(base),()=> 'the approved source');
    const counts=linked?null:countFields(e,base,true,reads),days=daysEditor(base),valid=validEditor(base);
    const fields=ruleConditionFields(ctx),who=conditionBuilder(fields.filter(x=>!SESSION_FIELDS.includes(x.key)),base.who,{allOf:true,startEmpty:true,context:'rule'}),
      where=conditionBuilder(fields.filter(x=>SESSION_FIELDS.includes(x.key)),base.where,{allOf:true,startEmpty:true,count:()=>e.count_sides.includes('where'),context:'rule'});
    const start=field('Starts at','text',base.start_time||'00:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]'}),end=field('Ends at','text',base.end_time||'24:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00'});
    form.append(el('div',{class:'span-2'},numbers),...(counts?[el('div',{class:'span-2'},counts)]:[]));
    if(!linked){
      form.append(el('section',{class:'span-2 rc-lp-section'},el('h3',{},'When this person matches'),who.root),el('section',{class:'span-2 rc-lp-section'},el('h3',{},'Where'),where.root),
        el('div',{class:'field span-2'},el('label',{},'Days'),days.node),start.wrap,end.wrap,el('div',{class:'span-2'},valid.node));
      reads.push(()=>{base.who=who.read();base.where=where.read();base.weekdays=days.read();base.start_time=start.input.value;base.end_time=end.input.value;Object.assign(base,valid.read());});
    }
    if(kind==='overtime_offer'){
      const selected=(base.settings?.allocation_overtime?.required_policy_ids||[]) as string[],policies=ctx.catalogues.policies||[];
      const caps=multiPick('Required overtime limits',policies.map(r=>({value:r.id,label:labelOf(r)+(r.status==='active'?'':' · '+r.status)})),selected,undefined,'No required limits');
      const volunteer=check('Only when they volunteer',f?base.settings?.allocation_overtime?.volunteer_required===true:true,
        'On: overtime only within their approved Available for overtime request. Off: whenever they are free.');
      volunteer.input.setAttribute('role','switch');volunteer.wrap.classList.add('span-2');
      form.append(el('div',{class:'field span-2'},el('label',{},'Required overtime limits'),caps.wrap),volunteer.wrap);
      reads.push(()=>{base.settings={...base.settings,allocation_overtime:{...base.settings?.allocation_overtime,required_policy_ids:caps.values(),volunteer_required:volunteer.input.checked}};});
    }else if(!linked){const edit=button('Settings…',()=>settingsPopover(ctx,e as Entry,edit,base.settings||{},true,value=>{base.settings=value;}),'secondary small');form.append(el('div',{class:'span-2'},edit));}
    if(linked)form.append(el('p',{class:'span-2 muted'},'Scope, period and strength come from the approved Fair-share goal.'));
    const enabled=check('Enabled',f?.on!==false);enabled.input.disabled=!!base.source_default;enabled.wrap.classList.add('span-2');form.append(enabled.wrap);
    const note=area('Note',base.note||'',2);note.input.maxLength=500;form.append(note.wrap);reads.push(()=>{base.note=note.input.value;});
    const reason=area('Reason','',2);reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;form.append(reason.wrap);
    return async()=>{
      for(const r of reads)r();
      if(!Object.keys(base.numbers).length)throw new Error(kind==='overtime_offer'?'Enable the offer.':'Give it a minimum, maximum or target.');
      const line=wire([base],f?[f.data.line]:[])[0];
      await command(`${FAMILY[kind]}.propose`,{kind,person_id:p.id,catalogue_id:e.id,
        catalogue_version:f?.data.catalogue_version??e.catalogue_version,line,goal_ref:f?.data.goal_ref??(goal?{policy_id:goal.policy_id,revision:goal.revision,line_id:goal.template_line?.id??null}:null),on:enabled.input.checked,
        ...(f?{replaces_id:f.id,expected_revision:f.revision}:{}),reason:reason.input.value});
      after(f?'Change proposed; it applies once another authorised person approves it.':kind==='overtime_offer'?'Offer proposed for independent approval.':kind==='target'?'Target proposed for independent approval.':'Quota proposed; it applies once another authorised person approves it.');};
  },'Propose');
}
