/** Loading states: the one helper every screen uses. Nothing is shown for waits under 300 ms;
 *  once shown a state stays at least 400 ms (no flicker); content then fades in, rows ~80 ms apart. Waits are marked
 *  aria-busy and said once in one polite live region. Reduced motion keeps every state and drops the movement (CSS).
 *  No imports: api.ts and dom.ts both use it. */
// RETRY_AFTER: measured on dev under load (6 Nov 2026): reads p95 3.6 s; 15 s is about four times that. It bounds a
// pressed Retry (flight) and is when a skeleton starts saying how long it has waited (STILL_LOADING).
// READ_TIMEOUT: a read with no answer by then is abandoned (api.ts request aborts it) and its area says WAIT_WORDS.timedOut
// with Retry. DEV a5dd59837 (11 Nov 2026) measured cold reads of 24 s (rules catalogue) and 42 s (score review): a minute
// still lets those finish; the slow server is being fixed, not hidden under a shorter limit.
export const SHOW_AFTER=300,MIN_SHOWN=400,SLOW_AFTER=3000,RETRY_AFTER=15000,READ_TIMEOUT=60_000;
/** Reads whose measured time exceeds READ_TIMEOUT get their own limit, set once here (never per call site).
 *  shortcut: the rules catalogue read took 48–66 s on DEV ada47c964 (cold build, the cache not hitting), so 60 s killed
 *  working reads; 180 s until the cache-hit slice makes it sub-second, then set it from the measured p99 plus margin. */
export const SLOW_READS:readonly [RegExp,number][]=[[/^\/api\/rules\/catalogue(\?|$)/,180_000]];
export const readTimeout=(path:string)=>SLOW_READS.find(([p])=>p.test(path))?.[1]??READ_TIMEOUT;
/** The words of a long wait (Design Lead DL-045), one place (terms.ts re-exports them): past RETRY_AFTER a skeleton says
 *  "Still loading · 0:18"; a live Retry is offered only for an idempotent read the caller can replace, and late answers
 *  are dropped; past READ_TIMEOUT its area says "Didn’t load in 1:00." with
 *  Retry. startup.ts (before any module) keeps the same words, pinned by tests/test_web_loading.py. */
export const WAIT_WORDS={slow:'Still loading',timedOut:'Didn’t load in'} as const;
/** "Didn’t load in 1:00.": the limit is READ_TIMEOUT, formatted, never typed. */
export const timedOutWords=(limit=READ_TIMEOUT)=>`${WAIT_WORDS.timedOut} ${clock(limit)}.`;
const now=()=>performance.now();
const node=(tag:string,cls:string,text='')=>{const n=document.createElement(tag);n.className=cls;if(text)n.textContent=text;return n;};

/* maintenance: the client's one ResizeObserver (tests/test_web_ssot_guards.py keeps it the only one). `run(el, size)` runs in
 * the animation frame after `el`'s border box changed size, never inside the observer's delivery: a callback that
 * resizes an observed box there is the "ResizeObserver loop completed with undelivered notifications" error. It skips a
 * size (whole pixels) it already ran for, and a box off the page: one built off-page runs once it is shown, one that was
 * shown and has left is let go. Returns the stop. (Here, not dom.ts: this module has no imports, so api.ts can use it.) */
type SizeRun=(el:Element,size:{width:number;height:number})=>void;
type Sized={runs:Map<SizeRun,string>;size:{width:number;height:number};key:string;shown?:boolean;attached?:boolean;ref:WeakRef<Element>};
const sized=new WeakMap<Element,Sized>();let sizes:ResizeObserver|null=null,sizesDue=new Set<Element>(),sizesFrame=0;
// Every box observed now. A box observed while hidden (0×0: display:none, a closed tab) and removed while still hidden
// is never reported, so the observer would hold it for good: once a frame, one that was on the page and has left goes.
// Weakly: a box built off-page that left before any sweep saw it mounted must not keep its old page alive (SC-01).
const observed=new Set<WeakRef<Element>>();let sweepDue=false;
const letGo=(el:Element)=>{const s=sized.get(el);if(s)observed.delete(s.ref);sized.delete(el);sizes?.unobserve(el);};
function sweep(){sweepDue=false;for(const ref of observed){const el=ref.deref(),s=el&&sized.get(el);if(!el||!s){observed.delete(ref);continue;}
  if(el.isConnected)s.attached=true;else if(s.attached||s.shown)letGo(el);}}
function sizesLater(){if(!sizesFrame)sizesFrame=requestAnimationFrame(()=>{sizesFrame=0;const due=sizesDue;sizesDue=new Set();
  if(sweepDue)sweep();
  for(const el of due){const s=sized.get(el);if(!s)continue;
    if(!el.isConnected){if(s.shown)letGo(el);continue;}
    s.shown=true;for(const [run,ran] of [...s.runs])if(ran!==s.key&&s.runs.has(run)){s.runs.set(run,s.key);run(el,s.size);}}});}
export function observeSize(el:Element,run:SizeRun):()=>void{
  if(typeof ResizeObserver==='undefined')return ()=>{};
  sizes??=new ResizeObserver(list=>{for(const e of list){const s=sized.get(e.target);if(!s)continue;
    const b=e.borderBoxSize?.[0],width=Math.round(b?.inlineSize??e.contentRect.width),height=Math.round(b?.blockSize??e.contentRect.height);
    s.size={width,height};s.key=`${width}x${height}`;sizesDue.add(e.target);}
    if(sizesDue.size)sizesLater();});
  // A run wrapped per call, so the same function watching a box twice is two watches.
  const mine:SizeRun=(e,size)=>run(e,size);
  let s=sized.get(el);
  if(!s){sized.set(el,s={runs:new Map(),size:{width:0,height:0},key:'',attached:el.isConnected,ref:new WeakRef(el)});sizes.observe(el,{box:'border-box'});observed.add(s.ref);}
  else if(s.key){sizesDue.add(el);}  // already observed: the observer reports changes only, so hear this size
  sweepDue=true;sizesLater();
  s.runs.set(mine,'');
  // The stop holds its box and run weakly: one kept after the box left (a hook of a page left behind) never keeps that
  // page alive; `sized` holds the run as long as the box lives.
  const box=s.ref,watch=new WeakRef(mine);
  return ()=>{const b=box.deref(),m=watch.deref(),t=b&&sized.get(b);if(!b||!m||!t?.runs.delete(m)||t.runs.size)return;letGo(b);};
}

/** The timing rule. `show` runs once the wait passes 300 ms. `end()` resolves when what follows may appear: at once
 *  (false) when nothing was shown, else no sooner than 400 ms after it was shown, after `hide` ran (true). */
export function delayed(show:()=>void,hide:()=>void=()=>{}){
  let shownAt=-1,ended:Promise<boolean>|null=null;
  const timer=setTimeout(()=>{shownAt=now();show();},SHOW_AFTER);
  return {end():Promise<boolean>{
    if(ended)return ended;clearTimeout(timer);
    if(shownAt<0)return ended=Promise.resolve(false);
    return ended=new Promise(done=>setTimeout(()=>{hide();done(true);},Math.max(0,MIN_SHOWN-(now()-shownAt))));
  }};
}

let live:HTMLElement|null=null;
/** The one polite live region for waits ("Loading Schedule…") and for news that is no longer a toast's (a result that
 *  lands after the person moved on, maintenance); the same words twice in a row are said once. */
export function say(text:string){
  if(!live||!live.isConnected){live=node('div','sr-only');live.id='loading-live';live.setAttribute('role','status');live.setAttribute('aria-live','polite');document.body.append(live);}
  if(live.textContent!==text)live.textContent=text;
  // SW3-D-10: said, then cleared, so a screen reader reading the page later never finds "Loading …" after the load.
  const region=live;setTimeout(()=>{if(region.textContent===text)region.textContent='';},LIVE_CLEAR_MS);
}
/** How long a live message stays in its region once said (long enough to be spoken, short enough not to go stale). */
export const LIVE_CLEAR_MS=5000;

/** The 2 px accent bar under the top bar for any page read, command or page change that takes longer than 300 ms
 *  (indeterminate). The shell puts it in the top bar; it is decoration (aria-hidden): skeletons and buttons speak. */
// Made on first use: modules that import this one also run without a page (node checks of planner_logic).
let bar:HTMLElement|null=null;
export function topProgress(){if(!bar){bar=node('div','top-progress');bar.setAttribute('aria-hidden','true');}return bar;}
let waits=0,barTiming:{end():Promise<boolean>}|null=null;
const hideBar=()=>{if(!waits)topProgress().classList.remove('on');};
export function track<T>(work:Promise<T>):Promise<T>{
  // A wait that starts while the bar is still on (its 400 ms) keeps it on, without a new 300 ms gap.
  if(waits++===0)barTiming=topProgress().classList.contains('on')?{end:async()=>{hideBar();return true;}}:delayed(()=>topProgress().classList.add('on'),hideBar);
  const done=()=>{if(--waits===0)void barTiming?.end();};
  work.then(done,done);return work;
}

/** One run of `once` at a time (the app refresh). join() (a pulse, a save's follow-up) shares the run on its way and runs
 *  once more after it, so the last state lands. retry() (a person's Retry) never joins a run it did not start: that run
 *  (perhaps hung) is abandoned (`stale()` turns true, so its late answer is dropped) and exactly one new run starts. A
 *  second Retry within RETRY_AFTER of the first joins it (no stacking); its promise, which keeps the Retry button disabled
 *  (dom.ts button), settles when the run does or after RETRY_AFTER, so a hung run never leaves Retry dead. */
export function flight(once:(stale:()=>boolean)=>Promise<void>,more:()=>boolean=()=>true){
  let current:Promise<void>|null=null,again=false,runs=0,retried:{run:Promise<void>;at:number}|null=null;
  const start=()=>{const mine=++runs,stale=()=>mine!==runs;
    // Cleared in the same step as the last run, so one asked for as it ends starts anew instead of joining it.
    const run=(async()=>{try{do{again=false;await once(stale);}while(again&&more()&&!stale());}finally{if(!stale())current=null;}})();
    return current=run;};
  return {join():Promise<void>{if(current){again=true;return current;}return start();},
    retry():Promise<void>{const at=now();if(!(retried&&current===retried.run&&at-retried.at<RETRY_AFTER))retried={run:start(),at};
      const left=RETRY_AFTER-(at-retried.at);return Promise.race([retried.run,new Promise<void>(done=>setTimeout(done,left))]);}};
}
/** SW3-B-01: a Retry asks again, never joins the read already on its way (api.ts `get` shares one in-flight read per
 *  path, so a Retry that joined it did nothing until the slow answer came). api.ts registers how reads start afresh. */
let afresh=()=>{};
export const setRetryAfresh=(f:()=>void)=>{afresh=f;};
/** A Retry's action: shared reads are let go first (so it asks afresh), then `retry` reads again. */
export const retrying=(retry:()=>unknown)=>()=>{afresh();return retry();};
export type Shape='table'|'cards'|'calendar'|'panel'|'lines'|'line'|'timeline'|'boot';
// `line`: one bar where one line of text will be (a row's description, a hover card, a list item).
// `boot`: start-up, before any page has a layout: one thin accent line under the centred emblem.
const ROWS:Record<Shape,[number,number]>={table:[8,4],cards:[6,3],calendar:[35,1],panel:[3,3],lines:[3,1],line:[1,1],timeline:[8,2],boot:[1,1]};
/** A page's skeleton (`fill`): the most rows a tall screen fits (calendar weeks, card rows of 4), clipped to its room. */
const FILL_ROWS:Partial<Record<Shape,number>>={table:40,calendar:12,cards:8,timeline:40};
const MIN_FILL=240;
/** A skeleton in the real layout (table rows, cards, a calendar month, panel sections or a few lines) — never a blank
 *  page or a bare "Loading…". Invisible for its first 300 ms. `fill` (a page's skeleton): it takes the content area's
 *  height, measured from where it sits down to the footer (ResizeObserver), its rows repeating to fill it. While it waits
 *  nothing is drawn over it reviewed: the moving skeleton says it, and the words are for screen readers only.
 *  Past RETRY_AFTER one quiet line in flow at its top (under the bar at start-up), "Still loading · 0:18", its clock
 *  ticking each second; said once. A caller may provide a Retry only for an idempotent read it can replace; its late
 *  answer is dropped; otherwise the wait ends when the read does (data, an empty state, or readFailed: a failure, or
 *  READ_TIMEOUT's "Didn’t load in 1:00.", each with Retry). The skeleton keeps its
 *  height, so nothing moves. `label` is the skeleton's accessible name and what the live region says. Replace it with
 *  settle(). */
export function skeleton(shape:Shape,label='Loading…',o:{fill?:boolean;retry?:()=>unknown}={}):HTMLElement{
  const box=node('div',`loading sk-${shape} sk-wait`);box.setAttribute('aria-busy','true');box.dataset.born=String(now());
  box.append(node('span','sr-only',label));
  const fill=!!o.fill&&shape in FILL_ROWS,[base,lines]=ROWS[shape],grid=node('div','sk-grid');grid.setAttribute('aria-hidden','true');
  // Enough rows for a tall screen; the box's measured height clips them (a short one shows fewer, never a gap below).
  const items=fill?FILL_ROWS[shape]!*(shape==='calendar'?7:shape==='cards'?4:1):base;
  if(shape==='table')grid.append(node('span','sk sk-tools'));
  if(shape==='calendar')for(let d=0;d<7;d++)grid.append(node('span','sk sk-head'));
  for(let i=0;i<items;i++){const item=node('div','sk-item');for(let j=0;j<lines;j++)item.append(node('span','sk'));grid.append(item);}
  box.append(grid);
  const later=(ms:number,run:()=>void)=>setTimeout(()=>{if(box.getAttribute('aria-busy'))run();},ms);
  let measure=()=>{};
  if(fill){box.classList.add('sk-fill');
    let unwatch=()=>{};
    const stop=()=>{unwatch();removeEventListener('resize',measure);};
    measure=()=>{if(!box.isConnected||!box.getAttribute('aria-busy')){if(!box.getAttribute('aria-busy'))stop();return;}
      const top=Math.max(0,box.getBoundingClientRect().top),foot=document.querySelector('.app-footer')?.getBoundingClientRect().top||innerHeight;
      box.style.setProperty('--sk-room',Math.max(MIN_FILL,Math.min(innerHeight,foot)-top-12)+'px');};
    unwatch=observeSize(document.documentElement,()=>measure());addEventListener('resize',measure);requestAnimationFrame(()=>measure());}
  later(SHOW_AFTER,()=>{box.classList.remove('sk-wait');measure();if(box.isConnected)say(label);});
  // D-1, DL-045: past RETRY_AFTER one line says how long it has waited, in flow; the clock is not re-announced.
  later(RETRY_AFTER,()=>{if(!box.isConnected)return;const born=Number(box.dataset.born),time=node('span','elapsed-time');
    time.setAttribute('aria-hidden','true');const line=node('p','sk-slow',WAIT_WORDS.slow+' · ');line.append(time);
    const tick=()=>{if(!box.isConnected||!box.getAttribute('aria-busy')){clearInterval(timer);return;}time.textContent=clock(now()-born);};
    const timer=setInterval(tick,1000);tick();
    const again=o.retry;
    if(again&&shape!=='boot'){const run=retrying(again),b=node('button','link-button','Retry') as HTMLButtonElement;b.type='button';
      b.addEventListener('click',()=>{const hadFocus=document.activeElement===b,parent=box.parentElement;b.disabled=true;
        void Promise.resolve(run()).finally(()=>{b.disabled=false;if(hadFocus&&parent?.isConnected){parent.tabIndex=-1;parent.focus({preventScroll:true});}});});line.append(' · ',b);}
    // The line takes room from the skeleton's own rows (clipped), never from what sits below it.
    if(shape==='boot')box.append(line);else{const h=box.offsetHeight;box.prepend(line);
      if(!fill)box.style.setProperty('height',Math.max(h,line.offsetTop+line.offsetHeight+8)+'px');}
    say(WAIT_WORDS.slow);});
  return box;
}
/** When the skeleton may give way: at once if it never showed (false), else once it has been shown 400 ms (true). */
export function settle(sk:Element|null):Promise<boolean>{
  const age=sk instanceof HTMLElement&&sk.dataset.born?now()-Number(sk.dataset.born):0;
  // The wait is over: a status region still saying "Loading …" would mislead screen-reader users who read it later.
  const done=()=>{sk?.removeAttribute('aria-busy');if(live&&live.textContent===sk?.querySelector(':scope>.sr-only')?.textContent)live.textContent='';};
  if(age<SHOW_AFTER){done();return Promise.resolve(false);}
  return new Promise(ok=>setTimeout(()=>{done();ok(true);},Math.max(0,SHOW_AFTER+MIN_SHOWN-age)));
}
/** Content that replaced a shown skeleton fades in as one block (P-07: staggered rows left the table half blank). */
export function reveal(content:Element){
  for(const p of content.children){p.classList.add('fade-in');(p as HTMLElement).style.setProperty('--i','0');}
}
/** Put `content` where a skeleton is, once settle() allows, fading in if the skeleton showed. A host that changed
 *  meanwhile (its skeleton gone) is left alone. */
export async function swap(host:Element,...content:Node[]){
  const sk=host.querySelector(':scope>.loading');const shown=await settle(sk);
  if(sk&&!sk.isConnected)return;host.replaceChildren(...content);if(shown)reveal(host);
}

/** A spinner line for work being done (not data being read): "Checking 40 people against every active rule…". */
export function working(text:string){const n=node('div','wk-loading',text);n.setAttribute('role','status');const s=node('span','spinner');s.setAttribute('aria-hidden','true');n.prepend(s);return n;}
/** Elapsed time as m:ss ("1:12", "12:05"); never an estimate of what is left. */
export const clock=(ms:number)=>{const s=Math.max(0,Math.floor(ms/1000));return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;};
/** Long work (a rota check): "Checking the rota · 1:12", the time since `since` (epoch ms) ticking each second, and
 *  Cancel when given. No ETA. The status is said once; the ticking clock is not re-announced. */
export function elapsed(text:string,since:number,cancel?:()=>void){
  const time=node('span','elapsed-time'),line=node('span','wk-loading long-work');line.setAttribute('role','status');
  const s=node('span','spinner');s.setAttribute('aria-hidden','true');time.setAttribute('aria-hidden','true');
  line.append(s,text+' · ',time);
  const tick=()=>{if(line.isConnected||!time.textContent){time.textContent=clock(Date.now()-since);}else clearInterval(timer);};
  const timer=setInterval(tick,1000);tick();
  if(cancel){const b=node('button','link-button','Cancel') as HTMLButtonElement;b.type='button';b.setAttribute('aria-label','Cancel: '+text.toLowerCase());b.addEventListener('click',()=>{clearInterval(timer);cancel();});line.append(' ',b);}
  return line;
}

// shortcut: the first word of a button's label becomes its busy label; labels that start with another word keep their
// text (spinner only). Add verbs here as buttons need them.
const VERBS:Record<string,string>={Apply:'Applying',Save:'Saving',Publish:'Publishing',Submit:'Submitting',Approve:'Approving',
  Reject:'Rejecting',Delete:'Deleting',Remove:'Removing',Create:'Creating',Add:'Adding',Run:'Running',Import:'Importing',Export:'Exporting',
  Send:'Sending',Assign:'Assigning',Replace:'Replacing',Check:'Checking',Propose:'Proposing',Update:'Updating',Activate:'Activating',
  Archive:'Archiving',Restore:'Restoring',Duplicate:'Duplicating',Reopen:'Reopening',Withdraw:'Withdrawing',Accept:'Accepting',
  Decline:'Declining',Discard:'Discarding',Undo:'Undoing',Reset:'Resetting',Generate:'Generating',Invite:'Inviting',Copy:'Copying',Retry:'Retrying'};
export function busyLabel(label:string){const [first,...rest]=label.trim().replace(/…$/,'').split(' ');const ing=VERBS[first];return ing?[ing,...rest].join(' ')+'…':label;}
/** A button doing its work: at once aria-busy and disabled (no double press); after 300 ms a spinner replaces its icon
 *  and its label reads "Applying…". The returned function restores it (after the 400 ms minimum). The label is read
 *  when the spinner shows, not at the press: one rewritten meanwhile (settings arriving) is kept, never put back.
 *  Releasing twice restores once. */
export function busy(b:HTMLButtonElement,label?:string):()=>Promise<void>{
  b.setAttribute('aria-busy','true');b.disabled=true;
  let before:ChildNode[]=[];const spin=node('span','spinner');spin.setAttribute('aria-hidden','true');
  const t=delayed(()=>{before=[...b.childNodes];b.classList.add('is-busy');b.replaceChildren(spin,label??(b.dataset.busy||busyLabel(b.textContent||'')));});
  // SW-12: work past SLOW_AFTER says how long it has run ("Publishing… · 0:12"), the same clock as elapsed(), for every
  // long action (submit, publish, apply, approve) from this one place.
  const start=Date.now(),time=node('span','elapsed-time');time.setAttribute('aria-hidden','true');
  const timer=setInterval(()=>{const ms=Date.now()-start;if(ms<SLOW_AFTER||b.firstChild!==spin||!b.isConnected)return;
    if(!time.isConnected)b.append(time);time.textContent=' · '+clock(ms);},1000);  // hidden: the button's name stays its label
  let done:Promise<void>|undefined;
  return ()=>done??=(async()=>{clearInterval(timer);await t.end();if(b.firstChild===spin)b.replaceChildren(...before);b.classList.remove('is-busy');b.removeAttribute('aria-busy');b.disabled=false;})();
}

/** The sign-in card fades out (~150 ms) over the app frame that replaced it. */
export function fadeOut(card:HTMLElement){
  card.removeAttribute('id');card.classList.add('fade-out');card.setAttribute('aria-hidden','true');card.inert=true;document.body.append(card);
  setTimeout(()=>card.remove(),160);
}

/** Optimistic changes, one look everywhere: the change shows at once and what is still saving carries a small spinner
 *  and a dotted underline; ✓ once confirmed; a row that failed puts itself back and says "Couldn't save · Retry"
 *  (no Retry for a conflict: the record changed, so the page is read again instead). One summary toast says the rest. */
export function savingMark(text:string){const n=node('span','save-mark saving');n.setAttribute('aria-busy','true');const s=node('span','spinner');s.setAttribute('aria-hidden','true');n.append(s,node('span','saving-text',text));return n;}
export const savedMark=(text:string)=>{const n=node('span','save-mark saved');const t=node('span','','✓');t.setAttribute('aria-hidden','true');n.append(t,' '+text);return n;};
export function failedMark(retry?:()=>void){
  const n=node('span','save-mark failed',"Couldn't save");
  if(retry){const b=node('button','link-button','Retry') as HTMLButtonElement;b.type='button';b.addEventListener('click',retry);n.append(' · ',b);}
  return n;
}
/** A refusal because the record changed meanwhile (stale revision): Retry would be refused the same way. */
export const conflict=(e:unknown)=>String((e as {code?:string})?.code||'').startsWith('STALE_');
