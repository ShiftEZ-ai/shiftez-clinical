import {el,checkTarget,icon,notify,popover,menu,opens,menuItem,button,showDialog,plural,note,select,latestReading,type IconName,type Choice} from './dom.js';
import {request,onSessionReset,sessionChanged} from './api.js';
import {selectionBar} from './selection_bar.js';
import {setTriState,triStateOf} from './check_tree.js';
import {conditionBuilder,isTyped,type ConditionField} from './conditions.js';
import {BULK_COLLECTIONS} from './generated.js';
import type {Conditions} from './generated.js';

/** One bulk action on the selected records. `run` gets the ids and a callback that clears the selection once done.
 *  `primary`, `more` and `danger` place it in the selection bar (selection_bar.ts). */
/** `all`: every record the list's filters match on the server (a list paged on the server, "Select all N matching");
 *  then `ids` are only the rows loaded so far. */
export interface BulkAction{label:string;icon:IconName;run:(ids:string[],done:()=>void,all?:SelectionValue)=>unknown;danger?:boolean;primary?:boolean;more?:boolean;tip?:string;
  /** Same-button confirm (press twice), only for an action that cannot be undone (D42). */armed?:boolean}

// ---- The bulk registry (contracts): what each collection is, its fields, actions, children and condition fields ----
export type FieldSpec={label:string;kind:'scalar'|'date'|'multi'|string;how:string[];required?:boolean;nullable?:boolean;
  schema?:Record<string,any>;ref?:string;choices?:Choice[]|{value:string;label:string}[]};
export type ActionSpec={kind:string;destructive:boolean;governed:boolean;roles:string[];label?:string;fields?:Record<string,FieldSpec>};
export type CollectionSpec={label:string;catalogue?:string|null;fields:Record<string,FieldSpec>;actions:Record<string,ActionSpec>;
  children:string[];parent_of:Record<string,string>;conditions:{key:string;label:string}[];dated:boolean;
  columns?:Record<string,{label:string;read_only?:boolean}>;related_columns?:Record<string,{label:string;collection?:string;field?:string}>;
  /** The collection's own status words (workflow, or Current / Future / Ended at a day) and the ones a list starts with. */
  status?:{options:{value:string;label:string}[];defaults:string[]}};
/** Fields stored as the day after the last day (not included): shown and typed as the last day (lastDayIncluded). */
export const LAST_DAY=new Set(['ends_on']);
export const registry=(c:string):CollectionSpec|undefined=>(BULK_COLLECTIONS as unknown as Record<string,CollectionSpec>)[c];
/** The command a field edit of this type runs (the type's own update; a governed type proposes). */
export const editAction=(key:string)=>{const a=registry(key)?.actions||{};return ['edit','update','update_dates','propose'].map(n=>a[n]).find(Boolean);};
/** The roles that may change a record of this type: its edit command's own roles (the server's command_roles), the one
 *  source for the Sheet and every single edit; undefined for a type without one. Lives here, not in sheet.ts, so
 *  logic-only modules (requests.ts → home.ts) never load the Sheet's page listeners. */
export const editRoles=(key:string)=>editAction(key)?.roles;
/** A collection's words for one record and many, from its label ("1 person", "2 clinic permissions", "1 leave & availability"). */
export const nounOf=(c:string):[string,string]=>{const l=(registry(c)?.label||'records').toLowerCase();return [l==='people'?'person':l.replace(/ies$/,'y').replace(/s$/,''),l];};

/** A selection as a value the server resolves (§3.2): a collection, its filters, pasted names, ticks and unticks, and at
 *  most one parent level. */
export type SelectionValue={collection:string;parent?:{collection:string;selection:SelectionValue};
  where?:{conditions?:Conditions|null;as_of?:string;q?:string;filters?:Record<string,string[]>};names?:string[];include_ids?:string[];exclude_ids?:string[]};
export type Resolved={count:number;token:string;sample?:Record<string,any>[];unmatched?:string[];ambiguous?:{line:string;candidates:Record<string,any>[]}[];by_parent?:Record<string,number>};
const post=<T>(path:string,body:unknown,signal?:AbortSignal)=>request<T>(path,{method:'POST',body:JSON.stringify(body),signal});
export const resolveSelection=(sel:SelectionValue,signal?:AbortSignal)=>post<Resolved>('/api/bulk/resolve',sel,signal);
export const bulkRows=(selection:SelectionValue,o:{columns?:string[];offset?:number;limit?:number}={})=>
  post<{count:number;rows:Record<string,any>[]}>('/api/bulk/rows',{selection,limit:5000,...o});
/** Every id a selection holds (the resolver's sample holds the first 100; more come from its rows). */
export async function selectionIds(sel:SelectionValue,r?:Resolved):Promise<string[]>{
  const got=r||await resolveSelection(sel);
  if((got.sample||[]).length>=got.count)return (got.sample||[]).map(x=>String(x.id));
  return (await bulkRows(sel,{columns:[]})).rows.map(x=>String(x.id));
}

/** What the app knows that the bulk screens need (choices for a field, people's condition fields, labels, roles):
 *  set once by app.ts, so these modules depend on no page. */
export type BulkContext={conditionFields:(keys:string[])=>ConditionField[];choices:(field:FieldSpec,key:string,collection:string)=>Choice[]|undefined;
  permits:(...roles:string[])=>boolean;labelOf:(collection:string,id:string)=>string;open?:(collection:string,id:string)=>void;
  /** How many kept (staged) changes this person has, from the signed-in summary. */stagedCount?:()=>number;
  /** How many changes sent for independent review this person may review (someone else's), from the signed-in summary. */reviewCount?:()=>number;
  /** The signed-in identity (a kept change of one's own is resolved, another's is reviewed). */identity?:()=>string};
let context:BulkContext={conditionFields:()=>[],choices:()=>undefined,permits:()=>false,labelOf:(_,id)=>id};
export const setBulkContext=(c:BulkContext)=>{context=c;};
export const bulkContext=()=>context;
/** Whether this person may read a collection's records (the registry's `readers`, the roles its rows endpoint allows). */
/** A record's name by its id, or undefined when the app does not know it (never the id itself). */
export const knownName=(id:string)=>{const l=context.labelOf('',id);return l==='Record'?undefined:l;};
export const canRead=(c:string)=>{const r=(registry(c) as {readers?:readonly string[]}|undefined)?.readers;return !r||!!r.length&&context.permits(...r);};
/** Whether this person may change records of a collection in bulk (any action its roles allow; rules and facts are
 *  proposed through their own flows). */
/** Whether a saved rule (its policy id) has unsaved edits on the Rules page: told by the rule catalogue, read by every
 *  bulk path that proposes from saved lines (a proposal would drop those edits). */
let ruleDrafted:(policyId:string)=>boolean=()=>false;
export const setRuleDraftCheck=(check:(policyId:string)=>boolean)=>{ruleDrafted=check;};
export const ruleHasDraft=(policyId:string)=>ruleDrafted(policyId);
/** Rules (policy ids) with a proposal on its way to the server: their editors read only (whichever page shows them)
 *  until the reply is in, as a draft made meanwhile would be dropped by the new proposal. */
// Counted per rule: two proposals of one rule out together (one sent from another page) keep it read only until both settle.
const proposing=new Map<string,number>();
/** Bulk work waiting on the server (a count, a dry run, a proposal): while any is out the page says how many with
 *  data-bulk-busy on <html>, a state a reader, and a test, can wait on instead of guessing a time. */
let working=0;
const showWork=()=>{if(working)document.documentElement.setAttribute('data-bulk-busy',String(working));else document.documentElement.removeAttribute('data-bulk-busy');};
export async function bulkWork<T>(task:()=>Promise<T>):Promise<T>{
  working++;showWork();
  try{return await task();}finally{working--;showWork();}
}
// A rule is named by its policy id once saved, and by `catalogue:<entry id>` (ruleKey) for a first version not saved yet.
export const ruleKey=(e:{id:string;rule?:{id:string}|null})=>e.rule?e.rule.id:'catalogue:'+e.id;
export const ruleProposing=(key:string)=>proposing.has(key);
/** Pages drawn read only while a proposal was out: each asks to be drawn again once it settles, sent or failed. */
const settledWaiters=new Set<()=>void>();
export const whenProposalsSettle=(redraw:()=>void)=>{settledWaiters.add(redraw);};
/** A page drawn read only for a proposal on its way (`target`, maybe still being built behind a loading skeleton):
 *  drawn again once the proposal settles, sent or failed, as soon as it is on screen. A page left meanwhile (another
 *  address) or replaced before it was ever shown is not drawn again. */
export function redrawWhenSettled(target:HTMLElement,redraw:()=>unknown){
  const address=location.hash;let shown=false;
  // shortcut: a page never shown is given up after 10 s of frames; a longer skeleton would need a mount event.
  const until=Date.now()+10_000;
  const check=()=>{if(location.hash!==address)return;
    if(target.isConnected){void redraw();return;}
    if(shown||Date.now()>until)return;  // replaced after it was shown, or abandoned while built
    requestAnimationFrame(check);};
  const watchShown=()=>{if(target.isConnected)shown=true;else if(!shown&&Date.now()<until)requestAnimationFrame(watchShown);};
  // On screen before the settle, it counts as shown (a later replacement ends it); still building, the settle waits.
  requestAnimationFrame(watchShown);
  whenProposalsSettle(check);
}
export const markProposing=(ids:Iterable<string>,on:boolean)=>{const keys=[...ids];
  for(const id of keys){const n=(proposing.get(id)||0)+(on?1:-1);if(n>0)proposing.set(id,n);else proposing.delete(id);}
  if(!on&&keys.length){const waiting=[...settledWaiters];settledWaiters.clear();for(const redraw of waiting)redraw();}};
onSessionReset(()=>{proposing.clear();settledWaiters.clear();});
/** Saved rule lines as the server selects them: these lines of these rule versions (never a retired version's). */
export const lineSelection=(policyIds:string[],ids:string[]):SelectionValue=>({collection:'policy_lines',where:{filters:{policy_id:[...new Set(policyIds)],id:ids}}});
export const canChange=(collection:string)=>Object.values(registry(collection)?.actions||{}).some(a=>!a.governed&&context.permits(...a.roles));

// A list's selection outlives a redraw of the same page (after a change, "now End the other 8" is one more action) and
// a trip into its child records and back; another page starts afresh.
// A server selection (Select all N matching, by condition, pasted names) is kept whole: its base, members and overlays,
// so a redraw (an activity pulse, a reload after a change, a layout or view switch) still sends it, not the rows loaded.
type ServerSelection={sel:SelectionValue;count:number;base:string;members?:Set<string>;include:Set<string>;exclude:Set<string>};
const kept=new Map<string,{page:string;ids:Set<string>;whole:ServerSelection|null}>(),selectionGenerations=new Map<string,number>();
const pageOf=()=>typeof location==='undefined'?'':location.hash.replace(/^#/,'').split(/[/?]/)[0];
const keptOf=(key:string)=>{const k=kept.get(key);return k&&k.page===pageOf()?k:undefined;};
export const keptSelection=(key:string)=>[...keptOf(key)?.ids||[]];
/** Start a list's next drawing with these records selected (a child list narrowed to fewer parents keeps its rows still in scope). */
export const keepSelection=(key:string,ids:string[])=>{if(ids.length)kept.set(key,{page:pageOf(),ids:new Set(ids),whole:null});else kept.delete(key);};

// Rows a bulk change just changed are marked "Changed" in their list for a while, which offers "Show changed · N".
// The list also keeps "Undo last change" (the server's reverse of that change) until the next one.
type Undo={kind:string;payload:Record<string,unknown>};
const changedRows=new Map<string,{ids:Set<string>;at:number;undo?:Undo}>();
export const markRows=(table:string,ids:string[],undo?:Undo)=>{if(ids.length)changedRows.set(table,{ids:new Set(ids),at:Date.now(),undo});};
export const recentlyChanged=(table:string)=>{const c=changedRows.get(table);return c&&Date.now()-c.at<10*60_000?c.ids:null;};
export const lastUndo=(table:string)=>recentlyChanged(table)?changedRows.get(table)?.undo:undefined;
// An Undo made elsewhere (the toast's) ends the marks and the list's Undo: there is nothing left to undo.
if(typeof window!=='undefined')window.addEventListener('rota-saved',e=>{const d=(e as CustomEvent).detail;if(d?.kind==='bulk.apply'&&String(d.payload?.reason||'').startsWith('Undo'))changedRows.clear();});
// The list whose bulk action ran last: a change started from it (a page's own action that names no table) keeps its
// "Undo last change" there after the toast. Marks show only for rows of that list, so a change elsewhere marks nothing.
export let actingList='';
export const actOn=(table:string)=>{actingList=table;};
export const forgetChanged=(table:string)=>{changedRows.delete(table);};
onSessionReset(()=>{kept.clear();changedRows.clear();});

export type SelectionOptions={
  /** Remembered under this key while the page stays (the list's id). */key?:string;
  /** Every selectable id the list's filters match, on every page (Select all N matching, Invert). */matching?:()=>string[];
  /** Every selectable id of the list, filters aside (paste and conditions select among them). */all?:()=>string[];
  /** The list as the server knows it (collection, parent, As of): Paste names and Select by condition resolve on it. */base?:()=>SelectionValue|null;
  /** The list's own day ('' = today), named beside a condition's count. */asOf?:()=>string;
  /** A list read from the server in pages: its matches as a server selection, counted by the resolver (also once every
   *  page is loaded); null when its filters are only the rows' own. */server?:()=>SelectionValue|null;
  /** Its matches are not known yet (a relative value still resolving, or not resolved): everything that takes the
   *  matching set (Select all shown or matching, a group's box, Invert, Ctrl A) and the bar's actions wait; hand picks stay. */held?:()=>boolean};

/**
 * Selection for a list: a checkbox per record, a select-all box for the records shown with a ▾ selection menu (Select
 * all N matching, Paste names or codes…, Select by condition…, Invert, Select none), a Gmail-style "select all
 * matching" banner and the floating bar of actions. The selection survives searching and paging; Shift-click ticks a
 * range, x ticks the focused row, Ctrl A ticks the rows shown and, again, all matching.
 */
export function bulkSelection(noun:[string,string],actions:(ids:string[])=>BulkAction[],detail?:(ids:string[])=>string,o:SelectionOptions={}){
  // A record grouped under several values (tags, skills) is drawn once per group: every copy of its box follows.
  const selected=new Set<string>(o.key?keptSelection(o.key):[]),boxes=new Map<string,HTMLInputElement[]>(),groups=new Map<HTMLInputElement,string[]>();
  let shown:string[]=[],last='';
  const sel=selectionBar({label:'Bulk actions'}),bar=sel.root;
  const all=el('input',{type:'checkbox',class:'bulk-check','aria-label':`Select all ${noun[1]} shown`}) as HTMLInputElement;
  const caret=opens(el('button',{type:'button',class:'dt-check-menu','aria-label':'Selection options','data-tip':'Selection options'},icon('chevronDown')),'menu');
  const head=el('span',{class:'dt-check-head'},checkTarget(all),caret);
  const banner=el('div',{class:'dt-select-banner',role:'status','aria-live':'polite',hidden:true});
  const held=()=>!!o.held?.();
  // Unknown matches are no matches: a list still resolving shows every row, but none of them is "matching" yet.
  const matching=()=>held()?[]:o.matching?.()||shown,known=()=>new Set(o.all?.()||matching());
  const paint=(id:string)=>{for(const n of boxes.get(id)||[])n.checked=selected.has(id);};
  // "Select all N matching" on the server: the selection is the list's matches (counted by the resolver), not ids.
  // `base`: the list's server matches it was taken from; a new search or filter ends it.
  // Rows ticked or unticked by hand on top of it are overlays (include / exclude), so the server selection holds and its
  // count follows exactly. `members`: every record it holds, from the resolver (none: it is the list's own matches, so
  // every row the list loads is one). Rows loaded later (Show N more) are ticked when they are members.
  let everything:ServerSelection|null=o.key?keptOf(o.key)?.whole||null:null;const counts=new Map<string,Promise<number>>();
  const overlaid=(e:NonNullable<typeof everything>):SelectionValue=>({...e.sel,...(e.include.size?{include_ids:[...(e.sel.include_ids||[]),...e.include]}:{}),...(e.exclude.size?{exclude_ids:[...(e.sel.exclude_ids||[]),...e.exclude]}:{})});
  const member=(e:NonNullable<typeof everything>,id:string)=>!e.members||e.members.has(id);
  /** One record ticked or unticked: on a server selection it becomes an overlay. */
  // Every change of the selection is a new generation: an awaited operation (Select all, Invert) whose reply arrives after
  // one (a Clear, a tick) is dropped.
  // Shared by every drawing of the same list (its key): a reply to a list since rebuilt and cleared is dropped too.
  let own=0;
  const generation=()=>o.key?selectionGenerations.get(o.key)||0:own,bump=()=>{if(o.key)selectionGenerations.set(o.key,generation()+1);else own++;};
  const mark=(id:string,on:boolean)=>{bump();on?selected.add(id):selected.delete(id);paint(id);const e=everything;if(!e)return;
    if(on){if(!e.exclude.delete(id)&&!member(e,id))e.include.add(id);}else if(!e.include.delete(id)&&member(e,id))e.exclude.add(id);};
  const count=(sel:SelectionValue)=>{const k=JSON.stringify(sel);let c=counts.get(k);if(!c){c=resolveSelection(sel).then(r=>r.count);counts.set(k,c);c.catch(()=>counts.delete(k));}return c;};
  const clear=()=>{bump();everything=null;selected.clear();boxes.forEach((_,id)=>paint(id));sync();};
  const roll=(box:HTMLInputElement,ids:string[])=>setTriState(box,triStateOf(ids.filter(id=>selected.has(id)).length,ids.length));
  function sync(){
    const every=matching(),srv=o.server?.()||null;
    if(everything&&(!srv||JSON.stringify(srv)!==everything.base)){everything=null;if(o.key&&kept.has(o.key))kept.get(o.key)!.whole=null;}
    const e=everything,overlays=!!e&&(e.include.size>0||e.exclude.size>0),whole=e?overlaid(e):undefined;
    // Rows loaded since the server selection was taken: its members are ticked (unless unticked by hand), before the
    // header and group boxes roll up, so they never show mixed while every shown row is ticked.
    if(e)for(const id of every)if(!selected.has(id)&&member(e,id)&&!e.exclude.has(id)){selected.add(id);paint(id);}
    if(o.key){if(selected.size||everything)kept.set(o.key,{page:pageOf(),ids:new Set(selected),whole:everything});else kept.delete(o.key);}
    all.disabled=held();roll(all,shown);for(const [box,ids] of groups)if(box.isConnected){box.disabled=held();roll(box,ids);}else groups.delete(box);
    const ids=[...selected];
    const allMatching=!!e&&!overlays||!srv&&every.length>shown.length&&every.every(id=>selected.has(id));
    // A server selection (every match on a paged list) holds even when none of its records is loaded here.
    const total=e?Math.max(0,e.count+e.include.size-e.exclude.size):ids.length,any=ids.length>0||!!e&&total>0,acts=any&&!held()?actions(ids):[];
    // A selection nothing can be done to says so (e.g. a coordinator ticking leave that an approver decides), not an empty bar.
    const words=held()?'Waiting for the filter to be worked out':acts.length?[allMatching?'all matching':'',detail?.(ids)||''].filter(Boolean).join(' · '):'Nothing you can do to these together';
    sel.show(any?{count:total,noun,detail:words||undefined,clear,key:[total,...ids].join(),actions:acts.map(a=>({...a,run:()=>a.run(ids,clear,whole)}))}:null);
    // Gmail: every row shown is ticked and more match → offer them all; all ticked → say so, with Clear. A list paged on
    // the server asks the resolver how many match ("Counting…").
    const page=shown.length>0&&shown.every(id=>selected.has(id));
    banner.hidden=!(page&&(every.length>shown.length||!!srv))||held();
    if(banner.hidden)return;
    if(allMatching){const n=e?.count||every.length;banner.replaceChildren(icon('check'),el('span',{},`All ${n} matching ${noun[1]} are selected.`),button('Clear selection',clear,'',{class:'link-button'}));return;}
    if(!srv){banner.replaceChildren(icon('check'),el('span',{},`All ${shown.length} shown are selected.`),button(`Select all ${every.length} matching`,()=>void selectAllMatching(),'',{class:'link-button'}));return;}
    banner.replaceChildren(icon('check'),el('span',{},`All ${shown.length} shown are selected.`),el('span',{class:'muted'},'Counting…'));
    const asked=JSON.stringify(srv);
    count(srv).then(n=>{if(banner.hidden||everything||JSON.stringify(o.server?.()||null)!==asked)return;
      if(n<=every.length){banner.replaceChildren(icon('check'),el('span',{},`All ${shown.length} shown are selected.`),...(every.length>shown.length?[button(`Select all ${every.length} matching`,()=>void selectAllMatching(),'',{class:'link-button'})]:[]));return;}
      banner.replaceChildren(icon('check'),el('span',{},`All ${shown.length} shown are selected.`),button(`Select all ${n} matching`,()=>void selectAllMatching(),'',{class:'link-button'}));},
      ()=>{if(!banner.hidden)banner.replaceChildren(icon('check'),el('span',{},`All ${shown.length} shown are selected.`));});
  }
  const tick=(ids:string[],on:boolean)=>{for(const id of ids)mark(id,on);sync();};
  /** "Select all N matching" (banner, menu, Ctrl A twice): every match; on a list paged on the server, the server's
   *  selection of them (counted by the resolver), not only the rows loaded. */
  const selectAllMatching=()=>bulkWork(selectAllNow);
  async function selectAllNow(){
    if(held())return;
    // Records selected before (another search, another page of the list) stay: they join the server selection as its
    // include overlay, and the count is the server's of the union.
    const every=matching(),srv=o.server?.()||null,matched=new Set(every),before=[...selected].filter(id=>!matched.has(id));
    const was=everything,keptBefore=[...(was?.include||[])];everything=null;tick(every,true);if(!srv)return;
    const asked=JSON.stringify(srv),mine=generation(),n=await count(srv);
    if(generation()!==mine||JSON.stringify(o.server?.()||null)!==asked||n<=every.length)return;
    const e:ServerSelection={sel:srv,count:n,base:asked,include:new Set([...before,...keptBefore]),exclude:new Set()};
    if(e.include.size){const union=await count(overlaid(e));if(generation()!==mine)return;e.count=union-e.include.size;}
    everything=e;sync();
  }
  const replace=(ids:string[])=>{bump();everything=null;selected.clear();boxes.forEach((_,id)=>paint(id));tick(ids,true);};
  /** Select from a paste or a condition: on a paged list the server's selection (its count), ticking the rows loaded. */
  const takeAll=(ids:string[],whole?:SelectionValue,n?:number,members?:string[])=>{const srv=o.server?.();replace(ids);
    if(whole&&srv&&n!==undefined){everything={sel:whole,count:n,base:JSON.stringify(srv),members:new Set(members||ids),include:new Set(),exclude:new Set()};sync();}};
  /** Invert, within every match: on a list paged on the server, every server match except the ones selected now (a server
   *  selection with them excluded); inverting that again gives back just those. */
  const invert=()=>bulkWork(invertNow);
  async function invertNow(){
    if(held())return;
    const every=matching(),srv=o.server?.()||null,e=everything;
    // All matching less some: just those some.
    if(e&&!e.members){replace([...e.exclude]);return;}
    // A condition or paste (its members, less and plus the hand-made changes): every other match of the list.
    const was=e?new Set([...[...e.members!].filter(id=>!e.exclude.has(id)),...e.include]):new Set(every.filter(id=>selected.has(id)));
    if(srv){const asked=JSON.stringify(srv),mine=generation(),n=await count(srv);if(generation()!==mine)return;
      if(JSON.stringify(o.server?.()||null)===asked&&(e||n>every.length)){replace(every.filter(id=>!was.has(id)));
        everything={sel:srv,count:n,base:asked,include:new Set(),exclude:was};sync();return;}}
    for(const id of was){selected.delete(id);paint(id);}tick(every.filter(id=>!was.has(id)),true);
  }
  all.addEventListener('change',()=>{if(held()){roll(all,shown);return;}tick(shown,all.checked);});
  /** The menu's Select all N matching: N is the server's count on a list paged on the server. */
  const selectAllItem=(close:()=>void)=>{const every=matching(),srv=o.server?.()||null;
    const m=menuItem(`Select all ${srv?'':every.length+' '}matching`,'check',()=>{close();return selectAllMatching();},held()?{disabled:true}:{},srv?'…':'');
    if(srv)count(srv).then(n=>{const h=m.querySelector('.hint');if(h)h.textContent=String(Math.max(n,every.length));},()=>m.querySelector('.hint')?.remove());
    return m;};
  // A paged list: paste and conditions resolve within its server matches, and Select takes all of them.
  let closeMenu=()=>{};
  caret.addEventListener('click',()=>{closeMenu=menu(caret,'Selection options',()=>{
    const base=o.base?.(),paged=o.server?.()||null;
    return [
      selectAllItem(()=>closeMenu()),
      ...(base?[{label:'Paste names or codes…',icon:'copy' as IconName,run:()=>pasteNamesDialog(noun,paged||base,known(),(ids,add,whole,n,members)=>add?tick(ids,true):takeAll(ids,whole,n,members),!!paged)},
        ...(conditionKeys(base.collection).length?[{label:'Select by condition…',icon:'filter' as IconName,run:()=>byCondition(caret,noun,paged||base,known(),o.asOf?.()||'',
          (ids,how,whole,n,members)=>how==='select'?takeAll(ids,whole,n,members):tick(ids,how==='add'),!!paged)}]:[])]:[]),
      {label:'Invert selection',icon:'reset' as IconName,run:invert,disabled:held()},
      ...(selected.size?[{label:'Select none',icon:'close' as IconName,run:clear}]:[])];
  },{placement:'below'});});
  return {
    bar,all,head,banner,selected,
    /** Call before drawing rows: the ids now shown (for select-all). */
    show(ids:string[]){shown=ids;boxes.clear();groups.clear();sync();},
    box(id:string,label:string){
      const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':'Select '+label}) as HTMLInputElement;b.checked=selected.has(id);
      b.addEventListener('change',()=>{mark(id,b.checked);sync();});
      // Shift-click: every record shown between the last one ticked and this one takes this one's state.
      b.addEventListener('click',e=>{e.stopPropagation();const from=shown.indexOf(last),to=shown.indexOf(id);
        if(e.shiftKey&&from>=0&&to>=0){for(const x of shown.slice(Math.min(from,to),Math.max(from,to)+1))mark(x,b.checked);sync();}
        last=id;});
      boxes.set(id,[...(boxes.get(id)||[]),b]);return b;
    },
    /** A group's box (a group heading): ticks its records; shows ticked, empty or mixed (rolls up to select-all). */
    group(ids:string[],label:string){
      const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':`Select all ${label}`}) as HTMLInputElement;
      b.addEventListener('change',()=>{if(held()){roll(b,ids);return;}tick(ids,b.checked);});b.addEventListener('click',e=>e.stopPropagation());
      groups.set(b,ids);roll(b,ids);return b;
    },
    /** Keys inside the list: x ticks the row that has focus; Ctrl A ticks the rows shown, pressed again all matching. */
    keys(scope:HTMLElement){scope.addEventListener('keydown',e=>{
      const t=e.target as HTMLElement;if(t.closest('input:not([type=checkbox]),textarea,select,[contenteditable=true],.dt-editor'))return;
      if(e.key==='x'&&!e.ctrlKey&&!e.metaKey&&!e.altKey){const id=t.closest<HTMLElement>('[data-row]')?.dataset.row;if(!id||!boxes.has(id))return;e.preventDefault();tick([id],!selected.has(id));}
      else if((e.key==='a'||e.key==='A')&&(e.ctrlKey||e.metaKey)&&t.closest('.dt-body')){e.preventDefault();if(held())return;
        if(shown.some(id=>!selected.has(id)))tick(shown,true);else void selectAllMatching();}
    });},
    /** Replace the selection (a fix "Select the 3 refused", a list opened with a selection). */
    set:replace,
  };
}

/** The condition fields a collection's selection offers (registry), as the shared builder's fields: the who and where
 *  choices, and the dates, text and numbers the records of this kind hold (their columns; a session's day by its start). */
const conditionKeys=(collection:string)=>{const columns=registry(collection)?.columns||{};
  return (registry(collection)?.conditions||[]).map(c=>c.key).filter(k=>k!=='roles'&&(!isTyped(k)||k in columns||k==='date'&&'start' in columns));};

/** "Paste names or codes": one per line or comma separated; names, short codes or staff numbers (people) or names and
 *  codes (other records), matched by the server's staff-import matcher. Live result: matched · match more than one
 *  (pick each) · not found (Copy). Select replaces the selection, Add adds to it. */
/** `paged`: the list holds only some of its rows (paged on the server): Select then takes every match on the server
 *  (`sel`, `count`), not only the rows loaded; Add ticks the loaded ones. */
export function pasteNamesDialog(noun:[string,string],base:SelectionValue,known:Set<string>,done:(ids:string[],add:boolean,sel?:SelectionValue,count?:number,members?:string[])=>void,paged=false){
  showDialog('Paste names or codes',`One per line, or separated by commas. ${base.collection==='people'||base.parent?.collection==='people'?'Names, short codes or staff numbers.':'Names or short codes.'}`,(body,close)=>{
    const text=el('textarea',{rows:8,'aria-label':'Names or codes',class:'bulk-paste',placeholder:'Keiko Vega\nSMP\n00000'}) as HTMLTextAreaElement;
    const result=el('div',{class:'bulk-paste-result',role:'status','aria-live':'polite'});
    const pick=button('Select',()=>finish(false),'primary',{disabled:true,why:'Nothing matches yet.'}),add=button('Add to selection',()=>finish(true),'secondary',{disabled:true,why:'Nothing matches yet.'});
    let ids:string[]=[],every:string[]=[],chosen=new Map<string,string>(),timer=0,matched=0,asked:SelectionValue|null=null;
    // The names as they are now: a new keystroke outdates the reading in flight at once (its reply is dropped).
    const reads=latestReading(text);
    const lines=()=>[...new Set(text.value.split(/[\n,;\t]+/).map(x=>x.trim()).filter(Boolean))];
    const extra=()=>[...chosen.values()].filter(Boolean);
    const label=()=>{const n=(paged?matched:ids.length)+extra().length,loaded=ids.length+extra().length;pick.textContent=n?`Select ${n}`:'Select';
      add.textContent=loaded?`Add ${loaded}${paged&&loaded<n?' loaded':''} to selection`:'Add to selection';pick.disabled=!n;add.disabled=!loaded;};
    async function check(){
      const names=lines(),mine=reads.start();chosen=new Map();
      if(!names.length){ids=[];result.replaceChildren();label();return;}
      result.replaceChildren(el('span',{class:'muted'},'Matching…'));
      try{
        const sel={...base,names},r=await resolveSelection(sel);if(!reads.current(mine))return;
        asked=sel;matched=r.count;
        const held=await selectionIds(sel,r);if(!reads.current(mine))return;every=held;ids=held.filter(id=>known.has(id));
        const amb=r.ambiguous||[],none=r.unmatched||[];
        const copy=button('Copy not found',()=>navigator.clipboard?.writeText(none.join('\n')).then(()=>notify('Not found names copied.')),'',{class:'link-button'});
        result.replaceChildren(el('p',{class:'bulk-paste-count'},el('strong',{},`${paged?matched:ids.length} matched`),...(paged&&ids.length<matched?[el('span',{class:'muted'},` (${ids.length} loaded here)`)]:[]),` · ${amb.length} match${amb.length===1?'es':''} more than one · ${none.length} not found`),
          ...(amb.length?[el('div',{class:'bulk-paste-list'},el('div',{class:'dt-pop-label'},'Match more than one — choose'),...amb.map(a=>{
            const s=select(a.line,[{value:'',label:'Skip this line'},...a.candidates.map(c=>({value:String(c.id),label:String(c.display_name||c.name||c.id)+(c.short_code?` · ${c.short_code}`:'')}))],'',false);
            s.input.addEventListener('change',()=>{chosen.set(a.line,s.input.value);label();});return s.wrap;}))]:[]),
          ...(none.length?[el('div',{class:'bulk-paste-list'},el('div',{class:'dt-pop-label'},'Not found'),el('ul',{class:'bulk-paste-none'},...none.map(x=>el('li',{},x))),copy)]:[]),
          ...(!ids.length&&!amb.length?[note(`Nothing matched. Check the spelling, or paste short codes or staff numbers.`,'warning')]:[]));
        label();
      }catch(e){if(!sessionChanged(e)&&reads.current(mine))result.replaceChildren(note((e as Error).message,'error',e));}
    }
    text.addEventListener('input',()=>{reads.start();ids=[];matched=0;asked=null;chosen=new Map();pick.disabled=add.disabled=true;window.clearTimeout(timer);timer=window.setTimeout(check,350);});
    // On a paged list Select is the server's selection: the names, plus the records chosen for ambiguous lines.
    // A line naming two people with the same name: the choice is a person; in a list of their records (clinic
    // permissions, leave) it stands for that person's records within this list's scope.
    const via=base.collection!=='people'?registry(base.collection)?.parent_of?.people:undefined;
    const recordsOf=async(people:string[])=>via&&people.length?(await Promise.all(people.map(id=>
      selectionIds({...base,where:{...base.where,filters:{...base.where?.filters,[via]:[id]}}})))).flat():people;
    const finish=async(more:boolean)=>{const mine=reads.start();let picked:string[];
      try{picked=[...new Set(await recordsOf(extra()))];}catch(e){if(!sessionChanged(e)&&reads.current(mine))result.replaceChildren(note((e as Error).message,'error',e));return;}
      if(!reads.current(mine))return;
      done([...ids,...picked.filter(id=>known.has(id))],more,paged&&asked?{...asked,include_ids:picked}:undefined,matched+picked.length,[...every,...picked]);close();};
    body.append(text,result,el('div',{class:'form-footer'},button('Cancel',close,'secondary'),add,pick));
    text.focus();
  });
}

/** "Select by condition": the shared condition builder under the selection caret, a live count on the list's As of
 *  day, and Select (replace) / Add / Remove from selection. */
export function byCondition(anchor:HTMLElement,noun:[string,string],base:SelectionValue,known:Set<string>,asOf:string,done:(ids:string[],how:'select'|'add'|'remove',sel?:SelectionValue,count?:number,members?:string[])=>void,paged=false){
  popover(anchor,(panel,close)=>{
    panel.classList.add('bulk-condition-pop');
    const fields=context.conditionFields(conditionKeys(base.collection));
    const b=conditionBuilder(fields,null,{context:'selection',asOf:()=>base.where?.as_of||'',collection:base.collection});
    const count=el('p',{class:'bulk-condition-count',role:'status','aria-live':'polite'},'Choose a condition');
    // The list's own conditions (a paged list's Filter) still hold: the new condition narrows within them (And).
    const within=(tree:Conditions):SelectionValue=>{const own=base.where?.conditions;
      return {...base,where:{...base.where,conditions:own?{match:'all',conditions:[own,tree]} as Conditions:tree}};};
    const acts=(['select','add','remove'] as const).map(how=>button(how==='select'?'Select':how==='add'?'Add':'Remove from selection',async()=>{
      const tree=b.read();if(!tree)return;const sel=within(tree),r=await resolveSelection(sel);
      const held=await selectionIds(sel,r);if(!anchor.isConnected)return;close();done(held.filter(id=>known.has(id)),how,paged&&how==='select'?sel:undefined,r.count,held);},how==='select'?'primary small':'secondary small',{disabled:true,why:'Choose a condition first.'}));
    let timer=0,turn=0;
    const recount=()=>{window.clearTimeout(timer);timer=window.setTimeout(async()=>{
      const tree=b.read(),mine=++turn;acts.forEach(x=>x.disabled=!tree);
      if(!tree){count.textContent='Choose a condition';return;}
      count.textContent='Counting…';
      // The count is what Select will take: every match on a paged list; else the matches this list holds.
      try{const sel=within(tree),r=await resolveSelection(sel);if(mine!==turn)return;
        const here=(await selectionIds(sel,r)).filter(id=>known.has(id)).length;if(mine!==turn)return;const n=paged?r.count:here;
        count.textContent=`${plural(n,noun[0],noun[1])} match${n===1?'es':''}${asOf?' as of '+asOf:''}${paged&&here<n?` · ${here} loaded here (Add and Remove use those)`:''}`;}
      catch(e){if(!sessionChanged(e)&&mine===turn)count.textContent=(e as Error).message;}},300);};
    b.root.addEventListener('change',recount);
    panel.append(el('div',{class:'dt-pop-label'},'Select by condition'),b.root,count,el('div',{class:'actions'},...acts.reverse()));
  },{label:'Select by condition',placement:'below'});
}
