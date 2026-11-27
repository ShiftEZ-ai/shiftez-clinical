import {phoneQuery,el,checkTarget,icon,popover,adoptPopover,multiPick,menuItem,menu,menuSource,opens,armed,badge,empty,field,select,check,button,iconButton,formDialog,notify,departmentToday,type Child,type Choice,type IconName,CAPABILITY} from './dom.js';
import {dateInput,showDay} from './date_picker.js';
import {treeMatches,conditionBuilder,conditionsText,negativeOp,type BuilderOptions,exclusiveLastDay,shownLastDay,lineMatches,relativeLines,relativeSummary,resolveOperands,resolvedNow,resolvable,resolveFailure,retryResolve,operandText,type ConditionField,type ResolveScope,type ValueKind} from './conditions.js';
import type {Condition,Conditions,TableViewDefaults} from './generated.js';
import {TABLE_CONDITION_DEFINITIONS} from './generated.js';
import {background,command,sessionChanged,sessionKey,applyUndone,onSessionReset,type Undone} from './api.js';
import {bulkSelection,canChange,recentlyChanged,forgetChanged,lastUndo,actOn,bulkContext,type BulkAction} from './bulk.js';
import {openChange,stagedNotice} from './bulk_change.js';
import {showTheir} from './bulk_children.js';
import {registry} from './bulk.js';
import {columnHeader,wrapAllColumns,columnGrip,headerReorder,trackWidths,sized,sizeColumn} from './table_menu.js';
import {setTriState,triStateOf} from './check_tree.js';
import {markOpen,rowMark,type PanelPosition} from './side_panel.js';
import {skeleton,observeSize} from './loading.js';
import {filteredWords,findReferences,kindLabel,normalSearch,ownKind,recordMatcher,searchCollection,searchWords,serverCapability,type SearchCapability,type SearchEntityKind,type SearchOptions,type SearchScope} from './search.js';
import {SEARCH_MODES,SEARCH_SCOPES} from './generated.js';
import {openPicker} from './picker.js';
import {timelineSettings,timelineWindow,plusMonths,type Layer,type Zoom} from './timeline.js';
import type {TimelineViewSettings} from './generated.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {collator} from './dom.js';  // the one collator for every list's sort and value order
import {groupToggle as sharedGroupToggle} from './dom.js';  // a group's header is its toggle (UX › controls S5)
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)
import {panelSearch} from './dom.js';  // the one search box: Esc clears first (UX › controls S9)

/**
 * The one collection view used by every list (Notion-style): table, cards or board layout; search, filters,
 * group by, sortable columns, shown/hidden properties, multi-select with a bulk bar, a ⋯ menu per row and named
 * views saved on the server for each person. Pages only describe their columns and actions.
 */
export interface Column<T>{
  key:string;label:string;
  cell:(row:T)=>Child;
  /** Plain text used for search and, unless `sort` is given, sorting. */
  text?:(row:T)=>string;
  /** false: this display-only column offers no sort, including from its header menu. */
  sort?:false|((row:T)=>string|number);
  /** Values this row has for filtering and grouping (several for tags, services…). */
  facet?:(row:T)=>string|string[];
  /** Filter values always offered, even when no loaded row has them (lists paged on the server). */
  options?:string[];
  /** The row's record ids behind its facet values (a Person column's person ids): the viewer's values (Me, People I
   *  approve, Clinics I can work at) are offered on it and matched by id. */
  ids?:(row:T)=>string|string[]|null|undefined;
  /** Filtered by its dates, text or number (not by the values listed): the Filter offers its type's operators, relative
   *  values (This month, Next 2 weeks) and Is empty. `parts` are several dates of one column (Dates · Start / End), each
   *  keyed by its contract field (`start`, `end`) so saved views keep them; dates as yyyy-mm-dd. */
  filter?:{kind:'date'|'text'|'number';value?:(row:T)=>unknown;parts?:{key:string;label:string;server?:{field:string};value:(row:T)=>unknown}[]};
  num?:boolean;
  /** A class on the column's header and cells (its width or alignment). */
  class?:string;
  /** Only for search, filters and grouping; never drawn. */
  hidden?:boolean;
  /** Drawn only while a loaded row has a value here: an empty column never keeps its label. */
  whenAny?:(row:T)=>boolean;
  /** What its text is to the search's "Search in" (search.ts): the row's own details, or the records it references or
   *  belongs to (a rank, clinic, programme, tag, person). Columns with `ids` are related unless marked 'self'. */
  search?:'self'|'related';
  /** Each value a search with options compares on its own (each name of a multi-name column), instead of the joined
   *  text; default: the facet's names for a column with `ids`, else the text. */
  terms?:(row:T)=>unknown;
  /** The records this column explicitly references, for a picked record (search.ts reference): their kind and ids.
   *  Default: a column with `ids` whose contract column names its kind (TABLE_CONDITION_DEFINITIONS). */
  refs?:{kind:SearchEntityKind;ids:(row:T)=>unknown};
  /** The record field this column shows when its key is a display name (phone → work_phone): the Sheet edits it. */
  field?:string;
  /** A list paged on the server: this column's field and canonical value there (a shown "available in app" → state
   *  "available_in_app"), so its Or / is-not conditions are evaluated before the server pages (tableServerConditions). */
  server?:{field:string;value?:(shown:string)=>string};
  /** true: groups follow the rows' order; an array fixes group order independently of row sorting. */
  ordered?:boolean|string[];
  /** Group puts a row under one value only: the first in `ordered` (a person's rank in the department's rank order),
   *  else its first (the main area). Filter still matches any of its values. */
  groupFirst?:boolean;
  /** Inline editing (click the cell). With several rows selected, the value can go to all of them. */
  edit?:EditSpec<T>;
}
/** How a cell is edited in place. `save` gets every row to change and writes them in one audited request. */
export interface EditSpec<T>{
  kind:'text'|'number'|'date'|'select'|'multi'|'toggle'|'custom';
  value:(row:T)=>unknown;
  options?:()=>Choice[];
  /** Return authoritative saved rows when available, so the committed edit is shown even if refresh fails. */
  save:(rows:T[],value:unknown)=>Promise<T[]|void>;
  /** Refresh after the save message claims Undo; awaited before moving to the next editable cell. */
  afterSave?:()=>Promise<void>;
  required?:boolean;maxLength?:number;
  /** Multi only: allow new values (tags). */
  creatable?:boolean;
  /** Custom only: the editor, shown in a popover anchored to the cell; it calls `finish(true)` to save (e.g. on Enter). */
  editor?:(row:T,finish:(save:boolean,move?:1|-1)=>void)=>{node:HTMLElement;read:()=>unknown};
  /** No "saved" message: the edit goes to a local draft that is saved later (a rule's lines). */
  quiet?:boolean;
}
export interface RowAction{label:string;icon:IconName;run:()=>unknown;danger?:boolean;/** Same-button confirm, only for an action that cannot be undone (D42). */armed?:boolean}
export interface TableSpec<T>{
  id:string;noun:[string,string];rows:T[];rowId:(row:T)=>string;columns:Column<T>[];
  searchLabel?:string;searchPlaceholder?:string;
  /** Optional normalized key, in addition to literal text matching (People shares the command menu key). */
  searchKey?:(text:string)=>string;
  bulk?:(ids:string[])=>BulkAction[];
  /** Use a selection kept elsewhere (e.g. the planner's session bar) instead of the table's own bulk bar. */
  selection?:{set:Set<string>;changed:()=>void};
  /** The row's ⋯ menu (dom.ts menu()): actions, a named group of them, or a separator. */
  rowActions?:(row:T)=>(RowAction|'separator'|{group:string;items:RowAction[]})[];
  /** Always-visible buttons for a row (kept for the most common action, e.g. Edit). */
  rowButtons?:(row:T)=>Child[];
  /** A row clicked (or ↑ ↓ in its side panel): `position` is where it is among the rows shown, in the order drawn. */
  onOpen?:(row:T,position?:PanelPosition)=>void;
  /** false: another toolbar owns search, filters and views (the planner), so none are drawn or applied here. */
  toolbar?:false;
  /** Called with each drawn row (table row or card), e.g. to tag it for the planner's rule highlight. */
  decorate?:(row:T,node:HTMLElement)=>void;
  /** Extra controls in the toolbar (page-specific inputs such as a date): page scope first. */
  controls?:Child[];
  /** Collection actions follow search, filters and views; they remain available on an empty list. */
  actions?:Child[];
  /** Rows kept out before search/filters (page-specific controls call `refresh(true)` to start at the top). */
  where?:(row:T)=>boolean;
  empty?:[string,string];
  pageSize?:number;
  /** Drawn for paper or export (print, PNG, CSV read the table): a table even on a phone, where it is cards by default. */
  print?:boolean;
  /** The page loads the rows in server pages (its own Show more / Previous · Next): every loaded row is drawn, no local pages. */
  serverPaged?:true;
  /** Server filters changed: return the read's promise to keep the rows busy until the read settles. */
  onFilter?:(filters:Record<string,string[]>)=>void|Promise<unknown>;
  /** Search changed: the caller searches the complete server list and replaces rows; local search is skipped.
   *  Return the read's promise to keep the rows busy until the read settles. */
  onSearch?:(query:string,options?:SearchOptions)=>void|Promise<unknown>;
  /** A list searched on the server: its search registry key (SEARCH_COLLECTIONS) when not its `collection`, so the box
   *  offers only the scopes and references that server applies (sent as `search_options` with the query). */
  searchIn?:string;
  /** Sort changed: the caller orders the complete server list before paging; local sorting is skipped.
   *  Return the read's promise to keep the rows busy until the read settles. */
  onSort?:(sort:SortValue)=>void|Promise<unknown>;
  /** Identity of the listed results when the address also contains transient panel/form state. Defaults to the route. */
  resultScope?:()=>string;
  /** Narrowed outside the toolbar (a link's own filter chips): an empty result reads as "No matching", not as an empty list. */
  narrowed?:()=>boolean;
  /** A list paged on the server with a read on its way: an empty list meanwhile shows loading, never its empty state. */
  loading?:()=>boolean;
  /** How many records a group (by `groupKey`, value `group`) holds in all, for a list paged on the server. */
  groupTotal?:(groupKey:string,group:string)=>number|undefined;
  defaults?:{group?:string;then?:string;sort?:[string,'asc'|'desc'];layout?:Layout;hidden?:string[];filters?:Record<string,string[]>;wrap?:string[]};
  /** Built-in views that replace a status tab strip: each sets the filters it names (other filters stay), counts its
   *  rows (given, for lists counted on the server, or counted here) and may carry a warning for the views button. */
  /** A filter key that is no column (Requests' `for_me`) is the view's server scope: read by the page, never a Filter
   *  row or a client-side match. */
  views?:()=>{label:string;filters:Record<string,string[]>;count?:number;warn?:string;
    /** What this view says when it holds nothing; its button shows the view without a filter ("Show all requests"). */
    empty?:[string,string];
    /** A view drawn by another list on the same page (Rules ↔ All lines): choosing it runs this instead of filtering. */
    open?:()=>void}[];
  /** A card's title and its one compact line, instead of the first column and a labelled list (phones). */
  card?:(row:T,view:{group:string;then:string})=>{title:Child;line:Child};
  /** Beside each group's name (e.g. "Open rule →"). */
  groupAction?:(key:string,rows:T[])=>Child;
  /** Long groups show their first rows and "Show N more" (one click shows the rest of that group). */
  groupLimit?:number;
  /** What a row's checkbox is named ("Select <this>"); the first column's text when not given. */
  rowLabel?:(row:T)=>string;
  /** Rows that can be selected (default all): the others get no box, and group and select-all boxes leave them out. */
  selectable?:(row:T)=>boolean;
  /** A Calendar layout (View settings › Layout): draws the rows of each group (or all rows) as one calendar, e.g. a
   *  week grid of weekly patterns. Groups keep their heading, count, select box and collapse. */
  calendar?:(rows:T[])=>HTMLElement;
  /** A Timeline layout (View settings › Layout; timeline.ts): draws the matching rows, in their groups, as rows of dated
   *  bars over the window its zoom and period show. The list keeps the period (‹ › and Today in its toolbar) and the
   *  view's timeline settings (zoom, hidden layers, density, weekends, saved with the view); `layers` are the page's
   *  (from its data), shown in View settings as the legend. `sub` is what the list's shown properties say for a row. */
  timeline?:{draw:(groups:{key:string;name:string;rows:T[]}[],t:TimelineDraw<T>)=>HTMLElement;layers:(settings:Required<TimelineViewSettings>)=>Omit<Layer,'on'>[]};
  /** Built-in views that set a whole view (a layout, its grouping and settings), offered under Built-in after the
   *  default: People › Where is everyone. Choosing one names the toolbar's Saved view until another view is chosen. */
  presets?:{label:string;definition:Partial<Definition>}[];
  /** Show only this preset (People on a phone: Where is everyone). Its state is kept apart from the list's own (another
   *  device's choices stay), and there is no Saved view menu or other layout. */
  pinned?:string;
  /** More about the rows counted, after the count ("41 sessions · 125 series"). */
  countNote?:(rows:T[])=>string;
  /** The list's key in the bulk registry (BULK_COLLECTIONS): selections can be resolved on the server (paste names,
   *  Select by condition, all matching), "Show their…" lists its child records and View offers the Sheet layout. */
  collection?:string;
  /** How many records match on the server when the list loads them in pages (Show N more). */
  serverTotal?:()=>number;
  /** Filters (by column key) the server applies with the same values (a paged list's Status). */
  serverFilters?:string[];
  /** After a bulk change made from this list (Change…): read the rows again. */
  afterChange?:()=>unknown;
  /** The parent selection of a child list ("Show their…"): the server resolves this list within it. */
  parent?:()=>{collection:string;selection:import('./bulk.js').SelectionValue}|undefined;
  /** The list reads dated facts (ranks, tags, permissions) on a day: the Filter's "As of" row sets it (today by default)
   *  and the list reads its rows again for that day. */
  asOf?:(day:string)=>void;
}
type Layout='table'|'cards'|'board'|'calendar'|'sheet'|'timeline';
/** What a list gives its Timeline layout on each draw. */
export type TimelineDraw<T>={start:string;end:string;zoom:Zoom;settings:Required<TimelineViewSettings>;collapsed:string[];toggle:(key:string)=>void;sub:(row:T)=>string};
/** `then`: a second grouping level; `wrap`: columns that wrap (pills all shown); `order`: column order (keys).
 *  `then_sort` further sort keys; `filters` are the Filter's plain rows (property is any of values, all joined by And); `conditions` the rest of its
 *  tree (Or, is not, groups), joined to them by And; `as_of` the one day the list reads dated facts on ('' = today). */
type Definition={layout:Layout;hidden:string[];filters:Record<string,string[]>;group:string;sort:[string,'asc'|'desc']|null;q:string;then:string;wrap:string[];order:string[];conditions?:Conditions|null;as_of?:string;timeline?:TimelineViewSettings|null;
  /** The search's options (search.ts), saved with the view's search; only what differs from the defaults. */search_options?:SearchOptions|null;
  /** Sort keys after `sort`, in order (Sort › + Add sort). */then_sort?:SortKey[];
  /** Resized columns (px) a named view keeps: only the list's contract-known columns (TABLE_CONDITION_DEFINITIONS). */widths?:Record<string,number>|null;
  /** A group level's own order ([group or then key, 'desc']; A → Z when not named). */group_sort?:SortKey[]|null};
/** `hydrated`: the named view's saved search and As of were restored once (its first read in this memory); later draws
 *  keep what the person changed since, until they pick a view again. */
/** `widths`: columns the person resized (px), kept per account on this device like the open groups, not in saved views. */
type View=Definition&{page:number;collapsed:string[];widths?:Record<string,number>;viewId:string;chosen?:boolean;scroll?:{scope:string;left:number;top:number};hydrated?:boolean;
  /** The preset (TableSpec.presets) last chosen, while no other view has been. */preset?:string};
type Saved=SavedView<Definition>;
const LAYOUTS:[Layout,string,IconName][]=[['table','Table','menu'],['cards','Cards','grid'],['board','Board','layers'],['calendar','Calendar','calendar'],['timeline','Timeline','timeline'],['sheet','Sheet','edit']];
/** The Sheet layout (View settings › Layout) of a list in the bulk registry: the Bulk editor's grid (sheet.ts) draws
 *  the same collection in place. sheet.ts registers it, so this module does not depend on it. */
export type SheetLayout=(o:{collection:string;table:string;rows:unknown[];rowId:(row:unknown)=>string;refresh:()=>void;
  /** The visible columns' keys, in order, and the grouping, as the list shows them. */columns:string[];group:string})=>HTMLElement;
let sheetLayout:SheetLayout|null=null;
export const useSheetLayout=(draw:SheetLayout)=>{sheetLayout=draw;};
type Node=Condition|Conditions;
const isGroup=(n:Node):n is Conditions=>'conditions' in n;
/** The Filter's tree, split into its plain rows (`filters`, a property is any of values, joined by And: what server-paged
 *  lists and column headers read) and the rest (`conditions`), joined to them by And. */
export function splitFilter(t:Conditions|null):{filters:Record<string,string[]>;conditions:Conditions|null}{
  if(!t?.conditions.length)return {filters:{},conditions:null};
  if(t.match==='any'&&t.conditions.length>1)return {filters:{},conditions:t};
  const filters:Record<string,string[]>={},rest:Node[]=[];
  for(const n of t.conditions){if(!isGroup(n)&&(n.op||'any')==='any'&&n.value==null&&n.values.length&&!filters[n.field])filters[n.field]=[...n.values];else rest.push(n);}
  return {filters,conditions:rest.length?{match:'all',conditions:rest}:null};
}
/** The tree the Filter shows: the plain rows first, then the rest. */
export function joinFilter(filters:Record<string,string[]>,conditions:Conditions|null|undefined):Conditions|null{
  const rows:Node[]=Object.entries(filters).filter(([,x])=>x.length).map(([field,values])=>({field:field as Condition['field'],op:'any',values:[...values]}));
  if(!rows.length)return conditions?.conditions.length?conditions:null;
  const rest=!conditions?.conditions.length?[]:conditions.match==='any'&&conditions.conditions.length>1?[conditions]:conditions.conditions;
  return {match:'all',conditions:[...rows,...rest]};
}
/** Whether a row's values (by property) meet a Filter tree, groups with And/Or: the shared line evaluator
 *  (conditions.lineMatches: is any of, has all of, is not, dates, text, numbers, relative values, Is empty). */
export function holds(n:Node,have:(field:string)=>unknown[],kind?:(field:string)=>ValueKind|undefined,scope?:ResolveScope,ids?:(field:string)=>unknown[]|undefined):boolean{
  return treeMatches(n,c=>lineMatches(c,have(c.field),kind?.(c.field),scope,ids?.(c.field)));
}
/** The fields that describe a person (packages/domain/conditions.py PERSON_FIELDS). */
const PERSON_FIELDS=new Set(['roles','person_ids','category_ids','capability_ids','programme_ids','rotation_type_ids','cohort_ids','tags','eligible_service_ids']);
/** Whether a session meets a Filter tree as the server reads it (conditions.evaluate_candidates): positive person lines
 *  hold for one participant together (`people`, one value reader each); "is not" person lines and "has all of" people
 *  read the whole session (`session`). A session with nobody is read as itself. */
export function holdsForSomeone(t:Node,session:(field:string)=>unknown[],people:((field:string)=>unknown[])[]):boolean{
  return (people.length?people:[session]).some(w=>treeMatches(t,n=>{
    const personal=PERSON_FIELDS.has(n.field),whole=n.field==='person_ids'&&n.op==='all'||personal&&(negativeOp(n.op)||n.op==='not_contains');
    return lineMatches(n,(whole?session:w)(n.field));}));
}
/** A list's condition tree (Or, is not, groups) in its server's words, for the list's paged endpoint (`filter`):
 *  null when it has none or a line names a column the server cannot read (then it stays a filter of the rows shown). */
const serverTrees=new Map<string,Conditions|null>();
export const tableServerConditions=(id:string):Conditions|null=>serverTrees.get(id)??null;
function serverTree<T>(n:Node,columns:Column<T>[]):Node|null{
  if(isGroup(n)){const parts=n.conditions.map(x=>serverTree(x,columns));return parts.every(Boolean)?{...n,conditions:parts as Node[]} as Conditions:null;}
  const to=columns.find(c=>c.key===n.field)?.server||columns.flatMap(c=>c.filter?.parts||[]).find(p=>p.key===n.field)?.server;if(!to)return null;
  const map=(v:unknown):unknown=>{
    if(Array.isArray(v))return v.map(map);
    if(v&&typeof v==='object')return 'kind' in v&&v.kind==='absolute'&&'value' in v?{...v,value:map(v.value)}:v;
    return typeof v==='string'&&'value' in to&&typeof to.value==='function'?to.value(v):v;
  };
  return {...n,field:to.field,values:n.values.map(v=>map(v)),...(n.value==null?{}:{value:map(n.value)})} as Condition;
}
const leafCount=(n:Node|null|undefined):number=>!n?0:isGroup(n)?n.conditions.reduce((a,x)=>a+leafCount(x),0):1;
const sameValues=(a:string[]=[],b:string[]=[])=>a.length===b.length&&[...a].sort().join('\n')===[...b].sort().join('\n');
/** Rows above which a list searched in the browser waits for a pause in typing before it draws. */
const LONG_SEARCH=200;
const SEARCH_KINDS=new Set<string>(Object.values(TABLE_CONDITION_DEFINITIONS).flatMap(t=>Object.values(t).map(c=>(c as {kind:string}).kind)).filter(k=>ownKind(k).length));
/** A collection's Filter: its plain rows (`filters`: a property is any of values, joined by And; column headers, built-in
 *  views and server-paged lists read them) and the rest of its tree (`conditions`: Or, is not, groups), joined by And. */
export type FilterState={filters:Record<string,string[]>;conditions:Conditions|null};
/** Whether a tree mentions a property (with one of `values`, when given) in a line that is not "is not". */
export const mentions=(t:Conditions|null|undefined,field:string,values?:string[]):boolean=>!!t&&t.conditions.some(n=>isGroup(n)?mentions(n,field,values)
  :n.field===field&&n.op!=='none'&&n.op!=='is_not'&&(!values||n.values.some(x=>values.includes(x))));
/** The values a tree names for a property in lines that are not "is not" (what a rule line's population, its who, names). */
export const namedValues=(t:Conditions|null|undefined,field:string):string[]=>!t?[]:t.conditions.flatMap(n=>isGroup(n)?namedValues(n,field)
  :n.field===field&&n.op!=='none'&&n.op!=='is_not'?n.values:[]);
/**
 * The one Filter of every collection (Notion-style; single source of truth): the shared condition builder
 * (conditions.ts) over the collection's own properties: + Add filter rule on any property with its type's operators,
 * And / Or, filter groups, ⋯ per row; the toolbar button counts the active rules, the popover lists them with × and Clear
 * all. Saved views keep the state as `filters` + `conditions`. `before` adds a collection's scope row (As of); `extra`
 * its active entries. Pages pass their properties and state; matching uses `holds` (or the server, for paged lists).
 */
export function conditionFilter(o:{fields:()=>ConditionField[];get:()=>FilterState;set:(next:FilterState)=>void;
  /** A property's name in the active list (a date part reads "Dates · Start"); a value's, when it is an id. */label:(key:string)=>string;valueLabel?:(key:string,value:string)=>string;
  builder?:BuilderOptions;before?:(panel:HTMLElement,changed:()=>void)=>void;
  extra?:()=>{count:number;active:ActiveFilter[];clear:()=>void};result?:()=>string;watch?:(changed:(fields?:boolean)=>void)=>void;
  /** The list's own filter (its defaults, the built-in or saved view shown): not counted, so Filter is "on" only once the
   *  person changes it. Still a builder row. */
  baseline?:()=>FilterState}):NonNullable<ToolbarSpec['filter']>{
  const value=o.valueLabel||((_:string,x:string)=>x),text=(t:Conditions)=>conditionsText(t,value,o.label);
  // What differs from the baseline: a property whose values are the baseline's is the list's own, not an active filter.
  const changed=()=>{const s=o.get(),b=o.baseline?.();if(!b)return s;
    return {filters:Object.fromEntries(Object.entries(s.filters).filter(([k,x])=>!sameValues(x,b.filters[k]))),
      conditions:JSON.stringify(s.conditions||null)===JSON.stringify(b.conditions||null)?null:s.conditions};};
  let live:ReturnType<typeof conditionBuilder>|null=null;
  const all=()=>{const s=o.get();return Object.values(s.filters).reduce((a,x)=>a+x.length,0)+leafCount(s.conditions)+(o.extra?.().count||0);};
  return {any:()=>all()>0,total:all,
    count:()=>{const s=changed();return Object.values(s.filters).reduce((a,x)=>a+x.length,0)+leafCount(s.conditions)+(o.extra?.().count||0);},
    clear:()=>{o.extra?.().clear();o.set({filters:{},conditions:null});},
    reset:()=>{const b=o.baseline?.();o.extra?.().clear();o.set(b?structuredClone(b):{filters:{},conditions:null});},
    // Rows of the builder are marked `row`: the popover shows them as builder rows, not a second list (X-02).
    active:()=>{const s=changed();return [...(o.extra?.().active||[]),
      ...Object.entries(s.filters).flatMap(([k,want])=>want.map(x=>({name:o.label(k),value:value(k,x),row:true,
        remove:()=>{const now=o.get(),f={...now.filters,[k]:(now.filters[k]||[]).filter(y=>y!==x)};if(!f[k].length)delete f[k];o.set({filters:f,conditions:now.conditions});}}))),
      ...(s.conditions?[{name:'',value:text(s.conditions),row:true,remove:()=>o.set({filters:o.get().filters,conditions:null})}]:[])];},
    result:o.result,watch:o.watch,refield:()=>live?.setFields(o.fields()),
    build:(panel,changed)=>{
      o.before?.(panel,changed);
      const fields=o.fields();if(!fields.length)return;
      const s=o.get(),b=conditionBuilder(fields,joinFilter(s.filters,s.conditions),o.builder);b.root.classList.add('dt-filter-builder');live=b;
      b.root.addEventListener('change',()=>{o.set(splitFilter(b.read()));changed();});
      panel.append(b.root);}};
}
const views=new Map<string,View>();
/** A named view saved on the server for one list (or the planner): its owner's, or (Schedule) shared with the department,
 *  which only coordinators and admins change (`can_edit`). */
export type SavedView<D>={id:string;revision:number;name:string;definition:D;scope?:'personal'|'department';can_edit?:boolean};
// Saved views are read once per list and account: a page redraw (after any save) does not ask again; a saved-view
// change (table_view.*) or another account reads afresh.
const viewCache=new Map<string,{session:number;views:Promise<unknown[]>}>();
/** Each list's default choices (personal → department → built-in), as last read or set (an Undo included). */
const viewDefaults=new Map<string,TableViewDefaults>();
// A read that started before a default was set must not bring the older choice back.
let defaultsSet=0;
const putDefaults=(key:string,d:TableViewDefaults)=>{defaultsSet++;viewDefaults.set(key,d);};
if(typeof window!=='undefined')window.addEventListener('rota-saved',e=>{const d=(e as CustomEvent).detail;if(!String(d?.kind).startsWith('table_view.'))return;
  viewCache.clear();if(d.kind==='table_view.set_default'&&d.result?.defaults)putDefaults(d.payload.table_key,d.result.defaults);});
export const fetchViews=<D>(key:string):Promise<SavedView<D>[]>=>{
  const hit=viewCache.get(key);if(hit&&hit.session===sessionKey())return hit.views as Promise<SavedView<D>[]>;
  // An app-wide read (not the page's): leaving the page must not abort it, or the next visit asks again.
  const turn=defaultsSet,views=background<{views:SavedView<D>[];defaults?:TableViewDefaults}>('/api/table-views?table='+encodeURIComponent(key)).then(r=>{if(turn===defaultsSet)viewDefaults.set(key,r.defaults||{});return r.views;});
  viewCache.set(key,{session:sessionKey(),views});views.catch(()=>viewCache.delete(key));return views;};
/** The view a list opens with: the personal default, else the department's; `null` is the built-in view and
 *  `undefined` no default chosen (the list keeps what this browser remembers). Read once views are fetched. */
export function defaultView<D>(key:string,saved:SavedView<D>[]):SavedView<D>|null|undefined{
  const d=viewDefaults.get(key)||{},id=d.personal_set?d.personal_view_id:d.department_set?d.department_view_id:undefined;
  return id===undefined?undefined:id===null?null:saved.find(x=>x.id===id);}
const openedLists=new Set<string>();
/** A list's first read this session opens its default view (if one is chosen); later visits keep what was shown. */
export function openingView<D>(key:string,saved:SavedView<D>[]):SavedView<D>|null|undefined{
  if(openedLists.has(key))return undefined;openedLists.add(key);return defaultView(key,saved);}
export type SavedViewMenu<D>={key:string;noun:string;saved:SavedView<D>[];activeId:string;current:()=>D;dirty:(saved:D)=>boolean;
  apply:(view:SavedView<D>|null)=>void;done:(saved:SavedView<D>[],activeId:string)=>void;close:()=>void;
  /** Back to the list's default display, keeping the active view (its changes can then be saved or dropped). */
  reset?:()=>void;
  /** New views may be shared with the department (default: for coordinators and admins). */
  shareable?:boolean;
  /** Built-in views (e.g. one per status) listed first, with counts, in place of "Default view". */
  builtins?:BuiltinView[];
  /** Where the views' relative values are resolved (their menu says what they mean today). */
  scope?:ResolveScope;
  /** What one is called in its menu and messages ("preset"); "view" when not given. */
  thing?:string};
export type BuiltinView={label:string;count?:number;warn?:string;on:boolean;apply:()=>void};
/** Named views on the server, the one way every list and the Schedule save, duplicate, rename and delete them. */
/** A table wider than its box: while columns continue under the pinned actions column, that column casts an edge
 *  (`.more-right`), so a heading beneath it reads as scrolled, not cut. The same observer gives each resizable heading's
 *  separator its width (aria-valuenow). One observer for every table; a removed scroller or heading leaves it. */
const edgeMark=(s:Element)=>s.classList.toggle('more-right',s.scrollLeft+s.clientWidth<s.scrollWidth-1);
const unwatchEdge=new WeakMap<Element,()=>void>();
/** Observed afresh: a scroller drawn off-page reports once it is laid out. */
const watchEdge=(s:Element)=>{unwatchEdge.get(s)?.();unwatchEdge.set(s,observeSize(s,edgeMark));};
function scrollEdge(s:HTMLElement){s.addEventListener('scroll',()=>edgeMark(s),{passive:true});watchEdge(s);return s;}

const leads=()=>bulkContext().permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN);
export function viewActions<D>(o:Omit<SavedViewMenu<D>,'close'|'apply'>){
  const t=o.thing||'view',T=t[0].toUpperCase()+t.slice(1);
  const found=o.saved.find(x=>x.id===o.activeId),active=found?.can_edit===false?undefined:found;  // a read-only shared view: Save as new only
  // A saved view shown before the saved views have loaded is named by its id: the defaults name it too (never the first
  // built-in in its place), and once loaded its own scope decides which defaults it offers.
  const loading=!found&&!!o.activeId;
  const d=viewDefaults.get(o.key)||{},target=found?.id??(loading?o.activeId:null);
  // Defaults name the view shown (a saved one, or the built-in when none is): the server keeps one choice per scope, and
  // its `null` is the first built-in. Another built-in has no id there: saving it as a view first makes it a default.
  const settable=!!found||loading||!o.builtins?.length||o.builtins[0].on;
  const setDefault=(scope:'personal'|'department',clear=false)=>async()=>{
    const r=await command<{defaults:TableViewDefaults}>('table_view.set_default',{table_key:o.key,scope,...(clear?{clear:true}:{view_id:target})});
    putDefaults(o.key,r.defaults);
    notify(clear?'This list now opens with the department default.':scope==='personal'?'This list now opens with this view for you.':'This list now opens with this view for the department.');};
  const mine=d.personal_set&&d.personal_view_id===target,theirs=d.department_set&&d.department_view_id===target;
  const named=(title:string,text:string,value:string,submit:string,save:(name:string,shared:boolean)=>Promise<void>,share=false)=>formDialog(title,text,form=>{
    const name=field(`${T} name`,'text',value,{required:true,maxLength:160});form.append(name.wrap);
    const shared=share?check('Share with the department',false):null;if(shared)form.append(shared.wrap);
    return ()=>save(name.input.value,!!shared?.input.checked);},submit);
  const create=(name:string,shared=false)=>command<{view:SavedView<D>}>('table_view.create',{table_key:o.key,name,definition:o.current(),...(shared?{scope:'department'}:{})}).then(r=>{o.done([...o.saved,r.view],r.view.id);return r.view;});
  return {active,found,
    saveAs:()=>named(`New ${t}`,o.thing?'':`Keeps how ${o.noun} are shown, filtered and grouped.`,'',`Save ${t}`,async (name,shared)=>{await create(name,shared);notify(shared?`${T} saved and shared with the department.`:`${T} saved.`);},o.shareable??leads()),
    defaults:d,
    makeMine:!settable||mine||(!d.personal_set&&theirs)?null:setDefault('personal'),
    makeDepartment:!settable||!leads()||theirs||found&&found.scope!=='department'?null:setDefault('department'),
    followDepartment:d.personal_set?setDefault('personal',true):null,
    // A copy of what is on screen now (unsaved changes included), opened at once.
    duplicate:found?()=>named(`Duplicate ${t}`,`A new ${t} with the same settings; the original stays as it was.`,`${found.name} copy`,'Duplicate',async name=>{await create(name);notify(`${T} duplicated.`);}):null,
    rename:active?(name?:string)=>name!==undefined?renameTo(active,name):named(`Rename ${t}`,'Only the name changes; the settings stay as saved.',active.name,'Rename',name=>renameTo(active,name)):null,
    update:active?async()=>{const r=await command<{view:SavedView<D>}>('table_view.update',{table_key:o.key,name:active.name,definition:o.current()},active.id,active.revision);o.done(o.saved.map(x=>x.id===active.id?r.view:x),active.id);notify(`${T} updated.`);}:null,
    remove:active?async()=>{await command('table_view.delete',{},active.id,active.revision);o.done(o.saved.filter(x=>x!==active),'');notify(`${T} deleted.`);}:null};
  async function renameTo(view:SavedView<D>,name:string){
    if(!name.trim()||name.trim()===view.name)return;
    const r=await command<{view:SavedView<D>}>('table_view.update',{table_key:o.key,name:name.trim(),definition:view.definition},view.id,view.revision);
    o.done(o.saved.map(x=>x.id===view.id?r.view:x),o.activeId);notify(`${T} renamed.`);}
}
/** The saved-views menu used by every list and the Schedule: default, each view, then new, update, duplicate, rename,
 *  delete and reset. */
// The views are one choice (menuitemradio): the active one is ticked and announced as checked; a count is its hint.
const viewChoice=(close:()=>void,name:string,on:boolean,run:()=>void,hint='',warn='',glyph:IconName='layers',key='builtin:'+name,mark='')=>{
  const b=menuItem(name,warn?'alert':glyph,()=>{close();run();},{role:'menuitemradio','aria-checked':String(on),'data-view-key':key,...(warn?{class:'warn','aria-label':`${name}${hint?', '+hint:''}, ${warn}`}:{})},hint);
  if(mark)b.append(badge(mark,'info','view-default'));if(on)b.append(icon('check'));return b;};
/** The badge on the view a list opens with: the viewer's own default, else the department's. */
const defaultMark=(d:TableViewDefaults,id:string|null)=>d.personal_set?(d.personal_view_id===id?'Default':''):d.department_set&&d.department_view_id===id?'Department default':'';
const section=(label:string,items:HTMLElement[])=>items.length?[el('div',{role:'group','aria-label':label,class:'menu-group'},el('div',{class:'menu-heading','aria-hidden':'true'},label),...items)]:[];
/** Built-in views alone (a panel's status views, not saved on the server): the same rows as in savedViewItems. */
export const builtinViewItems=(builtins:BuiltinView[],close:()=>void,saved=false,mark='')=>
  builtins.map((x,i)=>viewChoice(close,x.label,x.on&&!saved,x.apply,x.count===undefined?'':String(x.count),x.warn,'layers','builtin:'+x.label,i?'':mark));
export function savedViewItems<D>(o:SavedViewMenu<D>):HTMLElement[]{
  const a=viewActions(o),active=a.active;
  const item=(label:string,glyph:IconName,run:()=>unknown)=>menuItem(label,glyph,()=>{o.close();return run();},{'data-view-key':'action:'+label});
  // A view with relative values (This month, Me) moves with the day and the viewer: a clock and what it means today.
  const savedItem=(x:SavedView<D>)=>{const tree=(x.definition as {conditions?:Conditions|null}).conditions,scope=o.scope||{context:'filter'};
    const hint=()=>relativeSummary(tree,scope);
    const b=viewChoice(o.close,x.name,x.id===o.activeId,()=>o.apply(x),hint(),'',hint()?'clock':'layers','saved:'+x.id,defaultMark(a.defaults,x.id));
    // Any save (this view's own) or reload forgets resolved values: ask again and show today's dates in the open menu.
    if(hint())void resolveOperands(relativeLines(tree),scope).then(()=>{const h=b.querySelector('.hint');if(h)h.textContent=hint();});
    return b;};
  const builtinMark=defaultMark(a.defaults,null);
  return [
    ...section('Built-in',o.builtins?.length?builtinViewItems(o.builtins,o.close,!!o.activeId,builtinMark):[viewChoice(o.close,'Default view',!o.activeId,()=>o.apply(null),'','','layers','builtin:Default view',builtinMark)]),
    ...section('Department',o.saved.filter(x=>x.scope==='department').map(savedItem)),
    ...section('Mine',o.saved.filter(x=>x.scope!=='department').map(savedItem)),
    el('hr',{class:'menu-sep'}),
    item(`New ${o.thing||'view'}…`,'plus',a.saveAs),
    ...(active&&a.update&&o.dirty(active.definition)?[item(`Update “${active.name}”`,'check',a.update)]:[]),
    ...(a.duplicate?[item(`Duplicate “${o.saved.find(x=>x.id===o.activeId)!.name}”…`,'copy',a.duplicate)]:[]),
    ...(a.rename?[item(`Rename “${active!.name}”…`,'edit',()=>a.rename!())]:[]),
    ...(a.remove?[menuItem(`Delete “${active!.name}”`,'close',()=>{o.close();return a.remove!();},{class:'danger','data-view-key':'action:delete:'+JSON.stringify([o.key,active!.id,active!.revision,active!.name])})]:[]),
    ...(o.reset?[item('Reset to default','reset',o.reset)]:[]),
    ...(a.makeMine||a.makeDepartment||a.followDepartment?[el('hr',{class:'menu-sep'})]:[]),
    ...(a.makeMine?[item('Set as my default','pin',a.makeMine)]:[]),
    ...(a.makeDepartment?[item('Set as department default','people',a.makeDepartment)]:[]),
    ...(a.followDepartment?[item('Use department default','reset',a.followDepartment)]:[]),
  ];
}
// ---- The collection toolbar ----
/** A choice in a Group or Sort menu. */
export type ToolOption={key:string;label:string};
export type SortKey=[string,'asc'|'desc'];
export type SortValue=SortKey|null;
/** One active filter value: the property, the value as shown, and how to drop it. */
export type ActiveFilter={name:string;value:string;remove:()=>void;/** Shown as a row of the Filter's own builder (not listed again in its head). */row?:boolean};
/**
 * One collection toolbar (single source of truth) for every list, the Schedule and dashboards. The order never changes:
 * Saved view (named after the view shown) → scope (period/navigation where present) → Search → Filter →
 * Group → Sort → View settings (an icon) → actions. Each part is optional; a screen passes only what it
 * supports and keeps its own state. Every button looks and behaves the same: an outline popover, an accent "on" state
 * with a count, and Clear inside the Filter popover.
 */
export interface ToolbarSpec{
  /** Accessible name of the collection, e.g. "People". */
  label:string;
  /** Screens that redraw everything on a change (the planner) give a key, so an open popover stays open across redraws. */
  key?:string;
  scope?:Child[];
  /** `onInput` keeps what is typed at once, so leaving the page never drops a letter; `apply` (a redraw, a server read)
   *  waits `wait` ms for a pause in typing (at once without `wait`, and on Enter), and never runs once the box has left
   *  the page (a later view owns the state then). */
  search?:{value:string;label:string;placeholder?:string;onInput:(value:string)=>void;apply?:()=>void;wait?:number;
    /** The search's options (search.ts): match, scope and a picked record, only what the list really applies (`can`); a
     *  change applies at once. Typing clears a picked record: the text then searches as text. */
    options?:SearchOptionsSpec};
  /** Build the filter controls into the panel; call `changed()` after each change (`close()` for an Apply button). `count` is the number of active filters. */
  filter?:{count:()=>number;build:(panel:HTMLElement,changed:()=>void,close:()=>void)=>void;clear:()=>void;
    /** The active filters, one per value: the button summarises the values ("Filter: Unread, Leave") and the popover
     *  lists each with × (no separate chip row). */
    active?:()=>ActiveFilter[];
    /** What the filters leave, for the popover's "Active · 9 of 43 people" line; `watch` says when it changes (a
     *  relative value resolved, server rows arrived), so the open popover never shows an earlier count; `fields` when a
     *  property's values arrived (Works in's read), so the open popover offers them. */
    result?:()=>string;watch?:(changed:(fields?:boolean)=>void)=>void;
    /** The open builder takes its properties' new values in place: a row still being filled in is not part of the filter
     *  yet, so rebuilding from the filter would drop it. */
    refield?:()=>void;
    /** Whether any rule is set, the list's own included (count() leaves those out): the popover then offers Clear all. */
    any?:()=>boolean;
    /** Every rule in force, the view's own included (F-C2: a view that filters by itself says so on the button). */total?:()=>number;
    /** Back to the view's own filters (the filtered strip's Show all); `clear` when not given. */reset?:()=>void};
  /** `active` (group, sort, view) marks a choice that differs from the collection's default: only then is the button highlighted. */
  group?:{options:ToolOption[];value:()=>string;set:(key:string)=>void;active?:()=>boolean;
    /** The collection's default grouping, marked "default" in the menu. */defaultKey?:string;
    /** A second level ("Then by"), saved with the view; offered once a first grouping is chosen. */then?:{value:()=>string;set:(key:string)=>void};
    /** Each level's own order, keyed by the level's field ([key, asc|desc]); a level not named reads A → Z. */order?:{value:()=>SortKey[];set:(keys:SortKey[])=>void};
    /** Close or open every group at once (the heads keep their counts). */collapse?:{all:()=>void;none:()=>void};
    /** The whole edit at once (levels and their orders, one redraw) instead of set, then.set and order.set in turn. */levels?:(keys:string[],order:SortKey[])=>void};
  /** `directions: false` for orders that only go one way (e.g. by start time). */
  sort?:{options:ToolOption[];value:()=>SortValue;set:(value:SortValue)=>void;directions?:false;/** What the default order is called ("Default order" when not given). */defaultLabel?:string;/** false: the current order is the list's default (not marked on). */active?:()=>boolean;
    /** Further keys after the first (Notion's sort list: + Add sort, each with its direction, moved up or down, removed):
     *  the popover edits the whole list and gives it, first key included, to `then.set`. */then?:{value:()=>SortKey[];set:(keys:SortKey[])=>void}};
  /** Layouts (Table/Cards/Board, or the planner's) plus any display settings. `active` marks a non-default view. */
  view?:{layouts?:[string,string,IconName][];value?:()=>string;set?:(key:string)=>void;build?:(panel:HTMLElement,changed:()=>void,close:()=>void)=>void;active?:()=>boolean;
    /** What the settings show now, for the button's accessible name (the Schedule: "Calendar, Week"). */label?:()=>string;
    /** The tooltip (the Schedule lists what its settings hold). */tip?:string};
  /** Named views: the button shows the active view's name, with a dot when it has unsaved changes. */
  saved?:{name:()=>string;dirty:()=>boolean;items:(close:()=>void)=>HTMLElement[];
    /** The shown view's count, and a warning that stays on the button ("2 waiting for you"). */
    warn?:()=>string;/** What the button says when no named view is shown (else "Default view"). */defaultName?:()=>string};
  actions?:Child[];
}
/** A labelled single-choice menu (Group by, Sort by, a view setting): menuitemradio rows, the chosen one ticked. */
/** A one-choice menu: the chosen option alone carries the tick. `glyph` leads each option (Group by, Sort by); none by
 *  default, so a menu of words never shows a tick on every row. */
export function optionMenu(label:string,options:ToolOption[],value:string,pick:(key:string)=>void,glyph:IconName|null=null,defaultKey?:string):HTMLElement[]{
  const tick=(b:HTMLElement,on:boolean)=>{b.setAttribute('aria-checked',String(on));b.querySelector(':scope>.tick')?.remove();if(on){const t=icon('check');t.classList.add('tick');b.append(t);}};
  const menu=el('div',{class:'menu-list',role:'menu','aria-label':label},...options.map(o=>{
    const b=el('button',{type:'button',role:'menuitemradio',class:'menu-item'},glyph?icon(o.key?glyph:'close'):null,el('span',{},o.label),defaultKey!==undefined&&o.key===defaultKey?el('span',{class:'hint'},'default'):null);tick(b,o.key===value);
    // The tick moves at once: a popover that stays open across the redraw (adoptPopover) must not show the old choice.
    b.addEventListener('click',()=>{menu.querySelectorAll<HTMLElement>('[role=menuitemradio]').forEach(n=>tick(n,n===b));pick(o.key);});return b;}));
  return [el('div',{class:'dt-pop-label'},label),menu];
}
/** Notion's sort list: one row per key ([property] [direction] ↑ ↓ ×), + Add sort, Delete sort. Every key is one property
 *  at most; the list is as long as the properties allow. Stays open while editing (the list redraws in place). */
function sortList(panel:HTMLElement,o:{options:ToolOption[];value:()=>SortValue;set:(v:SortValue)=>void;then:{value:()=>SortKey[];set:(k:SortKey[])=>void}},sync:()=>void){
  const keys=():SortKey[]=>{const v=o.value();return v?[v,...o.then.value()]:[];};
  const put=(next:SortKey[],focus?:{row:number;what:string})=>{o.then.set(next);sync();draw();
    if(focus)(list.children[Math.min(focus.row,list.children.length-1)]?.querySelector<HTMLElement>(focus.what)||adder)?.focus();};
  const list=el('div',{class:'cond-list dt-sort-list',role:'list','aria-label':'Sorts'});
  const adder=el('button',{type:'button',class:'link-button cond-add'},'+ Add sort') as HTMLButtonElement;
  adder.addEventListener('click',()=>{const k=keys(),free=o.options.find(x=>!k.some(y=>y[0]===x.key));if(free)put([...k,[free.key,'asc']],{row:k.length,what:'select'});});
  const wipe=armed(button('Delete sort',()=>put([],{row:0,what:''}),'',{class:'link-button cond-delete'}),'Delete every sort?');
  function draw(){
    const k=keys();
    list.replaceChildren(...k.map(([key,dir],i)=>{
      const prop=select(i?'Then by':'Sort by',o.options.filter(x=>x.key===key||!k.some(y=>y[0]===x.key)).map(x=>({value:x.key,label:x.label})),key);
      prop.input.addEventListener('change',()=>{const n=[...k];n[i]=[prop.input.value,dir];put(n,{row:i,what:'select'});});
      const way=select('Direction',[{value:'asc',label:'Ascending'},{value:'desc',label:'Descending'}],dir);
      way.input.addEventListener('change',()=>{const n=[...k];n[i]=[key,way.input.value as 'asc'|'desc'];put(n,{row:i,what:'.dt-sort-dir select'});});
      way.wrap.classList.add('dt-sort-dir');
      const move=(by:-1|1)=>{const n=[...k];[n[i],n[i+by]]=[n[i+by],n[i]];put(n,{row:i+by,what:`[data-move="${by}"]`});};
      const label=o.options.find(x=>x.key===key)?.label||key;
      return el('div',{class:'cond-row dt-sort-row',role:'listitem'},el('span',{class:'cond-where'},i?'Then':'Sort'),
        el('div',{class:'cond-line'},prop.wrap,way.wrap),
        el('span',{class:'dt-sort-moves'},iconButton(`Move ${label} up`,'chevronUp',()=>move(-1),{class:'bare','data-move':'-1',disabled:i===0}),
          iconButton(`Move ${label} down`,'chevronDown',()=>move(1),{class:'bare','data-move':'1',disabled:i===k.length-1}),
          iconButton(`Remove sort ${label}`,'close',()=>put(k.filter((_,j)=>j!==i),{row:i,what:'select'}),{class:'bare'})));}));
    adder.hidden=k.length>=o.options.length;wipe.hidden=!k.length;root.hidden=!k.length;start.hidden=!!k.length;
    // No sort yet: the properties to sort by, one click from the first key (as in Notion).
    if(!k.length)start.replaceChildren(...optionMenu('Sort by',o.options,'',key=>put([[key,'asc']],{row:0,what:'select'}),'sort'));
  }
  const start=el('div',{}),root=el('div',{class:'cond-root dt-sort-root'},list,el('div',{class:'cond-actions'},adder,el('span',{class:'grow'}),wipe));
  draw();
  panel.append(start,root,el('p',{class:'dt-col-note'},'Saved with the view'));
}
type GroupSpec=NonNullable<ToolbarSpec['group']>;
/** Group as Sort is edited (the same rows): one row per level ([field] [A → Z | Z → A] ↑ ↓ ×), + Add subgroup, Remove
 *  grouping, and Collapse all / Expand all. Levels are the contract's two (group, then by); each keeps its own order.
 *  Stays open while editing (the rows redraw in place); the default grouping is marked. */
function groupList(panel:HTMLElement,g:GroupSpec,sync:()=>void){
  const most=g.then?2:1,levels=():string[]=>[g.value(),g.then?.value()||''].filter(Boolean);
  const dir=(key:string):'asc'|'desc'=>g.order?.value().find(([k])=>k===key)?.[1]||'asc';
  // Keys and their orders together: the order of a level that went away goes with it.
  const put=(keys:string[],dirs:Record<string,'asc'|'desc'>,focus?:{row:number;what:string})=>{
    const order=keys.filter(k=>dirs[k]==='desc').map(k=>[k,'desc'] as SortKey);
    if(g.levels)g.levels(keys,order);else{g.set(keys[0]||'');g.then?.set(keys[1]||'');g.order?.set(order);}
    sync();draw();
    if(focus)(list.children[Math.min(focus.row,list.children.length-1)]?.querySelector<HTMLElement>(focus.what)||adder)?.focus();};
  const dirsNow=()=>Object.fromEntries(levels().map(k=>[k,dir(k)])) as Record<string,'asc'|'desc'>;
  const list=el('div',{class:'cond-list dt-sort-list dt-group-list',role:'list','aria-label':'Groups'});
  const adder=el('button',{type:'button',class:'link-button cond-add'},'+ Add subgroup') as HTMLButtonElement;
  adder.addEventListener('click',()=>{const k=levels(),free=g.options.find(x=>!k.includes(x.key));if(free)put([...k,free.key],dirsNow(),{row:k.length,what:'select'});});
  const wipe=button('Remove grouping',()=>put([],{},{row:0,what:''}),'',{class:'link-button cond-delete'});
  const folds=el('div',{class:'dt-group-folds'});
  if(g.collapse)folds.append(button('Collapse all',()=>{g.collapse!.all();sync();},'ghost small'),button('Expand all',()=>{g.collapse!.none();sync();},'ghost small'));
  const label=(key:string)=>g.options.find(x=>x.key===key)?.label||key;
  function draw(){
    const k=levels();
    list.replaceChildren(...k.map((key,i)=>{
      const prop=select(i?'Then by':'Group by',g.options.filter(x=>x.key===key||!k.includes(x.key)).map(x=>({value:x.key,label:x.label+(x.key===g.defaultKey?' (default)':'')})),key);
      prop.input.addEventListener('change',()=>{const n=[...k],d=dirsNow();d[prop.input.value]=d[key];n[i]=prop.input.value;put(n,d,{row:i,what:'select'});});
      const row=el('div',{class:'cond-row dt-sort-row',role:'listitem'},el('span',{class:'cond-where'},i?'Then':'Group'),el('div',{class:'cond-line'},prop.wrap));
      if(g.order){const way=select('Group order',[{value:'asc',label:'A → Z'},{value:'desc',label:'Z → A'}],dir(key));
        way.input.addEventListener('change',()=>{const d=dirsNow();d[key]=way.input.value as 'asc'|'desc';put(k,d,{row:i,what:'.dt-sort-dir select'});});
        way.wrap.classList.add('dt-sort-dir');row.querySelector('.cond-line')!.append(way.wrap);}
      const move=(by:-1|1)=>{const n=[...k];[n[i],n[i+by]]=[n[i+by],n[i]];put(n,dirsNow(),{row:i+by,what:`[data-move="${by}"]`});};
      row.append(el('span',{class:'dt-sort-moves'},iconButton(`Move ${label(key)} up`,'chevronUp',()=>move(-1),{class:'bare','data-move':'-1',disabled:i===0}),
        iconButton(`Move ${label(key)} down`,'chevronDown',()=>move(1),{class:'bare','data-move':'1',disabled:i===k.length-1}),
        iconButton(`Remove group ${label(key)}`,'close',()=>put(k.filter((_,j)=>j!==i),dirsNow(),{row:i,what:'select'}),{class:'bare'})));
      return row;}));
    adder.hidden=k.length>=most||k.length>=g.options.length||!k.length;wipe.hidden=!k.length;root.hidden=!k.length;start.hidden=!!k.length;folds.hidden=!k.length;
    // No grouping yet: the fields to group by, one click from the first level (as Sort does).
    if(!k.length)start.replaceChildren(...optionMenu('Group by',g.options,'',key=>put([key],{},{row:0,what:'select'}),'layers',g.defaultKey));
  }
  const start=el('div',{}),root=el('div',{class:'cond-root dt-sort-root'},list,el('div',{class:'cond-actions'},adder,el('span',{class:'grow'}),wipe));
  draw();
  panel.append(start,root,folds,el('p',{class:'dt-col-note'},'Saved with the view'));
}
/** The toolbar, a `sync()` that refreshes button states after the screen changes something itself, and the search box. */
/** View › Shown properties, the one component for every collection: each property shown or hidden and moved up or down
 *  (the column order, as the column menu's Move left / right). `cols`: the properties in their current order. */
export function shownProperties(cols:()=>{key:string;label:string}[],o:{hidden:()=>string[];setHidden:(hidden:string[])=>void;setOrder:(keys:string[])=>void}):HTMLElement[]{
  const props=el('div',{class:'dt-pop-list dt-props',role:'list','aria-label':'Shown properties'});
  const paint=(focus?:string)=>{const list=cols();props.replaceChildren(...list.map((c,i)=>{
    const box=el('input',{type:'checkbox',checked:!o.hidden().includes(c.key)}) as HTMLInputElement;
    box.addEventListener('change',()=>o.setHidden(box.checked?o.hidden().filter(k=>k!==c.key):[...o.hidden(),c.key]));
    const move=(by:-1|1)=>{const keys=list.map(x=>x.key);[keys[i],keys[i+by]]=[keys[i+by],keys[i]];o.setOrder(keys);paint(`[data-move="${c.key}:${by}"]`);};
    return el('div',{class:'dt-pop-item dt-prop',role:'listitem'},el('label',{class:'dt-prop-name'},box,el('span',{},c.label)),
      el('span',{class:'dt-sort-moves'},iconButton(`Move ${c.label} up`,'chevronUp',()=>move(-1),{class:'bare','data-move':`${c.key}:-1`,disabled:i===0}),
        iconButton(`Move ${c.label} down`,'chevronDown',()=>move(1),{class:'bare','data-move':`${c.key}:1`,disabled:i===list.length-1})));}));
    if(focus){const b=props.querySelector<HTMLButtonElement>(focus);(b&&!b.disabled?b:props.querySelector<HTMLElement>(focus.replace(/:-?1"/,':'+(focus.includes(':-1')?'1':'-1')+'"')))?.focus();}};
  paint();return [el('div',{class:'dt-pop-label'},'Shown properties'),props];
}
/** The search box's options (search.ts): `can` is what the list really applies, from the search registry. */
export type SearchOptionsSpec={value:()=>SearchOptions|undefined;set:(next:SearchOptions|undefined)=>void;can:SearchCapability;
  /** What a picked record shows (the Schedule: the person alone, their recorded team, or everyone on their duties):
   *  offered only while `when` holds (a person picked by name, never guessed from typed text); the default is not said. */
  show?:{label:string;options:readonly {key:string;label:string}[];value:()=>string;defaultKey:string;set:(key:string)=>void;when:(o:SearchOptions|undefined)=>boolean}};
/** The search box's options behind its search icon: Match, Search in (where the list tells a record's own text from the
 *  records it references) and Find a record (a person, programme or tag picked by name: its id is what is searched, its
 *  name only shown). Narrowed options stay written in the box, a picked record is a token beside the text. */
function searchOptionControls(o:SearchOptionsSpec,input:HTMLInputElement,typed:(text:string)=>void,key?:string):{nodes:HTMLElement[];sync:()=>void}{
  const words=el('span',{class:'dt-search-opt-text'});
  const btn=el('button',{type:'button',class:'dt-search-opt',...popup('dialog')},icon('search'),words);
  const kind=el('span',{class:'dt-search-ref-kind'});
  const unpick=el('button',{type:'button',class:'dt-search-ref-x'},icon('close'));
  const token=el('span',{class:'dt-search-ref',hidden:true},kind,unpick);
  const put=(next:SearchOptions|undefined)=>{o.set(normalSearch(next));sync();};
  // × searches the words in the box as text again; the picked record is let go, the text stays. Focus goes back to the
  // box first, so a page that redraws its toolbar on a change keeps it there.
  unpick.addEventListener('click',()=>{input.focus();put({...o.value(),reference:null});});
  const findRecord=(anchor:HTMLElement,k:SearchEntityKind,done:()=>void)=>{const found:Choice[]=[];
    openPicker(anchor,anchor.parentElement||anchor,{label:'Find '+kindLabel(k).toLowerCase(),choices:found,multiple:false,selected:new Set(),recent:false,lookupOnly:true,emptyText:'Type a name',
      lookup:q=>findReferences(o.can.collection!,k,q).then(r=>({choices:r.items.map(x=>({value:x.id,label:x.label})),more:r.has_more})),
      onPick:id=>{const name=found.find(c=>c.value===id)?.label||'';input.value=name;typed(name);done();input.focus();put({...o.value(),reference:{kind:k,id}});}});};
  btn.addEventListener('click',()=>popover(btn,(panel,close)=>{panel.classList.add('dt-pop','dt-search-pop');
    const v=o.value()||{};
    panel.append(...optionMenu('Match',SEARCH_MODES.map(m=>({key:m.value,label:m.label})),v.match||SEARCH_MODES[0].value,k=>put({...o.value(),match:k as SearchOptions['match']})));
    if(o.can.scopes.length>1)panel.append(...optionMenu('Search in',SEARCH_SCOPES.filter(x=>o.can.scopes.includes(x.value)).map(x=>({key:x.value,label:x.label})),v.scope||SEARCH_SCOPES[0].value,k=>put({...o.value(),scope:k as SearchScope})));
    if(o.can.collection&&o.can.kinds.length)panel.append(el('div',{class:'dt-pop-label'},'Find a record'),el('div',{class:'menu-list',role:'menu','aria-label':'Find a record'},
      ...o.can.kinds.map(k=>{const b:HTMLButtonElement=menuItem(kindLabel(k),k==='people'?'people':k==='tags'?'bookmark':'layers',()=>findRecord(b,k,close),{...popup('dialog')},v.reference?.kind===k?'picked':'');return b;})));
    const show=o.show;
    if(show?.when(v))panel.append(...optionMenu(show.label,[...show.options],show.value(),k=>{show.set(k);sync();},null,show.defaultKey));
    if(normalSearch(v))panel.append(el('div',{class:'dt-pop-foot'},button('Reset search options',()=>{close();input.focus();put(undefined);},'ghost small')));
  },{label:'Search options',placement:'below-end',key}));
  // A toolbar drawn again (the Schedule's, after a change) keeps the open options on its new button.
  if(key)queueMicrotask(()=>adoptPopover(key,btn));
  function sync(){const v=normalSearch(o.value()),shown=o.show&&o.show.when(v)&&o.show.value()!==o.show.defaultKey?o.show.options.find(x=>x.key===o.show!.value())?.label||'':'',
    said=[searchWords(v),shown].filter(Boolean).join(' · ');
    words.textContent=said;btn.classList.toggle('on',!!said);btn.disabled=input.disabled;
    btn.setAttribute('aria-label','Search options'+(said?': '+said:''));btn.setAttribute('data-tip',said||'Search options');
    token.hidden=!v?.reference;kind.textContent=v?.reference?kindLabel(v.reference.kind):'';
    unpick.setAttribute('aria-label',v?.reference?`Search the text instead of the picked ${kindLabel(v.reference.kind).toLowerCase()} record`:'');}
  sync();
  return {nodes:[btn,token],sync};
}
/** The shared collection toolbar. Its caller says after each draw what it shows (`shown`: the count, what the view
 *  holds before the search and filters, or undefined when the server applied them, and the noun): the toolbar then
 *  says what they hide, none left included ("Filtered: 9 of 43 people shown · Show all"), so no list filtered to
 *  nothing is a dead end. */
export function collectionToolbar(spec:ToolbarSpec):{root:HTMLElement;sync:()=>void;search:HTMLInputElement|null;shown:(count:number,of:number|undefined,noun:[string,string])=>void}{
  const tool=(label:string,glyph:IconName)=>el('button',{type:'button',class:'dt-tool',...popup('dialog'),'aria-label':label,'data-tip':label},icon(glyph),el('span',{class:'dt-tool-label'},label),el('span',{class:'dt-tool-count'}));
  const state=(b:HTMLElement,label:string,on:boolean,count='',detail='')=>{
    b.classList.toggle('on',on);b.querySelector('.dt-tool-count')!.textContent=count;
    b.setAttribute('aria-label',on?`${label}${detail?': '+detail:''}${count?` (${count} active)`:''}`:label);};
  const pop=(anchor:HTMLElement,label:string,build:(panel:HTMLElement,close:()=>void)=>void)=>{
    const key=spec.key&&`${spec.key}:${label}`;
    anchor.addEventListener('click',()=>popover(anchor,(panel,close)=>{panel.classList.add('dt-pop');build(panel,close);},{label,placement:'below',key}));
    if(key)queueMicrotask(()=>adoptPopover(key,anchor));};
  const parts:HTMLElement[]=[];let search:HTMLInputElement|null=null,syncSearch:(()=>void)|undefined;
  if(spec.search){
    const o=spec.search;let timer=0;
    // The one panel search (dom.ts panelSearch): Esc clears what is typed first, and only then reaches the panel or dock
    // around it (live 235391b: a docked list's first Esc closed it with the text in it).
    const input=panelSearch(o.label,o.placeholder||'Search',undefined,'dt-search');input.value=o.value;input.maxLength=120;
    const apply=()=>{window.clearTimeout(timer);timer=0;if(input.isConnected)o.apply?.();};
    // Editing the text lets a picked record go (its id no longer says what is typed): the words then search as text.
    input.addEventListener('input',()=>{if(o.options?.value()?.reference){o.options.set(normalSearch({...o.options.value(),reference:null}));syncSearch?.();}
      o.onInput(input.value);window.clearTimeout(timer);if(o.wait)timer=window.setTimeout(apply,o.wait);else apply();});
    // Enter applies the latest text now, once (the pending pause is dropped).
    input.addEventListener('keydown',e=>{if(e.key==='Enter'&&o.wait){e.preventDefault();if(timer)apply();}});
    // An option changed applies at once (a pending pause in typing is applied with it).
    const opts=o.options&&searchOptionControls({...o.options,set:next=>{o.options!.set(next);apply();}},input,o.onInput,spec.key&&`${spec.key}:Search options`);syncSearch=opts?.sync;
    const wrap=opts?el('div',{class:'dt-search-wrap'},opts.nodes[0],opts.nodes[1],input):el('label',{class:'dt-search-wrap'},icon('search'),input);
    // The box never shrinks below its placeholder ("Search trai", D-156): app.css sizes its minimum from the length.
    wrap.style.setProperty('--ph',String(input.placeholder.length));search=input;parts.push(wrap);
  }
  const filterBtn=spec.filter?tool('Filter','filter'):null,groupBtn=spec.group?tool('Group','layers'):null,sortBtn=spec.sort?tool('Sort','sort'):null;
  // View settings is an icon (named for screen readers and in its tooltip): the saved view's name says what is shown.
  const viewBtn=spec.view?el('button',{type:'button',class:'dt-tool dt-glyph','aria-haspopup':'dialog','aria-expanded':'false','aria-label':'View settings','data-tip':spec.view.tip||'View settings'},icon('sliders')):null;
  const savedBtn=spec.saved?el('button',{type:'button',class:'dt-tool dt-saved','aria-haspopup':'dialog','aria-expanded':'false','data-tip':'Switch, add or manage views'},icon('layers'),el('span',{class:'dt-tool-label'}),el('span',{class:'dt-dirty','aria-hidden':'true'}),el('span',{class:'dt-view-warn','aria-hidden':'true'}),icon('chevronDown')):null;
  let syncSaved:(()=>void)|undefined;
  // The filtered strip: a search, or filters beyond the view's own (filter.count() leaves those out), hiding rows. Show
  // all clears exactly those, and the list draws again through its own search apply and filter set.
  const strip=el('div',{class:'dt-filtered',role:'status',hidden:true});let drawn:[number,number|undefined,[string,string]]|null=null,said='';
  const searching=()=>!!search?.value.trim()||!!normalSearch(spec.search?.options?.value())?.reference;
  const showAll=()=>{const o=spec.search,f=spec.filter,typed=!!o&&searching();
    if(typed){search!.value='';o!.options?.set(undefined);o!.onInput('');}
    if(f&&f.count()>0)(f.reset||f.clear)();
    if(typed)o!.apply?.();
    // Focus stays in the toolbar (the strip has gone), the one drawn again included when the list rebuilds its toolbar.
    if(root.isConnected){sync();(search||filterBtn)?.focus();}
    else{const bar=document.querySelector(`.dt-toolbar[aria-label="${CSS.escape(spec.label)}"]`);  // the same order: Search, else Filter
      (bar?.querySelector<HTMLElement>('.dt-search')||bar?.querySelector<HTMLElement>('.dt-tool[aria-label^="Filter"]'))?.focus();}};
  const paintStrip=()=>{
    const words=drawn&&filteredWords({searching:searching(),filters:spec.filter?.count()||0,count:drawn[0],of:drawn[1],noun:drawn[2],options:searchWords(spec.search?.options?.value())})||'';
    strip.hidden=!words;if(words===said)return;said=words;  // a status region: said again only when it changes
    strip.replaceChildren(...(words?[icon('filter'),el('span',{},words),button('Show all',showAll,'',{class:'link-button'})]:[]));};
  const sync=()=>{
    syncSearch?.();
    // Active filters show inside the Filter control: its label lists the values (cut with an ellipsis), the tooltip
    // names each property too.
    if(filterBtn&&spec.filter){const n=spec.filter.count(),act=spec.filter.active?.()||[],day=act.find(a=>a.name==='As of'),values=[act.filter(a=>a!==day).map(a=>a.value).join(', '),day?.value].filter(Boolean).join(' · ');
      // The view's own rules are counted on the button too, without the accent (that marks a change from the view).
      const own=Math.max(0,(spec.filter.total?.()||0)-n);
      state(filterBtn,'Filter',n>0,n||own?String(n+own):'');filterBtn.querySelector('.dt-tool-label')!.textContent=act.length?'Filter: '+values:'Filter';
      if(act.length)filterBtn.setAttribute('aria-label',`Filter: ${values} (${n} active${own?`, ${own} from the view`:''})`);
      else if(own)filterBtn.setAttribute('aria-label',`Filter (${own} from the view)`);
      filterBtn.dataset.tip=act.length?'Filter: '+act.map(a=>a.name?`${a.name}: ${a.value}`:a.value).join(' · '):own?`Filter · ${own} from the view`:'Filter';}
    // Group names its field on the button ("Group: Clinic", "Group: Clinic › Rank"), as in Notion.
    if(groupBtn&&spec.group){const k=spec.group.value(),o=spec.group.options.find(x=>x.key===k),t=o&&spec.group.options.find(x=>x.key===spec.group!.then?.value());
      const name=o?o.label+(t?' › '+t.label:''):'';state(groupBtn,'Group',spec.group.active?spec.group.active():!!o,'',name);groupBtn.querySelector('.dt-tool-label')!.textContent=o?'Group: '+name:'Group';if(o)groupBtn.setAttribute('aria-label','Group: '+name);}
    if(sortBtn&&spec.sort){const v=spec.sort.value(),o=v?spec.sort.options.find(x=>x.key===v[0]):undefined,more=v?spec.sort.then?.value()||[]:[];
      const words=(k:SortKey)=>{const x=spec.sort!.options.find(y=>y.key===k[0]);return x?(spec.sort!.directions===false?x.label:`${x.label}, ${k[1]==='asc'?'ascending':'descending'}`):'';};
      state(sortBtn,'Sort',spec.sort.active?spec.sort.active():!!o,more.length?String(more.length+1):'',o&&v?[v,...more].map(words).filter(Boolean).join(', then '):'');}
    if(viewBtn&&spec.view){const l=spec.view.layouts?.find(x=>x[0]===spec.view!.value?.()),named=spec.view.label?.();
      viewBtn.classList.toggle('on',!!spec.view.active?.());viewBtn.setAttribute('aria-label','View settings'+(named||spec.view.active?.()&&l?': '+(named||l![1]):''));}
    if(savedBtn&&spec.saved){const name=spec.saved.name(),dirty=spec.saved.dirty();savedBtn.querySelector('.dt-tool-label')!.textContent=name||spec.saved.defaultName?.()||'Default view';savedBtn.querySelector('.dt-dirty')!.textContent=dirty?'•':'';
      // The view's row count is the footer's; the views menu lists each view's count.
      const warn=spec.saved.warn?.()||'';
      const w=savedBtn.querySelector('.dt-view-warn')!;w.replaceChildren(...(warn?[icon('alert'),el('span',{class:'dt-view-warn-words'},warn)]:[]));
      savedBtn.classList.toggle('on',!!name);savedBtn.setAttribute('aria-label',`Saved views${name||spec.saved.defaultName?.()?': '+(name||spec.saved.defaultName!()):''}${dirty?' (changed)':''}${warn?', '+warn:''}`);syncSaved?.();}
    paintStrip();
  };
  if(filterBtn){const f=spec.filter!;pop(filterBtn,'Filter',(panel,close)=>{
    // The popover starts with what is active: "Active · 9 of 43 people" and Clear all. Rules of the builder below are
    // its rows (each with its own ⋯ › Remove); only entries without a row (a link's filter) are listed with ×.
    // Clear all and the summary stay the same elements while the values are read again (a list polling its server), so a
    // press on them lands; each is in the popover only while it applies, since a hidden one still takes the first focus.
    const label=el('span',{class:'dt-pop-label'}),list=el('ul',{class:'dt-active-list'});
    const clear=el('button',{type:'button',class:'button ghost small dt-clear','aria-label':'Clear all filters'},'Clear all');
    const head=el('div',{class:'dt-active'},el('div',{class:'dt-active-head'},label,clear));
    clear.addEventListener('click',()=>{f.clear();draw();refocus();});
    const changed=()=>{sync();const n=f.count(),act=(f.active?.()||[]).filter(a=>!a.row),on=n>0||!!f.any?.();
      if(!on){head.remove();return;}if(head.parentNode!==panel)panel.prepend(head);
      label.textContent=`Active · ${f.result?.()||n||''}`.replace(/ · $/,'');
      list.replaceChildren(...act.map(a=>{
        const x=el('button',{type:'button',class:'icon-button bare','aria-label':`Remove filter ${a.name}: ${a.value}`},icon('close'));
        x.addEventListener('click',()=>{a.remove();draw();refocus();});
        return el('li',{},el('span',{class:'truncate'},el('span',{class:'muted'},a.name+' '),a.value),x);}));
      if(act.length)head.append(list);else list.remove();};
    const draw=()=>{panel.replaceChildren(...(head.parentNode===panel?[head]:[]));f.build(panel,changed,close);changed();};
    // New values redraw the builder's rows in place, unless a value list is open (it keeps what it offers until reopened).
    f.watch?.(fields=>{if(!panel.isConnected)return;if(!fields||document.querySelector('.picker'))return changed();const had=panel.contains(document.activeElement);if(f.refield){f.refield();changed();}else draw();if(had)refocus();});
    // Focus stays in the popover when the control that had it goes away; never on Clear all, where Enter would wipe
    // every filter.
    const refocus=()=>((panel.querySelector('.dt-active-list button,.dt-filter-builder button,.dt-filter-builder input,.dt-filter-builder select')
      ||[...panel.querySelectorAll<HTMLElement>('button,input,select')].find(x=>!x.matches('.dt-clear'))) as HTMLElement|null)?.focus();
    draw();
    // Opened on the first rule (popover() focuses the first button, here Clear all): after it, in the same task.
    queueMicrotask(()=>{const at=document.activeElement;if(panel.isConnected&&(!at||at.matches('.dt-clear')||!panel.contains(at)))refocus();});});parts.push(filterBtn);}
  // Group by lists the collection's own groupable fields (its columns), the default marked; "Then by" adds a second level.
  if(groupBtn){const g=spec.group!;pop(groupBtn,'Group',(panel,close)=>{
    if(g.then||g.order){groupList(panel,g,sync);return;}
    panel.append(...optionMenu('Group by',[{key:'',label:'No grouping'},...g.options],g.value(),k=>{close();g.set(k);sync();},'layers',g.defaultKey));
    panel.append(el('p',{class:'dt-col-note'},'Saved with the view'));});parts.push(groupBtn);}
  if(sortBtn){const o=spec.sort!;pop(sortBtn,'Sort',(panel,close)=>{
    if(o.then&&o.directions!==false){sortList(panel,o as Required<Pick<NonNullable<ToolbarSpec['sort']>,'options'|'value'|'set'|'then'>>,sync);return;}
    const v=o.value();
    panel.append(...optionMenu('Sort by',[{key:'',label:o.directions===false?o.defaultLabel||'Default order':'No sorting'},...o.options],v?.[0]||'',k=>{close();o.set(k?[k,v?.[0]===k?v[1]:'asc']:null);sync();},'sort'));
    if(v&&o.directions!==false)panel.append(el('div',{class:'dt-pop-label'},'Direction'),segmented('Sort direction',[['asc','Ascending',{icon:'chevronUp'}],['desc','Descending',{icon:'chevronDown'}]],v[1] as 'asc'|'desc',
      d=>{close();o.set([v[0],d]);sync();},{cls:'dt-direction',activation:'manual'}));
  });parts.push(sortBtn);}
  if(viewBtn){const o=spec.view!;pop(viewBtn,'View settings',(panel,close)=>{
    if(o.layouts?.length){const group=segmented('Layout',o.layouts.map(([key,text,glyph])=>[key,text,{icon:glyph}]),o.value?.()||null,key=>{o.set?.(key);sync();},{cls:'dt-layouts',activation:'manual'});
      panel.append(el('div',{class:'dt-pop-label'},'Layout'),group);}
    o.build?.(panel,sync,close);});parts.push(viewBtn);}
  if(savedBtn){const o=spec.saved!;pop(savedBtn,'Saved views',(panel,close)=>{
    const build=()=>el('div',{class:'menu-list',role:'menu','aria-label':'Saved views'},...o.items(close));
    let menu=build(),queued=false,pendingFocus:string|null=null;
    // The catalogue may arrive after opening. Refresh the menu without rebuilding the collection or moving focus.
    syncSaved=()=>{if(queued||!menu.isConnected||!savedBtn.isConnected)return;queued=true;queueMicrotask(()=>{
      queued=false;if(!menu.isConnected||!savedBtn.isConnected){pendingFocus=null;return;}
      const focused=document.activeElement;
      if(menu.contains(focused))pendingFocus=(focused as HTMLElement).dataset.viewKey||'';
      else if(focused!==document.body)pendingFocus=null;
      const next=build();menu.replaceWith(next);menu=next;
      // el() initializes the menu's roving tabindex first; adjacent syncs retain the key until the latest rebuild.
      queueMicrotask(()=>{if(menu!==next||!next.isConnected||!savedBtn.isConnected)return;
        const key=pendingFocus;pendingFocus=null;if(key===null||document.activeElement!==document.body)return;
        const items=[...next.querySelectorAll<HTMLElement>('[role^=menuitem]')].filter(n=>!n.matches(':disabled,[aria-disabled=true]')&&!n.closest('[hidden]'));
        (items.find(n=>n.dataset.viewKey===key)||items.find(n=>n.getAttribute('aria-checked')==='true')||items[0])?.focus();});
    });};
    panel.append(el('div',{class:'dt-pop-label'},'Saved views'),menu);
  });}
  const root=el('div',{class:'dt-toolbar',role:'group','aria-label':spec.label},savedBtn,...(spec.scope||[]),...parts,...(spec.actions||[]),strip);
  sync();
  return {root,sync,search,shown:(count,of,noun)=>{drawn=[count,of,noun];paintStrip();}};
}

let tableAccount='';
const stored=(id:string):Partial<View>=>{try{return tableAccount?JSON.parse(localStorage.getItem('rota.table.'+tableAccount+id)||'{}'):{};}catch{return {};/* private mode: defaults */}};
function load(id:string,d:TableSpec<unknown>['defaults']):View{
  if(views.has(id))return views.get(id)!;
  const saved=stored(id);
  const v:View={q:'',filters:saved.filters||{...d?.filters},conditions:saved.conditions||null,as_of:'',group:saved.group??d?.group??'',then:saved.then??d?.then??'',wrap:saved.wrap||d?.wrap||[],order:saved.order||[],sort:saved.sort===undefined?d?.sort??null:saved.sort,then_sort:saved.then_sort||[],timeline:saved.timeline||null,layout:saved.layout||d?.layout||'table',hidden:saved.hidden||d?.hidden||[],page:0,collapsed:saved.collapsed||[],widths:saved.widths||{},group_sort:saved.group_sort||[],viewId:saved.viewId||'',chosen:!!saved.chosen,preset:saved.preset||''};
  views.set(id,v);return v;
}
// Free-text search is never stored (it can hold private notes, and an old search silently hid rows): filters are.
/** One Tab stop for a table's records, as in the schedule grid (G-08, WAI-ARIA roving tabindex): Tab enters the row last
 *  used and moves within it; ↑ ↓ move to the same control in the row above or below. Group headings and "Show more" keep
 *  their own Tab stops. In a text field ↑ ↓ still move rows; in a select, number or text area Escape leaves the field
 *  for the table, from where Tab and Shift+Tab go on. */
let roveSequence=0;
export const ROVE_HINT='Use arrow keys to move between rows.';
function roveRows(tbody:HTMLElement){
  const stops=(tr:Element)=>[...tr.querySelectorAll<HTMLElement>('[data-rove]')];
  const rows=()=>[...tbody.querySelectorAll<HTMLElement>(':scope>tr[data-row]')].filter(r=>{for(const n of r.querySelectorAll<HTMLElement>('a[href],button,input,select,textarea,[tabindex]'))
    if(n.dataset.rove===undefined&&n.tabIndex>=0&&!n.matches(':disabled'))n.dataset.rove='';return stops(r).length>0;});
  let current:Element|null=null;
  const sync=(row?:Element|null)=>{const list=rows();current=row&&list.includes(row as HTMLElement)?row:current&&list.includes(current as HTMLElement)?current:list[0]||null;
    for(const r of list)for(const n of stops(r))n.tabIndex=r===current?0:-1;};
  sync();
  tbody.addEventListener('focusin',e=>{const tr=(e.target as HTMLElement).closest('tbody>tr[data-row]');if(tr&&tr!==current)sync(tr);});
  tbody.addEventListener('keydown',e=>{
    const t=e.target as HTMLElement,tr=t.closest<HTMLElement>('tbody>tr[data-row]');
    if(e.defaultPrevented||e.altKey||e.ctrlKey||e.metaKey||!tr)return;
    if(e.key==='Escape'&&t.matches('select,textarea,input:not([type=checkbox],[type=radio])')){
      const box=tbody.closest<HTMLElement>('.table-scroll');if(box){e.preventDefault();e.stopPropagation();box.focus();}return;}
    if(!['ArrowUp','ArrowDown'].includes(e.key)||t.matches('select,textarea,input:not([type=checkbox],[type=radio],[type=text],[type=search],[type=email],[type=tel],[type=url])'))return;
    const list=rows(),next=list[list.indexOf(tr)+(e.key==='ArrowDown'?1:-1)];
    if(!next)return;
    e.preventDefault();const col=stops(tr).indexOf(t);sync(next);const s=stops(next);s[Math.max(0,Math.min(col,s.length-1))]?.focus();
  });
  return ()=>sync();  // rows appended later join the one Tab stop
}
function save(id:string,v:View){if(!tableAccount)return;try{localStorage.setItem('rota.table.'+tableAccount+id,JSON.stringify({filters:v.filters,conditions:v.conditions||null,group:v.group,then:v.then,wrap:v.wrap,order:v.order,sort:v.sort,then_sort:v.then_sort||[],timeline:v.timeline||null,layout:v.layout,hidden:v.hidden,collapsed:v.collapsed,widths:v.widths||{},group_sort:v.group_sort||[],viewId:v.viewId,chosen:v.chosen,preset:v.preset||''}));}catch{/* not persisted */}}
/** Keeps a list's whole state as it is now (in memory and on this device) and returns how to put it back exactly, as often
 *  as asked: a passing change (Print or export's Columns) never stays with the list. The same View, no second schema. */
export function holdTableState(id:string):()=>void{
  const key='rota.table.'+tableAccount+id,account=tableAccount,kept=views.has(id)?structuredClone(views.get(id)!):null;
  let disk:string|null=null;try{disk=localStorage.getItem(key);}catch{/* nothing stored */}
  return ()=>{if(account!==tableAccount)return;  // another account since: its lists are not this one's
    const now=views.get(id);
    // In place: the table drawn now holds this object.
    if(kept&&now){for(const k of Object.keys(now))delete (now as Record<string,unknown>)[k];Object.assign(now,structuredClone(kept));}
    else if(kept)views.set(id,structuredClone(kept));else views.delete(id);
    try{if(disk===null)localStorage.removeItem(key);else localStorage.setItem(key,disk);}catch{/* not persisted */}};
}
/** Open a list already filtered (e.g. from "55 people have no current rank"): applied once, on the next draw of that list. */
const pendingFilters=new Map<string,Record<string,string[]>>(),pendingSearch=new Map<string,string>(),pendingAsOf=new Map<string,string>();
/** `q` also fills the search box (Ctrl K › a person opens People searched for them). */
export function openFiltered(id:string,filters:Record<string,string[]>,q=''){pendingFilters.set(id,filters);pendingSearch.set(id,q);}
/** Open a list on a day ('' = today), e.g. a child list on its parent list's As of: applied on that list's next draw. */
export function openAsOf(id:string,day:string){pendingAsOf.set(id,day);}
/** A list's current filters, including ones queued by openFiltered (for pages that filter on the server). */
// Read without caching a view, so the list's own defaults still apply when it is first drawn.
export const tableFilters=(id:string):Record<string,string[]>=>pendingFilters.get(id)||views.get(id)?.filters||stored(id).filters||{};
/** Whether a list has filters of its own yet (a visit, a saved state or a preset): an empty set then means none, not its defaults. */
export const tableFiltered=(id:string):boolean=>pendingFilters.has(id)||views.has(id)||!!stored(id).filters;
export const tableSearch=(id:string):string=>pendingSearch.get(id)??views.get(id)?.q??'';
/** A list's search options as its server read sends them (`search_options`); undefined for none (the older plain search). */
export const tableSearchOptions=(id:string):SearchOptions|undefined=>pendingSearch.has(id)?undefined:normalSearch(views.get(id)?.search_options);
/** A list's one As of day ('' = today) as its shared View holds it: a page reading its own rows starts from it on re-entry. */
/** The key a list remembers its selection under: a child list's for its parents only (another parent set starts afresh). */
export const selectionKey=(id:string,parent?:unknown)=>id+(parent?'|'+JSON.stringify(parent):'');
export const tableAsOf=(id:string):string=>pendingAsOf.get(id)??views.get(id)?.as_of??'';
export const tableSort=(id:string):SortValue=>views.has(id)?views.get(id)!.sort:stored(id).sort??null;
/** What a list shows now, for a feedback report: filters, grouping, sort, layout and saved view. Never the search text
 *  (it can hold private notes) nor any row. */
export const tableState=(id:string)=>{const v=views.get(id);return v?{filters:v.filters,group:v.group,sort:v.sort,layout:v.layout,view_id:v.viewId}:null;};
/** Opens a list as a feedback report saw it: applied on that list's next draw ("Open where it happened"). */
export function openTableState(id:string,state:{filters?:Record<string,string[]>;group?:string;sort?:SortValue;layout?:string}){
  const v=load(id,undefined);
  Object.assign(v,{filters:{...state.filters},conditions:null,as_of:'',group:state.group??v.group,sort:state.sort===undefined?v.sort:state.sort,
    layout:LAYOUTS.some(l=>l[0]===state.layout)?state.layout:v.layout,viewId:'',page:0,q:'',search_options:undefined});
}
/** A list's columns (shown, order, wrap, widths) as a saved view keeps them, for a page whose own saved view holds them
 *  (the Schedule's List: a TableViewDefinition's top-level hidden, order, wrap and widths). Set before the list is first
 *  drawn, they wait for that draw, so its other defaults (sort) stay. */
export type TableColumns={hidden:string[];order:string[];wrap:string[];widths?:Record<string,number>};
const pendingColumns=new Map<string,TableColumns>();
// Widths by key name: a saved definition comes back from the server with its keys reordered.
const columnsOf=(id:string,c:Partial<TableColumns>|undefined):TableColumns=>{const w=viewWidths(id,c?.widths);
  return {hidden:[...c?.hidden||[]],order:[...c?.order||[]],wrap:[...c?.wrap||[]],...(w?{widths:Object.fromEntries(Object.entries(w).sort(([a],[b])=>a<b?-1:1))}:{})};};
export const tableColumns=(id:string):TableColumns=>columnsOf(id,pendingColumns.get(id)||views.get(id)||stored(id));
export const sameColumns=(id:string,c:Partial<TableColumns>|undefined)=>JSON.stringify(columnsOf(id,c))===JSON.stringify(tableColumns(id));
/** The list's columns become `c`'s (none: the defaults, every column shown in its own order, one line, natural widths). */
export function setTableColumns(id:string,c:Partial<TableColumns>|undefined){const n=columnsOf(id,c),next={...n,widths:n.widths||{}};
  const v=views.get(id);if(!v){pendingColumns.set(id,next);return;}Object.assign(v,next);save(id,v);}
const clearTableMemory=()=>{views.clear();openedLists.clear();viewDefaults.clear();pendingFilters.clear();pendingSearch.clear();pendingAsOf.clear();pendingColumns.clear();viewCache.clear();serverTrees.clear();};
// A sign-out forgets the lists' state with the session: the next account starts with no inherited (hidden) filter.
onSessionReset(clearTableMemory);
/** Queries may contain private notes. Keep every local view with its canonical account and department; never adopt
 * unscoped legacy preferences whose owner is unknown. Expiry drops memory while same-account reload retains storage. */
export function setTableAccount(identity='',department=''){
  const next=identity&&department?`${identity}.${department}.`:'';
  if(next===tableAccount)return;tableAccount=next;clearTableMemory();
}
/** Deliberate sign-out leaves no table preferences in this browser (named views stay on the server). */
export function clearTableViews(){clearTableMemory();try{for(const k of Object.keys(localStorage))if(k.startsWith('rota.table.'))localStorage.removeItem(k);}catch{/* nothing stored */}}
/** Checked Undo replies carry the saved definition to display again, including a recreated deleted view's new id. */
export function restoreTableViews(done:Undone){
  let planner:SavedView<{planner:any}>|undefined;
  for(const item of done){
    const restored=item.kind.startsWith('table_view.')&&item.result?.view;
    if(!restored)continue;
    viewCache.delete(restored.table_key);
    if(restored.table_key==='planner'){planner=restored;continue;}
    if(restored.table_key==='rule-tree'||restored.table_key==='fairness')continue; // These collections restore their own state.
    const current=load(restored.table_key,undefined);
    Object.assign(current,{then_sort:[]},restored.definition,{viewId:restored.id,page:0,collapsed:[]});
    save(restored.table_key,current);
  }
  return planner;
}
const list=(x:string|string[]|undefined)=>x===undefined||x===''?[]:Array.isArray(x)?x:[x];
/** The widths a named view of list `id` may keep: the columns its contract knows; the rest stay on this device. */
const viewWidths=(id:string,w:Record<string,number>|undefined|null)=>{const known=(TABLE_CONDITION_DEFINITIONS as Record<string,object>)[id]||{};
  const kept=Object.entries(w||{}).filter(([k,n])=>n>0&&Object.hasOwn(known,k));return kept.length?Object.fromEntries(kept):undefined;};
const definitionOf=(v:View,id:string):Definition=>({layout:v.layout,hidden:v.hidden,filters:v.filters,group:v.group,sort:v.sort,q:v.q,then:v.then,wrap:v.wrap,order:v.order,
  ...(v.conditions?{conditions:v.conditions}:{}),...(v.as_of?{as_of:v.as_of}:{}),...(v.then_sort?.length?{then_sort:v.then_sort}:{}),...(v.timeline?{timeline:v.timeline}:{}),
  ...(normalSearch(v.search_options)?{search_options:normalSearch(v.search_options)}:{}),...(viewWidths(id,v.widths)?{widths:viewWidths(id,v.widths)}:{}),...(groupOrder(v).length?{group_sort:groupOrder(v)}:{})});
/** The orders of the levels shown (the server refuses one naming a level that is not active). */
const groupOrder=(v:{group:string;then:string;group_sort?:SortKey[]|null})=>(v.group_sort||[]).filter(([k,d])=>d==='desc'&&k&&(k===v.group||k===v.then));
// Views saved before then/wrap/order (or search options) existed read as their defaults.
const same=(a:Definition,b:Definition)=>JSON.stringify(a)===JSON.stringify({layout:b.layout,hidden:b.hidden,filters:b.filters,group:b.group,sort:b.sort,q:b.q,then:b.then||'',wrap:b.wrap||[],order:b.order||[],
  ...(b.conditions?{conditions:b.conditions}:{}),...(b.as_of?{as_of:b.as_of}:{}),...(b.then_sort?.length?{then_sort:b.then_sort}:{}),...(b.timeline?{timeline:b.timeline}:{}),
  ...(normalSearch(b.search_options)?{search_options:normalSearch(b.search_options)}:{}),...(b.widths&&Object.keys(b.widths).length?{widths:b.widths}:{}),...(groupOrder({group:b.group,then:b.then||'',group_sort:b.group_sort}).length?{group_sort:groupOrder({group:b.group,then:b.then||'',group_sort:b.group_sort})}:{})});
// One breakpoint listener; replaced tables are not retained by global event callbacks.
const narrowQuery=phoneQuery;
const tableDraws=new WeakMap<HTMLElement,()=>void>(),tableRoots=new Set<WeakRef<HTMLElement>>();
/** Native viewport positions live only in the current account's table memory, never in saved views or storage. */
function tableScroll(scope:HTMLElement|null,restore:boolean){
  if(!scope)return;
  for(const reference of tableRoots){const root=reference.deref();if(!root){tableRoots.delete(reference);continue;}
    if(!root.isConnected||!scope.contains(root))continue;
    const kept=views.get(root.dataset.table!)?.scroll,scroll=root.querySelector<HTMLElement>('.dt-body>.table-scroll');
    if(restore&&scroll){
      watchEdge(scroll); // Detached loading content may have left the observer before mounting.
      const head=scroll.querySelector('thead>tr');if(head)trackWidths(head);
      fitHeight(root); // A detached draw's animation frame may have elapsed before the page was mounted.
    }
    if(!kept||!scroll)continue;
    if(restore&&kept.left===0&&kept.top===0)continue; // A fresh viewport is already at its origin.
    if(restore){scroll.scrollLeft=kept.left;scroll.scrollTop=kept.top;}
    kept.left=scroll.scrollLeft;kept.top=scroll.scrollTop;
  }
}
/** Capture before a page rebuild; restore only after its detached content has actually been mounted. */
export const captureTableScroll=(scope:HTMLElement|null)=>tableScroll(scope,false);
export const restoreTableScroll=(scope:HTMLElement)=>tableScroll(scope,true);
/** A main collection's table fills the page down to the status footer (app.css reads --dt-top: where its viewport
 *  starts in the page's own scroll). Few rows keep their natural height. Set after mounting, on draw and on resize. */
function fitHeight(root:HTMLElement){
  const s=root.querySelector<HTMLElement>(':scope>.dt-body>.table-scroll');if(!s?.isConnected)return;
  const host=s.closest<HTMLElement>('.page-contained');
  root.style.setProperty('--dt-top',`${Math.round(s.getBoundingClientRect().top+(host?host.scrollTop-host.getBoundingClientRect().top:scrollY))}px`);
}
/** fitHeight in the next frame, once however often the list redraws before it: reading the layout straight after
 *  writing the rows forced a layout per draw (PI-5, SH-05); the frame lays out once before it paints. */
const fitting=new WeakSet<HTMLElement>();
function fitLater(root:HTMLElement){if(fitting.has(root))return;fitting.add(root);requestAnimationFrame(()=>{fitting.delete(root);fitHeight(root);});}
if(typeof window!=='undefined')window.addEventListener('resize',()=>{for(const reference of tableRoots){const root=reference.deref();if(root?.isConnected)fitHeight(root);}},{passive:true});
narrowQuery.addEventListener('change',()=>{
  for(const reference of tableRoots){
    const root=reference.deref();
    if(!root||!root.isConnected){tableRoots.delete(reference);continue;}
    tableDraws.get(root)?.();
  }
});

/** A resized column: its heading and every cell take its width (a heading alone cannot narrow a table column). */
export function dataTable<T>(spec:TableSpec<T>){
  const account=tableAccount;
  // A pinned preset keeps its own state (`<id>.<preset>`), starting from the preset.
  const pin=spec.pinned?spec.presets?.find(x=>x.label===spec.pinned):undefined,stateId=pin?`${spec.id}.${pin.label.toLowerCase().replace(/\W+/g,'-')}`:spec.id;
  const v=load(stateId,spec.defaults as TableSpec<unknown>['defaults']);
  const columns=pendingColumns.get(stateId);if(columns){pendingColumns.delete(stateId);Object.assign(v,columns);save(stateId,v);}
  if(pin&&v.preset!==pin.label)Object.assign(v,{...pin.definition,filters:{...pin.definition.filters},preset:pin.label});
  const preset=pendingFilters.get(spec.id);// An explicit preset (Home's "Active") is the whole filter: no condition from an earlier visit stays under it.
  // A day given on purpose (the parent list's, a link's) wins over the one a named view brings back on its first read.
  const givenDay=pendingAsOf.has(spec.id);if(givenDay){v.as_of=pendingAsOf.get(spec.id)!;pendingAsOf.delete(spec.id);v.page=0;}
  if(preset){pendingFilters.delete(spec.id);v.filters={...preset};v.conditions=null;v.q=pendingSearch.get(spec.id)||'';v.search_options=undefined;pendingSearch.delete(spec.id);v.page=0;v.viewId='';}
  // A list paged on the server draws every row it has loaded: one pager, the page's own (no Previous / Next here).
  const size=()=>spec.serverPaged?Math.max(1,spec.rows.length):spec.pageSize||50,facetCols=spec.columns.filter(c=>c.facet),typedCols=spec.columns.filter(c=>c.filter),sortableCols=spec.columns.filter(c=>c.sort!==false&&(c.sort||c.text)),drawable=spec.columns.filter(c=>!c.hidden);
  // The first column (the row's name) stays first; the others follow the view's order (Move left / right).
  const orderedCols=()=>{const rank=(c:Column<T>)=>{const i=v.order.indexOf(c.key);return i<0?1000+drawable.indexOf(c):i;};return [drawable[0],...drawable.slice(1).sort((a,b)=>rank(a)-rank(b))].filter(Boolean);};
  const shownCols=()=>orderedCols().filter((c,i)=>i===0||!v.hidden.includes(c.key)&&(!c.whenAny||spec.rows.some(c.whenAny)));
  let saved:Saved[]=[];
  // External selection: plain checkboxes bound to the caller's set; the caller shows its own bar.
  const ext=spec.selection,extBoxes=new Map<string,HTMLInputElement>();
  const extAll=ext?el('input',{type:'checkbox',class:'bulk-check','aria-label':`Select all ${spec.noun[1]} shown`}) as HTMLInputElement:null;
  let extShown:string[]=[];
  const extSync=()=>{if(!ext||!extAll)return;setTriState(extAll,triStateOf(extShown.filter(id=>ext.set.has(id)).length,extShown.length));};
  extAll?.addEventListener('change',()=>{for(const id of extShown)extAll.checked?ext!.set.add(id):ext!.set.delete(id);extBoxes.forEach((b,id)=>b.checked=ext!.set.has(id));ext!.changed();});
  const editable=spec.columns.filter(c=>c.edit&&!c.hidden),fieldEditable=editable.filter(c=>c.edit!.kind!=='custom');
  const rowsById=(ids:string[])=>spec.rows.filter(r=>ids.includes(spec.rowId(r)));
  // A list in the bulk registry offers the one universal Change… (any field or action, preview, one Undo); others keep
  // Edit field… (one value for every selected row). Operator bulk is desktop only.
  const universal=()=>!!spec.collection&&canChange(spec.collection)&&!narrowQuery.matches;
  const revisionOf=(id:string)=>(spec.rows.find(r=>spec.rowId(r)===id) as {revision?:number}|undefined)?.revision;
  const editAction=(ids:string[]):BulkAction[]=>universal()?[{label:'Change…',icon:'edit',primary:true,run:(chosen,_,all)=>openChange({collection:spec.collection!,noun:spec.noun,...(all?{selection:all}:{ids:chosen}),revision:revisionOf,table:spec.id,labelOf:id=>{const r=spec.rows.find(x=>spec.rowId(x)===id);return r?label(r):'';},
      rowOf:id=>spec.rows.find(x=>spec.rowId(x)===id) as Record<string,unknown>|undefined,
      onDone:async()=>{await spec.afterChange?.();}})}]
    :fieldEditable.length&&ids.length>1?[{label:'Edit field…',icon:'edit',run:(chosen,done)=>editDialog(rowsById(chosen),done)}]:[];
  // "Show their…": the child records of the selected ones (a list of its own; level 2 has no Show their… again).
  const theirs=():BulkAction[]=>spec.collection&&!spec.parent&&canChange(spec.collection)&&registry(spec.collection)?.children.length&&!narrowQuery.matches?[{label:'Show their…',icon:'layers',
    run:chosen=>showTheir(document.activeElement as HTMLElement,spec.collection!,spec.noun,chosen,spec.noun[1][0].toUpperCase()+spec.noun[1].slice(1),v.as_of||'')}]:[];
  const pageActions=(ids:string[])=>{const own=(spec.bulk?.(ids)||[]).map(a=>({...a,run:(...x:Parameters<BulkAction['run']>)=>{actOn(spec.id);return a.run(...x);}}));return [...(universal()?own.map(a=>({...a,primary:false})):own),...theirs()];};
  // The list as the server knows it: Paste names and Select by condition resolve there (on the list's As of day).
  const serverBase=()=>spec.collection?{collection:spec.collection,...(spec.parent?.()?{parent:spec.parent()!}:{}),...(v.as_of?{where:{as_of:v.as_of}}:{})}:null;
  const bulk:{all:HTMLInputElement;bar:HTMLElement;selected:Set<string>;show:(ids:string[])=>void;box:(id:string,label:string)=>HTMLInputElement;group:(ids:string[],label:string)=>HTMLInputElement;head?:HTMLElement;banner?:HTMLElement;keys?:(scope:HTMLElement)=>void}|null=spec.bulk||(editable.length||universal()||theirs().length)&&!ext?bulkSelection(spec.noun,ids=>[...editAction(ids),...pageActions(ids)],undefined,
    // A child list remembers its selection for its parents only (another parent set starts afresh).
    {key:selectionKey(spec.id,spec.parent?.()),held:()=>unsettled().length>0,matching:()=>matches().filter(pickable).map(spec.rowId),all:()=>base().filter(pickable).map(spec.rowId),base:serverBase,asOf:()=>v.as_of?showDay(v.as_of):'',
      // Paged on the server: "Select all N matching" is the server's matches of the search (counted by the resolver);
      // local filters over the loaded rows cannot be sent, so then it stays with the rows loaded.
      server:()=>{const b=serverBase(),n=spec.serverTotal?.(),own=(k:string)=>!!spec.serverFilters?.includes(k);
        // Conditions the server reads (tableServerConditions) are part of its matches; others keep it to the rows loaded.
        const tree=tableServerConditions(spec.id);
        // Also once every page is loaded (Show N more): a server selection taken earlier stays the same selection.
        // "Show changed" narrows the list to rows only this page knows: Select all then takes those rows, not the server's.
        if(!b||n===undefined||onlyChanged||Object.keys(v.filters).some(k=>!own(k))||v.conditions&&!tree)return null;
        const f=Object.fromEntries(Object.entries(v.filters).filter(([k])=>own(k)));
        const so=normalSearch(v.search_options);
        return {...b,where:{...b.where,...(v.q.trim()?{q:v.q.trim()}:{}),...(so?{search_options:so}:{}),...(Object.keys(f).length?{filters:f}:{}),...(tree?{conditions:tree}:{})}};}}):ext?{all:extAll!,bar:el('span',{}),selected:ext.set,show:(ids:string[])=>{extShown=ids;extBoxes.clear();extSync();},box:(id:string,label:string)=>{
    const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':'Select '+label}) as HTMLInputElement;b.checked=ext.set.has(id);
    b.addEventListener('change',()=>{b.checked?ext.set.add(id):ext.set.delete(id);extSync();ext.changed();});b.addEventListener('click',e=>e.stopPropagation());extBoxes.set(id,b);return b;},
    group:(ids:string[],label:string)=>{const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':`Select all ${label}`}) as HTMLInputElement;
      setTriState(b,triStateOf(ids.filter(id=>ext.set.has(id)).length,ids.length));b.addEventListener('click',e=>e.stopPropagation());
      b.addEventListener('change',()=>{for(const id of ids)b.checked?ext.set.add(id):ext.set.delete(id);extBoxes.forEach((x,id)=>x.checked=ext.set.has(id));extSync();ext.changed();});return b;}}:null;
  const pickable=(r:T)=>!spec.selectable||spec.selectable(r),boxOf=(r:T)=>bulk&&pickable(r)?checkTarget(bulk.box(spec.rowId(r),label(r))):null;
  const root=el('div',{class:'dt','data-table':spec.id});
  // "Show changed · N" is on: only the rows the last bulk change changed (read by matches(), so declared first).
  let onlyChanged=false;
  const count=el('span',{class:'dt-count muted',role:'status'});
  let resultChanged:((fields?:boolean)=>void)|null=null;
  // Rows hidden by a search or by filters beyond the view's own say so, with one way back to everything.
  const unresolvedNote=el('div',{class:'dt-unresolved',role:'status',hidden:true});
  const body=el('div',{class:'dt-body'}),pager=el('div',{class:'dt-pager'});
  // While the list is reading, defer the last row open and block row actions on the previous result.
  const busy=()=>body.getAttribute('aria-busy')==='true';
  body.addEventListener('click',e=>{if(busy()&&(e.target as HTMLElement).closest('[data-row] :is(.dt-actions-col,.dt-card-actions) button,[data-row] .dt-more')){
    e.preventDefault();e.stopPropagation();}},true);
  // Another toolbar owns search, filters and views (the planner): this table only groups, sorts and draws.
  const own=spec.toolbar!==false;if(!own){v.q='';v.filters={};v.viewId='';}
  // The Timeline layout's settings (saved with the view) and its period (this visit only: around today at first).
  const timelineNow=():Required<TimelineViewSettings>=>({zoom:'year',hidden:['eligibility'],properties:null,density:'comfortable',weekends:true,bar_label:'name_length',colour_by:'rotation_type',one_line:false,...v.timeline});
  // One line at 9 or 12 months starts two months back (what has just been, then what is coming).
  const home=()=>{const s=timelineNow(),today=departmentToday();return s.one_line&&(zoomNow()==='year'||zoomNow()==='quarter')?plusMonths(today.slice(0,7)+'-01',-2):today;};
  // A phone shows the view's range too: its months swipe under the names (TIMELINE-UX).
  const zoomNow=()=>timelineNow().zoom as Zoom;
  let from='';const window_=()=>timelineWindow(zoomNow(),from||home());
  const period=el('div',{class:'wk-nav dt-period',hidden:true});
  const paintPeriod=()=>{const w=window_(),today=departmentToday(),now=button('Today',()=>{from='';draw();},'ghost small');now.hidden=today>=w.start&&today<w.end;
    period.replaceChildren(iconButton('Previous','chevronLeft',()=>{from=w.step(-1);draw();}),el('strong',{},w.label),iconButton('Next','chevronRight',()=>{from=w.step(1);draw();}),now);};
  const defaultDefinition=():Definition=>({layout:spec.defaults?.layout||'table',hidden:spec.defaults?.hidden||[],filters:{...spec.defaults?.filters},group:spec.defaults?.group||'',sort:spec.defaults?.sort||null,q:'',then:spec.defaults?.then||'',wrap:spec.defaults?.wrap||[],order:[]});
  const layouts=LAYOUTS.filter(l=>(!pin||l[0]===pin.definition.layout)&&(l[0]!=='board'||facetCols.length)&&(l[0]!=='calendar'||!!spec.calendar)&&(l[0]!=='timeline'||!!spec.timeline)&&(l[0]!=='sheet'||!!sheetLayout&&!!spec.collection));
  // Built-in views (a status tab strip, as views): each sets the filters it names; the shown one is the one whose
  // filters match on those properties, so filtering by hand names it too.
  // Before the toolbar: its views count rows as it is built.
  const base=()=>spec.rows.filter(r=>!spec.where||spec.where(r));
  const builtins=()=>spec.views?.()||[];
  type Builtin=ReturnType<typeof builtins>[number];
  const dims=()=>[...new Set(builtins().filter(b=>!b.open).flatMap(b=>Object.keys(b.filters)))];
  const sameSet=(a:string[]=[],b:string[]=[])=>[...a].sort().join('\n')===[...b].sort().join('\n');
  const builtinOn=()=>{if(v.viewId)return undefined;const keys=dims();return builtins().find(b=>!b.open&&keys.every(k=>sameSet(v.filters[k],b.filters[k])));};
  const withView=(b:Builtin)=>{const f={...v.filters};for(const k of dims()){if(b.filters[k]?.length)f[k]=[...b.filters[k]];else delete f[k];}return f;};
  // A view's own count: its rows, whatever is typed in the search (R-35: "All requests 4", not "0" while searching).
  const countOf=(b:Builtin)=>b.count??matches(withView(b),'',null).length;
  const applyBuiltin=(b:Builtin)=>{if(b.open){b.open();return;}v.filters=withView(b);v.viewId='';v.page=0;draw();};
  /** The filter the list shows by itself: the saved view's, else its defaults with the built-in view shown (Status =
   *  Active on People, a status view on Feedback). Filter counts only what differs from it (X-01, A-33). */
  const ownFilter=():FilterState=>{const named=saved.find(x=>x.id===v.viewId);
    if(named)return {filters:named.definition.filters||{},conditions:named.definition.conditions||null};
    const f:Record<string,string[]>={...spec.defaults?.filters},b=builtinOn();
    if(b)for(const k of dims()){if(b.filters[k]?.length)f[k]=b.filters[k];else delete f[k];}
    return {filters:f,conditions:null};};
  const colLabel=(k:string)=>spec.columns.find(c=>c.key===k)?.label||k,isColumn=(k:string)=>spec.columns.some(c=>c.key===k||!!c.filter?.parts?.some(p=>p.key===k));
  // The Filter's typed properties: a column's own key, or each of its parts (Dates · Start / End).
  type Typed={key:string;label:string;parent?:string;kind:ValueKind;value:(row:T)=>unknown};
  // The list's own table in the contract (its columns' fields), else its record type's (field metadata, relative values).
  const defs=TABLE_CONDITION_DEFINITIONS as unknown as Record<string,Record<string,{value_kind:string;operand_field?:string;kind?:string}>>;
  const tableKey=defs[spec.id]?spec.id:spec.collection&&defs[spec.collection]?spec.collection:undefined;
  const typedFields:Typed[]=typedCols.flatMap((c):Typed[]=>c.filter!.parts?c.filter!.parts.map(p=>({key:p.key,label:p.label,parent:c.label,kind:c.filter!.kind,value:p.value}))
    :[{key:c.key,label:c.label,kind:c.filter!.kind,value:c.filter!.value||((r:T)=>c.text?.(r))}])
    // A stored exclusive end (the registry's exclusive_last_day) is matched as the last day shown, projected once here;
    // operands go to the server as chosen (it compares the same way).
    .map(t=>t.kind==='date'&&exclusiveLastDay(spec.columns.find(c=>c.key===t.key)?.server?.field||t.key,tableKey)?{...t,value:(r:T)=>shownLastDay(t.value(r))}:t);
  const typedOf=(key:string)=>typedFields.find(x=>x.key===key);
  const fieldLabel=(k:string)=>{const t=typedOf(k);return t?(t.parent?`${t.parent} · ${t.label}`:t.label):colLabel(k);};
  const have=(r:T)=>(k:string)=>{const t=typedOf(k);return t?[t.value(r)]:list(spec.columns.find(x=>x.key===k)?.facet?.(r));};
  const idsOf=(r:T)=>(k:string)=>{const c=spec.columns.find(x=>x.key===k);return c?.ids?list(c.ids(r)||undefined):undefined;};
  const scope=():ResolveScope=>({context:'filter',tableKey,collection:spec.collection,asOf:v.as_of||''});
  // Relative values of the Filter, resolved by the server once per change; the list redraws when they arrive.
  const resolving=()=>{const rel=relativeLines(v.conditions).filter(x=>resolvable(x.operand)&&!resolvedNow(x.field,x.operand,scope())&&!resolveFailure(x.field,x.operand,scope()));
    if(rel.length)void resolveOperands(rel,scope()).then(()=>{if(rel.some(x=>resolvedNow(x.field,x.operand,scope())||resolveFailure(x.field,x.operand,scope())))draw();});};
  // Relative values the server could not resolve: the list says so (Retry) and holds back what needs its matches.
  const unresolved=()=>relativeLines(v.conditions).filter(x=>resolveFailure(x.field,x.operand,scope()));
  // Still resolving, or not resolved: the list's matches are unknown, so selection by matches waits (bulk.ts held).
  const unsettled=()=>relativeLines(v.conditions).filter(x=>resolvable(x.operand)&&!resolvedNow(x.field,x.operand,scope()));
  const unresolvedWhy=()=>{const f=unresolved();return f.length?`${f.map(x=>operandText(x.operand)).join(', ')} could not be worked out (${resolveFailure(f[0].field,f[0].operand,scope())})`:'';};
  const retryUnresolved=()=>retryResolve(unresolved(),scope()).then(()=>draw());
  // The list's one day ('' = today): its rows, filters, Select by condition and child lists read dated facts on it.
  const setAsOf=(day:string)=>{if((v.as_of||'')===day)return;v.as_of=day;spec.asOf?.(day||departmentToday());};
  /** "As of is [day] ×" — one row at the top of the Filter, the shared date field; × goes back to today. */
  const asOfRow=(changed:()=>void)=>{
    const input=el('input',{value:v.as_of||departmentToday(),'aria-label':'As of'}) as HTMLInputElement,box=dateInput(input);
    input.addEventListener('change',()=>{if(!input.value)return;setAsOf(input.value===departmentToday()?'':input.value);v.page=0;draw();changed();});
    const reset=iconButton('Back to today','close',()=>{input.value=departmentToday();setAsOf('');v.page=0;draw();changed();},{class:'bare cond-x'});
    return el('div',{class:'cond-line dt-as-of'},el('span',{class:'cond-where'},'As of'),el('span',{class:'dt-as-of-op muted'},'is'),box,reset);};
  // Search options (search.ts). A list searched here offers what its columns can tell apart: Search in needs a column of
  // related records, Find a record a column naming that kind's ids (and the server's suggestions for the list's
  // collection). A list the server searches offers what that server applies (its registry entry), sent with its read.
  const linked=(c:Column<T>)=>c.search==='related'||c.search!=='self'&&!!c.ids;
  const refsOf=(c:Column<T>)=>c.refs||(c.ids&&tableKey&&SEARCH_KINDS.has(defs[tableKey]?.[c.key]?.kind||'')?{kind:defs[tableKey][c.key].kind as SearchEntityKind,ids:c.ids}:undefined);
  const can:SearchCapability|null=spec.onSearch?serverCapability(spec.searchIn||spec.collection):(()=>{
    const reg=searchCollection(spec.collection),related=spec.columns.some(c=>c.text&&linked(c));
    const kinds=[...new Set([...ownKind(spec.collection),...spec.columns.flatMap(c=>refsOf(c)?.kind||[])])].filter(k=>!reg||ownKind(spec.collection).includes(k)||(reg.reference_kinds as readonly string[]).includes(k));
    return {scopes:(reg?.scopes||SEARCH_SCOPES.map(x=>x.value)).filter(x=>related||x==='all'),kinds:reg?kinds:[],...(reg?{collection:spec.collection}:{})};})();
  const searchOptions:SearchOptionsSpec|undefined=can?{value:()=>v.search_options||undefined,set:next=>{v.search_options=normalSearch(next);v.page=0;},can}:undefined;
  const parts:Omit<ToolbarSpec,'label'>={
    // A long list searched here waits for a pause in typing (one draw, not one per key: PI-3); a short one answers each key.
    search:{value:v.q,label:spec.searchLabel||'Search '+spec.noun[1],placeholder:spec.searchPlaceholder,get wait(){return spec.onSearch?200:spec.rows.length>LONG_SEARCH?120:undefined;},onInput:q=>{v.q=q;v.page=0;},apply:()=>draw(),options:searchOptions},
    // Filter is the shared condition builder (Where / And-Or / + Add filter / + Add group) over the list's properties,
    // with the list's one "As of" day on top when its facts are dated; column headers add rows to it.
    filter:facetCols.length||typedCols.length||spec.asOf?conditionFilter({label:fieldLabel,
      fields:()=>[...facetCols.filter(c=>!c.filter).map(c=>{const d=c.ids&&tableKey?defs[tableKey]?.[c.key]:undefined;
          return {key:c.key,label:c.label,choices:values(c).map(([x,n])=>({value:x,label:x,hint:c.options?undefined:String(n)})),...(d?.operand_field?{operand:d.operand_field,kind:d.value_kind as ValueKind}:{})};}),
        ...typedFields.map(t=>({key:t.key,label:t.label,kind:t.kind as ValueKind,choices:[],...(t.parent?{parent:t.parent}:{})}))],
      // A built-in view's server scope (a key with no column) stays the view's: not a builder row, kept through Clear.
      get:()=>({filters:Object.fromEntries(Object.entries(v.filters).filter(([k])=>isColumn(k))),conditions:v.conditions||null}),
      set:n=>{v.filters={...n.filters,...Object.fromEntries(Object.entries(v.filters).filter(([k])=>!isColumn(k)))};v.conditions=n.conditions;v.page=0;draw();},
      builder:{context:'filter',asOf:()=>v.as_of||'',collection:spec.collection,tableKey},
      before:spec.asOf?(panel,changed)=>panel.append(asOfRow(changed)):undefined,
      extra:()=>({count:v.as_of?1:0,clear:()=>setAsOf(''),active:v.as_of?[{name:'As of',value:'as of '+showDay(v.as_of),remove:()=>{setAsOf('');v.page=0;draw();}}]:[]}),
      result:()=>count.textContent||'',watch:fn=>{resultChanged=fn;},baseline:ownFilter}):undefined,
    // Highlighted only when changed from this collection's defaults (a default sort or group is not "on").
    group:facetCols.length?{options:facetCols.map(c=>({key:c.key,label:c.label})),value:()=>v.group,set:k=>{v.group=k;if(!k||v.then===k)v.then='';v.page=0;draw();},active:()=>v.group!==(spec.defaults?.group||'')||v.then!==(spec.defaults?.then||'')||groupOrder(v).length>0,
      defaultKey:spec.defaults?.group||'',then:{value:()=>v.then,set:k=>{v.then=k;v.page=0;draw();}},
      order:{value:()=>v.group_sort||[],set:k=>{v.group_sort=k;draw();}},
      levels:(keys,order)=>{v.group=keys[0]||'';v.then=keys[1]||'';v.group_sort=order;v.page=0;draw();},
      // Every group head of what is shown (the whole list, every page), and its Then by heads.
      collapse:{all:()=>{v.collapsed=[...whole.keys()];draw();},none:()=>{v.collapsed=[];draw();}}}:undefined,
    // Several keys (Sort › + Add sort), except where the server orders the pages (one key there).
    sort:sortableCols.length?{options:sortableCols.map(c=>({key:c.key,label:c.label})),value:()=>v.sort,set:x=>{v.sort=x;v.page=0;draw();},active:()=>JSON.stringify(v.sort)!==JSON.stringify(spec.defaults?.sort||null)||!!v.then_sort?.length,
      ...(spec.onSort?{}:{then:{value:()=>v.then_sort||[],set:k=>{v.sort=k[0]||null;v.then_sort=k.slice(1);v.page=0;draw();}}})}:undefined,
    view:{layouts,value:()=>layoutNow().layout,set:k=>{v.layout=k as Layout;v.chosen=true;draw();},active:()=>v.layout!==(spec.defaults?.layout||'table')||[...v.hidden].sort().join()!==[...(spec.defaults?.hidden||[])].sort().join()||[...v.wrap].sort().join()!==[...(spec.defaults?.wrap||[])].sort().join(),
      // Properties: each shown or hidden and moved up or down (the column order, as the column menu's Move left / right).
      // A column drawn only with data (whenAny) is not offered while no loaded row has any.
      build:(panel,changed)=>{
        // The Timeline's own settings first (zoom, layers as the legend, density, weekends); its name column shows the
        // shown properties below.
        if(layoutNow().layout==='timeline'){const s=timelineNow(),put=(x:Partial<TimelineViewSettings>)=>{v.timeline={...timelineNow(),...x};draw();changed();};
          // The start month: a year either side of this month.
          const month=departmentToday().slice(0,7)+'-01',starts=Array.from({length:25},(_,i)=>plusMonths(month,i-12)),w=window_();
          timelineSettings(panel,{zoom:s.zoom as Zoom,setZoom:zoom=>put({zoom}),layers:spec.timeline!.layers(s).map(l=>({...l,on:!!l.fixed||!s.hidden.includes(l.key)})),
            // Read now, not when the popover opened: two choices in one open popover both stay.
            setLayer:(k,on)=>{const h=timelineNow().hidden;put({hidden:on?h.filter(x=>x!==k):[...new Set([...h,k])]});},density:s.density,setDensity:density=>put({density}),weekends:s.weekends,setWeekends:weekends=>put({weekends}),
            oneLine:s.one_line?{start:{value:w.start.slice(0,7)+'-01',options:starts.map(m=>[m,timelineWindow('year',m).label.split(' – ')[0]]),set:m=>{from=m;draw();changed();}},
              labels:{value:s.bar_label==='name'?'name':'short',set:l=>put({bar_label:l})}}:undefined});}
        else panel.append(wrapAllColumns(allWrapped(),on=>{setWrapAll(on);changed();}));if(drawable.length<=2)return;
        panel.append(...shownProperties(()=>orderedCols().slice(1).filter(c=>!c.whenAny||spec.rows.some(c.whenAny)),{hidden:()=>v.hidden,setHidden:h=>{v.hidden=h;draw();changed();},setOrder:keys=>{v.order=keys;draw();changed();}}));}},
    saved:pin?undefined:{name:()=>{const b=builtinOn();return saved.find(x=>x.id===v.viewId)?.name||(b&&b.label!==builtins()[0].label?b.label:'');},
      defaultName:()=>builtins().length?builtinOn()?.label||'Custom view':v.preset||'',warn:()=>builtins().find(b=>b.warn)?.warn||'',dirty:()=>{const a=saved.find(x=>x.id===v.viewId);return !!a&&!same(definitionOf(v,spec.id),a.definition);},
      items:close=>savedViewItems<Definition>({key:spec.id,noun:spec.noun[1],saved,scope:scope(),activeId:v.viewId,current:()=>definitionOf(v,spec.id),dirty:d=>!same(definitionOf(v,spec.id),d),
        builtins:builtins().length||!spec.presets?.length?builtins().map(b=>({label:b.label,count:countOf(b),warn:b.warn,on:b.label===builtinOn()?.label,apply:()=>applyBuiltin(b)}))
          :[{label:'Default view',on:!v.preset,apply:()=>apply(defaultDefinition(),'')},...spec.presets.map(x=>({label:x.label,on:v.preset===x.label,apply:()=>apply({...defaultDefinition(),...x.definition},'',false,x.label)}))],
        apply:x=>apply(x?x.definition:defaultDefinition(),x?.id||''),done:(next,id)=>{saved=next;v.viewId=id;draw();},close,reset:()=>apply(defaultDefinition(),v.viewId,true)})},
  };
  const bar=collectionToolbar({label:spec.noun[1][0].toUpperCase()+spec.noun[1].slice(1),scope:[period,...(spec.controls||[])],actions:spec.actions,...parts});
  // The count and, after a bulk change, Undo last change + History: lifted together into the page footer (dom.ts).
  const footExtra=el('span',{class:'dt-foot-extra'});
  const toolbar=bar.root,changedSlot=el('span',{class:'dt-changed-slot'});toolbar.append(changedSlot,el('span',{class:'grow'}),count,footExtra);
  if(own)root.append(toolbar);
  // Rows of an earlier bulk change kept to resolve: "Resolve N" on the list they belong to.
  const kept=spec.collection&&universal()?stagedNotice({collection:spec.collection,noun:spec.noun,table:spec.id,onDone:async()=>{await spec.afterChange?.();}}):null;
  root.append(unresolvedNote,...(kept?[kept]:[]),...(bulk?.banner?[bulk.banner]:[]),body,pager);if(bulk)root.append(bulk.bar);bulk?.keys?.(body);
  const values=(c:Column<T>)=>{const m=new Map<string,number>();for(const x of c.options||[])m.set(x,0);for(const r of base())for(const x of list(c.facet!(r)))m.set(x,(m.get(x)||0)+1);return [...m.entries()].sort((a,b)=>collator.compare(a[0],b[0]));};
  // The rows the current draw matches, kept until the next draw: ticking a row (bulk "matching") or reading the order
  // again never filters and sorts the whole list anew (AD-2). A draw or new rows start afresh.
  // Holds only between draws (draw() resets it): a change of sort, conditions or a row replaced in place needs a draw first.
  let memo:{source:T[];length:number;out:T[]}|null=null;
  /** `so` null: without the search's options too (a view's own count ignores the whole search). */
  function matches(filters=v.filters,q0=v.q,so:SearchOptions|null|undefined=v.search_options){
    const mine=filters===v.filters&&q0===v.q&&so===v.search_options;
    if(mine&&memo&&memo.source===spec.rows&&memo.length===spec.rows.length)return memo.out;
    resolving();
    // Without options: the list's own search as before (any column's text, People's normalized key too). With options:
    // the one record matcher (search.ts), each column's values on their own, as the server compares them.
    const o=normalSearch(so),plain=!spec.onSearch&&!o?q0.trim().toLowerCase():'',key=plain&&spec.searchKey?.(plain);
    const m=!spec.onSearch&&o?recordMatcher(q0,o):null,cols=spec.columns.filter(c=>c.text),terms=(c:Column<T>,r:T)=>c.terms?list(c.terms(r) as string|string[]):c.ids&&c.facet?list(c.facet(r)):[c.text!(r)];
    const ownCols=cols.filter(c=>!linked(c)),relCols=cols.filter(linked),refCols=spec.columns.flatMap(c=>refsOf(c)||[]);
    const record=(r:T)=>({own:()=>ownCols.flatMap(c=>terms(c,r)),related:()=>relCols.flatMap(c=>terms(c,r)),id:spec.rowId(r),refs:()=>refCols.flatMap(x=>list(x.ids(r) as string|string[]))});
    let rows=base().filter(r=>(m?m(record(r)):!plain||cols.some(c=>{const text=c.text!(r)||'';return text.toLowerCase().includes(plain)||!!key&&!!spec.searchKey?.(text).includes(key);}))&&
      // A filter the server applies (a paged list's Status) has already chosen these rows: not applied again here.
      Object.entries(filters).every(([k,want])=>{const c=spec.columns.find(x=>x.key===k);return spec.serverFilters?.includes(k)||!c?.facet||!want.length||list(c.facet(r)).some(x=>want.includes(x));})&&
      (!onlyChanged||!!recentlyChanged(spec.id)?.has(spec.rowId(r)))&&(!v.conditions||!!spec.onFilter&&!!tableServerConditions(spec.id)||holds(v.conditions,have(r),k=>typedOf(k)?.kind as ValueKind|undefined,scope(),idsOf(r))));
    // Each sort key in turn (Sort › Then): a tie on one is ordered by the next. Every row's keys are read once, then
    // compared (one collator), instead of being worked out again on every comparison (AD-1).
    const keys=(v.sort&&!spec.onSort?[v.sort,...(v.then_sort||[])]:[]).flatMap(([k,dir])=>{const c=sortableCols.find(x=>x.key===k);return c?[{f:(c.sort||c.text)!,d:dir==='asc'?1:-1}]:[];});
    if(keys.length){const keyed=rows.map(r=>({r,k:keys.map(({f})=>{const x=f(r);return typeof x==='number'?x:String(x);})}));
      keyed.sort((a,b)=>{for(let i=0;i<keys.length;i++){const x=a.k[i],y=b.k[i];
        const n=(typeof x==='number'&&typeof y==='number'?x-y:collator.compare(String(x),String(y)))*keys[i].d;if(n)return n;}return 0;});
      rows=keyed.map(x=>x.r);}
    if(mine)memo={source:spec.rows,length:spec.rows.length,out:rows};
    return rows;
  }
  const label=(r:T)=>String(spec.rowLabel?.(r)||spec.columns[0].text?.(r)||spec.noun[0]);
  // When some shown row has a ⋯ menu, rows without one keep an invisible slot so the buttons line up.
  let menuSlot=false;
  const moreMenu=(r:T)=>{const acts=spec.rowActions?.(r)||[];if(!acts.length)return menuSlot?el('span',{class:'dt-more-gap','aria-hidden':'true'}):null;
    const more=opens(el('button',{type:'button',class:'icon-button bare dt-more','aria-label':'More actions for '+label(r)},icon('dots')),'menu');
    // D-057: the menu stays on its row across a redraw (the row's id in this table); the items then come from menuSource below.
    more.addEventListener('click',e=>{e.stopPropagation();menu(more,'Row actions',acts,{placement:'below-end',keep:`[data-table="${CSS.escape(spec.id)}"] ${rowMark(spec.rowId(r))} .dt-more`});});
    return more;};
  // A kept ⋯ menu's items: the actions of the row as this table holds it now (never the row object of an earlier draw).
  menuSource(root,more=>{const id=more.closest<HTMLElement>('[data-row]')?.dataset.row,r=spec.rows.find(x=>spec.rowId(x)===id);return r&&spec.rowActions?.(r)||[];});
  // The rows in the order drawn (page by page, by group, without collapsed groups): ↑ ↓ in the side panel follow it.
  const drawnOrder=()=>{const rows=matches(),{groupKey}=layoutNow(),then=v.then&&v.then!==groupKey?v.then:'',seen=new Set<string>(),out:{r:T;page:number}[]=[];
    const flat=(items:T[])=>!groupKey?items:grouped(items,groupKey).flatMap(([k,rs])=>v.collapsed.includes(k)?[]:!then?rs:grouped(rs,then).flatMap(([k2,s])=>v.collapsed.includes(k+' › '+k2)?[]:s));
    const per=size();for(let p=0;p*per<rows.length;p++)for(const r of flat(rows.slice(p*per,(p+1)*per))){const id=spec.rowId(r);if(!seen.has(id)){seen.add(id);out.push({r,page:p});}}
    return out;};
  // ↑ ↓ step through every row (other pages, rows behind "Show N more"): the one opened is drawn first.
  const positionOf=(order:{r:T;page:number}[],i:number):PanelPosition=>({index:i,total:order.length,noun:spec.noun[0],go:j=>{const x=drawnOrder()[j];if(!x)return;
    if(x.page!==v.page){v.page=x.page;draw();}
    if(!body.querySelector(rowMark(spec.rowId(x.r)))){const {groupKey}=layoutNow(),then=v.then&&v.then!==groupKey?v.then:'',keys=(k:string)=>{const l=list(spec.columns.find(c=>c.key===k)?.facet?.(x.r));return l.length?l:['—'];};
      for(const k of keys(groupKey)){unlimited.add(k);if(then)for(const k2 of keys(then))unlimited.add(k+' › '+k2);}draw();}
    openRow(x.r);}});
  let pendingOpen:string|null=null,pendingRead:Promise<unknown>|null=null;
  const openRow=(r:T)=>{const id=spec.rowId(r);if(busy()){pendingOpen=id;return;}
    const list=drawnOrder(),i=list.findIndex(x=>spec.rowId(x.r)===id);spec.onOpen!(r,i<0?undefined:positionOf(list,i));};
  const trackRead=(read:void|Promise<unknown>)=>{
    if(!(read instanceof Promise))return;
    pendingRead=read;
    const settled=()=>{
      if(pendingRead!==read)return;  // a newer read owns the list
      pendingRead=null;body.setAttribute('aria-busy','false');
      const id=pendingOpen;pendingOpen=null;
      if(id===null||account!==tableAccount||!root.isConnected||!body.querySelector(rowMark(id)))return;
      const row=spec.rows.find(r=>spec.rowId(r)===id);
      if(row!==undefined)openRow(row);
    };
    void read.then(settled,settled);
  };
  const opener=(node:HTMLElement,r:T)=>{spec.decorate?.(r,node);node.dataset.row=spec.rowId(r);
    // Just changed by a bulk change: marked "Changed" for a while (text, not colour alone).
    if(recentlyChanged(spec.id)?.has(spec.rowId(r))){node.classList.add('dt-changed');node.querySelector('td[data-col],.dt-card-title')?.append(badge('Changed','ok','dt-changed-mark'));}if(spec.onOpen){node.classList.add('dt-open');node.addEventListener('click',e=>{
    // Only a control inside the row keeps the click; a table placed inside <details> (a collapsible section) still opens rows.
    const inner=(e.target as HTMLElement).closest('button,a,input,select,label,summary,details,.dt-edit,.dt-editor');if(!inner||!node.contains(inner))openRow(r);});
    // A card opens from the keyboard too, and is where focus returns when its panel closes.
    if(node.tagName==='ARTICLE'){node.tabIndex=0;node.addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target===node){e.preventDefault();openRow(r);}});}}return node;};
  const actionsOf=(r:T)=>[...(spec.rowButtons?.(r)||[]),moreMenu(r)];
  // Grouped by a column whose cells are only the group's name as plain text: the group header already says it, so the
  // column is not repeated (AC-02). Badges (a tone, a tooltip), richer cells and the first column stay.
  const colsOf=(items:T[],groupKey:string)=>{const same=(c:Column<T>)=>items.every(r=>{const f=c.facet?.(r),v=c.cell(r);return typeof v==='string'&&(Array.isArray(f)?f.join(', '):String(f??''))===v.trim();});
    return shownCols().filter((c,i)=>i===0||c.key!==groupKey||!same(c));};
  const rowOf=(r:T,cols:Column<T>[])=>opener(el('tr',{'data-row':spec.rowId(r)},...(bulk?[el('td',{class:'dt-check'},boxOf(r))]:[]),...cols.map(c=>cellOf(r,c)),
    ...(spec.rowActions||spec.rowButtons?[el('td',{class:'dt-actions-col'},...actionsOf(r))]:[])),r);
  // Group headings carry a box that ticks the group (mixed when part of it is ticked; rolls up to select-all).
  const headingRow=(k:string,rows:T[],closed:boolean,key:string,width:number,sub=false)=>el('tr',{class:'dt-group'+(sub?' dt-subgroup':'')},el('td',{colspan:String(width)},...groupHead(k,rows,closed,key)));
  const roving=new WeakMap<HTMLElement,()=>void>();
  // The table drawn last: Show more (more rows of the same list, the earlier ones unchanged) appends to it instead of
  // building every row again, so 300 rows cost what 100 new ones do (SH-02).
  let drawn:{sig:string;items:T[];tbody:HTMLElement;groupKey:string;cols:string}|null=null;
  function appendRows(items:T[],groupKey:string):boolean{
    const was=drawn;if(!was||!was.tbody.isConnected||was.groupKey!==groupKey||items.length<=was.items.length||!was.items.every((r,i)=>items[i]===r))return false;
    const cols=colsOf(items,groupKey);if(cols.map(c=>c.key).join()!==was.cols)return false;
    const tbody=was.tbody,width=tbody.closest('table')!.tHead!.rows[0].cells.length,old=new Set(was.items);
    // Every box was handed out again by this draw (bulk.show): the kept rows take fresh ones, in place.
    const byId=new Map(was.items.map(r=>[spec.rowId(r),r]));
    for(const tr of tbody.querySelectorAll<HTMLElement>(':scope>tr[data-row]')){const r=byId.get(tr.dataset.row!),cell=tr.querySelector(':scope>td.dt-check');
      if(r&&cell){const b=boxOf(r);cell.replaceChildren(...(b?[b]:[]));}}
    if(!groupKey)tbody.append(...items.slice(was.items.length).map(r=>rowOf(r,cols)));
    else{const headings=new Map<string,HTMLElement>();
      for(const tr of tbody.querySelectorAll<HTMLElement>(':scope>tr.dt-group'))headings.set(tr.querySelector<HTMLElement>('.dt-group-toggle')?.dataset.group||'',tr);
      const groups=grouped(items,groupKey);
      groups.forEach(([k,rows],i)=>{const closed=v.collapsed.includes(k),next=groups.slice(i+1).map(([x])=>headings.get(x)).find(Boolean)||null;
        // A group's heading (its count and box) is drawn again; its rows stay.
        const prior=headings.get(k),head=headingRow(k,rows,closed,k,width);
        if(prior)tbody.replaceChild(head,prior);else tbody.insertBefore(head,next);
        if(!closed)for(const r of rows)if(!old.has(r))tbody.insertBefore(rowOf(r,cols),next);});}
    roving.get(tbody)?.();
    drawn={...was,items};return true;
  }
  // ---- Column width and order by pointer ----
  // Width: the heading's right edge is a focusable separator (WAI-ARIA window splitter): drag it, ← → (Shift: larger
  // steps) or double-click it to fit the contents again; the column menu's Reset width does the same.
  function setWidth(key:string,w:number|undefined){const rest={...v.widths};if(w)rest[key]=w;else delete rest[key];v.widths=rest;save(stateId,v);}
  function widthGrip(c:Column<T>){return columnGrip({label:`${c.label} column`,width:()=>v.widths?.[c.key],keep:w=>setWidth(c.key,w),size:(w,g)=>sizeColumn(g.closest('table')!,c.key,w)});}
  // Order: drag a heading onto another column (table_menu.ts headerReorder). The first column stays first. It takes the
  // target's place: the columns between move one step towards where it came from.
  function dragToOrder(th:HTMLElement,c:Column<T>){headerReorder(th,to=>{
    const keys=orderedCols().slice(1).map(x=>x.key),from=keys.indexOf(c.key),at=keys.indexOf(to.dataset.col!);
    if(from<0||at<0)return;keys.splice(from,1);keys.splice(at,0,c.key);v.order=keys;draw();
    body.querySelector<HTMLElement>(`thead th[data-col="${CSS.escape(c.key)}"] .dt-sort`)?.focus({preventScroll:true});});}
  function tableOf(items:T[],groupKey:string){
    const cols=colsOf(items,groupKey);
    const head=el('tr',{},...(bulk?[el('th',{class:'dt-check'},bulk.head||checkTarget(bulk.all))]:[]),...cols.map((c,i)=>{
      const cls=[c.num?'num':'',c.class||'',v.wrap.includes(c.key)?'dt-wrap':''].join(' ').trim();
      const on=v.sort?.[0]===c.key,dir=on?v.sort![1]:null;
      // One menu per column header (table_menu.ts): sort, filter, group, wrap, hide, move; saved with the view.
      const others=cols.slice(1),at=others.indexOf(c);
      const b=columnHeader({label:c.label,
        sort:sortableCols.includes(c)?{dir,set:d=>{v.sort=d?[c.key,d]:null;v.then_sort=[];v.page=0;draw();}}:undefined,
        filter:own&&c.facet&&values(c).length?panel=>panel.append(multiPick(c.label,values(c).map(([x,n])=>({value:x,label:x,hint:c.options?undefined:String(n)})),v.filters[c.key]||[],
          picked=>{if(picked.length)v.filters[c.key]=picked;else delete v.filters[c.key];v.page=0;draw();},'Any').wrap):undefined,
        group:c.facet?{on:v.group===c.key,set:g=>{v.group=g?c.key:'';if(!g||v.then===c.key)v.then='';v.page=0;draw();}}:undefined,
        wrap:{on:v.wrap.includes(c.key),set:w=>{v.wrap=w?[...v.wrap,c.key]:v.wrap.filter(k=>k!==c.key);draw();}},
        wrapAll:{on:allWrapped(),set:setWrapAll},
        hide:i>0?()=>{v.hidden=[...v.hidden,c.key];draw();}:undefined,
        move:i>0&&others.length>1?{left:at>0,right:at<others.length-1,go:by=>{const keys=orderedCols().slice(1).map(x=>x.key),from=keys.indexOf(c.key),to=from+by;
          [keys[from],keys[to]]=[keys[to],keys[from]];v.order=keys;draw();
          [...body.querySelectorAll<HTMLElement>('thead .dt-sort')].find(x=>x.firstChild?.textContent===c.label)?.focus({preventScroll:true});}}:undefined,
        // Read when the menu opens: a resize since this heading was drawn offers it at once, a reset withdraws it.
        get resetWidth(){return v.widths?.[c.key]?()=>{setWidth(c.key,undefined);draw();}:undefined;},
        // D-057: an open column menu stays on this column of this table across a redraw (a late refresh included).
        keep:`[data-table="${CSS.escape(spec.id)}"] thead th[data-col="${CSS.escape(c.key)}"] > .dt-sort`});
      // Its name is the column's alone: the resize separator inside it is a control of its own, named by itself.
      const th=el('th',{scope:'col',class:cls,'aria-label':c.label||undefined,'data-col':c.key,'data-reorder':i>0&&others.length>1?'':undefined,...(sortableCols.includes(c)?{'aria-sort':on?(dir==='asc'?'ascending':'descending'):'none'}:{})},b,widthGrip(c));
      sized(th,v.widths?.[c.key]);if(i>0&&others.length>1)dragToOrder(th,c);return th;}),
      ...(spec.rowActions||spec.rowButtons?[el('th',{class:'dt-actions-col'},el('span',{class:'sr-only'},'Actions'))]:[]));
    trackWidths(head);
    const width=head.childElementCount,tbody=el('tbody',{});
    const row=(r:T)=>rowOf(r,cols),heading=(k:string,rows:T[],closed:boolean,key:string,sub=false)=>headingRow(k,rows,closed,key,width,sub);
    const then=v.then&&v.then!==groupKey?v.then:'';
    if(groupKey){for(const [k,rows] of grouped(items,groupKey)){const closed=v.collapsed.includes(k);tbody.append(heading(k,rows,closed,k));if(closed)continue;
      if(!then){const [few,more]=limited(k,rows);tbody.append(...few.map(row),...(more?[el('tr',{class:'dt-more-row'},el('td',{colspan:String(width)},more))]:[]));continue;}
      for(const [k2,sub] of grouped(rows,then)){const key=k+' › '+k2,shut=v.collapsed.includes(key);tbody.append(heading(k2,sub,shut,key,true));if(!shut)tbody.append(...sub.map(row));}}}
    else tbody.append(...items.map(row));
    roving.set(tbody,roveRows(tbody));
    drawn={sig:'',items,tbody,groupKey,cols:cols.map(c=>c.key).join()};
    const hint=`dt-rove-${++roveSequence}`;
    // The hint follows the table: the table stays the scroller's first child (its height is the content's).
    return scrollEdge(el('div',{class:'table-scroll',tabindex:0,'aria-label':`${spec.noun[1]} table`},
      // Every shared table is named by its caption (WCAG 1.3.1, QA-127e), visually hidden: the page heading says it.
      el('table',{class:'dt-table','aria-describedby':hint},el('caption',{class:'sr-only'},spec.noun[1].charAt(0).toUpperCase()+spec.noun[1].slice(1)),el('thead',{},head),tbody),el('span',{class:'sr-only',id:hint},ROVE_HINT)));
  }
  // ---- Inline editing ----
  function cellOf(r:T,c:Column<T>){
    // Plain text stays on one line (a long job title must not make the row five lines tall); the whole text is the tooltip.
    const value=c.cell(r);
    const td=el('td',{class:[c.num?'num':'',c.class||'',v.wrap.includes(c.key)?'dt-wrap':''].join(' ').trim(),'data-col':c.key},typeof value==='string'&&value?el('span',{class:'dt-clip',title:value},value):value);
    sized(td,v.widths?.[c.key]);
    if(!c.edit)return td;
    td.classList.add('dt-edit');td.tabIndex=0;td.setAttribute('aria-label',`${c.label}: ${c.text?.(r)||''}. Press Enter to edit`);
    const start=(e:Event)=>{const inner=(e.target as HTMLElement).closest('button,a,input,select,textarea,details');if(inner&&td.contains(inner))return;e.stopPropagation();startEdit(td,r,c);};
    td.addEventListener('click',start);td.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target===td){e.preventDefault();startEdit(td,r,c);}});
    return td;
  }
  /** Rows an edit applies to: every selected row when this row is one of several selected, otherwise just this row. */
  const targetsOf=(r:T)=>{const id=spec.rowId(r),sel=bulk?.selected;return sel&&sel.has(id)&&sel.size>1?rowsById([...sel]):[r];};
  async function commit(c:Column<T>,rows:T[],value:unknown,restore:()=>void){
    let saved:T[]|void;
    try{saved=await c.edit!.save(rows,value);}
    catch(err){if(sessionChanged(err))return;notify((err as Error).message,'error',undefined,err);restore();return;}
    if(!c.edit!.quiet)notify(rows.length>1?`${c.label} updated for ${rows.length} ${spec.noun[1]}.`:`${c.label} saved.`);
    keepSavedRows(saved);
    await refreshAfterEdit(c);
  }
  function keepSavedRows(saved:T[]|void){
    if(!saved?.length)return;
    const byId=new Map(saved.map(r=>[spec.rowId(r),r]));spec.rows=spec.rows.map(r=>byId.get(spec.rowId(r))||r);
    if(root.isConnected)draw();
  }
  async function refreshAfterEdit(c:Column<T>){
    try{await c.edit!.afterSave?.();}
    catch(err){if(!sessionChanged(err))notify('Changes saved, but the table could not refresh. '+(err as Error).message,'error',undefined,err);}
  }
  function editorFor(c:Column<T>,current:unknown){
    const e=c.edit!;
    if(e.kind==='select'){const f=select(c.label,e.options?.()||[],String(current??''),!!e.required);return {node:f.input as HTMLElement,read:()=>f.input.value};}
    if(e.kind==='multi'){const f=multiPick(c.label,e.options?.()||[],(current as string[])||[],undefined,'None',!!e.creatable);setTimeout(()=>{if(f.wrap.isConnected)f.open();},0);return {node:f.wrap,read:()=>f.values()};}
    const input=el('input',{type:e.kind==='number'?'number':e.kind==='date'?'date':'text',value:String(current??''),'aria-label':c.label,...(e.maxLength?{maxLength:e.maxLength}:{}),...(e.required?{required:true}:{})}) as HTMLInputElement;
    // A date is the shared date picker (date_picker.ts); its value stays ISO.
    return {node:e.kind==='date'?dateInput(input):input as HTMLElement,read:()=>e.kind==='number'?(input.value===''?null:Number(input.value)):input.value};
  }
  /** Tab / Shift+Tab: the next or previous editable cell (across rows) opens once this edit is kept. */
  function moveFrom(r:T,c:Column<T>,dir:1|-1){
    // A saved edit may redraw the whole page; continue in the replacement table, never its detached predecessor.
    const liveBody=root.isConnected?body:document.querySelector<HTMLElement>(`[data-table="${CSS.escape(spec.id)}"] .dt-body`);
    const cells=[...(liveBody?.querySelectorAll<HTMLElement>('td.dt-edit')||[])],id=spec.rowId(r);
    const at=cells.findIndex(td=>td.dataset.col===c.key&&td.closest('tr')?.dataset.row===id),next=at<0?null:cells[at+dir];
    if(next){next.focus();next.click();}
  }
  function startEdit(td:HTMLElement,r:T,c:Column<T>){
    const e=c.edit!,targets=targetsOf(r),current=e.value(r);
    if(e.kind==='toggle'){void commit(c,targets,!current,()=>draw());return;}
    if(e.kind==='custom'){popover(td,(panel,close)=>{
      let done=false;const finish=(save:boolean,move?:1|-1)=>{if(done)return;done=true;const value=ed.read();close();
        const kept=save&&JSON.stringify(value)!==JSON.stringify(current)?commit(c,targets,value,()=>draw()):Promise.resolve();
        if(move)void kept.then(()=>moveFrom(r,c,move));};
      const ed=e.editor!(r,finish);
      panel.classList.add('dt-cell-pop');
      panel.append(el('div',{class:'dt-pop-label'},`${c.label}${targets.length>1?` · all ${targets.length} selected`:''}`),ed.node,
        el('div',{class:'actions'},button('Cancel',()=>{done=true;close();},'ghost small'),button('Save',()=>finish(true),'primary small')));},{label:'Edit '+c.label});return;}
    const before=[...td.childNodes],restore=()=>{td.replaceChildren(...before);td.classList.remove('editing','saving');};
    const ed=editorFor(c,current);let done=false;
    const finish=(save:boolean,move?:1|-1)=>{if(done)return;done=true;const value=ed.read(),then=()=>{if(move)moveFrom(r,c,move);};
      if(!save||JSON.stringify(value)===JSON.stringify(current)){restore();then();return;}
      if(e.required&&(value===''||value===null||(Array.isArray(value)&&!value.length))){notify(`${c.label} cannot be empty.`,'warning');restore();return;}
      td.classList.add('saving');void commit(c,targets,value,restore).then(then);};
    const bar=el('div',{class:'dt-editor'},ed.node,...(targets.length>1?[el('span',{class:'dt-edit-scope'},`All ${targets.length} selected`)]:[]),
      ...(e.kind==='multi'?[button('Save',()=>finish(true),'primary small')]:[]));
    td.replaceChildren(bar);td.classList.add('editing');
    const focusable=(ed.node.matches('input,select')?ed.node:ed.node.querySelector('input'))as HTMLInputElement|null;focusable?.focus();if(focusable?.select)focusable.select();
    bar.addEventListener('keydown',ev=>{if(ev.key==='Enter'&&e.kind!=='multi'){ev.preventDefault();finish(true);}
      if(ev.key==='Tab'&&e.kind!=='multi'&&!(ev.target as HTMLElement).closest('.date-box button')){ev.preventDefault();finish(true,ev.shiftKey?-1:1);}if(ev.key==='Escape'){ev.preventDefault();ev.stopPropagation();finish(false);}});
    bar.addEventListener('click',ev=>ev.stopPropagation());
    if(e.kind==='select')ed.node.addEventListener('change',()=>finish(true));
    // focusout: a date editor is a box (input + calendar button); its calendar is not leaving the cell.
    if(e.kind!=='multi')ed.node.addEventListener('focusout',()=>setTimeout(()=>{if(!bar.contains(document.activeElement)&&!document.activeElement?.closest('.date-pop'))finish(true);},0));
  }
  /** Bulk bar "Edit field…": choose a property and one value for every selected row. */
  function editDialog(rows:T[],done:()=>void){
    const editable=fieldEditable;
    formDialog(`Edit ${rows.length} ${spec.noun[1]}`,'The same value is saved to every selected record, checked like a single edit.',form=>{
      const which=select('Field',editable.map(c=>({value:c.key,label:c.label})),editable[0].key);
      const slot=el('div',{class:'field'});let read=():unknown=>null;
      const drawField=()=>{const c=editable.find(x=>x.key===which.input.value)!;
        if(c.edit!.kind==='toggle'){const t=check(c.label,true);slot.replaceChildren(t.wrap);read=()=>t.input.checked;return;}
        const ed=editorFor(c,c.edit!.kind==='multi'?[]:'');slot.replaceChildren(...(ed.node.matches('input,select')?[el('label',{},c.label)]:[]),ed.node);read=ed.read;};
      which.input.addEventListener('change',drawField);drawField();form.append(which.wrap,slot);
      return async()=>{const c=editable.find(x=>x.key===which.input.value)!;const saved=await c.edit!.save(rows,read());notify(`${c.label} updated for ${rows.length} ${spec.noun[1]}.`);keepSavedRows(saved);await refreshAfterEdit(c);done();};
    },'Apply to all');
  }
  /** View settings › Wrap all columns: every column wraps (pills all shown) or none does. */
  const allWrapped=()=>drawable.every(c=>v.wrap.includes(c.key));
  const setWrapAll=(on:boolean)=>{v.wrap=on?drawable.map(c=>c.key):[];draw();};
  function cardOf(r:T){
    const [first,...rest]=shownCols();
    const buttons=spec.rowButtons?.(r)||[];
    if(spec.card){const {layout,groupKey}=layoutNow(),c=spec.card(r,{group:groupKey,then:layout==='cards'&&groupKey&&v.then!==groupKey?v.then:''});return opener(el('article',{class:'dt-card dt-card-compact'},el('div',{class:'dt-card-head'},boxOf(r),
      el('div',{class:'dt-card-title'},c.title),moreMenu(r)),el('div',{class:'dt-card-line muted'},c.line)),r);}
    // The ⋯ menu sits top-right in the head; the actions row is drawn only for visible buttons.
    return opener(el('article',{class:'dt-card'},el('div',{class:'dt-card-head'},boxOf(r),el('div',{class:'dt-card-title'},first.cell(r)),moreMenu(r)),
      el('dl',{class:'dt-card-props'},...rest.flatMap(c=>{const val=c.cell(r);
        // Under its group's heading a card does not repeat that value ("Status: Waiting" under "Waiting"); more than it says stays.
        const said=typeof val==='string'?val:val instanceof Node?val.textContent:null,group=layoutNow().groupKey;
        if(c.key===group&&said!==null&&list(c.facet?.(r)).includes(said.trim()))return [];
        // A card lists only what the record has: its facts are labelled, so an empty one ("—") needs no line (X-04).
        // A drawn value with no text (an icon, a swatch) still says something and stays.
        return val===null||val===undefined||said?.trim()==='—'||typeof val==='string'&&!val.trim()?[]:[el('dt',{},c.label),el('dd',{},val)];})),
      buttons.length?el('div',{class:'dt-card-actions'},...buttons):null),r);
  }
  // A group's heading counts, and its box selects, the whole group on every page, not only the rows on this page.
  let whole=new Map<string,T[]>();
  const wholeGroups=(rows:T[],groupKey:string,then:string)=>{const m=new Map<string,T[]>();if(!groupKey)return m;
    for(const [k,rs] of grouped(rows,groupKey)){m.set(k,rs);if(then)for(const [k2,s] of grouped(rs,then))m.set(k+' › '+k2,s);}return m;};
  const groupHead=(k:string,rows:T[],closed:boolean,key:string)=>{const all=whole.get(key)||rows,ids=all.filter(pickable).map(spec.rowId);
    const box=ids.length?bulk?.group?.(ids,k==='—'?'without a value':k):null;
    return [box?checkTarget(box):null,groupToggle(key,groupCount(key,k,all.length),closed,k),spec.groupAction?.(k,all)];};
  // A list paged on the server knows how many a group holds beyond the rows read so far ("100 of 411").
  const groupCount=(key:string,k:string,loaded:number)=>{const total=key===k?spec.groupTotal?.(layoutNow().groupKey,k):undefined;return total&&total>loaded?`${loaded} of ${total}`:String(loaded);};
  // Groups opened past their limit this visit ("Show N more").
  const unlimited=new Set<string>();
  const limited=(k:string,rows:T[]):[T[],HTMLElement|null]=>{const n=spec.groupLimit;if(!n||unlimited.has(k)||rows.length<=n+1)return [rows,null];
    return [rows.slice(0,n),button(`Show ${rows.length-n} more`,()=>{unlimited.add(k);draw();},'link-button',{'aria-label':`Show ${rows.length-n} more in ${k==='—'?'None':k}`})];};
  // The redraw replaces the toggle: keep keyboard focus on the same group instead of dropping it to the page.
  const groupToggle=(k:string,n:string,closed:boolean,name=k)=>sharedGroupToggle(el('strong',name.length>56?{'data-tip':name}:{},name==='—'?'None':name),n,!closed,()=>{
    v.collapsed=closed?v.collapsed.filter(x=>x!==k):[...v.collapsed,k];draw();[...body.querySelectorAll<HTMLElement>('.dt-group-toggle')].find(x=>x.dataset.group===k)?.focus({preventScroll:true});},
    {cls:'dt-group-toggle',attrs:{'data-group':k}});
  function grouped(items:T[],key:string){
    const c=spec.columns.find(x=>x.key===key),groups=new Map<string,T[]>();
    const first=(keys:string[])=>Array.isArray(c?.ordered)?[...keys].sort((a,b)=>((c.ordered as string[]).indexOf(a)+1||1e9)-((c.ordered as string[]).indexOf(b)+1||1e9)).slice(0,1):keys.slice(0,1);
    for(const r of items){const all=list(c?.facet?.(r)),keys=c?.groupFirst?first(all):all;for(const k of keys.length?keys:['—']){const g=groups.get(k);if(g)g.push(r);else groups.set(k,[r]);}}
    // Z → A turns the level's own order round (a dated or ranked order too); "None" stays last either way.
    const way=v.group_sort?.some(([k,d])=>k===key&&d==='desc')?-1:1;
    if(Array.isArray(c?.ordered)){const order=c.ordered,rank=(k:string)=>{const i=order.indexOf(k);return i<0?order.length:i;};
      return [...groups.entries()].sort((a,b)=>way*(rank(a[0])-rank(b[0])));}
    if(c?.ordered)return way<0?[...groups.entries()].reverse():[...groups.entries()];
    // Status groups read in order of what needs doing: waiting first, then done, then ended; other groups A–Z.
    const rank=(k:string)=>key!=='status'?0:/^(Waiting|Ready|Needs|Open|Proposed|Pending|Invited)/i.test(k)?0:/^(Done|Approved|Active|Current|Published|Can sign in)/i.test(k)?1:2;
    return [...groups.entries()].sort((a,b)=>a[0]==='—'?1:b[0]==='—'?-1:way*(rank(a[0])-rank(b[0])||a[0].localeCompare(b[0])));
  }
  function layoutNow(){
    // On a phone a wide table is hard to read: cards by default, unless the person picked a layout.
    const narrow=!spec.print&&!v.chosen&&!v.viewId&&v.layout==='table'&&narrowQuery.matches;
    const layout=narrow?'cards':v.layout==='board'&&!facetCols.length||v.layout==='calendar'&&!spec.calendar||v.layout==='timeline'&&!spec.timeline||v.layout==='sheet'&&!(sheetLayout&&spec.collection)?'table':v.layout;
    return {layout,groupKey:layout==='board'?(v.group||facetCols[0]?.key||''):v.group};
  }
  let reported:string|null=null,reportedSearch:string|null=null,reportedSort:string|null=null;
  const searchKey=()=>v.q.trim()+'\n'+JSON.stringify(normalSearch(v.search_options)??null);
  // After a change the owning list reads its records again (afterChange) before the rows are drawn anew.
  const reload=async()=>{await spec.afterChange?.();if(root.isConnected)draw();};
  function draw(resetScroll=false){
    if(account!==tableAccount)return;  // a delayed search from a signed-out table cannot write into another account
    const scroller=body.querySelector<HTMLElement>(':scope>.table-scroll');
    const left=scroller?.isConnected?scroller.scrollLeft:v.scroll?.left||0,top=scroller?.isConnected?scroller.scrollTop:v.scroll?.top||0;
    const active=document.activeElement as HTMLElement|null,focused=active&&root.contains(active)?active:null;
    const row=focused?.closest<HTMLElement>('[data-row]'),cell=focused?.closest<HTMLElement>('[data-col]'),group=focused?.closest<HTMLElement>('[data-group]');
    const col=cell?`[data-col="${CSS.escape(cell.dataset.col!)}"]`:'';
    const focusSelector=row?rowMark(row.dataset.row!)+(col?' '+col:''):cell?'thead '+col:group?`[data-group="${CSS.escape(group.dataset.group!)}"]`:
      focused?.hasAttribute('aria-label')?`[aria-label="${CSS.escape(focused.getAttribute('aria-label')!)}"]`:null;
    const focusScopes=focusSelector?[...root.querySelectorAll<HTMLElement>(focusSelector)]:[],focusAt=focusScopes.findIndex(n=>n===focused||!!focused&&n.contains(focused));
    const focusScope=focusScopes[focusAt],focusables='button,input,select,textarea,a[href],[tabindex]';
    const focusIndex=focusScope&&focused!==focusScope?[...focusScope.querySelectorAll<HTMLElement>(focusables)].indexOf(focused!):-1;
    // Older views may name a column that never supplied a usable sort value.
    if(v.sort&&!sortableCols.some(c=>c.key===v.sort![0]))v.sort=null;
    v.then_sort=v.sort?(v.then_sort||[]).filter(([k])=>k!==v.sort![0]&&sortableCols.some(c=>c.key===k)):[];
    save(stateId,v);
    // A paged list hears of its filters and of its condition tree as the server reads it (also when first drawn with one).
    const tree=v.conditions?serverTree(v.conditions,spec.columns) as Conditions|null:null;serverTrees.set(spec.id,tree);
    const f=JSON.stringify(v.filters),heard=f+JSON.stringify(tree);
    if(reported===null?!!tree:heard!==reported)trackRead(spec.onFilter?.(v.filters));reported=heard;
    // The text and its options are one search: a change of either reads again.
    const q=v.q.trim(),sk=searchKey(),searched=reportedSearch!==null&&sk!==reportedSearch&&!!spec.onSearch;
    if(searched)trackRead(spec.onSearch!(q,normalSearch(v.search_options)));reportedSearch=sk;
    const sort=JSON.stringify(v.sort);if(reportedSort!==null&&sort!==reportedSort)trackRead(spec.onSort?.(v.sort));reportedSort=sort;
    memo=null;
    // A search the server runs: the rows shown stay (marked busy) until its answer redraws the list once, instead of
    // drawing the old rows again first (SH-05). The page reads again on every search (onSearch's contract).
    body.setAttribute('aria-busy',String(searched||!!pendingRead));  // a search answered by a later redraw is busy until then
    if(searched&&body.firstElementChild){bar.sync();return;}
    const {layout,groupKey}=layoutNow();
    const rows=matches(),per=size(),pages=Math.max(1,Math.ceil(rows.length/per));v.page=Math.min(v.page,pages-1);
    // A changed result set starts at the top. Selection, expansion, wrapping and data refresh keep the reader's place.
    const scope=JSON.stringify([spec.resultScope?.()??location.hash,q,f,v.sort,v.then_sort,v.page,layout,groupKey,v.then]),keepTop=!resetScroll&&scope===v.scroll?.scope;
    v.scroll={scope,left,top:keepTop?top:0};
    // shortcut: a calendar shows every matching row (no pages), so a group is never split across pages; page it when
    // one list's calendar outgrows a few hundred rows.
    const shown=layout==='calendar'||layout==='timeline'?rows:rows.slice(v.page*per,(v.page+1)*per);
    period.hidden=layout!=='timeline';if(layout==='timeline')paintPeriod();
    whole=wholeGroups(rows,groupKey,layout!=='board'&&v.then&&v.then!==groupKey?v.then:'');
    bulk?.show(shown.filter(pickable).map(spec.rowId));
    menuSlot=!!spec.rowActions&&shown.some(r=>spec.rowActions!(r).length>0);
    // The count is out of what the page offers (after its own `where`); the source total keeps recovery controls visible.
    const total=spec.rows.length,offered=spec.where?base().length:total;
    const note=spec.countNote?.(rows);
    // SW3-D-24: a server-paged list counts what the server holds, not the page it has loaded.
    const held=spec.serverTotal?.(),counted=held!==undefined&&held>offered&&rows.length===offered?held:offered;
    count.textContent=(rows.length===offered?`${counted} ${counted===1?spec.noun[0]:spec.noun[1]}`:`${rows.length} of ${offered} ${spec.noun[1]}`)+(note?' · '+note:'');
    resultChanged?.();
    const filtered=Object.keys(v.filters).length>0||!!v.conditions||!!v.q.trim()||!!normalSearch(v.search_options)?.reference||!!spec.narrowed?.();
    // The search's options say how it matched ("Exact match in own details only"), so a short list is not read as complete.
    const searching=!!v.q.trim()||!!normalSearch(v.search_options)?.reference;
    const optionWords=()=>searching?searchWords(v.search_options):'';
    // The toolbar's filtered strip says what the search and the filters beyond the view's own hide (a list the server
    // searches or filters says only how many it shows), none left included: that is the way back.
    if(rows.length||!spec.loading?.())bar.shown(rows.length,(spec.onSearch||spec.onFilter)&&rows.length>=offered?undefined:offered,spec.noun);
    const why=unresolvedWhy();unresolvedNote.hidden=!why;
    if(why)unresolvedNote.replaceChildren(icon('alert'),el('span',{},`${why}: the list is not filtered by it yet, and Select all matching waits.`),
      button('Retry',()=>{void retryUnresolved();},'',{class:'link-button'}));
    // A filter that leaves nothing keeps its toolbar, so it can be cleared.
    toolbar.hidden=!total&&!saved.length&&!filtered&&!spec.controls?.length&&!spec.actions?.length;root.classList.toggle('dt-none',!total);
    bar.sync();
    // "Show changed · N": the rows the last bulk change changed, one toggle away.
    const changed=recentlyChanged(spec.id),inList=changed?base().filter(r=>changed.has(spec.rowId(r))).length:0;if(!inList)onlyChanged=false;
    changedSlot.replaceChildren(...(inList?[(()=>{const b=el('button',{type:'button',class:'dt-tool'+(onlyChanged?' on':''),'aria-pressed':String(onlyChanged)},icon('check'),el('span',{class:'dt-tool-label'},`Show changed · ${inList}`));
      b.addEventListener('click',()=>{onlyChanged=!onlyChanged;v.page=0;draw();});return b;})(),
      // "Undo last change" stays with the list (and History has the batch) until the next change.
      ...(onlyChanged?[]:[iconButton('Stop marking changed rows','close',()=>{forgetChanged(spec.id);draw();},{class:'bare'})])]:[]));
    // "Undo last change" + History sit with the list's status in the page footer (design §3.1b) until the next change.
    footExtra.replaceChildren(...(inList&&lastUndo(spec.id)?[button('Undo last change',async()=>{const u=lastUndo(spec.id)!;const r=await command(u.kind,u.payload);forgetChanged(spec.id);
        await applyUndone([{kind:u.kind,result:r}]).catch(()=>undefined);notify('Undone. The previous values are back.');await spec.afterChange?.();if(root.isConnected)draw();},'',{class:'link-button'}),
      el('a',{href:'#system/audit',class:'link-button'},'History')]:[]));
    // Nothing matches: one message, with the way back (Show all) inside it.
    if(!rows.length&&spec.loading?.()){body.replaceChildren(skeleton('lines'));pager.replaceChildren();drawn=null;return;}
    // A built-in view with its own words, shown as it is (no search or conditions): those words, and the unfiltered view.
    const shownView=!rows.length&&!searching&&!v.conditions?builtinOn():undefined;
    if(shownView?.empty){const every=builtins().find(b=>b!==shownView&&!b.open&&dims().every(k=>!b.filters[k]?.length));
      body.replaceChildren(empty(shownView.empty[0],shownView.empty[1],every?button(`Show ${every.label.toLowerCase()}`,()=>applyBuiltin(every),'secondary small'):null,'check'));
      pager.replaceChildren();drawn=null;return;}
    if(!rows.length){const none=total||(filtered&&!!(spec.onFilter||spec.onSearch)),clearable=searching||Object.keys(v.filters).length>0||!!v.conditions;
      const [title,line]=none?['No matching '+spec.noun[1],clearable?optionWords():'Nothing matches this view.']:spec.empty||['Nothing here yet',''];
      body.replaceChildren(empty(title,line,null,'search'));pager.replaceChildren();drawn=null;return;}
    // Everything but the rows as drawn last time: when only rows were added (Show more), they are appended.
    const sig=JSON.stringify([scope,v.conditions,v.collapsed,v.hidden,v.wrap,v.order,v.widths,menuSlot,onlyChanged,recentlyChanged(spec.id)?.size||0,[...unlimited],own,shownCols().map(c=>c.key)]);  // a column that appears with a row (whenAny) redraws the head
    const appendable=layout==='table'&&!(groupKey&&(spec.groupLimit||v.then&&v.then!==groupKey))&&drawn?.sig===sig;
    if(!(appendable&&appendRows(shown,groupKey))){drawn=null;
      if(layout==='table'){body.replaceChildren(tableOf(shown,groupKey));drawn!.sig=sig;}
      else drawLayout();}
    function drawLayout(){
    if(layout==='sheet')body.replaceChildren(sheetLayout!({collection:spec.collection!,table:spec.id,rows,rowId:spec.rowId as (r:unknown)=>string,refresh:()=>void reload(),
      // The view's columns as record fields, the grouped column first after the record, rows in their groups' order.
      columns:[...new Set([...(groupKey?[groupKey]:[]),...shownCols().map(c=>c.key)].map(k=>spec.columns.find(c=>c.key===k)?.field||k))],group:groupKey,
      ...(groupKey?{rows:[...new Set(grouped(rows,groupKey).flatMap(([,x])=>x))]}:{})}));
    else if(layout==='cards'){const then=v.then&&v.then!==groupKey?v.then:'';
      const cardGroup=(k:string,items:T[],key:string,sub:boolean):HTMLElement[]=>{const closed=v.collapsed.includes(key);
        return [el('div',{class:'dt-card-group'+(sub?' dt-subgroup':'')},...groupHead(k,items,closed,key)),
          ...(closed?[]:!sub&&then?grouped(items,then).flatMap(([k2,rows])=>cardGroup(k2,rows,key+' › '+k2,true)):((few,more)=>[el('div',{class:'dt-cards'},...few.map(cardOf)),...(more?[more]:[])])(...limited(key,items)))];};
      body.replaceChildren(...(groupKey?grouped(shown,groupKey).flatMap(([k,items])=>cardGroup(k,items,k,false)):[el('div',{class:'dt-cards'},...shown.map(cardOf))]));}
    else if(layout==='calendar'){const cal=spec.calendar!;
      body.replaceChildren(...(groupKey?grouped(shown,groupKey).flatMap(([k,items])=>{const closed=v.collapsed.includes(k);
        return [el('div',{class:'dt-card-group'},...groupHead(k,items,closed,k)),...(closed?[]:[cal(items)])];}):[cal(shown)]));}
    else if(layout==='timeline'){const w=window_(),s=timelineNow();
      body.replaceChildren(spec.timeline!.draw(groupKey?grouped(rows,groupKey).map(([k,items])=>({key:k,name:k==='—'?'None':k,rows:items})):[{key:'',name:'',rows}],
        {start:w.start,end:w.end,zoom:zoomNow(),settings:s,collapsed:v.collapsed,toggle:k=>{v.collapsed=v.collapsed.includes(k)?v.collapsed.filter(x=>x!==k):[...v.collapsed,k];draw();},
          sub:r=>shownCols().slice(1,4).map(c=>c.text?.(r)||'').filter(Boolean).join(' · ')}));}
    else body.replaceChildren(el('div',{class:'dt-board'},...grouped(shown,groupKey).map(([k,items])=>el('section',{class:'dt-lane'},el('header',{class:'dt-lane-head'},el('strong',{},k==='—'?'None':k),el('span',{class:'dt-group-count'},String((whole.get(k)||items).length))),...items.map(cardOf)))));
    }
    pager.replaceChildren(...(pages>1&&layout!=='calendar'&&layout!=='timeline'?[
      Object.assign(el('button',{type:'button',class:'icon-button','aria-label':'Previous page'},icon('chevronLeft')),{disabled:v.page===0,onclick:()=>{v.page--;draw();}}),
      el('span',{class:'muted'},`Page ${v.page+1} of ${pages}`),
      Object.assign(el('button',{type:'button',class:'icon-button','aria-label':'Next page'},icon('chevronRight')),{disabled:v.page>=pages-1,onclick:()=>{v.page++;draw();}})]:[]));
    fitLater(root);
    markOpen(body,false,spec.onOpen?m=>{const order=drawnOrder(),i=order.findIndex(x=>rowMark(spec.rowId(x.r))===m);return i<0?undefined:positionOf(order,i);}:undefined);
    const next=body.querySelector<HTMLElement>(':scope>.table-scroll');if(next?.isConnected&&(left!==0||v.scroll.top!==0)){next.scrollLeft=left;next.scrollTop=v.scroll.top;v.scroll.left=next.scrollLeft;v.scroll.top=next.scrollTop;}
    // Redrawn controls keep keyboard ownership; focusing must not undo the scroll position just restored.
    if(focused&&focusSelector&&document.activeElement===document.body){const host=root.querySelectorAll<HTMLElement>(focusSelector)[focusAt];
      const target=focusIndex<0?host:host?.querySelectorAll<HTMLElement>(focusables)[focusIndex];
      (target?.matches(':disabled')?pager.querySelector<HTMLElement>('button:not(:disabled)')||next:target||next)?.focus({preventScroll:true});}
  }
  // Defaults restore adaptive layout; a named view keeps its explicit saved layout.
  function apply(d:Definition,id:string,reset=false,preset=''){v.preset=preset;const newDay=(d.as_of||'')!==(v.as_of||'');if(newDay)setAsOf(d.as_of||'');Object.assign(v,{layout:d.layout,hidden:[...d.hidden],filters:JSON.parse(JSON.stringify(d.filters)),conditions:d.conditions?JSON.parse(JSON.stringify(d.conditions)):null,group:d.group,sort:d.sort,then_sort:[...(d.then_sort||[])],timeline:d.timeline?{...d.timeline}:null,q:d.q,search_options:normalSearch(d.search_options),then:d.then||'',wrap:[...(d.wrap||[])],order:[...(d.order||[])],widths:d.widths?{...d.widths}:id||reset?{}:{...v.widths},group_sort:[...(d.group_sort||[])],page:0,viewId:id,chosen:!!id});save(stateId,v);if(bar.search)bar.search.value=v.q;if(!(newDay&&spec.asOf))draw();}  // a list that reads its rows for the day draws once they are read
  tableDraws.set(root,draw);tableRoots.add(new WeakRef(root));draw();
  // Saved views come from the server; the table works at once and picks them up when they arrive.
  // The rows do not depend on them: only the toolbar updates, unless the remembered view no longer exists (then the
  // table redraws with its defaults). Redrawing every row here replaced what the person was reading or about to click.
  // A named view keeps the search it was saved with (the person chose to save it); a typed search alone is never kept.
  // The view state is shared by every table of this list: an earlier render (replaced before the views arrived) may have
  // taken the search already, so this table compares with the search it last reported, not with an empty `v.q`.
  const dayAtLoad=v.as_of;
  if(own)void fetchViews<Definition>(spec.id).then(r=>{saved=r;
    // Opened from elsewhere already filtered (a preset or a day): that is what the person asked to see.
    const open=openingView(spec.id,saved);
    if(open!==undefined&&!preset&&!givenDay&&(open?.id||'')!==v.viewId){v.hydrated=true;apply(open?open.definition:defaultDefinition(),open?.id||'');return;}
    // What each saved view's relative values mean today (its menu shows the dates or "per viewer").
    void resolveOperands(saved.flatMap(x=>relativeLines(x.definition.conditions)),scope()).then(()=>bar.sync());const named=v.viewId?saved.find(x=>x.id===v.viewId):undefined;
    if(v.viewId&&!named){v.viewId='';draw();return;}
    const first=!!named&&!v.hydrated;v.hydrated=true;
    // The view's search comes back as saved, its options with it (both or neither: options alone would change a search typed since).
    if(first&&!v.q&&!normalSearch(v.search_options)){v.q=named!.definition.q||'';v.search_options=normalSearch(named!.definition.search_options);}
    // The saved view's As of day comes back with it on its first read (a reload); a day changed since stays.
    // …unless the day was changed while the views were on their way (that newer choice stays). A list that reads its
    // rows for the day (spec.asOf) redraws once they are read: drawn now, the day would show over the previous day's rows.
    if(first&&!givenDay&&v.as_of===dayAtLoad&&(named!.definition.as_of||'')!==(v.as_of||'')){setAsOf(named!.definition.as_of||'');if(!spec.asOf)draw();return;}
    if(searchKey()!==reportedSearch){if(bar.search)bar.search.value=v.q;draw();}else bar.sync();}).catch(()=>undefined);
  /** `parts` lets another toolbar (the planner's) offer this table's Group and Sort. */
  /** Every row's id in the order drawn, over all pages (not just the rows on screen), and one row's place in it. */
  const ids=()=>drawnOrder().map(x=>spec.rowId(x.r));
  const position=(id:string)=>{const l=drawnOrder(),i=l.findIndex(x=>spec.rowId(x.r)===id);return i<0?undefined:positionOf(l,i);};
  /** Whether the view reads this column now (filters, grouping, sort or shown), e.g. to redraw when its values arrive. */
  const uses=(key:string)=>!!v.filters[key]||JSON.stringify(v.conditions||null).includes(`"${key}"`)||v.group===key||v.then===key||v.sort?.[0]===key||(v.then_sort||[]).some(x=>x[0]===key)||shownCols().some(c=>c.key===key);
  return {root,refresh:draw,fieldsChanged:()=>resultChanged?.(true),uses,selected:()=>bulk?[...bulk.selected]:[],parts,ids,position};
}
