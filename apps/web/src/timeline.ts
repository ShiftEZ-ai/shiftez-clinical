import {el,icon,groupToggle,check,select,hoverCard,popover,WEEKEND_DAYS,weekOrder,departmentToday,PALETTE,plural,type Child,type IconName,type Legend} from './dom.js';
import {spokenName} from './person_label.js';
import {observeSize} from './loading.js';
import {segment} from './view_settings.js';
import {markOpen} from './side_panel.js';
import {issueMarker} from './issue_marker.js';
import {enableDrag,focusOwed,type DragSpec} from './timeline_drag.js';

/**
 * The Timeline layout of the shared collection system (Table · Cards · Timeline; approved mockup 21 Oct 2026): one row
 * per record, its dated items as bars across a date scale. Pages keep the toolbar (period, Search, Filter, Group, Sort,
 * View settings, Saved views) and their data; they map it to rows of bars, and the layers (what a row shows) come from
 * them, never from here. Draws:
 * - a sticky header: two scale rows by zoom (months/weeks, or quarters/months) and Today on its own row;
 * - weekend shading at week zoom; group rows with a count, a summary and collapse;
 * - lanes: overlapping bars stack; a bar shows its name, a short name or only its mark as its width allows, with a
 *   minimum width; time away too narrow for its words shows them beside it;
 * - conflict zones with their badge on a strip at the top of the row (red with an alert, or gold with a clock);
 * - an optional duty-density heat strip, one cell per day; hover cards (pointers that hover only);
 * - only the rows in view (virtualised: 150 people × a year stays light).
 * Phones get timelineAgenda instead: a person's own items as an agenda, or who is away, elsewhere and starting soon;
 * one-line rows stay a timeline whose months swipe under the sticky names.
 */
export type Zoom='week'|'month'|'quarter'|'year';
/** How a bar is drawn. Hues are duty-type hues; red is only ever a conflict, with its icon and words. */
export type BarStyle='block'|'outline'|'leave'|'protected'|'offer'|'pending'|'plan'|'rail'|'milestone'|'out';
export type Bar={id:string;start:string;/** The day after the last. */end:string;label:string;short?:string;style:BarStyle;hue?:string;
  /** ↗ an external rotation (away from the department). */external?:boolean;meta?:string;
  /** What a screen reader says (the label, its dates, its state). */aria:string;
  open?:()=>void;card?:()=>Node[];marks?:IconName[];
  /** Not published yet (a draft or the usual plan): the same mark, hatched (hatching means only this). */draft?:boolean;
  /** Its whole name where the bar shows a shorter one (the phone's list of a person's stretches). */full?:string;
  /** One-line rows: a leave type's short code, kept when the label does not fit (AL, Sick, Conf). */code?:string;
  /** What this bar puts in the view's key (`TimelineSpec.legend`): its mark's swatch classes and word; `code` makes the
   *  swatch that code (in `mark`'s classes when given, else a code chip), a short label whose full name the key gives (entered only while the bar shows words).
   *  `order`: the dom.ts legend's (colours under 1, marks 1–2, codes 2). */keys?:{key:string;mark:string;words:string;order:number;code?:string}[]};
export type Zone={start:string;end:string;label:string;tone:'error'|'approval'};
export type TimelineRow={id:string;name:string;
  /** The name as shown (a person's label); `name` stays the full name for initials, headings and what is read out. */label?:string;sub?:string;flag?:string;bars:Bar[];zones?:Zone[];
  /** Short marks after the name (EXT: an external rotator; a notes count), each with its words; `key`: its legend entry (a mark tag). */tags?:{text:string;tip:string;icon?:IconName;class?:string;key?:NonNullable<Bar['keys']>[number]}[];
  /** Duty density per day (yyyy-mm-dd → 0–3); `errors` are days that conflict (a duty on leave). */heat?:{levels:Record<string,number>;errors?:string[]};
  /** What the row draws besides its bars (its heat strip) for the view's key, as `Bar.keys`. */keys?:Bar['keys'];
  open?:()=>void};
export type TimelineGroup={key:string;name:string;rows:TimelineRow[];summary?:{text:string;tone?:'error'|'warn'}[]};
export type TimelineSpec={label:string;start:string;end:string;zoom:Zoom;groups:TimelineGroup[];corner?:Child;
  weekends?:boolean;density?:'comfortable'|'compact';collapsed?:string[];toggle?:(key:string)=>void;today?:string;
  /** What a bar's words are (View settings › Bar label); "Plan rotations" bars keep theirs. */barLabel?:BarLabel;
  /** Planning by hand (timeline_drag.ts): which bars move, what a change would do, and how it is applied. */drag?:DragSpec;
  /** One line per row (People › Where is everyone): bars never overlap, a bar too narrow for its words is drawn thin
   *  without words (its card has them), and arrow keys move between bars. */oneLine?:boolean;
  /** One line: a bar too narrow for any word folds into the bar it touches (that bar's card lists it) instead of being
   *  drawn thin; one with no neighbour is left out (View settings › Short stretches off). */fold?:boolean;
  /** The view's key (dom.ts `legend`, the one key component): filled from the bars as they are laid out, so it lists
   *  only what this view and range draw (collapsed groups add nothing). */legend?:Legend};
/** A bar's words: its name and length (the default), its name only, or its dates. */
export type BarLabel='name_length'|'name'|'dates';
export const BAR_LABELS:[BarLabel,string][]=[['name_length','Name and length'],['name','Name'],['dates','Dates']];

/** The hues a timeline draws data in: duty-type hues, never coral (data is never red; red is only a conflict). */
export const HUES=PALETTE.filter(c=>c!=='coral');
/** Named by the span they show reviewed. */
export const ZOOMS:[Zoom,string][]=[['week','3 months'],['month','6 months'],['quarter','9 months'],['year','12 months']];
/** What each zoom shows (its window) and where a dragged bar snaps. Week: 13 weeks by week, weekends shaded;
 *  Month: 6 months by week; Quarter: 9 months by month; Year: 12 months by month. */
const SHOWS:Record<Zoom,{weeks?:number;months?:number;snap:'day'|'week'|'month'}>={week:{weeks:13,snap:'day'},month:{months:6,snap:'week'},quarter:{months:9,snap:'week'},year:{months:12,snap:'month'}};
export const snapOf=(z:Zoom)=>SHOWS[z].snap;

export const dayNumber=(day:string)=>Math.round(Date.parse(day.slice(0,10)+'T00:00:00Z')/864e5);
export const dayOf=(n:number)=>new Date(n*864e5).toISOString().slice(0,10);
export const plusDays=(day:string,n:number)=>dayOf(dayNumber(day)+n);
export const plusMonths=(day:string,n:number)=>{const [y,m]=day.split('-').map(Number),t=y*12+m-1+n;return `${Math.floor(t/12)}-${String(t%12+1).padStart(2,'0')}-01`;};
/** Server weekday numbers: Monday 0 … Sunday 6. */
const weekday=(day:string)=>(new Date(day+'T00:00:00Z').getUTCDay()+6)%7;
const fmt=(day:string,o:Intl.DateTimeFormatOptions)=>new Intl.DateTimeFormat('en-GB',{...o,timeZone:'UTC'}).format(new Date(day+'T12:00:00Z')).replace('Sept','Sep');  // three-letter months everywhere
export const shortDay=(day:string)=>fmt(day,{day:'numeric',month:'short'});
/** "1 Oct – 30 Nov 2026" (the year once when both share it), from a start and the day after the last. */
export const spanWords=(a:string,b:string)=>{const last=plusDays(b,-1);return `${shortDay(a)}${a.slice(0,4)===last.slice(0,4)?'':' '+a.slice(0,4)} – ${shortDay(last)} ${last.slice(0,4)}`;};
export const lengthWords=(a:string,b:string)=>{const d=dayNumber(b)-dayNumber(a);return d>=14?`${Math.round(d/7)} wk`:plural(d,'day');};

/** The first day of the department week (its first weekday) that holds a day. */
export const weekStartOf=(day:string)=>plusDays(day,-((weekday(day)-weekOrder()[0]+7)%7));
/** The window a zoom shows from a day: weeks start on the department's first weekday, months on the 1st. */
export function timelineWindow(zoom:Zoom,from:string){
  const s=SHOWS[zoom];
  const start=s.weeks?weekStartOf(from):from.slice(0,7)+'-01';
  const end=s.weeks?plusDays(start,s.weeks*7):plusMonths(start,s.months!);
  const last=plusDays(end,-1);
  const label=s.weeks?`${fmt(start,{day:'numeric',month:'short',year:'numeric'})} – ${fmt(last,{day:'numeric',month:'short',year:'numeric'})}`
    :`${fmt(start,{month:'short',year:'numeric'})} – ${fmt(last,{month:'short',year:'numeric'})}`;
  /** Previous or next: a week at week zoom, a month otherwise. */
  const step=(n:number)=>s.weeks?plusDays(start,7*n):plusMonths(start,n);
  return {start,end,label,step};
}

/** Short name for a narrow bar: the initials of a several-word name ("Urgent care" → UC); one word stays whole (it
 *  ends in an ellipsis where it does not fit). */
export const shortName=(name:string)=>{const w=name.split(/[\s·]+/).filter(x=>/^\p{L}/u.test(x));return w.length>1?w.slice(0,3).map(x=>x[0].toUpperCase()).join(''):name;};
const canHover=()=>typeof matchMedia==='function'&&matchMedia('(hover: hover) and (pointer: fine)').matches;
const ICONS:Partial<Record<BarStyle,IconName>>={leave:'plane',protected:'book',pending:'clock',plan:'plus'};
const AWAY:BarStyle[]=['leave','protected'];

type Key=NonNullable<Bar['keys']>[number];
/** A key entry's swatch: its code as a chip, else the mark's classes as drawn. */
const swatchOf=(k:Key)=>k.code?el('span',{class:k.mark||'code-chip','aria-hidden':'true'},k.code):el('span',{class:'tl-swatch '+k.mark,'aria-hidden':'true'});
/** A conflict zone (and a duty conflict on the density strip) or a waiting approval: its key entry. */
const ZONE_KEYS:Record<Zone['tone'],Key>={error:{key:'zone:error',mark:'tl-zone-key',words:'Conflict',order:1.8},approval:{key:'zone:approval',mark:'tl-zone-key approval',words:'Needs approval',order:1.81}};
/** Every key entry a row draws: its own (heat), its zones', and its bars' (a code only where its words are drawn). */
const rowKeys=(r:TimelineRow,lay:Layout):Key[]=>[...(r.keys||[]),...(r.tags||[]).flatMap(t=>t.key?[t.key]:[]),...(r.zones||[]).map(z=>ZONE_KEYS[z.tone]),...(r.heat?.errors?.length?[ZONE_KEYS.error]:[]),
  ...lay.placed.flatMap(p=>(p.bar.keys||[]).filter(k=>!k.code||p.mode!=='thin'&&p.mode!=='quiet'))];
/** The smallest pointer target (WCAG 2.2 SC 2.5.8 Target Size (Minimum)), in CSS px; app.css `button.tl-bar` matches. */
const TARGET=24;
/** `quiet`: a joined bar whose words are its run's; `joined`: it continues the bar before it (only `draft` differs). */
type Placed={bar:Bar;track:'thin'|'main';lane:number;px:number;mode:'in'|'short'|'side'|'bare'|'thin'|'quiet';end:number;joined?:boolean};
type Layout={placed:Placed[];height:number;pad:number;main:number;heatTop:number;lanes:{thin:number;main:number}};

/** Greedy lanes: each bar takes the first lane free from its start (time away that shows its words beside it keeps
 *  room for them, so nothing in its lane covers them). */
export function layoutRow(r:TimelineRow,span:[number,number],width:number,compact:boolean,oneLine=false,fold=false):Layout{
  // WCAG 2.5.8: a bar that opens something is at least 24 × 24 px (TARGET); its lane keeps that room free.
  const total=span[1]-span[0],perDay=width/Math.max(1,total),GAP=compact?3:4,PAD=compact?4:7;
  if(oneLine)return lineRow(r,span,perDay,compact,fold);
  const placed:Placed[]=r.bars.filter(b=>dayNumber(b.end)>span[0]&&dayNumber(b.start)<span[1]).map(bar=>{
    const a=Math.max(dayNumber(bar.start),span[0]),b=Math.min(dayNumber(bar.end),span[1]),px=(b-a)*perDay;
    const track=bar.style==='rail'||bar.style==='milestone'?'thin':'main';let mode:Placed['mode']='in',end=b;
    if(AWAY.includes(bar.style)&&px<bar.label.length*6.3+34){mode='side';end=a+(Math.max(px,bar.open?TARGET:14)+10+(bar.label.length+6)*6.1)/perDay;}
    else if(track==='main'&&px<72)mode='short';
    if(bar.open&&px<TARGET)end=Math.max(end,a+TARGET/perDay);
    // A milestone's words follow its diamond: its lane is taken that far.
    if(bar.style==='milestone')end=Math.max(end,a+(14+bar.label.length*6.1)/perDay);  // never less than the 24 px target
    return {bar,track,lane:0,px,mode,end};});
  const lanes={thin:0,main:0};
  for(const t of ['thin','main'] as const){const ends:number[]=[];
    for(const p of placed.filter(x=>x.track===t).sort((x,y)=>dayNumber(x.bar.start)-dayNumber(y.bar.start)||dayNumber(y.bar.end)-dayNumber(x.bar.end))){
      const s=dayNumber(p.bar.start);let i=ends.findIndex(e=>e<=s);if(i<0){i=ends.length;ends.push(0);}ends[i]=p.end;p.lane=i;}
    lanes[t]=ends.length;}
  const LH={thin:placed.some(p=>p.track==='thin'&&p.bar.open)?TARGET:14,main:TARGET};
  const top=PAD+(r.zones?.length?16:0),main=top+lanes.thin*(LH.thin+GAP);
  for(const p of placed)p.lane=p.track==='thin'?top+p.lane*(LH.thin+GAP):main+p.lane*(LH.main+GAP);
  let y=main+lanes.main*(LH.main+GAP);const heatTop=y+2;if(r.heat)y+=12;
  return {placed,height:Math.max(compact?34:44,y+PAD-GAP),pad:top,main,heatTop,lanes};
}

/** Width (px) a bar needs for its words: 6.5 px a character (11.5 px Geist) and its padding; a leave code alone needs
 *  6 px a character, so an 8-day leave at 12 months (23 px) still shows its code. */
const fits=(text:string,px:number,code=false)=>code?text.length*6+10<=px:text.length*6.5+12<=px;
const same=(a:Bar,b:Bar)=>a.label===b.label&&a.style===b.style&&a.hue===b.hue&&!!a.external===!!b.external;
/** A stretch folded into its neighbour: the neighbour covers its days and its card and words name it. */
const absorb=(into:Bar,x:Bar):Bar=>({...into,start:into.start<x.start?into.start:x.start,end:into.end>x.end?into.end:x.end,
  aria:`${into.aria}; also ${x.aria}`,card:()=>[...(into.card?.()||[]),el('div',{class:'tl-card-also muted'},`Also: ${x.full||x.label} · ${spanWords(x.start,x.end)}`)]});
/** One line: every bar in one lane (the page gives stretches that never overlap). The words that fit: the label, the
 *  short name (or a leave code), else no words (thin, or folded into a neighbour with `fold`). A bar that continues the
 *  one before it (same words, only published/draft differs) joins it, and the run says its words once. */
function lineRow(r:TimelineRow,span:[number,number],perDay:number,compact:boolean,fold:boolean):Layout{
  const PAD=compact?3:6,px=(b:Bar)=>(Math.min(dayNumber(b.end),span[1])-Math.max(dayNumber(b.start),span[0]))*perDay;
  const mode=(b:Bar):Placed['mode']=>{const w=px(b),short=b.code||b.short||b.label;return fits(b.label,w)?'in':fits(short,w)?'short':fits(short,w,!!b.code)?'bare':'thin';};
  let bars=r.bars.filter(b=>dayNumber(b.end)>span[0]&&dayNumber(b.start)<span[1]).sort((a,b)=>a.start.localeCompare(b.start));
  if(fold){const out:Bar[]=[];
    for(let i=0;i<bars.length;i++){const b=bars[i],prev=out[out.length-1],next=bars[i+1];
      if(mode(b)!=='thin'){out.push(b);continue;}
      if(prev&&prev.end===b.start)out[out.length-1]=absorb(prev,b);
      else if(next&&next.start===b.end)bars[i+1]=absorb(next,b);}
    bars=out;}
  const placed:Placed[]=bars.map(bar=>({bar,track:'main',lane:PAD,px:px(bar),mode:mode(bar),end:Math.min(dayNumber(bar.end),span[1])}));
  for(let i=1;i<placed.length;i++){const a=placed[i-1],b=placed[i];if(a.bar.end===b.bar.start&&same(a.bar,b.bar))b.joined=true;}
  // A joined run says its words once: on its first part that fits them, its other parts quiet.
  for(let i=0;i<placed.length;){let j=i+1;while(j<placed.length&&placed[j].joined)j++;
    if(j-i>1){const at=placed.slice(i,j).findIndex(p=>p.mode!=='thin');placed.slice(i,j).forEach((p,k)=>{if(k!==at)p.mode='quiet';});}
    i=j;}
  return {placed,height:PAD*2+24,pad:PAD,main:PAD,heatTop:PAD+24,lanes:{thin:0,main:1}};
}

/** The axis's calendar periods from `start` to `end` (exclusive), each [start, end): weeks from the department's week
 *  start, months from the 1st, cut at both ends. One helper for every date axis (the timeline's head, Who's available). */
export function units(start:string,end:string,unit:'week'|'month'|'quarter'){
  const out:[string,string][]=[];let d=unit==='week'?weekStartOf(start):start.slice(0,7)+'-01';
  if(unit==='quarter')d=plusMonths(d,-((Number(d.slice(5,7))-1)%3));
  while(d<end){const n=unit==='week'?plusDays(d,7):plusMonths(d,unit==='quarter'?3:1);out.push([d<start?start:d,n>end?end:n]);d=n;}
  return out;
}

/** Where each timeline (by its label) was scrolled: a redraw (a collapse, a layer, new data) keeps the reader's place. */
const kept=new Map<string,number>();
export function timelineView(spec:TimelineSpec):HTMLElement{
  const span:[number,number]=[dayNumber(spec.start),dayNumber(spec.end)],total=span[1]-span[0],today=spec.today||departmentToday();
  const pct=(day:string|number)=>Math.max(0,Math.min(100,((typeof day==='number'?day:dayNumber(day))-span[0])/total*100));
  const place=(n:HTMLElement,a:string|number,b:string|number,min=0)=>{const l=pct(a);n.style.setProperty('--l',l.toFixed(3));n.style.setProperty('--w',Math.max(pct(b)-l,min).toFixed(3));return n;};
  const weekly=spec.zoom==='week'||spec.zoom==='month',compact=spec.density==='compact',collapsed=new Set(spec.collapsed||[]);
  const root=el('div',{class:'tl'+(compact?' tl-compact':'')+(spec.oneLine?' tl-one':''),role:'region','aria-label':spec.label,tabindex:'-1'});
  if(spec.oneLine)root.style.setProperty('--tl-months',String(units(spec.start,spec.end,'month').length));  // a phone's swipe width

  // ---- Header: two scale rows and Today ----
  // A narrow period (a quarter at a long zoom) ends its label in "…", the whole label as its tooltip.
  const cell=(a:string,b:string,text:string,now:boolean)=>place(el('span',{class:'tl-cell'+(now?' now':''),title:text},el('span',{class:'truncate'},text)),a,b);
  // One line at 9 or 12 months: months only (approved v2), no quarter row.
  const months=spec.oneLine&&!weekly;
  const top=months?[]:weekly?units(spec.start,spec.end,'month').map(([a,b])=>cell(a,b,fmt(a,{month:'short',year:'numeric'}),false))
    :units(spec.start,spec.end,'quarter').map(([a,b])=>cell(a,b,`Q${Math.floor((Number(a.slice(5,7))-1)/3)+1} ${a.slice(0,4)}`,false));
  const sub=weekly?units(spec.start,spec.end,'week').map(([a,b])=>cell(a,b,shortDay(a),today>=a&&today<b))
    :units(spec.start,spec.end,'month').map(([a,b])=>cell(a,b,fmt(a,{month:'short'})+(a.slice(5,7)==='01'||a===spec.start?' '+a.slice(0,4):''),today.slice(0,7)===a.slice(0,7)));
  const inView=today>=spec.start&&today<spec.end;
  const head=el('div',{class:'tl-head'},el('div',{class:'tl-corner'},spec.corner),
    el('div',{class:'tl-scale'+(months?' months':''),'aria-hidden':'true'},months?null:el('div',{class:'tl-r1'},...top),el('div',{class:'tl-r2'},...sub),
      el('div',{class:'tl-r3'},inView?place(el('span',{class:'tl-today-pill'+(pct(today)<6?' at-start':pct(today)>94?' at-end':'')},`Today · ${shortDay(today)}`),today,today):null)));

  // ---- Body: grid lines, weekends and Today behind the rows ----
  const grid=el('div',{class:'rb-tl-grid','aria-hidden':'true'});
  for(const [a] of units(spec.start,spec.end,weekly?'week':'month').slice(1))grid.append(place(el('span',{class:'tl-line'+(!weekly&&(Number(a.slice(5,7))-1)%3===0?' strong':'')}),a,a));
  if(spec.zoom==='week'&&spec.weekends!==false)for(let d=span[0];d<span[1];d++){const day=dayOf(d);
    if(WEEKEND_DAYS.includes(weekday(day))&&!WEEKEND_DAYS.includes(weekday(dayOf(d-1)))){let e=d+1;while(e<span[1]&&WEEKEND_DAYS.includes(weekday(dayOf(e))))e++;grid.append(place(el('span',{class:'tl-weekend'}),d,e));}}
  if(inView)grid.append(place(el('span',{class:'tl-today'}),today,today));
  const body=el('div',{class:'tl-body'},grid);
  root.append(head,body);

  // ---- Rows (virtualised) ----
  type Entry={key:string;h:number;top:number;draw:()=>HTMLElement};
  let entries:Entry[]=[],width=0;
  const shown=new Map<string,HTMLElement>();
  const groupHead=(g:TimelineGroup)=>{const closed=collapsed.has(g.key);
    const t=groupToggle(el('strong',{},g.name||'None'),g.rows.length,!closed,()=>spec.toggle?.(g.key),{cls:'tl-gtoggle'});
    return el('div',{class:'tl-group',role:'group','aria-label':`${g.name||'None'}, ${plural(g.rows.length,'row')}`},t,
      el('div',{class:'tl-gsum'},...(g.summary||[]).map(x=>el('span',{class:x.tone?'tl-'+x.tone:''},x.tone==='error'?icon('alert'):null,x.text))));};
  /** A one-line bar: a button (its card opens on hover, keyboard focus or a tap); only a row's first is in the Tab order,
   *  arrow keys move along and across rows (lineKeys). */
  // A drawn mark is stamped as its key entries (dom.ts legend `mark`): every mark has one.
  const stamp=(n:Element,keys:readonly Key[]|undefined,words=true)=>{if(spec.legend)for(const k of keys||[])if(!k.code||words)spec.legend.mark(n,k.key,swatchOf(k),k.words,k.order);};
  const lineBar=(p:Placed,first:boolean)=>{const b=p.bar;
    // No dot or icon on the line: the words and the mark's form say what it is (View settings › Show is the key).
    const words=p.mode==='thin'||p.mode==='quiet'?null:p.mode==='in'?b.label:b.code||b.short||b.label;
    const n=el('button',{type:'button',class:`tl-bar tl-${b.style}${b.hue?' duty-'+b.hue:''}${b.draft?' draft':''}${b.external?' ext':''}${p.joined?' joined':''}${p.mode==='thin'||p.mode==='bare'?' '+p.mode:''}`,'data-bar':b.id,
      'aria-label':b.aria,tabindex:first?'0':'-1'},words?el('span',{class:'tl-lbl'},words):null);stamp(n,b.keys,!!words);
    place(n,Math.max(dayNumber(b.start),span[0]),Math.min(dayNumber(b.end),span[1]));n.style.setProperty('--t',String(p.lane));
    if(b.card){hoverCard(n,b.card);
      // A tap (no hover) opens the same card.
      n.addEventListener('click',()=>{if(b.open)b.open();else if(!canHover())popover(n,panel=>{panel.classList.add('hovercard');panel.append(...b.card!());},{label:b.aria});});}
    else if(b.open)n.addEventListener('click',b.open);
    return n;};
  const rowNode=(r:TimelineRow,lay:Layout)=>{
    const who=el(r.open?'button':'div',{class:'tl-who',...(r.open?{type:'button','aria-label':`${spokenName(r.label||r.name,r.name)}: open`}:{})},
      // No initials disc (DESIGN: a name says who; audit C19 keeps one avatar, for accounts and candidates).
      el('span',{class:'tl-nm'},el('b',{'data-tip':r.label&&r.label!==r.name?r.name:undefined},r.label||r.name,!r.open&&spokenName(r.label||r.name,r.name)!==(r.label||r.name)?el('span',{class:'sr-only'},', '+r.name):''),r.sub?el('small',{},r.sub):null),...(r.tags||[]).map(t=>{const n=el('abbr',{class:t.class||'wk-tag ext','data-tip':t.tip,'aria-label':t.tip},t.icon?icon(t.icon):null,t.text);stamp(n,t.key&&[t.key]);return n;}),
      r.flag?(m=>(m&&stamp(m,[ZONE_KEYS.error]),m))(issueMarker([{severity:'error',words:r.flag}],{title:r.name})):null);  // its key: the row's conflict
    if(r.open)who.addEventListener('click',r.open);
    const lane=el('div',{class:'tl-lane','data-main':String(lay.main)});
    if(spec.oneLine){lay.placed.forEach((p,i)=>lane.append(lineBar(p,i===0)));return el('div',{class:'tl-row','data-row':r.id},who,lane);}
    for(const p of lay.placed){const b=p.bar,away=AWAY.includes(b.style),tiny=p.px<34||p.mode==='side'&&away;
      const mark=b.external?icon('external'):ICONS[b.style]?icon(ICONS[b.style]!):b.style==='block'||b.style==='outline'?el('span',{class:'tl-dot'}):null;
      // Initials only for rotation-like blocks ("UC"); other words keep their start and end in an ellipsis.
      const dates=spec.barLabel==='dates'&&b.style!=='plan',meta=(!spec.barLabel||spec.barLabel==='name_length')&&b.meta;
      const text=dates?(p.mode==='short'?shortDay(b.start):spanWords(b.start,b.end)):p.mode==='short'&&(b.short||b.style==='block'||b.style==='outline')?b.short||shortName(b.label):b.label;
      const n=el(b.open?'button':'span',{class:`tl-bar tl-${b.style}${b.hue?' duty-'+b.hue:''}${b.external?' ext':''}${tiny?' tiny':''}`,'data-bar':b.id,
        ...(b.open?{type:'button','aria-label':b.aria}:{role:'img','aria-label':b.aria})},
        p.px>=(away?0:34)?mark:null,!(away&&p.mode==='side')&&(p.px>=18||b.style==='milestone')?el('span',{class:'tl-lbl'},text):null,
        meta&&p.px>=110?el('span',{class:'tl-meta'},meta):null,...(b.marks||[]).map(icon));
      place(n,Math.max(dayNumber(b.start),span[0]),Math.min(dayNumber(b.end),span[1]));n.style.setProperty('--t',String(p.lane));
      if(spec.drag?.movable(b.id,r.id)){n.dataset.start=b.start;n.dataset.end=b.end;n.classList.add('movable');
        n.setAttribute('aria-keyshortcuts','ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight Alt+ArrowLeft Alt+ArrowRight Control+D');}
      if(b.open)n.addEventListener('click',b.open);
      if(b.card&&canHover())hoverCard(n,b.card);
      stamp(n,b.keys);lane.append(n);
      if(p.mode==='side'&&away){const o=el('span',{class:'tl-side','aria-hidden':'true'},`${b.label} · ${lengthWords(b.start,b.end)}`);
        o.style.setProperty('--l',pct(b.start).toFixed(3));o.style.setProperty('--px',String(Math.max(p.px,b.open?TARGET:14)+5));o.style.setProperty('--t',String(p.lane));lane.append(o);}
    }
    for(const z of r.zones||[]){const n=place(el('span',{class:'tl-zone'+(z.tone==='approval'?' approval':'')}),z.start,z.end,0.4);n.style.setProperty('--t',String(lay.pad-4));
      n.append(el('span',{class:'tl-zbadge'},icon(z.tone==='approval'?'clock':'alert'),z.label));stamp(n,[ZONE_KEYS[z.tone]]);lane.append(n);}
    if(r.heat){const strip=el('div',{class:'tl-heatmap','aria-hidden':'true'});strip.style.setProperty('--t',String(lay.heatTop));
      for(let d=span[0];d<span[1];d++){const day=dayOf(d),v=r.heat.levels[day]||0;strip.append(el('span',{class:r.heat.errors?.includes(day)?'hx':v?'h'+Math.min(3,v):''}));}
      stamp(strip,r.keys);lane.append(strip);
      // A conflicting day is never colour alone: each run of them gets an alert mark with its words.
      const bad=[...new Set(r.heat.errors||[])].filter(d=>d>=spec.start&&d<spec.end).sort();
      for(let i=0;i<bad.length;){let j=i;while(j+1<bad.length&&bad[j+1]===plusDays(bad[j],1))j++;
        const words=`Duty conflict: ${j>i?spanWords(bad[i],plusDays(bad[j],1)):shortDay(bad[i])}`;
        const m=el('span',{class:'tl-heat-x',role:'img','aria-label':words,'data-tip':words},icon('alert'));stamp(m,[ZONE_KEYS.error]);
        m.style.setProperty('--l',pct(bad[i]).toFixed(3));m.style.setProperty('--t',String(lay.heatTop));lane.append(m);i=j+1;}}
    return el('div',{class:'tl-row','data-row':r.id},who,lane);
  };
  const build=()=>{
    width=Math.max(120,body.offsetWidth-(parseFloat(getComputedStyle(root).getPropertyValue('--tl-name'))||240));
    entries=[];let y=0;spec.legend?.clear();
    const add=(key:string,h:number,draw:()=>HTMLElement)=>{entries.push({key,h,top:y,draw});y+=h;};
    for(const g of spec.groups){
      if(g.key||g.name)add('g:'+g.key,compact?30:34,()=>groupHead(g));
      if(collapsed.has(g.key))continue;
      for(const r of g.rows){const lay=layoutRow(r,span,width,compact,spec.oneLine,spec.fold);add('r:'+g.key+':'+r.id,lay.height,()=>rowNode(r,lay));
        // The key lists the whole laid-out view (rows drawn as they scroll into view stamp the same entries).
        if(spec.legend)for(const k of rowKeys(r,lay))spec.legend.laidOut(k.key,swatchOf(k),k.words,k.order);}
    }
    body.style.setProperty('--tl-height',String(y));
    for(const n of shown.values())n.remove();shown.clear();
  };
  // Rows within this distance of the viewport are kept drawn.
  const AHEAD=600;
  const paint=()=>{
    const s=root.scrollTop-head.offsetHeight,from=s-AHEAD,to=s+root.clientHeight+AHEAD;
    const want=new Set<string>();let prev:Element=grid;
    // In visual order (each after the one above it), so Tab follows the rows as seen, after scrolling either way.
    for(const e of entries){if(e.top+e.h<from)continue;if(e.top>to)break;want.add(e.key);
      let n=shown.get(e.key);if(!n){n=e.draw();n.style.setProperty('--top',String(e.top));n.style.setProperty('--h',String(e.h));shown.set(e.key,n);}
      if(prev.nextElementSibling!==n)prev.after(n);prev=n;}
    for(const [k,n] of shown)if(!want.has(k)){n.remove();shown.delete(k);}
    markOpen(body);
  };
  let frame=0;
  root.addEventListener('scroll',()=>{kept.set(spec.label,root.scrollTop);if(!frame)frame=requestAnimationFrame(()=>{frame=0;paint();});},{passive:true});
  // Drawn once in the page (widths are known then), and again when its width changes.
  let seen=0;
  const fit=()=>{const w=root.clientWidth;if(!w||w===seen)return;const first=!seen;seen=w;
    // Rows are drawn afresh at the new width: a bar or name that had focus has it again.
    const had=document.activeElement instanceof HTMLElement&&root.contains(document.activeElement)?document.activeElement:null;
    const again=had?.dataset.bar?`[data-bar="${CSS.escape(had.dataset.bar)}"]`:had?.matches('.tl-who')?`[data-row="${CSS.escape(had.closest<HTMLElement>('[data-row]')!.dataset.row!)}"] .tl-who`:'';
    build();if(first)root.scrollTop=kept.get(spec.label)||0;paint();
    // A phone's one line is wider than the screen: open on the month before today's (the months swipe).
    if(first&&spec.oneLine&&inView&&root.scrollWidth>root.clientWidth)root.scrollLeft=Math.max(0,pct(today)/100*width-width/units(spec.start,spec.end,'month').length);
    if(again)root.querySelector<HTMLElement>(again)?.focus({preventScroll:true});else if(first)focusOwed(root);};
  observeSize(root,()=>fit());
  if(spec.drag)enableDrag(root,span,spec.zoom,spec.drag);
  if(spec.oneLine)lineKeys(root);
  // Measured after the key's own paint (a microtask queued while the rows were drawn), so its line is counted.
  requestAnimationFrame(()=>{fit();queueMicrotask(()=>fitHeight(root));});
  return root;
}

/** One-line rows by keyboard: ← → the previous or next stretch, Home / End the first or last, ↑ ↓ the stretch at the
 *  same place in the row above or below (the rows drawn around the view). The focused stretch takes the row's tab stop. */
function lineKeys(root:HTMLElement){
  root.addEventListener('keydown',e=>{
    const b=(e.target as HTMLElement).closest?.<HTMLElement>('.tl-bar');if(!b||!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key))return;
    const row=b.closest<HTMLElement>('.tl-row')!,bars=(r:Element)=>[...r.querySelectorAll<HTMLElement>('.tl-lane>.tl-bar')];
    let to:HTMLElement|undefined;
    if(e.key==='ArrowLeft'||e.key==='ArrowRight'){const all=bars(row);to=all[all.indexOf(b)+(e.key==='ArrowLeft'?-1:1)];}
    else if(e.key==='Home'||e.key==='End'){const all=bars(row);to=e.key==='Home'?all[0]:all[all.length-1];}
    else{let r:Element|null=row;do r=e.key==='ArrowUp'?r.previousElementSibling:r.nextElementSibling;while(r&&!r.matches('.tl-row'));
      if(r){const a=b.getBoundingClientRect(),x=a.left+a.width/2,all=bars(r);
        to=all.find(n=>{const c=n.getBoundingClientRect();return c.left<=x&&c.right>=x;})||all.reduce<HTMLElement|undefined>((m,n)=>!m||Math.abs(n.getBoundingClientRect().left-x)<Math.abs(m.getBoundingClientRect().left-x)?n:m,undefined);}}
    e.preventDefault();if(!to)return;
    for(const n of bars(row))n.tabIndex=-1;for(const n of bars(to.closest('.tl-row')!))n.tabIndex=-1;
    to.tabIndex=0;to.focus();to.scrollIntoView({block:'nearest',inline:'nearest'});
  });
}

/** A small timeline for a side panel (approved mockup v3, bar side panel › Dates): the months around one item, its bars
 *  (drawn as on the timeline, words as width allows), its conflict zones and Today. The words are in `label` for
 *  screen readers; the drawing itself is hidden from them. */
export function timelineMini(o:{label:string;start:string;end:string;bars:Bar[];zones?:Zone[];today?:string}):HTMLElement{
  const span=[dayNumber(o.start),dayNumber(o.end)],total=span[1]-span[0],today=o.today||departmentToday();
  const pct=(day:string)=>Math.max(0,Math.min(100,(dayNumber(day)-span[0])/total*100));
  const place=(n:HTMLElement,a:string,b:string)=>{n.style.setProperty('--l',pct(a).toFixed(3));n.style.setProperty('--w',Math.max(pct(b)-pct(a),0.4).toFixed(3));return n;};
  const months=units(o.start,o.end,'month').map(([a,b])=>place(el('span',{class:'tl-cell'+(today.slice(0,7)===a.slice(0,7)?' now':'')},fmt(a,{month:'short'})),a,b));
  const width=(b:Bar)=>(dayNumber(b.end)-dayNumber(b.start))/total;
  const lane=el('div',{class:'tl-r2'},...(o.zones||[]).map(z=>{const n=place(el('span',{class:'tl-zone'+(z.tone==='approval'?' approval':'')}),z.start,z.end);n.style.setProperty('--t','1');return n;}),
    ...o.bars.map(b=>{const n=place(el('span',{class:`tl-bar tl-${b.style}${b.hue?' duty-'+b.hue:''}${b.external?' ext':''}`,'data-bar':b.id}),b.start,b.end);
      n.style.setProperty('--t','3');n.append(b.external?icon('external'):el('span',{class:'tl-dot'}));if(width(b)>0.12)n.append(el('span',{class:'tl-lbl'},b.meta||b.label));return n;}));
  const shown=today>=o.start&&today<o.end?place(el('span',{class:'tl-today'}),today,today):null;
  return el('div',{class:'tl-mini',role:'img','aria-label':o.label},el('div',{class:'tl-scale'},el('div',{class:'tl-r1'},...months),lane,shown));
}

/** The timeline fills the page down to the status footer (app.css reads --tl-top); few rows keep their own height. What
 *  the page draws after it (its key, one line below) keeps its room (--tl-below), so the timeline
 *  scrolls, never the page. */
function fitHeight(root:HTMLElement){
  if(!root.isConnected)return;const host=root.closest<HTMLElement>('.page-contained'),box=root.getBoundingClientRect();
  root.style.setProperty('--tl-top',`${Math.round(box.top+(host?host.scrollTop-host.getBoundingClientRect().top:scrollY))}px`);
  let below=0;
  for(let n:Element|null=root;n&&!n.matches('.page');n=n.parentElement)for(let s=n.nextElementSibling;s;s=s.nextElementSibling)
    if(!s.matches('.footer-status'))below=Math.max(below,s.getBoundingClientRect().bottom-box.bottom);
  root.style.setProperty('--tl-below',`${Math.round(below)}px`);
}

/** One layer of a timeline: what it is, its swatch (the legend), how many it shows, and whether it is on. */
export type Layer={key:string;label:string;style:BarStyle|'conflict'|'heat';hue?:string;count?:number;on:boolean;fixed?:boolean;
  /** Parts shown or hidden one by one under the layer (leave types under Leave), each with its own key. */parts?:{key:string;label:string;on:boolean}[];
  /** Its key swatches' classes, where its style alone does not show the mark (hatched, outlined); [] for none. */swatches?:string[]};
/** The Timeline's View settings: Zoom, Layers (they are the legend), the name column's properties, row density and
 *  weekends. The page owns the state; each choice redraws it (the popover stays open). */
export function timelineSettings(panel:HTMLElement,o:{zoom:Zoom;setZoom:(z:Zoom)=>void;layers:Layer[];setLayer:(key:string,on:boolean)=>void;
  properties?:{key:string;label:string;on:boolean}[];setProperty?:(key:string,on:boolean)=>void;
  density:'comfortable'|'compact';setDensity:(d:'comfortable'|'compact')=>void;weekends:boolean;setWeekends:(on:boolean)=>void;
  /** Bar label and what rotations are coloured by (the page's own choices), each a shared dropdown. */
  barLabel?:{value:BarLabel;set:(v:BarLabel)=>void};colourBy?:{label:string;options:[string,string][];value:string;set:(v:string)=>void};
  /** One line per row (Where is everyone): Show, Range with its start month, Labels and Density only (approved v2). */
  oneLine?:{start:{value:string;options:[string,string][];set:(v:string)=>void};labels:{value:'short'|'name';set:(v:'short'|'name')=>void}}}){
  panel.classList.add('tl-settings');
  if(o.oneLine){const show=el('div',{class:'tl-layers',role:'group','aria-label':'Show'});
    // Show is the key (no footer legend): each toggle with the marks it draws.
    for(const l of o.layers){const sw=(l.swatches||[`tl-${l.style}${l.hue?' duty-'+l.hue:''}`]).map(x=>el('span',{class:'tl-swatch '+x,'aria-hidden':'true'}));
      if(l.fixed){show.append(el('div',{class:'tl-layer'},...sw,el('span',{class:'grow'},l.label),el('span',{class:'hint'},'Always shown')));continue;}
      const c=check(l.label,l.on);show.append(el('div',{class:'tl-layer'},...sw,c.wrap));
      // Types as toggle chips (aria-pressed), shown while their layer is (the popover stays open: marked in place).
      const parts=l.parts?.length?el('div',{class:'tl-parts',role:'group','aria-label':l.label+' types',hidden:!l.on},...l.parts.map(t=>{
        const b=el('button',{type:'button',class:'tl-part','aria-pressed':String(t.on)},t.label);
        b.addEventListener('click',()=>{const on=b.getAttribute('aria-pressed')!=='true';b.setAttribute('aria-pressed',String(on));o.setLayer(t.key,on);});return b;})):null;
      c.input.addEventListener('change',()=>{if(parts)parts.hidden=!c.input.checked;o.setLayer(l.key,c.input.checked);});
      if(parts)show.append(parts);}
    const start=select('Starts',o.oneLine.start.options.map(([v,t])=>({value:v,label:t})),o.oneLine.start.value);start.input.addEventListener('change',()=>o.oneLine!.start.set(start.input.value));
    panel.append(el('div',{class:'dt-pop-label'},'Show'),show,...segment('Range',ZOOMS,o.zoom,k=>o.setZoom(k as Zoom)),el('div',{class:'vs-fields'},start.wrap),
      ...segment('Labels',[['short','Short'],['name','Full']],o.oneLine.labels.value,k=>o.oneLine!.labels.set(k as 'short'|'name')),
      ...segment('Density',[['comfortable','Comfortable'],['compact','Compact']],o.density,k=>o.setDensity(k as 'comfortable'|'compact')));
    return;}
  panel.append(...segment('Range',ZOOMS,o.zoom,k=>o.setZoom(k as Zoom)));
  const layers=el('div',{class:'tl-layers',role:'group','aria-label':'Layers'});
  for(const l of o.layers){const sw=(l.swatches||[`tl-${l.style}${l.hue?' duty-'+l.hue:''}`]).map(x=>el('span',{class:'tl-swatch '+x,'aria-hidden':'true'}));
    if(l.fixed){layers.append(el('div',{class:'tl-layer'},...sw,el('span',{class:'grow'},l.label),el('span',{class:'hint'},'Always shown')));continue;}
    const c=check(l.label,l.on);c.input.addEventListener('change',()=>o.setLayer(l.key,c.input.checked));
    layers.append(el('div',{class:'tl-layer'},...sw,c.wrap,l.count===undefined?null:el('span',{class:'hint'},String(l.count))));}
  panel.append(el('div',{class:'dt-pop-label'},'Layers'),layers);  // the key is the visible legend above the timeline
  if(o.properties?.length){const props=el('div',{class:'tl-layers',role:'group','aria-label':'Name column shows'});
    for(const p of o.properties){const c=check(p.label,p.on);c.input.addEventListener('change',()=>o.setProperty?.(p.key,c.input.checked));props.append(el('div',{class:'tl-layer'},c.wrap));}
    panel.append(el('div',{class:'dt-pop-label'},'Name column shows'),props);}
  panel.append(...segment('Row density',[['comfortable','Comfortable'],['compact','Compact']],o.density,k=>o.setDensity(k as 'comfortable'|'compact')));
  const choose=(label:string,options:[string,string][],value:string,set:(v:string)=>void)=>{const f=select(label,options.map(([v,t])=>({value:v,label:t})),value);
    f.input.addEventListener('change',()=>set(f.input.value));return f.wrap;};
  const picks=[o.barLabel?choose('Bar label',BAR_LABELS,o.barLabel.value,v=>o.barLabel!.set(v as BarLabel)):null,
    o.colourBy?choose(o.colourBy.label,o.colourBy.options,o.colourBy.value,o.colourBy.set):null].filter(Boolean) as HTMLElement[];
  if(picks.length)panel.append(el('div',{class:'vs-fields'},...picks));
  const we=check('Shade weekends (Week zoom)',o.weekends);we.input.addEventListener('change',()=>o.setWeekends(we.input.checked));
  panel.append(el('div',{class:'tl-layers'},el('div',{class:'tl-layer'},we.wrap)));
}

/**
 * Phones (390): one person's items as an agenda — what is on now with its progress, then the rest by month; or, for
 * many people, who is away now, who is elsewhere now and what starts in the next two weeks, each with a mini bar.
 */
export function timelineAgenda(spec:{label:string;rows:TimelineRow[];start:string;end:string;today?:string;mine:boolean;full?:()=>void;
  /** The view's key: each item is stamped as its bar's entries (every mark has one). */legend?:Legend}):HTMLElement{
  const today=spec.today||departmentToday(),root=el('div',{class:'tl-agenda',role:'region','aria-label':spec.label});
  const on=(b:Bar)=>b.start<=today&&b.end>today;
  const item=(b:Bar,who?:TimelineRow)=>{const n=el(b.open?'button':'div',{class:`tl-ag-item tl-${b.style}${b.hue?' duty-'+b.hue:''}`,...(b.open?{type:'button','aria-label':b.aria}:{})},
    el('b',{},b.external?icon('external'):ICONS[b.style]?icon(ICONS[b.style]!):null,who?who.name:b.label),
    el('span',{},`${who?b.label+' · ':''}${spanWords(b.start,b.end)} · ${lengthWords(b.start,b.end)}`),...(b.marks||[]).map(icon));
    if(b.open)n.addEventListener('click',b.open);stamp(n,b);return n;};
  const stamp=(n:Element,b:Bar)=>{if(spec.legend)for(const k of b.keys||[])if(!k.code)spec.legend.mark(n,k.key,swatchOf(k),k.words,k.order);};
  const section=(title:string,items:HTMLElement[])=>items.length?el('section',{class:'tl-ag-sec'},el('h3',{},el('span',{},title),el('span',{class:'muted'},String(items.length))),...items):null;
  if(spec.mine){
    const bars=spec.rows.flatMap(r=>r.bars).filter(b=>b.style!=='plan'&&b.end>spec.start&&b.start<spec.end).sort((a,b)=>a.start.localeCompare(b.start));
    const now=bars.find(b=>on(b)&&(b.style==='block'||b.style==='outline'));
    if(now){const done=dayNumber(today)-dayNumber(now.start),left=dayNumber(now.end)-dayNumber(today);
      const fill=el('i',{class:'tl-ag-done'});fill.style.setProperty('--w',(done/(done+left)*100).toFixed(1));
      const bar=el('span',{class:'tl-ag-progress',role:'progressbar','aria-label':'Done so far','aria-valuemin':'0','aria-valuemax':String(done+left),'aria-valuenow':String(done)},fill);
      // The hero is drawn in the stretch's hue: stamped as its entries too.
      const hero=el('div',{class:`tl-ag-hero duty-${now.hue||'slate'}`},item(now),bar,el('div',{class:'tl-ag-left'},el('span',{},plural(done,'day')+' done'),el('span',{},plural(left,'day')+' left')));stamp(hero,now);
      root.append(el('section',{class:'tl-ag-sec'},el('h3',{},el('span',{},'Now'),el('span',{class:'muted'},`Week ${Math.floor(done/7)+1} of ${Math.ceil((done+left)/7)}`)),
        hero));}
    const months=new Map<string,Bar[]>();for(const b of bars)if(b!==now){const m=b.start<spec.start?spec.start.slice(0,7):b.start.slice(0,7);months.set(m,[...(months.get(m)||[]),b]);}
    for(const [m,list] of months)root.append(section(fmt(m+'-01',{month:'long',year:'numeric'}),list.map(b=>item(b)))!);
    if(!root.children.length)root.append(el('p',{class:'muted'},'Nothing in these months.'));
    return root;
  }
  const soon=dayOf(dayNumber(today)+14),of=(f:(b:Bar)=>boolean)=>spec.rows.flatMap(r=>{const b=r.bars.find(f);return b?[item(b,r)]:[];});
  root.append(...[section('Away now',of(b=>on(b)&&AWAY.includes(b.style))),section('Elsewhere now',of(b=>on(b)&&!!b.external)),
    section('Starting in the next 2 weeks',of(b=>b.start>today&&b.start<=soon&&b.style!=='plan'))].filter(Boolean) as HTMLElement[]);
  if(!root.children.length)root.append(el('p',{class:'muted'},'No one is away, elsewhere or starting something in the next 2 weeks.'));
  if(spec.full){const b=el('button',{type:'button',class:'button secondary small'},'Show the timeline');b.addEventListener('click',spec.full);
    root.append(el('p',{class:'tl-ag-note muted'},'The timeline is made for wider screens. ',b));}
  return root;
}
