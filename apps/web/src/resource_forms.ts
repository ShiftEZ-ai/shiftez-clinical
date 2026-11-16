import {WEEKDAYS,WEEKEND_DAYS,weekOrder,el,field,area,select,multiPick,button,iconButton,note,labelOf,inlineCheck} from './dom.js';
import {entryChoices} from './date_sets.js';
import {withUnavailableEntries} from './conditions.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';
import type {DateSetCalendarRule,OpeningWindow,OperatingCalendarDefinition,ResourceRequirement} from './generated.js';

const clockPattern='([01][0-9]|2[0-3]):[0-5][0-9]|24:00';
/** Words of one opening-hours list: by default "<label> opening start", "Add <label> window"…; a date-set rule says them
 *  plainly ("Open from", "Add hours") because its fieldset already names the set. */
type WindowWords={start:string;end:string;add:string;remove:string};
const windowWords=(label:string):WindowWords=>({start:label+' opening start',end:label+' opening end',add:'Add '+label+' window',remove:'Remove '+label+' window'});
function windowsBuilder(label:string,initial:OpeningWindow[]=[],words:WindowWords=windowWords(label)){
  const wrap=el('div',{}),body=el('div',{});const readers:(()=>OpeningWindow)[]=[];
  const changed=()=>wrap.dispatchEvent(new Event('change',{bubbles:true}));
  function add(value?:OpeningWindow){
    if(readers.length>=8)return;
    const start=field(words.start,'text',value?.start_time||'08:00',{required:true,pattern:clockPattern});
    const end=field(words.end,'text',value?.end_time||'16:00',{required:true,pattern:clockPattern});
    // The server's rule (OpeningWindow.ordered): a window ends after it starts, within one day; nothing comes after 24:00.
    const ordered=inlineCheck(end,()=>!start.input.validity.valid||!end.input.value||end.input.validity.patternMismatch?''
      :start.input.value==='24:00'||end.input.value<=start.input.value?'Must be after the opening start. For overnight hours, add hours on both days.':'');
    start.input.addEventListener('input',ordered);start.input.addEventListener('change',ordered);
    const row=el('div',{class:'mini-grid'},start.wrap,end.wrap);
    const read=()=>({start_time:start.input.value,end_time:end.input.value});readers.push(read);
    row.append(button(words.remove,()=>{readers.splice(readers.indexOf(read),1);row.remove();changed();},'ghost'));body.append(row);
  }
  initial.forEach(add);wrap.append(body,button(words.add,()=>{add();changed();},'secondary'));
  const read=()=>readers.map(r=>r());
  // Replace all windows (used by 'copy Monday to weekdays'): a real change is an edit, like Add and Remove, so the
  // unsaved-changes guard and the impact preview see it.
  const set=(values:OpeningWindow[])=>{const was=JSON.stringify(read());readers.length=0;body.replaceChildren();values.forEach(add);if(JSON.stringify(read())!==was)changed();};
  return {wrap,read,set};
}

export function operatingCalendarFields(ctx:Context,row?:Row){
  const definition:OperatingCalendarDefinition=row?.definition||{timezone:ctx.me.organisation.timezone};
  const zone=field('Opening calendar timezone','text',definition.timezone,{required:true,maxlength:100});zone.wrap.classList.add('span-2');
  const days=weekOrder().map(weekday=>({weekday,windows:windowsBuilder(WEEKDAYS[weekday],definition.week?.find(d=>d.weekday===weekday)?.windows||[])}));
  const exceptions=el('div',{}),reads:(()=>{on:string;label:string;windows:OpeningWindow[]})[]=[];
  function addOverride(value?:{on:string;label:string;windows?:OpeningWindow[]}){
    if(reads.length>=366)return;
    const day=field('Override date','date',value?.on||'',{required:true});
    const label=field('Override label','text',value?.label||'',{required:true,maxlength:160});
    const windows=windowsBuilder('override',value?.windows||[]);
    const row=el('fieldset',{class:'nested-card'},el('legend',{},'Date-specific replacement'),el('div',{class:'mini-grid'},day.wrap,label.wrap),
      el('p',{class:'muted'},'Leave hours empty to close this date. Added hours replace the usual weekly hours.'),windows.wrap);
    const read=()=>({on:day.input.value,label:label.input.value,windows:windows.read()});reads.push(read);
    row.append(button('Remove date override',()=>{reads.splice(reads.indexOf(read),1);row.remove();},'ghost'));exceptions.append(row);
  }
  definition.overrides?.forEach(addOverride);
  // Holidays & dates off: "On <set> [entries]: Closed | Open these hours". The first rule that matches a day applies.
  const sets=ctx.catalogues.date_sets||[],rules=el('div',{}),ruleReads:{read:()=>DateSetCalendarRule;node:HTMLElement;paint:(i:number)=>void;focus:()=>void}[]=[];
  // Move rule up is off on the first rule (nothing to pass); a rule's buttons and legend name its set.
  const order=()=>ruleReads.forEach((r,i)=>r.paint(i));
  // Reordering or removing a rule changes the calendar without an input event: tell the form (dirty-close guard, preview).
  const changed=()=>rules.dispatchEvent(new Event('change',{bubbles:true}));
  function addRule(value?:DateSetCalendarRule){
    const set=select('Date set',sets.map(r=>({value:r.id,label:labelOf(r)+(r.active===false?' · not in use':'')})),value?.date_set_id||'');
    const setName=()=>{const r=sets.find(x=>x.id===set.input.value);return r?labelOf(r):'a date set';};
    // The entries of the chosen set: the shared picker, none chosen means all (the same words as the rule builder).
    const entryBox=el('div',{class:'span-2'});let chosen:string[]=[];
    const drawEntries=(keep:string[])=>{const r=sets.find(x=>x.id===set.input.value);chosen=[...keep];
      const choices=withUnavailableEntries(entryChoices(r?[r]:[]),chosen);
      const pick=multiPick('Entries',choices.map(c=>({...c,tone:'all'})),chosen,v=>{chosen=v;},'All entries');
      entryBox.replaceChildren(...(r&&!choices.length?[note('This set has no entries yet.','warning')]:r?[el('div',{class:'field'},el('label',{'aria-hidden':'true'},'Entries'),pick.wrap)]:[]));};
    drawEntries(value?.entry_names||[]);
    const mode=select('On these days',[{value:'closed',label:'Closed'},{value:'open',label:'Open these hours'}],value?.windows?.length?'open':'closed');
    const windows=windowsBuilder('date set',value?.windows||[],{start:'Open from',end:'Open until',add:'Add hours',remove:'Remove these hours'});
    const checkHours=inlineCheck(mode,()=>mode.input.value==='open'&&!windows.read().length?'Add opening hours or choose Closed.':'');
    const sync=()=>{const open=mode.input.value==='open';windows.wrap.hidden=!open;if(open&&!windows.read().length)windows.set([{start_time:'08:00',end_time:'16:00'}]);
      windows.wrap.querySelectorAll<HTMLInputElement>('input').forEach(i=>i.disabled=!open);checkHours();};
    windows.wrap.addEventListener('change',checkHours);
    mode.input.addEventListener('change',sync);
    const legend=el('legend',{});
    const node=el('fieldset',{class:'nested-card'},legend,el('div',{class:'mini-grid'},set.wrap,mode.wrap),entryBox,windows.wrap);
    const up=iconButton('Move rule up','chevronUp',()=>{const i=ruleReads.indexOf(item);if(i<1)return;ruleReads.splice(i,1);ruleReads.splice(i-1,0,item);
      node.previousElementSibling?.before(node);order();changed();(up.disabled?remove:up).focus();});
    const remove=button('Remove rule',()=>{const i=ruleReads.indexOf(item);ruleReads.splice(i,1);node.remove();order();changed();
      (ruleReads[i]?.focus||ruleReads[i-1]?.focus||(()=>add.focus()))();},'ghost');
    const paint=(i:number)=>{const n=setName();legend.textContent=`On ${n}`;up.disabled=i===0;
      up.setAttribute('aria-label',`Move the rule for ${n} up`);up.dataset.tip=up.getAttribute('aria-label')!;remove.setAttribute('aria-label',`Remove the rule for ${n}`);};
    const item={node,paint,focus:()=>set.input.focus(),read:():DateSetCalendarRule=>({date_set_id:set.input.value,entry_names:chosen,windows:mode.input.value==='open'?windows.read():[]})};
    set.input.addEventListener('change',()=>{drawEntries(chosen);order();});
    ruleReads.push(item);
    node.append(el('div',{class:'actions'},up,remove));
    rules.append(node);sync();order();
  }
  definition.date_set_rules?.forEach(addRule);
  const add=button('Add date set rule',()=>{addRule();ruleReads[ruleReads.length-1]?.focus();},'secondary');
  const dateSetPart=sets.length||definition.date_set_rules?.length?[el('h3',{class:'span-2'},'Holidays & dates off'),
    el('p',{class:'hint span-2'},'Closed, or open other hours, on the days of a date set. The first rule that matches a day applies; a date-specific replacement above comes before every rule.'),
    el('div',{class:'span-2'},rules),add]
    :[el('h3',{class:'span-2'},'Holidays & dates off'),el('p',{class:'hint span-2'},'No date sets yet. Add them in Configuration › Holidays & dates off, then close or change hours on those days here.')];
  const wrap=el('section',{class:'span-2 form-section'},el('h3',{},'Operating and holiday calendar'),
    el('p',{},'Days without opening hours are closed. For overnight opening, add hours on both dates. Add holidays below.'),el('div',{class:'form-grid'},zone.wrap,
    // One compact row per weekday: its name, its opening windows, and an add button.
    button(`Copy ${WEEKDAYS[days[0].weekday]} hours to the other working days`,()=>{const first=days[0].windows.read();for(const d of days.slice(1))if(!WEEKEND_DAYS.includes(d.weekday))d.windows.set(first);},'ghost small'),
    el('div',{class:'week-hours span-2'},...days.map(d=>el('div',{class:'day-row',role:'group','aria-label':WEEKDAYS[d.weekday]},el('strong',{},WEEKDAYS[d.weekday]),d.windows.wrap))),
    el('h3',{class:'span-2'},'Holiday and date exceptions'),el('div',{class:'span-2'},exceptions),button('Add date override',()=>addOverride(),'secondary'),...dateSetPart));
  return {wrap,read:():OperatingCalendarDefinition=>({schema_version:1,timezone:zone.input.value,week:days.map(d=>({weekday:d.weekday,windows:d.windows.read()})),overrides:reads.map(read=>read()),
    date_set_rules:ruleReads.map(r=>r.read())})};
}

export function resourceRequirementsBuilder(ctx:Context,initial:ResourceRequirement[]=[]){
  const body=el('div',{}),reads:(()=>ResourceRequirement)[]=[];
  function add(value?:ResourceRequirement){
    if(reads.length>=24)return;
    const pool=select('Resource pool',(ctx.catalogues.resource_pools||[]).map(r=>({value:r.id,label:labelOf(r)+(r.active===false?' · inactive':'')})),value?.resource_pool_id||'');
    const units=field('Required resource units','number',value?.units||1,{min:1,max:1000,required:true});
    const start=field('Start after session begins - minutes','number',value?.from_minute||0,{min:0,max:2160,required:true});
    const end=field('End after session begins - minutes','number',value?.to_minute??'',{min:1,max:2160,placeholder:'Session end'});
    const row=el('div',{class:'nested-card'},el('div',{class:'mini-grid'},pool.wrap,units.wrap,start.wrap,end.wrap));
    const read=()=>({resource_pool_id:pool.input.value,units:Number(units.input.value),from_minute:Number(start.input.value),to_minute:end.input.value?Number(end.input.value):null});reads.push(read);
    row.append(button('Remove resource requirement',()=>{reads.splice(reads.indexOf(read),1);row.remove();},'ghost'));body.append(row);
  }
  initial.forEach(add);
  return {wrap:el('section',{class:'span-2 form-section'},el('h3',{},'Pooled equipment and resources'),
    el('p',{},'Resources are reserved once per session. Availability is checked against this draft and published duties. Enter minutes from the session start; leave the end blank to use the session end.'),body,
    button('Add resource requirement',()=>add(),'secondary')),read:()=>reads.map(read=>read())};
}
