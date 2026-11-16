import {isolate,el,badge,field,select,check,button,inlineCheck,note,notify,confirmAction,table,dateUnits,plural,labelOf,statusBadge,reportError,departmentToday,type Choice,CAPABILITY} from './dom.js';
import {readFailed} from './dom.js';  // a failed read: refused (403) or an error with Retry (maintenance)
import {command,get} from './api.js';
import {dateRange,showDay} from './date_picker.js';
import {dockPanel,rowMark} from './side_panel.js';
import {skeleton} from './loading.js';
import type {Context} from './forms.js';
import type {DateSetMarker,Row} from './types.js';
import type {DateSetEntry,ResolvedDateSetEntry} from './generated.js';


/** Department date sets (Configuration › Holidays & dates off). Dates are worked out by the
 *  server's one resolver (packages/domain/date_sets.py, GET /api/date-sets/resolved): nothing here converts calendars. */
type Repeat=NonNullable<DateSetEntry['repeat']>;
const REPEATS:Record<Repeat,string>={once:'Once',yearly_gregorian:'Every year (Gregorian)',yearly_hijri:'Every year (Hijri, Umm al-Qura)'};
/** The same, short, where the entry's name is beside it (Dates). */
const SHORT:Record<Repeat,string>={once:'Once',yearly_gregorian:'Every year',yearly_hijri:'Every Hijri year'};
const REPEAT_CHOICES:Choice[]=(Object.keys(REPEATS) as Repeat[]).map(value=>({value,label:REPEATS[value]}));
/** Month names of the Hijri calendar, only to read a typed Hijri date back in words ("1 Shawwal"). */
const HIJRI_MONTHS=['Muharram','Safar','Rabi al-Awwal','Rabi al-Thani','Jumada al-Ula','Jumada al-Akhirah','Rajab','Shaban','Ramadan','Shawwal','Dhu al-Qadah','Dhu al-Hijjah'];
const HIJRI_PATTERN='\\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|30)';
const hijriWords=(d:string)=>{const m=/^(\d{4})-(\d{2})-(\d{2})$/.exec(d);return m&&HIJRI_MONTHS[Number(m[2])-1]?`${Number(m[3])} ${HIJRI_MONTHS[Number(m[2])-1]}`:'';};
const addDays=(day:string,n:number)=>{const x=new Date(day+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+n);return x.toISOString().slice(0,10);};
const dayCount=(a:string,b:string)=>Math.round((Date.parse(b)-Date.parse(a))/864e5)+1;
/** "Sun 29 Mar 2026" or "Sun 29 Mar 2026 – Wed 1 Apr 2026", whole dates (they wrap between dates, never inside one). */
export const occurrenceDates=(o:{first_day:string;last_day:string})=>dateUnits(o.first_day===o.last_day?[showDay(o.first_day)]:[showDay(o.first_day),'– '+showDay(o.last_day)]);
const occurrenceText=(o:{first_day:string;last_day:string})=>o.first_day===o.last_day?showDay(o.first_day):`${showDay(o.first_day)} – ${showDay(o.last_day)}`;
const resolved=(start:string,end:string,ids:string[])=>get<ResolvedDateSetEntry[]>('/api/date-sets/resolved?'+new URLSearchParams([['start',start],['end',end],...ids.map(id=>['set_id',id])]));
/** The named entries of these sets, as choices (a name shared by two sets is one choice: names are what filters match). */
export const entryChoices=(sets:Row[]):Choice[]=>[...new Set(sets.flatMap(s=>((s.entries||[]) as DateSetEntry[]).map(e=>e.name)))].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true})).map(n=>({value:n,label:n}));
const setOf=(ctx:Context,id:string)=>(ctx.catalogues.date_sets||[]).find(r=>r.id===id);
const editable=(ctx:Context)=>ctx.permits(CAPABILITY.ADMIN);

// ---- The list: entries and the next date of each set (read once per draw) ----
let upcoming=new Map<string,ResolvedDateSetEntry>();
/** The next occurrence of every set from today, within two years (once-only dates further away read "—"). */
export async function loadUpcoming(sets:Row[]){
  const today=departmentToday(),next=new Map<string,ResolvedDateSetEntry>();
  if(sets.length)try{for(const o of await resolved(today,addDays(today,730),[])){const seen=next.get(o.set_id);if(!seen||o.first_day<seen.first_day)next.set(o.set_id,o);}}
  catch(e){reportError(e,'Next dates could not be worked out. ');}
  upcoming=next;
}
/** Extra columns of the Holidays & dates off list ([heading, value]). */
export const dateSetColumns=():[string,(r:Row)=>Node|string][]=>[
  ['Entries',r=>String((r.entries||[]).length)],
  ['Next',r=>{const o=upcoming.get(r.id);return o?el('span',{class:'ds-next'},el('span',{},o.name),' ',occurrenceDates(o)):el('span',{class:'muted'},'—');}]];
/** What a set holds, in a line under its name ("Eid al-Fitr, National Day +3"). */
export const entrySummary=(r:Row)=>{const names=((r.entries||[]) as DateSetEntry[]).map(e=>e.name);return names.length?names.slice(0,3).join(', ')+(names.length>3?` +${names.length-3}`:''):'No entries yet';};

// ---- The set form's entries (catalogueForm › date_sets) ----
/** One row per named entry: name, how it repeats, its first and last day (the shared date range for Gregorian dates;
 *  Hijri year-month-day for a Hijri repeat) and, for a saved entry, its next dates as the server works them out. */
export function dateSetEntries(row?:Row){
  const body=el('div',{class:'ds-entries'}),rows:{read:()=>DateSetEntry;paint:()=>void}[]=[];
  const saved=new Map(((row?.entries||[]) as DateSetEntry[]).map(e=>[e.id!,e]));
  const next=new Map<string,ResolvedDateSetEntry>();let loaded=!row;
  const same=(a:DateSetEntry,b:DateSetEntry)=>a.name===b.name&&a.repeat===b.repeat&&a.first_day===b.first_day&&a.last_day===b.last_day&&!!a.last_day_of_month===!!b.last_day_of_month
    &&(a.start_year??null)===(b.start_year??null)&&!!a.fixed_duration===!!b.fixed_duration;
  function add(v?:DateSetEntry){
    const hijri0=v?.repeat==='yearly_hijri';
    const name=field('Entry name','text',v?.name||'',{required:true,maxlength:160,placeholder:'e.g. Eid al-Fitr'});
    const repeat=select('Repeats',REPEAT_CHOICES,v?.repeat||'yearly_gregorian');
    const gFirst=field('First day','date',v&&!hijri0?v.first_day:'',{required:true}),gLast=field('Last day','date',v&&!hijri0?v.last_day:'',{required:true});
    const gregorian=dateRange('Dates',gFirst,gLast);
    const hijri=(label:string,value:string)=>field(label,'text',value,{required:true,pattern:HIJRI_PATTERN,placeholder:'1447-10-01',inputmode:'numeric',maxlength:10,autocomplete:'off',title:'Hijri year-month-day, for example 1447-10-01'});
    const hFirst=hijri('First day (Hijri)',hijri0?v!.first_day:''),hLast=hijri('Last day (Hijri)',hijri0?v!.last_day:'');
    // Any repeat can end with its last day's month (a Hijri month has 29 or 30 days, February 28 or 29).
    const monthEnd=check('Until the end of the month',!!v?.last_day_of_month,'The entry ends on the final day of the last day’s month; a Hijri month’s length follows the Umm al-Qura calendar.');
    const words=el('small',{class:'hint ds-hijri-words','aria-live':'polite'}),when=el('p',{class:'hint ds-when','aria-live':'polite'});
    // Umm al-Qura months have 29 or 30 days: a 30th is not in every year (the server refuses a first day of 30).
    const day30=(f:typeof hFirst)=>/^\d{4}-\d{2}-30$/.test(f.input.value);
    inlineCheck(hFirst,()=>day30(hFirst)?'Not every Hijri month has a 30th; start on the 29th or earlier.':'');
    const lastCheck=inlineCheck(hLast,()=>day30(hLast)&&!monthEnd.input.checked?'Not every Hijri month has a 30th: tick Until the end of the month.':'');
    monthEnd.input.addEventListener('change',lastCheck);
    // An imported yearly event (iCalendar) starts in its first year and keeps its length: shown, and clearable, only
    // where an entry has them (a hand-made yearly entry repeats its month and day in every year).
    const imported=!!v&&(v.start_year!=null||!!v.fixed_duration);
    const fromFirst=check('Not before its first year',v?.start_year!=null,v?.start_year!=null?`No dates in the years before ${v.start_year}.`:'No dates in the years before the first day’s year.');
    const sameLength=check('Same number of days every year',!!v?.fixed_duration,'Each year lasts as many days as the first and last day here, even across 29 February.');
    const gBox=imported?el('div',{class:'span-2'},fromFirst.wrap,sameLength.wrap):null;
    const hBox=el('div',{class:'ds-hijri span-2'},el('div',{class:'mini-grid'},hFirst.wrap,hLast.wrap),words);
    const endBox=el('div',{class:'span-2'},monthEnd.wrap);
    const isHijri=()=>repeat.input.value==='yearly_hijri';
    const read=():DateSetEntry=>{const h=isHijri();return {...(v?.id?{id:v.id}:{}),name:name.input.value.trim(),repeat:repeat.input.value as Repeat,
      first_day:h?hFirst.input.value:gFirst.input.value,last_day:h?hLast.input.value:gLast.input.value,last_day_of_month:monthEnd.input.checked,
      ...(imported&&repeat.input.value==='yearly_gregorian'?{start_year:fromFirst.input.checked?(v?.start_year??(gFirst.input.value?Number(gFirst.input.value.slice(0,4)):null)):null,fixed_duration:sameLength.input.checked}:{}),
      // A year's adjustment is keyed by the repeat's own year (Hijri or Gregorian): it stays only while the repeat does.
      overrides:v&&repeat.input.value===(v.repeat||'once')?v.overrides||[]:[]};};
    const paint=()=>{
      const h=isHijri();gregorian.hidden=h;hBox.hidden=!h;
      if(gBox){gBox.hidden=repeat.input.value!=='yearly_gregorian';for(const i of [fromFirst.input,sameLength.input])i.disabled=gBox.hidden;}
      // A fixed length and a month end are alternatives: each disables the other while it is ticked.
      const fixed=!!gBox&&!gBox.hidden&&sameLength.input.checked;
      monthEnd.input.disabled=fixed;if(gBox&&!gBox.hidden)sameLength.input.disabled=monthEnd.input.checked;
      for(const i of [gFirst.input,gLast.input])i.disabled=h;for(const i of [hFirst.input,hLast.input])i.disabled=!h;
      words.textContent=h&&hijriWords(hFirst.input.value)?`${hijriWords(hFirst.input.value)} – ${monthEnd.input.checked?'end of '+(HIJRI_MONTHS[Number(hLast.input.value.slice(5,7))-1]||'the month'):hijriWords(hLast.input.value)||'…'}`:'';
      // Dates are the server's: a saved, unchanged entry shows its next dates; an edited one says when they are worked out.
      const before=v?.id?saved.get(v.id):undefined,o=v?.id?next.get(v.id):undefined;
      remove.setAttribute('aria-label',`Remove entry ${name.input.value.trim()||'without a name'}`);
      when.textContent=before&&same(before,read())?(!loaded?'Working out the next dates…':o?`Next: ${occurrenceText(o)}${o.adjusted?' · adjusted':''}`:'No date in the next two years'):h?'The Gregorian dates are worked out when you save.':'';
    };
    for(const n of [name.input,repeat.input,gFirst.input,gLast.input,hFirst.input,hLast.input,monthEnd.input,fromFirst.input,sameLength.input]){n.addEventListener('input',paint);n.addEventListener('change',paint);}
    name.wrap.classList.add('span-2');
    const node=el('fieldset',{class:'nested-card ds-entry'},el('legend',{},'Entry'),el('div',{class:'form-grid'},name.wrap,repeat.wrap,gregorian,...(gBox?[gBox]:[]),hBox,endBox),when);
    const item={read,paint};rows.push(item);
    const remove=button('Remove entry',()=>{rows.splice(rows.indexOf(item),1);node.remove();node.dispatchEvent(new Event('change',{bubbles:true}));body.dispatchEvent(new Event('change',{bubbles:true}));},'ghost small');
    node.append(remove);body.append(node);paint();
  }
  ((row?.entries||[]) as DateSetEntry[]).forEach(add);
  if(!row)add();
  if(row?.entries?.length){const today=departmentToday();
    void resolved(today,addDays(today,730),[row.id]).then(list=>{for(const o of list){const seen=next.get(o.entry_id);if(!seen||o.first_day<seen.first_day)next.set(o.entry_id,o);}},()=>undefined).finally(()=>{loaded=true;rows.forEach(r=>r.paint());});}
  const wrap=el('section',{class:'span-2 form-section'},el('h3',{},'Entries'),
    el('p',{},'Each entry is a named day or run of days. A yearly Hijri entry follows the Umm al-Qura calendar; adjust a year in Dates when the announced dates differ.'),
    body,button('Add entry',()=>add(),'secondary'));
  return {wrap,read:()=>rows.map(r=>r.read())};
}

// ---- Dates: one year's occurrences, adjusted one year at a time ----
/** The set's dates in a year (docked beside the list): each occurrence, marked when adjusted, with Adjust this year and
 *  Back to the computed dates for admins. Everyone else reads the dates. */
export function dateSetDates(ctx:Context,set:Row){
  const thisYear=Number(departmentToday().slice(0,4));let year=thisYear;
  // The set as last saved: a reply (an adjustment, its Undo) lands before the lists are read again.
  let latest:Row=setOf(ctx,set.id)||set;
  const fresh=()=>{const listed=setOf(ctx,set.id);return listed&&listed.revision>latest.revision?listed:latest;};
  let again:((e:Event)=>void)|null=null;
  dockPanel({key:'date-set-dates',title:'Dates',sub:labelOf(set),mark:rowMark(set.id),width:600,onClose:()=>{if(again)window.removeEventListener('rota-saved',again);again=null;},build:({body})=>{
    const years=Array.from({length:8},(_,i)=>thisYear-2+i).map(y=>({value:String(y),label:String(y)}));
    const pick=select('Year',years,String(year));pick.wrap.classList.add('ds-year');
    const out=el('div',{class:'ds-dates','aria-live':'polite'});
    pick.input.addEventListener('change',()=>{year=Number(pick.input.value);void load();});
    // Only the latest load draws: a slower reply for a year no longer chosen is dropped.
    let loads=0;
    const load=async()=>{
      const mine=++loads;
      out.replaceChildren(skeleton('table','Loading dates…'));
      let list:ResolvedDateSetEntry[];
      try{list=await resolved(`${year}-01-01`,`${year}-12-31`,[set.id]);}
      catch(e){if(body.isConnected&&mine===loads)out.replaceChildren(readFailed('the dates',e,()=>load()));return;}
      if(!body.isConnected||mine!==loads)return;
      const current=latest=fresh(),entries=new Map(((current.entries||[]) as DateSetEntry[]).map(e=>[e.id!,e]));
      const can=editable(ctx)&&current.active!==false;
      // Actions sit under the entry they change, so the table stays three columns wide on a phone too.
      const rows=list.sort((a,b)=>a.first_day.localeCompare(b.first_day)).map(o=>{const e=entries.get(o.entry_id),h=e?.repeat==='yearly_hijri';
        const acts=can&&e&&e.repeat!=='once'?el('span',{class:'ds-acts'},button('Adjust this year',()=>adjust(e,o),'secondary small',{'aria-label':`Adjust ${o.name} ${o.year}`}),
          o.adjusted?button('Back to the computed dates',()=>restore(e,o),'ghost small',{'aria-label':`Back to the computed dates for ${o.name} ${o.year}`}):null):null;
        return [el('span',{class:'ds-name'},el('strong',{},o.name),el('small',{class:'muted'},(e?SHORT[e.repeat||'once']:'')+(h?` · ${o.year} AH`:'')),o.adjusted?statusBadge('preview','Adjusted'):null,acts),
          occurrenceDates(o),String(dayCount(o.first_day,o.last_day))];});
      // A set switched off has no dates in force: say so rather than an empty year.
      out.replaceChildren(...(current.active===false?[note('This set is switched off: rules and opening hours do not follow its dates. Switch it on in the list to use them again.','warning')]
        :[table(['Entry','Dates','Days'],rows,`No dates in ${year}.`)]));
    };
    // An adjustment, its Undo or an edit of this set elsewhere draws the dates again.
    again=(e:Event)=>{if(!body.isConnected)return;
      const r=(e as CustomEvent).detail?.result,saved=[r?.record,...(r?.records||[])].find((x:Row|undefined)=>x?.id===set.id);
      if(saved){if(saved.revision>latest.revision)latest=saved;void load();}};
    window.addEventListener('rota-saved',again);
    body.append(el('div',{class:'ds-dates-head'},pick.wrap,el('p',{class:'hint'},'Dates are the department’s local days, first and last day included. Adjusting changes one year only.')),out);
    void load();
  }});
  /** A one-year adjustment (a moon-sighting shift): the replacement Gregorian dates, with a reason. */
  function adjust(e:DateSetEntry,o:ResolvedDateSetEntry){
    const first=field('First day','date',o.first_day,{required:true}),last=field('Last day','date',o.last_day,{required:true});
    const intro=el('div',{class:'form-grid'},dateRange('Dates',first,last),el('p',{class:'hint span-2'},`Computed: ${occurrenceText(o)}${o.adjusted?' (already adjusted)':''}. Other years keep their own dates.`));
    confirmAction(`Adjust ${o.name} ${o.year}${e.repeat==='yearly_hijri'?' AH':''}`,'Only this year changes. Sessions already in a rota are checked again.',async reason=>{
      if(last.input.value<first.input.value)throw new Error('Choose a last day on or after the first day.');
      const current=fresh();
      await command('date_set.adjust',{entry_id:e.id,year:o.year,override:{year:o.year,first_day:first.input.value,last_day:last.input.value},reason},current.id,current.revision);
      notify(`${o.name} adjusted for ${o.year}.`);await ctx.refresh();},'Save adjustment',intro);
  }
  function restore(e:DateSetEntry,o:ResolvedDateSetEntry){
    confirmAction(`Back to the computed dates for ${o.name} ${o.year}`,'The adjustment for this year is removed; the calendar’s dates apply again.',async reason=>{
      const current=fresh();
      await command('date_set.adjust',{entry_id:e.id,year:o.year,override:null,reason},current.id,current.revision);
      notify(`${o.name} ${o.year} is back to the computed dates.`);await ctx.refresh();},'Use computed dates');
  }
}

// ---- Import entries and Copy to next year ----
/** Append entries from a CSV (name, first_day, last_day, optional repeat and last_day_of_month) or an iCalendar file of
 *  all-day events. Nothing is saved unless every entry is valid. */
export function importDateSet(ctx:Context,set:Row){
  const file=field('File','file','',{required:true,accept:'.csv,.ics,.ical,text/csv,text/calendar'});
  const format=select('Format',[{value:'csv',label:'CSV'},{value:'ical',label:'iCalendar (.ics)'}],'csv');
  const input=file.input as HTMLInputElement;
  input.addEventListener('change',()=>{const n=input.files?.[0]?.name.toLowerCase()||'';if(/\.(ics|ical)$/.test(n))format.input.value='ical';else if(n.endsWith('.csv'))format.input.value='csv';format.input.dispatchEvent(new Event('change'));});
  const intro=el('div',{class:'form-grid'},file.wrap,format.wrap,el('p',{class:'hint span-2'},'CSV: a heading row with name, first_day and last_day (YYYY-MM-DD), and optionally repeat (once, yearly_gregorian, yearly_hijri) and last_day_of_month (true or false). iCalendar: all-day events; a plain yearly repeat becomes Every year (Gregorian). New entries are added; existing ones stay.'));
  confirmAction(`Import entries into ${labelOf(set)}`,'Every entry is checked first; nothing is added if one is not valid.',async reason=>{
    const chosen=input.files?.[0];if(!chosen)throw new Error('Choose a file.');
    if(chosen.size>1024*1024)throw new Error('Choose a file of at most 1 MB.');
    const current=setOf(ctx,set.id)||set,before=(current.entries||[]).length;
    const result=await command('date_set.import',{format:format.input.value,content:await chosen.text(),reason},current.id,current.revision);
    notify(`${plural((result.record?.entries||[]).length-before,'entry','entries')} imported into ${isolate(labelOf(current))}.`);await ctx.refresh();},'Import',intro);
}
/** A separate set for the next year: once-only dates move one year on; yearly entries are not copied because they
 *  already repeat in the original set, which stays as it is. */
export function copyDateSet(ctx:Context,set:Row){
  const thisYear=Number(departmentToday().slice(0,4));
  const year=select('Copy the dates of',Array.from({length:5},(_,i)=>thisYear-1+i).map(y=>({value:String(y),label:`${y} → ${y+1}`})),String(thisYear));
  const name=field('Name of the new set (optional)','text','',{maxlength:160});
  const hint=()=>{name.input.placeholder=`${labelOf(set)} ${Number(year.input.value)+1}`;};year.input.addEventListener('change',hint);hint();
  confirmAction(`Copy ${labelOf(set)} to next year`,'Creates a new set with that year’s once-only dates moved one year on. Yearly entries already repeat, so they are not copied.',async reason=>{
    const current=setOf(ctx,set.id)||set;
    const result=await command('date_set.copy_next_year',{year:Number(year.input.value),...(name.input.value.trim()?{name:name.input.value.trim()}:{}),reason},current.id,current.revision);
    notify(`${isolate(labelOf(result.record))} created.`);await ctx.refresh();},'Copy',el('div',{class:'form-grid'},year.wrap,name.wrap));
}

// ---- Markers on the Schedule's day headers ----
/** The named dates on a day, quiet and in words (colour is never the only signal): one chip per entry, the set in its
 *  tooltip. */
export function dateMarks(list:readonly DateSetMarker[]|undefined):HTMLElement|null{
  if(!list?.length)return null;
  return el('span',{class:'date-marks'},...list.map(m=>badge(m.name,'','date-mark',{tip:`${m.name} · ${m.set_name}`})));
}
/** The same in an accessible name ("Eid al-Fitr, National Day"). */
export const dateMarkWords=(list:readonly DateSetMarker[]|undefined)=>(list||[]).map(m=>m.name).join(', ');
