import {TIMES_OF_DAY_DEFAULTS,TIMES_OF_DAY_LIMIT,type TimeOfDay} from './generated.js';
import {el,button,field,select,saveBar,confirmAction,sectionHead,notify,inlineCheck,COLOUR_NAMES,PALETTE} from './dom.js';
import {command,undoInPlace} from './api.js';
import {drawnBase} from './settings_base.js';
import {TERMS} from './terms.js';
import type {Row} from './types.js';
import {CONFIG_KINDS} from './config_kinds.js';

/** The department's times of day by start (DisplaySettingsInput.times_of_day): the contract's defaults until it saves its own. */
export const timesOfDay=(department:Row|undefined):readonly TimeOfDay[]=>(department?.display_settings?.times_of_day as TimeOfDay[]|undefined)||TIMES_OF_DAY_DEFAULTS;
/** The time of day a duty starting at local "HH:MM" belongs to: the latest start at or before it; before the first
 *  start, the last one (it runs past midnight to the first start). `times` sorted by start, as the server keeps them. */
export function timeOfDayAt(times:readonly TimeOfDay[],hhmm:string):TimeOfDay{
  let at=times[times.length-1];for(const t of times)if(t.start<=hhmm)at=t;return at;}
/** Where each time of day ends, in `times`' own order: the next start; the last ends at the first start the next day. */
export function untilWords(times:readonly {start:string}[]):string[]{
  const order=times.map((_,i)=>i).sort((a,b)=>times[a].start.localeCompare(times[b].start)||a-b),out:string[]=[];
  order.forEach((i,n)=>{out[i]=n===order.length-1?`Until ${times[order[0]].start} next day`:`Until ${times[order[n+1]].start}`;});
  return out;}
/** The first other time of day that already uses `value` as its `key` (any case). */
export const clashOf=(times:readonly TimeOfDay[],self:number,key:'name'|'code'|'start',value:string)=>{
  const v=value.trim().toLowerCase();return v?times.find((t,i)=>i!==self&&t[key].trim().toLowerCase()===v):undefined;};

export interface TimesOfDayDeps{department:Row;
  /** The command's reply patches the session and draws the page again (app.ts settingsSaved). */saved:(result:Row)=>Promise<void>;
  /** A save refused as stale says so in `slot` with Reload (app.ts settingsMoved): false for any other error. */moved:(e:unknown,slot:HTMLElement)=>boolean;
  discard:()=>unknown;setLeave:(dirty:(()=>boolean)|null)=>void}
/** Configuration › Times of day: the department's named parts of the day (name, one-character code, start, colour) that
 *  Colour by › Time of day draws duties in. Saved through department.display: versioned, audited, with Undo. */
export function timesOfDayPage(target:HTMLElement,d:TimesOfDayDeps){
  const base=drawnBase(d.department as {id:string;revision:number}),conflict=el('div',{});
  const draft:TimeOfDay[]=[...timesOfDay(d.department)].map(t=>({...t})).sort((a,b)=>a.start.localeCompare(b.start));
  const list=el('div',{class:'tod-list'}),actions=el('div',{class:'actions'});
  const save=button('Save',()=>{
    if(![...list.querySelectorAll<HTMLInputElement|HTMLSelectElement>('input,select')].every(f=>f.reportValidity()))return;
    const want=draft.map(t=>({...t,name:t.name.trim()})).sort((a,b)=>a.start.localeCompare(b.start));
    confirmAction('Save times of day',want.map(t=>`${t.code} ${t.name} ${t.start}`).join(' · '),async reason=>{
      try{const result=await command('department.display',{times_of_day:want,reason},base.id,base.revision);notify('Times of day saved.');await d.saved(result);}
      catch(e){if(!d.moved(e,conflict))throw e;}},'Save');},'primary',{disabled:true,why:TERMS.nothingChanged});
  const touch=()=>{save.disabled=false;};
  // Rows keep their place while edited (a new start re-orders them at the next draw); "Until" follows at once.
  function draw(focus?:TimeOfDay){
    const checks:(()=>void)[]=[],until:HTMLElement[]=[];
    const sayUntil=()=>untilWords(draft).forEach((w,i)=>{until[i].textContent=w;});
    list.replaceChildren(...draft.map((t,i)=>{
      const name=field('Name','text',t.name,{required:true,maxlength:40,autocomplete:'off'});
      const code=field('Code','text',t.code,{required:true,maxlength:1,autocomplete:'off',autocapitalize:'characters',spellcheck:'false'});
      const start=field('Starts','time',t.start,{required:true});
      const colour=select('Colour',PALETTE.map(c=>({value:c,label:COLOUR_NAMES[c]})),t.colour);
      // The code in the colour: what a duty shows when Colour by is Time of day (colour is never the only cue).
      const mark=el('span',{class:`tod-code duty-${t.colour}`,'aria-hidden':'true'},t.code);
      name.input.addEventListener('input',()=>{t.name=name.input.value;touch();});
      code.input.addEventListener('input',()=>{code.input.value=code.input.value.toUpperCase();t.code=code.input.value;mark.textContent=t.code;touch();});
      start.input.addEventListener('input',()=>{if(/^\d\d:\d\d$/.test(start.input.value)){t.start=start.input.value;sayUntil();}touch();});
      colour.input.addEventListener('change',()=>{t.colour=colour.input.value as TimeOfDay['colour'];mark.className=`tod-code duty-${t.colour}`;touch();});
      const clash=(key:'name'|'code'|'start',input:HTMLInputElement)=>{const other=clashOf(draft,i,key,input.value);return other?`${other.name.trim()||'Another time of day'} already uses ${input.value.trim()}`:'';};
      checks.push(inlineCheck(name,()=>clash('name',name.input)),
        inlineCheck(code,()=>code.input.value&&!/^[A-Z0-9]$/.test(code.input.value)?'Use one letter or digit':clash('code',code.input)),
        inlineCheck(start,()=>clash('start',start.input)));
      const remove=button('Remove',()=>{draft.splice(i,1);touch();draw();},'ghost small',
        draft.length>1?{'aria-label':`Remove ${t.name.trim()||'this time of day'}`}:{disabled:true,why:'A department keeps at least one time of day.'});
      const row=el('div',{class:'tod-row'},mark,name.wrap,code.wrap,start.wrap,colour.wrap,until[i]=el('span',{class:'tod-until'}),remove);
      if(t===focus)queueMicrotask(()=>{name.input.focus();name.input.select();});
      return row;}));
    sayUntil();
    // A duplicate is said on both rows: every row's checks run again when any field changes.
    list.oninput=list.onchange=()=>checks.forEach(c=>c());
    const full=draft.length>=TIMES_OF_DAY_LIMIT;
    actions.replaceChildren(button('Add time of day',()=>{
      // A new one starts an hour after the last, with the first free code and colour and a name to type over.
      const last=draft[draft.length-1],m=last?(Number(last.start.slice(0,2))*60+Number(last.start.slice(3))+60)%1440:0;
      const free=(have:string[],from:readonly string[])=>from.find(c=>!have.includes(c))||from[0];
      const next:TimeOfDay={name:free(draft.map(t=>t.name),Array.from({length:TIMES_OF_DAY_LIMIT+1},(_,n)=>`Time of day ${n+1}`)),
        code:free(draft.map(t=>t.code),[...'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789']),
        start:`${String(Math.floor(m/60)).padStart(2,'0')}:${String(m%60).padStart(2,'0')}`,colour:free(draft.map(t=>t.colour),PALETTE) as TimeOfDay['colour']};
      draft.push(next);touch();draw(next);},'secondary small',full?{disabled:true,why:`A department has at most ${TIMES_OF_DAY_LIMIT} times of day.`}:{}));
  }
  draw();d.setLeave(()=>!save.disabled);
  // Undo of a save (or of an earlier display change) draws this page again from the department it returns.
  undoInPlace(async done=>{if(!list.isConnected||done.some(x=>!['department.display','department.display.restore'].includes(x.kind)))return false;
    for(const x of done)await d.saved(x.result);return true;});
  target.append(sectionHead(CONFIG_KINDS.times_of_day.label,''),conflict,el('section',{class:'panel'},list,actions),saveBar(save,d.discard));
}
