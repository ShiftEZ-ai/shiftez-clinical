/** Configuration › Scheduling groups reviewed: who schedules whom. A group is a people selector in
 *  the one condition builder (conditions.ts), with owners and co-editors who schedule its people in the shared rota, a
 *  priority (when someone matches two groups, the lowest number is their group) and dates. Each save appends a version
 *  with its reason; History restores any earlier version (a new version too) and every save has Undo. The Rota
 *  Coordinators (who change session definitions and schedule people in no group) and the private-draft limits are the
 *  department's planning settings, versioned the same way. Admins change them (`planning.configure`); planners read. */
import {el,field,area,button,note,empty,sectionHead,ruleFacts,formDialog,historyDialog,notify,withIcon,formatDate,plural,multiPick,grants,liftFooterStatus,type Choice,CAPABILITY,roleForCapability,disabledWhy,holders} from './dom.js';
import {dateRange,lastDayIncluded} from './date_picker.js';
import {get,command} from './api.js';
import {conditionBuilder,type ConditionField} from './conditions.js';
import {dataTable} from './table_view.js';
import {kindLabel,purposeOf,createLabel} from './config_kinds.js';
import {GENERATION_SCOPE_CAPABILITY,type Conditions,type PlanningSettingsDefinition} from './generated.js';
import type {Me} from './types.js';

export type GroupDefinition={name:string;owner_identity_ids:string[];co_editor_identity_ids:string[];membership:Conditions;primary_priority:number;
  starts_on:string;ends_on?:string|null;active?:boolean};
export type GroupVersion={id:string;group_id:string;revision:number;definition:GroupDefinition;actor_id:string;reason:string;created_at:string;
  /** P2-BE: the owners' names (one batched read), for planners who cannot read Access. */owner_names?:string[]};
/** The contract's planning settings as the server answers them (planning_groups.settings_definition: every field filled). */
export type PlanningSettings=Required<PlanningSettingsDefinition>;
type SettingsVersion={id:string;revision:number;definition:PlanningSettings;actor_id:string;reason:string;created_at:string};
/** GET /api/planning-settings (planning_groups.read_settings): the latest version, and the definition in force at the top. */
export type PlanningSettingsRead={settings:SettingsVersion|null;definition:PlanningSettings};
type Access={bindings:{identity_id:string;role:string;revoked?:boolean;starts_on:string;ends_on?:string|null}[];identities:{id:string;display_name?:string;disabled?:boolean}[]};

export interface GroupDeps{me:Me;zone:string;today:string;
  /** The rule fields (forms.ts ruleConditionFields): the builder offers those the server accepts for a population. */
  fields:ConditionField[];
  /** A membership in words (the rule list's own wording). */words:(c:Conditions|null|undefined)=>string;}

/** Group membership takes the fields a scheduling population takes (the contract's GENERATION_SCOPE_CAPABILITY). */
export const groupFields=(fields:ConditionField[])=>fields.filter(f=>(GENERATION_SCOPE_CAPABILITY.fields as readonly string[]).includes(f.key));
const lastIncluded=(end:string)=>new Date(Date.parse(end.slice(0,10)+'T12:00:00Z')-86400000).toISOString().slice(0,10);
export const groupStatus=(d:GroupDefinition,today:string)=>d.active===false?'Inactive':d.starts_on>today?'Upcoming':d.ends_on&&d.ends_on<=today?'Ended':'Active';

/** The one words for a department without Rota Coordinators (the group form here, a private draft's Merge in app.ts). */
export const NO_ROTA_COORDINATORS='No Rota Coordinators yet';
export async function schedulingGroupsPage(target:HTMLElement,deps:GroupDeps){
  const admin=grants(deps.me.roles,'planning.configure');
  const [{groups},settings,access]=await Promise.all([get<{groups:GroupVersion[]}>('/api/planning-groups?limit=100'),
    get<PlanningSettingsRead>('/api/planning-settings'),
    // Names and who may own a group (scheduling access). Read-only planners may not read Access: names fall back.
    admin?get<Access>('/api/access'):Promise.resolve<Access>({bindings:[],identities:[]})]);
  const names=new Map(access.identities.map(i=>[i.id,String(i.display_name||'')]));
  // Names come from Access (admins); planners read the owners' names the group list carries (owner_names).
  const who=(id:string)=>names.get(id)||(id===deps.me.identity_id?'You':'');
  const people=(ids:string[],suffix='')=>{const known=ids.map(who).filter(Boolean).map(n=>n+suffix),rest=ids.length-known.length;
    return [...known,...(rest?[plural(rest,known.length?'other':'planner')]:[])].join(', ');};
  const live=(b:Access['bindings'][number])=>!b.revoked&&b.starts_on<=deps.today&&(!b.ends_on||b.ends_on>deps.today);
  const schedulers=new Set(access.bindings.filter(b=>b.role===roleForCapability(CAPABILITY.COORDINATOR)&&live(b)).map(b=>b.identity_id));
  const editorChoices=():Choice[]=>access.identities.filter(i=>!i.disabled&&schedulers.has(i.id)).map(i=>({value:i.id,label:String(i.display_name)}));
  // SX-27: drawn again in place, the list's count goes back to the footer (the shell lifts it only when a page opens).
  const redraw=async()=>{target.replaceChildren();await schedulingGroupsPage(target,deps);if(target.isConnected)liftFooterStatus(target);};

  // ---- Rota Coordinators and private drafts (the department's planning settings) ----
  const s=settings.definition;
  const facts=ruleFacts([
    ['Rota Coordinators',s.coordinator_identity_ids.length?people(s.coordinator_identity_ids):el('span',{class:'muted'},'None · needed before a group can be active')],
    ['Private drafts per person',s.active_private_drafts_per_owner?String(s.active_private_drafts_per_owner):'No limit'],
    ['Archive private drafts after',s.archive_after_days?plural(s.archive_after_days,'day'):'Never']]);
  const settingsBox=el('section',{class:'panel planning-settings','aria-label':'Rota Coordinators and private drafts'},
    sectionHead('Rota Coordinators and private drafts','',...(admin?[button('Edit',()=>settingsForm(),'secondary small'),withIcon(button('History',()=>settingsHistory(),'ghost small'),'history')]:[])),facts);

  // One empty state everywhere (C-03): the kind's purpose and its one Create, never also in the head.
  const create=()=>admin?withIcon(button(createLabel('scheduling_groups'),()=>groupForm(),'primary'),'plus'):null;
  target.append(sectionHead(kindLabel('scheduling_groups'),'',groups.length?create():null));
  if(!groups.length){target.append(empty('No scheduling groups yet',purposeOf('scheduling_groups'),create(),'people'),settingsBox);return;}
  const status=(g:GroupVersion)=>groupStatus(g.definition,deps.today);
  const dates=(d:GroupDefinition)=>`From ${formatDate(d.starts_on,deps.zone)}${d.ends_on?' until '+formatDate(lastIncluded(d.ends_on),deps.zone):''}`;
  const owners=(d:GroupDefinition,g?:GroupVersion)=>g?.owner_names?.length&&!names.size?g.owner_names.join(', '):people(d.owner_identity_ids);
  // Session editors: their own property, column and filter beside Responsible (names, never ids).
  const editors=(d:GroupDefinition)=>people(d.co_editor_identity_ids??[])||'—';
  const list=dataTable<GroupVersion>({id:'scheduling-groups',noun:['scheduling group','scheduling groups'],rows:groups,rowId:g=>g.group_id,
    searchLabel:'Search scheduling groups',searchPlaceholder:'Search scheduling groups',
    columns:[
      {key:'name',label:'Group',text:g=>g.definition.name,sort:g=>g.definition.name,
        cell:g=>el('span',{class:'ct-name'},el('h3',{},g.definition.name),el('small',{},dates(g.definition)))},
      {key:'who',label:'Who',text:g=>deps.words(g.definition.membership)||'Everyone',cell:g=>deps.words(g.definition.membership)||'Everyone'},
      {key:'owners',label:'Responsible',text:g=>owners(g.definition,g),cell:g=>owners(g.definition,g),facet:g=>g.owner_names?.length?g.owner_names:g.definition.owner_identity_ids.map(who).filter(Boolean)},
      {key:'editors',label:'Session editors',text:g=>editors(g.definition),cell:g=>editors(g.definition),facet:g=>(g.definition.co_editor_identity_ids??[]).map(who).filter(Boolean)},
      {key:'priority',label:'Priority',num:true,sort:g=>g.definition.primary_priority,text:g=>String(g.definition.primary_priority),
        cell:g=>el('span',{class:'ct-mono'},String(g.definition.primary_priority))},
      {key:'status',label:'Status',text:status,facet:status,cell:g=>el('span',{class:'ct-status '+(status(g)==='Active'?'on':'off')},status(g))}],
    defaults:{sort:['priority','asc']},
    // D-191 (DL-007): an owner or session editor sees Edit, unavailable with the reason (the server refuses: planning.configure).
    rowButtons:g=>{if(!admin&&![...(g.definition.owner_identity_ids??[]),...(g.definition.co_editor_identity_ids??[])].includes(deps.me.identity_id))return [];
      const edit=button('Edit',()=>groupForm(g),'secondary small',{'aria-label':'Edit '+g.definition.name});
      return [admin?edit:disabledWhy(edit,`Only ${holders('planning.configure')} change scheduling groups.`)];},
    rowActions:g=>[{label:'History',icon:'history',run:()=>groupHistory(g)},
      ...(admin?[{label:g.definition.active===false?'Switch on':'Switch off',icon:(g.definition.active===false?'check':'close') as 'check'|'close',
        run:()=>switchGroup(g)}]:[])]});
  target.append(list.root,settingsBox);

  /** New or Edit: name, who (the one condition builder), owners and co-editors, priority, dates and the reason. */
  function groupForm(g?:GroupVersion){
    const d=g?.definition,next=Math.max(0,...groups.map(x=>x.definition.primary_priority+1));
    formDialog(g?'Edit '+d!.name:'New scheduling group','',form=>{
      const name=field('Name','text',d?.name||'',{required:true,maxLength:160});
      const members=conditionBuilder(groupFields(deps.fields),d?.membership||null,{context:'rule',lead:''});
      let ownerIds=d?.owner_identity_ids||[],coIds=d?.co_editor_identity_ids||[];
      const ownersPick=multiPick('Responsible',editorChoices(),ownerIds,v=>{ownerIds=v;});
      const coPick=multiPick('Session editors',editorChoices(),coIds,v=>{coIds=v;},'None');
      const priority=field('Priority (lowest wins)','number',d?.primary_priority??next,{required:true,min:0,step:1});
      const from=field('From','date',d?.starts_on||deps.today,{required:true}),until=field('Until (included)','date',d?.ends_on||'');lastDayIncluded(until.input);  // SX-22: lastDayIncluded is the one conversion
      const reason=area('Reason','',2);reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
      // SW3-D-03 (D41): an active group needs Rota Coordinators. Said before the form is filled, with the setting at hand.
      if(!s.coordinator_identity_ids.length&&(d?.active??true)){
        const missing=note(`${NO_ROTA_COORDINATORS}: an active group needs them.`,'warning');
        missing.append(' ',button('Choose Rota Coordinators',()=>settingsForm(()=>missing.remove()),'secondary small'));
        form.append(el('div',{class:'span-2'},missing));}
      form.append(name.wrap,el('div',{class:'span-2 audience-conditions','aria-label':'Who'},el('p',{class:'pick-label'},'Who'),members.root),
        el('div',{class:'pick-field'},el('span',{class:'pick-label'},'Responsible'),ownersPick.wrap),el('div',{class:'pick-field'},el('span',{class:'pick-label'},'Session editors'),coPick.wrap),
        priority.wrap,dateRange('Dates',from,until),el('div',{class:'span-2'},reason.wrap));
      return async()=>{
        const membership=members.read();
        if(!membership)throw new Error('Choose who is in this group.');
        if(!ownerIds.length)throw new Error('Choose who is responsible.');
        const definition:GroupDefinition={name:name.input.value.trim(),owner_identity_ids:ownerIds,co_editor_identity_ids:coIds.filter(id=>!ownerIds.includes(id)),
          membership,primary_priority:Number(priority.input.value),starts_on:from.input.value,ends_on:until.input.value||null,active:d?.active??true};
        await command(g?'planning_group.update':'planning_group.create',{definition,reason:reason.input.value.trim()},g?.group_id,g?.revision);
        notify(g?'Scheduling group saved.':'Scheduling group added.');await redraw();};
    },g?'Save':'Add group');
  }
  function switchGroup(g:GroupVersion){
    const on=g.definition.active===false;
    formDialog(`${on?'Switch on':'Switch off'} ${g.definition.name}`,on?'':'Its people are scheduled by the Rota Coordinators until it is switched on again.',form=>{
      const reason=area('Reason','',2);reason.input.required=true;reason.input.minLength=3;form.append(el('div',{class:'span-2'},reason.wrap));
      return async()=>{await command('planning_group.update',{definition:{...g.definition,active:on},reason:reason.input.value.trim()},g.group_id,g.revision);
        notify(on?'Switched on.':'Switched off.');await redraw();};},on?'Switch on':'Switch off');
  }
  /** Restore asks why, then saves the chosen version as a new one (the history keeps every version). */
  const restoreForm=(revision:number,run:(reason:string)=>Promise<unknown>,said:string)=>(close:()=>void)=>formDialog(`Restore version ${revision}`,'Saved as a new version; the history keeps every version.',form=>{
    const reason=area('Reason','',2);reason.input.required=true;reason.input.minLength=3;form.append(el('div',{class:'span-2'},reason.wrap));
    return async()=>{await run(reason.input.value.trim());close();notify(said);await redraw();};},'Restore');
  /** Every version, newest first, in the shared history (dom.ts historyDialog). */
  function groupHistory(g:GroupVersion){
    historyDialog(g.definition.name+' history','Group',async offset=>{
      const r=await get<{groups:GroupVersion[];has_more:boolean}>(`/api/planning-groups?group_id=${encodeURIComponent(g.group_id)}&limit=100&offset=${offset}`);
      return {more:r.has_more,versions:r.groups.map(v=>({version:v.revision,saved:formatDate(v.created_at,deps.zone),by:who(v.actor_id),actor_id:v.actor_id,reason:v.reason,
        value:`${v.definition.name} · ${deps.words(v.definition.membership)||'Everyone'} · ${groupStatus(v.definition,deps.today)}`,
        restore:admin&&v.revision!==g.revision?restoreForm(v.revision,reason=>command('planning_group.rollback',{version_id:v.id,reason},g.group_id,g.revision),'Earlier version restored.'):undefined}))};});
  }
  function settingsForm(saved?:()=>void){
    formDialog('Rota Coordinators and private drafts','',form=>{
      let ids=s.coordinator_identity_ids;
      const coordinators=multiPick('Rota Coordinators',editorChoices(),ids,v=>{ids=v;},'None');
      const limit=field('Private drafts per person','number',s.active_private_drafts_per_owner??'',{min:1,step:1,placeholder:'No limit'});
      const age=field('Archive private drafts after (days)','number',s.archive_after_days??'',{min:1,step:1,placeholder:'Never'});
      const reason=area('Reason','',2);reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
      form.append(el('div',{class:'pick-field span-2'},el('span',{class:'pick-label'},'Rota Coordinators'),coordinators.wrap),limit.wrap,age.wrap,el('div',{class:'span-2'},reason.wrap));
      const num=(f:typeof limit)=>f.input.value?Number(f.input.value):null;
      return async()=>{await command('planning.settings',{definition:{coordinator_identity_ids:ids,active_private_drafts_per_owner:num(limit),archive_after_days:num(age)},reason:reason.input.value.trim()},undefined,settings.settings?.revision);
        notify('Saved.');if(ids.length)saved?.();await redraw();};});
  }
  function settingsHistory(){
    historyDialog('Rota Coordinators and private drafts history','Rota Coordinators',async()=>{
      const r=await get<{versions:SettingsVersion[]}>('/api/planning-settings?history=true&limit=100');
      return {more:false,versions:r.versions.map(v=>({version:v.revision,saved:formatDate(v.created_at,deps.zone),by:who(v.actor_id),actor_id:v.actor_id,reason:v.reason,
        value:people(v.definition.coordinator_identity_ids||[])||'None',
        restore:admin&&v.revision!==settings.settings?.revision?restoreForm(v.revision,reason=>command('planning.settings.rollback',{version_id:v.id,reason},undefined,settings.settings?.revision),'Earlier settings restored.'):undefined}))};});
  }
}
