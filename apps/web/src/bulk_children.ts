import {el,icon,menu,opens,menuItem,button,empty,note,plural,departmentToday,departmentDateTime} from './dom.js';
import {sessionChanged,onSessionReset} from './api.js';
import {skeleton} from './loading.js';
import {dataTable,selectionKey,tableFilters,tableFiltered,tableSearch,tableSearchOptions,tableAsOf,openAsOf,tableServerConditions,type Column} from './table_view.js';
import type {SearchOptions} from './search.js';
import {registry,canRead,knownName,nounOf,resolveSelection,bulkRows,bulkContext,keepSelection,LAST_DAY,type SelectionValue} from './bulk.js';
import {showDay,shiftDay} from './date_picker.js';
import {isTyped,kindOf} from './conditions.js';
import {TABLE_CONDITION_DEFINITIONS} from './generated.js';
import {recordText,isStructured} from './record_text.js';

/* "Show their…": from a parent selection, the child records of every selected
 * parent open as an ordinary list (a route with a breadcrumb), where the same selection, filters and Change… apply.
 * Level 2 is just another collection of the registry; nothing here names one. */

/** `day`: the parent list's As of ('' = today), the day its child list opens on. */
type Parent={collection:string;noun:[string,string];ids:string[];back:string;label:string;day:string};
let parent:Parent|null=null;
/** The child list on screen: an app refresh (a save, the pulse) re-reads it in place, so the rows the reader asked for
 *  (Show more) and its read numbering survive; a new list would start again from the first page. */
let onScreen:{child:string;target:HTMLElement;load:()=>Promise<boolean|void>}|null=null;
onSessionReset(()=>{parent=null;onScreen=null;});
/** Re-reads the child list `child` in place: false when the read failed (the list says so), undefined when that list is
 *  not the one on screen (then the page renders afresh). */
export async function refreshRelated(child:string):Promise<boolean|undefined>{
  const s=onScreen;if(s?.child!==child||!s.target.isConnected)return undefined;
  return await s.load()!==false;
}
/** The parent set as the server selects it: exactly these records. */
export const parentSelection=(p:Parent):{collection:string;selection:SelectionValue}=>({collection:p.collection,selection:{collection:p.collection,where:{filters:{id:p.ids}}}});
const childLabel=(c:string)=>registry(c)?.label||c;

/** The bar's "Show their…" menu: each child collection of the selected parents with how many records it holds. */
export function showTheir(anchor:HTMLElement,collection:string,noun:[string,string],ids:string[],label:string,day=''){
  const children=(registry(collection)?.children||[]).filter(c=>registry(c)&&canRead(c));
  const p:Parent={collection,noun,ids,back:location.hash,label,day};
  let close=()=>{};
  // Counts come one by one; a child the person may not read is taken out, and an empty list says so (a note item).
  const nothing=menuItem('Nothing related you can open','close',()=>undefined,{disabled:true});
  // The counts are the menu's: a pick stops the ones still out, so the list it opens is not queued behind them
  // (a browser holds about six requests to one server at once).
  const counting=new AbortController();
  const items=children.map(c=>{
    const item=menuItem(childLabel(c),'layers',()=>{counting.abort();close();open(p,c);},{},'…');
    resolveSelection({collection:c,parent:parentSelection(p),...(day?{where:{as_of:day}}:{})},counting.signal).then(r=>{const h=item.querySelector('.hint');if(h)h.textContent=String(r.count);},
      e=>{if(sessionChanged(e)||counting.signal.aborted)return;const list=item.parentElement;item.remove();if(list&&!list.querySelector('[role^=menuitem]'))list.append(nothing);});
    return item;});
  close=menu(anchor,'Show their…',items.length?items:[nothing],{placement:'above-start',head:`Show their… · ${plural(ids.length,noun[0],noun[1])}`});
}
// The parent set travels in the address (?parent=people&of=<ids>), so a child list survives a reload and a shared link.
// shortcut: ids in the address (≈37 characters each) suit department-sized sets; a server-kept selection token would
// replace them if a parent set ever grows to thousands.
const listPath=(hash:string)=>hash.replace(/^#/,'').split('?')[0].replace(/\/related\/.*$/,'');
const address=(p:Parent,child:string)=>'#'+listPath(p.back)+'/related/'+encodeURIComponent(child)+'?'+new URLSearchParams({parent:p.collection,of:p.ids.join(','),...(p.day?{as_of:p.day}:{})});
// The child list opens on the parent list's day (not one kept from an earlier visit); a day chosen there later stays.
function open(p:Parent,child:string){parent=p;openAsOf('related-'+child,p.day);location.hash=address(p,child);}
/** The parent set of the child list shown: this visit's, or the address's (reload, a link). */
function parentNow(child:string):Parent|null{
  const q=new URLSearchParams(location.hash.split('?')[1]||''),collection=q.get('parent')||'',ids=(q.get('of')||'').split(',').filter(Boolean);
  if(!collection||!ids.length||!registry(collection))return parent;
  // A link with another day (the same parents) is a new visit on that day, not the one in memory.
  const day=/^\d{4}-\d{2}-\d{2}$/.test(q.get('as_of')||'')?q.get('as_of')!:'';
  if(parent&&parent.collection===collection&&parent.ids.join()===ids.join()&&parent.day===day)return parent;
  openAsOf('related-'+child,day);
  return parent={collection,ids,noun:nounOf(collection),label:registry(collection)!.label,back:'#'+listPath(location.hash),day};
}

/** A child list: breadcrumb back to the parent list (its selection kept), the parent chip in the toolbar's scope slot,
 *  the rows from the server (the registry's own and related columns), and the shared table with Change…. */
export async function relatedPage(target:HTMLElement,child:string){
  const spec=registry(child),p=parentNow(child);
  const back=()=>{location.hash=p?.back||'#home';};
  const crumb=el('nav',{class:'bulk-crumb','aria-label':'Breadcrumb'},button(`‹ ${p?.label||'Back'}`,back,'',{class:'link-button'}),el('span',{class:'muted','aria-hidden':'true'},' / '),el('h1',{class:'bulk-crumb-here'},spec?.label||child));
  target.append(crumb);
  if(!spec||!p){target.append(empty('Choose the records first','Select some records in a list, then use Show their… to open their related records here.',button('Back',back,'secondary small'),'layers'));return;}
  let day='',carry:string[]|null=null;
  const section=el('section',{class:'panel'});target.append(section);
  const chip=opens(el('button',{type:'button',class:'dt-tool dt-parent'},icon('people'),el('span',{class:'dt-tool-label'},`For ${plural(p.ids.length,'selected '+p.noun[0],'selected '+p.noun[1])}`),icon('chevronDown')),'menu');
  chip.addEventListener('click',()=>menu(chip,'Parents',()=>[
    {label:`Back to ${p.label} (keeps the selection)`,icon:'chevronLeft',run:back},'separator',
    ...p.ids.slice(0,50).map(id=>({label:`Remove ${bulkContext().labelOf(p.collection,id)}`,icon:'close' as const,run:()=>{p.ids=p.ids.filter(x=>x!==id);
      history.replaceState(null,'',address(p,child));chip.querySelector('.dt-tool-label')!.textContent=`For ${plural(p.ids.length,'selected '+p.noun[0],'selected '+p.noun[1])}`;
      // Fewer parents: the list is drawn afresh for them, keeping only the selected rows still in it.
      carry=table?.selected()||[];table=null;void load();},
      // The last parent stays: a child list needs at least one.
      disabled:p.ids.length<2})),
    ...(p.ids.length>50?[{label:`and ${p.ids.length-50} more`,icon:'dots' as const,run:()=>undefined,disabled:true}]:[])],{placement:'below'}));
  const text=(key:string,v:unknown):string=>v===null||v===undefined||v===''?'':isStructured(key,v)?recordText(key,v,knownName):Array.isArray(v)?v.map(x=>text(key,x)).join(', '):typeof v==='boolean'?(v?'Yes':'No')
    :/^\d{4}-\d{2}-\d{2}T/.test(String(v))?departmentDateTime(String(v)):/^\d{4}-\d{2}-\d{2}$/.test(String(v))?showDay(LAST_DAY.has(key)?shiftDay(String(v),-1):String(v)):/^[0-9a-f]{8}-[0-9a-f]{4}-/.test(String(v))?bulkContext().labelOf('',String(v)):String(v);
  const list=(v:unknown,key:string)=>Array.isArray(v)?v.map(x=>text(key,x)).filter(Boolean):[text(key,v)].filter(Boolean);
  // Status is the server's, in the collection's own words (a workflow's Proposed / Active, or Current / Future / Ended at
  // the list's day), as its Status filter is: never derived here.
  const statuses=spec.status?.options||[],word=(v:string)=>statuses.find(o=>o.value===v)?.label||v,value=(l:string)=>statuses.find(o=>o.label===l)?.value||l;
  const status=(r:Record<string,any>)=>typeof r.status==='string'?word(r.status):'';
  const firstStates=(spec.status?.defaults||[]).map(word);
  const shown=[...Object.entries(spec.related_columns||{}).filter(([k])=>!/\.(staff_number|programme_ids)$/.test(k)),
    ...Object.entries(spec.columns||{}).filter(([k])=>k!=='id'&&k!=='revision'&&k!=='status')];
  const definitions=(TABLE_CONDITION_DEFINITIONS as unknown as Record<string,Record<string,{value_kind:string;display_values?:boolean;operand_field?:string}>>)[child]||{};
  const columns:Column<Record<string,any>>[]=[
    // Dates, text and numbers filter by comparing (relative dates, Is empty); everything else by its values.
    ...shown.map(([key,c]):Column<Record<string,any>>=>{const d=definitions[key],type=d&&!d.display_values?d.value_kind:spec.fields[key]?.kind==='date'?'date':isTyped(key)?kindOf(undefined,key):'';
      const kind=['date','text','number'].includes(type)?type as 'date'|'text'|'number':null;
      const related=spec.related_columns?.[key],id=related?.collection?related.field===d?.operand_field?key:spec.parent_of[related.collection]:undefined;
      const server=kind||/\.(display_name|name)$/.test(key)?{field:key}:undefined;
      return {key,label:c.label,...(server?{server}:{}),...(id?{ids:r=>r[id]}:{}),text:r=>text(key,r[key]),...(kind?{filter:{kind,value:r=>r[key]}}:{facet:r=>list(r[key],key)}),cell:r=>text(key,r[key])||el('span',{class:'muted'},'—')};}),
    ...(statuses.length?[{key:'status',label:'Status',server:{field:'status',value},text:status,facet:status,options:statuses.map(o=>o.label),cell:(r:Record<string,any>)=>status(r)||el('span',{class:'muted'},'—'),ordered:statuses.map(o=>o.label)} as Column<Record<string,any>>]:[]),
  ];
  // Paged on the server (/api/bulk/rows): the first page, then "Show N more"; the search and the Status filter are the
  // server's, so the count, "Select all N matching" and Change… all mean the same records.
  // `want`: how many rows the reader has asked for (Show more); a reload after a save reads them all again, not just the first page.
  const PAGE=200,rows:Record<string,any>[]=[];let want=PAGE,total=0,q='',so:SearchOptions|undefined,states:string[]=[],tree='',read=0,table:ReturnType<typeof dataTable<Record<string,any>>>|null=null;
  const more=el('div',{class:'dt-more-row bulk-more'});
  // The list's Or / is-not conditions are the server's too (tableServerConditions), as on every paged list.
  const selection=()=>({collection:child,parent:parentSelection(p!),where:{...(day?{as_of:day}:{}),...(q?{q}:{}),...(so?{search_options:so}:{}),...(states.length?{filters:{status:states.map(value)}}:{}),...(tree?{conditions:JSON.parse(tree)}:{})}});
  // Each read is numbered: a slower earlier search or page never overwrites a newer one's rows, total or Show more.
  const page=async(offset:number,limit=PAGE)=>{const mine=read,r=await bulkRows(selection(),{offset,limit});if(mine!==read)return null;total=r.count;return r.rows;};
  const paintMore=()=>{const left=total-rows.length;more.replaceChildren(...(left>0?[el('span',{class:'muted'},`Showing ${rows.length} of ${total}`),
    button(`Show ${Math.min(left,PAGE)} more`,async()=>{want=rows.length+PAGE;const next=await page(rows.length);if(!next)return;rows.push(...next);table?.refresh();paintMore();},'secondary small')]:[]));};
  // Resolves false when the read failed and was reported (a search's busy rows clear).
  async function load():Promise<boolean|void>{
    if(!table)section.replaceChildren(skeleton('table'));
    // A re-read starts from the first page: Show more's offset and count are stale until it lands (a click now would
    // add a page the re-read then drops). One already out is dropped by the read number.
    more.replaceChildren();
    let first:Record<string,any>[]|null;const mine=++read;
    try{first=await page(0,Math.max(PAGE,want));if(!first)return;}
    catch(e){if(sessionChanged(e)||mine!==read)return;section.replaceChildren(note((e as Error).message,'error',e),button('Try again',load,'secondary small'));table=null;return false;}
    rows.splice(0,rows.length,...first);
    // Nothing at all (not just nothing for this status): the parents have none of these records.
    // (a search, Status or a condition that matches nothing keeps the list and its toolbar, to clear it)
    // Filtered to nothing: does the unfiltered list hold any? (a failed count keeps the list; another account's reply ends here)
    const asked=!rows.length&&!q&&!so&&(states.length>0||!!tree)?await resolveSelection({collection:child,parent:parentSelection(p!),...(day?{where:{as_of:day}}:{})}).catch(e=>sessionChanged(e)?null:{count:1}):{count:1};
    if(!asked||mine!==read)return;
    const none=!rows.length&&!q&&!so&&(!states.length&&!tree||!asked.count);
    if(none){table=null;section.replaceChildren(empty(`None of the ${plural(p!.ids.length,p!.noun[0],p!.noun[1])} has ${spec!.label.toLowerCase()}`,'',button(`Back to ${p!.label}`,back,'secondary small'),'layers'));return;}
    if(table){table.refresh(true);paintMore();return;}
    if(carry){
      // Kept: the picks still in the narrowed list, checked against all of it (picks on later pages too), not only the first page.
      let scope=new Set(rows.map(r=>String(r.id)));
      if(carry.length&&total>rows.length){try{scope=new Set((await bulkRows(selection(),{columns:[]})).rows.map(r=>String(r.id)));}
        catch(e){if(sessionChanged(e))return;}if(mine!==read)return;}
      keepSelection(selectionKey('related-'+child,parentSelection(p!)),carry.filter(id=>scope.has(id)));carry=null;}
    table=dataTable<Record<string,any>>({id:'related-'+child,noun:nounOf(child),rows,rowId:r=>String(r.id),
      collection:child,parent:()=>parentSelection(p!),controls:[chip],serverPaged:true,serverTotal:()=>total,serverFilters:['status'],
      // A record is named by its first two properties ("Alex Morgan · Continuity clinic").
      rowLabel:r=>columns.slice(0,2).map(c=>c.text?.(r)).filter(Boolean).join(' · ')||String(r.id),
      asOf:spec!.dated?d=>{day=d===departmentToday()?'':d;want=PAGE;void load();}:undefined,
      onSearch:(text,options)=>{q=text;so=options;want=PAGE;return load();},
      onFilter:f=>{const next=f.status||[],c=tableServerConditions('related-'+child),t=c?JSON.stringify(c):'';
        if(next.join()!==states.join()||t!==tree){states=[...next];tree=t;want=PAGE;void load();}},
      afterChange:load,columns:columns.length?columns:[{key:'id',label:'Record',text:r=>String(r.id),cell:r=>String(r.id)}],
      defaults:{...(firstStates.length?{filters:{status:firstStates}}:{})}});
    section.replaceChildren(table.root,more);paintMore();
  }
  // The list starts with its own Status filter (one cleared stays cleared), else the collection's first statuses, read
  // by the server from the first page.
  states=tableFiltered('related-'+child)?tableFilters('related-'+child).status||[]:firstStates;
  // A list visited before keeps its As of day and search in the shared View: the rows read here start from the same ones.
  day=spec.dated?tableAsOf('related-'+child):'';q=tableSearch('related-'+child).trim();so=tableSearchOptions('related-'+child);
  onScreen={child,target,load};
  await load();
}
