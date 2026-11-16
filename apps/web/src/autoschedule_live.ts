/* Auto-schedule live view reviewed. Loaded only while a
 * run is shown (or the Configuration preview plays). The grid follows the solver's real better schedules
 * (job.progress.events): People, Timeline and Week move the chips that changed and Counts their numbers, as a steady
 * stream (not a jump per poll), as the department's animation settings say;
 * the footer carries one run strip (step, time bar, countdown); a finish card says what the run gained.
 * Motion is transform and opacity only, through the Web Animations API (the app's reduced-motion CSS does not reach
 * it, so reduced motion is checked here: every change settles at once, nothing moves). A viewer's Still choice (account
 * menu, theme.ts) shows the run as the department's Off does: the grid holds still and the result lands at Done. */
import {el,icon,button,iconButton,phoneQuery,duration,legend,paintMore} from './dom.js';
import {markOpen,CELL_MORE} from './planner_week.js';
import {motionStill} from './theme.js';
import {diff,dutiesChanged,styleOf,fallback,frameBatcher,arcFrames,key,liveModel,runOf,type Op,type Style} from './autoschedule_motion.js';
import {liveProgress,liveSolution,LIVE_STEPS,type AnimationSettings,type LiveProgress} from './generation_options.js';
import type {Row,WorkspaceData} from './types.js';
import {isClosed} from './planner_logic.js';
import {applyWrote} from './api.js';

const EASE='cubic-bezier(.4,0,.2,1)';
const reducedMotion=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;
const esc=(s:string)=>CSS.escape(s);
type Motion={speed:number;reduced:boolean};
type Batch=ReturnType<typeof frameBatcher>;

/* ---- primitives ---- */
let flying=0;
function glide(node:HTMLElement,first:DOMRect,last:DOMRect,bow:number,m:Motion){
  const dx=first.left-last.left,dy=first.top-last.top;
  if(Math.abs(dx)+Math.abs(dy)<1){flying--;return;}
  const {frames,duration}=arcFrames(dx,dy,bow,m.speed);node.classList.add('asl-flying');
  const a=node.animate(frames,{duration,easing:EASE});
  let ended=false;const end=()=>{if(ended)return;ended=true;flying--;if(!node.getAnimations().some(x=>x!==a&&x.playState==='running'))node.classList.remove('asl-flying');};
  a.onfinish=end;a.oncancel=end;
}
const typers=new Map<HTMLElement,{from:string;to:string;at:number;per:number}>();let typeFrame=0;
function typeLoop(now:number){typeFrame=0;
  for(const [node,t] of typers){const e=now-t.at,del=t.from.length*22*t.per,typ=t.to.length*30*t.per;let s:string;
    if(e<del)s=t.from.slice(0,t.from.length-Math.floor(e/(22*t.per)));else if(e<del+typ)s=t.to.slice(0,Math.floor((e-del)/(30*t.per))+1);
    else{s=t.to;typers.delete(node);node.classList.remove('asl-caret');}
    if(node.textContent!==s)node.textContent=s;}
  if(typers.size)typeFrame=requestAnimationFrame(typeLoop);}
/** Every name being typed shows its final text at once (motion turned off mid-run). */
function settleText(){for(const [node,t] of typers){node.textContent=t.to;node.classList.remove('asl-caret');}typers.clear();}
/** Typewriter: deletes at 22 ms a character, types at 30 ms (times the speed). */
function retype(node:HTMLElement,to:string,m:Motion){typers.set(node,{from:node.textContent||'',to,at:performance.now(),per:m.speed});node.classList.add('asl-caret');if(!typeFrame)typeFrame=requestAnimationFrame(typeLoop);}
/** Odometer: the old text slides up and out while the new one rises in. */
function roll(node:HTMLElement,to:string,m:Motion){
  const box=node.parentElement;if(!box){node.textContent=to;return;}
  const old=node.cloneNode(true) as HTMLElement;old.classList.add('asl-roll-old');old.setAttribute('aria-hidden','true');box.classList.add('asl-rollbox');box.append(old);node.textContent=to;
  const d=300*m.speed;
  old.animate([{transform:'none',opacity:1},{transform:'translateY(-110%)',opacity:0}],{duration:d,easing:EASE,fill:'forwards'}).onfinish=()=>{old.remove();if(!box.querySelector('.asl-roll-old'))box.classList.remove('asl-rollbox');};
  node.animate([{transform:'translateY(110%)',opacity:0},{transform:'none',opacity:1}],{duration:d,easing:EASE});
}
const fade=(node:HTMLElement,m:Motion)=>node.animate([{opacity:0},{opacity:1}],{duration:(m.reduced?200:260)*m.speed,easing:'ease-out'});
function swapText(node:HTMLElement|null,to:string,style:Style,m:Motion){
  if(!node||node.textContent===to)return;
  if(style==='retype')retype(node,to,m);else if(style==='roll')roll(node,to,m);
  else{node.textContent=to;if(style!=='none')fade(node.parentElement||node,m);}
}
function appear(chip:HTMLElement,text:HTMLElement,to:string,style:Style,m:Motion){
  if(style==='retype'){text.textContent='';retype(text,to,m);return;}
  text.textContent=to;
  if(style==='roll')chip.animate([{opacity:0,transform:'translateY(70%)'},{opacity:1,transform:'none'}],{duration:280*m.speed,easing:EASE});
  else if(style!=='none')fade(chip,m);
}
function leave(chip:HTMLElement,style:Style,m:Motion){
  chip.getAnimations().forEach(a=>a.cancel());
  if(style==='none'){chip.remove();return;}
  chip.setAttribute('aria-hidden','true');chip.classList.add('asl-leaving');
  const frames=style==='roll'?[{opacity:1,transform:'none'},{opacity:0,transform:'translateY(-70%)'}]:style==='retype'?[{opacity:1},{opacity:1,offset:.7},{opacity:0}]:[{opacity:1},{opacity:0}];
  chip.animate(frames,{duration:(style==='retype'?420:style==='roll'?260:220)*m.speed,easing:EASE,fill:'forwards'}).onfinish=()=>chip.remove();
  if(style==='retype'){const t=chip.querySelector<HTMLElement>('b,.pchip-name');if(t)retype(t,'',m);}
}
/** A count that changed (the Open places "+n", a gap's "+ 2 open places") nudges once. */
const nudge=(node:HTMLElement,m:Motion)=>{if(!m.reduced)node.animate([{opacity:.35,transform:'translateY(-3px)'},{opacity:1,transform:'none'}],{duration:180*m.speed});};

/* ---- the grids ---- */
/** A saved chip the preview changes stands for another assignment until Apply redraws the grid: a copy takes its place,
 *  without its click, menu, hover card, drag or rule mark, and says it is not available. */
export function frozen(x:HTMLElement|null){
  if(!x||x.dataset.livePreview!=null)return x;
  const y=x.cloneNode(true) as HTMLElement;y.dataset.livePreview='';y.setAttribute('aria-disabled','true');
  y.removeAttribute('draggable');y.removeAttribute('title');y.classList.remove('draggable');delete y.dataset.rk;delete y.dataset.rkCard;delete y.dataset.assignment;
  x.replaceWith(y);return y;
}
type Duty={code:string;hue:string;name:string;time:string};
type Ctx={shift:(id:string)=>Row|undefined;day:(id:string)=>string;duty:(id:string)=>Duty;name:(person:string)=>string;label:(person:string)=>string};
/** `apply` returns the flights it started (play releases the rest of what it reserved). */
type View={kind:'people'|'week';grid:HTMLElement;rows:(op:Op)=>number;apply:(op:Op,style:Style,f:Batch,m:Motion)=>number;
  /** The cells a change of the result is in (ringed at Done). */ring:(op:Op)=>(HTMLElement|null|undefined)[]};
/** A duty's hours: the person's own times on the session when they are on it already, else the session's. */
const hoursOf=(s:Row|undefined,person=''):number=>{if(!s)return 0;const p=person?((s.participants||[]) as Row[]).find(x=>x.person_id===person):undefined;
  return (Date.parse(p?.end||s.end)-Date.parse(p?.start||s.start))/36e5;};

/** Stamps a chip the live view draws through its grid's own key (LiveHost.stamp: that grid's colour rule and entries;
 *  `s` null: an open place), so a live chip is keyed exactly as the grid would key it (every mark has one). */
const stampAs=(grid:HTMLElement,chip:Element,s:string|null,p='')=>{chip.removeAttribute('data-lg');(grid as LiveHost).stamp?.(chip,s,p);};

/** People: a row per person, a chip per duty; places come from and go back to the Open places row. */
export function peopleView(grid:HTMLElement,c:Ctx):View{
  const rows=new Map<string,number>(),cells=new Map<string,HTMLElement>(),open=new Map<string,HTMLElement>(),hours=new Map<string,HTMLElement>();
  grid.querySelectorAll<HTMLElement>('.pg-row.person-group[data-row-person]').forEach((r,i)=>{const p=r.dataset.rowPerson!;rows.set(p,i);
    for(const cell of r.querySelectorAll<HTMLElement>('.pg-cell[data-day]'))cells.set(p+'|'+cell.dataset.day,cell);
    const h=r.querySelector<HTMLElement>('.pg-hours[data-hours]');if(h)hours.set(p,h);});
  for(const cell of grid.querySelectorAll<HTMLElement>('.pg-open-row .pg-cell[data-day]'))open.set(cell.dataset.day!,cell);
  const cellOf=(p:string,s:string)=>cells.get(p+'|'+c.day(s));
  const chipOf=(s:string,p:string)=>frozen(cellOf(p,s)?.querySelector<HTMLElement>(`.pg-chip[data-session="${esc(s)}"]:not(.asl-leaving)`)||null);
  const timed=!!grid.querySelector('.pg-chip-time');
  // A chip is its duty type's mark (the grid's "t:" entry: its code and hue).
  const make=(s:string,p:string)=>{const d=c.duty(s),n=el('span',{class:`pg-chip duty-${d.hue}`,'data-session':s,'data-person':p,'data-live-preview':'','aria-label':`${c.label(p)} · ${d.name} · ${d.time}`},el('b',{},''),
    timed?el('span',{class:'pg-chip-time'},d.time):'');stampAs(grid,n,s,p);return n;};
  const become=(chip:HTMLElement,s:string,p:string,style:Style,m:Motion)=>{const d=c.duty(s);chip.dataset.session=s;chip.dataset.person=p;
    chip.className=chip.className.replace(/\bduty-\S+/,'duty-'+d.hue);stampAs(grid,chip,s,p);chip.setAttribute('aria-label',`${c.label(p)} · ${d.name} · ${d.time}`);swapText(chip.querySelector('b'),d.code,style,m);
    const t=chip.querySelector<HTMLElement>('.pg-chip-time');if(t)t.textContent=d.time;};
  const openBy=(s:string,delta:number,m:Motion)=>{const cell=open.get(c.day(s));if(!cell)return;
    let chip=cell.querySelector<HTMLElement>('.pg-open');const n=Number(chip?.dataset.open||0)+delta;
    if(n<=0){chip?.remove();return;}
    if(!chip){chip=el('span',{class:'pg-chip pg-open'},el('b',{}));stampAs(grid,chip,null);cell.prepend(chip);}
    chip.dataset.open=String(n);chip.firstElementChild!.textContent='+'+n;nudge(chip,m);};
  const work=(p:string,s:string,sign:number)=>{const h=hours.get(p);if(!h||!cellOf(p,s))return;
    const now=Number(h.dataset.hours)+sign*hoursOf(c.shift(s)),of=Number(h.dataset.of)||1,target=h.dataset.target;h.dataset.hours=String(now);
    const text=h.querySelector('span:not(.pg-bar)');if(text)text.textContent=`${Math.round(now)}${target?' / '+target:''} h`;
    h.querySelector<HTMLElement>('.pg-bar>span')?.style.setProperty('--fill',String(Math.min(1,Math.max(0,now/of))));};
  // FLIP: read where each chip is, move it, read where it landed, then play it from the old place.
  const fly=(f:Batch,m:Motion,moves:{chip:HTMLElement;from?:HTMLElement;to:HTMLElement;bow:number}[],write:()=>void)=>{
    const firsts:DOMRect[]=[],lasts:DOMRect[]=[];
    f.add({first:()=>moves.forEach((x,i)=>{firsts[i]=(x.from||x.chip).getBoundingClientRect();}),
      write:()=>{for(const x of moves){x.chip.getAnimations().forEach(a=>a.cancel());x.to.append(x.chip);}write();},
      last:()=>moves.forEach((x,i)=>{lasts[i]=x.chip.getBoundingClientRect();}),play:()=>moves.forEach((x,i)=>glide(x.chip,firsts[i],lasts[i],x.bow,m))});};
  return {kind:'people',grid,ring:op=>op.t==='fill'||op.t==='leave'?[cellOf(op.p,op.s)]:op.t==='shift'?[cellOf(op.p,op.to)]:op.t==='person'?[cellOf(op.from,op.s),cellOf(op.to,op.s)]
    :[cellOf(op.a,op.s1),cellOf(op.b,op.s2)],rows:op=>op.t==='person'?Math.abs((rows.get(op.from)??0)-(rows.get(op.to)??0)):0,apply(op,style,f,m){
    if(op.t==='fill'){const cell=cellOf(op.p,op.s),src=open.get(c.day(op.s))?.querySelector<HTMLElement>('.pg-open');
      if(!cell){f.add({write:()=>openBy(op.s,-1,m)});return 0;}
      const n=make(op.s,op.p),code=c.duty(op.s).code;
      if(style==='fly'&&src){n.firstElementChild!.textContent=code;fly(f,m,[{chip:n,from:src,to:cell,bow:1}],()=>{openBy(op.s,-1,m);work(op.p,op.s,1);});return 1;}
      f.add({write:()=>{cell.append(n);openBy(op.s,-1,m);work(op.p,op.s,1);appear(n,n.firstElementChild as HTMLElement,code,style==='fly'?'fade':style,m);}});
      return 0;}
    if(op.t==='leave'){const x=chipOf(op.s,op.p);f.add({write:()=>{if(x)leave(x,style,m);openBy(op.s,1,m);work(op.p,op.s,-1);}});return 0;}
    if(op.t==='shift'){const x=chipOf(op.from,op.p);f.add({write:()=>{if(x)become(x,op.to,op.p,style,m);work(op.p,op.from,-1);work(op.p,op.to,1);}});return 0;}
    if(op.t==='person'){const x=chipOf(op.s,op.from),to=cellOf(op.to,op.s);
      const hrs=()=>{work(op.from,op.s,-1);work(op.to,op.s,1);};
      if(style==='fly'&&x&&to){fly(f,m,[{chip:x,to,bow:(rows.get(op.to)??0)>(rows.get(op.from)??0)?1:-1}],()=>{x.dataset.person=op.to;x.setAttribute('aria-label',`${c.label(op.to)} · ${c.duty(op.s).name}`);hrs();});return 1;}
      const st=style==='fly'?'fade':style;
      f.add({write:()=>{if(x)leave(x,st,m);if(to){const n=make(op.s,op.to);to.append(n);appear(n,n.firstElementChild as HTMLElement,c.duty(op.s).code,st,m);}hrs();}});return 0;}
    // swap: a's chip (s1) goes to b and b's (s2) to a
    const x=chipOf(op.s1,op.a),y=chipOf(op.s2,op.b),ca=cellOf(op.a,op.s1),cb=cellOf(op.b,op.s2);
    const hrs=()=>{work(op.a,op.s1,-1);work(op.a,op.s2,1);work(op.b,op.s2,-1);work(op.b,op.s1,1);};
    if(style==='fly'&&x&&y&&ca&&cb){fly(f,m,[{chip:x,to:cb,bow:1},{chip:y,to:ca,bow:1}],()=>{x.dataset.person=op.b;y.dataset.person=op.a;hrs();});return 2;}
    const st=style==='fly'?'fade':style;
    f.add({write:()=>{if(x)become(x,op.s2,op.a,st,m);if(y)become(y,op.s1,op.b,st,m);hrs();}});return 0;
  }};
}

/** Week: a row per session; names change in place, and a person moving between sessions travels. */
export function weekView(grid:HTMLElement,c:Ctx):View{
  // Chips are found by data-shift: data-session and data-person are the open panel's highlight marks (planner_week highlightFocus).
  const chipOf=(s:string,p:string)=>frozen(grid.querySelector<HTMLElement>(`.pchip[data-shift="${esc(s)}"][data-person="${esc(p)}"]:not(.asl-leaving)`));
  // "No one yet" (Open places hidden) carries its session too: the session's place in the cell, replaced on a fill.
  const emptyOf=(s:string)=>grid.querySelector<HTMLElement>(`.pchip.empty[data-shift="${esc(s)}"]`);
  const gapOf=(s:string)=>grid.querySelector<HTMLElement>(`.pchip.gap[data-shift="${esc(s)}"]`);
  // The cell, not a chip's parent: a member chip sits inside its team pill (planner_week), never a home for others.
  const homeOf=(s:string)=>cellOf(gapOf(s)||grid.querySelector<HTMLElement>(`.pchip[data-shift="${esc(s)}"]`))||null;
  const sibOf=(s:string)=>grid.querySelector<HTMLElement>(`.pchip[data-shift="${esc(s)}"]:not(.gap):not(.empty)`);
  const hueOf=(s:string)=>sibOf(s)?.className.match(/\bduty-(\S+)/)?.[1]||c.duty(s).hue;
  const put=(s:string,node:HTMLElement)=>{const g=gapOf(s),e=emptyOf(s);if(g)g.before(node);else if(e)e.replaceWith(node);else homeOf(s)?.append(node);emptyOf(s)?.remove();};
  // `at`: the session's cell, taken before its last chip leaves (an instant leave removes it at once).
  const gapBy=(s:string,delta:number,m:Motion,at?:HTMLElement|null)=>{let g=gapOf(s);const n=(g?Number(/\d+/.exec(g.textContent||'')?.[0]||0):0)+delta;
    if(n<=0){g?.remove();return;}
    if(!g){const home=homeOf(s)||at;if(!home)return;g=el('span',{class:'pchip gap','data-shift':s},`+ ${n} open place${n>1?'s':''}`);stampAs(grid,g,null);home.append(g);}
    else g.textContent=(g.textContent||'').replace(/\d+/,String(n)).replace(/places?\b/,n===1?'place':'places');
    nudge(g,m);};
  const cellOf=(n:HTMLElement|null)=>n?.closest<HTMLElement>('.wk-cell')||undefined;
  // A live change says nothing about supervision: the team box a changed chip is in opens into plain chips, so no one
  // shows under a supervisor they were not named with (the redraw at Done groups the teams again).
  const loose=(x:HTMLElement|null)=>{const t=x?.closest<HTMLElement>('.team-box');if(t)t.replaceWith(...t.querySelectorAll<HTMLElement>('.pchip'));};
  const make=(s:string,p:string)=>{const n=el('span',{class:`pchip duty-${hueOf(s)}`,'data-shift':s,'data-person':p,'data-live-preview':'','aria-label':`${c.label(p)}, ${c.duty(s).name}`},el('span',{class:'pchip-name'},''));
    stampAs(grid,n,s,p);return n;};
  // A busy cell shows its first chips and "+N" (the button carries the renderer's limit): after a change, the
  // cells with hidden chips show the right ones again, so a hidden chip moved into a quiet cell shows.
  const refit=()=>{for(const cell of grid.querySelectorAll<HTMLElement>('.wk-cell:has(>.pchip.more[data-limit]),.wk-cell:has(>:is(.pchip,.team-box)[hidden])')){
    const more=cell.querySelector<HTMLElement>(':scope>.pchip.more[data-limit]'),limit=Number(more?.dataset.limit)||Infinity;
    const chips=[...cell.querySelectorAll<HTMLElement>(':scope>:is(.pchip,.team-box):not(.gap):not(.more):not(.asl-leaving)')];
    chips.forEach((x,i)=>{x.hidden=i>=limit;});
    if(!more)continue;
    if(chips.length<=limit){more.remove();continue;}
    const n=chips.length-limit;chips[limit-1].after(more);paintMore(more,n,CELL_MORE);}};
  return {kind:'week',grid,rows:()=>0,ring:op=>(op.t==='fill'?[chipOf(op.s,op.p)]:op.t==='leave'?[gapOf(op.s)]:op.t==='person'?[chipOf(op.s,op.to)]:op.t==='shift'?[chipOf(op.to,op.p),gapOf(op.from)]
    :[chipOf(op.s1,op.b),chipOf(op.s2,op.a)]).map(n=>cellOf(n)),apply(op,style,f,m){const flights=change(op,style,f,m);f.add({write:refit});return flights;}};
  function change(op:Op,style:Style,f:Batch,m:Motion):number{
    const st=style==='fly'?'fade':style;
    if(op.t==='fill'){f.add({write:()=>{const home=homeOf(op.s);if(!home)return;const n=make(op.s,op.p);put(op.s,n);gapBy(op.s,-1,m);appear(n,n.firstElementChild as HTMLElement,c.name(op.p),st,m);}});return 0;}
    if(op.t==='leave'){const x=chipOf(op.s,op.p);f.add({write:()=>{const at=cellOf(x);loose(x);if(x)leave(x,st,m);gapBy(op.s,1,m,at);}});return 0;}
    if(op.t==='person'){const x=chipOf(op.s,op.from);f.add({write:()=>{if(!x)return;loose(x);x.dataset.person=op.to;x.setAttribute('aria-label',`${c.label(op.to)}, ${c.duty(op.s).name}`);swapText(x.querySelector('.pchip-name'),c.name(op.to),st,m);}});return 0;}
    if(op.t==='shift'){const x=chipOf(op.from,op.p);
      if(style==='fly'&&x&&!x.closest('[hidden]')&&homeOf(op.to)){let first:DOMRect,last:DOMRect;const from=cellOf(x);  // a hidden chip (past + N more) has no place to fly from
        f.add({first:()=>{first=x.getBoundingClientRect();},write:()=>{x.getAnimations().forEach(a=>a.cancel());loose(x);const hue=hueOf(op.to);put(op.to,x);x.dataset.shift=op.to;
          x.className=x.className.replace(/\bduty-\S+/,'duty-'+hue);stampAs(grid,x,op.to,op.p);gapBy(op.from,1,m,from);gapBy(op.to,-1,m);},last:()=>{last=x.getBoundingClientRect();},play:()=>glide(x,first,last,1,m)});return 1;}
      f.add({write:()=>{const at=cellOf(x);loose(x);if(x)leave(x,st,m);gapBy(op.from,1,m,at);const home=homeOf(op.to);if(home){const n=make(op.to,op.p);put(op.to,n);appear(n,n.firstElementChild as HTMLElement,c.name(op.p),st,m);}gapBy(op.to,-1,m);}});return 0;}
    const x=chipOf(op.s1,op.a),y=chipOf(op.s2,op.b);
    if(style==='fly'&&x&&y&&!x.closest('[hidden]')&&!y.closest('[hidden]')){let fx:DOMRect,fy:DOMRect,lx:DOMRect,ly:DOMRect;
      f.add({first:()=>{fx=x.getBoundingClientRect();fy=y.getBoundingClientRect();},
        write:()=>{for(const n of [x,y]){n.getAnimations().forEach(a=>a.cancel());loose(n);}const px=el('i'),py=el('i');x.before(px);y.before(py);px.replaceWith(y);py.replaceWith(x);
          const hx=x.className.match(/\bduty-\S+/)?.[0]||'',hy=y.className.match(/\bduty-\S+/)?.[0]||'';x.className=x.className.replace(hx,hy);y.className=y.className.replace(hy,hx);
          stampAs(grid,x,op.s2,op.a);stampAs(grid,y,op.s1,op.b);
          x.dataset.shift=op.s2;y.dataset.shift=op.s1;},
        last:()=>{lx=x.getBoundingClientRect();ly=y.getBoundingClientRect();},play:()=>{glide(x,fx,lx,1,m);glide(y,fy,ly,1,m);}});return 2;}
    f.add({write:()=>{loose(x);loose(y);if(x){x.dataset.person=op.b;swapText(x.querySelector('.pchip-name'),c.name(op.b),st,m);}if(y){y.dataset.person=op.a;swapText(y.querySelector('.pchip-name'),c.name(op.a),st,m);}}});return 0;
  }
}

/** What the Schedule grid (schedule_grid.ts: Timeline, Counts, any rows × columns) says about its own layout, set on it
 *  as `live`: only it knows where a person's duty is drawn (rows and Group › Then by of any dimension, people with no row
 *  yet, columns of time or of a dimension). Cells a duty can be drawn in carry it as `data-units` ("session~person", start
 *  order); a count of places has `data-count` and its `data-sessions`; a row's hours `data-hours`, its duties `data-duties`. */
export type LiveGrid={
  /** The cells a person's duty is drawn in (each row and column it is in); `make` adds the person's row where they have none. */
  cells:(shift:string,person:string,make:boolean)=>HTMLElement[];
  /** A session's places as drawn (a checked draft), or nothing. */cover:(shift:string)=>{required:number;available:number}|undefined;
  /** A chip's accessible name in a cell, as the grid names it (`n` duties of one code: "2×A"); stamps `chip` with its duty
   *  type's key entry (every mark drawn has one). */
  name:(shift:string,cell:HTMLElement,n:number,chip:HTMLElement)=>string;
  /** Stamps a "+N" it draws with the key's entry. */more:(node:HTMLElement)=>void};
/** A grid the live view animates: `live` (the codes/counts grid's map), `stamp` (Week and People × Days: draws a chip
 *  through the grid's own key; `s` null: an open place). */
export type LiveHost=HTMLElement&{live?:LiveGrid;stamp?:(node:Element,s:string|null,p:string)=>void};
const pairsOf=(op:Op):[[string,string][],[string,string][]]=>op.t==='fill'?[[],[[op.s,op.p]]]:op.t==='leave'?[[[op.s,op.p]],[]]
  :op.t==='shift'?[[[op.from,op.p]],[[op.to,op.p]]]:op.t==='person'?[[[op.s,op.from]],[[op.s,op.to]]]:[[[op.s1,op.a],[op.s2,op.b]],[[op.s1,op.b],[op.s2,op.a]]];

/** The grid's code cells (a chip per duty code, "2×A" for repeats, past two codes the first and "+N") and Counts
 *  (numbers): what changed is drawn again in place; a duty given to another person flies when it is a chip in both cells. */
export function rowsView(grid:HTMLElement,c:Ctx,g:LiveGrid):View{
  const counts=grid.classList.contains('ct-grid');
  const ids=(n:HTMLElement)=>(n.dataset.sessions||'').split(' ').filter(Boolean);
  const units=(n:HTMLElement)=>(n.dataset.units||'').split(' ').filter(Boolean);
  const sid=(u:string)=>u.split('~')[0],start=(u:string)=>String(c.shift(sid(u))?.start||'');
  const tallies=new Map<string,HTMLElement[]>(),avail=new Map<string,number>(),order=new Map<string,number>();
  for(const n of grid.querySelectorAll<HTMLElement>('[data-count]'))for(const s of ids(n))tallies.set(s,[...(tallies.get(s)||[]),n]);
  grid.querySelectorAll<HTMLElement>('.pg-row[data-row-person]').forEach((r,i)=>{if(!order.has(r.dataset.rowPerson!))order.set(r.dataset.rowPerson!,i);});
  const chipOf=(cell:HTMLElement,s:string)=>cell.querySelector<HTMLElement>(`:scope>.pg-chip[data-session="${esc(s)}"]:not(.sg-more):not(.asl-leaving)`);
  // A number that changed rises into place (fades with reduced motion); its label says the new one.
  const number=(n:HTMLElement,to:string,style:Style,m:Motion)=>{if(n.textContent===to)return;n.textContent=to;
    if(style==='fade')fade(n,m);else if(style!=='none')n.animate([{opacity:0,transform:'translateY(60%)'},{opacity:1,transform:'none'}],{duration:240*m.speed,easing:EASE});};
  const said=(n:HTMLElement,words:string)=>{const l=n.getAttribute('aria-label');if(l)n.setAttribute('aria-label',l.replace(/: [^:]*$/,': '+words));};
  // Places filled / needed (Staffed / needed, a duty's cells): each session's places follow its live duties.
  const placed=(s:string,sign:number,style:Style,m:Motion)=>{const v=g.cover(s);if(!v)return;avail.set(s,(avail.get(s)??v.available)+sign);
    for(const n of tallies.get(s)||[]){let need=0,have=0;
      for(const x of ids(n)){const y=g.cover(x);if(y){need+=y.required;have+=Math.min(y.required,avail.get(x)??y.available);}}
      n.classList.toggle('short',have<need);number(n,`${have}/${need}`,style,m);said(n,`${have} of ${need} places filled`);}};
  const work=(row:HTMLElement,s:string,p:string,sign:number,style:Style,m:Motion)=>{
    const d=row.querySelector<HTMLElement>('.pg-hours[data-duties]');
    if(d){d.dataset.duties=String(Number(d.dataset.duties)+sign);number(d,d.dataset.duties,style,m);}
    const h=row.querySelector<HTMLElement>('.pg-hours[data-hours]');if(!h)return;
    const now=Number(h.dataset.hours)+sign*hoursOf(c.shift(s),p);h.dataset.hours=String(now);h.textContent=`${Math.round(now)} h`;
    if(h.hasAttribute('aria-label'))h.setAttribute('aria-label',`${Math.round(now)} hours`);};
  // The grid's name stamps the chip as what it now shows; the stamps of what it showed before go first.
  const named=(chip:HTMLElement,s:string,p:string,cell:HTMLElement,n:number)=>{chip.dataset.session=s;chip.dataset.person=p;chip.removeAttribute('data-lg');
    chip.className=chip.className.replace(/\bduty-\S+/,'').trim()+' duty-'+c.duty(s).hue;chip.setAttribute('aria-label',g.name(s,cell,n,chip));};
  let carried=new Map<string,HTMLElement>();  // chips flying to their new cell (during one change's write)
  /** A cell: its duties in start order as the grid draws them; in Counts, how many. */
  const draw=(cell:HTMLElement,list:string[],style:Style,m:Motion)=>{
    cell.dataset.units=list.join(' ');
    if(counts){number(cell,list.length?String(list.length):'',style,m);said(cell,!list.length?'none':`${list.length} dut${list.length>1?'ies':'y'}`);return;}
    const byCode=new Map<string,string[]>();for(const u of list){const k=c.duty(sid(u)).code;byCode.set(k,[...(byCode.get(k)||[]),u]);}
    const codes=[...byCode.entries()],drawn=codes.length>2?codes.slice(0,1):codes;
    const chips=[...cell.querySelectorAll<HTMLElement>(':scope>.pg-chip:not(.asl-leaving)')];let prev:HTMLElement|null=null;
    drawn.forEach(([code,us],i)=>{
      const [s,p]=us[0].split('~'),text=us.length>1?`${us.length}×${code}`:code,x=chips[i],flown=us.length===1?carried.get(s):undefined;
      let n:HTMLElement;
      if(flown){carried.delete(s);named(flown,s,p,cell,1);if(x)x.replaceWith(flown);else if(prev)prev.after(flown);else cell.prepend(flown);n=flown;}
      else if(!x){n=el('span',{class:'pg-chip','data-live-preview':''},el('b',{},''));named(n,s,p,cell,us.length);if(prev)prev.after(n);else cell.prepend(n);appear(n,n.firstElementChild as HTMLElement,text,style,m);}
      else{n=x;if(x.dataset.session!==s||x.firstElementChild?.textContent!==text){n=frozen(x)!;named(n,s,p,cell,us.length);swapText(n.querySelector('b'),text,style,m);}}
      n.classList.toggle('sg-more',us.length>1);prev=n;});
    for(const x of chips.slice(drawn.length))if(x.isConnected)leave(x,style,m);
    // The rest opens from "+N" (the saved list until Apply, so the button is a preview like the chips).
    const old=cell.querySelector<HTMLElement>(':scope>.tl-more'),n=codes.slice(drawn.length).reduce((a,[,us])=>a+us.length,0);
    if(n<1){old?.remove();return;}
    const more=old?frozen(old)!:cell.appendChild(el('button',{type:'button',class:'link-button tl-more','data-live-preview':'','aria-disabled':'true'}));g.more(more);
    if(more.textContent!=='+'+n){more.textContent='+'+n;more.setAttribute('aria-label',`${n} more session${n>1?'s':''}`);nudge(more,m);}};
  const change=(s:string,p:string,sign:number,style:Style,m:Motion)=>{placed(s,sign,style,m);const k=`${s}~${p}`,rows=new Set<HTMLElement>();
    for(const cell of g.cells(s,p,sign>0)){const row=cell.closest<HTMLElement>('.pg-row')!;
      if(row.dataset.livePreview===''){row.dataset.livePreview='shown';if(style!=='none')fade(row,m);}  // the person's first duty here
      const list=units(cell).filter(x=>x!==k);if(sign>0)list.push(k);list.sort((a,b)=>start(a).localeCompare(start(b)));
      draw(cell,list,style,m);rows.add(row);}
    for(const row of rows)work(row,s,p,sign,style,m);};
  return {kind:'people',grid,rows:op=>op.t==='person'?Math.abs((order.get(op.from)??order.size)-(order.get(op.to)??order.size)):0,
    ring:op=>{const [r,a]=pairsOf(op);return [...a,...r].flatMap(([s,p])=>[...g.cells(s,p,false),...(counts?tallies.get(s)||[]:[])]);},
    apply(op,style,f,m){
      const [removes,adds]=pairsOf(op),st:Style=style==='fly'?(counts?'roll':'fade'):style;
      const write=()=>{for(const [s,p] of removes)change(s,p,-1,st,m);for(const [s,p] of adds)change(s,p,1,st,m);};
      const moves=counts||style!=='fly'?[]:op.t==='person'?[{s:op.s,p:op.from}]:op.t==='swap'?[{s:op.s1,p:op.a},{s:op.s2,p:op.b}]:[];
      const go=moves.map(x=>{const cells=g.cells(x.s,x.p,false),chip=cells.length===1?chipOf(cells[0],x.s):null;return chip?{s:x.s,chip:frozen(chip)!}:null;});
      if(!go.length||go.some(x=>!x)){f.add({write});return 0;}
      const list=go as {s:string;chip:HTMLElement}[],firsts:DOMRect[]=[],lasts:(DOMRect|null)[]=[];
      f.add({first:()=>list.forEach((x,i)=>{firsts[i]=x.chip.getBoundingClientRect();}),
        write:()=>{carried=new Map();
          for(const x of list)if(x.chip.isConnected&&x.chip.dataset.session===x.s){x.chip.getAnimations().forEach(a=>a.cancel());x.chip.remove();carried.set(x.s,x.chip);}
          write();carried=new Map();},
        last:()=>list.forEach((x,i)=>{lasts[i]=x.chip.isConnected?x.chip.getBoundingClientRect():null;}),
        play:()=>list.forEach((x,i)=>{const l=lasts[i];if(l)glide(x.chip,firsts[i],l,1,m);else flying--;})});
      return list.length;}};
}
function viewIn(root:ParentNode,c:Ctx):View|null{
  const grid=root.querySelector<LiveHost>('.people-grid,.wk-grid');if(!grid)return null;
  return grid.live?rowsView(grid,c,grid.live):grid.classList.contains('wk-grid')?weekView(grid,c):peopleView(grid,c);
}
/** Shows a list of changes on a view in one frame: each in its style, flights within the cap, the rest in place.
 *  (Exported with peopleView, weekView and inFlight for the Node unit tests.) */
export const inFlight=()=>flying;
export function play(view:View,ops:Op[],settings:AnimationSettings,f:Batch,instant=false){
  const m={speed:settings.speed,reduced:reducedMotion()};
  for(const op of ops){
    let style:Style=instant?'none':styleOf(op,{settings,view:view.kind,rows:view.rows(op),reduced:m.reduced});
    let need=0;if(style==='fly'){need=op.t==='swap'?2:1;if(flying+need>settings.max_concurrent_flights){style=fallback(settings);need=0;}else flying+=need;}
    // A change that does not fly after all (a chip not on screen, a row filtered out) gives its reservation back.
    flying-=need-view.apply(op,style,f,m);
  }
}

/* ---- the footer run strip: "3/5 Improving", a 120×4 time bar, "2:14 used" — one line, nothing else ---- */
function runStrip(){
  const step=el('span',{class:'asl-step'},''),box=el('span',{class:'asl-stepbox','aria-hidden':'true'},step);
  const said=el('span',{class:'sr-only',role:'status','aria-live':'polite','aria-atomic':'true'});
  const fill=el('i',{}),bar=el('span',{class:'asl-bar',role:'progressbar','aria-label':'Search time used','aria-valuemin':'0','aria-valuemax':'100','aria-valuenow':'0'},fill);
  const left=el('span',{class:'asl-left','aria-hidden':'true'},'');
  const node=el('span',{class:'asl-strip',role:'group','aria-label':'Auto-schedule run'},box,said,bar,left);
  let shown=0,pct=-1;
  return {node,set(n:number,time:string,frac:number,how:'roll'|'retype',m:Motion){
    if(n!==shown){shown=n;const text=`${n}/5 ${LIVE_STEPS[n-1]}`;said.textContent=`Step ${n} of 5: ${LIVE_STEPS[n-1]}`;
      if(!step.textContent||m.reduced)step.textContent=text;else if(how==='retype')retype(step,text,m);else roll(step,text,m);
      bar.classList.toggle('done',n===5);}
    if(left.textContent!==time){left.textContent=time;bar.setAttribute('aria-valuetext',time);}
    const p=Math.round(Math.min(1,Math.max(0,frac))*100);if(p!==pct){pct=p;fill.style.setProperty('--f',String(p/100));bar.setAttribute('aria-valuenow',String(p));}
  }};
}

/** How long "5/5 Done" stays in the footer before the screen's own status returns (D-123). */
export const DONE_STRIP_MS=3000;
const PANEL='#auto-panel,.asl-result';
/** Done is shown once: `gone` runs after DONE_STRIP_MS or at the first focus, press or key in the Auto-schedule panel or
 *  result card, whichever is first. Returns a stop that clears the timer and listeners without calling `gone`. */
export function lingerDone(gone:()=>void):()=>void{
  const kinds=['focusin','pointerdown','keydown'] as const;
  const inside=(e:Event)=>{if((e.target as Element|null)?.closest?.(PANEL))end();};
  const stop=()=>{window.clearTimeout(timer);for(const k of kinds)document.removeEventListener(k,inside,true);};
  const end=()=>{stop();gone();};
  const timer=window.setTimeout(end,DONE_STRIP_MS);
  for(const k of kinds)document.addEventListener(k,inside,true);
  return stop;
}

/* ---- one run ---- */
export type LiveHooks={data:WorkspaceData;settings:()=>AnimationSettings;day:(shift:Row)=>string;duty:(shift:Row)=>Duty;name:(person:string)=>string;label:(person:string)=>string;
  /** Draw the schedule again (the grid shows the draft as saved). */redraw:()=>void;
  /** The job's detail: its progress carries the latest complete solution (catching up after missed events). */read:(id:string)=>Promise<Row>};
export type FinishCard={gain:number|null;review:()=>void;apply:(()=>Promise<unknown>)|null};
type Run={id:string;hooks:LiveHooks;update:(job:Row)=>void;finish:(job:Row,card:FinishCard|null)=>void;dispose:()=>void;ended:()=>boolean};
let current:Run|null=null;

/** The live view of one auto-schedule run; asking again for the same job gives the same one (redraws keep it). */
export function liveRun(jobId:string,hooks:LiveHooks):Run{
  if(current?.id===jobId){current.hooks=hooks;return current;}
  current?.dispose();
  const run=start(jobId,hooks);current=run;return run;
}
function start(id:string,first:LiveHooks):Run{
  const run:Run={id,hooks:first,update,finish,dispose,ended:()=>done};
  // The department's settings, Off for a viewer who chose Still (read each tick: a change applies mid-run).
  const settings=():AnimationSettings=>{const s=run.hooks.settings();return motionStill()?{...s,mode:'off'}:s;};
  const shifts=new Map(first.data.shifts.map(s=>[String(s.id),s]));
  const day=(s:string)=>{const x=shifts.get(s);return x?run.hooks.day(x):'';};
  const base=new Set<string>();for(const s of first.data.shifts)if(!s.context)for(const p of s.participants||[])base.add(key(String(s.id),String(p.person_id)));
  const ctx:Ctx={shift:s=>shifts.get(s),day,duty:s=>{const x=shifts.get(s);return x?run.hooks.duty(x):{code:'?',hue:'slate',name:'',time:''};},name:p=>run.hooks.name(p),label:p=>run.hooks.label(p)};
  let shown=new Set(base);
  const model=liveModel(base,s=>!!shifts.get(s)&&!shifts.get(s)!.context&&!isClosed(shifts.get(s)!)),{target,queue}=model;
  let held=false,catching=false,progress:LiveProgress|null=null,readAt=0,lastUpdate=performance.now(),done=false,view:View|null=null,pendingGrid:HTMLElement|null=null;
  const f=frameBatcher(),strip=runStrip();
  // After Done the strip lingers once (lingerDone), then the footer shows the screen's status again.
  let stripOver=false,stopLinger:(()=>void)|null=null;
  const motion=()=>({speed:run.hooks.settings().speed,reduced:reducedMotion()||settings().mode==='off'});
  // The grid on screen: a redraw (live update, another view) shows the draft again, so what this run showed is put back
  // at once, one tick later (People draws its rows in batches).
  // Only this rota's schedule (its view key starts with the rota's id).
  const ours=()=>{const host=document.getElementById('schedule-view');return host?.dataset.viewKey?.startsWith(first.data.workspace.id+'|')?host:null;};
  // `last`: Done binds a grid drawn just before it at once (the final result must land); after Done a redraw shows the
  // draft as saved.
  const bind=(last=false)=>{
    const host=ours(),grid=host?.querySelector<HTMLElement>('.people-grid,.wk-grid')||null;
    if(!grid||done&&!last){view=view?.grid.isConnected?view:null;return;}
    if(view?.grid===grid)return;
    if(pendingGrid!==grid&&!last){pendingGrid=grid;return;}
    pendingGrid=null;pending=[];view=viewIn(host!,ctx);if(!view)return;  // a new grid shows all of `shown` at once
    grid.setAttribute('aria-busy','true');
    play(view,diff(base,shown,day),settings(),f,true);
  };
  // Continuous motion: the changes of each better schedule are queued and played as a steady stream, spread evenly over
  // the time until the next one is expected (the gaps seen so far, at least 1/updates-per-second, at most 4 s), behind
  // what is still queued, so flights overlap (within the cap) instead of landing together every poll.
  let pending:{op:Op;at:number}[]=[],pump=0,lastBatch=0,gap=0,drained:(()=>void)|null=null;
  const stream=(ops:Op[])=>{
    const now=performance.now(),s=settings();
    if(lastBatch){const g=Math.min(4000,now-lastBatch);gap=gap?gap*.6+g*.4:g;}lastBatch=now;
    const w=Math.max(1000/s.max_updates_per_second,Math.min(4000,gap)),all=[...pending.map(x=>x.op),...ops];
    pending=all.map((op,i)=>({op,at:now+w*i/all.length}));
    if(!pump)pump=requestAnimationFrame(step);
  };
  function step(){pump=0;if(!view||held)return;
    const now=performance.now();let n=0;while(n<pending.length&&pending[n].at<=now)n++;
    n=runOf(pending.slice(0,n).map(x=>x.op));
    if(n)play(view,pending.splice(0,n).map(x=>x.op),settings(),f);
    if(pending.length)pump=requestAnimationFrame(step);else if(drained){f.add({last:drained});drained=null;}
  }
  /** Shows what is queued (and `more`) at once, in order (motion turned off, Done while off). */
  const flush=(more:Op[]=[])=>{const ops=[...pending.map(x=>x.op),...more];pending=[];
    const next=(rest:Op[])=>{if(!view)return;const n=Math.max(1,runOf(rest));play(view,rest.slice(0,n),settings(),f,true);
      if(rest.length>n)f.add({play:()=>next(rest.slice(n))});else if(drained){f.add({last:drained});drained=null;}};
    next(ops);};
  const mountStrip=()=>{const status=document.querySelector<HTMLElement>('.app-footer .page-status>.footer-status');
    // On a phone the open Auto-schedule sheet shows the run; closed, the footer keeps its status visible (H-12).
    if(!ours()||!status||phoneQuery.matches&&document.getElementById('auto-panel')?.hidden===false){strip.node.remove();if(done){dispose();if(!ours())document.querySelector('.asl-result')?.remove();}return;}
    if(stripOver)return;
    if(status.firstElementChild!==strip.node)status.prepend(strip.node);};
  const paint=()=>{if(!progress)return;
    const s=settings(),m=motion(),searched=Math.min(progress.limit||Infinity,progress.searched+(progress.searching&&!done?(performance.now()-readAt)/1000:0));
    // Time used, not a guessed time left (A4), until a measured estimate exists.
    const time=`${duration(searched)} used`;
    strip.set(done?5:progress.step,time,done?1:progress.limit?searched/progress.limit:0,s.step_change,m);};
  const tick=()=>{
    if(performance.now()-lastUpdate>60_000&&!done){dispose();return;}  // the page that polled it is gone
    mountStrip();bind();paint();
    const s=settings();
    // Off (the department's or this viewer's Still): moving chips land at once and the grid holds still; motion turned
    // back on shows what changed meanwhile, at once.
    if(!done&&s.mode==='off'){if(!held){held=true;flush();view?.grid.getAnimations({subtree:true}).forEach(a=>a.finish());settleText();}}
    else if(held){held=false;model.take();if(view&&!model.lost){flush(diff(shown,target,day));shown=new Set(target);}}
    // No grid bound yet (the first read can carry events): the queue waits, or its changes would be taken unshown.
    if(done||model.lost||!queue.length||!view)return;
    model.take();
    if(s.mode==='off'||held)return;  // the model follows; the grid is drawn at the end
    stream(diff(shown,target,day));shown=new Set(target);
  };
  // The model follows the solver's events (shown or not); the grid follows the model.
  const timer=window.setInterval(tick,100);
  // A finished run ends once its result is applied (discarded: endFinished); its strip has left already (lingerDone).
  // A held Apply (the proposal retained or refused) wrote nothing: the run stays.
  const applied=(e:Event)=>{const d=(e as CustomEvent).detail;if(done&&d?.kind==='generation.apply'&&applyWrote(d.result))dispose();};
  window.addEventListener('rota-saved',applied);
  function update(job:Row){
    lastUpdate=performance.now();progress=liveProgress(job);readAt=performance.now();
    // Changes we did not see (opened mid-run, events dropped from the buffer, or too many to send): the grid holds
    // still until a snapshot catches up.
    model.observe(progress.events);
    if(model.lost&&!catching&&!done)void catchUp();
    tick();
  }
  // Missed events (opened mid-run, a slow poll, more than the event cap): the job's latest complete solution replaces
  // the preview, and the events after it replay. A snapshot older than the events
  // seen leaves the model lost, and the next poll asks again.
  async function catchUp(){catching=true;
    try{const sol=liveSolution(await run.hooks.read(id));if(sol&&!done)model.install(sol);}
    catch{/* the result reconciles at Done */}
    finally{catching=false;}
  }
  function finish(job:Row,card:FinishCard|null){
    if(done)return;done=true;lastUpdate=performance.now();
    // Cancelled or failed: nothing to review; a grid that showed changes goes back to the draft.
    if(job.status!=='succeeded'||!job.result){dispose();if(view&&diff(base,shown,day).length)run.hooks.redraw();return;}
    const before=progress?.searched??0,counted=progress?.improvements??0;progress=liveProgress(job);
    progress.searched=Number(job.result.phase_timings?.search_seconds??(progress.searched||before));paint();
    stopLinger=lingerDone(()=>{stopLinger=null;stripOver=true;strip.node.remove();});
    // The result is the truth: its additions and removals on the draft as it was.
    const byAssignment=new Map<string,string>();for(const s of first.data.shifts)for(const p of s.participants||[])byAssignment.set(String(p.id),String(p.person_id));
    const final=new Set(base);
    for(const r of (job.result.removals||[]) as Row[]){const p=r.person_id||r.participant?.person_id||byAssignment.get(String(r.assignment_id));if(p)final.delete(key(String(r.shift_id),String(p)));}
    for(const a of (job.result.additions||[]) as Row[]){const p=a.person_id||a.participant?.person_id;if(p)final.add(key(String(a.shift_id),String(p)));}
    queue.length=0;bind(true);
    const s=settings(),last=diff(shown,final,day);shown=final;
    // A quiet ring where a duty now differs from the draft (the existing "changed" look), once the last moves have landed.
    const result=diff(base,final,day);
    drained=()=>{view?.grid.setAttribute('aria-busy','false');for(const op of result)for(const cell of view?.ring(op)||[])cell?.classList.add('asl-changed');};
    if(!view){drained=null;}
    else if(s.mode==='off')flush(last);
    else if(pending.length||last.length)stream(last);  // the tail plays on as the stream did
    else{f.add({last:drained});drained=null;}
    held=false;  // at Done nothing is held; a stream still playing finishes
    // The worker's count (every better schedule, shown or not); never fewer than were shown.
    const better=Math.max(progress.improvements??0,counted,model.seen);
    if(card)finishCard(card,dutiesChanged(result),better);
  }
  function dispose(){window.clearInterval(timer);stopLinger?.();stopLinger=null;window.removeEventListener('rota-saved',applied);cancelAnimationFrame(pump);pump=0;strip.node.remove();if(current===run)current=null;}
  return run;
}

/** A finished run's result was discarded: its strip and card go (a run still going stays). */
export function endFinished(){if(current?.ended()){current.dispose();document.querySelector('.asl-result')?.remove();}}
/** Another account signs in: the run, its strip and its card go. */
export function endLive(){current?.dispose();document.querySelector('.asl-result')?.remove();}
/** At Done: only what the footer does not show (owner rule) — score gain, duties changed, better schedules — and the
 *  two ways on: Review result, Apply to draft. */
function finishCard(card:FinishCard,duties:number,better:number){
  document.querySelector('.asl-result')?.remove();
  const close=():void=>box.remove();
  const stat=(label:string,value:string)=>el('div',{},el('dt',{},label),el('dd',{},value));
  const box:HTMLElement=el('section',{class:'asl-result',role:'region','aria-label':'Auto-schedule result'},
    el('header',{},el('span',{class:'auto-icon','aria-hidden':'true'},icon('sparkle')),el('strong',{},'Ready for review'),iconButton('Close','close',close,{class:'bare'})),
    el('dl',{},...(card.gain==null?[]:[stat('Score gain',(card.gain>0?'+':card.gain<0?'−':'±')+Math.abs(card.gain))]),stat('Duties changed',String(duties)),stat('Better schedules',String(better))),
    el('div',{class:'actions'},button('Review result',()=>{close();card.review();},'secondary'),card.apply?button('Apply to draft',async()=>{await card.apply!();close();},'primary'):''));
  box.addEventListener('keydown',(e:KeyboardEvent)=>{if(e.key==='Escape'){e.stopPropagation();close();}});
  document.body.append(box);
}

/* ---- Configuration › Auto-schedule › Animation: a small grid that plays the settings before they are saved ---- */
const PREVIEW_PEOPLE=['a','b','c','d','e'],PREVIEW_DAYS=['1','2','3','4','5'],PREVIEW_DUTIES:Duty[]=[{code:'C',hue:'teal',name:'Clinic',time:''},{code:'UC',hue:'sky',name:'Urgent care',time:''},{code:'N',hue:'violet',name:'Night',time:''}];
export function preview(host:HTMLElement,settings:()=>AnimationSettings){
  const shift=(d:string,k:number)=>`${d}:${k}`;
  const ctx:Ctx={shift:()=>({id:'',revision:0,start:'2026-01-01T08:00:00Z',end:'2026-01-01T16:00:00Z'}),day:s=>s.split(':')[0],duty:s=>PREVIEW_DUTIES[Number(s.split(':')[1])],name:p=>p.toUpperCase(),label:p=>p.toUpperCase()};
  const start=[key(shift('1',0),'a'),key(shift('1',1),'d'),key(shift('2',0),'b'),key(shift('3',2),'a'),key(shift('3',0),'c'),key(shift('4',0),'a'),key(shift('5',1),'b'),key(shift('5',0),'e')];
  // Each step is one better schedule: a fill from Open places, a swap, a far and a near move, a place left open, a shift change.
  const steps:[string[],string[]][]=[[[key(shift('2',1),'e')],[]],[[key(shift('1',0),'d'),key(shift('1',1),'a')],[key(shift('1',0),'a'),key(shift('1',1),'d')]],
    [[key(shift('4',0),'e')],[key(shift('4',0),'a')]],[[key(shift('3',0),'d')],[key(shift('3',0),'c')]],[[],[key(shift('5',1),'b')]],[[key(shift('2',2),'b')],[key(shift('2',0),'b')]]];
  const open:Record<string,number>={'1':0,'2':2,'3':0,'4':1,'5':0};
  const draw=()=>{
    const grid:LiveHost=el('div',{class:'people-grid asl-preview-grid','aria-hidden':'true'});grid.style.setProperty('--days',String(PREVIEW_DAYS.length));
    // Its sample duties (C, UC, N) and open places in the shared key, drawn and animated through the grid's stamp, as the
    // Schedule's grids do (every mark has one).
    const key=legend('Key');
    grid.stamp=(n,s,_p)=>{if(s===null){markOpen(key,n,el('span',{class:'pg-chip pg-open lg-mark','aria-hidden':'true'},el('b',{},'+1')));return;}
      const u=ctx.duty(s);key.mark(n,'t:'+u.code,el('span',{class:`pg-chip duty-${u.hue}`,'aria-hidden':'true'},el('b',{},u.code)),u.name,0);};
    const cells=(p:string|null)=>PREVIEW_DAYS.map(d=>{const cell=el('div',{class:'pg-cell','data-day':d});
      if(p===null&&open[d])cell.append((n=>(grid.stamp!(n,null,''),n))(el('span',{class:'pg-chip pg-open','data-open':String(open[d])},el('b',{},'+'+open[d]))));
      if(p)for(const k of start){const [s,who]=k.split('|');if(who===p&&s.split(':')[0]===d){const u=ctx.duty(s),n=el('span',{class:`pg-chip duty-${u.hue}`,'data-session':s,'data-person':p},el('b',{},u.code));grid.stamp!(n,s,p);cell.append(n);}}
      return cell;});
    grid.append(el('div',{class:'pg-row pg-open-row'},el('div',{class:'pg-name'},el('strong',{},'Open places')),...cells(null),el('div',{class:'pg-hours'})),
      ...PREVIEW_PEOPLE.map(p=>el('div',{class:'pg-row person-group','data-row-person':p},el('div',{class:'pg-name'},el('strong',{},p.toUpperCase())),...cells(p),el('div',{class:'pg-hours'}))));
    host.replaceChildren(grid,key.root);return grid;};
  let timer=0;
  const replay=()=>{window.clearTimeout(timer);const grid=draw(),view=peopleView(grid,ctx),f=frameBatcher();
    let shown=new Set(start),i=0;
    const next=()=>{if(!grid.isConnected||i>=steps.length)return;const s=settings(),[adds,removes]=steps[i++],to=new Set(shown);
      removes.forEach(k=>to.delete(k));adds.forEach(k=>to.add(k));play(view,diff(shown,to,ctx.day),s,f);shown=to;
      timer=window.setTimeout(next,Math.max(700,1000/s.max_updates_per_second));};
    timer=window.setTimeout(next,300);};
  replay();
  return replay;
}
