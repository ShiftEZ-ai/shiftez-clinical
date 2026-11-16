import {el,icon,button,desktopNotice,withIcon,armed,field,area,select,check,multiPick,popover,menuItem,empty,note,notify,announce,plural,formatDate,departmentDateTime,footerStatus,phoneQuery,labelOf,departmentToday,type Choice} from './dom.js';
import type {Catalogues} from './types.js';
import {lastDayIncluded} from './date_picker.js';
import {request,onSessionReset,sessionChanged} from './api.js';
import {dataTable,useSheetLayout,tableAsOf,type Column} from './table_view.js';
import {type Context} from './forms.js';
import {skeleton} from './loading.js';
import {BULK_COLLECTIONS,TABLE_CONDITION_DEFINITIONS} from './generated.js';
import {pasteDialog,readValue} from './sheet_paste.js';
import {parseTable,toTable,joinValues} from './table_text.js';
import {bulkContext,canRead,nounOf,lineSelection,LAST_DAY,editAction} from './bulk.js';
import {runBulk,openChange,switchesOff,type BulkBody} from './bulk_change.js';
import {recordText,isStructured} from './record_text.js';
import {openPicker} from './picker.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)

// ---- The registry (design §3.2a): record types, their fields and related columns, from generated contracts ----
type Spec={label:string;kind:'date'|'multi'|'scalar';how:readonly string[];required:boolean;nullable:boolean;schema:any};
type Entry={label:string;catalogue:string|null;fields:Record<string,Spec>;columns:Record<string,{label:string;read_only:boolean}>;
  related_columns:Record<string,{label:string;collection:string;field:string;read_only:boolean}>;
  actions:Record<string,{kind:string;destructive:boolean;governed:boolean;roles:readonly string[]}>;parent_of:Record<string,string>};
const REGISTRY=BULK_COLLECTIONS as unknown as Record<string,Entry>;

/** A column of the Sheet: a field of the record type (editable when the registry marks it bulk) or a related record's
 *  field (Person › Rank, Clinic › Tags), shown as read-only context. Ids are shown by the name of their record. */
export type SheetField={key:string;label:string;kind:'text'|'number'|'date'|'datetime'|'select'|'multi'|'toggle'|'other';editable:boolean;
  related?:boolean;choices?:Choice[];required?:boolean;creatable?:boolean;maxLength?:number;
  /** An end stored as the day after the last day (ends_on): shown, typed and pasted as the last day included. */lastDay?:boolean};
export type SheetRow={id:string;revision:number;[key:string]:unknown};
/** Edits not saved yet, by record: the row they were made on (its revision binds the change) and the new values. */
/** `rebase`: the record was just saved with some of its cells; the cells staged since keep waiting, on the record as
 *  the Sheet next reads it (its new revision). */
export type Staged=Map<string,{row:SheetRow;cells:Map<string,unknown>;rebase?:boolean}>;

/** Types the Sheet can edit: a registry entry with bulk fields and an update command. */
// Only types this person may read (the registry's `readers`, the roles the rows endpoint itself allows).
export const sheetTypes=()=>Object.entries(REGISTRY).filter(([k,e])=>Object.keys(e.fields).length&&editAction(k)&&canRead(k)).map(([key,e])=>({key,label:e.label}));
// shortcut: the record type groups are read from the registry's shape (governed → Rules, catalogue → Configuration,
// a person's records → People & permissions, the rest → Rota); a `group` in BULK_COLLECTIONS would replace this.
const groupOf=(key:string)=>{const e=REGISTRY[key],a=editAction(key);
  return a?.governed?'Rules':key==='people'||e.parent_of.people&&!/^(recurring|availability|wishes|sessions)/.test(key)?'People & permissions':e.catalogue&&!e.parent_of.people?'Configuration':'Rota';};

let context:Context|null=null;
/** The app's context (catalogues for names and choices, roles): set once at start-up and by the Bulk editor. */
export const setSheetContext=(ctx:Context)=>{context=ctx;};
const catalogues=():Catalogues=>context?.catalogues||{people:[]};
const cap=(s:string)=>s.replace(/^./,c=>c.toUpperCase());
// Any record id reads as its record's name, whichever list it is in (dynamic: nothing names a catalogue per field).
let names:Map<string,string>|null=null,namesFrom:unknown=null;
const nameOf=(id:string)=>{const c=catalogues();if(namesFrom!==c||!names){names=new Map();namesFrom=c;for(const rows of Object.values(c))if(Array.isArray(rows))for(const r of rows)if(r?.id)names.set(r.id,labelOf(r));}return names.get(id);};
/** The list an id field picks from: <name>_id(s) → the catalogue of that name (person/mentor → people, parent → its
 *  own type), else the catalogue that holds the values already in the column. */
function idChoices(collection:string,key:string,values:unknown[],spec?:Spec):Choice[]{
  const given=spec&&bulkContext().choices(spec as never,key,collection);if(given?.length)return given;
  const c=catalogues(),base=key.replace(/_ids?$/,''),guess=[base==='person'||base==='mentor'?'people':base==='parent'?collection:'',base+'s',base.replace(/y$/,'ies'),'operating_'+base+'s'].filter(Boolean);
  const held=new Set(values.flat().filter(Boolean) as string[]);
  const kind=guess.find(k=>Array.isArray(c[k])&&c[k].length)||Object.keys(c).find(k=>Array.isArray(c[k])&&c[k].some(r=>held.has(r.id)));
  return kind?(c[kind]||[]).filter(r=>r.active!==false||held.has(r.id)).map(r=>({value:r.id,label:labelOf(r)})):[];
}
const isId=(schema:any)=>/0-9a-f\]\{8\}/.test(JSON.stringify(schema||{}).slice(0,200));
const plain=(schema:any)=>(schema?.anyOf||[schema]).find((p:any)=>p?.type!=='null')||schema||{};
const roleOk=(roles:readonly string[]|undefined)=>!!roles?.length&&bulkContext().permits(...roles);

/** The Sheet's columns for a type: its own fields (editable when bulk) and its related columns (read-only). */
export function sheetFields(collection:string,rows:SheetRow[]=[]):SheetField[]{
  const e=REGISTRY[collection];if(!e)return [];
  const editable=roleOk(editAction(collection)?.roles);
  const own=Object.entries({...e.columns,...Object.fromEntries(Object.keys(e.fields).map(k=>[k,{label:e.fields[k].label,read_only:false}]))})
    .filter(([k])=>k!=='id'&&k!=='revision').map(([key,c]):SheetField=>{
      const spec=e.fields[key],p=plain(spec?.schema),items=plain(p.items),values=rows.map(r=>r[key]);
      const lastDay=LAST_DAY.has(key);
      const label=lastDay?'Until (included)':cap(c.label.toLowerCase());
      let kind:SheetField['kind']='text',choices:Choice[]|undefined,creatable=false;
      if(spec?.kind==='date')kind=p.format==='date-time'?'datetime':'date';
      else if(spec?.kind==='multi'){kind='multi';choices=items.enum?items.enum.map((v:string)=>({value:v,label:cap(v.replaceAll('_',' '))})):isId(items)?idChoices(collection,key,values,spec)
        :key==='tags'?(catalogues().tags||[]).map(t=>({value:t.name,label:'#'+t.name})):undefined;creatable=key==='tags';if(!choices&&!creatable)kind='other';}
      else if(p.type==='boolean')kind='toggle';
      else if(p.type==='integer'||p.type==='number')kind='number';
      else if(p.enum){kind='select';choices=p.enum.map((v:string)=>({value:v,label:cap(v.replaceAll('_',' '))}));}
      else if(isId(p)||/_id$/.test(key)){kind='select';choices=idChoices(collection,key,values,spec);}
      else if(spec&&p.type!=='string')kind='other';   // definitions, conditions, numbers: the Change form edits them
      // A read-only structure (an audience, a repeat): shown in words, never typed or pasted.
      if(kind==='text'&&values.some(v=>isStructured(key,v)))kind='other';
      return {key,label,kind,choices,creatable,lastDay,editable:editable&&!!spec&&kind!=='other'&&kind!=='datetime',required:!!spec?.required&&!spec.nullable,maxLength:p.maxLength};});
  const related=Object.entries(e.related_columns).map(([key,c]):SheetField=>({key,label:c.label,related:true,editable:false,
    kind:/_ids$|^tags$/.test(c.field)?'multi':'text'}));
  return [...own,...related];
}

/** What a value reads as in a cell, the cell bar and a copy: dates "1 Sep 2026", ids by their record's name. */
export function shownValue(f:SheetField,v:unknown):string{
  if(f.key==='who'||f.key==='where')return recordText(f.key,v,nameOf);
  if(v==null||v===''||Array.isArray(v)&&!v.length)return '—';
  const name=(x:unknown)=>f.choices?.find(c=>c.value===x)?.label||(typeof x==='string'&&nameOf(x))||String(x);
  if(f.kind==='date'){const d=String(v).slice(0,10);return formatDate(f.lastDay?shift(d,-1):d,'UTC');}
  if(f.kind==='datetime')return departmentDateTime(String(v));
  if(f.kind==='toggle')return v?'Yes':'No';
  if(isStructured(f.key,v))return recordText(f.key,v,nameOf);
  if(Array.isArray(v))return joinValues(v.map(name));
  return f.kind==='select'||f.related?name(v):String(v);
}
const shift=(d:string,n:number)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+n);return x.toISOString().slice(0,10);};
const same=(a:unknown,b:unknown)=>JSON.stringify(a??null)===JSON.stringify(b??null)||isBlank(a)&&isBlank(b);
/** A text field long enough for paragraphs (a description, a note): edited in a text area, never a one-line box. */
const isMultiline=(f:SheetField)=>f.kind==='text'&&(f.maxLength??0)>160;
const isBlank=(v:unknown)=>v==null||v===''||Array.isArray(v)&&!v.length;
/** The row's own title (the record a line is about): its people, else the record's name; a session nobody works is
 *  Unstaffed and a rule line names whom it is for — never an id. */
const titleOf=(r:SheetRow)=>{const p=r['person.display_name'];
  if(Array.isArray(p)?p.length:p!=null&&p!=='')return Array.isArray(p)?joinValues(p.map(String)):String(p);
  const own=r.display_name??r.name??r.title??nameOf(String(r.person_id||''));if(own!=null&&own!=='')return String(own);
  return Array.isArray(r.participants)?'Unstaffed':'who' in r?recordText('who',r.who??{conditions:[]},nameOf):'Record';};

// ---- Staged edits, kept per type across redraws (the list redraws on every filter change) ----
const stagedByType=new Map<string,Staged>(),changedCells=new Set<string>(),shownRows=new Map<string,number>();
/** How many cells are staged in any Sheet (the page's unload guard asks before they would be lost). */
export const sheetStagedCount=()=>[...stagedByType.values()].reduce((a,t)=>a+[...t.values()].reduce((b,s)=>b+s.cells.size,0),0);
const stagedOf=(collection:string)=>{if(!stagedByType.has(collection))stagedByType.set(collection,new Map());return stagedByType.get(collection)!;};
/** Stage values, from a cell edit, fill or paste alike (a value back to what is saved un-stages it). */
function stageCells(collection:string,list:{row:SheetRow;key:string;value:unknown}[]){
  const staged=stagedOf(collection);
  for(const {row,key,value:v} of list){const e=staged.get(row.id);
    if(same(v,row[key])){e?.cells.delete(key);if(e&&!e.cells.size)staged.delete(row.id);}
    else{const x=e||{row,cells:new Map()};x.cells.set(key,v);staged.set(row.id,x);}
    changedCells.delete(`${collection}|${row.id}|${key}`);}
}
/** A cell's value as the Sheet shows it: the staged one when staged (a staged clear included), else the record's. */
/** The cell editor open in a Sheet: a redraw that replaces the grid (Search, Filter) stages its valid text first. */
let openEdit:(()=>void)|null=null;
const stagedValue=(collection:string,row:SheetRow,key:string)=>{const c=stagedOf(collection).get(row.id)?.cells;return c?.has(key)?c.get(key):row[key];};
/** The staged edits of a type as `bulk.apply` items of the universal change model (one item per record, one change per
 *  cell): the same items the Change form builds (design §2.9 SSOT). */
export function stagedItems(collection:string){
  const a=editAction(collection);
  return {item_kind:a?.kind,governed:!!a?.governed,common:a?.kind==='catalogue.update'?{catalogue:collection}:{},
    items:[...stagedOf(collection).entries()].map(([id,s])=>({target_id:id,expected_revision:s.row.revision,
      changes:[...s.cells].map(([field,value])=>({field,how:isBlank(value)?'clear':'set',...(isBlank(value)?{}:{value})}))}))};
}

/** Preview N changes: the shared Preview → Apply → Results flow (bulk_change.ts) with the staged edits as its items;
 *  applied rows leave the stage and their cells are marked changed. */
function previewStaged(collection:string,table:string,refresh:()=>void){
  const p=stagedItems(collection),staged=stagedOf(collection);
  // Lines of a rule propose one new version per rule from a selection of lines, as Propose a change… does: one set of
  // changes for every line sent.
  // shortcut: lines staged with different changes preview one change at a time; per-line items on the server would lift it.
  // Only rule lines go as lines of their rule version; a rule's own fields (the Rules sheet) are items of that rule.
  const lines=collection==='policy_lines'&&p.item_kind==='rule.propose',changes=p.items[0]?.changes||[];
  if(lines&&p.items.some(i=>JSON.stringify(i.changes)!==JSON.stringify(changes))){notify('Rule lines propose one change for all of them at a time: discard the others, preview this one, then stage the rest.','warning');return;}
  const body={item_kind:p.item_kind,common:p.common,reason:'Edited in the Bulk editor',
    ...(lines?{selection:lineSelection(p.items.map(i=>String(staged.get(i.target_id)?.row.policy_id||'')).filter(Boolean),p.items.map(i=>i.target_id)),changes}:{items:p.items})} as unknown as BulkBody;
  // Switching records off is destructive, as in the Change form: never the light path, Apply is the armed confirm.
  const off=switchesOff(p.items.flatMap(i=>i.changes));
  return runBulk({collection,noun:nounOf(collection),ids:p.items.map(i=>i.target_id),revision:id=>staged.get(id)?.row.revision,rowOf:id=>staged.get(id)?.row,body,governed:p.governed,table,
    ...(off?{destructive:true,verb:'Switch off'}:{}),
    title:`Preview ${plural(p.items.reduce((a,i)=>a+i.changes.length,0),'staged change')}`,
    // Applied rows (the stable outcome "changed"), proposals sent for approval ("waiting") and rows that needed no change
    // ("unchanged"): the cells sent leave the stage, unless a newer value was staged in one since (it stays, on the record's
    // new revision). Only applied cells are marked changed (a proposal changes nothing until approved); refused and
    // skipped rows (kept to resolve) stay staged.
    onDone:reply=>{const sent=new Map(p.items.map(i=>[i.target_id,i.changes])),parents=Object.values(REGISTRY[collection]?.parent_of||{});
      for(const r of reply.items as {result:string;target_id?:string|null;after?:Record<string,unknown>}[]){
      const applied=['changed','ready','fixed'].includes(r.result);if(!applied&&r.result!=='waiting'&&r.result!=='unchanged')continue;
      const target=String(r.target_id||'');
      if(applied){const keys=Object.keys(r.after||{});(keys.length?keys:(sent.get(target)||[]).map(c=>c.field)).forEach(k=>changedCells.add(`${collection}|${target}|${k}`));}
      // A proposal's row names the rule its lines belong to: those lines' sent cells leave the stage too.
      for(const id of [target,...[...staged.keys()].filter(x=>x!==target&&parents.some(f=>staged.get(x)!.row[f]===target))]){const s=staged.get(id);
        if(!s)continue;for(const c of sent.get(id)||[])if(same(s.cells.get(c.field),'value' in c?c.value:null))s.cells.delete(c.field);
        if(s.cells.size)s.rebase=true;else staged.delete(id);}}
      refresh();}});
}

const PAGE=100;
const SHEET_KEYS='Arrows move · Shift extends · Enter or F2 edits · Esc cancels · Ctrl C / Ctrl V copy and paste · drag the corner or Ctrl D fills down';

/**
 * The Sheet layout (design §2.9, artboards B2/B4): a collection's rows as a grid (role=grid, one cell in the tab order,
 * arrows move, Shift extends a range, Enter/F2 edit with the field's standard input in the cell bar, Esc cancels,
 * Ctrl C/V copy and paste, Ctrl D or the corner handle fills down). Edits are staged (marked cell, struck "was" value,
 * "changed, not saved" for screen readers) until Preview; Discard is a same-button confirm.
 */
export function sheetGrid(o:{collection:string;table:string;rows:unknown[];refresh:()=>void;columns?:string[]}):HTMLElement{
  openEdit?.();openEdit=null;
  const rows=o.rows as SheetRow[],fields=sheetFields(o.collection,rows),entry=REGISTRY[o.collection];
  // Without the list's own column choice: the type's fields and the related records' names (the clinic of a permission).
  const shown=(o.columns||fields.filter(f=>!f.related||/\.(name|display_name)$/.test(f.key)).map(f=>f.key)).map(k=>fields.find(f=>f.key===k)).filter((f):f is SheetField=>!!f&&f.key!==titleKey());
  function titleKey(){return rows.some(r=>r['person.display_name']!=null)?'person.display_name':fields.some(f=>f.key==='display_name')?'display_name':'name';}
  const cols:(SheetField|null)[]=[null,...shown];   // column 0 is the record, never edited
  const staged=stagedOf(o.collection),canEdit=roleOk(editAction(o.collection)?.roles);
  // Cells staged after an Apply of their record continue from the record as read now (its new revision).
  for(const r of rows){const s=staged.get(r.id);if(s?.rebase){s.row=r;s.rebase=false;}}
  const root=el('div',{class:'sheet'}),cellbar=el('div',{class:'cellbar'}),stageBar=el('div',{class:'stage-bar',role:'status','aria-live':'polite'});
  const limit=()=>shownRows.get(o.table)||PAGE;
  const value=(row:SheetRow,f:SheetField)=>stagedValue(o.collection,row,f.key);
  const textOf=(row:SheetRow,f:SheetField|null)=>f?shownValue(f,value(row,f)):titleOf(row);
  const editable=(c:number)=>{const f=cols[c];return canEdit&&f?.editable?f:null;};
  let cells:HTMLTableCellElement[][]=[],cur={r:0,c:0},anchor={r:0,c:0},editing:((keep:boolean)=>void)|null=null;

  function draw(){
    const n=Math.min(rows.length,limit()),list=rows.slice(0,n);
    const head=el('tr',{role:'row'},...cols.map((f,i)=>el('th',{scope:'col',role:'columnheader','aria-colindex':String(i+1),class:f?.related?'rel':''},f?f.label:fields.find(x=>x.key===titleKey())?.label||'Record')));
    cells=[];
    const body=el('tbody',{},...list.map((row,r)=>{const tds=cols.map((f,c)=>cell(row,f,r,c));cells.push(tds);
      return el('tr',{role:'row','aria-rowindex':String(r+2),'data-row':row.id,class:staged.has(row.id)?'row-staged':''},...tds);}));
    const grid=el('table',{class:'dt-table sheet-grid',role:'grid','aria-label':`${entry?.label||'Records'} sheet`,'aria-multiselectable':'true',
      'aria-rowcount':String(rows.length+1),'aria-colcount':String(cols.length),...(canEdit?{}:{'aria-readonly':'true'})},el('thead',{},head),body);
    events(grid);
    const rest=rows.length-n;
    // The focus ring of an edge cell stays inside the scroll box (CSP: a property set, not a style attribute).
    const scroll=el('div',{class:'table-scroll sheet-scroll'},grid);
    root.replaceChildren(cellbar,scroll,
      ...(rest>0?[el('div',{class:'dt-more-row'},button(`Show ${Math.min(rest,PAGE)} more`,()=>{shownRows.set(o.table,limit()+PAGE);const at={...cur};draw();move(at.r,at.c);},'',{class:'link-button'}),
        el('span',{class:'muted'},` · ${rest.toLocaleString('en')} more ${rest===1?'row':'rows'}`))]:[]),stageBar,footerStatus(footer));
    cur={r:Math.min(cur.r,n-1),c:Math.min(cur.c,cols.length-1)};anchor={...cur};
    paint(false);paintStage();
  }
  const footer=el('span',{class:'dt-count muted'});
  function cell(row:SheetRow,f:SheetField|null,r:number,c:number){
    const td=el('td',{role:'gridcell',tabindex:'-1','aria-colindex':String(c+1),class:'cell'+(f?.related?' rel':'')+(f&&/date|number/.test(f.kind)?' mono':'')}) as HTMLTableCellElement;
    td.dataset.r=String(r);td.dataset.c=String(c);fill(td,row,f);return td;
  }
  function fill(td:HTMLTableCellElement,row:SheetRow,f:SheetField|null){
    td.classList.remove('is-staged','is-changed');
    if(!f){td.replaceChildren(el('strong',{},titleOf(row)));return;}
    if(!canEdit||!f.editable)td.setAttribute('aria-readonly','true');
    const s=staged.get(row.id)?.cells;
    if(s?.has(f.key)){td.classList.add('is-staged');
      td.replaceChildren(el('span',{class:'was'},shownValue(f,row[f.key])),shownValue(f,s.get(f.key)),el('span',{class:'sr-only'},', changed, not saved'));return;}
    const done=changedCells.has(`${o.collection}|${row.id}|${f.key}`);if(done)td.classList.add('is-changed');
    td.replaceChildren(shownValue(f,row[f.key]),...(done?[el('span',{class:'sr-only'},', changed')]:[]));
  }
  const repaint=()=>{cells.forEach((tds,r)=>{tds.forEach((td,c)=>fill(td,rows[r],cols[c]));tds[0].parentElement!.classList.toggle('row-staged',staged.has(rows[r].id));});paint(false);paintStage();};
  const range=()=>({r0:Math.min(cur.r,anchor.r),r1:Math.max(cur.r,anchor.r),c0:Math.min(cur.c,anchor.c),c1:Math.max(cur.c,anchor.c)});
  function paint(focus=true){
    if(!cells.length)return;
    const {r0,r1,c0,c1}=range(),multi=r0!==r1||c0!==c1;
    cells.forEach((tds,r)=>tds.forEach((td,c)=>{const on=r>=r0&&r<=r1&&c>=c0&&c<=c1,here=r===cur.r&&c===cur.c;
      td.tabIndex=here?0:-1;td.classList.toggle('sel',here);td.classList.toggle('sel-range',on&&multi);
      td.setAttribute('aria-selected',String(on));td.querySelector(':scope>.fill-handle')?.remove();}));
    if(editable(c1))cells[r1]?.[c1]?.append(el('span',{class:'fill-handle','aria-hidden':'true'}));
    if(focus)cells[cur.r]?.[cur.c]?.focus();
    bar();
  }
  /** The cell bar (artboard B2): record · field, its value, the keys, and "Change N cells…" for a range. */
  function bar(){
    if(editing)return;
    const f=cols[cur.c],row=rows[cur.r],{r0,r1,c0,c1}=range(),n=(r1-r0+1)*(c1-c0+1);if(!row)return cellbar.replaceChildren();
    // Record · field and its value; the keys are behind one help mark, not spelled out on every cell.
    cellbar.replaceChildren(el('span',{class:'ref'},f?`${titleOf(row)} · ${f.label}`:titleOf(row)),...(f?[el('span',{class:'cellbar-value','data-tip':textOf(row,f)},textOf(row,f))]:[]),
      ...(!canEdit?[el('span',{class:'muted'},'Read only')]:n>1?[el('span',{class:'muted'},`${n} cells selected`)]:[]),
      ...(canEdit?[el('button',{type:'button',class:'icon-button bare','aria-label':'Sheet keys','data-help':'','data-tip':SHEET_KEYS},icon('help'))]:[]),
      el('span',{class:'grow'}),...(canEdit&&n>1?[button(`Change ${n} cells…`,()=>changeRange(r0,r1,c0,c1),'secondary small')]:[]));
  }
  /** "Change N cells…": the universal Change form for the range's records, its editable fields chosen (SSOT). */
  function changeRange(r0:number,r1:number,c0:number,c1:number){
    const list=rows.slice(r0,r1+1),keys=cols.slice(c0,c1+1).filter((f):f is SheetField=>!!f?.editable).map(f=>f.key);
    // Change… saves from the records as read: their staged cells would be left on an old revision.
    if(list.some(r=>staged.has(r.id)))return notify('Some of these rows have staged changes: preview or discard them first.','warning');
    openChange({collection:o.collection,noun:nounOf(o.collection),ids:list.map(r=>r.id),revision:id=>list.find(r=>r.id===id)?.revision,rowOf:id=>list.find(r=>r.id===id),table:o.table,
      preset:(keys.length?keys:[undefined]).map(k=>k?{field:k,how:'set'}:{}),onDone:()=>o.refresh()});
  }
  function move(r:number,c:number,extend=false){cur={r:Math.max(0,Math.min(cells.length-1,r)),c:Math.max(0,Math.min(cols.length-1,c))};if(!extend)anchor={...cur};paint();}
  function stage(list:{row:SheetRow;key:string;value:unknown}[]){stageCells(o.collection,list);repaint();}
  /** Ctrl D / the corner handle: the first `from` rows of the range repeat down the rest of it (editable columns). */
  function fillDown(from:number,to:{r0:number;r1:number;c0:number;c1:number}){
    const out:{row:SheetRow;key:string;value:unknown}[]=[];
    for(let c=to.c0;c<=to.c1;c++){const f=editable(c);if(!f)continue;for(let r=to.r0+from;r<=to.r1;r++)out.push({row:rows[r],key:f.key,value:value(rows[to.r0+(r-to.r0)%from],f)});}
    if(!out.length)return announce('Nothing to fill: these columns are read-only.');
    stage(out);announce(`${plural(out.length,'cell')} filled down · not saved yet`);
  }
  /** Ctrl V: cells from the cursor; one value pasted over a range fills the whole range. */
  function pasteText(text:string){
    const block=parseTable(text,true);if(!block.length)return;  // by position: blank rows keep their place, commas stay in cells
    const {r0,r1,c0,c1}=range(),one=block.length===1&&block[0].length===1,out:{row:SheetRow;key:string;value:unknown}[]=[];let bad=0,locked=0;
    const put=(r:number,c:number,s:string)=>{if(r>=cells.length||c>=cols.length)return;const f=editable(c);if(!f){locked++;return;}const v=readValue(f,s);if(!v){bad++;return;}out.push({row:rows[r],key:f.key,value:v.value});};
    if(one){for(let r=r0;r<=r1;r++)for(let c=c0;c<=c1;c++)put(r,c,block[0][0]);}else block.forEach((line,i)=>line.forEach((s,j)=>put(cur.r+i,cur.c+j,s)));
    stage(out);
    const said=[`${plural(out.length,'cell')} pasted · not saved yet`,bad?`${bad} not recognised`:'',locked?`${locked} read-only, left as they were`:''].filter(Boolean).join(' · ');
    if(bad||locked)notify(said,'error');else announce(said);
  }
  // Copied as Excel does (quoted where a cell holds a tab, line break or quote): pasting it back gives the same cells.
  const copyText=()=>{const {r0,r1,c0,c1}=range(),lines:string[][]=[];for(let r=r0;r<=r1;r++)lines.push(cols.slice(c0,c1+1).map(f=>textOf(rows[r],f).replace(/^—$/,'')));return toTable(lines);};
  function clearRange(){const {r0,r1,c0,c1}=range(),out:{row:SheetRow;key:string;value:unknown}[]=[];
    for(let c=c0;c<=c1;c++){const f=editable(c);if(!f||f.required)continue;for(let r=r0;r<=r1;r++)out.push({row:rows[r],key:f.key,value:f.kind==='multi'?[]:f.kind==='toggle'?false:null});}
    if(out.length){stage(out);announce(`${plural(out.length,'cell')} cleared · not saved yet`);}}

  /** Enter / F2: the field's standard shared input in the cell bar. Enter keeps and moves down, Tab right, Esc cancels. */
  function edit(seed?:string){
    const f=editable(cur.c),row=rows[cur.r];if(!row)return;
    if(!f)return announce(!canEdit?'Read only: you can look and copy, not change.':cols[cur.c]?.related?'Related columns are changed in their own record type.':'This column is changed with Change… (it is not a single value).');
    const label=`${f.label} for ${titleOf(row)}`,v=value(row,f);let read:()=>unknown,wrap:HTMLElement,focus:HTMLElement,onChange=false;
    if(f.kind==='select'){const s=select(label,f.choices||[],isBlank(v)?'':String(v),!!f.required,'None');wrap=s.wrap;focus=s.input;read=()=>s.input.value||null;onChange=true;}
    else if(f.kind==='multi'){const p=multiPick(label,[...(f.choices||[])],(v as string[])||[],undefined,'None',!!f.creatable);wrap=el('div',{class:'field'},el('span',{class:'sr-only'},label),p.wrap);focus=p.wrap.querySelector('button')!;read=p.values;}
    else if(f.kind==='toggle'){const x=check(label,!!v);wrap=x.wrap;focus=x.input;read=()=>x.input.checked;onChange=true;}
    // Long text (a description, a note) or a value with line breaks is the shared text area: its lines are kept exactly.
    else if(f.kind==='text'&&(isMultiline(f)||/[\r\n]/.test(String(v??'')))){const x=area(label,isBlank(v)?'':String(v),3);
      if(f.maxLength)x.input.maxLength=f.maxLength;if(f.required)x.input.required=true;
      wrap=x.wrap;focus=x.input;read=()=>x.input.value===''?null:x.input.value;
      if(seed!==undefined)x.input.value=seed;}
    else{const x=field(label,f.kind==='number'?'number':f.kind==='date'?'date':'text',isBlank(v)?'':String(v),{...(f.maxLength?{maxlength:f.maxLength}:{}),...(f.required?{required:true}:{})});
      if(f.lastDay)lastDayIncluded(x.input);
      wrap=x.wrap;focus=x.input;read=()=>{const t=x.input.value;return t===''?null:f.kind==='number'?Number(t):t;};
      if(seed!==undefined&&f.kind!=='date')x.input.value=seed;}
    wrap.querySelector('label')?.classList.add('sr-only');
    const at={...cur};
    const valid=(x:unknown)=>!(f.required&&isBlank(x)||f.kind==='number'&&x!==null&&!Number.isFinite(x as number)||(focus as HTMLInputElement).validity?.valid===false);
    const finish=(keep:boolean,dr=0,dc=0)=>{if(editing!==finish)return;
      if(keep){const x=read();
        if(!valid(x)){notify(`${f.label}: enter a valid value.`,'error');focus.focus();return;}
        editing=null;stage([{row,key:f.key,value:x}]);}
      editing=null;bar();move(at.r+dr,at.c+dc);};
    editing=finish;
    // Replaced by a redraw: keep valid text staged without moving focus (it stays in the Search box).
    openEdit=()=>{if(editing!==finish)return;const x=read();editing=null;if(valid(x))stageCells(o.collection,[{row,key:f.key,value:x}]);};
    cellbar.replaceChildren(el('span',{class:'ref'},`${titleOf(row)} · ${f.label}`),wrap,el('span',{class:'muted'},'Enter keeps · Tab keeps and moves right · Esc cancels'),el('span',{class:'grow'}));
    // An input method's Enter confirms the composed text, never the cell.
    wrap.addEventListener('keydown',e=>{if(e.defaultPrevented||e.isComposing||e.keyCode===229)return;
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();finish(false);}
      // In a text area Alt+Enter is a new line (as in Excel); Enter keeps, as in every cell.
      else if(e.key==='Enter'&&e.altKey&&e.target instanceof HTMLTextAreaElement){e.preventDefault();e.target.setRangeText('\n',e.target.selectionStart,e.target.selectionEnd,'end');}
      else if(e.key==='Enter'&&!(e.target as HTMLElement).closest('.pick')){e.preventDefault();finish(true,e.shiftKey?-1:1);}
      else if(e.key==='Tab'){e.preventDefault();finish(true,0,e.shiftKey?-1:1);}});
    if(onChange)wrap.addEventListener('change',()=>finish(true));
    focus.focus();
  }
  function events(t:HTMLTableElement){
    const at=(e:Event)=>{const td=(e.target as HTMLElement).closest<HTMLTableCellElement>('td[role=gridcell]');return td?{r:+td.dataset.r!,c:+td.dataset.c!}:null;};
    let drag:''|'range'|'fill'='',from:ReturnType<typeof range>|null=null;
    t.addEventListener('pointerdown',e=>{if(e.button!==0)return;
      if((e.target as HTMLElement).classList.contains('fill-handle')){drag='fill';from=range();e.preventDefault();return;}
      const p=at(e);if(!p)return;editing?.(true);if(editing)return;move(p.r,p.c,e.shiftKey);drag='range';});
    t.addEventListener('pointerover',e=>{const p=at(e);if(!drag||!p)return;
      if(drag==='range')move(p.r,p.c,true);else if(from){anchor={r:from.r0,c:from.c0};cur={r:Math.max(p.r,from.r1),c:from.c1};paint(false);}});
    const stop=()=>{if(drag==='fill'&&from){const to=range();if(to.r1>from.r1)fillDown(from.r1-from.r0+1,to);}drag='';from=null;};
    // Bound for one drag at a time: a redrawn grid leaves no listener behind on the window.
    t.addEventListener('pointerdown',e=>{if(e.button===0)window.addEventListener('pointerup',stop,{once:true});},{capture:true});
    t.addEventListener('dblclick',e=>{if(at(e))edit();});
    // Focus put on a cell (Tab back into the grid, a click, a test) makes it the cursor.
    t.addEventListener('focusin',e=>{const p=at(e);if(p&&!editing&&(p.r!==cur.r||p.c!==cur.c)){cur=p;anchor={...p};paint(false);}});
    t.addEventListener('keydown',e=>{
      if(e.isComposing||e.keyCode===229||!(e.target as HTMLElement).matches('td[role=gridcell]'))return;
      const k=e.key,ctrl=e.ctrlKey||e.metaKey,last=cells.length-1,end=cols.length-1;
      const go=(r:number,c:number)=>{e.preventDefault();move(r,c,e.shiftKey);};
      if(k==='ArrowDown')go(ctrl?last:cur.r+1,cur.c);else if(k==='ArrowUp')go(ctrl?0:cur.r-1,cur.c);
      else if(k==='ArrowRight')go(cur.r,ctrl?end:cur.c+1);else if(k==='ArrowLeft')go(cur.r,ctrl?0:cur.c-1);
      else if(k==='Home')go(ctrl?0:cur.r,0);else if(k==='End')go(ctrl?last:cur.r,end);
      else if(k==='PageDown')go(cur.r+10,cur.c);else if(k==='PageUp')go(cur.r-10,cur.c);
      else if(k==='Enter'||k==='F2'){e.preventDefault();edit();}
      else if(k==='Escape'&&(anchor.r!==cur.r||anchor.c!==cur.c)){e.preventDefault();e.stopPropagation();anchor={...cur};paint();}
      else if(ctrl&&k.toLowerCase()==='d'){e.preventDefault();const r=range();if(canEdit&&r.r1>r.r0)fillDown(1,r);}
      else if(ctrl&&k.toLowerCase()==='a'){e.preventDefault();anchor={r:0,c:0};cur={r:last,c:end};paint();}
      else if((k==='Delete'||k==='Backspace')&&canEdit){e.preventDefault();clearRange();}
      else if(k.length===1&&!ctrl&&!e.altKey&&canEdit){const f=editable(cur.c);if(f&&(f.kind==='text'||f.kind==='number')){e.preventDefault();edit(k);}}
    });
    t.addEventListener('copy',e=>{e.preventDefault();e.clipboardData?.setData('text/plain',copyText());announce('Copied.');});
    t.addEventListener('paste',e=>{e.preventDefault();if(canEdit)pasteText(e.clipboardData?.getData('text/plain')||'');});
  }
  /** "N changes in N rows · not saved yet · Discard… · Preview N changes" (artboard B2). */
  function paintStage(){
    const n=[...staged.values()].reduce((a,s)=>a+s.cells.size,0);
    footer.textContent=n?`${plural(n,'staged change')} · not saved`:'';
    stageBar.hidden=!n;if(!n)return stageBar.replaceChildren();
    const discard=armed(button('Discard…',()=>{staged.clear();repaint();announce('Staged changes discarded.');},'ghost small',{'aria-label':`Discard ${plural(n,'staged change')}`}),'Discard all?');
    // A clean change saves at once (the light path); one with problems opens Preview first.
    stageBar.replaceChildren(icon('edit'),el('span',{},el('strong',{},`${plural(n,'change')} in ${plural(staged.size,'row')}`),' · not saved yet · marked cells'),el('span',{class:'grow'}),discard,
      button(`Save ${plural(n,'change')}`,()=>previewStaged(o.collection,o.table,o.refresh),'primary small'));
  }
  // Operator screens are desktop-only (mobile-scope decision 2026-10-09): on a phone width, also after a resize, the
  // Sheet is the desktop notice (staged edits stay staged for the desktop).
  const fit=()=>{if(phoneQuery.matches)root.replaceChildren(desktopNotice('Sheet view'));else draw();};
  watchWidth(root,fit);fit();
  return Object.assign(root,{stage,paste:(list:{row:SheetRow;key:string;value:unknown}[])=>stage(list)});
}

// One width listener for every Sheet drawn; Sheets no longer on the page are dropped as others are drawn.
const sheets=new Map<HTMLElement,()=>void>();let widthWatched=false;
function watchWidth(root:HTMLElement,fit:()=>void){
  for(const r of sheets.keys())if(!r.isConnected)sheets.delete(r);
  sheets.set(root,fit);
  if(!widthWatched){widthWatched=true;phoneQuery.addEventListener('change',()=>{for(const [r,f] of sheets)if(r.isConnected)f();else sheets.delete(r);});}
}

useSheetLayout(o=>sheetGrid({collection:o.collection,table:o.table,rows:o.rows,refresh:o.refresh,columns:(o as {columns?:string[]}).columns}));

// ---- System › Bulk editor ----
async function sheetRows(collection:string,as_of:string,offset=0,limit=5000){
  // No column list: the server sends each whole record with its related columns (the registry's columns include
  // schema-only names the rows endpoint refuses).
  return request<{rows:SheetRow[];count:number}>('/api/bulk/rows',{method:'POST',body:JSON.stringify({selection:{collection,where:{as_of}},offset,limit})});
}
let editorType='';
const LAST_TYPE='rota.bulk.type';
// Another account in this tab starts with nothing staged, marked or chosen by the one before.
onSessionReset(()=>{openEdit=null;stagedByType.clear();changedCells.clear();shownRows.clear();editorType='';names=null;namesFrom=null;});
// Another bulk change of a record (an Undo above all) leaves its cells as the server has them: their "changed" marks go
// (a Save from the Sheet marks its cells again once its reply is in).
window.addEventListener('rota-saved',e=>{const {kind,payload}=(e as CustomEvent).detail||{};if(kind!=='bulk.apply')return;
  const ids=new Set(((payload?.items||[]) as {target_id?:string}[]).map(i=>String(i.target_id||'')));
  for(const key of [...changedCells])if(ids.has(key.split('|')[1]))changedCells.delete(key);});
/** The fields the server's name matcher reads (staff import's matcher: name, short code, staff number). */
const NAMED=new Set(['display_name','name','short_code','staff_number']);
const fitted=new WeakSet<HTMLElement>();
/**
 * System › Bulk editor (design §2.9, artboard B1): pick a record type from the registry (grouped, with counts), then
 * it is the shared collection (`dataTable`: saved views, search, Filter with As of, Group, Sort, View with the columns)
 * shown as a Sheet, plus Paste from Excel.
 */
export function bulkEditorPage(target:HTMLElement,ctx:Context){
  setSheetContext(ctx);
  // Operator screens are desktop-only (mobile-scope decision 2026-10-09): a phone-sized window shows the notice, also
  // when the window narrows after the page was drawn.
  // The page follows the window between phone and desktop size; a page left removes its listener.
  const fit=()=>{if(!target.isConnected){phoneQuery.removeEventListener('change',fit);fitted.delete(target);return;}target.replaceChildren();bulkEditorPage(target,ctx);};
  if(!fitted.has(target)){fitted.add(target);phoneQuery.addEventListener('change',fit);}
  if(phoneQuery.matches){target.append(desktopNotice('Bulk editor'));return;}
  const types=sheetTypes();
  if(!types.length){target.append(empty('No record types to edit','Record types appear here once the department has some to change.',null,'grid'));return;}
  // Opens on the type used last (this browser), else People, else the first one this person may change (SY-02).
  if(!types.some(t=>t.key===editorType)){let last='';try{last=localStorage.getItem(LAST_TYPE)||'';}catch{/* not kept */}
    // A read-only role opens on People (a list with rows), not on the first type (often empty).
    editorType=(types.find(t=>t.key===last)||types.find(t=>t.key==='people'&&roleOk(editAction(t.key)?.roles))||types.find(t=>roleOk(editAction(t.key)?.roles))||types.find(t=>t.key==='people')||types[0]).key;}
  const host=el('section',{class:'panel sheet-page'});target.append(host);
  const counts=new Map<string,number>();
  let countsRead:Promise<{collections:Record<string,{count:number}>}>|null=null;
  // Each type's list keeps its As of day in the shared View: the rows read start from it, also on re-entry.
  let asOf=tableAsOf('bulk-editor-'+editorType),rows:SheetRow[]=[],read=0,total=0;
  async function load(){
    const n=++read,key=editorType;host.replaceChildren(skeleton('table',`Loading ${REGISTRY[key].label.toLowerCase()}…`));
    try{await ctx.ready();const r=await sheetRows(key,asOf||departmentToday());if(n!==read)return;rows=r.rows;counts.set(key,r.count);total=r.count;draw();}
    catch(e){if(n!==read||sessionChanged(e))return;host.replaceChildren(note((e as Error).message,'error',e),button('Try again',load));}
  }
  function draw(){
    const key=editorType,e=REGISTRY[key],fields=sheetFields(key,rows),noun=nounOf(key);
    const definitions=(TABLE_CONDITION_DEFINITIONS as unknown as Record<string,Record<string,{value_kind:string;operand_field?:string}>>)[key]||{};
    const columns:Column<SheetRow>[]=fields.map(f=>{const d=definitions[f.key],kind=!f.related&&d&&['date','text','number'].includes(d.value_kind)?d.value_kind as 'date'|'text'|'number':null;
      const related=e.related_columns[f.key],id=related?related.field===d?.operand_field?f.key:e.parent_of[related.collection]:f.key;
      return {key:f.key,label:f.label,cell:r=>shownValue(f,r[f.key]),text:r=>shownValue(f,r[f.key]),
        ...(kind?{filter:{kind,value:(r:SheetRow)=>r[f.key]}}:{}),
        ...(f.kind==='select'||f.kind==='multi'||f.kind==='toggle'||f.related?{facet:(r:SheetRow)=>{const v=r[f.key];return isBlank(v)?[]:Array.isArray(v)?v.map(x=>shownValue({...f,kind:'text'},x)):shownValue(f,v);}}:{}),
        ...(d?.operand_field&&id?{ids:(r:SheetRow)=>isBlank(r[id])?[]:Array.isArray(r[id])?(r[id] as unknown[]).map(String):String(r[id])}:{}),
        ...(f.kind==='date'||f.kind==='number'?{sort:(r:SheetRow)=>r[f.key] as string|number??''}:{})};});
    const shownByDefault=new Set([...fields.filter(f=>!f.related).map(f=>f.key),'person.display_name','service.name']);
    // The sheet holds up to 5,000 rows of a type: past that it says so (filter or choose a day to narrow).
    const capped=total>rows.length?note(`Showing the first ${rows.length.toLocaleString('en')} of ${total.toLocaleString('en')} ${noun[1]}. Filter or choose an As of day to narrow; Paste from Excel matches only these.`,'warning'):null;
    host.replaceChildren(...(capped?[capped]:[]),dataTable<SheetRow>({id:'bulk-editor-'+key,collection:key,noun,rows,rowId:r=>r.id,columns,
      rowLabel:titleOf,searchLabel:'Search rows',searchPlaceholder:'Search rows',
      defaults:{layout:'sheet',sort:[fields.some(f=>f.key==='person.display_name')?'person.display_name':fields.some(f=>f.key==='display_name')?'display_name':'name','asc'],hidden:fields.filter(f=>!shownByDefault.has(f.key)).map(f=>f.key)},
      asOf:day=>{asOf=day;void load();},afterChange:load,
      controls:[typeButton()],
      // Structured columns (who / where, rule lines, details) are neither typed nor pasted: their own editors change them.
      // Paste matches rows, so it needs some.
      actions:rows.length&&roleOk(editAction(key)?.roles)&&fields.some(f=>f.editable)?[withIcon(button('Paste from Excel',()=>paste(key,fields.filter(f=>f.kind!=='other')),'secondary small'),'clipboard')]:[],
      empty:[`No ${noun[1]} yet`,'']}).root);
  }
  /** Record type (artboard B1): the registry's types grouped, each with its count, in the shared picker (picker.ts). */
  function typeButton(){
    const b=el('button',{type:'button',class:'dt-tool on',...popup('dialog'),'aria-label':'Record type: '+REGISTRY[editorType].label},icon('layers'),el('span',{class:'dt-tool-label'},REGISTRY[editorType].label),icon('chevronDown'));
    const groups=['People & permissions','Rota','Rules','Configuration'];
    b.addEventListener('click',async()=>{
      // One count read per visit (switching types keeps counts refreshed by their row reads); the list opens once it is
      // in, or without counts when it fails.
      countsRead??=request<{collections:Record<string,{count:number}>}>('/api/bulk/collections');
      try{const r=await countsRead;
        for(const t of types)if(r.collections[t.key]&&!counts.has(t.key))counts.set(t.key,r.collections[t.key].count);
        for(let i=types.length-1;i>=0;i--)if(!r.collections[types[i].key]&&types[i].key!==editorType)types.splice(i,1);}
      catch(e){countsRead=null;if(sessionChanged(e))return;}
      if(!b.isConnected)return;
      const choices=groups.flatMap(g=>types.filter(t=>groupOf(t.key)===g).map(t=>({value:t.key,label:t.label,group:g,hint:counts.get(t.key)?.toLocaleString('en')})));
      openPicker(b,b.parentElement!,{label:'Record types',choices,multiple:false,selected:new Set([editorType]),recentKey:'bulk-editor-types',
        footer:'From the shared record definitions — new types appear here on their own.',
        onPick:key=>{if(key===editorType)return;editorType=key;try{localStorage.setItem(LAST_TYPE,key);}catch{/* not kept */}asOf=tableAsOf('bulk-editor-'+key);void load();}});});
    return b;
  }
  /** Paste from Excel (artboard B3): matched against every row of the type (the 5,000-row window), staged in the Sheet. */
  function paste(key:string,fields:SheetField[]){
    pasteDialog({noun:nounOf(key),fields,title:titleOf,candidates:async()=>rows,
      // What the server's matcher reads (a record's or its person's name, short code, staff number) goes to it.
      identity:k=>NAMED.has(k.replace(/^person\./,'')),
      resolve:async names=>(await request<{matches?:{line:string;ids:string[]}[]}>('/api/bulk/resolve',{method:'POST',body:JSON.stringify({collection:key,names,where:{as_of:asOf||departmentToday()}})})).matches||[],
      text:(r,k)=>{const f=fields.find(x=>x.key===k);const v=r[k];return f?Array.isArray(v)?v.map(x=>shownValue({...f,kind:'text'},x)).join(', '):shownValue(f,v).replace(/^—$/,''):'';},
      differs:(r,k,v)=>!same(v,stagedValue(key,r,k)),
      stage:list=>{stageCells(key,list);draw();}});
  }
  void load();
}
