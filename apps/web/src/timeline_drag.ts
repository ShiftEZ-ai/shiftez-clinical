import {el,icon,weekOrder,announce,departmentToday} from './dom.js';
import {dayNumber,dayOf,plusDays,plusMonths,spanWords,lengthWords,type Zoom} from './timeline.js';

/**
 * Planning on a Timeline by hand (approved mockup, 21 Oct 2026): drag a bar to move it, its right edge to resize it,
 * Alt-drag to copy it; drag a chip (a rotation type or a template, `data-drop`) from a palette onto a row to place it.
 * Dates snap to the zoom's unit (days at Week, weeks at Month and Quarter, months at Year). While dragging, a dashed
 * ghost shows where it lands with a tip: its dates, its length and what the page's preview says (a conflict in red with
 * its words, "goes for approval" in gold with a clock). Letting go applies it through the page (`apply`), which uses
 * its governed command; one that cannot apply is not applied and says why.
 * Keyboard, the same gestures: on a bar ← → move a week (Shift a month), Alt+← → resize, Ctrl+D copy; on a chip Enter
 * places it on the row last focused (↑ ↓ choose another row). Enter applies, Escape cancels.
 */
export type Gesture='create'|'move'|'resize'|'copy'|'template';
/** A chip's payload: what is placed and how long it is (its blocks' total for a template). */
export type Drop={kind:'type'|'template';id:string;label:string;length:[number,'weeks'|'months'];hue?:string;
  /** A template's blocks in order: its end advances block by block, as the server does (training.py _block_end). */
  blocks?:[number,'weeks'|'months'][]};
export type Change={action:Gesture;row:string;bar?:string;start:string;end:string;drop?:Drop};
export type Verdict={ok:boolean;lines:{text:string;tone:'error'|'warn'}[]};
export type DragSpec={movable:(bar:string,row:string)=>boolean;preview:(c:Change)=>Promise<Verdict>;apply:(c:Change)=>Promise<void>};

type Geometry={root:HTMLElement;span:[number,number];zoom:Zoom;spec:DragSpec;lastRow:string};
const timelines=new Set<Geometry>();
const weekday=(day:string)=>(new Date(day+'T00:00:00Z').getUTCDay()+6)%7;
/** The nearest unit boundary to a day. */
function snap(day:number,zoom:Zoom):number{
  if(zoom==='week')return day;
  const d=dayOf(day);
  if(zoom==='year'){const first=dayNumber(d.slice(0,7)+'-01'),next=dayNumber(plusMonths(d,1));return day-first<=next-day?first:next;}
  const back=(weekday(d)-weekOrder()[0]+7)%7;return back<=3?day-back:day-back+7;
}
/** A day moved by months, kept within the month it lands in (31 Jan + 1 month = 28 Feb). */
const byMonths=(day:string,n:number)=>{const first=plusMonths(day,n),last=Number(plusDays(plusMonths(first,1),-1).slice(8));return first.slice(0,8)+String(Math.min(Number(day.slice(8)),last)).padStart(2,'0');};
const add=(day:string,[n,unit]:[number,'weeks'|'months'])=>unit==='weeks'?plusDays(day,n*7):byMonths(day,n);
/** Where a placed chip ends: each block from where the last one ended (2 months then 1 week from 1 Feb: 8 Apr). */
export const dropEnd=(start:string,drop:Pick<Drop,'length'|'blocks'>)=>(drop.blocks||[drop.length]).reduce(add,start);

/** Called by timelineView for a timeline with a DragSpec: its rows' bars and the page's chips become draggable. */
export function enableDrag(root:HTMLElement,span:[number,number],zoom:Zoom,spec:DragSpec){
  const g:Geometry={root,span,zoom,spec,lastRow:''};
  for(const x of timelines)if(!x.root.isConnected)timelines.delete(x);timelines.add(g);
  root.addEventListener('focusin',e=>{const r=(e.target as Element).closest<HTMLElement>('[data-row]');if(r)g.lastRow=r.dataset.row!;});
  root.addEventListener('pointerdown',e=>{const bar=(e.target as Element).closest<HTMLElement>('.tl-bar[data-bar]');
    if(!bar||e.button!==0||e.pointerType==='touch')return;const row=bar.closest<HTMLElement>('[data-row]')!.dataset.row!;
    if(!spec.movable(bar.dataset.bar!,row))return;
    const r=bar.getBoundingClientRect(),action:Gesture=r.right-e.clientX<=8?'resize':e.altKey?'copy':'move';
    pointerDrag(e,g,{action,row,bar:bar.dataset.bar,start:bar.dataset.start!,end:bar.dataset.end!});});
  root.addEventListener('keydown',e=>{const bar=(e.target as Element).closest<HTMLElement>('.tl-bar[data-bar]');if(!bar||session)return;
    const row=bar.closest<HTMLElement>('[data-row]')!.dataset.row!;if(!spec.movable(bar.dataset.bar!,row))return;
    const base={row,bar:bar.dataset.bar,start:bar.dataset.start!,end:bar.dataset.end!};
    if(e.key==='d'&&(e.ctrlKey||e.metaKey)){e.preventDefault();keyboard(bar,g,{...base,action:'copy',start:base.end,end:plusDays(base.end,dayNumber(base.end)-dayNumber(base.start))});}
    else if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();const s=keyboard(bar,g,{...base,action:e.altKey?'resize':'move'});s.key(e);}});
}

/** The redraw after a change replaces the bar the keys were on, and draws its rows a frame later (timeline.ts fit): the
 *  focus follows the moved bar into the new board, unless the person has moved it meanwhile. The page's own redraw
 *  leaves focus on #main when it cannot find the old bar by its words (its dates changed): that is still owed. */
let owed:{id:string;was:Element|null}|null=null;
const barOf=(root:HTMLElement,id:string)=>root.querySelector<HTMLElement>(`.tl-bar[data-bar="${CSS.escape(id)}"]`);
const focusFree=(was:Element|null)=>{const a=document.activeElement;return !a||a===document.body||!a.isConnected||a===was||a.id==='main';};
function refocus(id:string,was:Element|null){
  const b=[...timelines].find(t=>t.root.isConnected)?.root;const bar=b&&barOf(b,id);
  if(bar&&bar!==was){if(focusFree(was))bar.focus();return;}
  owed={id,was};  // not drawn yet: the new board's first fit gives it focus
}
/** Called by timelineView once its rows are drawn: the bar owed focus gets it (one try, on the board that replaced it). */
export function focusOwed(root:HTMLElement){
  const o=owed;if(!o)return;const bar=barOf(root,o.id);if(bar===o.was)return;
  owed=null;if(bar&&focusFree(o.was))bar.focus();
}
// ---- One gesture at a time: its ghost, its tip and its verdict ----
type Session={g:Geometry;c:Change;ghost:HTMLElement;tip:HTMLElement;verdict:Promise<Verdict>|null;asked:string;key:(e:KeyboardEvent)=>void;end:(apply:boolean)=>Promise<void>};
let session:Session|null=null;

function begin(g:Geometry,c:Change):Session{
  const ghost=el('div',{class:'tl-bar tl-ghost','aria-hidden':'true'}),tip=el('div',{class:'tl-drag-tip',role:'status'});
  let timer=0,seq=0;
  const s:Session={g,c,ghost,tip,verdict:null,asked:'',key:()=>{},end:async apply=>{
    window.clearTimeout(timer);
    if(!apply){session=null;ghost.remove();tip.remove();return;}
    // The gesture holds the board until its change is applied and the board redrawn: a gesture started meanwhile
    // would be drawn on the board the refresh replaces, and lose its ghost and its keys.
    const v=await ask();
    if(!v.ok){session=null;paintTip(v);announce(v.lines.map(l=>l.text).join('. ')||'Not applied');window.setTimeout(()=>{ghost.remove();tip.remove();},2600);return;}
    const was=document.activeElement;
    try{await g.spec.apply(s.c);}finally{if(session===s)session=null;ghost.remove();tip.remove();if(s.c.bar)refocus(s.c.bar,was);}}};
  // Asked once per landing place (debounced while moving); the last answer wins.
  const ask=()=>{const key=JSON.stringify(s.c);if(s.asked!==key||!s.verdict){s.asked=key;const n=++seq;
    s.verdict=g.spec.preview(s.c).catch(e=>({ok:false,lines:[{text:(e as Error).message,tone:'error' as const}]}));
    void s.verdict.then(v=>{if(n===seq&&session===s)paintTip(v);});}return s.verdict;};
  const paintTip=(v?:Verdict)=>{
    tip.replaceChildren(el('b',{},spanWords(s.c.start,s.c.end)),` · ${lengthWords(s.c.start,s.c.end)}`,
      ...(v?v.lines.map(l=>el('div',{class:'tl-gov '+l.tone},icon(l.tone==='error'?'alert':'clock'),l.text)):[el('div',{class:'tl-gov'},'Checking…')]));
    tip.classList.toggle('blocked',!!v&&!v.ok);ghost.classList.toggle('blocked',!!v&&!v.ok);};
  s.key=()=>{};
  // Draw where the change lands, on its row (drawn rows only: a row scrolled away keeps its ghost hidden).
  (s as Session&{paint:()=>void}).paint=()=>{
    const lane=g.root.querySelector<HTMLElement>(`[data-row="${CSS.escape(s.c.row)}"] .tl-lane`);if(!lane){ghost.remove();tip.remove();return;}
    const total=g.span[1]-g.span[0],l=(dayNumber(s.c.start)-g.span[0])/total*100,w=(dayNumber(s.c.end)-dayNumber(s.c.start))/total*100;
    ghost.style.setProperty('--l',l.toFixed(3));ghost.style.setProperty('--w',Math.max(w,0.4).toFixed(3));
    const own=s.c.bar&&s.c.action!=='copy'?lane.querySelector<HTMLElement>(`[data-bar="${CSS.escape(s.c.bar)}"]`):null;
    ghost.style.setProperty('--t',own?.style.getPropertyValue('--t')||lane.dataset.main||'7');
    ghost.replaceChildren(el('span',{class:'tl-lbl'},s.c.drop?.label||''));
    if(s.c.drop?.hue)ghost.className=`tl-bar tl-ghost duty-${s.c.drop.hue}`;
    tip.style.setProperty('--l',Math.min(l+w,100).toFixed(3));
    if(ghost.parentElement!==lane)lane.append(ghost,tip);
    paintTip();window.clearTimeout(timer);timer=window.setTimeout(()=>void ask(),220);};
  session=s;return s;
}
const paint=(s:Session)=>(s as Session&{paint:()=>void}).paint();

/** The day under the pointer on a row, or null off every timeline row. */
function at(x:number,y:number):{g:Geometry;row:string;day:number}|null{
  const n=document.elementFromPoint(x,y),row=n?.closest<HTMLElement>('.tl-row[data-row]'),lane=row?.querySelector<HTMLElement>('.tl-lane');if(!row||!lane)return null;
  const g=[...timelines].find(t=>t.root.contains(row));if(!g)return null;
  const r=lane.getBoundingClientRect();if(x<r.left)return null;
  return {g,row:row.dataset.row!,day:g.span[0]+Math.floor((x-r.left)/r.width*(g.span[1]-g.span[0]))};
}

function pointerDrag(e:PointerEvent,g:Geometry,c:Change){
  const from=at(e.clientX,e.clientY);if(!from)return;
  const x0=e.clientX,y0=e.clientY,len=dayNumber(c.end)-dayNumber(c.start);let s:Session|null=null;
  const move=(m:PointerEvent)=>{
    if(!s){if(Math.hypot(m.clientX-x0,m.clientY-y0)<5)return;s=begin(g,{...c});document.body.classList.add('tl-dragging');}
    const p=at(m.clientX,m.clientY);if(!p||p.g!==g)return;
    const delta=p.day-from.day;
    if(c.action==='resize'){const end=Math.max(snap(dayNumber(c.end)+delta,g.zoom),dayNumber(c.start)+1);s.c={...c,end:dayOf(end)};}
    else{const start=snap(dayNumber(c.start)+delta,g.zoom);s.c={...c,start:dayOf(start),end:dayOf(start+len)};}
    paint(s);};
  // Every way out (let go, Escape) removes all three listeners, so a cancelled drag keeps nothing of the board alive.
  const stop=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);document.removeEventListener('keydown',esc,true);
    document.body.classList.remove('tl-dragging');};
  const up=(u:PointerEvent)=>{stop();if(!s)return;
    // The click that ends a drag does not open the bar.
    document.addEventListener('click',x=>{x.stopPropagation();x.preventDefault();},{capture:true,once:true});
    void s.end(!!at(u.clientX,u.clientY));};
  const esc=(k:KeyboardEvent)=>{if(k.key==='Escape'&&s){k.preventDefault();k.stopPropagation();stop();void s.end(false);s=null;}};
  document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);document.addEventListener('keydown',esc,true);
}

/** A keyboard gesture from a bar or a chip: arrows move it, Enter applies, Escape or leaving cancels. */
function keyboard(from:HTMLElement,g:Geometry,c:Change):Session{
  const s=begin(g,c);paint(s);
  const step=(e:KeyboardEvent,dir:1|-1)=>{const unit=e.shiftKey?'months':'weeks',by=(day:string)=>unit==='weeks'?plusDays(day,7*dir):byMonths(day,dir);
    if(s.c.action==='resize'){const end=by(s.c.end);if(end>s.c.start)s.c={...s.c,end};}
    else{const start=by(s.c.start);s.c={...s.c,start,end:plusDays(start,dayNumber(s.c.end)-dayNumber(s.c.start))};}};
  const rows=()=>[...g.root.querySelectorAll<HTMLElement>('.tl-row[data-row]')].map(r=>r.dataset.row!);
  s.key=e=>{
    if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();step(e,e.key==='ArrowLeft'?-1:1);paint(s);
      announce(`${spanWords(s.c.start,s.c.end)}. Enter applies, Escape cancels.`);}
    else if((e.key==='ArrowUp'||e.key==='ArrowDown')&&s.c.drop){e.preventDefault();const list=rows(),i=list.indexOf(s.c.row),next=list[i+(e.key==='ArrowUp'?-1:1)];if(next){s.c={...s.c,row:next};paint(s);}}
    else if(e.key==='Enter'){e.preventDefault();stop();void s.end(true);}
    else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();stop();void s.end(false);announce('Cancelled');}};
  const keys=(e:KeyboardEvent)=>s.key(e),blur=()=>{stop();if(session===s)void s.end(false);};
  const stop=()=>{from.removeEventListener('keydown',keys);from.removeEventListener('blur',blur);};
  // The first key is the bar's own (it started the gesture); later ones come here.
  queueMicrotask(()=>{from.addEventListener('keydown',keys);from.addEventListener('blur',blur);});
  return s;
}

// ---- Chips (a palette's rotation types and templates) ----
const dropOf=(n:HTMLElement)=>JSON.parse(n.dataset.drop!) as Drop;
if(typeof document!=='undefined'){
  document.addEventListener('pointerdown',e=>{const chip=(e.target as Element)?.closest?.<HTMLElement>('[data-drop]');if(!chip||e.button!==0||e.pointerType==='touch')return;
    const drop=dropOf(chip);let s:Session|null=null;const x0=e.clientX,y0=e.clientY;
    const move=(m:PointerEvent)=>{if(!s&&Math.hypot(m.clientX-x0,m.clientY-y0)<5)return;chip.classList.add('dragging');document.body.classList.add('tl-dragging');
      const p=at(m.clientX,m.clientY);if(!p){if(s){s.ghost.remove();s.tip.remove();}return;}
      const start=dayOf(snap(p.day,p.g.zoom)),c:Change={action:drop.kind==='template'?'template':'create',row:p.row,start,end:dropEnd(start,drop),drop};
      if(!s||s.g!==p.g){s?.ghost.remove();s?.tip.remove();s=begin(p.g,c);}else s.c=c;paint(s);};
    const stop=()=>{document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);document.removeEventListener('keydown',esc,true);
      chip.classList.remove('dragging');document.body.classList.remove('tl-dragging');};
    const up=(u:PointerEvent)=>{stop();if(s)void s.end(!!at(u.clientX,u.clientY));};
    // Escape cancels a chip drag as it does a bar's: nothing is placed when the button is let go.
    const esc=(k:KeyboardEvent)=>{if(k.key!=='Escape'||!s)return;k.preventDefault();k.stopPropagation();stop();void s.end(false);s=null;announce('Cancelled');};
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);document.addEventListener('keydown',esc,true);});
  document.addEventListener('keydown',e=>{const chip=(e.target as Element)?.closest?.<HTMLElement>('[data-drop]');if(!chip||e.key!=='Enter'||session||e.defaultPrevented)return;
    const g=[...timelines].find(t=>t.root.isConnected);if(!g)return;e.preventDefault();
    const row=g.lastRow&&g.root.querySelector(`[data-row="${CSS.escape(g.lastRow)}"]`)?g.lastRow:g.root.querySelector<HTMLElement>('.tl-row[data-row]')?.dataset.row;if(!row)return;
    const drop=dropOf(chip),today=dayNumber(departmentToday()),start=dayOf(snap(Math.min(Math.max(today,g.span[0]),g.span[1]-1),g.zoom));
    keyboard(chip,g,{action:drop.kind==='template'?'template':'create',row,start,end:dropEnd(start,drop),drop});
    announce(`${drop.label} on the focused row, ${spanWords(start,dropEnd(start,drop))}. Arrows move it, Enter places it, Escape cancels.`);});
}
