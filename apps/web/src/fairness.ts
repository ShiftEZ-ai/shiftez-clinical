import {skeleton,observeSize} from './loading.js';
import {el,svg,sparkline,legend,icon,select,field,multiPick,button,popover,footerStatus,empty,emptyLine,note,badge,sectionHead,panelSearch,showDialog,readFailed,table,dateUnits,formatDate,formatTime,formatDay,labelOf,plural,withIcon,check,notify,reportError,type Child,type Choice,CAPABILITY} from './dom.js';
import {nameText,personName,spokenName,fullNameOf} from './person_label.js';
import {dateRange,lastDayIncluded} from './date_picker.js';
import {request,keptRead,keptAnswers,type KeptReadOptions,type ApiError,type Undone} from './api.js';
import {conditionBuilder,conditionsText,RULE_FIELDS,SESSION_FIELDS,type ConditionField} from './conditions.js';
import {ruleConditionFields,type Context} from './forms.js';
import {tagLabel} from './tags.js';
import {fetchViews,openingView,savedViewItems,collectionToolbar,type SavedView} from './table_view.js';
import {tableColumnMenus} from './table_menu.js';
import {recordMatcher,type SearchOptions} from './search.js';
import {scoreWhy,scoreTone,COMPONENT_LABELS} from './score.js';
import {levelWords} from './rule_view.js';
import {dockPanel} from './side_panel.js';
import {METRIC_WORDS} from './balance_goal.js';
import {periodNav} from './planner_week.js';
import {RULE_CATALOGUE_V2,type Conditions,type FairnessViewSettings} from './generated.js';
import type {Row} from './types.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {LONG_LIST} from './picker.js';

/* The fairness dashboard: schedule score and its trend, Balance goals, each person's share of any measure against
 * their expected band, monthly overtime and lead-turn trends, and the exposure matrix. Every number comes from
 * /api/fairness/* (packages/domain/fairness.py and score.py); this module only draws it. Staff see what the API
 * returns for them: their own rows. Charts are plain HTML/SVG with numbers in text, never colour alone. */

export interface FairnessLinks {publication?:(id:string)=>void;  // absent: the viewer cannot open published rotas
  /** R-3: the rota in focus (the one the Schedule last opened), for the goals' default month. */focus?:()=>Row|null}
type Section='score'|'goals'|'distribution'|'people'|'trends'|'exposure';
type Period='last_3_months'|'last_6_months'|'last_12_months'|'this_year'|'next_3_months'|'custom';
// Count only one kind of session (the ledger keeps one filter per key): duty types, clinics or locations. `within`: a
// location counts the locations inside it too. `trend_who`: the people drawn as lines (none: the furthest from share).
type View={period:Period;starts_on?:string|null;ends_on?:string|null;who?:Conditions|null;metric:string;assignment_type_ids:string[];service_ids:string[];
  location_ids:string[];within:boolean;group_by:string;rows?:Conditions|null;cols?:Conditions|null;trend_who?:Conditions|null;pairing:'present'|'supervised';
  sort:'name'|'value'|'gap';hidden:Section[];density:'comfortable'|'compact'};
type WorkFilter='assignment_type_ids'|'service_ids'|'location_ids';

// The measures the page offers, in order: exactly the contract's (an unknown or missing key fails the build).
const SHOWN:Record<NonNullable<FairnessViewSettings['metric']>,0>={duties:0,hours:0,session_units:0,weekends:0,overtime_duties:0,overtime_hours:0,lead_turns:0,
  supervision:0,wishes_granted:0,wishes_declined:0,wishes_submitted:0};
export const MEASURES:Choice[]=(Object.keys(SHOWN) as (keyof typeof SHOWN)[]).map(value=>({value,label:METRIC_WORDS[value]}));
const WISH=(m:string)=>m.startsWith('wishes_');
/** How a distribution's expected share is worked out, from its `basis` (a goal's own, or the reference's) and a goal's
 *  share_by, groups (comparison: the catalogue's words) and window. */
const SHARED_BY:Record<string,string>={available_days:'Shared by days each person is available',offered:'Shared by overtime each person offered',
  supervisable_sessions:'Shared by sessions each person can supervise',equal:'Shared equally'};
const GROUP_WORDS=RULE_CATALOGUE_V2.find(e=>e.id==='fair_share')?.setting_option_labels?.comparison||{};
const basisWords=(d:Row,zone:string)=>d.basis==='goal'?[SHARED_BY[d.share_by]||'',GROUP_WORDS[d.comparison]||'',
    d.period_start&&d.period_end?`${formatDate(d.period_start,zone)} – ${formatDate(addDay(d.period_end,-1),zone)}`:''].filter(Boolean).join(' · ')
  :`Reference (not a goal) · ${d.basis==='reference_offered'?'pro rata to overtime offered':'pro rata to days available'}`;
/** The figure each person's goal share is worked out from, as a table column (equal shares have none). */
const BASIS_COLUMN:Record<string,[string,string,number]>={available_days:['Days available','available_days',0],offered:['Overtime offered (h)','overtime_offered_hours',1],
  supervisable_sessions:['Sessions they can supervise','supervisable_sessions',0]};
/** A goal named once ("Weekly clinical target", never "… target target"). */
const goalName=(goal:Row)=>String(goal.name).replace(/\b(\w+) \1$/i,'$1');
// Rotas are published ahead of today, so the "past" periods also take in next month.
const PERIODS:Choice[]=[{value:'last_3_months',label:'Past 3 months + next month'},{value:'last_6_months',label:'Past 6 months + next month'},{value:'last_12_months',label:'Past 12 months + next month'},
  {value:'this_year',label:'This year'},{value:'next_3_months',label:'Next 3 months'},{value:'custom',label:'Custom dates'}];
// Group by who people are or by what the work was: the condition fields (one list of names for the whole app).
const GROUPS:Choice[]=[{value:'',label:'No grouping'},...RULE_FIELDS.filter(f=>!['person_ids','usual_assignee_ids','date_set_ids','date'].includes(f.key)).map(f=>({value:f.key,label:f.label,group:SESSION_FIELDS.includes(f.key)?'What the work was':'Who they are'}))];
const WORK:WorkFilter[]=['assignment_type_ids','service_ids','location_ids'];
const fieldLabel=(k:string)=>RULE_FIELDS.find(f=>f.key===k)?.label||k;
const SECTIONS:[Section,string][]=[['score','Schedule score'],['goals','Balance goals'],['distribution','Distribution'],['people','Trends per person'],['trends','Overtime & team lead turns'],['exposure','Exposure']];
/** A section's name in a sentence ("Couldn’t load balance goals."). */
const sectionName=(k:Section)=>SECTIONS.find(([s])=>s===k)![1].toLowerCase();
/** Lines drawn at most: more is noise; the rest stay in the table. */
export const TREND_LINES=8;
const PERSON_FIELDS=new Set(['person_ids','category_ids','capability_ids','programme_ids','rotation_type_ids','cohort_ids','tags','eligible_service_ids']);
const DEFAULT:View={period:'last_12_months',metric:'duties',assignment_type_ids:[],service_ids:[],location_ids:[],within:true,group_by:'',pairing:'present',sort:'gap',hidden:[],density:'comfortable',who:null,rows:null,cols:null,trend_who:null};
const measureLabel=(m:string)=>METRIC_WORDS[m as keyof typeof METRIC_WORDS]||m;
const num=(n:number|null|undefined,d=1)=>n==null?'—':String(Math.round(n*10**d)/10**d);
/** A fair-share target as a person says it (maintenance): "5", or "about 5" for a share like 5.3; the drill-down tables keep num. */
const aim=(n:number|null|undefined)=>n==null?'—':Math.abs(n-Math.round(n))<0.05?String(Math.round(n)):`about ${Math.round(n)}`;
const signed=(n:number,d=1)=>(n>0?'+':n<0?'−':'±')+num(Math.abs(n),d);

type Saved={fairness:View};
let drawSerial=0;
// maintenance: answers are kept per URL across rebuilds of the page (api.ts keptRead). The rota list and the goals follow the
// rota in focus, a draft too: any change drops them. Every other answer counts published rotas only: a draft's own change
// (scope workspace, an auto-schedule's jobs) keeps it. shortcut: 24 newest answers in each; past that, the oldest read again.
const FOLLOWS_DRAFTS=/^\/api\/(?:home\/rotas\?|fairness\/goals(?:\?|$))/;
const DRAFT_READS:KeptReadOptions={keepScopes:[],cap:24},PUBLISHED_READS:KeptReadOptions={keepScopes:['workspace','jobs'],cap:24};
const readsOf=(path:string)=>FOLLOWS_DRAFTS.test(path)?DRAFT_READS:PUBLISHED_READS;
/** The page's one read of a URL: kept, or shared while on its way. (Exported for tests/test_client_fairness_reads.py.) */
export const fairnessRead=(path:string):Promise<Row>=>keptRead<Row>(path,signal=>request<Row>(path,{signal}),readsOf(path)).p;
// `find`: the people search over the Distribution list (not part of a saved view).
// `goalPick`: the goal the Distribution shows (its own figures), '' for the reference; this visit only.
let view:View={...DEFAULT},viewId='',saved:SavedView<Saved>[]|null=null,find='',findOptions:SearchOptions|undefined,goalPick='';
/** R-3: the month whose rota the score and Balance goals show ('YYYY-MM'; '' = the rota in focus, else this month). */
let rotaMonth='';
let periodChosen=false;
/** Sign-out: forget the chosen view and the saved views (they belong to the account). */
export function resetFairness(){view={...DEFAULT};viewId='';saved=null;find='';findOptions=undefined;goalPick='';rotaMonth='';periodChosen=false;}
/** A checked Undo must replace this dashboard's own view and list cache before redraw. */
export function restoreFairnessViews(done:Undone){
  for(const item of done){
    const restored=item.kind.startsWith('table_view.')&&item.result?.view;
    if(!restored||restored.table_key!=='fairness')continue;
    view=normal(restored.definition.fairness);viewId=restored.id;saved=null;rotaMonth='';
  }
}
const clone=(v:View):View=>JSON.parse(JSON.stringify(v));
/** A view as the server keeps it (defaults filled, unset dates null), so saved and current compare equal. */
const normal=(v:Partial<View>|undefined):View=>{const x={...DEFAULT,starts_on:null,ends_on:null,...clone((v||{}) as View)};return Object.fromEntries(Object.keys(x).sort().map(k=>[k,(x as Record<string,unknown>)[k]??null])) as unknown as View;};
/** The chosen period as [first day, day after the last], in the department's time zone. */
export function periodDates(v:Pick<View,'period'|'starts_on'|'ends_on'>,today:string):[string,string]{
  const y=+today.slice(0,4),m=+today.slice(5,7),month=(yy:number,mm:number)=>new Date(Date.UTC(yy,mm-1,1)).toISOString().slice(0,10);
  const back=(n:number):[string,string]=>[month(y,m-n+1),month(y,m+2)];
  switch(v.period){
    case 'last_3_months':return back(3);case 'last_6_months':return back(6);case 'this_year':return [`${y}-01-01`,`${y+1}-01-01`];
    case 'next_3_months':return [month(y,m),month(y,m+3)];case 'custom':return [v.starts_on||month(y,m-11),v.ends_on||month(y,m+1)];
    default:return back(12);
  }
}

/** Query string for the dashboard reads; wish measures are never filtered by duty type or clinic (the API refuses). */
/** 'YYYY-MM' moved by n months. */
const addMonthsTo=(month:string,n:number)=>{const d=new Date(Date.UTC(+month.slice(0,4),+month.slice(5,7)-1+n,1));return d.toISOString().slice(0,7);};
const monthAfter=(month:string)=>addMonthsTo(month,1);
function query(extra:Record<string,string|string[]|null|undefined>):string{
  const p=new URLSearchParams();
  for(const [k,v] of Object.entries(extra)){if(v==null||v==='')continue;for(const x of Array.isArray(v)?v:[v])p.append(k,x);}
  return p.toString();
}
const tree=(t:Conditions|null|undefined)=>t?.conditions?.length?JSON.stringify(t):null;
/** The count-only filter as query parameters (`within` only matters with locations). */
const workParams=(v:View)=>({assignment_type_ids:v.assignment_type_ids,service_ids:v.service_ids,location_ids:v.location_ids,within:v.location_ids.length?String(v.within):null});

// ---- small charts ------------------------------------------------------------------------------------------------

/** A chart drawn for its own box (I-06): the viewBox is the box's width, so 1 unit is 1 px and the 11 px ticks and
 *  24 px targets keep their size whatever the window or a docked panel does. It draws again when the width changes. */
function boxChart(draw:(width:number)=>SVGSVGElement,drawn?:(chart:SVGSVGElement)=>void):HTMLElement{
  const holder=el('div',{class:'fx-fit'});let width=0;
  const put=(w:number)=>{width=w;const chart=draw(w);holder.replaceChildren(chart);drawn?.(chart);};
  put(typeof innerWidth==='number'&&innerWidth<600?340:560);  // before layout: a sensible first size
  // maintenance: the new chart changes the holder's own height: observeSize draws it in the next frame, never inside the
  // observer's delivery.
  observeSize(holder,()=>{const w=Math.round(holder.clientWidth);if(w&&Math.abs(w-width)>1)put(Math.max(240,w));});
  return holder;
}
/** A score's band in words: the key's entries and the one-rota sentence. */
const SCORE_TONE:Record<string,string>={ok:'85 or more',neutral:'60 to 84',warn:'Below 60',err:'Not publishable'};
/** One published rota: a chart of one dot says nothing (maintenance), so its score and band in words, the rota a link. */
function oneScore(p:Row,open:(p:Row)=>void,zone:string):HTMLElement{
  return el('p',{class:'fx-one'},button(String(p.name),()=>open(p),'link-button'),` (from ${formatDate(p.starts_on,zone)}) scored `,el('strong',{},num(p.score)),
    ` of 100: ${SCORE_TONE[scoreTone(p)].toLowerCase()}. The trend shows from the second published rota.`);
}
/** Score over publications (two or more): 0-100 axis with the 60 / 85 lines, one focusable point per publication (opens it). */
function scoreTrend(points:Row[],open:(p:Row)=>void,zone:string):HTMLElement{
  // The axis starts a little below the lowest score (at most 60, the publishable line), so small moves show.
  const lo=Math.max(0,Math.min(60,Math.floor((Math.min(...points.map(p=>p.score))-5)/10)*10));
  const key=legend(),TONE=SCORE_TONE;
  const draw=(w:number)=>{
  const h=140,l=30,r=10,t=10,b=24,x=(i:number)=>points.length<2?(l+w-r)/2:l+i*(w-l-r)/(points.length-1),y=(v:number)=>t+(1-(v-lo)/(100-lo))*(h-t-b);
  const chart=svg('svg',{viewBox:`0 0 ${w} ${h}`,class:'fx-trend',role:'group','aria-label':'Schedule score by published rota'});
  for(const v of [...new Set([lo,60,85,100])].filter(v=>v>=lo))chart.append(svg('line',{x1:l,x2:w-r,y1:y(v),y2:y(v),class:v===lo?'fx-axis':'fx-gridline'}),svg('text',{x:l-6,y:y(v)+4,class:'fx-tick','text-anchor':'end'},document.createTextNode(String(v))));
  if(points.length>1)chart.append(svg('polyline',{points:points.map((p,i)=>`${x(i)},${y(p.score)}`).join(' '),class:'fx-line'}));
  // As many date labels as fit (about one per 70 px), never crowded.
  const every=Math.max(1,Math.ceil(points.length/Math.max(2,Math.floor((w-l-r)/70))));
  points.forEach((p,i)=>{
    const text=`${p.name}: ${p.score} of 100${p.publishable?'':', not publishable'}`;
    // A 24 px target (WCAG 2.5.8) around the 10 px dot it draws.
    const hit=svg('circle',{cx:x(i),cy:y(p.score),r:12,class:'fx-hit',tabindex:0,role:'button','aria-label':text+'. Open this rota'},svg('title',{},document.createTextNode(text)));
    hit.addEventListener('click',()=>open(p));hit.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open(p);}});
    chart.append(hit,key.mark(svg('circle',{cx:x(i),cy:y(p.score),r:5,class:'fx-point '+scoreTone(p),'aria-hidden':'true'}),scoreTone(p),'lg-dot '+scoreTone(p),TONE[scoreTone(p)],Object.keys(TONE).indexOf(scoreTone(p))));
    if(i%every===0)chart.append(svg('text',{x:x(i),y:h-6,class:'fx-tick','text-anchor':'middle'},document.createTextNode(formatDate(p.starts_on,zone).replace(/ \d{4}$/,''))));
  });
  return chart;};
  return el('div',{class:'fx-chart'},boxChart(draw),key.root,asTable('Scores as a table',['Rota','Starts','Score','Publishable'],points.map(p=>[p.name,dateUnits([formatDate(p.starts_on,zone)]),num(p.score),p.publishable?'Yes':'No'])));
}
/** Month totals as bars (buttons): the number sits on each bar; choosing a month lists who it counts. */
/** "Oct", or "Jan 26" for January: a month key (YYYY-MM) as an axis names it. */
// One formatter for the page (a new toLocaleString formatter per call cost 8-11 ms per draw; perf PI-4).
const MONTH=new Intl.DateTimeFormat('en-GB',{month:'short',timeZone:'UTC'});
const monthName=(k:string)=>MONTH.format(new Date(k+'-15T12:00:00Z')).slice(0,3)+(k.endsWith('-01')?' '+k.slice(2,4):'');
/** The month keys (YYYY-MM) that [first, last) touches. */
export function monthsOf(first:string,last:string):string[]{
  const out:string[]=[];let y=+first.slice(0,4),m=+first.slice(5,7);
  while(`${y}-${String(m).padStart(2,'0')}-01`<last){out.push(`${y}-${String(m).padStart(2,'0')}`);if(++m>12){m=1;y++;}}
  return out;
}
/** One line per person over the months, each with its own colour AND dash pattern (never colour alone), a point per
 *  month with its number, and the shared key (dom.ts legend): each person's entry, with their total, highlights their
 *  line on hover or focus and opens their sessions (LegendUse). */
export function personLines(rows:Row[],months:string[],unit:string,label:string,open:(r:Row)=>void):HTMLElement{
  // Months with nothing counted (before the first published rota, or not yet) leave the chart at both ends instead of
  // drawing a flat zero for them (I-07): the axis starts at the first counted month.
  const counted=(m:string)=>rows.some(p=>p.months?.[m]!==undefined);
  let start=0,end=months.length;while(start<end-1&&!counted(months[start]))start++;while(end>start+1&&!counted(months[end-1]))end--;months=months.slice(start,end);
  const max=Math.max(1,...rows.flatMap(p=>months.map(m=>p.months?.[m]??0)));
  const summary=rows.map(p=>`${p.name} ${num(p.value)}`).join(', ');
  let current:SVGSVGElement|null=null;
  // The shared key's entries highlight a line in whichever drawing is on screen (the chart redraws when its box resizes).
  const key=legend('People');
  const focusOn=(k:number|null)=>{if(!current)return;current.classList.toggle('fx-focus',k!==null);current.querySelectorAll('.fx-series').forEach((g,i)=>g.classList.toggle('on',i===k));};
  const draw=(w:number)=>{
  const h=180,l=34,r=10,t=10,b=24;
  const x=(i:number)=>months.length<2?(l+w-r)/2:l+i*(w-l-r)/(months.length-1),y=(v:number)=>t+(1-v/max)*(h-t-b);
  const chart=svg('svg',{viewBox:`0 0 ${w} ${h}`,class:'fx-trend fx-people-chart',role:'img','aria-label':`${label} per month for ${plural(rows.length,'person','people')} (totals: ${summary}). The table below has every month.`});
  const step=max<=5?1:Math.ceil(max/4);
  for(let v=0;v<=max;v+=step)chart.append(svg('line',{x1:l,x2:w-r,y1:y(v),y2:y(v),class:v?'fx-gridline':'fx-axis'}),svg('text',{x:l-6,y:y(v)+4,class:'fx-tick','text-anchor':'end'},document.createTextNode(num(v))));
  const every=Math.max(1,Math.ceil(months.length/Math.max(2,Math.floor((w-l-r)/56))));
  months.forEach((m,i)=>{if(i%every===0)chart.append(svg('text',{x:x(i),y:h-6,class:'fx-tick','text-anchor':'middle'},document.createTextNode(monthName(m))));});
  rows.forEach((p,k)=>{
    // The line is drawn as its person's key entry (every mark has one); a redraw on resize stamps its new line too.
    const g=key.mark(svg('g',{class:`fx-series fx-s${k}`,'data-person':p.person_id}),'p:'+p.person_id,
      el('span',{class:'lg-line','aria-hidden':'true'},svg('svg',{viewBox:'0 0 24 8',class:`fx-key fx-s${k}`,focusable:'false'},svg('line',{x1:1,x2:23,y1:4,y2:4,class:'fx-pline'}))),
      String(p.name),1,{hover:on=>focusOn(on?k:null),act:()=>open(p),note:num(p.value),aria:`${p.name}: ${num(p.value)} ${unit} in the period. Show sessions`});
    const pts=months.map((m,i)=>[x(i),y(p.months?.[m]??0)]);
    if(pts.length>1)g.append(svg('polyline',{points:pts.map(q=>q.join(',')).join(' '),class:'fx-pline'}));
    months.forEach((m,i)=>g.append(svg('circle',{cx:pts[i][0],cy:pts[i][1],r:3,class:'fx-pdot'},svg('title',{},document.createTextNode(`${p.name}, ${monthName(m)} ${m.slice(0,4)}: ${num(p.months?.[m]??0)} ${unit}`)))));
    chart.append(g);});
  return chart;};
  const box=boxChart(draw,c=>{current=c;});
  return el('div',{class:'fx-chart'},box,key.root);
}
function monthBars(months:Record<string,number>,unit:string,label:string,pick:(month:string)=>void):HTMLElement{
  const keys=Object.keys(months).sort(),max=Math.max(1,...keys.map(k=>months[k]));
  if(!keys.length)return emptyLine('None in this period.',null,'fx-none');
  const bars=el('div',{class:'fx-bars',role:'group','aria-label':label},...keys.map(k=>{
    const b=el('button',{type:'button',class:'fx-bar-col','aria-label':`${monthName(k)} ${k.slice(0,4)}: ${num(months[k])} ${unit}. Show who`,'data-tip':`${num(months[k])} ${unit}`},
      el('span',{class:'fx-bar-num'},num(months[k])),el('span',{class:'fx-bar-fill'}),el('span',{class:'fx-bar-label'},monthName(k)));
    b.style.setProperty('--h',String(months[k]/max));b.addEventListener('click',()=>pick(k));return b;}));
  return el('div',{class:'fx-chart'},bars,asTable(label+' as a table',['Month',unit],keys.map(k=>[k,num(months[k])])));
}
/** A chart's numbers as a table, built only when someone opens it (PI-4: the closed tables were a quarter of the page). */
const asTable=(summary:string,head:string[],rows:Child[][])=>{
  const box=el('details',{class:'fx-table'},el('summary',{},summary));
  box.addEventListener('toggle',()=>{if(box.open&&box.childElementCount<2)box.append(table(head,rows));});
  return box;};

// ---- drill-down ------------------------------------------------------------------------------------------------

/** The sessions (or wishes) behind a number, per published rota. */
export async function drill(params:Record<string,string|string[]|null|undefined>,title:string,zone:string,links:FairnessLinks){
  let r:Row;
  try{r=await request<Row>('/api/fairness/sessions?'+query(params));}
  catch(e){reportError(e,'Sessions unavailable. ');return;}
  dockPanel({key:'fairness-drill',title,width:620,sub:`${r.metric_value!=null?`${num(r.metric_value)} ${r.unit} · `:''}${formatDate(r.starts_on,zone)} – ${formatDate(addDay(r.ends_on,-1),zone)}`,build:({body,close})=>{
    if(!r.publications.length){body.append(empty('Nothing counted','No published rota in this period counts toward this number.',null,'chart'));return;}
    if(r.truncated)body.append(note('Showing the first 500 sessions. Narrow the period to see the rest.','warning'));
    const key=legend('Tags');  // the TL and OT drawn on the sessions, below them
    for(const p of r.publications as Row[]){
      const open=links.publication?button('Open rota',()=>{close();links.publication!(p.publication_id);},'secondary small'):'';
      body.append(el('section',{class:'fx-drill'},el('div',{class:'fx-drill-head'},el('div',{},el('h3',{},p.name),el('small',{class:'muted'},`${formatDate(p.starts_on,zone)} – ${formatDate(addDay(p.ends_on,-1),zone)} · ${num(p.value)} ${r.unit}`)),open?withIcon(open,'chevronRight'):''),
        // Dense rows: the day with its weekday, the time whole, the duty with clinic · place under it (I-10).
        p.sessions.length?(()=>{const counts=p.sessions.some((s:Row)=>Number(s.value)!==1);
          return table(['Day','Time','Duty',...(counts?['Counts']:[])],p.sessions.map((s:Row)=>[el('span',{class:'nowrap'},formatDay(s.day,zone)),el('span',{class:'mono nowrap'},`${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`),
            el('span',{class:'fx-duty'},el('span',{},s.type_name,s.team_lead?key.tag('lead'):'',s.overtime?key.tag('ot'):''),el('small',{class:'muted'},[s.services.join(', '),s.location].filter(Boolean).join(' · ')||'—')),
            ...(counts?[num(s.value,2)]:[])]));})():'',
        p.wishes?.length?table(['Wish','Days','Points','Outcome'],p.wishes.map((w:Row)=>[String(w.kind).replaceAll('_',' '),dateUnits([formatDate(w.first_day,zone),...(w.first_day===w.last_day?[]:['– '+formatDate(w.last_day,zone)])]),num(w.points),w.outcome||'open'])):''));
    }
    body.append(key.root);
  }});
}
const addDay=(d:string,n:number)=>new Date(Date.parse(d.slice(0,10)+'T12:00:00Z')+n*86400000).toISOString().slice(0,10);

/** The one "nothing published" of Insights (Fairness, Workload, Exports; I-02): the next step for those who plan. */
export const nothingPublished=(action:Node|null=null)=>empty('Nothing published yet','Appears once a rota is published.',action,'chart');

// ---- the page ----------------------------------------------------------------------------------------------------

export async function fairnessPage(ctx:Context,target:HTMLElement,links:FairnessLinks){
  // `planner`: sees everyone (auditors and programme leads read only); `editor`: may open the rules to add a goal.
  const zone=ctx.me.organisation.timezone,planner=ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.ADMIN,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD);
  const editor=ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.ADMIN);
  const today=new Date().toLocaleDateString('en-CA',{timeZone:zone});
  const cat=(kind:string)=>(ctx.catalogues[kind]||[]) as Row[];
  const fields:ConditionField[]=ruleConditionFields(ctx).filter(f=>PERSON_FIELDS.has(f.key));
  const labelFor=(fieldKey:string,value:string)=>{const kind=RULE_FIELDS.find(f=>f.key===fieldKey)?.kind;const r=kind?cat(kind).find(x=>x.id===value):undefined;return r?labelOf(r):tagLabel(ctx,value)||(kind?labelOf(r):value);};
  // Supervision is done by people with a duty type's supervising skill: with no one chosen they are its audience, never
  // trainees (who have no share of it). Shown under the toolbar like any Who.
  const supervisors=():Conditions|null=>{const ids=[...new Set(cat('assignment_types').map(t=>t.definition?.supervision?.capability_id).filter(Boolean))] as string[];
    return ids.length?{match:'all',conditions:[{field:'capability_ids',op:'any',values:ids}]}:null;};
  const audience=()=>view.who??(view.metric==='supervision'?supervisors():null);
  // G-03: goals sharing a rule's name (one per line of a Fair share rule) are told apart by their audience (the server's
  // audience_label: "Consultants · Duties"), the rule's own name then beside the strength.
  const byAudience=(goal:Row,all:Row[])=>!!goal.audience_label&&new Set(all.filter(x=>goalName(x)===goalName(goal)).map(x=>x.audience_label)).size>1;
  const goalTitle=(goal:Row,all:Row[])=>byAudience(goal,all)?`${goal.audience_label} · ${measureLabel(goal.metric)}`:goalName(goal);
  const goalPeriod=(goal:Row)=>{const a=goal.starts_on||goal.period_start,b=goal.ends_on||goal.period_end;
    return a&&b?`${formatDate(a)} – ${formatDate(new Date(Date.parse(b+'T12:00:00Z')-864e5).toISOString().slice(0,10))}`:'';};
  /** C3-F4: one name per goal, the same on its card and in Compare with: its title, then, only while goals of that name
   *  still read alike, the facts the API sends that tell them apart: period, how it is shared, how it compares (the
   *  contract's words), its strength and, last, a number in the rule's order ("(2)"), never "line N" (maintenance). */
  const goalLabels=(all:Row[])=>{
    const facts:((g:Row)=>string)[]=[goalPeriod,g=>SHARED_BY[String(g.share_by)]||'',g=>String((GROUP_WORDS as Record<string,string>)[String(g.comparison)]||''),
      g=>g.strength?levelWords(g.strength):''];
    const label=new Map(all.map(g=>[g,goalTitle(g,all)]));
    for(const fact of facts){const alike=new Map<string,Row[]>();for(const g of all)alike.set(label.get(g)!,[...(alike.get(label.get(g)!)||[]),g]);
      for(const same of alike.values())if(same.length>1&&new Set(same.map(fact)).size>1)for(const g of same){const f=fact(g);if(f)label.set(g,`${label.get(g)} · ${f}`);}}
    const alike=new Map<string,Row[]>();for(const g of all)alike.set(label.get(g)!,[...(alike.get(label.get(g)!)||[]),g]);
    for(const same of alike.values())if(same.length>1)same.sort((a,b)=>Number(a.line??0)-Number(b.line??0)).forEach((g,i)=>label.set(g,`${label.get(g)} (${i+1})`));
    return label;};
  if(saved===null)saved=await fetchViews<Saved>('fairness').catch(()=>[]);
  const open=openingView('fairness',saved);if(open!==undefined){viewId=open?.id||'';view=open?normal(open.definition.fairness):{...DEFAULT};rotaMonth='';}

  const root=el('div',{class:'fx-page'});
  // The collection toolbar (redrawn with the page); the Distribution list repaints on Sort without a reload.
  let bar:ReturnType<typeof collectionToolbar>|null=null,repaint=()=>{},paintScope=()=>{},findQueued=false;
  // The headline schedule score, in the footer (planners only; staff never get the score).
  const footScore=el('span',{class:'muted'});

  // Answers (fairnessRead, kept across rebuilds): a Measure or Group change asks again only for the sections that depend on it. A draw
  // cancels the reads still on their way that it no longer needs (typing dates), and a late answer from an older draw
  // is ignored.
  let wanted=new Set<string>();
  const read=(path:string)=>{wanted.add(path);return fairnessRead(path);};
  /** The month whose rota the score and goals show: as chosen, else the rota in focus's, else this month (R-3). */
  const monthInView=()=>rotaMonth||String(links.focus?.()?.starts_on||'').slice(0,7)||today.slice(0,7);
  const draw=(keepToolbar=false)=>{
    wanted=new Set();
    root.className='fx-page'+(view.density==='compact'?' compact':'');
    const [first,last]=periodDates(view,today);
    const month=monthInView(),chosenMonth=rotaMonth,implicitPeriod=!periodChosen&&!viewId;
    const common={who:tree(view.who),starts_on:first,ends_on:last};
    const work=workParams(view),filters=WISH(view.metric)?{}:work;
    // Grouping by what the work was needs the same kind of filter or none (and wishes have no clinic or duty type).
    const kinds=WORK.filter(k=>view[k].length),byWork=SESSION_FIELDS.includes(view.group_by);
    const groupNote=!byWork?'':WISH(view.metric)?`Wish points are not counted per ${fieldLabel(view.group_by).toLowerCase()}.`
      :kinds.some(k=>k!==view.group_by)?`To group by ${fieldLabel(view.group_by).toLowerCase()}, count all sessions or only chosen ${fieldLabel(view.group_by).toLowerCase()}s (Filter).`:'';
    const show=(s:Section)=>!view.hidden.includes(s)&&(s!=='score'||planner);
    const slots=Object.fromEntries(SECTIONS.map(([k,title])=>[k,el('section',{class:'panel fx-section','data-section':k,'aria-busy':'true'},el('div',{class:'fx-head'},el('h2',{},title)),skeleton('lines',`Loading ${title.toLowerCase()}…`))])) as Record<Section,HTMLElement>;
    // Kept in place (never detached), so the date field being typed in keeps focus.
    if(keepToolbar&&root.firstElementChild){while(root.lastElementChild!==root.firstElementChild)root.lastElementChild!.remove();bar?.sync();paintScope();}else root.replaceChildren(toolbar(first,last));
    const hasSections=SECTIONS.some(([k])=>show(k));
    if(!hasSections)root.append(empty('Every section is hidden','Choose sections to show under View.',null,'sliders'));
    const grid=el('div',{class:'fx-grid'});root.append(grid);
    for(const [k] of SECTIONS)if(show(k))(k==='score'||k==='goals'?grid:root).append(slots[k]);
    if(!grid.children.length)grid.remove();
    // Readiness for tests and slow devices: data-ready once every shown section has drawn, and data-render-ms, the
    // time spent building the sections in the page (network waits excluded).
    const serial=++drawSerial;let pending=SECTIONS.filter(([k])=>show(k)).length+(hasSections?1:0),renderMs=0;
    delete root.dataset.ready;delete root.dataset.renderMs;
    let unpublished=false;
    const live=()=>serial===drawSerial&&!unpublished;
    const settle=()=>{if(live()&&--pending===0){root.dataset.renderMs=String(Math.round(renderMs));root.dataset.ready='true';}};
    // A section that did not load keeps its heading and offers Retry (answers that failed are not kept, so it asks again).
    const fail=(k:Section)=>(e:Error)=>{if(!live())return;slots[k].removeAttribute('aria-busy');
      slots[k].replaceChildren(slots[k].firstElementChild!,readFailed(sectionName(k),e,()=>draw(true),sectionName(k)));settle();};
    const done=(k:Section,build:()=>Child[])=>{if(!live())return;const t=performance.now();slots[k].removeAttribute('aria-busy');slots[k].replaceChildren(...build().filter(Boolean) as Node[]);renderMs+=performance.now()-t;settle();};
    if(kinds.length>1&&!WISH(view.metric)){for(const k of (['distribution','people','trends','exposure'] as Section[]).filter(show))done(k,()=>[note('Count one kind of session: duty types, clinics or locations. Change it under Filter.','warning')]);}
    const need=(k:Section)=>show(k)&&!slots[k].querySelector('.notice');
    // This report uses publications, including when its score and goals sections are hidden.
    // R-3: planners choose the month whose rota the score and goals show (the toolbar's ‹ month ›): the rota in focus when
    // it covers that month (a draft being planned too), else that month's published rota; with none, the server's
    // default. Another draft's goals are never shown before it is published.
    const goalsRead=(planner?read('/api/home/rotas?'+query({start:month+'-01',end:monthAfter(month)+'-01',limit:'100'})).then(r=>{
      const rotas=(r.workspaces||[]) as Row[],focus=links.focus?.();
      const inScope=(x:Row)=>!!chosenMonth||x.starts_on>=first&&x.starts_on<last;
      return (focus&&rotas.find(x=>x.id===focus.id&&(implicitPeriod||inScope(x))))||
        rotas.find(x=>x.status==='published'&&inScope(x));},()=>undefined):Promise.resolve(undefined))
      .then(rota=>read('/api/fairness/goals?'+query({...common,workspace_id:rota?.id})));
    if(hasSections)goalsRead.then(async g=>{
      if(!live())return;
      let action:HTMLElement|null=null;
      if(g.workspace===null){
        const {workspace}=await read('/api/fairness/goals');
        if(!live())return;
        if(!workspace){
          unpublished=true;footScore.textContent='';
          root.replaceChildren(nothingPublished(links.publication?button('Open Schedule',()=>{location.hash='schedule';},'secondary'):null));
          root.dataset.renderMs=String(Math.round(renderMs));root.dataset.ready='true';return;
        }
        if(planner&&workspace.starts_on>today){
          const showUpcoming=()=>{rotaMonth=workspace.starts_on.slice(0,7);
            change({period:'custom',starts_on:workspace.starts_on,ends_on:workspace.ends_on});};
          // Change the visible period before showing the upcoming rota's figures.
          if(implicitPeriod&&!chosenMonth&&view.period===DEFAULT.period){showUpcoming();return;}
          action=button('Show upcoming published rota',showUpcoming,'secondary small');
        }
      }
      if(planner&&live())footScore.textContent=g.schedule?`Schedule score ${num(g.schedule.score)} / 100`:'';
      if(need('score'))done('score',()=>scoreSection(g,action));
      if(need('goals'))done('goals',()=>goalsSection(g,first,last,need('score')?null:action));
      settle();
    }).catch(e=>{
      if(!live())return;
      if(!need('score')&&!need('goals'))root.append(el('div',{},readFailed('the fairness report',e,()=>draw(true))));
      if(need('score'))fail('score')(e);if(need('goals'))fail('goals')(e);settle();
    });
    // One read serves the distribution and, when no one is chosen, the trend lines (the people furthest from share).
    // The people's rows never depend on the grouping: Trends per person reuses whichever answer has them, so a Group
    // change reloads only the distribution.
    const spreadUrl=(who:string|null,group:string)=>'/api/fairness/distribution?'+query({...common,who,...filters,metric:view.metric,group_by:group});
    const chosen=planner?tree(view.trend_who):null,who=tree(audience()),grouped=spreadUrl(who,groupNote?'':view.group_by),plain=spreadUrl(who,'');
    // A chosen goal reads its own figures with the goal cards' exact query, so the two can never differ.
    // The picker's goals come from the goals read whenever it answers: the distribution never waits for it.
    if(need('distribution'))read(goalPick?'/api/fairness/distribution?'+query({...common,goal_id:goalPick}):grouped)
      .then(d=>done('distribution',()=>distributionSection(d,first,last,groupNote,goalsRead.then(g=>(g.goals||[]) as Row[]))))
      .catch(e=>{if(goalPick&&(e as ApiError).status===404){goalPick='';draw(true);return;}fail('distribution')(e);});  // the goal is gone: back to the reference
    if(need('people')){const answers=keptAnswers(PUBLISHED_READS),url=chosen?spreadUrl(chosen,''):answers.has(plain)||!need('distribution')?plain:grouped;
      read(url).then(d=>done('people',()=>peopleSection(d,first,last))).catch(fail('people'));
      if(url===grouped&&grouped!==plain){answers.set(plain,answers.get(grouped)!);wanted.add(plain);}}
    if(need('trends'))Promise.all(['overtime_duties','lead_turns'].map(metric=>read('/api/fairness/distribution?'+query({...common,...work,metric}))))
      .then(([o,l])=>done('trends',()=>trendsSection(o,l,first,last))).catch(fail('trends'));
    if(need('exposure'))read('/api/fairness/exposure?'+query({rows:tree(view.rows),cols:tree(view.cols),pairing:view.pairing,...work,starts_on:first,ends_on:last}))
      .then(x=>done('exposure',()=>exposureSection(x,first,last))).catch(fail('exposure'));
    for(const answers of [keptAnswers(DRAFT_READS),keptAnswers(PUBLISHED_READS)]){const kept=new Set([...wanted].map(p=>answers.get(p)));
      for(const [path,a] of answers)if(!a.done&&!kept.has(a)){a.stop.abort();answers.delete(path);}}
  };
  const change=(patch:Partial<View>)=>{view={...view,...patch};draw();};
  const drillFor=(person:Row,extra:{metric?:string;other_person_id?:string;other_name?:string;pairing?:string;workspace_id?:string;goal_id?:string}={},first?:string,last?:string)=>{
    const [a,b]=periodDates(view,today),metric=extra.metric||view.metric;
    return drill({person_id:person.person_id,metric,other_person_id:extra.other_person_id,pairing:extra.pairing,starts_on:first||a,ends_on:last||b,
      workspace_id:extra.workspace_id,goal_id:extra.goal_id,...(WISH(metric)||extra.goal_id?{}:workParams(view))},
      `${measureLabel(metric)} · ${person.name}${extra.other_name?' with '+extra.other_name:''}`,zone,links);
  };

  // ---- toolbar: the shared collection toolbar. Saved view → scope (period, dates, measure) → Filter → Group → Sort → View settings ----
  function toolbar(first:string,last:string){
    const period=select('Period',PERIODS,view.period);
    period.input.addEventListener('change',()=>{const p=period.input.value as Period;periodChosen=true;rotaMonth='';
      change(p==='custom'?{period:p,starts_on:first,ends_on:last}:{period:p,starts_on:null,ends_on:null});});
    const from=field('From','date',first,{required:true}),until=field('Until (included)','date',last,{required:true});lastDayIncluded(until.input);
    const dates=dateRange('Dates',from,until);dates.classList.add('labelled');  // dates need their words in view
    // Typing a date fires a change per keystroke: reload once the dates rest (400 ms) or the field is left, keeping
    // the toolbar (and the caret) in place.
    let timer=0;
    const apply=()=>{clearTimeout(timer);if(view.period!=='custom')return;const a=from.input.value,b=until.input.value;
      if(a&&b&&a<b&&(a!==view.starts_on||b!==view.ends_on)){periodChosen=true;rotaMonth='';view={...view,starts_on:a,ends_on:b};draw(true);}};
    for(const f of [from,until]){for(const e of ['input','change'])f.input.addEventListener(e,()=>{clearTimeout(timer);timer=window.setTimeout(apply,400);});f.input.addEventListener('blur',apply);}
    const measure=select('Measure',MEASURES,view.metric);measure.input.addEventListener('change',()=>change({metric:measure.input.value}));
    const shownMonth=monthInView();
    const rotaNav=planner?periodNav(new Date(shownMonth+'-01T12:00:00Z').toLocaleString('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}),'month',
      {days:[],page:0,has_previous:true,has_next:true,unit:'month',label:''},p=>{rotaMonth=addMonthsTo(shownMonth,p);draw();},'The rota whose score and Balance goals are shown'):null;
    bar=collectionToolbar({label:'Fairness',key:'fairness',scope:[...(rotaNav?[rotaNav]:[]),period.wrap,...(view.period==='custom'?[dates]:[]),measure.wrap],
      // Planners find a person in the Distribution list at once (staff see only their own row).
      search:planner&&!view.hidden.includes('distribution')?{value:find,label:'Find a person',placeholder:'Find a person',onInput:q=>{find=q;},
        // A person's own name only (Contains or Exact text); one repaint per frame.
        apply:()=>{if(!findQueued){findQueued=true;requestAnimationFrame(()=>{findQueued=false;repaint();});}},options:{value:()=>findOptions,set:o=>{findOptions=o;},can:{scopes:['all'],kinds:[]}}}:undefined,
      // Staff see their own numbers: Period and Measure only, no Filter, Saved views or View settings (I-19).
      filter:!planner?undefined:{count:()=>(view.who?.conditions?.length||0)+WORK.filter(k=>view[k].length).length,
        clear:()=>change({who:null,assignment_type_ids:[],service_ids:[],location_ids:[]}),
        // Live, as every collection's Filter: each change reloads the dashboard in place (the toolbar and this popover stay).
        build:(panel,changed)=>{
          const set=(patch:Partial<View>)=>{view={...view,...patch};draw(true);changed();};
          const who=conditionBuilder(fields,view.who||null);who.root.addEventListener('change',()=>set({who:who.read()}));
          const types=multiPick('Duty types',(cat('assignment_types')).map(r=>({value:r.id,label:labelOf(r),tone:'place'})),view.assignment_type_ids,v=>set({assignment_type_ids:v}),'Any duty type');
          const clinics=multiPick('Clinics',(cat('services')).map(r=>({value:r.id,label:labelOf(r),tone:'place'})),view.service_ids,v=>set({service_ids:v}),'Any clinic');
          const places=multiPick('Locations',(cat('locations')).map(r=>({value:r.id,label:labelOf(r),tone:'place'})),view.location_ids,v=>set({location_ids:v}),'Any location');
          const inside=check('Include locations inside',view.within);inside.input.addEventListener('change',()=>set({within:inside.input.checked}));
          panel.classList.add('fx-filter-pop');
          panel.append(planner?el('div',{class:'popover-section'},el('span',{class:'popover-label'},'People'),el('div',{class:'audience-conditions'},who.root)):'',
            el('div',{class:'popover-section'},el('span',{class:'popover-label'},'Count only'),el('div',{class:'fx-row-fields'},el('div',{class:'field'},el('span',{class:'pick-label'},'Duty types'),types.wrap),el('div',{class:'field'},el('span',{class:'pick-label'},'or clinics'),clinics.wrap),
              el('div',{class:'field fx-loc'},el('span',{class:'pick-label'},'or locations'),el('div',{class:'fx-loc-row'},places.wrap,inside.wrap))),
              el('small',{class:'muted'},'One kind at a time. Wish points are never counted by duty type, clinic or location.')));
        }},
      // Group by who people are or by what the work was (the condition fields).
      group:planner?{options:GROUPS.filter(g=>g.value).map(g=>({key:g.value,label:g.label})),value:()=>view.group_by,set:k=>change({group_by:k})}:undefined,
      // Orders the Distribution list; the default is the people furthest from their share.
      sort:planner?{options:[{key:'value',label:'Highest first'},{key:'name',label:'Name'}],directions:false,value:()=>view.sort==='gap'?null:[view.sort,'asc'],set:v=>{view.sort=(v?.[0]||'gap') as View['sort'];repaint();}}:undefined,
      view:!planner?undefined:{active:()=>view.hidden.length>0||view.density!=='comfortable',build:panel=>panel.append(
        el('div',{class:'dt-pop-label'},'Sections'),el('div',{class:'dt-pop-list'},...SECTIONS.filter(([k])=>k!=='score'||planner).map(([k,label])=>{
          const c=check(label,!view.hidden.includes(k));c.input.addEventListener('change',()=>change({hidden:c.input.checked?view.hidden.filter(x=>x!==k):[...view.hidden,k]}));return c.wrap;})),
        // Manual activation: each density change reloads the dashboard.
        el('div',{class:'dt-pop-label',id:'fx-density'},'Density'),el('div',{class:'segmented',role:'radiogroup',activation:'manual','aria-labelledby':'fx-density'},...(['comfortable','compact'] as const).map(d=>{
          const b=el('button',{type:'button',role:'radio','aria-checked':String(view.density===d)},d[0].toUpperCase()+d.slice(1));b.addEventListener('click',()=>{panel.querySelectorAll('[role=radio]').forEach(n=>n.setAttribute('aria-checked',String(n===b)));change({density:d});});return b;})))},
      saved:!planner?undefined:{name:()=>saved!.find(x=>x.id===viewId)?.name||'',dirty:()=>{const a=saved!.find(x=>x.id===viewId);return !!a&&JSON.stringify(normal(a.definition.fairness))!==JSON.stringify(normal(view));},
        items:close=>savedViewItems<Saved>({key:'fairness',noun:'fairness charts',saved:saved!,activeId:viewId,
          current:()=>({fairness:clone(view)}),dirty:d=>JSON.stringify(normal(d.fairness))!==JSON.stringify(normal(view)),close,
          apply:v=>{viewId=v?.id||'';view=v?normal(v.definition.fairness):{...DEFAULT};rotaMonth='';periodChosen=false;draw();},
          done:(list,id)=>{saved=list;viewId=id;draw();}})}});
    bar.root.classList.add('fx-toolbar');
    // What the filter counts, in words under the toolbar; repainted in place as the live filter changes.
    const scopeLine=el('p',{class:'fx-scope muted'});
    paintScope=()=>{const whoText=conditionsText(audience(),labelFor);
      const counted=WORK.filter(k=>view[k].length).map(k=>`${({assignment_type_ids:'Duty types',service_ids:'Clinics',location_ids:'Locations'})[k]}: ${view[k].map(id=>labelFor(k,id)).join(', ')}${k==='location_ids'&&view.within?' and inside':''}`);
      scopeLine.hidden=!whoText&&!counted.length;scopeLine.replaceChildren(icon('filter'),[whoText?'People: '+whoText:'',...counted].filter(Boolean).join(' · '));};
    paintScope();
    return el('div',{},bar.root,scopeLine);
  }

  // ---- schedule score ----
  function scoreSection(g:Row,action:Node|null):Child[]{
    const s=g.schedule,trend=(g.score_trend||[]) as Row[];
    const h=el('div',{class:'fx-head'},el('h2',{},'Schedule score'),g.workspace?el('small',{class:'muted truncate'},g.workspace.name):'');
    if(!g.workspace)return [h,emptyLine('No published rota in this period.',action)];
    if(!s&&!trend.length)return [h,empty('No score yet','No score was stored for this published rota.',null,'chart')];
    const nameOf=(pid:string)=>fullNameOf(cat('people').find(p=>p.id===pid));
    const why=s?scoreWhy(s,nameOf):[];
    // At 100 nothing was lost: no breakdown to open.
    const detail=s&&s.score<100?button('Why this score',()=>{},'ghost small',{'aria-haspopup':'dialog','aria-expanded':'false'}):null;
    if(detail){detail.addEventListener('click',()=>popover(detail,panel=>panel.append(...scoreWhy(s,nameOf)),{label:'Why this score',placement:'below'}));}
    return [h,s?el('div',{class:'fx-score'},el('div',{class:'fx-score-num'},el('strong',{class:'fx-big'},num(s.score)),el('span',{class:'muted'},'/ 100'),
      badge(s.publishable?'Publishable':'Not publishable',s.publishable?'ok':'err'),s.fresh===false&&g.workspace?.status!=='published'?badge('Check again for a current score','warn'):'',detail),why[1]||''):'',
      trend.length>1?scoreTrend(trend,p=>links.publication?.(p.publication_id),zone):trend.length?oneScore(trend[0],p=>links.publication?.(p.publication_id),zone)
        :el('p',{class:'muted'},'The trend appears once rotas are published in this period.')];
  }

  // ---- goals ----
  function goalsSection(g:Row,first:string,last:string,action:Node|null):Child[]{
    const goals=(g.goals||[]) as Row[];
    const h=el('div',{class:'fx-head'},el('h2',{},'Balance goals'),el('small',{class:'muted'},g.workspace?`On ${g.workspace.name}`:''));
    if(!g.workspace)return [h,emptyLine('No published rota in this period.',action)];
    if(!goals.length)return [h,empty('No balance goals',editor?'Add a Balance goal under Rules to track how evenly something is shared.':planner?'No goal tracks how evenly work is shared yet.':'Your coordinator has not set any goals for how evenly work is shared.',editor?button('Open rules',()=>ctx.navigate('rules'),'secondary small'):null,'rules')];
    // A card is named once ("Weekly clinical target", never "… target target"); goals of one rule that differ only by
    // their period (one per week) say which period (LIVE-42).
    const labels=goalLabels(goals),title=(goal:Row)=>labels.get(goal)!;
    // Two goals that read and count the same are one card.
    const same=(goal:Row)=>JSON.stringify([title(goal),goal.metric,goal.score,((goal.pairs?.length?goal.pairs:goal.people) as Row[]).map(r=>[r.person_id,r.partner_id,r.actual,r.status])]);
    const shown=[...new Map(goals.map(goal=>[same(goal),goal])).values()];
    return [h,el('div',{class:'fx-goals'},...shown.map(goal=>{
      const rows=(goal.pairs?.length?goal.pairs:goal.people) as Row[],out=rows.filter(r=>r.status!=='within'),nobody=!rows.length;
      const trend=(goal.trend||[]).map((t:Row)=>t.score);
      const person=(r:Row)=>{const b=el('button',{type:'button',class:'fx-link'},(()=>{const who=r.name+(r.partner_name?' · '+r.partner_name:''),shown=personName(r.person_id,r.name)+(r.partner_name?' · '+personName(r.partner_id,r.partner_name):'');
        return el('span',{class:'truncate','data-ctip':who},shown,shown===who?'':el('span',{class:'sr-only'},', '+who));})(),el('small',{class:'muted'},`${num(r.actual)} of ${aim(r.expected)} ${goal.unit||''}`.trimEnd()));
        const workload=goal.metric==='workload';
        b.addEventListener('click',()=>drillFor(r,workload?{metric:goal.metric,workspace_id:g.workspace.id,goal_id:goal.goal_id}
          :goal.metric==='pair_exposure'?{metric:'pair_exposure',other_person_id:r.partner_id,other_name:r.partner_name}:{metric:goal.metric},
          workload?g.workspace.starts_on:first,workload?g.workspace.ends_on:last));return el('li',{},b);};
      // The whole list docks beside the page (C26: lists are panels, never modals); a long one gets the panel search.
      const all=out.length>3?button(`All ${out.length}`,()=>dockPanel({key:'fairness-goal',title:title(goal),sub:`${out.length} outside the band · ${goal.unit}`,build:({body})=>{
        const items=out.map(person),none=emptyLine('No one matches');none.hidden=true;
        const find=out.length>LONG_LIST?panelSearch('Find a person','Find a person',q=>{const s=q.toLocaleLowerCase();let n=0;
          for(const li of items){li.hidden=!!s&&!li.textContent!.toLocaleLowerCase().includes(s);if(!li.hidden)n++;}none.hidden=n>0;},'fx-find'):null;
        body.append(...(find?[find]:[]),el('ul',{class:'fx-list'},...items),none);}}),'ghost small'):null;
      return el('article',{class:'fx-goal'},
        el('div',{class:'fx-goal-top'},el('div',{class:'fx-goal-name'},el('h3',{class:'clamp-2','data-ctip':title(goal)},title(goal)),(()=>{const said=`${byAudience(goal,goals)?goalName(goal):measureLabel(goal.metric)} · ${levelWords(goal.strength||'medium')}`;return el('small',{class:'muted truncate',title:said},said);})()),
          goal.score!=null&&!nobody?el('strong',{class:'fx-goal-score'},num(goal.score,0)):''),
        trend.length?sparkline(trend,`${title(goal)} score by publication: ${trend.map((v:number)=>num(v,0)).join(', ')}`):planner?el('small',{class:'muted'},'No trend yet'):'',
        out.length?el('div',{},el('span',{class:'fx-out'},icon('alert'),(goal.pairs?.length?plural(out.length,'pair'):plural(out.length,'person','people'))+' outside the band'),el('ul',{class:'fx-list'},...out.slice(0,3).map(person)),all)
          // An empty audience scores 100 on the server: a warning, never a perfect score.
          :nobody?el('span',{class:'fx-out'},icon('alert'),g.workspace?'Applies to nobody in this rota':'Applies to nobody in this period')
          :el('span',{class:'fx-ok'},icon('check'),'Everyone within the band'));
    }))];
  }

  // ---- distribution ----
  function distributionSection(d:Row,first:string,last:string,groupNote='',goals:Promise<Row[]>=Promise.resolve([])):Child[]{
    const goal=d.basis==='goal';if(goal)groupNote='';  // a goal owns its scope and groups
    let rows=((d.people||[]) as Row[]).map(r=>goal?{...r,value:r.actual}:r);let only:Row|null=null;
    const pick=select('Compare with',[{value:'',label:'Reference (not a goal)'}],'');pick.wrap.hidden=true;
    void goals.then(all=>{const list=all.filter(x=>!x.pairs?.length),labels=goalLabels(all);if(!list.length)return;
      pick.input.append(...list.map(x=>el('option',{value:String(x.goal_id)},byAudience(x,all)?labels.get(x)!:`${labels.get(x)} · ${measureLabel(x.metric)}`)));
      pick.input.value=goalPick;pick.wrap.hidden=false;},()=>{});  // no goals (or none read): the reference alone, no picker
    pick.input.addEventListener('change',()=>{goalPick=pick.input.value;draw(true);});
    const basis=goal?BASIS_COLUMN[d.share_by]:BASIS_COLUMN[d.share_by==='offered'?'offered':'available_days'];
    const drillOf=(r:Row)=>goal?drillFor(r,{metric:d.metric,...(d.metric==='workload'?{workspace_id:d.workspace?.id,goal_id:d.goal_id}:{})}):drillFor(r);
    const own=(m:string)=>planner?`${measureLabel(m)} per person`:`Your ${measureLabel(m).toLowerCase()}`;
    const h=el('div',{class:'fx-head'},el('h2',{},own(d.metric)),el('small',{class:'muted fx-basis'},basisWords(d,zone)),pick.wrap);
    const key=legend();
    if(!rows.length){bar?.shown(0,undefined,['person','people']);return [h,empty('Nobody to show','No one in the department matches these filters in this period.',null,'people')];}
    // No days available anywhere: nothing is published in the period (the ledger counts published rotas only).
    if(rows.every(r=>r.expected==null))return [h,empty('Nothing published in this period','The numbers appear once a rota in this period is published. Choose another period to see earlier rotas.',links.publication?button('Open Schedule',()=>{location.hash='schedule';},'secondary small'):null,'chart')];
    // Everyone at 0 of an expected 0 is not "within range": nothing was counted (the same answer as the months below, I-29).
    if(rows.every(r=>!r.value&&!r.expected))return [h,empty('Nothing counted in this period','No published duty in this period counts toward this measure.',null,'chart')];
    const list=el('div',{class:'fx-dist',role:'list'}),groupsBox=el('div',{});
    const gap=(r:Row)=>r.expected==null?0:Math.abs(r.value-r.expected);
    const words=(r:Row)=>r.status==='above'?`above by ${num(r.value-r.expected)}`:r.status==='below'?`below by ${num(r.expected-r.value)}`:r.status==='within'?'within band':'';
    // Each person's row is built once and reused: Find, Sort and a group pick only reorder and show them (perf PI-4).
    const built=new Map<string,HTMLElement>();
    function paint(){
      const q=find.trim().toLocaleLowerCase(),m=findOptions&&recordMatcher(find,findOptions);
      // A Set, not a scan of the chosen people per row: paint runs on every keystroke in Find.
      const picked=only?new Set(only.person_ids):null;
      const shown=(picked?rows.filter(r=>picked.has(r.person_id)):rows).filter(r=>findOptions?!m||m({own:()=>[r.name]}):!q||String(r.name).toLocaleLowerCase().includes(q)).sort(view.sort==='name'?(a,b)=>String(a.name).localeCompare(String(b.name)):view.sort==='value'?(a,b)=>b.value-a.value:(a,b)=>gap(b)-gap(a));
      const max=Math.max(1e-9,...shown.map(r=>Math.max(r.value,r.high??0,r.expected??0)));
      key.clear();for(const g of groupMarks)g();
      bar?.shown(shown.length,undefined,['person','people']);  // the server applied the filters: no "of"
      if(!shown.length){list.replaceChildren(emptyLine('No one matches the search.',null,'fairness-empty'));return;}
      const STATUS:Record<string,string>={within:'Total, on share',above:'Total, above share',below:'Total, below share'};
      list.replaceChildren(...shown.map(r=>{
        const kept=built.get(r.person_id);
        if(kept){const b=kept.firstElementChild as HTMLElement;scale(b,r);marks(b,r);return kept;}
        const b=el('button',{type:'button',class:'fx-drow','aria-label':`${spokenName(personName(r.person_id,r.name),String(r.name||''))}: ${num(r.value)} ${d.unit}${r.expected!=null?`, expected ${aim(r.expected)} (band ${num(r.low)}–${num(r.high)}), ${words(r)}`:''}. Show sessions`},
          el('span',{class:'fx-name truncate'},personName(r.person_id,r.name)),
          el('span',{class:'fx-track','aria-hidden':'true'},r.low!=null?el('span',{class:'fx-band'}):'',el('span',{class:'fx-fill '+(r.status||'')}),r.expected!=null?el('span',{class:'fx-exp'}):''),
          el('span',{class:'fx-val'},num(r.value)),el('span',{class:'fx-expv muted'},r.expected!=null?num(r.expected):'—'),
          el('span',{class:'fx-status '+(r.status||'')},r.status==='within'?icon('check'):r.status?icon('alert'):'',r.status==='within'?el('span',{class:'fx-sw'},'Within range'):r.status==='above'?signed(r.value-r.expected):r.status==='below'?signed(r.value-r.expected):''));
        scale(b,r);marks(b,r);b.addEventListener('click',()=>void drillOf(r));
        const item=el('div',{role:'listitem'},b);built.set(r.person_id,item);return item;
      }));
      // A row's fill, band and expected tick, each drawn as its key entry (again after the key is cleared).
      function marks(b:HTMLElement,r:Row){const t=b.querySelector('.fx-track')!;
        key.mark(t.querySelector('.fx-fill')!,'fill:'+(r.status||''),el('span',{class:'lg-bar fx-fill '+(r.status||''),'aria-hidden':'true'}),STATUS[r.status]||'Total',0);
        if(r.low!=null)key.mark(t.querySelector('.fx-band')!,'band',el('span',{class:'lg-bar fx-band','aria-hidden':'true'}),goal?'Expected band (the goal’s leeway)':'Default band (not a goal’s)');
        if(r.expected!=null)key.mark(t.querySelector('.fx-exp')!,'exp',el('span',{class:'lg-tick fx-exp','aria-hidden':'true'}),'Expected share');}
      // The bars are drawn against the largest value shown, so a search rescales them.
      function scale(b:HTMLElement,r:Row){b.style.setProperty('--v',String(r.value/max));
        if(r.low!=null){b.style.setProperty('--lo',String(r.low/max));b.style.setProperty('--hi',String(Math.min(1,(r.high??r.low)/max)));b.style.setProperty('--e',String(r.expected/max));}}
    }
    const groups=(d.groups||[]) as Row[],byWork=d.group_kind==='work',field=view.group_by as WorkFilter,groupMarks:(()=>void)[]=[];
    if(groupNote){groupsBox.className='fx-groups';groupsBox.append(note(groupNote,'info'));}
    else if(groups.length){
      // People groups: average per person against their expected share; the row shows only that group.
      // Work groups (clinic, duty type, location): what was done there, per person who did any; the row counts only it.
      const gmax=Math.max(1e-9,...groups.map(g=>Math.max(byWork?g.value:g.mean,g.expected_mean||0)));
      const only1=(g:Row)=>byWork&&!!g.key&&view[field]?.length===1&&view[field][0]===g.key;
      groupsBox.className='fx-groups';
      groupsBox.append(el('span',{class:'popover-label'},`By ${fieldLabel(view.group_by).toLowerCase()} · ${byWork?'total, and people who did any':'average per person'}`),...groups.map(g=>{
        const counts=byWork&&!!g.key,shown=byWork?g.value:g.mean;
        const b=el('button',{type:'button',class:'fx-drow fx-grow'+(byWork?' work':''),'aria-pressed':String(only1(g)),
          'aria-label':byWork?`${g.label}: ${num(g.value)} ${d.unit} by ${plural(g.people,'person','people')}. ${counts?(only1(g)?'Count every '+fieldLabel(field).toLowerCase()+' again':'Count only this '+fieldLabel(field).toLowerCase()):'Show only these people'}`
            :`${g.label}: ${plural(g.people,'person','people')}, average ${num(g.mean)} ${d.unit}, expected ${num(g.expected_mean)}. Show only this group`},
          el('span',{class:'fx-name truncate'},g.label,el('small',{class:'muted'},' '+g.people)),el('span',{class:'fx-track','aria-hidden':'true'},el('span',{class:'fx-fill'}),g.expected_mean!=null?el('span',{class:'fx-exp'}):''),
          el('span',{class:'fx-val'},num(shown)),el('span',{class:'fx-expv muted'},num(g.expected_mean)),el('span',{class:'fx-status'},''));
        b.style.setProperty('--v',String(shown/gmax));if(g.expected_mean!=null)b.style.setProperty('--e',String(g.expected_mean/gmax));
        // A group's bar is drawn in the groups' own colour; its swatch takes it from the same token.
        const fill=b.querySelector('.fx-fill')!,exp=b.querySelector('.fx-exp');
        groupMarks.push(()=>{const sw=el('span',{class:'lg-bar fx-fill','aria-hidden':'true'});sw.style.setProperty('background','var(--teal-c)');
          key.mark(fill,'gfill',sw,byWork?'Group total':'Group average per person',0);if(exp)key.mark(exp,'exp',el('span',{class:'lg-tick fx-exp','aria-hidden':'true'}),'Expected share');});
        b.addEventListener('click',()=>{
          if(counts){change({[field]:only1(g)?[]:[g.key],...(field==='location_ids'?{within:false}:{})});return;}
          only=only===g?null:g;groupsBox.querySelectorAll('[aria-pressed]').forEach(n=>n.setAttribute('aria-pressed',String(n===b&&!!only)));paint();});
        return b;}));
    }
    paint();repaint=paint;
    return [h,groupsBox,el('div',{class:'fx-dhead','aria-hidden':'true'},el('span',{},'Person'),el('span',{},''),el('span',{},measureLabel(d.metric)),el('span',{},'Expected'),el('span',{},'')),list,key.root,
      // Overtime shares by offered hours: the table shows the hours each share was worked out from.
      asTable(`${measureLabel(d.metric)} as a table`,['Person',...(basis?[basis[0]]:[]),d.unit,'Expected','Band'],rows.map(r=>[r.name,...(basis?[num(r[basis[1]],basis[2])]:[]),num(r.value),num(r.expected),r.low!=null?`${num(r.low)}–${num(r.high)}`:'—']))];
  }

  // ---- trends per person ----
  function peopleSection(d:Row,first:string,last:string):Child[]{
    const all=(d.people||[]) as Row[],chosen=!!tree(view.trend_who)&&planner;
    // Nobody chosen: the people furthest from their share (the ones worth a look), else everyone shown.
    const gap=(r:Row)=>r.expected==null?0:Math.abs(r.value-r.expected);
    const rows=(chosen?all.slice().sort((a,b)=>b.value-a.value):all.slice().sort((a,b)=>gap(b)-gap(a))).slice(0,TREND_LINES);
    const who=el('button',{type:'button',class:'button secondary small',...popup('dialog')},icon('people'),
      el('span',{class:'label truncate'},`People: ${chosen?conditionsText(view.trend_who,labelFor):'furthest from share'}`));
    who.addEventListener('click',()=>popover(who,(panel,close)=>{const c=conditionBuilder(fields,view.trend_who||null);panel.classList.add('fx-filter-pop');
      panel.append(el('div',{class:'popover-section'},el('span',{class:'popover-label'},'Draw a line for'),el('div',{class:'audience-conditions'},c.root),
        el('small',{class:'muted'},`Up to ${TREND_LINES} lines; everyone chosen is in the table.`)),
        el('div',{class:'popover-section fx-pop-actions'},button('Furthest from share',()=>{close();change({trend_who:null});},'ghost small'),button('Apply',()=>{close();change({trend_who:c.read()});},'primary small')));},
      {label:'People to draw',placement:'below'}));
    const h=el('div',{class:'fx-head'},el('h2',{},planner?`${measureLabel(d.metric)} per person by month`:`Your ${measureLabel(d.metric).toLowerCase()} by month`),
      el('small',{class:'muted'},!planner?'Your total each month.':chosen?(all.length>rows.length?`The ${rows.length} highest of ${all.length} chosen; the table has everyone.`:'The people you chose.')
        :`The ${plural(rows.length,'person','people')} furthest from their expected share. Choose people to compare others.`),
      planner?el('div',{class:'fx-head-tools'},who):'');
    if(!all.length){bar?.shown(0,undefined,['person','people']);return [h,empty('Nobody to show',chosen?'No one matches these people in this period.':'No one in the department matches these filters in this period.',null,'people')];}
    if(all.every(r=>!Object.keys(r.months||{}).length))return [h,empty('Nothing counted in this period','The lines appear once a published rota in this period counts toward this measure.',null,'chart')];
    return [h,personLines(rows,monthsOf(first,last),d.unit,measureLabel(d.metric),r=>void drillFor(r)),
      asTable(`${measureLabel(d.metric)} per person by month as a table`,['Person',...monthsOf(first,last).map(monthName),'Total'],
        (chosen?all:rows).map(r=>[r.name,...monthsOf(first,last).map(m=>num(r.months?.[m]??0)),num(r.value)]))];
  }

  // ---- trends ----
  function trendsSection(o:Row,l:Row,first:string,last:string):Child[]{
    const h=el('div',{class:'fx-head'},el('h2',{},'Overtime & team lead turns'),el('small',{class:'muted'},planner?'Totals per month for the people shown. Choose a month to see who.':'Your totals per month. Choose a month to see the sessions.'));
    const who=(d:Row)=>(month:string)=>{
      const rows=(d.people as Row[]).filter(r=>r.months?.[month]).sort((a,b)=>b.months[month]-a.months[month]);
      const [a,b]=[month+'-01',new Date(Date.UTC(+month.slice(0,4),+month.slice(5,7),1)).toISOString().slice(0,10)];
      if(!planner&&rows.length===1){void drillFor(rows[0],{metric:d.metric},a<first?first:a,b>last?last:b);return;}  // staff: their own sessions
      dockPanel({key:'fairness-month',title:`${measureLabel(d.metric)} · ${month}`,sub:plural(rows.length,'person','people'),build:({body})=>body.append(el('ul',{class:'fx-list'},...rows.map(r=>{
        const x=el('button',{type:'button',class:'fx-link'},nameText(r.person_id,r.name),el('small',{class:'muted'},` ${num(r.months[month])} ${d.unit}`));// Opened from this panel, the person's drill-down comes back to it (Back).
        x.addEventListener('click',()=>{void drillFor(r,{metric:d.metric},a<first?first:a,b>last?last:b);});return el('li',{},x);})))});
    };
    return [h,el('div',{class:'fx-trends'},...[o,l].map(d=>el('div',{},el('h3',{},measureLabel(d.metric)),monthBars(d.months||{},d.unit,measureLabel(d.metric)+' per month',who(d)))))];
  }

  // ---- exposure ----
  function exposureSection(x:Row,first:string,last:string):Child[]{
    const conf=(which:'rows'|'cols',text:string)=>{
      const b=el('button',{type:'button',class:'button secondary small',...popup('dialog')},icon('people'),el('span',{class:'label truncate'},`${text}: ${conditionsText(view[which],labelFor)||'everyone'}`));
      b.addEventListener('click',()=>popover(b,(panel,close)=>{const c=conditionBuilder(fields,view[which]||null);panel.classList.add('fx-filter-pop');
        panel.append(el('div',{class:'popover-section'},el('span',{class:'popover-label'},text),el('div',{class:'audience-conditions'},c.root)),
          el('div',{class:'popover-section fx-pop-actions'},button('Everyone',()=>{close();change({[which]:null});},'ghost small'),button('Apply',()=>{close();change({[which]:c.read()});},'primary small')));},{label:text,placement:'below'}));
      return b;};
    const pairing=select('Together means',[{value:'present',label:'On the same session'},{value:'supervised',label:'Named supervisor'}],view.pairing);
    pairing.input.addEventListener('change',()=>change({pairing:pairing.input.value as View['pairing']}));
    const h=el('div',{class:'fx-head'},el('h2',{},'Exposure'),el('small',{class:'muted'},'Sessions together, rows with columns. Choose a number to see the sessions.'),
      el('div',{class:'fx-head-tools'},planner?conf('rows','Rows'):'',conf('cols','Columns'),pairing.wrap));
    let rows=x.rows as Row[],cols=x.cols as Row[],cells=x.cells as number[][];
    // A whole department is a 300 × 300 grid (90,000 cells): show the people with the most sessions together and say so.
    // shortcut: capped in the page; the API still returns the full matrix — add a server limit if its size shows in timings.
    const CAP=30,allRows=rows.length,allCols=cols.length,totals=cells.map(r=>r.reduce((a,b)=>a+b,0));
    if(allRows>CAP||allCols>CAP){
      const colTotals=cols.map((_,j)=>cells.reduce((a,r)=>a+r[j],0));
      const ri=rows.map((_,i)=>i).sort((a,b)=>totals[b]-totals[a]).slice(0,CAP),cj=cols.map((_,j)=>j).sort((a,b)=>colTotals[b]-colTotals[a]).slice(0,CAP);
      rows=ri.map(i=>rows[i]);cols=cj.map(j=>cols[j]);cells=ri.map(i=>cj.map(j=>cells[i][j]));totals.splice(0,totals.length,...ri.map(i=>totals[i]));
    }
    const capped=rows.length<allRows||cols.length<allCols;
    if(!rows.length||!cols.length)return [h,empty('Nobody to compare','No one matches the rows or the columns in this period.',null,'people')];
    if(!cells.flat().some(Boolean))return [h,empty('No sessions together','No published session in this period puts these people together.',null,'people')];
    const max=Math.max(...cells.flat()),key=legend();
    // A level's sessions: level k holds the values in ((k-1)/5·max, k/5·max].
    const level=(v:number)=>Math.max(1,Math.ceil(v/max*5));
    const levelWords=(k:number)=>{const lo=Math.floor((k-1)*max/5)+1,hi=Math.floor(k*max/5);return `${lo<hi?`${num(lo)}–${num(hi)}`:num(hi)} ${hi===1?'session':'sessions'} together`;};
    const grid=el('table',{class:'fx-heat'},el('caption',{class:'sr-only'},`Sessions together, ${rows.length} rows by ${cols.length} columns`),
      el('thead',{},el('tr',{},el('th',{scope:'col',class:'fx-corner'},'Person'),...cols.map(c=>el('th',{scope:'col',class:'fx-col','data-ctip':c.name},nameText(c.person_id,c.name))),el('th',{scope:'col',class:'fx-col fx-total'},el('span',{},'Total')))),
      el('tbody',{},...rows.map((r,i)=>el('tr',{},el('th',{scope:'row',class:'truncate'},nameText(r.person_id,r.name)),...cols.map((c,j)=>{
        const v=cells[i][j],self=r.person_id===c.person_id;
        const td=el('td',{class:self?'fx-self':'heat-'+(v?level(v):0),'data-i':i,'data-j':j,tabindex:i===0&&j===0?0:-1,role:self||!v?null:'button','aria-label':self?`${r.name} (same person)`:`${r.name} with ${c.name}: ${num(v)}`},self?'·':v?num(v):'');  // empty = none: the numbers that matter stand out
        if(self)key.mark(td,'self',el('span',{class:'lg-mark','aria-hidden':'true'},'·'),'Same person',9);else if(v)key.mark(td,'heat'+level(v),'lg-swatch heat-'+level(v),levelWords(level(v)),level(v));
        return td;}),el('td',{class:'fx-total'},num(totals[i]))))));
    tableColumnMenus(grid,false);
    const cell=(i:number,j:number)=>grid.querySelector<HTMLElement>(`td[data-i="${i}"][data-j="${j}"]`);
    const open=(td:HTMLElement)=>{const i=+td.dataset.i!,j=+td.dataset.j!;if(td.getAttribute('role')!=='button')return;
      void drillFor(rows[i],{metric:'pair_exposure',other_person_id:cols[j].person_id,other_name:cols[j].name,pairing:view.pairing},first,last);};
    grid.addEventListener('click',e=>{const td=(e.target as HTMLElement).closest<HTMLElement>('td[data-i]');if(td)open(td);});
    // One tab stop; arrow keys move between cells (a roving focus), Enter opens.
    grid.addEventListener('keydown',e=>{const td=(e.target as HTMLElement).closest<HTMLElement>('td[data-i]');if(!td)return;
      if(e.key==='Enter'||e.key===' '){e.preventDefault();open(td);return;}
      const d:Record<string,[number,number]>={ArrowUp:[-1,0],ArrowDown:[1,0],ArrowLeft:[0,-1],ArrowRight:[0,1]};if(!d[e.key])return;e.preventDefault();
      const next=cell(+td.dataset.i!+d[e.key][0],+td.dataset.j!+d[e.key][1]);if(next){td.tabIndex=-1;next.tabIndex=0;next.focus();}});
    return [h,el('div',{class:'fx-heat-wrap',tabindex:-1},grid),key.root,capped?el('p',{class:'fx-legend muted'},`The ${rows.length} × ${cols.length} people with the most sessions together (of ${allRows} × ${allCols}); choose Rows or Columns to see others.`):''];
  }

  target.append(sectionHead('Fairness',planner?'Counted from published rotas.':'Counted from published rotas. You see your own numbers.'),root,...(planner?[footerStatus(footScore)]:[]));
  draw();
}

/** Compare drafts: score components, each goal and each person side by side, with the change. The server computes
 *  every number (planner_tools.compare); this only lays them out. */
export function compareDetail(detail:Row|null|undefined,before:string,after:string):HTMLElement|null{
  if(!detail)return null;
  const change=(d:number|null,scale=1)=>d==null||!Math.round(d*scale*10)?el('span',{class:'muted'},'—'):el('span',{class:'fx-delta '+(d>0?'up':'down')},icon(d>0?'chevronUp':'chevronDown'),signed(d*scale));
  const pct=(v:number|null)=>v==null?'—':Math.round(v*1000)/10+'%';
  const people=(detail.people||[]) as Row[];
  const peopleTable=(n:number)=>table(['Person',before,after,'Change'],people.slice(0,n).map(p=>[p.name,num(p.before),num(p.after),change(p.delta)]),'No one’s score changed.');
  const peopleBox=el('div',{},peopleTable(10));
  return el('div',{class:'fx-compare'},
    el('section',{},el('h3',{},'Score parts'),table(['Part',before,after,'Change'],(detail.components as Row[]).map(c=>[COMPONENT_LABELS[c.component]||c.component,pct(c.before),pct(c.after),change(c.delta,100)]))),
    // Balance goals only when the department has some: an empty table is noise.
    (detail.goals as Row[]).length?el('section',{},el('h3',{},'Balance goals'),table(['Goal',before,after,'Change'],(detail.goals as Row[]).map(g=>[g.name,num(g.before),num(g.after),change(g.delta)]))):'',
    el('section',{},el('h3',{},'People'),peopleBox,people.length>10?button(`Show all ${people.length}`,()=>peopleBox.replaceChildren(peopleTable(people.length)),'ghost small'):''));
}
