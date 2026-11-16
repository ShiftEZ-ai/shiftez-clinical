import {avatar,el,field,select,check,button,note,table,toLocal,formatTime,labelOf} from './dom.js';
import {fullNameOf} from './person_label.js';
import {collectionToolbar} from './table_view.js';
import {dockPanel} from './side_panel.js';
import {request,instant} from './api.js';
import {shiftForm,type Context} from './forms.js';
import type {Row,WorkspaceData} from './types.js';
import type {StaffingSuggestionsInput} from './generated.js';
import {normalSearch,sameSearch,type SearchOptions} from './search.js';

/** Named options checked per request: few at a time (each is a full validation). */
const PAGE=10;

/** Choosing a candidate opens the ordinary editor. It never saves or consents. */
export function staffingDialog(ctx:Context,data:WorkspaceData,shift:Row){
  const zone=ctx.me.organisation.timezone;
  dockPanel({key:'staffing',title:'Staffing and replacement options',width:620,
    sub:'Each named option is checked against the same current validator. Results are alphabetical, not a fairness ranking or proof of complete availability.',build:({body,close})=>{
    const mode=select('Staffing action',[{value:'add',label:'Add a participant'},...shift.participants.map((p:Row)=>({value:p.id,label:'Replace '+fullNameOf(ctx.catalogues.people.find(person=>person.id===p.person_id))}))],'add');
    const start=field('Proposed reserved start','datetime-local',toLocal(shift.start,zone),{required:true});
    const end=field('Proposed reserved end','datetime-local',toLocal(shift.end,zone),{required:true});
    start.input.readOnly=true;end.input.readOnly=true;
    const role=select('Proposed participation role',['staff','trainee','supervisor'].map(value=>({value,label:value})),'staff');
    const overtime=check('Proposed planned overtime',false);role.wrap.classList.add('span-2');
    const addition=el('div',{class:'form-grid'},start.wrap,end.wrap,role.wrap,overtime.wrap);
    // The options are a collection: the shared toolbar's live search, then "Show 10 more" (the server pages them, as
    // each one is checked with the validator). Changing the proposed values asks for a new calculation; changing the
    // search restarts a pending or finished one, and each page belongs to the search that produced its first page.
    const output=el('div',{'aria-live':'polite'});let query='',pageQuery='',opts:SearchOptions|undefined,pageOpts:SearchOptions|undefined,epoch=0,active=false,shown:Row[]=[],last:Row|null=null;
    const bar=collectionToolbar({label:'Staffing options',search:{value:'',label:'Find staff by name',placeholder:'Find staff by name',wait:300,
      onInput:q=>{query=q.slice(0,80);},apply:()=>{if(active&&(query!==pageQuery||!sameSearch(opts,pageOpts)))void load(true);},
      // A person's own name only (the server refuses related scopes here): Contains or Exact text.
      options:{value:()=>opts,set:o=>{opts=o;},can:{scopes:['all'],kinds:[]}}}});
    const invalidate=()=>{epoch++;active=false;shown=[];last=null;addition.hidden=mode.input.value!=='add';output.replaceChildren(note('Calculate options for these values. Nothing has been saved.'));};
    for(const input of [mode.input,start.input,end.input,role.input,overtime.input])input.addEventListener('input',invalidate);
    mode.input.addEventListener('change',invalidate);
    const review=(result:Row,candidate:Row)=>button('Review '+candidate.display_name+' in draft',()=>{
      const updated=structuredClone(shift),participant={person_id:candidate.person_id,...result.slot};
      updated.participants=result.replace_assignment_id?updated.participants.map((p:Row)=>p.id===result.replace_assignment_id?participant:p):[...updated.participants,participant];
      epoch++;close();shiftForm(ctx,data,updated);
    },candidate.person_blockers?'secondary':'primary');
    const row=(result:Row,candidate:Row,ok:boolean)=>{
      const mine=candidate.findings.filter((f:Row)=>f.person_id===candidate.person_id),reasons=mine.length?mine:candidate.findings.slice(0,3);
      // The findings in full, under a heading that says what they are (not folded away).
      const details=el('section',{class:'candidate-details'},el('h4',{},`${candidate.person_blockers} person-specific blockers · ${candidate.mandatory_findings_remaining} mandatory findings remain with this person`),
        candidate.findings.length?table(['Severity','Finding'],candidate.findings.map((f:Row)=>[f.severity,f.message])):'');
      if(candidate.findings_partial)details.append(note(`${candidate.findings_total} relevant findings; showing the first 100. Inspect the full draft validation before any decision.`,'warning'));
      return el('section',{class:'nested-card candidate '+(ok?'ok':'blocked')},
        el('div',{class:'candidate-head'},avatar(String(candidate.display_name)),el('h3',{},candidate.display_name),review(result,candidate)),
        el('ul',{class:'reason-chips'},...(ok?[el('li',{class:'ok'},'No person-specific issues')]:reasons.slice(0,4).map((f:Row)=>el('li',{class:f.severity==='error'?'err':'warn'},f.message)))),details);
    };
    function draw(result:Row){
      const can=shown.filter(c=>!c.person_blockers),cannot=shown.filter(c=>c.person_blockers);bar.shown(result.total,undefined,['person','people']);  // searched on the server
      output.replaceChildren(note(`${shown.length} of ${result.total} matching people checked. The original draft has ${result.baseline_mandatory_findings} mandatory findings. No changes saved.`),
        note(`Reserved slot: ${formatTime(result.slot.start,zone)}–${formatTime(result.slot.end,zone)} · ${result.slot.participation}${result.slot.segments.length?' · exact existing presence segments retained':''}. All findings must be resolved before review/publication.`));
      if(can.length)output.append(el('h3',{class:'candidate-group'},`Can work this session · ${can.length}`),...can.map(c=>row(result,c,true)));
      if(cannot.length)output.append(el('h3',{class:'candidate-group'},`Not available or would break a rule · ${cannot.length}`),...cannot.map(c=>row(result,c,false)));
      if(!shown.length)output.append(note('No unassigned departmental people match this search. This is not a proof that no feasible staffing exists.'));
      const left=result.total-shown.length;
      if(result.has_more&&left>0)output.append(el('div',{class:'actions dt-more-row'},el('span',{class:'muted'},`Showing ${shown.length} of ${result.total}`),
        button(`Show ${Math.min(left,PAGE)} more`,()=>load(false),'secondary small')));
      output.append(note(result.basis));
    }
    async function load(fresh:boolean){
      // `query` is the box's text as typed (only the read waits for a pause), so a fresh calculation uses all of it.
      const started=++epoch;if(fresh){pageQuery=query;pageOpts=normalSearch(opts);active=true;shown=[];last=null;output.replaceChildren(note('Evaluating named options. No assignments are saved.'));}
      try{
        const value:StaffingSuggestionsInput={workspace_id:data.workspace.id,shift_id:shift.id,expected_revision:data.workspace.revision,
          search:pageQuery,...(pageOpts?{search_options:pageOpts}:{}),offset:shown.length,limit:PAGE};
        if(mode.input.value==='add'){
          if(!start.input.reportValidity()||!end.input.reportValidity())throw new Error('Enter valid proposed start and end times.');
          value.slot={start:await instant(start.input.value),end:await instant(end.input.value),participation:role.input.value as 'staff'|'trainee'|'supervisor',overtime:overtime.input.checked,segments:[]};
        }else value.replace_assignment_id=mode.input.value;
        const result=await request<Row>('/api/planner/staffing-suggestions',{method:'POST',body:JSON.stringify(value)});
        if(started!==epoch)return;
        shown=[...shown,...result.candidates];last=result;draw(result);
      }catch(error){if(started===epoch)output.replaceChildren(note((error as Error).message,'error',error),button('Try again',()=>load(true),'secondary small'));}
    }
    body.append(mode.wrap,addition,bar.root,button('Calculate staffing options',()=>load(true),'primary'),output);invalidate();
  }});
}
