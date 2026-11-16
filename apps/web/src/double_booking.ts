import {el,icon,button,check,note,notify,popover,showDialog,formatDay,formatTime,departmentTimeZone,plural} from './dom.js';
import {command,request,ApiError,knownRevision} from './api.js';
import type {SeriesOverlapConflict as Clash,OverlapCancelAction,SeriesOverlapPreview} from './generated.js';
import type {Row} from './types.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)

/** Double-booked people. The server's one overlap evaluator finds every clash and offers
 *  the commands that cancel one side; this module only words them and runs those commands, the same on every screen:
 *  the recurring-session form (live, before saving), the schedule's sessions and side panels, and rota-check findings. */
export type {Clash};
const when=(c:Clash)=>{const z=departmentTimeZone();return `${formatDay(c.date,z)} ${formatTime(c.start,z)}–${formatTime(c.end,z)}`;};
/** "Dr Jules Evans is already in Autism, Tue 29 Sep 08:30–12:00" (`both`: names both sessions, for a finding). */
export function clashText(c:Clash,both=false):string{
  const what=c.code==='EXTERNAL_ROTATION'?'is away on another rotation'
    :both?`is booked in both ${c.session_name} and ${c.other_session_name}`:`is already in ${c.other_session_name}`;
  // Leave reads as the server words it for this reader (the department's leave display setting; "Unavailable" when
  // hidden): nothing is added here.
  return c.code==='UNAVAILABLE'?`${c.person_name}: ${c.label}, ${when(c)}`:`${c.person_name} ${what}, ${when(c)}`;
}
const isOther=(a:OverlapCancelAction)=>a.side==='other';
/** Positively an occurrence not saved as a session yet: the server offers its template's cancel, or its series is not
 *  saved yet. Only such a side may be cancelled locally (an excluded date); any other may be a saved session (locked,
 *  submitted or published sessions come with no command), and excluding its date would only clear the preview. */
export const unsaved=(c:Clash,side:'this'|'other')=>(c.cancel_actions||[]).some(a=>a.side===side&&a.kind==='series.occurrence.cancel')
  ||(side==='this'?!c.series_id&&c.shift_id.startsWith('series:'):!c.other_series_id&&!!c.other_shift_id?.startsWith('series:'));
/** After a cancel: the open draft's rota check runs again if the change made it out of date (app.ts sets it, debounced). */
let recheck:()=>Promise<void>=async()=>{};
export const setClashRecheck=(run:()=>Promise<void>)=>{recheck=run;};

/** The server's cancel commands for one clash: each acts at once and Undo comes with the success message
 *  (api.ts UNDO). `own`/`other`: a change still being edited cancels that side locally (an excluded date in its pending
 *  value, saved with the change), used instead of the server's command, which would save that series on its own. */
export function cancelButtons(c:Clash,after:()=>unknown,own?:()=>void,other?:()=>void,ownAsked=false):HTMLElement[]{
  const actions=((c.cancel_actions||[]) as OverlapCancelAction[]).filter(a=>isOther(a)?!other:!own);
  const named=(label:string,other:boolean)=>`${label}: ${other?c.other_session_name:c.session_name}, ${when(c)}`;
  const out:HTMLElement[]=actions.map(a=>button(CLOSE[a.side],async()=>{
    // A draft session's cancel comes without a revision (the stable session detail): the rota's newest one is sent.
    const revision=a.expected_revision??(a.kind==='shift.action'?knownRevision(a.payload.workspace_id):undefined);
    await command(a.kind,{...a.payload,reason:`Double booking: ${clashText(c,true)}`.slice(0,500)},a.target_id??undefined,revision);
    notify(`${isOther(a)?c.other_session_name:c.session_name} on ${formatDay(c.date,departmentTimeZone())} closed.`);
    await recheck();await after();},'secondary small',{'aria-label':named(CLOSE[a.side],isOther(a))}));
  if(other)out.unshift(button(CLOSE.other,other,'secondary small',{'aria-label':named(CLOSE.other,true)}));
  if(own)out.unshift(button(CLOSE.this,own,'secondary small',{'aria-label':named(CLOSE.this,false)}));
  // A change being edited whose clashing side is a saved session with no command for this reader: said, not hidden.
  else if(ownAsked&&!actions.some(a=>!isOther(a))){
    const off=button(CLOSE.this,()=>{},'secondary small',{'aria-label':named(CLOSE.this,false)});off.disabled=true;
    out.unshift(off,el('span',{class:'muted'},'Saved session: close it on its rota'));}
  return out;
}

/** One line per clash with its close buttons; `remove`: the person off this session instead (SW3-A-15), first. */
export function clashList(list:Clash[],after:()=>unknown,o:{both?:boolean;own?:(c:Clash)=>(()=>void)|undefined;other?:(c:Clash)=>(()=>void)|undefined;remove?:(c:Clash)=>(()=>void)|undefined}={}):HTMLElement{
  return el('ul',{class:'clash-list'},...list.map(c=>{const off=c.code==='TIME_OVERLAP'?o.remove?.(c):undefined;
    return el('li',{},el('span',{},clashText(c,o.both)),
      el('span',{class:'actions'},off?button(`Remove ${c.person_name} from this session`,off,'secondary small'):'',...cancelButtons(c,after,o.own?.(c),o.other?.(c),!!o.own)));}));
}
/** A session's flags (`all`: leave and rotations too, else the double bookings only, which the mark is about). */
const clashesOf=(s:Row,person?:string,all=false)=>((s.double_booking_conflicts||[]) as Clash[]).filter(c=>(all||c.code==='TIME_OVERLAP')&&(!person||c.person_id===person));
const headline=(n:number)=>n===1?'Double-booked: someone is on two sessions at the same time.':`Double-booked ${plural(n,'time')}: someone is on two sessions at the same time.`;
/** SX-16: the one name for cancelling a session or an occurrence, as everywhere else (Close the session). */
const CLOSE={this:'Close this session',other:'Close the other session'} as const;
/** A session's clashes in its side panel or details: said, explained and fixable there. `remove`: takes the double-booked
 *  person off this session (a draft the reader may change). */
export function clashNote(s:Row,after:()=>unknown,remove?:(c:Clash)=>(()=>void)|undefined):HTMLElement|null{
  const list=clashesOf(s,undefined,true),booked=list.filter(c=>c.code==='TIME_OVERLAP').length;if(!list.length)return null;
  const n=note('','warning');n.classList.add('clash-note');
  n.replaceChildren(el('strong',{},icon('alert'),' ',booked?headline(booked):'Someone on this session is away.'),clashList(list,after,{remove}));return n;
}
/** The mark on a session in the schedule (icon and words, never colour alone): hover says why, a click offers the fix. */
export function clashMark(s:Row,after:()=>unknown,now:()=>Row|undefined=()=>s):HTMLElement|null{
  const list=clashesOf(s);if(!list.length)return null;
  const words=list.map(c=>clashText(c)).join(' · ');
  const b=el('button',{type:'button',class:'clash-mark','data-tip':words,'aria-label':`Double-booked: ${words}`,...popup('dialog')},icon('alert'),el('span',{},'Double-booked'));
  marks.set(b,{now,after});
  return b;
}
// Every mark's click, in one listener (capture: before the grid's handlers): its clashes as the session has them now
// (`now`, a planner grid's held draft), not as drawn.
const marks=new WeakMap<HTMLElement,{now:()=>Row|undefined;after:()=>unknown}>();
// Node client tests stub `document` without listeners.
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('click',e=>{const b=e.target instanceof Element?e.target.closest<HTMLElement>('.clash-mark'):null;
  const m=b&&marks.get(b);if(!m)return;e.stopPropagation();const s=m.now(),list=s?clashesOf(s):[];if(!list.length)return;
  popover(b!,(panel,close)=>panel.append(el('div',{class:'clash-pop'},el('p',{class:'clash-head'},headline(list.length)),
    clashList(list,async()=>{close();await m.after();}))),{label:'Double-booked'});},true);
/** The same mark inside something already clickable (an agenda item, a timeline chip: `person`, only their clashes). */
export const clashTag=(s:Row,person?:string)=>clashesOf(s,person).length?el('span',{class:'clash-tag'},icon('alert'),'Double-booked'):null;
export {clashesOf};

/** A quick edit (a table cell) that would double-book someone: the same clashes and fixes in a dialog, and the
 *  acknowledgement. 'save': acknowledged; 'again': something was fixed, check again; 'back': not saved. */
export function clashDialog(list:Clash[],own:(c:Clash)=>(()=>void)|undefined,other?:(c:Clash)=>(()=>void)|undefined):Promise<'save'|'again'|'back'>{
  return new Promise(resolve=>{let answer:'save'|'again'|'back'='back';
    showDialog('Double bookings','This change would double-book someone. Close one session of each, or save with these double bookings.',(body,close)=>{
      const done=(a:typeof answer)=>{answer=a;close();};
      body.closest('dialog')!.addEventListener('close',()=>resolve(answer));
      body.append(el('div',{class:'notice warning clash-note'},el('strong',{},icon('alert'),' ',headline(list.length)),
          clashList(list,()=>done('again'),{own:c=>{const f=own(c);return f&&(()=>{f();done('again');});},other:c=>{const f=other?.(c);return f&&(()=>{f();done('again');});}})),
        el('p',{class:'hint'},'Each stays a must fix on the rota until one of its sessions is closed.'),
        // The safe way is the primary; saving the clash is the quiet choice (SR-16).
        el('div',{class:'actions'},button('Save with these double bookings',()=>done('save'),'secondary'),button('Back',()=>done('back'),'primary')));
    });});
}

/** The recurring-session form's live check: as people, days, times, dates or the place change, the server previews the
 *  draft pattern (side-effect free) and every clash is listed with its fix. Saving with clashes left needs the tick;
 *  `ack()` is then the payload field the server checks. `exclude` adds a date to the form's excluded dates. */
export function liveClashCheck(form:HTMLFormElement,read:()=>Record<string,unknown>,exclude:(day:string)=>void){
  const out=el('div',{'aria-live':'polite'});
  const tick=check('Save with these double bookings',false,'Each stays a must fix on the rota until one of its sessions is closed.');
  const box=el('section',{class:'span-2 form-section clash-check'},el('h3',{},'Double bookings'),out,tick.wrap);
  tick.wrap.hidden=true;
  let captured='',token='',conflicts:Clash[]=[],timer=0,turn=0,running:Promise<void>|null=null,drawn=false;
  const ids=(list:Clash[])=>list.map(c=>[c.person_id,c.shift_id,c.other_shift_id,c.availability_id,c.start].join('|')).join(',');
  const draw=()=>{drawn=true;
    tick.wrap.hidden=!conflicts.length;
    out.replaceChildren(conflicts.length?el('div',{class:'notice warning clash-note'},el('strong',{},icon('alert'),' ',headline(conflicts.length)),
      clashList(conflicts,()=>refresh(),{own:c=>unsaved(c,'this')?()=>exclude(c.occurrence_on||c.date):undefined})):el('p',{class:'muted'},icon('check'),' No one is double-booked by this pattern.'));
  };
  async function run(){
    window.clearTimeout(timer);timer=0;
    // An incomplete form: a check still on its way must not show the clashes of the values before.
    if(!form.checkValidity()){turn++;captured='';conflicts=[];token='';drawn=false;tick.wrap.hidden=true;out.replaceChildren(el('p',{class:'muted'},'Checked for double bookings once the form is complete.'));return;}
    const body=JSON.stringify(read());if(body===captured)return;
    const mine=++turn;
    try{const r=await request<SeriesOverlapPreview>('/api/series/preview',{method:'POST',body});
      if(mine!==turn)return;
      // Exactly the same clashes again (a field filled in late, a recheck): the list stays as drawn, so a fix button under
      // the pointer or focus is not swapped for a fresh one. Compared whole: a changed
      // fact or cancel action (another admin's edit moves its revision) redraws, so no button keeps a stale revision.
      const same=drawn&&JSON.stringify(r.conflicts)===JSON.stringify(conflicts);
      if(ids(r.conflicts)!==ids(conflicts))tick.input.checked=false;  // a different set of clashes is acknowledged anew
      captured=body;token=r.conflict_token;conflicts=r.conflicts;if(!same)draw();}
    catch(e){if(mine===turn){captured='';drawn=false;out.replaceChildren(note(`Double bookings could not be checked: ${(e as Error).message}`,'warning'));}}
  }
  const go=()=>{running=run().finally(()=>{running=null;});return running;};
  const soon=()=>{window.clearTimeout(timer);timer=window.setTimeout(()=>void go(),300);};
  // A clash fixed on the other side (no field changed here): check again.
  const refresh=()=>{captured='';return go();};
  form.addEventListener('input',e=>{if(e.target!==tick.input)soon();});form.addEventListener('change',e=>{if(e.target!==tick.input)soon();});
  soon();
  return {box,
    /** Before saving: the check for exactly these values, then the acknowledgement if clashes remain. */
    async ready(){if(running)await running;if(timer||captured!==JSON.stringify(read()))await go();
      if(conflicts.length&&!tick.input.checked){tick.input.focus();throw new Error(`Tick “Save with these double bookings”, or close one session of each clash first.`);}},
    ack:():Record<string,string>=>conflicts.length&&tick.input.checked?{overlap_acknowledgement:token}:{},
    /** The server saw other clashes than the ones acknowledged (someone changed a session meanwhile). */
    stale(e:unknown){if(!(e instanceof ApiError&&e.code==='SERIES_OVERLAP_ACKNOWLEDGEMENT'))return false;
      tick.input.checked=false;void refresh();return true;}};
}
