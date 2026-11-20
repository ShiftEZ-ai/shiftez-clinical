import {statusLabel,el,field,area,select,check,button,note,badge,empty,sectionHead,table,formDialog,showDialog,confirmAction,notify,dateUnits,formatDate,formatTime,toLocal,roleLabel,statusBadge,footerStatus,plural,formatDay,armed,type Tone,hasAccess,capabilityForRole,CAPABILITY,roleForCapability,icon,helpButton,factLine,PAGE_WHY} from './dom.js';
import {couldntLoad} from './dom.js';  // a failed read's words (DL-045)
import {failedNote} from './dom.js';  // a failed check: refused (403) or an error with Retry (maintenance)
import {working} from './loading.js';
import {dateRange,lastDayIncluded} from './date_picker.js';
import {get,command,instant,sessionChanged} from './api.js';
import {TERMS} from './terms.js';
import type {Context} from './forms.js';
import {dataTable,type RowAction} from './table_view.js';
import {pillList} from './table_menu.js';
import type {Row,WorkspaceData} from './types.js';
import type {ReviewStep,ReviewRouteInput,ReviewDelegationInput,SelfApprovalInput} from './generated.js';
import {mayDecide} from './independence.js';
import {ROLE_NAMES,SWAP_LIMITS,SWAP_KINDS,MAX_APPROVALS_REQUIRED} from './generated.js';

const APPROVER_ROLE=roleForCapability(CAPABILITY.APPROVER),PROGRAMME_LEAD_ROLE=roleForCapability(CAPABILITY.PROGRAMME_LEAD);

const permitted=(ctx:Context,...keys:string[])=>ctx.permits(...keys);
const zone=(ctx:Context)=>ctx.me.organisation.timezone;
const dateTime=(ctx:Context,value:string)=>`${formatDate(value,zone(ctx))} ${formatTime(value,zone(ctx))}`;
let routeOffset=0;

/** A one-line hint under a field, read with it (aria-describedby). */
function hint(f:{wrap:HTMLElement;input:HTMLElement},text:string){
  const h=el('small',{class:'hint',id:f.input.id+'-hint'},text);f.wrap.append(h);f.input.setAttribute('aria-describedby',h.id);
}
function reasonField(label='Reason'){
  const reason=area(label,'',3);
  reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
  return reason;
}

function stepBuilder(initial:ReviewStep[],prefix:string){
  const body=el('div',{class:'span-2'});
  const entries:{node:HTMLElement;remove:HTMLButtonElement;read:()=>ReviewStep;key?:string}[]=[];
  const addButton=button('Add '+prefix+' review step',()=>add(),'secondary');
  // A step's key is an internal name: kept as saved, else made from its label (unique in the route's steps).
  const keyOf=(entry:{key?:string},label:string)=>{if(entry.key)return entry.key;
    const base=(prefix+'_'+label.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_+|_+$/g,'')).slice(0,36).replace(/_+$/,'')||prefix;
    const taken=new Set(entries.filter(e=>e.key).map(e=>e.key));let key=base,n=1;while(taken.has(key))key=`${base}_${++n}`;return key;};
  function add(value?:ReviewStep){
    if(entries.length>=5)return;
    const label=field('Step label','text',value?.label||'',{required:true,maxlength:160});
    const role=select('Required review role',[{value:APPROVER_ROLE,label:'Approver'},{value:PROGRAMME_LEAD_ROLE,label:'Programme lead'}],value?.role||APPROVER_ROLE);
    const quorum=field('Distinct approvals required','number',value?.approvals_required||1,{required:true,min:1,max:MAX_APPROVALS_REQUIRED});
    const deadline=field('Deadline (hours)','number',value?.deadline_hours||48,{required:true,min:1,max:168});
    const escalation=select('Escalation review role',[{value:APPROVER_ROLE,label:'Approver'},{value:PROGRAMME_LEAD_ROLE,label:'Programme lead'}],value?.escalation_role||'',false);
    const node=el('section',{class:'nested-card'},el('div',{class:'mini-grid'},label.wrap,role.wrap,quorum.wrap,deadline.wrap,escalation.wrap));
    const read=():ReviewStep=>({key:entry.key=keyOf(entry,label.input.value),label:label.input.value,role:role.input.value as ReviewStep['role'],
      approvals_required:Number(quorum.input.value),deadline_hours:Number(deadline.input.value),
      escalation_role:(escalation.input.value||null) as ReviewStep['escalation_role']});
    const remove=button('Remove review step',()=>{entries.splice(entries.indexOf(entry),1);node.remove();sync();},'ghost');
    node.append(remove);
    const entry:{node:HTMLElement;remove:HTMLButtonElement;read:()=>ReviewStep;key?:string}={node,remove,read,key:value?.key};entries.push(entry);body.insertBefore(node,addButton);sync();
  }
  // A route needs at least one step: the last one cannot be removed.
  function sync(){addButton.disabled=entries.length>=5;
    for(const e of entries){const last=entries.length===1;e.remove.disabled=last;if(last)e.remove.dataset.tip='A route needs at least one step';else delete e.remove.dataset.tip;}}
  body.append(addButton);initial.forEach(step=>add(step));
  return {body,read:()=>entries.map(entry=>entry.read())};
}

export const ROUTE_PURPOSES=[{value:'rota',label:'Rotas and amendments'},{value:'swap',label:'Swap amendments only'}];
function routeForm(ctx:Context,existing?:Row,copy=false,urgentOn=false){
  formDialog(copy?'Propose replacement review route':existing?'Edit proposed review route':'Propose review route',
    'A proposal has no operational authority until another approver activates it. Steps are sequential; each seat represents a different person.',form=>{
      const name=field('Route name','text',existing?.name||'',{required:true,maxlength:160});
      // A swap route reviews swap amendments only and may overlap the rota routes (Department settings picks it).
      const purpose=select('Reviews',ROUTE_PURPOSES,existing?.purpose||'rota');
      const start=field('Effective from','date',existing?.starts_on||toLocal(new Date().toISOString(),zone(ctx)).slice(0,10),{required:true});
      const end=field('Effective until (included)','date',existing?.ends_on||'');lastDayIncluded(end.input);
      const routine=stepBuilder(existing?.definition.steps||[{key:'',label:'Independent clinical review',role:APPROVER_ROLE}],'routine');
      const urgentEnabled=check('Enable urgent amendment route',Boolean(existing?.definition.urgent_steps)||urgentOn);
      const urgent=stepBuilder(existing?.definition.urgent_steps||[{key:'',label:'Urgent independent review',role:APPROVER_ROLE}],'urgent');
      // A fieldset (so it can be switched off as a whole) laid out like the routine section: title, line, steps.
      const urgentWrap=el('fieldset',{class:'form-section span-2'},el('legend',{},'Urgent amendment steps'),el('p',{},'Used instead of the routine steps when an amendment is marked urgent.'),urgent.body);
      const updateUrgent=()=>{urgentWrap.hidden=!urgentEnabled.input.checked;urgentWrap.disabled=!urgentEnabled.input.checked;};
      urgentEnabled.input.addEventListener('change',updateUrgent);updateUrgent();
      const reason=reasonField('Configuration reason');
      name.wrap.classList.add('span-2');
      form.append(name.wrap,purpose.wrap,dateRange('Dates',start,end),el('section',{class:'form-section'},el('h3',{},'Routine review steps'),el('p',{},'Every draft goes through these steps in order.'),routine.body),urgentEnabled.wrap,urgentWrap,
        note('Urgency never bypasses clinical eligibility, availability, coverage or staff consent. Rota review routes cannot overlap each other. Swap review routes may overlap.','warning'),reason.wrap);
      return async()=>{
        const payload:ReviewRouteInput={name:name.input.value,purpose:purpose.input.value as 'rota'|'swap',starts_on:start.input.value,ends_on:end.input.value||null,
          definition:{schema_version:1,steps:routine.read(),urgent_steps:urgentEnabled.input.checked?urgent.read():null},reason:reason.input.value};
        await command(existing&&!copy?'review_route.update':'review_route.create',payload,existing&&!copy?existing.id:undefined,existing&&!copy?existing.revision:undefined);
        await ctx.refresh();notify('Review route proposal saved. Independent activation is still required.');
      };
    },'Save route proposal');
}

function delegationForm(ctx:Context,routes:Row[],reviewers:Row[]){
  const choices=routes.filter(route=>route.status==='active').flatMap(route=>[...route.definition.steps,...(route.definition.urgent_steps||[])]
    .filter((step:ReviewStep)=>hasAccess(ctx.me.roles,step.role?capabilityForRole(step.role):CAPABILITY.APPROVER)).map((step:ReviewStep)=>({value:`${route.id}|${step.key}`,label:`${route.name} · ${step.label}`})));
  formDialog('Delegate a review step','Choose another authorised reviewer for your review step, for up to fourteen days. They cannot pass this delegation on.',form=>{
    const step=select('Route and review step',choices);
    const delegate=select('Delegate reviewer',reviewers.filter(r=>r.id!==ctx.me.identity_id).map(r=>({value:r.id,label:`${r.display_name} · ${(r.roles as string[]).map(roleLabel).join(', ')}`})));
    const start=field('Delegation starts','datetime-local',toLocal(new Date().toISOString(),zone(ctx)),{required:true});
    const end=field('Delegation expires','datetime-local',toLocal(new Date(Date.now()+86400000).toISOString(),zone(ctx)),{required:true});
    const reason=reasonField('Delegation reason');
    form.append(step.wrap,delegate.wrap,start.wrap,end.wrap,reason.wrap);
    if(!choices.length||!reviewers.length)form.append(note('An active route step and another eligible reviewer are required.','warning'));
    return async()=>{
      const [routeId,stepKey]=step.input.value.split('|');
      if(!routeId||!stepKey||!delegate.input.value)throw new Error('Choose an active step and another eligible reviewer.');
      const payload:ReviewDelegationInput={route_id:routeId,step_key:stepKey,delegate_id:delegate.input.value,
        starts_at:await instant(start.input.value),expires_at:await instant(end.input.value),reason:reason.input.value};
      await command('review_delegation.create',payload);notify('Review delegation recorded. No candidate was approved.');await ctx.refresh();
    };
  },'Record delegation');
}

/** Review routes. On a phone (`phone`, maintenance) it is the approvals part only: each route a card, its steps stacked, and
 *  Activate / Retire as 44px targets; proposing routes, self-approval and delegation stay on the desktop. */
export async function routingPage(ctx:Context,target:HTMLElement,o:{phone?:boolean}={}){
  const data=await get<{routes:Row[];total:number;has_more:boolean}>(`/api/review-routes?limit=50&offset=${routeOffset}`);
  const activeRoutes=data.routes.filter(r=>r.status==='active').length;
  // Header first (it moves into the top bar), then the note (its ⓘ tip); the footer status can sit anywhere.
  target.append(sectionHead('Effective review routes','The review route applies automatically for the rota period.',
    permitted(ctx,CAPABILITY.ADMIN)&&!o.phone?button('Propose review route',()=>routeForm(ctx),'primary'):null));
  // With no route, the Built-in independent approval card already says what is in use: the footer does not repeat it.
  if(data.routes.length)target.append(footerStatus(el('span',{class:'muted'},activeRoutes?plural(activeRoutes,'active review route'):'Built-in independent approval in use')));
  for(const route of data.routes){
    const actions=el('div',{class:'actions'});
    if(permitted(ctx,CAPABILITY.ADMIN)&&!o.phone){
      if(route.status==='proposed')actions.append(button('Edit proposal',()=>routeForm(ctx,route),'secondary'));
      actions.append(button('Propose replacement',()=>routeForm(ctx,route,true),'secondary'));
    }
    // Its author decides it only under a self-approval permission (the server also counts later editors).
    if(permitted(ctx,CAPABILITY.APPROVER)&&['proposed','active'].includes(route.status)&&mayDecide(ctx.me,'route.decide',route.created_by===ctx.me.identity_id)){
      const action=route.status==='proposed'?'activate':'retire',own=route.created_by===ctx.me.identity_id;
      // F20: deciding your own proposal is said before it is done (it is recorded and announced as self-approved).
      actions.append(button(action==='activate'?'Activate route':'Retire route',()=>confirmAction(action==='activate'?'Activate review route':'Retire review route',
        (own?'You proposed this route: deciding it is self-approved, recorded and announced. ':'')+(action==='activate'?'Affected rotas will need fresh review; anything that must be fixed still stops them being submitted.':route.purpose==='swap'?'Affected rotas will need fresh approval.':'Then activate its replacement: until one is active, rotas in its dates cannot be submitted for review. Affected rotas will need fresh approval.'),async reason=>{
          await command('review_route.'+action,{reason},route.id,route.revision);
          notify(action==='activate'?`${route.name||'The review route'} is active. Rotas submitted from now use it.`:`${route.name||'The review route'} is retired. Affected rotas need fresh approval.`);
          await ctx.refresh();
        }),action==='activate'?'primary':'secondary'));
    }
    const stepRows=(mode:string,steps:ReviewStep[])=>steps.map((step,index)=>[mode+' '+(index+1),step.label,roleLabel(step.role||APPROVER_ROLE),
      String(step.approvals_required||1),`${step.deadline_hours||48} hours`,step.escalation_role?roleLabel(step.escalation_role):'None']);
    const dates=dateUnits([formatDate(route.starts_on),'– '+(route.ends_on?formatDate(lastDay(route.ends_on)):'no end')]);
    const rowsNow=[...stepRows('Routine',route.definition.steps),...stepRows('Urgent',route.definition.urgent_steps||[])];
    // A phone stacks each step as one row (what, then who and how long): six columns never fit 390px.
    const steps=o.phone?el('ul',{class:'ph-rows route-steps'},...rowsNow.map(([path,label,role,people,deadline,escalation])=>el('li',{class:'ph-row'},
        el('span',{class:'ph-what'},el('strong',{},label),el('small',{class:'muted'},[path,role,plural(Number(people),'person','people'),escalation==='None'?'':'escalates to '+escalation].filter(Boolean).join(' · '))),
        el('span',{class:'ph-when'},deadline))))
      :table(['Path / step','Review','Authority','Distinct people','Deadline','Escalation'],rowsNow);
    target.append(el('section',{class:'panel route-card'},sectionHead(route.name||'Review route','',statusBadge(route.status)),
      el('p',{class:'muted'},route.purpose==='swap'?'Swap amendments only · ':'',dates),steps,actions));
  }
  if(!data.routes.length)target.append(el('section',{class:'panel route-default'},el('div',{class:'route-default-head'},statusBadge('active','In use'),el('strong',{},'Built-in independent approval')),
    el('div',{class:'review-steps'},el('span',{class:'review-step'},el('span',{class:'review-step-dot','aria-hidden':'true'}),'Coordinator submits'),el('span',{class:'route-arrow','aria-hidden':'true'},'→'),
      el('span',{class:'review-step'},el('span',{class:'review-step-dot','aria-hidden':'true'}),'One independent approver'),el('span',{class:'route-arrow','aria-hidden':'true'},'→'),el('span',{class:'review-step'},el('span',{class:'review-step-dot','aria-hidden':'true'}),'Publish')),
    el('p',{class:'muted'},'Every routine rota uses this until a review route is approved. Urgent amendments need an explicit route.')));
  if(data.total>data.routes.length||routeOffset>0)target.append(el('div',{class:'actions'},el('span',{},`${data.total} routes · showing ${data.routes.length?routeOffset+1:0}–${routeOffset+data.routes.length}`),
    button('Previous routes',()=>{routeOffset=Math.max(0,routeOffset-50);ctx.refresh();},'secondary',{disabled:routeOffset===0,why:PAGE_WHY.first}),
    button('Next routes',()=>{routeOffset+=50;ctx.refresh();},'secondary',{disabled:!data.has_more,why:PAGE_WHY.last})));
  target.append(urgentSection(ctx,data.routes,o.phone));
  if(!permitted(ctx,CAPABILITY.ADMIN,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR))return;
  // Access administration is desktop work: one quiet line names it, with no way into the desktop notice (as Home).
  if(o.phone){target.append(el('p',{class:'muted route-desktop-line'},'Self-approval permissions and review delegation need a desktop.'));return;}
  target.append(await selfApprovalSection(ctx));
  const delegationData=await get<{delegations:Row[];reviewers:Row[];delegations_partial:boolean;reviewers_partial:boolean}>('/api/review-delegations');
  const who=(id:string)=>delegationData.reviewers.find(r=>r.id===id)?.display_name||'Former or unavailable reviewer';
  // Nothing to delegate and nothing delegated: no empty section.
  if(!data.routes.some((r:Row)=>r.status==='active')&&!delegationData.delegations.length)return;
  target.append(sectionHead('Review delegation','',
    // Only approved routes have steps to hand over; the built-in approval has none.
    permitted(ctx,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD)&&data.routes.some((r:Row)=>r.status==='active')?button('Delegate review step',()=>delegationForm(ctx,data.routes,delegationData.reviewers),'secondary'):null));
  if(delegationData.delegations_partial||delegationData.reviewers_partial)target.append(note('This list is incomplete. Some reviewers or delegations are not shown.','warning'));
  // A delegation names its step by the step's label (the key is internal).
  const stepLabel=(row:Row)=>{const route=data.routes.find(r=>r.id===row.route_id);
    return ([...(route?.definition.steps||[]),...(route?.definition.urgent_steps||[])] as ReviewStep[]).find(s=>s.key===row.step_key)?.label||'Review step';};
  if(!delegationData.delegations.length)target.append(empty('No delegations','A reviewer can hand one review step to a colleague for a set period, for example during leave.',null,'swap'));else target.append(table(['Step','From','Delegate','Effective interval','State','Action'],delegationData.delegations.map(row=>[
    stepLabel(row),who(row.delegator_id),who(row.delegate_id),dateUnits([formatDate(row.starts_at,zone(ctx)),formatTime(row.starts_at,zone(ctx)),'— '+formatDate(row.expires_at,zone(ctx)),formatTime(row.expires_at,zone(ctx))]),
    row.revoked?'Revoked':Date.parse(row.expires_at)<=Date.now()?'Expired':Date.parse(row.starts_at)>Date.now()?'Future':'Active',
    !row.revoked&&(permitted(ctx,CAPABILITY.ADMIN)||row.delegator_id===ctx.me.identity_id)?button('Revoke delegation',()=>confirmAction('Revoke delegation','A candidate relying on this delegation must receive renewed review.',async reason=>{
      await command('review_delegation.revoke',{reason},row.id,row.revision);notify('Delegation revoked.');await ctx.refresh();
    }),'small'):'—'])));
}

/** The rota's own rule changes (SR-18), signed off with it: named in the approve dialog and the review strip. */
function ruleChanges(ctx:Context,w:Row):HTMLElement|null{
  const list=(w.rule_adjustments||[]) as Row[];if(!list.length)return null;
  const policies=new Map((ctx.catalogues.policies||[]).map((p:Row)=>[p.id,p]));
  return el('div',{class:'span-2 rota-rule-changes'},el('strong',{},plural(list.length,'rule change')+' for this rota'),
    el('ul',{},...list.map(a=>el('li',{},`${policies.get(a.policy_id)?.name||a.policy_name||'A department rule'} · ${a.mode==='off'?'switched off':'set to '+a.amount}${a.reason?` · “${a.reason}”`:''}`))));
}
/** Approvals given under a self-approval permission (LIVE-26). */
const selfApproved=(step:Row)=>(step.reviewers||[]).some((r:Row)=>!!r.self_approval_id);

/** A step's due time in words; once it has passed, "Overdue since …" in the error tone with its icon (never colour alone). */
function dueFact(ctx:Context,step:Row):HTMLElement|string{
  if(!step.due_at||step.complete)return '';
  const when=`${formatDay(step.due_at,zone(ctx))} ${formatTime(step.due_at,zone(ctx))}`;
  return Date.parse(step.due_at)<=Date.now()?el('span',{class:'overdue'},icon('alert'),`Overdue since ${when}`):`due ${when}`;
}
export function reviewPanel(ctx:Context,data:WorkspaceData):HTMLElement{
  const progress=data.review_progress;
  if(!progress?.route)return note('Review steps are recorded when a rota is submitted. Previous approvals remain in its history.');
  // L11: the rota's one status is the top bar's badge; the review is facts in words (the step in hand, its approvals and
  // its due time), never another coloured pill. The rules behind it are the "?" help beside the heading.
  const step:Row=progress.pending_step||progress.steps[progress.steps.length-1]||{};
  const rules=ruleChanges(ctx,data.workspace);
  // A one-step route named after its step ("Independent approval (built-in)") is not named twice.
  const named=!(progress.steps.length===1&&String(progress.route.name).replace(/\s*\(built-in\)$/,'')===progress.steps[0].label);
  // The route's mode changes what happens, so it stays a fact when it is not the routine one.
  const mode=progress.route.mode&&progress.route.mode!=='routine'?String(progress.route.mode).replace(/^./,(c:string)=>c.toUpperCase())+' review':'';
  const facts=factLine([named?String(progress.route.name):'',mode,progress.steps.length>1?`Step ${progress.steps.indexOf(step)+1} of ${progress.steps.length}`:'',
    String(step.label||''),`${step.approved_count??0} of ${step.approvals_required??1} approvals`,selfApproved(step)?badge('Self-approved','warn'):'',dueFact(ctx,step)],'review-facts');
  // Out of date, not cancelled: every decision stays recorded; the coordinator's reopen, check and resubmit renews it.
  const help=helpButton('About this review',[progress.stale_inputs?'Rules, people or availability changed after it was submitted, so it can’t be approved. Decisions stay recorded. The coordinator reopens it, checks it and submits it again.':'',
    'Changes after submitting always need a new approval.'].filter(Boolean).join(' '),'review-help');
  help.addEventListener('click',e=>e.preventDefault());  // in a multi-step strip's <summary>: opens its help, never toggles the table
  const head=(tag:'div'|'summary')=>el(tag,{class:'review-head'},el('strong',{},'Review'),help,facts);
  // One step: one line. Several steps: their table opens on demand (open while a step waits for review).
  if(progress.steps.length===1)return el('div',{class:'panel review-strip'},head('div'),rules||'');
  return el('details',{class:'panel review-strip',open:!!progress.pending_step},head('summary'),rules||'',
    table(['Step','Reviewed by','Approvals','Deadline','State'],progress.steps.map((step:Row)=>[
      step.label,roleLabel(String(step.role)),`${step.approved_count} of ${step.approvals_required}${selfApproved(step)?' · self-approved':''}`,step.due_at?dateUnits([formatDate(step.due_at,zone(ctx)),formatTime(step.due_at,zone(ctx))]):'After previous step',
      step.complete?'Complete':step.escalated?`Escalated to ${step.escalation_role}`:progress.pending_step?.key===step.key?(progress.stale_inputs?'On hold · needs resubmitting':statusLabel('submitted')):'Not yet open'])));
}

/** `summary`: the rota as Submit showed it (app.ts rotaReviewSummary, SR-32), drawn into the form, for a whole rota and
 *  a published change alike. It may answer what Approve adds (acknowledged gaps, D3): `gaps` decided, sent as its `payload`; with gaps the
 *  reason is required and Approve says how many and is pressed twice. */
/** SX-01 (maintenance): the server's decision facts for the reader (`review_progress.can_decide`, the step's
 *  `decision_role` and the live `delegation_ids` it may act under), the same ones approve_step enforces. */
type Decision={can_decide?:boolean;decision_role?:string|null;delegation_ids?:string[]};
export const decidesStep=(data:WorkspaceData)=>(data.review_progress as Decision|undefined)?.can_decide===true;
async function stepAuthority(ctx:Context,data:WorkspaceData){
  const d=(data.review_progress||{}) as Decision,ids=d.delegation_ids||[];
  const own=decidesStep(data)&&!!d.decision_role&&hasAccess(ctx.me.roles,`ui.${d.decision_role}`);
  // The delegation rows are read only for their labels (until when), when the server says one applies.
  const options=ids.length?(await get<{delegations:Row[]}>('/api/review-delegations')).delegations.filter(x=>ids.includes(String(x.id))):[];
  return {own:own||!ids.length,options,decides:decidesStep(data)};
}
/** Who reviews the pending step, said to someone who does not decide it (never an empty dialog, never silence). */
export function notDecider(data:WorkspaceData){const pending=data.review_progress?.pending_step;
  notify(`This step is reviewed by ${roleLabel(String(pending?.role||APPROVER_ROLE))}${pending?.escalation_role?' (or '+roleLabel(String(pending.escalation_role))+' once escalated)':''}.`,'warning');}
export async function approvalDialog(ctx:Context,data:WorkspaceData,summary:(form:HTMLElement)=>void|{gaps:number;payload:()=>Record<string,unknown>;label?:()=>string}){
  const w=data.workspace,pending=data.review_progress?.pending_step;
  const {own,options,decides}=await stepAuthority(ctx,data);
  // The Approve button shows only to whoever decides the step (app.ts); should one still be pressed, it is said, never an empty dialog.
  if(!decides){notDecider(data);return;}
  // SX-17: an amendment is approved as changes, not as a whole rota.
  const noun=w.base_publication_id?'Approve changes':'Approve rota';
  // Further reviews are mentioned only when the route has more steps after this one (the route's own data).
  const later=pending?(data.review_progress?.steps||[]).filter((s:Row)=>!s.complete&&s.key!==pending.key).length:0;
  // L11: the review strip beside it already names the step; only what follows it is said here.
  formDialog(noun,pending?(later?`${plural(later,'more review step')} follow${later===1?'s':''} before it can be published.`:''):
    'Approve this version of the rota. Nobody who changed it or swaps in it can approve it.',form=>{
      const delegation=select('Review authority',[...(own?[{value:'self',label:'Myself'}]:[]),...options.map(d=>({value:d.id,label:`Delegated: ${pending?.label||'this step'} · until ${dateTime(ctx,d.expires_at)}`}))],'self');
      // An independent approval needs no reason (maintenance: an optional note, never a fixed one); approving your own work
      // under a self-approval permission still needs a genuine reason.
      const self=data.reviewer_involved??[w.created_by,w.last_edited_by].includes(ctx.me.identity_id);
      const reason=self?reasonField():area('Note','',2);if(!self){reason.input.maxLength=500;reason.input.placeholder='Optional';}
      // ME-26/27: what is being approved is the summary the Publish dialog shows (the rota's Fix list, what an amendment
      // changes, the submit note), so its counts are causes everywhere; a published change adds that its check is scoped.
      const v=w.validation,scoped=v?.scope==='swap'||v?.scope==='published_changes',swap=v?.scope==='swap'||!!data.swap_id;
      // Approving your own work under a self-approval permission is said before it is done (LIVE-26).
      // The server's own test (every contributor, swap party and alias), not just the creator and last editor.
      form.append(el('div',{class:'span-2 approve-summary'},el('strong',{},`${w.name}`,self?badge('Self-approved','warn','audit-self'):''),scoped?el('span',{class:'muted'},'Only the changes rechecked'):''));
      // The summary lists the rota adjustments itself, each with its justification (D16).
      const decided=summary(form);
      if(swap&&ctx.me.department.swap_auto_publish===true)
        form.append(el('p',{class:'span-2'},'After the final independent review, this swap is published automatically.'));
      // With no delegation to act under, the only authority is your own: no choice to make.
      if(options.length)form.append(delegation.wrap);
      form.append(reason.wrap);
      // Items decided one by one (R5): the summary names and enables the button; the decision is pressed twice.
      if(decided?.gaps){
        queueMicrotask(()=>{const b=form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');if(b)armed(b,'Confirm decision?');});}
      return async()=>{const result=await command('workspace.approve',{...decided?.payload(),...(self?{reason:reason.input.value}:{note:reason.input.value.trim()||null}),delegation_id:delegation.input.value==='self'||!delegation.input.value?null:delegation.input.value},w.id,w.revision);
        // An approved swap amendment may publish itself (Department settings); otherwise publishing is a separate step.
        // Said before the page reads again (SW3-B-03): the approval is saved whatever that read does.
        notify(result.publication_id?'Approved · the swap is published':result.workspace.status==='approved'?'Approved · ready to publish':result.workspace.status==='draft'?'Returned to the coordinator with your decisions':'Review step saved · another review is required');
        await ctx.refresh();};
    },noun);
}

// Same-day duties show the date once: "QA day shift · 1 Dec 2026 08:00–16:00 · Treatment area".
function assignmentLabel(ctx:Context,a:Row){const sameDay=formatDate(a.start,zone(ctx))===formatDate(a.end,zone(ctx));return `${a.type} · ${dateTime(ctx,a.start)}–${sameDay?formatTime(a.end,zone(ctx)):dateTime(ctx,a.end)} · ${a.location}${a.segments?.length?' · presence '+a.segments.map((part:Row)=>dateTime(ctx,part.start)+'–'+dateTime(ctx,part.end)).join('; ')+' (gaps remain reserved)':''}`;}
function assignmentCard(ctx:Context,title:string,a:Row|null){
  return el('div',{class:'nested-card'},el('h3',{},title),a?el('div',{},el('strong',{},a.person),el('p',{},assignmentLabel(ctx,a)),
    a.overtime?el('p',{},'Overtime'):null):el('p',{},'No duty has been offered in return yet.'));
}

/** Keyed by the contract's SWAP_KINDS, so a new kind fails the build here. */
export const SWAP_KIND_WORDS:Record<typeof SWAP_KINDS[number],string>={exchange:'Exchange',give:'Give away'};
/** `allowed_kinds`: the kinds the department allows now (sent by newer servers, maintenance); else the department record says. */
export type SwapOptions={assignments:Row[];recipients:Row[];assignments_partial:boolean;recipients_partial:boolean;allowed_kinds?:string[]};
/** Your own future published duties and the colleagues you may offer one to. */
export const swapOptions=()=>get<SwapOptions>('/api/swaps/options');
/** maintenance: for one duty, each colleague with `eligible` and a plain `message`; for a swap, the return duties likewise. */
const swapChoices=(q:Record<string,string>)=>get<SwapOptions>('/api/swaps/options?'+new URLSearchParams({kind:'exchange',...q}));
/** The fields of a swap offer built into `form`; returns the save (Requests › New request › Swap). */
/** `ask`: opens another request instead (Requests › Other request), offered when nobody can receive an offer. */
export function offerFields(ctx:Context,form:HTMLElement,options:SwapOptions,chosen?:string,ask?:()=>void){
  // The kinds of offer the department allows (Department settings › Swaps): an exchange with a named colleague, or
  // without naming anyone (open offers) an exchange or a give-away.
  const dept=(ctx.me.department||{}) as Row,allowed=(options.allowed_kinds??dept.swap_kinds_allowed??SWAP_KINDS) as string[],open=dept.swap_open_offers!==false;
  const kinds=[...(allowed.includes('exchange')?[{value:'exchange',label:'Swap with a colleague'}]:[]),
    ...(open&&allowed.includes('exchange')?[{value:'open',label:'Open swap · anyone eligible offers a duty in return'}]:[]),
    ...(open&&allowed.includes('give')?[{value:'give',label:'Give away · anyone eligible takes it'}]:[])];
  // Swaps switched off says so first, whatever duties there are.
  if(!kinds.length||!options.assignments.length||!options.recipients.length){
    form.append(note(!kinds.length?'Swaps are switched off for this department.':!options.assignments.length?'You have no upcoming published duty to offer. Once you are on a published rota, you can offer a duty here.'
      :'No colleague can receive a swap offer yet: offers go to colleagues whose staff sign-in is linked to their profile.','warning'));
    if(kinds.length&&options.assignments.length&&ask)form.append(el('div',{class:'actions'},button('Ask your coordinator instead',ask,'secondary small')));
    return async()=>{throw new Error('Nothing to offer yet.');};
  }
  const kind=select('Kind of offer',kinds,kinds[0].value);
  const duty=select('Your duty to swap',options.assignments.map(a=>({value:a.assignment_id,label:assignmentLabel(ctx,a)})),
    options.assignments.some(a=>a.assignment_id===chosen)?chosen:'');
  const recipient=select('Swap with',options.recipients.map(r=>({value:r.id,label:r.display_name||r.id})));
  // The colleague is always chosen by the person, even when only one is possible (LIVE-51).
  const blank=()=>{if(!recipient.input.querySelector('option[value=""]'))recipient.input.prepend(el('option',{value:'',disabled:true,hidden:true},'Choose…'));recipient.input.value='';};
  blank();
  // F11 (maintenance): only colleagues who can take the chosen duty are offered; nobody, said plainly.
  // SW-03/SW-25: the same for a give-away or an open offer (it goes to every eligible colleague): when nobody can take the
  // duty, Send is off and says why, so an offer is never sent to nobody.
  // SW3-C-06: the check has its own states, said apart: checking, could not check (Retry), nobody, or (hidden) some.
  const NOBODY='Nobody eligible can take this duty. Ask your coordinator instead, or choose another duty.';
  const whoCan=el('div',{id:'swap-check','aria-live':'polite'});whoCan.hidden=true;
  let eligible:number|null=null,checking=false;
  const sendButton=()=>form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');
  // Send waits for the answer and is off when nobody can take the duty; a failed check leaves it to the server's own.
  // data-held: the New request sheet's own relabel keeps it off too.
  const paintSend=(again=true)=>{const b=sendButton();if(!b){if(again)setTimeout(()=>paintSend(false));return;}  // fields built before the sheet attaches them
   const off=checking||eligible===0;b.disabled=off;b.toggleAttribute('data-held',off);
    if(off)b.setAttribute('aria-describedby','swap-check');else b.removeAttribute('aria-describedby');};
  const show=(...nodes:Node[])=>{whoCan.hidden=!nodes.length;whoCan.replaceChildren(...nodes);};
  // Who can take it depends on the kind: an exchange (named or open) needs a duty in return, a give-away does not.
  const offerKind=()=>kind.input.value==='give'?'give':'exchange';
  const eligibleFor=async()=>{const id=duty.input.value,k=offerKind();eligible=null;checking=!!id;
    show(...(id?[working('Checking who can take this duty…')]:[]));recipient.input.disabled=checking||kind.input.value!=='exchange';paintSend();if(!id)return;
    let r:SwapOptions;
    try{r=await swapChoices({assignment_id:id,kind:k});}
    catch(e){if(sessionChanged(e)||duty.input.value!==id||offerKind()!==k)return;checking=false;
      show(failedNote('Couldn’t check who can take this duty.',e,()=>void eligibleFor()));recipient.input.disabled=kind.input.value!=='exchange';paintSend();return;}
    if(duty.input.value!==id||offerKind()!==k)return;  // a later choice is being checked
    const ok=r.recipients.filter(x=>x.eligible!==false),chosen=recipient.input.value;
    recipient.input.replaceChildren(...ok.map(x=>el('option',{value:x.id},x.display_name||x.id)));blank();
    // A colleague chosen while the list was read stays chosen when they can take the duty.
    if(ok.some(x=>x.id===chosen))recipient.input.value=chosen;
    checking=false;eligible=ok.length;show(...(ok.length?[]:[note(NOBODY,'info')]));recipient.input.disabled=!ok.length||kind.input.value!=='exchange';paintSend();};
  duty.wrap.classList.add('span-2');  // the duty's full label (time and clinic) has the form's width
  // A colleague is named only for an exchange; an open offer or a give-away goes to every eligible colleague.
  const syncKind=()=>{const named=kind.input.value==='exchange';recipient.wrap.hidden=!named;recipient.input.disabled=!named||checking||eligible===0;
    // Only the label's words change: its required mark stays.
    const words=[...reason.wrap.querySelector('label')?.childNodes||[]].find(n=>n.nodeType===Node.TEXT_NODE);if(words)words.textContent=named?'Message to them':'Message to colleagues';
    const tip=reason.wrap.querySelector('.hint');if(tip)tip.textContent=`${named?'Your colleague':'Colleagues'} and the reviewers see it: leave out private medical or personal details.`;
    if(!expires.wrap.querySelector('.hint'))hint(expires,'');
    expires.wrap.querySelector('.hint')!.textContent=`Up to ${days} day${days===1?'':'s'}, and before ${named?'both duties':'your duty'}.`;};
  kind.input.addEventListener('change',()=>{syncKind();void eligibleFor();});
  const expires=field('Offer open until','datetime-local','',{required:true});
  const days=Number((ctx.me.department as Row|undefined)?.swap_offer_window_days)||SWAP_LIMITS.offer_window_days;  // the department's offer window
  // Default: the day before the duty, at its start time (within the window); a duty within a day closes a minute before
  // it starts. The latest allowed is the window or the duty's start.
  const updateExpiry=()=>{const a=options.assignments.find(a=>a.assignment_id===duty.input.value);if(!a)return;const start=Date.parse(a.start)-60000;
    const dayBefore=Math.min(Date.parse(a.start)-86400000,Date.now()+days*86400000);
    expires.input.value=toLocal(new Date(dayBefore>Date.now()?dayBefore:start).toISOString(),zone(ctx));
    expires.input.max=toLocal(new Date(Math.min(Date.now()+days*86400000,start)).toISOString(),zone(ctx));};
  duty.input.addEventListener('change',updateExpiry);updateExpiry();
  duty.input.addEventListener('change',()=>void eligibleFor());void eligibleFor();
  const reason=reasonField('Message to them');
  // Guidance sits on the fields it is about, as hints (not a standing warning).
  hint(reason,'Your colleague and the reviewers see it: leave out private medical or personal details.');
  syncKind();
  form.append(...(kinds.length>1?[kind.wrap]:[]),duty.wrap,recipient.wrap,whoCan,expires.wrap,reason.wrap);
  if(options.assignments_partial||options.recipients_partial)form.append(note('The list of duties or colleagues is partial. Not all are shown.','warning'));
  return async()=>{const a=options.assignments.find(a=>a.assignment_id===duty.input.value);if(!a)throw new Error('Choose your own future published duty.');
    if(checking)throw new Error('Still checking who can take this duty.');if(eligible===0)throw new Error(NOBODY);
    const k=kind.input.value;
    await command('swap.offer',{kind:k==='give'?'give':'exchange',publication_id:a.publication_id,assignment_id:a.assignment_id,recipient_person_id:k==='exchange'?recipient.input.value:null,
      expires_at:await instant(expires.input.value),reason:reason.input.value});await ctx.refresh();
    notify(k==='exchange'?'Offer sent. Your duty stays the same until the swap is completed.':'Offer shown to eligible colleagues. Your duty stays the same until the swap is completed.');};
}

function counterofferForm(ctx:Context,row:Row,options:Row[]){
  const available=options.filter(a=>a.publication_id===row.publication_id&&Date.parse(a.start)>=Date.parse(row.expires_at));
  formDialog(TERMS.offerInReturn,'',form=>{
    // F11 (maintenance): a return duty that cannot work says why, in plain words, and cannot be chosen.
    const duty=select('Your duty in return',available.map(a=>({value:a.assignment_id,label:a.eligible===false&&a.message?`${assignmentLabel(ctx,a)} — ${a.message}`:assignmentLabel(ctx,a),...(a.eligible===false?{disabled:true}:{})})));
    if(available.some(a=>a.eligible!==false))duty.input.value=available.find(a=>a.eligible!==false)!.assignment_id;
    const reason=reasonField('Message to them');
    form.append(assignmentCard(ctx,'You would receive',row.offered_assignment),duty.wrap,reason.wrap);
    if(!available.length)form.append(note('You have no selectable future duty in this publication with the requested expiry. Withdraw this invitation and arrange a new one.','warning'));
    return async()=>{if(!duty.input.value)throw new Error('Choose an eligible duty or withdraw the invitation.');
      await command('swap.counteroffer',{assignment_id:duty.input.value,reason:reason.input.value},row.id,row.revision);notify('Sent. They confirm the pair next. The rota changes only after independent approval and publication.');await ctx.refresh();};
  },TERMS.offerInReturn);
}

function prepareForm(ctx:Context,row:Row){
  // Preparing never changes published duties; what happens after the final approval is a Department setting.
  const after=(ctx.me.department as Row).swap_auto_publish??true?'After the final approval the swap is published automatically.':'After the final approval a coordinator publishes it.';
  formDialog('Prepare consented amendment',`This creates one draft with both substitutions; published duties stay as they are. Full validation must succeed or neither substitution nor the draft is saved. ${after}`,form=>{
    const urgency=select('Amendment pathway',[{value:'routine',label:'Routine review route'},{value:'urgent',label:'Explicitly configured urgent amendment route'}],'routine');
    const reason=reasonField('Preparation reason');form.append(urgency.wrap,reason.wrap);
    return async()=>{const result=await command('swap.prepare',{urgency:urgency.input.value,reason:reason.input.value},row.id,row.revision);
      ctx.setWorkspace(result.workspace.id);ctx.navigate('schedule');await ctx.refresh();notify('Both substitutions were prepared and validated. Submit the draft for independent review.');};
  },'Prepare draft amendment');
}

/** What happens next on a swap and who it waits on (the status tip and the side panel). */
export const swapNext=(r:Row,ctx?:Context)=>{
  if(r.effective_status==='offered'&&r.open_offer&&!r.recipient_person_id){
    if(r.audience_failed)return 'Withdraw this offer and offer it again to retry';
    // F12: the server found nobody to tell; never "waiting" for a colleague who cannot exist.
    // SW-03: one sentence (the status word already says nobody can take it); the server's per-colleague refusal is not
    // repeated, as it speaks of "the person taking this duty" when there is none.
    if(r.no_eligible_recipients)return 'Withdraw it and ask your coordinator to find cover';
    return r.audience_pending?'Checking eligibility before notifying colleagues'
      :`Waiting for an eligible colleague to ${r.kind==='give'?'accept this duty':'offer a duty in return'}`;
  }
  // Whose turn it is is the status word ("Your turn", "Waiting for Jordan Lee"): this line names only the step.
  return (({
  offered:r.kind==='give'?'Accept the duty':'Offer a duty in return',
  counteroffered:r.kind==='give'?'Confirm the hand-over':'Confirm the pair',
  accepted:r.prepare_refusal?`Could not be prepared automatically (${r.prepare_refusal.message}); a coordinator prepares it`:'Both agreed; a coordinator prepares the rota change',
  reviewing:r.publish_refusal?`Approved, but not published automatically (${r.publish_refusal.message}); a coordinator publishes it`:'The rota change is waiting for independent approval',
  published:'Published in the rota',expired:'Nobody acted before it expired',stale:'The rota changed since this was offered',
  access_revoked:'One person no longer has access',withdrawn:'Withdrawn before it was published',rejected:'Closed by a coordinator or approver',
} as Record<string,string>)[r.effective_status]||'');};
/** The next steps on a swap for this person: a button for what waits on them, the rest in the row menu. Both people must
 *  agree; either can withdraw before publication. */
export function swapActions(ctx:Context,row:Row):{buttons:HTMLElement[];menu:RowAction[]}{
  const staff=permitted(ctx,CAPABILITY.STAFF)&&ctx.me.staff_swap_access;
  const state=row.effective_status,open=['offered','counteroffered','accepted','reviewing'].includes(state),out:HTMLElement[]=[];
  // The invited colleague, or anyone the open offer is shown to (the server lists it only to eligible colleagues).
  const unclaimed=row.open_offer&&!row.recipient_person_id&&row.proposer_id!==ctx.me.identity_id;
  const give=row.kind==='give',recipient=staff&&state==='offered'&&(row.recipient_person_id===ctx.me.person_id||unclaimed);
  // A give-away is taken as it is; an exchange needs one of the recipient's own duties in return.
  if(recipient&&give)out.push(button(TERMS.acceptDuty,()=>confirmAction(TERMS.acceptDuty,
    `You would take ${assignmentLabel(ctx,row.offered_assignment)} and give nothing in return. ${row.proposer_name} confirms next, then a coordinator updates the rota.`,async reason=>{
      await command('swap.accept',{reason},row.id,row.revision);notify('Accepted. They confirm next, then a coordinator updates the rota.');await ctx.refresh();
    },TERMS.acceptDuty),'secondary small'));
  if(recipient&&!give)out.push(button(TERMS.offerInReturn,async()=>{
    // The offered duty in full (participation, overtime) and your own duties to choose from.
    const [full,options]=await Promise.all([get<{swap:Row}>('/api/swaps/'+row.id),swapChoices({swap_id:row.id}).catch(()=>swapOptions())]);counterofferForm(ctx,full.swap,options.assignments);},'secondary small'));
  if(staff&&row.proposer_id===ctx.me.identity_id&&state==='counteroffered')out.push(button('Confirm swap',()=>confirmAction('Confirm swap',
    `${give?`${row.recipient_name} takes ${assignmentLabel(ctx,row.offered_assignment)}; you receive no duty in return`:`Your received duty: ${assignmentLabel(ctx,row.counteroffered_assignment)}`}. The published rota remains unchanged until independent approval and publication.`,async reason=>{
      const done=(await command('swap.confirm',{reason},row.id,row.revision)).swap;
      notify(done?.effective_status==='reviewing'?'Swap confirmed. The rota change was prepared and sent for independent approval.'
        :done?.prepare_refusal?`Swap confirmed. It could not be prepared automatically (${done.prepare_refusal.message}); a coordinator prepares it.`:'Swap confirmed. A coordinator prepares the rota change next.');
      await ctx.refresh();
    },'Confirm swap'),'secondary small'));
  if(permitted(ctx,CAPABILITY.COORDINATOR)&&state==='accepted')out.push(button('Prepare amendment',()=>prepareForm(ctx,row),'secondary small'));
  if(permitted(ctx,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)&&row.workspace_id)out.push(button('Open rota',()=>{ctx.setWorkspace(row.workspace_id);ctx.navigate('schedule');},'secondary small'));
  const menu:RowAction[]=[];
  if(staff&&open&&(row.proposer_id===ctx.me.identity_id||(row.recipient_person_id===ctx.me.person_id&&(!row.recipient_id||row.recipient_id===ctx.me.identity_id))))menu.push({label:'Withdraw from swap',icon:'close',danger:true,run:()=>confirmAction('Withdraw from swap',row.open_offer&&!row.recipient_person_id?'The offer stops and your duty stays as published.':'The swap stops and both duties stay as published. Your colleague is told.',async reason=>{
    await command('swap.withdraw',{reason},row.id,row.revision);notify('Swap withdrawn.');await ctx.refresh();},'Withdraw',undefined,true)});
  if(permitted(ctx,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)&&open)menu.push({label:'Decline swap',icon:'close',danger:true,run:()=>confirmAction('Decline swap request','Close this request without changing published duties. Both people are told.',async reason=>{
    await command('swap.reject',{reason},row.id,row.revision);notify('Swap declined.');await ctx.refresh();},'Decline swap')});
  return {buttons:out,menu};
}

/** D5: a published rota offers Urgent only while the rota route in force has urgent steps (maintenance: the route's
 *  definition.urgent_steps). Who is notified at once is those steps' role and number of people. Changing it is the
 *  route's own versioned flow: an admin proposes, another approver activates. */
function urgentSection(ctx:Context,routes:Row[],phone=false):HTMLElement{
  const today=toLocal(new Date().toISOString(),zone(ctx)).slice(0,10);
  const live=routes.find(r=>r.status==='active'&&r.purpose!=='swap'&&r.starts_on<=today&&(!r.ends_on||today<r.ends_on));
  const steps=(live?.definition.urgent_steps||[]) as ReviewStep[];
  const who=steps.map(st=>`${plural(st.approvals_required||1,roleLabel(st.role||APPROVER_ROLE).toLowerCase())}`).join(', then ');
  const change=permitted(ctx,CAPABILITY.ADMIN)&&!phone?button(live?'Change…':'Set up…',()=>routeForm(ctx,live,!!live,true),'secondary'):null;
  return el('section',{class:'urgent-route'},sectionHead('Urgent approval','',change),
    el('p',{class:'panel urgent-route-state'},steps.length?badge('On','ok'):badge('Off','neutral'),
      el('span',{},steps.length?`${who} notified at once`:'Urgent is not offered on published rotas')));
}

type Action={key:string;label:string};
/** Who may decide their own work. Admins manage it; approvers and auditors can see it. */
async function selfApprovalSection(ctx:Context):Promise<HTMLElement>{
  /** `limits.default_days` (maintenance): how long a new permission runs by default. */
  const data=await get<{actions:Action[];permissions:Row[];limits?:{default_days?:number};grantable?:Grantable}>('/api/self-approval');
  const admin=permitted(ctx,CAPABILITY.ADMIN);
  const label=(key:string)=>data.actions.find(a=>a.key===key)?.label||key;
  const who=(p:Row)=>p.subject||`Everyone with the ${roleLabel(String(p.role)).toLowerCase()} role`;
  const stage=(p:Row):[string,Tone|'']=>(({active:['Active','ok'],upcoming:['Starts later','info'],ended:['Ended','neutral']} as Record<string,[string,Tone]>)[p.state]||[p.state,'']);
  const section=el('section',{},sectionHead('Self-approval permissions','',
    admin?button('Add permission',()=>selfApprovalForm(ctx,data.actions,data.limits?.default_days,data.grantable),'secondary'):null));
  const list=dataTable<Row>({id:'self-approval',noun:['permission','permissions'],rows:data.permissions,rowId:r=>r.id,
    // F-A2: the reason wraps, so Status stays beside the row's actions at 1280 instead of under them.
    searchLabel:'Search permissions',searchPlaceholder:'Person, role or reason',defaults:{group:'state',wrap:['reason']},
    empty:['Nobody can approve their own work',admin?'Every decision needs a second person. Add a permission when there are too few approvers.':'Every decision needs a second person.'],
    columns:[
      {key:'who',label:'Who',search:'related',text:who,cell:p=>el('strong',{},who(p))},
      {key:'actions',label:'May decide their own',text:p=>(p.actions as string[]).map(label).join(' '),cell:p=>{const unusable=(p.unusable_actions||[]) as string[],list=pillList((p.actions as string[]).map(a=>el('span',{class:'tag-chip'},label(a))),{label:['action','actions']});  // one line, +N (LIVE-56)
        // SW3-B-18 (maintenance): a granted action nobody it covers can do is kept as granted, and said: it is never used.
        if(!unusable.length)return list;const b=badge(`Never used: ${unusable.map(label).join(', ')}`,'warn');b.dataset.tip='Their roles cannot do this work themselves.';return el('span',{},list,' ',b);}},
      {key:'dates',label:'Dates',sort:p=>String(p.starts_on),cell:p=>dateUnits([formatDate(p.starts_on),'– '+(p.ends_on?formatDate(lastDay(p.ends_on)):p.end_label||'No end date')])},
      {key:'reason',label:'Reason',text:p=>String(p.reason),cell:p=>p.reason},
      // SW3-B-20: an ended permission says when, by whom and why it ended (its dates are the ones it was granted for).
      {key:'state',label:'Status',facet:p=>stage(p)[0],cell:p=>{const e=p.ended as {at?:string;by_name?:string|null;reason?:string}|null|undefined;
        return e?.at?el('span',{},badge(...stage(p)),el('small',{class:'muted grant-ended'},` ${formatDate(e.at,zone(ctx))}${e.by_name?` by ${e.by_name}`:''}${e.reason?` · “${e.reason}”`:''}`)):badge(...stage(p));}},
    ],
    rowActions:p=>admin&&p.state!=='ended'?[{label:'End permission',icon:'close',danger:true,run:()=>confirmAction('End permission',`${who(p)} will need a second person again.`,async reason=>{
      // SW3-D-17: the reply is the ended permission: the row says Ended at once, not after the page reads again.
      const r=await command<{permission?:Row}>('self_approval.revoke',{reason},p.id,p.revision);
      // The reply is the row as stored (no subject or state): a revoked permission is Ended (independence.permission_state).
      if(r?.permission?.revoked){data.permissions[data.permissions.indexOf(p)]={...p,...r.permission,state:'ended',ended:{at:new Date().toISOString(),by_name:ctx.me.display_name,reason}};list.refresh();}
      // The page reads again behind it (the dialog does not wait for that read).
      notify('Permission ended.');void ctx.refresh();},'End permission')}]:[]});
  section.append(el('div',{class:'panel'},list.root));
  return section;
}
const lastDay=(end:string)=>{const d=new Date(end+'T12:00:00Z');d.setUTCDate(d.getUTCDate()-1);return d.toISOString().slice(0,10);};
/** The actions each person and each role can take as their own work (maintenance, from INDEPENDENT_ACTION_ROLES). */
type Grantable={identities:Record<string,string[]>;roles:Record<string,string[]>};
async function selfApprovalForm(ctx:Context,actions:Action[],days?:number,grantable?:Grantable){
  // The people to choose from load first (the button shows it is busy); if they cannot be read, say so and offer Retry.
  let access:{identities:Row[]};
  try{access=await get<{identities:Row[]}>('/api/access');}
  catch(e){if(sessionChanged(e))return;
    notify(couldntLoad('the people to choose from',e),'error',{label:'Retry',run:()=>selfApprovalForm(ctx,actions,days,grantable)},e);return;}
  formDialog('Add a self-approval permission','Lets a person, or everyone with a role, decide their own work for the actions you tick. Every use still needs a reason and is recorded.',form=>{
    const kind=select('Applies to',[{value:'person',label:'One person'},{value:'role',label:'Everyone with a role'}],'person');
    const person=select('Person',access.identities.filter(i=>!i.disabled).map(i=>({value:i.id,label:String(i.display_name)})));
    const role=select('Role',ROLE_NAMES.map(r=>({value:r,label:roleLabel(r)})),APPROVER_ROLE);
    const boxes=actions.map(a=>({key:a.key,box:check(a.label,false)}));
    const today=toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
    const start=field('From','date',today,{required:true}),until=field('Until (included)','date'),reason=area('Reason','');reason.input.required=true;lastDayIncluded(until.input);
    // D4: an end date by default (the server's default length from the start, until one is typed; left empty, the
    // server applies the same default); an open-ended permission is the explicit "No end date".
    let typed=false;until.input.addEventListener('input',()=>{typed=true;});
    const defaultEnd=()=>{if(days&&!typed&&start.input.value)until.input.value=new Date(Date.parse(start.input.value+'T12:00:00Z')+days*86400000).toISOString().slice(0,10);};
    for(const t of ['input','change'])start.input.addEventListener(t,defaultEnd);defaultEnd();
    const noEnd=check('No end date',false);
    // F-A3: "No end date" empties the end (the dates then read open-ended); unticked, the end it had comes back.
    let kept='';until.input.placeholder='No end date';
    const syncEnd=()=>{const open=noEnd.input.checked;if(open&&until.input.value){kept=until.input.value;until.input.value='';until.input.dispatchEvent(new Event('change'));}
      else if(!open&&!until.input.value&&kept){until.input.value=kept;until.input.dispatchEvent(new Event('change'));}until.input.disabled=open;};
    noEnd.input.addEventListener('change',syncEnd);syncEnd();
    // SW3-B-18: only the actions the chosen person or role can do as their own work are offered (a permission for work
    // they can never do would never be used); the server refuses the rest too.
    const none=note('Their roles cannot do any of this work themselves, so there is nothing to grant.','info');none.classList.add('span-2');
    const offer=()=>{const can=!grantable?null:kind.input.value==='person'?grantable.identities[person.input.value]||[]:grantable.roles[role.input.value]||[];
      for(const b of boxes){const ok=!can||can.includes(b.key);b.box.wrap.hidden=!ok;if(!ok)b.box.input.checked=false;}
      none.hidden=boxes.some(b=>!b.box.wrap.hidden);};
    const sync=()=>{person.wrap.hidden=kind.input.value!=='person';person.input.required=!person.wrap.hidden;role.wrap.hidden=kind.input.value!=='role';offer();};
    for(const x of [kind.input,person.input,role.input])x.addEventListener('change',sync);sync();
    form.append(kind.wrap,person.wrap,role.wrap,el('fieldset',{class:'multi-field span-2'},el('legend',{},'May decide their own'),el('div',{class:'check-grid'},...boxes.map(b=>b.box.wrap)),none),dateRange('Dates',start,until),noEnd.wrap,reason.wrap);
    return async()=>{
      const picked=boxes.filter(b=>b.box.input.checked).map(b=>b.key);
      if(!picked.length)throw new Error('Tick at least one action.');
      const open=noEnd.input.checked;
      const value={actions:picked,starts_on:start.input.value,ends_on:open?null:until.input.value||null,...(open?{no_end_date:true}:{}),reason:reason.input.value,
        ...(kind.input.value==='person'?{identity_id:person.input.value}:{role:role.input.value})} as SelfApprovalInput;
      await command('self_approval.grant',value);notify('Permission added.');await ctx.refresh();
    };
  },'Add permission');
}
