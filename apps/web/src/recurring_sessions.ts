import {el,icon,button,popover,menuItem,multiPick,field,plural,labelOf,formatDate,typeColour,WEEKDAYS,WEEKEND_DAYS,weekOrder,inlineCheck} from './dom.js';
import {command,request} from './api.js';
import {clashDialog,unsaved} from './double_booking.js';
import {WEEKDAY_SHORT_LABELS as DAY,type SeriesOverlapBatchPreview} from './generated.js';
import {dataTable,type Column,type EditSpec} from './table_view.js';
import {pillList,columnSizing} from './table_menu.js';
import {catalogueForm,peopleChoices,staffNeeded,activeOn,type Context} from './forms.js';
import {nameText,personName,fullNameOf} from './person_label.js';
import {dateRange,lastDayIncluded,showDay} from './date_picker.js';
import type {BulkAction} from './bulk.js';
import type {Row} from './types.js';
import {menu} from './dom.js';  // the shared menu (UX › controls S2b)

/**
 * Configuration › Recurring sessions. One row per series type: the recurring series that share a clinic, duty type,
 * times and dates (older data keeps one series per weekday or per person; they read and edit as one row). The shared
 * collection (table_view.ts) gives search, filters, Group (clinic, clinic type, duty type, person), sort, views and
 * inline edits; its Calendar layout draws each group as a week grid of time slots × days, with who covers each place.
 */
export type SeriesDeps={ctx:Context;editable:boolean;today:string;refresh:()=>Promise<void>;
  /** The catalogue's bulk actions for these series ids (switch on/off). */bulk:(seriesIds:string[])=>BulkAction[];
  /** One series' definition history. */history:(series:Row)=>void};
export type SeriesGroup={id:string;series:Row[];clinic:string;clinicType:string;type:Row|undefined;typeName:string;
  times:string;start:string;starts_on:string;ends_on:string|null;active:boolean;state:string;
  /** The series running on each weekday (two series on a day: two sessions). */days:Map<number,Row[]>;people:string[];description:string};

const lastIncluded=(exclusive:string)=>new Date(Date.parse(exclusive+'T12:00:00Z')-864e5).toISOString().slice(0,10);
/** "1 Sep 2026 – 31 Aug 2027", "From 1 Sep 2026". */
export const periodText=(starts:string,ends:string|null)=>ends?`${formatDate(starts)} – ${formatDate(lastIncluded(ends))}`:`From ${formatDate(starts)}`;
const timesOf=(s:Row)=>[...new Set(((s.definition?.days||[]) as Row[]).map(d=>`${d.start_time}–${d.end_time}`))].join(', ');

/** The series grouped into series types, sorted by clinic, then start time and duty type. */
export function seriesGroups(ctx:Context,today:string):SeriesGroup[]{
  const c=ctx.catalogues,find=(kind:string)=>(id:string)=>(c[kind]||[]).find(r=>r.id===id);
  const service=find('services'),location=find('locations'),locType=find('location_types'),type=find('assignment_types');
  const groups=new Map<string,SeriesGroup>();
  for(const s of c.recurring_series||[]){
    const d=s.definition||{},svc=service(d.service_ids?.[0]||''),loc=location(s.location_id);
    const clinic=svc?labelOf(svc):loc?labelOf(loc):'No clinic',times=timesOf(s),t=type(s.assignment_type_id);
    const key=[svc?.id||loc?.id||'',s.assignment_type_id,times,s.starts_on,s.ends_on||'',s.active!==false].join('|');
    let g=groups.get(key);
    if(!g){const state=s.active===false?'Inactive':activeOn(s,today)?'Active':s.starts_on>today?'Upcoming':'Ended';
      g={id:key,series:[],clinic,clinicType:loc?labelOf(locType(loc.location_type_id)||{name:'Other places'}):'Other places',type:t,typeName:t?labelOf(t):'No duty type',
        times,start:times.slice(0,5),starts_on:s.starts_on,ends_on:s.ends_on||null,active:s.active!==false,state,days:new Map(),people:[],description:''};groups.set(key,g);}
    g.series.push(s);
    for(const day of (d.days||[]) as Row[]){const list=g.days.get(day.weekday)||[];list.push(s);g.days.set(day.weekday,list);}
    for(const p of (d.default_person_ids||[]) as string[])if(!g.people.includes(p))g.people.push(p);
  }
  // A description repeated across rows ("Reference fixed clinic; …") says nothing about this row: only its own shows.
  const said=new Map<string,number>();
  for(const g of groups.values()){const ds=new Set(g.series.map(s=>String(s.description||'').trim()));g.description=ds.size===1?[...ds][0]:'';said.set(g.description,(said.get(g.description)||0)+1);}
  for(const g of groups.values())if((said.get(g.description)||0)>1)g.description='';
  return [...groups.values()].sort((a,b)=>a.clinic.localeCompare(b.clinic)||a.start.localeCompare(b.start)||a.typeName.localeCompare(b.typeName)||a.starts_on.localeCompare(b.starts_on));
}

/** Sessions a week: one per series and weekday. */
const weekly=(g:SeriesGroup)=>[...g.days.values()].reduce((n,list)=>n+list.length,0);
const valueOf=(s:Row)=>({name:s.name,description:s.description||'',starts_on:s.starts_on,ends_on:s.ends_on||null,active:s.active!==false,
  assignment_type_id:s.assignment_type_id,location_id:s.location_id,definition:s.definition});
type Step={item_kind:string;target_id?:string;expected_revision?:number;payload:Record<string,unknown>};
const update=(s:Row,change:Record<string,unknown>):Step=>({item_kind:'catalogue.update',target_id:s.id,expected_revision:s.revision,payload:{catalogue:'recurring_series',value:{...valueOf(s),...change}}});
/** Several saves as one change: one bulk command (one transaction, one message, one Undo for all of them). */
const together=async(steps:Step[])=>{if(!steps.length)return;const {items,ack}=await acknowledged(steps);await command('bulk.apply',{items,...ack,reason:'Inline edit'});};
/** A quick edit that would double-book someone shows the clashes and their fixes first: the server previews every
 *  pending series together (each row checked against the others as they will be, series_conflicts.py), and the same
 *  dialog as everywhere (double_booking.ts) lists them. Saving with clashes left sends the batch's one acknowledgement;
 *  the server still refuses clashes nobody acknowledged. */
async function acknowledged(steps:Step[]):Promise<{items:Step[];ack:{overlap_acknowledgement?:string}}>{
  for(;;){
    const series=steps.filter(s=>s.payload.catalogue==='recurring_series'&&['catalogue.update','catalogue.create'].includes(s.item_kind));
    if(!series.length)return {items:steps,ack:{}};
    const p=await request<SeriesOverlapBatchPreview>('/api/series/preview',{method:'POST',body:JSON.stringify({items:series.map(s=>
      ({value:s.payload.value,series_id:s.target_id??null,expected_revision:s.expected_revision??null}))})});
    // Two pending rows clashing are listed under both: the dialog shows each clash once.
    const seen=new Set<string>(),found=p.items.flatMap(it=>it.conflicts.map(c=>({c,s:series[it.index]}))).filter(({c})=>{
      const key=[c.person_id,...[c.shift_id,c.other_shift_id||c.availability_id||''].sort()].join('|');if(seen.has(key))return false;seen.add(key);return true;});
    if(!found.length)return {items:steps,ack:{}};
    // An occurrence positively not yet a session (double_booking.ts `unsaved`) of a series in this change is cancelled
    // in the change: the date becomes an excluded date of its pending value (saved with it, checked again now). A saved
    // session is cancelled by the server's own command, or shown as unavailable (locked, submitted or published):
    // excluding its date would leave the session itself as it is.
    const exclude=(s:Step,day:string)=>{const v=s.payload.value as Row;
      s.payload={...s.payload,value:{...v,definition:{...v.definition,excluded_dates:[...new Set([...(v.definition.excluded_dates||[]),day])].sort()}}};};
    const answer=await clashDialog(found.map(f=>f.c),c=>unsaved(c,'this')?()=>exclude(found.find(f=>f.c===c)!.s,c.occurrence_on||c.date):undefined,
      c=>{const s=c.other_series_id&&unsaved(c,'other')?series.find(x=>x.target_id===c.other_series_id):undefined;
        return s&&(()=>exclude(s,c.other_occurrence_on||c.date));});
    if(answer==='back')throw new Error('Not saved: this change would double-book someone.');
    if(answer==='save')return {items:steps,ack:{overlap_acknowledgement:p.conflict_token}};
  }
}

export function recurringSessions(target:HTMLElement,deps:SeriesDeps){
  const {ctx}=deps,people=ctx.catalogues.people||[];
  const person=(id:string)=>people.find(p=>p.id===id);
  const chip=(id:string)=>person(id)?nameText(id,'',{class:'rp person'}):el('span',{class:'rp person'},'Unknown person');
  const groups=seriesGroups(ctx,deps.today),byId=new Map(groups.map(g=>[g.id,g]));
  const seriesIds=(ids:string[])=>ids.flatMap(id=>byId.get(id)?.series.map(s=>s.id)||[]);
  const editSeries=(s:Row)=>catalogueForm(ctx,'recurring_series',s);
  /** One series opens its form; several (one per day or person) are listed first. */
  const editGroup=(g:SeriesGroup,anchor:HTMLElement,list=g.series)=>{if(list.length===1){editSeries(list[0]);return;}
    menu(anchor,'Recurring sessions',list.map(s=>({label:`${((s.definition?.days||[]) as Row[]).map(d=>WEEKDAYS[d.weekday]).join(', ')} · ${((s.definition?.default_person_ids||[]) as string[]).map(id=>person(id)?personName(id):'Unknown person').join(', ')||'Assign in the draft'}`,
      icon:'edit' as const,run:()=>editSeries(s)})),{placement:'below-end',head:`${g.clinic} · ${g.typeName} · ${g.times}`});};

  // ---- Inline edits: each saves every series of the row (and of the other selected rows) as one change ----
  const save=(run:(g:SeriesGroup,value:any)=>Step[])=>async(rows:SeriesGroup[],value:unknown)=>{await together(rows.flatMap(g=>run(g,value)));};
  const custom=(editor:EditSpec<SeriesGroup>['editor'],value:(g:SeriesGroup)=>unknown,run:(g:SeriesGroup,value:any)=>Step[]):EditSpec<SeriesGroup>|undefined=>
    deps.editable?{kind:'custom',value,editor,save:save(run),afterSave:deps.refresh}:undefined;
  const dayOrder=weekOrder();
  const daysEdit=custom((g,finish)=>{
    const boxes=dayOrder.map(w=>{const b=el('input',{type:'checkbox','aria-label':WEEKDAYS[w]}) as HTMLInputElement;b.checked=g.days.has(w);b.dataset.day=String(w);return b;});
    const node=el('div',{class:'sr-days-edit'},el('div',{class:'sr-day-boxes',role:'group','aria-label':'Days'},...boxes.map((b,i)=>el('label',{class:'sr-day-box'+(WEEKEND_DAYS.includes(dayOrder[i])?' weekend':'')},b,el('span',{},DAY[dayOrder[i]])))),
      g.series.length>1?el('p',{class:'hint'},'A new day starts with no usual assignee; a day taken off switches its session off.'):null);
    node.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();finish(true);}});
    setTimeout(()=>boxes[0]?.focus(),0);
    return {node,read:()=>boxes.filter(b=>b.checked).map(b=>Number(b.dataset.day)).sort()};
  },g=>[...g.days.keys()].sort(),(g,want:number[])=>{
    if(!want.length)throw new Error('Keep at least one day. To stop it, switch it off in Status.');
    const [start,end]=(g.times.split(', ')[0]||'08:00–16:00').split('–');
    if(g.series.length===1){const s=g.series[0],old=(s.definition.days||[]) as Row[];
      return [update(s,{definition:{...s.definition,days:want.map(w=>old.find(d=>d.weekday===w)||{weekday:w,start_time:start,end_time:end})}})];}
    const steps:Step[]=[];
    for(const s of g.series){const old=(s.definition.days||[]) as Row[],kept=old.filter(d=>want.includes(d.weekday));
      if(kept.length===old.length)continue;
      steps.push(update(s,kept.length?{definition:{...s.definition,days:kept}}:{active:false}));}
    const model=g.series[0];
    for(const w of want.filter(w=>!g.days.has(w))){const from=((model.definition.days||[]) as Row[])[0]?.weekday;
      steps.push({item_kind:'catalogue.create',payload:{catalogue:'recurring_series',value:{...valueOf(model),active:true,
        name:from!==undefined&&String(model.name).includes(WEEKDAYS[from])?String(model.name).replace(WEEKDAYS[from],WEEKDAYS[w]):model.name,
        definition:{...model.definition,days:[{weekday:w,start_time:start,end_time:end}],default_person_ids:[]}}}});}
    return steps;});
  const timesEdit=custom((g,finish)=>{
    const [a,b]=(g.times.split(', ')[0]||'').split('–');
    const start=field('Start','text',a||'08:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]',inputmode:'numeric',maxlength:5}),end=field('End','text',b||'16:00',{required:true,pattern:'([01][0-9]|2[0-3]):[0-5][0-9]|24:00',inputmode:'numeric',maxlength:5});
    const node=el('div',{class:'sr-times-edit'},el('div',{class:'mini-grid'},start.wrap,end.wrap),g.times.includes(',')?el('p',{class:'hint'},'Its days have different times now; every day gets these.'):null,
      el('p',{class:'hint'},'24-hour time; an end before the start continues into the next day, 24:00 is midnight.'));
    node.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();if(start.input.reportValidity()&&end.input.reportValidity())finish(true);}});
    setTimeout(()=>start.input.select(),0);
    return {node,read:()=>start.input.checkValidity()&&end.input.checkValidity()?`${start.input.value}–${end.input.value}`:g.times};
  },g=>g.times,(g,value:string)=>{const [start,end]=value.split('–');
    return g.series.map(s=>update(s,{definition:{...s.definition,days:((s.definition.days||[]) as Row[]).map(d=>({...d,start_time:start,end_time:end}))}}));});
  const crowded=(g:SeriesGroup)=>[...g.days.values()].some(list=>list.length>1);
  const peopleEdit=custom((g,finish)=>{
    if(crowded(g))return {node:el('p',{class:'hint sr-note'},'Some days have more than one session, each with its own people. Open a day in the Calendar view to change who covers it.'),read:()=>g.people};
    const pick=multiPick('Usual assignees',peopleChoices(ctx),g.people,undefined,'Assign in the draft');
    setTimeout(()=>{if(pick.wrap.isConnected)pick.open();},0);
    pick.wrap.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.ctrlKey)finish(true);});
    return {node:el('div',{},pick.wrap,el('p',{class:'hint'},'The same people on every day of this row.')),read:()=>pick.values()};
  },g=>g.people,(g,ids:string[])=>g.series.map(s=>update(s,{definition:{...s.definition,default_person_ids:ids}})));
  const datesEdit=custom((g,finish)=>{
    const from=field('Effective from','date',g.starts_on,{required:true}),until=field('Effective until (included)','date',g.ends_on||'');lastDayIncluded(until.input);
    until.input.placeholder='No end date';
    const range=dateRange('Dates',from,until);
    const check=inlineCheck(until,()=>{const a=from.input.value,b=until.input.value;if(!a||!b)return '';const n=Math.round((Date.parse(b)-Date.parse(a))/864e5);
      return n<1?'Choose a last day on or after the first day.':n>366?`More than one year: choose ${showDay(new Date(Date.parse(a)+365*864e5).toISOString().slice(0,10))} or earlier.`:'';});
    from.input.addEventListener('change',check);
    const node=el('div',{class:'sr-dates-edit'},range,el('p',{class:'hint'},'Up to one year per version; leave the last day empty for no end date.'));
    node.addEventListener('keydown',e=>{if(e.key==='Enter'&&!(e.target as HTMLElement).closest('.date-inline')){e.preventDefault();if(from.input.reportValidity()&&until.input.reportValidity())finish(true);}});
    return {node,read:()=>from.input.checkValidity()&&until.input.checkValidity()?[from.input.value,until.input.value||null]:[g.starts_on,g.ends_on]};
  },g=>[g.starts_on,g.ends_on],(g,[starts,ends]:[string,string|null])=>g.series.map(s=>update(s,{starts_on:starts,ends_on:ends})));
  const activeEdit:EditSpec<SeriesGroup>|undefined=deps.editable?{kind:'toggle',value:g=>g.active,afterSave:deps.refresh,
    // Switching on is checked for double bookings like any edit (all the rows together); switching off cannot clash.
    save:async(rows,value)=>{if(value){await together(rows.flatMap(g=>g.series.map(s=>update(s,{active:true}))));return;}
      await command('catalogue.bulk_update',{catalogue:'recurring_series',items:rows.flatMap(g=>g.series.map(s=>({id:s.id,revision:s.revision}))),active:value,reason:'Inline edit'});}}:undefined;

  const dayChips=(g:SeriesGroup)=>el('span',{class:'sr-days','aria-label':dayOrder.filter(w=>g.days.has(w)).map(w=>WEEKDAYS[w]+((g.days.get(w)!.length>1)?` (${g.days.get(w)!.length} sessions)`:'')).join(', ')},
    ...dayOrder.map(w=>{const n=g.days.get(w)?.length||0;return el('span',{class:'sr-day'+(n?' on':'')+(WEEKEND_DAYS.includes(w)?' weekend':''),'aria-hidden':'true'},DAY[w].slice(0,2),n>1?el('sup',{},String(n)):null);}));
  // The usual assignees' switches (forms.ts), shown with an icon and words (never colour alone); a row whose series
  // differ says how many have it. DL-009: the retired close switch changes nothing (D38) but stays findable, worded as such.
  const SWITCHES=[['usual_assignees_fixed','lock','Keeps usual people','Kept'],['close_when_all_usual_unavailable','power','Close setting · no effect','No effect']] as const;
  const switchesOf=(list:Row[])=>SWITCHES.map(([k,i,long,short])=>({i,long,short,n:list.filter(s=>s.definition?.[k]).length})).filter(x=>x.n)
    .map(x=>({...x,long:x.long+(x.n<list.length?` (${x.n} of ${list.length})`:'')}));
  const switchChips=(list:Row[],brief=false)=>switchesOf(list).map(x=>el('span',{class:'sr-switch','data-tip':x.long},icon(x.i),el('span',{class:'chip-text'},brief?x.short:x.long)));
  const swatch=(g:SeriesGroup)=>el('span',{class:`ct-swatch duty-${g.type?typeColour(g.type.id):'slate'}`,'aria-hidden':'true'},el('span',{}));
  const columns:Column<SeriesGroup>[]=[
    {key:'name',label:'Session',class:'sr-session',text:g=>`${g.clinic} ${g.typeName} ${g.description}`,sort:g=>`${g.clinic}|${g.start}|${g.typeName}`,
      cell:g=>el('div',{class:'ct-main'},swatch(g),el('span',{class:'ct-name'},el('h3',{title:g.clinic},g.clinic),el('small',{},`${g.typeName}${g.description?' · '+g.description:''}`)))},
    {key:'clinic',label:'Clinic',search:'related',hidden:true,text:g=>g.clinic,facet:g=>g.clinic,cell:g=>g.clinic},
    {key:'clinic_type',label:'Clinic type',hidden:true,text:g=>g.clinicType,facet:g=>g.clinicType,cell:g=>g.clinicType},
    {key:'type',label:'Duty type',hidden:true,text:g=>g.typeName,facet:g=>g.typeName,cell:g=>g.typeName},
    {key:'days',label:'Days',text:g=>dayOrder.filter(w=>g.days.has(w)).map(w=>WEEKDAYS[w]).join(' '),facet:g=>dayOrder.filter(w=>g.days.has(w)).map(w=>WEEKDAYS[w]),
      ordered:dayOrder.map(w=>WEEKDAYS[w]),sort:g=>Math.min(...[...g.days.keys()].map(w=>dayOrder.indexOf(w))),cell:dayChips,edit:daysEdit},
    {key:'times',label:'Times',text:g=>g.times,sort:g=>g.start,class:'ct-mono',cell:g=>g.times,edit:timesEdit},
    // Places: sessions a week, and the staff each needs (from the duty type).
    {key:'places',label:'Places',text:g=>`${weekly(g)} a week ${staffNeeded(g.type)}`,sort:g=>weekly(g),
      cell:g=>el('span',{class:'ct-name'},el('span',{},`${weekly(g)} a week`),el('small',{},`${staffNeeded(g.type)} each`))},
    {key:'staff',label:'Staff needed',hidden:true,text:g=>staffNeeded(g.type),facet:g=>staffNeeded(g.type),cell:g=>staffNeeded(g.type)},
    {key:'people',label:'Usual assignees',search:'related',class:'sr-people',text:g=>[...g.people.map(id=>fullNameOf(person(id))),...switchesOf(g.series).map(x=>x.long)].join(' '),facet:g=>g.people.length?g.people.map(id=>fullNameOf(person(id))):['Assign in the draft'],
      cell:g=>g.people.length?el('div',{class:'sr-people-cell'},pillList(g.people.map(id=>()=>chip(id)),{label:['usual assignee','usual assignees'],shown:2}),...switchChips(g.series)):el('span',{class:'muted'},'Assign in the draft'),edit:peopleEdit},
    {key:'dates',label:'Dates',text:g=>periodText(g.starts_on,g.ends_on),sort:g=>g.starts_on,
      cell:g=>el('span',{class:'ct-name'},el('span',{},formatDate(g.starts_on)),el('small',{},g.ends_on?`until ${formatDate(lastIncluded(g.ends_on))}`:'no end date')),edit:datesEdit},
    {key:'status',label:'Status',text:g=>g.state,facet:g=>g.state,ordered:['Active','Upcoming','Ended','Inactive'],
      cell:g=>el('span',{class:'ct-status '+(g.state==='Active'?'on':'off')},g.state),edit:activeEdit},
  ];

  // ---- Calendar: per group, a week grid of time slots (duty type and times) × days, each place and who covers it ----
  function calendar(rows:SeriesGroup[]){
    const slots=new Map<string,{g:SeriesGroup;cells:Map<number,{g:SeriesGroup;s:Row}[]>}>();
    for(const g of [...rows].sort((a,b)=>a.clinic.localeCompare(b.clinic)||a.start.localeCompare(b.start))){const key=`${g.clinic}|${g.start}|${g.typeName}|${g.times}`;let slot=slots.get(key);if(!slot){slot={g,cells:new Map()};slots.set(key,slot);}
      for(const [w,list] of g.days){const cell=slot.cells.get(w)||[];cell.push(...list.map(s=>({g,s})));slot.cells.set(w,cell);}}
    const head=el('tr',{},el('th',{scope:'col',class:'sw-slot-col'},'Time slot'),...dayOrder.map(w=>el('th',{scope:'col',class:WEEKEND_DAYS.includes(w)?'weekend':'',abbr:WEEKDAYS[w]},DAY[w])));
    const entry=(g:SeriesGroup,s:Row,list:{g:SeriesGroup;s:Row}[])=>{
      const ids=(s.definition?.default_person_ids||[]) as string[],tag=g.state==='Active'?'':g.state==='Upcoming'?`From ${formatDate(g.starts_on)}`:g.state==='Ended'?`Ended ${formatDate(lastIncluded(g.ends_on!))}`:'Off';
      const b=el('button',{type:'button',class:'sw-entry'+(g.state==='Active'?'':' sw-'+g.state.toLowerCase().replace(' ','-'))+(ids.length?'':' sw-open'),
        'aria-label':`${g.clinic}, ${g.typeName} ${g.times}: ${ids.length?ids.map(id=>fullNameOf(person(id))).join(', '):'assign in the draft'}${switchesOf([s]).map(x=>', '+x.long.toLowerCase()).join('')}${tag?', '+tag:''}${list.length>1?`, one of ${list.length} sessions`:''}. Edit`},
        ...(ids.length?ids.map(chip):[el('span',{class:'sw-assign'},'Assign in the draft')]),...switchChips([s],true),tag?el('span',{class:'sw-tag'},tag):null);
      b.addEventListener('click',()=>editSeries(s));return b;};
    // Several clinics in one grid (grouped by duty type or person): each slot names its clinic first.
    const clinics=new Set(rows.map(g=>g.clinic)).size>1;
    const body=[...slots.values()].map(({g,cells})=>el('tr',{},
      el('th',{scope:'row',class:'sw-slot'},el('span',{class:'sw-slot-name'},swatch(g),el('span',{},el('strong',{},clinics?g.clinic:g.typeName),el('span',{class:'ct-mono'},g.times))),
        // F-C4: cut with "…" in a narrow column; the whole line in its tooltip.
        (line=>el('small',{class:'muted','data-tip':line},line))(`${clinics?g.typeName+' · ':''}${staffNeeded(g.type)} each`)),
      ...dayOrder.map(w=>{const list=cells.get(w)||[];
        if(list.length)return el('td',{class:WEEKEND_DAYS.includes(w)?'weekend':''},el('div',{class:'sw-cell'},...list.map(x=>entry(x.g,x.s,list))));
        // An empty slot: a gap in the weekly pattern, one click from a new recurring session for that day and time.
        const [start,end]=(g.times.split(', ')[0]||'').split('–'),model=g.series[0];
        return el('td',{class:'sw-gap'+(WEEKEND_DAYS.includes(w)?' weekend':'')},deps.editable?button('',()=>catalogueForm(ctx,'recurring_series',undefined,{assignment_type_id:model.assignment_type_id,location_id:model.location_id,
          definition:{service_ids:model.definition?.service_ids||[],days:[{weekday:w,start_time:start,end_time:end}],default_person_ids:[]}}),'',{class:'sw-add','aria-label':`Add ${g.clinic}, ${g.typeName} ${g.times} on ${WEEKDAYS[w]}`,'data-tip':`Add on ${WEEKDAYS[w]}`}):null);
      })));
    body.forEach(tr=>tr.querySelectorAll('.sw-add').forEach(b=>b.append(icon('plus'))));
    const grid=el('table',{class:'sw-week'},el('thead',{},head),el('tbody',{},...body));columnSizing(grid,{reorder:false});
    return el('div',{class:'sw-scroll'},grid);
  }

  const list=dataTable<SeriesGroup>({id:'catalogue-recurring_series',noun:['session','sessions'],rows:groups,rowId:g=>g.id,
    searchLabel:'Search recurring sessions',searchPlaceholder:'Clinic, duty type, person or day',columns,
    defaults:{group:'clinic',layout:'calendar',sort:['name','asc']},
    calendar,
    // A row is a kind of session; the series it holds are counted too (the Configuration list counts rows).
    countNote:rows=>plural(rows.reduce((n,g)=>n+g.series.length,0),'series','series'),
    // Beside each group's name: how many series and sessions a week it holds, and the period it runs.
    groupAction:(_,rows)=>{const n=rows.reduce((a,g)=>a+g.series.length,0),live=rows.filter(g=>g.state!=='Ended'&&g.state!=='Inactive');
      const first=live.map(g=>g.starts_on).sort()[0],ends=live.some(g=>!g.ends_on)?null:live.map(g=>g.ends_on!).sort().at(-1)||null;
      return el('span',{class:'sw-group-facts muted'},`${plural(n,'series','series')} · ${plural(live.reduce((a,g)=>a+weekly(g),0),'session')} a week${first?' · '+periodText(first,ends):''}`);},
    bulk:deps.editable?ids=>deps.bulk(seriesIds(ids)).map(a=>({...a,run:(i:string[],done:()=>void)=>a.run(seriesIds(i),done)})):undefined,
    // A row opens its recurring session (several: a list to choose from); its cells edit in place.
    onOpen:g=>editGroup(g,target.querySelector<HTMLElement>(`[data-row="${CSS.escape(g.id)}"]`)||target),
    rowActions:g=>[{label:'Edit…',icon:'edit',run:()=>editGroup(g,target.querySelector<HTMLElement>(`[data-row="${CSS.escape(g.id)}"] .dt-more`)||target)},
      ...(g.series.length===1?[{label:'History',icon:'history' as const,run:()=>deps.history(g.series[0])}]:[])],
    rowLabel:g=>`${g.clinic} ${g.typeName} ${g.times}`,
  });
  target.append(list.root);
  return list;
}
