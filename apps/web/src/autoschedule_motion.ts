/* Auto-schedule live animation, the parts with no DOM (unit tests run them in Node): what changed between two
 * schedules, how each change is shown, and one batch of reads and writes per frame. The DOM side is autoschedule_live.ts.
 * Both load only when a run is shown or the settings preview plays. */
import type {AnimationSettings,LivePair} from './generation_options.js';

export type Style='fly'|'roll'|'fade'|'retype'|'none';
/** One change between the schedule shown and the next one:
 *  swap   s1 had a, s2 had b → s1 has b, s2 has a (same day);
 *  shift  person p moved from session `from` to `to` (same day);
 *  person the place on session s went from a to b;
 *  fill   an open place on s got p; leave  p left s and the place is open. */
export type Op={t:'swap';s1:string;s2:string;a:string;b:string}|{t:'shift';p:string;from:string;to:string}|{t:'person';s:string;from:string;to:string}
  |{t:'fill';s:string;p:string}|{t:'leave';s:string;p:string};
export const key=(s:string,p:string)=>s+'|'+p;
const split=(k:string):LivePair=>{const i=k.indexOf('|');return {shift:k.slice(0,i),person:k.slice(i+1)};};

/** Every change from `shown` to `next` (sets of "shift|person"), paired so a move reads as one move, not a leave and a fill.
 *  `day` gives a session's day: only changes on the same day pair (a person works one place a day). Linear in the changes. */
export function diff(shown:ReadonlySet<string>,next:ReadonlySet<string>,day:(shift:string)=>string):Op[]{
  const adds=[...next].filter(k=>!shown.has(k)).map(split),removes=[...shown].filter(k=>!next.has(k)).map(split);
  const addKeys=new Set(adds.map(x=>key(x.shift,x.person))),gone=new Set<string>(),used=new Set<string>(),ops:Op[]=[];
  const index=(list:LivePair[],by:'shift'|'person')=>{const m=new Map<string,LivePair[]>();for(const x of list)m.set(x[by],[...(m.get(x[by])||[]),x]);return m;};
  const addsByShift=index(adds,'shift'),addsByPerson=index(adds,'person'),removesByPerson=index(removes,'person');
  const free=(x:LivePair,set:Set<string>)=>!set.has(key(x.shift,x.person));
  // 1 · Swaps: a left s1 for s2 while b left s2 for s1.
  for(const r of removes){if(!free(r,gone))continue;
    for(const b of addsByShift.get(r.shift)||[]){if(!free(b,used)||b.person===r.person)continue;
      const r2=(removesByPerson.get(b.person)||[]).find(x=>x.shift!==r.shift&&free(x,gone)&&day(x.shift)===day(r.shift)&&addKeys.has(key(x.shift,r.person))&&!used.has(key(x.shift,r.person)));
      if(!r2)continue;
      gone.add(key(r.shift,r.person));gone.add(key(r2.shift,r2.person));used.add(key(r.shift,b.person));used.add(key(r2.shift,r.person));
      ops.push({t:'swap',s1:r.shift,s2:r2.shift,a:r.person,b:b.person});break;}}
  // 2 · The same person on another session that day.
  for(const r of removes){if(!free(r,gone))continue;
    const a=(addsByPerson.get(r.person)||[]).find(x=>free(x,used)&&day(x.shift)===day(r.shift));
    if(a){gone.add(key(r.shift,r.person));used.add(key(a.shift,a.person));ops.push({t:'shift',p:r.person,from:r.shift,to:a.shift});}}
  // 3 · The same place given to someone else.
  for(const r of removes){if(!free(r,gone))continue;
    const a=(addsByShift.get(r.shift)||[]).find(x=>free(x,used));
    if(a){gone.add(key(r.shift,r.person));used.add(key(a.shift,a.person));ops.push({t:'person',s:r.shift,from:r.person,to:a.person});}}
  // 4 · The rest: places filled, places left open.
  for(const a of adds)if(free(a,used))ops.push({t:'fill',s:a.shift,p:a.person});
  for(const r of removes)if(free(r,gone))ops.push({t:'leave',s:r.shift,p:r.person});
  return ops;
}
/** How many of the first changes can share a frame: each looks its chips up before the frame writes, so a change on a
 *  session an earlier one in the frame touches waits for the next frame (one diff never repeats a session; a stream of
 *  several can). */
export function runOf(ops:Op[]):number{
  const seen=new Set<string>();
  for(let i=0;i<ops.length;i++){const o=ops[i],ss=o.t==='swap'?[o.s1,o.s2]:o.t==='shift'?[o.from,o.to]:[o.s];
    if(ss.some(s=>seen.has(s)))return i;ss.forEach(s=>seen.add(s));}
  return ops.length;
}
/** How many duties an op changes (a swap changes two). */
export const dutiesChanged=(ops:Op[])=>ops.reduce((n,o)=>n+(o.t==='swap'?2:1),0);

/** Whether a change travels across the grid in this view (People: rows are people; Week: rows are sessions). */
export function travels(op:Op,view:'people'|'week'):boolean{
  return view==='people'?op.t==='swap'||op.t==='fill'||op.t==='person':op.t==='swap'||op.t==='shift';
}
/** The style one change is shown in. Reduced motion always fades, with no flights (not configurable). Hybrid flies the
 *  big moves its settings name and shows the rest in place; `rows` is how far a place moved in the People view. */
export function styleOf(op:Op,o:{settings:AnimationSettings;view:'people'|'week';rows?:number;reduced:boolean}):Style{
  const {settings:s}=o;
  if(s.mode==='off')return 'none';
  if(o.reduced)return 'none';  // reduced motion: every change settles at once
  const far=travels(op,o.view);
  if(s.mode==='fly')return far?'fly':'fade';
  if(s.mode!=='hybrid')return s.mode;
  const b=s.big_move,big=far&&(op.t==='swap'?b.swaps:op.t==='fill'?b.fills_from_open:op.t==='shift'?b.cross_session:op.t==='person'?(o.rows??0)>=b.min_row_distance:false);
  return big?s.big_style:s.small_style;
}
/** In place of a flight over the cap: the small style in Hybrid, otherwise a fade. */
export const fallback=(s:AnimationSettings):Style=>s.mode==='hybrid'?s.small_style:'fade';

/** One frame of work: every job's first read, then every write, then every last read, then every animation start — two
 *  layouts per frame however many chips move (interleaving reads and writes halved the frame rate in the mockup). */
export type FrameJob={first?:()=>void;write?:()=>void;last?:()=>void;play?:()=>void};
export function frameBatcher(raf:(run:()=>void)=>unknown=f=>requestAnimationFrame(f)){
  let jobs:FrameJob[]=[],queued=false;
  const run=()=>{queued=false;const batch=jobs;jobs=[];
    for(const j of batch)j.first?.();for(const j of batch)j.write?.();for(const j of batch)j.last?.();for(const j of batch)j.play?.();};
  return {add(job:FrameJob){jobs.push(job);if(!queued){queued=true;raf(run);}},get pending(){return jobs.length;}};
}

/** The live preview's model: `target` plus the changes still queued is always the solver's state at `at` (0 = the draft).
 *  Events arrive in any order and with gaps (slow polls, the 50-event buffer, truncation); the ones after a gap wait in
 *  `later` until a complete snapshot fills it, so the cursor never runs ahead of what is installed. */
type Change={adds:LivePair[];removes:LivePair[]};
export function liveModel(base:Iterable<string>,keep:(shift:string)=>boolean){
  const target=new Set(base),queue:Change[]=[],later=new Map<number,Change>();
  // Solver snapshots omit inactive sessions; their existing duties stay on screen.
  const fixed=[...target].filter(k=>!keep(split(k).shift));
  let at=0,seen=0;
  const drain=()=>{for(let e=later.get(at+1);e;e=later.get(at+1)){later.delete(++at);queue.push(e);}
    for(const s of later.keys())if(s<=at)later.delete(s);};
  return {target,queue,
    /** Events seen but not installed: the grid holds still until a snapshot catches up. */get lost(){return seen>at;},
    /** The highest event seen (the solver's better schedules so far). */get seen(){return seen;},
    observe(events:{seq:number;adds:LivePair[];removes:LivePair[];truncated:boolean}[]){
      for(const e of events){seen=Math.max(seen,e.seq);if(e.seq>at&&!e.truncated)later.set(e.seq,{adds:e.adds,removes:e.removes});}drain();},
    /** A complete snapshot replaces the preview (an emptied session included); events after it then replay. */
    install(sol:{seq:number;pairs:LivePair[]}){if(sol.seq<=at)return;
      target.clear();for(const k of fixed)target.add(k);for(const p of sol.pairs)if(keep(p.shift))target.add(key(p.shift,p.person));
      queue.length=0;queue.push({adds:[],removes:[]});at=sol.seq;seen=Math.max(seen,at);drain();},
    /** Moves the next `n` queued changes into `target`. */
    take(n=queue.length){for(const e of queue.splice(0,n)){
      for(const r of e.removes)if(keep(r.shift))target.delete(key(r.shift,r.person));for(const a of e.adds)if(keep(a.shift))target.add(key(a.shift,a.person));}}};
}

/** Quadratic arc from (dx,dy) home to (0,0), with a slight lift at the middle: the FLIP "invert, then play". */
export function arcFrames(dx:number,dy:number,bow:number,speed:number){
  const d=Math.hypot(dx,dy)||1,nx=-dy/d,ny=dx/d,lift=Math.min(56,10+d*.2)*bow,cx=dx/2+nx*lift,cy=dy/2+ny*lift-Math.min(14,d*.05),frames:Keyframe[]=[];
  for(let k=0;k<=14;k++){const t=k/14,u=1-t,x=u*u*dx+2*u*t*cx,y=u*u*dy+2*u*t*cy,sc=1+.14*Math.sin(Math.PI*t);
    frames.push({transform:`translate(${x.toFixed(1)}px,${y.toFixed(1)}px) scale(${sc.toFixed(3)})`});}
  return {frames,duration:Math.min(900,Math.max(480,440+d*.8))*speed};
}
