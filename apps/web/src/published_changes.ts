/** Changes to a published rota (mockups B1, B2; D5, D10–D12): the coordinator changes a published session from its own
 *  panel (Replace…, Swap with another duty…, Mark absent, Move to another session…, Add a person, Close the session…). Every
 *  change joins the publication's one hidden change list, checked for the changed sessions only, and is sent for approval
 *  alone (urgent) or with the others (routine). The contract is mapped once, in published_changes_api.ts. */
import {issueList} from './issue_marker.js';
import {el,icon,button,iconButton,withIcon,note,notify,badge,statusBadge,armed,popover,menuItem,plural,labelOf,formatDay,formatTime,toLocal,reportError,needText,emptyLine} from './dom.js';
import {ApiError,onSessionReset} from './api.js';
import {openPicker} from './picker.js';
import {dockPanel,type Dock} from './side_panel.js';
import {candidateList} from './planner_week.js';
import {addDays} from './planner_logic.js';
import {working} from './loading.js';
import {personPill} from './person_pill.js';
import type {Row} from './types.js';
import {pendingChanges,makeChange,undoChange,submitChanges,withdrawChanges,replaceOptions,
  type ChangeSet,type ChangeCommand,type ChangeReply,type PublishedChange,type Urgency,absenceWords,absenceCategoryWord,type AbsenceCategory} from './published_changes_api.js';
import {ABSENCE_CATEGORIES} from './generated.js';
import {menu as openMenu,opens} from './dom.js';  // the shared menu (UX › controls S2b)
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)
import {panelSearch} from './dom.js';  // the one search box in a panel (UX › controls S10)

/** What the Schedule gives: names, the published sessions it knows and a redraw (chip and grid marks). */
export type PublishedHooks={zone:string;name:(personId:string)=>string;place:(s:Row)=>string;sessions:()=>Row[];sources:()=>Row[];redraw:()=>void;
  /** Opens a published session's panel (a change list row's fix). */open:(s:Row)=>void};

// The change lists read this visit, per publication; a change, Undo or send replaces its entry.
const sets=new Map<string,ChangeSet>(),reading=new Set<string>();
const LIVE=new Set(['draft','submitted','approved']);
const live=(pub:string)=>(sets.get(pub)?.changes||[]).filter(c=>LIVE.has(c.status));
const keep=(pub:string,reply:{workspace?:Row|null;changes?:PublishedChange[];urgent_route?:ChangeSet['urgent_route']})=>{
  const was=sets.get(pub);sets.set(pub,{workspace:reply.workspace??was?.workspace??null,changes:reply.changes??was?.changes??[],urgent_route:reply.urgent_route??was?.urgent_route??{active:false,approver_count:0}});};
/** Read the change lists of these publications once (each redraw after reads draws the chip and marks). */
export function loadChanges(pubs:string[],redraw:()=>void){
  const missing=[...new Set(pubs)].filter(p=>p&&!sets.has(p)&&!reading.has(p));if(!missing.length)return;
  missing.forEach(p=>reading.add(p));
  void Promise.all(missing.map(p=>pendingChanges(p).then(set=>{sets.set(p,set);},()=>{sets.set(p,{workspace:null,changes:[],urgent_route:{active:false,approver_count:0}});})))
    // Only a live change draws anything (chip, marks): an empty list redraws nothing, so an open warning or menu stays open.
    .then(()=>{missing.forEach(p=>reading.delete(p));if(missing.some(p=>live(p).length))redraw();});
}
onSessionReset(()=>{sets.clear();reading.clear();});
/** SW3-B-05: a decision on the amendment itself (resubmit, approve, return) changes its changes' state: read again. */
export const forgetChanges=(pub:string|null|undefined)=>{if(pub)sets.delete(pub);};

// SX-16: closing a session has one name everywhere (panel, step, change list, approver): Close.
const KIND:Record<string,string>={replace:'Replace',swap:'Swap',absence:'Absence',cancel:'Close',move:'Move',add:'Add'};
// The categories a coordinator marks an absence with: the contract's one list (sick, leave, emergency, other), never typed here.
const REASONS=ABSENCE_CATEGORIES;
const offline=(e?:unknown)=>!navigator.onLine||e instanceof ApiError&&e.status===0&&e.code!=='PAGE_LEFT';
/** The change-list rule (D12): unsent changes go together (routine); one may go alone only as urgent. */
export const sendLabel=(urgency:Urgency,unsent:number)=>urgency==='urgent'?'Send for urgent approval':unsent>1?`Send ${unsent} changes for approval`:'Send for approval';
/** Urgent is the default for today and tomorrow when an urgent route exists (D5); a later day is routine.
 *  shortcut: "tomorrow" is the mockup's default (README B2 owner decision 1); make it a department setting if asked. */
const urgentFirst=(s:Row,zone:string)=>toLocal(s.start,zone).slice(0,10)<=addDays(toLocal(new Date().toISOString(),zone).slice(0,10),1);
/** Whether a coordinator may change this published session now (not ended, not cancelled). */
export const changeable=(s:Row)=>!s.cancelled&&Date.parse(s.end)>Date.now();

/** Open the next published session panel straight on Replace for this person (the absence warning's one fix). */
let replaceFirst:{shift:string;assignment:string}|null=null;
export const replaceNext=(shift:string,assignment:string)=>{replaceFirst={shift,assignment};};
/** SX-06: open the next published session panel straight on its open place's picker (the change list's Find someone). */
let fillFirst='';

/** SX-04 (D38): the published grid counts the change list as it stands: a staged absence or move leaves its place open,
 *  an add or a move fills one, a closed session needs nobody. Applied to the live check's coverage rows before drawing. */
export function stagedCoverage<T extends {coverage:Row[]}>(evaluation:T):T{
  const delta=new Map<string,number>(),closed=new Set<string>();
  for(const [pub] of sets)for(const c of live(pub)){
    if(c.kind==='cancel')closed.add(c.shift_id);
    if(c.kind==='absence'||c.kind==='move')delta.set(c.shift_id,(delta.get(c.shift_id)||0)-1);
    if(c.kind==='add')delta.set(c.shift_id,(delta.get(c.shift_id)||0)+1);
    if(c.kind==='move'&&c.target_shift_id)delta.set(c.target_shift_id,(delta.get(c.target_shift_id)||0)+1);}
  if(!delta.size&&!closed.size)return evaluation;
  return {...evaluation,coverage:evaluation.coverage.map(r=>closed.has(r.shift_id)?{...r,status:'cancelled'}
    :delta.has(r.shift_id)?stagedRow(r,delta.get(r.shift_id)!):r)};
}
/** One coverage row with `d` people added (negative: taken off). The grid reads the check's own places first
 *  (planner_week.ts coverOf: places_open, places_filled), so they move by what the headcount shortfall changed: a
 *  skill or supervision shortfall the server counted stays as it was. */
export function stagedRow(r:Row,d:number):Row{
  const required=Number(r.required??Number(r.status==='gap')),was=Number(r.available_eligible??0),now=Math.max(0,was+d);
  const out:Row={...r,available_eligible:now,scheduled:Math.max(0,Number(r.scheduled??0)+d)};
  if(r.places_open!=null){
    const open=Math.max(0,Number(r.places_open)+Math.max(0,required-now)-Math.max(0,required-was));
    out.places_open=open;
    if(r.places_filled!=null)out.places_filled=Math.max(0,Number(r.places_filled)-(open-Number(r.places_open)));
  }
  return out;
}
/** Who on a published session is away (approved leave, a Can't make it, another rotation): Replace is offered first. */
const awayOn=(s:Row)=>new Map([...((s.absence_reports||[]) as Row[]).map(r=>[String(r.person_id),''] as const),
  ...((s.double_booking_conflicts||[]) as Row[]).filter(c=>c.code==='UNAVAILABLE'||c.code==='EXTERNAL_ROTATION').map(c=>[String(c.person_id),String(c.label||'')] as const)]);
/** G-02: who reported they can't make this session (staff "Can't make it", BE absence_reports), in words. */
const reportedOn=(s:Row,name:(id:string)=>string,changed:Set<unknown>)=>((s.absence_reports||[]) as Row[]).filter(r=>!changed.has(String(r.assignment_id))).map(r=>
  `${name(String(r.person_id))} reported absent · ${r.status==='approved'?'needs cover':'waiting for approval'}`);
/** SX-18: the reason an away person is replaced for, from what was recorded (never asked again). */
const recordedReason=(label:string):AbsenceCategory=>REASONS.find(c=>c===label.trim().toLowerCase())??(/sick/i.test(label)?'sick':/leave|annual/i.test(label)?'leave':'other');
/** A change's absence reason as the server disclosed it to this reader, " · sick", or nothing (no dangling "·"). */
const absentWhy=(c:{absence_reason?:string})=>{const why=absenceWords(c.absence_reason);return why&&!/^absent$/i.test(why)?` · ${why.toLowerCase()}`:'';};
/** One change in words (the change list row and the session panel say it the same way). */
function changeWhat(c:PublishedChange,hooks:PublishedHooks,byId:Map<string,Row>){
  const s=byId.get(c.shift_id),t=c.target_shift_id?byId.get(c.target_shift_id):undefined;
  const old=s&&personOf(s,c.assignment_id),other=t&&personOf(t,c.target_assignment_id);
  return c.kind==='replace'?`${old?hooks.name(old):'?'} → ${c.person_id?hooks.name(c.person_id):'?'}`:c.kind==='add'?`+ ${c.person_id?hooks.name(c.person_id):''}`
    :c.kind==='swap'?`${old?hooks.name(old):'?'} ⇄ ${other?hooks.name(other):'?'}`:c.kind==='cancel'?(s?s.type_snapshot.name:'Session')+' · closed'
    :c.kind==='move'?`${old?hooks.name(old):'?'} → ${t?sessionWhen(t,hooks.zone):'another session'}`:`${old?hooks.name(old):'?'} · absent${absentWhy(c)}`;
}

/** The top bar's chip: "2 changes · not sent" (teal, opens the list) or "1 change waiting" (gold). `shown` is the rota on
 *  screen: a change set that is that amendment has no chip, since the rota's own status badge already says its state. */
export function changesChip(pubs:string[],hooks:PublishedHooks,shown?:string):HTMLElement|null{
  const pub=pubs.find(p=>live(p).length&&(!shown||sets.get(p)?.workspace?.id!==shown));if(!pub)return null;
  const unsent=live(pub).filter(c=>c.status==='draft').length,back=live(pub).filter(c=>c.returned).length,waiting=live(pub).length-unsent-back;
  // SW-06: a set the approver returned is never "waiting".
  const words=unsent?`${plural(unsent,'change')} · not sent`:back?`${plural(back,'change')} returned`:`${plural(waiting,'change')} waiting`;
  const chip=el('button',{type:'button',class:`rota-status ${unsent||back?'draft':'warn'} pc-chip`,'aria-haspopup':'dialog','data-pubs':JSON.stringify(pubs)},el('span',{class:'dot','aria-hidden':'true'}),words);
  chip.addEventListener('click',()=>openChanges(pub,hooks));
  return chip;
}

/** The grid shows a change at once: who leaves struck through, who joins in a dashed ring (until it is published). */
export function markChanges(root:HTMLElement,hooks:PublishedHooks){
  const byId=new Map(hooks.sessions().map(s=>[String(s.id),s]));
  for(const [pub] of sets)for(const c of live(pub)){
    const s=byId.get(c.shift_id);if(!s)continue;
    const leaving=c.kind==='cancel'?s.participants.map((p:Row)=>p.person_id):[s.participants.find((p:Row)=>p.id===c.assignment_id)?.person_id].filter(Boolean);
    const chips=[...root.querySelectorAll<HTMLElement>(`.pchip[data-shift="${CSS.escape(String(s.id))}"]`)];
    chips.filter(n=>leaving.includes(n.dataset.person)).forEach(n=>{n.classList.add('pc-leaving');n.setAttribute('aria-description',c.status==='draft'?'leaving · change not sent':'leaving · waiting for approval');});
    if(c.person_id&&['replace','add'].includes(c.kind)&&chips.length&&!root.querySelector(`.pc-joining[data-change="${CSS.escape(c.id)}"]`))
      // SX-15: the name is cut inside the dashed box like any chip's (the whole name in its tip).
      // The one person pill (its short forms in a narrow cell).
      chips[chips.length-1].after(personPill({base:el('span',{class:'pc-joining','data-change':c.id,'data-tip':hooks.name(c.person_id),'aria-label':`${hooks.name(c.person_id)} · joining, ${c.status==='draft'?'change not sent':'waiting for approval'}`}),label:hooks.name(c.person_id),person:c.person_id}));
  }
}

const sessionWhen=(s:Row,zone:string)=>`${formatDay(s.start,zone)} · ${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`;
const personOf=(s:Row,assignment?:string|null)=>s.participants.find((p:Row)=>p.id===assignment)?.person_id as string|undefined;

/** The published session panel for a coordinator: the session's people, each with its ⋯ changes, and the steps
 *  (Replace, Add, Cancel) inside the same panel, with "‹ Back to session". `summary` draws the session as it is shown
 *  read-only, with an action beside each person. */
export function publishedSession(dock:Dock,s:Row,pub:string,hooks:PublishedHooks,summary:(person?:(p:Row)=>Node|null)=>HTMLElement){
  const {body,setHead}=dock,zone=hooks.zone,when=sessionWhen(s,zone),title=dock.panel.querySelector('h2')?.textContent||'',sub=dock.panel.querySelector('.side-head-text>small:not(.side-pos)')?.textContent||'';
  const place=hooks.place(s),type=s.type_snapshot.name as string,stepSub=`${type} · ${place} · ${when}`;
  let stop=()=>{};
  const show=(...nodes:(Node|string)[])=>{stop();stop=()=>{};body.replaceChildren(...nodes);};
  const back={label:'session',go:()=>session()};
  const set=()=>sets.get(pub)||{workspace:null,changes:[],urgent_route:{active:false,approver_count:0}};
  const mine=()=>live(pub).filter(c=>c.shift_id===s.id||c.target_shift_id===s.id);
  const after=(reply:Parameters<typeof keep>[1])=>{keep(pub,reply);hooks.redraw();};

  function session(){
    setHead(title,sub,null);dock.clean();  // a step left (Back, Keep session, Discard) leaves nothing unsaved
    const changes=mine(),inReview=changes.some(c=>c.status!=='draft'&&!c.returned);
    const closing=changes.find(c=>c.kind==='cancel'&&c.shift_id===s.id),changed=new Set(changes.map(c=>c.assignment_id).filter(Boolean));
    // SX-05 (D13/D38): someone away (approved leave or Can't make it) is replaced first; they are already absent.
    const away=awayOn(s),awayP=closing?[]:(s.participants as Row[]).filter(p=>away.has(String(p.person_id))&&!changed.has(p.id));
    // SW3-A-18: a staged absence keeps its Replace (cover added on top of it) after leaving the Replace step.
    const absentP=closing?[]:(s.participants as Row[]).filter(p=>changes.some(c=>c.kind==='absence'&&c.status==='draft'&&c.assignment_id===p.id));
    // One ⋯ per person (the shared menu); none while a change to this session waits for approval or it is being closed,
    // nor for a person this session's changes already move (their change has its own Undo).
    const menu=(p:Row)=>{if(inReview||closing||changed.has(p.id))return null;const who=hooks.name(p.person_id),isAway=away.has(String(p.person_id));
      const b:HTMLButtonElement=opens(iconButton(`Change ${who} on this session`,'dots',()=>openMenu(b,`Change ${who}`,[
        {label:'Replace…',icon:'people',run:()=>pick(p)},{label:'Swap with another duty…',icon:'swap',run:()=>swap(p,b)},
        // SW3-A-28: why they are absent is asked once, here (one press each, acting at once with Undo), never again on Replace.
        ...(isAway?[]:REASONS.map(r=>({label:`Mark absent · ${absenceCategoryWord(r)}`,icon:'leave' as const,run:()=>void absent(p,r)}))),{label:'Move to another session…',icon:'route',run:()=>move(p,b)}],{placement:'below-end'}),{class:'bare'}),'menu');
      return b;};
    // SX-07: each change to this session in words, with Undo while it is not sent; a closing reads as the session's state.
    const byId=new Map(hooks.sessions().map(x=>[String(x.id),x]));
    const state=changes.map(c=>{const what=changeWhat(c,hooks,byId);
      return el('div',{class:'pc-state'+(c.kind==='cancel'?' notice warning':'')},el('div',{class:'pc-state-line'},changeBadge(c),
        el('strong',{class:'pc-state-what'},c.kind==='cancel'?'This session is closed':what),
        c.returned?el('span',{class:'muted'},returnedWords(c.returned,zone)):c.submitted_at?el('span',{class:'muted'},'Sent '+formatTime(c.submitted_at,zone)):'',
        el('span',{class:'grow'}),c.status==='draft'?withIcon(button('Undo',()=>void undo(c.id),'ghost small',{'aria-label':`Undo: ${what}`}),'undo'):''),wasNow(c));});
    const sent=changes.filter(c=>c.status==='submitted'&&!c.returned),back=changes.filter(c=>c.returned);
    // SW-06: a returned change is taken back to edit (it becomes unsent: Undo and the ⋯ changes come back).
    if(back.length)return show(...state,summary(menu),el('footer',{class:'form-footer pc-foot'},el('span',{class:'grow'}),
      button('Edit the changes',()=>withdraw(back.map(c=>c.id),'Taken back to edit after it was returned'),'primary small')));
    const reported=closing?[]:reportedOn(s,hooks.name,changed).map(t=>note(t,'warning'));
    show(...reported,...state,summary(menu),el('footer',{class:'form-footer pc-foot'},...(sent.length
      // Withdraw takes back the whole set that was sent (maintenance: one set under review per publication).
      ?[button(sent.length>1?'Withdraw the set':'Withdraw',()=>withdraw(sent.map(c=>c.id)),'ghost danger small',{'aria-label':'Withdraw the changes sent for approval'}),el('span',{class:'grow'}),button('Open in Changes',()=>openChanges(pub,hooks),'secondary small')]
      :closing?[el('span',{class:'grow'}),button('Open in Changes',()=>openChanges(pub,hooks),'secondary small')]
      :[...awayP.map(p=>withIcon(button(`Replace ${hooks.name(p.person_id)}…`,()=>pick(p),'primary small'),'people')),
        ...absentP.map(p=>withIcon(button(`Replace ${hooks.name(p.person_id)}…`,()=>pick(undefined,true,p),'primary small'),'people')),
        withIcon(button('Add a person',()=>pick(),'secondary small'),'plus'),el('span',{class:'grow'}),button('Close the session…',()=>cancel(),'ghost danger small')])));
  }
  const wasNow=(c:PublishedChange)=>{const old=personOf(s,c.assignment_id);
    return c.kind==='replace'&&old&&c.person_id?el('div',{class:'change-summary'},el('p',{},el('span',{class:'change-label'},'Was'),el('span',{},hooks.name(old),absentWhy(c)?el('span',{class:'muted'},absentWhy(c)):'')),
      el('p',{},el('span',{class:'change-label'},'Now'),el('strong',{},hooks.name(c.person_id)))):'';};

  /** Replace (with `leaving`) or Add a person: the reason, then who can take the place, from the draft's own list.
   *  `absent`: the person whose staged absence left this place open (SW3-A-18): the cover is added on top of the
   *  absence, which stays in the list, so leaving this step changes nothing. */
  function pick(leaving?:Row,fill=false,absent?:Row){
    const old=leaving?hooks.name(leaving.person_id):'';
    setHead(leaving?`Replace ${old}`:absent?`Replace ${hooks.name(absent.person_id)}`:fill?'Fill the open place':'Add a person',stepSub,back);
    const recorded=leaving?awayOn(s).get(String(leaving.person_id)):undefined;
    let reason:AbsenceCategory=recorded!==undefined?recordedReason(recorded):REASONS[0];
    const seg=segmented('Reason',REASONS.map(r=>[r,absenceCategoryWord(r)]),reason,r=>{reason=r as AbsenceCategory;},{labelledBy:'pc-why'});
    const search=panelSearch('Find a person','Find a person or code',undefined,'wk-search');
    const list=el('div',{class:'wk-cands','aria-live':'polite'},working('Checking who can take this place…'));
    // An absence already recorded (approved leave or Can't make it) is not asked again: its reason is said.
    show(el('div',{class:'pc-step'},leaving?recorded!==undefined?el('p',{class:'muted'},`Away · ${recorded||reason}`):el('div',{class:'field'},el('span',{class:'field-label',id:'pc-why'},'Reason'),seg):'',search,list));
    search.focus({preventScroll:true});
    replaceOptions(pub,s,leaving,set()).then(({result,set:ready})=>{keep(pub,ready);if(!list.isConnected)return;
      // A published change never breaks a rule on purpose (maintenance has no override): a blocked person is not offered.
      candidateList({result,list,search,code:id=>hooks.name(id),replace:!!leaving,breakable:()=>false,
        assign:c=>void review({kind:leaving?'replace':'add',shift_id:s.id,...(leaving?{assignment_id:leaving.id,absence_reason:reason}:{}),person_id:c.person_id,
          reason:leaving?`${c.display_name} replaces ${old}`:absent?`${c.display_name} covers ${hooks.name(absent.person_id)}`:`${c.display_name} added`} as ChangeCommand,c,leaving,absent)});
    }).catch(e=>{list.replaceChildren(note((e as Error).message,'error',e),button('Try again',()=>pick(leaving,fill,absent),'secondary small'));});
  }

  /** Review: was / now, the scoped check, the approval route and who is told; one primary named after its step. The change
   *  is made at once (it joins the list as not sent); offline, it is kept here and sent when the connection is back. */
  async function review(cmd:ChangeCommand,c:Row,leaving?:Row,absent?:Row){
    const old=leaving?hooks.name(leaving.person_id):'',joining=String(c.display_name),gone=absent?hooks.name(absent.person_id):'';
    setHead(leaving?`Replace ${old}`:absent?`Replace ${gone}`:'Add a person',stepSub,back);
    show(working(`Checking ${joining} on this session and their other duties…`));
    let reply:ChangeReply|null=null;
    try{reply=await makeChange(pub,cmd,set());after(reply);}
    // ME-01: a change that failed says so here, with Try again (the same command and key, so a save that went through is
    // not made twice) and the way back to the list; the footer's save state alone is not enough.
    catch(e){if(!offline(e)){show(el('div',{class:'pc-step'},failure(e,'This change was not made.'),el('div',{class:'actions'},
      button('Choose someone else',()=>pick(leaving,!leaving,absent),'ghost small'),el('span',{class:'grow'}),button('Try again',()=>review(cmd,c,leaving,absent),'primary small'))));return;}}
    const v=reply?.validation,errors=v?.error_count||0,found=((reply?.change as {findings?:Row[]}|undefined)?.findings||reply?.issues?.added||[]) as Row[];
    const route=set().urgent_route;let urgency:Urgency=route.active&&urgentFirst(s,zone)?'urgent':'routine';
    const check=!reply?'':errors||v?.warning_count?el('div',{class:'pc-check'},...findingGroups(found,hooks),
        errors?note(`${plural(errors,'must-fix finding')}: choose someone else to send this change.`,'error'):'')
      :el('div',{class:'wk-flash',role:'status'},icon('check'),el('span',{},'Passes every check'),
        el('button',{type:'button',class:'icon-button bare','aria-label':'What was checked','data-help':'',
          'data-tip':`Checked: this session and ${joining}’s other duties (rest, hours, leave). Sessions you did not change keep the checks they were published with.`},icon('help')));
    const why=el('p',{class:'pc-why muted',id:'pc-send-why'}),sendFailed=el('div',{class:'pc-failed'});
    const send=withIcon(button(sendLabel(urgency,1),()=>go(),'primary small',{'aria-describedby':'pc-send-why'}),'send');
    const unsent=()=>live(pub).filter(x=>x.status==='draft').length||1;
    const paint=()=>{send.lastChild!.textContent=sendLabel(urgency,unsent());
      const off=!navigator.onLine||!reply;send.disabled=off||errors>0;
      why.textContent=off?'Offline. Your change is kept here; send it when the connection is back.':'';why.hidden=!why.textContent;};
    const approval=approvalChoice(route,urgency,u=>{urgency=u;paint();});
    const told=el('p',{class:'pc-told muted'},leaving?`${joining} and ${old} are told once it is approved.`:`${joining} is told once it is approved.`);
    const discard=button('Discard',async()=>{if(reply){after(await undoChange(pub,reply.change.id,set()));}session();},'ghost small');
    show(el('div',{class:'pc-step'},el('div',{class:'change-summary'},el('p',{},el('span',{class:'change-label'},'Was'),el('span',{},leaving?old:gone?`${gone} · absent`:'Open place',leaving&&cmd.absence_reason?el('span',{class:'muted'},' · '+cmd.absence_reason.toLowerCase()):'')),
      el('p',{},el('span',{class:'change-label'},'Now'),el('strong',{},joining))),check,approval,told,why,sendFailed),el('footer',{class:'form-footer pc-foot'},discard,el('span',{class:'grow'}),send));
    paint();
    const online=async()=>{if(!body.isConnected){stop();return;}if(!reply&&navigator.onLine){try{reply=await makeChange(pub,cmd,set());after(reply);}catch(e){if(!offline(e))reportError(e);}}paint();};
    addEventListener('online',online);addEventListener('offline',paint);stop=()=>{removeEventListener('online',online);removeEventListener('offline',paint);};
    async function go(){if(!reply)return;const id=reply.change.id;sendFailed.replaceChildren();
      // F8: a send that failed is said in the panel (the change stays in the list, not sent); Send tries again.
      try{await sendChanges(pub,{ids:urgency==='urgent'?[id]:undefined,urgency,reason:cmd.reason},hooks);}
      catch(e){if(body.isConnected){sendFailed.replaceChildren(failure(e,'Not sent. The change is still in the list.'));send.focus();}return;}
      session();
      notify(`Sent for ${urgency==='urgent'?'urgent ':''}approval. ${joining} shows as waiting until it is approved.`,'success',{label:'Undo',run:()=>withdraw([id])});}
  }

  /** Close the session…: a reason, the approval route (routine for a later day), then the armed "Confirm close?".
   *  D38/SX-16: the one name for this audited, undoable cancellation, whether or not an absence left nobody on it.
   *  `vacated`: an absence just left nobody on it (its people are not "freed" then). */
  function cancel(vacated=false){
    setHead('Close the session',sub,back);
    const names=vacated?[]:s.participants.map((p:Row)=>hooks.name(p.person_id));
    const reason=el('input',{type:'text',id:'pc-cancel-reason',required:true,minlength:3,maxlength:500}) as HTMLInputElement;
    const route=set().urgent_route;let urgency:Urgency=route.active&&urgentFirst(s,zone)?'urgent':'routine',failed=el('div',{class:'pc-failed'});
    const form=el('form',{class:'pc-step'},el('div',{class:'notice warning',role:'note'},el('strong',{},'Close this session? '),
        names.length?`${names.join(', ')} ${names.length>1?'are':'is'} freed and told once it is approved. You can undo it.`:'Its needed place is removed. You can undo it.'),
      el('div',{class:'field'},el('label',{for:'pc-cancel-reason'},'Reason'),reason),approvalChoice(route,urgency,u=>{urgency=u;}),failed) as HTMLFormElement;
    const confirm=armed(withIcon(button('Close session',()=>{},'danger small',{type:'submit','form':'pc-cancel'}),'trash'),'Confirm close?');
    form.id='pc-cancel';
    form.addEventListener('submit',async e=>{e.preventDefault();if(!form.reportValidity())return;confirm.disabled=true;
      try{const r=await makeChange(pub,{kind:'cancel',shift_id:s.id,reason:reason.value.trim()},set());after(r);
        if(urgency==='urgent')await sendChanges(pub,{ids:[r.change.id],urgency,reason:reason.value.trim()},hooks);
        session();notify(urgency==='urgent'?'Closing sent for urgent approval.':'Closing added to the changes. Send it with the others.','success',{label:'Undo',run:()=>urgency==='urgent'?withdraw([r.change.id]):undo(r.change.id)});}
      catch(err){confirm.disabled=false;failed.replaceChildren(failure(err,'The session was not closed.'));}});
    show(form,el('footer',{class:'form-footer pc-foot'},button('Keep it open',()=>session(),'ghost small'),el('span',{class:'grow'}),confirm));
    reason.focus();
  }

  /** Mark absent: the person comes off this session at once (a change, not sent); the open place it leaves is a Must fix.
   *  D38 reviewed: an absence never closes a session by itself. When nobody is left on it (maintenance
   *  `sessions[].closes_session`), that is said before anything is sent, with Replace as the primary; closing it is only
   *  the coordinator's explicit "Close the session" (the audited, undoable cancellation). */
  async function absent(p:Row,why:AbsenceCategory){const who=hooks.name(p.person_id);
    const r=await makeChange(pub,{kind:'absence',shift_id:s.id,assignment_id:p.id,absence_reason:why,reason:`${who} absent`},set()).catch(e=>{reportError(e);return null;});
    if(!r)return;after(r);
    if(!r.change.sessions?.some(x=>(x as {closes_session?:boolean}).closes_session)){session();notify(`${who} marked absent. The change is not sent yet.`,'success',{label:'Undo',run:()=>undo(r.change.id)});return;}
    setHead(`${who} absent`,stepSub,back);
    show(el('div',{class:'pc-step'},el('div',{class:'notice warning',role:'status'},el('strong',{},'Nobody is left on this session. '),
        `Its place stays open. Replace ${who}, or close the session.`)),
      el('footer',{class:'form-footer pc-foot'},button('Undo',async()=>{await undo(r.change.id);},'ghost small'),el('span',{class:'grow'}),
        button('Close the session…',()=>cancel(true),'secondary small'),
        withIcon(button(`Replace ${who}…`,()=>pick(undefined,true,p),'primary small'),'people')));}

  /** Swap and Move pick their other side with the shared picker, from this publication's future sessions in view. */
  const others=()=>hooks.sessions().filter(x=>x.id!==s.id&&changeable(x)&&(x.publication_id||pub)===(s.publication_id||pub));
  function swap(p:Row,anchor:HTMLElement){const who=hooks.name(p.person_id);
    const choices=others().flatMap(x=>x.participants.filter((q:Row)=>q.person_id!==p.person_id).map((q:Row)=>({value:`${x.id}|${q.id}`,label:hooks.name(q.person_id),group:`${sessionWhen(x,zone)} · ${x.type_snapshot.name}`})));
    if(!choices.length){notify('No other duty in view to swap with. Move to the week of the other duty first.','warning');return;}
    openPicker(anchor,anchor.parentElement||document.body,{label:`Swap ${who} with`,multiple:false,selected:new Set(),choices,recent:false,onPick:v=>{const [shift,assignment]=v.split('|');
      void makeChange(pub,{kind:'swap',shift_id:s.id,assignment_id:p.id,target_shift_id:shift,target_assignment_id:assignment,reason:`${who} swaps duties`},set())
        .then(r=>{after(r);session();notify(`Swap added to the changes. It is not sent yet.`,'success',{label:'Undo',run:()=>undo(r.change.id)});},reportError);}});}
  function move(p:Row,anchor:HTMLElement){const who=hooks.name(p.person_id);
    const choices=others().map(x=>({value:String(x.id),label:`${x.type_snapshot.name} · ${hooks.place(x)}`,group:formatDay(x.start,zone),hint:`${formatTime(x.start,zone)}–${formatTime(x.end,zone)}`}));
    if(!choices.length){notify('No other session in view to move to.','warning');return;}
    openPicker(anchor,anchor.parentElement||document.body,{label:`Move ${who} to`,multiple:false,selected:new Set(),choices,recent:false,onPick:shift=>{
      void makeChange(pub,{kind:'move',shift_id:s.id,assignment_id:p.id,target_shift_id:shift,reason:`${who} moved`},set())
        .then(r=>{after(r);session();notify(`Move added to the changes. It is not sent yet.`,'success',{label:'Undo',run:()=>undo(r.change.id)});},reportError);}});}

  const undo=async(id:string)=>{after(await undoChange(pub,id,set()));if(body.isConnected)session();notify('Change undone.');};
  const withdraw=async(_ids:string[],reason?:string)=>{after(await withdrawChanges(pub,set(),reason));if(body.isConnected)session();notify(reason?'Back in the list, not sent. Change it or undo it, then send again.':'Withdrawn. The change is back in the list, not sent.');};
  const first=replaceFirst?.shift===s.id?s.participants.find((p:Row)=>p.id===replaceFirst!.assignment):undefined;replaceFirst=null;
  const fill=fillFirst===s.id;fillFirst='';
  if(first)pick(first);else if(fill)pick(undefined,true);else session();
}

/** maintenance `change_diff`: each changed session before and after (null: added or removed), and the week to open. */
export type ChangeDiff={base_publication_id?:string|null;first_changed_date?:string|null;review_week_start?:string|null;
  /** `changes`: the change(s) behind the session (ME-26), with the coordinator's reason (Sick, Leave…). */
  sessions?:{before?:Row|null;after?:Row|null;changes?:{kind:string;absence_reason?:string}[]}[]};
/** ME-26: the sessions an amendment changes, ringed on its grid: whoever joins them, or the open place they leave. */
export function markDiff(root:HTMLElement,diff:ChangeDiff|null|undefined){
  for(const {before,after} of diff?.sessions||[]){if(!after)continue;
    const was=new Set(((before?.participants||[]) as Row[]).map(p=>String(p.person_id)));
    for(const n of root.querySelectorAll<HTMLElement>(`.pchip[data-shift="${CSS.escape(String(after.id))}"]`))
      if(!n.dataset.person||!was.has(n.dataset.person)){n.classList.add('pc-changed');n.setAttribute('aria-description','changed in this amendment');}}
}
/** F4: "What changes" for the approver (and the coordinator sending it): one row per changed session, its day, time,
 *  duty and place, who was on it and who is now. Null when nothing is listed. */
export function changedSessions(diff:ChangeDiff|null|undefined,h:{zone:string;name:(id:string)=>string;place:(s:Row)=>string}):HTMLElement|null{
  const rows=(diff?.sessions||[]).filter(x=>x.before||x.after);if(!rows.length)return null;
  const people=(s?:Row|null)=>((s?.participants||[]) as Row[]).map(p=>h.name(String(p.person_id)));
  const LIMIT=20;
  // SX-17: counted as changes, as Home counts them (one session may carry an absence and its cover).
  const n=rows.reduce((k,r)=>k+Math.max(1,(r.changes||[]).length),0);
  return el('section',{class:'span-2 pc-diff','aria-label':'What changes'},el('div',{class:'decide-head'},el('h3',{},'What changes'),badge(n===rows.length?plural(n,'change'):`${plural(n,'change')} · ${plural(rows.length,'session')}`,'neutral')),
    el('ul',{class:'home-needs-list pc-changes'},...rows.slice(0,LIMIT).map(({before,after,changes})=>{const s=(after||before)!;
      const was=people(before),now=people(after),gone=was.filter(x=>!now.includes(x)),came=now.filter(x=>!was.includes(x));
      const what=!before?'Added session':!after||after.cancelled||after.closed&&!before.closed?'Closes the session':gone.length||came.length?`${gone.join(', ')||'Open place'} → ${came.join(', ')||'open place'}`:'Changed';
      // Why, as the coordinator gave it (Sick, Leave…), beside who left, as the change list says it.
      const why=[...new Set((changes||[]).map(c=>absenceWords(c.absence_reason)).filter(Boolean))].join(', ').toLowerCase();
      return el('li',{},needText([what,why?el('span',{class:'muted'},` · ${why}`):''],`${sessionWhen(s,h.zone)} · ${String(s.type_snapshot?.name||'Session')} · ${h.place(s)}`));}),
      ...(rows.length>LIMIT?[el('li',{class:'muted'},`${rows.length-LIMIT} more`)]:[])));
}
/** One change's state: Not sent, Returned (SW-06), or Waiting / approved. */
const changeBadge=(c:PublishedChange,waiting='Waiting for approval')=>c.status==='draft'?badge('Not sent','draft'):c.returned?badge('Returned','warn')
  :statusBadge(c.status,c.status==='submitted'?`${waiting}${c.urgency==='urgent'?' · urgent':''}`:undefined);
const returnedWords=(r:NonNullable<PublishedChange['returned']>,zone:string)=>`${formatDay(r.at,zone)} ${formatTime(r.at,zone)} · “${r.reason}”`;
/** A refusal said where it happened: what did not happen, then the server's words (Retry is the caller's). */
const failure=(e:unknown,lead:string)=>note(`${lead} ${(e as Error).message||''}`.trim(),'error',e);
/** F6: the scoped check's findings in the Fix list's two levels, each line naming its person, the same line once with
 *  its count; at most six lines, then how many more. */
function findingGroups(found:Row[],hooks:PublishedHooks):HTMLElement[]{
  // The one issue list (issue_marker.ts): maintenance findings carry their level, person, day and rule names; older replies
  // only severity and ids. The same words on several people or days are one item with them as chips.
  return found.length?[issueList(found.map(f=>({severity:(f.level?f.level==='must':f.severity==='error')?'error':'warning',
    words:String(f.message||f.rule_name||''),where:[f.person_name||(f.person_id?hooks.name(String(f.person_id)):''),f.starts_on?formatDay(String(f.starts_on),hooks.zone):''].filter(Boolean).join(' · '),
    ...(f.shift_id?{shift:String(f.shift_id)}:{})})),{limit:12})]:[];
}
/** Sends changes; the reply is the review candidate, so the hidden list (its revision binds the next change) is read again. */
const tipped=(n:HTMLElement,tip:string)=>{n.dataset.tip=tip;return n;};
async function sendChanges(pub:string,o:Parameters<typeof submitChanges>[1],hooks:PublishedHooks){
  const was=sets.get(pub)||{workspace:null,changes:[],urgent_route:{active:false,approver_count:0}};
  const done=await submitChanges(pub,o,was);keep(pub,{changes:done.changes});
  // ME-21: the chip says the reply at once ("1 change waiting"); the page is drawn again once the list is read.
  for(const n of document.querySelectorAll<HTMLElement>('.pc-chip[data-pubs]')){const c=changesChip(JSON.parse(n.dataset.pubs!),hooks);if(c)n.replaceWith(c);else n.remove();}
  sets.set(pub,await pendingChanges(pub).catch(()=>sets.get(pub)!));hooks.redraw();
}

/** The approval route as two radio cards: Urgent only when the department has an active urgent route (D5). */
function approvalChoice(route:ChangeSet['urgent_route'],value:Urgency,onChange:(u:Urgency)=>void):HTMLElement|''{
  // SW3-A-29: without an urgent route there is nothing to choose (routine, the approvers' queue): no lone radio.
  if(!route.active)return '';
  const opt=(u:Urgency,title:string,text:string)=>{const input=el('input',{type:'radio',name:'pc-route',value:u}) as HTMLInputElement;input.checked=u===value;
    const label=el('label',{class:'sd-option'+(input.checked?' on':'')},input,el('span',{class:'sd-option-body'},el('strong',{},u==='urgent'?icon('clock'):'',title),el('span',{},text)));
    input.addEventListener('change',()=>{box.querySelectorAll('.sd-option').forEach(n=>n.classList.toggle('on',n.contains(input)));onChange(u);});return label;};
  const box=el('fieldset',{class:'sd-options pc-route'},el('legend',{class:'wk-h3'},'Approval'),
    opt('urgent','Urgent',`${plural(route.approver_count,'approver')} notified at once`),opt('routine','Routine','Approvers’ queue'));
  return box;
}

/** "Changes to <month>": the publication's changes, one row each (kind, what, when and where, state, its one fix, Undo);
 *  "Send N changes for approval" stays disabled while a Must fix remains, and says why. */
export function openChanges(pub:string,hooks:PublishedHooks){
  const source=hooks.sources().find(x=>x.id===pub),month=source?new Date(String(source.starts_on)+'T12:00:00Z').toLocaleString('en-GB',{month:'long',timeZone:'UTC'}):'';
  dockPanel({key:'published-changes',title:month?`Changes to ${month}`:'Changes to the published rota',sub:'Published rota',className:'pc-panel',build:({body,setHead})=>{
    const draw=()=>{
      const all=live(pub),byId=new Map(hooks.sessions().map(x=>[String(x.id),x]));
      setHead(month?`Changes to ${month}`:'Changes to the published rota',`Published rota · ${plural(all.length,'change')}`);
      const unsent=all.filter(c=>c.status==='draft'),errors=Number(set().workspace?.validation?.error_count||0);
      const fixes=all.filter(c=>c.must_fix?.length),places=fixes.some(c=>c.kind==='absence'||c.kind==='move');
      const row=(c:PublishedChange)=>{const s=byId.get(c.shift_id),what=changeWhat(c,hooks,byId);
        const where=s?`${sessionWhen(s,hooks.zone)} · ${hooks.place(s)}${c.kind==='cancel'?` · ${plural(s.participants.length,'person','people')} freed`:''}`:'A session outside this view';
        const must=c.must_fix||[];
        // D38 (maintenance sessions[].closes_session): an absence that leaves nobody on the session; said on the row (it stays open).
        // SX-17: once someone is added to that session (or it is closed), nobody-left no longer holds.
        const closes=c.kind==='absence'&&(c.sessions as {closes_session?:boolean}[]|undefined)?.some(x=>x.closes_session)&&!all.some(x=>x.shift_id===c.shift_id&&(x.kind==='add'||x.kind==='cancel'));
        const state=must.length?tipped(badge(plural(must.length,'must fix','must fix'),'err'),must.map(f=>String(f.message)).join(' · ')):changeBadge(c,'Waiting');
        // An absence or move leaves an open place: its one fix is to find someone for that session.
        // SX-06: Find someone opens that session on its open place's picker (who can take it), not on the session.
        const fix=must.length&&s&&(c.kind==='absence'||c.kind==='move')?button('Find someone',()=>{fillFirst=String(s.id);hooks.open(s);},'secondary small'):'';
        return el('li',{},el('span',{class:'role-chip duty-slate home-kind'},el('span',{class:'chip-text'},KIND[c.kind]||c.kind)),
          needText(what,where),closes?badge('Nobody left','warn'):'',state,fix,
          c.status==='draft'?iconButton(`Undo this change: ${what}`,'undo',async()=>{after(await undoChange(pub,c.id,set()));draw();},{class:'bare'}):el('span',{class:'pc-undo-gap','aria-hidden':'true'}));};
      const blocked=fixes.length?(places?'Fill the open place to send':`Fix ${plural(fixes.length,'change')} with a must fix to send`):errors?`Fix ${plural(errors,'must-fix finding')} to send`:'';
      const why=el('span',{class:'muted',id:'pc-list-why'},!navigator.onLine?'Offline. Send when the connection is back.':blocked);
      const send=withIcon(button(`Send ${plural(unsent.length,'change')} for approval`,async()=>{await sendChanges(pub,{urgency:'routine',reason:`Changes to ${month||'the published rota'}`},hooks);draw();
        notify('Sent for approval.');},'primary small',{'aria-describedby':'pc-list-why'}),'send');
      send.disabled=!unsent.length||!!blocked||!navigator.onLine;
      // F9: nothing left to send, no Send button (never "Send 0 changes").
      // SW-06: the approver's return is said once above the list, with the way to edit the set and send it again.
      const back=all.filter(c=>c.returned),ret=back[0]?.returned;
      const edit=back.length?button('Edit the changes',async()=>{after(await withdrawChanges(pub,set(),'Taken back to edit after it was returned'));draw();
        notify('Back in the list, not sent. Change or undo them, then send again.');},'primary small'):null;
      body.replaceChildren(...(ret?[note(`Returned by ${ret.by_name||'an approver'} · ${returnedWords(ret,hooks.zone)}`,'warning')]:[]),
        all.length?el('ul',{class:'home-needs-list pc-changes'},...all.map(row)):emptyLine('No changes.'),
        ...(unsent.length?[el('footer',{class:'rc-lp-foot pc-list-foot'},why,el('span',{class:'grow'}),send)]:edit?[el('footer',{class:'rc-lp-foot pc-list-foot'},el('span',{class:'grow'}),edit)]:[]));};
    const set=()=>sets.get(pub)||{workspace:null,changes:[],urgent_route:{active:false,approver_count:0}};
    const after=(reply:Parameters<typeof keep>[1])=>{keep(pub,reply);hooks.redraw();};
    draw();
    // Fresh from the server each time it opens (another coordinator may have changed the list).
    void pendingChanges(pub).then(fresh=>{sets.set(pub,fresh);if(body.isConnected)draw();hooks.redraw();}).catch(()=>{});
  }});
}
