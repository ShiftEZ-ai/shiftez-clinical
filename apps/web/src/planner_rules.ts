import {working,skeleton} from './loading.js';
import {sidePanel} from './side_panel.js';
/* Rules in context on the schedule: what applies to whatever you point at, which rule wins, where something deviates
 * from the general rule, and a highlight of everything one rule applies to. The server decides every match
 * (GET /api/workspaces/{id}/rule-map, the same evaluator the checks use); this file only maps ids to the screen. */
import {el,icon,plural,overflowMore,hoverCard,refreshHoverCard,notify,reportError,button,guard,labelOf,formatDate,formatTime,note,announce,toLocal,tabs,legend,segmented,menu,menuSource,awaitAdoption,opens,type Legend,type Child,type MenuEntry,legendAt} from './dom.js';
import {availabilityPanel} from './rota_availability.js';
import {personLink,fullNameOf} from './person_label.js';
import {rotaSentences,unexplainedLine,type ExplainedRule,type Conflict} from './rules_explained.js';
import {TERMS} from './terms.js';
import {issueGlyph,issueList,findingIssue,SEVERITY_WORDS} from './issue_marker.js';
import {get,command} from './api.js';
import {collectionToolbar} from './table_view.js';
import type {Context} from './forms.js';
import type {Row,WorkspaceData} from './types.js';
import type {RuleMapProposal} from './generated.js';
import {dateInput,lastDayIncluded} from './date_picker.js';
import {causeText} from './findings.js';
import {recordMatcher,type SearchOptions} from './search.js';

export type Num={field:string;value:number;maximum?:number|null;unit:string;stricter:'higher'|'lower'|null};
/** The rota's workload targets dock (app.ts): rules for this rota are this panel's, the server's rule map. */
export const ROTA_TARGETS='Workload targets for this rota';
export type MapRule={id:string;name:string;kind:string;severity:string;strength?:string;safety:boolean;exception_allowed:boolean;adjustable:boolean;overridable?:boolean;
  number:Num|null;department_value:number|null;binding_value?:number|null;adjusted:null|{mode:'set'|'off';amount:number|null;by:string|null;by_name?:string|null;at:string|null;applied?:boolean};off:boolean;workspace_only:boolean;
  overrides:string[];overridden_by:string[];person_side?:boolean;session_side?:boolean;starts_on?:string;ends_on?:string|null;block?:[string,string]|null;
  named:{people:string[];services:string[];locations:string[];types:string[];weekdays:number[]|null;dated:boolean};
  sessions:number[];people:Record<string,[number,number][]>;replaced:Record<string,[number,number][]>;days:number[];lines:(Row & {binding_value?:number|null})[];
  /** Who follows a line that names no person (a programme's or ranks' line): [first day, end day, index into lines]. */
  line_people?:Record<string,[number,number,number][]>;
  /** Sessions where a person's days say the rule holds but that session does not (a skill held at other clinics only). */
  not_here?:Record<string,number[]>;
  status:{state:'breach'|'warning'|'met'|'unchecked';errors:number;warnings:number};
  /** A catalogue rule's adjustments for one line, person or dates of this rota (D16). */
  scoped_adjustments?:Row[];
  /** A change to this rule waiting for approval (the server's: who proposed it, when), else null. */
  proposal?:RuleMapProposal|null;
  /** Days the rota's stored check found short: [day, errors, warnings, first message]. */
  short_days?:[number,number,number,string][]};
export type Marker={kind:string;rule_id:string;rule_name:string;text:string;by:string|null;by_name?:string|null;at:string|null;people:string[];services:string[];locations:string[];types:string[];days:number[]|null;sessions:number[];global:boolean};
export type RuleMap={days:string[];shifts:string[];checked:boolean;fresh:boolean;rules:MapRule[];markers:Marker[]};
/** What a hover card or the pinned panel is about. `shifts`/`days` are what the target covers in the current view. */
export type Target={kind:'rota'|'day'|'service'|'location'|'type'|'row'|'person'|'session'|'assignment';label:string;sub?:string;
  day?:string;service?:string;location?:string;type?:string;person?:string;shift?:string;shifts?:string[];days?:string[]};
/** What the planner lends this module: permissions, the full rule records and the canonical forms. */
export interface RuleHooks{
  editable:boolean;         // a draft this person may change (rota-only changes)
  canPropose:boolean;       // may propose rules (coordinator, admin)
  canRequestException:boolean;
  policy:(id:string)=>Row|undefined;
  sentence:(rule:Row,definition?:Row)=>string;
  /** Open rule: the rule's own page (its lines, history and checks), for a person when one is selected. */
  openRule?:(id:string,person?:string)=>unknown;
  /** Fix: the rota's Fix list, at a session when one is selected. */
  fix?:(focus?:{shift?:string})=>unknown;
  /** Make it the department default: the rule's propose form (prefilled with this rota's number when it changed it), or
   *  for a catalogue rule the server's seed of one rota adjustment in the line panel (`adjustment`: its id), else its
   *  page; proposed, then activated by a different approver. */
  changeForEveryone:(policy:Row,amount?:number,adjustment?:string)=>unknown;
  /** A session's places from the rota's last check (planner_week coverOf): what a selection's facts count. */
  cover?:(shiftId:string)=>{required:number;open:number}|undefined;
  /** A person's details: the one person card (person_card.ts: name, rank, programme, every contact the reader may see). */
  personDetails?:(personId:string)=>HTMLElement;
  rotaChanges:()=>void;     // the rota's workload targets dock
  /** This rota's own rules still waiting for approval (not in the rule map until active). */
  ownRules?:()=>Row[];
  addRotaRule?:()=>unknown;    // a rule for this rota only (proposed and approved like any rule)
  refresh:()=>Promise<void>;
  onOpen?:()=>void;         // another side panel should close
}
type Item={rule:MapRule;why:'named'|'applies';number:Num|null;own?:Row;binding?:boolean;looser?:string;replacedBy?:string[];exception?:Row|null;state:string;errors:number;warnings:number;messages:string[];
  /** Several selected: how many of them the rule reaches. */hits?:number;of?:number};

// One loaded map per rota version; the request itself revalidates with the ETag, so an unchanged rota is not resent.
let map:RuleMap|null=null,mapKey='',loading:Promise<RuleMap>|null=null,loadingKey='';
/** The explainer's sentences by rule for the loaded rota version (rules_explained.ts): each row says its rule in them. */
let said:Map<string,ExplainedRule>|null=null,saidKey='',conflicts:Conflict[]=[],overlaps=new Map<string,Map<string,number>>();
function loadSentences(){
  if(!dataNow)return;const k=keyOf(dataNow);if(saidKey===k)return;saidKey=k;said=null;conflicts=[];overlaps=new Map();
  rotaSentences(dataNow.workspace.id,k).then(reply=>{if(saidKey!==k)return;said=new Map((reply.rules??[]).map(r=>[r.rule_id,r]));
    const all=reply.conflicts??[];overlaps=ownOverlaps(all);conflicts=all.filter(c=>c.rule_ids[0]!==c.rule_ids[1]);if(pinned)drawPanel();},
    ()=>{if(saidKey===k)saidKey='';});  // the rows keep the client's sentence; the next draw asks again
}
/** A marker with its days as a set and its place in the map's order (every result keeps the server's order). */
type Mark={m:Marker;n:number;days:Set<number>|null};
/** What a rule names, as sets (`lines`: its lines' person ids). */
type Named={people:Set<string>;services:Set<string>;locations:Set<string>;types:Set<string>;lines:Set<unknown>};
/** Everything the matching asks per tagged node, indexed once per rule map (a decorate sweep at department scale would
 *  otherwise scan every marker's and rule's lists for every node: ~200,000 steps per sweep). */
let idx:{day:Map<string,number>;shift:Map<string,number>;sessions:Map<string,Set<number>>;days:Map<string,Set<number>>;notHere:Map<string,Set<string>>;
  named:Map<string,Named>;marks:Record<'person'|'service'|'location'|'type',Map<string,Mark[]>>&Record<'session'|'day',Map<number,Mark[]>>}|null=null;
function indexMarks(markers:Marker[]){
  const put=<K,>(m:Map<K,Mark[]>,k:K,x:Mark)=>{const l=m.get(k);if(!l)m.set(k,[x]);else if(l[l.length-1]!==x)l.push(x);};
  const out={person:new Map<string,Mark[]>(),service:new Map<string,Mark[]>(),location:new Map<string,Mark[]>(),type:new Map<string,Mark[]>(),session:new Map<number,Mark[]>(),day:new Map<number,Mark[]>()};
  markers.forEach((m,n)=>{const x:Mark={m,n,days:m.days?new Set(m.days):null};
    for(const k of m.people)put(out.person,k,x);for(const k of m.services)put(out.service,k,x);
    for(const k of m.locations)put(out.location,k,x);for(const k of m.types)put(out.type,k,x);for(const k of m.sessions)put(out.session,k,x);
    // A day's own marks: dated and naming no person, clinic, place or duty.
    if(m.days&&!m.people.length&&!m.services.length&&!m.locations.length&&!m.types.length)for(const k of m.days)put(out.day,k,x);});
  return out;
}
let ctxNow:Context|null=null,dataNow:WorkspaceData|null=null,hooksNow:RuleHooks|null=null;
let host:HTMLElement|null=null,main:HTMLElement|null=null,observer:MutationObserver|null=null;
/** The pinned panel (per rota) and the rule highlight (preview on hover, locked on click). */
type Pinned={workspace:string;target:Target;q:string;search_options?:SearchOptions;all?:string[];tab?:PanelTab;
  /** Everything selected (the first is `target`). */sel?:Target[];
  /** The selection is the ticked sessions (selectTicked): it follows the bulk set while it is. */ticked?:boolean;
  /** The rows folded under “N more are met” are shown. */met?:boolean};
/** The rota panel's tabs: its rules, and who is available over its dates. */
export type RotaTab='rules'|'people';
/** A selection's tabs (few, and the same for every kind): Details or Summary first, Rules, Issues, and a person's Duties. */
export type PanelTab=RotaTab|'details'|'summary'|'issues'|'duties';
// A department has ~1000 rules: each section draws this many until "Show all" (search narrows it instead).
const FIRST=40;
let pinned:Pinned|null=null;
/** The rule whose details are open (in the pinned panel or a session's person panel). */
/** The opened rows: one at a time, or both rules of a conflict (Open both). */
let openRules=new Set<string>(),openFor='';  // openFor: whose session panel the open rows belong to
/** What opened the pinned panel (focus goes back there on close). Held weakly, as the multiple-choice registry is (dom.ts):
 *  a redraw replaces it, and a pinned panel must not keep the replaced view alive through it. */
let opener:WeakRef<HTMLElement>|null=null;
/** Phones: the panel that stepped aside for a highlight, reopened from the highlight bar. */
let lastPinned:Pinned|null=null;
let lock:{workspace:string;rule:string}|null=null,preview='';
/** "Show on the schedule" from the Rules page: locked once the planner has its map. */
let pending='';
// Read when used, not at load: record_text pulls this module into Node-run logic tests, which have no matchMedia.
const touch={get matches(){return typeof matchMedia==='function'&&matchMedia('(hover: none)').matches;}};

/** "Monday 5 Oct": a day as the panel names it. */
export const dayTitle=(day:string)=>new Intl.DateTimeFormat('en-GB',{weekday:'long',day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'));
/** The rota version a rule map answers for. */
export const keyOf=(d:WorkspaceData)=>`${d.workspace.id}:${d.workspace.revision}:${d.workspace.validation?.evaluated_at||''}:${d.current_fingerprint}`;
export function loadRuleMap(d:WorkspaceData):Promise<RuleMap>{
  const key=keyOf(d);
  if(map&&mapKey===key)return Promise.resolve(map);
  if(loading&&loadingKey===key)return loading;
  loadingKey=key;
  // Asked from a clean stack (a microtask): a fetch on its way keeps the stack it was started from, and an event
  // listener's frame there (a hover or click on the view) would keep that view alive until the map arrives.
  const mine:Promise<RuleMap>=Promise.resolve().then(()=>get<RuleMap>(`/api/workspaces/${d.workspace.id}/rule-map`)).then(m=>{
    // A newer rota version was asked for meanwhile: that answer wins, never this older one.
    if(loadingKey!==key)return loading&&loading!==mine?loading:m;
    map=m;mapKey=key;loading=null;
    idx={day:new Map(m.days.map((x,i)=>[x,i])),shift:new Map(m.shifts.map((x,i)=>[x,i])),
      sessions:new Map(m.rules.map(r=>[r.id,new Set(r.sessions)])),days:new Map(m.rules.map(r=>[r.id,new Set(r.days)])),
      notHere:new Map(m.rules.filter(r=>r.not_here&&Object.keys(r.not_here).length).map(r=>[r.id,new Set(Object.entries(r.not_here!).flatMap(([p,l])=>l.map(i=>p+'|'+i)))])),
      named:new Map(m.rules.map(r=>[r.id,{people:new Set(r.named.people),services:new Set(r.named.services),locations:new Set(r.named.locations),
        types:new Set(r.named.types),lines:new Set(r.lines.map(l=>l.person_id))}])),marks:indexMarks(m.markers)};
    return m;}).catch(e=>{if(loadingKey===key)loading=null;throw e;});
  loading=mine;
  return loading;
}
const ready=()=>!!map&&!!dataNow&&mapKey===keyOf(dataNow);

// ---- Matching (set membership only: the server already decided who and what each rule applies to) ----------------
const inRanges=(r:[number,number][]|undefined,d:number)=>!!r&&r.some(([a,b])=>a<=d&&d<b);
const dayIdx=(day?:string)=>day===undefined?-1:idx!.day.get(day)??-1;
const zone=()=>ctxNow!.me.organisation.timezone;
// Each session's local day, worked out once per rota version (decorating and highlighting ask for it per tagged node).
const shiftDays=new WeakMap<object,Map<string,string>>();
const shiftDay=(id:string)=>{if(!dataNow)return '';let m=shiftDays.get(dataNow);
  if(!m){m=new Map(dataNow.shifts.map(s=>[s.id as string,toLocal(s.start,zone()).slice(0,10)]));shiftDays.set(dataNow,m);}return m.get(id)||'';};
function personOn(r:MapRule,pid:string,days:string[]|undefined){
  const ranges=r.people[pid];if(!ranges)return false;
  if(!days?.length)return true;
  return days.some(d=>inRanges(ranges,dayIdx(d)));
}
/** Whether the rule holds for this person on this session: their days, less the sessions the server found it does not
 *  (the same per-session test as the "Why?" panel). */
const heldAt=(r:MapRule,pid:string,i:number)=>!idx!.notHere.get(r.id)?.has(pid+'|'+i);
function hits(r:MapRule,t:Target):'named'|'applies'|null{
  if(isOff(r))return null;
  const S=idx!.sessions.get(r.id)!,N=idx!.named.get(r.id)!,anyShift=(ids?:string[])=>!!ids?.some(id=>{const i=idx!.shift.get(id)??-1;return S.has(i)&&(!t.person||heldAt(r,t.person,i));});
  switch(t.kind){
    case 'rota':return r.named.people.length||r.named.services.length||r.named.locations.length||r.named.types.length?'named':'applies';
    case 'day':return idx!.days.get(r.id)!.has(dayIdx(t.day))?(r.named.weekdays||r.named.dated?'named':'applies'):null;
    case 'session':return S.has(idx!.shift.get(t.shift!)??-1)?(r.session_side?'named':'applies'):null;
    case 'assignment':{const i=idx!.shift.get(t.shift!)??-1;return S.has(i)&&personOn(r,t.person!,[shiftDay(t.shift!)])&&heldAt(r,t.person!,i)?(N.people.has(t.person!)?'named':'applies'):null;}
    // A rule about certain sessions (a clinic, a duty, some days) reaches a person through the sessions they work here.
    case 'person':return !personOn(r,t.person!,t.days)?null:N.people.has(t.person!)||N.lines.has(t.person)?'named':!r.session_side||anyShift(t.shifts)?'applies':null;
    case 'service':return N.services.has(t.service!)?'named':anyShift(t.shifts)?'applies':null;
    case 'location':return N.locations.has(t.location!)?'named':anyShift(t.shifts)?'applies':null;
    case 'type':return N.types.has(t.type!)?'named':anyShift(t.shifts)?'applies':null;
    case 'row':return N.types.has(t.type!)||(!!t.location&&N.locations.has(t.location))?'named':anyShift(t.shifts)?'applies':null;
  }
}
/** Findings from the rota check that belong to this rule and this target. */
function findingsFor(r:MapRule,t:Target):Row[]{
  return findingsOf(t,((dataNow?.workspace.validation?.findings||[]) as unknown as Row[]).filter(f=>f.rule_id===r.id));
}
/** The findings of `all` that belong to a target. A cell (a row on one day) holds only its own sessions' findings. */
function findingsOf(t:Target,all:Row[]):Row[]{
  if(t.kind==='rota')return all;
  const shifts=new Set(t.shift?[t.shift]:t.shifts||[]),days=new Set(t.day?[t.day]:t.days||[]),cell=t.kind==='row'&&!!t.day;
  return all.filter(f=>(t.person?f.person_id===t.person:true)&&(cell?!!f.shift_id&&shifts.has(f.shift_id):shifts.size?!f.shift_id||shifts.has(f.shift_id):true)
    &&(t.kind==='day'?!!f.shift_id&&days.has(shiftDay(f.shift_id)):true)&&(t.kind==='person'&&days.size&&f.shift_id?days.has(shiftDay(f.shift_id)):true));
}
/** A line's own number, or the rule's when the line has none (a line of a grouped rule may keep the rule's number). */
const ownNumber=(r:MapRule,own?:Row|null):Num|null=>own&&own.amount!=null&&r.number?{...r.number,value:Number(own.amount)}:r.number;
/** A person's line in words: “their own number” only when it has one; otherwise which part of the rule is theirs. */
const lineWords=(own?:Row)=>!own?'':own.person_id?(own.amount!=null?'their own number':'their line'):own.programme_id?'their programme’s line':own.category_ids?.length?'their rank’s line':'their line';
/** The number a rule gives this target: a person's own line when they have one on that day, else the programme or rank
 *  line they follow that day (the server's `line_people`). */
function numberFor(r:MapRule,t:Target):{number:Num|null;own?:Row}{
  const day=t.day||(t.shift?shiftDay(t.shift):t.days?.[0]);
  let own=t.person?r.lines.find(l=>l.person_id===t.person&&(!day||((!l.starts_on||l.starts_on<=day)&&(!l.ends_on||day<l.ends_on)))):undefined;
  if(t.person&&!own){const di=day?dayIdx(day):-1,span=r.line_people?.[t.person]?.find(([a,b])=>di<0||a<=di&&di<b);if(span)own=r.lines[span[2]];}
  return {number:ownNumber(r,own),own};
}
/** Several rules of one kind: the strictest wins; the others still count but ask less. */
function stack(items:Item[]){
  const groups=new Map<string,Item[]>();
  for(const i of items)if(i.number?.stricter&&!i.replacedBy?.length){const k=i.rule.kind+'|'+i.number.field+'|'+i.number.unit;groups.set(k,[...(groups.get(k)||[]),i]);}
  for(const list of groups.values()){
    if(list.length<2)continue;
    const best=list.reduce((a,b)=>(a.number!.stricter==='lower'?b.number!.value<a.number!.value:b.number!.value>a.number!.value)?b:a);
    for(const i of list){if(i.number!.value===best.number!.value)i.binding=true;else i.looser=best.rule.name;}
  }
}
function itemsAt(t:Target):Item[]{
  if(!ready())return [];
  const out:Item[]=[];
  for(const r of map!.rules){
    // A rule switched off for this rota reaches nothing, but it is still this rota's change: listed with its facts.
    const why=hits(r,t)||(t.kind==='rota'&&isOff(r)&&changed(r)?'applies':null);
    const replacedBy=t.person&&!why&&personReplaced(r,t)?r.overridden_by.map(id=>map!.rules.find(x=>x.id===id)?.name||'another rule'):undefined;
    if(!why&&!replacedBy)continue;
    const f=t.kind==='rota'?[]:findingsFor(r,t),{number,own}=numberFor(r,t);
    const errors=t.kind==='rota'?r.status.errors:f.filter(x=>x.severity==='error').length,warnings=t.kind==='rota'?r.status.warnings:f.length-errors;
    const state=replacedBy?'replaced':isOff(r)?'off':errors?'breach':warnings?'warning':map!.checked&&map!.fresh?'met':'unchecked';
    out.push({rule:r,why:why||'applies',number,own,replacedBy,state,errors,warnings,messages:[...new Set(f.map(x=>causeText(x)||String(x.message)))].slice(0,3)});
  }
  stack(out);
  return out.sort(byState);
}
const RANK:Record<string,number>={breach:0,warning:1,unchecked:2,met:3,off:3,replaced:4};
const byState=(a:Item,b:Item)=>RANK[a.state]-RANK[b.state]||(a.why==='named'?0:1)-(b.why==='named'?0:1)||a.rule.name.localeCompare(b.rule.name);
/** The rules of a selection: one member's, or for several each rule once (the worst state, the issues added up, named
 *  when it names any of them, and how many of them it reaches). */
function itemsFor(sel:Target[]):Item[]{
  if(sel.length<2)return sel.length?itemsAt(sel[0]):[];
  const by=new Map<string,Item>();
  for(const t of sel)for(const i of itemsAt(t)){const had=by.get(i.rule.id);
    if(!had){by.set(i.rule.id,{...i,binding:false,looser:undefined,hits:1,of:sel.length});continue;}
    had.hits!++;had.errors+=i.errors;had.warnings+=i.warnings;if(i.why==='named')had.why='named';
    if(RANK[i.state]<RANK[had.state])had.state=i.state;had.messages=[...new Set([...had.messages,...i.messages])].slice(0,3);}
  return [...by.values()].sort(byState);
}
function personReplaced(r:MapRule,t:Target){
  const ranges=r.replaced?.[t.person!];if(!ranges)return false;
  const days=t.day?[t.day]:t.shift?[shiftDay(t.shift)]:t.days||[];
  return !days.length||days.some(d=>inRanges(ranges,dayIdx(d)));
}
function markersAt(t:Target):Marker[]{
  if(!ready())return [];
  const di=t.day?dayIdx(t.day):t.shift?dayIdx(shiftDay(t.shift)):-1,I=idx!.marks,at=(id?:string)=>idx!.shift.get(id!)??-1;
  const onDay=(x:Mark)=>di<0||!x.days||x.days.has(di);
  // Candidates come from the indexes, each marker once and in the map's order; the tests are the ones a scan of every
  // marker would make.
  const union=(...lists:(Mark[]|undefined)[])=>{const seen=new Set<Mark>(),out:Mark[]=[];
    for(const l of lists)for(const x of l||[])if(!seen.has(x)){seen.add(x);out.push(x);}return out.sort((a,b)=>a.n-b.n);};
  let xs:Mark[];
  switch(t.kind){
    case 'rota':return map!.markers.slice();
    case 'day':xs=I.day.get(di)||[];break;
    case 'person':{const days=t.days?.length?t.days.map(d=>dayIdx(d)):null;
      xs=(I.person.get(t.person!)||[]).filter(x=>!days||!x.days||days.some(d=>x.days!.has(d)));break;}
    case 'assignment':xs=union(I.person.get(t.person!),I.session.get(at(t.shift))).filter(onDay);break;
    case 'session':xs=I.session.get(at(t.shift))||[];break;
    case 'service':xs=I.service.get(t.service!)||[];break;
    case 'location':xs=I.location.get(t.location!)||[];break;
    case 'type':xs=I.type.get(t.type!)||[];break;
    case 'row':xs=union(I.type.get(t.type!),t.location?I.location.get(t.location):undefined,
      ...(t.shift?[t.shift]:t.shifts||[]).map(id=>I.session.get(at(id))?.filter(x=>!x.m.people.length)));break;
  }
  return xs.map(x=>x.m);
}

// ---- Words ------------------------------------------------------------------------------------------------------
const unitWord=(n:Num)=>n.unit==='hours'?'h':n.unit==='minutes'?'min':n.value===1&&n.maximum==null?n.unit==='people'?'person':n.unit.replace(/ies$/,'y').replace(/s$/,''):n.unit;
export const ruleNumber=(n:Num|null)=>numText(n);
const numOne=(v:number)=>Number.isInteger(v)?String(v):v.toFixed(1);
const numRange=(n:Num)=>`${numOne(n.value)}${n.maximum!=null?`–${numOne(n.maximum)}`:""}`;
const numText=(n:Num|null)=>n?`${numRange(n)} ${unitWord(n)}`.trim():"";
/** The live value when it differs from the number shown (a safety rule's map may already show the live value). */
const bindsNow=(n:Num|null,binds?:number|null)=>n&&binds!=null&&binds!==n.value?binds:null;
const both=(n:Num,binds:number)=>`${numRange(n)}, now binds ${numText({...n,value:binds,maximum:null})}`;
/** A number a revised mandatory rule now binds stricter than the draft captured (the rule map's binding_value): both. */
export const numBoth=(n:Num|null,binds?:number|null)=>{const v=bindsNow(n,binds);return v!=null?'captured '+both(n!,v):numText(n);};
/** The same as a sentence for secondary text; nothing when the captured number still binds. */
export const bindsNote=(n:Num|null,binds?:number|null)=>{const v=bindsNow(n,binds);return v!=null?`Captured ${both(n!,v)}.`:'';};
/** The binding value for the number shown: a person's own line's, else the rule's. */
const bindsOf=(r:MapRule,own?:Row)=>(own?own.binding_value:r.binding_value)??null;
const STATE:Record<string,[string,string,string]>={breach:['err','!','Broken'],warning:['warn','!','Warning'],met:['ok','✓','Met'],unchecked:['na','–','Not checked yet'],off:['na','–','Switched off for this rota'],replaced:['na','↷','Replaced here']};
const markerWhat:Record<string,string>={template_line:'Own number',override:'Replaced here',exception:'Person exception',rota_adjustment:'Changed for this rota',target_override:'Target changed for this rota'};
/** Who made a change: "You", the server's name for them, or nothing (an identity id is never shown). */
const byWho=(by:unknown,name?:unknown)=>by&&by===ctxNow?.me.identity_id?'you':name?String(name):'';
const markerWho=(m:Marker&{by_name?:string|null})=>{const who=byWho(m.by,m.by_name);return who||m.at?`${['rota_adjustment','target_override'].includes(m.kind)?'Changed':'Approved'}${who?' by '+who:''}${m.at?' · '+formatDate(m.at.slice(0,10)):''}`:'';};
const subjectWord:Record<string,string>={person:'this person',assignment:'this person',service:'this clinic',location:'this place',type:'this duty',row:'this duty',day:'this day',session:'this clinic'};
function targetWords(t:Target){return ({rota:'this rota',day:'this day',service:'this clinic',location:'this place',type:'this duty',row:'this duty',person:'this person',session:'this session',assignment:'this person here'} as Record<string,string>)[t.kind];}

/** Hover-card lines for what deviates here (exceptions, own numbers, rota-only changes) and who approved it. */
export function markerLines(t:Target):HTMLElement[]{
  return markersAt(t).slice(0,3).map(m=>el('li',{class:'rk-hc-line mark'},el('span',{class:'rk-dot','aria-hidden':'true'},icon('fork')),el('span',{class:'rk-hc-name'},el('strong',{},markerWhat[m.kind]||'Exception'),m.text,m.by?el('small',{},markerWho(m)):'')));
}
// ---- Hover summary ----------------------------------------------------------------------------------------------
/** Lines for a hover card: the rules here (worst first, strictest marked) and anything that deviates. Summary only. */
export function ruleSummary(t:Target):Node[]{
  if(!ready())return [el('div',{class:'rk-hc-empty'},loading?skeleton('line','Loading the rules that apply…'):'Rules appear once the rota has loaded.')];
  const items=itemsAt(t).filter(i=>i.state!=='replaced'),marks=markersAt(t);
  const named=items.filter(i=>i.why==='named').length;
  const head=el('div',{class:'rk-hc-head'},el('strong',{},items.length===1?'1 rule applies':items.length?`${items.length} rules apply`:'No rules apply'),
    named&&t.kind!=='rota'?el('span',{class:'muted'},` · ${named} specific to ${targetWords(t)}`):'');
  const lines=items.slice(0,4).map(i=>{const [tone,glyph]=STATE[i.state];
    return el('li',{class:'rk-hc-line '+tone},stateMark(i),el('span',{class:'rk-hc-name truncate'},i.rule.name),
      i.number?el('span',{class:'rk-num'},numBoth(i.number,bindsOf(i.rule,i.own))):'',i.binding?el('span',{class:'rk-win'},'strictest'):'');});
  const more=items.length>4?[el('li',{class:'hc-more'},overflowMore(items.length-4,['rule','rules'],{as:'span',cls:''}))]:[];
  const ex=marks.slice(0,2).map(m=>el('li',{class:'rk-hc-line mark'},el('span',{class:'rk-dot','aria-hidden':'true'},icon('fork')),el('span',{class:'rk-hc-name'},m.text,m.by?el('small',{},markerWho(m)):'')));
  return [head,el('ul',{class:'rk-hc-lines'},...lines,...more,...ex),...(marks.length>2?[overflowMore(marks.length-2,['exception','exceptions'],{as:'span',cls:'rk-hc-more'})]:[]),
    el('div',{class:'hc-foot'},touch.matches?'Tap to see its duties and rules':'Click or press Enter to see its duties and rules')];
}

// ---- Tagging and decoration --------------------------------------------------------------------------------------
const parsed=new WeakMap<Element,Target>(),wired=new WeakSet<Element>();
/** Marks an element as a place rules apply to. `own`: it already shows ruleSummary in its own hover card.
 *  `pin`: selecting it (a click, a tap or Enter) opens the panel for it: its duties and the rules that apply there
 *  (REQ-PANEL, replacing SI-18's hover-only mouse path). Controls inside it (⋯, a checkbox) keep their own action. */
/** `pin:'mouse'`: selectable by a click only (a cell whose controls are its keyboard path); `quiet`: no marks or rule
 *  highlight drawn on it (a cell: its sessions carry them). */
export function tag<T extends HTMLElement>(node:T,t:Target,opts:{own?:boolean;pin?:boolean|'mouse';quiet?:boolean}={}):T{
  node.dataset.rk=JSON.stringify(t);parsed.set(node,t);
  if(opts.own)node.dataset.rkCard='1';if(opts.pin)node.dataset.rkPin=opts.pin==='mouse'?'mouse':'1';if(opts.quiet)node.dataset.rkQuiet='1';
  return node;
}
/** What identifies a selected thing (a row and a cell differ by its day). */
const keyOfTarget=(t:Target)=>[t.kind,t.day||'',t.person||'',t.shift||'',t.service||'',t.location||'',t.type||''].join('|');
const selectionOf=()=>pinned&&dataNow&&pinned.workspace===dataNow.workspace.id&&pinned.target.kind!=='rota'?pinned.sel||[pinned.target]:[];
const GRID_ROLES=new Set(['gridcell','columnheader','rowheader','row']);
/** The selection shows on the grid: every selected thing carries the accent ring (and aria-selected where its role has
 *  it); a selected session rings its cell (Week) or its chips (People) through their data-session. */
function markSelection(){
  if(!main)return;
  const sel=selectionOf(),keys=new Set(sel.map(keyOfTarget));
  const mark=(n:HTMLElement,on:boolean)=>{n.classList.toggle('rk-selected',on);if(GRID_ROLES.has(n.getAttribute('role')||''))on?n.setAttribute('aria-selected','true'):n.removeAttribute('aria-selected');};
  for(const n of main.querySelectorAll<HTMLElement>('.rk-selected'))mark(n,false);
  if(keys.size)for(const n of main.querySelectorAll<HTMLElement>('[data-rk-pin]'))if(keys.has(keyOfTarget(targetOf(n))))mark(n,true);
  for(const t of sel)if(t.kind==='session'&&t.shift)for(const n of main.querySelectorAll<HTMLElement>(`[data-session~="${CSS.escape(t.shift)}"]`))mark(n,true);
}
/** Select mode (the panel's Select toggle, for touch): every tap adds to or takes from the selection. */
let selecting=false;
/** Whether a click should add to the selection: a modifier, or Select mode while a selection is open. */
/** Whether a session is in the open selection (its ring where the view's own marking does not reach, e.g. a popover). */
export const sessionSelected=(id:string)=>selectionOf().some(t=>t.kind==='session'&&t.shift===id);
export const addsToSelection=(e:MouseEvent|KeyboardEvent)=>e.ctrlKey||e.metaKey||e.shiftKey||(selecting&&!!selectionOf().length);
/** A session added to (or taken from) the selection: a modifier-click on anything of it in a cell, or a tap in Select
 *  mode. A plain click still opens the session panel, where it is staffed. */
const sessionTarget=(shift:Row):Target=>{const z=zone(),place=labelOf((ctxNow?.catalogues.locations||[]).find((x:Row)=>x.id===shift.location_id));
  return {kind:'session',shift:shift.id,shifts:[shift.id],label:`${shift.type_snapshot.name} · ${dayTitle(shiftDay(shift.id))}`,
    sub:[`${formatTime(shift.start,z)}–${formatTime(shift.end,z)}`,place].filter(Boolean).join(' · ')};};
export function selectSession(shift:Row){
  if(!dataNow)return;
  pin(sessionTarget(shift),undefined,'',true);
}
/** The ticked sessions (the planner's bulk set) are the selection too (maintenance): an open panel reads the set whatever
 *  changed it (a box, Clear, Select all, a range, a refresh that drops sessions), "2 sessions" with their rules and issues
 *  together; none ticked, the rota's again. A tick never opens the panel, and focus stays where it is. app.ts calls this
 *  from the set's one change point. */
let tickedIds:string[]=[];
export function selectTicked(ids:string[]){
  tickedIds=ids;
  if(applyTicked()){drawPanel();markSelection();if(pinned?.sel)announce(`${plural(pinned.sel.length,'session')} selected`);}
}
/** Puts the ticked sessions held now into the open panel's selection; true when the panel must redraw. */
function applyTicked():boolean{
  if(!pinned||!dataNow||pinned.workspace!==dataNow.workspace.id)return false;
  const byId=new Map(dataNow.shifts.map(x=>[String(x.id),x])),ts=tickedIds.map(id=>byId.get(id)).filter(x=>x!==undefined).map(sessionTarget);
  // None ticked: a panel showing the ticked set goes back to the rota's rules; one showing a clicked selection stays.
  if(!ts.length){if(!pinned.ticked)return false;pinned.target={kind:'rota',label:'Rules in this rota'};pinned.sel=undefined;pinned.ticked=false;return true;}
  pinned.target=ts[0];pinned.sel=ts;pinned.ticked=true;return true;
}
const targetOf=(n:Element)=>{let t=parsed.get(n);if(!t){t=JSON.parse((n as HTMLElement).dataset.rk||'{}') as Target;parsed.set(n,t);}return t;};
/** Whether an element is part of the highlighted rule. */
function lit(r:MapRule,t:Target){
  if(t.kind==='assignment')return hits(r,t)!==null;
  return hits(r,t)!==null;
}
/** A place's hover card and selection, wired on its first pointer, focus, click or key (SI-50): a sweep over thousands of
 *  places sets only their Tab stops and marks. Listened for in the capture phase on the view, so every place under the
 *  event is wired before its own handlers and the shared hover card (dom.ts, on the document) read it. */
function wireAt(e:Event){
  const view=e.currentTarget as HTMLElement;
  for(let n=e.target instanceof Element?e.target.closest<HTMLElement>('[data-rk]'):null;n&&view.contains(n);n=n.parentElement?.closest<HTMLElement>('[data-rk]')??null)wire(n);
}
function wire(n:HTMLElement){
  if(wired.has(n))return;wired.add(n);
  const t=targetOf(n);
  if(!n.dataset.rkCard)hoverCard(n,()=>[el('div',{class:'hc-head'},el('span',{class:'hc-badge rk-badge'},icon('rules')),el('div',{},el('strong',{},t.label),t.sub?el('small',{},t.sub):'')),...ruleSummary(t)],400);
  if(n.dataset.rkPin){
    // Ctrl, ⌘ or Shift adds it to (or takes it from) the selection; Shift+Enter on the keyboard.
    n.addEventListener('click',e=>{const inner=(e.target as Element).closest('button,a,input,select,label,.pchip,.team-box');if(inner&&inner!==n)return;pin(t,undefined,'',addsToSelection(e));});
    n.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target===n){e.preventDefault();pin(t,undefined,'',e.shiftKey||(selecting&&!!selectionOf().length));}});
  }
}
function decorate(){
  if(!main||!main.isConnected)return;
  observer?.disconnect();
  // Measured (SI-50): the sweep over every [data-rk] node is named "rk-decorate" in the page's performance timeline.
  const started=performance.now();
  try{
    const nodes=[...main.querySelectorAll<HTMLElement>('[data-rk]')];
    const ok=ready();
    for(const n of nodes){
      // In a Schedule grid it joins the roving set (planner_week.ts roving owns the one stop; arrows reach it): never a
      // Tab stop of its own, which roving, settled already and watching child lists only, would not take back (S-17).
      if(n.dataset.rkPin&&!n.classList.contains('rk-pinnable')){
        if(n.dataset.rkPin!=='mouse'&&!n.matches('button,a,[tabindex]'))n.tabIndex=n.closest('[role=grid]')?-1:0;
        n.classList.add('rk-pinnable');
      }
      // Markers: something here deviates from the general rule. Drawn where the old one was (a list row's first cell).
      const slot=n.matches('button')?n:n.querySelector<HTMLElement>(':scope>.rk-mark-slot')||n;
      slot.querySelector(':scope>.rk-mark')?.remove();
      const t=ok&&!n.dataset.rkQuiet?targetOf(n):null;
      if(t&&t.kind!=='rota'){
        const marks=markersAt(t);
        if(marks.length){
          const words=marks.map(m=>`${markerWhat[m.kind]||'Exception'}: ${m.text}${m.by?' · '+markerWho(m):''}`);
          const tip=marks.length===1?words[0]:`${marks.length} exceptions here · ${words[0]}`;
          const mark=el('span',{class:'rk-mark',role:'img','aria-label':marks.length===1?words[0]:`${marks.length} exceptions: ${words.join('; ')}`,'data-tip':tip},icon('fork'));
          // Drawn as an entry of the key of the view it sits in (dom.ts legendAt; every mark has one).
          const key=legendAt(n);if(key)exceptionMark(key,mark);
          slot.append(mark);
          // A button's name comes from its own label, so the exception is said there too.
          if(n.matches('button')){const base=n.dataset.rkLabel??n.getAttribute('aria-label');if(base!==null){n.dataset.rkLabel=base;n.setAttribute('aria-label',`${base}. ${marks.length===1?words[0]:marks.length+' exceptions'}`);}}
        }
      }
      n.classList.remove('rk-hit');
    }
    applyHighlight(nodes);
    markSelection();
    // A ready signal (tests, tooling): the map is drawn onto this view. An attribute, so the observer ignores it.
    if(ok)main.dataset.rkRules=String(map!.rules.length);
  }finally{if(main?.isConnected)observer?.observe(main,{childList:true,subtree:true});performance.measure?.('rk-decorate',{start:started});}
}
function activeRule(){const id=preview||(lock&&lock.workspace===dataNow?.workspace.id?lock.rule:'');return id&&ready()?map!.rules.find(r=>r.id===id)||null:null;}
function applyHighlight(nodes=[...(main?.querySelectorAll<HTMLElement>('[data-rk]')||[])]){
  if(!main)return;
  const r=activeRule();
  main.classList.toggle('rk-dim',!!r);
  if(r)for(const n of nodes)n.classList.toggle('rk-hit',!n.dataset.rkQuiet&&lit(r,targetOf(n)));
  else for(const n of nodes)n.classList.remove('rk-hit');
  drawSpot();
}
function counts(r:MapRule){return {people:Object.keys(r.people).length,sessions:r.sessions.length,days:r.days.length};}
function countText(r:MapRule){const c=counts(r);return isOff(r)?'Switched off for this rota':!c.people&&!c.sessions&&!c.days?'Not used in this rota':`applies to ${plural(c.people,'person','people')} · ${plural(c.sessions,'session')} · ${plural(c.days,'day')}`;}
/** This rota's own adjustments of a rule (the draft's record: justification, who, when; D16). */
/** This rota's adjustments of a rule that are in force: one the server does not apply (a safety rule's older
 *  adjustment, adjusted.applied false) is said once as not applied, never as switched off or changed. */
const adjustmentsOf=(r:MapRule)=>r.adjusted?.applied===false?[]:((dataNow?.workspace.rule_adjustments||[]) as Row[]).filter(a=>a.policy_id===r.id);
const isOff=(r:MapRule)=>r.off&&r.adjusted?.applied!==false;
const changed=(r:MapRule)=>!!r.adjusted||!!r.scoped_adjustments?.length;
/** Where a rota's changes are in their one review with the rota (D16): a governance fact, so always in words. */
const reviewState=():[string,boolean]=>{const st=String(dataNow?.workspace.status||'draft');
  return st==='draft'?['Reviewed when the rota is submitted',false]:st==='submitted'?['In review with the rota',false]:['Approved with the rota',true];};
/** A rule's state mark: the one issue glyph (D78: triangle must fix, circle could improve) when it has issues, else
 *  the met / not checked dot. Hidden from screen readers: the row says its state in words. Drawn as its entry in the
 *  panel's key (`key`, dom.ts legend; the issue entries as every view names them, issue_marker.ts). */
/** An exception mark (the fork): one key entry wherever it is drawn (the panel, a view's header or chip). */
const exceptionMark=<T extends Element>(key:Legend,node:T):T=>key.mark(node,'exception',el('span',{class:'rk-mark','aria-hidden':'true'},icon('fork')),'Differs from the general rule here');
const stateDot=(state:string)=>el('span',{class:'rk-dot '+STATE[state][0],'aria-hidden':'true'},STATE[state][1]);
const stateMark=(i:Item,key?:Legend)=>{const sev=i.state==='breach'?'error':i.state==='warning'?'warning':null;
  const m=sev?issueGlyph(sev,sev==='error'?i.errors:i.warnings):stateDot(i.state);
  return !key?m:sev?key.mark(m,'issue:'+sev,issueGlyph(sev),SEVERITY_WORDS[sev]):key.mark(m,'state:'+i.state,stateDot(i.state),STATE[i.state][2]);};
/** The bar that names the highlighted rule, with its counts; Esc or × clears it. */
function drawSpot(){
  if(!main)return;
  main.querySelector(':scope>.rk-spot')?.remove();
  const r=activeRule();if(!r)return;
  const locked=!!lock&&lock.rule===r.id;
  const inView=main.querySelectorAll('[data-rk].rk-hit').length;
  const clear=el('button',{type:'button',class:'icon-button bare','aria-label':'Clear highlight (Esc)','data-tip':'Clear highlight · Esc'},icon('close'));
  clear.addEventListener('click',()=>{lock=null;preview='';applyHighlight();});
  // Not a live region: it is redrawn as rules are previewed; locking a rule is announced once (setLock).
  const bar=el('div',{class:'rk-spot'+(locked?' locked':''),role:'region','aria-label':'Rule highlight'},el('span',{class:'rk-spot-icon','aria-hidden':'true'},icon('rules')),
    el('span',{class:'rk-spot-text'},el('strong',{class:'truncate'},'“'+r.name+'”'),el('span',{class:'rk-chip'},countText(r)),inView?el('span',{class:'muted rk-spot-view'},`${inView} highlighted in this view`):el('span',{class:'muted rk-spot-view'},'Nothing it applies to is in this view')),
    locked?el('span',{class:'muted rk-spot-hint'},touch.matches?'':'Esc to clear'):el('span',{class:'muted rk-spot-hint'},'Preview · open the rule to keep it highlighted'),
    locked&&!pinned&&lastPinned?(()=>{const b=button('Rules',()=>{const p=lastPinned;lastPinned=null;if(p&&dataNow&&p.workspace===dataNow.workspace.id){pinned=p;hooksNow?.onOpen?.();drawPanel(true);drawSpot();}},'secondary small');b.prepend(icon('rules'));return b;})():'',locked?clear:'');
  main.prepend(bar);
}
function setLock(ruleId:string|null){lock=ruleId&&dataNow?{workspace:dataNow.workspace.id,rule:ruleId}:null;preview='';applyHighlight();
  const r=activeRule();if(r)announce(`Highlighting “${r.name}”: ${countText(r)}.`);}
export function highlightRule(ruleId:string){pending=ruleId;}
export const highlighted=()=>lock?.rule||'';

// ---- Pinned panel --------------------------------------------------------------------------------------------------
export function pin(t:Target,tab?:PanelTab,rule='',add=false){
  if(!dataNow)return;
  const sel=selectionOf();
  if(add&&sel.length&&t.kind!=='rota'){
    const k=keyOfTarget(t),next=sel.some(x=>keyOfTarget(x)===k)?sel.filter(x=>keyOfTarget(x)!==k):[...sel,t];
    if(!next.length){pin({kind:'rota',label:'Rules in this rota'});return;}
    // The same panel, its selection changed in place (focus and the panel's place kept).
    pinned!.sel=next;pinned!.target=next[0];pinned!.ticked=false;drawPanel();
    announce(next.length>1?`${next.length} selected`:`${next[0].label} selected`);return;}
  const from=document.activeElement instanceof HTMLElement&&!document.activeElement.closest('.rk-panel,.popover,.menu-list')?document.activeElement:null;
  unpin(()=>{
    if(!dataNow)return;
    pinned={workspace:dataNow.workspace.id,target:t,q:'',tab:tab||(t.kind==='rota'?undefined:selectionKind([t])==='summary'?'rules':'details')};openRules=new Set(rule?[rule]:[]);opener=from?new WeakRef(from):null;
    hooksNow?.onOpen?.();drawPanel(true);
  });
}
export function unpin(next:()=>unknown=()=>{}){
  const leave=async()=>{if(pinned){pinned=null;selecting=false;drawPanel();markSelection();}await next();};
  const go=()=>{void leave().catch(reportError);};
  if(panelGuard)panelGuard.tryCloseThen(go);else go();
}
let panelGuard:ReturnType<typeof guard>|null=null;
const panelOf=new WeakMap<HTMLElement,object>();  // which pinned panel an element shows
function drawPanel(focus=false){
  // Live refreshes redraw the panel: whoever was typing in its search, or on a rule, stays there.
  const old=host?.querySelector<HTMLElement>(':scope>.rk-panel'),had=old&&document.activeElement instanceof HTMLElement&&old.contains(document.activeElement)?document.activeElement:null;
  const back=had?.matches('input[type=search]')?{search:true,at:(had as HTMLInputElement).selectionStart}:had?.closest('[data-rk-row]')?{row:had.closest('[data-rk-row]')!.getAttribute('data-rk-row')}:null;
  const scroll=old?.querySelector('.rk-body')?.scrollTop||0;
  panelGuard?.dispose();panelGuard=null;
  // The same panel drawn again (its rules arrived, a live refresh) keeps its element: only its content changes, so
  // nothing holding the panel (focus, a screen reader, a test) loses it.
  const keep=old&&pinned&&panelOf.get(old)===pinned?old:null;
  if(!keep)old?.remove();
  document.querySelector('.rules-pill')?.setAttribute('aria-expanded',String(!!pinned&&pinned.target.kind==='rota'));
  if(!host||!pinned||!dataNow||pinned.workspace!==dataNow.workspace.id){old?.remove();return;}
  const t=pinned.target,state=pinned;
  // Focus goes back to what opened the panel (or the rules pill when that is gone after a refresh).
  const close=()=>{pinned=null;selecting=false;drawPanel();markSelection();const back=opener?.deref();(back?.isConnected?back:document.querySelector<HTMLElement>('.rules-pill'))?.focus({preventScroll:true});opener=null;};
  const x=el('button',{type:'button',class:'icon-button bare','aria-label':'Close rules panel (Esc)','data-tip':'Close · Esc'},icon('close'));
  // Select (touch has no modifier keys): while on, a tap adds to or takes from the selection; off again on close.
  const pick=t.kind==='rota'?null:el('button',{type:'button',class:'button ghost small rk-select','aria-pressed':String(selecting),'data-tip':'Tap things on the schedule to add or remove them'},icon('check'),'Select');
  pick?.addEventListener('click',()=>{selecting=!selecting;pick.setAttribute('aria-pressed',String(selecting));announce(selecting?'Select is on: tap to add or remove':'Select is off');});
  const body=el('div',{class:'rk-body'});
  const sub=el('small',{},t.sub||'');
  const aside=keep||el('aside',{class:'rk-panel',role:'dialog','aria-modal':'false','aria-labelledby':'rk-title'});
  aside.replaceChildren(
    el('header',{class:'rk-head'},el('span',{class:'rk-head-icon','aria-hidden':'true'},icon('rules')),el('div',{},el('h2',{id:'rk-title',class:'truncate'},t.kind!=='rota'?t.label:state.tab==='people'?'Who’s available':'Rules in this rota'),sub),pick||'',x),body);
  sidePanel(aside,{key:'rules-in-context',width:380,dismiss:close});
  panelGuard=guard(aside,close,true);
  x.addEventListener('click',()=>panelGuard!.tryClose());
  if(!keep){aside.addEventListener('keydown',e=>{if(e.key==='Escape'&&!(e.target as Element).closest('.rk-num-input')){e.stopPropagation();panelGuard!.tryClose();}});host.append(aside);}
  panelOf.set(aside,state);
  if(!ready()){
    body.append(working('Working out which rules apply…'));
    drawWhenReady(state,focus);
    return;
  }
  loadSentences();
  // The selection drives the panel (Why panel v2): one thing or several, else (nothing selected) the whole rota.
  const sel=t.kind==='rota'?[t]:state.sel||[t],many=sel.length>1;
  const rota:Target={kind:'rota',label:'Rules in this rota'};
  const items=itemsFor(sel);
  const marks=[...new Set(sel.flatMap(markersAt))].filter(m=>t.kind!=='rota'||m.kind!=='rota_adjustment');
  if(many){aside.querySelector('#rk-title')!.textContent=sel.every(x=>x.kind==='session')?plural(sel.length,'session'):`${sel.length} selected`;}
  const unexplained=items.filter(i=>i.state!=='replaced'&&said?.get(i.rule.id)?.unexplained).length;
  const rulesSub=[t.kind==='rota'?dataNow.workspace.name:many?sel.map(x=>x.label).join(' · '):t.sub,plural(items.filter(i=>i.state!=='replaced').length,'rule'),marks.length?plural(marks.length,'exception'):''].filter(Boolean).join(' · ');
  // Who's available is about the rota's dates: its heading line says them (the rules count belongs to the Rules tab).
  const wk=dataNow.workspace,peopleSub=`${wk.name} · ${formatDate(String(wk.starts_on).slice(0,10))} – ${formatDate(lastDay(String(wk.ends_on).slice(0,10)))}`;
  // DL-037: the rules the explainer could not word, counted once at the end of the rules line, in the warning tone.
  const subOf=(people:boolean)=>sub.replaceChildren(...people?[peopleSub]:[rulesSub,...unexplained?[' · ',unexplainedLine(TERMS.rulesUnexplained(unexplained))]:[]]);
  subOf(t.kind==='rota'&&state.tab==='people');
  const tools=el('div',{class:'rk-tools'});
  // Search only, and only when there is enough to need it (Why panel v2: the list orders itself; no views or switches).
  const bar=collectionToolbar({label:'Rules',
    search:items.length>8?{value:state.q,label:'Find a rule',placeholder:`Search ${plural(items.length,'rule')}`,onInput:q=>{state.q=q;},
      // A rule's own name and kind only: what it applies to is the server's rule map, never a text match.
      apply:renderList,options:{value:()=>state.search_options,set:o=>{state.search_options=o;},can:{scopes:['all'],kinds:[]}}}:undefined});
  tools.prepend(bar.root);
  if(t.kind==='rota'){const b=button(ROTA_TARGETS,()=>hooksNow!.rotaChanges(),'ghost small');b.prepend(icon('sliders'));tools.append(b);
    if(hooksNow!.addRotaRule){const a=button('Add a rule for this rota',()=>unpin(()=>hooksNow!.addRotaRule!()),'ghost small');a.prepend(icon('plus'));tools.append(a);}
    // A rule only for this rota is in the map once active; until then it is a governance fact, said here.
    const waiting=hooksNow!.ownRules?.()||[];
    if(waiting.length)tools.append(el('p',{class:'muted rk-none rk-own'},`Rules only for this rota · waiting for approval: ${waiting.map(r=>r.name).join(', ')}`));}
  // The list's key after it (dom.ts legend): each state mark and exception mark the list draws, entered as drawn.
  const list=el('div',{class:'rk-list'}),key=legend('Key'),listed=el('div',{},list,key.root);
  // A selection reads: its facts, its issues, then its rules (this selection's, or all the rota's).
  if(t.kind!=='rota'){
    // Tabs, few and the same for every kind (the shared tabs): a person's Details or a cell's, else a Summary; Rules;
    // Issues; and a person's Duties. The default follows what was clicked (pin's tab).
    const kind=selectionKind(sel),issues=issuesOf(sel),first=kind==='summary'?'summary':'details';
    const count=(text:string,n:number)=>el('span',{},text,n?el('span',{class:'rk-count'},String(n)):'');
    const panes:[string,Child,()=>Node][]=[[first,first==='details'?'Details':'Summary',()=>kind==='person'?el('div',{},hooksNow?.personDetails?.(sel[0].person!)||el('p',{class:'muted rk-none'},'No details to show.'))
        :kind==='cell'?cellDetails(sel[0]):(factsAt(sel)||el('p',{class:'muted rk-none'},'Nothing in this selection yet.')) as Node],
      ['rules',count('Rules',items.filter(i=>i.state!=='replaced').length),()=>el('div',{},tools,listed)],
      ['issues',count('Issues',issues.count),()=>issues.node],
      ...(kind==='person'?[['duties','Duties',()=>el('div',{},factsAt(sel),dutiesOf(sel[0].person!))] as [string,Child,()=>Node]]:[])];
    // A tab that this kind of selection does not have (Details after adding a day) falls back to its first.
    const wanted=state.tab==='details'||state.tab==='summary'?first:state.tab;
    body.append(tabs('Selection',panes,panes.some(p=>p[0]===wanted)?wanted!:panes[0][0],k=>{state.tab=k as PanelTab;}));
  }
  // The rota's panel answers both questions beside the schedule: its rules, and who is available over its dates.
  else body.append(tabs('Rules and people',[['rules','Rules',()=>el('div',{},tools,listed)],
    ['people','Who’s available',()=>availabilityPanel(dataNow!.workspace,dataNow!.current_availability_revision,id=>String((ctxNow?.catalogues.categories||[]).find(c=>c.id===id)?.short_code||''))]],state.tab||'rules',
    k=>{state.tab=k as PanelTab;aside.querySelector('#rk-title')!.textContent=k==='people'?'Who’s available':'Rules in this rota';subOf(k==='people');}));
  function renderList(){
    const q=recordMatcher(state.q,state.search_options);
    const inView=items,shown=inView.filter(i=>!q||q({own:()=>[i.rule.name,i.rule.kind.replaceAll('_',' ')]}));bar.shown(shown.length,inView.length,['rule','rules']);
    const out:HTMLElement[]=[];key.clear();  // the key lists what this draw draws
    // Rows that are marked here (an exception, an own number, a rota-only change) count as changed.
    const marked=new Set(marks.map(m=>m.rule_id)),row=(i:Item)=>ruleRow(i,many?rota:t,()=>drawPanel(),key,marked.has(i.rule.id));
    // A long list draws its first rows, and the open rule wherever it is, until "Show all".
    const cut=(name:string,list:Item[])=>{
      const shownNow=state.all?.includes(name)||list.length<=FIRST+5?list:[...list.slice(0,FIRST),...list.slice(FIRST).filter(i=>openRules.has(i.rule.id))];
      const more=list.length-shownNow.length;
      return [el('ul',{class:'rk-rules'},...shownNow.map(row)),...(more?[button(`Show all ${list.length}`,()=>{state.all=[...(state.all||[]),name];renderList();},'ghost small rk-more')]:[])];};
    {
      // One list (CONTRACT §10): what needs fixing, then what changed here, then the rest folded under one worded
      // toggle. A search shows every match unfolded.
      // A rule whose lines overlap stays in view on the rota's list: its row is where the overlap is said.
      const changedHere=(i:Item)=>changed(i.rule)||marked.has(i.rule.id)||!!i.rule.proposal||t.kind==='rota'&&!!overlapWords(said?.get(i.rule.id),overlaps.get(i.rule.id));
      // DL-037: a rule the explainer could not word is never folded away, safety rules first (as the subtitle counts).
      const bad=shown.filter(i=>['breach','warning'].includes(i.state));
      const unexp=shown.filter(i=>!bad.includes(i)&&i.state!=='replaced'&&said?.get(i.rule.id)?.unexplained).sort((a,b)=>+b.rule.safety-+a.rule.safety);
      const rest=shown.filter(i=>!bad.includes(i)&&!unexp.includes(i));
      const top=[...bad,...unexp,...rest.filter(changedHere)],fold=q?[]:rest.filter(i=>!changedHere(i));
      if(q)top.push(...rest.filter(i=>!changedHere(i)));
      // Two rules that can't both hold, first (the server finds them; CONTRACT §6): those that reach this selection.
      const here=new Set(items.map(i=>i.rule.id)),clash=q?[]:conflicts.filter(c=>t.kind==='rota'||c.rule_ids.some(id=>here.has(id)));
      if(clash.length)out.push(el('ul',{class:'rk-rules rk-conflicts'},...clash.map(c=>conflictRow(c,key,()=>{
        // Open both: their rows opened side by side in this list (unfolded when either sits in the fold).
        openRules=new Set(c.rule_ids);if(c.rule_ids.some(id=>fold.some(i=>i.rule.id===id)))state.met=true;renderList();
        requestAnimationFrame(()=>{const first=list.querySelector<HTMLElement>(c.rule_ids.map(id=>`[data-rk-row="${CSS.escape(id)}"] .rk-row-head`).join(','));
          first?.scrollIntoView({block:'nearest'});first?.focus({preventScroll:true});});}))));
      if(top.length)out.push(...cut('top',top));
      // An open row stays where it is read (D-057): a fold holding it stays unfolded, never moves the row or hides it.
      if(fold.some(i=>openRules.has(i.rule.id)))state.met=true;
      if(fold.length){
        const by=(st:string)=>fold.filter(i=>i.state===st).length,met=by('met');
        const parts=[[met,'met'],[by('unchecked'),'not checked yet'],[by('replaced'),'decided by a more specific rule']].filter(([n])=>n).map(([n,w])=>`${n} ${w}`);
        const words=met===fold.length?`${fold.length} more ${fold.length===1?'is':'are'} met`:by('unchecked')===fold.length?`${fold.length} more, not checked yet`:`${fold.length} more: ${parts.join(', ')}`;
        const id='rk-fold-'+(t.kind==='rota'?'rota':'sel');
        const toggle=el('button',{type:'button',class:'link-button rk-fold','aria-expanded':String(!!state.met),'aria-controls':id},words);
        toggle.addEventListener('click',()=>{state.met=!state.met;if(!state.met)openRules=new Set([...openRules].filter(id=>!fold.some(i=>i.rule.id===id)));renderList();list.querySelector<HTMLElement>('.rk-fold')?.focus({preventScroll:true});});
        out.push(toggle);
        if(state.met)out.push(el('div',{id},...cut('fold',fold)));
      }
      if(!shown.length)out.push(el('p',{class:'rk-none muted'},q?'No rule matches that search.':t.kind==='rota'?'No department rules apply to this rota. Basic staffing and overlap checks still apply.':'No rule applies to '+targetWords(t)+'.'));
    }
    list.replaceChildren(...out);
  }
  renderList();
  markSelection();
  body.scrollTop=scroll;
  if(back?.search){const q=aside.querySelector<HTMLInputElement>('input[type=search]');q?.focus({preventScroll:true});q?.setSelectionRange(back.at??q.value.length,back.at??q.value.length);}
  else if(back?.row)aside.querySelector<HTMLElement>(`[data-rk-row="${CSS.escape(back.row)}"] .rk-row-head`)?.focus({preventScroll:true});
  else{
    // Any other control of the panel that held focus (a tool, a tab, Close) is drawn again: its new self keeps it, so a
    // redraw (the rules' sentences arriving, a live refresh) never drops the keyboard to the page behind the panel.
    const said=(n:Element)=>n.getAttribute('aria-label')||n.textContent,again=had?[...aside.querySelectorAll<HTMLElement>('button,[role=tab],input,select')].find(n=>n.tagName===had.tagName&&said(n)===said(had)):null;
    if(again)again.focus({preventScroll:true});
    else if(focus||had)requestAnimationFrame(()=>(aside.querySelector<HTMLElement>('.rk-row-head,.rk-tools button,input')||aside.querySelector<HTMLElement>('[role=tab][aria-selected=true]'))?.focus({preventScroll:true}));
  }
}
/** The planner's session panel, lent by planner_week.ts (it imports this module, so it registers here). */
let sessionOpener:((shift:Row)=>boolean)|null=null;
export const setSessionOpener=(open:(shift:Row)=>boolean)=>{sessionOpener=open;};
/** A selection's own facts, one line of chips: its sessions, places filled / needed and open (the last check's), and a
 *  person's duties, hours and workload targets in this rota. Never a list of dates. */
function factsAt(sel:Target[]):HTMLElement|string{
  if(!dataNow)return '';
  const shifts=dataNow.shifts as Row[],chip=(b:string,words:string)=>el('span',{class:'rk-fact'},el('b',{},b),' '+words);
  const people=[...new Set(sel.filter(t=>t.person&&t.kind!=='assignment').map(t=>t.person!))];
  const ids=new Set(sel.flatMap(t=>t.kind==='person'?[]:t.shift?[t.shift]:t.shifts||[]));
  const out:HTMLElement[]=[];
  if(ids.size||sel.some(t=>t.kind!=='person')){
    const list=shifts.filter(s=>ids.has(s.id));let need=0,open=0;
    for(const s of list){const c=hooksNow?.cover?.(s.id);if(c){need+=c.required;open+=c.open;}}
    out.push(chip(String(list.length),list.length===1?'session':'sessions'));
    if(need)out.push(chip(`${need-open}/${need}`,'filled'));else out.push(chip(String(list.reduce((n,s)=>n+(s.participants||[]).length,0)),'assigned'));
    if(open)out.push(chip(String(open),open===1?'open place':'open places'));
  }
  if(people.length){
    const mine=shifts.flatMap(s=>((s.participants||[]) as Row[]).filter(p=>people.includes(p.person_id)).map(p=>({s,p})));
    const hours=mine.reduce((h,{s,p})=>h+(Date.parse(p.end||s.end)-Date.parse(p.start||s.start))/3.6e6,0);
    out.push(chip(String(mine.length),mine.length===1?'duty':'duties'),chip(numOne(hours),'h'));
    if(people.length===1)for(const r of (dataNow.workspace.validation?.quota_report?.rows||[]) as Row[])if(r.person_id===people[0])
      out.push(chip(`${numOne(Number(r.actual))}/${numOne(Number(r.amount))}`,r.policy_name));
  }
  return out.length?el('div',{class:'rk-facts-row',role:'group','aria-label':'Facts'},...out):'';
}
/** What a selection is, for its tabs: one person, one cell (a row on a day), or anything else (a summary). */
const selectionKind=(sel:Target[]):'person'|'cell'|'summary'=>sel.length!==1?'summary':sel[0].kind==='person'?'person':sel[0].kind==='session'||sel[0].kind==='row'&&sel[0].day?'cell':'summary';
/** A selection's issues (the rota's last check), in the one issue list (issue_marker.ts issueList): the same sentence is
 *  one item with its sessions as chips (the day, and who), each going to its session; it ends with Open all in Fix list. */
function issuesOf(sel:Target[]):{count:number;node:HTMLElement}{
  const all=(dataNow?.workspace.validation?.findings||[]) as unknown as Row[],found=new Set<Row>();
  for(const t of sel)for(const f of findingsOf(t,all))found.add(f);
  const shifts=new Map((dataNow?.shifts||[]).map(s=>[s.id as string,s])),people=ctxNow?.catalogues.people||[],z=zone();
  const xs=[...found].map(f=>{const s=f.shift_id?shifts.get(f.shift_id):undefined,who=f.person_id?fullNameOf(people.find(p=>p.id===f.person_id)):'';
    return findingIssue(f,[s?formatDate(s.start,z):'',who].filter(Boolean).join(' · '));});
  return {count:new Set(xs.map(i=>i.severity+'|'+i.words)).size,
    node:xs.length?issueList(xs,{limit:20}):el('p',{class:'muted rk-none'},dataNow?.workspace.validation?'Nothing to fix here.':'Not checked yet.')};
}
/** A cell's details: each session's time, place and clinics, and its people (each name opens their card); Open session
 *  opens the session panel, where it is staffed. */
function cellDetails(t:Target):HTMLElement{
  const cat=ctxNow?.catalogues,name=(kind:string,id:string)=>labelOf((cat?.[kind]||[]).find((x:Row)=>x.id===id))||'';
  const list=(dataNow?.shifts||[]).filter(s=>(t.shifts||[]).includes(s.id)),z=zone();
  const facts=factsAt([t]);
  if(!list.length)return el('div',{},facts||'',el('p',{class:'muted rk-none'},'No session here.'));
  return el('div',{},facts||'',...list.map(s=>{const open=button('Open session',()=>{if(!sessionOpener?.(s))notify('Open the planner to see this session.','info');},'ghost small');
    return el('section',{class:'rk-session','aria-label':s.type_snapshot.name},el('h3',{class:'rk-h3'},String(s.type_snapshot.name)),
      el('p',{class:'rk-session-facts'},[`${formatTime(s.start,z)}–${formatTime(s.end,z)}`,name('locations',s.location_id),...(s.service_ids||[]).map((id:string)=>name('services',id))].filter(Boolean).join(' · ')),
      (s.participants||[]).length?el('ul',{class:'rk-people'},...s.participants.map(p=>el('li',{},personLink(p.person_id,name('people',p.person_id)||'Someone',v=>ctxNow?.navigate(v))))):el('p',{class:'muted'},'No one yet'),open);}));
}
/** A person's duties in this rota, in date order: each opens its session. */
function dutiesOf(personId:string):HTMLElement{
  const z=zone(),mine=(dataNow?.shifts||[]).filter(s=>s.participants.some(p=>p.person_id===personId)).sort((a,b)=>String(a.start).localeCompare(String(b.start)));
  if(!mine.length)return el('p',{class:'muted rk-none'},'No duties in this rota.');
  return el('ul',{class:'rk-rules rk-duties'},...mine.map(s=>{const b=el('button',{type:'button',class:'link-button'},`${formatDate(s.start,z)} · ${formatTime(s.start,z)}–${formatTime(s.end,z)} · ${s.type_snapshot.name}`);
    b.addEventListener('click',()=>{if(!sessionOpener?.(s))notify('Open the planner to see this session.','info');});return el('li',{},b);}));
}
/** A pinned panel waiting for the rule map draws again once it arrives. Kept out of drawPanel on purpose: a callback
 *  waiting on the network there would share its scope (the panel, and through it the whole view) until the answer came,
 *  so a redraw meanwhile could not release the old view. It finds the panel shown by then instead. */
function drawWhenReady(state:object,focus:boolean){
  if(!dataNow)return;
  // The rota version asked for (a string, not the data): a live refresh's newer answer owns the panel, so an older
  // request settling later, above all failing, must not draw over it.
  const asked=keyOf(dataNow),mine=()=>answerApplies(pinned,state,dataNow,asked);
  const shown=()=>host?.querySelector<HTMLElement>(':scope>.rk-panel');
  // Opening it still moves focus in once its rules arrive, unless the person has gone elsewhere meanwhile.
  loadRuleMap(dataNow).then(()=>{if(mine())drawPanel(focus&&(document.activeElement===document.body||document.activeElement===opener?.deref()||!!shown()?.contains(document.activeElement)));})
    .catch(e=>{if(mine())shown()?.querySelector('.rk-body')?.replaceChildren(note((e as Error).message,'error',e),button('Try again',()=>drawPanel(),'secondary small'));});
}
/** Whether a rule-map answer asked for rota version `asked` by the pinned panel `state` may still draw: the same pin is
 *  open and the planner still shows that version. */
export const answerApplies=(pinnedNow:object|null,state:object,data:WorkspaceData|null,asked:string)=>pinnedNow===state&&!!data&&keyOf(data)===asked;
/** Hovering (or focusing) a rule previews its highlight; leaving restores the locked one. */
function previewOn(node:HTMLElement,r:MapRule){
  if(touch.matches)return;
  const on=()=>{if(preview!==r.id){preview=r.id;applyHighlight();}},off=()=>{if(preview===r.id){preview='';applyHighlight();}};
  node.addEventListener('mouseenter',on);node.addEventListener('mouseleave',off);
  node.addEventListener('focusin',on);node.addEventListener('focusout',off);
}
/** A row's words (CONTRACT §10): one sentence for the rule here, then its status in words; nothing else when folded (no
 *  rule name, number chip or pill: those are the opened row's and the rule's own page). `changed`: a rota change, an
 *  exception here or a proposal waiting, which keeps the row above the fold. */
type Words={text:string;status:string;changed:boolean;/** DL-037: the explainer could not word it; its line replaces any sentence. */unexplained:boolean};
const counted=(n:number,one:string,many:string)=>n<=1?one:many.replace('#',String(n));
const stop=(s:string)=>/[.!?…”]$/.test(s.trim())?s.trim():s.trim()+'.';
/** shortcut: until the explainer serves a selection's rows (`target=` with `status.sentence` and `changed`, agreed with
 *  LEAD-RULES-CONFIG), the browser words the status from the rule map and its findings and picks the rule's sentence by
 *  its line; delete this with numberFor, stack and findingsFor when those fields land. */
function wordsOf(i:Item,marked=false,rota=false):Words{
  const r=i.rule,line=String(i.own?.id||''),told=said?.get(r.id),list=told?.sentences,p=hooksNow?.policy(r.id);
  // The whole rota: overlapping lines of one rule are its row, with each line's number and why the strictest binds.
  const lap=rota?overlapWords(told,overlaps.get(r.id)):null;
  const byLine=list?.find(x=>!!line&&!!x.source_line_ids?.includes(line)),s=byLine||(!i.own&&list?.length===1?list[0]:undefined);
  // A person's own line without a line sentence (a template rule's per-person or rank table): the rule's sentence with
  // THEIR number, never the department's nor another line's sentence (the rule's only sentence may be someone else's row).
  const own=!byLine&&i.own&&p&&p.definition.rule.kind!=='catalogue'&&r.number&&i.number
    ?hooksNow!.sentence({...p.definition.rule,[r.number.field]:i.number.value},p.definition):'';
  // The line's own sentence, else the rule's summary (live 235391b: 14 of 17 rows fell back to catalogue text). The
  // rule's own wording only while the explainer has not answered (or failed), never beside its answer.
  const text=lap?lap.text:stop(own||s?.text||told?.summary_sentence||(p?hooksNow!.sentence(p.definition.rule,p.definition):r.name));
  const state=i.state==='breach'?counted(i.errors,'Not met.','Not met # times.'):i.state==='warning'?counted(i.warnings,'Could be better.','Could be better in # places.')
    :i.state==='off'?'Switched off for this rota.':i.state==='replaced'?'A more specific rule decides here.':i.state==='unchecked'?'Not checked yet.':'Met.';
  const notes=[lap?.why||'',bindsNote(i.number,bindsOf(r,i.own)),r.adjusted&&i.state!=='off'&&r.adjusted.applied!==false?'Changed for this rota.':'',marked?'An exception applies here.':'',
    r.proposal?'A change is waiting for approval.':'',i.of?`Applies to ${i.hits} of ${i.of}.`:''];
  return {text,status:[state,...notes].filter(Boolean).join(' '),changed:changed(r)||marked||!!r.proposal||!!lap,unexplained:!!told?.unexplained};
}
/** One rule's overlapping lines (the server's conflicts naming the same rule twice, one per pair of lines): per rule,
 *  each line once with its department-wide number. They are that rule's row, never rows of their own. */
export function ownOverlaps(cs:Conflict[]):Map<string,Map<string,number>>{
  const out=new Map<string,Map<string,number>>();
  for(const c of cs)if(c.rule_ids[0]===c.rule_ids[1]){const m=out.get(c.rule_ids[0])||new Map<string,number>();
    c.line_ids.forEach((id,n)=>m.set(id,c.line_refs[n]));out.set(c.rule_ids[0],m);}
  return out;
}
/** Where lines overlap, every one of them applies and the strictest binds (AGENTS §3); said once per rule. */
export const OVERLAP_WHY='Where these lines overlap, all of them apply, so the strictest binds.';
/** A rule with overlapping lines, said once: the explainer's sentence of each overlapping line (its number, in the
 *  server's words and order), then why the strictest binds; opened, which lines (by number). Null when the explainer
 *  has no sentence for them. */
export function overlapWords(told:ExplainedRule|undefined,lines:Map<string,number>|undefined){
  const xs=lines&&told?.sentences.filter(x=>x.source_line_ids?.some(id=>lines.has(id)))||[];
  if(!xs.length)return null;
  const refs=[...new Set(lines!.values())].sort((a,b)=>a-b).map(n=>'#'+n);
  return {text:xs.map(x=>stop(x.text)).join(' '),why:OVERLAP_WHY,
    lines:`Lines ${refs.length>1?refs.slice(0,-1).join(', ')+' and '+refs[refs.length-1]:refs[0]} overlap.`};
}
/** A conflict row: the server's sentence; opened, the two line numbers (the only place lines are numbered) and how much
 *  of the rota it reaches, then Open both. */
let openConflict='';
function conflictRow(c:Conflict,key:Legend,both:()=>void):HTMLElement{
  const open=openConflict===c.conflict_id,id='rk-c-'+c.conflict_id.replace(/[^\w-]/g,'_');
  const glyph=key.mark(issueGlyph('error'),'conflict',issueGlyph('error'),'Two rules can’t both be true');
  const head=el('button',{type:'button',class:'rk-row-head','aria-expanded':String(open),...(open?{'aria-controls':id}:{})},glyph,el('span',{class:'rk-row-text'},stop(c.sentence)));
  head.addEventListener('click',()=>{openConflict=open?'':c.conflict_id;drawPanel();
    requestAnimationFrame(()=>document.querySelector<HTMLElement>(`[data-rk-conflict="${CSS.escape(c.conflict_id)}"] .rk-row-head`)?.focus({preventScroll:true}));});
  const li=el('li',{class:'rk-row err rk-conflict'+(open?' open':''),'data-rk-conflict':c.conflict_id},head);
  if(open){const refs=c.line_refs,n=c.reach.sessions_count;
    li.append(el('div',{class:'rk-detail',id},
      ...(refs.length===2?[el('p',{},`Lines #${refs[0]} and #${refs[1]}.`)]:[]),
      ...(n?[el('p',{},`${plural(n,'session')} ${n===1?'is':'are'} affected.`)]:[]),
      el('div',{class:'rk-acts'},button('Open both',both,'primary small'))));}
  return li;
}
/** The safety lock, said in words to a screen reader and entered in the key as drawn. */
const lockMark=(key?:Legend)=>{const m=()=>el('span',{class:'rk-lockmark',role:'img','aria-label':TERMS.safetyRuleProtection},icon('lock'));
  return key?key.mark(m(),'safety',el('span',{class:'rk-lockmark','aria-hidden':'true'},icon('lock')),TERMS.safetyRuleProtection):m();};
function ruleRow(i:Item,t:Target,redraw:()=>void,key?:Legend,marked=false):HTMLElement{
  const r=i.rule,[tone]=STATE[i.state],open=openRules.has(r.id),locked=lock?.rule===r.id,w=wordsOf(i,marked,t.kind==='rota');
  const head=el('button',{type:'button',class:'rk-row-head','aria-expanded':String(open),...(open?{'aria-controls':'rk-d-'+r.id}:{})},
    stateMark(i,key),el('span',{class:'rk-row-text'},el('span',{class:'rk-row-sentence'},w.unexplained?unexplainedLine():w.text),' ',el('span',{class:'rk-status'},w.status)),
    r.safety?lockMark(key):'');
  const li=el('li',{class:'rk-row '+tone+(open?' open':'')+(locked?' locked':''),'data-rk-row':r.id,'data-rk-name':r.name},head);
  previewOn(li,r);
  head.addEventListener('click',()=>{const scroller=head.closest('.rk-body,.wk-panel-body'),top=scroller?.scrollTop||0;openRules=open?new Set([...openRules].filter(x=>x!==r.id)):new Set([r.id]);
    // Opening a rule keeps its highlight on the schedule; closing it lets go.
    if(!open)setLock(r.id);else if(lock?.rule===r.id)setLock(null);redraw();
    requestAnimationFrame(()=>{const again=document.querySelector<HTMLElement>(`[data-rk-row="${CSS.escape(r.id)}"] .rk-row-head`);again?.focus({preventScroll:true});const sc=again?.closest('.rk-body,.wk-panel-body');if(sc)sc.scrollTop=top;});});
  if(open)li.append(ruleDetail(i,t,redraw));
  return li;
}
/** The popover key of a rule row's ⋯ menu (awaitAdoption in mountRules). */
const RULE_MORE='rule-more';
/** The opened row (CONTRACT §10): a few short sentences (where the number comes from, what changed here with who, when,
 *  why and its review, what it overrides), then Fix when it fails and ⋯ for the rest (the shared menu). Readers who
 *  can't change it, and phones, get Open rule only. A chosen change opens its short form below. */
function ruleDetail(i:Item,t:Target,redraw:()=>void):HTMLElement{
  const r=i.rule,h=hooksNow!,policy=h.policy(r.id);
  const acts=el('div',{class:'rk-acts'});
  const d=el('div',{class:'rk-detail',id:'rk-d-'+r.id},...detailWords(i,t).map(x=>el('p',{},x)),acts);
  if(touch.matches||!h.editable&&!h.canPropose){if(h.openRule)acts.append(button('Open rule',()=>h.openRule!(r.id,t.person),'secondary small'));return d;}
  if(['breach','warning'].includes(i.state)&&h.fix)acts.append(button('Fix',()=>h.fix!(t.shift?{shift:t.shift}:undefined),'primary small'));
  const more=opens(el('button',{type:'button',class:'icon-button rk-more','aria-label':'More actions','data-tip':'More actions'},icon('dots')),'menu');
  // D-057: a live redraw (the sentences or rule map arriving) rebuilds this row; the open ⋯ moves to its new button and
  // reads that draw's items (menuSource), never closes under the person or acts on the previous draw's rule.
  menuSource(more,()=>ruleMenu(i,t,policy,redraw));
  more.addEventListener('click',()=>menu(more,'More actions',()=>ruleMenu(i,t,policy,redraw),{key:RULE_MORE,keep:`[data-rk-row="${CSS.escape(r.id)}"] .rk-detail .rk-more`}));
  acts.append(more);
  const form=openAct===r.id+':rota'?rotaOnly(r):openAct===r.id+':exc'?exceptionAct(r,t,policy):null;
  if(form?.tagName==='FORM'){
    const cancel=button('Cancel',()=>{openAct='';redraw();requestAnimationFrame(()=>document.querySelector<HTMLElement>(`[data-rk-row="${CSS.escape(r.id)}"] .rk-more`)?.focus());},'ghost small');
    d.append(el('div',{class:'rk-act open',id:'rk-a-'+openAct.replace(/[^\w-]/g,'_')},form,cancel));}
  return d;
}
/** shortcut: the opened row's sentences are worded here from the rule map and the explainer's who/where until it serves
 *  source_sentence, change_sentence and override_sentence (CONTRACT §10); delete with wordsOf. */
function detailWords(i:Item,t:Target):string[]{
  const r=i.rule,line=String(i.own?.id||''),list=said?.get(r.id)?.sentences;
  const byLine=list?.find(x=>!!line&&!!x.source_line_ids?.includes(line)),s=byLine||(!i.own&&list?.length===1?list[0]:undefined);
  const dept=r.adjusted?.mode==='set'&&r.department_value!=null&&r.number?`: ${numBoth({...r.number,value:r.department_value,maximum:null},r.binding_value)}`:'';
  const source=r.workspace_only?'From a rule added for this rota only.'
    :`From the department rule${s&&s.who&&s.who!=='Everyone'?' for '+s.who:''}${s&&s.where&&s.where!=='All clinics and duties'?' at '+s.where:''}${!byLine&&i.own?` (${lineWords(i.own)})`:''}${dept}`;
  const adjustments=adjustmentsOf(r).map(a=>{const who=byWho(a.by,a.by_name),person=a.person_id?fullNameOf(ctxNow?.catalogues.people.find(p=>p.id===a.person_id)):'';
    const tail=`${person?' for '+person:''}${a.starts_on&&a.ends_on?` from ${formatDate(a.starts_on)} to ${formatDate(lastDay(a.ends_on))}`:''}${who?' by '+who:''}${a.at?' on '+formatDate(String(a.at).slice(0,10)):''}${a.reason?`: “${a.reason}”`:''}`;
    if(a.mode==='off')return `Switched off for this rota${tail}`;
    const was=r.department_value!=null&&!a.line_id?r.department_value:null,now=Number(a.amount);
    return `${was==null?'Changed':now>was?'Raised':now<was?'Lowered':'Set'}${was!=null?' from '+numOne(was):''} to ${numOne(now)} for this rota${tail}`;});
  const marks=markersAt(t).filter(m=>m.rule_id===r.id&&m.kind!=='rota_adjustment').map(m=>`${markerWhat[m.kind]||'Exception'}: ${m.text}. ${markerWho(m)}`);
  const override=r.overrides.length?'This overrides a more general rule here.':i.binding?'It is the strictest rule of its kind here, so it decides.':i.looser?'A stricter rule of its kind decides here.':'';
  const lap=t.kind==='rota'?overlapWords(said?.get(r.id),overlaps.get(r.id))?.lines??'':'';
  const stale=r.adjusted?.applied===false?'The department rule applies. The older adjustment is not applied.':'';
  return [source,lap,stale,...adjustments,adjustments.length?reviewState()[0]:'',...marks,r.proposal?`${proposalWords(r.proposal)}: a change waiting for approval`:'',override].filter(Boolean).map(stop);
}
/** The opened row's ⋯: open the rule, show it on the schedule, change it for this rota (stricter only for a safety rule),
 *  an exception (never for safety), or propose it for everyone. A disabled item says why. */
function ruleMenu(i:Item,t:Target,policy:Row|undefined,redraw:()=>void):MenuEntry[]{
  const r=i.rule,h=hooksNow!,out:MenuEntry[]=[];
  const show=(key:string)=>()=>{openAct=key;redraw();
    requestAnimationFrame(()=>document.querySelector<HTMLElement>(`#rk-a-${key.replace(/[^\w-]/g,'_')} :is(input:not([type=hidden]),select,button)`)?.focus());};
  if(h.openRule)out.push({label:'Open rule',icon:'rules',run:()=>h.openRule!(r.id,t.person)});
  const on=lock?.rule===r.id;
  out.push({label:on?'Stop showing on the schedule':'Show on the schedule',icon:'pin',run:()=>setLock(on?null:r.id)});
  const st=String(dataNow!.workspace.status);
  const rotaWhy=!h.editable?(st!=='draft'?'Only a draft rota can be changed. Reopen it as a draft first.':'Only coordinators and admins change a draft rota.')
    :r.workspace_only?'This rule was added for this rota only; change the rule itself.'
    :r.safety&&!r.adjusted&&(!r.adjustable||!r.number)?(r.lines.length?'Change these line numbers through a department rule proposal.':'It has no number to make stricter here; the department rule changes with approval.'):'';
  out.push({label:r.safety?'Make stricter for this rota':'Adjust for this rota',icon:'calendar',run:show(r.id+':rota'),disabled:!!rotaWhy,hint:rotaWhy||undefined});
  // An exception (AGENTS §3: never for a safety kind): offered when the rule and the selection allow one, else said why.
  if(!r.safety){const f=exceptionAct(r,t,policy);
    out.push(f.tagName==='FORM'?{label:f.getAttribute('aria-label')+'…',icon:'fork',run:show(r.id+':exc')}
      :{label:f.querySelector('.rk-off-label')?.textContent||'Make an exception',icon:'fork',run:()=>undefined,disabled:true,hint:f.querySelector('small')?.textContent||undefined});}
  const adj=adjustmentsOf(r),amount=r.adjusted?.mode==='set'&&r.adjusted.applied!==false&&r.adjusted.amount!=null?Number(r.adjusted.amount):undefined;
  const defWhy=!policy?'This rule is not in your rule list.':!h.canPropose?'Coordinators and admins propose rule changes; an approver activates them.'
    :r.proposal?`A change is waiting for approval: ${proposalWords(r.proposal).replace(/^Proposed/,'proposed')}.`:'';
  // A catalogue rule with one rota adjustment promotes that one (the server's seed); with several, each has its own item.
  const one=r.kind==='catalogue'&&adj.length===1?String(adj[0].id||''):'';
  out.push({label:'Make department default…',icon:'edit',run:()=>h.changeForEveryone(policy!,amount,one||undefined),disabled:!!defWhy,
    hint:defWhy||undefined});  // AGENTS §2: an enabled item says what it does in its label; only a disabled one says why
  if(!defWhy&&r.kind==='catalogue'&&adj.length>1)for(const a of adj)if(a.id&&a.mode!=='off')
    out.push({label:`Make ${numOne(Number(a.amount))} the department default…`,icon:'edit',run:()=>h.changeForEveryone(policy!,undefined,String(a.id))});
  return out;
}
/** A change chosen from ⋯ stays open across live refreshes (one at a time). */
let openAct='';
function disabledAct(label:string,why:string){return el('div',{class:'rk-off'},el('span',{class:'rk-off-label'},label),el('small',{class:'muted'},why));}
/** "Proposed by Dr A · 8 Nov": who proposed a waiting change and when (the server's privacy-safe name, or none). */
const proposalWords=(p:RuleMapProposal)=>['Proposed'+(p.by_name?' by '+p.by_name:''),p.at?formatDate(String(p.at).slice(0,10)):''].filter(Boolean).join(' · ');
const lastDay=(end:string)=>{const x=new Date(end+'T12:00:00Z');x.setUTCDate(x.getUTCDate()-1);return x.toISOString().slice(0,10);};
function rotaOnly(r:MapRule):HTMLElement{
  // D16: every change for this rota carries its justification; the approver reads it with the rota.
  const why=el('input',{type:'text',class:'rk-why',required:true,minLength:3,maxLength:500,placeholder:'Why (reviewed with the rota)','aria-label':'Justification'}) as HTMLInputElement;
  // The rota as held now (a kept panel outlives the data it was drawn from).
  const apply=async(mode:'off'|'set'|'clear',amount?:number)=>{const w=dataNow!.workspace;
    await command('workspace.rule_adjust',{policy_id:r.id,mode,...(mode==='set'?{amount}:{}),reason:why.value.trim()},w.id,w.revision);
    box.dispatchEvent(new Event('rota-form-saved',{bubbles:true}));
    notify(mode==='clear'?`“${r.name}” is back to the department rule for this rota.`:mode==='off'?`“${r.name}” is switched off for this rota.`:`“${r.name}” changed for this rota.`);
    await hooksNow!.refresh();};
  const act=(mode:'off'|'clear')=>()=>{if(!why.reportValidity())return;return apply(mode).catch(err=>notify((err as Error).message,'error',undefined,err));};
  const label=r.safety?'Make stricter for this rota':'Adjust for this rota';
  const box=el('form',{class:'rk-inline','aria-label':label});
  const title=el('span',{class:'rk-act-title'},label);
  box.append(title);
  if(r.adjustable&&r.number&&!(r.adjusted&&r.adjusted.applied===false)){
    const now=bindsNow(r.number,r.binding_value);
    const n=el('input',{type:'number',class:'rk-num-input',min:r.safety&&r.number.stricter==='higher'?(r.department_value??r.number.value):0,...(r.safety&&r.number.stricter==='lower'?{max:r.department_value??r.number.value}:{}),step:'any',value:String(r.number.value),'aria-label':`New number for “${r.name}” in this rota${now!=null?`, ${numBoth(r.number,now)}`:''}`,required:true}) as HTMLInputElement;
    const use=el('button',{type:'submit',class:'button primary small'},'Use for this rota');
    box.addEventListener('submit',e=>{e.preventDefault();if(!n.reportValidity()||!why.reportValidity())return;const v=Number(n.value);if(!n.value||v===r.number!.value){n.focus();return;}
      use.disabled=true;apply('set',v).catch(err=>{use.disabled=false;notify((err as Error).message,'error',undefined,err);});});
    box.append(el('span',{class:'rk-inline-row'},n,el('span',{class:'muted'},r.number.unit+(now!=null?`, now binds ${numOne(now)}`:'')),use));
  }
  box.append(why);
  const back=r.adjusted?button('Back to the department rule',act('clear'),'secondary small'):null;
  const off=r.safety||r.adjusted?.mode==='off'?null:button('Switch off for this rota',act('off'),'ghost small danger-text');
  box.append(el('span',{class:'rk-inline-row'},...(back?[back]:[]),...(off?[off]:[])),el('small',{class:'muted'},'Applies at once to this draft only; Undo is offered. Reviewed with the rota.'));
  return box;
}
function exceptionAct(r:MapRule,t:Target,policy:Row|undefined):HTMLElement{
  const h=hooksNow!,person=t.person,subject=subjectWord[t.kind];
  const title=el('span',{class:'rk-act-title'},subject?`Make an exception for ${subject}`:'Make an exception');
  if(!subject)return disabledAct('Make an exception','Point at a person, clinic, place, duty or day to make an exception for it.');
  if(!policy)return disabledAct(title.textContent!,'This rule is not in your rule list.');
  const waiver=!r.safety&&!!person&&r.exception_allowed;
  const override=!r.safety&&!!r.overridable&&!!r.number;
  if(!waiver&&!override){
    const why=r.safety?`“${r.name}” keeps people safe, so it can’t have an exception for ${subject}. Changes to the department rule need approval.`
      :!r.number?`“${r.name}” has no number to set for ${subject}. Change it for this rota only, or change the rule for everyone.`
      :person&&!r.overridable?`Only limits with a number or advisory rules can have their own number for one person, and this rule doesn’t allow person exceptions.`
      :`A mandatory rule can only be replaced by the same kind of limit for a narrower group. Change it for this rota only, or change the rule for everyone.`;
    return el('div',{class:'rk-off refused',role:'note'},el('span',{class:'rk-off-label'},icon('lock'),title.textContent),el('small',{},why));
  }
  if(!(waiver?h.canRequestException||h.canPropose:h.canPropose))return disabledAct(title.textContent!,'Coordinators and admins request exceptions; an approver decides them.');
  const w=dataNow!.workspace,day=t.day||(t.shift?shiftDay(t.shift):'');
  const next=(d:string)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+1);return x.toISOString().slice(0,10);};
  const clip=(a:string,b:string)=>[a<(policy.starts_on||a)?policy.starts_on:a,policy.ends_on&&policy.ends_on<b?policy.ends_on:b];
  const [from0,to0]=t.kind==='day'?[day,next(day)]:clip(String(w.starts_on).slice(0,10),String(w.ends_on).slice(0,10));
  const form=el('form',{class:'rk-inline','aria-label':title.textContent!});
  // One segmented choice (dom.ts segmented): a new number or a waiver, when the rule allows both.
  let kind:'own'|'waive'='own';
  const mode=waiver&&override?segmented<'own'|'waive'>('Kind of exception',[['own',`New number for ${subject}`],['waive',`Waive for ${subject}`]],kind,k=>{kind=k;sync();},{cls:'rk-mode'}):null;
  const isWaive=()=>waiver&&(!override||kind==='waive');
  const num=r.number?el('input',{type:'number',class:'rk-num-input',min:0,step:'any',value:String(numberFor(r,t).number?.value??r.number.value),'aria-label':`Number for ${subject}`,required:true}) as HTMLInputElement:null;
  const numRow=num?el('label',{class:'rk-inline-row'},el('span',{class:'rk-lbl'},'Number'),num,el('span',{class:'muted'},r.number!.unit)):null;
  const from=el('input',{type:'date',value:r.kind==='block_quota'&&r.block?r.block[0]:from0,required:true}) as HTMLInputElement;
  const to=el('input',{type:'date',value:r.kind==='block_quota'&&r.block?r.block[1]:to0,required:true}) as HTMLInputElement;
  const dates=el('div',{class:'rk-dates'},el('label',{},el('span',{class:'rk-lbl'},'From'),dateInput(from)),el('label',{},el('span',{class:'rk-lbl'},'Until (included)'),(()=>{const box=dateInput(to);lastDayIncluded(to);return box;})()));
  const reason=el('input',{type:'text',required:true,minLength:3,maxLength:400,placeholder:'Why (kept with the request)','aria-label':'Reason'}) as HTMLInputElement;
  const submit=el('button',{type:'submit',class:'button primary small'},'Send for approval');
  const hint=el('small',{class:'muted'});
  const sync=()=>{const wv=isWaive();if(numRow)numRow.hidden=wv;if(num)num.required=!wv;
    hint.textContent=wv?`“${r.name}” won’t apply to them on these dates once an approver agrees.`:`A narrower rule that replaces “${r.name}” for ${subject}; it applies once an approver activates it.`;};
  form.append(title,...(mode?[mode]:[]),...(numRow?[numRow]:[]),dates,reason,el('span',{class:'rk-inline-row'},submit),hint);
  sync();
  form.addEventListener('submit',async e=>{e.preventDefault();submit.disabled=true;
    try{
      if(isWaive()){
        await command('exception.create',{policy_id:r.id,person_id:person,starts_on:from.value,ends_on:to.value,reason:reason.value});
        notify('Exception requested. It applies once an approver agrees.');
      }else{
        const def=structuredClone(policy.definition) as Row,scope=def.scope||{};
        const line=t.kind==='person'||t.kind==='assignment'?{field:'person_ids',op:'any',values:[person]}
          :t.kind==='service'?{field:'service_ids',op:'any',values:[t.service]}:t.kind==='location'?{field:'location_ids',op:'any',values:[t.location]}
          :t.kind==='type'||t.kind==='row'?{field:'assignment_type_ids',op:'any',values:[t.type]}
          :t.kind==='session'?(()=>{const s=dataNow!.shifts.find(x=>x.id===t.shift)!;return s.service_ids?.length?{field:'service_ids',op:'any',values:[s.service_ids[0]]}:{field:'assignment_type_ids',op:'any',values:[s.assignment_type_id]};})():null;
        if(line)scope.conditions={match:'all',conditions:[...(scope.conditions?[scope.conditions]:[]),line]};
        if(t.kind==='day')scope.weekdays=[(new Date(day+'T12:00:00Z').getUTCDay()+6)%7];
        def.scope=scope;def.overrides=[r.id];def.per_person=[];def.merged_ids=[];def.replaces_id=null;def.calendar_entry_id=null;
        if(num&&r.number)def.rule[r.number.field]=Number(num.value);
        // A narrower maximum keeps the rule's target only while it still fits under the new number.
        if(def.rule.target_amount!=null&&def.rule.comparison==='maximum'&&def.rule.target_amount>def.rule.amount){delete def.rule.target_amount;delete def.rule.target_strength;}
        await command('policy.create',{name:`${policy.name} · ${t.label}`.slice(0,160),owner:policy.owner,source:reason.value,starts_on:from.value,ends_on:to.value||null,definition:def});
        notify(`Exception proposed for ${t.label}. It applies once an approver activates it.`);
      }
      // Sent: nothing here is unsaved any more (a proposal leaves the rule map, so the panel may not redraw).
      form.dispatchEvent(new Event('rota-form-saved',{bubbles:true}));
      await h.refresh();
    }catch(err){submit.disabled=false;form.querySelector('.rk-error')?.remove();form.append(el('p',{class:'rk-error',role:'alert'},(err as Error).message));}
  });
  return form;
}

// ---- Mounting ------------------------------------------------------------------------------------------------------
/** Called on every planner render: `bodyWrap` hosts the pinned panel, `mainCol` holds the views to decorate. */
export function mountRules(ctx:Context,data:WorkspaceData,bodyWrap:HTMLElement,mainCol:HTMLElement,hooks:RuleHooks){
  // A cached Schedule redraw (including a breakpoint change) changes the host, not the rules. Move the panel with
  // its form controls and dirty guard intact; a fresh data object or another pinned target still redraws normally.
  const old=host?.querySelector<HTMLElement>(':scope>.rk-panel');
  // A change being typed (a dirty form, e.g. while the automatic rota check lands) keeps its panel as it is: the
  // panel redraws on the next refresh after it is saved, discarded or closed.
  const typing=!!panelGuard?.dirty()&&pinned?.workspace===data.workspace.id;
  const keep=!pending&&(ready()&&dataNow===data||typing)&&pinned&&old&&panelOf.get(old)===pinned?old:null;
  const focused=keep&&document.activeElement instanceof HTMLElement&&keep.contains(document.activeElement)?document.activeElement:null;
  ctxNow=ctx;dataNow=data;hooksNow=hooks;host=bodyWrap;main=mainCol;
  if(pinned?.ticked)applyTicked();
  if(lock&&lock.workspace!==data.workspace.id)lock=null;
  observer?.disconnect();
  let queued=0;
  observer=new MutationObserver(()=>{if(!queued)queued=requestAnimationFrame(()=>{queued=0;decorate();});});
  observer.observe(mainCol,{childList:true,subtree:true});
  for(const type of ['pointerover','focusin','click','keydown'])mainCol.addEventListener(type,wireAt,true);
  requestAnimationFrame(decorate);
  // A rota whose check changed (a check landing) shows its panel's rows only once its rule map is read again: an open
  // row menu waits for its row's new button (awaitAdoption) instead of closing when the page swaps in.
  const adopt=!keep&&pinned&&!ready()?awaitAdoption(RULE_MORE):()=>{};
  if(keep){host.append(keep);if(focused)requestAnimationFrame(()=>{if(keep.isConnected&&document.activeElement===document.body)focused.focus({preventScroll:true});});}
  else drawPanel();
  // The map is read when it is first needed (a highlight to show, a pinned panel, or the first hover or focus on the
  // schedule), not on every open: opening a view does not wait for it.
  // Closures here share one scope with load(), whose callback waits on the network: naming mainCol in any of them would
  // keep this render's view (and everything its listeners reach) alive until a slow rule-map answer arrives.
  const need=(e:Event)=>{const at=e.currentTarget as HTMLElement;at.removeEventListener('pointerover',need);at.removeEventListener('focusin',need);void load();};
  if(pendingPanel?.rota===data.workspace.id){const p=pendingPanel;pendingPanel=null;openRotaRules('rules',p.rule);}
  if(lock||pending||pinned||ready())void load();
  else{mainCol.addEventListener('pointerover',need);mainCol.addEventListener('focusin',need);}
  function load(){return loadRuleMap(data).then(()=>{if(dataNow!==data)return;
    if(pending){lock={workspace:data.workspace.id,rule:pending};const r=map!.rules.find(x=>x.id===pending);pending='';
      if(r){pin({kind:'rota',label:'Rules in this rota'});openRules=new Set([r.id]);}else{lock=null;notify('That rule does not apply to this rota.','info');}}
    decorate();refreshHoverCard();if(pinned&&(!keep||panelOf.get(keep)!==pinned))drawPanel();}).catch(e=>{if(pinned)drawPanel();console.warn('Rule map unavailable',e);}).finally(adopt);}
}
/** The "Why?" list for one person on one session, from the exact per-session check (shift_rules), with the same
 *  rows and change actions as the pinned panel. */
export function assignmentRules(result:Row,shift:Row,personId:string,redraw:()=>void,key?:Legend):HTMLElement{
  const mine=(result.people as Row[]).find(x=>x.person_id===personId),t:Target={kind:'assignment',label:'',person:personId,shift:shift.id};
  // Another person's or session's panel starts with its rows closed; a redraw of the same panel keeps them (D-057).
  const panelKey=`${shift.id}:${personId}`;if(openFor!==panelKey){openRules=new Set();openFor=panelKey;}
  const person=ctxNow?.catalogues.people.find(p=>p.id===personId);t.label=fullNameOf(person);
  const state:Record<string,string>={broken:'breach',warning:'warning',passes:'met',unchecked:'unchecked',replaced:'replaced'};
  // One row per rule: a rule matched by two of its lines (Supervision for a trainee and a supervisor) is said once, with
  // its worse state and both lines' messages (FD-06).
  const worse=['broken','warning','unchecked','passes','replaced'],once=new Map<string,Row>();
  for(const row of (mine?.rules||[]) as Row[]){const had=once.get(row.id);
    once.set(row.id,!had?row:{...(worse.indexOf(row.status)<worse.indexOf(had.status)?row:had),messages:[...(had.messages||[]),...(row.messages||[])]});}
  const items:Item[]=[...once.values()].map(row=>{
    const r=(ready()?map!.rules.find(x=>x.id===row.id):undefined)||fallbackRule(row);
    const st=state[row.status]||'unchecked',own=row.own_line||undefined;
    return {rule:r,why:r.named.people.includes(personId)||own?'named':'applies',number:row.number||ownNumber(r,own),own,
      replacedBy:(row.replaced_by||[]).map((x:Row)=>x.name),exception:row.exception||null,state:st,errors:st==='breach'?Math.max(1,row.messages?.length||0):0,warnings:st==='warning'?Math.max(1,row.messages?.length||0):0,messages:row.messages||[]} as Item;});
  stack(items);
  if(!items.length)return el('ul',{class:'rk-rules'},el('li',{class:'rk-none muted'},'No active rule applies to this person here.'));
  return el('ul',{class:'rk-rules','aria-label':'Rules applied to this assignment'},...items.map(i=>ruleRow(i,t,redraw,key)));
}
/** The rules that apply to one session, as the rules panel's rows (the session panel's Rules tab): problems first. */
export function sessionRules(t:Target,redraw:()=>void,key?:Legend):HTMLElement{
  if(!ready()){if(dataNow)void loadRuleMap(dataNow).then(redraw).catch(()=>{});return working('Working out which rules apply…');}
  const items=itemsAt(t).filter(i=>i.state!=='replaced').sort((a,b)=>Number(!['breach','warning'].includes(a.state))-Number(!['breach','warning'].includes(b.state)));
  if(!items.length)return el('p',{class:'muted rk-none'},'No rule applies to this session.');
  return el('ul',{class:'rk-rules wk-rk','aria-label':'Rules for this session'},...items.map(i=>ruleRow(i,t,redraw,key)));
}
/** A row the map does not have (e.g. still loading): shown, without change actions. */
function fallbackRule(row:Row):MapRule{
  return {id:String(row.id),name:String(row.name),kind:row.kind,severity:row.severity,safety:!!row.safety,exception_allowed:!!row.exception_allowed,adjustable:!!row.adjustable,number:row.number||null,department_value:null,adjusted:null,off:false,workspace_only:false,
    overrides:[],overridden_by:[],named:{people:[],services:[],locations:[],types:[],weekdays:null,dated:false},sessions:[],people:{},replaced:{},days:[],lines:[],status:{state:'unchecked',errors:0,warnings:0}};
}
/** The planner's rules pill: the non-modal list of every rule in the rota. */
export function openRotaRules(tab:RotaTab='rules',rule=''){pin({kind:'rota',label:'Rules in this rota'},tab,rule);}
/** A rota's Rules tab (at a rule when given) from anywhere: now when the planner shows that rota, else once it does.
 *  True when it opened now. */
let pendingPanel:{rota:string;rule:string}|null=null;
/** A link to the Rules panel already counts as a request for the whole rota. */
export const hasPendingRulePanel=(rotaId:string)=>!!pending||pendingPanel?.rota===rotaId;
export function openRotaRulesFor(rotaId:string,rule=''){
  if(dataNow?.workspace.id===rotaId&&!dataNow.partial&&host?.isConnected){openRotaRules('rules',rule);return true;}
  pendingPanel={rota:rotaId,rule};return false;}
// Esc clears a locked highlight, then the selection, when nothing else wants it.
if(typeof window!=='undefined')window.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&!e.defaultPrevented&&!(lock||preview)&&selectionOf().length&&!document.querySelector('dialog[open],.popover,.hovercard')
    &&!(e.target as Element)?.closest?.('.rk-panel,input,textarea,select')){pin({kind:'rota',label:'Rules in this rota'});return;}  // nothing selected: the rota's rules
  if(e.key!=='Escape'||e.defaultPrevented||!(lock||preview))return;
  if(document.querySelector('dialog[open],.popover,.hovercard')||(e.target as Element)?.closest?.('.rk-panel,input,textarea,select'))return;
  lock=null;preview='';applyHighlight();
});
