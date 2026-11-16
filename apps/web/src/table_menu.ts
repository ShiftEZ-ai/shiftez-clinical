import {el,icon,popover,menuItem,handOver,refocusItem,type Child} from './dom.js';
import {observeSize} from './loading.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {overflowMore,type MoreNoun} from './dom.js';  // the one "+N" and its words (UX › controls S9)

// ---- Column width and order: one lifecycle for every table (shared collections, the Rules database, detail tables) ----
/** The width a person may give a column (px): the shared presentation clamp. */
export const COL_MIN=64,COL_MAX=800;
/** A cell drawn at a chosen width (CSS reads `--dt-w`), or back at its content's width. */
export function sized(n:HTMLElement,w:number|undefined){n.classList.toggle('dt-sized',!!w);if(w)n.style.setProperty('--dt-w',w+'px');else n.style.removeProperty('--dt-w');}
/** Every cell of one column (`data-col`) of a table, header included. */
export const sizeColumn=(table:Element,key:string,w:number|undefined)=>{for(const n of table.querySelectorAll<HTMLElement>(`:scope>*>tr>[data-col="${CSS.escape(key)}"]`))sized(n,w);};
const showWidth=(g:Element,w:number)=>{g.setAttribute('aria-valuenow',String(w));g.setAttribute('aria-valuetext',`${w} pixels`);};
/** A grip's value is its column's width now: set when the column is laid out (a saved width included) and whenever it
 *  changes, by one shared observer (no layout read of its own). A removed heading leaves it. */
const widthOf=(t:Element,{width}:{width:number})=>{const g=t.querySelector(':scope>.dt-resize');if(g&&width)showWidth(g,width);};
const unwatchWidth=new WeakMap<Element,()=>void>();
/** Watch the gripped headings under `root` (a heading row, or a page once it is shown). Observing again starts afresh: a
 *  page is drawn off-page and shown later (app.ts), and a heading observed before it had a layout can miss its first
 *  report, while a fresh observation of a laid-out heading always reports. */
export function trackWidths(root:ParentNode){for(const g of root.querySelectorAll('th>.dt-resize')){const th=g.parentElement!;unwatchWidth.get(th)?.();unwatchWidth.set(th,observeSize(th,widthOf));}}
/**
 * A column's width grip (WAI-ARIA window splitter) on the right edge of its heading: drag it, ← → (Shift: larger
 * steps) or double-click it to fit the contents again. `size` draws a width (undefined: the content's), `keep` stores it.
 * One drag at a time. Its document listeners go in one idempotent end(), whichever comes first: release, cancel,
 * Escape, lost capture, or the grip leaving the page mid-drag (navigation, a redraw: the browser then sends
 * lostpointercapture to the document, so it is listened for there). Only a drag still on the page puts its width back.
 */
export function columnGrip(o:{label:string;width:()=>number|undefined;size:(w:number|undefined,grip:HTMLElement)=>void;keep:(w:number|undefined)=>void;min?:number;max?:number}):HTMLElement{
  const min=o.min??COL_MIN,max=o.max??COL_MAX;
  const g=el('span',{class:'dt-resize',role:'separator','aria-orientation':'vertical',tabindex:0,'aria-label':`Resize ${o.label}`,
    'aria-valuemin':min,'aria-valuemax':max,'aria-keyshortcuts':'ArrowLeft ArrowRight'});
  const now=()=>Math.round(g.parentElement!.getBoundingClientRect().width);
  const apply=(w:number|undefined,keep:boolean)=>{if(w!==undefined)w=Math.round(Math.max(min,Math.min(max,w)));o.size(w,g);if(keep)o.keep(w);showWidth(g,w??now());};
  let from=0,x0=0,pid=-1,before:number|undefined,drag:{host:Element}|null=null;
  const host=()=>g.closest('table,[role=grid]')||g.parentElement!;
  const end=(how:'keep'|'undo',e?:PointerEvent)=>{const d=drag;if(!d)return;drag=null;
    for(const [type,f] of on)document.removeEventListener(type,f as EventListener,true);
    g.classList.remove('is-dragging');d.host.classList.remove('dt-resizing');
    if(g.isConnected){if(how==='keep'&&e)apply(from+e.clientX-x0,true);else if(how==='undo')apply(before,false);}
    if(g.hasPointerCapture(pid))g.releasePointerCapture(pid);};
  const mine=(e:PointerEvent)=>e.pointerId===pid;
  const on:[string,(e:never)=>void][]=[
    ['pointerup',(e:PointerEvent)=>{if(mine(e))end('keep',e);}],
    ['pointercancel',(e:PointerEvent)=>{if(mine(e))end('undo');}],
    // After pointerup (already ended), or capture taken away: the width it had before.
    ['lostpointercapture',(e:PointerEvent)=>{if(mine(e))end('undo');}],
    // Escape while dragging puts the width back; once the grip is gone, Escape is the page's again.
    ['keydown',(e:KeyboardEvent)=>{if(e.key!=='Escape')return;if(g.isConnected){e.preventDefault();e.stopPropagation();}end('undo');}],
  ];
  g.addEventListener('pointerdown',e=>{if(e.button!==0)return;e.preventDefault();e.stopPropagation();end('undo');
    pid=e.pointerId;g.setPointerCapture(pid);x0=e.clientX;from=now();before=o.width();drag={host:host()};
    g.classList.add('is-dragging');drag.host.classList.add('dt-resizing');
    for(const [type,f] of on)document.addEventListener(type,f as EventListener,true);});
  g.addEventListener('pointermove',e=>{if(drag&&mine(e)&&g.hasPointerCapture(e.pointerId))apply(from+e.clientX-x0,false);});
  g.addEventListener('click',e=>e.stopPropagation());
  g.addEventListener('dblclick',e=>{e.stopPropagation();apply(undefined,true);});
  g.addEventListener('focus',()=>showWidth(g,now()));
  g.addEventListener('keydown',e=>{if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight')return;e.preventDefault();e.stopPropagation();
    apply(now()+(e.key==='ArrowRight'?1:-1)*(e.shiftKey?64:16),true);
    // Focus stays on the grip, also when the new width draws its view again (its successor by name; maintenance).
    const name=g.getAttribute('aria-label')||'';
    requestAnimationFrame(()=>{const at=document.activeElement;if(at&&at!==document.body)return;
      (g.isConnected?g:document.querySelector<HTMLElement>(`.dt-resize[aria-label="${CSS.escape(name)}"]`))?.focus({preventScroll:true});});});
  return g;
}
/**
 * Order by pointer: drag a heading (mouse or pen; past a few pixels, so a click still opens its menu) onto another
 * column (`th[data-reorder]` in the same row); `commit` gets the heading it was dropped on. Touch scrolls the table
 * instead: the column menu's Move left / right orders columns there and from the keyboard.
 */
export function headerReorder(th:HTMLElement,commit:(target:HTMLElement)=>void){
  th.addEventListener('pointerdown',e=>{
    if(e.button!==0||e.pointerType==='touch'||(e.target as Element).closest('.dt-resize'))return;
    const id=e.pointerId,x0=e.clientX,y0=e.clientY,row=th.parentElement!,table=th.closest('table')!,scroller=th.closest<HTMLElement>('.table-scroll');
    let dragging=false,target:HTMLElement|null=null;
    const heads=()=>[...row.querySelectorAll<HTMLElement>(':scope>th[data-reorder]')];
    const mark=(at:HTMLElement|null)=>{for(const h of heads())h.classList.remove('dt-drop-target');target=at;if(at&&at!==th)at.classList.add('dt-drop-target');};
    const move=(ev:PointerEvent)=>{if(ev.pointerId!==id)return;if(!th.isConnected){end(false);return;}
      if(!dragging){if(Math.hypot(ev.clientX-x0,ev.clientY-y0)<5)return;
        dragging=true;th.setPointerCapture(id);getSelection()?.removeAllRanges();th.classList.add('dt-drag-src');table.classList.add('dt-reordering');}
      // Near the scroller's edge the table scrolls, so a column can go to a place off screen.
      if(scroller){const r=scroller.getBoundingClientRect();scroller.scrollLeft+=ev.clientX<r.left+32?-16:ev.clientX>r.right-32?16:0;}
      const list=heads();
      mark(list.find(h=>{const r=h.getBoundingClientRect();return ev.clientX>=r.left&&ev.clientX<r.right;})||(ev.clientX<list[0].getBoundingClientRect().left?list[0]:list[list.length-1]));};
    const swallow=(ev:Event)=>{ev.preventDefault();ev.stopPropagation();};
    const end=(ok:boolean)=>{
      document.removeEventListener('pointermove',move,true);document.removeEventListener('pointerup',up,true);document.removeEventListener('pointercancel',cancel,true);document.removeEventListener('keydown',quit,true);document.removeEventListener('lostpointercapture',cancel,true);
      if(!dragging)return;
      if(th.hasPointerCapture(id))th.releasePointerCapture(id);
      th.classList.remove('dt-drag-src');table.classList.remove('dt-reordering');const to=target;mark(null);
      // The heading left the page mid-drag (navigation, a redraw): nothing to move, and the next click is someone else's.
      if(!th.isConnected)return;
      // The click that ends a drag neither opens the column menu nor sorts; it may come after the redraw, on the moved
      // heading: it is dropped once, unless another press comes first.
      const keep=()=>document.removeEventListener('click',swallow,true);
      document.addEventListener('click',swallow,{capture:true,once:true});document.addEventListener('pointerdown',keep,{capture:true,once:true});document.addEventListener('keydown',keep,{capture:true,once:true});
      if(ok&&to&&to!==th)commit(to);};
    const up=(ev:PointerEvent)=>{if(ev.pointerId===id)end(true);},cancel=(ev:PointerEvent)=>{if(ev.pointerId===id)end(false);};
    const quit=(ev:KeyboardEvent)=>{if(ev.key!=='Escape'||!dragging)return;if(th.isConnected){ev.preventDefault();ev.stopPropagation();}end(false);};
    // A heading removed while it holds the pointer loses it to the document: the drag ends there.
    document.addEventListener('pointermove',move,true);document.addEventListener('pointerup',up,true);document.addEventListener('pointercancel',cancel,true);document.addEventListener('keydown',quit,true);document.addEventListener('lostpointercapture',cancel,true);
  });
}


/** What a column header's menu offers; each part is optional except Wrap. */
export interface ColumnMenu{
  label:string;
  /** Existing non-interactive heading content (for example a matrix's vertical label). */
  content?:Child;
  /** A separate menu button beside an interactive heading; never nest its controls. */
  compact?:boolean;
  sort?:{dir:'asc'|'desc'|null;set:(dir:'asc'|'desc'|null)=>void};
  /** Builds the column's filter control into the menu (the same picker the Filter button uses). */
  filter?:(panel:HTMLElement)=>void;
  group?:{on:boolean;set:(on:boolean)=>void};
  wrap:{on:boolean;set:(on:boolean)=>void};
  wrapAll?:{on:boolean;set:(on:boolean)=>void};
  hide?:()=>void;
  move?:{left:boolean;right:boolean;go:(by:-1|1)=>void};
  /** Given when the column was resized: back to the width its contents need. */
  resetWidth?:()=>void;
  /** D-057: a selector for this heading's button that holds across redraws (its table's id and the column's key). An
   *  open menu then moves to the heading now matching, with that heading's items; it closes when none matches. */
  keep?:string;
}
/** Each heading button's menu contents, so a kept menu refills from the heading that replaced its own. */
const fills=new WeakMap<HTMLElement,(panel:HTMLElement,close:()=>void)=>void>();
/**
 * A shared table's column header: the label (with its sort arrow) is a button that opens the column menu — on click,
 * Enter/Space or right-click — with Sort A→Z / Z→A, Filter by, Group by, Wrap this column (a switch), Hide column and
 * Move left / right. Every choice is saved with the view by the table.
 */
export function columnHeader(m:ColumnMenu):HTMLButtonElement{
  const dir=m.sort?.dir;
  const b=el('button',{type:'button',class:'dt-sort'+(m.compact?' dt-column-options':''),...popup('menu'),...(m.compact?{'aria-label':`${m.label} column options`}:{})},m.compact?null:m.content??el('span',{class:'dt-column-label'},m.label),
    dir?icon(dir==='asc'?'chevronUp':'chevronDown'):null,el('span',{class:'dt-col-caret','aria-hidden':'true'},icon('chevronDown')));
  // The menu's contents for this heading as drawn; also what a kept menu is filled with again (D-057, `keep`).
  const fill=(panel:HTMLElement,close:()=>void)=>{
    // Pressed between a redraw and the hand-over: the same item of the new heading's menu runs instead (dom.ts handOver).
    const stale=(x:HTMLElement)=>!!m.keep&&handOver(x);
    const item=(label:string,glyph:Parameters<typeof icon>[0],run:()=>void,attrs:Record<string,unknown>={},closes=true)=>{
      const x:HTMLElement=menuItem(label,glyph,()=>{if(stale(x))return;if(closes)close();run();},attrs);return x;};
    const check=(label:string,glyph:Parameters<typeof icon>[0],on:boolean,run:()=>void,role='menuitemcheckbox')=>{
      const x=item(label,glyph,run,{role,'aria-checked':String(on)});if(role==='menuitemcheckbox')x.append(el('span',{class:'switch-mark','aria-hidden':'true'}));else if(on)x.append(icon('check'));return x;};
    const list=el('div',{class:'menu-list',role:'menu','aria-label':`${m.label} column`},
      ...(m.sort?[check('Sort A → Z','chevronUp',dir==='asc',()=>m.sort!.set(dir==='asc'?null:'asc'),'menuitemradio'),
        check('Sort Z → A','chevronDown',dir==='desc',()=>m.sort!.set(dir==='desc'?null:'desc'),'menuitemradio')]:[]),
      // The filter view holds fields being typed: a kept menu then only follows its new heading, never refills over them.
      ...(m.filter?[item(`Filter by ${m.label}…`,'filter',()=>{panel.dataset.colView='filter';panel.replaceChildren(el('div',{class:'dt-pop-label'},`Filter by ${m.label}`));m.filter!(panel);
        (panel.querySelector('button,input') as HTMLElement|null)?.focus();},{},false)]:[]),
      ...(m.group?[check(`Group by ${m.label}`,'layers',m.group.on,()=>m.group!.set(!m.group!.on))]:[]),
      check('Wrap this column','menu',m.wrap.on,()=>m.wrap.set(!m.wrap.on)),
      ...(m.wrapAll?[check('Wrap all columns','menu',m.wrapAll.on,()=>m.wrapAll!.set(!m.wrapAll!.on))]:[]),
      ...(m.hide?[item('Hide column','eyeOff',m.hide)]:[]),
      ...(m.move?[item('Move left','chevronLeft',()=>m.move!.go(-1),{disabled:!m.move.left}),item('Move right','chevronRight',()=>m.move!.go(1),{disabled:!m.move.right})]:[]),
      ...(m.resetWidth?[item('Reset width','reset',m.resetWidth)]:[]));
    // No footnote: the menu was just opened that way (X-18).
    panel.append(list);
  };
  fills.set(b,fill);
  const open=()=>{let pane:HTMLElement|null=null,shut=()=>{};
    popover(b,(panel,close)=>{pane=panel;shut=close;panel.classList.add('dt-col-pop');fill(panel,close);},{label:`${m.label} column`,placement:'below',
      // D-057: a redraw replacing this heading hands the open menu to the same column's new heading, its items those of
      // the column as it is now and the focus on the same item; no such heading, it closes.
      keep:m.keep?()=>{
        const next=document.querySelector<HTMLElement>(m.keep!),again=next&&fills.get(next);if(!pane||!again)return null;
        if(pane.dataset.colView==='filter')return next;
        const at=pane.contains(document.activeElement)?document.activeElement!.textContent:null;
        pane.replaceChildren();again(pane,shut);refocusItem(pane,at);return next;}:undefined});};
  b.addEventListener('click',e=>{if(m.compact)e.stopPropagation();open();});
  b.addEventListener('contextmenu',e=>{e.preventDefault();if(m.compact)e.stopPropagation();if(b.getAttribute('aria-expanded')!=='true')open();});
  if(m.compact)b.addEventListener('keydown',e=>e.stopPropagation());
  return b;
}

/** The same all-column switch in a collection's View settings and its column menus. The owner stores the choice. */
export function wrapAllColumns(on:boolean,set:(on:boolean)=>void):HTMLElement{
  const box=el('input',{type:'checkbox',role:'switch',checked:on}) as HTMLInputElement;
  box.addEventListener('change',()=>set(box.checked));
  return el('label',{class:'dt-pop-item dt-wrap-all'},box,el('span',{},'Wrap all columns'),el('span',{}));
}

/** A heading with its own controls or a rule pin: it keeps its content and gets a compact options button. */
const interactiveHeading=(th:Element)=>th.matches('[tabindex],[data-rk-pin]')||!!th.querySelector('button,a,input,select,textarea,[tabindex]:not(.dt-resize)');
/** Call before a grip goes into a heading: the grip's own name would join a heading named by its content, so a plain
 *  heading keeps its visible text as its name, as dataTable's do. An explicit name, an interactive heading (its own
 *  controls, rule markers) or an empty structural one keeps the name it has. */
export function gripHeadingName(h:Element,label=h.textContent?.trim()||''){
  if(label&&!h.hasAttribute('aria-label')&&!h.hasAttribute('aria-labelledby')&&!interactiveHeading(h))h.setAttribute('aria-label',label);
}
/** Detail tables and matrices (no saved view of their own): each column's width, and the order of a plain table's
 *  columns, kept on this device per table shape (its class and headings; `rota.*`, forgotten at sign-out). The same grip,
 *  drag and keyboard as the shared collections; a table with spanning or row-heading cells is resized only, never
 *  reordered (moving a matrix's days would misread it). */
type Sizing={reset:(key:string)=>(()=>void)|undefined;move:(key:string)=>{left:boolean;right:boolean;go:(by:-1|1)=>void}|undefined};
const sizings=new WeakMap<HTMLTableElement,Sizing>();
export function columnSizing(table:HTMLTableElement,o:{reorder?:boolean}={}):Sizing{
  const had=sizings.get(table);if(had)return had;
  const head=table.tHead?.rows[0];
  const none:Sizing={reset:()=>undefined,move:()=>undefined};
  if(!head){sizings.set(table,none);return none;}
  const seen=new Map<string,number>();
  const cols=[...head.cells].flatMap(th=>{const label=th.textContent?.trim()||'';if(!label||th.colSpan!==1)return [];
    const n=(seen.get(label)||0)+1;seen.set(label,n);return [{th,label,key:n>1?`${label}#${n}`:label}];});
  if(!cols.length){sizings.set(table,none);return none;}
  const bodyRows=[...table.tBodies].flatMap(b=>[...b.rows]);
  const flat=[head,...bodyRows].every(r=>[...r.cells].every(c=>c.colSpan===1&&c.rowSpan===1&&c.tagName==='TD'||r===head))&&head.cells.length===cols.length;
  const store='rota.cols.'+(table.className||'table')+':'+cols.map(c=>c.key).join('|');
  let kept:{w:Record<string,number>;o:string[]}={w:{},o:[]};
  try{const x=JSON.parse(localStorage.getItem(store)||'null');if(x&&typeof x==='object')kept={w:x.w&&typeof x.w==='object'?x.w:{},o:Array.isArray(x.o)?x.o:[]};}catch{/* nothing kept */}
  const write=()=>{try{if(Object.keys(kept.w).length||kept.o.length)localStorage.setItem(store,JSON.stringify(kept));else localStorage.removeItem(store);}catch{/* not kept */}};
  // Each cell names its column, so a width reaches the whole column (sizeColumn) and a move takes every cell along.
  const at=new Map<HTMLTableCellElement,number>();{let n=0;for(const th of head.cells){at.set(th,n);n+=th.colSpan;}}
  const index=new Map(cols.map(c=>[at.get(c.th)!,c.key]));
  for(const r of [head,...bodyRows]){let at=0;for(const cell of r.cells){if(cell.colSpan===1&&index.has(at))cell.dataset.col=index.get(at)!;at+=cell.colSpan;}}
  const reorder=o.reorder!==false&&flat&&cols.length>2;
  const keys=()=>[...head.cells].map(c=>c.dataset.col!).filter(Boolean);
  const arrange=(order:string[])=>{for(const r of [head,...bodyRows]){const byKey=new Map([...r.cells].map(c=>[c.dataset.col,c]));for(const k of order){const c=byKey.get(k);if(c)r.append(c);}}};
  const setOrder=(order:string[])=>{kept.o=order.join()===cols.map(c=>c.key).join()?[]:order;write();arrange(order);};
  for(const c of cols){
    gripHeadingName(c.th,c.label);
    c.th.append(columnGrip({label:`${c.label} column`,width:()=>kept.w[c.key],keep:w=>{if(w)kept.w[c.key]=w;else delete kept.w[c.key];write();},size:w=>sizeColumn(table,c.key,w)}));
    if(kept.w[c.key])sizeColumn(table,c.key,kept.w[c.key]);
    if(reorder){c.th.dataset.reorder='';headerReorder(c.th,to=>{const k=keys(),from=k.indexOf(c.key),at=k.indexOf(to.dataset.col!);if(from<0||at<0)return;k.splice(from,1);k.splice(at,0,c.key);setOrder(k);});}
  }
  if(reorder&&kept.o.length&&kept.o.every(k=>cols.some(c=>c.key===k)))arrange(kept.o);
  trackWidths(head);
  const sizing:Sizing={reset:key=>kept.w[key]?()=>{delete kept.w[key];write();sizeColumn(table,key,undefined);}:undefined,
    move:key=>{if(!reorder)return undefined;const k=keys(),i=k.indexOf(key);
      return {left:i>0,right:i<k.length-1,go:by=>{const n=keys(),j=n.indexOf(key);[n[j],n[j+by]]=[n[j+by],n[j]];setOrder(n);}};}};
  sizings.set(table,sizing);return sizing;
}
/** Give compact detail tables and matrices the same menus without replacing their cells, controls or row groups.
 *  These renderers have one row of data headings. Blank action headings and spanning summary cells stay structural.
 *  Every column is resizable at once (`columnSizing`); `whenUseful`: the menus (Wrap, Reset width, Move) are added only
 *  once the table, unwrapped, would be wider than its box: a short table that never wraps has plain headings (X-18). */
export function tableColumnMenus(table:HTMLTableElement,initiallyWrapped=true,whenUseful=false,o:{reorder?:boolean}={}):void{
  const sizing=columnSizing(table,o);
  if(whenUseful){
    // Measured after layout, and again when the box changes size (a phone turned, a panel opened), until it is needed.
    const unwatch=observeSize(table,()=>{
      const box=table.parentElement;if(!table.isConnected||!box||!table.tBodies[0]?.rows.length||table.querySelector('.empty-row'))return;
      table.classList.add('dt-measure');const wide=table.scrollWidth>box.clientWidth+1;table.classList.remove('dt-measure');
      if(wide){unwatch();tableColumnMenus(table,initiallyWrapped,false,o);}});
    return;
  }
  const headings=[...(table.tHead?.rows[0]?.cells||[])],columns=headings.flatMap((heading,index)=>{
    const label=heading.textContent?.trim()||'';
    return label&&heading.colSpan===1?[{heading,index,label}]:[];
  }),wrapped=new Set(initiallyWrapped?columns.map(c=>c.heading):[]);
  // Cells by column key (columnSizing named them), so wrapping follows a column the person moved.
  const cellsOf=(c:{heading:HTMLTableCellElement})=>[...table.querySelectorAll<HTMLElement>(`:scope>*>tr>[data-col="${CSS.escape(c.heading.dataset.col||'')}"]`)];
  const sync=()=>columns.forEach(c=>cellsOf(c).forEach(cell=>{cell.classList.add('dt-column-cell');cell.classList.toggle('dt-wrap',wrapped.has(c.heading));}));
  const all={get on(){return columns.every(c=>wrapped.has(c.heading));},set:(on:boolean)=>{columns.forEach(c=>on?wrapped.add(c.heading):wrapped.delete(c.heading));sync();}};
  for(const c of columns){
    const interactive=interactiveHeading(c.heading);
    const grip=c.heading.querySelector(':scope>.dt-resize');
    const content=interactive?undefined:el('span',{class:'dt-column-label'},...[...c.heading.childNodes].filter(n=>n!==grip));
    const key=c.heading.dataset.col||'';
    const menu=columnHeader({label:c.label,content,compact:interactive,wrap:{get on(){return wrapped.has(c.heading);},set:on=>{on?wrapped.add(c.heading):wrapped.delete(c.heading);sync();}},wrapAll:all,
      get move(){return sizing.move(key);},get resetWidth(){return sizing.reset(key);}});
    c.heading.classList.add('dt-column-heading');if(grip)grip.before(menu);else c.heading.append(menu);
  }
  sync();
}

/**
 * Pills on one line: the first `shown` and a "+N" button that opens the whole list, numbered. In a wrapped column
 * (`.dt-wrap`, Wrap this column / Wrap all columns) every pill shows and "+N" hides — CSS only, no redraw.
 * Interactive pills supply a factory so the expanded list keeps their handlers and its own visible anchors.
 */
export type PillItem=Child|(()=>Child);
const ITEMS=['item','items'] as const;
/** `rest`: the items are only the part of a record someone chose to see (a person display draws some of a session's
 *  people). They all show, and "+N" counts exactly the rest, stays when wrapped or printed and opens the whole record
 *  (`open`), never a list of just these items. */
/** `counts`: how many of `label` each item holds (a supervision team box holds its whole team), so "+N" and "show all"
 *  count people, not pills. `nouns`: what each item is (a rank, a clinic; none for a mixed group): "+N" names the hidden
 *  ones' kind when they share one ("2 more ranks"), else "items"; `label` then only titles the list it opens. */
export function pillList(items:PillItem[],o:{label:MoreNoun;shown?:number;rest?:{count:number;open:()=>void};counts?:number[];
  nouns?:(readonly [string,string]|null)[]}):HTMLElement{
  const kept=items.map((x,i)=>({x,c:o.counts?.[i]??1,noun:o.nouns?.[i]??null})).filter(({x})=>x!==null&&x!==undefined&&x!==false&&x!=='');
  const nodes=kept.map(k=>k.x),rest=o.rest?.count?o.rest:null,n=rest?nodes.length:o.shown??2;
  const sum=(from:number,to=kept.length)=>kept.slice(from,to).reduce((t,k)=>t+k.c,0),many=sum(0);
  const draw=(x:PillItem,copy=false)=>typeof x==='function'?x():copy&&x instanceof Node?x.cloneNode(true):x;
  const wrap=el('span',{class:'pill-list'},...nodes.map((x,i)=>el('span',{class:i<n?'pill-item':'pill-item pill-over'},draw(x))));
  if(rest){
    wrap.append(overflowMore(rest.count,o.label,{all:many+rest.count,open:rest.open,cls:'pill-more others'}));
  }else if(nodes.length>n){
    const hidden=kept.slice(n).map(k=>k.noun),kind=hidden[0],title=typeof o.label==='string'?o.label:o.label[1];
    const noun=!o.nouns?o.label:kind&&hidden.every(x=>x?.[1]===kind[1])?kind:ITEMS;
    const more=overflowMore(sum(n),noun,{all:many,title,list:()=>nodes.map(x=>draw(x,true))});
    wrap.append(more);
  }
  return wrap;
}
