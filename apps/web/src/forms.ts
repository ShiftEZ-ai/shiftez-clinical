import {skeleton,working} from './loading.js';
import {fullNameOf} from './person_label.js';
import {ruleReview} from './rule_review.js';
import {draftImpact} from './draft_impact.js';
import {helpButton,icon,popover,menuItem,multiPick,type Choice,type IconName,emptyLine,CAPABILITY} from './dom.js';
import {WEEKDAYS,weekOrder,weekdayOptions,weekdayNames,routineReason,inlineCheck,el,field,area,select,check,multi,button,iconButton,formDialog,formInto,showDialog,note,notify,labelOf,table,dateUnits,formatDate,formatTime,toLocal,spanLabel,confirmAction,roleLabel,statusBadge,type Field} from './dom.js';
import {isolate,autoCode,plural,announce,suggested,rankColour,markFields,PROBLEM_HUES} from './dom.js';
import {command,get,instant,knownRevision,plainWords,request,rotaMeta} from './api.js';
import {fillOptions} from './candidates.js';
import {overlapChoice,type Overlaps} from './schedule.js';
import {operatingCalendarFields,resourceRequirementsBuilder} from './resource_forms.js';
import {configurationImpact} from './configuration_impact.js';
import {runBulk} from './bulk_change.js';
import {leaveTypes,leaveTypeName} from './findings.js';
import {TERMS,onlyRotaCoordinator} from './terms.js';
import {singularOf} from './config_kinds.js';
import {dockPanel,rowMark} from './side_panel.js';
import {missingSetup,type SetupOpeners} from './setup_guide.js';
import type {Me,Catalogues,Row,PersonRow,WorkspaceData} from './types.js';
import {mayDecide} from './independence.js';
import type {LeadContextInput,RotaCoverageGap,CustomField,TypeDefinition,RequirementSegment,Supervision,Command,DepartmentContentInput} from './generated.js';
import {CONTACT_KINDS,CONTACT_VISIBILITY} from './contacts.js';
import {TAG_LIMITS,ROLE_NAMES,USUAL_ASSIGNEES_FIXED_DEFAULT,WEEKDAY_SHORT_LABELS,RULE_KINDS as GEN_RULE_KINDS,RULE_HELP as GEN_RULE_HELP,RULE_NUMBER_FIELD as GEN_RULE_NUMBER_FIELD,RULE_TOPICS as GEN_RULE_TOPICS,LINE_KINDS,PER_PERSON_KINDS,LINE_NUMBER_FIELDS,LINE_SETTINGS,ANY_LINE_SETTINGS,RULE_CATALOGUE,RULE_CATALOGUE_V2,NEVER_OVERRIDDEN} from './generated.js';
import type {Conditions,PersonAmount,LineSettings,RuleLine} from './generated.js';
import {tagChoices,tagLabel} from './tags.js';
import {conditionBuilder,conditionsText,lineMatches,treeMatches,scopeAsConditions,withoutFields,ownLines,RULE_FIELDS,SESSION_FIELDS,type ConditionField} from './conditions.js';
import {goalBuilder,goalPreview,STRENGTHS} from './balance_goal.js';
import {dateRange,dateList,lastDayIncluded,showDay} from './date_picker.js';
import {addDays,needsClinic} from './planner_logic.js';
import {dateSetEntries,entryChoices} from './date_sets.js';
import {liveClashCheck} from './double_booking.js';
import {BASELINE_SOURCES,GAPS_SHOWN,nearestGaps} from './draft_baseline.js';
import {issueList,findingIssue} from './issue_marker.js';
import {leadContext,leadState,leadNeed,type LeadState} from './planner_teams.js';
import {menu,opens} from './dom.js';  // the shared menu (UX › controls S2b)
import {panelSearch} from './dom.js';  // the one search box in a panel (UX › controls S10)

/** `ready` resolves once the deferred catalogue lists are loaded (app.ts ensureCatalogues): await it before reading them. */
export interface Context {me:Me;catalogues:Catalogues;ready:()=>Promise<void>;refresh:()=>Promise<void>;navigate:(view:string)=>void;setWorkspace:(id:string)=>void;permits:(...keys:string[])=>boolean;
  /** Where setup steps are done (missingSetup): absent where no setup can be offered. */
  setup?:SetupOpeners;
  /** Opens a draft session's fill panel on the Schedule, its search set to `person` (a name). */
  fill?:(workspace:string,shift:Row,person?:string)=>void}
/** A participant's own times and presence segments as the server holds them. */
export type Attendance={start:string;end:string;segments?:{start:string;end:string}[]};
/** What a session save keeps of one participant's attendance. The server books every person for the whole session
 *  (FULL_ASSIGNMENT_REQUIRED), so partial attendance is its presence segments, whose outer edges are the session's times.
 *  Unchanged session times: the row's own segments are sent verbatim. Changed times: the segments still inside the new
 *  times are kept and the outer edges move to the new start and end; `cut` says a segment no longer fits (it is
 *  removed, and the form names the person before saving). Never a silent replacement with a whole-duty booking. */
export function keptAttendance(own:Attendance,was:{start:string;end:string},start:string,end:string):{start:string;end:string;segments:{start:string;end:string}[];cut:boolean}{
  const t=Date.parse,s=t(start),e=t(end),parts=own.segments||[];
  if(!parts.length)return {start,end,segments:[],cut:false};
  if(t(was.start)===s&&t(was.end)===e)return {start:own.start,end:own.end,segments:parts,cut:false};
  const kept=parts.filter(x=>t(x.start)>=s&&t(x.end)<=e).map(x=>({...x})),cut=kept.length<parts.length;
  if(kept.length<2)return {start,end,segments:[],cut:cut||kept.length===1&&(t(kept[0].start)!==s||t(kept[0].end)!==e)};
  kept[0].start=start;kept[kept.length-1].end=end;
  return {start,end,segments:kept,cut};
}
const options=(rows:Row[])=>rows.map(r=>({value:r.id,label:labelOf(r)+(r.active===false?' · inactive':'')}));
function all(ctx:Context,kind:'people'):PersonRow[];
function all(ctx:Context,kind:string):Row[];
function all(ctx:Context,kind:string):Row[]{return ctx.catalogues[kind]||[];}
/** Rank choices, each chip in its rank's one tint (`rankColour`, C21). */
const rankOptions=(ctx:Context)=>options(all(ctx,'categories')).map(c=>({...c,tone:'duty-'+rankColour(c.value)}));
/** Dated facts apply from their start, up to (but not including) their end. */
export const activeOn=(r:Row|undefined,day:string)=>!!r&&!r.revoked&&r.active!==false&&(!r.starts_on||r.starts_on<=day)&&(!r.ends_on||r.ends_on>day);
/** People as picker choices, grouped by their current rank. */
/** Why this person may not lead a session of this duty type on this day ('' = may lead, or no lead setting). Only the
 *  fallback while the server's lead context (planner_teams.ts leadContext) is unavailable: a hint mirroring
 *  packages/domain/conditions.py over the loaded catalogues (one missing is not held against anyone); the server check
 *  is authoritative. */
export function leadBlock(ctx:Context,typeId:string,personId:string,day:string){
  const lead=lookup(ctx,'assignment_types',typeId)?.definition?.lead;
  const tree=lead&&scopeAsConditions({capability_ids:lead.capability_ids,category_ids:lead.category_ids,conditions:lead.conditions});
  if(!tree||!personId)return '';
  const on=(r:Row|undefined)=>activeOn(r,day);
  // A rotation counts while it runs and its cohort (and the cohort's programme) is current, as conditions.py current_rotation.
  const rotationsNow=()=>all(ctx,'rotations').filter(r=>{if(r.person_id!==personId||!on(r))return false;const c=lookup(ctx,'cohorts',r.cohort_id);
    if(ctx.catalogues.cohorts&&!on(c))return false;return !c?.programme_id||!ctx.catalogues.programmes||on(lookup(ctx,'programmes',c.programme_id));});
  const mine=(kind:string,key:string)=>ctx.catalogues[kind]?new Set(all(ctx,kind).filter(r=>r.person_id===personId&&on(r)).map(r=>r[key])):null;
  const holds=(f:string):Set<string>|null=>f==='person_ids'?new Set([personId]):f==='tags'?new Set((lookup(ctx,'people',personId)?.tags||[]) as string[])
    :f==='category_ids'?mine('memberships','category_id'):f==='capability_ids'?mine('grants','capability_id'):f==='programme_ids'?mine('programme_memberships','programme_id')
    :f==='eligible_service_ids'?mine('eligibilities','service_id'):f==='rotation_type_ids'&&ctx.catalogues.rotations?new Set(rotationsNow().filter(r=>!ctx.catalogues.rotation_types||on(lookup(ctx,'rotation_types',r.rotation_type_id))).map(r=>r.rotation_type_id))
    :f==='cohort_ids'&&ctx.catalogues.rotations?new Set(rotationsNow().map(r=>r.cohort_id)):null;
  // A fact whose list is not loaded is not held against anyone; the rest is the shared walk and line test.
  if(treeMatches(tree,c=>{const held=holds(c.field);return !held||lineMatches(c,[...held]);}))return '';
  const kind=(f:string)=>RULE_FIELDS.find(x=>x.key===f)?.kind;
  return 'Cannot lead · who may lead: '+conditionsText(tree,(f,v)=>kind(f)?labelOf(lookup(ctx,kind(f)!,v)||{name:v}):v);
}
export function peopleChoices(ctx:Context){
  const day=today(ctx),rank=new Map<string,string>();
  for(const m of all(ctx,'memberships'))if(activeOn(m,day)&&!rank.has(m.person_id))rank.set(m.person_id,labelOf(lookup(ctx,'categories',m.category_id))||'');
  // Searchable by code, staff number, rota names and tags too; grouped by today's rank so a whole rank can be picked at once.
  const person=new Map(all(ctx,'people').map(p=>[p.id,p]));
  return options(all(ctx,'people')).map(o=>{const p=person.get(o.value);return {...o,group:rank.get(o.value)||'No rank',hint:String(p?.short_code||''),
    keywords:[p?.staff_number,...((p?.aliases||[]) as string[]),...((p?.tags||[]) as string[])].filter(Boolean).join(' ')};}).sort((a,b)=>a.group.localeCompare(b.group)||a.label.localeCompare(b.label,undefined,{numeric:true}));
}
function lookup(ctx:Context,kind:'people',id:string):PersonRow|undefined;
function lookup(ctx:Context,kind:string,id:string):Row|undefined;
function lookup(ctx:Context,kind:string,id:string):Row|undefined{return all(ctx,kind).find(r=>r.id===id);}
const today=(ctx:Context)=>toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
const optionalNumber=(input:HTMLInputElement)=>input.value===''?null:Number(input.value);
/** How many people a session of this duty type needs, in words ("2 people · with skills"); the Duty types list says the same. */
export function staffNeeded(type:Row|undefined){const q=(type?.definition?.requirements||[]) as Row[];if(!q.length)return 'No minimum';
  const min=placesOf(type),skills=q.some(x=>x.capabilities?.length);return `${min} ${min===1?'person':'people'}${skills?' · with skills':''}${type!.definition.supervision?' · supervised trainees':''}`;}
/** The fewest people a session of this duty type needs at its busiest (0: no minimum). */
export const placesOf=(type:Row|undefined)=>Math.max(0,...((type?.definition?.requirements||[]) as Row[]).map(x=>x.minimum||0));
// SW3-D-12: a section's explanation sits behind ⓘ beside its heading, not as a paragraph under every heading.
const part=(title:string,description:string,...children:Node[])=>el('section',{class:'form-section'},el('h3',{class:'form-section-head'},title,description?helpButton('About '+title,description,'field-help'):''),el('div',{class:'form-grid'},...children));

function customFieldsBuilder(initial:CustomField[]=[]){
  const body=el('div',{class:'span-2'}),rows:{node:HTMLElement;read:()=>CustomField}[]=[];
  function add(value?:CustomField){
    const key=field('Field key','text',value?.key||'',{required:true,pattern:'[a-z][a-z0-9_]{0,39}',placeholder:'review_reference'});
    const label=field('Field label','text',value?.label||'',{required:true,maxlength:160});
    const kind=select('Field type',['text','long_text','number','boolean','date','choice','multi_choice'].map(v=>({value:v,label:v.replace('_',' ')})),value?.kind||'text');
    const required=check('Required',value?.required||false),exported=check('Include in exports',value?.export||false);
    const visibility=select('Field visibility',[{value:'staff',label:'Staff and planners'},{value:'planners',label:'Planners only'}],value?.visibility||'staff');
    const minimum=field('Field minimum','number',value?.minimum??'',{step:'any'}),maximum=field('Field maximum','number',value?.maximum??'',{step:'any'});
    const choices=area('Allowed options · one per line',(value?.options||[]).join('\n'),2);
    const help=field('Help text','text',value?.help_text||'',{maxlength:400});
    const condition=field('Required when field key','text',value?.required_when?.field||'',{placeholder:'Optional: another scalar field key'});
    const equals=field('Required when equals','text',String(value?.required_when?.equals??''),{placeholder:'Exact text, number or true/false'});
    const node=el('div',{class:'nested-card'},el('div',{class:'mini-grid'},key.wrap,label.wrap,kind.wrap,visibility.wrap,minimum.wrap,maximum.wrap),choices.wrap,help.wrap,el('div',{class:'check-grid'},required.wrap,exported.wrap),el('div',{role:'group','aria-label':'Conditional requirement'},el('p',{class:'dt-pop-label'},'Conditional requirement'),el('div',{class:'mini-grid'},condition.wrap,equals.wrap)));
    function update(){const numeric=kind.input.value==='number';minimum.input.disabled=!numeric;maximum.input.disabled=!numeric;choices.input.disabled=!['choice','multi_choice'].includes(kind.input.value);}
    kind.input.addEventListener('change',update);update();
    const row={node,read:():CustomField=>{
      const result:CustomField={key:key.input.value,label:label.input.value,kind:kind.input.value as CustomField['kind'],required:required.input.checked,export:exported.input.checked,visibility:visibility.input.value as 'staff'|'planners',help_text:help.input.value,
        options:['choice','multi_choice'].includes(kind.input.value)?choices.input.value.split('\n').map(v=>v.trim()).filter(Boolean):[],
        minimum:kind.input.value==='number'?optionalNumber(minimum.input):null,maximum:kind.input.value==='number'?optionalNumber(maximum.input):null};
      if(condition.input.value){let value:string|boolean|number=equals.input.value;if(value==='true'||value==='false')value=value==='true';else if(value.trim()!==''&&!Number.isNaN(Number(value)))value=Number(value);result.required_when={field:condition.input.value,equals:value};}
      return result;
    }};
    rows.push(row);node.append(button('Remove field',()=>{rows.splice(rows.indexOf(row),1);node.remove();},'ghost'));body.append(node);
  }
  initial.forEach(add);
  const wrap=part('Extended fields','Safe, versioned attributes. They never grant clinical eligibility or application permissions.',body,button('Add custom field',()=>add(),'secondary'));
  return {wrap,read:()=>rows.map(r=>r.read())};
}

/** − / + buttons around a number field; typing still works. */
function stepper(f:Field){
  const change=(d:number)=>{const i=f.input as HTMLInputElement;try{d>0?i.stepUp():i.stepDown();}catch{i.value=String(Math.max(Number(i.min||0),(Number(i.value)||0)+d));}i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));};
  const minus=el('button',{type:'button',class:'step-btn','aria-label':'Decrease '+(f.wrap.querySelector('label')?.textContent||'')},'−'),plus=el('button',{type:'button',class:'step-btn','aria-label':'Increase '+(f.wrap.querySelector('label')?.textContent||'')},'+');
  minus.addEventListener('click',()=>change(-1));plus.addEventListener('click',()=>change(1));
  const input=f.input;const box=el('div',{class:'stepper-box'});input.replaceWith(box);box.append(minus,input,plus);f.wrap.classList.add('has-stepper');return f;
}
function requirementsBuilder(ctx:Context,initial:RequirementSegment[]=[],sessionMinutes:()=>number=()=>480){
  const body=el('div',{class:'span-2'}),rows:{node:HTMLElement;read:()=>RequirementSegment}[]=[];
  const timeline=el('div',{class:'staff-timeline span-2','aria-live':'polite'});
  const clock=(m:number)=>`+${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}`;
  function drawTimeline(){
    const total=Math.max(sessionMinutes(),...rows.map(r=>r.read().to_minute||0),1);
    timeline.replaceChildren(...rows.map(r=>r.read()).map(v=>({...v,from_minute:v.from_minute??0,minimum:v.minimum??0,capabilities:v.capabilities||[]})).sort((a,b)=>a.from_minute-b.from_minute).map(v=>{
      const to=v.to_minute??total,seg=el('span',{class:'staff-seg'+(v.minimum?'':' none'),'data-tip':`${clock(v.from_minute)} to ${v.to_minute==null?'session end':clock(to)}`},
        el('strong',{},`${v.minimum}${v.target!=null&&v.target!==v.minimum?'–'+v.target:''}${v.maximum!=null?' (max '+v.maximum+')':''}`),el('small',{},v.capabilities.length?`${v.capabilities.length} skill rule${v.capabilities.length>1?'s':''}`:`${v.minimum===1?'person':'people'}`));
      seg.style.setProperty('flex-grow',String(Math.max(1,to-v.from_minute)));return seg;}),el('span',{class:'staff-scale'},el('span',{},'Start'),el('span',{},'End')));
  }
  body.addEventListener('input',drawTimeline);body.addEventListener('change',drawTimeline);
  function add(value?:RequirementSegment){
    const from=field('Requirement from minute','number',value?.from_minute??0,{required:true,min:0,max:2160});
    const to=field('Requirement to minute','number',value?.to_minute??'',{min:1,max:2160,placeholder:'Session end'});
    const minimum=stepper(field('Minimum staff','number',value?.minimum??1,{min:0,max:100,required:true}));
    const target=stepper(field('Target staff · advisory','number',value?.target??'',{min:0,max:100}));
    const maximum=stepper(field('Maximum participants','number',value?.maximum??'',{min:0,max:100}));
    const distinct=check('Capability clauses require distinct people',value?.distinct_capability_seats||false);
    const clauses:{node:HTMLElement;read:()=>{capability_id:string;minimum:number}}[]=[];
    const clauseBody=el('div',{});
    function addClause(v?:{capability_id:string;minimum:number}){
      const capability=select('Required capability',options(all(ctx,'capabilities')),v?.capability_id||'');
      const number=field('Qualified people required','number',v?.minimum??1,{required:true,min:1,max:50});
      const node=el('div',{class:'nested-card'},el('div',{class:'mini-grid'},capability.wrap,number.wrap));
      const row={node,read:()=>({capability_id:capability.input.value,minimum:Number(number.input.value)})};clauses.push(row);
      node.append(button('Remove capability clause',()=>{clauses.splice(clauses.indexOf(row),1);node.remove();},'ghost'));clauseBody.append(node);
    }
    (value?.capabilities||[]).forEach(addClause);
    const node=el('div',{class:'nested-card'},el('div',{class:'mini-grid'},from.wrap,to.wrap,minimum.wrap,target.wrap,maximum.wrap),clauseBody,
      button('Add capability clause',()=>addClause(),'secondary'),distinct.wrap);
    const row={node,read:():RequirementSegment=>({from_minute:Number(from.input.value),to_minute:optionalNumber(to.input),minimum:Number(minimum.input.value),target:optionalNumber(target.input),maximum:optionalNumber(maximum.input),capabilities:clauses.map(c=>c.read()),distinct_capability_seats:distinct.input.checked})};
    rows.push(row);node.append(button('Remove staffing interval',()=>{rows.splice(rows.indexOf(row),1);node.remove();drawTimeline();},'ghost'));body.append(node);drawTimeline();
  }
  (initial.length?initial:[{minimum:1}]).forEach(add);
  // Split the last interval in two so staffing can change part-way through the session.
  const split=button('+ Split at another time',()=>{const last=rows.at(-1);if(!last)return add();const v=last.read(),from=v.from_minute??0,end=v.to_minute??sessionMinutes(),mid=Math.round((from+end)/2/15)*15;
    if(mid<=from||mid>=end)throw new Error('This interval is too short to split.');
    const toInput=[...last.node.querySelectorAll<HTMLInputElement>('input')].find(i=>i.closest('.field')?.querySelector('label')?.textContent?.startsWith('Requirement to minute'));if(toInput){toInput.value=String(mid);}
    add({...v,from_minute:mid,to_minute:v.to_minute});drawTimeline();
    // A structural change like any typed one (AOC-01): the shared dirty guard and the impact preview hear it.
    toInput?.dispatchEvent(new Event('input',{bubbles:true}));toInput?.dispatchEvent(new Event('change',{bubbles:true}));},'secondary');
  return {wrap:part('Staffing through time','How many people the session needs, and when. Minutes count from the session start; split the session when staffing changes part-way through.',timeline,body,el('div',{class:'actions'},split,button('Add staffing interval',()=>add(),'ghost'))),read:()=>rows.map(r=>r.read())};
}

/** A rotation plan's blocks, in order (a rotation template, or Plan rotations): what each block is and how long. */
export type RotationBlock={rotation_type_id:string|null;service_id:string|null;length:number;unit:'weeks'|'months';department_available:boolean};
export function rotationBlocks(ctx:Context,initial:RotationBlock[]=[]){
  const body=el('ol',{class:'block-list span-2'}),rows:{node:HTMLElement;read:()=>RotationBlock}[]=[];
  const count=el('p',{class:'hint span-2','aria-live':'polite'});
  const total=()=>{const w=rows.reduce((n,r)=>{const v=r.read();return n+(v.unit==='weeks'?v.length:v.length*52/12);},0);
    count.textContent=rows.length?`${plural(rows.length,'block')} · about ${Math.round(w/52*12*10)/10} months`:'Add at least one block.';};
  function add(v?:Partial<RotationBlock>){
    // Active choices, plus the block's own even if retired since, so saving something else keeps it.
    const kept=(kind:string,current:string|null|undefined)=>options(all(ctx,kind).filter(r=>r.active!==false||r.id===current));
    const type=select('Rotation type',kept('rotation_types',v?.rotation_type_id),v?.rotation_type_id||'',false);
    const service=select('Service (optional)',kept('services',v?.service_id),v?.service_id||'',false);
    const length=field('Length','number',v?.length??4,{required:true,min:1,max:52});
    const unit=select('Unit',[{value:'weeks',label:'Weeks'},{value:'months',label:'Months'}],v?.unit||'weeks');
    const where=select('Department duties',[{value:'true',label:'Internal · may take duties'},{value:'false',label:'External · away'}],String(v?.department_available??true));
    const node=el('li',{class:'nested-card block-row'},el('div',{class:'mini-grid'},type.wrap,service.wrap,length.wrap,unit.wrap,where.wrap));
    const row={node,read:():RotationBlock=>({rotation_type_id:type.input.value||null,service_id:service.input.value||null,length:Number(length.input.value)||1,
      unit:unit.input.value as 'weeks'|'months',department_available:where.input.value==='true'})};
    const up=iconButton('Move block earlier','chevronUp',()=>{const i=rows.indexOf(row);if(i>0){rows.splice(i,1);rows.splice(i-1,0,row);node.previousElementSibling?.before(node);}});
    node.append(el('div',{class:'actions'},up,button('Remove block',()=>{rows.splice(rows.indexOf(row),1);node.remove();total();},'ghost small')));
    rows.push(row);body.append(node);total();
  }
  (initial.length?initial:[{}]).forEach(add);
  body.addEventListener('input',total);body.addEventListener('change',total);
  return {wrap:part('Blocks','In order. Each block needs a rotation type or a service.',body,count,el('div',{class:'actions span-2'},button('Add block',()=>add(rows.at(-1)?.read()),'secondary'))),
    read:()=>rows.map(r=>r.read())};
}

/** `preset`: values a new record starts from (a recurring session made from an empty slot of the week grid, or from a
 *  breach's fix); its dates and Active also change an edited record (a recurring session extended or switched on).
 *  `then`: what saving leads to next (said on the form), run after the save. */
export function catalogueForm(ctx:Context,kind:string,row?:Row,preset?:Record<string,any>,then?:{note:string;run:()=>Promise<void>|void}){
  const notice=kind==='department_content',titleNames:Record<string,string>={[kind]:singularOf(kind)};  // one source: config_kinds
  return formDialog(notice?(row?'Edit announcement':TERMS.newAnnouncement):`${row?'Edit':'New'} ${titleNames[kind]||kind}`,
    '',form=>{
    // Announcements read in plain words: a title, a message, the first and last day shown.
    const name=field(kind==='people'?'Display name':notice?'Title':'Name','text',row?.name||row?.display_name||'',{required:true,maxlength:160});
    const description=area(notice?'Message':'Description',row?.description||'');
    const start=field(notice?'First day shown':'Effective from','date',preset?.starts_on||row?.starts_on||today(ctx),{required:true});
    const end=field(notice?'Last day shown (optional)':kind==='mentorships'?'Last day (included, optional)':'Effective until (included)','date',preset?.ends_on||row?.ends_on||'');lastDayIncluded(end.input);
    const dates=dateRange('Dates',start,end);
    const active=check(notice?'Show this announcement':'Active for future use',preset?.active??(row?.configured_active??row?.active)!==false,notice?'Turn off to hide it from everyone without deleting it.':'Inactive records stay in history but cannot be chosen for new work.');
    const reason=field('Change reason','text','',{maxlength:500,placeholder:'Optional note, e.g. new clinic from October'});
    // Records that belong to one person (mentorship, programme membership, rotation) are created for many people at
    // once: pick them all, and one record is saved for each. Editing a saved record keeps its one person.
    let who='';
    const personField=(value:string)=>{
      // Full width in both modes so the fields after it always pair up.
      if(row){const one=select('Person',peopleChoices(ctx),value);one.wrap.classList.add('span-2');return {wrap:one.wrap,ids:()=>[one.input.value],names:()=>[one.input.selectedOptions[0]?.textContent||''],get:()=>one.input.value};}
      const many=multi('People',peopleChoices(ctx),[],'Choose one or more people');many.wrap.classList.add('span-2');forEachPerson=many;return {wrap:many.wrap,ids:many.values,names:many.labels,get:()=>who};
    };
    let forEachPerson:{values:()=>string[];labels:()=>string[]}|null=null;
    let clashes:ReturnType<typeof liveClashCheck>|null=null;
    let value:()=>Record<string,unknown>=()=>({name:name.input.value,description:description.input.value,starts_on:start.input.value,ends_on:end.input.value||null,active:active.input.checked});
    if(kind==='people'){
      const title=field('Job title','text',row?.title||'',{maxlength:160}),section=field('Directory section','text',row?.directory_section||'',{maxlength:160});
      const phone=field('Hospital phone','tel',row?.work_phone||'',{maxlength:80}),extension=field('Office extension','text',row?.work_extension||'',{maxlength:80});
      const number=field('Employee or student number','text',row?.staff_number||'',{maxlength:80}),pager=field('Pager number','text',row?.pager||'',{maxlength:40}),order=field('Directory order','number',row?.directory_order??1000,{min:0,max:100000,required:true});
      const code=field('Short code','text',row?.short_code||'',{maxlength:6,pattern:'[A-Z0-9]{0,6}',autocomplete:'off'});
      const codeHint=el('small',{class:'hint'}),syncCode=()=>{code.input.value=code.input.value.toUpperCase().replace(/[^A-Z0-9]/g,'');const auto=autoCode(name.input.value);codeHint.textContent=code.input.value?'Shown on dense rota views instead of the automatic code.':auto?`Leave empty to use the automatic code ${auto}.`:'';codeHint.hidden=!codeHint.textContent;};
      code.wrap.append(codeHint);code.input.addEventListener('input',syncCode);name.input.addEventListener('input',syncCode);syncCode();
      // Tags: the one creatable picker the list's Tags cell uses (P-05): the department's tags for people and those already
      // on people, or a new name.
      const tagNames=[...new Set([...(all(ctx,'tags').filter(t=>t.active!==false&&(t.labels||[]).includes('people')).map(t=>String(t.name))),
        ...all(ctx,'people').flatMap(x=>(x.tags||[]) as string[])])].sort((a,b)=>a.localeCompare(b));
      // The server's limits (TAG_LIMITS), said under the field as tags are picked, so Save stops there.
      const tagCheck=(v:string[])=>tagPick.wrap.querySelector('select')!.setCustomValidity(v.length>TAG_LIMITS.per_person?`Up to ${TAG_LIMITS.per_person} tags.`
        :v.some(t=>t.length>TAG_LIMITS.length)?`Each tag up to ${TAG_LIMITS.length} characters.`:'');
      const tagPick=multiPick('Tags',tagNames.map(t=>({value:t,label:t})),(row?.tags||[]) as string[],v=>tagCheck(v),'None',true);
      const tags={wrap:el('div',{class:'pick-field'},el('span',{class:'pick-label'},'Tags'),tagPick.wrap),values:tagPick.values};
      const colour=colourSwatches(row?.colour||'',true,'Optional. Used when the planner colours by person. Automatic picks a stable colour.');
      // How rotas and imports name them (free text, any characters): searchable everywhere people are picked.
      const rotaName=field('Name on the rota','text',row?.rota_name||'',{maxlength:160,autocomplete:'off'});
      rotaName.wrap.append(el('small',{class:'hint'},'Optional. The name source rotas and imports use for this person.'));
      const aliases=area('Other names on rotas',((row?.rota_aliases||[]) as string[]).join('\n'),2);
      aliases.wrap.append(el('small',{class:'hint'},'One per line, up to 30. Found when searching for this person.'));
      const aliasList=()=>[...new Set(aliases.input.value.split('\n').map(a=>a.trim()).filter(Boolean))];
      aliases.input.addEventListener('input',()=>{const list=aliasList();
        aliases.input.setCustomValidity(list.length>30?'Up to 30 other names.':list.some(a=>a.length>160)?'Each name up to 160 characters.':'');});
      form.append(part('Who they are','',name.wrap,code.wrap,title.wrap,tags.wrap,rotaName.wrap,aliases.wrap,colour.wrap,active.wrap),
        part('Directory & contact','',section.wrap,order.wrap,phone.wrap,extension.wrap,pager.wrap,number.wrap));
      value=()=>({colour:colour.input.value,display_name:name.input.value,active:active.input.checked,title:title.input.value,directory_section:section.input.value,work_phone:phone.input.value,work_extension:extension.input.value,staff_number:number.input.value,pager:pager.input.value,directory_order:Number(order.input.value),short_code:code.input.value,tags:tags.values(),
        rota_name:rotaName.input.value.trim(),rota_aliases:aliasList()});
    }else if(kind==='programme_calendar_entries'){
      const programme=select('Programme',options(all(ctx,'programmes')),row?.programme_id||'');
      const entryKind=select('Entry type',[{value:'academic_block',label:'Academic block'},{value:'break',label:'Break'},{value:'notice',label:'Notice'}],row?.kind||'academic_block');
      name.wrap.classList.add('span-2');end.input.required=true;form.append(name.wrap,programme.wrap,entryKind.wrap,dates,description.wrap,note('These dates do not assign employees or create leave. To keep the programme off duty at these times, use “Protected time” on the entry.'));
      if(row){const protection=el('div',{class:'span-2'});form.append(protection);
        void get<{protections:Row[]}>(`/api/programme-calendar-entries/${row.id}/protections`).then(r=>{
          if(!form.isConnected||!r.protections.some(p=>['active','proposed'].includes(p.status)))return;
          for(const input of [programme.input,entryKind.input,start.input,end.input])input.disabled=true;
          protection.append(note('This calendar entry has approved or waiting protection. Retire, decline or undo that protection before changing its programme, type or dates. Names and descriptions can still be saved.','warning'),
            button('View protected time',()=>calendarProtectionPanel(ctx,row),'secondary small'));
        }).catch(e=>{if(form.isConnected)protection.append(note((e as Error).message,'error',e),button('View protected time',()=>calendarProtectionPanel(ctx,row),'secondary small'));});}
      value=()=>({name:name.input.value,programme_id:programme.input.value,kind:entryKind.input.value,starts_on:start.input.value,ends_on:end.input.value,description:description.input.value});
    }else if(kind==='mentorships'){
      const person=personField(row?.person_id||''),mentor=select('Mentor',peopleChoices(ctx),row?.mentor_id||''),programme=select('Programme (optional)',options(all(ctx,'programmes')),row?.programme_id||'',false);
      const types=multi('Duty types',options(all(ctx,'assignment_types')),row?.assignment_type_ids||[],'All duty types'),clinics=multi('Clinics',options(all(ctx,'services')),row?.service_ids||[],'All clinics');
      const strength=select('Pairing',[{value:'required',label:'Required · always pair them'},{value:'preferred',label:'Preferred · pair them when possible'}],row?.strength||'preferred');
      form.append(person.wrap,mentor.wrap,strength.wrap,programme.wrap,types.wrap,clinics.wrap,dates);
      value=()=>({person_id:person.get(),mentor_id:mentor.input.value,programme_id:programme.input.value||null,starts_on:start.input.value,ends_on:end.input.value||null,
        assignment_type_ids:types.values(),service_ids:clinics.values(),strength:strength.input.value});
    }else if(kind==='programme_memberships'){
      const person=personField(row?.person_id||''),programme=select('Programme',options(all(ctx,'programmes')),row?.programme_id||'');
      // An external rotator: a trainee of another programme placed here, managed by this department's programme lead.
      const external=check('External rotator',row?.external===true,'A trainee from another programme, placed here for these dates.');
      const home=field('Home programme','text',row?.home_programme||'',{maxlength:160,placeholder:'e.g. Internal medicine, another hospital'});
      const syncExternal=()=>{home.wrap.hidden=!external.input.checked;};external.input.addEventListener('change',syncExternal);syncExternal();
      programme.wrap.classList.add('span-2');end.input.required=true;form.append(person.wrap,programme.wrap,dates,external.wrap,home.wrap);
      value=()=>({person_id:person.get(),programme_id:programme.input.value,starts_on:start.input.value,ends_on:end.input.value,
        external:external.input.checked,home_programme:external.input.checked?home.input.value:''});
    }else if(kind==='rotations'){
      const person=personField(row?.person_id||''),cohort=select('Cohort',options(all(ctx,'cohorts')),row?.cohort_id||''),service=select('Primary service (optional)',options(all(ctx,'services')),row?.service_id||'',false);
      const rotationType=select('Rotation type',options(all(ctx,'rotation_types')),row?.rotation_type_id||'',false);
      const available=select('Department availability',[{value:'true',label:'Internal rotation · may assign eligible duties'},{value:'false',label:'External rotation · unavailable for departmental duties'}],String(row?.department_available??true));
      available.wrap.classList.add('span-2');end.input.required=true;form.append(person.wrap,cohort.wrap,rotationType.wrap,service.wrap,available.wrap,dates);
      value=()=>({rotation_type_id:rotationType.input.value||null,person_id:person.get(),cohort_id:cohort.input.value,service_id:service.input.value||null,mentor_id:row?.mentor_id||null,department_available:available.input.value==='true',starts_on:start.input.value,ends_on:end.input.value});
    }else if(kind==='rotation_templates'){
      // A programme lead's templates belong to a programme they lead (shared ones are the coordinators').
      const lead=!ctx.permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR),led=all(ctx,'programmes');
      const programme=lead?select('Programme',options(led),row?.programme_id||''):select('Programme (optional)',options(led),row?.programme_id||'',false);
      const blocks=rotationBlocks(ctx,row?.blocks||[]);
      name.wrap.classList.add('span-2');form.append(name.wrap,programme.wrap,active.wrap,description.wrap,blocks.wrap);
      value=()=>({name:name.input.value,programme_id:programme.input.value||null,description:description.input.value,active:active.input.checked,blocks:blocks.read()});
    }else if(kind==='cohorts'){
      const programme=select('Programme',options(all(ctx,'programmes')),row?.programme_id||'');end.input.required=true;
      form.append(name.wrap,programme.wrap,dates);value=()=>({name:name.input.value,programme_id:programme.input.value,starts_on:start.input.value,ends_on:end.input.value});
    }else if(kind==='leave_types'){
      // A type applies from its next use: leave already recorded or asked for keeps its type and whether it blocks.
      const code=field('Short code','text',row?.short_code||'',{required:true,maxlength:6,pattern:'[A-Z0-9]{1,6}',autocomplete:'off'});
      code.input.addEventListener('input',()=>{code.input.value=code.input.value.toUpperCase().replace(/[^A-Z0-9]/g,'');});
      const order=field('Order in lists','number',row?.sort_order??Math.max(-1,...all(ctx,'leave_types').map(t=>Number(t.sort_order)||0))+1,{required:true,min:0,max:100000,step:1});
      const colour=colourSwatches(row?.colour||'',true,'Shown beside this type wherever leave appears.');
      const blocks=check('Blocks duties',row?.counts_as_unavailable!==false,'On: people on this leave cannot be scheduled. Off: shown on the rota only, for example study time they can be called from.');
      const offered=check('Offered for new leave',row?.active!==false,'Off archives it: old leave keeps its type, new leave cannot choose it.');
      name.wrap.classList.add('span-2');form.append(name.wrap,code.wrap,order.wrap,colour.wrap,el('div',{class:'switch-group'},blocks.wrap,offered.wrap));
      value=()=>({name:name.input.value,short_code:code.input.value,sort_order:Number(order.input.value),colour:colour.input.value,counts_as_unavailable:blocks.input.checked,active:offered.input.checked});
    }else if(kind==='contact_types'){
      // D68: what each contact is, who sees it, who edits it and how it dials; the card reads only these rows.
      const mode=select('Who can see it',Object.entries(CONTACT_VISIBILITY).map(([value,label])=>({value,label})),row?.visibility?.mode||'self_and_admins');
      const roles=multi('Roles that see it',ROLE_NAMES.map(r=>({value:r,label:roleLabel(r)})),row?.visibility?.mode==='roles'?row.visibility.roles||[]:[]);
      const type=select('Kind',Object.entries(CONTACT_KINDS).map(([value,label])=>({value,label})),row?.kind||'phone');
      const order=field('Order on the card','number',row?.sort_order??Math.max(-1,...all(ctx,'contact_types').map(t=>Number(t.sort_order)||0))+10,{required:true,min:0,max:100000,step:1});
      const oneTap=check('One-tap call',!!row?.one_tap,'Call and Message dial it in one tap. Pagers and extensions also need the switchboard line.');
      const line=field('Switchboard line','tel',row?.dial?.line||'',{maxlength:40,pattern:'\\+[1-9][0-9]{1,14}',placeholder:'+966…'});
      const join=select('Then',[{value:'wait',label:'Wait, then dial the code'},{value:'pause',label:'Pause, then dial the code'}],row?.dial?.join||'wait');
      const required=check('Required',!!row?.required,'Every person needs a value.');
      const pattern=field('Format (pattern)','text',row?.format?.pattern||'',{maxlength:500,placeholder:'Optional, e.g. ^[0-9]{4}$',autocomplete:'off'});
      const length=field('Longest value','number',row?.format?.max_length??254,{required:true,min:1,max:254,step:1});
      const editors=select('Who edits it',[{value:'admins',label:'Admins'},{value:'person_and_admins',label:'The person and admins'}],row?.edited_by||'admins');
      const narrow=check('The person may narrow who sees theirs',!!row?.person_may_change);
      const sync=()=>{roles.wrap.hidden=mode.input.value!=='roles';const dials=type.input.value==='pager'||type.input.value==='extension';line.wrap.hidden=join.wrap.hidden=!dials;};
      mode.input.addEventListener('change',sync);type.input.addEventListener('change',sync);sync();
      // A type the department always had (its seeded start, before any real date) keeps that start unless one is chosen.
      const always=!!row&&String(row.starts_on)<'1000';
      if(always){start.input.value='';start.input.required=false;start.input.placeholder='Always';}
      name.wrap.classList.add('span-2');roles.wrap.classList.add('span-2');
      form.append(name.wrap,type.wrap,order.wrap,mode.wrap,editors.wrap,roles.wrap,line.wrap,join.wrap,pattern.wrap,length.wrap,dates,el('div',{class:'switch-group span-2'},oneTap.wrap,required.wrap,narrow.wrap,active.wrap));
      value=()=>({name:name.input.value,kind:type.input.value,icon:row?.icon||'',sort_order:Number(order.input.value),required:required.input.checked,one_tap:oneTap.input.checked,
        starts_on:start.input.value||(always?row!.starts_on:''),ends_on:end.input.value||null,active:active.input.checked,edited_by:editors.input.value,person_may_change:narrow.input.checked,
        visibility:{mode:mode.input.value,roles:mode.input.value==='roles'?roles.values():[]},format:{pattern:pattern.input.value.trim()||null,max_length:Number(length.input.value)},
        dial:{line:line.input.value.trim(),join:join.input.value}});
    }else if(kind==='date_sets'){
      // Holidays & dates off: a name, whether it is in use, and its named entries.
      name.input.placeholder='e.g. Public holidays';
      const inUse=check('In use',row?.active!==false,'Off: rules and opening hours stop matching its dates. Its history stays.');
      const entries=dateSetEntries(row);
      name.wrap.classList.add('span-2');form.append(name.wrap,el('div',{class:'switch-group span-2'},inUse.wrap),entries.wrap);
      value=()=>({name:name.input.value,active:inUse.input.checked,entries:entries.read()});
    }else{
      // Name full width, then the date pair, then the on/off switch row.
      name.wrap.classList.add('span-2');
      // Announcements read top to bottom: what it says, where it links, when it shows, who it is for, then the switches.
      if(notice)form.append(name.wrap,description.wrap);
      else{form.append(name.wrap,dates,active.wrap);if(kind!=='locations')form.append(description.wrap);}
      if(kind==='department_content'){
        // An optional link shows as "Open link" under the message (a policy, a form, a rota document).
        const url=field('Link (optional)','url',row?.url||'',{maxlength:2000,placeholder:'https://… shown as “Open link” under the message'});url.wrap.classList.add('span-2');form.append(url.wrap);
        const audience=audienceFields(ctx,row?.audience);
        // Keyed by the contract's importance values, so a new value fails the build here instead of being dropped on save.
        const levels:Record<NonNullable<DepartmentContentInput['importance']>,string>={normal:'Normal',important:'Important',must_acknowledge:'Must acknowledge'};
        const importance=select('Importance',Object.entries(levels).filter(([value])=>row?.kind!=='link'||value!=='must_acknowledge').map(([value,label])=>({value,label})),row?.importance||'normal');
        const hint=el('small',{class:'hint',id:importance.input.id+'-hint'},row?.kind==='link'?'Important shows first on Home.':'Important shows first on Home. Must acknowledge also asks each person to press “I acknowledge”.');
        importance.wrap.append(hint);importance.input.setAttribute('aria-describedby',hint.id);
        // New notices go to the inbox by default; an edit only sends again when asked.
        const notify=check(row?'Send it to their inbox again':'Send it to their inbox',!row,row?'Only when the change is worth telling everyone again.':'On its first day, if that is later than today.');
        // The two switches share one row (one per row on phones).
        form.append(dates,audience.root,importance.wrap,el('div',{class:'switch-group'},notify.wrap,active.wrap));
        value=()=>({name:name.input.value,description:description.input.value,active:active.input.checked,starts_on:start.input.value,ends_on:end.input.value||null,kind:row?.kind||'announcement',url:url.input.value,
          audience:audience.read(),importance:importance.input.value,notify:notify.input.checked});
      }else if(kind==='services'){
        const mode=select('Consultation mode',[{value:'unspecified',label:'Not specified'},{value:'physical',label:'In person'},{value:'virtual',label:'Virtual'}],row?.mode||'unspecified');
        const calendar=openingHours(ctx,row);
        const colour=colourSwatches(row?.colour||'',true,'Colours this service’s group on the schedule and calendars. Automatic picks a stable colour for you.');
        form.append(mode.wrap,calendar.wrap,colour.wrap);
        value=()=>({name:name.input.value,description:description.input.value,active:active.input.checked,starts_on:start.input.value,ends_on:end.input.value||null,mode:mode.input.value,calendar_id:calendar.input.value||null,colour:colour.input.value});
      }else if(kind==='event_types'){
        // An event type's defaults (EventTypeInput): new events of it start from these. also_email keeps its saved value.
        const colour=colourSwatches(row?.colour||'',true,'Shown beside events of this type. Automatic picks a stable colour.');
        const time=field('Usual start','time',row?.default_start_time||'09:00',{required:true});
        const length=field('Usual length · minutes','number',row?.default_length_minutes??60,{required:true,min:1,max:10080,step:1});
        const audience=audienceFields(ctx,row?.default_audience);
        const attendance=select('Attendance',[{value:'optional',label:'Optional'},{value:'expected',label:'Expected'},{value:'must_acknowledge',label:'Must acknowledge'}],row?.attendance||'expected');
        const calendar=select('On calendars',[{value:'everyone',label:'Everyone it is for'},{value:'going_only',label:'Only those going'},{value:'none',label:'Not shown'}],row?.calendar||'everyone');
        const tags=multi('Tags',options(all(ctx,'tags').filter(t=>t.active!==false||(row?.default_tag_ids||[]).includes(t.id))),row?.default_tag_ids||[],'None');
        const blocks=check('Takes people off duties',!!row?.blocks_duties),rsvp=check('Asks who is going',!!row?.rsvp),inbox=check('Sends to their inbox',row?.notify_inbox!==false);
        // The server's own rule (optional_duty_protection), said before Save.
        const cross=()=>blocks.input.setCustomValidity(blocks.input.checked&&attendance.input.value==='optional'&&!rsvp.input.checked?'An optional event can take people off duties only when it asks who is going.':'');
        for(const x of [blocks.input,rsvp.input,attendance.input])x.addEventListener('change',cross);cross();
        form.append(colour.wrap,time.wrap,length.wrap,attendance.wrap,calendar.wrap,tags.wrap,audience.root,el('div',{class:'switch-group span-2'},blocks.wrap,rsvp.wrap,inbox.wrap));
        value=()=>({name:name.input.value,description:description.input.value,active:active.input.checked,starts_on:start.input.value,ends_on:end.input.value||null,colour:colour.input.value,
          default_start_time:time.input.value,default_length_minutes:Number(length.input.value),default_audience:audience.read(),attendance:attendance.input.value,calendar:calendar.input.value,
          default_tag_ids:tags.values(),blocks_duties:blocks.input.checked,rsvp:rsvp.input.checked,notify_inbox:inbox.input.checked});
      }else if(kind==='categories'){
        // A rank is never offered a problem hue; an existing choice remains selectable until changed.
        const colour=colourSwatches(row?.colour||'',true,'This rank’s one colour everywhere it is shown. Automatic picks a free colour.',PROBLEM_HUES);
        form.append(colour.wrap);
        value=()=>({name:name.input.value,description:description.input.value,active:active.input.checked,starts_on:start.input.value,ends_on:end.input.value||null,colour:colour.input.value});
      }else if(kind==='locations'){
        const type=select('Location type',options(all(ctx,'location_types')),row?.location_type_id||'');
        const parent=select('Parent location or shared resource',options(all(ctx,'locations').filter(r=>r.id!==row?.id)),row?.parent_id||'',false);
        const capacity=field('Concurrent session capacity','number',row?.capacity??1,{min:1,max:1000,required:true});
        const calendar=openingHours(ctx,row);
        form.append(type.wrap,parent.wrap,capacity.wrap,calendar.wrap,note('Capacity counts simultaneous sessions consuming this resource, including sessions in child locations. It does not count people.'));
        value=()=>({name:name.input.value,active:active.input.checked,starts_on:start.input.value,ends_on:end.input.value||null,location_type_id:type.input.value,parent_id:parent.input.value||null,calendar_id:calendar.input.value||null,capacity:Number(capacity.input.value)});
      }
      if(kind==='operating_calendars'){
        const calendar=operatingCalendarFields(ctx,row);form.append(calendar.wrap);
        const base=value;value=()=>({...base(),definition:calendar.read()});
      }
      if(kind==='resource_pools'){
        const capacity=field('Pool capacity · units','number',row?.capacity??1,{min:1,max:1000,required:true});
        const location=select('Limit pool to location subtree',options(all(ctx,'locations')),row?.location_id||'',false);
        const calendar=openingHours(ctx,row);
        location.wrap.classList.add('span-2');form.append(capacity.wrap,calendar.wrap,location.wrap,note('A pool represents interchangeable equipment or capacity. It does not track individual assets, maintenance or consumable stock.'));
        const base=value;value=()=>({...base(),capacity:Number(capacity.input.value),location_id:location.input.value||null,calendar_id:calendar.input.value||null});
      }
      if(kind==='assignment_types'){
        const d:TypeDefinition=row?.definition||{};
        const shortCode=field('Short code','text',row?.short_code||'',{maxlength:6,pattern:'[A-Z0-9]{0,6}',autocomplete:'off'});
        shortCode.input.addEventListener('input',()=>{shortCode.input.value=shortCode.input.value.toUpperCase().replace(/[^A-Z0-9]/g,'');});
        const purpose=select('Purpose',[{value:'staffed',label:'Staffed timed duty'},{value:'group',label:'Group activity'},{value:'protected',label:'Protected time'}],d.purpose||'staffed');
        const colour=colourSwatches(d.colour||'',true,'Colours this duty on the schedule, calendars and rotas. Automatic picks a stable colour; changing it never needs the draft refreshed.');
        const maximumDuration=field('Maximum duration · minutes','number',d.maximum_duration_minutes||720,{required:true,min:15,max:2160});
        // These times are the duty's set hours: every session of it must book the whole span (not just a suggestion).
        const defaultStart=field('Session starts','text',d.default_timing?.start_time||'',{pattern:'([01][0-9]|2[0-3]):[0-5][0-9]',placeholder:'HH:mm'}),defaultEnd=field('Session ends','text',d.default_timing?.end_time||'',{pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00',placeholder:'HH:mm, including 24:00'});
        defaultEnd.wrap.append(el('small',{class:'hint'},'Set hours: every session of this duty books this whole span. Changing them is checked against existing sessions before saving.'));
        const allowedCategories=multi('Permitted ranks · empty means any eligible rank',rankOptions(ctx),d.allowed_category_ids||[]);
        const allowedLocations=multi('Permitted location types · empty means any',options(all(ctx,'location_types')),d.allowed_location_type_ids||[]);
        const shared=check('Support several services as one reviewed common duty',d.allow_shared_services||false);
        const sharedReason=area('Common-duty explanation',d.shared_duty_reason||'',2);
        // Extra cover: auto-schedule fills these sessions with overtime only; the second choice says what an unfilled place is.
        const extraCover=check('Extra cover: overtime only',d.extra_cover||false);
        const unfilled=select('If nobody volunteers',[{value:'open',label:'Leave it open'},{value:'gap',label:'Treat as a cover gap'}],d.extra_cover_unfilled||'open');
        unfilled.wrap.querySelector('label')?.append(el('button',{type:'button',class:'link-button small','aria-label':'About If nobody volunteers','data-help':'','data-tip':'Leave it open: the place stays open and never blocks submitting or publishing. Treat as a cover gap: it blocks them like any other gap.'},icon('help')));
        unfilled.wrap.append(el('small',{class:'hint'},'Minimum-cover rules still apply either way.'));
        const extraCoverPart=part('Extra cover','Auto-schedule fills these sessions only with people allowed overtime on this duty, and marks them overtime. It gives nobody permission to work overtime; overtime offers and caps still decide who may.',extraCover.wrap,unfilled.wrap);
        const sessionUnits=field('Session units per duty','number',d.session_units??1,{step:'any',min:0,max:100,required:true});
        const workload=field('Workload credits per scheduled hour','number',d.workload_credit_per_hour??1,{step:'any',min:0,max:10,required:true});
        const training=field('Educational credits per scheduled hour','number',d.training_credit_per_hour??0,{step:'any',min:0,max:10,required:true});
        const sessionLength=()=>{const a=defaultStart.input.value,b=defaultEnd.input.value;if(a&&b){const [ah,am]=a.split(':').map(Number),[bh,bm]=b.split(':').map(Number);let m=bh*60+bm-(ah*60+am);if(m<=0)m+=1440;return m;}return Number(maximumDuration.input.value)||480;};
        const requirements=requirementsBuilder(ctx,d.requirements||[],sessionLength);
        const resources=resourceRequirementsBuilder(ctx,d.resources||[]);
        const supervise=check('Require continuous supervision for selected trainee groups',Boolean(d.supervision));
        const traineeCategories=multi('Trainee ranks · labels never imply seniority',rankOptions(ctx),d.supervision?.trainee_category_ids||[]);
        const supervisorCap=select('Verified supervisor capability',options(all(ctx,'capabilities')),d.supervision?.capability_id||'');
        const ratio=field('Maximum trainees per qualified supervisor','number',d.supervision?.trainees_per_supervisor||1,{min:1,max:20});
        const pairing=check('Require a named supervisor for each trainee',d.supervision?.require_pairing||false);
        const needsLead=check('This duty needs a team lead',Boolean(d.lead));
        const leadCount=select('How many team leads',[{value:'one',label:'Exactly one'},{value:'range',label:'A range'}],d.lead&&(d.lead.minimum!==1||d.lead.maximum!==1)?'range':'one');
        const leadMin=field('Fewest team leads','number',d.lead?.minimum??1,{min:0,max:20});
        const leadMax=field('Most team leads · empty means no limit','number',d.lead?d.lead.maximum??'':1,{min:1,max:20});
        // Who may lead: the shared condition builder over person fields (older skill / rank lists open as lines).
        const leadWho=conditionBuilder(leadConditionFields(ctx),scopeAsConditions({capability_ids:d.lead?.capability_ids,category_ids:d.lead?.category_ids,conditions:d.lead?.conditions}));
        const leadWhoWrap=el('div',{class:'span-2 audience-conditions'},el('p',{class:'hint'},'Who may lead · checked at the time of each session; no conditions means anyone on the session.'),leadWho.root);
        const leadPart=part('Team lead','Names who leads each session. Only people who meet these conditions can be chosen, by you or by auto-schedule; a session without its lead is flagged. How fairly lead turns are shared, and any limit per person, are rules (Fair share and Workload limit).',needsLead.wrap,leadCount.wrap,leadMin.wrap,leadMax.wrap,leadWhoWrap);
        const supervisionPart=part('Supervision','A trainee rank cannot bypass supervision by selecting a different participation label.',supervise.wrap,traineeCategories.wrap,supervisorCap.wrap,ratio.wrap,pairing.wrap);
        const custom=customFieldsBuilder(d.fields||[]);
        const preview=el('div',{class:'duty-preview','aria-live':'polite'});
        const drawPreview=()=>{const c=colour.input.value||'teal',t=defaultStart.input.value&&defaultEnd.input.value?`${defaultStart.input.value}–${defaultEnd.input.value}`:'Times set per session';
          preview.replaceChildren(el('span',{class:'hint'},'On the schedule'),el('span',{class:`duty-preview-row duty-${c}`},el('span',{class:'wk-swatch','aria-hidden':'true'}),el('span',{class:'duty-preview-name'},name.input.value||'New duty type'),el('small',{},t)),el('span',{class:`pchip duty-${c}`},el('span',{class:'pchip-name'},'A. Person'),el('span',{class:'wk-tag sup'},'SUP')));};
        for(const n of [name.input,colour.input,defaultStart.input,defaultEnd.input]){n.addEventListener('input',drawPreview);n.addEventListener('change',drawPreview);}drawPreview();
        for(const w of [allowedCategories.wrap,allowedLocations.wrap,traineeCategories.wrap,training.wrap])w.classList.add('span-2');
        form.append(part('Participation and time','All supported purposes reserve the participant’s time. There is no unchecked overlap exemption.',preview,purpose.wrap,shortCode.wrap,maximumDuration.wrap,defaultStart.wrap,defaultEnd.wrap,colour.wrap,allowedCategories.wrap,allowedLocations.wrap,shared.wrap,sharedReason.wrap),requirements.wrap,extraCoverPart,resources.wrap,supervisionPart,leadPart,part('Counting','Session units count once for the complete assignment. Configure a separate duty type and unit value for shorter duties.',sessionUnits.wrap,workload.wrap,training.wrap),custom.wrap);
        function sync(){const protectedTime=purpose.input.value==='protected';requirements.wrap.hidden=protectedTime;supervisionPart.hidden=protectedTime;for(const root of [requirements.wrap,supervisionPart])root.querySelectorAll<HTMLInputElement|HTMLSelectElement>('input,select').forEach(control=>control.disabled=protectedTime);sharedReason.input.disabled=!shared.input.checked;sharedReason.input.required=shared.input.checked;supervisorCap.input.disabled=protectedTime||!supervise.input.checked;supervisorCap.input.required=!protectedTime&&supervise.input.checked;ratio.input.disabled=protectedTime||!supervise.input.checked;
          traineeCategories.wrap.hidden=pairing.wrap.hidden=protectedTime||!supervise.input.checked;
          leadPart.hidden=protectedTime;extraCoverPart.hidden=protectedTime;unfilled.wrap.hidden=!extraCover.input.checked;unfilled.input.disabled=protectedTime||!extraCover.input.checked;const lead=!protectedTime&&needsLead.input.checked,range=leadCount.input.value==='range';
          leadCount.input.disabled=!lead;leadMin.wrap.hidden=leadMax.wrap.hidden=!range;leadMin.input.disabled=leadMax.input.disabled=!lead||!range;
          leadWhoWrap.hidden=!lead;}
        purpose.input.addEventListener('change',sync);shared.input.addEventListener('change',sync);supervise.input.addEventListener('change',sync);needsLead.input.addEventListener('change',sync);extraCover.input.addEventListener('change',sync);leadCount.input.addEventListener('change',sync);sync();
        const readLead=()=>!needsLead.input.checked||purpose.input.value==='protected'?null:{...(leadCount.input.value==='one'?{minimum:1,maximum:1}
          :{minimum:Number(leadMin.input.value||0),maximum:leadMax.input.value===''?null:Number(leadMax.input.value)}),capability_ids:[],category_ids:[],conditions:leadWho.read(),
          // A former per-type lead cap now lives in the Workload limit rule; it is kept as stored, never edited here.
          most_turns:d.lead?.most_turns??null,most_turns_per:d.lead?.most_turns_per||'month'};
        value=()=>({name:name.input.value,short_code:shortCode.input.value,description:description.input.value,starts_on:start.input.value,ends_on:end.input.value||null,active:active.input.checked,definition:{schema_version:1,purpose:purpose.input.value,extra_cover:purpose.input.value!=='protected'&&extraCover.input.checked,extra_cover_unfilled:unfilled.input.value as 'open'|'gap',colour:colour.input.value,maximum_duration_minutes:Number(maximumDuration.input.value),default_timing:defaultStart.input.value||defaultEnd.input.value?{start_time:defaultStart.input.value,end_time:defaultEnd.input.value}:null,allowed_category_ids:allowedCategories.values(),allowed_location_type_ids:allowedLocations.values(),allow_shared_services:shared.input.checked,shared_duty_reason:shared.input.checked?sharedReason.input.value:'',requirements:purpose.input.value==='protected'?[]:requirements.read(),supervision:supervise.input.checked&&purpose.input.value!=='protected'?{trainee_category_ids:traineeCategories.values(),capability_id:supervisorCap.input.value,trainees_per_supervisor:Number(ratio.input.value),require_pairing:pairing.input.checked}:null,lead:readLead(),session_units:Number(sessionUnits.input.value),session_unit_basis:'fixed',workload_credit_per_hour:Number(workload.input.value),training_credit_per_hour:Number(training.input.value),resources:resources.read(),fields:custom.read()}});
      }
      if(kind==='recurring_series'){
        const d=row?.definition||preset?.definition||{},types=all(ctx,'assignment_types');
        const typ=select('Duty type',options(types),row?.assignment_type_id||preset?.assignment_type_id||''),location=select('Location',options(all(ctx,'locations')),row?.location_id||preset?.location_id||'');
        const assignees=multi('Usual assignees',peopleChoices(ctx),d.default_person_ids||[],'Assign in the draft');
        // A new series keeps its usual assignees by default (the contract's default); a stored series keeps its own value.
        // Both need usual assignees to mean anything: without any, Keep is off and cannot be chosen.
        const fixedDefault='usual_assignees_fixed' in d||row?!!d.usual_assignees_fixed:USUAL_ASSIGNEES_FIXED_DEFAULT;
        const fixed=check('Keep the usual assignees on every session',fixedDefault,'Auto-schedule never moves them and adds only trainees.');
        let fixedChosen:boolean|null=null;fixed.input.addEventListener('change',e=>{if(e.isTrusted)fixedChosen=fixed.input.checked;});
        const followPeople=()=>{const any=assignees.values().length>0;fixed.input.disabled=!any;fixed.input.checked=any&&(fixedChosen??fixedDefault);};
        assignees.wrap.addEventListener('change',followPeople);followPeople();
        // DL-009 (D38: sessions stay open on absence): the retired close switch is never offered; a stored series that has
        // it on says so, read-only, and keeps its stored value (read and ignored by the server).
        const closes=d.close_when_all_usual_unavailable?note(TERMS.retiredCloseSetting,'info'):null;
        const needsPeople=()=>fixed.input.setCustomValidity(fixed.input.checked&&!assignees.values().length?'Choose the usual assignees first.':'');
        fixed.input.addEventListener('change',needsPeople);assignees.wrap.addEventListener('change',needsPeople);needsPeople();
        const services=multi('Clinics and services',options(all(ctx,'services')),d.service_ids||[]);
        const anchor=field('First week of the pattern','date',d.anchor||start.input.value,{required:true});
        const interval=field('Repeat every (weeks)','number',d.interval_weeks||1,{min:1,max:12,required:true});
        const occurrence=select('Monthly weekday',[{value:'',label:'Every matching week'},{value:'1',label:'First'},{value:'2',label:'Second'},{value:'3',label:'Third'},{value:'4',label:'Fourth'},{value:'5',label:'Fifth'},{value:'-1',label:'Last'}],d.month_occurrence==null?'':String(d.month_occurrence),false);
        for(const w of [services.wrap,assignees.wrap])w.classList.add('span-2');
        // The one shared date list (date_picker.ts): calendar, chips, copy and paste.
        const excluded=dateList('Excluded dates',d.excluded_dates||[],{hint:'No session on these dates, for example public holidays.'});
        // Times come from the duty type until someone types their own (an edit keeps its saved times).
        const timing=()=>lookup(ctx,'assignment_types',typ.input.value)?.definition?.default_timing as {start_time:string;end_time:string}|undefined;
        let ownTimes=!!row;
        const dayRows=weekOrder().map(i=>{const label=WEEKDAYS[i],prior=(d.days||[]).find((v:any)=>v.weekday===i),t=timing();const chosen=check(label,Boolean(prior));
          // Text, not the time field: an end of 24:00 (midnight at the end of the day) is valid here.
          const begin=field(`${label} start`,'text',prior?.start_time||t?.start_time||'08:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]',inputmode:'numeric',maxlength:5}),finish=field(`${label} end`,'text',prior?.end_time||t?.end_time||'16:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00',inputmode:'numeric',maxlength:5});
          for(const f of [begin,finish])f.input.addEventListener('input',e=>{if(e.isTrusted)ownTimes=true;});
          const sync=()=>{begin.input.disabled=finish.input.disabled=!chosen.input.checked;};chosen.input.addEventListener('change',()=>{sync();syncDays();});sync();
          return {chosen,begin,finish,i};});
        // At least one day, said on the first day's box (the browser shows it there on Save).
        function syncDays(){dayRows[0]?.chosen.input.setCustomValidity(dayRows.some(r=>r.chosen.input.checked)?'':'Tick at least one day.');}
        syncDays();
        const needed=el('p',{class:'series-staff span-2','aria-live':'polite'});
        const drawNeeded=()=>{const type=lookup(ctx,'assignment_types',typ.input.value);
          needed.replaceChildren(...(type?[el('span',{class:'muted'},'Staff needed per session '),el('strong',{},staffNeeded(type)),el('span',{class:'muted'},` · from ${labelOf(type)}`),
            button('Edit duty type',()=>catalogueForm(ctx,'assignment_types',type),'link-button')]:[el('span',{class:'muted'},'Staff needed comes from the duty type.')]));};
        // A new pattern is named after its clinic and duty type ("Poly Urgent Clinic · Morning") until a name is typed.
        const offer=suggested(name.input,!!row?.name);
        const suggest=()=>{const clinic=services.labels()[0]||(location.input.value?labelOf(lookup(ctx,'locations',location.input.value)||{}):''),type=typ.input.value?labelOf(lookup(ctx,'assignment_types',typ.input.value)||{}):'';
          const next=[clinic.replace(/ Room \d+$/,''),type].filter(Boolean).join(' · ');if(offer(next))name.input.dispatchEvent(new Event('input',{bubbles:true}));};
        const attrBox=el('div',{class:'span-2'});let attrReader=()=>({} as Record<string,unknown>);
        function attrs(){const type=lookup(ctx,'assignment_types',typ.input.value);const fields=attributesForm(type?.definition?.fields||[],d.attributes||{});attrBox.replaceChildren(fields.wrap);attrReader=fields.read;}
        typ.input.addEventListener('change',()=>{attrs();drawNeeded();suggest();const t=timing();
          if(t&&!ownTimes)for(const r of dayRows){r.begin.input.value=t.start_time;r.finish.input.value=t.end_time;r.begin.input.dispatchEvent(new Event('input'));}});
        location.input.addEventListener('change',suggest);services.wrap.addEventListener('change',suggest);
        attrs();drawNeeded();if(!row)suggest();
        // The dates: the end is optional (no end date), and a dated version covers up to one year (the server's limit),
        // checked beside the field as it is typed, not after Save.
        const yearOn=(s:string)=>{const x=new Date(s+'T12:00:00Z');x.setUTCFullYear(x.getUTCFullYear()+1);return x.toISOString().slice(0,10);};
        let endAuto=!row&&!preset?.ends_on;if(endAuto&&start.input.value)end.input.value=yearOn(start.input.value);
        end.input.addEventListener('input',e=>{if(e.isTrusted)endAuto=false;});
        end.input.placeholder='No end date';
        const endCheck=inlineCheck(end,()=>{const a=start.input.value,b=end.input.value;if(!a||!b)return '';
          const days=Math.round((Date.parse(b)-Date.parse(a))/864e5),last=new Date(Date.parse(a)+365*864e5).toISOString().slice(0,10);
          return days<1?'Choose a last day on or after the first day.':days>366?`More than one year: choose ${showDay(last)} or earlier, or leave it empty for no end date.`:'';});
        let anchored=!row&&!d.anchor;anchor.input.addEventListener('input',e=>{if(e.isTrusted)anchored=false;});
        start.input.addEventListener('change',()=>{if(endAuto&&start.input.value)end.input.value=yearOn(start.input.value);endCheck();if(anchored)anchor.input.value=start.input.value;});
        const hours=part('Weekly timing','Tick the days it runs. Times are the department’s local time and start from the duty type; an end earlier than the start continues into the next day, and 24:00 is midnight.',
          needed,
          // One row per weekday: tick, start and end on one line (the time labels stay for screen readers).
          el('div',{class:'week-hours span-2'},...dayRows.map(r=>el('div',{class:'day-row'},r.chosen.wrap,el('div',{},r.begin.wrap,el('span',{class:'hint','aria-hidden':'true'},'to'),r.finish.wrap)))));
        dates.append(el('small',{class:'hint span-2'},'Up to one year per version; leave the last day empty for no end date.'));
        fixed.wrap.classList.add('span-2');closes?.classList.add('span-2');
        form.append(typ.wrap,location.wrap,services.wrap,name.wrap,dates,hours,assignees.wrap,fixed.wrap,...(closes?[closes]:[]),excluded.wrap,anchor.wrap,interval.wrap,occurrence.wrap,attrBox,active.wrap,description.wrap);
        // Double bookings, checked live by the server's one overlap evaluator (no rule engine here).
        clashes=liveClashCheck(form,()=>({value:value(),series_id:row?.id??null,expected_revision:row?.revision??null}),day=>excluded.set([...excluded.values(),day]));
        form.append(clashes.box);
        value=()=>({name:name.input.value,description:description.input.value,active:active.input.checked,starts_on:start.input.value,ends_on:end.input.value||null,assignment_type_id:typ.input.value,location_id:location.input.value,definition:{schema_version:1,timezone:ctx.me.organisation.timezone,anchor:anchor.input.value,interval_weeks:Number(interval.input.value),month_occurrence:occurrence.input.value?Number(occurrence.input.value):null,excluded_dates:excluded.values(),service_ids:services.values(),default_person_ids:assignees.values(),
          // Always sent: what the form shows is what is saved (the server's default for a new series is Keep).
          usual_assignees_fixed:fixed.input.checked,
          ...('close_when_all_usual_unavailable' in d?{close_when_all_usual_unavailable:!!d.close_when_all_usual_unavailable}:{}),
          attributes:attrReader(),days:dayRows.filter(r=>r.chosen.input.checked).map(r=>({weekday:r.i,start_time:r.begin.input.value,end_time:r.finish.input.value}))}});
      }
    }
    reason.wrap.classList.add('span-2');form.append(reason.wrap);
    if(then?.note){const next=note(then.note,'info');next.classList.add('span-2');form.append(next);}
    // Announcements change no rota, so there is no impact to preview: save and say who is told.
    if(kind==='department_content')return async()=>{
      const result=await command(row?'catalogue.update':'catalogue.create',{catalogue:kind,value:value(),reason:routineReason(reason.input.value).replace(/^Routine update$/,'Announcement')},row?.id,row?.revision);
      notify(String(result.message||'Announcement saved.'));await ctx.refresh();};
    const one=():Command=>({kind:row?'catalogue.update':'catalogue.create',payload:{catalogue:kind,value:value(),reason:routineReason(reason.input.value).replace(/^Routine update$/,'Configuration update')},target_id:row?.id,expected_revision:row?.revision});
    const people=forEachPerson as {values:()=>string[];labels:()=>string[]}|null;
    const save=people?configurationImpact(form,()=>people.values().map(id=>{who=id;return one();}),people.labels)
      :configurationImpact(form,one,undefined,c=>clashes?{...c,payload:{...c.payload,...clashes.ack()}}:c);
    return async()=>{
      if(people&&!people.values().length)throw new Error('Choose at least one person.');
      await clashes?.ready();
      const n=await save().catch(e=>{throw clashes?.stale(e)?new Error('The double bookings changed meanwhile. Review them below and tick again.'):e;});
      const saved={...row,...value()} as Row;
      if(kind==='people'&&row)notify(`${isolate(fullNameOf(saved))} saved and is live.`);else notify(n>1?`${n} records saved, one for each person. Published rotas stay as they were.`:`${saved.name||saved.display_name?isolate(labelOf(saved))+' '+(row?'saved':'created'):row?'Saved':'Created'}. Published rotas stay as they were.`);
      await ctx.refresh();
      // The entry's protected-time rule keeps the old dates until it is updated too: offer that now.
      const linked=kind==='programme_calendar_entries'&&row?protectedTimeRule(ctx,row.id):undefined;
      if(linked&&(saved.starts_on!==linked.starts_on||saved.ends_on!==linked.ends_on||saved.name!==row!.name))setTimeout(()=>protectTimeForm(ctx,saved,linked));
      if(then)setTimeout(()=>void Promise.resolve(then.run()).catch(e=>notify((e as Error).message,'error',undefined,e)));};
  },row?'Save version':kind==='department_content'?'Post':kind==='operating_calendars'?'Set opening hours':`Add ${singularOf(kind)}`);
}

export function attributesForm(definitions:CustomField[],values:Record<string,unknown>={}){
  const wrap=el('div',{class:'form-grid'});const reads=new Map<string,()=>unknown>();const controls=new Map<string,HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement>();
  for(const d of definitions){
    let input:Field<HTMLInputElement|HTMLTextAreaElement|HTMLSelectElement>;
    if(d.kind==='boolean'){
      const f=check(d.label,values[d.key]===true,d.help_text||'');input=f;reads.set(d.key,()=>f.input.checked);
    }else if(d.kind==='choice'){
      const f=select(d.label,(d.options||[]).map(v=>({value:v,label:v})),String(values[d.key]||''),Boolean(d.required));input=f;reads.set(d.key,()=>f.input.value||undefined);
    }else if(d.kind==='multi_choice'){
      const f=multi(d.label,(d.options||[]).map(v=>({value:v,label:v})),Array.isArray(values[d.key])?values[d.key] as string[]:[]);wrap.append(f.wrap);reads.set(d.key,f.values);continue;
    }else if(d.kind==='long_text'){
      const f=area(d.label,String(values[d.key]||''));input=f;reads.set(d.key,()=>f.input.value||undefined);
    }else{
      const f=field(d.label,d.kind==='number'?'number':d.kind==='date'?'date':'text',String(values[d.key]??''),{step:d.kind==='number'?'any':undefined,min:d.minimum??undefined,max:d.maximum??undefined,maxlength:d.kind==='text'?300:undefined});input=f;reads.set(d.key,()=>f.input.value===''?undefined:d.kind==='number'?Number(f.input.value):f.input.value);
    }
    input.input.required=Boolean(d.required);controls.set(d.key,input.input);if(d.help_text&&d.kind!=='boolean')input.wrap.append(el('small',{class:'muted'},d.help_text));wrap.append(input.wrap);
  }
  function sync(){for(const d of definitions){const input=controls.get(d.key);if(input)input.required=Boolean(d.required||(d.required_when&&reads.get(d.required_when.field)?.()===d.required_when.equals));}}
  wrap.addEventListener('input',sync);sync();
  const read=()=>Object.fromEntries([...reads].map(([key,read])=>[key,read()]).filter(([,value])=>value!==undefined));
  return {wrap,read};
}

/** New draft (or, with `base`, an amendment of a published rota). `preset`: a name and dates to start from, as Home's
 *  "Start" for one month gives them (`ends_on` is the day after the last). */
export async function workspaceForm(ctx:Context,base?:Row,preset?:{name:string;starts_on:string;ends_on:string}){
  const [list]=await Promise.all([base?{workspaces:[],coverage_gaps:[],has_more:false}:get<{workspaces:Row[];coverage_gaps:RotaCoverageGap[];has_more?:boolean}>('/api/workspaces?limit=100'),ctx.ready()]);
  const previous:Row[]=list.workspaces;
  formDialog(base?'Create amendment':'New rota',base?'A draft copy of the published rota. Changes reach staff only after review and publication.':'',form=>{
    const name=field('Rota name','text',base?`Amendment · ${base.name||'publication '+base.sequence}`:preset?.name||'',{required:true,maxlength:160});
    // The name follows the dates ("Rota 1–14 Oct 2026", from the server) until someone types one; a preset name stays.
    // Only until someone types (suggested): the dialog opens with the focus in this field (focusFirst), so focus alone
    // must not stop the name from following.
    const suggest=suggested(name.input,Boolean(base||preset?.name));
    // "1–31 Oct 2026" for [a, b) (end exclusive).
    const dates=(a:string,b:string)=>spanLabel(a+'T00:00:00Z',b+'T00:00:00Z','UTC');
    // People think in last days; the rota stores the day after (end exclusive).
    const shift=(d:string,n:number)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+n);return x.toISOString().slice(0,10);};
    // A new rota starts where the latest one ends (the next unplanned period, never in the past) and is as long as it.
    const latest=[...previous].sort((a,b)=>String(b.ends_on).localeCompare(String(a.ends_on)))[0];
    const days=latest?Math.round((Date.parse(latest.ends_on)-Date.parse(latest.starts_on))/864e5):7;
    // With nothing planned ahead, the next full week from the department's week start (S-32), not today mid-week.
    const weekStartJs=((ctx.me.organisation.week_start??6)+1)%7;
    const nextWeekStart=(d:string)=>{let x=d;while(new Date(x+'T12:00:00Z').getUTCDay()!==weekStartJs)x=shift(x,1);return x;};
    const first=base?.starts_on||preset?.starts_on||(latest&&latest.ends_on>today(ctx)?latest.ends_on:nextWeekStart(today(ctx)));
    // After whole calendar months (1 Oct – 31 Oct) the next rota is the next whole month(s), not the same number of days.
    const months=(a:string,b:string)=>a.slice(8)==='01'&&b.slice(8)==='01'?(+b.slice(0,4)-+a.slice(0,4))*12+(+b.slice(5,7)-+a.slice(5,7)):0;
    const addMonths=(d:string,n:number)=>{const x=new Date(d+'T12:00:00Z');x.setUTCMonth(x.getUTCMonth()+n);return x.toISOString().slice(0,10);};
    const whole=!preset&&latest&&first===latest.ends_on?months(latest.starts_on,latest.ends_on):0;
    const start=field('First day','date',first,{required:true});
    const end=field('Last day','date',shift(base?.ends_on||preset?.ends_on||(whole?addMonths(first,whole):shift(first,days)),-1),{required:true});
    if(base){start.input.disabled=true;end.input.disabled=true;}
    // Last day can be First day but not before it: the shared date range (dateRange) says so on Last day.
    const urgency=select('Review',[{value:'routine',label:'Routine review'},{value:'urgent',label:'Urgent (uses the urgent review route)'}],'routine');
    const urgentReason=area('Urgency reason','');urgentReason.input.maxLength=500;
    const updateUrgency=()=>{urgentReason.wrap.hidden=urgency.input.value!=='urgent';urgentReason.input.required=urgency.input.value==='urgent';};
    urgency.input.addEventListener('change',updateUrgency);updateUrgency();
    // A start that cannot succeed is offered disabled with its reason (no recurring sessions, no rota to copy), never
    // as a choice that fails on Create (G-01, G-42).
    const noSeries=!all(ctx,'recurring_series').some(r=>r.active),noRota=!previous.length;
    const setup=select('Start from',[{value:'empty',label:'Empty rota'},{value:'patterns',label:'Recurring sessions',...(noSeries?{disabled:true,hint:'None set up yet · Configuration'}:{hint:`${all(ctx,'recurring_series').filter(r=>r.active).length} in use`})},
      {value:'previous',label:'Copy a previous rota',...(noRota?{disabled:true,hint:'no rota to copy yet'}:{})}],'empty');
    // ME-05: newest first, each said with its state, so two rotas of one name and dates can be told apart.
    const STATE:Record<string,string>={draft:'draft',submitted:'in review',approved:'approved',published:'published'};
    const rotaChoice=(r:Row)=>({value:r.id,label:[labelOf(r),dates(r.starts_on,r.ends_on),STATE[r.status]||r.status].filter(Boolean).join(' · ')});
    const byNewest=[...previous].sort((a,b)=>String(b.starts_on).localeCompare(String(a.starts_on))||String(b.created_at||'').localeCompare(String(a.created_at||'')));
    const source=select('Previous rota',byNewest.map(rotaChoice));
    // The list is the newest 100 rotas: said when there are more, never cut silently (maintenance).
    if(list.has_more)source.wrap.append(el('small',{class:'muted'},`Showing the latest ${previous.length} rotas`));
    source.wrap.append(el('small',{class:'muted'},'Copies sessions day by day; the last day follows its length.'));
    // A copy covers as many days as its source (the server refuses any other length): the last day follows the source.
    const fitSource=()=>{
      const r=previous.find(x=>x.id===source.input.value);if(base||!r||setup.input.value!=='previous'||!start.input.value)return;
      const last=shift(start.input.value,Math.round((Date.parse(r.ends_on)-Date.parse(r.starts_on))/864e5)-1);
      if(end.input.value!==last){end.input.value=last;end.input.dispatchEvent(new Event('change'));}};
    // The rota ending closest before the first day is chosen for you (the server finds it, even beyond the first 100),
    // unless you have already chosen one: a late answer never overrides your choice.
    let sourceChosen=false;source.input.addEventListener('change',()=>{sourceChosen=true;fitSource();});
    const pickSource=async()=>{
      const day=start.input.value;if(base||!day||sourceChosen)return;
      const id=(await get<{previous_workspace_id:string|null}>(`/api/workspaces?limit=1&copy_starts_on=${day}`).catch(()=>null))?.previous_workspace_id;
      if(!id||day!==start.input.value||sourceChosen)return;
      if(!previous.some(r=>r.id===id)){const w=(await rotaMeta(id).catch(()=>null))?.workspace;if(!w)return;previous.push(w);source.input.append(el('option',{value:w.id},rotaChoice(w).label));}
      source.input.value=id;fitSource();};
    const people=check('Include assigned people',false);
    const automate=check('Run auto-schedule after setup',true);
    const activeTemplates=all(ctx,'recurring_series').filter(r=>r.active);
    const allTemplates=check('Use all active recurring sessions',true);
    const templates=multi('Recurring sessions',options(activeTemplates));
    // Draft creation freezes the department's inputs: one coherent snapshot by default. A custom scope (controlled
    // sandboxes) waits under Advanced; the server adds what the chosen groups depend on and records what it left out.
    const customScope=check('Custom scope',false);
    const scopeChecks=BASELINE_SOURCES.map(([value,label])=>{const f=check(label,false);f.input.value=value;return f;});
    const scopeCount=el('p',{class:'hint','aria-live':'polite'});
    const scopeGroups=el('fieldset',{class:'multi-field'},el('legend',{},'Baseline groups'),el('div',{class:'check-grid'},...scopeChecks.map(f=>f.wrap)),scopeCount);
    const advanced=el('summary',{},'Advanced');
    const baseline=el('details',{class:'span-2'},advanced,el('div',{class:'switch-group'},customScope.wrap),scopeGroups);
    const syncBaseline=()=>{const custom=customScope.input.checked,n=scopeChecks.filter(f=>f.input.checked).length;
      scopeGroups.hidden=!custom;
      scopeCount.textContent=n?`${plural(n,'group')}, plus what ${n===1?'it depends':'they depend'} on`:'Choose at least one group.';};
    for(const f of [customScope,...scopeChecks])f.input.addEventListener('change',syncBaseline);syncBaseline();
    const updateSetup=()=>{source.input.disabled=setup.input.value!=='previous';source.wrap.hidden=people.wrap.hidden=setup.input.value!=='previous';allTemplates.wrap.hidden=setup.input.value!=='patterns';templates.wrap.hidden=setup.input.value!=='patterns'||allTemplates.input.checked;automate.wrap.hidden=setup.input.value==='empty';};
    setup.input.addEventListener('change',()=>{updateSetup();fitSource();});allTemplates.input.addEventListener('change',updateSetup);updateSetup();
    const templateIds=()=>allTemplates.input.checked?activeTemplates.map(r=>r.id):templates.values();
    for(const w of [name.wrap,urgency.wrap,setup.wrap])w.classList.add('span-2');
    // Dates that touch a published rota: how the draft fits, right under the dates (they stay editable). The primary
    // button repeats the choice ("Create draft from 2 Nov"); "Change the published rota" opens its amendment instead.
    const fit=el('div',{class:'span-2','aria-live':'polite'});let choice:ReturnType<typeof overlapChoice>|null=null,asked='';
    const submit=()=>form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');
    // F-N1: with auto-schedule after setup ticked the button says so, so Create never starts a whole-month run unseen.
    const verb=()=>!automate.wrap.hidden&&automate.input.checked?'Create and auto-schedule':'Create draft';
    const label=()=>{const b=submit();if(b)b.textContent=choice?choice.verb(verb()):verb();};
    for(const x of [automate.input,setup.input])x.addEventListener('change',label);
    const checkDates=async()=>{
      // Dates not yet whole or in order: an answer still on its way for the earlier dates no longer names this rota.
      if(base||!start.input.value||!end.input.value||end.input.value<start.input.value){asked='';return;}
      const key=start.input.value+'|'+end.input.value;if(key===asked)return;asked=key;
      const o=await get<Overlaps>(`/api/schedule/overlaps?starts_on=${start.input.value}&ends_on=${shift(end.input.value,1)}`).catch(()=>null);
      if(key!==asked)return;
      // Dates changed after a choice was made (or an answer for half-typed dates was drawn first): the choice stays.
      choice=o&&o.publications.length?overlapChoice(o,ctx.me.organisation.timezone,label,undefined,choice?.value()?.choice):null;
      suggest((o as {suggested_name?:string}|null)?.suggested_name);
      fit.replaceChildren(...(choice?[choice.root]:o?.drafts.length?[note(`Also being planned for these days: ${o.drafts.map(d=>d.name).join(', ')}. You can compare them side by side once this one is created.`,'info')]:[]),...gapNotes());label();
    };
    // Days in no rota: next to these dates, or anywhere ahead in the department. One line each, with the dates to use.
    const gapNotes=()=>{
      const a=start.input.value,b=shift(end.input.value,1),now=today(ctx);
      const covered=(x:string,y:string)=>previous.some(r=>r.starts_on<y&&r.ends_on>x);
      const before=previous.filter(r=>r.ends_on<=a).sort((x,y)=>y.ends_on.localeCompare(x.ends_on))[0];
      const after=previous.filter(r=>r.starts_on>=b).sort((x,y)=>x.starts_on.localeCompare(y.starts_on))[0];
      const gaps:{starts_on:string;ends_on:string;suggested_name?:string}[]=[
        ...(before&&before.ends_on<a&&!covered(before.ends_on,a)?[{starts_on:before.ends_on,ends_on:a}]:[]),
        ...(after&&after.starts_on>b&&!covered(b,after.starts_on)?[{starts_on:b,ends_on:after.starts_on}]:[]),
        ...list.coverage_gaps.filter(g=>g.ends_on>now&&!(g.starts_on<b&&g.ends_on>a))];
      const notes=nearestGaps(gaps.filter((g,i)=>gaps.findIndex(x=>x.starts_on===g.starts_on&&x.ends_on===g.ends_on)===i),a,b).map(g=>{
        const n=note(`${dates(g.starts_on,g.ends_on)} ${Date.parse(g.ends_on)-Date.parse(g.starts_on)>864e5?'are':'is'} in no rota. `,'warning');
        n.append(button('Use these dates',()=>{start.input.value=g.starts_on;end.input.value=shift(g.ends_on,-1);suggest(g.suggested_name);
          for(const f of [start,end])f.input.dispatchEvent(new Event('change'));},'link-button'));
        return n;});
      // The nearest stay in view; the rest fold into one quiet line, never a wall of warnings.
      return notes.length>GAPS_SHOWN?[...notes.slice(0,GAPS_SHOWN),el('details',{},el('summary',{},`${notes.length-GAPS_SHOWN} more in no rota`),...notes.slice(GAPS_SHOWN))]:notes;
    };
    for(const f of [start,end])f.input.addEventListener('change',()=>void checkDates());
    start.input.addEventListener('change',()=>{if(sourceChosen)fitSource();else void pickSource();});
    queueMicrotask(()=>{void checkDates();void pickSource();});
    form.append(name.wrap,dateRange('Dates',start,end),fit);if(base)form.append(urgency.wrap,urgentReason.wrap);
    else form.append(setup.wrap,source.wrap,people.wrap,allTemplates.wrap,templates.wrap,automate.wrap);
    form.append(baseline);
    // "Use all" with none active is an empty rota, never a choice to make; only a hand-picked empty list asks for one.
    return async()=>{if(!base&&setup.input.value==='patterns'&&!allTemplates.input.checked&&!templates.values().length)throw new Error('Choose at least one recurring session.');
      const fits=choice?.value();
      if(fits?.choice==='amend'){const p=(await get<{publications:Row[]}>('/api/publications')).publications.find(x=>x.id===fits.publication_id);if(p){form.closest('dialog')?.remove();return workspaceForm(ctx,p);}}
      const custom=customScope.input.checked,sources=scopeChecks.filter(f=>f.input.checked).map(f=>f.input.value);
      if(custom&&!sources.length){baseline.open=true;throw new Error('Choose at least one baseline group.');}
      const urgent=Boolean(base&&urgency.input.value==='urgent');const result=await command('workspace.create',{name:name.input.value,starts_on:fits?fits.starts_on:start.input.value,
        ...(fits?.choice==='replace_from'?{replaces_publication_id:fits.publication_id}:{}),ends_on:base?base.ends_on:shift(end.input.value,1),base_publication_id:base?.id||null,urgency:urgent?'urgent':'routine',urgency_reason:urgent?urgentReason.input.value:'',baseline:custom?{mode:'custom',sources}:{mode:'department'},source_workspace_id:!base&&setup.input.value==='previous'?source.input.value:null,source_revision:!base&&setup.input.value==='previous'?previous.find(r=>r.id===source.input.value)?.revision:null,copy_people:people.input.checked,series_ids:!base&&setup.input.value==='patterns'?templateIds():[],generate_assignments:!base&&setup.input.value!=='empty'&&automate.input.checked});
      if(!base&&setup.input.value!=='empty'&&automate.input.checked)window.dispatchEvent(new CustomEvent('rota-auto-schedule',{detail:{workspace:result.workspace.id}}));
      ctx.setWorkspace(result.workspace.id);ctx.navigate('schedule');notify(base?'Amendment draft saved. Nothing published.':['Draft saved',result.setup?.created?plural(result.setup.created,'session')+' created':!base&&setup.input.value==='previous'&&!result.setup?.skipped?'0 sessions copied (the previous rota has none)':'',result.setup?.skipped?plural(result.setup.skipped,'date')+' skipped':'',result.template_skipped?.length?plural(result.template_skipped.length,'template place')+' left open (on leave, away or already booked)':''].filter(Boolean).join(' · ')+'. Nothing published.');await ctx.refresh();};
  },'Create draft');
}

/** Session editor tabs: Session (the form), Suggested staff, Checks and History. Only the form saves. */
function sessionTabs(ctx:Context,data:WorkspaceData,shift:Row,form:HTMLFormElement,add:(personId:string)=>void){
  const zone=ctx.me.organisation.timezone,w=data.workspace;
  const findings=((w.validation?.findings||[]) as unknown as Row[]).filter(f=>f.shift_id===shift.id);
  const tabs:[string,string][]=[['session','Session'],['staff','Suggested staff'],['checks',`Checks${findings.length?' · '+findings.length:''}`],['history','History']];
  const bar=el('div',{class:'span-2 sheet-tabs',role:'tablist','aria-label':'Session editor'});
  const prefix='session-editor-'+shift.id;
  const panels:Record<string,HTMLElement>=Object.fromEntries(tabs.map(([id])=>[id,el('div',{id:prefix+'-panel-'+id,class:id==='session'?'span-2 form-grid':'span-2 sheet-panel',role:'tabpanel','aria-labelledby':prefix+'-tab-'+id})]));
  const footer=form.querySelector('.form-footer'),error=footer?.previousElementSibling;
  for(const child of [...form.children])if(child!==footer&&child!==error)panels.session.append(child);
  const loaded=new Set<string>();
  const show=(id:string)=>{
    bar.querySelectorAll('[role=tab]').forEach(t=>t.setAttribute('aria-selected',String((t as HTMLElement).dataset.tab===id)));
    for(const [key,panel] of Object.entries(panels))panel.hidden=key!==id;
    if(!loaded.has(id)){loaded.add(id);if(id==='staff')loadStaff();if(id==='checks')loadChecks();if(id==='history')loadHistory();}
  };
  for(const [id,label] of tabs){const t=el('button',{id:prefix+'-tab-'+id,type:'button',role:'tab','data-tab':id,'aria-controls':prefix+'-panel-'+id,'aria-selected':String(id==='session')},label);t.addEventListener('click',()=>show(id));bar.append(t);}
  form.prepend(bar);for(const [id,panel] of Object.entries(panels)){panel.hidden=id!=='session';form.insertBefore(panel,error||footer);}
  function loadStaff(){
    const box=panels.staff;box.replaceChildren(working('Checking everyone against every active rule…'));
    if(w.status!=='draft'||shift.locked||shift.cancelled){box.replaceChildren(note('Suggestions are available for open, unlocked sessions in a draft.'));return;}
    fillOptions({workspace_id:w.id,shift_id:shift.id,expected_revision:w.revision}).then(result=>{
      const best=result.candidates.filter((c:Row)=>c.status!=='blocked').slice(0,10);
      box.replaceChildren(el('h3',{},'Best matches'),el('p',{class:'hint'},'Everyone is checked against every active rule. Best fit first: no new issues, then fewest hours this week. Adding someone here changes nothing until you save.'),
        ...(best.length?best.map((c:Row)=>{const b=button('Add',()=>{add(c.person_id);show('session');},c.status==='ok'?'primary small':'secondary small');
          const target=48,h=Number(c.week_hours)||0,bar=el('span',{class:'hours-bar','aria-label':`${h} of ${target} hours this week`},el('span',{}));(bar.firstChild as HTMLElement).style.setProperty('width',Math.min(100,Math.round(h/target*100))+'%');
          return el('div',{class:'wk-cand '+(c.status==='ok'?'ok':'warn')},el('div',{class:'wk-cand-top'},el('strong',{},c.display_name),b),
            el('div',{class:'cand-hours'},bar,el('small',{},`${h} h this week · ${c.rota_hours} h this rota${c.rest_before_hours!=null?` · rested ${Math.round(c.rest_before_hours)} h`:''}${c.can_lead?' · can lead':''}`)),
            c.status==='warn'?el('p',{class:'wk-why'},c.findings.slice(0,2).map((f:Row)=>'! '+f.message).join(' · ')):'');}):[note('Nobody else passes every rule for this session. Open the session’s fill panel on the schedule to see who is blocked and why.','warning')]));
    }).catch(e=>box.replaceChildren(note((e as Error).message,'error',e)));
  }
  function loadChecks(){
    const box=panels.checks,name=(id:string)=>fullNameOf(ctx.catalogues.people.find(p=>p.id===id));
    box.replaceChildren(el('h3',{},'Rota check for this session'),findings.length?issueList(findings.map(f=>findingIssue(f,f.person_id?name(f.person_id):'Whole session'))):note(w.validation?'No issues on this session at the last check.':'Not checked yet.',w.validation?'success':'info'),
      el('h3',{},'Rules applied'),skeleton('lines','Loading the rules applied…'));
    get<Row>(`/api/workspaces/${w.id}/shifts/${shift.id}/rules`).then(r=>{
      box.lastElementChild!.replaceWith(el('div',{},...r.people.map((person:Row)=>el('div',{class:'rules-person'},el('strong',{},name(person.person_id)),
        el('ul',{class:'rule-list'},...person.rules.map((x:Row)=>el('li',{class:'rule-row '+x.status},el('span',{class:'rule-icon','aria-hidden':'true'},x.status==='passes'?'✓':x.status==='unchecked'?'–':'!'),el('span',{class:'rule-text'},x.name,x.messages.length?el('small',{},x.messages.join(' · ')):null))))))));
    }).catch(e=>box.lastElementChild!.replaceWith(note((e as Error).message,'error',e)));
  }
  function loadHistory(){
    const box=panels.history;box.replaceChildren(skeleton('table','Loading history…'));
    get<Row>(`/api/workspaces/${w.id}/shifts/${shift.id}/history`).then(r=>{
      box.replaceChildren(el('h3',{},'History'),r.entries.length?el('ol',{class:'history-list'},...r.entries.map((e:Row)=>el('li',{class:e.undone?'undone':''},el('strong',{},e.changes.join(' · ')),el('small',{},`${formatDate(e.at,zone)} · ${new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:zone}).format(new Date(e.at))} · ${e.by}${e.reason&&!/^(Edit|Create) session$/.test(e.reason)?' · '+e.reason:''}${e.undone?' · undone':''}`)))):note('No recorded changes to this session yet.'),
        r.partial?el('p',{class:'hint'},'Showing changes from the last 60 edits to this rota.'):'');
    }).catch(e=>box.replaceChildren(note((e as Error).message,'error',e)));
  }
}
/** SW3-A-10: on the period's rota only its Rota Coordinator adds, removes, closes or redefines sessions. Decided by the
 *  server (the rota read's `can_change_fixed`: planning_groups.unrestricted for this reader, the one gate in front of
 *  every such refusal in guard_changes), never by a copy of its rule. The words: terms.ts onlyRotaCoordinator. */
export const sessionsHeld=(data:WorkspaceData|null|undefined)=>(data as {can_change_fixed?:boolean}|null|undefined)?.can_change_fixed===false;
/** `like` starts a new session from an existing one (same duty, place, services and times) on `day`. Every Add session
 *  comes here: on a rota whose sessions are held (sessionsHeld) it says why instead of opening a form the server refuses. */
export function shiftForm(ctx:Context,data:WorkspaceData,shift?:Row,like?:{shift?:Row;day?:string}){
  if(!shift&&sessionsHeld(data)){notify(onlyRotaCoordinator('add'),'info');return;}
  const ws=data.workspace;
  dockPanel({key:'session-edit',rota:ws.id,width:620,mark:shift&&rowMark(shift.id),title:shift?'Edit session and participants':'Add session',sub:`Use ${ctx.me.organisation.timezone} local time. Changes stay in the draft until reviewed and published.`,build:d=>formInto(d.body,d.close,d.tryClose,form=>{
    const base=shift||like?.shift,zone=ctx.me.organisation.timezone,day=like?.day||ws.starts_on;
    // A template keeps its clock times and day offset, moved to the chosen day.
    const moved=(value:string)=>{const local=toLocal(value,zone);if(shift||!like?.day)return local;const shiftBy=(Date.parse(local.slice(0,10))-Date.parse(toLocal(like.shift!.start,zone).slice(0,10)));return new Date(Date.parse(day)+shiftBy).toISOString().slice(0,10)+local.slice(10);};
    const typ=select('Duty type',options(all(ctx,'assignment_types')),base?.assignment_type_id||'');
    const location=select('Location',options(all(ctx,'locations')),base?.location_id||'');
    // A staffed duty needs a service (the server says so too): with only one service it is chosen already, otherwise
    // the form asks before anything is calculated or saved.
    const serviceChoices=options(all(ctx,'services'));
    // The shared multi-choice field (dom.ts multi: ticks for a short list, the multi-select once it is long).
    const services=multi('Clinics',serviceChoices,base?.service_ids||(serviceChoices.length===1?[serviceChoices[0].value]:[]));
    // D-074: required exactly when the server requires it (needsClinic), so the field never reads "(optional)" for a staffed
    // duty and Save stops at the field; the cue follows the duty type. A tick list has no required state: its first box
    // carries the message.
    const needService=()=>{const need=needsClinic(lookup(ctx,'assignment_types',typ.input.value)),
        holder=services.wrap.querySelector<HTMLSelectElement|HTMLInputElement>('select[data-native],input[type=checkbox]');if(!holder)return;
      const was=holder.required;if(holder instanceof HTMLSelectElement)holder.required=need;
      holder.setCustomValidity(need&&!services.values().length?'Choose at least one clinic.':'');
      const form=services.wrap.closest('form');if(form&&was!==holder.required)markFields(form);};
    services.wrap.addEventListener('change',needService);typ.input.addEventListener('change',needService);needService();
    const start=field('Session start','datetime-local',base?moved(base.start):`${day}T08:00`,{required:true,step:60});
    const end=field('Session end','datetime-local',base?moved(base.end):`${day}T12:00`,{required:true,step:60});
    let customTiming=Boolean(base);
    // The end must come after the start: said on the field itself, as the browser shows any field error.
    const order=()=>end.input.setCustomValidity(start.input.value&&end.input.value&&end.input.value<=start.input.value?'The session must end after it starts.':'');
    for(const input of [start.input,end.input]){input.addEventListener('input',()=>{customTiming=true;});input.addEventListener('change',order);input.addEventListener('input',order);}
    const customBox=el('div',{class:'span-2'});let attrs=()=>({} as Record<string,unknown>);
    function renderAttrs(){const type=lookup(ctx,'assignment_types',typ.input.value);const controls=attributesForm(type?.definition?.fields||[],shift?.attributes||{});customBox.replaceChildren(controls.wrap);attrs=controls.read;}
    const useDefaultTiming=()=>{
      const timing=lookup(ctx,'assignment_types',typ.input.value)?.definition?.default_timing;if(!timing)return false;
      const day=start.input.value.slice(0,10)||ws.starts_on,next=new Date(day+'T00:00:00Z');
      if(timing.end_time==='24:00'||timing.end_time<timing.start_time)next.setUTCDate(next.getUTCDate()+1);
      start.input.value=day+'T'+timing.start_time;end.input.value=next.toISOString().slice(0,10)+'T'+(timing.end_time==='24:00'?'00:00':timing.end_time);
      start.input.dispatchEvent(new Event('change'));end.input.dispatchEvent(new Event('change'));
      return true;
    };
    const timingButton=button('Use default timing',()=>{if(useDefaultTiming()){customTiming=false;syncTiming();}},'link-button');
    // The duty type's default timing is a quiet fact while the times follow it; "Use default timing" only once they differ.
    const timingFact=el('small',{class:'muted'},'Default timing applied'),timing=el('div',{class:'span-2 session-timing'},timingFact,timingButton);
    function syncTiming(){const has=!!lookup(ctx,'assignment_types',typ.input.value)?.definition?.default_timing;timing.hidden=!has;timingFact.hidden=customTiming;timingButton.hidden=!customTiming;}
    for(const input of [start.input,end.input])input.addEventListener('input',syncTiming);
    typ.input.addEventListener('change',()=>{renderAttrs();if(!customTiming)useDefaultTiming();syncTiming();});renderAttrs();syncTiming();
    // Nothing to choose from yet (a new department): said under the field, with where it is set up.
    for(const [wrap,kind] of [[typ.wrap,'assignment_types'],[location.wrap,'locations'],[services.wrap,'services']] as const){const missing=missingSetup(ctx,kind);if(missing)wrap.append(missing);}
    // Duty type, clinic, place, then when (SR-12).
    form.append(typ.wrap,services.wrap,location.wrap,start.wrap,end.wrap,timing,customBox);
    // Held (sessionsHeld): what defines the session (guard_changes compares all of it: duty type, clinics, place, times,
    // its fields) stays in place, disabled, and says why; who works it stays editable.
    if(shift&&sessionsHeld(data)){const why=el('p',{class:'hint sessions-held span-2',id:'session-define-held'},onlyRotaCoordinator('define'));typ.wrap.before(why);
      for(const wrap of [typ.wrap,services.wrap,location.wrap,start.wrap,end.wrap,timing,customBox])
        for(const c of wrap.querySelectorAll<HTMLInputElement>('input,select,textarea,button')){c.disabled=true;c.setAttribute('aria-describedby','session-define-held');}}
    const participantBody=el('div',{class:'span-2'}),rows:{node:HTMLElement;person:()=>string;remove:()=>void;read:()=>Promise<Record<string,unknown>>;cut:()=>Promise<string|null>}[]=[];
    const leadGroup='team-lead-'+Math.random().toString(36).slice(2);
    const noLead=el('input',{type:'radio',name:leadGroup,value:'',checked:!(shift?.participants||[]).some((p:Row)=>p.team_lead)}) as HTMLInputElement;
    const leadSyncs:(()=>void)[]=[],leadInputs:HTMLInputElement[]=[];
    const noLeadWrap=el('label',{class:'checkbox span-2'},noLead,el('span',{class:'check-text'},el('span',{},'No team lead'),el('span',{class:'hint'},'Or choose “Team lead” on one person below.')));
    const manyHint=el('p',{class:'hint span-2'});
    // One lead: a radio per person plus "No team lead". A duty type allowing more: a checkbox per person, up to its most.
    // Who may lead, from the server's dated lead context (TL-03); the type's legacy setting and the local hint only
    // while it is not available. Pending, failed and stale answers are said; saving checks it again either way.
    let leadNow:LeadState|null=null,leadAsk=0;
    const askLead=async()=>{const n=++leadAsk;
      if(!typ.input.value||!location.input.value||!start.input.value||!end.input.value){leadNow=null;syncLeads();return;}
      leadNow={state:'pending'};syncLeads();
      try{const parts=(await Promise.all(rows.map(r=>r.read()))).filter(p=>p.person_id).map(p=>({...p,team_lead:false})) as unknown as NonNullable<LeadContextInput["shift"]["participants"]>;
        const c=await leadContext(ws.id,ws.revision,{shift:{start:await instant(start.input.value),end:await instant(end.input.value),assignment_type_id:typ.input.value,location_id:location.input.value,
          service_ids:services.values(),attributes:attrs(),participants:parts},shift_id:shift?.id??null,person_ids:[...new Set(parts.map(p=>String(p.person_id)))]});
        if(n===leadAsk){leadNow=leadState(c,ws.revision);syncLeads();}}
      catch(e){if(n===leadAsk){leadNow={state:'failed',error:e as Error};syncLeads();}}};
    const syncLeads=()=>{
      const typeLead=lookup(ctx,'assignment_types',typ.input.value)?.definition?.lead,need=leadNow?.state==='ready'?leadNeed(leadNow.context):null;
      const lead=need?need.enabled&&{maximum:need.maximum}:typeLead,most=lead?lead.maximum??Infinity:1,many=most>1;
      let seen=0;
      for(const i of leadInputs){i.type=many?'checkbox':'radio';if(!many&&i.checked&&seen++)i.checked=false;}
      noLeadWrap.hidden=many;manyHint.hidden=!many;
      if(!many)noLead.checked=!leadInputs.some(i=>i.checked);
      // A duty type without a team lead setting offers no lead (a lead kept from before stays visible so it can be cleared).
      if(!lead){noLeadWrap.hidden=!leadInputs.some(i=>i.checked);for(const i of leadInputs)i.closest('label')!.hidden=!i.checked;}
      else for(const i of leadInputs)i.closest('label')!.hidden=false;
      manyHint.textContent=`Choose “Team lead” on ${most===Infinity?'any number of':'up to '+most} people below.`;
      leadSyncs.forEach(f=>f());
      if(many&&leadInputs.filter(i=>i.checked).length>=most)leadInputs.forEach(i=>{if(!i.checked)i.disabled=true;});
    };
    function addParticipant(prior?:Row){
      // The row's own attendance as the server holds it; kept while this row is the same person (keptAttendance).
      const own=prior&&shift?{person:String(prior.person_id),times:{start:String(prior.start),end:String(prior.end),segments:((prior.segments||[]) as Row[]).map(x=>({start:String(x.start),end:String(x.end)}))},was:{start:String(shift.start),end:String(shift.end)}}:null;
      const person=select('Participant',peopleChoices(ctx),prior?.person_id||'');
      const participation=select('Participation role',[{value:'staff',label:'Staff member'},{value:'trainee',label:'Trainee'},{value:'supervisor',label:'Supervisor · verified capability required'}],prior?.participation||'staff');
      const overtime=check('Overtime',prior?prior.overtime||false:Boolean(lookup(ctx,'assignment_types',typ.input.value)?.definition?.extra_cover)),fixed=check('Keep assigned',prior?.fixed||false);
      // Two ideas named side by side (SR-15): Keep assigned holds this person; Lock (on the session) holds all of it.
      fixed.wrap.dataset.tip='Auto-schedule keeps this person here. Lock the session to keep everyone.';
      const supervisor=select('Named supervisor',peopleChoices(ctx),prior?.supervisor_person_id||'',false);
      const leadInput=el('input',{type:'radio',name:leadGroup,checked:Boolean(prior?.team_lead)}) as HTMLInputElement,leadWhy=el('span',{class:'hint'});
      const lead={wrap:el('label',{class:'checkbox'},leadInput,el('span',{class:'check-text'},el('span',{},'Team lead'),leadWhy)),input:leadInput};
      const syncLead=()=>{const pid=person.input.value,st=leadNow,local=()=>leadBlock(ctx,typ.input.value,pid,start.input.value.slice(0,10));
        const me=st?.state==='ready'?st.context.people.find(x=>x.person_id===pid):undefined;
        const block=!pid?'':st?.state==='ready'?(me&&!me.can_lead?'Cannot lead · '+[...new Set(me.failures.map(f=>f.code==='LEAD_NOT_ENABLED'?'no team lead at this time':'does not meet who may lead this duty'))].join(' · '):''):!st?local():st.state==='failed'?local():'';
        leadWhy.textContent=block||(!pid||!st?'':st.state==='pending'?'Checking who may lead…':st.state==='stale'?'The rota changed · saving checks who may lead':st.state==='failed'?'Couldn’t check who may lead · saving checks it':me?'':'Not checked yet');
        leadInput.disabled=Boolean(block)&&!leadInput.checked;};
      person.input.addEventListener('change',syncLeads);leadInput.addEventListener('change',syncLeads);leadSyncs.push(syncLead);leadInputs.push(leadInput);syncLeads();
      // The inline switches name their person for screen readers; a named supervisor matters for trainees (or one already set).
      const syncRow=()=>{const who=person.input.value?person.input.selectedOptions[0]?.textContent||'':'this participant';overtime.input.setAttribute('aria-label',`Overtime — ${who}`);fixed.input.setAttribute('aria-label',`Keep assigned — ${who}`);supervisor.wrap.hidden=participation.input.value!=='trainee'&&!supervisor.input.value;};
      person.input.addEventListener('change',syncRow);participation.input.addEventListener('change',syncRow);syncRow();
      const flags=el('div',{class:'check-grid span-2'},overtime.wrap,fixed.wrap);
      const node=el('div',{class:'nested-card'},el('div',{class:'form-grid'},person.wrap,participation.wrap,lead.wrap,supervisor.wrap,flags));
      const remove=()=>{rows.splice(rows.indexOf(row),1);leadSyncs.splice(leadSyncs.indexOf(syncLead),1);leadInputs.splice(leadInputs.indexOf(leadInput),1);node.remove();syncLeads();form.dispatchEvent(new Event('participants-changed'));};
      const row={node,person:()=>person.input.value,remove,read:async()=>({person_id:person.input.value,participation:participation.input.value,
        supervisor_person_id:supervisor.input.value||null,team_lead:lead.input.checked,overtime:overtime.input.checked,fixed:fixed.input.checked,
        // The times and segments only: `cut` is the form's own warning, never sent (the server refuses extra fields).
        ...(({cut:_cut,...times})=>times)(await attendance())}),
        // Null while this row keeps all its attendance; else the person whose partial attendance the new times cut.
        cut:async()=>own&&person.input.value===own.person&&(await attendance()).cut?person.input.selectedOptions[0]?.textContent||'':null};rows.push(row);
      async function attendance(){const s0=await instant(start.input.value),e0=await instant(end.input.value);
        if(!own||person.input.value!==own.person)return {start:s0,end:e0,segments:[] as Attendance['segments'],cut:false};
        return keptAttendance(own.times,own.was,s0,e0);}
      flags.append(button('Remove participant',remove,'ghost'));participantBody.append(node);
      form.dispatchEvent(new Event('participants-changed'));
    }
    (shift?.participants||[]).forEach(addParticipant);
    // Said only when the session's new times cut someone's partial attendance or presence segments, naming them,
    // before saving; an edit that leaves the times alone keeps everyone's attendance as it is (keptAttendance).
    const cutNote=el('p',{class:'hint','aria-live':'polite'});cutNote.hidden=true;form.append(cutNote);
    let cutAsk=0;
    const syncCut=async()=>{const n=++cutAsk;if(!shift||!start.input.value||!end.input.value){cutNote.hidden=true;return;}
      const names=(await Promise.all(rows.map(r=>r.cut()))).filter((x):x is string=>x!==null);if(n!==cutAsk)return;
      cutNote.hidden=!names.length;
      cutNote.textContent=names.length?`The new times cut the partial attendance of ${names.join(', ')}: the parts outside the new times are removed when you save.`:'';};
    for(const f of [start,end])f.input.addEventListener('change',()=>void syncCut());form.addEventListener('participants-changed',()=>void syncCut());
    typ.input.addEventListener('change',syncLeads);start.input.addEventListener('change',syncLeads);syncLeads();
    // Asked again when anything it depends on changes (the answer is cached per draft revision and form state).
    form.addEventListener('change',e=>{if(!(e.target as Element).matches?.(`input[name="${leadGroup}"]`))void askLead();});form.addEventListener('participants-changed',()=>void askLead());void askLead();
    // Inputs hold organisation wall-clock time; read as UTC so formatting shows exactly that clock time.
    const wholeDuty=el('p',{}),at=(v:string)=>`${formatDate(v+':00Z','UTC')}, ${formatTime(v+':00Z','UTC')}`;
    const syncWhole=()=>{const a=start.input.value,b=end.input.value;wholeDuty.textContent=a&&b?`Whole session for everyone · ${at(a)} – ${a.slice(0,10)===b.slice(0,10)?formatTime(b+':00Z','UTC'):at(b)}`:'';};
    start.input.addEventListener('change',syncWhole);end.input.addEventListener('change',syncWhole);syncWhole();
    form.append(el('section',{class:'form-section'},el('h3',{},'People and planned presence'),wholeDuty,el('div',{class:'form-grid'},
      noLeadWrap,manyHint,participantBody,button('Add participant',()=>addParticipant(),'secondary'))));
    // D-139: a new session is added to the rota as this page knows it now (knownRevision), not as it was when the form
    // opened: the person's own Undo or a check meanwhile must not refuse it "Another session changed this record".
    // An edit keeps the revision it was opened at, so a newer change to that session is never overwritten.
    const save=draftImpact(form,ctx,async()=>({kind:shift?'shift.update':'shift.create',expected_revision:shift?ws.revision:knownRevision(ws.id)??ws.revision,
      payload:{workspace_id:ws.id,assignment_type_id:typ.input.value,location_id:location.input.value,service_ids:services.values(),start:await instant(start.input.value),end:await instant(end.input.value),attributes:attrs(),participants:await Promise.all(rows.map(r=>r.read())),...(shift?{shift_id:shift.id}:{})}}),true,undefined,people=>{
        // Save without the people the server named; the first is then one step from the fill panel's reasoned assign.
        for(const r of rows.filter(r=>people.includes(r.person())))r.remove();
        assignNext=people[0];form.requestSubmit();});
    let assignNext='';
    if(shift)queueMicrotask(()=>{if(form.isConnected)sessionTabs(ctx,data,shift,form,person=>{addParticipant({id:'',revision:0,person_id:person,participation:'staff',start:shift.start,end:shift.end});notify('Added to this session. Check the impact below, then save.');});});
    // A clean save is reported once, by the "Session added/saved" message with Undo; findings get their own warning.
    return async()=>{const next=assignNext;assignNext='';const result=await save();
      const name=next&&fullNameOf(ctx.catalogues.people.find(p=>p.id===next)),saved=(result.shifts as Row[]|undefined)?.find(s=>s.id===result.shift_id);
      if(name&&saved&&ctx.fill)notify(shift?'Session saved.':'Session added.','success',undefined,undefined,{label:`Assign ${name} in the fill panel`,run:()=>ctx.fill!(ws.id,saved,name)});
      if(!result.validation.valid)notify(`Draft saved with ${plural(result.validation.error_count,'mandatory finding')}. Resolve them before review.`,'warning');await ctx.refresh();};
  },'Save draft session')});
}

export function relationshipForm(ctx:Context,kind:'membership'|'eligibility'|'grant',person?:Row){
  // The page's words (P-03): Add rank · Authorise clinics · Grant skill. The governance line stays visible: it limits
  // what the record means, so it is not cut with the other helper text.
  const titles={membership:'Add rank',eligibility:'Authorise clinics',grant:'Grant skill'};
  const list=kind==='membership'?'categories':kind==='eligibility'?'services':'capabilities';
  const said={membership:'Rank added.',eligibility:'Clinic authorised.',grant:'Skill granted.'};
  formDialog(titles[kind],'Membership labels do not establish clinical authority. Clinical records require an authorised credentialer.',form=>{
    const who=select('Person',peopleChoices(ctx),person?.id||'');
    const category=select(kind==='membership'?TERMS.rank:'Skill',options(all(ctx,list)));
    const start=field('From','date',today(ctx),{required:true}),end=field('Last day (optional)','date','');lastDayIncluded(end.input);
    const service=select('Only at clinic',options(all(ctx,'services')),'',false),evidence=area('Evidence you checked','');
    // Onboarding usually covers several clinics: eligibility takes many clinics, one record each, same dates.
    const services=kind==='eligibility'?multi('Clinics',options(all(ctx,'services')),[],'Choose one or more clinics'):null;
    const choice=services?services.wrap:category.wrap;
    // Nothing to choose yet (P-24): say what is missing and where it is set up, instead of an empty required field.
    // Judged by the choices offered: a retired rank or skill is still a choice for historical dates.
    const none=!all(ctx,list).length,missing=none?missingSetup(ctx,list)||el('p',{class:'hint missing-setup',role:'status'},`No ${kind==='grant'?'skills':kind==='membership'?'ranks':'clinics'} yet. An admin sets these up in Configuration.`):null;
    if(missing)choice.append(missing);
    // SW3-D-14: opened from a person's panel, the person is fixed: said, never a picker.
    form.append(person?el('div',{class:'field relationship-person'},el('span',{class:'field-label'},'Person'),el('strong',{},fullNameOf(person))):who.wrap,choice,dateRange('Dates',start,end));
    if(kind==='grant'){evidence.input.required=true;evidence.input.minLength=3;evidence.input.maxLength=500;form.append(service.wrap,evidence.wrap);}
    if(services)return async()=>{
      const ids=services.values();if(!ids.length)throw new Error(none?'Set up clinics first.':'Choose at least one clinic.');
      await runBulk({collection:'eligibilities',noun:['clinic permission','clinic permissions'],message:ids.length>1?'{n} clinics authorised.':said.eligibility,
        onDone:()=>ctx.refresh(),body:{item_kind:'eligibility.create',common:{person_id:who.input.value,starts_on:start.input.value,ends_on:end.input.value||null},items:ids.map(id=>({payload:{service_id:id}})),reason:'Clinic permissions authorised'}});};
    return async()=>{if(none)throw new Error(`Set up ${kind==='grant'?'skills':'ranks'} first.`);
      await command(kind+'.create',{person_id:who.input.value,[kind==='membership'?'category_id':'capability_id']:category.input.value,starts_on:start.input.value,ends_on:end.input.value||null,...(kind==='grant'?{service_id:service.input.value||null,evidence:evidence.input.value}:{})});
      // Said with Undo (the one Undo registry, api.ts): a record just made is taken back as if never added. Said before the
      // refresh, so a slow refresh cannot hand the Undo to the generic "Saved." fallback.
      notify(said[kind]);await ctx.refresh();};
  },titles[kind]);
}

export function programmeEligibilityForm(ctx:Context){
  formDialog('Authorise programme eligibility','Choose the dated roster and services. Only a credentialer can save clinical permissions.',form=>{
    const programme=select('Programme',options(all(ctx,'programmes')));
    const services=multi('Eligible services',options(all(ctx,'services')),[]);
    const start=field('Valid from','date',today(ctx),{required:true});
    const end=field('Valid until (included)','date','',{required:true});lastDayIncluded(end.input);
    const reason=area('Verification evidence or source','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    const roster=el('div',{class:'span-2'});
    const people=()=>[...new Set(all(ctx,'programme_memberships').filter(m=>m.programme_id===programme.input.value
      &&m.starts_on<=start.input.value&&(!m.ends_on||m.ends_on>=end.input.value)).map(m=>String(m.person_id)))];
    const update=()=>{const ids=people();roster.replaceChildren(note(`${ids.length} people have membership for these full dates.`),
      ...(ids.length?[el('p',{class:'dt-pop-label'},'People covered'),el('ul',{class:'tag-member-list','aria-label':'People covered'},...ids.map(id=>el('li',{},fullNameOf(lookup(ctx,'people',id)))))]:[]));};
    for(const input of [programme.input,start.input,end.input])input.addEventListener('change',update);
    form.append(programme.wrap,services.wrap,dateRange('Dates',start,end),reason.wrap,roster);update();
    return async()=>{const ids=people();if(!ids.length)throw new Error('No programme members cover the full selected dates.');
      const result=await command('eligibility.programme',{programme_id:programme.input.value,person_ids:ids,
        service_ids:services.values(),starts_on:start.input.value,ends_on:end.input.value,reason:reason.input.value});
      notify(`${result.created_count} clinical permissions saved for ${result.people_count} people.`);await ctx.refresh();};
  },'Authorise reviewed roster');
}

/** One name for the calendar of when a place or service is open: Opening hours (its page in Configuration). */
function openingHours(ctx:Context,row:Row|null|undefined){
  const list=all(ctx,'operating_calendars'),s=select('Opening hours',options(list),row?.calendar_id||'',false);
  if(!list.length)s.wrap.append(el('small',{class:'hint'},'None yet: add them in Configuration › Opening hours.'));
  return s;
}
/** Recorded kinds that are leave (they carry a leave type); the rest (day off, commitments…) do not. */
export const LEAVE_RECORD_KINDS=['recorded_leave','actual_absence'];
/** The one leave Type control: the shared picker over the department's active types, required. */
export function leaveTypePicker(){
  return select('Type',leaveTypes().map(t=>({value:String(t.id),label:String(t.name),hint:t.counts_as_unavailable===false?'Does not block duties':undefined})));
}
/** Prefill: the planner opens this for a person and day it already knows. */
/** Record leave for a person (staff ask for leave from Requests › New request). `_requestOnly` is kept for callers. */
export function availabilityForm(ctx:Context,_requestOnly=false,prefill:{person_id?:string;day?:string}={}){
  formDialog('Record leave','Record it even if it leaves a gap; the gap then shows in the schedule. Published rotas are not changed automatically.',
    form=>availabilityFields(ctx,form,false,prefill),'Record leave');
}
/** What a leave record is recorded as (AvailabilityUpdate's kinds; approved leave comes from a decided request). */
const RECORD_AS=[{value:'recorded_leave',label:'Leave'},{value:'off_duty',label:'Day off'},{value:'actual_absence',label:'Actual absence'},{value:'external_commitment',label:'External commitment'},{value:'protected_time',label:'Protected time'}];
/** Change one leave record (availability.update, the command the Bulk editor's Leave & availability runs, so single
 *  edit and Bulk agree): only the fields changed are sent; the reply's Undo is offered. */
export function availabilityEditForm(ctx:Context,r:Row,done:()=>Promise<void>|void){
  const zone=ctx.me.organisation.timezone;
  formDialog('Change leave record','Published rotas are not changed automatically.',form=>{  // a behaviour fact, as on Record leave
    const kinds=RECORD_AS.some(k=>k.value===r.kind)?RECORD_AS:[...RECORD_AS,{value:String(r.kind),label:r.kind==='approved_leave'?'Approved leave':String(r.kind).replaceAll('_',' ')}];
    const kind=select('Record as',kinds,String(r.kind)),leaveType=leaveTypePicker();leaveType.input.value=String(r.leave_type_id||leaveType.input.value);
    const start=field('Unavailable from','datetime-local',toLocal(String(r.start),zone),{required:true}),end=field('Unavailable until','datetime-local',toLocal(String(r.end),zone),{required:true});
    const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;reason.wrap.classList.add('span-2');
    const isLeave=()=>[...LEAVE_RECORD_KINDS,'approved_leave'].includes(kind.input.value);
    const sync=()=>{leaveType.wrap.hidden=!isLeave();leaveType.input.disabled=!isLeave();};kind.input.addEventListener('change',sync);sync();
    form.append(kind.wrap,leaveType.wrap,start.wrap,end.wrap,reason.wrap);
    return async()=>{
      const [from,until]=await Promise.all([instant(start.input.value),instant(end.input.value)]);
      const type=isLeave()?leaveType.input.value||null:null;
      const patch:Record<string,unknown>={...(kind.input.value!==r.kind?{kind:kind.input.value}:{}),...(Date.parse(from)!==Date.parse(String(r.start))?{start:from}:{}),
        ...(Date.parse(until)!==Date.parse(String(r.end))?{end:until}:{}),...(type!==(r.leave_type_id||null)?{leave_type_id:type}:{})};
      if(!Object.keys(patch).length)throw new Error('Nothing changed.');
      const result=await command<{warnings?:string[]}>('availability.update',{...patch,reason:reason.input.value},String(r.id),Number(r.revision));
      notify(['Leave record changed.',...(result.warnings?.length?['Warning:',...result.warnings]:[])].join(' '),result.warnings?.length?'warning':'success');await done();
    };
  },'Save');
}
/** The fields of a request (`requestOnly`; `prefill.kinds` limits its kinds) or of recorded availability, built into
 *  `form`; returns the save. Shared by availabilityForm and the Requests page's New request dialog. */
export function availabilityFields(ctx:Context,form:HTMLElement,requestOnly:boolean,prefill:{person_id?:string;person_ids?:string[];day?:string;kinds?:string[]}={}){
    const canChoose=ctx.permits(CAPABILITY.COORDINATOR);
    const people=canChoose?all(ctx,'people'):[{id:ctx.me.person_id!,revision:1,display_name:ctx.me.display_name}];
    const person=select('Person',canChoose?peopleChoices(ctx):options(people),prefill.person_id||ctx.me.person_id||'');
    // Coordinators record the same leave, day off or commitment for several people in one go.
    const many=canChoose&&!requestOnly?multi('People',peopleChoices(ctx),prefill.person_ids||(prefill.person_id?[prefill.person_id]:[]),'Choose one or more people'):null;
    const day=prefill.day||today(ctx);
    const start=field(requestOnly?'Request from':'Unavailable from','datetime-local',`${day}T08:00`,{required:true}),end=field(requestOnly?'Request until':'Unavailable until','datetime-local',`${day}T16:00`,{required:true});
    const kinds=[{value:'leave',label:'Leave'},{value:'clinic_change',label:'Clinic change'},{value:'meeting',label:'Meeting'},{value:'administrative',label:'Administrative'}].filter(k=>!prefill.kinds||prefill.kinds.includes(k.value));
    const requestKind=select('Request type',kinds,kinds[0].value);
    const kind=select('Record as',RECORD_AS,'recorded_leave');
    // Leave says which leave it is, from the department's active types (Configuration › Leave types); never free text.
    const leaveType=leaveTypePicker();
    const isLeave=()=>requestOnly?requestKind.input.value==='leave':LEAVE_RECORD_KINDS.includes(kind.input.value);
    const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=1000;
    const privacy=el('small',{class:'hint',id:reason.input.id+'-hint'},'Private · no patient information');reason.wrap.append(privacy);reason.input.setAttribute('aria-describedby',privacy.id);
    // Whole days (most leave): pick dates only; it runs from the first day's start to the end of the last day.
    const allDay=check('All day',false);
    // All day swaps the two times for one range of days (the shared date picker), carrying the days across.
    const first=field('First day','date',day,{required:true}),last=field('Last day','date',day,{required:true}),days=dateRange('Dates',first,last);
    const sync=()=>{const on=allDay.input.checked;days.hidden=!on;start.wrap.hidden=end.wrap.hidden=on;
      for(const f of [first,last])f.input.disabled=!on;for(const f of [start,end])f.input.disabled=on;};
    const put=(f:{input:HTMLInputElement},v:string)=>{if(!v||f.input.value===v)return;f.input.value=v;f.input.dispatchEvent(new Event('change',{bubbles:true}));};
    // Switching keeps the dates typed so far (and the times, when switching back).
    let times=['08:00','16:00'];
    allDay.input.addEventListener('change',()=>{
      if(allDay.input.checked){times=[start.input.value.slice(11)||times[0],end.input.value.slice(11)||times[1]];put(first,start.input.value.slice(0,10));put(last,end.input.value.slice(0,10));}
      else{put(start,`${first.input.value}T${times[0]}`);put(end,`${last.input.value}T${times[1]}`);}
      sync();
    });sync();
    // The end follows the start: moving the first day moves the last by the same number of days (never before it).
    const follow=(a:{input:HTMLInputElement},b:{input:HTMLInputElement},date:(v:string)=>string,set:(v:string,d:string)=>string)=>{let was=date(a.input.value);
      a.input.addEventListener('change',()=>{const now=date(a.input.value),end=date(b.input.value);if(!now||!was||!end){was=now;return;}
        const shift=Math.round((Date.parse(now)-Date.parse(was))/864e5),moved=shiftDays(end,shift);
        if(shift)put(b,set(b.input.value,moved<now?now:moved));was=now;});};
    const shiftDays=(d:string,n:number)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+n);return x.toISOString().slice(0,10);};
    follow(first,last,v=>v,(_,d)=>d);follow(start,end,v=>v.slice(0,10),(v,d)=>d+v.slice(10));
    const lead=[...(many?[many.wrap]:canChoose?[person.wrap]:[]),...(requestOnly?(kinds.length>1?[requestKind.wrap]:[]):[kind.wrap]),leaveType.wrap];
    // From and until stay side by side: All day takes the whole row unless a field before it fills the other half.
    const syncType=()=>{const on=isLeave();leaveType.wrap.hidden=!on;leaveType.input.disabled=!on;
      allDay.wrap.classList.toggle('span-2',lead.filter(x=>x!==many?.wrap&&!x.hidden).length%2===0);};  // People (many) is a whole row
    // Leave starts as whole days and other kinds as times, until the person sets All day themselves.
    let chosen=false;allDay.input.addEventListener('change',e=>{if(e.isTrusted)chosen=true;});
    const follows=()=>{if(chosen||allDay.input.checked===isLeave())return;allDay.input.checked=isLeave();allDay.input.dispatchEvent(new Event('change'));};
    requestKind.input.addEventListener('change',syncType);kind.input.addEventListener('change',syncType);syncType();
    for(const k of [requestKind,kind])k.input.addEventListener('change',follows);follows();
    const nextDay=(d:string)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+1);return x.toISOString().slice(0,10);};
    const from=()=>allDay.input.checked?first.input.value+'T00:00':start.input.value,until=()=>allDay.input.checked?nextDay(last.input.value)+'T00:00':end.input.value;
    const clash=requestOnly?requestClash(ctx,form,()=>({person:person.input.value,from:from(),until:until(),kind:requestKind.input.value,leaveType:isLeave()?leaveType.input.value:''})):null;
    form.append(...lead,allDay.wrap,start.wrap,end.wrap,days,reason.wrap,...(clash?[clash.wrap]:[]));
    return async()=>{
      if(clash&&await clash.duplicate())throw new Error(person.input.value===ctx.me.person_id?'You already asked for exactly this. Open it under Requests instead of sending it again.':'This person already has exactly this request. Open it under Requests instead of sending it again.');
      const body=async(id:string)=>({person_id:id,start:await instant(from()),end:await instant(until()),private_reason:reason.input.value,kind:requestOnly?requestKind.input.value:kind.input.value,leave_type_id:isLeave()?leaveType.input.value:null});
      if(many&&many.values().length>1){const ids=many.values(),{person_id:_,...common}=await body(ids[0]);
        await runBulk({collection:'availability',noun:['leave record','leave records'],message:'Recorded for {n} people. Review live staffing gaps and arrange cover.',onDone:()=>ctx.refresh(),
          body:{item_kind:'availability.record',common,items:ids.map(id=>({payload:{person_id:id}})),reason:'Leave recorded for several people'}});return;}
      if(many&&!many.values().length)throw new Error('Choose at least one person.');
      const result=await command<{message:string;warnings?:string[]}>(requestOnly?'request.create':'availability.record',await body(many?many.values()[0]:person.input.value));notify(requestOnly?['Request submitted, not yet approved.',...(result.warnings?.length?['Warning:',...result.warnings]:[])].join(' '):'Leave recorded. Check the rota for gaps and arrange cover.',requestOnly&&result.warnings?.length?'warning':undefined);await ctx.refresh();};
}

/** Every waiting or approved leave/other request of one person, page by page (the API pages at 200): a copy at row 201
 *  is still a copy. */
async function allRequests(person:string):Promise<Row[]>{
  const out:Row[]=[];
  for(;;){const page=await get<Row>('/api/requests/all?'+new URLSearchParams({person_id:person,type:'leave,other',status:'waiting,approved',limit:'200',offset:String(out.length)}));
    const items=(page.items||[]) as Row[];out.push(...items);if(items.length<200||out.length>=(page.total??0))return out;}
}
/** Before a request is sent (R-05): the person's own duties and open requests its dates overlap, read again a moment
 *  after the dates change, and whether it is an exact copy of one already waiting or approved (Send then refuses it). */
function requestClash(ctx:Context,form:HTMLElement,read:()=>{person:string;from:string;until:string;kind:string;leaveType:string}){
  const wrap=el('div',{class:'span-2 request-clash','aria-live':'polite'}),zone=ctx.me.organisation.timezone;
  let asked=0,timer=0,mine:Row[]=[],seen='';
  const local=(v:string)=>toLocal(v,zone);
  const same=(r:Row,x:ReturnType<typeof read>)=>r.source==='request'&&r.kind===x.kind&&String(r.leave_type_id||'')===x.leaveType&&local(r.start)===x.from&&local(r.end)===x.until;
  const look=async(again=true)=>{
    const x=read();if(!again&&JSON.stringify(x)===seen)return;seen=JSON.stringify(x);const turn=++asked;
    if(!x.person||!x.from||!x.until||x.until<=x.from){wrap.replaceChildren();return;}
    // The calendar's end day is exclusive: an end inside a day (16:00) reads that whole day; an end at midnight does not.
    const days={start:x.from.slice(0,10),end:x.until.slice(11,16)>'00:00'?addDays(x.until.slice(0,10),1):x.until.slice(0,10)};
    const [cal,reqs]=await Promise.all([get<Row>('/api/calendar?'+new URLSearchParams({...days,person_id:x.person})).catch(()=>null),
      allRequests(x.person).catch(()=>null)]);
    if(turn!==asked||!wrap.isConnected)return;
    const over=(r:Row)=>local(r.start)<x.until&&local(r.end)>x.from;
    const duties=((cal?.events||[]) as Row[]).filter(e=>e.kind==='duty'&&e.person_id===x.person&&!e.cancelled&&over(e));
    mine=(reqs||[]).filter(r=>r.source==='request'||r.source==='availability');
    const others=mine.filter(over),copy=mine.find(r=>same(r,x));
    const when=(r:Row)=>spanLabel(r.start,r.end,zone);
    // Filing for someone else (a coordinator): the notes name them, never "you".
    const own=x.person===ctx.me.person_id,name=own?'':fullNameOf(lookup(ctx,'people',x.person))||'This person';
    const list=duties.slice(0,3).map(d=>`${when(d)} · ${d.label} · ${d.location_name}`).join('; ')+(duties.length>3?` and ${duties.length-3} more`:'');
    wrap.replaceChildren(...(copy?[note(own?'You already asked for exactly this, so it cannot be sent again.':`${name} already has exactly this request, so it cannot be sent again.`,'warning')]:[]),
      duties.length?note(own?`On these dates you have ${plural(duties.length,'duty','duties')}: ${list}. If it is approved, your coordinator arranges cover.`
          :`On these dates ${name} has ${plural(duties.length,'duty','duties')}: ${list}. If it is approved, arrange cover.`,'warning')
        :note(cal?(own?'No duty of yours on these dates.':`${name} has no duty on these dates.`):own?'Your duties on these dates could not be checked.':`${name}’s duties on these dates could not be checked.`,'info'),
      ...(others.length&&!copy?[note(`${own?'You already have':name+' already has'} ${plural(others.length,'request')} on these dates: ${others.slice(0,3).map(r=>`${leaveTypeName(r)||'Request'} ${when(r)}`).join('; ')}.`,'info')]:[]));
  };
  const later=()=>{window.clearTimeout(timer);timer=window.setTimeout(()=>void look(false),400);};
  form.addEventListener('change',later);form.addEventListener('input',later);later();
  return {wrap,duplicate:async()=>{await look();const x=read();return mine.some(r=>same(r,x));}};
}
/** Duty colour as swatches. The colour follows the duty everywhere in the schedule and calendars. */
export function colourSwatches(value:string,automatic=false,hint='Shown on every session of this duty in the schedule and calendars.',exclude:readonly string[]=[]){
  const input=el('input',{type:'hidden',value}) as HTMLInputElement;
  const names:Record<string,string>={...(automatic?{'':'Automatic'}:{}),teal:'Teal',blue:'Sky',violet:'Purple',amber:'Gold',slate:'Slate',coral:'Coral',indigo:'Indigo',pink:'Pink'};
  const wrap=el('fieldset',{class:'multi-field swatches',role:'radiogroup','aria-label':'Colour'},el('legend',{},'Colour'),
    el('div',{class:'swatch-row'},...Object.entries(names).filter(([v])=>!exclude.includes(v)).map(([v,label])=>{
      const radio=el('input',{type:'radio',name:'duty-colour',value:v,'aria-label':label}) as HTMLInputElement;radio.checked=v===value;
      radio.addEventListener('change',()=>{if(radio.checked){input.value=v;input.dispatchEvent(new Event('change',{bubbles:true}));}});
      return el('label',{class:`swatch ${v?'duty-'+v:'auto'}`,'data-tip':label},radio,el('span',{'aria-hidden':'true'}));
    })),el('small',{class:'hint'},hint),input);
  return {wrap,input};
}
/** Rule kinds (plain name, explanation, example), what each does, and the number each is about: the contracts' lists. */
export const RULE_KINDS:readonly (readonly [string,string,string,string])[]=GEN_RULE_KINDS;
export const RULE_HELP:Readonly<Record<string,string>>=GEN_RULE_HELP;
export const RULE_NUMBER_FIELD:Readonly<Record<string,readonly [string,string]>>=GEN_RULE_NUMBER_FIELD;
export {LINE_KINDS};
/** The number field a rule's lines set ('' for rules without a number: restrictions, overtime, transitions):
 *  the rule's own choice (`line_number`, e.g. an allocation order's session priority), else the usual one (generated SSOT). */
export const lineField=(kind:string,definition?:Record<string,any>|null)=>definition?.line_number||LINE_NUMBER_FIELDS[kind]?.[0]||'';
/** What a line's number is called on screen. */
export const lineLabel=(kind:string,fieldName:string)=>fieldName==='session_priority'?'Order':RULE_NUMBER_FIELD[kind]?.[1]||'Number';
/** An allocation order: its lines are sessions in the order auto-schedule fills them (the line's number ranks them). */
export const isOrder=(definition?:Record<string,any>|null)=>definition?.line_number==='session_priority';
/** Who a line names, by kind: on a staffing level the person or ranks are who counts; on a supervision limit, the supervisor. */
export const lineWho=(kind:string)=>['minimum_cover','maximum_participants'].includes(kind)?'Who counts':kind==='supervision_limit'?'Supervisor':'Person';
/** Rule topics, in the order the Rules page shows them (also the groups of rule types in the rule form). One place:
 *  a quota that caps (comparison maximum) reads as a limit, everything else by its kind. */
export const RULE_TOPICS:readonly {name:string;kinds:readonly string[];icon:IconName;hint:string;hue:string}[]=GEN_RULE_TOPICS;
export function ruleTopic(rule:{kind:string;comparison?:string;id?:string;catalogue_version?:number}):typeof RULE_TOPICS[number]{
  if(rule.kind==='block_quota'&&rule.comparison==='maximum')return RULE_TOPICS[1];
  // A catalogue rule belongs with the kind its entry's numbers resolve to (the rule catalogue says which).
  const part=rule.kind==='catalogue'?Object.values(catalogueEntryOf(rule)?.numbers||{}).flat()[0]:undefined;
  if(part)return ruleTopic({...part.fixed,kind:part.kind});
  return RULE_TOPICS.find(t=>t.kinds.includes(rule.kind))||{name:'Other rules',kinds:[],icon:'rules' as IconName,hint:'',hue:'slate'};
}
/** Rules that keep people safe: never replaced by another rule (the server checks too). */
/** A catalogue rule's entry in the representation it was stored in (v1 ids stay readable in history). */
export const catalogueEntryOf=(rule:{kind:string;id?:string;catalogue_version?:number}|undefined)=>rule?.kind==='catalogue'?(rule.catalogue_version===2?RULE_CATALOGUE_V2:RULE_CATALOGUE).find(e=>e.id===rule.id):undefined;
export const safetyRule=(rule:{kind:string;id?:string;catalogue_version?:number}|undefined)=>!!rule&&(NEVER_OVERRIDDEN.includes(rule.kind)||!!catalogueEntryOf(rule)?.safety);
export const allowsException=(policy:Row|undefined)=>!!policy?.definition?.exception_allowed&&!safetyRule(policy?.definition?.rule);
const LINE_PAGE=25;
type LineCol=string;
// Columns a line may use beyond who and its number; shown when some line uses them, or added with “Add column”.
const LINE_COLS:[LineCol,string][]=[['programme','Programme'],['ranks','Ranks'],['dates','Dates'],['days','Days'],['hours','Hours'],['clinics','Clinics'],['types','Duty types']];
const EXTRA_COLS:LineCol[]=['ranks','days','hours','clinics','types'];
/** What a line's own settings are called (which a kind's lines may set: LINE_SETTINGS, generated). Empty: the rule's. */
export const SETTING_LABELS:Record<string,string>={when_person_ids:'When on duty',supervisor_ids:'Led by',mode:'Supervises',
  trainee_category_ids:'Trainee ranks',overflow_maximum:'Extra places',overflow_category_ids:'Extra places for',maximum_amount:'Maximum',target_amount:'Target'};
/** A quota line's number is its minimum (a range, or a minimum), its maximum or its amount. */
export const quotaNumberLabel=(rule:Record<string,any>)=>['minimum','range'].includes(rule.comparison)?'Minimum':rule.comparison==='maximum'?'Maximum':'Target';
export const MODE_LABELS:Record<string,string>={allowed_ranks:'Only these ranks',capacity:'Up to N of these ranks'};
/** A line's setting, else the rule's (the one reading of a line's settings). */
export const lineSetting=(rule:Record<string,any>|undefined,line:PersonAmount|Record<string,any>,key:string)=>{const v=(line.settings as Record<string,unknown>|null|undefined)?.[key];return v==null?rule?.[key]:v;};
/** An allowed-ranks supervision line has no number. */
export const lineHasNumber=(rule:Record<string,any>|undefined,line?:PersonAmount|Record<string,any>)=>!(rule?.kind==='supervision_limit'&&line&&lineSetting(rule,line,'mode')==='allowed_ranks');
/** A quota's limits in one control (rule form and “Change…”): minimum, maximum and an advisory target, any of them.
 *  What is filled decides the meaning: both limits a range, one a minimum or a maximum, only a target a target rule. */
export function quotaLimits(given:Record<string,any>={}){
  const cmp=given.comparison,n=(v:unknown)=>v==null?'':String(v);
  const opts={min:0,max:100000,step:'any',placeholder:'None'};
  const min=field('Minimum','number',['minimum','range'].includes(cmp)?n(given.amount):'',opts);
  const max=field('Maximum','number',cmp==='range'?n(given.maximum_amount):cmp==='maximum'?n(given.amount):'',opts);
  const target=field('Target','number',cmp==='target'?n(given.amount):n(given.target_amount),opts);
  const strength=select('Target strength',STRENGTHS,given.target_strength||'medium');
  const num=(f:{input:HTMLInputElement})=>f.input.value===''?null:Number(f.input.value);
  const onlyTarget=()=>num(min)==null&&num(max)==null&&num(target)!=null;
  const sync=()=>{
    const lo=num(min),hi=num(max),t=num(target);
    min.input.setCustomValidity(lo==null&&hi==null&&t==null?'Set a minimum, a maximum or a target.':'');
    max.input.setCustomValidity(lo!=null&&hi!=null&&hi<lo?'The maximum cannot be below the minimum.':'');
    target.input.setCustomValidity(t!=null&&(lo!=null&&t<lo||hi!=null&&t>hi)?'The target must lie within the minimum and maximum.':'');
    strength.wrap.hidden=t==null||onlyTarget();
  };
  for(const f of [min,max,target])f.input.addEventListener('input',sync);
  sync();
  const wrap=el('fieldset',{class:'span-2 quota-limits'},el('legend',{},'Limits'),el('div',{class:'quota-limit-grid'},min.wrap,max.wrap,target.wrap,strength.wrap),
    el('p',{class:'hint'},'Fill any of them. The minimum and maximum follow the rule’s classification; the target is advice auto-schedule aims for.'));
  const read=()=>{const lo=num(min),hi=num(max),t=num(target);
    const comparison=lo!=null&&hi!=null?'range':lo!=null?'minimum':hi!=null?'maximum':'target';
    return {comparison,amount:lo??hi??t??0,maximum_amount:comparison==='range'?hi:null,
      target_amount:comparison!=='target'?t:null,target_strength:comparison!=='target'&&t!=null?strength.input.value:null};};
  return {wrap,read,onlyTarget,step:(s:string)=>{for(const f of [min,max,target])f.input.step=s;}};
}
/** An allocation order's numbers from the lines' positions: 50, 40 … 10 for five lines (under 100 for long orders). */
export const orderAmounts=(n:number)=>{const step=Math.max(1,Math.min(10,Math.floor(100/n)));return Array.from({length:n},(_,i)=>(n-i)*step);};
/** The one line editor of a rule (SSOT): the rule form, “Change…”, the Rules page's inline “Edit lines” and a Balance
 *  goal's own targets all use it. Each line is a narrower copy of the rule: who (a person, a programme, or both), its own
 *  number (empty: the rule's), dates, and, for a rule that applies only through its lines, days, hours, clinics and duty
 *  types. Columns show only what some line uses; “Add column” reveals the rest. Big rules page 25 lines at a time with
 *  a search, and edits are kept in memory, so thousands of lines stay light. `listed` sets “Only the listed lines”. */
export function perPersonEditor(ctx:Context,kind:string,given:PersonAmount[]=[],opts:{label?:string;hint?:string;dates?:boolean;listed?:boolean;base?:number|null;lineNumber?:string|null;rule?:()=>Record<string,any>}={}){
  const order=opts.lineNumber==='session_priority';
  const rows:PersonAmount[]=given.map(r=>({...r,category_ids:[...(r.category_ids||[])],service_ids:[...(r.service_ids||[])],assignment_type_ids:[...(r.assignment_type_ids||[])],settings:r.settings?{...r.settings}:null}));
  // An allocation order lists its lines first to last: the line's number is only its place.
  if(order)rows.sort((a,b)=>Number(b.amount??0)-Number(a.amount??0));
  const goal=opts.dates===false,personKinds=goal||PER_PERSON_KINDS.includes(kind),lineKinds=!goal&&LINE_KINDS.includes(kind);
  // The line's own settings (a rule for its lines only): columns like the others, empty meaning the rule's.
  const settingKeys:string[]=goal?[]:[...(LINE_SETTINGS[kind]||[])],ruleNow=():Record<string,any>=>({kind,...(opts.rule?.()||{})});
  const allCols:[LineCol,string][]=[...LINE_COLS,...settingKeys.map(k=>[k,SETTING_LABELS[k]||k] as [LineCol,string])];
  const numberField=goal?'amount':lineField(kind,{line_number:opts.lineNumber});
  const quota=!goal&&kind==='block_quota',label=()=>opts.label||(quota?quotaNumberLabel(ruleNow()):lineLabel(kind,numberField));
  // Settings every line may set (a quota line's own maximum and target), not only a rule for its lines.
  const anyLine=!goal&&ANY_LINE_SETTINGS.includes(kind);
  // A quota line's maximum only on a range; its target once the rule, or a line, has one.
  const settingOn=(k:string)=>k==='maximum_amount'?ruleNow().comparison==='range':k==='target_amount'?ruleNow().target_amount!=null||shown.has(k):shown.has(k);
  const who=goal?'Person':lineWho(kind),anyone=who==='Who counts'?'Anyone it matches':who==='Supervisor'?'Any supervisor':'Not specified';
  // Staffing levels and supervision count sessions: they take lines only as a rule for its lines.
  const forced=lineKinds&&!personKinds;
  const listed=check('Only the listed lines',forced||!!opts.listed,'On: the rule applies only through its lines. Off: everyone the rule matches gets its number, and a listed person gets their own.');
  listed.input.disabled=forced||!lineKinds;listed.input.dataset.reviewDisabled=String(forced);
  const tone=(choices:Choice[],t:string)=>choices.map(c=>({...c,tone:t}));  // chip colours by kind (.rp)
  const people=tone(peopleChoices(ctx),'person'),programmes=options(all(ctx,'programmes')),services=tone(options(all(ctx,'services')),'place'),types=tone(options(all(ctx,'assignment_types')),'place'),ranks=rankOptions(ctx);
  const nameOf=(list:Choice[],id?:string|null)=>id?list.find(c=>c.value===id)?.label||'':'';
  const setIn=(r:PersonAmount,k:string)=>{const v=(r.settings as Record<string,unknown>|null|undefined)?.[k];return v!=null&&!(Array.isArray(v)&&!v.length);};
  const uses:Record<LineCol,(r:PersonAmount)=>boolean>={programme:r=>!!r.programme_id,ranks:r=>!!r.category_ids?.length,dates:r=>!!(r.starts_on||r.ends_on),days:r=>!!r.weekdays,hours:r=>!!r.start_time,clinics:r=>!!r.service_ids?.length,types:r=>!!r.assignment_type_ids?.length,
    ...Object.fromEntries(settingKeys.map(k=>[k,(r:PersonAmount)=>setIn(r,k)]))};
  const shown=new Set<LineCol>(allCols.map(c=>c[0]).filter(k=>rows.some(uses[k])));
  let page=0,query='';
  const on=()=>listed.input.checked;
  const cols=()=>allCols.filter(([k])=>settingKeys.includes(k)?(on()||anyLine)&&settingOn(k):on()&&EXTRA_COLS.includes(k)&&shown.has(k));
  // A quota line's own maximum and target sit beside its minimum, before its dates.
  const numCols=()=>cols().filter(([k])=>k==='maximum_amount'||k==='target_amount'),otherCols=()=>cols().filter(c=>!numCols().includes(c));
  // Columns “Add column” may still reveal.
  const addable=()=>allCols.filter(([k])=>!cols().some(c=>c[0]===k)&&(settingKeys.includes(k)?(on()||anyLine)&&k!=='maximum_amount':on()&&(EXTRA_COLS.includes(k)||k==='programme'||k==='dates')&&!shown.has(k)));
  // The line's number column: not on an allocation order (its place is its number) nor a range quota (the rule's two numbers).
  const num=()=>!!numberField&&!order&&lineHasNumber(ruleNow());
  const showProg=()=>on()&&shown.has('programme'),showDates=()=>!goal&&(!on()||shown.has('dates'));
  const changed=()=>wrap.dispatchEvent(new Event('change',{bubbles:true}));
  const body=el('div',{class:'pp-rows'}),head=el('div',{class:'pp-head','aria-hidden':'true'}),pager=el('div',{class:'pp-pager'});
  const search=panelSearch('Find a line','Find a person or programme',undefined,'pp-search');
  const summary=el('span',{class:'muted'});
  const whoText=(r:PersonAmount)=>[nameOf(people,r.person_id),nameOf(programmes,r.programme_id)].filter(Boolean).join(' ');
  function rowNode(r:PersonAmount){
    const cells:HTMLElement[]=[];
    if(order)cells.push(el('span',{class:'pp-rank mono'},String(rows.indexOf(r)+1)));
    const person=select(who,people,r.person_id||'',!on(),anyone);person.input.addEventListener('change',()=>{r.person_id=person.input.value||null;});cells.push(person.wrap);
    if(showProg()){const prog=select('Programme',programmes,r.programme_id||'',false,'Any programme');prog.input.addEventListener('change',()=>{r.programme_id=prog.input.value||null;});cells.push(prog.wrap);}
    if(num()){const n=field(label(),'number',r.amount??'',{step:'any',required:!on()&&!quota,...(on()||quota?{placeholder:opts.base!=null?String(opts.base):'Rule’s'}:{})});
      n.input.addEventListener('input',()=>{r.amount=n.input.value===''?null:Number(n.input.value);});cells.push(n.wrap);
      // Only these ranks: the supervisor may supervise those ranks, with no count.
      if(!lineHasNumber(ruleNow(),r)){n.input.value='';n.input.disabled=true;n.input.required=false;n.input.placeholder='—';n.input.title='No number: this line allows only its trainee ranks';}}
    for(const [k,text] of numCols())cells.push(settingCell(r,k,text));
    if(showDates()){const from=field('From','date',r.starts_on||''),until=field('Until (included)','date',r.ends_on||'');lastDayIncluded(until.input);
      from.input.addEventListener('change',()=>{r.starts_on=from.input.value||null;});until.input.addEventListener('change',()=>{r.ends_on=until.input.value||null;});cells.push(from.wrap,until.wrap);}
    for(const [k,text] of otherCols()){
      if(k==='hours'){const a=field('From time','text',r.start_time||'',{pattern:'([01][0-9]|2[0-3]):[0-5][0-9]',placeholder:'hh:mm'}),b=field('Until time','text',r.end_time||'',{pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00',placeholder:'hh:mm'});
        const sync=()=>{r.start_time=a.input.value||null;r.end_time=b.input.value||null;};a.input.addEventListener('change',sync);b.input.addEventListener('change',sync);
        cells.push(el('div',{class:'pp-hours'},a.wrap,b.wrap));continue;}
      if(settingKeys.includes(k)){cells.push(settingCell(r,k,text));continue;}
      const [choices,values,set]=k==='days'?[WEEKDAY_SHORT_LABELS.map((label,i)=>({value:String(i),label})),(r.weekdays||[]).map(String),(v:string[])=>{r.weekdays=v.length?v.map(Number).sort():null;}]
        :k==='clinics'?[services,r.service_ids||[],(v:string[])=>{r.service_ids=v;}]:k==='ranks'?[ranks,r.category_ids||[],(v:string[])=>{r.category_ids=v;}]:[types,r.assignment_type_ids||[],(v:string[])=>{r.assignment_type_ids=v;}];
      const p=multiPick(text,[...choices] as Choice[],values as string[],set as (v:string[])=>void,k==='days'?'Every day':'Any');
      cells.push(el('div',{class:'field pp-multi'},el('span',{class:'sr-only'},text),p.wrap));
    }
    const node=el('div',{class:'pp-row'},...cells);
    const remove=iconButton('Remove this line','trash',()=>{const at=rows.indexOf(r),pos=[...body.children].indexOf(node);removed.push([r,at]);rows.splice(at,1);draw();changed();
      // Keyboard flow: focus the next line's remove button (else Undo), and say Undo is there.
      (body.children[Math.min(pos,body.children.length-1)]?.querySelector<HTMLElement>('[aria-label="Remove this line"]')||undo).focus();
      announce(`Line removed. ${rows.length} left; Undo puts it back.`);},{class:'bare'});
    if(!order){node.append(remove);return node;}
    const i=rows.indexOf(r),up=iconButton('Move up','chevronUp',()=>move(r,-1),{class:'bare'}) as HTMLButtonElement,down=iconButton('Move down','chevronDown',()=>move(r,1),{class:'bare'}) as HTMLButtonElement;
    // Moving is by place in the whole order: off while a search shows only some lines.
    up.disabled=i===0||!!query.trim();down.disabled=i===rows.length-1||!!query.trim();
    node.append(el('span',{class:'pp-order'},up,down,remove));
    return node;
  }
  /** One of a line's own settings: people, ranks, the kind of limit or a number. Empty: the rule's. */
  function settingCell(r:PersonAmount,k:string,text:string):HTMLElement{
    const s=()=>(r.settings||{}) as Record<string,unknown>,put=(v:unknown)=>{r.settings={...s(),[k]:v} as LineSettings;};
    if(k==='mode'){const m=select(text,Object.entries(MODE_LABELS).map(([value,label])=>({value,label})),String(s().mode||''),false,'As the rule');
      m.input.addEventListener('change',()=>{put(m.input.value||null);draw();});return m.wrap;}
    if(['overflow_maximum','maximum_amount','target_amount'].includes(k)){const base=ruleNow()[k];const n=field(text,'number',s()[k]==null?'':String(s()[k]),{min:0,max:k==='overflow_maximum'?100:100000,step:k==='overflow_maximum'?1:'any',placeholder:base!=null?String(base):'Rule’s'});
      n.input.addEventListener('input',()=>put(n.input.value===''?null:Number(n.input.value)));return n.wrap;}
    const p=multiPick(text,k.endsWith('category_ids')?ranks:people,(s()[k]||[]) as string[],v=>put(v.length?v:null),'As the rule');
    return el('div',{class:'field pp-multi'},el('span',{class:'sr-only'},text),p.wrap);
  }
  function move(r:PersonAmount,by:number){
    const i=rows.indexOf(r),j=i+by;if(j<0||j>=rows.length)return;
    [rows[i],rows[j]]=[rows[j],rows[i]];page=Math.floor(j/LINE_PAGE);draw();changed();
    // Keep focus on the moved line's arrow (the other one at either end).
    const moved=body.children[j-page*LINE_PAGE];
    (moved?.querySelector<HTMLButtonElement>(`[aria-label="${by<0?'Move up':'Move down'}"]:not(:disabled)`)||moved?.querySelector<HTMLButtonElement>('.pp-order button:not(:disabled)'))?.focus();
    announce(`Moved to place ${j+1} of ${rows.length}.`);
  }
  function draw(){
    const q=query.trim().toLocaleLowerCase(),match=rows.filter(r=>!q||whoText(r).toLocaleLowerCase().includes(q));
    const pages=Math.max(1,Math.ceil(match.length/LINE_PAGE));page=Math.min(page,pages-1);
    const titles=[...(order?['#']:[]),who,...(showProg()?['Programme']:[]),...(num()?[label()]:[]),...numCols().map(c=>c[1]),...(showDates()?['From','Until']:[]),...otherCols().map(c=>c[1])];
    head.replaceChildren(...titles.map(t=>el('span',{},t)),el('span',{}));
    // Choice columns grow to their widest choice (a select is capped in CSS), so a label is not cut while there is room.
    const template=[...(order?['28px']:[]),'minmax(150px,max-content)',...(showProg()?['minmax(130px,max-content)']:[]),...(num()?['minmax(80px,120px)']:[]),...numCols().map(()=>'minmax(80px,120px)'),...(showDates()?['minmax(128px,160px)','minmax(128px,160px)']:[]),
      ...otherCols().map(([k])=>k==='hours'?'minmax(170px,210px)':k==='overflow_maximum'?'minmax(80px,120px)':k==='mode'?'minmax(140px,max-content)':'minmax(110px,max-content)'),order?'104px':'32px'].join(' ');
    wrap.style.setProperty('--pp-cols',template);
    head.hidden=!rows.length;
    body.replaceChildren(...match.slice(page*LINE_PAGE,(page+1)*LINE_PAGE).map(rowNode));
    const prev=el('button',{type:'button',class:'icon-button','aria-label':'Previous lines'},icon('chevronLeft')) as HTMLButtonElement,next=el('button',{type:'button',class:'icon-button','aria-label':'Next lines'},icon('chevronRight')) as HTMLButtonElement;
    prev.disabled=page===0;next.disabled=page>=pages-1;prev.addEventListener('click',()=>{page--;draw();});next.addEventListener('click',()=>{page++;draw();});
    pager.replaceChildren(...(pages>1?[prev,el('span',{class:'muted'},`${page*LINE_PAGE+1}–${Math.min((page+1)*LINE_PAGE,match.length)} of ${match.length}`),next]:[]));
    search.hidden=rows.length<=LINE_PAGE;
    summary.textContent=rows.length?` · ${rows.length} ${rows.length===1?'line':'lines'}`:'';
    addCol.hidden=!addable().length;
    undo.hidden=!removed.length;undo.textContent=removed.length>1?`Undo remove (${removed.length})`:'Undo remove';
    add.replaceChildren(icon('plus'),on()||!personKinds?'Add a line':'Add a person');
  }
  search.addEventListener('input',()=>{query=search.value;page=0;draw();});
  // Removing a line is only a change to this form until it is saved: Undo puts the latest removed line back in its place.
  const removed:[PersonAmount,number][]=[];
  const undo=el('button',{type:'button',class:'button ghost small'},'Undo remove') as HTMLButtonElement;
  undo.addEventListener('click',()=>{const last=removed.pop();if(!last)return;const [r,at]=last;rows.splice(Math.min(at,rows.length),0,r);query='';search.value='';page=Math.floor(Math.min(at,rows.length-1)/LINE_PAGE);draw();changed();
    announce('Line put back.');(removed.length?undo:add).focus();});
  const add=button('Add a person',()=>{rows.push({person_id:null,service_ids:[],assignment_type_ids:[]} as unknown as PersonAmount);query='';search.value='';page=Math.floor((rows.length-1)/LINE_PAGE);draw();changed();
    requestAnimationFrame(()=>body.lastElementChild?.querySelector<HTMLElement>('select,input,button')?.focus());},'secondary small');
  // Notion's “+ property”: reveal a column no line uses yet.
  const addCol=opens(el('button',{type:'button',class:'button ghost small'},icon('plus'),'Add column') as HTMLButtonElement,'menu');
  addCol.addEventListener('click',()=>menu(addCol,'Add column',()=>addable().map(([k,text])=>({label:text,icon:'plus' as const,run:()=>{shown.add(k);draw();}}))));
  listed.input.addEventListener('change',draw);
  const hint=opts.hint||(goal?'':order?'Auto-schedule fills the first line’s sessions first, then the next. Use the arrows to change the order.'
    :kind==='block_quota'?'Each line is its own block with its own numbers. An empty number uses the rule’s.'
    :who==='Who counts'?'A line narrows the rule to some sessions and who counts (a person or ranks; empty: anyone it matches). An empty number uses the rule’s.'
    :who==='Supervisor'?'A line narrows the rule to one supervisor, or some sessions or ranks. An empty number uses the rule’s.'
    :'A line gives some people or sessions their own number. An empty number uses the rule’s.');
  const wrap=el('fieldset',{class:'span-2 pp-editor'},el('legend',{},goal?'Own targets':'Lines',summary),
    hint?el('p',{class:'hint'},hint):'',...(lineKinds?[listed.wrap]:[]),search,el('div',{class:'pp-grid'},head,body),pager,el('div',{class:'pp-actions'},add,addCol,undo));
  draw();
  const clean=(r:PersonAmount):PersonAmount=>{const o:Record<string,unknown>={};
    if(r.person_id)o.person_id=r.person_id;if(on()&&r.programme_id)o.programme_id=r.programme_id;if(numberField&&(order||num()&&lineHasNumber(ruleNow(),r))&&r.amount!=null&&!Number.isNaN(r.amount))o.amount=r.amount;
    if(!goal){if(r.starts_on)o.starts_on=r.starts_on;if(r.ends_on)o.ends_on=r.ends_on;}
    if(on()){if(r.category_ids?.length)o.category_ids=r.category_ids;if(r.weekdays?.length)o.weekdays=r.weekdays;if(r.start_time&&r.end_time){o.start_time=r.start_time;o.end_time=r.end_time;}if(r.service_ids?.length)o.service_ids=r.service_ids;if(r.assignment_type_ids?.length)o.assignment_type_ids=r.assignment_type_ids;}
    if(on()||anyLine){const own=Object.fromEntries(settingKeys.filter(k=>setIn(r,k)&&cols().some(c=>c[0]===k)).map(k=>[k,(r.settings as Record<string,unknown>)[k]]));if(Object.keys(own).length)o.settings=own as LineSettings;}
    return o as PersonAmount;};
  // A line that names nothing (no one, no sessions) is an unfinished row: left out.
  const read=()=>{const out=rows.map(clean).filter(r=>Object.keys(r).some(k=>!['amount','starts_on','ends_on'].includes(k)));
    if(order){const n=orderAmounts(out.length);out.forEach((r,i)=>{r.amount=n[i];});}
    return out;};
  // `redraw`: the rule's own settings changed (a range, a supervision mode): the number and placeholders follow.
  return {wrap,read,listed:()=>on()&&read().length>0,redraw:draw};
}
/** Protect an academic calendar entry: a mandatory restriction for the programme (optionally some cohorts or rotation
 *  types) on the entry's dates and chosen days and hours. Approved like any rule; an updated version replaces the old one. */
const OWN_FIELDS=['programme_ids','cohort_ids','rotation_type_ids'];
export function protectTimeForm(ctx:Context,entry:Row,existing?:Row){
  if(!existing?.definition)return protectionFactForm(ctx,entry,existing);
  const rule=existing?.definition?.rule,lines:Row[]=existing?.definition?.scope?.conditions?.conditions||[];
  // The form's own fields read their absolute values, plain or typed; everything else (a relative one too) is kept.
  const owned=ownLines(lines as never,OWN_FIELDS),valuesOf=owned.values;
  const programme=labelOf(lookup(ctx,'programmes',entry.programme_id));
  formDialog(existing?'Update protected time':'Protect this time',`${labelOf(entry)} · ${programme} · ${formatDate(entry.starts_on)} – ${formatDate(entry.ends_on)} (not included)`,form=>{
    const days=multi('Protected days',weekdayOptions(),(rule?.weekdays||[0,1,2,3,4,5,6]).map(String));
    const from=field('From · local time','text',rule?.start_time||'00:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]'});
    const until=field('Until · local time','text',rule?.end_time||'24:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00'});
    const cohorts=multi('Only these cohorts (optional)',options(all(ctx,'cohorts').filter(c=>c.programme_id===entry.programme_id)),valuesOf('cohort_ids'),'Every cohort');
    const types=multi('Only on these rotation types (optional)',options(all(ctx,'rotation_types')),valuesOf('rotation_type_ids'),'Any rotation');
    const source=area('Source or departmental decision',existing?.source||`Academic calendar: ${labelOf(entry)}`);source.input.required=true;source.input.minLength=3;source.input.maxLength=500;
    // A protected-time rule may carry more than this form sets (lines for people, clinics, other conditions): all kept.
    const d=existing?.definition,kept=(d?.scope?.conditions?.match||'all')==='all'?owned.kept as unknown as Row[]:[];
    const extra=(d?.per_person?.length||0)+kept.length+Object.entries(d?.scope||{}).filter(([k,v])=>!['conditions','weekdays','start_time','end_time'].includes(k)&&(Array.isArray(v)?v.length:v)).length;
    form.append(note(`Keeps people in ${programme} off duty at these times, checked on the day of each duty (their programme, cohort and rotation then). It is mandatory once an approver activates it${existing?', and replaces the protected time now in force':''}.`),
      ...(extra?[note(`Its other settings (${plural(extra,'more setting')}, such as lines or clinics) are kept. Change those on the Rules page.`)]:[]),
      days.wrap,from.wrap,until.wrap,cohorts.wrap,types.wrap,source.wrap);
    return async()=>{
      const weekdays=days.values().map(Number);if(!weekdays.length)throw new Error('Choose at least one day.');
      if((d?.scope?.conditions?.match||'all')!=='all')throw new Error('This protected time uses “or” conditions. Change it on the Rules page.');
      const who:Conditions['conditions']=[{field:'programme_ids',op:'any',values:[entry.programme_id]}];
      if(cohorts.values().length)who.push({field:'cohort_ids',op:'any',values:cohorts.values()});
      if(types.values().length)who.push({field:'rotation_type_ids',op:'any',values:types.values()});
      const base=d?structuredClone(d):{schema_version:1,exception_allowed:false,scope:{}};
      await command('policy.create',{name:existing?.name||`Protected time · ${labelOf(entry)}`.slice(0,160),owner:existing?.owner||String((ctx.me.department as Row|undefined)?.name||programme),source:source.input.value,
        starts_on:entry.starts_on,ends_on:entry.ends_on,active:true,definition:{...base,severity:'mandatory',replaces_id:undefined,merged_ids:[],
          scope:{...base.scope,conditions:{match:'all',conditions:[...who,...kept]}},rule:{...(base.rule||{}),kind:'restriction',weekdays,start_time:from.input.value,end_time:until.input.value,leave_buffer_days:base.rule?.leave_buffer_days??null},calendar_entry_id:entry.id}});
      if(existing?.status==='proposed')await command('policy.decline',{reason:'Replaced by an updated protected-time proposal'},existing.id,existing.revision);
      notify(existing?'Update proposed. Once approved it replaces the protected time in force.':'Protected time proposed. An approver activates it.');await ctx.refresh();
    };
  },existing?'Propose update':'Propose protected time');
}
/** The protected-time rule made from an academic calendar entry (the newest one not retired), if any. */
export const protectedTimeRule=(ctx:Context,entryId:string)=>all(ctx,'policies').filter(p=>p.definition?.calendar_entry_id===entryId&&p.status!=='retired')
  .sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||'')))[0];
/** Calendar dates describe an academic block; its protection is an independently reviewed child record. */
export function calendarProtectionPanel(ctx:Context,entry:Row){
  dockPanel({key:'calendar-protection',title:'Protected time',sub:labelOf(entry),width:640,build:({body})=>{
    const load=async()=>{
      body.replaceChildren(skeleton('lines','Loading protected time…'));
      try{
        const {protections}=await get<{protections:Row[]}>(`/api/programme-calendar-entries/${entry.id}/protections`);
        if(!body.isConnected)return;
        const can=(...keys:string[])=>ctx.permits(...keys),pending=protections.filter(f=>f.status==='proposed');
        const changed=async()=>{await ctx.refresh();await load();};
        const decide=(f:Row,action:string,title:string,text:string,label:string)=>confirmAction(title,text,async reason=>{
          await command(`calendar.protection.${action}`,{reason},f.id,f.revision);notify('Protected time '+(action==='undo'?'restored or undone':action)+'.');await changed();},label);
        const legacy=protectedTimeRule(ctx,entry.id),active=protections.filter(f=>f.status==='active');
        body.replaceChildren(note(active.length||legacy?.status==='active'?'Approved protection keeps the programme off duty at its stated times. Calendar names and descriptions can change separately. Retire or undo protection before moving the calendar dates or programme.'
          :'These calendar dates do not keep anyone off duty until protection is independently approved.'));
        if(legacy)body.append(el('div',{class:'actions'},statusBadge(legacy.status,'Protected time · '+legacy.status),
          can(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN)?button('Update protected time',()=>protectTimeForm({...ctx,refresh:changed},entry,legacy),'secondary small'):''));
        if(can(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN)&&entry.kind==='academic_block'&&!legacy)body.append(button('Add protection',()=>protectionFactForm({...ctx,refresh:changed},entry),'secondary small'));
        if(!protections.length&&!legacy)body.append(emptyLine('No protection recorded.'));
        const rows=protections.map(f=>{
          const l=f.data.line as RuleLine,summary=`${(l.weekdays||[]).length===7?'Every day':weekdayNames(l.weekdays||[])} · ${l.start_time}–${l.end_time}`;
          const dates=dateUnits([formatDate(l.starts_on||entry.starts_on),'– '+formatDate(addDays(l.ends_on||entry.ends_on,-1))]);
          const linked=!!f.migration_proposal_id,own=f.proposed_by===ctx.me.identity_id,actions=[];
          if(f.status==='active'&&can(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN)&&!pending.some(p=>p.replaces_id===f.id))actions.push(button('Change…',()=>protectionFactForm({...ctx,refresh:changed},entry,f),'secondary small'));
          if(!linked&&f.status==='proposed'){
            if(can(CAPABILITY.APPROVER)&&mayDecide(ctx.me,'rule.activate',own))actions.push(button('Review',()=>ruleReview({...f,name:'Protected time · '+labelOf(entry)},true,true,changed,
              (kind,id)=>labelOf(all(ctx,kind).find(r=>r.id===id)),ctx.me.organisation.timezone,false,[],'Protected time · '+labelOf(entry),summary,ctx,'calendar.protection'),'primary small'));
            if(own&&can(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN))actions.push(button('Withdraw',()=>decide(f,'undo','Withdraw protection','It never applies; the proposal remains in history.','Withdraw'),'ghost small'));
            else if(can(CAPABILITY.APPROVER,CAPABILITY.COORDINATOR,CAPABILITY.ADMIN))actions.push(button('Decline',()=>decide(f,'decline','Decline protection','The approved protection remains as it is.','Decline'),'ghost small'));
          }
          if(!linked&&f.status==='active'&&can(CAPABILITY.APPROVER))actions.push(button('Retire',()=>decide(f,'retire','Retire protection','It stops applying. The approved record stays in history.','Retire'),'ghost small'),
            button('Undo',()=>decide(f,'undo','Undo protection','Restore its predecessor, if any. The decision stays in history.','Undo'),'ghost small'));
          if(!linked&&f.status==='retired'&&f.approved_by&&can(CAPABILITY.APPROVER)&&!protections.some(p=>p.replaces_id===f.id&&['active','proposed'].includes(p.status)))
            actions.push(button('Restore',()=>decide(f,'undo','Restore protection','It applies again with its previously approved details.','Restore'),'ghost small'));
          return [el('span',{},summary,el('small',{class:'muted'},conditionsText(l.who,(key,id)=>{const kind=RULE_FIELDS.find(x=>x.key===key)?.kind;return tagLabel(ctx,id)||(kind?labelOf(all(ctx,kind).find(r=>r.id===id)):id);})||'Programme members')),
            dates,statusBadge(f.status,f.status==='active'&&f.on===false?'Off':undefined),el('div',{class:'actions'},...actions),f.reason||''];
        });
        if(rows.length)body.append(table(['Protection','Dates','State','Actions','Decision reason'],rows));
        if(protections.some(f=>f.migration_proposal_id))body.append(button('View department proposal',()=>ctx.navigate('rules?simplify=true'),'secondary small'));
      }catch(e){if(body.isConnected)body.replaceChildren(note((e as Error).message,'error',e),button('Try again',load,'secondary small'));}
    };
    void load();
  }});
}
function protectionFactForm(ctx:Context,entry:Row,existing?:Row){
  const base:RuleLine=existing?structuredClone(existing.data.line):{id:crypto.randomUUID(),who:{match:'all',conditions:[{field:'programme_ids',op:'any',values:[entry.programme_id]}]},
    where:null,weekdays:[0,1,2,3,4,5,6],start_time:'00:00',end_time:'24:00',starts_on:entry.starts_on,ends_on:entry.ends_on,numbers:{apply:{value:null,level:'mandatory'}},settings:{restriction:{leave_buffer_days:null}},calendar_entry_id:entry.id,note:''};
  formDialog(existing?'Change protected time':'Protect this time',`${labelOf(entry)} · independently approved before applying`,form=>{
    const fields=ruleConditionFields(ctx),who=conditionBuilder(fields.filter(f=>!SESSION_FIELDS.includes(f.key)),base.who,{allOf:true,startEmpty:true,context:'rule'}),
      where=conditionBuilder(fields.filter(f=>SESSION_FIELDS.includes(f.key)),base.where,{allOf:true,startEmpty:true,context:'rule'});
    const days=multi('Protected days',weekdayOptions(),(base.weekdays||[0,1,2,3,4,5,6]).map(String)),start=field('From · local time','text',base.start_time,{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]'}),
      end=field('Until · local time','text',base.end_time,{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00'});
    const first=field('First protected day','date',base.starts_on||entry.starts_on,{required:true,min:entry.starts_on,max:addDays(entry.ends_on,-1)}),
      last=field('Last protected day (included)','date',base.ends_on||entry.ends_on,{required:true,min:entry.starts_on,max:addDays(entry.ends_on,-1)});lastDayIncluded(last.input);
    const reason=area('Reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    form.append(note('Keeps the matching programme members off duty. Another authorised person reviews and activates this proposal.'),
      part('Who','',who.root),part('Where','',where.root),days.wrap,start.wrap,end.wrap,dateRange('Protected dates',first,last),reason.wrap);
    return async()=>{
      if(!days.values().length)throw new Error('Choose at least one protected day.');
      const line={...base,who:who.read(),where:where.read(),weekdays:days.values().map(Number),start_time:start.input.value,end_time:end.input.value,starts_on:first.input.value,ends_on:last.input.value};
      delete line.measure;delete line.period;delete line.mode;
      await command('calendar.protection.propose',{kind:'protected_time',calendar_entry_id:entry.id,person_id:existing?.person_id||null,catalogue_id:existing?.data.catalogue_id||'protected_time',
        catalogue_version:existing?.data.catalogue_version??1,line,on:existing?.on??true,...(existing?{replaces_id:existing.id,expected_revision:existing.revision}:{}),reason:reason.input.value});
      notify('Protected time proposed. Another approver activates it.');await ctx.refresh();
    };
  },'Propose protected time');
}
const PATTERN_KINDS=['run_length','days_off_run','weekend_shape','shift_succession','together_apart'];
/** Settings for the work-pattern rules (advisory: they guide auto-schedule and the score, never block). Returns the rule reader. */
function patternFields(ctx:Context,kind:string,config:HTMLElement):()=>Record<string,unknown>{
  const types=options(all(ctx,'assignment_types')),num=(f:{input:{value:string}})=>Number(f.input.value);
  const help=(text:string)=>note(text);
  if(kind==='run_length'){
    const min=field('Shortest run (days in a row)','number',2,{min:2,max:7,step:1}),max=field('Longest run (days in a row)','number','',{min:1,max:31,step:1});
    config.append(min.wrap,max.wrap,help('Counts the days a matching duty starts on. Leave one box empty to set only the other. Example: at least 2 and at most 5 avoids single isolated days and long stretches.'));
    return ()=>({kind,minimum:optionalNumber(min.input),maximum:optionalNumber(max.input)});
  }
  if(kind==='days_off_run'){
    const min=field('Days off together (at least)','number',2,{required:true,min:2,max:7,step:1});
    config.append(min.wrap,help('A break between working days should last at least this many days, so people get proper rest instead of single days off.'));
    return ()=>({kind,minimum:num(min)});
  }
  if(kind==='weekend_shape'){
    const shape=select('Shape of the weekend',[{value:'complete',label:'Whole weekend: work every weekend day or none'},{value:'identical_types',label:'Same duty on each weekend day worked'}],'complete');
    const first=select('Weekend starts',weekdayOptions(),'4'),length=field('Weekend length in days','number',2,{required:true,min:2,max:3,step:1});
    shape.wrap.classList.add('span-2');config.append(shape.wrap,first.wrap,length.wrap,help('Uses the day each duty starts. A night that starts on Friday counts as Friday.'));
    return ()=>({kind,shape:shape.input.value,weekend_start:num(first),weekend_days:num(length)});
  }
  if(kind==='shift_succession'){
    const mode=select('Preference',[{value:'forward',label:'Forward rotation: never an earlier duty the day after a later one'},{value:'avoid',label:'Avoid one duty soon after another'},{value:'rest_after_series',label:'Rest after a run of these duties'}],'forward');
    const order=[0,1,2,3].map(i=>select(['Earliest duty','Next duty','Next duty','Latest duty'][i],types,'',i<2,'Not used'));
    const from=multi('After these duties',types),to=multi('Avoid these duties',types);
    const within=field('Within this many days','number',1,{required:true,min:1,max:3,step:1});
    const series=field('Run of at least (days)','number',2,{required:true,min:1,max:7,step:1}),rest=field('Then days off','number',2,{required:true,min:1,max:3,step:1});
    const orderBox=el('div',{class:'span-2 form-grid'},...order.map(o=>o.wrap)),avoidBox=el('div',{class:'span-2 form-grid'},from.wrap,to.wrap,within.wrap),seriesBox=el('div',{class:'span-2 form-grid'},series.wrap,rest.wrap);
    const explain=help('');
    const sync=()=>{const m=mode.input.value;orderBox.hidden=m!=='forward';avoidBox.hidden=m==='forward';to.wrap.hidden=within.wrap.hidden=m!=='avoid';seriesBox.hidden=m!=='rest_after_series';
      for(const o of order.slice(0,2))o.input.required=m==='forward';within.input.disabled=m!=='avoid';series.input.disabled=rest.input.disabled=m!=='rest_after_series';
      explain.textContent=m==='forward'?'Order the duties from earliest to latest (e.g. morning, evening, night). Moving later from one day to the next is fine; moving earlier is avoided.'
        :m==='avoid'?'Example: after an evening duty, avoid a morning duty within 2 days.':'Example: after 3 or more evenings in a row, keep the next 2 days off.';};
    mode.input.addEventListener('change',sync);mode.wrap.classList.add('span-2');config.append(mode.wrap,orderBox,avoidBox,seriesBox,explain);sync();
    return ()=>{const m=mode.input.value;return {kind,mode:m,type_order:m==='forward'?order.map(o=>o.input.value).filter(Boolean):[],
      from_type_ids:m==='forward'?[]:from.values(),to_type_ids:m==='avoid'?to.values():[],within_days:num(within),series_length:num(series),rest_days:num(rest)};};
  }
  const mode=select('Preference',[{value:'apart',label:'Keep apart: not on the same day'},{value:'together',label:'Keep together: on the same days'}],'apart');
  const level=select('Counts as together when',[{value:'day',label:'On duty the same day'},{value:'session',label:'In the same session'}],'day');
  const people=multi('People (2 to 10)',peopleChoices(ctx));people.wrap.classList.add('span-2');
  config.append(mode.wrap,level.wrap,people.wrap,help('Set by a coordinator and activated by an approver. Only planners see this rule; the people named are never told about each other.'));
  return ()=>({kind,mode:mode.input.value,level:level.input.value,person_ids:people.values()});
}
/** Step one of Propose rule: rule kinds as cards. The hidden input keeps the form's existing value/change contract. */
function ruleTypeCards(kinds:string[]){
  const input=el('input',{type:'hidden',value:''}) as HTMLInputElement;
  const wrap=el('fieldset',{class:'multi-field span-2 rule-cards',role:'radiogroup','aria-label':'Supported rule'},el('legend',{},'Supported rule'),'');
  let first='';
  for(const {name:group,kinds:list} of RULE_TOPICS){
    const cards=RULE_KINDS.filter(k=>list.includes(k[0])&&kinds.includes(k[0]));
    if(!cards.length)continue;
    wrap.append(el('div',{class:'rule-card-group'},el('span',{class:'rule-card-label'},group),el('div',{class:'rule-card-grid'},...cards.map(([value,name,desc,eg])=>{
      if(!first)first=value;
      const radio=el('input',{type:'radio',name:'rule-kind',value,'aria-label':name,required:true}) as HTMLInputElement;
      radio.addEventListener('change',()=>{if(radio.checked){input.value=value;input.dispatchEvent(new Event('change',{bubbles:true}));}});
      return el('label',{class:'rule-card'},radio,el('span',{class:'rule-card-text'},el('strong',{},name),el('span',{},desc),el('small',{},'e.g. '+eg)));
    }))));
  }
  const choose=(value:string)=>{const r=wrap.querySelector<HTMLInputElement>(`input[value="${value}"]`);if(r){r.checked=true;input.value=value;}};
  choose(kinds.includes('restriction')?'restriction':first);
  wrap.append(input);
  return {wrap,input,choose};
}
/** Rule scope the planner already knows (person, duty, clinic, place, weekday) is filled in; the approval route is unchanged. */
/** Everything a rule's conditions can filter on, with this department's records as the choices. */
/** Who may lead a duty type: the person fields of the rule conditions. */
// Without tags: who may lead is checked on each session's frozen duty type, which rules' tag resolution does not reach.
export const leadConditionFields=(ctx:Context)=>ruleConditionFields(ctx,false).filter(f=>!SESSION_FIELDS.includes(f.key));
export const ruleConditionFields=(ctx:Context,tags=true):ConditionField[]=>RULE_FIELDS.map(f=>({key:f.key,label:f.label,creatable:f.value_kind==='tags',group:SESSION_FIELDS.includes(f.key)?'What · duties':'Who · people',
  // Tags that label the field's kind are offered with its records (a tags line offers tags labelling people).
  choices:[...(f.kind==='people'?peopleChoices(ctx):f.kind?options(all(ctx,f.kind)):[]),...(tags?tagChoices(ctx,f.key):[])],
  // A date set line may narrow to some of its named entries (Eid al-Fitr, National Day).
  ...(f.kind==='date_sets'?{entries:(ids:string[])=>entryChoices(all(ctx,'date_sets').filter(r=>ids.includes(r.id)))}:{}),
  // The Tag field with no tags yet says so, with the way to make some (Configuration › Tags).
  ...(f.key==='tags'?{empty:{text:'No tags yet',action:ctx.permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR)?{label:'Create tags',run:()=>ctx.navigate('configuration/tags')}:undefined}}:{})}));
export type RuleScope={cohort_id?:string;workspace_id?:string;starts_on?:string;ends_on?:string;person_id?:string;assignment_type_id?:string;service_ids?:string[];location_id?:string;weekdays?:string[];title?:string;kind?:string;merged_ids?:string[];
  /** Prefill: the rule's own settings (e.g. a preference converted to a goal) and its conditions. */rule?:Record<string,any>;conditions?:Conditions|null};
/** Add rule: type first, then its settings (a quota's Limits), who and when, source; an approver activates it. */
export async function policyForm(ctx:Context,rotationTypeId='',scope:RuleScope={}){
  // A new rule starts with the earliest rota still being planned that has already begun (so the rota being planned is
  // covered from its first day), else today. A rota-scoped rule passes its own dates. The query asks only for rotas
  // running across today (ends after it, starts before it), so newer rotas cannot push the one being planned off the page.
  const day=today(ctx);
  const [catalogue,planning]=await Promise.all([request<{rule_kinds:string[]}>('/api/policies/capabilities'),
    scope.starts_on?null:get(`/api/workspaces?limit=100&current_only=true&start=${day}&end=${day}`).catch(()=>null)]);
  const from=(planning?.workspaces||[] as Row[]).filter((w:Row)=>['draft','submitted','approved'].includes(w.status)&&w.starts_on<day&&w.ends_on>day)
    .sort((a:Row,b:Row)=>a.starts_on<b.starts_on?-1:1)[0];
  formDialog('Add rule','Proposed first: an approver who did not propose it activates it.',form=>{
    const name=field('Rule title','text',scope.title||'',{required:true}),owner=field('Policy owner','text',String((ctx.me.department as Row|undefined)?.name||''),{required:true});
    const source=area('Source or departmental decision','');source.input.required=true;source.input.placeholder='e.g. Department meeting, 12 Sept 2026 — agreed by the chair';source.input.minLength=3;source.input.maxLength=500;
    const start=field('Effective from','date',scope.starts_on||from?.starts_on||day,{required:true}),end=field('Effective until (included)','date',scope.ends_on||'');lastDayIncluded(end.input);
    const startsWith=from&&!scope.starts_on?el('p',{class:'hint span-2'},`From the first day of ${from.name||'the rota being planned'}.`):'';
    if(startsWith)start.input.addEventListener('change',()=>{startsWith.hidden=start.input.value!==from!.starts_on;});
    const severity=select('Classification',[{value:'mandatory',label:'Mandatory constraint'},{value:'advisory',label:'Advisory preference'}],'mandatory');
    // How much an advisory rule weighs against other advisory rules (Configuration › Auto-schedule & scoring sets the multipliers).
    const strength=select('Advisory strength',STRENGTHS,'medium');
    const syncStrength=()=>{strength.wrap.hidden=severity.input.value!=='advisory';};severity.input.addEventListener('change',syncStrength);
    const allowed=check('Exceptions allowed with approval',false,'A person may get a dated exception, approved separately.');
    // A fixed classification or a safety rule's refusal of exceptions remains part of the review.
    for(const input of [severity.input,allowed.input])input.dataset.reviewDisabled='true';
    const scopeDays=multi('Weekdays',weekdayOptions(),scope.weekdays||['0','1','2','3','4','5','6']);
    const scopeStart=field('Starts at','text','00:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]'}),scopeEnd=field('Ends at','text','24:00',{required:true,pattern:'(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)'});
    // Who and which sessions: the same condition builder as announcements. What the planner already knows is filled in.
    const fields=ruleConditionFields(ctx),personFields=fields.filter(f=>!SESSION_FIELDS.includes(f.key));
    const given=scopeAsConditions({person_id:scope.person_id,assignment_type_id:scope.assignment_type_id,service_ids:scope.service_ids,location_id:scope.location_id,rotation_type_id:rotationTypeId||null});
    let conditions=conditionBuilder(fields,scope.conditions!==undefined?scope.conditions:scope.cohort_id?{match:'all',conditions:[...(given?.conditions||[]),{field:'cohort_ids',op:'any',values:[scope.cohort_id]}]}:given,{context:'rule'});
    const who=el('div',{class:'span-2 audience-conditions scope-who-what'},el('h4',{},'Who & what'),conditions.root);
    // A balance goal's Scope is people only (what it counts is chosen on the goal): offer only the person fields.
    let peopleOnly=false;
    const scopeFields=(only:boolean)=>{if(only===peopleOnly)return;peopleOnly=only;const kept=conditions.read();
      conditions=conditionBuilder(only?personFields:fields,only?withoutFields(kept,SESSION_FIELDS):kept,{context:'rule'});who.replaceChildren(el('h4',{},only?'Who':'Who & what'),conditions.root);};
    const when=el('div',{class:'span-2 scope-when'},el('h4',{},'When'),scopeDays.wrap,el('div',{class:'scope-times'},scopeStart.wrap,scopeEnd.wrap));
    const matching=el('span',{class:'muted','data-people-now':''});
    const type=ruleTypeCards(catalogue.rule_kinds);
    // A rule type chosen before opening (quick-start cards) is picked once the form is wired up. No change event:
    // a choice made for the person is not an unsaved edit, so Escape still closes the untouched form.
    if(scope.kind)queueMicrotask(()=>{const r=type.wrap.querySelector<HTMLInputElement>(`input[value="${scope.kind}"]`);if(r){type.choose(scope.kind!);render();sayScope();setTimeout(()=>{if(config.offsetParent)config.scrollIntoView({block:'start'});},60);}});
    const config=el('div',{class:'span-2 form-grid'});let readRule=()=>({} as Record<string,unknown>),readPeople=():PersonAmount[]=>[],readListed=()=>false;
    function render(){queueMicrotask(syncStrength);config.replaceChildren();const value=type.input.value;severity.input.disabled=false;allowed.input.disabled=false;readPeople=()=>[];readListed=()=>false;
      config.dataset.step=value==='block_quota'?'Limits':'Settings';
      if(RULE_HELP[value])config.append(el('p',{class:'hint span-2 rule-help'},RULE_HELP[value]));
      when.hidden=value==='balance_goal';  // a goal counts whole days; what it counts is chosen in its sentence
      scopeFields(value==='balance_goal');
      if(value==='allocation_preference'||value==='allocation_overtime'){
        severity.input.value='advisory';severity.input.disabled=true;allowed.input.checked=false;allowed.input.disabled=true;
        const caps=multi('Required overtime limits',options(all(ctx,'policies').filter(p=>p.definition&&(p.definition.rule.kind==='block_quota'&&['maximum','range'].includes(p.definition.rule.comparison)||['forbidden_transition','max_consecutive_weekends'].includes(p.definition.rule.kind))&&p.definition.severity==='mandatory')));
        const priority=field('Allocation priority','number',0,{min:-100,max:100,step:1,required:true});
        const overtimePriority=field('Overtime priority','number',0,{min:-100,max:100,step:1,required:true});
        const sessionPriority=field('Session selection priority','number',0,{min:-100,max:100,step:1,required:true});
        const relationship=select('Supervisor relationship',[{value:'any',label:'Any qualified supervisor'},{value:'mentor',label:'Assigned mentor during the duty'}],'any');
        const supervisors=multi('Supervisor scope (optional)',peopleChoices(ctx));
        // Balancing duties or supervisor exposure is a Balance goal now (history-aware); the clinic mode mix stays here.
        const balance=select('Balance assignments',[{value:'none',label:'No balancing preference'},{value:'service_mode',label:'Physical / virtual duty mix'}],'none');
        const toGoal=el('p',{class:'hint span-2'},'To share duties, weekends or supervisor time fairly, use a ',button('Balance goal',()=>{type.choose('balance_goal');render();},'ghost small'),'.');
        const balanceWeight=field('Balancing weight','number',1,{min:0.01,max:100,step:'any',required:true});
        const balanceWindow=select('Balancing period',[{value:'week',label:'Configured calendar week'},{value:'rolling',label:'Rolling local-day window'},{value:'rota',label:'Current planning period'}],'week');
        const balanceDays=field('Balancing window days','number',28,{min:1,max:31,step:1,required:true});
        const targetMode=select('Target clinic mode',[{value:'virtual',label:'Virtual'},{value:'physical',label:'Physical'}],'virtual');
        const targetShare=field('Target share (%)','number',50,{min:0,max:100,step:'any',required:true});
        const modeFields=el('div',{class:'form-grid'},targetMode.wrap,targetShare.wrap,note('Counts duties, not hours. A shared physical/virtual duty counts once, half toward each mode. Duties with unspecified mode are excluded from this preference.'));
        const balanceFields=el('div',{class:'form-grid'},balanceWeight.wrap,balanceWindow.wrap,balanceDays.wrap);
        const updateBalance=()=>{modeFields.hidden=balance.input.value!=='service_mode';targetMode.input.disabled=targetShare.input.disabled=balance.input.value!=='service_mode';balanceFields.hidden=balance.input.value==='none';balanceWeight.input.disabled=balanceWindow.input.disabled=balance.input.value==='none';balanceDays.input.disabled=balance.input.value==='none'||balanceWindow.input.value!=='rolling';balanceDays.wrap.hidden=balanceWindow.input.value!=='rolling';};
        balance.input.addEventListener('change',updateBalance);balanceWindow.input.addEventListener('change',updateBalance);updateBalance();
        for(const w of [supervisors.wrap,balance.wrap,balanceDays.wrap,caps.wrap])w.classList.add('span-2');
        if(value==='allocation_preference')config.append(priority.wrap,sessionPriority.wrap,overtimePriority.wrap,relationship.wrap,supervisors.wrap,balance.wrap,balanceFields,modeFields,toGoal,note('Allocation priority ranks people within a session. Overtime priority ranks overtime candidates first; lower values mean last resort. Session priority considers matching sessions earlier; the highest combined employee score sets their order. Existing duties stay fixed and safety checks still apply.'));
        else config.append(caps.wrap,note('Selected caps must all be active before this permit can be used.'),note('Allows overtime suggestions only after regular candidates cannot improve the draft. The scope must cover the whole duty. Set overtime limits with a fixed-block quota counting only planned overtime; this setting never waives rest or clinical rules.'));
        readRule=()=>value==='allocation_preference'?{kind:value,priority:Number(priority.input.value),session_priority:Number(sessionPriority.input.value),overtime_priority:Number(overtimePriority.input.value),supervisor_relationship:relationship.input.value,supervisor_ids:supervisors.values(),balance_by:balance.input.value,balance_weight:Number(balanceWeight.input.value),balance_window:balanceWindow.input.value,balance_window_days:Number(balanceDays.input.value),balance_target_mode:targetMode.input.value,balance_target_share:Number(targetShare.input.value)/100}:{kind:value,required_policy_ids:caps.values()};
      }else if(value==='balance_goal'){
        severity.input.value='advisory';severity.input.disabled=true;allowed.input.checked=false;allowed.input.disabled=true;
        const partnerFields=personFields;
        const goal=goalBuilder(scope.rule||{},{types:options(all(ctx,'assignment_types')),services:options(all(ctx,'services')),partners:partnerFields},strength.input,
          rows=>perPersonEditor(ctx,value,rows,{label:'Target',dates:false,hint:'Optional. A listed person aims for their own amount over the period; everyone else in Scope aims for the amount above.'}));
        config.append(goal.root,goalPreview(()=>readPolicy().definition,()=>name.input.value,scope.workspace_id));
        readRule=goal.read;
      }else if(PATTERN_KINDS.includes(value)){
        severity.input.value='advisory';severity.input.disabled=true;allowed.input.checked=false;allowed.input.disabled=true;
        readRule=patternFields(ctx,value,config);
      }else if(value==='restriction'){
        const days=multi('Restricted weekdays',weekdayOptions(),['0','1','2','3','4','5','6']);
        const a=field('Restricted from · local wall time','text','00:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]'}),b=field('Restricted until · local wall time','text','24:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00'});
        const nearLeave=check('Only near approved or recorded leave',false),buffer=field('Days before or after leave','number',3,{required:true,min:1,max:31,step:1});buffer.wrap.hidden=true;buffer.input.disabled=true;buffer.wrap.classList.add('span-2');
        nearLeave.input.addEventListener('change',()=>{buffer.wrap.hidden=!nearLeave.input.checked;buffer.input.disabled=!nearLeave.input.checked;});
        config.append(days.wrap,el('div',{class:'form-grid'},a.wrap,b.wrap),nearLeave.wrap,buffer.wrap);readRule=()=>({kind:value,weekdays:days.values().map(Number),start_time:a.input.value,end_time:b.input.value,leave_buffer_days:nearLeave.input.checked?Number(buffer.input.value):null});
      }else if(value==='minimum_cover'){
        allowed.input.checked=false;allowed.input.disabled=true;
        const minimum=field('Minimum matching people','number',1,{required:true,min:1,max:100});const coverNote=note('Requires this many eligible, available people matching the selected person/rank/programme in each selected session interval.');
        const aggregation=select('Count coverage across',[{value:'assignment',label:'Each assignment separately'},{value:'service_group',label:'Selected clinics together'}],'assignment');
        const triggers=multi('Only when these employees are assigned',peopleChoices(ctx));
        triggers.wrap.classList.add('span-2');config.append(minimum.wrap,aggregation.wrap,coverNote,triggers.wrap,note('Leave empty to always require cover. If selected, any of these employees triggers the requirement; selected employees do not count toward the required other people.'),note('For a duty’s minimum, advisory target and maximum together, configure its Staffing intervals in Setup → Duty types. Minimum cover counts eligible and available people; a capacity limit counts scheduled participants.'),button('Choose staffing capacity instead',()=>{type.choose('maximum_participants');render();},'secondary small'));
        readRule=()=>({kind:value,minimum:Number(minimum.input.value),aggregation:aggregation.input.value,when_person_ids:triggers.values()});
      }else if(value==='supervision_limit'){
        severity.input.value='mandatory';severity.input.disabled=true;allowed.input.checked=false;allowed.input.disabled=true;
        const mode=select('Supervision limit',[{value:'capacity',label:'Maximum trainees per supervisor'},{value:'allowed_ranks',label:'Allowed trainee ranks only'}],'capacity');
        const ranks=multi('Trainee ranks',rankOptions(ctx));
        const maximum=field('Maximum trainees per supervisor','number',2,{required:true,min:0,max:100,step:1});
        const extra=field('Permitted extra places per supervisor','number',0,{required:true,min:0,max:100,step:1});
        const extraRanks=multi('Ranks permitted in extra places',rankOptions(ctx));
        const help=note('For capacity, leave trainee ranks empty to count all trainees. Allowed ranks requires a selection.');
        const syncCapacity=()=>{const capacity=mode.input.value==='capacity';maximum.wrap.hidden=!capacity;maximum.input.disabled=!capacity;extra.wrap.hidden=!capacity;extra.input.disabled=!capacity;extraRanks.wrap.hidden=!capacity||Number(extra.input.value)===0;};
        mode.input.addEventListener('change',syncCapacity);extra.input.addEventListener('input',syncCapacity);syncCapacity();
        for(const w of [mode.wrap,ranks.wrap,extraRanks.wrap])w.classList.add('span-2');
        config.append(mode.wrap,ranks.wrap,maximum.wrap,extra.wrap,extraRanks.wrap,help,note('The scope below selects supervisors. Named pairing and verified clinical authority are still required. Normal places are attempted first; extra places remain visible for review.'));
        readRule=()=>({kind:value,mode:mode.input.value,trainee_category_ids:ranks.values(),maximum:mode.input.value==='capacity'?Number(maximum.input.value):null,overflow_maximum:mode.input.value==='capacity'?Number(extra.input.value):0,overflow_category_ids:mode.input.value==='capacity'&&Number(extra.input.value)>0?extraRanks.values():[]});
      }else if(value==='maximum_participants'){
        const maximum=field('Normal maximum matching people','number',3,{required:true,min:0,max:100,step:1});
        const aggregation=select('Apply capacity per',[{value:'assignment',label:'Assignment'},{value:'service',label:'Clinic across concurrent assignments'}],'assignment');
        const extra=field('Permitted extra places','number',0,{min:0,max:100,step:1,required:true});
        const ranks=multi('Ranks permitted in extra places',rankOptions(ctx));
        const syncOverflow=()=>{const enabled=Number(extra.input.value)>0;ranks.wrap.hidden=!enabled;severity.input.disabled=enabled;if(enabled)severity.input.value='mandatory';};
        extra.input.addEventListener('input',syncOverflow);syncOverflow();
        ranks.wrap.classList.add('span-2');config.append(maximum.wrap,aggregation.wrap,extra.wrap,ranks.wrap,note('Allocation tries normal places first. Extra places apply only to selected ranks and remain visible for review.'),note('Caps simultaneous scheduled participants matching the selected scope, even if they are unavailable or ineligible. This is separate from supervisor-to-trainee ratios.'),note('For a duty’s minimum, advisory target and maximum together, configure its Staffing intervals in Setup → Duty types.'),button('Choose eligible minimum cover instead',()=>{type.choose('minimum_cover');render();},'secondary small'));
        allowed.input.checked=false;allowed.input.disabled=true;readRule=()=>({kind:value,maximum:Number(maximum.input.value),aggregation:aggregation.input.value,overflow_maximum:Number(extra.input.value),overflow_category_ids:Number(extra.input.value)>0?ranks.values():[]});
      }else if(value==='max_hours'||value==='max_duties'){
        const amount=field(value==='max_hours'?'Maximum hours':'Maximum duties','number',value==='max_hours'?40:5,{required:true,min:1,max:value==='max_hours'?744:500,step:value==='max_hours'?'any':1});
        const window=select('Counting window',[{value:'day',label:'Local calendar day'},{value:'week',label:'Configured calendar week'},{value:'rolling',label:'Rolling elapsed-day window'}],'week');
        const grouping=select('Count duties',[{value:'all',label:'All matching duties'},{value:'supervisor',label:'Separately for each named supervisor'},{value:'weekday',label:'Separately for each local weekday'}],'all');
        const days=field('Rolling window days','number',7,{min:1,max:31,disabled:true});const overtime=check('Include planned overtime',true);if(value==='max_hours')days.wrap.classList.add('span-2');
        window.input.addEventListener('change',()=>{days.input.disabled=window.input.value!=='rolling';});config.append(el('div',{class:'form-grid'},amount.wrap,window.wrap,days.wrap,...(value==='max_duties'?[grouping.wrap]:[]),overtime.wrap));
        readRule=()=>({kind:value,[value==='max_hours'?'hours':'maximum']:Number(amount.input.value),window:window.input.value,include_overtime:overtime.input.checked,window_days:window.input.value==='rolling'?Number(days.input.value):7,...(value==='max_duties'?{group_by:grouping.input.value}:{})});
      }else if(value==='block_quota'){
        const given=scope.rule?.kind==='block_quota'?scope.rule:{};
        const metric=select('Counts',[{value:'duties',label:'Duties — counted once at participant start'},{value:'session_units',label:'Session units per duty'},{value:'scheduled_hours',label:'Planned presence hours — gaps excluded'},{value:'reserved_hours',label:'Reserved hours — full envelope'},{value:'workload_credit',label:'Configured workload credits'},{value:'training_credit',label:'Configured planned educational credits'},{value:'lead_turns',label:'Team lead turns — duties where the person leads'}],given.metric||'duties');
        const overtime=select('Overtime',[{value:'include',label:'Counted with ordinary duties'},{value:'exclude',label:'Not counted'},{value:'only',label:'Only overtime is counted'}],given.overtime||'include');
        const types=multi('Duty types counted',options(all(ctx,'assignment_types')),given.assignment_type_ids||[],'Every duty type');
        const participation=multi('Roles counted',[{value:'staff',label:'Staff'},{value:'trainee',label:'Trainee'},{value:'supervisor',label:'Supervisor'}],given.participation||[],'Every role');
        const limits=quotaLimits(given);
        const blockStart=field('From','date',given.starts_on||start.input.value,{required:true}),blockEnd=field('Until (included)','date',given.ends_on||'',{required:true});lastDayIncluded(blockEnd.input);
        // One rule covers every week or month of the dates: no per-week copies.
        const repeat=select('Repeat',[{value:'once',label:'Once — the whole period'},{value:'calendar_week',label:'Each week'},{value:'calendar_month',label:'Each month'},{value:'fixed',label:'Every block of N days'},{value:'rolling',label:'Every rolling window of N days'}],
          given.window_days?given.window_alignment:!given.window_alignment||given.window_alignment==='rolling'?'once':given.window_alignment);
        repeat.input.dataset.reviewDisabled='true';  // Prorating fixes the saved period to Once.
        const inBlocks=()=>repeat.input.value==='fixed'||repeat.input.value==='rolling';
        const windowDays=field('Days in each block','number',given.window_days||28,{min:1,max:366,required:true});
        const prorate=check('Scale the target to each person’s days in the department',!!given.prorate_department_days);
        const referenceDays=field('Target is for this many days','number',given.reference_days||28,{required:true,min:1,max:366});
        const rounding=select('Round to whole duties',[{value:'nearest',label:'Nearest'},{value:'down',label:'Down'},{value:'up',label:'Up'}],given.rounding||'nearest');
        let wasSeverity=severity.input.value;
        const syncQuota=()=>{
          // Only a target (no minimum or maximum) is advisory, and only it may scale to departmental days.
          const onlyTarget=limits.onlyTarget();prorate.wrap.hidden=!onlyTarget;if(!onlyTarget)prorate.input.checked=false;
          if(prorate.input.checked)repeat.input.value='once';repeat.input.disabled=prorate.input.checked;
          referenceDays.wrap.hidden=rounding.wrap.hidden=!prorate.input.checked;referenceDays.input.disabled=!prorate.input.checked;
          windowDays.wrap.hidden=!inBlocks();windowDays.input.disabled=!inBlocks();
          // A target alone is advisory; once a limit is set again the classification goes back to what it was.
          if(onlyTarget&&!severity.input.disabled){wasSeverity=severity.input.value;severity.input.value='advisory';}else if(!onlyTarget&&severity.input.disabled)severity.input.value=wasSeverity;severity.input.disabled=onlyTarget;
          allowed.input.disabled=onlyTarget||repeat.input.value!=='once';if(allowed.input.disabled)allowed.input.checked=false;
          limits.step(['duties','lead_turns'].includes(metric.input.value)?'1':'any');};
        for(const c of [limits.wrap,repeat.input,prorate.input,metric.input]){c.addEventListener('input',syncQuota);c.addEventListener('change',syncQuota);}
        syncQuota();
        for(const w of [types.wrap,participation.wrap])w.classList.add('span-2');
        config.append(limits.wrap,metric.wrap,overtime.wrap,types.wrap,participation.wrap,
          el('fieldset',{class:'span-2 form-grid quota-period'},el('legend',{},'Period'),dateRange('Dates',blockStart,blockEnd),repeat.wrap,windowDays.wrap,prorate.wrap,referenceDays.wrap,rounding.wrap,
            el('p',{class:'hint span-2'},'Weeks start on the department’s first day of the week. Duties count on the day they start.')));
        readRule=()=>({kind:value,...limits.read(),prorate_department_days:prorate.input.checked,reference_days:Number(referenceDays.input.value),rounding:rounding.input.value,
          window_days:inBlocks()?Number(windowDays.input.value):null,window_alignment:repeat.input.value==='once'?'rolling':repeat.input.value,
          starts_on:blockStart.input.value,ends_on:blockEnd.input.value,metric:metric.input.value,overtime:overtime.input.value,assignment_type_ids:types.values(),participation:participation.values()});
      }else if(value==='max_consecutive_weekends'){
        const maximum=field('Maximum consecutive weekends','number',1,{required:true,min:1,max:4});
        const first=select('Weekend starts',weekdayOptions(),'4');
        const length=field('Weekend length in days','number',2,{required:true,min:1,max:3});
        const overtime=check('Only when the weekend run includes overtime',false);
        maximum.wrap.classList.add('span-2');config.append(maximum.wrap,first.wrap,length.wrap,overtime.wrap,note('Counts each calendar weekend once, including overnight work. A weekend without matching work breaks the run.'));
        readRule=()=>({kind:value,maximum:Number(maximum.input.value),weekend_start:Number(first.input.value),weekend_days:Number(length.input.value),overtime_only:overtime.input.checked});
      }else if(value==='forbidden_transition'){
        const from=multi('First-day duty types',options(all(ctx,'assignment_types'))),to=multi('Prohibited next-day duty types',options(all(ctx,'assignment_types')));
        const overtime=check('Only when either duty is planned overtime',false);
        config.append(from.wrap,to.wrap,overtime.wrap,note('Uses duty start dates in the department timezone. Applies even when minimum rest hours are met.'));
        readRule=()=>({kind:value,from_type_ids:from.values(),to_type_ids:to.values(),overtime_only:overtime.input.checked});
      }else if(value==='travel_buffer'){
        const from=select('Travel from location',options(all(ctx,'locations'))),to=select('Travel to location',options(all(ctx,'locations'))),minutes=field('Required transition minutes','number',30,{min:1,max:1440,required:true});
        minutes.wrap.classList.add('span-2');config.append(el('div',{class:'form-grid'},from.wrap,to.wrap,minutes.wrap));readRule=()=>({kind:value,from_location_id:from.input.value,to_location_id:to.input.value,minutes:Number(minutes.input.value)});
      }else{
        const amount=field(value==='min_rest'?'Minimum rest hours':'Maximum consecutive duty days','number',value==='min_rest'?11:6,{min:1,max:value==='min_rest'?168:31,required:true,step:value==='min_rest'?'any':1});config.append(amount.wrap);readRule=()=>({kind:value,[value==='min_rest'?'hours':'maximum']:Number(amount.input.value)});
      }
      if(safetyRule({kind:value})){allowed.input.checked=false;allowed.input.disabled=true;}
      // One rule with lines (a person, a programme, clinics, days…) instead of a copy of the rule for each.
      if(LINE_KINDS.includes(value)){const lines=perPersonEditor(ctx,value,[],{rule:()=>readRule()});config.append(lines.wrap);readPeople=lines.read;readListed=lines.listed;
        // The rule's own settings decide the lines' number column (a range, a supervision mode): follow their changes.
        config.addEventListener('change',e=>{if(!lines.wrap.contains(e.target as Node))lines.redraw();});}
    }
    type.input.addEventListener('change',render);render();
    const summary=el('p',{class:'scope-summary span-2','aria-live':'polite'});
    const sayScope=()=>{
      const label=(field:string,value:string)=>fields.find(f=>f.key===field)?.choices.find(c=>c.value===value)?.label||value;
      lastWho=conditionsText(conditions.read(),label);const who=lastWho||'everyone in the department, every duty';
      const days=scopeDays.labels(),dayText=days.length===7?'every day':days.length?days.map(d=>d.slice(0,3)).join(', '):'no days';
      const time=scopeStart.input.value==='00:00'&&scopeEnd.input.value==='24:00'?'all day':`${scopeStart.input.value}–${scopeEnd.input.value}`;
      // A goal has no days or hours of its own (it counts whole days): say only who.
      summary.replaceChildren('Applies to ',el('strong',{},peopleOnly?lastWho||'everyone in the department':who),...(peopleOnly?[]:[', ',el('strong',{},dayText),', ',el('strong',{},time)]),'. ',matching);
      suggestTitle();
    };
    // Suggest a title from what the rule is (e.g. "Minimum rest · 11 hours · Residents") until the person types their own.
    let lastWho='';const offerTitle=suggested(name.input);
    function suggestTitle(){
      const who=lastWho;
      const kind=RULE_KINDS.find(k=>k[0]===type.input.value)?.[1]||'';
      const num=config.querySelector<HTMLInputElement>('input[type=number]');
      const unit=(num?.labels?.[0]?.textContent||'').toLowerCase().match(/hours|days|minutes|duties|people|weekends/)?.[0]||'';
      offerTitle([kind,num?.value?`${num.value}${unit?' '+unit:''}`:'',who].filter(Boolean).join(' · ').slice(0,160));
    }
    form.addEventListener('input',e=>{if(e.target!==name.input)suggestTitle();});form.addEventListener('change',e=>{if(e.target!==name.input)suggestTitle();});
    // Controlled overrides: this rule replaces others for the people it matches (never a safety rule; the server checks the rest).
    const replaceable=all(ctx,'policies').filter(p=>p.definition&&p.status!=='retired'&&!safetyRule(p.definition.rule)&&!p.definition.workspace_id);
    const overrides=multi('Replaces these rules for the people it matches (optional)',options(replaceable),[],'Replaces no rule');
    overrides.wrap.classList.add('span-2');
    const overridesHelp=el('p',{class:'hint span-2'},'Only advisory rules (when this rule is advisory too), or a limit of the same kind for a narrower group, can be replaced. For the people this rule matches its own number applies instead, higher or lower, and their lines in the replaced rule’s per-person table no longer apply. Rest, restrictions, cover, capacity and supervision rules are never replaced.');
    const scopePart=part('Scope','Checked at each duty: who (rank, skills, programme, cohort then) and which sessions (clinic, location, duty type). No conditions: everyone, every duty.',summary,who,when,...(replaceable.length?[overrides.wrap,overridesHelp]:[])),scopeNote=el('span',{hidden:true});
    for(const box of [who,scopeDays.wrap,scopeStart.wrap,scopeEnd.wrap,type.input]){box.addEventListener('change',sayScope);box.addEventListener('input',sayScope);}sayScope();
    type.wrap.dataset.step='Rule type';scopePart.dataset.step='Who, what & when';name.wrap.dataset.step='Source & owner';
    // Rule types and quota settings switch the classification in code: resync after every change.
    syncStrength();form.addEventListener('change',()=>queueMicrotask(syncStrength));
    form.append(type.wrap,config,scopePart,scopeNote,name.wrap,owner.wrap,severity.wrap,strength.wrap,dateRange('Dates',start,end),startsWith,allowed.wrap,source.wrap);name.wrap.classList.add('span-2');
    const readPolicy=()=>({name:name.input.value,owner:owner.input.value,source:source.input.value,starts_on:start.input.value,ends_on:end.input.value||null,active:true,definition:{schema_version:1,scope:when.hidden?{conditions:conditions.read()}:{weekdays:scopeDays.values().map(Number),start_time:scopeStart.input.value,end_time:scopeEnd.input.value,conditions:conditions.read()},severity:severity.input.value,...(severity.input.value==='advisory'?{strength:strength.input.value}:{}),exception_allowed:allowed.input.checked,rule:readRule(),per_person:readPeople(),listed_only:readListed(),overrides:overrides.values(),...(scope.workspace_id?{workspace_id:scope.workspace_id}:{}),...(scope.merged_ids?.length?{merged_ids:scope.merged_ids}:{})}});
    const compatibility=el('div',{class:'span-2','aria-live':'polite','data-step':'Review'});
    form.append(compatibility);
    let checkVersion=0,checkTimer:number|undefined;
    const checkDefinition=async()=>{
      // This check replaces a pending debounced one: that one would start later and discard this check's answer.
      window.clearTimeout(checkTimer);
      const version=++checkVersion,definition=readPolicy().definition;
      compatibility.replaceChildren(working('Checking rule…'));
      try{
        const result=await request<{compatible:boolean;issues:Array<{message:string}>;people_now?:number}>('/api/policies/check',{method:'POST',body:JSON.stringify({definition})});
        if(version!==checkVersion||!form.isConnected)return false;
        // Live preview from the same evaluator the checks use.
        matching.textContent=result.people_now===undefined?'':`Matches ${result.people_now} ${result.people_now===1?'person':'people'} now.`;
        compatibility.replaceChildren(...(result.compatible?[el('p',{class:'muted'},'Rule settings supported. Check a rota before activation.')]:result.issues.map(issue=>note(plainWords(issue.message),'error'))));
        return result.compatible;
      }catch(error){
        if(version===checkVersion&&form.isConnected)compatibility.replaceChildren(note((error as Error).message,'error',error));
        return false;
      }
    };
    const scheduleCheck=()=>{++checkVersion;window.clearTimeout(checkTimer);compatibility.replaceChildren();checkTimer=window.setTimeout(()=>{if(form.isConnected)void checkDefinition();},450);};
    form.addEventListener('input',scheduleCheck);form.addEventListener('change',scheduleCheck);
    const checkRotas=button('Check against rotas',async()=>{
      if(!form.reportValidity()||!await checkDefinition())return;
      await ruleReview(readPolicy(),true,false,ctx.refresh,(kind,id)=>labelOf(lookup(ctx,kind,id)||{name:id}),ctx.me.organisation.timezone,true);
    });
    form.append(checkRotas);
    return async()=>{
      window.clearTimeout(checkTimer);
      const item=readPolicy();
      if(!await checkDefinition())throw new Error('Resolve the rule check before saving.');
      if(JSON.stringify(item)!==JSON.stringify(readPolicy()))throw new Error('The rule changed during its check. Review it and save again.');
      await command('policy.create',item);notify('Rule proposed. It applies once an approver activates it.');await ctx.refresh();
    };
  },'Propose rule');
}

export function exceptionForm(ctx:Context,prefill:{person_id?:string;policy_id?:string;day?:string;items?:{policy_id:string;person_id:string;day:string}[]}={},done?:()=>void){
  if(prefill.items)return exceptionsForm(ctx,prefill.items,done);
  // Nothing to choose: say why instead of showing a form that cannot be completed.
  if(!all(ctx,'policies').some(r=>r.status==='active'&&allowsException(r))){showDialog('Request a rule exception','No active rule allows exceptions yet. When proposing a rule, tick “Permit separately approved, expiring exceptions”; once it is approved, exceptions can be requested here.',()=>undefined);return;}
  formDialog('Request a rule exception','Choose a rule that allows exceptions. Overlap, clinical eligibility and basic staffing requirements still apply.',form=>{
    const policy=select('Exception-permitting rule',options(all(ctx,'policies').filter(r=>r.status==='active'&&allowsException(r))),prefill.policy_id||'');
    const person=select('Person',peopleChoices(ctx),prefill.person_id||'');
    const next=prefill.day?new Date(new Date(prefill.day+'T00:00:00Z').getTime()+86400000).toISOString().slice(0,10):'';
    const start=field('Exception starts','date',prefill.day||today(ctx),{required:true}),end=field('Exception ends (included)','date',next,{required:true});lastDayIncluded(end.input);const reason=area('Exception reason','');reason.input.required=true;reason.input.minLength=3;
    form.append(policy.wrap,person.wrap,dateRange('Dates',start,end),reason.wrap);
    return async()=>{await command('exception.create',{policy_id:policy.input.value,person_id:person.input.value,starts_on:start.input.value,ends_on:end.input.value,reason:reason.input.value});notify('Exception proposed. It has not relaxed the rule yet.');await ctx.refresh();};
  },'Request exception');
}

/** The same request for many issues at once (Schedule › Resolve issues): each rule, person and day as the single form
 *  would fill them in, one reason, one command (all or none). Safety rules never get exceptions. */
function exceptionsForm(ctx:Context,items:{policy_id:string;person_id:string;day:string}[],done?:()=>void){
  const next=(day:string,n=1)=>new Date(new Date(day+'T00:00:00Z').getTime()+n*86400000).toISOString().slice(0,10),prev=(day:string)=>next(day,-1);
  const rule=(id:string)=>lookup(ctx,'policies',id);
  // Rules that keep people safe never have exceptions (the server refuses them: issues.safety_rule).
  const safety=(id:string)=>safetyRule(rule(id)?.definition?.rule);
  // A quota exception covers its whole block; others the day of the issue.
  const span=(i:{policy_id:string;day:string})=>{const r=rule(i.policy_id)?.definition?.rule;return r?.kind==='block_quota'?{starts_on:r.starts_on,ends_on:r.ends_on}:{starts_on:i.day,ends_on:next(i.day)};};
  const unique=[...new Map(items.filter(i=>!safety(i.policy_id)).map(i=>({policy_id:i.policy_id,person_id:i.person_id,...span(i)})).map(i=>[JSON.stringify(i),i])).values()];
  const skipped=items.length-items.filter(i=>!safety(i.policy_id)).length;
  if(!unique.length){notify('These issues are under rules that keep people safe (rest, restrictions, supervision, cover): they cannot have exceptions.','warning');return;}
  formDialog(`Request ${unique.length===1?'an exception':unique.length+' exceptions'}`,'Each is approved separately and relaxes its rule for that person only on those days.',form=>{
    const reason=area('Exception reason','');reason.input.required=true;reason.input.minLength=3;
    form.append(table(['Rule','Person','Days'],unique.map(i=>[labelOf(rule(i.policy_id)),fullNameOf(lookup(ctx,'people',i.person_id)),dateUnits([formatDate(i.starts_on,'UTC'),...(i.ends_on===next(i.starts_on)?[]:['– '+formatDate(prev(i.ends_on),'UTC')])])])),
      skipped?el('p',{class:'muted span-2'},`${skipped} left out: rules that keep people safe (rest, restrictions, supervision, cover) cannot have exceptions.`):'',reason.wrap);
    return async()=>{await command('exception.create_many',{items:unique,reason:reason.input.value});done?.();notify(`${unique.length===1?'Exception':unique.length+' exceptions'} proposed. They do not relax the rules until approved.`);};
  },'Request exceptions');
}

/** Who a notice is for: everyone, or the people matching conditions (Notion-style: "Where rank is any of …",
 *  joined by and / or, with groups). Older notices saved in the flat form open as the same conditions. */
function audienceFields(ctx:Context,given?:Row){
  const a:Record<string,any>=given||{everyone:true};
  const who=select('For',[{value:'everyone',label:'Everyone in the department'},{value:'some',label:'People matching conditions'}],a.everyone?'everyone':'some');
  const fields:ConditionField[]=[
    {key:'category_ids',label:'Rank',choices:rankOptions(ctx)},
    {key:'roles',label:'Role',choices:ROLE_NAMES.map(r=>({value:r,label:roleLabel(r)}))},
    {key:'service_ids',label:'Clinic they can work',choices:[...options(all(ctx,'services')),...tagChoices(ctx,'service_ids')]},
    {key:'programme_ids',label:'Programme',choices:[...options(all(ctx,'programmes')),...tagChoices(ctx,'programme_ids')]},
    {key:'capability_ids',label:'Skill',choices:options(all(ctx,'capabilities'))},
    // Tags by record (a person's tags follow them); older notices may name a tag as text.
    {key:'tags',label:'Tag',choices:tagChoices(ctx,'tags'),creatable:true},
    {key:'person_ids',label:'Person',choices:[...peopleChoices(ctx),...tagChoices(ctx,'person_ids')]},
  ];
  // The flat form: roles or named people always count; the other groups together narrow it.
  const legacy=():Conditions|null=>{
    if(a.where)return a.where;
    const line=(field:string)=>a[field]?.length?[{field,op:'any',values:a[field]}]:[];
    const narrow=['category_ids','service_ids','programme_ids','capability_ids','tags'].flatMap(line);
    const any=[...line('roles'),...line('person_ids'),...(narrow.length>1?[{match:'all',conditions:narrow}]:narrow)] as Conditions['conditions'];
    return any.length?(any.length===1?{match:'all',conditions:any}:{match:'any',conditions:any}):null;
  };
  const conditions=conditionBuilder(fields,legacy());
  const body=el('div',{class:'span-2 audience-conditions'},conditions.root);
  const sync=()=>{body.hidden=who.input.value==='everyone';};who.input.addEventListener('change',sync);sync();
  return {root:el('fieldset',{class:'span-2 audience'},el('legend',{},'Who it is for'),who.wrap,body),
    read:()=>{
      if(who.input.value==='everyone')return {everyone:true};
      const where=conditions.read();if(!where)throw new Error('Add at least one condition with a value, or choose Everyone.');
      return {everyone:false,where};
    }};
}
