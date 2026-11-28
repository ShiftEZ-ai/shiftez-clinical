/** Print or export a Schedule view: one dialog with a live paged preview, presets (print_presets.ts) and every page
 *  setting. The view is copied (never moved), cleaned of controls and laid out in real pages here: column headers
 *  repeated, rows never split, a group header kept with its first row, page numbers counted. The preview is those pages,
 *  so it prints as it shows. PDF goes through the browser's print window (vector, searchable text), PNG through the
 *  feedback capture's renderer one page per image at the chosen image quality (never the screen's pixel density), CSV from
 *  the view's table (a view without one is drawn as the List). Always in the light theme. */
import {el,button,withIcon,select,check,field,notify,showDialog,popover} from './dom.js';
import {segment} from './view_settings.js';
import {render} from './feedback_capture.js';
import {printPages,type PrintData} from './schedule_print.js';
import {zipStore} from './zip.js';
import {savedViewItems,fetchViews,type SavedView} from './table_view.js';
import {PRINT_IMAGE_DPI,type PrintSettings,type TableViewDefinition,type TableViewDefaults} from './generated.js';
import {BUILT_INS,INCLUDES,normalise,samePrint,startingPreset,listPresets,session,openingPrint,dpiOf,PRINT_KEY,type Print} from './print_presets.js';

export interface ExportSource{
  /** The view's name ("Week", or a saved view's name), the period on screen and the department. */
  title:string;period:string;department:string;
  /** "Published", a published version, or the draft's name and stage; a draft is marked "Draft, not published". */
  status:string;draft:boolean;
  /** Who exports, for "Printed by … on …". */
  by:string;
  /** The active filters as "Name: value", for Include › Active filters. */
  filters:string[];
  /** The view panel (the grid and its key); it is copied, never moved. */
  view:HTMLElement;
  /** Week and Month: the view's own data, drawn for paper by the one renderer (schedule_print.ts), not copied. */
  data?:PrintData|null;
  /** Opt in to the slice-0 paper rules in cleanCopy (no controls, one header row, no empty weeks/lanes/groups, sessions stack); off by default. */
  prune?:boolean;
}
/** What the Schedule offers the dialog: the view on screen, its saved views, and drawing another view or range. */
export interface ExportHost{
  /** Saved Schedule views ("What"). */
  views:{id:string;name:string}[];
  /** Whether a rota is in view (Range › This rota). */
  rota:boolean;
  /** Draws the view and range asked for behind the dialog, one chunk per week or month when pages break so, and
   *  returns a copy of each (the view on screen as it is, when nothing else is asked); `restore` puts the Schedule back. */
  draw(o:{view_id:string|null;range:Print['range'];chunk:'week'|'month'|null;table:boolean}):Promise<ExportSource[]>;
  restore():void;
  /** Coordinators and admins share presets and set the department's default. */
  canShare:boolean;
  /** The saved view on screen: the print settings last used from it, and `keep` to record them on Print or Save
   *  (absent when this person cannot change the view). */
  kept?:PrintSettings|null;
  keep?:(print:Print)=>void;
  /** The published rota's own print sheet (server HTML), when there is one to print. */
  publishedSheet?:()=>void;
  /** View settings in display-only mode (Rows and columns, Properties, Colour by, List columns, Density) for this
   *  export alone; `changed` draws the preview again. */
  adjust?:(panel:HTMLElement,close:()=>void,changed:()=>void)=>void;
  /** A picked person (Search or Filter): who each duty lists in this export (Show). */
  people?:{options:[string,string][];value:()=>string;set:(key:string)=>void};
}
const TOO_LARGE='The image is too large for this browser. Choose a lower image quality or a smaller paper, or save a PDF.';

type Paper=Print['paper'];
const PAPER:Record<Paper,[number,number]>={a4:[210,297],a3:[297,420],letter:[215.9,279.4],legal:[215.9,355.6]};  // mm, portrait
const MARGIN:Record<Print['margins'],number>={narrow:6,normal:12,wide:20};  // mm
const TEXT:Record<Print['text_size'],number>={small:.85,normal:1,large:1.2};
const PX=96/25.4;
/** The page in mm and its printable box in CSS px. */
export function pageBox(s:Pick<Print,'paper'|'orientation'|'margins'>){
  const [w,h]=PAPER[s.paper],[pw,ph]=s.orientation==='landscape'?[h,w]:[w,h],m=MARGIN[s.margins];
  return {pw,ph,m,width:Math.floor((pw-2*m)*PX),height:Math.floor((ph-2*m)*PX)};
}
/** A page's size in image pixels at `dpi` (the paper's millimetres, whatever the screen). */
export function pagePixels(s:Pick<Print,'paper'|'orientation'>,dpi:number){
  const [w,h]=PAPER[s.paper],[pw,ph]=s.orientation==='landscape'?[h,w]:[w,h];
  return {width:Math.round(pw/25.4*dpi),height:Math.round(ph/25.4*dpi)};
}
const stamp=()=>new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date());

/** What paper never shows, in every view (D-127 E2): fields, icon and text buttons, an empty state's icon, planner editing
 *  state (a lock, a selection box, a drag handle: DL-060), and what a view marks `data-export-skip`. Any other button is content drawn as a button and is kept as plain text. */
export const EXPORT_SKIP='input,select,textarea,.button,.icon-button,.empty-icon,.wk-lock,.wk-check-hit,[draggable=true],[data-export=skip],[data-export-skip]';
/** A copy of the view without controls, side panels, menus and live accents; every person in a busy cell shown.
 *  `prune` (default off, dev's export unchanged): the slice-0 paper rules below (E1/E2/E5/E7). */
function cleanCopy(view:HTMLElement,s:Print,prune=false):HTMLElement{
  const copy=view.cloneNode(true) as HTMLElement;
  copy.removeAttribute('id');
  copy.querySelectorAll('.wk-more,.rc-panel,.side-panel,.selection-bar,.wk-check,.bulk-check,.dt-check-hit,.popover,.dt-actions-col,[data-export-skip],.wk-toggle svg,.wk-bar,.spinner,.dt-resize').forEach(n=>n.remove());
  copy.querySelectorAll('[id]').forEach(n=>n.removeAttribute('id'));
  copy.querySelectorAll('.today,.is-today,.is-explained,.rk-hit,.is-new').forEach(n=>n.classList.remove('today','is-today','is-explained','rk-hit','is-new'));
  copy.querySelectorAll('[aria-current]').forEach(n=>n.removeAttribute('aria-current'));
  // Paper has no "+ N more": a cell whose people are only hidden shows them all (a person display's others stay counted).
  copy.querySelectorAll('.pill-more:not(.others)').forEach(n=>n.remove());copy.querySelectorAll('.pill-over').forEach(n=>n.classList.remove('pill-over'));
  copy.querySelectorAll<HTMLElement>(':is(.pchip,.team-box)[hidden]').forEach(n=>{n.parentElement?.querySelector(':scope>.pchip.more')?.remove();n.hidden=false;});
  copy.querySelectorAll('td:last-child:empty,th.dt-actions-col').forEach(n=>n.remove());
  copy.querySelectorAll('.lg-settings').forEach(n=>n.remove());
  if(!s.include.includes('key'))copy.querySelectorAll('.view-legend').forEach(n=>n.remove());
  if(prune){
    // Paper has no controls (D-127 E2), in every view: fields, icon and text buttons, an empty state's icon go; what is
    // drawn as a button but is content (a person's pill, a timeline bar, a group's name) stays as plain text.
    const empty=!!copy.querySelector('.empty-state');
    copy.querySelectorAll(EXPORT_SKIP).forEach(n=>n.remove());
    copy.querySelectorAll('button').forEach(b=>{const t=document.createElement('span');t.className=b.className;t.append(...b.childNodes);b.replaceWith(t);});
    copy.querySelectorAll('[role=button]').forEach(n=>n.removeAttribute('role'));
    copy.querySelectorAll('[tabindex]').forEach(n=>n.removeAttribute('tabindex'));
    copy.querySelectorAll('.wk-grid').forEach(g=>{
      // One header row (E5): a month's dated week bands stand for the weekday row above them (the planner repeats a band).
      if(g.querySelector(':scope>.mo-wk'))g.querySelector(':scope>.wk-head')?.remove();
      // No empty space (E7): a week with no sessions goes with its band, a lane with nothing on any day goes, and a group
      // left with no lanes and nothing counted goes.
      g.querySelectorAll(':scope>.mo-none:not(:has(.empty-state))').forEach(n=>{if(n.previousElementSibling?.matches('.mo-wk'))n.previousElementSibling.remove();n.remove();});
      const blank=(r:Element)=>[...r.querySelectorAll(':scope>.wk-cell')].every(c=>!(c.textContent||'').replace(/[\s—–-]/g,''));
      g.querySelectorAll(':scope>.wk-row:not(.wk-head,.mo-wk,.wk-group,.mo-none)').forEach(r=>{if(r.querySelector(':scope>.wk-cell')&&blank(r))r.remove();});
      [...g.querySelectorAll(':scope>.wk-group')].reverse().forEach(r=>{const next=r.nextElementSibling;
        if((!next||next.matches(r.matches('.wk-subgroup')?'.wk-group':'.wk-group:not(.wk-subgroup)'))&&blank(r))r.remove();});
    });
    // Several sessions in a day stack (E1): each session starts a new line; its people still flow side by side.
    copy.querySelectorAll('.wk-cell').forEach(c=>{let last='';
      for(const n of [...c.children] as HTMLElement[]){const id=n.dataset.shift||n.querySelector<HTMLElement>('[data-shift]')?.dataset.shift||'';
        if(id&&last&&id!==last)n.before(el('span',{class:'export-break','aria-hidden':'true'}));if(id)last=id;}});
    // Nothing to show: said in words, once (pagesOf keeps such a page only when every page would be one).
    if(empty||[...copy.querySelectorAll('.wk-grid')].some(g=>!g.querySelector(':scope>.wk-row:not(.wk-head,.mo-wk)')))copy.dataset.exportEmpty='';
  }
  // Paper has no today: its stamp goes with the accent, so the copied key lists only the marks the copy draws (the
  // stamps, dom.ts legend `mark`, are copied with the marks; an entry whose marks were cleaned out above goes too).
  copy.querySelectorAll('[data-lg*=":today"]').forEach(n=>{const v=n.getAttribute('data-lg')!.split(' ').filter(x=>!x.endsWith(':today')).join(' ');if(v)n.setAttribute('data-lg',v);else n.removeAttribute('data-lg');});
  copy.querySelectorAll<HTMLElement>('.view-legend').forEach(k=>{
    k.querySelectorAll<HTMLElement>('li[data-lg-entry]').forEach(li=>{if(!copy.querySelector(`[data-lg~="${CSS.escape(li.dataset.lgEntry!)}"]`))li.remove();});
    if(!k.querySelector('li[data-lg-entry]'))k.hidden=true;});
  return copy;
}

// ---- Pages ----
const PAGED='.wk-grid,.people-grid,.sd-tl,.sd-year,table';
type Unit={owners:HTMLElement[];top:number;bottom:number;kind:'head'|'group'|'row'};
/** The view's rows as they are drawn: grids whose rows are boxes, `display:contents` rows or bare cells side by side
 *  (one row per line), and table rows. Column headers and group headers are told apart. */
function unitsOf(c:HTMLElement):Unit[]{
  const owners=c instanceof HTMLTableElement?[...c.rows]:[...c.children] as HTMLElement[],out:Unit[]=[];
  for(const o of owners){
    const leaves=getComputedStyle(o).display==='contents'?[...o.children]:[o];
    const rects=leaves.map(l=>l.getBoundingClientRect()).filter(r=>r.height>0);if(!rects.length)continue;
    const top=Math.min(...rects.map(r=>r.top)),bottom=Math.max(...rects.map(r=>r.bottom));
    const kind=o.matches('.wk-head,.pg-head,.wk-corner,.sd-axis,thead>tr')?'head':o.matches('.mo-wk,.wk-group,.pg-group')?'group':'row';
    const last=out.at(-1);
    if(last&&Math.abs(last.top-top)<1){last.owners.push(o);last.bottom=Math.max(last.bottom,bottom);}
    else out.push({owners:[o],top,bottom,kind});
  }
  return out;
}
type Plan={rows:number[];carry:number;first:boolean;postOnly?:boolean};
type Measured={copy:HTMLElement;container:HTMLElement|null;units:Unit[];heads:number;pre:number;post:number;heights:number[]};
/** Rows to pages: the column header on each page (or the first), a group header never last on a page and repeated
 *  ("continued") above its rows on the next, a new page at each group when asked. */
export function plan(m:{units:{kind:Unit['kind']}[];heights:number[];pre:number;post:number},budget:{first:number;next:number},repeat:boolean,breakAt:(i:number)=>boolean):Plan[]{
  const heads=m.units.findIndex(u=>u.kind!=='head'),nHead=heads<0?m.units.length:heads;
  const headH=m.heights.slice(0,nHead).reduce((a,b)=>a+b,0);
  const pages:Plan[]=[];let cur!:Plan,used=0,group=-1;
  const open=(first:boolean)=>{cur={rows:[],carry:-1,first};pages.push(cur);used=(first?m.pre:0)+(first||repeat?headH:0);};
  open(true);
  for(let i=nHead;i<m.units.length;i++){
    const u=m.units[i],h=m.heights[i],withNext=u.kind==='group'&&m.units[i+1]?.kind==='row'?m.heights[i+1]:0;
    if(cur.rows.length&&(breakAt(i)||used+h+withNext>(cur.first?budget.first:budget.next))){
      open(false);
      if(u.kind!=='group'&&group>=0){cur.carry=group;used+=m.heights[group];}
    }
    cur.rows.push(i);used+=h;if(u.kind==='group')group=i;
  }
  if(m.post>0&&used+m.post>(cur.first?budget.first:budget.next)&&cur.rows.length)pages.push({rows:[],carry:-1,first:false,postOnly:true});
  return pages;
}
/** Lays a copy out at the body's width and zoom (off screen, as print lays it out) and measures its rows as drawn on
 *  the paper: zoomed text wraps and rounds differently, so a height measured unzoomed times the zoom is not what is drawn. */
function measure(copy:HTMLElement,width:number,host:HTMLElement,zoom=1):Measured{
  const body=el('div',{class:'export-body'},copy);body.style.setProperty('width',width+'px');body.style.setProperty('zoom',String(zoom));
  host.replaceChildren(el('section',{class:'export-page export-sheet'},body));
  const container=[...copy.querySelectorAll<HTMLElement>(PAGED)].filter(n=>!n.parentElement?.closest(PAGED)).sort((a,b)=>b.offsetHeight-a.offsetHeight)[0]||null;
  const top=copy.getBoundingClientRect().top,bottom=copy.getBoundingClientRect().bottom;
  if(!container)return {copy,container,units:[],heads:0,pre:bottom-top,post:0,heights:[]};
  const units=unitsOf(container);
  const heights=units.map((u,i)=>(units[i+1]?.top??u.bottom)-u.top);
  // Every page keeps the same columns: the widths drawn for the whole view, fixed.
  if(container instanceof HTMLTableElement){container.querySelectorAll<HTMLElement>('thead th').forEach(th=>th.style.setProperty('width',th.getBoundingClientRect().width/zoom+'px'));container.classList.add('fixed-cols');}
  else{const cols=getComputedStyle(container).gridTemplateColumns;if(cols&&cols!=='none')container.style.setProperty('grid-template-columns',cols);}
  units.forEach((u,i)=>u.owners.forEach(o=>o.dataset.xi=String(i)));container.dataset.xc='';
  const heads=units.findIndex(u=>u.kind!=='head');
  return {copy,container,units,heads:heads<0?units.length:heads,pre:units.length?units[0].top-top:0,post:units.length?bottom-units.at(-1)!.bottom:0,heights};
}
/** One page's copy: the rows it holds, what comes before the rows on the first page, what comes after on the last. */
function pageCopy(m:Measured,p:Plan,last:boolean,repeat:boolean):HTMLElement{
  const copy=m.copy.cloneNode(true) as HTMLElement,c=copy.querySelector<HTMLElement>('[data-xc]');
  if(!c)return copy;
  const heads=p.first||repeat?m.units.slice(0,m.heads).map((_,i)=>i):[];
  const keep=new Set<number>([...heads,...(p.carry>=0?[p.carry]:[]),...p.rows]);
  c.querySelectorAll<HTMLElement>('[data-xi]').forEach(o=>{if(!keep.has(Number(o.dataset.xi)))o.remove();});
  if(p.carry>=0)c.querySelector(`[data-xi="${p.carry}"]`)?.classList.add('export-continued');
  // Before the rows on later pages, after them on all but the last: removed along the path from the rows to the copy.
  for(let n:HTMLElement=c;n!==copy;n=n.parentElement!){
    if(!p.first)while(n.previousElementSibling)n.previousElementSibling.remove();
    if(!last)while(n.nextElementSibling)n.nextElementSibling.remove();
  }
  if(p.postOnly)c.remove();
  return copy;
}
/** The zoom a page's content is drawn at, and the width it is laid out at before zooming. */
function zoomOf(copy:HTMLElement,s:Print,box:ReturnType<typeof pageBox>,host:HTMLElement,room:number){
  if(s.scale==='custom')return s.scale_percent!/100;
  const t=TEXT[s.text_size];
  if(s.scale==='actual')return t;
  // Each layout is of a fresh copy: measuring fixes a copy's columns at that width.
  const at=(width:number,zoom=1)=>measure(copy.cloneNode(true) as HTMLElement,width,host,zoom).copy;
  const z=t*(box.width/t)/Math.max(box.width/t,at(box.width/t).scrollWidth);
  return s.scale==='fit_page'?fitZoom(z,room,k=>at(box.width/k,k).getBoundingClientRect().height):z;
}
/** The zoom, from `z` down, whose drawn height fits `room`: each try measures the height drawn at that zoom and shrinks by
 *  the overflow. A few tries settle it; anything still over goes on to a next page (the summary counts it). */
export function fitZoom(z:number,room:number,drawn:(zoom:number)=>number,tries=4):number{
  for(let i=0;i<tries;i++){const h=drawn(z);if(h<=room)break;z*=room/h;}
  return z;
}

/** The document: pages of every chunk, each a sheet with its title block (first page of a chunk), the rows, the key
 *  (every page), the signature line (last page) and the footer. */
export function pagesOf(sources:ExportSource[],s:Print):{doc:HTMLElement;count:number;clipped:boolean;tall:number}{
  const box=pageBox(s),host=el('div',{class:'export-offscreen export-png','aria-hidden':'true'});document.body.append(host);
  const printed=stamp(),inc=(k:Print['include'][number])=>s.include.includes(k);
  const doc=el('div',{class:'export-doc'+(s.colour==='greyscale'?' grey':'')});
  doc.style.setProperty('--pw',box.pw+'mm');doc.style.setProperty('--ph',box.ph+'mm');doc.style.setProperty('--pm',box.m+'mm');
  const sheets:{page:HTMLElement;foot:HTMLElement}[]=[];let clipped=false,tall=0;
  try{
    for(const src of sources){
      const head=inc('title')||inc('period')||inc('status')||inc('filters')?el('header',{class:'export-head'},
        el('div',{class:'export-head-main'},inc('title')?el('strong',{class:'export-title'},src.title):'',
          el('span',{},[inc('title')?src.department:'',inc('period')?src.period:''].filter(Boolean).join(' · ')),el('span',{class:'grow'}),
          inc('status')?el('span',{class:'export-status'+(src.draft?' draft':'')},src.draft?'Draft, not published':src.status):''),
        inc('filters')&&src.filters.length?el('div',{class:'export-filters'},'Filtered: '+src.filters.join(' · ')):''):null;
      const sign=inc('signature')?el('div',{class:'export-sign'},...['Checked by','Signature','Date'].map(t=>el('span',{},t))):null;
      if(src.data){
        // Week and Month: drawn from the view's data (one renderer); pages cut by measuring, never shrunk.
        host.style.setProperty('--pw',box.pw+'mm');host.style.setProperty('--ph',box.ph+'mm');host.style.setProperty('--pm',box.m+'mm');
        const by=inc('printed_by')?`Printed ${printed} by ${src.by}`:'';
        const frame=()=>el('section',{class:'export-page export-sheet'+(src.draft?' draft':'')},head?head.cloneNode(true):'',el('div',{'data-rows':''}),el('footer',{class:'export-foot'}));
        for(const page of printPages(src.data,frame,host,inc('key'),sign)){const foot=page.querySelector<HTMLElement>('footer')!;
          foot.append(el('span',{},by));doc.append(page);sheets.push({page,foot});}
        continue;
      }
      // The room for rows on a page: the page less its title block (first page), footer and signature line.
      host.replaceChildren(el('section',{class:'export-page export-sheet'},head||'',sign||'',el('footer',{class:'export-foot'},'Page')));
      const page0=host.firstElementChild as HTMLElement,foot=page0.querySelector('footer')!.getBoundingClientRect().height+8;
      const headH=head?head.getBoundingClientRect().height+10:0,signH=sign?sign.getBoundingClientRect().height+16:0;
      const room=box.height-foot-signH-4;  // a little slack for rounding between screen and print
      const copy=cleanCopy(src.view,s,!!src.prune);
      // The key repeats on every page below the rows, at the body's width and zoom; it leaves the copied view (an empty one is hidden).
      const legends=[...copy.querySelectorAll<HTMLElement>('.view-legend')];legends.forEach(n=>n.remove());
      const shown=legends.filter(n=>!n.hidden);
      const keyAt=(zoom:number)=>{if(!shown.length)return null;
        const k=el('div',{class:'export-key'},...shown.map(n=>n.cloneNode(true)));k.style.setProperty('width',box.width/zoom+'px');k.style.setProperty('zoom',String(zoom));return k;};
      const keyH=(zoom:number)=>{const k=keyAt(zoom);if(!k)return 0;host.replaceChildren(el('section',{class:'export-page export-sheet'},k));return k.getBoundingClientRect().height;};
      // Fit on one page draws at or below the text size, where the key is no shorter.
      const z=zoomOf(copy,s,box,host,room-headH-keyH(TEXT[s.text_size])),rows=room-keyH(z);
      // Heights in paper px (measured zoomed); scrollWidth is unzoomed (CSS Viewport: only client rects are scaled).
      const m=measure(copy,box.width/z,host,z);
      if(m.copy.scrollWidth*z>box.width+1)clipped=true;
      tall+=m.heights.filter(h=>h>rows).length;  // a row taller than a page continues on the next sheet
      const plans=m.container?plan(m,{first:rows-headH,next:rows},s.repeat_headers,
        // Each group, or each week of a month (its week bands; other views draw a week per chunk).
        i=>s.page_break==='group'?m.units[i].kind==='group':s.page_break==='week'&&m.units[i].owners.some(o=>o.matches('.mo-wk'))):[{rows:[],carry:-1,first:true}];
      plans.forEach((p,i)=>{
        const last=i===plans.length-1,body=el('div',{class:'export-body'},m.container?pageCopy(m,p,last,s.repeat_headers):m.copy.cloneNode(true));
        body.style.setProperty('width',box.width/z+'px');body.style.setProperty('zoom',String(z));
        const footer=el('footer',{class:'export-foot'});
        const page=el('section',{class:'export-page export-sheet'+(src.draft?' draft':'')},i===0&&head?head.cloneNode(true):'',body,keyAt(z)||'',last&&sign?sign.cloneNode(true):'',footer);
        footer.append(el('span',{},[i>0&&inc('title')?`${src.title} · ${src.period}`:'',inc('printed_by')?`Printed ${printed} by ${src.by}`:''].filter(Boolean).join(' · ')));
        sheets.push({page,foot:footer});doc.append(page);
      });
    }
  }finally{host.remove();}
  sheets.forEach(({foot},i)=>{if(inc('page_numbers'))foot.append(el('span',{class:'grow'}),el('span',{},`Page ${i+1} of ${sheets.length}`));});
  return {doc,count:sheets.length,clipped,tall};
}

// ---- Output ----
let printing:(()=>void)|null=null;
/** PDF: the pages alone, each on a sheet of the chosen paper (no browser margins), light theme, then the print window. */
function printPdf(doc:HTMLElement,s:Print){
  printing?.();  // an earlier export whose print window never reported back
  const root=document.documentElement,theme=root.getAttribute('data-theme'),box=pageBox(s);
  root.setAttribute('data-theme','light');
  // The page size of this print alone (CSP allows constructed sheets; an inline <style> would be refused).
  const page=new CSSStyleSheet();page.replaceSync(`@page{size:${box.pw}mm ${box.ph}mm;margin:0}`);
  document.adoptedStyleSheets=[...document.adoptedStyleSheets,page];
  const host=el('div',{id:'export-print'},doc);document.body.append(host);document.body.classList.add('exporting');
  const done=printing=()=>{printing=null;host.remove();document.body.classList.remove('exporting');document.adoptedStyleSheets=document.adoptedStyleSheets.filter(x=>x!==page);
    if(theme===null)root.removeAttribute('data-theme');else root.setAttribute('data-theme',theme);};
  window.addEventListener('afterprint',done,{once:true});
  try{window.print();}catch(error){done();throw error;}
}
/** PNG: one page (`index`) at `dpi`, its paper's size in pixels whatever the screen, drawn by the feedback capture's
 *  renderer (app styles, fonts inlined). Only that page's picture is held; a document of several pages is saved a page at
 *  a time (the PDF holds them all). A canvas the browser refuses, or a page it leaves blank, is said in words; nothing is
 *  scaled down silently. */
export async function pagePng(doc:HTMLElement,index:number,dpi:number,stop:()=>boolean=()=>false):Promise<Blob|null>{
  const host=el('div',{class:'export-offscreen export-png','aria-hidden':'true'},doc);document.body.append(host);
  try{
    const page=doc.querySelectorAll<HTMLElement>(':scope>.export-page')[index];if(!page)throw new Error('That page is not in the export any more. Choose the page again.');
    if(stop())return null;
    const {width,height}=page.getBoundingClientRect(),shell=doc.cloneNode(false) as HTMLElement;shell.append(page.cloneNode(true));
    const sheet=await render({root:el('div',{class:'export-png'},shell),width,height,theme:'light',rootStyle:'',rootClass:'',copied:0,masked:0,ms:0},dpi/96);
    try{
      if(stop())return null;
      // The page's margin is paper white: a transparent corner means the browser drew nothing.
      if(sheet.getContext('2d')?.getImageData(1,1,1,1).data[3]!==255)throw new Error(TOO_LARGE);
      return await new Promise<Blob>((ok,no)=>{try{sheet.toBlob(b=>b?ok(b):no(new Error(TOO_LARGE)),'image/png');}
        catch{no(new Error('This browser does not allow saving the image. Save a PDF instead.'));}});
    }finally{sheet.width=sheet.height=0;}
  }finally{host.remove();}
}
/** One CSV field as a spreadsheet reads it (RFC 4180 quoting; OWASP CSV injection). Text a spreadsheet would run as a
 *  formula (starting = + - @, tab, carriage return or line feed, or the full-width ＝ ＋ － ＠) keeps a leading apostrophe
 *  and so stays text; a plain number (-5, 3.5) stays a number. A field holding a quote, comma, semicolon, tab or line
 *  break is quoted, so no locale's separator starts a new cell inside it. The words themselves are never changed. */
export function csvCell(v:string):string{
  const text=/^-?\d+(?:\.\d+)?$/.test(v)||!/^[=+\-@\t\r\n\uFF1D\uFF0B\uFF0D\uFF20]/.test(v)?v:"'"+v;
  return /[",;\t\r\n]/.test(text)?`"${text.replace(/"/g,'""')}"`:text;
}
/** Rows of fields as CSV text: CRLF after every row (the file adds the BOM). */
export const csvText=(rows:string[][])=>rows.map(r=>r.map(csvCell).join(',')).join('\r\n')+'\r\n';
/** CSV of the view's table (List, Coverage): headers, then each row, as shown. */
export function csvOf(view:HTMLElement):string|null{
  const table=cleanCopy(view,normalise({include:[]})).querySelector('table');if(!table)return null;
  // A cell's words as read: hidden labels left out, parts apart, people in a list separated by "; ".
  // A team's rows are people too: one per "; ", without the "Sup" word.
  const text=(n:Element)=>{const c=n.cloneNode(true) as Element;c.querySelectorAll('.sr-only,.tm-sup-word').forEach(x=>x.remove());
    c.querySelectorAll('.tm-row+.tm-row').forEach(x=>x.before('; '));
    c.querySelectorAll('*').forEach(x=>x.after(x.classList.contains('pill-item')&&x.nextElementSibling?.classList.contains('pill-item')?'; ':' '));
    return (c.textContent||'').replace(/\s+/g,' ').replace(/ ;/g,';').trim();};
  // Columns without a visible heading (row actions) are not data.
  const named=[...table.rows[0]?.cells||[]].map(c=>!!text(c));
  return csvText([...table.rows].map(r=>[...r.cells].filter((_,i)=>named[i]!==false).map(text)));
}
const fileName=(name:string)=>name.replace(/[\\/:*?"<>|]+/g,'-');
export function download(blob:Blob,name:string){
  const a=el('a',{href:URL.createObjectURL(blob),download:fileName(name)}) as HTMLAnchorElement;
  document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(a.href),10_000);
}

// ---- The dialog ----
type View=SavedView<TableViewDefinition>&{is_personal_default?:boolean;is_department_default?:boolean};
const RANGES:[Print['range'],string][]=[['view','As on screen'],['week','This week'],['2weeks','2 weeks'],['month','This month'],['rota','This rota']];
const BREAKS:[Print['page_break'],string][]=[['none','None'],['group','Each group'],['week','Each week'],['month','Each month']];
const SCALES:[Print['scale'],string][]=[['fit_width','Fit width'],['fit_page','Fit on one page'],['actual','Actual size'],['custom','Custom']];
const PAPERS:[Paper,string][]=[['a4','A4'],['a3','A3'],['letter','Letter'],['legal','Legal']];

export function exportDialog(host:ExportHost){
  const opening=openingPrint(host.kept,session.last),fresh=!opening;
  let presets:View[]=[],defaults:TableViewDefaults|undefined,presetId=opening?.id||BUILT_INS[0].id;
  let s:Print={...(opening?.print||BUILT_INS[0].print)},sources:ExportSource[]=[],drawn='',serial=0,closed=false;
  const presetPrint=()=>BUILT_INS.find(b=>b.id===presetId)?.print||normalise(presets.find(v=>v.id===presetId)?.definition.print);
  const presetName=()=>BUILT_INS.find(b=>b.id===presetId)?.name||presets.find(v=>v.id===presetId)?.name||BUILT_INS[0].name;
  showDialog('Print or export','',(body,close,_tryClose,onClose)=>{
    body.classList.add('export-dialog');
    // A preset saved, deleted or brought back by Undo while the dialog is open shows in its menu.
    const saved=(e:Event)=>{if(String((e as CustomEvent).detail?.kind).startsWith('table_view.'))void reload().catch(()=>{});};
    window.addEventListener('rota-saved',saved);
    // The Schedule is put back once, as the dialog closes (by any way); a preview still drawing then is dropped.
    onClose(()=>{closed=true;serial++;window.removeEventListener('rota-saved',saved);host.restore();});
    const stage=el('div',{class:'export-preview',role:'img','aria-label':'Preview of the pages'});
    const form=el('div',{class:'export-form','data-lookup':''});  // settings are kept for the session: closing loses nothing
    const summary=el('span',{class:'export-summary',role:'status'});
    const go=withIcon(button('Print…',()=>void run(),'primary'),'printer');
    const presetButton=el('button',{type:'button',class:'button secondary export-preset','aria-haspopup':'menu'});
    // The open menu is kept so a reload (a preset saved, deleted or brought back by Undo meanwhile) redraws it in place.
    let menu:{list:HTMLElement;close:()=>void}|null=null;
    presetButton.addEventListener('click',()=>popover(presetButton,(panel,closeMenu)=>{
      menu={list:el('div',{class:'menu-list',role:'menu','aria-label':'Presets'},...presetItems(closeMenu)),close:closeMenu};panel.append(menu.list);},{label:'Presets',placement:'below'}));
    const redrawMenu=()=>{if(!menu?.list.isConnected)return;
      const key=(document.activeElement as HTMLElement|null)?.closest<HTMLElement>('[data-view-key]')?.dataset.viewKey,had=menu.list.contains(document.activeElement);
      menu.list.replaceChildren(...presetItems(menu.close));
      if(had)(menu.list.querySelector<HTMLElement>(`[data-view-key="${CSS.escape(key||'')}"]`)||menu.list.querySelector<HTMLElement>('[role^=menuitem]'))?.focus();};

    // One menu for presets, defaults included (savedViewItems: set as my / department default, follow the department's).
    const presetItems=(closeMenu:()=>void):HTMLElement[]=>savedViewItems<TableViewDefinition>({key:PRINT_KEY,noun:'print settings',thing:'preset',saved:presets,
      activeId:presetId.startsWith('builtin:')?'':presetId,close:closeMenu,shareable:host.canShare,
      current:()=>({schema_version:1,print:s}),dirty:d=>!samePrint(d.print||{},s),
      apply:v=>{if(v)use(v.id,normalise(v.definition.print));},done:(next,id)=>{presets=next as View[];if(id)presetId=id;else if(!presets.some(v=>v.id===presetId))presetId=BUILT_INS[0].id;sync();},
      builtins:BUILT_INS.map(b=>({label:b.name,on:presetId===b.id,apply:()=>use(b.id,b.print)}))});
    const use=(id:string,print:Print)=>{presetId=id;s={...print};build();void draw();};
    // shortcut: fetchViews (cached per session, cleared by any saved-view change) only fills the shared menu's default
    // choices, beside listPresets' fresh read; one read once table_view.ts can return a list's defaults with its views.
    // Reads answer in any order: only the latest one started is applied, so an older list never replaces a newer one.
    let reads=0;
    const reload=()=>{const mine=++reads;return Promise.all([listPresets(),fetchViews(PRINT_KEY)]).then(([r])=>{if(mine!==reads||closed)return;
      presets=r.views as View[];defaults=r.defaults;sync();redrawMenu();});};

    // Controls, rebuilt when a preset is chosen (each reads `s` and redraws the preview on change).
    // A new format changes which settings apply (CSV is data: no paper, look or inclusions), so the form is redrawn.
    const change=(next:Partial<Print>,redraw=true)=>{const format=next.format&&next.format!==s.format;s=normalise({...s,...next});if(format)build();else sync();if(redraw)void draw();};
    const again=()=>{drawn='';void draw();};  // the drawn view changed (Adjust what shows, People): draw it again
    const adjust:HTMLElement|null=host.adjust?withIcon(button('Adjust what shows…',()=>popover(adjust!,(panel,closeIt)=>host.adjust!(panel,closeIt,again),{label:'Adjust what shows',placement:'below'}),'secondary small'),'eye'):null;
    const sel=<K extends keyof Print>(label:string,key:K,options:[string,string][])=>{
      const f=select(label,options.map(([value,label])=>({value,label})),String(s[key]));
      f.input.addEventListener('change',()=>change({[key]:f.input.value} as Partial<Print>));return f.wrap;};
    const seg=<K extends keyof Print>(label:string,key:K,options:[string,string][])=>segment(label,options,String(s[key]),k=>change({[key]:k} as Partial<Print>));
    const section=(title:string,...children:(HTMLElement|HTMLElement[]|'')[])=>el('section',{class:'export-group','aria-label':title},el('h3',{class:'export-group-title'},title),...children.flat());
    function build(){
      const views=[['','Current view'],...host.views.map(v=>[v.id,v.name])] as [string,string][];
      const what=select('What',views.map(([value,label])=>({value,label})),s.view_id||'');
      what.input.addEventListener('change',()=>change({view_id:what.input.value||null}));
      const pct=field('Zoom (%)','number',s.scale_percent??100,{min:25,max:200,step:5});pct.wrap.hidden=s.scale!=='custom';
      pct.input.addEventListener('change',()=>{if(pct.input.reportValidity())change({scale_percent:Number(pct.input.value)});});
      const scale=sel('Scale','scale',SCALES);scale.querySelector('select')!.addEventListener('change',()=>{pct.wrap.hidden=s.scale!=='custom';});
      const includes=INCLUDES.map(([k,label])=>{const c=check(label,s.include.includes(k));
        c.input.addEventListener('change',()=>change({include:c.input.checked?[...s.include,k]:s.include.filter(x=>x!==k)}));return c.wrap;});
      const repeat=check('Repeat column headers on every page',s.repeat_headers);repeat.input.addEventListener('change',()=>change({repeat_headers:repeat.input.checked}));
      const csv=s.format==='csv',people=host.people;
      form.replaceChildren(
        el('div',{class:'export-preset-row'},el('span',{class:'dt-pop-label'},'Preset'),presetButton),
        section('Content',...seg('Format','format',[['print','Print / PDF'],['png','Image (PNG)'],['csv','Data (CSV)']]),what.wrap,sel('Range','range',RANGES.filter(([k])=>k!=='rota'||host.rota)),
          people?(()=>{const f=select('People on each duty',people.options.map(([value,label])=>({value,label})),people.value());f.input.addEventListener('change',()=>{people.set(f.input.value);again();});return f.wrap;})():'',
          csv?'':sel('Page break','page_break',BREAKS),adjust||''),
        csv?'':section('Page',el('div',{class:'export-pair'},sel('Paper','paper',PAPERS),sel('Orientation','orientation',[['landscape','Landscape'],['portrait','Portrait']])),
          el('div',{class:'export-pair'},scale,pct.wrap),...seg('Margins','margins',[['narrow','Narrow'],['normal','Normal'],['wide','Wide']]),
          ...(s.format==='png'?[...segment('Image quality',Object.entries(PRINT_IMAGE_DPI),String(dpiOf(s)),k=>change({image_dpi:Number(k) as Print['image_dpi']},false)),pageSlot]:[])),
        csv?'':section('Appearance',...seg('Text size','text_size',[['small','Small'],['normal','Normal'],['large','Large']]),...seg('Colour','colour',[['colour','Colour'],['greyscale','Greyscale']])),
        csv?'':section('Include',el('div',{class:'export-checks'},...includes),repeat.wrap));
      sync();
    }
    /** The preset button (name, and "changed" when the settings differ from it), the action and the summary. */
    function sync(){
      const changed=!samePrint(presetPrint(),s);
      presetButton.replaceChildren(el('span',{class:'truncate'},presetName()),changed?el('span',{class:'badge'},'Changed'):'');
      presetButton.setAttribute('aria-label',`Preset: ${presetName()}${changed?', changed':''}`);
      go.lastChild!.textContent=s.format==='print'?'Print…':s.format==='png'?'Save PNG':'Save CSV';
      session.last={id:presetId,print:s};
      // An image is one page: its exact pixels, and which page of how many (the PDF holds every page).
      if(s.format==='png'&&pages){const px=pagePixels(s,dpiOf(s));
        summary.textContent=(pages<2?'1 page':pick==='all'?`${pages} pages`:`Page ${pick} of ${pages}`)+` · ${px.width} × ${px.height} px`+notes;}
    }
    /** The preview: the view and range drawn (when they changed), then paged. */
    let pages=0,notes='';  // the preview's page count and its warnings, for the summary
    // PNG of several pages: the page to save (1-based), or every page as one ZIP, chosen beside the image quality.
    let pick:number|'all'=1;const pageSlot=el('div',{class:'export-page-pick'});
    const pickPage=()=>{if(pick!=='all')pick=Math.min(Math.max(1,pick),Math.max(1,pages));
      if(pages<2){pageSlot.replaceChildren();return;}
      const f=select('Page to save',[...Array.from({length:pages},(_,i)=>({value:String(i+1),label:`Page ${i+1} of ${pages}`})),{value:'all',label:'All pages (.zip)'}],String(pick));
      f.input.addEventListener('change',()=>{pick=f.input.value==='all'?'all':Number(f.input.value);sync();});pageSlot.replaceChildren(f.wrap);};
    // Save is offered when the preview is drawn and no save is running (a preview finishing mid-save never re-offers it).
    let ready=false,saving=false;const able=()=>{go.disabled=saving||!ready;};
    async function draw(){
      if(closed)return;
      const table=s.format==='csv',mine=++serial,chunk=!table&&(s.page_break==='week'||s.page_break==='month')?s.page_break:null,want=JSON.stringify([s.view_id,s.range,chunk,table]);
      try{
        if(want!==drawn){
          stage.replaceChildren(el('p',{class:'muted export-wait'},'Drawing…'));ready=false;able();
          const next=await host.draw({view_id:s.view_id,range:s.range,chunk,table});
          if(mine!==serial)return;sources=next;drawn=want;
        }
        if(s.format==='csv'){
          const csv=csvOf(sources[0].view);ready=!!csv;able();
          const lines=csv?csv.split('\r\n').slice(0,-1):[];pages=0;
          stage.replaceChildren(csv?el('pre',{class:'export-csv'},lines.slice(0,40).join('\n')+(lines.length>40?`\n… ${lines.length-40} more rows`:'')):el('p',{class:'muted export-wait'},'Nothing to list for these days and filters.'));
          summary.textContent=csv?`${lines.length-1} rows · columns as in the List`:'';return;
        }
        const {doc,count:n,clipped,tall}=pagesOf(sources,s);
        doc.setAttribute('aria-hidden','true');doc.inert=true;  // a picture: its copied labels and buttons are not the dialog's
        stage.replaceChildren(doc);
        doc.style.setProperty('zoom',String(Math.min(1,(stage.clientWidth-32)/doc.offsetWidth)));
        notes=(clipped?' · wider than the page: choose Fit width':'')+(tall?` · ${tall} ${tall===1?'row is':'rows are'} taller than a page and continue on the next`:'');
        if(n!==pages){pages=n;pickPage();}
        summary.textContent=`${n} page${n===1?'':'s'} · ${s.paper.toUpperCase()} ${s.orientation}`+notes;sync();
        ready=true;able();
      }catch(error){if(mine===serial){stage.replaceChildren(el('p',{class:'muted export-wait'},(error as Error).message));ready=false;able();}}
    }
    // One save at a time, of the settings and drawn view as they were when Save was chosen (later changes only redraw
    // the preview). An image still drawing when the dialog closes is dropped, never downloaded afterwards.
    const run=async()=>{
      if(saving||go.disabled)return;saving=true;able();
      const print={...s},from=sources,page=pick,count=pages;
      host.keep?.(print);
      try{
        // A draft's file says so in its name, as its page says "Draft, not published"; the CSV's columns stay as they are.
        const src=from[0],name=`${src.title} ${src.period}${src.draft?' (draft, not published)':''}`;
        if(print.format==='csv'){const csv=csvOf(src.view);if(csv)download(new Blob(['﻿'+csv],{type:'text/csv'}),name+'.csv');}
        else{const {doc}=pagesOf(from,print);if(print.format==='print')printPdf(doc,print);
          else if(page==='all'&&count>1){
            // Every page, one at a time (only one picture is held while drawing), then one ZIP of them.
            const files:{name:string;data:Uint8Array}[]=[];
            for(let i=0;i<count;i++){go.lastChild!.textContent=`Saving ${i+1} of ${count}…`;
              const png=await pagePng(doc,i,dpiOf(print),()=>closed);if(!png||closed)return;
              files.push({name:fileName(`${name} page ${String(i+1).padStart(String(count).length,'0')}.png`),data:new Uint8Array(await png.arrayBuffer())});}
            download(new Blob([zipStore(files)],{type:'application/zip'}),name+'.zip');}
          else{const at=page==='all'?1:page;go.lastChild!.textContent='Saving…';const png=await pagePng(doc,at-1,dpiOf(print),()=>closed);if(!png||closed)return;
            download(png,name+(count>1?` page ${at}`:'')+'.png');
            // Several pages: the dialog stays for the next one.
            if(count>1){notify(`Page ${at} of ${count} saved.`,'success');if(at===pick&&pick<pages){pick++;pickPage();}return;}}}
        close();
      }catch(error){notify((error as Error).message,'error',undefined,error);}
      finally{saving=false;if(!closed){able();sync();}}
    };
    body.append(el('div',{class:'export-split'},stage,form),el('div',{class:'actions export-actions'},
      host.publishedSheet?button('Published sheet…',()=>{close();host.publishedSheet!();},'ghost'):'',summary,el('span',{class:'grow'}),button('Cancel',close,'secondary'),go));
    build();
    requestAnimationFrame(()=>void draw());
    // Saved presets and defaults: a default applies only when this session has not chosen settings yet.
    void reload().then(()=>{const start=startingPreset(presets,defaults);if(fresh&&!closed&&start.id!==presetId)use(start.id,start.print);})
      .catch(()=>{/* built-ins still work */});
  });
}
