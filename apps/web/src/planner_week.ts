import {byValue,dutyTime,personTags,priorityTag,type Dim,DIMENSIONS} from './schedule_dimensions.js';
import type {LiveHost} from './autoschedule_live.js';
import {onlyRotaCoordinator} from './terms.js';
import {working,savingMark,failedMark,skeleton,elapsed,observeSize,say as sayPolitely} from './loading.js';
import {fillOptions,assignFingerprint,askRegistry} from './candidates.js';
import {dateMarks,dateMarkWords} from './date_sets.js';
import {sidePanel,claimSlot,panelHead} from './side_panel.js';
import {select,plural,phoneQuery,typeColour,rankColour,legend,MARKS,markTag,reasoned,pairWords,autoCode,type MarkKey,el,icon,hoverCard,hoverWithin,keepHover,type HoverContent,popover,menuItem,type IconName,colourOf,formatDate,formatTime,formatDay,toLocal,labelOf,button,withIcon,note,notify,empty,emptyLine,panelHighlight,tabs,WEEKEND_DAYS,reasonForm,focusQuietly,reportError,type Legend,hostLegend,avatar} from './dom.js';
import {get,request,command,onSessionReset,ApiError,serverChecking} from './api.js';
import type {Context} from './forms.js';
import {isWeekend,addDays,isClosed,closureText,rangeWords,absentFrom} from './planner_logic.js';
import type {PlannerSession,Row,WorkspaceData} from './types.js';
import {setSessionOpener,tag,ruleSummary,pin,assignmentRules,sessionRules,dayTitle,addsToSelection,selectSession,sessionSelected,type Target as RuleTarget} from './planner_rules.js';
import {supervisedBy,teamOrder,teamGroups,teamText,shownPeople,leadContext,leadState,leadNeed,type TeamEntry,type LeadState} from './planner_teams.js';
import {teamView,teamProblem,teamLabel,teamIssue,rankOn,BOX_CODES,statesShortfall,TEAM_LAYOUT_DEFAULT,type TeamProblem,type TeamLayout} from './team_view.js';
import {personPill} from './person_pill.js';
import {issueMarker,issueGlyph,issueLabel,findingIssue,issueList,SEVERITY_WORDS,LEVEL_WORDS,type Issue as MarkIssue} from './issue_marker.js';
import {mustWords} from './fix_list.js';
import {routeHash} from './routes.js';
import type {LeadContextInput,StaffingSupervisionPlan} from './generated.js';
import {whyTab,whyOne,effectWords,signed,type WhyState,type WhyRules} from './planner_why.js';
import type {ToolbarSpec,SortKey} from './table_view.js';
import {columnGrip,gripHeadingName} from './table_menu.js';
import {namesHidden,personLink,fullNameOf} from './person_label.js';
import {PLANNER_TEAM_ROWS_DEFAULT,type ViewProperty} from './generated.js';
import {propertiesOf,sessionCode,itemParts,itemNodes,itemWords,ITEM_DRAWS,type ItemSurface,type PropKey} from './schedule_item.js';
import {leaveText,issueKey,findingNumbers,FINDING_WORDS} from './findings.js';
import {clashMark,clashNote,clashTag,clashesOf} from './double_booking.js';
import {scheduleEmpty,SCHED_VIEWS} from './schedule.js';
import {wishWords} from './wishes.js';
import {menu as openMenu,menuSource,alive} from './dom.js';  // the shared menu (UX › controls S2b)
import {failedNote} from './dom.js';  // a failed check: the one note (DL-045)
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {overflowMore,type MoreWords} from './dom.js';  // the one "+N" (DESIGN D8)
import {groupToggle} from './dom.js';  // a group's header is its toggle (UX › controls S5)
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)
const nameOf=(c:Row)=>String(c.display_name||'');
const say=(f:Row)=>f.leave?leaveText(f.leave):FINDING_WORDS[f.code]||f.message;

/** What the week grid needs from the planner page. */
export interface WeekHooks {
  /** The draft as held now (app.ts heldDraft): a name's click and menu act on it, never on what the grid was drawn from. */
  live?:()=>WorkspaceData;
  /** SW3-A-10: fixed assignments here are the Rota Coordinator's only (the rota read's `can_change_fixed` false, the
   *  server's planning_groups.guard_changes). Unset: anyone who may edit changes them. */
  fixedHeld?:boolean;
  /** SW3-A-10: this reader cannot add, remove, close, lock or redefine sessions here (forms.ts sessionsHeld): the session
   *  panel says so before anything is pressed (terms.ts onlyRotaCoordinator) and its Lock is held in place. */
  sessionsHeld?:boolean;
  name:(person:Row|undefined)=>string;
  code:(person:Row|undefined)=>string;
  details:(shift:Row)=>void;
  dnd:(node:HTMLElement,participant:Row,who:string)=>HTMLElement;
  proposal:{additions:Row[];removals:Row[]}|null;
  editable:boolean;
  fresh:boolean;
  selected:Set<string>;
  /** The sessions before the toolbar's filters (Person, Clinic…): an empty view says "Hidden by filters" when these
   *  have something in its period, not "No sessions" (scheduleEmpty, S-01). Unset: the data shown is unfiltered. */
  unfiltered?:Row[];
  /** The whole authorised rota before any display filter, My duties included (the hover card's hours and rest, SI-03). */
  rota?:Row[];
  /** A rota check is running now: "Checking…" instead of "Not checked yet" (a check that failed or never ran is not). */
  checking?:boolean;
  /** The check behind the latest edit ended without a result (failed, not started, cancelled) or nobody runs it: a
   *  panel never says it is checking then. Read when a panel draws (one redrawn from its own save's reply draws first). */
  checkEnded?:()=>boolean;
  /** An edit's reply arrived: Submit, Approve and Publish are held at once, before the page redraws. */
  edited?:()=>void;
  /** D59: only the window of a rota is in (its whole detail still loading): its marks are the last check's, so nothing
   *  says a person passes every rule. */
  kept?:boolean;
  /** The session panel's footer (S-09): Edit session (primary) and the ⋯ entries (close, cover, staffing) this person
   *  may use on this session; none for a reader. */
  sessionActions?:(shift:Row)=>{edit?:()=>void;more:MenuEntry[]}|undefined;
  onSelect:()=>void;
  refresh:()=>Promise<void>;
  /** Opens auto-schedule (offered by Resolve when many places are open). */
  autoSchedule?:(scope?:string)=>void;
  /** Resolve mode moved to an issue whose session is in another week: show that week. */
  onResolveMove?:(shift:Row)=>void;
  /** Called when a session or help panel opens, so other side panels can close. */
  onPanel?:(show:()=>void)=>void;
  /** The actual session after a panel opens, changes or closes, for an explicit detail-link address. */
  onPanelChange?:(shift:string|null)=>void;
  /** Open the fill panel for a session from another view (switches to the week that contains it). */
  fill?:(shift:Row)=>void;
  /** The schedule toolbar's first action, named when a period is empty: Add session (a draft) or New draft (planners, no draft). */
  create?:string;
  /** Read-only grids (published rotas): clicking opens this instead of the planner panels. */
  open?:(shift:Row)=>void;
  /** The empty state's next step: `create`'s action (Add session, New rota) and clearing the filters (scheduleEmpty). */
  onCreate?:()=>void;
  onShowAll?:()=>void;
  /** A staff sign-in with no linked staff profile: an empty view says why personal duties are missing (UNLINKED_STAFF). */
  unlinked?:boolean;
  /** Only the reader's own duties are shown (staff's published read, or My duties on): an empty view says so (maintenance). */
  mine?:boolean;
  /** Turns My duties off (readers who may see everyone's duties); the empty view's Show everyone. */
  onShowEveryone?:()=>void;
  /** The reader's next duty (label and where it opens): an empty view of your own duties offers it (L18). */
  nextDuty?:()=>Promise<{label:string;go:()=>void}|null>;
  /** Places filled / needed per day and group (planners). Staff's own duties are partial data: no tallies (S-19). */
  tallies?:boolean;
  /** The draft drawn from its window (its whole rota read on demand): no rota-wide hours or rest on a name's card. */
  windowOnly?:boolean;
  /** Schedule: a published session shown around (or under) the draft opens read-only with this. */
  view?:(shift:Row)=>void;
  /** Shortcuts for a row, day or person. Each opens the canonical form, so settings are never copied into the grid. */
  menu?:(target:MenuTarget)=>MenuEntry[];
  /** Lock or unlock a draft session in one click (Undo in the message; no reason or preview for a draft). */
  lock?:(shift:Row,on:boolean)=>Promise<void>;
  /** The List view's table: the planner's toolbar offers its Group and Sort. */
  onTable?:(parts:{group?:TableParts['group'];sort?:TableParts['sort'];view?:TableParts['view']})=>void;
  /** The Why tab's rule names, rule sheet and "Allow once…" (the exception form). */
  why?:WhyRules;
  /** Exchange this assignment with another (the Exchange dialog, first side filled): the chip menu's Exchange with… */
  exchange?:(assignment:string)=>void;
  /** FIXLIST-UX: a marker's Fix opens the Planner's Fix list panel, at the cause holding this session when there is one.
   *  Returns false when it has none for it (the session's own panel opens instead). */
  fixList?:(focus?:{shift?:string})=>boolean;
}
type TableParts=Omit<ToolbarSpec,'label'>;
export type MenuTarget={shift?:Row;day?:string;person_id?:string;shifts?:Row[]};
export type MenuEntry={label:string;icon:IconName;run:()=>void;hint?:string};
/** Row, day and person heads get a ⋯ button (shown on hover) and a right-click menu. */
export function attachMenu(node:HTMLElement,menu:WeekHooks['menu'],target:MenuTarget,label:string){
  if(!menu)return node;
  const b=el('button',{type:'button',class:'icon-button bare wk-more','aria-label':label,...popup('menu'),'data-tip':label},icon('dots'));
  b.addEventListener('click',e=>{e.stopPropagation();showMenu(b,menu(target),label);});
  node.addEventListener('contextmenu',e=>{e.preventDefault();showMenu(b,menu(target),label);});
  node.append(b);return node;
}
/** Opens the shortcuts for a target as a menu under the anchor. */
function showMenu(anchor:HTMLElement,entries:MenuEntry[],label:string,keep?:string){
  if(!entries.length)return;
  openMenu(anchor,label,entries,{placement:'below',keep});
}
type Period={days:string[];page:number;has_previous:boolean;has_next:boolean;unit?:string;label?:string;/** Monday 0 … Sunday 6 */weekStart?:number};
/** `open`: the session's open places, the rota summary's unit (packages/domain/rota_summary.py places_open: the
 *  greatest headcount deficit over its intervals). A gap with no open place is a missing named role (TL-02). */
/** `open`: the check's open places (places_open: headcount, skills, lead, supervision); `basic`: those of the headcount
 *  alone (required − available_eligible), what is left to say beside a team box that states its own shortfall. */
type Cover={required:number;scheduled:number;available:number;gap:boolean;open:number;basic:number};
// `q`: the candidate search, kept when a live refresh redraws the open panel.
/** The issue a fill panel was opened for (Resolve issues › Find someone): it stays in view while choosing. */
/** The issue a fill panel was opened for: its canonical key (issues.issue_key), words and the matched line's severity. */
export type FillIssue={key:string;title:string;detail?:string;rule_id?:string;severity?:string};
type Panel={workspace:string;kind:'session'|'fill'|'person'|'help';shift?:string;assignment?:string;replace?:string;flash?:string;more?:boolean;q?:string;issue?:FillIssue;
  /** The session panel's tab (Session, Why or Rules) and what the Why tab looks at. */
  tab?:'session'|'why'|'rules';why?:WhyState;
  /** The fill search's first focus is handled: the panel took it, or a linked replacement keeps it on the grid. */
  searched?:boolean;
  /** The revision this panel's own Assign saved: its result stays drawn while that revision is held, and the status slot
   *  says the whole-rota check behind it until that check settles. */
  flashRev?:number};

/** The groups a person belongs to in a range when people are grouped: their rank then (one), or each of their tags. */
export function personGroups(ctx:{catalogues:Record<string,Row[]>},pid:string,by:string,days:string[]):string[]{
  if(by==='tag'){const tags=((ctx.catalogues.people||[]).find(p=>p.id===pid)?.tags||[]) as string[];return tags.length?tags:['No tag'];}
  if(by!=='rank')return [''];
  const current=(r:Row)=>(!r.starts_on||r.starts_on<=days.at(-1)!)&&(!r.ends_on||r.ends_on>days[0]);
  const m=(ctx.catalogues.memberships||[]).find(r=>r.person_id===pid&&current(r));
  return [labelOf((ctx.catalogues.categories||[]).find(c=>c.id===m?.category_id))||'No rank'];
}
export const dutyColour=(shift:Row)=>typeColour(shift.assignment_type_id,shift.type_snapshot?.definition?.colour);
/** The Week grid's groups are the session dimensions (Group by and Then by); person dimensions belong to the grid layouts. */
export const SESSION_GROUPS:Dim[]=['service','type','location','time'];
export const isSessionGroup=(k:string):k is Dim=>(SESSION_GROUPS as string[]).includes(k);
/** Collapsed group heads: saved with the view (PlannerViewSettings.collapsed, SI-35). */
export const collapsed=new Set<string>();
/** Properties the Week grid shows beside each row (rowFacts), never on a person's chip. */
export const ROW_FACTS=['time','location','service','hours'] as const;
/** Whether supervision teams show as team boxes (the view's Supervisor property, for the kind of item drawn). */
export const showPairs=(surface:ItemSurface='card')=>propertiesOf(weekView.props,surface).some(p=>p.key==='supervisor'&&p.on);
/** View › Supervision teams: Tree | Box (the property's form; the contract's first form when the view names none). */
export const teamLayout=():TeamLayout=>{const f=propertiesOf(weekView.props).find(p=>p.key==='supervisor')?.form;return f==='tree'||f==='box'?f:TEAM_LAYOUT_DEFAULT;};
/** View › Rule marks: could-improve markers too (D78). Must-fix markers always show: an essential conflict stays visible (AGENTS §2). */
export const showAdvisory=(surface:ItemSurface='card')=>propertiesOf(weekView.props,surface).some(p=>p.key==='rule'&&p.on);
/** A session's open-place findings: their marker sits beside its "1 open" place, every other one at the cell's top right. */
const OPEN_PLACE_CODES=new Set(['COVERAGE_GAP','COVERAGE_TARGET','OPEN_EXTRA_COVER','DISTINCT_PERSON_GAP']);
export const weekView={props:null as ViewProperty[]|null,/** Trainee rows a team shows before "+N more" (PlannerViewSettings.team_rows). */teamRows:PLANNER_TEAM_ROWS_DEFAULT as number,/** Staffing numbers shown (PlannerViewSettings.staffing; false hides them). */staffing:true,onlyOpen:false,onlyIssues:false,compact:false,/** Drawn for print: every person in a month's cells and every row of the List (no "+N more", no pages). */forPrint:false,groupBy:'service' as Dim|'none',/** Then by: a second session dimension inside each group (the view's `subgroup`). */thenBy:'none' as Dim|'none',sortBy:'time' as 'time'|'type'|'place'|'hours'|'code',colourBy:'type' as 'type'|'service'|'role'|'person'|'tag',
  /** Colour by tag: the viewer's tag ids in priority order (PlannerViewSettings.colour_tags); the first one a person holds wins. */colourTags:[] as string[],
  /** Each grouping level's own order (PlannerViewSettings.group_sort): [dimension, 'desc'] turns that level round. */groupSort:[] as SortKey[],
  /** Resized parts of a Schedule grid (PlannerViewSettings.widths: row_heading, date, cell), px. */widths:{} as Record<string,number>,
  /** Told when a grip gives a new width (the page keeps it with the view); undefined back to automatic. */onWidth:null as ((key:string,w:number|undefined)=>void)|null,
  /** Search › Show (PlannerViewSettings.person_display) for a picked person: whom the renderers draw. Never applied to
   *  the data: a shift keeps every participant for panels, checks and commands. */personView:null as {people:string[];display:'person'|'team'|'duty'}|null,
  /** SW3-A-31: the people the Schedule search names, drawn first in their cells (order only). */matchPeople:[] as string[]};
/** The people a renderer draws on one session and the ones a person view leaves out (counted as "+N", which opens the
 *  session with everyone). Render-only: callers keep passing the whole shift to panels and commands. */
export function drawnPeople(s:Row):{shown:Row[];hidden:string[]}{
  const v=weekView.personView,first=new Set(weekView.matchPeople),given=(s.participants||[]) as Row[];
  // SW3-A-31: the people searched come first, so a cell's "+N more" never hides a match.
  const all=first.size?[...given.filter(p=>first.has(p.person_id)),...given.filter(p=>!first.has(p.person_id))]:given;
  const here=v&&v.display!=='duty'?v.people.filter(id=>all.some(p=>p.person_id===id)):[];
  // A session none of them is on matched another way, or Show › whole duty: it is drawn whole.
  if(!v||!here.length)return {shown:all,hidden:[]};
  const keep=new Set(here.flatMap(id=>shownPeople(all,id,v.display)));
  const shown=all.filter(p=>keep.has(p));
  return {shown,hidden:shown.length===all.length?[]:[...new Set(all.filter(p=>!shown.includes(p)).map(p=>p.person_id as string))]};
}
/** The group (and Then by) heads the Schedule drew last, for Collapse all in the toolbar's Group. */
export const shownGroups:string[]=[];
/** A grouping level's order: 1 A → Z (the default), −1 Z → A. */
export const groupWay=(dim:string)=>weekView.groupSort.some(([k,d])=>k===dim&&d==='desc')?-1:1;
const WIDTH_VARS:Record<string,string>={row_heading:'--sched-head',date:'--sched-date',cell:'--sched-cell'};
/** The view's widths on a Schedule grid (CSS variables its columns read) and the shared grip (table_menu.ts) on the row
 *  heading and on the first column heading: drag, ← →, double-click back to automatic; kept with the view. */
export function scheduleWidths(grid:HTMLElement,heads:{row?:HTMLElement|null;col?:HTMLElement|null;colKey:'date'|'cell'}){
  const put=(k:string,w:number|undefined)=>{if(w)grid.style.setProperty(WIDTH_VARS[k],w+'px');else grid.style.removeProperty(WIDTH_VARS[k]);};
  for(const k of Object.keys(WIDTH_VARS))put(k,weekView.widths[k]);
  if(weekView.forPrint||!weekView.onWidth)return;
  // A gripped heading is a grid item (roving): arrows reach it, then Tab its grip (maintenance).
  const grip=(k:string,label:string,at:HTMLElement)=>{gripHeadingName(at);if(!at.hasAttribute('tabindex'))at.tabIndex=-1;at.append(columnGrip({label,width:()=>weekView.widths[k],size:w=>put(k,w),
    keep:w=>{if(w)weekView.widths={...weekView.widths,[k]:w};else{const {[k]:_,...rest}=weekView.widths;weekView.widths=rest;}weekView.onWidth?.(k,w);}}));};
  if(heads.row)grip('row_heading','Row headings',heads.row);
  if(heads.col)grip(heads.colKey,heads.colKey==='date'?'Day columns':'Columns',heads.col);
}
// Busy cells show the first few people; '+N more' opens the rest (kept open while the page is open).
const CELL_LIMIT=8,expanded=new Set<string>();
/** A busy Week cell's "+N" (the live auto-schedule view repaints it with the same words). */
export const CELL_MORE:MoreWords={noun:['person','people'],action:'show all'};
/** SW3-A-31: a session one of the searched people works (drawn before the others of its cell). */
const searched=(s:Row)=>((s.participants||[]) as Row[]).some(p=>weekView.matchPeople.includes(p.person_id));
const cellKey=(r:{type:string;place:string;time:string},day:string)=>`${r.type}|${r.place}|${r.time}|${day}`;
let panel:Panel|null=null;
/** The panel owner of the planner view on screen: every view opens a session in the one session panel (S-09). */
let openCurrent:{open:(next:Omit<Panel,'workspace'>)=>void;root:HTMLElement}|null=null;
/** Opens the session panel beside the planner view shown (any layout); false when no planner view is shown (the
 *  published schedule opens the same content read-only in a docked panel instead). */
export function openSession(shift:Row):boolean{
  if(!openCurrent?.root.isConnected)return false;
  openCurrent.open({kind:'session',shift:shift.id});return true;
}
/** Opens one assignment (a person on a session) in the same panel: People and other views select a person this way. */
export function openAssignment(shift:Row,assignment:string):boolean{
  if(!openCurrent?.root.isConnected)return false;
  openCurrent.open({kind:'person',shift:shift.id,assignment});return true;
}
/** One session's facts, the Session tab of the session panel and the read-only published panel (S-09, S-22): status,
 *  the type's fields, a closure, a double booking, the people (one row each, facts as chips) and its checks inline. */
/** `name`: a person's full name (an opened session always says who in full); `code`: the label the grid shows, beside it
 *  when it differs (a coded view), so the code on the grid can be matched to the person. */
/** `person`: an action beside each person (Replace…, Mark absent… on a published session the coordinator may change). */
/** SW3-A-27: whether a short label only shortens the name ("L. Harris" for "Dr. Diego Harris"): each of its words
 *  of two letters or more is a word of the name. Then it says nothing more and is not repeated under the name. */
export const spells=(code:string,name:string)=>{const words=new Set(name.toLowerCase().split(/[^\p{L}\p{N}]+/u)),own=code.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(w=>w.length>1);
  return own.length>0&&own.every(w=>words.has(w));};
export function sessionSummary(ctx:Context,shift:Row,o:{validation?:Row|null;name:(id:string)=>string;code?:(id:string)=>string;refresh:()=>unknown;fresh?:boolean;kept?:boolean;person?:(p:Row)=>Node|null;
  /** false: the panel around it already says the double bookings. */clash?:boolean}){
  const box=el('div',{class:'wk-session'}),zone=ctx.me.organisation.timezone;
  const marks=[isClosed(shift)?statusChip('Closed'):'',shift.locked?statusChip('Locked'):''].filter(Boolean);
  if(marks.length)box.append(el('div',{class:'tag-row'},...marks));
  for(const f of shift.type_snapshot.definition.fields||[])if(shift.attributes?.[f.key]!==undefined&&shift.attributes[f.key]!=='')
    box.append(el('p',{class:'wk-fact'},el('span',{class:'muted'},f.label+' '),Array.isArray(shift.attributes[f.key])?shift.attributes[f.key].join(', '):String(shift.attributes[f.key])));
  if(shift.cancelled)box.append(note('Closed · '+(shift.cancellation_reason||'Reason not recorded on this older session.')));
  else if(shift.closed)box.append(note(closureText(shift,o.name)));
  const clash=o.clash===false?null:clashNote(shift,o.refresh);if(clash)box.append(clash);
  const people=shift.participants as Row[];
  const errs=((o.validation?.findings||[]) as Row[]).filter(f=>f.shift_id===shift.id&&f.severity==='error');
  // The panel's own key (the shared legend): its pills' duty hue and its team boxes, as drawn (every mark has one).
  const key=legend('Key');
  hostLegend(box,key);  // and the rule map's exception marks drawn in it
  box.append(el('h3',{class:'wk-h3'},'People'),people.length?teamList(ctx,shift,people,o.name,pid=>errs.filter(f=>f.person_id===pid),key,(p,boxed)=>{
    // In a team box the box says who supervises (badge, indent, rank, and a named supervisor who is not here).
    const facts=[boxed?'':p.participation==='trainee'?MARKS.tr[1]:p.participation==='supervisor'?MARKS.sup[1]:'',p.team_lead?MARKS.lead[1]:'',p.overtime?MARKS.ot[1]:''].filter(Boolean);
    const span=(a:string,b:string)=>formatTime(a,zone)+'–'+formatTime(b,zone);
    const partial=p.segments?.length?(p.segments as Row[]).map(x=>span(x.start,x.end)).join(', '):p.start&&p.end&&(p.start!==shift.start||p.end!==shift.end)?span(p.start,p.end):'';
    // The one person pill (D78), its name opening their card; the session's code, facts and times beside it.
    const pill=key.mark(personPill({base:personLink(p.person_id,o.name(p.person_id),ctx.navigate),label:o.name(p.person_id),hue:dutyColour(shift),key}),
      'c:t:'+shift.assignment_type_id,'duty-'+dutyColour(shift),shift.type_snapshot.name,0);
    return [el('div',{},pill,o.code&&!spells(o.code(p.person_id),o.name(p.person_id))?el('span',{class:'code-chip wk-code'},o.code(p.person_id)):'',facts.length?el('span',{class:'tag-row'},...facts.map(f=>statusChip(f))):'',
        partial?el('small',{class:'muted'},partial):''),...[o.person?.(p)].filter((n):n is Node=>!!n)];}):emptyLine('No people assigned'),key.root);
  // Its checks inline (not behind a disclosure): the findings of the rota's last check on this session.
  const found=((o.validation?.findings||[]) as Row[]).filter(f=>f.shift_id===shift.id);
  // While only the window is in (kept), the last check's findings show as such: nothing changed, the rest is loading.
  box.append(el('h3',{class:'wk-h3'},'Checks'),!o.validation?el('p',{class:'muted'},'Not checked yet'):o.kept?el('div',{},el('p',{class:'muted'},'From the last check'),
      found.length?issueList(found.map(f=>findingIssue(f,f.person_id?o.name(f.person_id):'')),{limit:12}):''):o.fresh===false?el('p',{class:'muted'},'The rota changed since the last check.'):
    found.length?issueList(found.map(f=>findingIssue(f,f.person_id?o.name(f.person_id):'')),{limit:12}):el('p',{class:'muted'},'Passes every check'));
  return box;
}
const statusChip=(text:string)=>el('span',{class:'tag'},text);
/** A session's people in team order (planner_teams.ts): each team in one box (team_view.ts), the rest one row each. */
function teamList(ctx:Context,shift:Row,people:Row[],name:(id:string)=>string,errors:(pid:string)=>Row[],key:Legend,item:(p:Row,boxed:boolean)=>(Node|string)[]):HTMLElement{
  const day=toLocal(shift.start,ctx.me.organisation.timezone).slice(0,10);
  return el('ul',{class:'wk-people'},...teamGroups(people).map(team=>{
    const problem=teamProblem(team,people,errors),boxed=team.length>1||!!problem;
    // The panel has room: the team's problem in words beside its marker.
    return boxed?el('li',{},teamView(team,{density:'card',key,nameOf:id=>name(id)||'Named supervisor',rankOf:id=>rankOn(ctx.catalogues,id,day),problem,
      layout:teamLayout(),words:true,member:e=>el('div',{class:'wk-person'},...item(e.p,true))})):el('li',{class:'wk-person'},...item(team[0].p,false));}));
}
const ruleCache=new Map<string,Promise<Row>>();
/** Candidate checks are shared by key, with one consumer token per surface. */
const asks=askRegistry((body,o)=>fillOptions(body,o));
onSessionReset(()=>{ruleCache.clear();asks.reset();});
/** A surface closed (the Fix list dock or session panel): release only its consumer. */
export const stopAsking=(surface:'fix'|'panel')=>asks.stop(surface);

let redrawCurrent:((opener?:string,panelOnly?:boolean)=>void)|null=null;
/** Whatever the open panel explains stays highlighted in every view (dom.ts panelHighlight): its session, or (Why › A
 *  person) that person's duties. Views mark what they draw with `data-session` and `data-person` (space-separated ids);
 *  a mark inside another (a Week chip in its session's cell) uses `data-shift`, so only the outer one is ringed. */
let scrolledFor='',current:HTMLElement[]=[];
function highlightFocus(root:HTMLElement,workspace:string){
  const on=!!panel&&panel.kind!=='help'&&panel.workspace===workspace;
  const person=on&&panel!.tab==='why'&&panel!.why?.mode==='person'?panel!.why.person||'':'';
  const nodes=!on?[]:[...root.querySelectorAll<HTMLElement>(person?`[data-person~="${CSS.escape(person)}"]`:`[data-session~="${CSS.escape(panel!.shift||'')}"]`)];
  panelHighlight.highlight(nodes);
  // Screen readers hear the ring too (SI-10): the names in what the panel explains are "current".
  for(const n of current)if(n.getAttribute('aria-current')==='true')n.removeAttribute('aria-current');
  current=nodes.flatMap(n=>n.matches('.pchip')?[n]:[...n.querySelectorAll<HTMLElement>('.pchip')]).filter(n=>!n.hasAttribute('aria-current'));
  for(const n of current)n.setAttribute('aria-current','true');
  // Brought into view once each time the panel's subject changes, not on every redraw.
  const key=on?[panel!.shift,panel!.tab,panel!.why?.mode,person].join('|'):'';
  if(key!==scrolledFor){scrolledFor=key;if(nodes[0])requestAnimationFrame(()=>nodes[0].isConnected&&nodes[0].scrollIntoView({block:'nearest',inline:'nearest'}));}
}
export function toggleHelp(workspace:string,onOpen?:()=>void){panel=panel?.kind==='help'?null:{workspace,kind:'help'};if(panel)onOpen?.();redrawCurrent?.(undefined,true);}
export const helpOpen=()=>panel?.kind==='help';
/** Open a session's fill panel; with `replace`, to pick someone in place of that assignment; with `issue`, for that issue;
 *  with `q`, its search already set (one named person). A linked replacement focuses its person chip instead. */
export function queueFill(workspace:string,shift:string,replace?:string,issue?:FillIssue,q?:string,focusSearch=true){
  panel={workspace,kind:'fill',shift,...(replace?{replace}:{}),...(issue?{issue}:{}),...(q?{q}:{}),searched:!focusSearch};}
export function closeWeekPanel(){stopAsking('panel');if(panel){panel=null;redrawCurrent?.(undefined,true);}}
/** Resolve mode: step through every open place and blocking rule problem, in date order, one panel at a time. */
export const resolve={active:false,workspace:'',current:'',skipped:new Set<string>(),
  /** Resolve these (the selection bar, SI-16): only these sessions' open places and problems; null for the whole rota. */
  only:null as Set<string>|null};
type Issue={key:string;kind:'fill'|'person';shift:Row;assignment?:string;label:string};
export function resolveQueue(data:WorkspaceData,only=resolve.active?resolve.only:null):Issue[]{
  const cover=coverOf(data),out:Issue[]=[],seen=new Set<string>();
  // Blocking person findings by session, once (not a scan of every finding per session: ~500 × 1,000 on every planner draw).
  const findings=findingIndex(((data.workspace.validation?.findings||[]) as unknown as Row[]).filter(f=>f.severity==='error'&&f.shift_id&&f.person_id));
  for(const s of [...data.shifts].sort((a,b)=>String(a.start).localeCompare(String(b.start)))){
    if(isClosed(s)||s.locked||only&&!only.has(s.id))continue;
    const c=cover.get(s.id),role=roleGap(data,s.id,c);
    if(c?.gap)out.push({key:'gap:'+s.id,kind:'fill',shift:s,label:role?ROLE_GAP_WORDS[role]:'Open place'});
    for(const f of findings.get(s.id)||[]){
      const p=s.participants.find(x=>x.person_id===f.person_id),key=`rule:${s.id}:${f.person_id}`;
      if(p&&!seen.has(key)){seen.add(key);out.push({key,kind:'person',shift:s,assignment:p.id,label:f.message});}
    }
  }
  return out.filter(i=>!resolve.skipped.has(i.key));
}
export function startResolve(workspace:string,only?:Iterable<string>){Object.assign(resolve,{active:true,workspace,current:'',only:only?new Set(only):null});resolve.skipped.clear();}

export function coverOf(data:WorkspaceData){
  const out=new Map<string,Cover>();
  for(const r of data.workspace.validation?.coverage||[]){
    if(r.status==='cancelled')continue;
    const c=out.get(r.shift_id)||{required:0,scheduled:Infinity,available:Infinity,gap:false,open:0,basic:0};
    // An older check may keep only a gap status: it still stands for one required place (as rota_summary reads it).
    const required=r.required??Number(r.status==='gap');
    // R-2 (BE-FIX-B, COVERAGE-STATUS.md): the check's own places (packages/domain/coverage.py) count a skill, rank or
    // supervisor shortfall as not filled; available_eligible is only the basic headcount, used for older checks.
    const needed=r.places_needed??required,filled=r.places_filled??r.available_eligible??0,open=r.places_open??required-(r.available_eligible??0);
    c.required=Math.max(c.required,needed);c.scheduled=Math.min(c.scheduled,r.scheduled??0);c.available=Math.min(c.available,filled);
    c.gap=c.gap||r.status==='gap'||open>0;c.open=Math.max(c.open,open);c.basic=Math.max(c.basic,Math.min(open,required-(r.available_eligible??0)));
    out.set(r.shift_id,c);
  }
  for(const c of out.values()){if(c.scheduled===Infinity)c.scheduled=0;if(c.available===Infinity)c.available=0;}
  // A staff view has the real checked coverage but only the viewer's own and paired participants.
  if(data.participantsFiltered)return out;
  // Someone absent now (leave or a rotation found after the check, a reported Can't make it) is struck through on the
  // grid, so they fill no place either: never more places filled than the people on the session who are not absent.
  // A check that already knew of the absence counted it, and the smaller of the two counts it once.
  for(const s of data.shifts||[]){const c=out.get(s.id);if(!c)continue;
    const away=absentFrom(s,[]),present=s.participants.filter(p=>!away.has(p.person_id)).length;
    if(present>=c.available)continue;
    c.available=present;const open=Math.max(0,c.required-present);
    if(open>c.open){c.open=open;c.gap=true;}c.basic=Math.max(c.basic,open);}
  return out;
}
/** Puts a person's new row in a group of a people grid (Timeline, Counts) in name order: the live auto-schedule view adds
 *  one when a run gives someone their first duty there. */
export function placeRow(grid:HTMLElement,group:string,row:HTMLElement,name:string){
  const g=CSS.escape(group),rows=[...grid.querySelectorAll<HTMLElement>(`.pg-row[data-row-person][data-group="${g}"]`)];
  const next=rows.find(r=>(r.querySelector('.pg-name strong')?.textContent||'').localeCompare(name)>0);
  if(next)next.before(row);
  else if(rows.length)rows.at(-1)!.after(row);
  else{let at=grid.querySelector(`.pg-group[data-group="${g}"]`);while(at?.nextElementSibling&&!at.nextElementSibling.classList.contains('pg-group'))at=at.nextElementSibling;
    if(at)at.after(row);else grid.append(row);}
  return row;
}
/** Open places: people still needed (never a missing team lead, skill or supervisor: roleGap). */
export const openPlaces=(c:Cover|undefined)=>c?c.open:0;
/** A problem is said once (owner): beside a team box that states a supervision or capacity shortfall, the open-place
 *  badge counts only the places the box does not explain (the headcount's); every other open place keeps its badge. */
export const openBeside=(c:Cover|undefined,boxStates:boolean)=>!c?0:boxStates?c.basic:c.open;
const leadCodes=new Set(['LEAD_GAP','LEAD_NOT_QUALIFIED']),gapCodes=new WeakMap<object,Map<string,Set<string>>>();
/** A session whose headcount is met but is still short (TL-02, rota summary other_gaps): 'lead' when its findings
 *  say a team lead is missing or not qualified, 'role' for a missing skill or supervisor, '' otherwise. */
export function roleGap(data:WorkspaceData,shiftId:string,c:Cover|undefined):''|'lead'|'role'{
  if(!c?.gap||c.open>0)return '';
  const v=data.workspace.validation;if(!v)return 'role';
  let m=gapCodes.get(v);if(!m){m=new Map();for(const f of (v.findings||[]) as unknown as Row[])if(f.shift_id){const k=m.get(f.shift_id)||new Set<string>();k.add(f.code);m.set(f.shift_id,k);}gapCodes.set(v,m);}
  return [...(m.get(shiftId)||[])].some(k=>leadCodes.has(k))?'lead':'role';
}
export const ROLE_GAP_WORDS={lead:'Needs a team lead',role:'Needs a skill or supervisor'} as const;
/** One key entry per meaning, whichever view draws it (dom.ts legend; live check 6cea25859 found People × Days'
 *  weekend and the phone agenda's open places drawn without theirs): an open place, a missing role, a weekend, today. */
export const markOpen=<E extends Element>(key:Legend,n:E,swatch:HTMLElement)=>key.mark(n,'open',swatch,'Open place: needs someone',3);
export const markRoleGap=<E extends Element>(key:Legend,n:E,role:'lead'|'role',swatch:HTMLElement)=>key.mark(n,'role-'+role,swatch,role==='lead'?'Team lead to choose':ROLE_GAP_WORDS[role],3);
export const markWeekend=<E extends Element>(key:Legend,n:E)=>key.mark(n,'weekend',el('span',{class:'lg-swatch lg-weekend','aria-hidden':'true'}),'Weekend',5);
export const markToday=<E extends Element>(key:Legend,n:E)=>key.mark(n,'today',el('span',{class:'lg-swatch lg-today','aria-hidden':'true'}),'Today',5);
/** A chip's wish mark: the shared outline star; one that goes against the wish is also struck through
 *  (shape and words, never colour alone). */
const wishMark=(kind:'grants'|'against')=>el('span',{class:'wk-wish-mark '+kind,'aria-hidden':'true'},icon('star'));
/** A double booking's key entry (Week's mark and the agenda's tag). */
const CLASH_SWATCH=()=>el('span',{class:'clash-tag','aria-hidden':'true'},icon('alert')),CLASH_WORDS='Double-booked: someone is on two sessions at once';
const line=(tone:'ok'|'err'|'warn'|'info'|'na',title:string,detail='')=>el('li',{class:'hc-line '+tone},el('span',{class:'hc-icon','aria-hidden':'true'},tone==='ok'?'✓':tone==='na'?'–':tone==='info'?'i':'!'),el('span',{},el('strong',{},title),detail?el('small',{},detail):null));
/** "Why here" in an assignment's hover card: the score with this person on the session (fair share, preferences) and
 * each wish it grants or goes against, from the server's Why (the same answer as the session panel's Why tab). Asked only
 * after the pointer rests on the name (SI-04): sweeping across the grid reads nothing; shown when it arrives, never a
 * loading line. */
const WHY_DWELL=700;
function whyHere(w:Row,s:Row,personId:string):HTMLElement{
  const list=el('ul',{class:'hc-lines','aria-label':'Why here'});list.hidden=true;
  window.setTimeout(()=>{if(!list.isConnected)return;
    whyOne(w,s.id,personId).then(r=>{
      const e=r?.effect as Row|undefined;
      if(!list.isConnected||!e)return;
      const parts=effectWords(e,false).join(' · ');
      list.replaceChildren(line(e.points<0?'warn':'ok',e.points?`Score ${signed(e.points)} with them here`:'No score difference with them here',parts),
        ...((e.wishes||[]) as Row[]).map(x=>line(x.granted?'ok':'warn',x.granted?'Grants their wish':'Goes against their wish',String(x.text))));
      list.hidden=false;
    }).catch(()=>{});},WHY_DWELL);
  return list;
}
const iconBtn=(label:string,glyph:'chevronLeft'|'chevronRight',run:()=>void,disabled:boolean)=>{const b=el('button',{type:'button',class:'icon-button','aria-label':label,'data-tip':label},icon(glyph)) as HTMLButtonElement;b.disabled=disabled;b.addEventListener('click',run);return b;};
const hcHead=(badge:string,title:string,sub:string,cls='')=>el('div',{class:'hc-head'},el('span',{class:'hc-badge '+cls},badge),el('div',{},el('strong',{},title),el('small',{},sub)));

/** Previous / period / next. The planner moves it into its toolbar beside the view picker; elsewhere it sits above the grid.
 *  The weekday names are their own spans: a narrow container drops them ("27 Sept – 10 Oct"), so the period fits whatever the font. */
export function periodNav(label:string,unit='week',period:Period,onPage:(page:number)=>void,tip=''){
  const parts=label.split(/\b((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) )/);  // odd indices: "Sun "
  return el('div',{class:'wk-nav'},iconBtn(`Previous ${unit}`,'chevronLeft',()=>onPage(period.page-1),!period.has_previous),
    el('strong',tip?{'data-tip':tip}:{},...parts.map((s,i)=>i%2?el('span',{class:'wk-wd'},s):s)),iconBtn(`Next ${unit}`,'chevronRight',()=>onPage(period.page+1),!period.has_next));
}
const shortDay=(day:string)=>formatDay(day,'UTC');
/** Three-letter month ("Sep", never en-GB's "Sept"), as everywhere else. */
const monthOf=(day:string)=>shortDay(day).split(' ')[2];
/** ISO week number of a day (the week holding its Thursday). */
const isoWeek=(day:string)=>{const t=new Date(day+'T12:00:00Z');t.setUTCDate(t.getUTCDate()+3-(t.getUTCDay()+6)%7);
  const jan4=new Date(Date.UTC(t.getUTCFullYear(),0,4));return 1+Math.round(((+t-+jan4)/864e5-3+(jan4.getUTCDay()+6)%7)/7);};
/** A month row's label (S-07): "Wk 41 · 4–10 Oct", or "Wk 40 · 27 Sep – 3 Oct" across two months. */
export const weekRowLabel=(week:string[])=>{const a=week[0],b=week[week.length-1];
  return `Wk ${isoWeek(week[Math.min(3,week.length-1)])} · `+(monthOf(a)===monthOf(b)?`${Number(a.slice(8))}–${Number(b.slice(8))} ${monthOf(b)}`:`${Number(a.slice(8))} ${monthOf(a)} – ${Number(b.slice(8))} ${monthOf(b)}`);};
/** The period a view shows, in words: "October 2026", or "Sun 4 Oct – Sat 10 Oct". */
export function periodTitle(period:Period,_zone?:string){
  if(!period.days.length)return 'No days';
  if(period.unit==='month'&&period.label)return new Intl.DateTimeFormat('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(period.label.slice(0,7)+'-15T12:00:00Z'));
  return period.days.length===1?shortDay(period.days[0]):`${shortDay(period.days[0])} – ${shortDay(period.days.at(-1)!)}`;
}
/** List or grid for a week: phones default to the day list, larger screens to the grid. The viewer's pick holds for this visit,
 *  shared by the published rota and the planner. */
let weekLayout:''|'grid'|'agenda'='';
export const weekLayoutNow=()=>weekLayout||(phoneQuery.matches?'agenda':'grid');
export function weekLayoutSwitch(redraw:()=>void){
  const layout=weekLayoutNow();
  // Icons only (named for screen readers and in the tooltip), so the switch fits beside the period on a phone.
  return segmented('Calendar layout',[['agenda','Agenda',{icon:'menu',iconOnly:true}],['grid','Grid',{icon:'grid',iconOnly:true}]],layout,
    key=>{weekLayout=key;redraw();},{cls:'pv-range',activation:'manual'});
}
/** Phone view of the same sessions and period as weekGrid: one list per day under a sticky day header,
 *  the reader's own duties marked. Selecting a session opens the same details as the grid.
 *  In the planner (`planner` given) it also shows open places, honours the grid's filters and offers adding a session per day. */
/** One session as an item of a list: the phone agenda, a Coverage cell's sessions and a People × Days chip's session
 *  reviewed. What it says is the view's
 *  Properties through schedule_item.ts (time, code and duty, place, service, hours, people); team boxes, open places,
 *  issue marks and the double-booked tag beside them; each colour and mark entered in `key`. Selecting it opens the
 *  session (the one session panel where a planner view has one). `data`: the draft or published rota, with its check. */
export function sessionItems(ctx:Context,hooks:Pick<WeekHooks,'name'|'details'|'open'>,o:{data?:WorkspaceData;days:readonly string[];key:Legend;onlyMine?:boolean}){
  const zone=ctx.me.organisation.timezone,me=ctx.me.person_id,key=o.key,onlyMine=!!o.onlyMine;
  const cover=o.data?coverOf(o.data):new Map<string,Cover>();
  const broken=new Set((o.data?.workspace.validation?.findings||[]).filter((f:Row)=>f.severity==='error'&&f.shift_id).map((f:Row)=>f.shift_id));
  // A session's rule problems on its item (D78): the issue marker's icon and count, its words read out with the item.
  // Findings by session, once (not a scan of every finding per item and per day).
  const ofShift=new Map<string,Row[]>();
  for(const f of (o.data?.workspace.validation?.findings||[]) as Row[])if(f.shift_id){const l=ofShift.get(f.shift_id);if(l)l.push(f);else ofShift.set(f.shift_id,[f]);}
  const agIssue=(s:Row)=>{const xs=(ofShift.get(s.id)||[]).filter(f=>f.severity==='error').map(f=>findingIssue(f));
    return el('span',{class:'ag-issue'},key.mark(issueGlyph('error',xs.length),'issue:error',issueGlyph('error'),SEVERITY_WORDS.error),el('span',{class:'sr-only'},issueLabel(xs)));};
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p]));
  // Each item says what the view's properties say (the one item renderer): its time, its code and duty in bold, then
  // place, service, hours and people in the view's order; Colour by is the grids' rule.
  const props=propertiesOf(weekView.props,'agenda'),on=(k:PropKey)=>props.some(p=>p.key===k&&p.on),colours=colourRule(ctx,o.days,key);
  const words=new Set([...ITEM_DRAWS.agenda].filter(k=>!['label','supervisor','rule','lead','people'].includes(k))),HEAD=new Set<PropKey>(['code','duty']);
  return (s:Row,day:string):HTMLElement=>{
      const own=s.participants.some((p:Row)=>p.person_id===me);
      const nameOf=(id:string)=>id===me?'You':hooks.name(people.get(id))||'named supervisor';
      // My duties: your own name goes (the scope says it), but a trainee keeps who supervises them ("(with …)").
      // Each supervisor once with their trainees (planner_teams.ts) and the shared TL mark.
      const tl=(p:Row)=>on('lead')&&p.team_lead?' '+MARKS.lead[0]:'';
      // Teams (and a trainee with a pairing problem) are drawn as team boxes; everyone else stays in the line of names.
      const errs=(pid:string)=>(ofShift.get(s.id)||[]).filter(f=>f.person_id===pid&&f.severity==='error');
      const teams=on('people')&&showPairs('agenda')?teamGroups(s.participants as Row[]).map(t=>({t,problem:teamProblem(t,s.participants as Row[],errs)})).filter(x=>x.t.length>1||x.problem):[];
      const inBox=new Set(teams.flatMap(x=>x.t.map(e=>e.p))),rest=(s.participants as Row[]).filter(p=>!inBox.has(p));
      // Said once: a box that states a supervision or capacity shortfall keeps it; the badge says only the rest.
      const open=isClosed(s)?0:openBeside(cover.get(s.id),teams.some(x=>statesShortfall(x.problem)));
      const rank=(id:string)=>rankOn(ctx.catalogues,id,day),fullName=(id:string)=>id===me?'You':people.has(id)?fullNameOf(people.get(id)):'Named supervisor';
      const short=(id:string)=>id===me?'You':people.has(id)?hooks.name(people.get(id)):'Named supervisor';
      // The session's rule problems all said by a team box: its "Needs a skill or supervisor" badge would repeat them.
      const sessionErrs=(ofShift.get(s.id)||[]).filter(f=>f.severity==='error');
      const told=teams.some(x=>x.problem)&&sessionErrs.length>0&&sessionErrs.every(f=>BOX_CODES.has(String(f.code)));
      const boxes=teams.length?el('span',{class:'ag-teams'},...teams.map(x=>teamView(x.t,{density:'card',presentational:true,nameOf:short,fullName,rankOf:rank,problem:x.problem,layout:teamLayout(),hue:dutyColour(s),key})),
        // Coded names: the item's own full line already says everyone in words.
        namesHidden()?null:el('span',{class:'sr-only'},teams.map(x=>teamLabel(x.t,fullName,rank,x.problem)).join('; '))):null;
      const who=!on('people')?[]:onlyMine?rest.map((p:Row)=>{const sup=showPairs('agenda')&&p.supervisor_person_id;
          return p.person_id===me?(sup?'(with '+nameOf(sup)+')':''):nameOf(p.person_id)+(sup?' (with '+nameOf(sup)+')':'');}).filter(Boolean)
        :showPairs('agenda')?[teamText(rest,nameOf,tl,s.participants as Row[])].filter(Boolean):s.participants.map((p:Row)=>nameOf(p.person_id)+tl(p));
      // The TL in the line of names: that line is its mark (the names are words, TL the shared tag).
      const led=!onlyMine&&on('lead')&&on('people')&&(showPairs('agenda')?rest:s.participants as Row[]).some(p=>p.team_lead);
      // Open places, rule issues and closures are written on the item; the key explains only colours and marks.
      const parts=itemParts(ctx,s,'',zone,props,{only:words}),time=parts.find(x=>x.key==='time');
      const head=parts.filter(x=>HEAD.has(x.key)),line=itemNodes(parts.filter(x=>x!==time&&!HEAD.has(x.key)),key,()=>'ag-part');
      const at=props.findIndex(p=>p.key==='people'),whoAt=parts.filter(x=>x!==time&&!HEAD.has(x.key)&&props.findIndex(p=>p.key===x.key)<at).length;
      // Coded names never get a key of people (legends explain symbols only): their full names are in the item's
      // tooltip and accessible name, and the item opens the session with everyone in full.
      const all=s.participants.length?teamText(s.participants as Row[],id=>id===me?'You':fullNameOf(people.get(id))):'';
      const full=namesHidden()?all:'',tip=full||(who.join(', ').length>80?all:'');
      const named=who.length?who.join(', '):onlyMine||boxes||!on('people')?'':'No one yet';
      if(named)line.splice(whoAt,0,(n=>led?key.mark(n,'tag:lead',markTag('lead'),MARKS.lead[1]):n)(el('span',{class:'ag-part','data-part':'people'},named)));
      const b=el('button',{type:'button',class:`ag-item${own?' mine':''}${isClosed(s)?' cancelled':''}`,'data-session':s.id,'data-person':s.participants.map((p:Row)=>p.person_id).join(' '),...(own?{'aria-current':'true'}:{}),...(tip?{'data-tip':tip}:{})},
        time?el('span',{class:'ag-time mono','data-part':'time'},time.text):null,
        // What the session is, for a screen reader, when the view hides its time or duty name.
        !time||!head.some(x=>x.key==='duty')?el('span',{class:'sr-only'},`${s.type_snapshot.name}, ${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`):null,
        el('span',{class:'ag-what'},el('strong',{},...itemNodes(head,key,()=>'ag-part').flatMap((n,i)=>i?[' · ',n]:[n]),isClosed(s)?' · closed':''),line.length?el('small',{class:'clamp-2',...(full?{'aria-hidden':'true'}:{})},...line.flatMap((n,i)=>i?[' · ',n]:[n])):null,boxes,
          full?el('span',{class:'sr-only'},[...itemWords(parts.filter(x=>x!==time&&!HEAD.has(x.key))),...(on('people')?[full]:[])].join(' · ')):null,
          own&&s.leave_pending?el('small',{class:'ag-leave'},'Leave approved · cover being arranged'):null,(n=>n&&key.mark(n,'clash',CLASH_SWATCH(),CLASH_WORDS))(clashTag(s))),
        open?markOpen(key,el('span',{class:'ag-open'},`${open} open`),el('span',{class:'ag-open lg-mark','aria-hidden':'true'},'1 open'))
          :o.data&&!isClosed(s)&&!told&&roleGap(o.data,s.id,cover.get(s.id))?(role=>markRoleGap(key,el('span',{class:'ag-open'},ROLE_GAP_WORDS[role]),role,el('span',{class:'ag-open lg-mark','aria-hidden':'true'},'!')))(roleGap(o.data,s.id,cover.get(s.id)) as 'lead'|'role'):broken.has(s.id)&&!told?agIssue(s):null);
      // Open places, rule issues and closures are written on the item; the key explains its colour (Colour by) and marks.
      b.classList.add('duty-'+colours.seen(s,b));
      if(own&&!onlyMine)key.mark(b,'mine',el('span',{class:'lg-swatch lg-mine','aria-hidden':'true'}),'Your duty');
      // The grids' selection rule: Ctrl, ⌘ or Shift (or Select mode) adds it to the selection, with the grids' ring; a
      // plain click opens the session. Selecting stops here, so a list that closes on open (Coverage's popover) stays.
      if(o.data&&sessionSelected(s.id))b.classList.add('rk-selected');
      b.addEventListener('click',e=>{if(o.data&&addsToSelection(e)){e.stopImmediatePropagation();selectSession(s);b.classList.toggle('rk-selected',sessionSelected(s.id));return;}(hooks.open||hooks.details)(s);});
      if(o.data)tag(b,{kind:'session',shift:s.id,label:s.type_snapshot.name},{own:true});
    return b;};
}
export function dayAgenda(ctx:Context,visible:Row[],period:Period,onPage:(page:number)=>void,hooks:WeekHooks,planner:{data:WorkspaceData;add?:(day:string)=>void}):HTMLElement;
export function dayAgenda(ctx:Context,visible:Row[],period:Period,onPage:(page:number)=>void,hooks:Pick<WeekHooks,'name'|'details'|'open'>):HTMLElement;
export function dayAgenda(ctx:Context,visible:Row[],period:Period,onPage:(page:number)=>void,hooks:Pick<WeekHooks,'name'|'details'|'open'>,planner?:{data:WorkspaceData;add?:(day:string)=>void}){
  const zone=ctx.me.organisation.timezone,me=ctx.me.person_id;
  const cover=planner?coverOf(planner.data):new Map<string,Cover>();
  const broken=new Set((planner?.data.workspace.validation?.findings||[]).filter((f:Row)=>f.severity==='error'&&f.shift_id).map((f:Row)=>f.shift_id));
  const findings=(planner?.data.workspace.validation?.findings||[]) as Row[];
  if(planner)visible=visible.filter(s=>(!weekView.onlyOpen||!!cover.get(s.id)?.gap)&&(!weekView.onlyIssues||broken.has(s.id)));
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p])),locations=new Map((ctx.catalogues.locations||[]).map(l=>[l.id,l]));
  const dayOf=(s:Row)=>toLocal(s.start,zone).slice(0,10),key=legend('Key'),today=toLocal(new Date().toISOString(),zone).slice(0,10);
  // Staff's own schedule is only their duties: "Your duty" and "You" on every item would say nothing (S-21).
  const onlyMine=!!me&&(!planner||(hooks as WeekHooks).tallies===false);
  const item=sessionItems(ctx,hooks,{data:planner?.data,days:period.days,key,onlyMine});
  const inDay=byDayOf(visible,dayOf),shiftDay=new Map(visible.map(s=>[s.id,dayOf(s)]));
  // Each day's findings, in the findings' order: one pass, not every finding per day.
  const findingsOn=byDayOf(findings.filter(f=>shiftDay.has(f.shift_id)),f=>shiftDay.get(f.shift_id)!);
  const dayNodes=period.days.map(day=>{
    const items=[...inDay.get(day)||[]].sort((a,b)=>String(a.start).localeCompare(String(b.start)));
    const mine=items.filter(s=>!isClosed(s)&&s.participants.some((p:Row)=>p.person_id===me)).length;
    // The day's issues rolled up on its heading (D78), as the Week grid's day header does.
    const here=new Map(items.map(x=>[x.id,x]));
    const dayIssues=(findingsOn.get(day)||[]).flatMap(f=>{const s=here.get(f.shift_id);
      return s&&(f.severity==='error'||showAdvisory('agenda')&&f.severity==='warning')?[findingIssue(f,[`${s.type_snapshot.name} ${formatTime(s.start,zone)}`,f.person_id?fullNameOf(people.get(f.person_id)):''].filter(Boolean).join(' · '))]:[];});
    return el('section',{class:'ag-day','aria-label':formatDate(day,zone)},
      (planner?(h:HTMLElement)=>tag(h,{kind:'day',label:dayTitle(day),day,days:[day],shifts:items.map(s=>s.id)},{pin:true}):(h:HTMLElement)=>h)(el('h3',{class:'ag-head',...(day===today?{'aria-current':'date'}:{})},el('span',{},formatDay(day,'UTC')),day===today?el('span',{class:'ag-today'},'Today'):null,
        dateMarks(planner?.data.date_set_markers?.[day]),issueMarker(dayIssues,{title:formatDate(day,zone),key}),
        mine&&!onlyMine?el('span',{class:'ag-mine-count'},mine===1?'Your duty':`${mine} of your duties`):el('span',{class:'muted'},items.length?String(items.length):''),
        planner?.add?(()=>{const b=el('button',{type:'button',class:'icon-button ag-add','aria-label':'Add session on '+formatDate(day,zone),'data-tip':'Add session'},icon('plus'));b.addEventListener('click',()=>planner.add!(day));return b;})():null)),
      items.length?el('ul',{class:'ag-list'},...items.map(s=>el('li',{},item(s,day)))):emptyLine('No sessions',null,'ag-none'));
  });
  // A month on a phone (and a week without Add session, for someone who does not plan it): a run of days without sessions (one or more) is one line ("4 days without sessions"); choosing
  // it shows those days (each with its Add session).
  const days:HTMLElement[]=[];
  for(let i=0;i<dayNodes.length;){
    let j=i;while((period.unit==='month'||!planner?.add)&&j<dayNodes.length&&dayNodes[j].querySelector(':scope>.ag-none'))j++;
    if(j===i){days.push(dayNodes[i]);i++;continue;}
    // The whole period empty: the one empty state (its title says the period; the navigator already shows the dates).
    if(i===0&&j===dayNodes.length){const h=hooks as WeekHooks;days.push(scheduleEmpty(period.unit||'week',{filtered:(h.unfiltered||[]).some(s=>period.days.includes(dayOf(s))),hooks:h,create:!!planner?.add,published:!planner||planner.data.workspace.status==='published'}));break;}
    const run=dayNodes.slice(i,j),f=(d:string)=>formatDay(d,'UTC');
    const open=el('button',{type:'button',class:'ag-gap'},el('span',{},run.length>1?`${run.length} days without sessions`:'No sessions'),el('small',{class:'muted'},run.length>1?`${f(period.days[i])} – ${f(period.days[j-1])}`:f(period.days[i])));
    open.addEventListener('click',()=>{open.replaceWith(...run);run[0].tabIndex=-1;run[0].focus();});
    days.push(open);i=j;
  }
  const nav=periodNav(periodTitle(period,zone),period.unit||'week',period,onPage);
  if(!planner)return swipe(hostLegend(el('div',{class:'ag'},nav,...days,key.root),key),period,onPage);
  const layout=el('div',{class:'wk-layout'},swipe(hostLegend(el('div',{class:'ag wk-content'},...days,key.root),key),period,onPage));
  return plannerPanels(ctx,planner.data,period,hooks as WeekHooks,nav,layout,()=>dayAgenda(ctx,visible,period,onPage,hooks as WeekHooks,planner)).root;
}
/** The swipe under way, held here and not by the node it began on: a live redraw that replaces the calendar mid-gesture
 *  (its later reads landing) must not lose it; the pointerup on the new node ends it with the new draw's period. */
let swipeFrom:{id:number;x:number;y:number}|null=null;
/** A phone calendar pages by swiping (SI-42): a mostly sideways swipe of 60 px or more is the previous or next period;
 *  the arrows stay. A mouse drag is not a swipe. */
export function swipe(node:HTMLElement,period:Pick<Period,'page'|'has_previous'|'has_next'>,onPage:(page:number)=>void){
  node.addEventListener('pointerdown',e=>{swipeFrom=e.pointerType==='mouse'?null:{id:e.pointerId,x:e.clientX,y:e.clientY};});
  node.addEventListener('pointerup',e=>{const from=swipeFrom;if(!from||from.id!==e.pointerId)return;swipeFrom=null;
    const dx=e.clientX-from.x,dy=e.clientY-from.y;
    if(Math.abs(dx)<60||Math.abs(dx)<Math.abs(dy)*2)return;
    if(dx<0&&period.has_next)onPage(period.page+1);else if(dx>0&&period.has_previous)onPage(period.page-1);});
  node.addEventListener('pointercancel',()=>{swipeFrom=null;});
  return node;
}
/** What changed since the grid was last drawn (per rota), so new names pop in and edited sessions pulse once. */
let drawn:{workspace:string;shifts:Map<string,string>}|null=null;
/** The grid is redrawn on every change: a control is found again by its class and name. */
const focusKey=(n:HTMLElement)=>n.getAttribute('aria-label')||n.className+'|'+(n.textContent||'').trim();
let panelOpener='';
/** One panel, cache and Resolve lifecycle for the planner's calendar and collection renderers. */
export function plannerPanels(ctx:Context,data:WorkspaceData,period:Period,hooks:WeekHooks,nav:HTMLElement,layout:HTMLElement,build?:()=>HTMLElement){
  const root=el('div',{class:'wk'},nav,layout),w=data.workspace,zone=ctx.me.organisation.timezone,days=period.days;
  const dayOf=(s:Row)=>toLocal(s.start,zone).slice(0,10);
  // The panels show the draft as held now (WeekHooks.live: an edit's delta patches it), not the copy the grid was drawn
  // from: a panel opened from, or left open beside, a cell kept on screen says and saves what is true now.
  const held=()=>hooks.live?.()||data;
  const dayLabel=(s:Row)=>formatDay(dayOf(s),'UTC');
  const placeName=(s:Row)=>{const place=labelOf(ctx.catalogues.locations.find(l=>l.id===s.location_id)),service=(s.service_ids as string[]).map(id=>labelOf(ctx.catalogues.services.find(r=>r.id===id))).sort().join(' + ')||'No service';return place.endsWith(' · '+service)?place.slice(0,-(service.length+3)):place;};
  // A panel opening, closing or changing leaves the grid as it is (it does not depend on the panel; the ring is
  // panelHighlight): only the panels are drawn again (perf PL-02). Anything else draws the view again.
  const redraw=(opener?:string,panelOnly=false)=>{if(!root.isConnected)return;
    const had=document.activeElement instanceof HTMLElement&&root.contains(document.activeElement)?document.activeElement:null;
    const active=document.activeElement instanceof HTMLElement?document.activeElement:null;
    if(opener)panelOpener=opener;else if(panel&&active&&active!==document.body&&!active.closest('.wk-panel'))panelOpener=focusKey(active);
    const back=keepHover(),next=build&&!panelOnly?build():root;
    // One period navigator (SSOT): the Schedule moves it into its toolbar, so a redrawn view drops its own copy.
    if(next!==root){const fresh=next.querySelector('.wk-nav');if(fresh&&!root.querySelector('.wk-nav'))fresh.remove();root.replaceWith(next);requestAnimationFrame(back);}
    else drawPanels();
    // Keyboard users keep their place: focus moves into a panel that opened, back to what opened it when it closes,
    // or else to the same control in the new grid.
    // A panel drawn beside the kept grid takes the focus from the control that opened it.
    const into=panelOnly&&!!panel&&!active?.closest('.wk-panel')?.isConnected;
    if((had&&!had.isConnected)||(panel&&active&&!next.contains(active))||into){
      const same=(key:string)=>[...next.querySelectorAll<HTMLElement>('button,[tabindex]')].find(n=>focusKey(n)===key);
      // ↑ ↓ in the panel head keep the focus on the arrow pressed; otherwise Close.
      const head=panel?next.querySelector<HTMLElement>('.wk-panel-head'):null,pressed=active?.closest('.wk-panel-head')?focusKey(active):'';
      const to=head?[...head.querySelectorAll<HTMLElement>('button')].find(n=>pressed&&focusKey(n)===pressed&&!(n as HTMLButtonElement).disabled)||head.querySelector<HTMLElement>('button[aria-label^="Close"]'):panel?null:same(panelOpener)||[...document.querySelectorAll<HTMLElement>('button')].find(n=>focusKey(n)===panelOpener)||(had?same(focusKey(had)):null);
      if(!panel)panelOpener='';
      if(to){if(panel)to.focus({preventScroll:true});else focusQuietly(to);}
    }};
  // Published renderers keep their read-only opener and never consume planner panel state.
  if(hooks.open)return {root,redraw,openPanel,rulesFor};
  openCurrent={open:openPanel,root};setSessionOpener(openSession);
  if(panel&&panel.workspace!==w.id){panel=null;panelOpener='';}
  function rulesFor(s:Row){
    const key=`${w.id}:${held().workspace.revision}:${s.id}`;
    if(!ruleCache.has(key))ruleCache.set(key,get<Row>(`/api/workspaces/${w.id}/shifts/${s.id}/rules`).catch(e=>{ruleCache.delete(key);throw e;}));
    return ruleCache.get(key)!;
  }
  function openPanel(next:Omit<Panel,'workspace'>){claimSlot();const active=document.activeElement,from=active instanceof HTMLElement&&active!==document.body&&!active.closest('.wk-panel')?focusKey(active):'';
    const show=()=>{panel={workspace:w.id,...next};redrawCurrent?.(from,true);};if(hooks.onPanel)hooks.onPanel(show);else show();}
  function closePanel(){stopAsking('panel');panel=null;resolve.active=false;redraw(undefined,true);}
  function drawPanels(){
    layout.querySelectorAll(':scope>.wk-panel').forEach(n=>n.remove());
    // Resolve mode: the next issue opens by itself; the person can still move around inside its panel.
    const now=held(),cover=coverOf(now),queue=resolve.active&&resolve.workspace===w.id?resolveQueue(now):[];
    if(resolve.active&&resolve.workspace===w.id){
      const cur=queue[0];
      if(!cur){resolve.active=false;panel=null;notify(resolve.skipped.size?`Resolve finished · ${resolve.skipped.size} skipped.`:'Everything is resolved: no open places, and nothing must be fixed.',resolve.skipped.size?'info':'success');}
      else if(cur.key!==resolve.current){resolve.current=cur.key;panel={workspace:w.id,kind:cur.kind,shift:cur.shift.id,assignment:cur.assignment,flash:panel?.flash};
        // Its session is in another week: the grid follows, so the person sees it in context.
        if(!days.includes(dayOf(cur.shift)))setTimeout(()=>hooks.onResolveMove?.(cur.shift),0);}
    }

    // A published session around the draft (context) is not in the draft: it is the one drawn.
    const shift=panel?.shift?now.shifts.find(s=>s.id===panel!.shift)||data.shifts.find(s=>s.context&&s.id===panel!.shift):undefined;
    if(panel?.kind==='help')layout.append(helpPanel(closePanel));
    else if(panel&&shift){
      // ↑ ↓ step through the sessions the view shows (its filters), each as held now; one gone since is skipped.
      const byId=new Map(now.shifts.map(s=>[s.id,s]));
      const range=data.shifts.filter(s=>!s.context&&days.includes(dayOf(s))).map(s=>now===data?s:byId.get(s.id)).filter(s=>s!==undefined)
        .sort((a,b)=>String(a.start).localeCompare(String(b.start))||String(a.id).localeCompare(String(b.id)));
      const aside=sessionPanel(ctx,now,shift,panel,hooks,{time:`${formatTime(shift.start,zone)}–${formatTime(shift.end,zone)}`,day:dayLabel(shift),place:placeName(shift),cover:cover.get(shift.id),rulesFor,close:closePanel,open:openPanel,range,highlight:()=>highlightFocus(root,w.id)});
      if(resolve.active&&queue.length){
        const cur=queue[0];
        const skip=button('Skip',()=>{resolve.skipped.add(cur.key);redraw();},'ghost small'),stop=button('Stop',()=>{resolve.active=false;panel=null;redraw();},'ghost small');
        const open=queue.filter(i=>i.kind==='fill').length;
        aside.querySelector('.wk-panel-head')?.after(el('div',{class:'resolve-bar',role:'status'},el('span',{class:'resolve-count'},`${queue.length} to resolve`),el('span',{class:'resolve-what truncate'},cur.kind==='fill'?'Open place: pick someone below':'Rule problem: '+cur.label),skip,stop),
          // Many open places: one auto-schedule run fills them faster than going one by one. It runs on the open
          // sessions only, so rule problems elsewhere are reported with the result instead of stopping the run.
          ...(open>20&&hooks.autoSchedule?[el('div',{class:'resolve-tip'},
            el('span',{},`${open} open places: auto-schedule can fill them in one go, then Resolve what is left.`),
            button('Auto-schedule',()=>{resolve.active=false;panel=null;hooks.autoSchedule!('open');},'primary small'))]:[]));
      }
      layout.append(aside);
    }
    // maintenance: the session's panel closed before its save's check landed (the person moved on): its result is no toast,
    // which would arrive late and out of context. The footer's rota health already shows the counts; it is said politely.
    else if(panel){if(panel.flash)sayPolitely(panel.flash);panel=null;}
    hooks.onPanelChange?.(panel?.workspace===w.id?panel.shift||null:null);
    // After the view has drawn (the grid and agenda add their content once this returns).
    queueMicrotask(()=>highlightFocus(root,w.id));
    document.querySelector<HTMLButtonElement>('.planner-bar button[aria-label="How to read this schedule"]')?.setAttribute('aria-pressed',String(helpOpen()));
  }
  drawPanels();
  redrawCurrent=redraw;
  return {root,redraw,openPanel,rulesFor};
}
/** Sessions by their local day, in the list's order: built once per draw, so a day's header or cell reads its bucket
 *  instead of scanning every session again (a Month asked ~35 days × every session, per header and per cell). */
function byDayOf(list:readonly Row[],dayOf:(s:Row)=>string):Map<string,Row[]>{
  const out=new Map<string,Row[]>();for(const s of list){const d=dayOf(s),l=out.get(d);if(l)l.push(s);else out.set(d,[s]);}return out;
}
/** Findings by session and by session + person (maintenance: indexed once per draw, not one scan of every finding per chip). */
function findingIndex(findings:readonly Row[]):Map<string,Row[]>{
  const byKey=new Map<string,Row[]>(),put=(k:string,f:Row)=>{const l=byKey.get(k);if(l)l.push(f);else byKey.set(k,[f]);};
  for(const f of findings)if(f.shift_id){const sev=f.severity==='error'?'e':'w';put(f.shift_id,f);put(sev+'|'+f.shift_id,f);put(sev+'|'+f.shift_id+'|'+(f.person_id||''),f);}
  return byKey;
}
/** The held draft's indexes, made once per draft copy (an edit's delta makes a new one). */
const heldIndexes=new WeakMap<WorkspaceData,{cover:Map<string,Cover>;byKey:Map<string,Row[]>}>();
/** Colour by (PlannerViewSettings.colour_by), one rule for every Schedule grid (Week/Month cards, People × Days and the
 *  codes/counts grid): a session's hue, a person's chip hue, and each colour added to the key as it is drawn, so nothing
 *  on screen is coloured without the key explaining it reviewed. */
export function colourRule(ctx:Context,days:readonly string[],key:Legend){
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p])),services=new Map((ctx.catalogues.services||[]).map(s=>[s.id,s]));
  const current=(r:Row)=>(!r.starts_on||r.starts_on<=days.at(-1)!)&&(!r.ends_on||r.ends_on>days[0]);
  // Each person's first current membership and the ranks by id, once per draw (not a scan of every membership per chip).
  const memberOf=new Map<string,Row>(),rankById=new Map((ctx.catalogues.categories||[]).map(c=>[c.id,c]));
  for(const r of ctx.catalogues.memberships||[])if(!memberOf.has(r.person_id)&&current(r))memberOf.set(r.person_id,r);
  const roleOf=(pid:string)=>{const m=memberOf.get(pid);return m?rankById.get(m.category_id):undefined;};
  const hue=(s:Row)=>weekView.colourBy==='service'?colourOf(services.get(s.service_ids[0]),s.service_ids[0]):dutyColour(s);
  // Person tags (records and member links), indexed once per draw: chip marks and Colour by tag read it.
  const heldTags=personTags(ctx.catalogues),tagOf=(pid:string)=>priorityTag(heldTags.get(pid),weekView.colourTags);
  // Chips follow the person or their rank (its one tint, C21) when the view asks for it; otherwise the session's colour.
  const chipHue=(s:Row,pid:string)=>weekView.colourBy==='person'?colourOf(people.get(pid),pid):weekView.colourBy==='role'?rankColour(roleOf(pid)?.id||'')
    :weekView.colourBy==='tag'?(()=>{const t=tagOf(pid);return t?colourOf(t):'slate';})():hue(s);
  // `node`: the element drawn in that hue, stamped as its colour's key entry (dom.ts legend `mark`; every mark has one).
  const enter=(node:Element,k:string,swatch:string,words:string)=>key.mark(node,k,swatch,words,0);
  const seen=(s:Row,node:Element)=>{const c=hue(s);if(weekView.colourBy==='service'){const id=s.service_ids[0];enter(node,'c:s:'+id,'duty-'+c,labelOf(services.get(id))||'No service');}else enter(node,'c:t:'+s.assignment_type_id,'duty-'+c,s.type_snapshot.name);return c;};
  const seenChip=(s:Row,pid:string,node:Element)=>{
    if(!pid)return seen(s,node);
    if(weekView.colourBy==='person'){enter(node,'c:p','lg-people','Colour: one per person');return chipHue(s,pid);}
    if(weekView.colourBy==='tag'){const t=tagOf(pid);enter(node,'c:g:'+(t?.id||''),'duty-'+chipHue(s,pid),t?'#'+t.name:'No chosen tag');return chipHue(s,pid);}
    if(weekView.colourBy!=='role')return seen(s,node);
    const r=roleOf(pid),c=chipHue(s,pid);enter(node,'c:r:'+(r?.id||''),'duty-'+c,r?labelOf(r):'No rank');return c;};
  return {hue,seen,seenChip,heldTags};
}
export function weekGrid(ctx:Context,data:WorkspaceData,visible:Row[],period:Period,onPage:(page:number)=>void,hooks:WeekHooks){
  const zone=ctx.me.organisation.timezone,w=data.workspace,checked=!!w.validation;
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p]));
  const services=new Map((ctx.catalogues.services||[]).map(s=>[s.id,s]));
  const locations=new Map((ctx.catalogues.locations||[]).map(l=>[l.id,l]));
  let cover=coverOf(data);const props=propertiesOf(weekView.props,period.unit==='month'?'pill':'card'),prop=(k:PropKey)=>props.find(p=>p.key===k)!;
  const sig=(s:Row)=>[isClosed(s)?'x':'',...s.participants.map((p:Row)=>p.id)].join(',');
  const prev=drawn?.workspace===w.id?drawn.shifts:null,changed=new Set(prev?data.shifts.filter(s=>prev.get(s.id)!==sig(s)).map(s=>s.id):[]);
  drawn={workspace:w.id,shifts:new Map(data.shifts.map(s=>[s.id,sig(s)]))};
  const findings:Row[]=(w.validation?.findings||[]) as unknown as Row[];
  // Findings by session and by session + person, indexed once per draw (maintenance: not one scan of every finding per chip).
  let byKey=findingIndex(findings);
  const errorsFor=(shiftId:string,personId?:string)=>byKey.get('e|'+shiftId+(personId===undefined?'':'|'+personId))||[];
  const warningsFor=(shiftId:string,personId:string)=>byKey.get('w|'+shiftId+'|'+personId)||[];
  // Day and group numbers count the sessions shown: with a filter on they say so (SI-34).
  const dayOf=(s:Row)=>toLocal(s.start,zone).slice(0,10);
  // Compared within the period: `unfiltered` holds the whole loaded window, `visible` the days drawn.
  const inPeriod=(list:Row[])=>list.filter(s=>period.days.includes(dayOf(s))).length;
  const filtered=weekView.onlyOpen||weekView.onlyIssues||!!hooks.unfiltered&&inPeriod(hooks.unfiltered)!==inPeriod(visible);
  const time=(s:Row)=>dutyTime(s,zone);
  const weekday=new Intl.DateTimeFormat('en-GB',{weekday:'short',timeZone:'UTC'});
  const month=new Intl.DateTimeFormat('en-GB',{month:'short',timeZone:'UTC'});
  const dayName=(day:string)=>weekday.format(new Date(day+'T12:00:00Z'));
  const dayLabel=(day:string)=>`${dayName(day)} ${Number(day.slice(8))} ${month.format(new Date(day+'T12:00:00Z'))}`;
  // A clinic row's or a day's issues rolled up (D78): one marker; its popover lists ISSUE_LIST_LIMIT of them, then "Show
  // all M in Fix list" (a draft's Fix list; a published or read-only grid has none, so it says how many more).
  const fixList=hooks.editable&&!hooks.open&&w.status!=='published'?()=>{if(!hooks.fixList?.())ctx.navigate(routeHash('schedule',['issues'],{draft:w.id}).slice(1));}:undefined;
  function rollUp(shifts:readonly Row[],title:string,where:(s:Row)=>string):HTMLElement|null{
    const xs:MarkIssue[]=[];
    for(const s of shifts)for(const f of byKey.get(s.id)||[])if(f.severity==='error'||showAdvisory()&&f.severity==='warning')
      xs.push(findingIssue(f,[where(s),f.person_id?fullNameOf(people.get(f.person_id)):''].filter(Boolean).join(' · ')));
    xs.sort((a,b)=>Number(b.severity==='error')-Number(a.severity==='error'));
    return issueMarker(xs,{title,act:fixList,all:fixList,key});
  }
  const unit=period.unit||'week',days=period.days,inWeek=visible.filter(s=>days.includes(dayOf(s))&&(!weekView.onlyOpen||!!cover.get(s.id)?.gap)&&(!weekView.onlyIssues||errorsFor(s.id).length>0));
  const inDay=byDayOf(inWeek,dayOf),onDay=(day:string)=>inDay.get(day)||[];
  const hoursOf=(s:Row)=>{const h=(Date.parse(s.end)-Date.parse(s.start))/36e5;return `${Number.isInteger(h)?h:h.toFixed(1)} h`;};
  const serviceName=(s:Row)=>(s.service_ids as string[]).map(id=>labelOf(services.get(id))).sort().join(' + ')||'No service';
  // A place named after its group ("Diabetes Clinic Room 1" under Diabetes Clinic) reads only what the head does not say.
  const placeName=(s:Row,group:string)=>{const n=labelOf(locations.get(s.location_id));
    if(n.endsWith(' · '+group))return n.slice(0,-(group.length+3));
    const rest=group&&n.length>group.length+1&&n.toLowerCase().startsWith(group.toLowerCase()+' ')?n.slice(group.length+1).replace(/^[·\-–—:\s]+/,''):'';
    return rest||n;};
  // A session as each of its items is named (QA-127a): duty, place, date and time. Moving along a row reads no row head.
  const sessionWords=(s:Row)=>`${s.type_snapshot.name}, ${labelOf(locations.get(s.location_id))}, ${formatDate(s.start,zone)} ${time(s)}`;
  const need=(s:Row)=>cover.get(s.id)?.required||0;
  const filled=(s:Row)=>{const c=cover.get(s.id);return c?Math.min(c.required,c.available):0;};
  // The key lists what this grid draws: each colour as it is painted, each tag, mark and code as it is placed.
  // `key` is where marks go now: the grid's key, or (drawnMarks) a holder that enters only what is drawn.
  const grid_key=legend('Key');let sink:Legend=grid_key;
  const key:Legend={...grid_key,mark:(n,k,sw,t,o)=>sink.mark(n,k,sw,t,o),tag:k=>sink.tag(k)};
  /** Builds with the key held: only marks inside what `drawn` keeps enter it (a Month cell's first chips, a team's
   *  first trainees), so the key never lists a mark that was built and left out. */
  function drawnMarks<T>(build:()=>T,drawn:(t:T)=>readonly Element[]):T{
    const was=sink,held:[Element,string,string|HTMLElement,string,number|undefined][]=[];
    const hold=<E extends Element>(n:E,k:string,sw:string|HTMLElement,t:string,o?:number):E=>{held.push([n,k,sw,t,o]);return n;};
    sink={...was,mark:hold,tag:k=>hold(markTag(k),'tag:'+k,markTag(k),MARKS[k][1])};
    let t:T;try{t=build();}finally{sink=was;}
    const kept=drawn(t);for(const [n,...a] of held)if(kept.some(x=>x.contains(n)))was.mark(n,...a);
    return t;
  }
  const {hue,seen,seenChip,heldTags}=colourRule(ctx,days,key);
  /** An open place, drawn here or by the live view: one entry. */
  const openKey=<E extends Element>(n:E)=>markOpen(key,n,el('span',{class:'pchip gap lg-mark','aria-hidden':'true'},prop('open').form==='count'?'+1':'+ 1 open place'));
  // A group head takes a hue only when its axis is what the view colours by (then the key names it); otherwise neutral,
  // so no colour appears that the key does not explain (DESIGN: hues are for duty types; S-34).
  const coloursGroups=()=>weekView.groupBy==='service'&&weekView.colourBy==='service'||weekView.groupBy==='type'&&weekView.colourBy==='type';
  const groupHue=(name:string,list:Row[])=>{const s=list[0];if(!s||!coloursGroups())return 'slate';return weekView.groupBy==='service'?colourOf(services.get(s.service_ids[0]),s.service_ids[0]):dutyColour(s);};
  // A coloured head is its colour's entry (the same as its sessions'); a neutral one is only its words, no hue to explain.
  const headHue=(n:HTMLElement,list:Row[])=>{if(list[0]&&coloursGroups())seen(list[0],n);else n.setAttribute('data-lg-self','');return n;};
  // A session's value on a session dimension; Duty time is the session's own clock in the department's zone.
  const valueOf=(by:Dim|'none',s:Row)=>by==='type'?s.type_snapshot.name:by==='location'?labelOf(locations.get(s.location_id)):by==='time'?time(s):by==='none'?'All sessions':serviceName(s);
  const groupOf=(s:Row)=>valueOf(weekView.groupBy,s);
  const thenBy=weekView.groupBy!=='none'&&weekView.thenBy!==weekView.groupBy&&isSessionGroup(weekView.thenBy)?weekView.thenBy:null;
  /** A group or Then by head: its words and the session dimension it names (a rule pin when that dimension has rules). */
  const headTag=(by:Dim|'none',name:string,all:Row[]):Parameters<typeof tag>[1]|null=>{const s0=all[0];return !s0?null
    :by==='service'&&s0.service_ids.length===1?{kind:'service',service:s0.service_ids[0],label:name,sub:'Clinic',shifts:all.map(x=>x.id)}
    :by==='type'?{kind:'type',type:s0.assignment_type_id,label:name,sub:'Duty type',shifts:all.map(x=>x.id)}:by==='location'?{kind:'location',location:s0.location_id,label:name,sub:'Place',shifts:all.map(x=>x.id)}:null;};
  const tally=(list:Row[])=>{const live=list.filter(s=>!isClosed(s));return {need:live.reduce((a,s)=>a+need(s),0),have:live.reduce((a,s)=>a+filled(s),0),sessions:live.length,open:live.reduce((a,s)=>a+openPlaces(cover.get(s.id)),0)};};

  const withMenu=(node:HTMLElement,target:MenuTarget,label:string)=>attachMenu(node,hooks.menu,target,label);
  const menus=!hooks.open&&!!(hooks.menu||hooks.editable);
  /** A session by id in the draft as held now (hooks.live: the copy an edit's delta patches); a grid without one (a
   *  published or read-only grid) looks in what it drew. Undefined: gone since the draw (the redraw on its way). */
  const sessionNow=(id:string|undefined)=>{const now=hooks.live?.(),find=(list:Row[])=>list.find(x=>x.id===id);return now?find(now.shifts):find(visible)||find(data.shifts);};
  /** Runs `f` (synchronous) on the draft as held now: the indexes the cell builders read (coverage, findings, the rota
   *  behind a person's hours) are swapped in for it, so a card, a popover or a menu built at event time says what is
   *  true now, not at the draw. */
  function asNow<T>(f:()=>T):T{
    const d=hooks.live?.();if(!d||d===data)return f();
    let held=heldIndexes.get(d);if(!held){held={cover:coverOf(d),byKey:findingIndex((d.workspace.validation?.findings||[]) as Row[])};heldIndexes.set(d,held);}
    const was={cover,byKey,rota,facts,cur};cover=held.cover;byKey=held.byKey;rota=d.shifts;facts=new Map();cur=d;
    try{return f();}finally{({cover,byKey,rota,facts,cur}=was);}
  }
  let cur=data;
  /** Every control inside the grid's cells, for the whole grid (one listener per event, not one per control): each
   *  finds its session (and assignment) by id when used, in the draft as held now, so nothing on the grid ever acts on
   *  a revision, roster or check the grid was drawn from. A control whose session is gone does nothing. */
  function cellEvent(e:Event){
    const t=e.target instanceof Element?e.target:null;if(!t)return;
    // A modifier-click (or a tap in Select mode) on anything of a session selects it instead of opening it; a session's
    // bulk checkbox stays a bulk action (the selection bar).
    if(e.type==='click'&&!hooks.open&&addsToSelection(e as MouseEvent)&&!t.closest('input.wk-check')){
      const s=sessionNow(t.closest<HTMLElement>('[data-shift]')?.dataset.shift);if(s){e.preventDefault();e.stopPropagation();selectSession(s);return;}}
    if(e.type==='change'){const box=t.closest<HTMLInputElement>('input.wk-check[data-shift]');if(box){box.checked?hooks.selected.add(box.dataset.shift!):hooks.selected.delete(box.dataset.shift!);hooks.onSelect();}return;}
    const chip=t.closest<HTMLElement>('button.pchip[data-assignment]');
    if(chip){
      const menu=e.type==='contextmenu'||!!t.closest('.pchip-more');
      if(menu&&!menus)return;  // read-only: the browser's own menu, as before
      if(e.type==='contextmenu')e.preventDefault();
      const s=sessionNow(chip.dataset.shift),p=s?.participants.find((x:Row)=>x.id===chip.dataset.assignment);if(!s||!p)return;
      if(!menu){if(hooks.open)hooks.open(s);else openPanel({kind:'person',shift:s.id,assignment:p.id});return;}
      // D-057: the menu stays on this person's place in the session across a redraw (the assignment's and session's ids).
      showMenu(chip,chipMenu(chip),'Actions for '+fullNameOf(people.get(p.person_id)),
        `button.pchip[data-assignment="${CSS.escape(p.id)}"][data-shift="${CSS.escape(s.id)}"]`);return;}
    if(e.type!=='click')return;
    // A cell's "+N": its hidden people show (kept for this cell while the page is open).
    const more=t.closest<HTMLElement>('button.pchip.more[data-cell]');
    if(more){expanded.add(more.dataset.cell!);more.closest('.wk-cell')?.querySelectorAll<HTMLElement>(':scope>:is(.pchip,.team-box)').forEach(c=>c.hidden=false);more.remove();return;}
    // A Month day's "+N": that lane's sessions on the day, drawn as they are now.
    const month=t.closest<HTMLElement>('button.pchip.more[data-sessions]');
    if(month){const list=month.dataset.sessions!.split(' ').map(sessionNow).filter((x):x is Row=>!!x);
      popover(month,panel=>panel.append(el('div',{class:'popover-head'},el('strong',{},month.dataset.group||''),el('small',{},`${month.dataset.day||''} · ${plural(list.length,'session')}`)),
        // The popover's chips repeat the grid's marks and close with it: they are not entered in the key.
        cellEvents(el('div',{class:'mo-more'},...asNow(()=>drawnMarks(()=>chipsOf(list),()=>[]))))),{label:month.dataset.label||'More',placement:'below'});return;}
    const lock=t.closest<HTMLElement>('button.wk-lock[data-shift]');
    if(lock){const s=sessionNow(lock.dataset.shift);if(s&&hooks.lock)void hooks.lock(s,false);return;}
    const open=t.closest<HTMLElement>('button[data-open][data-shift]');
    if(open){const kind=open.dataset.open,s=sessionNow(open.dataset.shift)||(kind==='view'?visible.find(x=>x.id===open.dataset.shift):undefined);if(!s)return;
      if(kind==='view')(hooks.view||hooks.details)(s);
      else if(kind==='closed')hooks.details(s);
      else if(kind==='details'||hooks.open)(hooks.open||hooks.details)(s);
      else openPanel({kind:'fill',shift:s.id,...(kind==='fill-more'?{more:true}:{})});}
  }
  /** The cards of names, open places and "+N" chips: one resolver for the grid, built when shown from the held draft. */
  const cardOf=(n:HTMLElement):HoverContent|null=>!n.matches('button.pchip[data-shift]:is([data-assignment],.gap,.others)')?null:{delay:280,build:()=>asNow(()=>{
    const s=sessionNow(n.dataset.shift);if(!s)return [];
    if(n.dataset.assignment){const p=s.participants.find((x:Row)=>x.id===n.dataset.assignment);return p?nameCard(s,p):[];}
    return n.classList.contains('others')?othersCard(s):n.classList.contains('role-gap')?roleCard(s):gapCard(s);})};
  /** A Month cell's people (names, team boxes, open places) for these sessions. */
  function chipsOf(list:Row[]){return list.flatMap(s=>cellContent(s).filter(n=>n.matches('.pchip,.team-box,.wk-open-row')));}
  /** A name's menu (⋯ or right-click) for its session and assignment as they are now; [] when either is gone. Also what
   *  a menu kept open across a redraw is filled with again (dom.ts menuSource), so its items never act on an earlier draw. */
  function chipMenu(chip:HTMLElement):MenuEntry[]{
    const s=sessionNow(chip.dataset.shift),p=s?.participants.find((x:Row)=>x.id===chip.dataset.assignment);if(!s||!p||!menus)return [];
    return [...chipEntries(s,p,hooks.live?.().workspace||w),...lockEntry(s),...(hooks.menu?hooks.menu({shift:s,person_id:p.person_id,day:dayOf(s)}):[])];
  }
  function cellEvents(host:HTMLElement){for(const t of ['click','contextmenu','change'])host.addEventListener(t,cellEvent);hoverWithin(host,cardOf);menuSource(host,chipMenu);return host;}
  // Schedule: days outside the draft are the published layer, greyed and read-only; an accent edge marks where it starts.
  const outside=(day:string)=>!!data.context&&(day<w.starts_on||day>=w.ends_on);
  const dayCls=(day:string)=>(outside(day)?' sd-ctx':'')+(data.context&&day===w.starts_on?' sd-edge':'');
  // A day outside the draft is "Published" only when a publication is in force that day; otherwise it is empty.
  const inForce=(day:string)=>outside(day)&&(data.published||[]).some(([from,until])=>day>=from&&day<until);
  /** The published layer's grey: a published day's header and its read-only names. */
  const ctxMark=<E extends Element>(n:E)=>key.mark(n,'ctx',el('span',{class:'pchip sd-greychip lg-mark','aria-hidden':'true'},'Name'),'Published, read-only',4);
  /** Every day outside the draft's dates, published or not: one grey, no numbers, and its key entry (maintenance). */
  const outsideMark=<E extends Element>(n:E)=>key.mark(n,'outside','lg-outside','Outside this rota',5);
  // Groups (service) → rows (duty · place · time) → cells (day).
  const groups=new Map<string,{name:string;rows:Map<string,{sub:string;type:string;place:string;time:string;service:string;hours:string;colour:string;shifts:Row[]}>}>();
  for(const s of inWeek){
    const g=groupOf(s),sub=thenBy?valueOf(thenBy,s):'';if(!groups.has(g))groups.set(g,{name:g,rows:new Map()});
    // With the Service property shown, sessions of different services are different rows (a row names one service).
    const key=`${sub}|${s.assignment_type_id}|${s.location_id}|${time(s)}${prop('service').on||thenBy==='service'?'|'+serviceName(s):''}`,rows=groups.get(g)!.rows;
    if(!rows.has(key))rows.set(key,{sub,type:s.type_snapshot.name,place:placeName(s,g),time:time(s),service:serviceName(s),hours:hoursOf(s),colour:hue(s),shifts:[]});
    rows.get(key)!.shifts.push(s);
  }
  const ordered=[...groups.values()].sort((a,b)=>byValue(a.name,b.name,groupWay(weekView.groupBy)));
  shownGroups.length=0;if(weekView.groupBy!=='none')for(const g of ordered){shownGroups.push(g.name);if(thenBy)for(const r of g.rows.values())if(!shownGroups.includes(`${g.name} › ${r.sub}`))shownGroups.push(`${g.name} › ${r.sub}`);}
  const nav=periodNav(periodTitle(period),unit,period,onPage,
    `${inWeek.length} session${inWeek.length===1?'':'s'} this ${unit} · ${ordered.length} ${({service:'service',type:'duty type',location:'location',time:'duty time',none:'group'} as Record<string,string>)[weekView.groupBy]}${ordered.length===1?'':'s'}`);
  const layout=el('div',{class:'wk-layout'});
  const {root,redraw,openPanel,rulesFor}=plannerPanels(ctx,data,period,hooks,nav,layout,()=>weekGrid(ctx,data,visible,period,onPage,hooks));

  // Longer ranges are the same grid with narrower day columns (scrolls sideways; duty names and dates stay pinned).
  // A month is the same grid once per week (a band of day headers, then one lane per group).
  const bands=unit==='month',tallies=hooks.tallies!==false&&weekView.staffing;
  // Declared before the grid is drawn below: a chip's pill reads them while drawing (a later const is in its TDZ).
  /** What a person's pill adds after their name (the one item renderer), in the view's order and form: duty code, TL, OT
   *  and tags; in the Week time, place, service and hours are the row's (rowFacts), a Month pill says them too. */
  const PILL=new Set<PropKey>(['code','lead','overtime','tags',...(bands?['time','location','service','duty','hours'] as PropKey[]:[])]);
  const pillParts=(s:Row,pid:string)=>itemNodes(itemParts(ctx,s,pid,zone,props,{only:PILL}),key,()=>'pchip-meta',{codeKey:true});
  const grid=el('div',{class:'wk-grid'+(weekView.compact?' compact':'')+(bands?' mo':days.length>7?' long':''),role:'grid','aria-label':`Sessions grouped by ${({service:'service',type:'duty type',location:'location',time:'duty time',none:'session'} as Record<string,string>)[weekView.groupBy]}, ${dayLabel(days[0])} to ${dayLabel(days.at(-1)!)}`});
  grid.style.setProperty('--days',String(days.length));
  cellEvents(grid);hostLegend(grid,key);  // the rule map's exception marks in this grid are this key's entries
  // The live auto-schedule view draws its chips through this grid's key (LiveHost.stamp; null: an open place): the same
  // colour rule and entries as the chips drawn here, so a live chip is keyed even for a duty type not on screen yet.
  const shiftOf=new Map(data.shifts.map(x=>[x.id,x]));
  (grid as LiveHost).stamp=(n,s,p)=>{if(s===null){openKey(n);return;}const x=shiftOf.get(s);if(x)seenChip(x,p,n);};
  // SW3-C-17: a Month of only my own duties (codes in its cells) fits a phone's width instead of scrolling sideways.
  if(bands&&hooks.open&&visible.length&&visible.every(s=>s.participants.length===1&&s.participants[0].person_id===ctx.me.person_id))grid.classList.add('mine');
  // Header row
  const head=el('div',{class:'wk-row wk-head',role:'row'},el('div',{class:'wk-corner',role:'columnheader'},bands?'Week':({service:'Service',type:'Duty type',location:'Location',time:'Duty time',none:'Session'} as Record<string,string>)[weekView.groupBy]));
  const weekendMark=<E extends Element>(n:E)=>markWeekend(key,n);
  /** A day's numbers and bar, entered in the key while they count something (a day without sessions says "—"). */
  const dayNumbers=(n:HTMLElement,sessions:number)=>!sessions?n:key.mark(n,'days',el('span',{class:'lg-mark wk-cov'},checked?'3/4':'3'),
    (checked?'Day numbers: places filled / needed (bar red when short)':'Day numbers: sessions')+(filtered?' (filtered sessions)':''),3);
  /** A day's column header: filled / needed, the bar, wishes, its hover card and menu. */
  function dayHead(day:string){
    const t=tally(onDay(day));
    const pct=t.need?Math.round(t.have/t.need*100):t.sessions?100:0,short=t.open>0;  // nothing planned: an empty bar, not a full one
    const pub=inForce(day);
    // A published day hides its bar (CSS): only a draft day's numbers and bar are entered in the key.
    const bar=tallies?dayNumbers(el('span',{class:'wk-bar'+(short?' short':''),'aria-hidden':'true'},el('span',{})),outside(day)?0:t.sessions):'';if(bar)(bar.firstChild as HTMLElement).style.setProperty('width',pct+'%');
    const label=(checked?`${t.have} of ${t.need} places filled`:plural(t.sessions,'session')+(w.status==='published'?'':' · not checked yet'))+(filtered?' · filtered':'');
    const named=data.date_set_markers?.[day];
    const cell=el('div',{class:'wk-day'+(short?' short':'')+dayCls(day),role:'columnheader',tabindex:0,'aria-label':`${formatDate(day,zone)}${named?.length?' · '+dateMarkWords(named):''} · ${outside(day)?(pub?'published, read-only':'outside this rota'):label}${data.context&&day===w.starts_on?' · the draft starts':''}`},
      el('div',{class:'wk-day-top'},el('strong',{},dayName(day)),el('span',{},String(Number(day.slice(8)))),rollUp(onDay(day),dayLabel(day),s=>`${s.type_snapshot.name} ${time(s)}`),...(tallies&&!outside(day)?[dayNumbers(el('span',{class:'wk-cov'},!t.sessions?'—':checked?`${t.have}/${t.need}`:String(t.sessions)),t.sessions)]:[])),bar,dateMarks(named));
    const dayIds=onDay(day).map(s=>s.id);
    if(outside(day)){outsideMark(cell);if(pub)ctxMark(cell);}
    tag(cell,{kind:'day',label:dayTitle(day),sub:checked?label:`${t.sessions} sessions`,day,days:[day],shifts:dayIds},{own:true,pin:true});
    // Staff wishes on this day: a quiet count; the day's hover card lists them.
    const wished=(data.wishes||[]).filter(x=>x.first_day<=day&&day<=x.last_day);
    if(wished.length)cell.querySelector('.wk-day-top')!.append(key.mark(el('span',{class:'wk-wish','aria-label':`${wished.length} staff wish${wished.length>1?'es':''}`},String(wished.length)),
      'wish',el('span',{class:'wk-wish lg-mark','aria-hidden':'true'},'2'),'Staff wishes on the day'));
    hoverCard(cell,()=>{
      const dayShifts=onDay(day),ids=new Set(dayShifts.map(s=>s.id));
      const errs=findings.filter(f=>f.severity==='error'&&f.shift_id&&ids.has(f.shift_id)),warns=findings.filter(f=>f.severity!=='error'&&f.shift_id&&ids.has(f.shift_id));
      const byRule=new Map<string,number>();for(const f of errs)byRule.set(f.message,(byRule.get(f.message)||0)+1);
      const lines=[!checked?(hooks.checking?line('na','Checking…','Open places and issues show when the check finishes'):line('na','Not checked yet','Run a rota check to see staffing and rules')):t.open?line('warn','Session staffing',`${t.open} place${t.open>1?'s':''} still open`):line('ok','Session staffing','Every session meets its minimum'),
        ...[...byRule.entries()].slice(0,4).map(([m,n])=>line('err',m,n>1?`${n} times on this day`:'Once on this day')),
        warns.length?line('info',`${warns.length} suggestion${warns.length>1?'s':''}`,'Optional preferences and targets'):null,
        ...wished.slice(0,4).map(x=>line('info',fullNameOf(people.get(x.person_id))||'Someone','Wish: '+wishWords(x,ctx.catalogues.assignment_types||[])))].filter(Boolean) as HTMLElement[];
      return [hcHead(dayName(day),formatDate(day,zone),checked?label+` · ${t.sessions} sessions`:label),el('ul',{class:'hc-lines'},...lines),...ruleSummary({kind:'day',label:dayTitle(day),day,days:[day],shifts:dayIds})];
    },400);
    return withMenu(cell,{day,shifts:onDay(day).filter(s=>!isClosed(s))},'Actions for '+formatDate(day,zone));
  }
  if(!bands)head.append(...days.map(dayHead));
  grid.append(head);
  scheduleWidths(grid,{row:head.querySelector<HTMLElement>(':scope>.wk-corner'),col:head.querySelector<HTMLElement>(':scope>.wk-day'),colKey:'date'});
  // Where the sticky day header and group head end (their real heights), so a tall row's duty sticks just below them.
  // The tallest group head: a narrower grid (a side panel open) wraps some names to two lines, and whichever group is
  // stuck, a row's duty must stick below it.
  const groupHeads=()=>[...grid.querySelectorAll<HTMLElement>(':scope>.wk-group:not(.wk-subgroup)>.wk-group-head')];
  const stickTops=()=>{if(!grid.isConnected)return;grid.style.removeProperty('--wk-group-min');const h=head.querySelector<HTMLElement>(':scope>.wk-corner')?.offsetHeight||0,g=Math.max(0,...groupHeads().map(n=>n.offsetHeight));
    grid.style.setProperty('--wk-head-h',h+'px');grid.style.setProperty('--wk-group-h',g+'px');grid.style.setProperty('--wk-group-min',g+'px');};
  // Measured once laid out (a grid may be mounted after its build) and again when the header's or a group head's height
  // changes (a side panel opening or closing, a window resize). Rows are `display:contents` (no box, never observed):
  // watch the corner cell and each group head (added below, once the groups are drawn).
  const corner=head.querySelector<HTMLElement>(':scope>.wk-corner');
  // observeSize runs in the next frame and lets a removed box go; several boxes reporting in one frame stick once.
  let owed=false;const stick=()=>{if(owed)return;owed=true;queueMicrotask(()=>{owed=false;stickTops();});};
  const watched=new WeakSet<Element>();  // a head observed once, however often the groups are drawn
  const sizes=!bands&&!weekView.forPrint&&corner?{observe:(n:Element)=>{if(!watched.has(n)){watched.add(n);observeSize(n,stick);}}}:null;
  sizes?.observe(corner!);

  // Open places are shown where they are (the dashed "+1 needed" chips) and listed by the "need staff" pill; no extra lane.
  // The published-only schedule (no draft) has nothing to check or add to.
  const published=w.status==='published';
  // A draft checks itself after every change (app.ts): while that runs say so, with no instruction.
  // Only while a check runs; a draft never checked (read by an auditor) or whose check failed says so, not a spinner.
  if(!checked&&!published)grid.append(el('div',{class:'wk-status info',role:'status'},...(hooks.checking?[el('span',{class:'spinner','aria-hidden':'true'}),'Checking…']
    :[icon('clock'),'Not checked yet. Run a rota check to see open places and issues on the grid.'])));
  // Hidden by filters: say so. Nothing there at all: name the toolbar's way forward (Add session, or New draft); a draft's
  // period outside its dates, or a read-only viewer, is told to move on.
  const hidden=(hooks.unfiltered||data.shifts).some(s=>days.includes(dayOf(s)));
  const create=hooks.create&&(!hooks.editable||days.some(d=>d>=w.starts_on&&d<w.ends_on))?hooks.create:'';
  // Month: the day grid alone already says "nothing" (its days stay click targets); no card above it too (S-02).
  if(!ordered.length&&!bands)grid.append(el('div',{class:'wk-empty'},scheduleEmpty(unit,{filtered:hidden,hooks,create:!!create,published})));
  // SW-26: a month with nothing at all says so with its way forward, under the day grid (never a card above it, S-02).
  if(bands){monthBands();if(!ordered.length)grid.append(el('div',{class:'wk-row mo-none',role:'row'},el('div',{class:'wk-cell mo-none-cell mo-empty',role:'gridcell'},scheduleEmpty(unit,{filtered:hidden,hooks,create:!!create,published}))));}
  else for(const group of ordered){
    const rows=[...group.rows.values()],all=rows.flatMap(r=>r.shifts),isCollapsed=collapsed.has(group.name);
    grid.append(headRow(weekView.groupBy,group.name,group.name,all,isCollapsed,false));
    if(isCollapsed)continue;
    if(!thenBy){drawRows(rows);continue;}
    const subs=new Map<string,typeof rows>();for(const r of rows)subs.set(r.sub,[...(subs.get(r.sub)||[]),r]);
    for(const [sub,list] of [...subs].sort((a,b)=>byValue(a[0],b[0],groupWay(thenBy||'')))){
      // A Then by head's collapse is kept per group (saved with the view as "group › value").
      const id=`${group.name} › ${sub}`,shut=collapsed.has(id);
      grid.append(headRow(thenBy,sub,id,list.flatMap(r=>r.shifts),shut,true));
      if(!shut)drawRows(list);
    }
  }
  /** A group (or Then by) head row: toggle, its count and, per day, places filled / needed. */
  function headRow(by:Dim|'none',name:string,id:string,all:Row[],isCollapsed:boolean,sub:boolean){
    const colour=sub?'slate':groupHue(name,all);
    const toggle=groupToggle(el('strong',{class:'clamp-2',title:name},name),all.length,!isCollapsed,()=>{collapsed.has(id)?collapsed.delete(id):collapsed.add(id);redraw();},
      {cls:'wk-toggle',lead:el('span',{class:`wk-swatch duty-${colour}`,'aria-hidden':'true'})});
    const gt=headTag(by,name,all);
    const ghead=headHue(el('div',{class:`wk-group-head duty-${colour}`,role:'rowheader'},toggle),sub?[]:all);
    // Rules for a group open Rules in context, which a draft mounts; a read-only grid (hooks.open) has none, so no dead button.
    if(gt){tag(ghead,gt);if(!hooks.open){const rb=el('button',{type:'button',class:'icon-button bare rk-pin-btn','aria-label':'Rules for '+name,'data-tip':'Rules for '+name},icon('rules'));rb.addEventListener('click',e=>{e.stopPropagation();pin(gt,'rules','',e.shiftKey||e.ctrlKey||e.metaKey);});ghead.append(rb);}}
    const grow=el('div',{class:'wk-row wk-group'+(sub?' wk-subgroup':''),role:'row'},ghead);
    for(const day of days){
      const t=tally(all.filter(s=>dayOf(s)===day));
      if(!tallies||outside(day)||!isCollapsed&&ordered.length===1&&!sub){grow.append(el('div',{class:'wk-cell'+dayCls(day),role:'gridcell'}));continue;}
      // Filled counts the people who count towards each minimum, not names on the grid: say so when the two differ (SI-32).
      const heads=all.filter(s=>dayOf(s)===day&&!isClosed(s)).reduce((n,s)=>n+s.participants.length,0);
      const tip=checked&&t.need&&heads!==t.have?{'data-tip':`${heads} on the grid · ${t.have} count towards the minimum`}:{};
      grow.append(el('div',{class:'wk-cell wk-sum'+dayCls(day),role:'gridcell'},!t.sessions?el('span',{class:'wk-none'},'—'):checked&&t.need?el('span',{class:t.open?'short':'ok',...tip},el('b',{},`${t.have}/${t.need}`),t.open?' short':' filled'):el('span',{class:'wk-none'},`${t.sessions} session${t.sessions>1?'s':''}`)));
    }
    return grow;
  }
  function drawRows(list:{type:string;place:string;time:string;service:string;hours:string;colour:string;shifts:Row[]}[]){
    for(const r of list.sort((a,b)=>weekView.sortBy==='type'?a.type.localeCompare(b.type)||a.time.localeCompare(b.time):weekView.sortBy==='place'?a.place.localeCompare(b.place)||a.time.localeCompare(b.time):a.time.localeCompare(b.time)||a.type.localeCompare(b.type)||a.place.localeCompare(b.place))){
      // The duty, time and place stay in view while a tall row (a big team) scrolls under the day and group heads.
      const rh=el('div',{class:`wk-rowhead duty-${r.colour}`,role:'rowheader',tabindex:0},el('div',{class:'wk-rowhead-in'},el('span',{class:'clamp-2'},r.type),el('small',{class:'truncate'},...rowFacts(r))),
        rollUp(r.shifts.filter(x=>days.includes(dayOf(x))),`${r.type} ${r.time}`,x=>dayLabel(dayOf(x))));
      seen(r.shifts[0],rh);
      const rt={kind:'row' as const,type:r.shifts[0].assignment_type_id,location:r.shifts[0].location_id,label:r.type,sub:`${r.time} · ${r.place}`,shifts:r.shifts.map(x=>x.id)};
      tag(rh,rt,{own:true,pin:true});
      hoverCard(rh,()=>{
        const s=r.shifts[0],def=s.type_snapshot.definition,req=Math.max(0,...r.shifts.map(need)),ids=new Set(r.shifts.map(x=>x.id));
        const errs=findings.filter(f=>f.severity==='error'&&f.shift_id&&ids.has(f.shift_id));
        const byRule=new Map<string,number>();for(const f of errs)byRule.set(f.message,(byRule.get(f.message)||0)+1);
        const mins=(def.requirements||[]).map((q:Row)=>q.minimum).filter((n:number)=>n>0);
        // Status only (SI-07): what it needs and how it stands; who may work it and supervision are configuration.
        const t=tally(r.shifts);
        return [hcHead(String(req||mins[0]||'–'),r.type,`${r.time} · ${r.place} · ${r.shifts.length} day${r.shifts.length>1?'s':''} this ${unit}`,`duty-${r.colour}`),el('ul',{class:'hc-lines'},
          line(t.open?'warn':'info',mins.length?`Needs ${Math.max(...mins)} ${Math.max(...mins)===1?'person':'people'}`:'No minimum staffing set',checked&&t.need?`${t.have}/${t.need} filled this ${unit}`:''),
          ...[...byRule.entries()].slice(0,2).map(([m,n])=>line('err',m,`${n} time${n>1?'s':''} this ${unit}`))),
          ...ruleSummary(rt)];
      },400);
      withMenu(rh,{shift:r.shifts[0],shifts:r.shifts.filter(s=>!isClosed(s))},`Actions for ${r.type} ${r.time}`);
      const row=el('div',{class:'wk-row',role:'row'},rh);
      for(const day of days){
        const cell=el('div',{class:'wk-cell'+dayCls(day),role:'gridcell'});
        const here=r.shifts.filter(x=>dayOf(x)===day).sort((a,b)=>Number(searched(b))-Number(searched(a)));if(here.length)cell.dataset.session=here.map(s=>s.id).join(' ');
        // A cell is selectable (the selection panel: its sessions' details, rules and issues). One with controls is reached
        // by the keyboard through them; an empty one joins the grid's arrows itself. Its marks stay on its sessions.
        if(!hooks.open&&!outside(day))tag(cell,{kind:'row',type:r.shifts[0].assignment_type_id,location:r.shifts[0].location_id,label:`${r.type} · ${dayTitle(day)}`,
          sub:`${r.time} · ${r.place}`,day,days:[day],shifts:here.map(x=>x.id)},{own:true,quiet:true,pin:here.length?'mouse':true});
        for(const s of here){cell.append(...cellContent(s));if(changed.has(s.id))cell.classList.add('is-changed');}
        // Only the cell's first line clears its selection box and session marker (D78); the lines below take the full width.
        cell.querySelector(':scope>:is(.pchip,.team-box,.wk-open-row)')?.classList.add('wk-first');
        const chips=[...cell.querySelectorAll<HTMLElement>(':scope>:is(.pchip,.team-box):not(.gap)')];
        if(chips.length>CELL_LIMIT&&!expanded.has(cellKey(r,day))){
          chips.slice(CELL_LIMIT).forEach(c=>c.hidden=true);
          // data-limit: the live auto-schedule view keeps the first CELL_LIMIT showing as chips move (and replaces the chips
          // it changes, so a click finds them again).
          const more=overflowMore(chips.length-CELL_LIMIT,CELL_MORE.noun,{...CELL_MORE,cls:'pchip more',attrs:{'data-limit':String(CELL_LIMIT),'data-cell':cellKey(r,day)}});
          chips[CELL_LIMIT-1].after(more);
        }
        row.append(cell);
      }
      grid.append(row);
    }
  }

  /** Month: one band per week (its days' headers), then one lane per group whose cells show the first few people
   *  (density: compact 3, comfortable 2) and "+N more", which lists the day's whole lane. Days of the next or last
   *  month fill the first and last band, greyed. */
  function monthBands(){
    const unit=DIMENSIONS.find(d=>d.key===weekView.groupBy),idleWords=(n:number)=>unit?plural(n,unit.one,unit.many):plural(n,'group');
    const js=((period.weekStart??ctx.me.organisation.week_start??6)+1)%7,dow=(d:string)=>new Date(d+'T12:00:00Z').getUTCDay();
    const shown=new Set(days),cols=[...new Set(days.map(dow))].sort((a,b)=>(a-js+7)%7-(b-js+7)%7),limit=weekView.forPrint?Infinity:weekView.compact?3:2;
    grid.style.setProperty('--days',String(cols.length));
    head.append(...cols.map(c=>(n=>WEEKEND_DAYS.includes((c+6)%7)?weekendMark(n):n)(el('div',{class:'wk-day'+(WEEKEND_DAYS.includes((c+6)%7)?' weekend':''),role:'columnheader'},el('div',{class:'wk-day-top'},el('strong',{},dayName(addDays('2026-01-04',c))))))));
    const lanes=new Map(ordered.map(g=>{const all=[...g.rows.values()].flatMap(r=>r.shifts).sort((a,b)=>String(a.start).localeCompare(String(b.start)));
      return [g,{all,byDay:byDayOf(all,dayOf)}];}));
    for(const start of [...new Set(days.map(d=>addDays(d,-((dow(d)-js+7)%7))))]){
      const week=cols.map(c=>addDays(start,(c-js+7)%7)),inside=week.filter(d=>shown.has(d)),insideSet=new Set(inside);
      const out=(d:string)=>el('div',{class:'wk-day mo-out',role:'columnheader','aria-label':formatDate(d,zone)+' · not in this month'},el('div',{class:'wk-day-top'},el('strong',{},String(Number(d.slice(8)))),el('span',{},monthOf(d))));
      // A group with no session this week is left out of the band (the band says how many), so a month reads as its
      // sessions, not as empty lanes; every group with something that week is drawn, short or not.
      const busy=ordered.filter(g=>inside.some(d=>lanes.get(g)!.byDay.has(d))),idle=ordered.length-busy.length;
      grid.append(el('div',{class:'wk-row mo-wk',role:'row'},el('div',{class:'wk-corner',role:'rowheader'},weekRowLabel(week),
        idle&&busy.length?el('small',{class:'mo-idle'},`${idleWords(idle)} without sessions`):null),...week.map(d=>shown.has(d)?dayHead(d):out(d))));
      if(!busy.length&&ordered.length)grid.append(el('div',{class:'wk-row mo-none',role:'row'},el('div',{class:'wk-cell mo-none-cell',role:'gridcell'},'No sessions this week')));
      for(const group of busy){
        const {all,byDay}=lanes.get(group)!;
        const row=el('div',{class:'wk-row',role:'row'},headHue(el('div',{class:`wk-rowhead duty-${groupHue(group.name,all)}`,role:'rowheader'},el('span',{class:'wk-swatch','aria-hidden':'true'}),el('span',{class:'clamp-2',title:group.name},group.name),
          rollUp(all.filter(x=>insideSet.has(dayOf(x))),`${group.name}, ${weekRowLabel(week)}`,x=>`${dayLabel(dayOf(x))} · ${x.type_snapshot.name}`)),all));
        for(const d of week){
          const cell=el('div',{class:'wk-cell'+(shown.has(d)?dayCls(d):' mo-out')+(isWeekend(d)?' weekend':''),role:'gridcell','aria-label':`${formatDate(d,zone)} · ${group.name}`});
          if(isWeekend(d))weekendMark(cell);
          // Counted, not drawn: only the chips that show are built (a busy month built ~1,900 chips to show 180).
          const list=shown.has(d)?[...byDay.get(d)||[]].sort((a,b)=>Number(searched(b))-Number(searched(a))):[],total=list.reduce((n,s)=>n+chipCount(s),0);
          // Marked as the Week's cells are: a cause's ring, the open session and the selection find the cell by its sessions.
          if(list.length)cell.dataset.session=list.map(s=>s.id).join(' ');
          // A session's marks a Month cell leaves out (its lock, clash and later chips) are not entered in the key.
          cell.append(...drawnMarks(()=>{const chips:HTMLElement[]=[];for(const s of list){if(chips.length>=limit)break;chips.push(...chipsOf([s]));}return chips.slice(0,limit);},c=>c));
          if(total>limit){
            const more=overflowMore(total-limit,['entry','entries'],{of:`on ${formatDate(d,zone)}, ${group.name}`,action:'show all',cls:'pchip more',
              attrs:{'data-sessions':list.map(s=>s.id).join(' '),'data-group':group.name,'data-day':formatDate(d,zone),'data-label':`${group.name}, ${formatDate(d,zone)}`}});
            cell.append(more);
          }
          row.append(cell);
        }
        grid.append(row);
      }
    }
  }
  /** A row's time, place, service and hours, as the view's properties ask (text, or a chip each). */
  function rowFacts(r:{time:string;place:string;service:string;hours:string}):HTMLElement[]{
    const text={time:r.time,location:r.place,service:r.service,hours:r.hours};
    // What a group or Then by head already names is not said again on each row.
    const headed=new Set<string>([weekView.groupBy,thenBy||'']);
    return ROW_FACTS.map(k=>({k,p:prop(k)})).filter(x=>x.p.on&&!headed.has(x.k)).sort((a,b)=>props.indexOf(a.p)-props.indexOf(b.p))
      .map(({k,p})=>el('span',{class:(p.form==='chip'?'wk-fact chip':'wk-fact')+' wk-fact-'+k},text[k]));
  }
  /** My duties' chip (S-12) says when and where, except what its group or Then by head names; a phone Month says the
   *  duty's code (its cells are narrow, SW3-C-17). */
  function ownWords(s:Row){
    if(bands&&phoneQuery.matches)return sessionCode(ctx,s);
    // Row details add information beside the chip; they never remove its time/place label (S-12).
    const named=new Set<string>([weekView.groupBy,thenBy||'']);
    return [named.has('time')?'':time(s),named.has('location')?'':labelOf(locations.get(s.location_id))].filter(Boolean).join(' · ')||'On duty';
  }
  /** Replace, Exchange and Remove on a person's chip (LIVE-36, SR-14): a coordinator's draft session that may change. */
  const chipEntries=(s:Row,p:Row,lw:Row):MenuEntry[]=>!hooks.editable||hooks.open||s.locked||isClosed(s)||heldFixed(hooks,p)?[]:[
    {label:'Replace…',icon:'people',run:()=>openPanel({kind:'fill',shift:s.id,replace:p.id})},
    ...(hooks.exchange?[{label:'Exchange with…',icon:'swap' as IconName,run:()=>hooks.exchange!(p.id)}]:[]),
    {label:'Remove from this session',icon:'trash',run:()=>void removeAssignment(lw,s,p.id).then(()=>hooks.refresh()).catch(reportError)}];
  // A person's hours and rest from the whole rota before the toolbar's filters and My duties (the hover card's facts, SI-03).
  const weekFrom=((ctx.me.organisation.week_start??6)+1)%7;let rota=hooks.rota||hooks.unfiltered||data.shifts,facts=new Map<string,Duty[]>();
  function personFacts(s:Row,pid:string):HTMLElement[]{
    let mine=facts.get(pid);if(!mine){mine=attendance(rota,pid);facts.set(pid,mine);}
    const f=workFacts(mine,s,dayOf,weekFrom),today=mine.filter(x=>x.s.id!==s.id&&dayOf(x.s)===dayOf(s));
    return [...hooks.windowOnly?[]:[line('info',`${f.week} h this week · ${f.rota} h this rota`,f.rest===null?'No earlier duty':f.rest>=48?`Off ${Math.round(f.rest/24)} days before`:`Rested ${Math.round(f.rest)} h`)],
      ...(today.length?[line('info','Also today',today.map(x=>`${x.s.type_snapshot.name} ${time(x.s)}`).join(' · '))]:[])];
  }
  /** Lock / Unlock this session: one click, Undo in the message (a person's right-click menu). */
  const lockEntry=(s:Row):MenuEntry[]=>hooks.editable&&hooks.lock&&!isClosed(s)?[{label:s.locked?'Unlock this session':'Lock this session',icon:'lock',
    hint:hooks.sessionsHeld?onlyRotaCoordinator('lock'):s.locked?'Lets you and auto-schedule change it':'Auto-schedule keeps it as it is',run:()=>void hooks.lock!(s,!s.locked)}]:[];
  /** How many chips cellContent draws for a session, without drawing them (keep the two in step). */
  function chipCount(s:Row):number{
    if(isClosed(s))return 1;
    const drawn=drawnPeople(s);
    const units=showPairs()?teamGroups(drawn.shown).length:drawn.shown.length;
    if(s.context)return Math.max(1,units+(drawn.hidden.length?1:0));
    const removed=new Set((hooks.proposal?.removals||[]).filter(r=>r.shift_id===s.id).map(r=>r.assignment_id)).size;
    const adds=(hooks.proposal?.additions||[]).filter(x=>x.shift_id===s.id).length;
    const open=prop('open').on?Math.max(0,openPlaces(cover.get(s.id))-adds+removed):0;
    const role=prop('open').on&&!open&&!adds&&!!roleGap(data,s.id,cover.get(s.id))&&!boxTells(s);
    return units+(drawn.hidden.length?1:0)+adds+(open||role||!s.participants.length&&!adds?1:0);
  }
  /** The people a person display leaves out of this session (app.ts: Search › Show; display only): one quiet "+N" that
   *  opens the session with everyone, red when one of them breaks a rule, so no problem is hidden with them. */
  function hiddenChip(s:Row):HTMLElement|null{
    const ids=drawnPeople(s).hidden;if(!ids.length)return null;
    const bad=ids.filter(id=>errorsFor(s.id,id).length).length;
    const b=el('button',{type:'button',class:'pchip others'+(bad?' issue':''),'data-shift':s.id,
      'aria-label':`${plural(ids.length,'other person','other people')} on ${sessionWords(s)}, not shown by the search${bad?`; ${bad} of them ${bad===1?'breaks':'break'} a rule`:''}. Open the session to see everyone.`},
      el('span',{class:'pchip-text'},`+${ids.length}`),bad?key.mark(issueGlyph('error',bad),'issue:error',issueGlyph('error'),SEVERITY_WORDS.error):'');
    b.dataset.open='details';
    return key.mark(b,'others',el('span',{class:'pchip others lg-mark','aria-hidden':'true'},'+2'),'Others on the session, not shown by the search',3);
  }
  /** The "+N" chip's card: the people the display leaves out, as the session has them now. */
  function othersCard(s:Row):Node[]{
    const ids=drawnPeople(s).hidden,bad=ids.filter(id=>errorsFor(s.id,id).length).length;if(!ids.length)return [];
    return [hcHead('+'+ids.length,s.type_snapshot.name,`${dayLabel(dayOf(s))} · ${time(s)}`,`duty-${hue(s)}`),
      el('ul',{class:'hc-lines'},line(bad?'err':'info',plural(ids.length,'other person','other people'),ids.map(id=>fullNameOf(people.get(id))).join(' · ')),
        ...(bad?[line('err',LEVEL_WORDS.must,`${bad} of them`)]:[])),el('div',{class:'hc-foot'},'Click to see everyone on the session')];
  }
  /** A name's card (SI-03): who, why here, what else today and their hours first, the rules in one line; nothing is read
   *  from the server to show it. Short, so it never covers the grid (SI-02). */
  function nameCard(s:Row,p:Row):Node[]{
    const person=people.get(p.person_id),errs=errorsFor(s.id,p.person_id),warns=warningsFor(s.id,p.person_id);
    const tagText=((person?.tags||[]) as string[]).join(' · ');
    const rules=errs.length?line('err',errs.length>1?`${errs.length} broken rules`:'1 broken rule',errs.slice(0,2).map(f=>f.message).join(' · '))
      :!checked?line('na',hooks.checking?'Rules: checking…':'Rules: not checked yet')
      :hooks.kept?line('na',warns.length?`${warns.length} warning${warns.length>1?'s':''} · from the last check`:'Rules: from the last check')
      :warns.length?line('warn',`Rules pass · ${warns.length} warning${warns.length>1?'s':''}`,warns[0].message):line('ok','Rules: all pass');
    // A double booking leads (both sides say it, S-07): rule checks of this one session cannot show it.
    const booked=clashesOf(s,p.person_id).map(c=>line('err','Double-booked',`also in ${c.other_session_name}`));
    const drag=hooks.editable&&!hooks.open&&!s.locked;
    return [hcHead(hooks.code(person),fullNameOf(person),`${s.type_snapshot.name} · ${dayLabel(dayOf(s))} · ${time(s)}${tagText?' · '+tagText:''}`,`duty-${hue(s)}`),
      el('ul',{class:'hc-lines'},...booked,...(hooks.open?[]:personFacts(s,p.person_id)),rules),...(hooks.open?[]:[whyHere(cur.workspace,s,p.person_id)]),
      el('div',{class:'hc-foot'},`Click to open · code ${hooks.code(person)}${drag?' · drag to exchange':''}`)];
  }
  /** Open places on a session as drawn (with a proposal shown, as it would leave them). */
  function openOn(s:Row):number{
    const adds=(hooks.proposal?.additions||[]).filter(x=>x.shift_id===s.id).length;
    const removed=new Set((hooks.proposal?.removals||[]).filter(r=>r.shift_id===s.id).map(r=>r.assignment_id)).size;
    return prop('open').on?Math.max(0,openBeside(cover.get(s.id),boxShortfall(s))-adds+removed):0;
  }
  function gapCard(s:Row):Node[]{
    const open=openOn(s),c=cover.get(s.id);if(!open||!c)return [];
    return [hcHead('+'+open,s.type_snapshot.name+' · open place',`${dayLabel(dayOf(s))} · ${time(s)} · ${placeName(s,serviceName(s))}`,'gap'),
      el('ul',{class:'hc-lines'},line('warn','Session staffing',`Minimum not met: ${open} more needed · ${c.available} of ${c.required} places filled`),line('info','Who can fill it','Click to see everyone who can work it, and why others cannot')),
      el('div',{class:'hc-foot'},hooks.editable?'Click to fill':'Read-only')];
  }
  function roleCard(s:Row):Node[]{
    const c=cover.get(s.id),role=roleGap(cur,s.id,c) as 'lead'|'role'|null;if(!role||!c)return [];const words=ROLE_GAP_WORDS[role];
    return [hcHead(role==='lead'?MARKS.lead[0]:'!',s.type_snapshot.name+' · '+words.toLowerCase(),`${dayLabel(dayOf(s))} · ${time(s)} · ${placeName(s,serviceName(s))}`,'gap'),
      el('ul',{class:'hc-lines'},line('ok','Places filled',`${c.available} of ${c.required}`),line('warn',words,role==='lead'?'Name a qualified person already on this session as team lead':'Someone with the required skill or a named supervisor is missing')),
      el('div',{class:'hc-foot'},hooks.editable?'Click to fix':'Read-only')];
  }
  /** A person's issues on a session (D78): must fix always, could improve with View › Rule marks; in a team box, a
   *  supervision problem is the box's own. */
  function personIssues(s:Row,p:Row,boxed:boolean):MarkIssue[]{
    const errs=errorsFor(s.id,p.person_id),marked=boxed?errs.some(f=>!BOX_CODES.has(String(f.code))):errs.length>0;
    return [...(marked?errs.filter(f=>!boxed||!BOX_CODES.has(String(f.code))):[]),...(showAdvisory()?warningsFor(s.id,p.person_id):[])].map(f=>findingIssue(f));
  }
  /** A session's own issues (D78): its open-place findings beside its open place, the rest at the cell's top right. */
  function sessionIssues(s:Row){
    const teamSays=showPairs()&&teamGroups(s.participants as Row[]).some(t=>!!teamProblem(t,s.participants as Row[],pid=>errorsFor(s.id,pid)));
    return [...errorsFor(s.id),...(showAdvisory()?byKey.get('w|'+s.id+'|')||[]:[])].filter(f=>!f.person_id&&!(teamSays&&BOX_CODES.has(String(f.code))));
  }
  /** One box per supervision team (team_view.ts): the supervisor and every trainee who names them here, each with
   *  their own chip (menu, drag, marks), so a cell's limit counts the team once and never hides a trainee. With the
   *  view's Supervisor property off, the chips are drawn side by side (chipCount counts them so). Declarations, not
   *  const arrows: the grid above draws its cells before these lines run. */
  function teamName(id:string){return people.has(id)?hooks.name(people.get(id)):'Named supervisor';}
  function teamBox(s:Row,team:TeamEntry<Row>[],members:HTMLElement[],problem:TeamProblem|null):HTMLElement[]{
    if(!showPairs())return members;
    // At most three trainee rows on screen; "+N more" opens the session with everyone (print shows everyone).
    return [teamView(team,{density:bands||weekView.compact?'compact':'card',nameOf:teamName,fullName:id=>people.has(id)?fullNameOf(people.get(id)):'Named supervisor',
      rankOf:id=>rankOn(ctx.catalogues,id,dayOf(s)),member:e=>members[team.indexOf(e)],problem,
      limit:weekView.forPrint?undefined:weekView.teamRows,more:()=>{const x=hooks.open?s:sessionNow(s.id);if(x)(hooks.open||hooks.details)(x);},
      layout:teamLayout(),key,act:()=>hooks.open?hooks.open(s):openPanel({kind:'session',shift:s.id})})];
  }
  /** The session's rule problems are all ones a team box on it already says (Needs supervisor, Over the limit): its
   *  "Needs a skill or supervisor" pill would say them twice. */
  /** A team box on the session states a staffing shortfall (Needs supervisor, Over the limit): its open places are said there. */
  function boxShortfall(s:Row):boolean{
    return showPairs()&&teamGroups(s.participants as Row[]).some(t=>statesShortfall(teamProblem(t,s.participants as Row[],pid=>errorsFor(s.id,pid))));
  }
  function boxTells(s:Row):boolean{
    const errs=errorsFor(s.id);
    return showPairs()&&errs.length>0&&errs.every(f=>BOX_CODES.has(String(f.code)))&&teamGroups(s.participants as Row[]).some(t=>!!teamProblem(t,s.participants as Row[],pid=>errorsFor(s.id,pid)));
  }
  function cellContent(s:Row):HTMLElement[]{
    const out:HTMLElement[]=[],c=cover.get(s.id),colour=hue(s);
    if(s.context){
      // Published around the draft: names only, grey (the pill's .sd-greychip modifier), read-only (opens its details).
      // Each is the one person pill, given the person: the same short forms in a narrow cell, tip and name as a draft chip,
      // and the same data-person mark (a person's highlight and a published change's leaving mark reach it too).
      const grey=(label:string,name:string,id?:string)=>ctxMark(personPill({base:el('button',{type:'button',class:'sd-greychip','data-open':'view','data-shift':s.id,...(id?{'data-person':id}:{}),'aria-label':name}),
        label,full:id?fullNameOf(people.get(id)):undefined,person:id}));
      if(isClosed(s))return [grey('Closed',`${s.type_snapshot.name} closed, published`)];
      const others=hiddenChip(s);
      return s.participants.length?[...teamGroups(drawnPeople(s).shown,s.participants as Row[]).flatMap(team=>drawnMarks(()=>teamBox(s,team,team.map(({p})=>{const person=people.get(p.person_id);
        return grey(hooks.name(person),`${fullNameOf(person)}, ${sessionWords(s)}, published, read-only`,p.person_id);}),
        teamProblem(team,s.participants as Row[])),n=>n)),...(others?[others]:[])]
        :[grey('No one',`${sessionWords(s)}, published, nobody assigned`)];
    }
    const removed=new Set((hooks.proposal?.removals||[]).filter(r=>r.shift_id===s.id).map(r=>r.assignment_id));
    const absent=absentFrom(s,byKey.get(s.id)||[],ctx.me.person_id);
    if(hooks.editable&&!isClosed(s)){
      const box=el('input',{type:'checkbox',class:'wk-check','data-shift':s.id,'aria-label':`Select ${sessionWords(s)}`}) as HTMLInputElement;
      box.checked=hooks.selected.has(s.id);
      // A 24 px target around the 14 px box (WCAG 2.5.8, SI-11): the label takes the click.
      out.push(el('label',{class:'wk-check-hit'},box));
    }
    // The mark unlocks for those who may; held (sessionsHeld), it stays the same control, aria-disabled, its reason its description.
    if(s.locked){const unlock=hooks.editable&&hooks.lock?hooks.lock:null,why=unlock&&hooks.sessionsHeld?el('span',{class:'sr-only',id:`wk-lock-why-${++lockWhySerial}`},onlyRotaCoordinator('lock')):null,
      mark=unlock?el('button',{type:'button',class:'wk-lock','data-shift':s.id,'data-export-skip':'','aria-label':`Unlock ${s.type_snapshot.name} at ${formatTime(s.start,zone)} on ${formatDate(s.start,zone)}`,
        ...(why?{'aria-disabled':'true','aria-describedby':why.id,'data-tip':why.textContent}:{'data-tip':'Locked: auto-schedule keeps it as it is. Click to unlock.'})},icon('lock'),why)
        :el('span',{class:'wk-lock','data-export-skip':'','data-tip':'Locked session · kept as-is by auto-schedule','aria-label':'Locked session'},icon('lock'));
      out.push(key.mark(mark,'locked',el('span',{class:'lg-mark lg-icon','aria-hidden':'true'},icon('lock')),'Locked: kept as-is by auto-schedule'));}
    const clash=clashMark(s,hooks.refresh,()=>sessionNow(s.id));
    if(clash)out.push(key.mark(clash,'clash',CLASH_SWATCH(),CLASH_WORDS));
    if(isClosed(s)){out.push(reasoned(el('button',{type:'button',class:'pchip closed','data-open':'closed','data-shift':s.id},'Closed'),closureText(s,id=>fullNameOf(people.get(id)))));return out;}
    // A team's members are built before the team draws its first few: only the drawn ones' marks enter the key.
    for(const team of teamGroups(drawnPeople(s).shown,s.participants as Row[]))out.push(...drawnMarks(()=>{const members:HTMLElement[]=[];
    // The box says who supervises (its badge, indent and rank), so its members drop the SUP/TR tags.
    const problem=showPairs()?teamProblem(team,s.participants as Row[],pid=>errorsFor(s.id,pid)):null,boxed=showPairs()&&(team.length>1||!!problem);
    for(const {p,missing} of team){
      const person=people.get(p.person_id),who=hooks.name(person),errs=errorsFor(s.id,p.person_id);
      const tags=([p.participation==='trainee'?'tr':null,p.participation==='supervisor'?'sup':null,p.team_lead?'lead':null,p.overtime?'ot':null] as (MarkKey|null)[]).filter((k):k is MarkKey=>!!k);
      const missingWords=missing?', named supervisor not on this session':'';
      // In a team box, a supervision problem is the box's own line: the chip marks only a rule the box does not say.
      const issue=errs.length>0,away=absent.has(p.person_id),marked=boxed?errs.some(f=>!BOX_CODES.has(String(f.code))):issue;
      // Pairs: a trainee's chip carries "↳ supervisor" (a short code when compact); every label says who is with whom.
      const pairs=pairWords(s.participants,p.person_id,id=>fullNameOf(people.get(id))||'named supervisor');
      const fresh=changed.has(s.id)&&!(prev?.get(s.id)||'').split(',').includes(p.id);
      // The person's own issues (D78): must fix always, could improve with View › Rule marks; a problem the team says
      // is its marker's, not the chip's.
      const issues=personIssues(s,p,boxed);
      // Wishes this assignment grants or goes against (the server's per-day evaluator; "against" wins when both).
      const wishFx=(p.wish_effects||[]) as Row[],wish:''|'grants'|'against'=wishFx.some(x=>x.effect==='against')?'against':wishFx.length?'grants':'';
      const chip=el('button',{type:'button','data-person':p.person_id,'data-shift':s.id,'data-assignment':p.id,
        'aria-label':`${fullNameOf(person)}, ${sessionWords(s)}${tags.length?', '+tags.map(t=>MARKS[t][1].toLowerCase()).join(', '):''}${pairs?', '+pairs:''}${missingWords}${issue?', breaks a rule: '+errs[0].message:away?', absent':''}${wish==='against'?', goes against their wish':wish?', grants their wish':''}`});
      chip.className=`duty-${seenChip(s,p.person_id,chip)}${away?' absent':''}${removed.has(p.id)?' proposed-remove':''}${fresh?' is-new':''}`;
      // The one person pill (person_pill.ts): this chip's own name and listeners, the pill's look and issue marker.
      // My duties (a read-only grid with only you on it): the chip says when and where, not your own name (S-12).
      if(away)key.mark(chip,'absent',el('span',{class:'pchip absent lg-mark','aria-hidden':'true'},'Name'),'Absent: leave or rotation');
      if(removed.has(p.id))key.mark(chip,'remove',el('span',{class:'pchip proposed-remove lg-mark','aria-hidden':'true'},'Name'),'Proposed removal');
      const own=hooks.open&&p.person_id===ctx.me.person_id&&s.participants.length===1;
      personPill({base:chip,label:own?ownWords(s):who,full:fullNameOf(person),issues,key,person:own?undefined:p.person_id,
        act:()=>hooks.open?hooks.open(s):openPanel({kind:'person',shift:s.id,assignment:p.id}),
        now:()=>asNow(()=>{const x=sessionNow(s.id),q=x?.participants.find((y:Row)=>y.id===p.id);return x&&q?personIssues(x,q,boxed):[];}),
        extra:[...(wish?[key.mark(wishMark(wish),'wish-'+wish,wishMark(wish),wish==='against'?'Goes against their wish':'Grants their wish')]:[]),...pillParts(s,p.person_id)]});
      // Its name already says the first broken rule; the could-improve marks its marker shows are said after it.
      const advisory=issues.filter(i=>i.severity==='warning');if(advisory.length)chip.setAttribute('aria-label',chip.getAttribute('aria-label')+'. '+issueLabel(advisory));
      const target:RuleTarget={kind:'assignment',person:p.person_id,shift:s.id,day:dayOf(s),label:fullNameOf(person),sub:`${s.type_snapshot.name} · ${time(s)}`};
      tag(chip,target,{own:true});
      // Its menu (right-click, Shift+F10, or the ⋯ shown on hover, SR-38) and its click are the grid's (nameEvent).
      if(menus)chip.append(el('span',{class:'pchip-more','aria-hidden':'true'},icon('dots')));
      // Its card is the grid's (cardOf › nameCard), built when shown from the draft as held now.
      members.push(hooks.editable&&!s.locked?hooks.dnd(chip,p,fullNameOf(person)):chip);
    }
    return teamBox(s,team,members,problem);},n=>n));
    const others=hiddenChip(s);if(others)out.push(others);
    const adds=(hooks.proposal?.additions||[]).filter(x=>x.shift_id===s.id);
    for(const a of adds){
      // The one person pill too (its short forms in a narrow cell); the dashed edge and the key say it is proposed.
      const id=a.participant.person_id,full=fullNameOf(people.get(id));
      const add=personPill({base:el('span',{class:'proposed-add','aria-label':'Proposed: '+full,'data-tip':'Proposed: '+full}),hue:colour,label:'+ '+hooks.name(people.get(id)),full,person:id});
      seen(s,key.mark(add,'add',el('span',{class:'pchip proposed-add lg-mark','aria-hidden':'true'},'+ Name'),'Proposed addition'));
      // The session's mark too: Show on schedule finds the proposed people when the proposal fills its open places.
      out.push(tag(add,{kind:'session',shift:s.id,label:s.type_snapshot.name},{own:true}));}
    // With a proposal shown, the places it fills (or empties) are counted as it would leave them.
    const open=prop('open').on?Math.max(0,openBeside(c,boxShortfall(s))-adds.length+removed.size):0;
    if(open){
      const g=tag(openKey(el('button',{type:'button',class:'pchip gap','data-open':'fill','data-shift':s.id,'aria-label':`${open} place${open>1?'s':''} open on ${sessionWords(s)}. ${hooks.editable?'Click to fill':'View staffing details'}.`},el('span',{class:'pchip-text'},prop('open').form==='count'?`+${open}`:`+ ${open} open place${open>1?'s':''}`)),
        ),{kind:'session',shift:s.id,label:s.type_snapshot.name},{own:true});
      out.push(g);
    }else if(prop('open').on&&!adds.length&&roleGap(data,s.id,c)&&!boxTells(s)){
      // Every place filled, but a named role is missing (TL-02): said as such, never as an extra open place.
      const role=roleGap(data,s.id,c) as 'lead'|'role',words=ROLE_GAP_WORDS[role];
      const g=tag(el('button',{type:'button',class:'pchip gap role-gap','data-open':'fill','data-shift':s.id,'aria-label':`${words} on ${sessionWords(s)}; every place is filled. ${hooks.editable?role==='lead'?'Click to choose a team lead':'Click to see who can fill it':'View staffing details'}.`},
        el('span',{class:'pchip-text'},role==='lead'?(prop('open').form==='count'?MARKS.lead[0]+'?':'Choose team lead'):words)),
        {kind:'session',shift:s.id,label:s.type_snapshot.name},{own:true});
      markRoleGap(key,g,role,el('span',{class:'pchip gap role-gap lg-mark','aria-hidden':'true'},role==='lead'&&prop('open').form==='count'?MARKS.lead[0]+'?':'!'));
      out.push(g);
    }else if(!s.participants.length&&!adds.length){
      // An empty session with no minimum is still a target (SI-17): it opens the fill panel, read-only grids its details.
      const none=tag(el('button',{type:'button',class:'pchip empty','data-open':'fill-more','data-shift':s.id,'aria-label':`No one yet on ${sessionWords(s)}. ${hooks.editable?'Click to add someone':'View staffing details'}.`},el('span',{class:'pchip-text'},'No one yet')),{kind:'session',shift:s.id,label:s.type_snapshot.name},{own:true});
      out.push(none);}
    // The session's own issues (D78), one marker each place: its open-place findings beside its "1 open" place, every
    // other one at the cell's top right. A problem a team on it says is the team's marker's.
    const own=sessionIssues(s);
    // A must fix's Fix is its cause in the Fix list panel (D78 ↔ FIXLIST-UX), the same item the panel lists.
    const atCause=(fs:Row[])=>!hooks.open&&fs.some(f=>f.severity==='error')&&!!hooks.fixList?.({shift:s.id});
    if(own.length){
      const title=`${s.type_snapshot.name}, ${formatDate(s.start,zone)} ${time(s)}`,gapPill=out.find(n=>n.matches('.pchip.gap:not(.role-gap)'));
      const atOpen=gapPill?own.filter(f=>OPEN_PLACE_CODES.has(String(f.code))):[],rest=own.filter(f=>!atOpen.includes(f));
      // Read again when opened (the draft as held now), split the same way.
      const split=!!gapPill,now=(atGap:boolean)=>()=>asNow(()=>{const x=sessionNow(s.id);return x?sessionIssues(x).filter(f=>atGap===(split&&OPEN_PLACE_CODES.has(String(f.code)))).map(f=>findingIssue(f)):[];});
      if(gapPill&&atOpen.length)out[out.indexOf(gapPill)]=el('div',{class:'wk-open-row'},gapPill,
        issueMarker(atOpen.map(f=>findingIssue(f)),{title,key,now:now(true),act:()=>atCause(atOpen)||(hooks.open?hooks.open(s):openPanel({kind:'fill',shift:s.id}))}));
      if(rest.length)out.push(el('span',{class:'wk-cell-mark'},issueMarker(rest.map(f=>findingIssue(f)),{title,key,now:now(false),act:()=>atCause(rest)||(hooks.open?hooks.open(s):openPanel({kind:'session',shift:s.id}))})));
    }
    return out;
  }

  // Weekend columns (Friday and Saturday) are shaded in every row that has one cell per day.
  // Today is tinted, and in longer ranges a firmer line marks where each week starts.
  const today=toLocal(new Date().toISOString(),zone).slice(0,10),firstWeekday=((ctx.me.organisation.week_start??6)+1)%7;
  const colClass=days.map((d,i)=>[isWeekend(d)?'weekend':'',d===today?'today':'',i&&days.length>7&&new Date(d+'T12:00:00Z').getUTCDay()===firstWeekday?'wk-weekstart':''].filter(Boolean));
  for(const row of grid.querySelectorAll<HTMLElement>(':scope>.wk-row'))if(row.childElementCount===days.length+1)[...row.children].slice(1).forEach((c,i)=>{c.classList.add(...colClass[i]);
    if(isWeekend(days[i]))weekendMark(c);if(days[i]===today)markToday(key,c);});
  // Keyboard (S-17, the APG grid pattern): the grid is one tab stop. Arrows move to the nearest day header, row head,
  // name or open place in that direction; Home/End to the first/last in its row, Ctrl+Home/End to the grid's first and
  // last; Enter opens it; Shift+F10 or the menu key opens its menu (the ⋯ buttons stay for the mouse, out of the tab order).
  for(const n of groupHeads())sizes?.observe(n);
  const scroller=el('div',{class:'wk-scroller','aria-label':'Week grid · scrollable'},grid);
  roving(grid,scroller);
  // x ticks the focused session (its name, open place or box); Shift+x ticks every session from the last one ticked, in
  // grid order (SI-13). Esc closes the innermost layer: the help card (dom.ts), the selection (selection_bar.ts), then the
  // session panel (SI-12).
  let anchor='';
  grid.addEventListener('keydown',e=>{
    if(e.key==='Escape'){if(!e.defaultPrevented&&!hooks.selected.size&&panel){e.preventDefault();closeWeekPanel();}return;}
    if(e.key.toLowerCase()!=='x'||e.ctrlKey||e.metaKey||e.altKey)return;
    const t=e.target as HTMLElement;let id=t.dataset.shift||'';
    if(!id)try{id=String(JSON.parse(t.closest<HTMLElement>('[data-rk]')?.dataset.rk||'{}').shift||'');}catch{id='';}
    const boxes=[...grid.querySelectorAll<HTMLInputElement>('input.wk-check')],at=boxes.findIndex(b=>b.dataset.shift===id);if(at<0)return;
    e.preventDefault();const from=e.shiftKey?boxes.findIndex(b=>b.dataset.shift===anchor):-1;
    const range=from<0?[boxes[at]]:boxes.slice(Math.min(from,at),Math.max(from,at)+1),on=from<0?!boxes[at].checked:true;
    for(const b of range)if(b.checked!==on){b.checked=on;b.dispatchEvent(new Event('change',{bubbles:true}));}
    anchor=id;});
  layout.prepend(scroller);
  root.append(grid_key.root);
  return root;
}

/** The roving item focused last, by its name (focusKey): a grid drawn again gives its Tab stop back to it (maintenance). */
let lastRoving='';
/** One tab stop for a grid of controls (S-17, the APG grid's roving tabindex): the item last focused (else today's first
 *  session, else the first item) keeps tabindex 0, the others -1; arrows move by position, Home/End within the row,
 *  Ctrl+Home/End to the ends, Shift+F10 / the menu key open its menu. Controls added later (Month's "+N more") join the
 *  roving set. A grid with no controls lets its scroller take focus. A column's resize grip (WAI-ARIA window splitter,
 *  its own ← →) is no grid item: it follows its heading in the Tab order while that heading holds the stop (maintenance).
 *  Every Schedule role=grid (Week, Month, People, Timeline, Counts) uses this one (tests/test_web_grid_keyboard.py). */
export function roving(grid:HTMLElement,scroller:HTMLElement){
  const focusable='button,a[href],input,select,textarea,[tabindex]';
  const grip=(n:Element)=>n.classList.contains('dt-resize');
  // In the roving set: not a grip, not a ⋯ (a mouse affordance), not disabled, not inside something hidden.
  const member=(n:HTMLElement)=>!grip(n)&&!n.closest('.wk-more,[hidden]')&&!n.matches(':disabled');
  const items=()=>[...grid.querySelectorAll<HTMLElement>(focusable)].filter(n=>member(n)&&n.getClientRects().length>0);
  let current:HTMLElement|null=null;
  const settle=()=>{const all=[...grid.querySelectorAll<HTMLElement>(focusable)];
    // The focused item drawn again in place (a live run, a "+N more"): focus goes to its successor, never to the page.
    const lost=!!current&&!current.isConnected&&(!document.activeElement||document.activeElement===document.body);
    if(!current||!grid.contains(current)){const pool=all.filter(member);
      const cell=(n:HTMLElement)=>!!n.closest('[role=gridcell]');
      current=pool.find(n=>lastRoving&&focusKey(n)===lastRoving)||pool.find(n=>cell(n)&&n.closest('.today'))||pool.find(cell)||pool[0]||null;}
    for(const n of all)n.tabIndex=(grip(n)?n.parentElement===current:n===current)?0:-1;
    if(current)scroller.removeAttribute('tabindex');else scroller.tabIndex=0;
    if(lost)current?.focus({preventScroll:true});};
  // Drawn after this returns (the grid is filled in, then mounted): settle once it is in the page, and again on changes.
  // Once per frame however many changes (SI-39/45: a live run or the rule marks change the grid many times a frame).
  // Settles a still-detached grid too: one mounted frames after it was filled gets no later mutation to settle it.
  let queued=false;const later=()=>{if(queued)return;queued=true;requestAnimationFrame(()=>{queued=false;settle();});};
  later();
  new MutationObserver(later).observe(grid,{childList:true,subtree:true});
  const reveal=(n:HTMLElement)=>n.scrollIntoView({block:'nearest',inline:'nearest'});
  grid.addEventListener('focusin',e=>{
    const t=e.target as HTMLElement;if(t.closest('.wk-more')||grip(t))return;
    // Tab can return to a cell covered by a sticky heading. A redraw restoring focus keeps its scroll position.
    if(t===current&&e.relatedTarget instanceof Node&&!grid.contains(e.relatedTarget)&&t.matches(':focus-visible'))reveal(t);
    lastRoving=focusKey(t);if(t!==current){current=t;settle();}
  });
  const centre=(r:DOMRect)=>[r.left+r.width/2,r.top+r.height/2];
  const rowOf=(n:HTMLElement)=>{const r=n.getBoundingClientRect();return items().filter(m=>{const q=m.getBoundingClientRect();return q.top<r.bottom-2&&q.bottom>r.top+2;})
    .sort((x,y)=>x.getBoundingClientRect().left-y.getBoundingClientRect().left);};
  grid.addEventListener('keydown',e=>{
    const from=(e.target as HTMLElement).closest<HTMLElement>('button,[tabindex]');if(!from||grip(from)||from.closest('.wk-more')||!grid.contains(from))return;
    if(e.key==='ContextMenu'||e.key==='F10'&&e.shiftKey){
      // The item's own menu: its right-click handler, or the ⋯ of the head it sits in.
      const host=from.closest<HTMLElement>('.wk-day,.wk-rowhead,.wk-cell')||from;
      const r=from.getBoundingClientRect();e.preventDefault();
      if(!from.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:r.left,clientY:r.bottom}))){return;}
      host.querySelector<HTMLElement>('.wk-more')?.click();return;
    }
    let to:HTMLElement|undefined;
    if(e.key==='Home'||e.key==='End'){const pool=e.ctrlKey?items():rowOf(from);to=e.key==='Home'?pool[0]:pool.at(-1);}
    else{
      const dir=({ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]} as Record<string,number[]>)[e.key];if(!dir)return;
      const [ax,ay]=centre(from.getBoundingClientRect());let score=Infinity;
      for(const b of items()){if(b===from)continue;const [bx,by]=centre(b.getBoundingClientRect()),dx=bx-ax,dy=by-ay;
        const along=dx*dir[0]+dy*dir[1],across=Math.abs(dx*dir[1])+Math.abs(dy*dir[0]);if(along<=1)continue;const d=along+across*3;if(d<score){score=d;to=b;}}
    }
    if(to&&to!==from){e.preventDefault();to.focus();reveal(to);}
  });
}

function helpPanel(close:()=>void){
  const items=[['Draft and status','The header names the draft you edit and its workflow. Published days around it are grey and read-only; an accent edge marks where the draft starts. The status footer shows open places, what must be fixed, overlaps and the score. Draft edits are visible only to planners.'],
    ['Choose a view','The saved view comes first in the toolbar. View settings (the sliders) chooses the layout (Calendar, Timeline, People, Coverage, List) and the range (Day to Month), how people are named, and the layers: published, one draft (or two side by side, or overlaid), leave and wishes; Open the year overview shows the year. In Year and Month a solid dot is published, a hollow one a draft, a half one two rotas on a day; colour is health. On phones, Agenda lists sessions by day; Grid groups them by service, duty type or location.'],
    ['People, Coverage and List','People groups duties by person. Coverage compares places filled with places needed. List shows each session as a row, card or board item.'],
    ['Fill open places','Select an open place in Grid, a short Coverage cell, or Fill open place in a List row menu. Resolve works through open places and what must be fixed, one by one.'],
    ['Keyboard','The grid is one Tab stop. Arrow keys move between day headers, rows, names and open places; Home and End go along a row, Ctrl+Home to the first heading; Enter opens one; Shift+F10 opens its menu. Tab from a heading reaches its resize grip, which ← → resize. In a fill panel, type to find a person. Esc closes the panel.'],
    ['Names and tags',`Grid chips identify people. Small tags mark trainees (${MARKS.tr[0]}), supervisors (${MARKS.sup[0]}), ${MARKS.lead[1].toLowerCase()}s (${MARKS.lead[0]}) and overtime (${MARKS.ot[0]}). A trainee's chip follows their supervisor's, marked ↳. A red ! means a rule is broken. Grid colour follows your View setting.`],
    ['Why someone can or cannot work','Open a session and choose Why: everyone, grouped as assigned, could work here, blocked by a rule, unavailable or not eligible, with the reason. A person shows one person across the sessions in view. Rules lists the rules for the session.'],
    ['Change things from here','Open session details to edit a draft. Grid and People shortcuts can add sessions, record leave or propose rules and exceptions. They use the same forms as Configuration.'],
    ['Your views',`Press ${SCHED_VIEWS.slice(0,-1).map(v=>v.letter).join(', ')} or ${SCHED_VIEWS.at(-1)!.letter} to switch views; Saved views keep a view with its filters. ‹ Today › moves through time across every rota.`]];
  const x=el('button',{type:'button',class:'icon-button bare','aria-label':'Close help','data-tip':'Close · Esc'},icon('close'));x.addEventListener('click',close);
  const aside=el('aside',{class:'wk-panel','aria-labelledby':'wk-help-title'},el('header',{class:'wk-panel-head'},el('h2',{id:'wk-help-title'},'How to read this schedule'),x),
    el('ol',{class:'wk-help'},...items.map(([t,b],i)=>el('li',{},el('span',{class:'wk-num'},String(i+1)),el('span',{},el('strong',{},t),el('span',{},b))))));
  aside.addEventListener('keydown',e=>{if(e.key==='Escape'){e.stopPropagation();close();}});return sidePanel(aside,{key:'schedule',dismiss:close});
}

const LEAD_FAILURE:Record<string,string>={LEAD_NOT_QUALIFIED:'Does not meet who may lead this duty',LEAD_NOT_ENABLED:'This duty has no team lead at that time'};
/** TL-02: every place is filled but no one (qualified) leads. Name a person already on the session as team lead: the
 *  server's dated lead context says who may (TL-03, rotation/cohort/programme dates included); the change is the
 *  ordinary validated session edit with Undo, which checks it again. Pending, failed and out-of-date answers enable nothing. */
function leadPicker(ctx:Context,data:WorkspaceData,shift:Row,hooks:WeekHooks,people:Map<string,Row>):HTMLElement{
  const w=data.workspace,ids=[...new Set((shift.participants as Row[]).map(p=>p.person_id as string))];
  const box=el('section',{class:'wk-lead','aria-labelledby':'wk-lead-title','aria-busy':'true'},el('h3',{class:'wk-h3',id:'wk-lead-title'},'Choose team lead'));
  const body=el('div',{'aria-live':'polite'},working('Checking who may lead this session…'));box.append(body);
  const input:LeadContextInput={shift:{start:shift.start,end:shift.end,assignment_type_id:shift.assignment_type_id,location_id:shift.location_id,service_ids:shift.service_ids,
    attributes:shift.attributes||{},participants:(shift.participants as Row[]).map(keep) as LeadContextInput['shift']['participants']},shift_id:shift.id,person_ids:ids};
  const draw=(st:LeadState)=>{box.setAttribute('aria-busy','false');
    if(st.state==='failed'){const {error}=st;body.replaceChildren(failedNote(`Couldn’t check who may lead. ${error.message}`.trim(),error,()=>{body.replaceChildren(working('Checking who may lead this session…'));load();}));return;}
    if(st.state==='stale'){body.replaceChildren(note('The rota changed since this was checked.','warning'),button('Check again',()=>void hooks.refresh(),'secondary small'));return;}
    if(st.state!=='ready')return;
    const need=leadNeed(st.context);
    const sources=[...new Map(st.context.segments.filter(x=>x.enabled).flatMap(x=>x.sources).filter(x=>x.policy_id).map(x=>[x.policy_id+'/'+(x.line_id||''),x])).values()];
    const rows=ids.map(pid=>{const who=fullNameOf(people.get(pid))||'Unnamed',me=st.context.people.find(x=>x.person_id===pid),leads=(shift.participants as Row[]).some(p=>p.person_id===pid&&p.team_lead);
      const why=me&&!me.can_lead?[...new Set(me.failures.map(f=>LEAD_FAILURE[f.code]||f.code))].join(' · '):'';
      const pick=me?.can_lead&&!leads&&hooks.editable&&!shift.locked?button('Make team lead',()=>void choose(pid,who,pick!,need.maximum),'primary small'):null;
      return el('li',{class:'wk-cand '+(me?.can_lead?'ok':'warn')},el('div',{class:'wk-cand-top'},avatar(who,'duty'),el('strong',{},who),
        leads?el('abbr',{class:'wk-tag lead','data-tip':MARKS.lead[1]},MARKS.lead[0]):'',pick||''),
        why||!me?el('p',{class:'wk-why'},why||'Not checked'):leads?el('p',{class:'wk-why'},'Leads now · does not meet who may lead'):'');});
    body.replaceChildren(
      el('p',{class:'wk-intro'},need.wholeDuty?'One person leads the whole duty.':'A lead is needed throughout the duty.',' ',need.maximum===1?'Choosing someone replaces any current lead.':''),
      need.conflict?note('The lead rules for this duty contradict each other or nobody may lead it. Check the rule.','warning'):'',
      el('ul',{class:'wk-cands'},...rows),
      el('p',{class:'wk-h3-row'},...sources.map(x=>el('a',{href:`#rules/${encodeURIComponent(x.policy_id!)}${x.line_id?'/'+encodeURIComponent(x.line_id):''}`},'Who may lead')),
        el('a',{href:'#insights/fairness'},'Team lead turns in Fairness')));
  };
  async function choose(pid:string,who:string,b:HTMLButtonElement,most:number|null){
    box.querySelectorAll('button').forEach(x=>(x as HTMLButtonElement).disabled=true);b.replaceChildren(savingMark('Saving'));
    // One lead for the whole duty: every row of the chosen person leads, and (most 1) nobody else does.
    const parts=(shift.participants as Row[]).map(p=>({...keep(p),team_lead:p.person_id===pid||(most!==1&&!!p.team_lead)})) as unknown as Row[];
    try{const issues=await saveParticipants(w,shift,parts);
      panel={workspace:w.id,kind:'fill',shift:shift.id,flash:`${who} leads this session.${rotaWords(issues)}`};await hooks.refresh();}
    catch(error){box.querySelectorAll('button').forEach(x=>(x as HTMLButtonElement).disabled=false);b.textContent='Make team lead';notify((error as Error).message,'error',undefined,error);}
  }
  const load=()=>leadContext(w.id,w.revision,input).then(c=>draw(leadState(c,w.revision))).catch(e=>draw({state:'failed',error:e as Error}));
  load();
  return box;
}
/** maintenance: someone added or swapped in leads only when the server's rota check says this session is short of a team
 *  lead (a LEAD_GAP shortfall, required above available, as the server's own trial reads it) and they may lead. Otherwise
 *  they join without the role, as fill options checked them; Make team lead names one. No lead rule is guessed here. */
export const joinsAsLead=(w:Row,shiftId:string,candidate:Row)=>!!candidate.can_lead&&((w.validation?.findings||[]) as Row[])
  .some(f=>f.code==='LEAD_GAP'&&f.shift_id===shiftId&&(f.detail?.required??0)>(f.detail?.available??0));
/** Saves one checked fill-options candidate into a draft session (an ordinary undoable, fingerprint-bound edit) and
 *  returns the rota's mandatory issue count after it (null: the reply checked only the edit). Used by the fill panel
 *  and the auto-schedule review's "Use …". */
export async function assignCandidate(w:Row,shift:Row,candidate:Row,result:Row,overrideReason=''):Promise<number|null>{
  // A supervised trainee goes in as the server trialled them (supervision.proposal: their named supervisor, times and
  // flags), saved only if the rota's inputs are still the ones it was checked against; with several qualified
  // supervisors one is chosen first (never guessed here).
  const plan=candidate.supervision as StaffingSupervisionPlan|null|undefined;
  if(plan&&!plan.proposal)throw new Error(plan.choices.length?`Choose who supervises ${nameOf(candidate)} first.`:'No qualified supervisor is available for this placement.');
  const fingerprint=assignFingerprint(candidate,result);
  if(plan&&!fingerprint)throw new Error('Open the session again to check the named supervisor before assigning.');
  const leaving=shift.participants.find((p:Row)=>p.id===result.replace_assignment_id);
  const added=plan?.proposal?{...plan.proposal}:{...result.slot,person_id:candidate.person_id,fixed:false,supervisor_person_id:null,team_lead:joinsAsLead(w,shift.id,candidate)};
  const back=onGrid(shift,[...(leaving?[leaving.person_id]:[])],nameOf(candidate));
  // Use the candidate check's binding token so changed scheduling inputs refuse the save.
  return saveParticipants(w,shift,result.replace_assignment_id?shift.participants.map((p:Row)=>p.id===result.replace_assignment_id?added:keep(p)):[...shift.participants.map(keep),added],overrideReason,
    fingerprint).catch(e=>{back();throw e;});
}
/** Takes one person off a draft session (an ordinary undoable edit); returns the rota's mandatory issue count after it
 *  (null: the reply checked only the edit). */
export function removeAssignment(w:Row,shift:Row,assignment:string){
  const back=onGrid(shift,[shift.participants.find((p:Row)=>p.id===assignment)?.person_id]);
  return saveParticipants(w,shift,shift.participants.filter((p:Row)=>p.id!==assignment).map(keep)).catch(e=>{back();throw e;});
}
/** Optimistic grid: at once, the people leaving a session read as saving (spinner, dotted underline) and the person
 *  joining shows on it, saving; the redraw after the save replaces both. A refusal puts the session back as it was. */
function onGrid(shift:Row,leaving:(string|undefined)[],joining=''):()=>void{
  const chips=[...document.querySelectorAll<HTMLElement>(`#main .pchip[data-rk*='"shift":"${shift.id}"']`)];
  const going=chips.filter(c=>leaving.includes(c.dataset.person));going.forEach(c=>c.classList.add('is-saving'));
  const pending=joining&&chips[0]?el('span',{class:'pchip is-saving is-pending'},savingMark(joining)):null;
  if(pending)chips[chips.length-1].after(pending);
  // A refusal puts the session back and marks it for a few seconds beside its names (SI-29); the toast says why.
  return()=>{going.forEach(c=>c.classList.remove('is-saving'));pending?.remove();
    const last=chips.filter(c=>c.isConnected).at(-1);if(!last)return;
    const mark=failedMark();last.after(mark);window.setTimeout(()=>mark.remove(),6000);};
}
type Duty={s:Row;parts:[number,number][]};
/** The Planner's sessions: `rota` is everything shown to this account (person facts); `unfiltered` is what the grid
 *  starts from, after My duties (`mine`: the viewer's person id) and the draft-only List view, before the toolbar's filters. */
export function plannerSessions(shifts:PlannerSession[],mine:string|null,listDraft:boolean){
  const base=listDraft?shifts.filter(s=>!s.context):shifts;
  return {rota:shifts,unfiltered:mine?base.filter(s=>s.participants.some(p=>p.person_id===mine)):base};
}
/** A person's attended intervals (ms) per session of a rota: presence segments, else their own start–end, else the
 *  session's (the same intervals as packages/domain/participation.py); published context and closed sessions excluded. */
export function attendance(shifts:Row[],pid:string):Duty[]{
  return shifts.filter(x=>!x.context&&!isClosed(x)).flatMap(x=>x.participants.filter((p:Row)=>p.person_id===pid).map((p:Row)=>({s:x,
    parts:(p.segments?.length?p.segments:[{start:p.start||x.start,end:p.end||x.end}]).map((part:Row)=>[Date.parse(part.start),Date.parse(part.end)] as [number,number])})));
}
/** Attended hours in the session's week and the whole rota, and hours rested since the last attended interval ended. */
export function workFacts(mine:Duty[],s:Row,dayOf:(s:Row)=>string,weekFrom:number):{week:number;rota:number;rest:number|null}{
  const hours=(list:Duty[])=>Math.round(list.reduce((n,x)=>n+x.parts.reduce((m,[a,b])=>m+(b-a)/36e5,0),0)*10)/10;
  const day=dayOf(s),from=addDays(day,-((new Date(day+'T12:00:00Z').getUTCDay()-weekFrom+7)%7)),to=addDays(from,7);
  const own=mine.find(x=>x.s.id===s.id),parts=mine.flatMap(x=>x.parts);
  let start=own?Math.min(...own.parts.map(p=>p[0])):Date.parse(s.start),joined;
  // Back-to-back attendance (08–12 then 12–16) is one stretch of work: rest is counted before the stretch began.
  while((joined=parts.filter(([a,b])=>a<start&&b>=start)).length)start=Math.min(...joined.map(p=>p[0]));
  const ends=parts.map(p=>p[1]).filter(t=>t<=start);
  return {week:hours(mine.filter(x=>{const d=dayOf(x.s);return d>=from&&d<to;})),rota:hours(mine),rest:ends.length?(start-Math.max(...ends))/36e5:null};
}
const keep=(p:Row)=>({person_id:p.person_id,start:p.start,end:p.end,fixed:!!p.fixed,participation:p.participation||'staff',overtime:!!p.overtime,supervisor_person_id:p.supervisor_person_id||null,team_lead:!!p.team_lead,segments:p.segments||[]});
/** Saves a session's people: one request. The edit re-checks the whole rota and its reply says what changed (the
 *  Schedule patches it in place); the draft's revision makes a stale edit refused, never merged. */
async function saveParticipants(w:Row,shift:Row,participants:Row[],overrideReason='',fingerprint=''):Promise<number|null>{
  const payload={workspace_id:w.id,shift_id:shift.id,assignment_type_id:shift.assignment_type_id,location_id:shift.location_id,service_ids:shift.service_ids,start:shift.start,end:shift.end,attributes:shift.attributes||{},participants,...(overrideReason?{override_reason:overrideReason}:{})};
  const saved=await command<Row>('shift.update',payload,undefined,w.revision,fingerprint||undefined);
  // A provisional reply (scope command_changes) checked only this edit: it has no count for the whole rota.
  return serverChecking(saved.validation)?null:saved.validation?.error_count??0;
}
/** What a save's reply says of the whole rota: nothing when it checked only the edit (the complete check follows). */
const rotaWords=(issues:number|null)=>issues===null?'':' '+(issues?mustWords(issues)+' left in the rota.':'Rota check passed.');
interface PanelInfo{time:string;day:string;place:string;cover:Cover|undefined;rulesFor:(s:Row)=>Promise<Row>;close:()=>void;open:(p:Omit<Panel,'workspace'>)=>void;range:Row[];
  /** Moves the schedule's ring to what the panel now explains, without drawing anything again (SI-40). */highlight:()=>void}
function sessionPanel(ctx:Context,data:WorkspaceData,shift:Row,state:Panel,hooks:WeekHooks,info:PanelInfo){
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p]));
  const open=openPlaces(info.cover);
  const replacing=state.replace?shift.participants.find((p:Row)=>p.id===state.replace):undefined;
  // A selected person titles their panel; the session it is on is the line under it (REQ-PANEL).
  const chosen=state.kind==='person'?shift.participants.find((p:Row)=>p.id===state.assignment):undefined;
  const title=state.kind==='fill'&&replacing?`Replace ${fullNameOf(people.get(replacing.person_id))}`:chosen?fullNameOf(people.get(chosen.person_id))||shift.type_snapshot.name:shift.type_snapshot.name+' · '+info.place;
  const when=`${chosen?shift.type_snapshot.name+' · '+info.place+' · ':''}${info.day} · ${info.time}${isClosed(shift)?' · closed':open?` · ${open} place${open>1?'s':''} open`:''}${shift.locked?' · locked':''}`;
  // The Session tab (who works it, or who can fill it); Why and Rules are built when first opened.
  const body=el('div',{class:'wk-tab'}),panelBody=el('div',{class:'wk-panel-body'});
  // One session panel (S-09): from a person or an open place, "Whole session" shows this panel's session mode.
  const details=state.kind==='session'?null:el('button',{type:'button',class:'link-button'},'Whole session');details?.addEventListener('click',()=>info.open({kind:'session',shift:shift.id}));
  const acts=state.kind==='session'?hooks.sessionActions?.(shift):undefined;
  const more=acts?.more.length?el('button',{type:'button',class:'icon-button','aria-label':'More session actions','aria-haspopup':'menu','aria-expanded':'false','data-tip':'More session actions','data-panel-session':shift.id},icon('dots')):null;
  // D-057: a redraw (a check landing, a save's refresh) replaces this panel; the open menu moves to the new panel's
  // button for the same session and runs that draw's actions, and closes when the session's panel is gone.
  const moreItems=()=>(acts?.more||[]).map(m=>({...m,run:()=>{info.close();m.run();}}));
  if(more){menuSource(more,moreItems);
    more.addEventListener('click',()=>openMenu(more,'More session actions',moreItems,{placement:'above-start',keep:`.wk-panel button[data-panel-session="${CSS.escape(shift.id)}"]`}));}
  // An accent edge says what opened the panel; the schedule rings the same session (the note shows when it does).
  // The shared panel head (SI-38): "Session 3 of 12" with ↑ ↓ through the sessions in view, in time order.
  const at=info.range.findIndex(s=>s.id===shift.id);
  // No hue tile beside the title: the title names the session, and its hue is on its people's pills, in the panel's key.
  const head=panelHead({title,sub:when,id:'wk-panel-title',close:info.close,
    position:at<0?undefined:{index:at,total:info.range.length,noun:'session',go:i=>{const next=info.range[i];if(next)info.open({kind:'session',shift:next.id,tab:state.tab,why:state.why});}}});
  head.classList.add('wk-panel-head');
  const aside=el('aside',{class:'wk-panel',role:'dialog','aria-modal':'false','aria-labelledby':'wk-panel-title'},
    head,
    panelBody,el('footer',{class:'wk-panel-foot'},acts?.edit?button('Edit session',acts.edit,'primary small'):'',details||'',
      // One click, Undo in the message: a draft lock needs no reason or preview.
      hooks.editable&&hooks.lock&&!isClosed(shift)?withIcon(button(shift.locked?'Unlock':'Lock',()=>hooks.lock!(shift,!shift.locked),'secondary small',
        {'aria-pressed':String(!!shift.locked),...(hooks.sessionsHeld?{'aria-disabled':'true','data-tip':onlyRotaCoordinator('lock')}
          :{'data-tip':shift.locked?'Locked: auto-schedule keeps it as it is. Unlock to change it.':'Lock it so auto-schedule keeps it as it is'})}),'lock'):null,more||'',
      acts&&hooks.sessionsHeld?el('p',{class:'hint sessions-held'},onlyRotaCoordinator('add')):null));
  const target:RuleTarget={kind:'session',shift:shift.id,label:shift.type_snapshot.name,sub:`${info.day} · ${info.time}`};
  // The Rules tab's own key: its rows' state and issue marks (every mark has one).
  const rulesPane=el('div',{class:'wk-tab'}),rulesKey=legend('Key'),drawRules=()=>{rulesKey.clear();rulesPane.replaceChildren(sessionRules(target,drawRules,rulesKey),rulesKey.root);};
  const whyHooks={editable:(s:Row)=>hooks.editable&&!s.locked&&!isClosed(s),code:hooks.code,rules:hooks.why,redraw:()=>redrawCurrent?.(undefined,true),
    open:(s:Row)=>info.open({kind:'fill',shift:s.id,tab:'why'}),
    assign:async(s:Row,r:Row)=>{await assignCandidate(data.workspace,s,r,{slot:{start:s.start,end:s.end,participation:'staff',overtime:false,segments:[]},replace_assignment_id:null} as unknown as Row);await hooks.refresh();},
    chooseSupervisor:(s:Row)=>info.open({kind:'fill',shift:s.id}),
    remove:async(s:Row,r:Row)=>{await removeAssignment(data.workspace,s,r.assignment_id);await hooks.refresh();}};
  panelBody.append(tabs('Session panel',[['session','Session',()=>body],['why','Why',()=>whyTab(ctx,data,shift,info.range,state.why||(state.why={}),whyHooks)],
    ['rules','Rules',()=>{drawRules();return rulesPane;}]],state.tab||'session',k=>{const was=state.tab||'session';state.tab=k as Panel['tab'];
      // Why › A person rings that person's duties instead of the session: the schedule follows the tab.
      // The Session tab is built only when shown (it checks everyone for a fill): opening it later draws it.
      // Only the Session tab may need drawing; the ring just moves (SI-40).
      if(was!==k&&k==='session'&&!body.childElementCount)redrawCurrent?.(undefined,true);else if(was!==k)info.highlight();}));
  aside.addEventListener('keydown',e=>{if(e.key==='Escape'){e.stopPropagation();info.close();}});sidePanel(aside,{key:'schedule',dismiss:info.close});
  if((state.tab||'session')!=='session')return aside;
  // An Assign's own result stays through the redraws its check brings (same revision); any other result shows once.
  const saved=state.flashRev!==undefined,own=saved&&state.flashRev===Number(data.workspace.revision);
  if(state.flash&&(own||!saved)){body.append(el('div',{class:'wk-flash',role:'status'},icon('check'),state.flash));if(!saved)state.flash=undefined;}
  // The fill panel's one status slot (its height kept when empty): Saving…, then the whole-rota check behind its edit.
  const slot=el('p',{class:'wk-save-slot',role:'status'},own&&(serverChecking(data.workspace.validation)||!!hooks.checking)&&!hooks.checkEnded?.()?'Saved · checking the rest of the rota…':'');
  if(state.kind==='fill')body.append(slot);
  if(state.kind==='session'){
    body.append(sessionSummary(ctx,shift,{validation:data.workspace.validation as Row|undefined,name:id=>fullNameOf(people.get(id)),code:id=>hooks.name(people.get(id)),refresh:hooks.refresh,fresh:hooks.fresh}));
    return aside;
  }
  // SW3-A-15: a double booking's own fix is taking the person off one session; closing a session stays beside it.
  const clash=clashNote(shift,hooks.refresh,hooks.editable&&!shift.locked&&!isClosed(shift)?c=>{const p=(shift.participants as Row[]).find(x=>x.person_id===c.person_id);
    return p&&!heldFixed(hooks,p)?()=>void removeAssignment(data.workspace,shift,p.id).then(()=>hooks.refresh()).catch(reportError):undefined;}:undefined);if(clash)body.append(clash);
  if(state.kind==='person'){
    const p=shift.participants.find((x:Row)=>x.id===state.assignment);
    if(!p){body.append(note('This person is no longer on this session.'));return aside;}
    const person=people.get(p.person_id),tags=[p.participation==='trainee'?'Trainee':p.participation==='supervisor'?'Supervisor':'',p.team_lead?'Team lead':'',p.overtime?'Overtime':'',...((person?.tags||[]) as string[])].filter(Boolean);
    const editable=hooks.editable&&!shift.locked&&!isClosed(shift)&&!heldFixed(hooks,p);
    const replace=editable?button('Replace',()=>info.open({kind:'fill',shift:shift.id,replace:p.id}),'secondary small'):null;
    body.append(el('h3',{class:'wk-h3'},'Working this session'),el('div',{class:'wk-person'},avatar(fullNameOf(person),'duty'),
      el('div',{},el('strong',{},personLink(p.person_id,fullNameOf(person),ctx.navigate)),el('small',{},[hooks.code(person),...tags].join(' · ')||'Full session'),
        (()=>{const words=pairWords(shift.participants,p.person_id,id=>fullNameOf(people.get(id))||'named supervisor'),away=teamOrder(shift.participants as Row[]).find(e=>e.p===p)?.missing;
          return words?el('small',{class:'wk-pair'},words.charAt(0).toUpperCase()+words.slice(1)+(away?' · not on this session':'')):null;})(),
        p.override_reason?el('small',{},'Overridden: '+p.override_reason):null),replace));
    if(heldFixed(hooks,p))body.append(note(FIXED_HELD,'info'));
    // SX-21: everyone else on it too, so a second person on the session is never hidden behind the one selected.
    const others=(shift.participants as Row[]).filter(x=>x.id!==p.id);
    if(others.length)body.append(el('p',{class:'wk-also'},el('span',{class:'muted'},'Also on this session: '),...others.flatMap((x,i)=>{
      const b=el('button',{type:'button',class:'link-button'},fullNameOf(people.get(x.person_id))||'Someone');b.addEventListener('click',()=>info.open({kind:'person',shift:shift.id,assignment:x.id}));
      return i?[', ',b]:[b];})));
    const problems=((data.workspace.validation?.findings||[]) as unknown as Row[]).filter(f=>f.shift_id===shift.id&&f.person_id===p.person_id);
    // The one issue list (issue_marker.ts issueList): the D78 icon, the plain words, Open all in Fix list.
    if(problems.length)body.append(el('h3',{class:'wk-h3'},'Needs attention'),issueList(problems.map(f=>findingIssue(f)),{limit:6}),
      editable&&problems.some(f=>f.severity==='error')?el('div',{class:'actions'},button('Find a replacement',()=>info.open({kind:'fill',shift:shift.id,replace:p.id}),'primary small')):'');
    const shortcuts=hooks.menu?.({shift,person_id:p.person_id,day:toLocal(shift.start,ctx.me.organisation.timezone).slice(0,10)})||[];
    if(shortcuts.length)body.append(el('h3',{class:'wk-h3'},'Change for '+fullNameOf(person)),el('div',{class:'menu-list wk-shortcuts',role:'menu','aria-label':'Changes for '+fullNameOf(person)},...shortcuts.map(m=>menuItem(m.label,m.icon,m.run,{},m.hint||''))));
    const rules=el('ul',{class:'rk-rules','aria-label':'Rules applied to this assignment'},el('li',{class:'rk-none'},skeleton('line','Loading the rules that apply…')));
    const all=el('a',{href:'#rules'},'All rules');
    const whyKey=legend('Key');  // the rows' state and issue marks
    body.append(el('div',{class:'wk-h3-row'},el('h3',{class:'wk-h3'},'Rules applied here'),all),rules,whyKey.root);
    info.rulesFor(shift).then(result=>{
      const mine=result.people.find((x:Row)=>x.person_id===p.person_id);
      const rows=(mine?.rules||[]) as Row[];
      // The same rule rows as the planner's rules panel: which rule wins, their number, and ways to change it.
      // A rule change redraws only a panel still on screen; the first draw does not wait for it: a cached result can
      // arrive while the view is still mounting this tree (one frame or several), and a skipped draw would leave it loading.
      const fill=()=>{whyKey.clear();const list=assignmentRules(result,shift,p.person_id,draw,whyKey);list.classList.add('wk-rk');rules.replaceChildren(...list.childNodes);};
      const draw=()=>{if(alive(rules))fill();};
      fill();
      // Only a draft is checked again; a rota in review or published shows the results it was checked with.
      if(!result.fresh&&data.workspace.status==='draft')rules.after(el('p',{class:'muted wk-stale'},'The rota changed since the last check. These results update when its check finishes.'));
      if(editable&&!problems.some(f=>f.severity==='error')&&rows.some((r:Row)=>r.status==='broken'))body.insertBefore(el('div',{class:'actions'},button('Find a replacement',()=>info.open({kind:'fill',shift:shift.id,replace:p.id}),'primary small')),rules.nextSibling);
    }).catch(error=>rules.replaceChildren(el('li',{},note((error as Error).message,'error',error))));
    return aside;
  }
  // Fill (or replace)
  // An eligible redraw synchronously rejoins its held ask below; an early return stops the previous session's ask.
  stopAsking('panel');
  if(!hooks.editable||shift.locked||isClosed(shift)){
    body.append(note(shift.locked?'This session is locked, so auto-schedule keeps it as it is. Unlock it (below) to change who works it.':isClosed(shift)?closureText(shift,id=>fullNameOf(people.get(id)))+'.':data.workspace.status!=='draft'?'This rota is in review, so it cannot be changed. Reopen it as a draft to make changes.':'Only coordinators can change a draft rota. Open the session details to see who is assigned.'));
    return aside;
  }
  if(replacing&&heldFixed(hooks,replacing)){body.append(note(FIXED_HELD,'info'));return aside;}
  const role=roleGap(data,shift.id,info.cover);
  // Opened for an issue (companion cover, a missing skill or supervisor…): what it is stays above the people, and the
  // rota's own re-check after each change says when it is gone (never guessed here: a stale check says nothing).
  const issue=state.replace?undefined:state.issue;
  if(issue){const checked=hooks.fresh&&!!data.workspace.validation,findings=((data.workspace.validation?.findings||[]) as unknown as Row[]);
    // A save can move the issue's key (its start or numbers change), so "met" needs the fresh full check to have no
    // finding of this rule and kind on this session at all; one that moved is said as still open, with its current words.
    const [code,rule]=issue.key.split('|'),same=findings.filter(f=>f.shift_id===shift.id&&f.code===code&&(f.rule_id||'')===rule);
    const exact=same.some(f=>issueKey(f)===issue.key),moved=checked&&!exact?same:[];
    body.append(!checked||same.length?el('div',{class:`notice ${(moved[0]||issue).severity==='error'?'error':'warning'} wk-issue`,role:'note'},el('strong',{},issue.title),
        ...(moved.length?[el('p',{},'Rota checked again: still open.'),...moved.slice(0,3).map(f=>el('p',{},[f.message,findingNumbers(f)].filter(Boolean).join(' · ')))]:issue.detail?[el('p',{},issue.detail)]:[]),
        el('p',{},'Pick someone this rule counts. The rota is checked again after each change.'),
        // A finding names its rule by policy id: Rules opens it through its source (the rule, its catalogue rule or the
        // person's scheduling fact), never as a catalogue address it is not (that read "This rule is not available").
        issue.rule_id?el('a',{href:'#rules?source='+encodeURIComponent(issue.rule_id)},'Open the rule'):'')
      :el('div',{class:'wk-flash',role:'status'},icon('check'),`${issue.title}: met on this session.`));}
  if(!open&&role==='lead'&&!state.replace&&!state.more){
    body.append(leadPicker(ctx,data,shift,hooks,people),el('div',{class:'actions'},button('Add another person anyway',()=>info.open({kind:'fill',shift:shift.id,more:true}),'ghost small')));
    return aside;
  }
  if(!open&&!state.replace&&!state.more&&!issue){
    if(role==='role'){body.append(note('Every place is filled, but a required skill or a named supervisor is missing. Add someone who has it, or edit the session.','warning'),
      button('Add another person',()=>info.open({kind:'fill',shift:shift.id,more:true}),'secondary small'));return aside;}
    const more=button('Add another person anyway',()=>{info.open({kind:'fill',shift:shift.id,more:true});},'secondary small');
    // SW3-A-09: who works it is shown (right after a Replace too), as the session's own panel shows them.
    body.append(el('div',{class:'wk-flash'},icon('check'),'Every place on this session is filled.'),
      sessionSummary(ctx,shift,{validation:data.workspace.validation as Row|undefined,name:id=>fullNameOf(people.get(id)),code:id=>hooks.name(people.get(id)),refresh:hooks.refresh,fresh:hooks.fresh,clash:false}),
      el('p',{class:'wk-intro'},'You can still add someone, for example a trainee or an extra pair of hands.'),more);
    return aside;
  }
  // Opened for an issue, the server also says per candidate whether adding them resolves it (`resolves`: true, false,
  // or null = not worked out); a key the fresh check no longer has (ISSUE_GONE) falls back to the ordinary list.
  // Per revision, as the server answers fill-options (expected_revision): the rota's own check after an edit moves the
  // revision, so an open panel asks once more then (LEAD-UIUX, 7 Nov 2026: no client key the server does not back).
  const w=data.workspace,findingKey=issue?.key||'',key=`${w.id}:${w.revision}:${shift.id}:${state.replace||''}:${findingKey}`;
  // The one ask (candidates.ts fillOptions): a job where workers serve jobs. Replace, the finding and a chosen supervisor
  // are fields of the same request (FillOptionsInput); the search filters the answer here (candidateList).
  const askBody=(finding:string,supervisor='')=>({workspace_id:w.id,shift_id:shift.id,expected_revision:w.revision,...(state.replace?{replace_assignment_id:state.replace}:{}),...(finding?{finding_key:finding}:{}),...(supervisor?{supervisor_person_id:supervisor}:{})});
  const held=asks.held('panel',key,f=>askBody(f),findingKey);
  const list=el('div',{class:'wk-cands','aria-live':'polite'},elapsed('Finding who can work it',held.started));
  const search=el('input',{type:'search',class:'wk-search','aria-label':'Find a person',placeholder:'Find a person or code',value:state.q||''}) as HTMLInputElement;
  body.append(search,list);
  // While this panel saves, a press on its held controls does nothing (capture: before their own handlers).
  list.addEventListener('click',e=>{if(list.getAttribute('aria-busy')==='true'&&(e.target as Element).closest('button')){e.preventDefault();e.stopPropagation();}},true);
  // Opened from the grid or Resolve: typing goes straight to the search (unless another field has focus). Only on the
  // panel's first draw: a redraw keeps the focus where the person is (D-057), e.g. a linked chip (G-05).
  if(!state.searched){state.searched=true;setTimeout(()=>{const a=document.activeElement;if(search.isConnected&&(!a||a===document.body||a.closest('.wk-grid,.ag,.wk-panel,.planner-bar')))search.focus({preventScroll:true});},0);}
  const draw=(result:Row)=>candidateList({result,list,search,code:id=>hooks.code(people.get(id)),replace:!!state.replace,issue,onSearch:q=>{state.q=q;},
    assign:(c,reason,form)=>void assignPerson(c,result,reason,form),supervisorPick:(c,plan,redraw)=>supervisorPick(c,plan,result,redraw)});
  // A provisional page: its people (final for each), one line while the rest are checked; the final page replaces it.
  let final=false;
  const page=(rows:Row)=>{if(final||!list.isConnected)return;draw(rows);list.append(el('p',{class:'muted fix-still',role:'status'},'Still checking others…'));};
  held.on.add(page);if(held.page)page(held.page);
  held.final.then(result=>{final=true;held.on.delete(page);
    // SW3-A-10 (maintenance): the server says before anyone is picked when this replace is not the reader's to make.
    if(result.replace_refusal){search.remove();list.replaceChildren(note(`${result.replace_refusal}.`,'info'));return;}
    draw(result);
  }).catch(error=>{held.on.delete(page);if(!list.isConnected)return;
    // Another session was opened meanwhile (its ask stopped this one): nothing to say here.
    if((error as {code?:string}).code==='STOPPED')return;
    list.replaceChildren(note((error as Error).message,'error',error),button('Try again',()=>{asks.forget(key);redrawCurrent?.();},'secondary small'));});
  // The chosen supervisor is checked by the server (fill-options with supervisor_person_id); only its answer for this
  // rota revision replaces the card, so Assign then uses that exact trial.
  function supervisorPick(c:Row,plan:StaffingSupervisionPlan,result:Row,render:()=>void){
    const pick=select(`Who supervises ${nameOf(c)}`,plan.choices.map(x=>({value:x.supervisor_person_id,label:x.display_name})),'',false,'Choose…');
    pick.wrap.classList.add('wk-pick');
    pick.input.addEventListener('change',()=>{const chosen=pick.input.value;if(!chosen)return;pick.input.disabled=true;
      void fillOptions(askBody(result.issue_gone?'':findingKey,chosen)).then(fresh=>{if(!list.isConnected)return;const next=(fresh.candidates as Row[]).find(x=>x.person_id===c.person_id);
        if(fresh.workspace_revision!==w.revision||!next){notify('The rota changed. Open the session again to see who can work it now.','warning');return;}
        result.candidates=(result.candidates as Row[]).map(x=>x===c?{...next,assignment_stamp:fresh.assignment_stamp,source_fingerprint:fresh.source_fingerprint}:x);render();},
        e=>{pick.input.disabled=false;notify((e as Error).message,'error',undefined,e);});});
    return pick.wrap;
  }
  async function assignPerson(candidate:Row,result:Row,overrideReason='',form?:HTMLFormElement){
    if(list.getAttribute('aria-busy')==='true')return;
    // Only this panel is held, and only until the save's reply: aria-disabled, so the pressed control keeps its focus.
    const holdList=(on:boolean)=>{list.setAttribute('aria-busy',String(on));list.querySelectorAll('button').forEach(b=>on?b.setAttribute('aria-disabled','true'):b.removeAttribute('aria-disabled'));};
    holdList(true);
    slot.replaceChildren(el('span',{class:'spinner','aria-hidden':'true'}),'Saving…');
    try{
      // The reply's own scope says whether it checked the whole rota (null: only the edit; its complete check follows).
      const issues=await assignCandidate(w,shift,candidate,result,overrideReason);
      if(issues===null)hooks.edited?.();
      // The reply's delta has patched the held draft (app.ts patchDraft): the panel is drawn from it at once, live for the
      // next edit at the new revision, with the edit's own findings. The page's redraw and the whole-rota check follow and
      // are never waited for here; Submit waits for that check (app.ts checkHold).
      const now=hooks.live?.()||data;
      panel={workspace:w.id,kind:'fill',shift:shift.id,...(state.issue&&!state.replace?{issue:state.issue}:{}),flashRev:Number(now.workspace.revision),
        flash:`${nameOf(candidate)} ${result.replace_assignment_id?'replaces '+fullNameOf(people.get(replacing?.person_id)):'added to the draft'}.${rotaWords(issues)}`};
      redrawCurrent?.(undefined,true);
      void hooks.refresh().catch(reportError);
    }catch(error){slot.replaceChildren();holdList(false);
      // Admission and schema refusals stay beside the pending assignment; one no reason can admit takes the reason away.
      const said=form?.querySelector<HTMLElement>('.wk-override-error'),after=error instanceof ApiError?reasonRefusal(error.code):null;
      if(said&&after){said.textContent=(error as Error).message;
        if(after==='retry')form!.querySelector('input')?.focus();
        else{form!.closest('li')?.querySelector('[aria-expanded]')?.remove();form!.replaceChildren(said);said.tabIndex=-1;said.focus();}}
      else notify((error as Error).message,'error',undefined,error);}
  }
  return aside;
}
/** RUL-E04: a refusal of a reasoned assign, beside its reason. 'retry' keeps the reason (a reason may still admit it,
 *  or the reason itself was refused); 'final' takes it away (no reason can); null is said as a toast. */
export const reasonRefusal=(code:string):'retry'|'final'|null=>['PERSON_UNAVAILABLE','SCHEMA_INVALID'].includes(code)?'retry'
  :['EDIT_INVALID','MOVE_INVALID','SWAP_INVALID'].includes(code)?'final':null;
/** SW3-A-10: a fixed assignment (a named person from recurring sessions) on a rota whose fixed assignments are the Rota
 *  Coordinator's: shown, never offered as a change the server then refuses. */
const heldFixed=(hooks:WeekHooks,p:Row)=>!!hooks.fixedHeld&&!!p.fixed;
const FIXED_HELD='Fixed assignment. Only the Rota Coordinator changes it on this rota.';
/** Who can take a place (the draft fill panel, and Replace or Add on a published session): the server's candidates in its
 *  order, searchable, with their facts. The first who passes every rule is the one primary Assign; each Assign is named
 *  after its person. "Break rule and assign" (RUL-E04: a reason, then the armed confirm) only where `breakable` allows:
 *  by default only where the server's `override_allowed` is true; never for someone not eligible. */
let blockedSerial=0,lockWhySerial=0;
export function candidateList(o:{result:Row;list:HTMLElement;search:HTMLInputElement;code:(personId:string)=>string;replace:boolean;issue?:FillIssue;
  assign:(c:Row,overrideReason?:string,form?:HTMLFormElement)=>void;supervisorPick?:(c:Row,plan:StaffingSupervisionPlan,redraw:()=>void)=>HTMLElement;
  breakable?:(c:Row)=>boolean;onSearch?:(q:string)=>void;
  /** How many who fit are shown before "Show all N" (the session panel 8; the Fix list's fix in place fewer). */
  limit?:number;
  /** The Fix list's fix in place: each card says the server's one short reason (fill-options reason_short) instead of
   *  its facts, and the people who cannot work it stay in the session panel. */
  brief?:boolean}){
  // SW3-A-15: only where the server says it may be overridden (never a double booking or a safety rule): a candidate
  // without the answer is not offered it.
  const {result,list,search,issue}=o,breakable=o.breakable||((c:Row)=>c.override_allowed===true);
  if(result.replace_refusal){list.replaceChildren(note(`${result.replace_refusal}.`,'info'));return;}
  const supervisorPick=(c:Row,plan:StaffingSupervisionPlan)=>o.supervisorPick?.(c,plan,render)||'';
  let warnOpen=false;
  const warned=(d:HTMLDetailsElement)=>{d.addEventListener('toggle',()=>{warnOpen=d.open;});return d;};
  const render=()=>{
    const q=search.value.trim().toLowerCase();
    const match=(c:Row)=>!q||nameOf(c).toLowerCase().includes(q)||o.code(c.person_id).toLowerCase().includes(q);
    // Who resolves the issue comes first within the server's own order (a stable sort); unknown is never promised.
    const first=(list:Row[])=>[...list].sort((a,b)=>Number(b.resolves===true)-Number(a.resolves===true));
    const rows=result.candidates.filter(match),ok=first(rows.filter((c:Row)=>c.status==='ok')),warn=first(rows.filter((c:Row)=>c.status==='warn')),blocked=rows.filter((c:Row)=>c.status==='blocked');
    const resolvesText=(c:Row)=>!issue||result.issue_gone?'':c.resolves===true?'Resolves '+issue.title:c.resolves===false?'Does not resolve '+issue.title+' alone':'';
    const resolves=(c:Row)=>resolvesText(c)?el('li',{class:c.resolves===true?'':'warn'},resolvesText(c)):null;
    const facts=(c:Row)=>[c.rest_before_hours!=null?(c.rest_before_hours>=48?`Off ${Math.round(c.rest_before_hours/24)} days before`:`Rested ${Math.round(c.rest_before_hours)} h`):'No earlier duty',`${c.week_hours} h this week`,`${c.rota_hours} h this rota`,...(c.can_lead?['Can lead']:[])];
    const shortRest=(c:Row)=>c.rest_before_hours!=null&&c.rest_before_hours<11;
    const card=(c:Row,tone:'ok'|'warn',primary=false)=>{
      // Several qualified supervisors on the session: one is chosen here, then this person is checked again with them.
      const plan=c.supervision as StaffingSupervisionPlan|null|undefined,choose=!!plan&&!plan.proposal&&plan.choices.length>0,paired=supervisedBy(c);
      // "Add with a warning": an advisory finding only; breaking a mandatory rule is a different, armed button (SR-13).
      const assign=choose?null:button(tone==='ok'?'Assign':'Assign with warning',()=>o.assign(c),tone==='ok'?primary?'primary small':'secondary small':'secondary small warn',
        {'aria-label':tone==='ok'?`Assign ${nameOf(c)}`:`Assign ${nameOf(c)} with a warning`});
      const reason=o.brief&&c.reason_short?String(c.reason_short):'';
      return el('div',{class:'wk-cand '+tone},el('div',{class:'wk-cand-top'},avatar(nameOf(c),'duty'),el('strong',{},nameOf(c)),assign),
        reason?el('p',{class:'wk-reason'+(tone==='warn'?' warn':''),'data-ctip':reason},reason):tone==='ok'?el('ul',{class:'wk-facts'},...(paired?[el('li',{},'With '+paired)]:[]),...facts(c).map((t,i)=>el('li',{class:i===0&&shortRest(c)?'warn':'','data-tip':i===0&&shortRest(c)?'Short break before this session. No rule blocks it, but check it is reasonable.':undefined},t)),resolves(c),c.fills?null:el('li',{class:'warn'},'Does not close the gap alone')):
        el('p',{class:'wk-why'},paired?el('span',{},'With '+paired):'',resolvesText(c)?el('span',{},resolvesText(c)):'',...c.findings.slice(0,2).map((f:Row)=>el('span',{},el('strong',{},'! '+(f.kind==='RULE'?say(f):f.kind.toLowerCase())),f.kind==='RULE'?'':' — '+say(f)))),
        choose?supervisorPick(c,plan!):'');
    };
    const limit=search.value?50:o.limit||8;
    list.replaceChildren(
      result.issue_gone?note('The rota was checked again and this issue changed. Everyone who can work the session is listed; open the Fix list for its current causes.'):'',
      // Nobody listed resolves it on their own (the server said so for each): said once, before the people, with the
      // rule one click away — not discovered card by card. Unknown (null) is never counted as "does not".
      issue&&!result.issue_gone&&!q&&ok.length+warn.length>0&&[...ok,...warn].every((c:Row)=>c.resolves===false)
        ?note(`Nobody here resolves ${issue.title} on their own: the rule needs someone it counts${issue.rule_id?'. Open the rule to see who':''}.`,'warning'):'',
      el('div',{class:'wk-h3-row'},el('h3',{class:'wk-h3'},o.replace?'Can take over':'Can work this session'),el('small',{class:'muted'},`${ok.length} ${ok.length===1?'person':'people'}`),
        // How the list is worked out: contextual help, not a paragraph under it (S-10).
        result.basis?el('button',{type:'button',class:'icon-button bare','aria-label':'How this list is worked out','data-help':'','data-tip':result.basis},icon('help')):''),
      ...(ok.length?ok.slice(0,limit).map((c:Row,i:number)=>card(c,'ok',i===0)):result.provisional_page?[]:q?[emptyLine(rows.length?'No people who pass every rule match this search.':'No people match this search.',button('Clear search',()=>{search.value='';o.onSearch?.('');render();search.focus();},'secondary small'))]:[emptyLine('Nobody passes every rule for this session.')]),
      ok.length>limit?moreButton(`Show all ${ok.length}`):'',
      ...(!warn.length?[]:!o.brief?[el('h3',{class:'wk-h3'},'Would add a warning · '+warn.length),...warn.slice(0,limit).map((c:Row)=>card(c,'warn'))]
        // UIUX-AUDIT L21: in the Fix list the people who fit stay the one choice; who would add a warning folds behind its
        // count (open when nobody fits), and stays open across a redraw once opened.
        // Live check F4: the count and the rows are one array (everyone behind the fold, which is closed until opened).
        :[warned(el('details',{class:'wk-warned',open:warnOpen||!ok.length},el('summary',{},'Would add a warning · '+warn.length),...warn.map((c:Row)=>card(c,'warn'))))]),
      // ME-08: the people a reason keeps out are listed under it once, with how many ("Not eligible for this clinic · 120").
      blocked.length&&!o.brief?el('section',{class:'wk-blocked','aria-label':`Not available · ${blocked.length}`},el('h3',{class:'wk-h3'},`Not available · ${blocked.length}`),
        ...[...blocked.slice(0,q?100:60).reduce((by:Map<string,Row[]>,c:Row)=>{const f=c.findings[0]||{kind:'RULE',message:'Would break a rule'},k=say(f);by.set(k,[...(by.get(k)||[]),c]);return by;},new Map<string,Row[]>())].map(([why,list],g)=>{
          const id=`wk-blocked-${++blockedSerial}-${g}`;
          return el('div',{class:'wk-blocked-group'},el('p',{class:'wk-blocked-why',id},`${why} · ${list.length}`),el('ul',{'aria-labelledby':id},...list.map((c:Row)=>{
            const f=c.findings[0]||{kind:'RULE',message:'Would break a rule'};
            const anyway=el('button',{type:'button',class:'link-button small','aria-expanded':'false','aria-label':`Break rule and assign ${nameOf(c)}`},'Break rule and assign');anyway.addEventListener('click',()=>breakRule(c,anyway));
            return el('li',{},avatar(nameOf(c),'muted'),el('span',{class:'wk-blocked-text'},el('span',{},el('strong',{},nameOf(c))),
              // Keep the safety reason on the candidate itself as well as in the grouping heading.
              el('small',{class:'wk-blocked-cause'},say(f)),
              // SW3-A-21: the group heading says why; no uppercase kind badge that reads like a job title. Other reasons
              // are said, not "+2 more".
              c.findings.length>1?el('small',{'data-tip':c.findings.slice(1).map((x:Row)=>say(x)).join(' · ')},plural(c.findings.length-1,'more reason')):''),
              f.kind==='NOT ELIGIBLE'?null:c.supervision&&!c.supervision.proposal?(c.supervision.choices.length?supervisorPick(c,c.supervision)||null:null):breakable(c)?anyway:null);})));}),
        blocked.length>(q?100:60)?el('p',{class:'muted'},`Search to see the other ${blocked.length-(q?100:60)}.`):''):'',
      '');
  };
  const moreButton=(text:string)=>{const b=el('button',{type:'button',class:'link-button'},text);b.addEventListener('click',()=>{search.value=' ';render();search.value='';});return b;};
  search.addEventListener('input',()=>{o.onSearch?.(search.value);render();});
  /** RUL-E04: breaking a mandatory rule (leave, time away, protected time, an "at most 0" line) is a manual, reasoned
   *  decision: a required reason, then the same-button armed confirm. The blocking issue stays visible after saving. */
  function breakRule(candidate:Row,opener:HTMLButtonElement){
    const row=opener.closest('li')!,open=row.querySelector('form.wk-override');
    if(open){open.remove();opener.setAttribute('aria-expanded','false');return;}
    const form=reasonForm(nameOf(candidate),(reason,form)=>o.assign(candidate,reason,form));
    row.append(form);opener.setAttribute('aria-expanded','true');form.querySelector('input')!.focus();
  }
  // Painted now, mounted or not: the page around a panel is often still off-page when the answer comes (app.ts shows
  // it after its reads), and a paint that waited for a mounted list left it at "Checking…" for good.
  render();
  return render;
}
/** FIXLIST-UX: a Fix list cause's fix in place. Who can work one of its sessions (the session panel's fill-options and
 *  candidate list, ranked by the server, those who resolve the cause first; the top `limit`, then "Show all N") and
 *  Assign, the same undoable, fingerprint-bound draft edit as the panel's. `done` runs after a saved Assign with the
 *  person's name (the Fix list moves to the next open session). Choosing a trainee's supervisor stays the session
 *  panel's step (`more`). */
/** Sessions an Assign from the Fix list is saving now (rota:shift). Kept outside the fix box: the panel may be drawn
 *  again while the save is on its way (a live update, the rota's check), and its new box must not offer Assign again. */
const assigning=new Set<string>();
export function inlineFill(o:{data:WorkspaceData;shift:Row;issue?:FillIssue;limit?:number;code:(personId:string)=>string;
  done:(who:string)=>unknown;more:()=>void;
  /** A save refused because the rota changed meanwhile (409 / STALE_*): read the rota and its Fix list again. */
  stale:()=>unknown;
  /** False: nobody asked for this session's candidates yet (the panel opened, an issue list's Open all): the box
   *  offers "Show who can work it" and asks the server only when pressed (`asked`), or shows an answer already held. */
  ask?:boolean;asked?:()=>void}):HTMLElement{
  const w=o.data.workspace,finding=o.issue?.key||'',key=`${w.id}:${w.revision}:${o.shift.id}::${finding}`,flight=`${w.id}:${o.shift.id}`;
  const box=el('div',{class:'fix-fill'});
  // Single flight: while this session's Assign is saving, every box drawn for it says so and offers nothing to press.
  if(assigning.has(flight)){box.append(working('Saving…'));return box;}
  // fill-options runs only when someone asks for candidates (LEAD-UIUX, 7 Nov 2026: it took 58 s, unasked, on open).
  if(o.ask===false&&!asks.has(key)){
    const show=button('Show who can work it',()=>{o.asked?.();box.replaceWith(inlineFill({...o,ask:true}));},'secondary small');
    box.append(show);return box;}
  // PERF async fill-options: a job, its pages shown as they come (each row final for its person, so assignable), the
  // final envelope in the same box. A redraw of this box joins the ask held for its session; asking for another
  // session stops this one's wait and cancels its job (candidates.ts askCandidates).
  const held=asks.held('fix',key,f=>({workspace_id:w.id,shift_id:o.shift.id,expected_revision:w.revision,...(f?{finding_key:f}:{})}),finding);
  const list=el('div',{class:'wk-cands fix-cands','aria-live':'polite'},elapsed('Finding who can work it',held.started));
  const search=el('input',{type:'search',hidden:true,'aria-hidden':'true',tabindex:-1}) as HTMLInputElement;
  box.append(list);
  const busy=(on:boolean)=>list.querySelectorAll('button').forEach(b=>(b as HTMLButtonElement).disabled=on);
  // A provisional page: its people, assignable, and one line while the rest are checked; the final page replaces it.
  let final=false;
  const page=(rows:Row)=>{if(final||!list.isConnected||assigning.has(flight))return;draw(rows);list.append(el('p',{class:'muted fix-still',role:'status'},'Still checking others…'));};
  held.on.add(page);if(held.page)page(held.page);
  void held.final.then(result=>{final=true;held.on.delete(page);
    if(result.replace_refusal){list.replaceChildren(note(`${result.replace_refusal}.`,'info'));return;}
    draw(result);
  }).catch(error=>{held.on.delete(page);if(!list.isConnected)return;const code=(error as {code?:string}).code;
    // Another session was chosen meanwhile: nothing to say here; this one can be asked for again.
    if(code==='STOPPED'){list.replaceChildren(button('Show who can work it',()=>box.replaceWith(inlineFill({...o,ask:true})),'secondary small'));return;}
    // The rota moved on while it was checked: say so, and read the rota again before asking (its revision is the job's).
    if(code==='STALE_INPUTS'){list.replaceChildren(note((error as Error).message,'warning'),button('Check again',()=>{void o.stale();},'secondary small'));return;}
    list.replaceChildren(note((error as Error).message,'error',error),button('Try again',()=>{asks.forget(key);box.replaceWith(inlineFill(o));},'secondary small'));});
  function draw(result:Row){
    candidateList({result,list,search,code:o.code,replace:false,issue:o.issue,limit:o.limit??3,brief:true,
      assign:(c,reason='')=>{if(assigning.has(flight))return;assigning.add(flight);busy(true);list.prepend(working('Saving…'));
        void (async()=>{
          try{await assignCandidate(w,o.shift,c,result,reason);asks.forget(key);assigning.delete(flight);await o.done(nameOf(c));}
          catch(error){assigning.delete(flight);asks.forget(key);
            // The rota moved on before this save (another edit, a check, another tab): nothing was saved. A stale save is
            // said once by the app's conflict message (api.ts rota-conflict); any other 409 says the server's words. Either
            // way the rota is read again, so the box offers who can work it now.
            if(error instanceof ApiError&&(error.status===409||error.code.startsWith('STALE_'))){
              if(!error.code.startsWith('STALE_'))notify(error.message||'The rota changed before this could be saved. Nothing was assigned.','warning');await o.stale();}
            else{busy(false);list.querySelector('.wk-loading')?.remove();if((error as Error).message)notify((error as Error).message,'error',undefined,error);}}})();},
      supervisorPick:()=>button('Choose a supervisor…',o.more,'secondary small')});
  }
  return box;
}
