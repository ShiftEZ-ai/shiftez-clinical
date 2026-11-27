import {phoneQuery,el,icon,iconButton,button,popover,notify,weekOrder,departmentToday,formatDay,WEEKDAYS,WEEKEND_DAYS,pinName,type Field} from './dom.js';
import {sheetHead} from './picker.js';

/** The one date picker, used by every date field through field(label,'date') and dateRange().
 *  A text input you can type in (1 Oct 2026, 01/10/2026 or 2026-10-01) shows "Thu 1 Oct 2026"; its.value reads and
 *  writes ISO yyyy-mm-dd, so forms, the API and tests keep ISO. A calendar button opens a dialog with a grid of day
 *  buttons (WAI-ARIA APG date picker dialog): arrows move by day/week, Home/End to the week's ends, PageUp/PageDown by
 *  month (with Shift by year), Enter picks, Escape closes; focus goes back to the field. Weeks start on the department's
 *  week start (/api/me), weekends are WEEKEND_DAYS, today is the department's today. min/max (a day, or on a date and
 *  time a local "yyyy-mm-ddTHH:MM", checked to the minute) and `disabled` days cannot be picked or typed. `maxDays`: the longest range the caller accepts (longer quick ranges are not offered;
 *  Use waits for a shorter range). */
export type DateOptions={disabled?:(day:string)=>boolean;maxDays?:number;
  /** Several separate days (dateList): click toggles a day, Shift+click adds every day from the last one clicked. */
  multi?:{days:string[];done:(days:string[])=>void}};

const MONTHS=['January','February','March','April','May','June','July','August','September','October','November','December'];
const utc=(d:string)=>new Date(d+'T12:00:00Z'),iso=(x:Date)=>x.toISOString().slice(0,10);
export const shiftDay=(d:string,n:number)=>{const x=utc(d);x.setUTCDate(x.getUTCDate()+n);return iso(x);};
const monthStart=(d:string)=>d.slice(0,8)+'01';
/** Same day n months on, or the month's last day when shorter (31 Jan + 1 month = 28 Feb). */
export const shiftMonth=(d:string,n:number)=>{const x=utc(d),y=x.getUTCFullYear(),m=x.getUTCMonth()+n,last=new Date(Date.UTC(y,m+1,0)).getUTCDate();return iso(new Date(Date.UTC(y,m,Math.min(x.getUTCDate(),last),12)));};
const monthEnd=(d:string)=>shiftDay(shiftMonth(monthStart(d),1),-1);
/** Server weekday number: Monday 0 … Sunday 6. */
const weekday=(d:string)=>(utc(d).getUTCDay()+6)%7;
const parts=(d:string)=>{const x=utc(d);return {day:x.getUTCDate(),month:MONTHS[x.getUTCMonth()],year:x.getUTCFullYear()};};
/** How a date shows in a field: "Thu 1 Oct 2026". */
export const showDay=(d:string)=>formatDay(d,'UTC',true);
const spoken=(d:string)=>{const p=parts(d);return `${WEEKDAYS[weekday(d)]} ${p.day} ${p.month} ${p.year}`;};
/** "1 – 31 Oct", "29 Nov – 26 Dec", "29 Dec 2026 – 4 Jan 2027"; one day reads "3 Oct" (X-13). */
export const shortRange=(a:string,b:string)=>{const x=parts(a),y=parts(b),m=(p:typeof x)=>p.month.slice(0,3);
  return a===b?`${x.day} ${m(x)}`:x.year!==y.year?`${x.day} ${m(x)} ${x.year} – ${y.day} ${m(y)} ${y.year}`:x.month!==y.month?`${x.day} ${m(x)} – ${y.day} ${m(y)}`:`${x.day} – ${y.day} ${m(y)}`;};

/** A typed date as ISO, or null: 2026-10-01, 1/10/2026 (day first), 1 Oct 2026, Thu 1 October 2026, Oct 1 2026. */
export function parseDay(text:string):string|null{
  const t=text.trim().replace(/,/g,' ').replace(/\s+/g,' ');let y=0,m=0,d=0,r:RegExpExecArray|null;
  if((r=/^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(t)))[y,m,d]=[+r[1],+r[2],+r[3]];
  else if((r=/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/.exec(t)))[d,m,y]=[+r[1],+r[2],+r[3]];
  else if((r=/^(?:[a-z]+\.? )?(\d{1,2}) ([a-z]{3,})\.? (\d{4})$/i.exec(t))){const name=r[2].toLowerCase();d=+r[1];y=+r[3];m=MONTHS.findIndex(n=>n.toLowerCase().startsWith(name))+1;}
  else if((r=/^(?:[a-z]+\.? )?([a-z]{3,})\.? (\d{1,2}) (\d{4})$/i.exec(t))){const name=r[1].toLowerCase();d=+r[2];y=+r[3];m=MONTHS.findIndex(n=>n.toLowerCase().startsWith(name))+1;}
  const x=new Date(Date.UTC(y,m-1,d,12));
  return m&&y>=1000&&x.getUTCMonth()===m-1&&x.getUTCDate()===d?iso(x):null;
}

/** The day a date field shows (and the calendar picks), whatever its .value means (see lastDayIncluded). */
const shown=new WeakMap<HTMLInputElement,{get:()=>string;set:(v:unknown)=>void;check:()=>void}>();
export const dayOf=(input:HTMLInputElement)=>shown.get(input)?.get()??input.value;
const setDayOf=(input:HTMLInputElement,v:string)=>{const s=shown.get(input);if(s)s.set(v);else input.value=v;};

/** The one conversion for end dates stored as the day after the last day (ends_on, not included): the field shows,
 *  takes and picks the last day included, while .value keeps reading and writing the stored end, so the API and the
 *  data mean the same as before. Use it on a date field (field(…,'date') or dateInput) and label it "… (included)". */
export function lastDayIncluded(input:HTMLInputElement):HTMLInputElement{
  const s=shown.get(input)!,stored=input.value;
  Object.defineProperty(input,'value',{configurable:true,get:()=>{const d=s.get();return d?shiftDay(d,1):'';},
    set:(v:unknown)=>{const d=parseDay(String(v??''));s.set(d?shiftDay(d,-1):v);}});
  input.value=stored;
  return input;
}

/** Compares a value with a bound at the precision both have: <0 before, 0 same, >0 after. */
const beyond=(v:string,bound:string)=>{const n=Math.min(v.length,bound.length),a=v.slice(0,n),b=bound.slice(0,n);return a<b?-1:a>b?1:0;};
const showBound=(b:string)=>showDay(b.slice(0,10))+(b.length>10?' '+b.slice(11,16):'');

/** Rules checked beside a date field (dom.ts inlineCheck), applied by the field's own check after its date checks. */
export const fieldRules=new WeakMap<HTMLInputElement,()=>string>();
/** Turns an input into a date field: returns the box holding it and its calendar button. */
export function dateInput(input:HTMLInputElement,options:DateOptions={}):HTMLElement{
  // Looked up here, not at import: Node imports these modules in unit tests without a DOM.
  const nativeValue=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!;
  const start=input.value,raw=()=>String(nativeValue.get!.call(input));
  input.type='text';input.autocomplete='off';input.spellcheck=false;input.classList.add('date-text');
  if(!input.placeholder)input.placeholder='e.g. 1 Oct 2026';
  // min/max are a day or, on a date-and-time field, a local "yyyy-mm-ddTHH:MM": the whole value (with its time, when
  // there is one) is compared to the minute; a day alone, or a day-only bound, compares days.
  // A rule beside the field (dom.ts inlineCheck) still applies once the date itself is fine: this check never clears it.
  const check=()=>{const t=raw().trim(),d=parseDay(t),full=String(input.value??''),v=d&&full.startsWith(d)?full:d||'';
    input.setCustomValidity(!t?fieldRules.get(input)?.()||'':!d?'Enter a date such as 1 Oct 2026 or 01/10/2026.':input.min&&beyond(v,input.min)<0?`Choose ${showBound(input.min)} or later.`
      :input.max&&beyond(v,input.max)>0?`Choose ${showBound(input.max)} or earlier.`:options.disabled?.(d)?`${showDay(d)} can't be chosen.`:fieldRules.get(input)?.()||'');};
  // shortcut: .value is redefined on this one input (reads ISO, writes ISO, shows "Thu 1 Oct 2026") so every caller
  // and the API keep ISO; a wrapper object would mean changing ~40 call sites. Playwright runs in its own JS world and
  // reads the shown text (to_have_value('Thu 1 Oct 2026')); fill() accepts any accepted format.
  let committed='';
  const day=()=>parseDay(raw())||'',setDay=(v:unknown)=>{const text=String(v??''),d=parseDay(text);nativeValue.set!.call(input,d?showDay(d):text);committed=d||'';check();};
  Object.defineProperty(input,'value',{configurable:true,get:day,set:setDay});
  shown.set(input,{get:day,set:setDay,check});
  input.value=start;
  new MutationObserver(check).observe(input,{attributes:true,attributeFilter:['min','max']});
  // Like a native date input, 'change' fires as soon as the typed text is a whole date or empty (not only on leaving the field);
  // leaving it then tidies the text to "Thu 1 Oct 2026" without a second 'change'.
  input.addEventListener('input',()=>{check();const v=day();if(v!==committed&&(v||!raw().trim())){committed=v;input.dispatchEvent(new Event('change',{bubbles:true}));}});
  input.addEventListener('change',e=>{check();if(!e.isTrusted)return;
    const d=parseDay(raw());if(d)nativeValue.set!.call(input,showDay(d));
    if((d||'')===committed)e.stopImmediatePropagation();else committed=d||'';});
  const open=iconButton('Choose date','calendar',()=>openCalendar(open,[input],options),{class:'bare date-open','aria-haspopup':'dialog','aria-expanded':'false'});
  // A date wrapped in its label (a row of From/Until) is named by the label's words, not also by this button.
  queueMicrotask(()=>pinName(input));
  return el('div',{class:'date-box'},input,open);
}

/** A typed time as 24-hour "HH:MM", or null: 8, 08, 830, 0830, 8:30, 08:30. */
export function parseTime(text:string):string|null{
  const r=/^(\d{1,2}):?(\d{2})?$/.exec(text.trim());if(!r)return null;
  const h=+r[1],m=+(r[2]||0);return h<24&&m<60?`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`:null;
}

/** The one time field (every former type=time, and the time part of a date and time): a 24-hour "HH:MM" text input
 *  that is a WAI-ARIA spinbutton: type 0830 or 8:30, ↑/↓ step 5 minutes (off the grid it snaps first: 08:03 ↑ 08:05),
 *  PageUp/PageDown an hour, wrapping at midnight. A step fires input and change like typing does. */
export function timeInput(time:HTMLInputElement):HTMLInputElement{
  time.type='text';time.classList.add('time-text');time.inputMode='numeric';time.autocomplete='off';time.maxLength=5;
  time.setAttribute('role','spinbutton');time.setAttribute('aria-valuemin','0');time.setAttribute('aria-valuemax','1439');if(!time.placeholder)time.placeholder='08:00';
  const minutes=()=>{const t=parseTime(time.value);return t?+t.slice(0,2)*60+ +t.slice(3):null;};
  const sync=()=>{const n=minutes();if(n===null){time.removeAttribute('aria-valuenow');time.removeAttribute('aria-valuetext');}else{time.setAttribute('aria-valuenow',String(n));time.setAttribute('aria-valuetext',parseTime(time.value)!);}
    time.setCustomValidity(!time.value.trim()||n!==null?'':'Enter a time such as 08:00 (24-hour).');};
  time.value=parseTime(time.value)||time.value;sync();
  time.addEventListener('keydown',e=>{
    const step=({ArrowUp:5,ArrowDown:-5,PageUp:60,PageDown:-60} as Record<string,number>)[e.key];if(!step||time.readOnly||time.disabled)return;e.preventDefault();
    const n=minutes()??480,next=((step>0?Math.floor(n/5)*5:Math.ceil(n/5)*5)+step+1440)%1440;
    time.value=`${String(Math.floor(next/60)).padStart(2,'0')}:${String(next%60).padStart(2,'0')}`;
    time.dispatchEvent(new Event('input',{bubbles:true}));time.dispatchEvent(new Event('change',{bubbles:true}));});
  time.addEventListener('input',sync);
  time.addEventListener('change',()=>{const t=parseTime(time.value);if(t)time.value=t;sync();});
  return time;
}

/** The one date-and-time field (every former datetime-local): the shared date field and a timeInput beside it named
 *  "<label> time". The labelled date input's .value reads and writes local ISO "yyyy-mm-ddTHH:MM" as datetime-local
 *  did, so callers and the API are unchanged; a whole "2026-10-05T09:00" typed or pasted into the date splits into
 *  both parts. disabled/readonly/required follow the date input; a time change fires input and change on it.
 *  The pair is one group named "<label>: date and time", so the label is announced for what the field holds. */
export function dateTimeInput(input:HTMLInputElement,label:string,options:DateOptions&{allDay?:'start'|'end'}={}):HTMLElement{
  const start=String(input.value||'');input.value=start.slice(0,10);
  const box=dateInput(input,options),date=shown.get(input)!;
  const time=timeInput(el('input',{'aria-label':label+' time',...(options.allDay?{placeholder:'All day','aria-description':'Leave the time blank for all day'}:{})}) as HTMLInputElement);
  const sync=()=>{
    const hasDate=!!date.get(),typedTime=time.value.trim();
    time.disabled=input.disabled;time.readOnly=input.readOnly;
    time.required=!options.allDay&&(input.required||hasDate);
    if(!typedTime||parseTime(typedTime))time.setCustomValidity(typedTime&&!hasDate?'Enter a date for this time.':
      hasDate&&!typedTime&&!options.allDay?'Enter a time such as 08:00 (24-hour).':'');
  };
  const changed=()=>{input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));};
  // Setting the value updates the time's own state (aria, validity) with a non-bubbling input, not the field's listeners.
  const set=(v:unknown)=>{const text=String(v??''),m=/^(.+?)[T ](\d{1,2}:\d{2})(?::\d{2}(?:\.\d+)?)?$/.exec(text.trim());
    if(m&&parseDay(m[1])){date.set(m[1]);time.value=parseTime(m[2])!;}else{date.set(text);if(!text)time.value='';}
    time.dispatchEvent(new Event('input'));date.check();};
  const get=()=>{const d=date.get(),t=parseTime(time.value);
    if(!d)return '';
    if(t)return `${d}T${t}`;
    if(!options.allDay||time.value.trim())return '';
    return `${options.allDay==='end'?shiftDay(d,1):d}T00:00`;
  };
  Object.defineProperty(input,'value',{configurable:true,get,set});
  time.addEventListener('input',sync);
  set(start);
  // A whole date and time typed or pasted into the date part (or a test's fill) lands in both parts.
  input.addEventListener('input',()=>{const raw=String(Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value')!.get!.call(input));
    const m=/^(.+?)[T ](\d{1,2}:\d{2})(?::\d{2})?$/.exec(raw.trim());if(m&&parseDay(m[1])){const before=get();set(raw);if(get()!==before)changed();}},true);
  input.addEventListener('input',sync);
  // The time's own events stop here; the field reports its change on the labelled input.
  time.addEventListener('input',e=>{if(!e.bubbles)return;e.stopPropagation();if(parseTime(time.value)||!time.value.trim())changed();});
  time.addEventListener('change',e=>{sync();e.stopPropagation();});
  new MutationObserver(sync).observe(input,{attributes:true,attributeFilter:['disabled','readonly','required']});
  return el('div',{class:'datetime-box',role:'group','aria-label':`${label}: date and time`},box,time);
}

/** One field for a pair of dates (first and last day, both included): two typeable inputs that keep their own
 *  accessible names, the number of days, and one calendar that picks the range. */
export function dateRange(label:string,first:Field,last:Field,options:DateOptions={}):HTMLElement{
  const id=`range-${Math.random().toString(36).slice(2)}`,count=el('span',{class:'date-count'});
  // Phone side panels show the range as one tap target ("Sun 4 Oct → Sat 10 Oct", the year only when it is not this
  // year's) that opens the calendar; the typed fields are for wider screens.
  const summary=el('button',{type:'button',class:'date-summary','aria-haspopup':'dialog'}),thisYear=parts(departmentToday()).year;
  const brief=(d:string)=>formatDay(d,'UTC',parts(d).year!==thisYear);
  // SW3-D-15: the last day is never before the first: said on Last day before anything is sent (its own min stays).
  const own=last.input.min;
  const sync=()=>{const a=dayOf(first.input),b=dayOf(last.input),floor=[own,a].filter(Boolean).sort().at(-1)||'';if(last.input.min!==floor)last.input.min=floor;const n=a&&b&&b>=a?Math.round((utc(b).getTime()-utc(a).getTime())/864e5)+1:0;count.textContent=n?`${n} day${n===1?'':'s'}`:'';
    summary.textContent=a||b?`${a?brief(a):'…'} → ${b?brief(b):'…'}`:'Choose dates';summary.setAttribute('aria-label',`${label}: ${a?spoken(a):'not set'} to ${b?spoken(b):'not set'}. Choose dates`);};
  for(const f of [first,last]){f.input.setAttribute('aria-label',f.wrap.querySelector('label')?.textContent||'');f.input.addEventListener('input',sync);f.input.addEventListener('change',sync);}
  sync();
  const open=iconButton('Choose dates','calendar',()=>openCalendar(open,[first.input,last.input],options),{class:'bare date-open','aria-haspopup':'dialog','aria-expanded':'false'});
  summary.addEventListener('click',()=>{if(!first.input.disabled&&!first.input.readOnly)open.click();});
  return el('div',{class:'field date-range span-2',role:'group','aria-labelledby':id},el('span',{class:'field-label',id},label),
    el('div',{class:'date-box range'},first.input,el('span',{class:'date-arrow','aria-hidden':'true'},'→'),last.input,summary,count,open));
}

function openCalendar(anchor:HTMLElement,inputs:HTMLInputElement[],options:DateOptions){
  const range=inputs.length===2,[a,b]=inputs,today=departmentToday(),multi=options.multi,chosen=new Set(multi?.days||[]);let lastPick='';
  // Days only here: a date-and-time bound ("…T14:00") keeps its own day pickable; the time is checked on the field.
  const min=a.min.slice(0,10),max=(b||a).max.slice(0,10),ok=(d:string)=>!(min&&d<min||max&&d>max||options.disabled?.(d));
  const fit=(d:string)=>min&&d<min?min:max&&d>max?max:d;
  let from=multi?'':dayOf(a),to=range?dayOf(b):'',hover='',focus=from||fit(today);
  // A field inside another popover (a filter panel) opens its calendar inline: one popover at a time.
  const inline=anchor.parentElement?.closest<HTMLElement>('.popover'),count=(range||multi)&&!inline&&!phoneQuery.matches?2:1;
  let view=monthStart(focus);
  const tooLong=(p:string,q:string)=>!!options.maxDays&&!!p&&!!q&&(utc(q).getTime()-utc(p).getTime())/864e5+1>options.maxDays;
  const ws=weekOrder()[0],thisWeek=shiftDay(today,-((weekday(today)-ws+7)%7)),nextMonth=shiftMonth(monthStart(today),1);
  const presets=range?([['Rest of this month',today,monthEnd(today)],['Next week',shiftDay(thisWeek,7),shiftDay(thisWeek,13)],
    ['Next 2 weeks',shiftDay(thisWeek,7),shiftDay(thisWeek,20)],['Next month',nextMonth,monthEnd(nextMonth)],['Next 3 months',nextMonth,monthEnd(shiftMonth(nextMonth,2))]] as [string,string,string][]).filter(([,p,q])=>!tooLong(p,q)):[];
  const grid=el('div',{class:'dp-months'});
  const use=el('button',{type:'button',class:'button primary small'},'Use') as HTMLButtonElement;
  const presetButtons=presets.map(([name,p,q])=>{const btn=el('button',{type:'button',class:'button ghost small dp-preset',disabled:!ok(p)||!ok(q)},name);
    btn.addEventListener('click',()=>{from=p;to=q;focus=p;show(p);});return btn;});
  const custom=el('button',{type:'button',class:'button ghost small dp-preset'},'Custom');custom.addEventListener('click',()=>day(focus)?.focus());
  const day=(d:string)=>grid.querySelector<HTMLButtonElement>(`[data-day="${d}"]`);
  const month=(first:string)=>{
    const p=parts(first),lead=(weekday(first)-ws+7)%7,last=monthEnd(first),cells:(string|null)[]=Array(lead).fill(null);
    for(let d=first;d<=last;d=shiftDay(d,1))cells.push(d);
    const rows=[];for(let i=0;i<cells.length;i+=7)rows.push(el('tr',{},...cells.slice(i,i+7).map(d=>el('td',{},d?el('button',{type:'button',
      class:'dp-day'+(WEEKEND_DAYS.includes(weekday(d))?' weekend':'')+(d===today?' today':''),'data-day':d,tabindex:-1,...(ok(d)?{}:{'aria-disabled':'true'}),...(d===today?{'aria-current':'date'}:{})},String(parts(d).day)):null))));
    return el('table',{class:'dp-month'},el('caption',{},`${p.month} ${p.year}`),
      el('thead',{},el('tr',{},...weekOrder().map(w=>el('th',{scope:'col',abbr:WEEKDAYS[w]},WEEKDAYS[w].slice(0,2))))),el('tbody',{},...rows));
  };
  const paint=()=>{
    const hi=to||(range&&from&&hover>=from?hover:'');
    for(const btn of grid.querySelectorAll<HTMLButtonElement>('.dp-day')){
      const d=btn.dataset.day!,edge=multi?chosen.has(d):d===from||d===to,inside=!!(from&&hi&&d>from&&d<hi);
      btn.classList.toggle('picked',edge);btn.classList.toggle('between',inside);btn.classList.toggle('preview',inside&&!to);
      btn.setAttribute('aria-pressed',String(edge));btn.tabIndex=d===focus?0:-1;
      // The weekend shading and today's outline are said in each day's name, so no legend is needed under the grid (X-12).
      btn.setAttribute('aria-label',spoken(d)+(btn.classList.contains('weekend')?', weekend':'')+(d===today?', today':'')+(range&&d===from?', first day':'')+(range&&d===to?', last day':'')+(inside&&to||multi&&edge?', selected':''));
    }
    if(multi){use.disabled=false;use.textContent=`Use ${chosen.size} date${chosen.size===1?'':'s'}`;return;}
    if(!range)return;
    const long=tooLong(from,to),done=!!from&&(!!to||!b.required)&&!long;use.disabled=!done;
    use.textContent=long?`Up to ${options.maxDays} days`:!done?'Use':to?`Use ${shortRange(from,to)}`:`Use from ${showDay(from)}`;
    let matched=false;presetButtons.forEach((btn,i)=>{const on=from===presets[i][1]&&to===presets[i][2];matched||=on;btn.setAttribute('aria-pressed',String(on));});
    custom.setAttribute('aria-pressed',String(!matched));
  };
  /** Shows the months holding `d` (keeping the view when it already does). */
  const show=(d:string)=>{const lastShown=monthEnd(shiftMonth(view,count-1));
    if(d<view)view=monthStart(d);else if(d>lastShown)view=shiftMonth(monthStart(d),1-count);
    grid.replaceChildren(...Array.from({length:count},(_,i)=>month(shiftMonth(view,i))));paint();};
  const move=(d:string)=>{focus=d;show(d);day(d)?.focus();};
  let close=()=>{};
  const apply=(values:string[])=>{inputs.forEach((input,i)=>{setDayOf(input,values[i]||'');input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));});close();a.focus();};
  grid.addEventListener('keydown',e=>{
    const d=(e.target as HTMLElement).closest<HTMLElement>('.dp-day')?.dataset.day;if(!d||e.altKey||e.ctrlKey||e.metaKey)return;
    const w=(weekday(d)-ws+7)%7,years=e.shiftKey?12:1;
    const next:Record<string,string>={ArrowLeft:shiftDay(d,-1),ArrowRight:shiftDay(d,1),ArrowUp:shiftDay(d,-7),ArrowDown:shiftDay(d,7),Home:shiftDay(d,-w),End:shiftDay(d,6-w),PageUp:shiftMonth(d,-years),PageDown:shiftMonth(d,years)};
    if(!(e.key in next))return;e.preventDefault();e.stopPropagation();move(next[e.key]);
  });
  grid.addEventListener('click',e=>{
    const btn=(e.target as HTMLElement).closest<HTMLElement>('.dp-day');if(!btn||btn.getAttribute('aria-disabled')==='true')return;
    const d=btn.dataset.day!;focus=d;
    if(multi){const on=!chosen.has(d);
      if((e as MouseEvent).shiftKey&&lastPick){const [p,q]=lastPick<d?[lastPick,d]:[d,lastPick];for(let x=p;x<=q;x=shiftDay(x,1))if(ok(x))chosen.add(x);}
      else if(on)chosen.add(d);else chosen.delete(d);
      lastPick=d;paint();return;}
    if(!range){apply([d]);return;}
    if(!from||to||d<from){from=d;to='';}else to=d;
    hover='';paint();if(to)use.focus();
  });
  const preview=(e:Event)=>{const d=(e.target as HTMLElement).closest<HTMLElement>('.dp-day')?.dataset.day;if(range&&from&&!to&&d&&d!==hover){hover=d;paint();}};
  grid.addEventListener('mouseover',preview);grid.addEventListener('focusin',preview);
  use.addEventListener('click',()=>{if(multi){multi.done([...chosen].sort());close();anchor.focus();return;}apply([from,to]);});
  const clear=range||multi||!a.required?el('button',{type:'button',class:'button ghost small'},'Clear'):null;
  clear?.addEventListener('click',()=>{if(multi){chosen.clear();paint();}else if(range){from=to=hover='';paint();}else apply(['']);});
  let panel:HTMLElement|null=null;
  const build=(node:HTMLElement)=>{node.dataset.lookup='';  // picking is not an edit until applied
    node.append(el('div',{class:'dp'+(range?' range':'')},
      range?el('div',{class:'dp-presets',role:'group','aria-label':'Quick ranges'},...presetButtons,custom):null,
      el('div',{class:'dp-main'},
        el('div',{class:'dp-nav'},iconButton('Show previous month','chevronLeft',()=>{view=shiftMonth(view,-1);show(view);}),el('span',{class:'grow'}),iconButton('Show next month','chevronRight',()=>{view=shiftMonth(view,1);show(view);})),
        grid,
        el('div',{class:'dp-foot'},multi?el('span',{class:'hint'},'Shift+click adds every day since the last one'):null,el('span',{class:'grow'}),clear,range||multi?use:null))));
    show(focus);
  };
  const label=range||multi?'Choose dates':'Choose a date',box=anchor.closest('.date-box')||anchor;
  if(inline){
    const shown=box.nextElementSibling;
    if(shown?.classList.contains('date-inline')){shown.remove();anchor.setAttribute('aria-expanded','false');return;}
    const node=el('div',{class:'date-inline',role:'group','aria-label':label});
    close=()=>{node.remove();anchor.setAttribute('aria-expanded','false');};
    build(node);box.after(node);anchor.setAttribute('aria-expanded','true');day(focus)?.focus();return;
  }
  // On a phone the calendar is a bottom sheet headed by the field it fills, with a Close (X-15). The shared popover's
  // tether draws the arrow on wider screens (X-14).
  const field=(anchor.closest('.field')?.querySelector('.field-label,label')?.textContent||'').trim()||label;
  popover(anchor,(node,done)=>{panel=node;close=done;node.classList.add('date-pop');if(phoneQuery.matches)node.append(sheetHead(field,()=>{done();anchor.focus();}));build(node);},{label,placement:'below-end'});
  if(!panel)return;
  day(focus)?.focus();
}

/** Every date in pasted text, in any common format: one per line, or separated by commas or semicolons (2026-10-12,
 *  12 Oct 2026, 12/10/2026, Oct 12, 2026), and ranges ("1 Oct 2026 – 5 Oct 2026", "… to …"). What could not be read is
 *  returned too, so the person can see it. */
export function parseDates(text:string):{days:string[];bad:string[]}{
  const days=new Set<string>(),bad:string[]=[];
  const one=(t:string)=>{const range=/^(.+?)\s+(?:–|—|-|to)\s+(.+)$/i.exec(t),p=range&&parseDay(range[1]),q=range&&parseDay(range[2]);
    if(p&&q){const [x,y]=p<q?[p,q]:[q,p];for(let d=x;d<=y&&days.size<=366;d=shiftDay(d,1))days.add(d);return true;}
    const d=parseDay(t);if(d)days.add(d);return !!d;};
  for(const line of text.split(/\r?\n|;/).map(x=>x.trim()).filter(Boolean)){
    if(one(line))continue;
    for(const piece of line.split(/,|\t/).map(x=>x.trim()).filter(Boolean))if(!one(piece))bad.push(piece);
  }
  return {days:[...days].sort(),bad};
}

/** The one list of separate dates (excluded dates, closed days…): chips removed one by one, Add dates (the shared
 *  calendar: several days, Shift+click for a range), Copy dates (ISO, one per line) and Paste dates (any common format),
 *  so a set moves from one record to another; Ctrl+V on the list adds pasted dates too. A change fires 'input' and
 *  'change' on a hidden field inside it, so forms, impact checks and the unsaved-changes guard see it like any field;
 *  the review summary reads its data-review-value. */
export function dateList(label:string,initial:string[]=[],options:{hint?:string;max?:number}={}){
  const max=options.max??366;let days=[...new Set(initial)].sort();
  const id=`dates-${Math.random().toString(36).slice(2)}`,holder=el('input',{type:'hidden'}) as HTMLInputElement;
  const list=el('ul',{class:'date-chips',tabindex:0,'aria-label':`${label}. Paste dates here with Ctrl+V`});
  const count=el('span',{class:'hint date-list-count'});
  const wrap=el('div',{class:'field date-list span-2',role:'group','aria-labelledby':id,'data-review-label':label},
    el('span',{class:'field-label',id},label),list);
  const changed=()=>{draw();holder.value=days.join(',');for(const t of ['input','change'])holder.dispatchEvent(new Event(t,{bubbles:true}));};
  const set=(next:string[])=>{const before=days.join();days=[...new Set(next)].sort();
    if(days.length>max){notify(`Up to ${max} dates. The first ${max} were kept.`,'warning');days=days.slice(0,max);}if(days.join()!==before)changed();};
  function draw(){
    wrap.dataset.reviewValue=days.length?days.map(showDay).join(', '):'None';count.textContent=days.length?`${days.length} date${days.length===1?'':'s'}`:'';
    list.replaceChildren(...(days.length?days.map(d=>el('li',{class:'date-chip'},el('span',{},showDay(d)),
      iconButton(`Remove ${spoken(d)}`,'close',()=>{set(days.filter(x=>x!==d));(list.querySelector<HTMLElement>('button')||add).focus();},{class:'bare'}))):[el('li',{class:'muted date-none'},'No dates')]));
  }
  const add=button('Add dates',()=>openCalendar(add,[el('input') as HTMLInputElement],{multi:{days,done:set}}),'secondary small',{'aria-haspopup':'dialog','aria-expanded':'false'});
  add.prepend(icon('calendar'));
  const copy=button('Copy dates',async()=>{
    if(!days.length){notify('There are no dates to copy.','warning');return;}
    try{await navigator.clipboard.writeText(days.join('\n'));notify(`${days.length} date${days.length===1?'':'s'} copied. Paste them into another date list.`);}
    catch{notify('The browser did not allow copying. Open Paste dates and copy them from there.','warning');}},'ghost small');
  copy.prepend(icon('copy'));
  const paste=button('Paste dates',()=>popover(paste,(panel,close)=>{
    const area=el('textarea',{rows:5,class:'date-paste','aria-label':'Dates to use','placeholder':'2026-10-12\n12 Oct 2026\n1 Nov 2026 – 5 Nov 2026'}) as HTMLTextAreaElement;
    area.value=days.join('\n');
    const result=el('p',{class:'hint','aria-live':'polite'}),use=button('Use these dates',()=>{set(parseDates(area.value).days);close();paste.focus();},'primary small');
    const read=()=>{const r=parseDates(area.value);result.textContent=`${r.days.length} date${r.days.length===1?' replaces':'s replace'} the list`+(r.bad.length?` · not read: ${r.bad.slice(0,5).join(', ')}${r.bad.length>5?' …':''}`:'');result.classList.toggle('warn',!!r.bad.length);};
    area.addEventListener('input',read);read();
    // The placeholder shows the formats; the result line says what will be used, and that it replaces the list (X-12).
    panel.append(el('div',{class:'dt-pop-label'},label),area,result,
      el('div',{class:'actions'},button('Cancel',()=>{close();paste.focus();},'ghost small'),use));
    setTimeout(()=>{area.focus();area.select();},0);},{label:'Paste dates'}),'ghost small',{'aria-haspopup':'dialog'});
  paste.prepend(icon('clipboard'));
  list.addEventListener('paste',e=>{const r=parseDates(e.clipboardData?.getData('text')||'');if(!r.days.length)return;e.preventDefault();
    set([...days,...r.days]);notify(`${r.days.length} date${r.days.length===1?'':'s'} added${r.bad.length?`; ${r.bad.length} could not be read`:''}.`,r.bad.length?'warning':'success');});
  wrap.append(el('div',{class:'date-list-actions'},add,copy,paste,count),holder);
  if(options.hint)wrap.append(el('small',{class:'hint'},options.hint));
  draw();
  return {wrap,values:()=>[...days],set};
}
