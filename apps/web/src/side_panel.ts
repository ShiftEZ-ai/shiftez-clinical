import {el,icon,guard,panelHighlight,type Child} from './dom.js';

/** Width a panel may take reviewed: at least 360px, at most 60% of the window, and never so wide
 *  that the content beside it has less than 360px. */
const MIN=360,KEEP=360,STEP=16,SHARE=0.6;
const PREFIX='rota.panel.',storeKey=(key:string)=>PREFIX+key;
/** Forget every panel's width (sign-out). */
export function forgetPanelWidths(){try{for(const k of Object.keys(localStorage))if(k.startsWith(PREFIX))localStorage.removeItem(k);}catch{/* nothing kept */}}
const remembered=(key:string)=>{try{return Number(localStorage.getItem(storeKey(key)))||0;}catch{return 0;/* private mode */}};
const remember=(key:string,w:number|null)=>{try{if(w===null)localStorage.removeItem(storeKey(key));else localStorage.setItem(storeKey(key),String(w));}catch{/* not kept */}};
/** The width actually used: within min/max and leaving room for what it sits beside. */
export const clampWidth=(w:number,min:number,max:number,room=Infinity)=>Math.round(Math.max(min,Math.min(w,max,room-KEEP)));

/**
 * A docked side panel (the session panel, Why, Auto-schedule, rules, a line editor…): it sits beside the content, which
 * reflows (never floats over it), has no coloured edge, and is resizable from a grip on its left edge — drag, double-click
 * to reset, ArrowLeft/ArrowRight (Shift: bigger steps) when the grip has focus. The width is kept per panel `key` in this
 * browser. Phones and narrow screens stack it in the flow at full width (no grip). `close` makes Esc close it.
 */
export function sidePanel<T extends HTMLElement>(aside:T,o:{key:string;width?:number;close?:()=>void;
  /** A page's own panel (the Schedule's session, Auto-schedule or rules panel): how to close it when a docked panel
   *  replaces it (see the one slot below). */dismiss?:()=>void;dock?:boolean}):T{
  const base=o.width||384,min=MIN;
  aside.classList.add('side-panel');
  const room=()=>aside.parentElement?.getBoundingClientRect().width||innerWidth;
  // The widest it may be now: 60% of the window, leaving the content beside it its room.
  const max=()=>clampWidth(Infinity,min,innerWidth*SHARE,room());
  // The panel's own default width (kept as it was), or the width this person last chose (within today's limits).
  const kept=remembered(o.key);let width=kept?clampWidth(kept,min,max()):base;
  const grip=el('div',{class:'panel-grip',role:'separator',tabindex:0,'aria-orientation':'vertical','aria-label':'Resize panel',
    'aria-valuemin':String(min),'data-tip':'Drag to resize · double-click to reset'});
  // A person's resize stays within 360px … 60% of the window; the default (and a reset) is the panel's own width. The
  // page beside keeps its 360px either way (CSS max-width on the docked panel).
  const set=(w:number,keep=true)=>{width=w===base?Math.round(Math.min(w,room()-KEEP)):clampWidth(w,min,max());aside.style.setProperty('--panel-w',width+'px');grip.setAttribute('aria-valuemax',String(max()));
    grip.setAttribute('aria-valuenow',String(width));grip.setAttribute('aria-valuetext',`${width} pixels wide`);if(keep)remember(o.key,width===base?null:width);};
  aside.style.setProperty('--panel-w',width+'px');grip.setAttribute('aria-valuenow',String(width));grip.setAttribute('aria-valuemax',String(max()));
  grip.addEventListener('pointerdown',e=>{
    if(e.button!==0)return;e.preventDefault();grip.setPointerCapture(e.pointerId);
    const x=e.clientX,start=aside.getBoundingClientRect().width;aside.classList.add('resizing');
    // The grip is on the left edge: dragging left widens the panel.
    const move=(m:PointerEvent)=>set(start+(x-m.clientX),false);
    const up=()=>{grip.removeEventListener('pointermove',move);grip.removeEventListener('pointerup',up);grip.removeEventListener('pointercancel',up);aside.classList.remove('resizing');set(width);};
    grip.addEventListener('pointermove',move);grip.addEventListener('pointerup',up);grip.addEventListener('pointercancel',up);});
  grip.addEventListener('dblclick',()=>set(base));
  grip.addEventListener('keydown',e=>{const d=e.shiftKey?STEP*4:STEP;width=Math.round(aside.getBoundingClientRect().width)||width;
    if(e.key==='ArrowLeft'){e.preventDefault();set(width+d);}else if(e.key==='ArrowRight'){e.preventDefault();set(width-d);}
    else if(e.key==='Home'){e.preventDefault();set(min);}else if(e.key==='End'){e.preventDefault();set(max());}});
  aside.prepend(grip);
  if(!o.dock)pagePanel(aside,o.key,o.dismiss);
  if(o.close)aside.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.defaultPrevented){e.preventDefault();e.stopPropagation();o.close!();}});
  return aside;
}

/** Previous / next through the list the panel was opened from ("Line 3 of 20"). */
/** `prefix`: words before "Line 2 of 5" (e.g. the rule's name), kept when the list redraws the count. */
export type PanelPosition={index:number;total:number;noun:string;go:(index:number)=>void;prefix?:string;
  /** The item's own number when it is not its place in the list (a rule's line keeps its stored number). */number?:number};
/**
 * A side panel's header: it names exactly what is open (and where it is in its list), with icon buttons — ↑ ↓ for the
 * previous/next item when there is a list, × to close (Esc) — each with a tooltip. Pair with `panelHighlight` (dom.ts)
 * so the opened item is outlined in its list.
 */
export function panelHead(o:{title:Child;sub?:Child;lead?:Child;position?:PanelPosition;close:()=>void;id?:string;
  /** Opened from another panel: "‹ Back to <it>" goes back to it. */back?:{label:string;go:()=>void}}):HTMLElement{
  const id=o.id||'side-title-'+Math.random().toString(36).slice(2,8);
  const btn=(label:string,tip:string,glyph:'chevronUp'|'chevronDown'|'close',run:()=>void)=>{
    const b=el('button',{type:'button',class:'icon-button bare','aria-label':label,'data-tip':tip},icon(glyph));b.addEventListener('click',run);return b;};
  let p=o.position;const noun=p?.noun||'',prefix=p?.prefix||'';
  const pos=p?el('small',{class:'side-pos'}):null;
  const arrows=p?[btn(`Previous ${noun}`,`Previous ${noun}`,'chevronUp',()=>p!.go(p!.index-1)),btn(`Next ${noun}`,`Next ${noun}`,'chevronDown',()=>p!.go(p!.index+1))]:[];
  // The list may be drawn again (a save, a live update): "N of M" and ↑ ↓ then follow the new list.
  const paint=(q:PanelPosition)=>{p=q;pos!.textContent=`${prefix}${noun.charAt(0).toUpperCase()+noun.slice(1)} ${q.number??q.index+1} of ${q.total}`;arrows[0].disabled=q.index<=0;arrows[1].disabled=q.index>=q.total-1;};
  if(p)paint(p);
  const back=o.back?backLink(o.back):null;
  const head=el('header',{class:'side-head'},o.lead||null,
    el('div',{class:'side-head-text'},back,pos,el('h2',{id,class:'truncate'},o.title),o.sub?el('small',{},o.sub):null),
    el('div',{class:'side-head-actions'},...arrows,btn('Close panel','Close · Esc','close',o.close)));
  if(p)repaint.set(head,paint);
  return head;
}
const repaint=new WeakMap<HTMLElement,(p:PanelPosition)=>void>();
const backLink=(b:{label:string;go:()=>void})=>{const n=el('button',{type:'button',class:'link-button side-back','data-tip':`Back to ${b.label}`},icon('chevronLeft'),el('span',{class:'truncate'},`Back to ${b.label}`));
  n.addEventListener('click',b.go);return n;};

/** What a docked item panel's build gets: its body, Close (at once, after a save), tryClose (through the unsaved-changes
 *  guard) and setHead (a title or subtitle known only once the item has loaded; `back`: a step inside the panel, e.g.
 *  Replace inside a session, offers "‹ Back to <it>"; null removes it) and clean (a step left without saving, e.g. Keep
 *  session after typing a cancel reason: nothing unsaved remains). */
export type Dock={panel:HTMLElement;body:HTMLElement;close:()=>void;tryClose:()=>void;setHead:(title:Child,sub?:Child,back?:{label:string;go:()=>void}|null)=>void;clean:()=>void};
export type DockOptions={key:string;title:Child;sub?:Child;className?:string;width?:number;
  position?:PanelPosition;mark?:string;onClose?:(leaving:boolean)=>void;build:(d:Dock)=>void;
  /** Drawn again with fresh data (a live update), not opened: focus stays where it is unless it was inside. */
  refresh?:boolean;
  /** The rota the panel edits: it closes when the page no longer shows that rota (dockOutlived, D-139). */
  rota?:string};
type Open={aside:HTMLElement;key:string;inner:HTMLElement;g:ReturnType<typeof guard>;opener:HTMLElement|null;mark?:string;onClose?:(leaving:boolean)=>void;o:DockOptions;h2:HTMLElement;outlived?:string};
let open:Open|null=null,pending:(()=>void)|null=null,sequence=0;
let focusReturn:{mark:string;fallback:HTMLElement;route:string}|null=null;
// A mark that is itself a control (a timeline bar) takes focus back; a row gives it to its first control.
const rowFocus=(row:Element|null)=>row?.matches('button,a[href],[tabindex]')?row as HTMLElement:row?.querySelector<HTMLElement>('button,a[href],[tabindex]');
/** A late panel read belongs to the slot generation in which it started, even if another panel opened and closed. */
export const dockRevision=()=>sequence;
/** A page's own panel opened by the person (the Schedule's session panel) takes the slot too: a late read started before
 *  it then never opens over it, nor after it closes. Redraws of an open panel do not call this. */
export const claimSlot=()=>{sequence++;};
const live=()=>open&&open.aside.isConnected?open:null;
/* One panel slot per page reviewed. A page's own panel (sidePanel: the Schedule's session,
 * Auto-schedule and rules panels) and the docked panel never show side by side:
 * - a docked panel opened from inside the panel in the slot takes its place, at its width, with "‹ Back to <it>": a page
 *   panel is only hidden (`covered`) and comes back; a docked one is kept on `stack` and drawn again;
 * - opened from anywhere else, it replaces it (a page panel is dismissed);
 * - a page panel shown while a docked one is open replaces that, after its unsaved-changes guard.
 * × and Esc close them all; Close after a save goes back to a covered page panel. */
type Return={name:string;row:string};
let covered:{key:string;label:string;back:boolean;dismiss?:()=>void}|null=null,stack:{o:DockOptions;title:string;focus:Return}[]=[];
let stopRefocus:(()=>void)|null=null;
/** The control that opened a panel from inside another, by what it says and its list row: Back draws the earlier panel
 *  anew, so its element is found again by these, not kept. */
const returnOf=(n:HTMLElement|null,mark?:string):Return=>n?.matches('button,a[href]')?{name:n.getAttribute('aria-label')||n.textContent?.trim()||'',row:n.closest('[data-row]')?.getAttribute('data-row')||''}
  :{name:'',row:mark&&document.querySelector(mark)?.getAttribute('data-row')||''};
function refocus(body:HTMLElement,to:Return,fallback:HTMLElement){
  const rowOf=(n:Element)=>n.closest('[data-row]')?.getAttribute('data-row')||'';
  // The same control again; for a row opened by a click (rows are not focusable), that row's first button or link.
  const find=()=>[...body.querySelectorAll<HTMLElement>(to.name?'button,a[href]':'button,a[href],input:not([type=hidden])')].find(n=>!(n as HTMLButtonElement).disabled&&rowOf(n)===to.row&&
    (!to.name||(n.getAttribute('aria-label')||n.textContent?.trim())===to.name));
  const found=find();if(found){found.focus();return;}
  fallback.focus();if(!to.name&&!to.row)return;
  // A tab that loads its content shows the control a moment later; focus moves there unless the person moved it.
  const stop=()=>{watch.disconnect();document.removeEventListener('focusin',moved);if(stopRefocus===stop)stopRefocus=null;};
  const moved=()=>{if(document.activeElement!==fallback)stop();};
  const watch=new MutationObserver(()=>{const n=find();if(!n)return;stop();if(document.activeElement===fallback)n.focus();});
  watch.observe(body,{childList:true,subtree:true});document.addEventListener('focusin',moved);stopRefocus=stop;
}
const dismissOf=new WeakMap<HTMLElement,(()=>void)|undefined>();
const slotLabel=(aside:HTMLElement)=>aside.getAttribute('aria-label')||aside.querySelector('h2')?.textContent?.trim()||'the panel';
const shown=(n:HTMLElement)=>n.isConnected&&!n.hidden&&!n.classList.contains('slot-covered')&&n.getClientRects().length>0;
const pageShown=()=>[...document.querySelectorAll<HTMLElement>('.side-panel:not(.dock-panel)')].find(shown);
function uncover(focus=false){
  const n=document.querySelector<HTMLElement>('.slot-covered');covered=null;
  document.querySelectorAll('.slot-covered').forEach(x=>x.classList.remove('slot-covered'));
  const h=n?.querySelector<HTMLElement>('h2');if(focus&&h){h.tabIndex=-1;h.focus();}
}
function pagePanel(aside:HTMLElement,key:string,dismiss?:()=>void){
  focusReturn=null;
  aside.dataset.slot=key;dismissOf.set(aside,dismiss);
  // The same panel drawn again (a live update) under a docked one stays under it.
  if(covered?.key===key&&covered.label===slotLabel(aside)){aside.classList.add('slot-covered');return;}
  const check=()=>{const cur=live();if(!cur||!shown(aside))return;
    if(!cur.g.dirty()){stack=[];finish(false);uncover();return;}
    aside.classList.add('slot-covered');covered={key,label:slotLabel(aside),back:false,dismiss};
    pending=()=>{stack=[];finish(false);uncover();};cur.g.tryClose();};
  // Drawn now: the page puts it in place in a moment. shortcut: polls up to ~2 s of frames for that; a page that
  // attaches its panel later is not caught (none does).
  let frames=120;const wait=()=>{if(aside.isConnected)check();else if(--frames>0)requestAnimationFrame(wait);};
  if(live())requestAnimationFrame(wait);
  // Shown later by un-hiding it (the Auto-schedule button).
  new MutationObserver(check).observe(aside,{attributes:true,attributeFilter:['hidden']});
}
/** The list row (dataTable `data-row`) a docked panel has open, as its `mark`. */
export const rowMark=(id:string)=>`[data-row="${CSS.escape(id)}"]`;
/** Outline the open item in its list; lists call this after each draw (`scope` may still be detached). `locate` (the
 *  list's own order) gives the open item's place in the list as now drawn, so its "N of M" and ↑ ↓ stay current. */
export function markOpen(scope:ParentNode=document,scroll=false,locate?:(mark:string)=>PanelPosition|undefined){
  // A linked panel can close while its list is still loading. Its mounted row gets focus unless the person moved it.
  const to=focusReturn;
  if(to){
    if(location.hash!==to.route||document.activeElement!==to.fallback)focusReturn=null;
    else{const row=scope.querySelector(to.mark);if(row?.isConnected){focusReturn=null;rowFocus(row)?.focus();}}
  }
  const cur=live(),m=cur?.mark;if(!m)return;const n=[...scope.querySelectorAll(m)];if(n.length)panelHighlight.highlight(n,scroll);
  const p=locate?.(m),head=cur.inner.querySelector<HTMLElement>(':scope>.side-head');if(p&&head)repaint.get(head)?.(p);}
/**
 * The item panel of a list (a person, a request, a rule, a tag): docked as the shell's last column beside the page,
 * which reflows (never a modal over it); a phone shows it full width as a sheet. One panel per page (see the slot
 * above): opening another item — a row, or ↑ ↓ through `position` — replaces it, after the unsaved-changes guard. It is
 * a labelled region named by its header ("Person 3 of 142"); focus moves into it and back to the opener on close; Esc
 * closes; `mark` (a selector) outlines the open item in its list. `onClose(leaving)`: leaving is true when navigating
 * away closed it.
 */
export function dockPanel(o:DockOptions){
  const cur=live(),from=document.activeElement,slot=cur?.aside||pageShown();
  // Opened from inside the panel in the slot (not ↑ ↓ or a redraw of the same one): it can go back there.
  const how=!o.refresh&&cur?.key!==o.key&&!!from&&!!slot?.contains(from)?'within':'replace';
  if(cur&&cur.g.dirty()){pending=()=>show(o,how);cur.g.tryClose();return;}
  show(o,how);
}
function show(o:DockOptions,how:'within'|'replace'|'back'='replace',to?:Return){
  stopRefocus?.();
  focusReturn=null;
  const cur=live(),active=document.activeElement as HTMLElement|null,within=!!cur&&!!active&&cur.aside.contains(active);
  // ↑ ↓ keep focus on the arrow pressed, so it can be pressed again.
  const step=within&&active?.closest('.side-head-actions')?active.getAttribute('aria-label'):null;
  const page=cur?null:pageShown(),width=cur&&cur.key!==o.key?cur.aside.getBoundingClientRect().width:page?.getBoundingClientRect().width;
  if(cur&&cur.key!==o.key&&how!=='back')stack=how==='within'?[...stack,{o:cur.o,title:cur.h2.textContent||'',focus:returnOf(active,o.mark)}]:[];
  if(page){const dismiss=dismissOf.get(page);
    if(how==='within'||!dismiss){page.classList.add('slot-covered');covered={key:page.dataset.slot||'',label:slotLabel(page),back:how==='within',dismiss};}
    else dismiss();}
  let aside:HTMLElement;
  if(cur&&cur.key===o.key){cur.g.dispose();cur.inner.remove();cur.onClose?.(false);aside=cur.aside;}
  else{
    if(cur)finish(false);
    aside=sidePanel(el('aside',{role:'region'}),{key:o.key,width:o.width||560,dock:true,close:()=>{pending=null;open?.g.tryClose();}});
    // It takes the slot at the width of the panel it replaces.
    if(width)aside.style.setProperty('--panel-w',Math.round(width)+'px');
    (document.querySelector('.shell')||document.body).append(aside);
  }
  const id='dock-title-'+(++sequence);
  aside.className=`side-panel dock-panel ${o.className||''}`.trim();aside.setAttribute('aria-labelledby',id);
  // × and Esc close every panel in the slot; Close after a save goes back to a covered page panel.
  const all=()=>{stack=[];const c=covered;covered=null;finish(true);if(c?.dismiss)c.dismiss();else if(c)uncover();};
  const close=()=>{if(covered?.back){stack=[];finish(false);uncover(true);}else all();},tryClose=()=>{pending=null;g.tryClose();};
  const prev=stack.at(-1);
  const back=prev?{label:prev.title,go:()=>{pending=()=>{const p=stack.pop()!;show(p.o,'back',p.focus);};g.tryClose();}}
    :covered?.back?{label:covered.label,go:()=>{pending=()=>{finish(false);uncover(true);};g.tryClose();}}:undefined;
  const head=panelHead({title:o.title,sub:o.sub,position:o.position,close:tryClose,id,back}),h2=head.querySelector<HTMLElement>('h2')!;h2.tabIndex=-1;
  const body=el('div',{class:'dialog-body dock-body'}),inner=el('div',{class:'dock-inner'},head,body);
  aside.append(inner);
  // Discard in the guard's bar: what was asked for next (another item, Back) goes ahead; otherwise the panels close.
  const g=guard(inner,()=>{const next=pending;pending=null;if(next){g.reset();next();}else all();});
  open={aside,key:o.key,inner,g,opener:within||(o.refresh&&cur?.key===o.key)?cur!.opener:active,mark:o.mark,onClose:o.onClose,o,h2};
  const setHead=(title:Child,sub?:Child,step?:{label:string;go:()=>void}|null)=>{h2.replaceChildren(...el('span',{},title).childNodes);const text=h2.parentElement!;
    // A step's Back stands in for the panel's own Back (which comes back when the step ends).
    if(step!==undefined){text.querySelector(':scope>.side-step')?.remove();const own=text.querySelector<HTMLElement>(':scope>.side-back');if(own)own.hidden=!!step;
      if(step){const b=backLink(step);b.classList.add('side-step');text.prepend(b);}}
    if(sub===undefined)return;
    const old=text.querySelector(':scope>small:not(.side-pos)'),next=el('small',{},sub);if(old)old.replaceWith(next);else text.append(next);};
  o.build({panel:aside,body,close,tryClose,setHead,clean:()=>g.reset()});
  markOpen(document,true);
  const arrow=step?head.querySelector<HTMLButtonElement>(`[aria-label="${CSS.escape(step)}"]`):null;
  if(o.refresh&&!within)return;
  const first=arrow&&!arrow.disabled?arrow:body.querySelector<HTMLElement>('[role=tab][aria-selected=true],input:not([type=hidden]):not(:disabled),select:not(:disabled),textarea:not(:disabled)')||h2;
  // Back returns to the control that opened the panel just left.
  if(to)refocus(body,to,first);else first.focus();
}
function finish(back:boolean,leaving=false){
  stopRefocus?.();
  const cur=open;if(!cur)return;open=null;pending=null;
  // Only a mark it drew is cleared (a session's mark finds no row in the week grid, which keeps its own highlight).
  cur.g.dispose();cur.aside.remove();if(cur.mark&&document.querySelector(cur.mark+'.is-explained'))panelHighlight.clear();cur.onClose?.(leaving);
  if(!back)return;
  // Back to the opener; when a save redrew it, to the open item's row.
  const row=cur.mark?document.querySelector(cur.mark):null;
  const opener=cur.opener?.isConnected&&cur.opener!==document.body?cur.opener:null,fallback=document.querySelector<HTMLElement>('#main');
  (opener||rowFocus(row)||fallback)?.focus();
  if(!opener&&!row&&cur.mark&&fallback&&document.activeElement===fallback){const to={mark:cur.mark,fallback,route:location.hash};focusReturn=to;
    // Its onClose may take the panel's own part off the address once the close settles (a rule line link): same route.
    queueMicrotask(()=>{to.route=location.hash;});}
}
// Esc from the page beside it (its list, or nothing focused) closes the panel too, unless something else took the key
// first (a menu, a selection) or it is typed in a field.
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('focusin',e=>{if(focusReturn&&e.target!==focusReturn.fallback)focusReturn=null;});
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('keydown',e=>{
  const cur=live(),t=e.target as HTMLElement|null;
  if(e.key!=='Escape'||e.defaultPrevented||!cur||t?.closest?.('input,textarea,select,[contenteditable=true]')||document.querySelector('dialog[open],.popover'))return;
  e.preventDefault();pending=null;cur.g.tryClose();});
/** Close the docked panel at once (after an action inside it, or when what it shows is gone); a page panel it covers
 *  comes back. */
export const closeDock=()=>{stack=[];const back=!!covered;finish(!back);if(back)uncover(true);};
/** Whether the docked panel holds unsaved changes (live updates wait). */
export const dockDirty=()=>!!live()?.g.dirty();
/** Navigation: a clean panel closes; one with unsaved changes asks first and the navigation waits for the answer.
 *  `then`: where the reader was going; Discard changes closes the panel and goes on (Keep editing stays, as before). */
export function dockLeave(then?:()=>void){focusReturn=null;const cur=live();if(!cur)return true;
  if(cur.g.dirty()){pending=then?()=>{stack=[];covered=null;finish(false,true);then();}:null;cur.g.tryClose();return false;}
  stack=[];covered=null;finish(false,true);return true;}
/** D-139: a panel bound to a rota never outlives it. Once the page shows another rota or none (`rota`), a clean one
 *  closes; one with unsaved changes asks with the same Keep editing / Discard changes guard (Discard closes it). */
export function dockOutlived(rota:string){const cur=live();if(!cur?.o.rota||cur.o.rota===rota)return;
  // Asked once per rota shown: Keep editing keeps the form; a redraw of that page does not ask again.
  if(cur.g.dirty()){if(cur.outlived!==rota){cur.outlived=rota;pending=null;cur.g.tryClose();}return;}
  stack=[];covered=null;finish(false,true);}
/** The shell is rebuilt on every page draw: the open panel moves into the new one, keeping its state; call the result
 *  once the new shell is in the page to give focus back. */
export function keepDock(shell:HTMLElement){const cur=live();if(!cur)return ()=>{};
  const had=cur.aside.contains(document.activeElement)?document.activeElement as HTMLElement:null;shell.append(cur.aside);return ()=>had?.focus({preventScroll:true});}
