import {el,button,badge,plural,notify,icon,popover,formatDate,formatTime,formatDay,dateUnits,toLocal,footerStatus,note,reportError,spanLabel,type Child} from './dom.js';
import {dataTable,type Column} from './table_view.js';
import {selectionBar,type SelectionAction} from './selection_bar.js';
import {command,request,get,undoInPlace,takeUndo,ApiError} from './api.js';
import {savingMark,savedMark,failedMark,conflict} from './loading.js';
import {leaveDates,leavePill,issueKind,findingNumbers,alsoShort,causeText,causeHead,issueKey} from './findings.js';
import type {FillIssue} from './planner_week.js';
import {cancelButtons,type Clash} from './double_booking.js';
import {RULE_KINDS,catalogueEntryOf,allowsException} from './forms.js';
import type {Row,WorkspaceData} from './types.js';
import type {FindingCauseGroup} from './generated.js';
import {personFixMenu,tableKey} from './rule_catalogue.js';
import type {Context} from './forms.js';

/** Schedule › Fix list › All checks (#schedule/issues?draft=<id>&view=checks): every issue of a draft's rota check in one table, resolved in
 *  batches. Each batch is ONE server command (issues.resolve / exception.create_many: one change set, one Undo) and its
 *  reply is patched into the table in place: the touched sessions, the issues that went and the ones that came. Nothing
 *  reloads the rota. Read-only for reviewers; staff never reach it (the rota read refuses them). */
export interface IssuesHooks{
  name:(kind:string,id:string)=>string;find:(kind:string,id:string)=>Row|undefined;rows:(kind:string)=>Row[];zone:()=>string;
  /** May change this draft (coordinator, draft). */
  editable:(w:Row)=>boolean;
  back:(workspaceId:string)=>void;/** The Fix list, this table's other view. */fixList?:(workspaceId:string)=>void;showOnSchedule:(workspaceId:string,s:Row)=>void;
  source?:(id:string)=>Row|undefined;loadSources?:(ids:string[])=>Promise<void>;openRule?:(id:string,personId?:string)=>void;
  /** An open place: the Schedule's fill panel for that session (who can work it, and why others cannot), with the issue
   *  it was opened for kept in view there. */
  fill:(workspaceId:string,s:Row,issue?:FillIssue)=>void;
  /** The exception request form, filled in for every chosen issue (forms.ts exceptionForm). */
  exceptions:(items:{policy_id:string;person_id:string;day:string}[],done:()=>void)=>void;
  /** The check's canonical cause groups (the rota summary's `cause_groups`), shared with the Schedule's issue cards. */
  /** The Schedule's own cause cards (one per server cause group holding any of these issues), with their fixes: what a
   *  cause group of this table opens, as the one issue list. `close` closes what it sits in before a fix moves the view. */
  causeCards?:(data:WorkspaceData,keys:string[],close:()=>void)=>HTMLElement;
  causes?:(data:WorkspaceData)=>Promise<FindingCauseGroup[]>;
  /** A waiting auto-schedule proposal for this draft, said once above the issues with its review (or null). */
  proposal?:(data:WorkspaceData)=>Promise<HTMLElement|null>;
  /** The app context, for a person row's ⋯ (Relax for <name>…, Open line N: rule_catalogue.ts personFixMenu). */
  context?:()=>Context;
}
export {issueKey};

type Issue={key:string;f:Row;shift?:Row;resolved?:string;saving?:string;failed?:{retry?:()=>void}};
type Preview={plan:Row[];plan_hash:string;replaceable:number;open:number;keys:Set<string>};

export async function issuesPage(target:HTMLElement,workspaceId:string,h:IssuesHooks){
  if(!workspaceId){target.append(note('Open a draft rota first: the issues belong to one draft.','warning'));return;}
  const data=await get<WorkspaceData>('/api/workspaces/'+workspaceId);
  let w=data.workspace;const zone=h.zone(),canEdit=h.editable(w);
  let fresh=!!w.validation&&w.validation.fingerprint===data.current_fingerprint;
  const checkNotice=note(w.validation?'The rota inputs changed. Check the rota again before resolving its issues.':'Check the rota to list its issues.','warning');
  checkNotice.classList.add('actions');
  checkNotice.hidden=fresh;
  if(canEdit)checkNotice.append(button('Check rota',async()=>{await command('workspace.validate',{reason:'Refresh issues'},w.id,w.revision);
    if(!target.isConnected)return;target.replaceChildren();await issuesPage(target,workspaceId,h);},'secondary small'));
  target.append(checkNotice);
  const waiting=canEdit?await h.proposal?.(data).catch(()=>null):null;if(waiting)target.append(waiting);
  const shifts=new Map(data.shifts.map(s=>[s.id,s]));
  const rows:Issue[]=[];
  // An issue in the check: a new row, or its row again (open, or Accepted while it is accepted) when a batch or Undo brings it back.
  const add=(f:Row)=>{const key=issueKey(f),row={key,f,shift:f.shift_id?shifts.get(f.shift_id):undefined,
      resolved:(w.accepted_issues||[]).some((a:Row)=>a.key===key)?'Accepted':undefined},had=rows.find(r=>r.key===key);
    if(had)Object.assign(had,row);else rows.push(row);};
  for(const f of (w.validation?.findings||[]) as Row[])add(f);
  // Issues that share a cause the server named (no eligible people, no sessions to fill, capacity short…) are grouped
  // under it by default, each still its own row; one the check did not group keeps its reason (its rule or kind).
  const causeOf=new Map<string,string>(),causeCodes=new Map<string,Set<string>>(),byKey=new Map(rows.map(r=>[r.key,r]));
  if(fresh&&h.causes)for(const g of await h.causes(data).catch(()=>[] as FindingCauseGroup[]))for(const k of g.finding_keys)
    if(byKey.get(k)?.f.cause){causeOf.set(k,causeHead(g.code,g.fix_hints).replace(/^./,c=>c.toUpperCase()));causeCodes.set(k,new Set([...causeCodes.get(k)||[],g.code]));}
  await h.loadSources?.([...new Set(rows.map(r=>r.f.rule_id).filter(Boolean) as string[])]);

  // ---- Words ----
  const rule=(f:Row)=>f.rule_id?h.source?.(f.rule_id)||h.find('policies',f.rule_id):undefined;
  const ruleName=(f:Row)=>{const p=rule(f),r=p?.definition?.rule;
    return String(p?.name||'').trim()||(r?.kind==='catalogue'?catalogueEntryOf(r)?.name:RULE_KINDS.find(e=>e[0]===r?.kind)?.[1])||(f.rule_id?'Rule':'');};
  const kind=(r:Issue)=>issueKind(r.f,ruleName(r.f));
  const day=(iso:string)=>formatDay(iso,zone,true);
  const clinic=(s:Row)=>(s.service_ids as string[]).map(x=>h.name('services',x)).join(' + ');
  // A session's issue names the session; a period's names the days it counts over (its window, else its own dates),
  // never a dash where the check said when (F5).
  const period=(f:Row)=>{const w=f.window as {start?:string;end?:string}|undefined,a=w?.start||f.start,b=w?.end||f.end;
    return a&&b&&a!==b?spanLabel(a,b,zone):a?formatDate(a,zone):'Whole rota';};
  const sessionParts=(r:Issue)=>r.shift?[r.shift.type_snapshot?.name,clinic(r.shift),day(r.shift.start),`${formatTime(r.shift.start,zone)}–${formatTime(r.shift.end,zone)}`]
    :[period(r.f)];
  const session=(r:Issue)=>sessionParts(r).filter(Boolean).join(' · ');
  const sessionNode=(r:Issue)=>el('span',{},...sessionParts(r).map((part,i)=>part&&(i>1||!r.shift&&r.f.start)?dateUnits([part]):part)
    .filter(Boolean).flatMap((part,i)=>[...(i?[' · ']:[]),part]));
  const today=new Date().toISOString().slice(0,10),ranks=new Map<string,string>();
  for(const m of h.rows('memberships'))if(m.starts_on<=today&&(!m.ends_on||m.ends_on>today)&&!ranks.has(m.person_id))ranks.set(m.person_id,h.name('categories',m.category_id));
  // Who: the person, or for an issue of the whole session (companion cover, a short session) the words "Whole session":
  // the session itself is in When & where, never repeated as if it were a person (LIVE-44, F5).
  const who=(r:Issue)=>r.f.person_id?h.name('people',r.f.person_id):r.shift?'Whole session':'—';
  const ruleText=(r:Issue)=>r.f.code==='UNAVAILABLE'?'Leave or absence':ruleName(r.f)||'—';
  // The page's key, after the table (dom.ts legend): the leave pill is its one mark, entered as it is drawn.
  const marks=tableKey({leave:[()=>badge('Leave','info','lg-mark'),'on leave or away']});
  const leave=(l:Row['leave']):Child=>{const p=leavePill(l);return p?marks.mark(p,'leave'):'';};
  // Dates stay whole while the recorded explanation wraps normally.
  const detail=(r:Issue):Child=>r.f.leave?el('span',{},dateUnits([leaveDates(r.f.leave)]),r.f.leave.note?' · '+r.f.leave.note:'',alsoShort(r.f)?' · '+alsoShort(r.f):''):findingNumbers(r.f);
  // What the fill panel keeps in view for a session's issue: its rule (or kind), why, and its numbers.
  const fillIssue=(r:Issue):FillIssue=>({key:r.key,title:kind(r),detail:[causeText(r.f)||r.f.message,findingNumbers(r.f)].filter(Boolean).join(' · '),...(r.f.rule_id?{rule_id:r.f.rule_id}:{}),severity:r.f.severity});
  // What is wrong, in plain words first: the server's cause ("there are no UCC sessions in this rota: needs 1; 0
  // exist…") or what the check said; then how strong the rule is and the numbers behind it (F5).
  const plain=(r:Issue)=>{const f=r.f,cause=causeText(f),msg=f.message&&f.message!==ruleName(f)?String(f.message):'';
    return cause||msg||(f.rule_id?`${ruleName(f)} not met`:String(f.message||''));};
  const reason=(r:Issue):Child=>{const f=r.f;
    if(f.code==='UNAVAILABLE')return el('div',{class:'issue-reason'},f.leave?leave(f.leave):el('span',{},issueKind(f)),f.leave?el('small',{class:'muted'},detail(r)):'');
    const numbers=findingNumbers(f),strength=f.rule_id?(f.severity==='error'?'Mandatory':'Advisory'):'';
    return el('div',{class:'issue-reason'},el('span',{},plain(r)),strength||numbers?el('small',{class:'muted'},[strength,numbers].filter(Boolean).join(' · ')):'');};

  // ---- Replace preview: the Fix column says who would take each place ----
  let preview:Preview|null=null;
  const planFor=(key:string)=>preview?.plan.find(p=>(p.keys as string[]).includes(key));
  const fix=(r:Issue):Child=>{
    if(r.saving)return savingMark(r.saving);
    if(r.failed)return failedMark(r.failed.retry);
    if(r.resolved)return savedMark(r.resolved);
    const p=planFor(r.key);if(!p)return '';
    return p.replacement_id?el('strong',{},'→ '+p.replacement_name):el('span',{class:'muted'},p.why||'No one free: stays open');};

  // ---- Selection and the one bar of batch actions (selection_bar.ts) ----
  const selected=new Set<string>(),bar=selectionBar({label:'Bulk actions'});
  const open=()=>rows.filter(r=>!r.resolved);
  const picked=()=>open().filter(r=>selected.has(r.key));
  const advisoryOnly=()=>picked().every(r=>r.f.severity!=='error');
  const excepted=(r:Issue)=>{const p=rule(r.f);return !!(p&&p.status==='active'&&allowsException(p)&&r.f.person_id&&(r.shift||r.f.start));};
  const keys=()=>picked().map(r=>r.key);
  // Actions follow what is selected: a person on a session can be replaced or unassigned; an open place is filled.
  const actions=():SelectionAction[]=>{const people=picked().some(r=>r.f.person_id&&r.shift),gap=picked().find(r=>r.shift&&!r.f.person_id);return [
    ...(gap?[{label:'Find someone…',icon:'people' as const,primary:!people,tip:'Who can work this session, and why others cannot',run:()=>h.fill(w.id,gap.shift!,fillIssue(gap))}]:[]),
    ...(people?[{label:'Replace…',icon:'swap' as const,primary:true,tip:'Best available person for each, previewed first',run:()=>startPreview()},
      {label:'Unassign',icon:'close' as const,run:()=>resolve('unassign',keys(),'Unassigned')}]:[]),
    // Only what the selection allows is offered, never an action that would then refuse (G-36).
    ...(picked().some(excepted)?[{label:'Exception…',icon:'shield' as const,run:()=>{const chosen=picked().filter(excepted);
      h.exceptions(chosen.map(r=>({policy_id:r.f.rule_id,person_id:r.f.person_id,day:toLocal((r.shift?.start||r.f.start) as string,zone).slice(0,10)})),()=>{selected.clear();redraw();});}}]:[]),
    ...(advisoryOnly()?[{label:'Accept',icon:'check' as const,run:()=>resolve('accept',keys(),'Accepted')}]:[]),
    ...(picked().some(r=>r.f.shift_id)?[{label:'Remove sessions',icon:'trash' as const,danger:true,run:()=>resolve('remove',keys(),'Session removed')}]:[]),
  ];};
  const synced=()=>{const n=picked().length,kinds=[...new Set(picked().map(kind))];
    bar.show(fresh&&n?{count:n,noun:['issue','issues'],detail:`issues · ${kinds.length===1?kinds[0]:plural(kinds.length,'reason')}`,clear:()=>{selected.clear();redraw();},actions:actions()}:null);status();};

  // ---- Table ----
  const facet=(r:Issue,value:string)=>r.resolved?'Resolved':value;
  const columns:Column<Issue>[]=[
    {key:'person',label:'Person',search:'related',text:who,facet:r=>facet(r,r.f.person_id?who(r):'—'),cell:r=>r.f.person_id?el('strong',{},who(r)):el('span',{class:'muted'},who(r))},
    {key:'reason',label:'Reason',text:r=>[kind(r),plain(r),r.f.leave?.type,r.f.leave?.note].filter(Boolean).join(' '),facet:r=>facet(r,kind(r)),cell:reason},
    // A period ("4–10 Oct 2026") is also found by its first whole date ("4 Oct 2026").
    {key:'session',label:'When & where',text:r=>{const a=(r.f.window as {start?:string}|undefined)?.start||r.f.start;
      return !r.shift&&a?`${session(r)} ${formatDate(a,zone)}`:session(r);},cell:sessionNode},
    {key:'rule',label:'Rule',search:'related',text:ruleText,facet:r=>facet(r,ruleText(r)),cell:r=>r.f.rule_id&&h.openRule?button(ruleText(r),()=>h.openRule!(r.f.rule_id,r.f.person_id),'link-button'):ruleText(r)},
    {key:'fix',label:'Fix',cell:fix},
    {key:'day',label:'Day',hidden:true,cell:()=>'',ordered:true,sort:r=>String(r.shift?.start||r.f.start||''),facet:r=>facet(r,r.shift?day(r.shift.start):r.f.start?formatDate(r.f.start,zone):'—')},
    {key:'rank',label:'Rank',hidden:true,cell:()=>'',facet:r=>facet(r,ranks.get(r.f.person_id)||'—')},
    {key:'clinic',label:'Clinic',hidden:true,cell:()=>'',facet:r=>facet(r,r.shift?clinic(r.shift)||'—':'—')},
    {key:'type',label:'Clinic type',hidden:true,cell:()=>'',facet:r=>facet(r,r.shift?.type_snapshot?.name||'—')},
    {key:'leave',label:'Leave type',hidden:true,cell:()=>'',facet:r=>facet(r,r.f.leave?.type||(r.f.code==='UNAVAILABLE'?'Not shown':'—'))},
    {key:'severity',label:'Severity',hidden:true,cell:()=>'',facet:r=>facet(r,r.f.severity==='error'?'Mandatory':'Advisory')},
    {key:'cause',label:'Cause',hidden:true,cell:()=>'',facet:r=>{const c=causeOf.get(r.key);return facet(r,c?`${c} · ${kind(r)}`:kind(r));}},  // the rule named too: two rules' causes never share a group
  ];
  const table=dataTable<Issue>({id:'resolve-issues',noun:['issue','issues'],rows,rowId:r=>r.key,columns,pageSize:2000,groupLimit:4,
    searchLabel:'Search issues',searchPlaceholder:'Person, clinic or day',
    // Person and Rule hide when no issue has either (an open place only): no columns of dashes (G-20).
    defaults:{group:causeOf.size?'cause':'reason',sort:['day','asc'],hidden:['person','rule'].filter(k=>rows.every(r=>k==='person'?!r.f.person_id:!r.f.rule_id&&r.f.code!=='UNAVAILABLE'))},
    views:()=>[{label:'All issues',filters:{severity:[]}},{label:'Mandatory',filters:{severity:['Mandatory']}},{label:'Advisory',filters:{severity:['Advisory']}}],
    empty:['No issues','The rota check found nothing to resolve.'],
    ...(canEdit&&fresh?{selection:{set:selected,changed:synced},selectable:(r:Issue)=>fresh&&!r.resolved}:{}),
    rowLabel:r=>[r.f.person_id?who(r):'',kind(r),session(r)].filter(Boolean).join(' · '),
    // A cause group: its cause card(s) and fixes, the same as the Schedule's (counts here stay one row per finding).
    groupAction:(_,list)=>causeButton(list),
    // A person's issue over a period (no session or day) is titled by the person, never a dash.
    card:(r,view)=>{const dated=!!(r.shift||r.f.start);return {title:el('span',{},el('strong',{},dated?sessionNode(r):who(r)),r.saving||r.failed?el('span',{class:'card-save'},' ',fix(r)):''),
      line:el('span',{},...[r.f.person_id&&dated?el('span',{},who(r),r.f.leave?' ':'',leave(r.f.leave)):r.f.leave?leave(r.f.leave):'',
        // Grouped by reason or by cause (whose head names the reason too), the group says it: the card does not repeat it.
        (r.resolved||!['reason','cause'].includes(view.group||'')&&!['reason','cause'].includes(view.then||''))&&!r.f.leave?kind(r):'',r.f.leave?detail(r):[plain(r),findingNumbers(r.f)].filter(Boolean).join(' · '),
        planFor(r.key)?.replacement_name?'→ '+planFor(r.key)!.replacement_name:planFor(r.key)?.why||r.resolved||''].filter(Boolean).flatMap((part,i)=>[...(i?[' · ']:[]),part]))};},
    // A session short of people gets its fix on the row (who can work it), not only after ticking it for the bulk bar.
    rowButtons:r=>r.shift?[...(canEdit&&fresh&&!r.resolved&&!r.f.person_id?[button('Find someone…',()=>h.fill(w.id,r.shift!,fillIssue(r)),'secondary small')]:[]),
      // A person breaking a mandatory rule: Replace on the row, previewed first like the bar's (SR-20: no dead end).
      ...(canEdit&&fresh&&!r.resolved&&r.f.person_id&&r.f.severity==='error'?[button('Replace…',()=>{selected.clear();selected.add(r.key);void startPreview().catch(reportError);},'secondary small')]:[]),
      // A double booking: cancel either session from its row (Undo in the message).
      ...(!r.resolved&&r.f.detail?.overlap?cancelButtons(r.f.detail.overlap as Clash,reload):[]),
      // A person's limit dated on this session: the same exception the bar offers, where the rule allows one (never safety).
      ...(canEdit&&fresh&&!r.resolved&&excepted(r)?[button('Exception…',()=>h.exceptions([{policy_id:r.f.rule_id,person_id:r.f.person_id,day:toLocal(r.shift!.start as string,zone).slice(0,10)}],()=>redraw()),'ghost small')]:[]),
      button('Show on schedule',()=>h.showOnSchedule(w.id,r.shift!),'ghost small')]  // said in words, not an unexplained icon
      // An issue over a period (no session): its rule for that person (Which rule applies, the numbers and lines), an
      // exception where the rule allows one, and Accept for an advisory one — the same actions the bar offers (SR-20).
      :r.resolved?[]:[...(r.f.rule_id&&h.openRule?[fixMenu(r)||button('Open rule',()=>h.openRule!(r.f.rule_id,r.f.person_id),'secondary small')]:[]),
        ...(canEdit&&fresh&&excepted(r)?[button('Exception…',()=>h.exceptions([{policy_id:r.f.rule_id,person_id:r.f.person_id,day:toLocal(r.f.start as string,zone).slice(0,10)}],()=>redraw()),'ghost small')]:[]),
        ...(canEdit&&fresh&&r.f.severity!=='error'?[button('Accept',()=>void resolve('accept',[r.key],'Accepted').catch(reportError),'ghost small')]:[])]});
  // B4: a person short under a catalogue line gets ⋯ (Relax for <name>…, Open line N) where its binding line is known.
  function fixMenu(r:Issue):HTMLElement|null{const f=r.f,p=rule(f),d=f.detail||{},lines=(p?.definition?.rule?.lines||[]) as Row[];
    const n=d.line_id?lines.findIndex(l=>l.id===d.line_id):typeof d.line==='number'?d.line:-1,win=(f.window||{}) as Row;
    if(!canEdit||!fresh||!h.context||!f.person_id||f.severity!=='error'||n<0||!lines[n]||!(win.start||f.start))return null;
    const day=(x:string)=>toLocal(x,zone).slice(0,10);
    return personFixMenu(h.context(),{rule_id:f.rule_id,person_id:f.person_id,person_name:who(r),binding_line_id:lines[n].id,binding_line_no:n+1,
      week_from:day(win.start||f.start),week_to:day(win.end||f.end||win.start||f.start),workspace_id:w.id,workspace_name:w.name,workspace_revision:w.revision,policy:p},{openLine:()=>h.openRule!(f.rule_id,f.person_id)});}
  const redraw=()=>{table.refresh();synced();};
  // A draft edit is checked again by the server; a change elsewhere leaves this check stale, which the page then says.
  async function reload(){if(!target.isConnected)return;target.replaceChildren();await issuesPage(target,workspaceId,h);}
  // A redraw replaces the rows: a focused checkbox or button hands focus to the same one (same name) in the new rows.
  const keepFocus=()=>{const a=document.activeElement as HTMLElement|null,name=a&&table.root.contains(a)?a.getAttribute('aria-label'):null;redraw();
    if(name&&!a!.isConnected)[...table.root.querySelectorAll<HTMLElement>('[aria-label]')].find(n=>n.getAttribute('aria-label')===name)?.focus({preventScroll:true});};

  // ---- Header: what this is and the counts (the reasons are the groups below, not repeated here) ----
  const counts=el('p',{class:'muted issues-counts'});
  const foot=el('span',{});
  /** The cause cards of one group, while this check is current (a stale check lists no fixes). */
  function causeButton(list:Issue[]):Child{
    const keys=list.map(r=>r.key).filter(k=>causeCodes.has(k));if(!fresh||!h.causeCards||!keys.length)return '';
    const codes=[...new Set(keys.flatMap(k=>[...causeCodes.get(k)!]))];
    const b=button('Why and how to fix',()=>popover(b,(panel,close)=>panel.append(h.causeCards!(data,keys,close)),
      {label:'Cause and fixes',placement:'below'}),'ghost small',{'data-causes':codes.join(' ')});
    return b;}
  function status(){
    const left=open(),done=rows.length-left.length;
    // The counts are said once, here (the empty state says "No issues" on its own); the footer adds only the selection.
    counts.hidden=!rows.length;
    counts.textContent=`${left.length} open · ${done} resolved${!fresh?' · needs checking':canEdit?'':' · read only'}`;
    foot.textContent=picked().length?`${picked().length} selected`:'';
  }
  const banner=el('div',{class:'notice warning issues-preview',role:'status'});banner.hidden=true;
  const cancelPreview=()=>{preview=null;banner.hidden=true;redraw();};
  const staleCheck=(e:unknown)=>{if(e instanceof ApiError&&e.code==='STALE_CHECK'){fresh=false;selected.clear();preview=null;banner.hidden=true;checkNotice.hidden=false;redraw();}};
  banner.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();cancelPreview();}});

  async function startPreview(){
    const keys=picked().filter(r=>r.f.person_id&&r.shift).map(r=>r.key);
    if(!keys.length){notify('Replace needs issues about a person on a session.','warning');return;}
    let result:Row;
    try{result=await request<Row>('/api/planner/issues/replace-preview',{method:'POST',body:JSON.stringify({workspace_id:w.id,expected_revision:w.revision,keys})});}
    catch(e){staleCheck(e);throw e;}
    preview={plan:result.plan,plan_hash:result.plan_hash,replaceable:result.replaceable,open:result.open,keys:new Set(keys)};
    const apply=button(`Apply ${plural(result.replaceable,'replacement')}`,()=>resolve('replace',[...preview!.keys],'Replaced',preview!.plan_hash),'primary',{disabled:!result.replaceable,why:'No one free can replace these.'});
    const cancel=button('Cancel',cancelPreview,'secondary');
    banner.replaceChildren(el('p',{},el('strong',{},'Replace with best available'),
      ` · ${result.replaceable} of ${keys.length} have someone free who no rule stops (shown → in the table)`+(result.open?` · ${result.open} stay open (no one free)`:'')+'.'),
      el('div',{class:'actions'},cancel,apply));
    banner.hidden=false;redraw();(apply.disabled?cancel:apply).focus();
  }

  /** A batch's reply, or its Undo's (the same shape): the rota, the sessions touched and removed. */
  function patch(result:Row){
    w={...w,...result.workspace};
    // Successful batches and Undo revalidate; compact command replies carry that check at the top level.
    const checked=result.validation||result.workspace?.validation;
    if(checked){w.validation=checked;fresh=!!checked.fingerprint;checkNotice.hidden=fresh;}
    for(const s of result.shifts as Row[])shifts.set(s.id,s);
    for(const id of result.removed_shift_ids as string[])shifts.delete(id);
    for(const r of rows)if(r.shift)r.shift=shifts.get(r.shift.id)||r.shift;
  }
  // Undo patches the table from its reply too: issues that came back are open again (an undone Accept as well), issues
  // the undone batch brought in (e.g. a companion issue on a session it changed) leave the table, the others stay
  // resolved; selection, scroll and focus stay. Not this draft's undo: the app reads the page again.
  undoInPlace(done=>{
    if(!target.isConnected||!done.every(d=>(d.kind==='workspace.undo'||d.kind==='workspace.redo')&&d.result?.workspace?.id===w.id&&d.result.issues))return false;
    for(const {result} of done){patch(result);
      const gone=new Set<string>(result.issues.removed),accepted=new Set<string>((w.accepted_issues||[]).map((a:Row)=>a.key));
      rows.splice(0,rows.length,...rows.filter(r=>!gone.has(r.key)));
      for(const r of rows)if(r.resolved==='Accepted'&&!accepted.has(r.key))r.resolved=undefined;
      for(const f of result.issues.added as Row[])add(f);}
    preview=null;banner.hidden=true;undoSlot.replaceChildren();for(const k of [...selected])if(!open().some(r=>r.key===k))selected.delete(k);
    keepFocus();return true;
  });

  /** One batch: one command, then the table is patched from its reply (no reload). Optimistic: the rows read as saving
   *  at once (who takes each place, or what is done); a refusal puts them back with "Couldn't save · Retry" (no Retry
   *  when the rota changed meanwhile: the toast asks to reload). */
  const SAVING:Record<string,string>={Replaced:'Replacing',Unassigned:'Unassigning',Accepted:'Accepting','Session removed':'Removing session'};
  async function resolve(action:string,keys:string[],how:string,planHash?:string){
    const mine=rows.filter(r=>keys.includes(r.key)&&!r.resolved);
    for(const r of mine){const p=planFor(r.key);r.failed=undefined;r.saving=p?.replacement_name?'→ '+p.replacement_name:SAVING[how]||how;}
    redraw();
    let result:Row;
    try{result=await command<Row>('issues.resolve',{workspace_id:w.id,action,keys,reason:`Fix list: ${how.toLowerCase()} (${keys.length})`,...(planHash?{plan_hash:planHash}:{})},undefined,w.revision);}
    catch(e){
      // Declined before sending (Edit the draft now? › Cancel): nothing was tried, so the rows and selection stay as they were.
      if(e instanceof ApiError&&e.code==='PAGE_LEFT'){for(const r of mine)r.saving=undefined;redraw();throw e;}
      for(const r of mine){r.saving=undefined;r.failed={retry:conflict(e)?undefined:()=>void resolve(action,keys,how,planHash).catch(reportError)};}staleCheck(e);redraw();throw e;}
    for(const r of mine)r.saving=undefined;
    patch(result);
    const gone=new Set<string>(result.issues.removed),plan=new Map<string,Row>();
    for(const p of (result.plan||[]) as Row[])for(const k of p.keys)plan.set(k,p);
    let fixed=0;
    for(const r of rows){
      if(gone.has(r.key)&&!r.resolved&&++fixed)r.resolved=plan.get(r.key)?.replacement_name?`Replaced by ${plan.get(r.key)!.replacement_name}`:keys.includes(r.key)?how:'Resolved';
      else if(action==='accept'&&keys.includes(r.key)&&!r.resolved&&++fixed)r.resolved='Accepted';
    }
    for(const f of result.issues.added as Row[])add(f);
    preview=null;banner.hidden=true;selected.clear();redraw();
    const said=`${plural(fixed,'issue')} resolved${result.issues.added.length?` · ${plural(result.issues.added.length,'new issue')}`:''}.`;
    offerUndo(said);
  }
  /** The batch's Undo: on its message and, until the next batch or leaving the page, as "Undo last batch" in the header
   *  (a message times out; the Undo must not). Either runs it once; the other then goes. */
  const undoSlot=el('span',{class:'issues-undo'});
  function offerUndo(said:string){
    const undo=takeUndo();undoSlot.replaceChildren();
    if(!undo){notify(said);return;}
    let used=false,dismiss:(()=>void)|undefined;
    const run=async()=>{if(used)return;used=true;dismiss?.();undoSlot.replaceChildren();
      try{await undo.run();notify(undo.done||'Undone. The previous arrangement is back and checked again.');}catch(e){reportError(e);}};
    undoSlot.append(button('Undo last batch',run,'secondary',{'data-tip':'Undo: '+said}));
    dismiss=notify(said,'success',{label:'Undo',run});
  }

  status();
  target.append(el('div',{class:'issues-head'},el('div',{},el('h2',{},`All checks · ${(w.name||'').replace(/\s+issues$/i,'')||w.name}`),counts),
      el('div',{class:'actions'},undoSlot,h.fixList?button('Fix list',()=>h.fixList!(w.id),'secondary'):'',button('Back to schedule',()=>h.back(w.id),'secondary'))),
    // The key explains only what the table shows (a leave pill), never badges that are not on it (G-20).
    banner,el('div',{class:'issues-list'},table.root,marks.watch(table.root),canEdit?bar.root:''),footerStatus(foot));
}
