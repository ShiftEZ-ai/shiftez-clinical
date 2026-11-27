import {get,command,request,ApiError,rotaMeta} from './api.js';
import {weekdayOptions,el,button,note,select,area,check,showDialog,table,notify,field,formDialog,multi,toLocal,icon,plural,formatDate,formatTime} from './dom.js';
import type {Row} from './types.js';
import {conditionBuilder,scopeAsConditions} from './conditions.js';
import {ruleConditionFields,type Context} from './forms.js';
import {candidateReview,diagnosticContext} from './candidate_review.js';
import {clock} from './loading.js';
import {waitingWord,checkCodeLabel,proposalProtection} from './rule_check_api.js';
import {TERMS} from './terms.js';

function checkProgress(job:Row){
  const elapsed=clock(Date.now()-new Date(job.started_at||job.created_at).getTime());
  const stages:Record<string,string>={checking:'Checking setup',preparing:'Preparing assignments',finding:'Checking feasibility',improving:'Checking feasibility',validating:'Validating result',explaining:'Explaining conflicts'};
  return `${job.progress?.check_phase==='baseline'?'Current rules':'Proposed rules'} · ${stages[job.progress?.stage]||'Working'} · ${elapsed}`;
}

/** One finding of a rota check in one plain line, with the check phase and index its candidates are read by. */
type Finding={key:string;text:string;worse:boolean;phase:string;index:number;item:Row};
/** What this rule changes on one rota: the findings it adds or makes worse (a shortage with fewer people able to work
 *  it), and those already there before it (owner, 13 Oct 2026: "Existing conflicts remain" twice, in two identical
 *  lists, said nothing about the rule). Shortages compare by requirement, clinics and time; errors by message. */
export function findingsDiff(result:Row,name:(kind:string,id:string)=>string,zone:string):{added:Finding[];existing:Finding[]}{
  const items=(phase:string):Finding[]=>{const c=result.checks?.[phase];if(!c)return [];
    const short=(c.diagnostics||[]).map((d:Row,index:number)=>({key:'d|'+JSON.stringify([d.requirement_name,d.capability_name,d.service_ids,d.start,d.end]),index,phase,item:d,worse:false,
      text:[[d.requirement_name,d.capability_name,...(d.service_ids||[]).map((id:string)=>name('services',id))].filter(Boolean).join(' · ')||'A staffing requirement',
        d.start&&d.end?`${formatDate(d.start,zone)} ${formatTime(d.start,zone)}–${formatTime(d.end,zone)}`:'',
        d.required!=null?`needs ${d.required}, ${Number(d.available_upper_bound)===0?'nobody can work it':`at most ${d.available_upper_bound} can work it`}`:''].filter(Boolean).join(' · ')}));
    const errors=[...(c.issues||[]),...(c.validation?.findings||[]).filter((f:Row)=>f.severity==='error')].map((f:Row,index:number)=>({key:'e|'+f.message,index,phase,item:f,worse:false,text:String(f.message)}));
    return [...short,...errors];};
  const before=new Map<string,Finding[]>();for(const f of items('baseline'))before.set(f.key,[...(before.get(f.key)||[]),f]);
  const added:Finding[]=[],existing:Finding[]=[];
  for(const f of items('proposed')){const was=before.get(f.key)?.shift();
    if(!was){added.push(f);continue;}
    const fewer=f.item.available_upper_bound!=null&&Number(f.item.available_upper_bound)<Number(was.item.available_upper_bound);
    if(fewer)added.push({...f,worse:true});else existing.push(f);}
  return {added,existing};
}
/** Review one rule, or several lines of one rule together (checked and approved in one decision). */
export async function ruleReview(given:Partial<Row>|Row[],canCheck:boolean,canApprove:boolean,refresh:()=>Promise<void>,name:(kind:string,id:string)=>string,zone:string,unsaved=false,replaces:Row[]=[],title='',summary='',context?:Context,factFamily=''){
  const lines=Array.isArray(given)?given:[given as Row],policy=lines[0];
  if(lines.length>100)throw new Error('Review at most 100 lines at a time: filter or select fewer lines.');
  const page=await get('/api/workspaces?limit=100&current_only=true');
  if(page.has_more)throw new Error('More than 100 rotas need review. Archive completed rotas before checking this rule.');
  // Rotas still being planned that overlap any line; published rotas keep the rules they were approved under.
  const work:Row[]=page.workspaces.filter((w:Row)=>['draft','submitted','approved'].includes(w.status)&&lines.some(p=>(!p.starts_on||p.starts_on<w.ends_on)&&(!p.ends_on||p.ends_on>w.starts_on)&&(!p.definition?.workspace_id||p.definition.workspace_id===w.id)));
  const ids=new Set(lines.map(p=>p.id));
  showDialog('Review '+(title||policy.name)+(lines.length>1?` · ${lines.length} lines`:''),'Check how this rule affects current rotas. Checks do not change assignments.',(body,close,_tryClose,onClose)=>{
    const results=new Map<string,Row>();
    const unsavedJobs=new Map<string,string>();
    const trials=new Map<string,string>();
    const trialResults=new Map<string,Row>();
    const list=el('div'),status=el('p',{role:'status','aria-live':'polite'});
    const budget=select('Check duration',[{value:'10',label:'Quick · 10 seconds search per configuration'},{value:'60',label:'Extended · 60 seconds search per configuration'}],'10');
    const acknowledgement=check('Activate even though some affected rotas are not checked or still have problems.');
    // A rule check's outcome comes from the server's comparison (what activation enforces); fact checks, and checks
    // stored before the comparison existed, fall back to the findings diff.
    const outcome=(result:Row,diff:{added:Finding[];existing:Finding[]})=>{
      if(factFamily||result.comparison_complete===undefined)return {added:diff.added.length,unchanged:diff.existing.length,unresolved:result.status==='inconclusive'&&!diff.existing.length};
      const complete=result.comparison_complete===true;
      return {added:complete?(result.new_or_worse_findings||[]).length:0,unchanged:Number(result.unchanged_existing_count)||0,unresolved:!complete};
    };
    const reason=area('Reason');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    let loading=false,timer:number|undefined,checksRequired=false;
    const run=async(w:Row)=>{
      const current=(await rotaMeta(w.id)).workspace;
      const queued=await command('job.create',{workspace_id:w.id,kind:'feasibility',check_seconds:Number(budget.input.value),...(factFamily?{fact_changes:lines.map(p=>({fact_id:p.id,expected_revision:p.revision}))}:unsaved?{proposed_rule:policy}:{rule_changes:lines.map(p=>({policy_id:p.id,expected_revision:p.revision}))})},undefined,current.revision);
      if(unsaved)unsavedJobs.set(w.id,queued.job.id);
    };
    const load=async()=>{
      if(loading||!body.isConnected)return;
      loading=true;window.clearTimeout(timer);
      try{
        // Every rota at once (each its own reads); progress shows per rota.
        await Promise.all(work.map(async w=>{
          const page=await get('/api/jobs?workspace_id='+w.id),jobs:Row[]=page.jobs;
          checksRequired=page.generation_capability?.engine==='cp_sat';
          const latest=unsaved?jobs.find(j=>j.id===unsavedJobs.get(w.id)):jobs.find(j=>j.kind==='feasibility'&&j.checked_rules?.length===lines.length&&j.checked_rules.every((c:Row)=>ids.has(c.policy_id)&&!c.temporary_change));
          if(latest)results.set(w.id,(await get('/api/jobs/'+latest.id)).job);else results.delete(w.id);
          const trialId=trials.get(w.id);
          if(trialId)trialResults.set(w.id,(await get('/api/jobs/'+trialId)).job);
        }));
        if(!body.isConnected)return;
        // Beside Activate: the rotas this review has not checked (Activate names no review that did not happen).
        const notChecked=work.filter(w=>{const j=results.get(w.id);return !j||j.check_stale||j.status!=="succeeded";}).length;
        unchecked.textContent=notChecked?`${plural(notChecked,"rota")} not checked`:"";
        list.replaceChildren(table(['Rota','Check result','Actions'],work.map(w=>{
          const job=results.get(w.id),result=job?.result;
          const live=job&&['queued','running'].includes(job.status);
          const diff=job?.status==='succeeded'&&result&&!job.check_stale?findingsDiff(result,name,zone):null;
          const adds=diff?diff.added.length:0,kept=diff?diff.existing.length:0;
          const {added,unchanged,unresolved}=job?.status==='succeeded'&&result&&!job.check_stale?outcome(result,diff!):{added:0,unchanged:0,unresolved:false};
          const label=!job?'Not checked':job.check_stale?'Inputs changed — check again':live?(waitingWord(job.status)||checkProgress(job)):job.status!=='succeeded'?(checkCodeLabel(job.error_code)||(job.status==='failed'?'Check failed · run it again':'Check '+job.status))
            :result.newly_introduced_contradiction?'This rule introduces a conflict':added?`Adds ${plural(added,'problem')}`
            :unresolved?(result.full_feasibility_established?'Works with this rule':result.status==='inconclusive'?'Ran out of time — try Extended':'Comparison unresolved')
            :unchanged?`No new problems from this rule · ${plural(unchanged,'existing problem')} unchanged`:result.full_feasibility_established?'Works with this rule':'No new problems from this rule';
          const tone=!job||live||job.check_stale?'':job.status!=='succeeded'||result.newly_introduced_contradiction||added?'err':unresolved&&!result.full_feasibility_established?'warn':'ok';
          const actions=el('div',{class:'actions'});
          if(canCheck&&!live)actions.append(button('Check rota',async()=>{await run(w);await load();}));
          const adjustable:Record<string,string>={hours:'Hours',minimum:'Minimum people',maximum:'Maximum',minutes:'Transition minutes',amount:'Quota amount',priority:'Allocation priority',session_priority:'Session priority',overtime_priority:'Overtime priority',balance_weight:'Balance importance',overflow_maximum:'Extra places',window_days:'Window days',reference_days:'Reference days',weekend_days:'Weekend days',leave_buffer_days:'Days around leave'};
          const keys=Object.keys(adjustable).filter(key=>typeof policy.definition?.rule?.[key]==='number');
          if(canCheck&&!unsaved&&!factFamily&&lines.length===1&&context)actions.append(button('Check a change',async()=>{
            const catalogues=await get('/api/catalogues');
            formDialog('Check a change to '+policy.name,'Try different settings on this rota. The saved rule and assignments will stay unchanged.',form=>{
            const fields=keys.map(key=>({key,control:field(adjustable[key],'number',policy.definition.rule[key],{required:true,step:'any'})}));
            const scope=policy.definition.scope;
            const who=conditionBuilder(ruleConditionFields({...context,catalogues}),scopeAsConditions(scope),{context:'rule'});
            const days=weekdayOptions();
            const scopeDays=multi('Weekdays',days,(scope.weekdays||[0,1,2,3,4,5,6]).map(String));
            const scopeStart=field('Starts at','text',scope.start_time||'00:00',{required:true});
            const scopeEnd=field('Ends at','text',scope.end_time||'24:00',{required:true});
            const ruleDays=Array.isArray(policy.definition.rule.weekdays)?multi('Rule weekdays',days,policy.definition.rule.weekdays.map(String)):null;
            const times=['start_time','end_time'].filter(key=>typeof policy.definition.rule[key]==='string').map(key=>({key,control:field(key==='start_time'?'Rule start time':'Rule end time','text',policy.definition.rule[key],{required:true})}));
            form.append(...fields.map(f=>f.control.wrap),...(ruleDays?[ruleDays.wrap]:[]),...times.map(f=>f.control.wrap),el('fieldset',{class:'span-2 form-grid'},el('legend',{},'Who and when'),who.root,scopeDays.wrap,scopeStart.wrap,scopeEnd.wrap));
            return async()=>{
              const definition=structuredClone(policy.definition);
              for(const {key,control} of fields)definition.rule[key]=Number(control.input.value);
              definition.scope={conditions:who.read(),weekdays:scopeDays.values().map(Number),start_time:scopeStart.input.value,end_time:scopeEnd.input.value};
              if(ruleDays)definition.rule.weekdays=ruleDays.values().map(Number);
              for(const {key,control} of times)definition.rule[key]=control.input.value;
              const checked=await request<{compatible:boolean;issues:Array<{message:string}>}>('/api/policies/check',{method:'POST',body:JSON.stringify({definition})});
              if(!checked.compatible)throw new Error(checked.issues.map(i=>i.message).join(' '));
              const current=(await rotaMeta(w.id)).workspace;
              const queued=await command('job.create',{workspace_id:w.id,kind:'feasibility',check_seconds:Number(budget.input.value),rule_changes:[{policy_id:policy.id,expected_revision:policy.revision,definition}]},undefined,current.revision);
              trials.set(w.id,queued.job.id);await load();
            };
          },'Check this change');}));
          const trialId=trials.get(w.id);
          const trialStatus=trialResults.get(w.id)?.status;
          if(trialId)actions.append(el('span',{role:'status'},trialStatus==='queued'?'Change check waiting':trialStatus==='running'?checkProgress(trialResults.get(w.id)!):trialStatus==='succeeded'?'Change check complete':'Change check '+trialStatus));
          if(trialId)actions.append(button('View change check',async()=>{
            const trial=(await get('/api/jobs/'+trialId)).job;
            showDialog('Change check','This result does not change or activate the saved rule.',details=>{
              const result=trial.result;
              details.append(note(trial.check_stale?'Inputs changed. Run a new check.':!result?'Check '+trial.status:result.full_feasibility_established?'A valid schedule was found with the proposed limits.':result.newly_introduced_contradiction?'The proposed limits introduce a conflict.':result.status==='requirements_conflict'?'Requirements still conflict.':'The check is unresolved.'));
              if(result?.conflict_resolved)details.append(note('The previously proven conflict is resolved for these inputs.'));
              if(result)details.append(el('p',{},`Completed in ${Number(result.elapsed_seconds).toFixed(1)} seconds`));
              for(const [index,finding] of (result?.checks?.proposed?.diagnostics||[]).entries()){
                details.append(diagnosticContext(finding,name,zone),note(finding.message));
                if(finding.candidate_count)details.append(button('View candidates for this shortage',()=>candidateReview(trial.id,name,zone,index,'proposed',finding)));
              }
              for(const finding of result?.checks?.proposed?.validation?.findings||[])details.append(note(finding.message));
            });
          }));
          // The findings this rule adds, each in one line with its one action; those already there only on demand.
          if(diff&&(adds||kept))actions.append(button(adds?`View ${plural(adds,'new problem')}`:`View existing (${kept})`,()=>showDialog(w.name||'Rota check','Only what this rule adds or makes worse. A check applies only to the inputs it checked.',details=>{
            const line=(f:Finding)=>el('li',{class:'review-finding'},el('span',{},f.worse?'Worse: ':'',f.text),
              f.item.candidate_count?button('View candidates',()=>candidateReview(job!.id,name,zone,f.index,f.phase,f.item),'secondary small'):'');
            details.append(el('p',{class:'review-result '+(adds?'err':'ok')},icon(adds?'alert':'check'),el('span',{},label)));
            if(result.protected_assignment_conflicts?.length)details.append(el('h3',{},'Published duties against this rule'),el('ul',{class:'review-findings'},
              ...result.protected_assignment_conflicts.map((c:Row)=>el('li',{class:'review-finding'},el('span',{},`${name('people',c.person_id)} · ${toLocal(c.start,zone)}–${toLocal(c.end,zone)}`+(c.detail?.required_hours!=null?` · ${c.detail.actual_hours}h available, ${c.detail.required_hours}h required`:''))))));
            if(adds)details.append(el('h3',{},`New with this rule (${adds})`),el('ul',{class:'review-findings'},...diff.added.map(line)));
            if(kept){const list=el('ul',{class:'review-findings muted'},...diff.existing.map(line));list.hidden=!!adds;
              const toggle=adds?button(`Show existing (${kept})`,()=>{list.hidden=!list.hidden;toggle!.textContent=list.hidden?`Show existing (${kept})`:`Hide existing (${kept})`;toggle!.setAttribute('aria-expanded',String(!list.hidden));},'',{class:'link-button','aria-expanded':'false'}):null;
              details.append(el('h3',{},`Already there before this rule (${kept})`),...(toggle?[toggle]:[]),list);}
          })));
          return [w.name||'Rota',el('span',{class:'review-result'+(tone?' '+tone:'')},tone?icon(tone==='ok'?'check':'alert'):'',el('span',{},label)),actions];
        })));
        status.textContent=work.length?`${work.length} affected rota${work.length===1?'':'s'}.`:'No rota being planned overlaps these dates. Published rotas keep the rules they were approved under.';
        if([...results.values(),...trialResults.values()].some(j=>['queued','running'].includes(j.status)))timer=window.setTimeout(()=>{void load();},3000);
        // The server asks for this when a rule check shows new or worse problems or an unresolved comparison, and for a
        // fact check whenever the rota is not fully feasible (policies.py RULE_CHECK_UNRESOLVED).
        const open=[...results.values()].filter(j=>j.status==='succeeded'&&j.result&&(factFamily?!j.result.full_feasibility_established:j.result.no_new_problems!==true));
        const onlyExisting=open.length>0&&open.every(j=>{if(j.check_stale||j.result.newly_introduced_contradiction)return false;const o=outcome(j.result,findingsDiff(j.result,name,zone));return !o.added&&!o.unresolved;});
        acknowledgement.wrap.hidden=!open.length;
        const ackText=acknowledgement.wrap.querySelector('.check-text>span');if(ackText)ackText.textContent=(onlyExisting?'Activate with the existing problems unchanged (they are not from this rule).':'Activate even though some affected rotas are not checked or still have problems.');
        // With the CP-SAT engine, activation needs a fresh check of every affected rota (commands.py require_checks,
        // policies.py RULE_CHECK_REQUIRED): say so beside the button, with the check to run, before anyone presses Activate.
        const state=(w:Row)=>{const j=results.get(w.id);return !j||j.check_stale||!['queued','running','succeeded'].includes(j.status)?'due':j.status==='succeeded'?'ok':'busy';};
        const due=work.filter(w=>state(w)==='due'),busy=work.filter(w=>state(w)==='busy');
        const rotas=(ws:Row[])=>ws.length===1?ws[0].name||'this rota':`${ws.length} rotas`;
        gate.replaceChildren();gate.hidden=!activate||!checksRequired||!due.length&&!busy.length;
        if(!gate.hidden&&due.length)gate.append(el('span',{},`Activation needs a fresh check of ${rotas(due)} first. `),canCheck?button(`Check ${rotas(due)}`,async()=>{await Promise.all(due.map(run));await load();},'primary small'):el('span',{},'Ask a coordinator to check it.'));
        else if(!gate.hidden)gate.append(`Checking ${rotas(busy)}. Activate when it finishes.`);
        updateActivation();
      }catch(e){status.textContent=(e as Error).message;}finally{loading=false;}
    };
    // What the approver is deciding on, before the checks: the rule in one sentence and where it comes from.
    if(summary)body.append(el('p',{class:'rule-review-summary'},el('strong',{},summary),policy.source?el('span',{class:'muted'},' · Source: '+policy.source):''));
    const protection=note(TERMS.reducesSafetyProtection,'warning');
    protection.id='rule-review-safety-protection';protection.hidden=true;
    const protectionStatus=el('span',{class:'hint',id:'rule-review-protection-status'});
    const protectionRetry=button('Retry',()=>reviewProtection(),'secondary small');protectionRetry.hidden=true;
    body.append(el('div',{role:'status'},protection,protectionStatus),protectionRetry);
    const controls=el('div',{class:'form-grid'});
    budget.wrap.classList.add('span-2');acknowledgement.wrap.classList.add('span-2');
    body.append(status,list,controls);list.hidden=!work.length;
    // Checks only when a rota being planned overlaps; otherwise the status line says so and nothing else is offered.
    if(work.length){
      const actions=el('div',{class:'actions'});
      if(canCheck){controls.append(budget.wrap);actions.append(button('Check all affected rotas',async()=>{await Promise.all(work.filter(w=>{const j=results.get(w.id);return !j||!['queued','running'].includes(j.status);}).map(run));await load();}));}
      actions.append(button('Refresh checks',load));controls.append(actions);
    }
    // Older active copies of the same rule (e.g. one per week) are retired when this one is activated, so it never applies twice.
    const replacing=replaces.length?multi(`Retire the ${replaces.length} older active cop${replaces.length===1?'y':'ies'} this rule replaces`,
      replaces.map(r=>({value:r.id,label:`${r.name} · ${r.starts_on}${r.ends_on?' – '+r.ends_on:''}`})),replaces.map(r=>r.id)):null;
    replacing?.wrap.classList.add('span-2');
    // The acknowledgement is asked only when a check actually came back unresolved.
    acknowledgement.wrap.hidden=true;
    // A refused activation is explained here, next to the button, with the check to run; the dialog stays open.
    const refusal=el('div',{class:'span-2'});refusal.hidden=true;
    const gate=el('p',{class:'span-2 hint rule-gate',role:'status'});gate.hidden=true;
    let activate:HTMLButtonElement|undefined;
    const unchecked=el('span',{class:'muted rc-unchecked',role:'status'});
    if(!unsaved&&canApprove&&lines.every(p=>p.status==='proposed'))controls.append(...(replacing?[replacing.wrap]:[]),acknowledgement.wrap,reason.wrap,refusal,gate,el('div',{class:'span-2 actions'},activate=button(factFamily?'Approve':lines.length>1?`Activate ${lines.length} lines`:'Activate',async()=>{
      if(activationBlocked()||!reason.input.reportValidity())return;
      const retire=replacing?.values()||[];
      const decision={reason:reason.input.value,check_job_ids:[...results.values()].map(j=>j.id),acknowledge_unresolved:acknowledgement.input.checked,replaces_ids:retire};
      refusal.hidden=true;refusal.replaceChildren();
      try{
        if(factFamily)await command(factFamily+'.activate',decision,policy.id,policy.revision);
        else if(lines.length>1)await command('policy.activate_many',{...decision,items:lines.map(p=>({policy_id:p.id,expected_revision:p.revision}))});
        else await command('policy.activate',decision,policy.id,policy.revision);
      }catch(e){
        if(!(e instanceof ApiError)||!e.code.startsWith('RULE_'))throw e;
        const target=work.find(w=>w.id===(e.details as Row|undefined)?.workspace_id),rota=target?.name||'this rota';
        const job=target&&results.get(target.id),live=job&&['queued','running'].includes(job.status);
        refusal.append(note(e.code==='RULE_CHECK_REQUIRED'?`Not activated: ${rota} needs a fresh check of ${lines.length>1?'these rules':'this rule'} first.`:`Not activated: ${e.message}`,'error',e));
        if(e.code==='RULE_CHECK_REQUIRED'&&target&&canCheck&&!live)refusal.append(button(`Check ${rota}`,async()=>{await run(target);refusal.hidden=true;refusal.replaceChildren();await load();},'primary small'));
        else if(e.code==='RULE_CHECK_REQUIRED')refusal.append(note(live?'The check is running; activate again when it finishes.':'Ask a coordinator to check this rota, then activate again.'));
        refusal.hidden=false;
        refusal.scrollIntoView({block:'nearest'});
        return;
      }
      // Confirm straight away; the page refresh that follows can take a few seconds on large rule sets.
      close();notify(factFamily?'Change approved.':`${lines.length>1?lines.length+' lines':'Rule'} activated${retire.length?`; ${retire.length} older cop${retire.length===1?'y':'ies'} retired`:''}.`);await refresh();
    },'primary'),
      // The other decision, with the same reason: the proposal is declined (kept in history, audited); nothing turns on.
      lines.length===1&&!factFamily?button('Decline',async()=>{if(!reason.input.reportValidity())return;
        await command('policy.decline',{reason:reason.input.value},policy.id,policy.revision);close();notify('Proposal declined.');await refresh();},'secondary'):'',unchecked));  // the button and what it has not checked, in one row
    let protectionRead:AbortController|undefined;
    let protectionPending=false,protectionFailed=false,needsReviewer=false;
    const isProposer=(p:Row)=>p.created_by===context?.me.identity_id;
    const ownsProposal=lines.some(p=>p.status==='proposed'&&isProposer(p));
    const stopProtection=()=>{protectionRead?.abort();protectionRead=undefined;};
    onClose(stopProtection);
    const activationBlocked=()=>protectionPending||(protectionFailed&&ownsProposal)||needsReviewer||!gate.hidden;
    const updateActivation=()=>{
      if(!activate)return;
      activate.disabled=activationBlocked();
      const description=protectionPending||protectionFailed?protectionStatus.id:protection.hidden?'':protection.id;
      if(description)activate.setAttribute('aria-describedby',description);else activate.removeAttribute('aria-describedby');
    };
    const reviewProtection=async():Promise<void>=>{
      if(unsaved||factFamily)return;
      stopProtection();
      const selected=replacing?.values()||[];
      // As policy.activate_many does, associate explicit replacements with the first proposal.
      const proposed=lines.filter(p=>p.status==='proposed'&&(p===policy&&selected.length||
        p.definition?.replaces_id||p.definition?.merged_ids?.length||p.definition?.calendar_entry_id));
      protection.hidden=true;needsReviewer=false;protectionFailed=false;protectionPending=proposed.length>0;
      protectionStatus.textContent=protectionPending?'Checking safety protection…':'';
      protectionRetry.hidden=true;
      updateActivation();
      if(!protectionPending)return;
      const read=protectionRead=new AbortController();
      try{
        const reviews=await Promise.all(proposed.map(p=>proposalProtection(p.id,p===policy?selected:[],read.signal)));
        if(protectionRead!==read)return;
        const weakened=new Set(reviews.filter(r=>r.weakens_safety).map(r=>r.policy_id));
        protection.hidden=!weakened.size;
        needsReviewer=proposed.some(p=>weakened.has(p.id)&&isProposer(p));
        protectionStatus.replaceChildren();
      }catch(error){
        if(protectionRead!==read)return;
        protectionFailed=true;
        protectionStatus.textContent=error instanceof ApiError&&error.code==='READ_TIMEOUT'
          ?error.message:'Could not check safety protection.';
        protectionRetry.hidden=false;
      }finally{
        if(protectionRead===read){stopProtection();protectionPending=false;updateActivation();}
      }
    };
    replacing?.wrap.addEventListener('change',()=>void reviewProtection());
    void reviewProtection();void load();
  });
}
