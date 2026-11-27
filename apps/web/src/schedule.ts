/** The Schedule page's own parts: views over one continuous timeline (period by anchor day), the published layer merged
 *  with the editable draft, Year/Month day dots, the Day timeline and the overlap choice. Week, People, Coverage and List
 *  reuse the planner renderers unchanged. */
import {grants,type Capability,announce,el,icon,legend,button,note,badge,empty,withIcon,formatDate,formatTime,formatDay,toLocal,labelOf,plural,hoverCard,typeColour,statusLabel,type IconName} from './dom.js';
import {fullNameOf} from './person_label.js';
import {get} from './api.js';
import {addDays,touchesDay,isWeekend,rangeWords} from './planner_logic.js';
import {dateMarks,dateMarkWords} from './date_sets.js';
import type {DateSetMarker} from './types.js';
import type {ScheduleWindow} from './generated.js';
import {periodNav,dutyColour,weekView,showPairs,teamLayout,groupWay,drawnPeople,colourRule,markWeekend,type WeekHooks} from './planner_week.js';
import {dutyCode,propertiesOf,itemParts,itemNodes,itemWords,ITEM_DRAWS,type PropKey} from './schedule_item.js';
import {personPill} from './person_pill.js';
import {issueGlyph} from './issue_marker.js';
import {byValue} from './schedule_dimensions.js';
import {MARKS} from './dom.js';
import {UNLINKED_STAFF} from './terms.js';
import {LEVEL_WORDS} from './issue_marker.js';
import {mustWords,couldWords} from './fix_list.js';
import {teamGroups,teamText} from './planner_teams.js';
import {teamView,teamProblem,rankOn,BOX_CODES} from './team_view.js';
import type {Context} from './forms.js';
import type {PlannerSession,Row,WorkspaceData,Evaluation} from './types.js';
import {dateInput} from './date_picker.js';
import {sidePanel} from './side_panel.js';
import {clashesOf} from './double_booking.js';

export type SchedView='year'|'month'|'week'|'day'|'timeline'|'people'|'coverage'|'list';
export const SCHED_VIEWS:{key:SchedView;label:string;glyph:IconName;letter:string}[]=[
  {key:'year',label:'Year',glyph:'calendar',letter:'Y'},{key:'month',label:'Month',glyph:'layers',letter:'M'},
  {key:'week',label:'Week',glyph:'calendar',letter:'W'},{key:'day',label:'Day',glyph:'clock',letter:'D'},{key:'timeline',label:'Timeline',glyph:'menu',letter:'T'},
  {key:'people',label:'People',glyph:'people',letter:'P'},{key:'coverage',label:'Coverage',glyph:'grid',letter:'C'},
  {key:'list',label:'List',glyph:'menu',letter:'L'}];
/** Drafts layer: edit one draft, show a second side by side (compare), or overlay the draft on the published days. */
export type LayerMode='edit'|'side'|'overlay';
export const LIVE=['draft','submitted','approved'];
/** The permission keys that let a reader act on a rota in each state (D-001): the same keys the server enforces
 *  (packages/contracts PERMISSIONS / CAPABILITIES), never role names. Closest to publication first. */
// shortcut: submitted keys on role-shaped UI capabilities (ui.approver/ui.programme_lead); swap to maintenance's "decide a submitted rota" permission when M0 lands.
export const ACTS_ON:Readonly<Record<string,readonly Capability[]>>={approved:['workspace.publish'],submitted:['ui.approver','ui.programme_lead'],draft:['planning.draft']};
/** Whether these roles can act on a rota in this state (publish it, decide it, edit it). */
export const actsOn=(status:string,roles:readonly string[])=>(ACTS_ON[status]||[]).some(k=>grants(roles,k));
/** The rota states the Schedule opens on by itself, closest to publication first: only those this reader acts on. Anyone
 *  who acts on none (an auditor, a reader) opens on the published rota; a draft stays one switcher pick away. */
export const defaultRotaStates=(roles:readonly string[]):string[]=>Object.keys(ACTS_ON).filter(s=>actsOn(s,roles));
/** The footer's left side for the rota on screen: the published layer's own status (its health, who is now away) unless a
 *  rota in work is open. A published rota is never a draft to check: no check pill, no "Check again". */
export const footerIsPublished=(w?:{[key:string]:unknown}|null)=>!w||!LIVE.includes(String(w.status));

export type Period={days:string[];page:number;has_previous:boolean;has_next:boolean;unit:string;label:string;/** Monday 0 … Sunday 6 */weekStart?:number};
/** How many days a view shows (its Range); each view has its own default, a saved view may choose another. */
export type SchedRange='year'|'month'|'2weeks'|'week'|'3days'|'day'|'custom';
export const VIEW_RANGE:Record<SchedView,SchedRange>={year:'year',month:'month',week:'week',day:'day',timeline:'month',people:'2weeks',coverage:'week',list:'month'};
export const RANGE_NAMES:[SchedRange,string][]=[['day','Day'],['3days','3 days'],['week','Week'],['2weeks','2 weeks'],['month','Month'],['custom','Custom']];
const UNIT:Record<SchedRange,string>={year:'year',month:'month','2weeks':'2 weeks',week:'week','3days':'3 days',day:'day',custom:'period'};
/** Days a range moves by (Year and Month move by the calendar). A custom range is `days` long. */
const STEP:Partial<Record<SchedRange,number>>={day:1,'3days':3,week:7,'2weeks':14};
/** The first day of the department's week holding `day` (organisation week_start: Monday 0 … Sunday 6). */
export const weekOf=(day:string,weekStart:number)=>{const js=(weekStart+1)%7,dow=new Date(day+'T12:00:00Z').getUTCDay();return addDays(day,-((dow-js+7)%7));};
/** Whether a ?shift= address that opens on its session's day says the session moved. A link to the session (Show on
 *  schedule, Home, a profile) names the day it was on (`namesDay`): any other day is a move. A session panel's own address
 *  names only the day in view, where the session was shown in that week: only another week is a move, never another day
 *  of the same week. */
export const linkMoved=(viewDay:string,sessionDay:string,weekStart:number,namesDay:boolean)=>
  !!viewDay&&viewDay!==sessionDay&&(namesDay||weekOf(viewDay,weekStart)!==weekOf(sessionDay,weekStart));
const monthStart=(day:string,move=0)=>{const d=new Date(day.slice(0,7)+'-01T12:00:00Z');d.setUTCMonth(d.getUTCMonth()+move);return d.toISOString().slice(0,10);};
/** The days a range shows around its anchor day. There is no rota boundary: the timeline is continuous. Day and 3 days
 *  start on the anchor; weeks, 2 weeks and a custom range on the week's first day. */
export function periodFor(view:SchedRange,anchor:string,weekStart:number,days=7):Period{
  let first=weekOf(anchor,weekStart),length=view==='custom'?days:STEP[view]||7;
  if(view==='3days')first=anchor;
  if(view==='year'){first=anchor.slice(0,4)+'-01-01';length=Math.round((Date.parse(`${Number(anchor.slice(0,4))+1}-01-01`)-Date.parse(first))/864e5);}
  else if(view==='month'){first=monthStart(anchor);length=Math.round((Date.parse(monthStart(anchor,1))-Date.parse(first))/864e5);}
  else if(view==='day'){first=anchor;length=1;}
  return {page:0,has_previous:true,has_next:true,unit:UNIT[view],label:first,weekStart,days:Array.from({length},(_,i)=>addDays(first,i))};
}
/** The anchor one period earlier (-1) or later (+1). */
export function stepAnchor(view:SchedRange,anchor:string,dir:number,days=7){
  if(view==='year')return `${Number(anchor.slice(0,4))+dir}-${anchor.slice(5)}`.replace(/-02-29$/,'-02-28');
  if(view==='month')return monthStart(anchor,dir);
  return addDays(anchor,dir*(view==='custom'?days:STEP[view]||7));
}
export function periodLabel(view:SchedRange,p:Period){
  if(view==='year')return p.days[0].slice(0,4);
  const f=(d:string,o:Intl.DateTimeFormatOptions)=>new Intl.DateTimeFormat('en-GB',{...o,timeZone:'UTC'}).format(new Date(d+'T12:00:00Z'));
  if(view==='month')return f(p.days[0],{month:'long',year:'numeric'});
  if(view==='day')return formatDay(p.days[0],'UTC',true);
  return `${formatDay(p.days[0],'UTC')} – ${formatDay(p.days.at(-1)!,'UTC')}`;
}
/** ‹ Today › period: the one date navigator, moved into the toolbar by the page. */
/** The one "nothing here" of every Schedule view (frontend audit S-01), the same card and next step everywhere: hidden
 *  by the filters → Show all; only your own duties shown → "You have no duties" (Show everyone where the reader may see
 *  them); a draft open → Add session; no draft → New rota (for someone who makes rotas), else simply nothing published. */
type EmptyOptions={filtered:boolean;hooks?:Pick<WeekHooks,'create'|'onCreate'|'onShowAll'|'unlinked'|'mine'|'onShowEveryone'|'nextDuty'>;create?:boolean;published?:boolean};
/** Which "nothing here" a Schedule view shows and its words: the one source for scheduleEmpty and the List table's card. */
export function scheduleEmptyWords(unit:string,o:EmptyOptions):{kind:'filtered'|'mine'|'create'|'unlinked'|'none';title:string;line:string}{
  const where=rangeWords(unit).where,h=o.hooks;
  if(o.filtered)return {kind:'filtered',title:'Hidden by filters',line:`Sessions ${where} do not match the filters or search.`};
  // maintenance: a view of your own duties says that, never "Nothing published" (the rota may be published without you on it).
  if(h?.mine)return {kind:'mine',title:`You have no duties ${where}`,line:''};
  if(h?.create&&(o.create??true))return {kind:'create',title:`No sessions ${where}`,line:''};
  if(h?.unlinked)return {kind:'unlinked',title:UNLINKED_STAFF.title,line:UNLINKED_STAFF.text};
  // A draft read by someone who does not edit it (an auditor) simply has no sessions here.
  return {kind:'none',title:o.published?`Nothing published ${where}`:`No sessions ${where}`,line:''};
}
export function scheduleEmpty(unit:string,o:EmptyOptions):HTMLElement{
  const h=o.hooks,w=scheduleEmptyWords(unit,o);
  switch(w.kind){
    case 'filtered':return empty(w.title,w.line,h?.onShowAll?button('Show all',h.onShowAll,'secondary'):null,'filter');
    case 'mine':{const node=empty(w.title,w.line,h?.onShowEveryone?button('Show everyone',h.onShowEveryone,'secondary'):null,'calendar');
      // L18: someone who reads only their own duties is offered the next one, once it is known (no next step otherwise).
      if(!h?.onShowEveryone)void h?.nextDuty?.().then(n=>{if(n)node.append(button(n.label,n.go,'secondary'));},()=>undefined);
      return node;}
    case 'create':return empty(w.title,w.line,h?.onCreate?withIcon(button(h.create!,h.onCreate,'primary'),'plus'):null,'plus');
    case 'unlinked':return empty(w.title,w.line,null,'lock');
    default:return empty(w.title,w.line,null,'calendar');
  }
}
/** A step of the period navigator not yet said: the view it draws says its new range (WCAG 4.1.3, QA-127d). */
let stepped=false;
export function scheduleNav(view:SchedRange,p:Period,today:string,move:(anchor:string)=>void,anchor:string|(()=>string),days=7,zone=''){
  const unit=UNIT[view],go=(a:string)=>{stepped=true;move(a);};
  if(stepped){stepped=false;announce(view==='week'?`Week of ${formatDate(p.days[0],'UTC')}`:periodLabel(view,p),true);}
  // Each step moves from the latest day asked for, not the one this navigator was drawn for: two quick presses while
  // the next week loads move two weeks (S-26).
  const nav=periodNav(periodLabel(view,p),unit,p,page=>go(stepAnchor(view,typeof anchor==='function'?anchor():anchor,page,days)));
  // The department's time zone, said only when it is not the viewer's own (S-33: not repeated in the footer).
  const own=Intl.DateTimeFormat().resolvedOptions().timeZone;
  if(zone&&zone!==own)nav.querySelector('strong')?.setAttribute('title',`Times in ${zone}`);
  const back=button('Today',()=>go(today),'secondary small',{'aria-label':'Go to today'});
  back.disabled=p.days.includes(today);
  nav.children[0]?.after(back);
  return nav;
}

/** The render layer also accepts a stored publication, whose rows use the snapshot format. */
export type ScheduleLayer=Pick<ScheduleWindow,'summary'|'starts_on'|'ends_on'> & Pick<WorkspaceData,'date_set_markers'|'shifts'> &
  {sources:Row[];coverage:Row[];findings:Row[];people:Record<string,Row>;locations:Record<string,Row>;services:Record<string,Row>};
/** One data set for every renderer. With a draft: its sessions, plus published ones as read-only context (`context`) on
 *  the days around it (and under it in Overlay). Without one: the published sessions with their stored checks. */
export function layered(draft:WorkspaceData|null,win:ScheduleLayer|null,mode:LayerMode,period:Period,zone:string,live?:Evaluation|null):WorkspaceData{
  const published=(win?.shifts||[]).map(s=>({...s,context:!!draft}));
  if(!draft){
    const validation=live||(win?.coverage.length&&{valid:!win.findings.some(f=>f.severity==='error'),error_count:win.findings.filter(f=>f.severity==='error').length,
      warning_count:win.findings.filter(f=>f.severity!=='error').length,findings:win.findings,coverage:win.coverage} as unknown as Evaluation)||null;
    return {workspace:{id:'published',revision:0,status:'published',starts_on:period.days[0],ends_on:addDays(period.days.at(-1)!,1),validation},shifts:published,date_set_markers:win?.date_set_markers,
      current_fingerprint:'',current_input_revision:0,current_availability_revision:0,approvals:[],review_progress:{route:null,steps:[],pending_step:null,complete:true}};
  }
  const w=draft.workspace,inDraft=(s:Row)=>{const d=toLocal(s.start,zone).slice(0,10);return d>=w.starts_on&&d<w.ends_on;};
  const spans=(win?.sources||[]).filter(s=>s.kind!=='draft').map(s=>[s.starts_on,s.in_force_until||s.ends_on] as [string,string]);
  return {...draft,context:true,published:spans,date_set_markers:win?.date_set_markers,shifts:[...draft.shifts,...published.filter(s=>mode==='overlay'||!inDraft(s))]};
}

// ---- Year and Month: one dot per day from the day summaries (GET /api/schedule/days) ----
type Source={id:string;kind:'published'|'draft';workspace_id:string;name:string;status:string;starts_on:string;ends_on:string;in_force_until:string|null;checked:boolean};
// `open`: sessions with a gap (legacy); `places_open`: open places, the rota summary's unit that the labels say.
// A session short of a skill (not of people) has a gap but no open place: then the sessions are said.
const placesOf=(e:{open:number;places_open?:number})=>e.places_open?plural(e.places_open,'open place'):plural(e.open,'session')+' short';
type DayEntry={source:string;sessions:number;open:number;places_open?:number;errors:number;warnings:number;issue:string|null;duties?:Row[]};
type Days={sources:Source[];days:Record<string,DayEntry[]>;date_set_markers?:Record<string,DateSetMarker[]>};
export interface CalendarOptions{
  view:'year'|'month';anchor:string;today:string;selected:string;
  /** The drafts drawn (the editable one and a compared one); published rotas always are. */
  drafts:string[];editId:string;
  pick:(day:string)=>void;open:(day:string,view:SchedView,draft?:string)=>void;
  /** Reads a day-summary address (the page reuses answers it already has). */
  read?:(path:string)=>Promise<unknown>;
}
const MONTHS=Array.from({length:12},(_,i)=>new Intl.DateTimeFormat('en-GB',{month:'long',timeZone:'UTC'}).format(new Date(Date.UTC(2026,i,15))));
/** Draws Year or Month; resolves with the number of days that need attention (for the footer). */
export async function calendarDays(ctx:Context,target:HTMLElement,o:CalendarOptions):Promise<{attention:number}>{
  const zone=ctx.me.organisation.timezone,ws=ctx.me.organisation.week_start;
  const first=o.view==='year'?o.anchor.slice(0,4)+'-01-01':weekOf(monthStart(o.anchor),ws);
  const end=o.view==='year'?`${Number(o.anchor.slice(0,4))+1}-01-01`:addDays(first,42);
  const path=`/api/schedule/days?start=${first}&end=${end}`,data=await (o.read?o.read(path):get(path)) as Days;
  const sources=new Map(data.sources.map(s=>[s.id,s]));
  const shown=(e:DayEntry)=>{const s=sources.get(e.source);return !!s&&(s.kind==='published'||o.drafts.includes(s.id));};
  const entriesOf=(day:string)=>(data.days[day]||[]).filter(shown);
  const key=legend(o.view==='year'?'Published and draft days':'Key');
  const toneOf=(list:DayEntry[])=>list.some(e=>e.errors)?'err':list.some(e=>e.open)?'warn':list.some(e=>!sources.get(e.source)!.checked&&sources.get(e.source)!.kind==='draft')?'unchecked':'';
  const TONE:Record<string,string>={'':'No issues',warn:'Open places',err:LEVEL_WORDS.must,unchecked:'Not checked yet'};
  const dot=(list:DayEntry[])=>{
    if(!list.length)return null;
    const state=list.length>1?'both':sources.get(list[0].source)!.kind==='draft'?'draft':'';const tone=toneOf(list);
    return stateDot(el('span',{class:`sd-dot ${tone} ${state}`.trim(),'aria-hidden':'true'}),state,tone);
  };
  // A day's dot is two entries: its fill (published, draft or both) and its colour (the day's worst state).
  const stateDot=(n:HTMLElement,state:string,tone:string)=>key.mark(key.mark(n,'s:'+state,el('span',{class:'sd-dot key '+state,'aria-hidden':'true'}),state==='both'?'Two rotas on one day':state==='draft'?'Draft':'Published',0),
    't:'+tone,el('span',{class:'sd-dot '+tone,'aria-hidden':'true'}),TONE[tone],1);
  const marks=(day:string)=>data.date_set_markers?.[day];
  const describe=(day:string,list:DayEntry[])=>`${formatDate(day,zone)}${marks(day)?.length?' · '+dateMarkWords(marks(day)):''}: ${list.length?list.map(e=>{const s=sources.get(e.source)!;
    return `${s.name}, ${s.kind==='draft'?'draft':'published'}, ${plural(e.sessions,'session')}${e.open?`, ${placesOf(e)}`:''}${e.errors?`, ${mustWords(e.errors)}`:''}`;}).join('; '):'not planned'}${day===o.today?', today':''}`;
  const edit=o.editId?sources.get(o.editId):undefined;
  // Opening a day keeps the draft being edited when it covers that day; another draft only when it does not (SR-08).
  const draftOn=(list:DayEntry[])=>o.editId&&list.some(e=>e.source===o.editId)?o.editId:list.map(e=>e.source).find(id=>sources.get(id)?.kind==='draft');
  const cells=new Map<string,HTMLButtonElement>();
  let selected=o.selected;
  const select=(day:string,focus=false)=>{
    const c=cells.get(day);if(!c)return;
    cells.get(selected)?.setAttribute('aria-pressed','false');cells.get(selected)?.setAttribute('tabindex','-1');
    selected=day;c.setAttribute('aria-pressed','true');c.setAttribute('tabindex','0');if(focus)c.focus();o.pick(day);renderPanel();};
  const dayButton=(day:string,cls:string,...content:(Node|string|null)[])=>{
    const list=entriesOf(day);
    const b=el('button',{type:'button',class:cls+(day===o.today?' today':'')+(isWeekend(day)?' weekend':'')+(marks(day)?.length?' has-date-mark':''),'aria-label':describe(day,list),...(marks(day)?.length&&o.view==='year'?{'data-tip':dateMarkWords(marks(day))}:{}),'aria-pressed':String(day===selected),tabindex:day===selected?0:-1,...(day===o.today?{'aria-current':'date'}:{})},...content) as HTMLButtonElement;
    b.addEventListener('click',()=>select(day));b.addEventListener('dblclick',()=>o.open(day,'week',draftOn(list)));
    // Month shades weekends and fills today's number on the cell (Year shades no weekend).
    if(o.view==='month'&&isWeekend(day))markWeekend(key,b);
    if(o.view==='month'&&day===o.today)key.mark(b,'today',el('span',{class:'lg-swatch lg-today-num','aria-hidden':'true'},String(Number(day.slice(8)))),'Today',5);
    // Year rings today and underlines a named date (its words are its tip): both are marks, so both are in the key.
    if(o.view==='year'&&day===o.today)key.mark(b,'today-ring',el('span',{class:'lg-swatch lg-today-ring','aria-hidden':'true'}),'Today',5);
    if(o.view==='year'&&marks(day)?.length)key.mark(b,'date-mark',el('span',{class:'lg-date-mark','aria-hidden':'true'},'7'),'Named date (holiday or date off)',5);
    cells.set(day,b);return b;
  };
  // One tab stop; arrows move by day and week, Page Up/Down by month, Enter opens the day (APG grid keys on a group of buttons).
  const keys=(e:KeyboardEvent)=>{
    const move:Record<string,number>={ArrowLeft:-1,ArrowRight:1,ArrowUp:-7,ArrowDown:7};
    let next=move[e.key]!==undefined?addDays(selected,move[e.key]):e.key==='PageUp'?monthStart(selected,-1):e.key==='PageDown'?monthStart(selected,1):e.key==='Home'?weekOf(selected,ws):e.key==='End'?addDays(weekOf(selected,ws),6):'';
    if(!next)return;e.preventDefault();if(!cells.has(next))next=[...cells.keys()].find(d=>d>=next)||selected;select(next,true);
  };
  const weekdays=Array.from({length:7},(_,i)=>new Intl.DateTimeFormat('en-GB',{weekday:o.view==='year'?'narrow':'short',timeZone:'UTC'}).format(new Date(addDays(weekOf(o.anchor,ws),i)+'T12:00:00Z')));
  let body:HTMLElement;
  if(o.view==='year'){
    body=el('div',{class:'sd-year'});
    for(let m=0;m<12;m++){
      const start=`${o.anchor.slice(0,4)}-${String(m+1).padStart(2,'0')}-01`,next=monthStart(start,1),lead=(new Date(start+'T12:00:00Z').getUTCDay()-((ws+1)%7)+7)%7;
      const grid=el('div',{class:'sd-yd',role:'group','aria-label':MONTHS[m]},...weekdays.map(w=>el('span',{'aria-hidden':'true'},w)),...Array.from({length:lead},()=>el('span',{'aria-hidden':'true'})));
      for(let d=start;d<next;d=addDays(d,1))grid.append(dayButton(d,'sd-yday',el('span',{class:'sd-num','aria-hidden':'true'},String(Number(d.slice(8)))),dot(entriesOf(d))||el('span',{class:'sd-dot none','aria-hidden':'true','data-lg-self':''})));  // an invisible placeholder that keeps the numbers in line
      const draftStarts=edit&&edit.starts_on.slice(0,7)===start.slice(0,7);
      body.append(el('section',{class:'sd-ym','aria-label':MONTHS[m]},el('div',{class:'sd-ym-head'},el('h2',{},MONTHS[m]),draftStarts?key.mark(el('small',{class:'sd-flag'},'Draft starts '+Number(edit!.starts_on.slice(8))),'start',el('span',{class:'sd-start-key','aria-hidden':'true'}),'Draft starts',3):null),grid));
    }
  }else{
    body=el('div',{class:'sd-month',role:'group','aria-label':periodLabel('month',{days:[monthStart(o.anchor)]} as Period)},...weekdays.map(w=>el('span',{class:'rc-head','aria-hidden':'true'},w)));
    const inMonth=(d:string)=>d.slice(0,7)===o.anchor.slice(0,7);
    for(let d=first;d<end;d=addDays(d,1)){
      const list=entriesOf(d),top=list.find(e=>e.errors)||list.find(e=>e.open);
      const text=!top?'':top.errors?(top.issue||mustWords(top.errors)):top.places_open?`${top.places_open} open`:`${top.open} short`;
      const duties=list.flatMap(e=>e.duties||[]);
      const cell=dayButton(d,'rc-cell'+(inMonth(d)?'':' empty')+(edit&&d===edit.starts_on?' sd-start':''),
        el('span',{class:'rc-date'},el('span',{class:'rc-num'},String(Number(d.slice(8)))),d.slice(8)==='01'?el('span',{class:'rc-month'},MONTHS[Number(d.slice(5,7))-1].slice(0,3)):null,duties.length?null:dot(list)),
        dateMarks(marks(d)),
        // The key shows each duty as its chip is drawn (colour and code), so a short code is never left unexplained.
        ...duties.slice(0,2).map(u=>key.mark(el('span',{class:'pchip duty-'+typeColour(u.assignment_type_id,u.colour||'')},el('span',{class:'pchip-name'},dutyCode(u))),
          'd:'+u.assignment_type_id,el('span',{class:'pchip lg-mark duty-'+typeColour(u.assignment_type_id,u.colour||''),'aria-hidden':'true'},dutyCode(u)),String(u.name),2)),
        text?el('span',{class:'sd-issue '+(top!.errors?'err':''),'aria-hidden':'true'},text):null,
        edit&&d===edit.starts_on?el('span',{class:'sd-flag','aria-hidden':'true'},'Draft starts'):null);
      if(edit&&d===edit.starts_on)key.mark(cell,'start',el('span',{class:'sd-start-key','aria-hidden':'true'}),'Draft starts',3);
      body.append(cell);
    }
  }
  body.addEventListener('keydown',keys);
  // The day's summary is a side panel like every other: the shared edge handle resizes it (kept per browser).
  const inner=el('div',{class:'sd-panel-inner','aria-live':'polite'});
  const panel=sidePanel(el('aside',{class:'rc-panel sd-panel'},inner),{key:'day-summary',width:320,dock:true});
  function renderPanel(){
    const list=entriesOf(selected),long=formatDay(selected,'UTC',true);
    const draftId=draftOn(list);
    const openWeek=el('button',{type:'button',class:'link-button'},'Open in Week',icon('chevronRight'));openWeek.addEventListener('click',()=>o.open(selected,'week',draftId));
    const openDay=el('button',{type:'button',class:'link-button'},'Open day',icon('chevronRight'));openDay.addEventListener('click',()=>o.open(selected,'day',draftId));
    inner.replaceChildren(el('div',{class:'rc-panel-head'},el('h2',{},long),dateMarks(marks(selected)),list.length?null:el('small',{class:'muted'},'Not planned')),
      list.length?el('div',{class:'rc-panel-body'},el('ul',{class:'rc-issues'},...list.map(e=>{const s=sources.get(e.source)!;
        const tone=e.errors?'err':e.open?'warn':'',state=s.kind==='draft'?'draft':'';
        return el('li',{class:'rc-issue'},stateDot(el('span',{class:`sd-dot ${tone} ${state}`.trim(),'aria-hidden':'true'}),state,tone),el('span',{class:'issue-text'},
          el('strong',{},`${s.name} · ${s.kind==='draft'?(s.status==='submitted'?statusLabel('submitted'):s.status==='approved'?'Approved':'Draft'):'Published'}`),
          el('small',{},[plural(e.sessions,(e.duties?'duty':'session'),e.duties?'duties':'sessions'),e.open?placesOf(e):'',e.errors?mustWords(e.errors):'',e.warnings?couldWords(e.warnings):''].filter(Boolean).join(' · ')),
          e.issue?el('small',{},e.issue):null,...(e.duties||[]).map(u=>el('small',{class:'mono'},`${formatTime(u.start,zone)}–${formatTime(u.end,zone)} · ${u.name}`))));}))):'',
      el('div',{class:'sd-panel-foot'},openWeek,openDay));
  }
  renderPanel();
  target.replaceChildren(el('div',{class:'sd-cal '+(o.view==='year'?'is-year':'is-month')},body,panel),key.root);
  return {attention:Object.keys(data.days).filter(d=>{const l=entriesOf(d);return l.some(e=>e.errors||e.open);}).length};
}
const clock=(t:string)=>Number(t.slice(0,2))*60+Number(t.slice(3,5));

// ---- Day: lanes × hours, published and draft bars, overnight carried over ----
/** `window`: the view's hours (Days & time); without one the axis fits the day's sessions (earliest start to latest end,
 *  an hour either side, at least six hours), so a day of clinics is not squeezed into a 24-hour axis. */
export function dayTimeline(ctx:Context,data:WorkspaceData,visible:Row[],period:Period,onPage:(page:number)=>void,hooks:WeekHooks,readOnly:(s:Row)=>void,nav:HTMLElement,window?:{start:string;end:string}|null){
  const zone=ctx.me.organisation.timezone,day=period.days[0],key=legend('Key');
  // Each bar says what the view's properties say (the one item renderer): its time first (the axis), then its words in
  // the view's order, its people where People sits; Colour by is the grids' rule (colourRule).
  const props=propertiesOf(weekView.props,'bar'),on=(k:PropKey)=>props.some(p=>p.key===k&&p.on),colours=colourRule(ctx,period.days,key);
  const words=new Set([...ITEM_DRAWS.bar].filter(k=>!['label','supervisor','lead','people'].includes(k)));
  const peopleAfter=(parts:ReturnType<typeof itemParts>)=>{const at=props.findIndex(p=>p.key==='people');return parts.filter(x=>props.findIndex(p=>p.key===x.key)<at).length;};
  // The check's findings by session, once per draw (each of the day's sessions read them several times: ~360,000 steps).
  const ofShift=new Map<string,Row[]>();for(const f of (data.workspace.validation?.findings||[]) as Row[])if(f.shift_id){const l=ofShift.get(f.shift_id);if(l)l.push(f);else ofShift.set(f.shift_id,[f]);}
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p])),locations=new Map((ctx.catalogues.locations||[]).map(l=>[l.id,l])),services=new Map((ctx.catalogues.services||[]).map(s=>[s.id,s]));
  const minute=(iso:string)=>{const l=toLocal(iso,zone);return (l.slice(0,10)<day?-1440:l.slice(0,10)>day?1440:0)+Number(l.slice(11,13))*60+Number(l.slice(14,16));};
  const shifts=visible.filter(s=>touchesDay(s,day,zone)&&(!weekView.onlyOpen||gapOf(data,s))&&(!window||minute(s.end)>clock(window.start)&&minute(s.start)<clock(window.end))).sort((a,b)=>String(a.start).localeCompare(String(b.start)));
  const first=Math.min(...shifts.map(s=>Math.max(0,minute(s.start)))),last=Math.max(...shifts.map(s=>Math.min(2880,minute(s.end))));
  const fitFrom=shifts.length?Math.max(0,Math.floor(first/60)*60-60):420,fitTo=shifts.length?Math.min(2880,Math.ceil(last/60)*60+60):fitFrom+1440;
  const fitSpan=Math.max(360,fitTo-fitFrom),from=window?clock(window.start):fitFrom,span=window?clock(window.end)-from:Math.ceil(fitSpan/(fitSpan>720?120:60))*(fitSpan>720?120:60);
  const groupOf=(s:Row)=>weekView.groupBy==='type'?s.type_snapshot.name:weekView.groupBy==='service'?(s.service_ids as string[]).map(id=>labelOf(services.get(id))).sort().join(' + ')||'No service':weekView.groupBy==='none'?'All sessions':labelOf(locations.get(s.location_id));
  const groups=new Map<string,Row[]>();for(const s of shifts){const g=groupOf(s);groups.set(g,[...(groups.get(g)||[]),s]);}
  const overlay=!!data.context&&shifts.some(s=>s.context)&&shifts.some(s=>!s.context);
  // What a draft session changes from the published one at the same time and place.
  const same=(a:Row,b:Row)=>a.assignment_type_id===b.assignment_type_id&&a.location_id===b.location_id&&a.start===b.start&&a.end===b.end;
  const diff=(s:Row)=>{if(!overlay||s.context)return '';const p=shifts.find(x=>x.context&&same(x,s));if(!p)return 'New';
    return [...s.participants.map((x:Row)=>x.person_id)].sort().join()===[...p.participants.map((x:Row)=>x.person_id)].sort().join()?'':'Changed';};
  const hh=(m:number)=>String(Math.floor(((m%1440)+1440)%1440/60)).padStart(2,'0')+':00';
  const step=span>720?120:60,axis=el('div',{class:'sd-axis','aria-hidden':'true'},...Array.from({length:Math.ceil(span/step)},(_,i)=>el('span',{},hh(from+i*step))));
  axis.style.setProperty('--ticks',String(Math.ceil(span/step)));
  const grid=el('div',{class:'sd-tl',role:'table','aria-label':`Sessions on ${formatDate(day,zone)} by ${({service:'service',type:'duty type',location:'location',none:'session'} as Record<string,string>)[weekView.groupBy]}`},
    el('div',{class:'wk-corner',role:'columnheader'},({service:'Service',type:'Duty',location:'Location',none:''} as Record<string,string>)[weekView.groupBy]),axis);
  let changes=0;
  for(const [name,list] of [...groups.entries()].sort((a,b)=>byValue(a[0],b[0],groupWay(weekView.groupBy)))){
    const track=el('div',{class:'sd-track'+(overlay?' overlay':''),role:'cell'});track.style.setProperty('--ticks',String(Math.ceil(span/step)));  // gridlines under the axis ticks
    // Sessions at the same time stack in lanes (first lane free by then), never on top of each other; with the
    // published layer shown, each layer packs its own lanes (published upper half, draft lower half).
    const lanes:Record<string,number[]>={};let laneCount=1;
    for(const s of list){
      const a=Math.max(0,minute(s.start)-from),b=Math.min(span,minute(s.end)-from);if(b<=0||a>=span)continue;
      const ends=lanes[overlay&&s.context?'published':'draft']??=[];let lane=ends.findIndex(e=>e<=a);if(lane<0)lane=ends.length;ends[lane]=b;laneCount=Math.max(laneCount,ends.length);
      // Each supervisor once, their trainees in brackets (planner_teams.ts); the shared TL mark.
      const tl=(p:Row)=>on('lead')&&p.team_lead?' '+MARKS.lead[0]:'',nameOf=(id:string)=>hooks.name(people.get(id))||'named supervisor';
      const drawn=drawnPeople(s),hidden=drawn.hidden.length;  // a person display's others: counted (Search › Show)
      // Teams as one-line team boxes (team_view.ts) where the bar is tall enough; the half bars of an overlay say them in words.
      const errs=(pid:string)=>(ofShift.get(s.id)||[]).filter(f=>f.person_id===pid&&f.severity==='error');
      const day0=toLocal(s.start,zone).slice(0,10),rank=(id:string)=>rankOn(ctx.catalogues,id,day0);
      const teams=on('people')&&showPairs('bar')?teamGroups(drawn.shown,s.participants as Row[]).map(t=>({t,problem:teamProblem(t,s.participants as Row[],errs)})).filter(x=>x.t.length>1||x.problem):[];
      const sessionErrs=(ofShift.get(s.id)||[]).filter(f=>f.severity==='error');
      // Every rule problem here is one a drawn team says: no "Needs role" badge repeating it.
      const told=!overlay&&teams.some(x=>x.problem)&&sessionErrs.length>0&&sessionErrs.every(f=>BOX_CODES.has(String(f.code)));
      const boxed=overlay?[]:teams,inBox=new Set(boxed.flatMap(x=>x.t.map(e=>e.p))),rest=drawn.shown.filter(p=>!inBox.has(p));
      const who=[(showPairs('bar')?teamText(rest,nameOf,tl,s.participants as Row[]):drawn.shown.map(p=>nameOf(p.person_id)+tl(p)).join(' · ')),hidden?`+${hidden} others`:''].filter(Boolean).join(' · ')||'No one yet';
      // A screen reader hears everyone in full, whatever label the bar shows (the hover card lists them too).
      const fullWho=[teamText(s.participants as Row[],id=>fullNameOf(people.get(id))||'named supervisor',tl),hidden?`${hidden} others`:''].filter(Boolean).join(' · ')||'No one yet';
      const gap=gapOf(data,s),open=Math.max(0,gap),change=diff(s);if(change)changes++;
      // The same issue marks as the Week grid (S-15): blocking rule findings and double bookings, in icon, count and words.
      const issues=sessionErrs.length+clashesOf(s).length;
      const layer=s.context||data.workspace.status==='published'?'published':'draft';
      const parts=itemParts(ctx,s,'',zone,props,{only:words}),time=parts.find(x=>x.key==='time'),said=itemWords(parts.filter(x=>x!==time&&x.key!=='duty'));  // the name says both
      const text=itemNodes(parts.filter(x=>x!==time),key,()=>'sd-part');
      const who0=on('people')&&!(boxed.length&&!rest.length&&!hidden)?[el('span',{class:'sd-part','data-part':'people'},who)]:[];
      text.splice(peopleAfter(parts.filter(x=>x!==time)),0,...who0);
      const bar=el('button',{type:'button',class:`sd-bar${layer==='draft'?' draft':''}${overlay?' half'+(layer==='draft'?' lower':''):''}${s.cancelled?' cancelled':''}`,
        'aria-label':`${layer==='draft'?'Draft':'Published'}, ${s.type_snapshot.name}, ${formatTime(s.start,zone)} to ${formatTime(s.end,zone)}${said.map(x=>', '+x).join('')}${on('people')?', '+fullWho:''}${open?`, ${plural(open,'open place')}`:gap<0?', every place filled, a team lead, skill or supervisor is missing':''}${issues?`, ${plural(issues,'issue')}`:''}${change?', '+change.toLowerCase():''}`},
        time?el('time',{'data-part':'time'},time.text):null,text.length?el('span',{class:'truncate'},...text.flatMap((n,i)=>i?[' · ',n]:[n])):null,
        ...boxed.map(x=>teamView(x.t,{density:'row',presentational:true,key,nameOf:id=>people.has(id)?hooks.name(people.get(id)):'Named supervisor',fullName:id=>fullNameOf(people.get(id))||'Named supervisor',rankOf:rank,problem:x.problem,layout:teamLayout()})),
        issues?(()=>{const g=issueGlyph('error',issues);g.classList.add('sd-issue');return key.mark(g,'issue',issueGlyph('error'),'Must fix: breaks a rule or double-booked',2);})():null,
        open?key.mark(badge('+'+open,'warn'),'open',badge('+1','warn'),'Open places',3):gap<0&&!told?badge('Needs role','warn'):null,change?key.mark(badge(change,'info'),'diff',badge('Changed','info'),'Differs from published',3):null);
      // The bar is its layer (fill or dashed), its colour (Colour by, colourRule), and the TL, pairs and closed it says.
      const hue=colours.seen(s,bar);bar.classList.add('duty-'+hue);
      key.mark(bar,'l:'+layer,el('span',{class:`sd-bar key ${layer==='draft'?'draft':''} duty-${hue}`,'aria-hidden':'true'}),layer==='draft'?'Draft':'Published',0);
      if(on('lead')&&s.participants.some((p:Row)=>p.team_lead))key.mark(bar,'tag:lead',el('abbr',{class:'wk-tag lead'},MARKS.lead[0]),MARKS.lead[1],2);
      if(showPairs('bar')&&overlay&&s.participants.some((p:Row)=>p.supervisor_person_id))key.mark(bar,'pair',el('span',{class:'pchip-with','aria-hidden':'true'},'(supervising …)'),'A supervisor and their named trainees',2);
      if(s.cancelled)key.mark(bar,'closed',el('span',{class:'sd-bar key cancelled lg-mark duty-'+hue,'aria-hidden':'true'},'08:00'),'Session closed',3);
      bar.style.setProperty('--lane',String(lane));bar.style.setProperty('left',`calc(${(a/span*100).toFixed(2)}% + 3px)`);bar.style.setProperty('width',`calc(${((b-a)/span*100).toFixed(2)}% - 6px)`);
      bar.addEventListener('click',()=>s.context||hooks.open?(s.context?readOnly(s):hooks.open!(s)):hooks.details(s));
      hoverCard(bar,()=>[el('div',{class:'hc-head'},el('div',{},el('strong',{},s.type_snapshot.name),el('small',{},`${formatTime(s.start,zone)}–${formatTime(s.end,zone)} · ${labelOf(locations.get(s.location_id))}`))),el('ul',{class:'hc-people'},...teamGroups(s.participants as Row[]).map(t=>el('li',{},teamView(t,{density:'card',nameOf:id=>fullNameOf(people.get(id))||'Named supervisor',rankOf:rank,
        problem:teamProblem(t,s.participants as Row[],errs),layout:teamLayout(),member:e=>personPill({label:fullNameOf(people.get(e.p.person_id))+tl(e.p)})}))))]);
      track.append(bar);
    }
    track.style.setProperty('--lanes',String(laneCount));
    grid.append(el('div',{class:'wk-rowhead',role:'rowheader'},el('span',{class:'clamp-2',title:name},name),el('small',{},plural(list.length,'session'))),track);
  }
  void onPage;
  const body=shifts.length?el('div',{class:'table-scroll sd-tl-scroll'},grid):scheduleEmpty('day',{filtered:(hooks.unfiltered||data.shifts).some(s=>touchesDay(s,day,zone)),hooks,published:data.workspace.status==='published'});
  return {root:el('div',{class:'sd-day'},nav,dateMarks(data.date_set_markers?.[day]),body,key.root),changes};
}
const coverCache=new WeakMap<object,Map<string,number>>();
function gapOf(data:WorkspaceData,s:Row){
  const v=data.workspace.validation;if(!v)return 0;
  // Open places as the rota summary counts them (TL-02): a gap with every place filled (a missing team lead, skill or
  // supervisor) is -1 here, short but with no open place to fill.
  let m=coverCache.get(v);if(!m){m=new Map();for(const r of v.coverage||[])if(r.status==='gap'){const n=Math.max(0,(r.required??1)-(r.available_eligible??0));m.set(r.shift_id,Math.max(m.get(r.shift_id)??-1,n||-1));}coverCache.set(v,m);}
  return m.get(s.id)||0;
}

// ---- Overlap: where a draft fits beside the published rotas (GET /api/schedule/overlaps) ----
export type Overlaps={starts_on:string;ends_on:string;publications:Row[];drafts:Row[];conflict:boolean;options:Row[]};
export type OverlapValue={choice:'start_after'|'replace_from'|'amend';starts_on:string;publication_id?:string};
const lastDay=(exclusive:string)=>addDays(exclusive,-1);
/** The three ways to place a rota beside a published one, as a radio group. One component for New draft, the footer's
 *  overlap pill and Publish…. Unavailable options stay visible with the server's reason. `keep`: the choice already
 *  made, kept when the group is drawn again for new dates (if it is still available). */
export function overlapChoice(o:Overlaps,zone:string,onChange:()=>void,legendText='How should the draft fit?',keep=''){
  const fmt=(d:string)=>formatDate(d,zone),pub=o.publications.find(p=>p.relation==='conflict')||o.publications[0];
  const name=pub?.name||'The published rota';
  const all=[pub?.starts_on,o.starts_on].filter(Boolean).sort()[0],stop=[pub?(pub.effective_until||pub.ends_on):'',o.ends_on].sort().at(-1)!;
  const pos=(d:string)=>Math.max(0,Math.min(100,(Date.parse(d)-Date.parse(all))/(Date.parse(stop)-Date.parse(all))*100));
  const seg=(cls:string,a:string,b:string,text:string)=>{const n=el('span',{class:cls},text);n.style.setProperty('left',pos(a)+'%');n.style.setProperty('width',Math.max(0,pos(b)-pos(a))+'%');return n;};
  const mini=(label:string,...tracks:HTMLElement[][])=>el('span',{class:'sd-mini',role:'img','aria-label':label},...tracks.map(t=>el('span',{class:'sd-mini-track'},...t)),
    el('span',{class:'sd-mini-axis','aria-hidden':'true'},...[all,stop].map((d,i)=>{const n=el('span',{},fmt(i?lastDay(d):d));n.style.setProperty(i?'right':'left','0');return n;})));
  const group='ov-'+Math.random().toString(36).slice(2,8);
  const opts=o.options;const preferred=opts.find(x=>x.choice===keep&&x.available)||opts.find(x=>x.default&&x.available)||opts.find(x=>x.available);
  let date:HTMLInputElement|null=null;
  const describe=(x:Row)=>{
    // Not available (the published rota covers every day): no range to state; the server's reason says why.
    if(x.choice==='start_after'&&!x.available)return {title:'Start after it',text:'',mini:null};
    if(x.choice==='start_after')return {title:'Start after it',text:`The draft runs ${fmt(x.starts_on)} – ${fmt(lastDay(x.ends_on))}. ${name} stays as published.`,
      mini:mini(`${name} until ${fmt(lastDay(x.starts_on))}, draft from ${fmt(x.starts_on)}`,[seg('sd-mini-pub',pub.starts_on,x.starts_on,name),seg('sd-mini-draft',x.starts_on,x.ends_on,'Draft')])};
    if(x.choice==='replace_from')return {title:'Replace from a date',text:'',mini:null};
    return {title:'Change the published rota',text:`Edit ${name} as an amendment instead of a new draft. Staff keep the published version until the change is approved and published.`,mini:null};
  };
  const items=opts.map(x=>{
    const d=describe(x),input=el('input',{type:'radio',name:group,value:x.choice,disabled:!x.available}) as HTMLInputElement;input.checked=x===preferred;
    const body=el('span',{class:'sd-option-body'},el('strong',{},d.title,x.default?badge('Default'):null),d.text?el('span',{},d.text):null);
    if(x.choice==='replace_from'&&x.available){
      date=el('input',{type:'date',value:x.starts_on,min:x.earliest,max:x.latest,'aria-label':'Takes over from'}) as HTMLInputElement;
      const words=el('span',{}),track=el('span',{});
      const draw=()=>{const v=date!.value||x.starts_on;words.textContent=`${name} stays in force until ${fmt(lastDay(v))}; this draft takes over from ${fmt(v)}.`;
        track.replaceChildren(mini(`${name} in force until ${fmt(lastDay(v))}, replaced from ${fmt(v)}; draft from ${fmt(v)}`,
          [seg('sd-mini-pub',pub.starts_on,v,name),seg('sd-mini-gone',v,pub.effective_until||pub.ends_on,'Replaced')],[seg('sd-mini-draft',v,x.ends_on,'Draft')]));};
      date.addEventListener('input',()=>{input.checked=true;sync();draw();onChange();});draw();
      body.append(el('label',{class:'field sd-date'},el('span',{},'Takes over from'),dateInput(date)),words,track);
    }else if(d.mini)body.append(d.mini);
    if(!x.available&&x.why)body.append(el('small',{class:'muted'},x.why));
    const label=el('label',{class:'sd-option'+(x.available?'':' disabled')},input,body);
    input.addEventListener('change',()=>{sync();onChange();});
    return {x,input,label};
  });
  const sync=()=>items.forEach(i=>i.label.classList.toggle('on',i.input.checked));sync();
  const pubs=o.publications.map(p=>el('div',{class:'rc-issue'},el('span',{class:'sd-dot','aria-hidden':'true'}),el('span',{class:'issue-text'},el('strong',{},`${p.name} · Published`),
    el('small',{},`${fmt(p.starts_on)} – ${fmt(lastDay(p.effective_until||p.ends_on))} · overlaps ${fmt(p.overlap.starts_on)} – ${fmt(lastDay(p.overlap.ends_on))} (${plural(Math.round((Date.parse(p.overlap.ends_on)-Date.parse(p.overlap.starts_on))/864e5),'day')})`))));
  const competing=o.drafts.length?note(`Also being planned for these days: ${o.drafts.map(d=>`${d.name} (${d.status==='draft'?'draft':d.status==='submitted'?statusLabel('submitted').toLowerCase():'approved'}, ${fmt(d.overlap.starts_on)} – ${fmt(lastDay(d.overlap.ends_on))})`).join(', ')}. You can compare them side by side.`,'info'):null;
  const root=el('fieldset',{class:'form-section span-2 sd-fit'},el('legend',{},legendText),...pubs,el('div',{class:'sd-options',role:'radiogroup','aria-label':legendText},...items.map(i=>i.label)),competing||'');
  const value=():OverlapValue|null=>{const i=items.find(i=>i.input.checked);if(!i)return null;
    return {choice:i.x.choice,starts_on:i.x.choice==='replace_from'&&date?.value?date.value:i.x.starts_on,publication_id:i.x.publication_id};};
  /** The primary button repeats the choice ("Create draft from 2 Nov"). */
  const verb=(prefix:string)=>{const v=value();return !v?prefix:v.choice==='amend'?'Change the published rota':`${prefix} from ${new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(v.starts_on+'T12:00:00Z'))}`;};
  return {root,value,verb,needed:o.publications.some(p=>p.relation==='conflict')};
}
