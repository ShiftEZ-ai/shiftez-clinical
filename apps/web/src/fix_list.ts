import {groupSections,attributed,groupRow,groupFill,planningOf,type PlanningContext,type GroupState} from './groups_view.js';
import {issueGlyph,issueList,dayLabels,type Issue} from './issue_marker.js';
import {formatTime,formatDay,needRow,adoptPopover,footerAct,el,button,badge,icon,plural,popover,formatDate,departmentTimeZone,type Child,needText,legend,type Legend,emptyLine,readFailed} from './dom.js';
import {get,keptRead,ApiError,type KeptReadOptions} from './api.js';
import {CHECK_AGAIN,summaryCheckWords} from './rule_check_api.js';
import {addDays} from './planner_logic.js';
import {skeleton,swap,clock} from './loading.js';
import {collectionToolbar,builtinViewItems,savedViewItems,optionMenu,fetchViews,conditionFilter,joinFilter,mentions,type SavedView,type FilterState} from './table_view.js';
import {treeMatches,lineMatches} from './conditions.js';
import type {Row} from './types.js';
import type {Conditions,FindingFixPrefill,FixList as FixListWire,FixListCause,FixListDetail,FixListLean,FixListSummary,RotaReviewItem,RotaItemDecision} from './generated.js';
import {FIX_ACTION_LABELS} from './generated.js';

/** The Fix list (owner decision D9): ONE server object (packages/domain/rota_summary.py, GET /api/workspaces/{id}/fix-list,
 *  also attached as `fix_list` to rota summaries, lifecycle replies and Home items) drawn on its page, the footer, Home,
 *  Submit, the approver's review and the auto-schedule result. Two levels (Must fix / Could improve); the count is of
 *  causes; causes come ranked by checks cleared. The client never counts or regroups: every number here is the server's.
 *
 *  The types below narrow the generated maintenance contract (generated.ts FixList; the server sends dicts for scope, rule
 *  lines, who and since) to the fields this client reads: one place, and a renamed server field fails the build here. */
export type FixLevel=FixListCause['level'];
export type FixActionKind=FixListCause['action']['kind'];
export interface FixRuleLine{rule_id:string;line_id?:string|null;line:number|null;slot?:string|number|null;href?:string|null}
/** `prefills`: the exact canonical fix hints behind an add_sessions / open_eligibility action (clinic, duty, place,
 *  days, dates, person), each an alternative; dates already clipped to the draft. */
export interface FixScope{person_ids?:string[];shift_ids?:string[];starts_on?:string|null;ends_on?:string|null;keep_existing?:boolean;classification?:string;rule_lines?:FixRuleLine[];prefills?:FindingFixPrefill[]}
export interface FixAcknowledgement{cause_id?:string;reason:string;by?:string|null;at?:string|null}
/** One evidence row: the source finding (person, session, rule), its affected sessions and its plain explanation. */
export interface FixEvidence{code?:string;person_id?:string|null;person_name?:string|null;shift_id?:string|null;rule_id?:string|null;start?:string|null;end?:string|null;
  outside_rota?:boolean|null;shift_ids?:string[];explanation?:string|null;detail?:Record<string,unknown>|null;
  /** maintenance: the sessions' clinic, place and duty type from the checked snapshot, the source rule's name and why (the
   *  measured eligible count and dates). */
  sessions?:{shift_id:string;service_names?:string[];location_name?:string|null;assignment_type_name?:string|null}[];rule_name?:string|null;reason?:string|null}
/** Fix kinds done in place by Assign (the Fix list's fill box), not by opening another step. */
export const ASSIGN_KINDS=new Set<string>(['assign','assign_supervisor']);
/** The server's presentation (packages/domain/fix_list_facts.py, FIXLIST-BACKEND) is the generated contract's:
 *  `detail_line` (the plain line under the sentence), `session_findings` (each session's finding key), `fixes` (best
 *  first; never a waiver for a safety kind) and `group` (Could improve, by what to do). */
export type FixCause=Omit<FixListCause,'action'|'rule_lines'|'who'|'evidence'|'acknowledgement'>&{action:Omit<FixListCause['action'],'scope'>&{scope:FixScope};
  rule_lines:FixRuleLine[];who:Conditions[];evidence:FixEvidence[];acknowledgement?:FixAcknowledgement|null};
/** `checking`: a recheck is running (keep the last counts, with a spinner); `since`: causes cleared / new today. */
export type FixSummary=Omit<FixListSummary,'since'>&{since?:{at?:string;cleared:number;new:number}|null;
  /** The check's words where its job is known (the Planner: checkingWords); else the summary's (summaryCheckWords). */checking_words?:string;
  /** When the check began (epoch ms; the Planner's one clock, from the server's stale_since): "· m:ss" after its words. */checking_since?:number;
  /** Must fix the server marked New on the held list (#1437's late must fix first): "n must fix · 1 new". */added?:number;
  check_stop?:{lead:string;cause:string;tryAgain?:()=>void}};
export type FixList=Omit<FixListWire,'summary'|'causes'|'review_items'>&{summary:FixSummary;causes:FixCause[];review_items?:ReviewItem[]};
export type ReviewItem=Omit<RotaReviewItem,'source'>&{source:Row};
export type ReviewDecision=RotaItemDecision;

const held=new Map<string,FixList>();
const deferredEvidence=new WeakMap<FixCause,()=>Promise<void>>(),deferredLists=new WeakMap<FixList,()=>Promise<void>>();
const CHANGED='The Fix list changed; refresh it before opening details';
const fixPath=(id:string,group?:string|null)=>`/api/workspaces/${encodeURIComponent(id)}/fix-list?${group?'group_version_id='+encodeURIComponent(group)+'&':''}`;
/** Row facts (LEAD-UIUX, 8 Nov 2026): THE one place the lean view (`view=lean`) becomes the Fix list every surface draws.
 *  A lean row carries what a collapsed row shows and does (sentence, Safety / New, its line, its action and scope, its
 *  sessions and finding keys); what only Show evidence needs is empty here until that cause's detail lands (causeDetail).
 *  A field moved between the lean and detail views changes here only. */
type LeanCause=FixListLean['causes'][number];
// A lean scope's dates are one object, the client's scope (FixScope) has them at its top; a scope without shift_ids
// works on the cause's own sessions (the lean view need not send that list a second time).
const leanAction=(c:LeanCause,a:LeanCause['action'])=>({...a,scope:{...a.scope,...a.scope?.dates,
  shift_ids:a.scope?.shift_ids??c.shift_ids}});
const rowFacts=(l:FixListLean):FixList=>({...l,summary:l.summary as FixSummary,review_items:l.review_items as ReviewItem[]|undefined,
  causes:l.causes.map(c=>({evidence:[],who:[],missing:[],...c,
    // Every finding key, the per-session ones included once (session_findings), however the lean view sends them.
    finding_keys:[...new Set([...c.finding_keys||[],...Object.values(c.session_findings||{})])],
    // Its first rule and binding line (line_id, slot: tests/test_fix_list_row_facts.py), the rest with the detail.
    rule_lines:c.rule_id?[{rule_id:c.rule_id,line:null,line_id:(c as {line_id?:string}).line_id,slot:(c as {slot?:string|number}).slot}]:[],
    action:leanAction(c,c.action),fixes:(c.fixes||[]).map(f=>leanAction(c,f))}) as unknown as FixCause)});
/** One cause's detail (its evidence, rule lines, who), read once per [rota, revision, fingerprint, cause] through api.ts
 *  keptRead: a second Show evidence, or the row drawn again, reads nothing. */
const DETAILS:KeptReadOptions={byId:['workspace'],cap:64};
const detailRead=(id:string,group:string|null|undefined,l:FixList,cause:string)=>{
  const q=['view=detail','cause_id='+encodeURIComponent(cause),...(l.revision!=null?['revision='+l.revision]:[]),...(l.fingerprint?['fingerprint='+encodeURIComponent(l.fingerprint)]:[])].join('&');
  return keptRead(`${id}|${l.revision??''}|${l.fingerprint??''}|${cause}|${group||''}`,()=>get<FixListDetail>(fixPath(id,group)+q).then(d=>d.cause as unknown as FixCause),DETAILS).p;};
/** The rota moved on since the list was read (the revision or fingerprint guard, or the cause is gone): read the lean
 *  list again, then that cause's detail once under it. */
const STALE=new Set(['STALE_REVISION','STALE_INPUTS','FIX_LIST_CHANGED','NOT_FOUND']);
async function causeDetail(id:string,group:string|null|undefined,l:FixList,cause:string):Promise<FixCause>{
  try{return await detailRead(id,group,l,cause);}
  catch(error){if(!(error instanceof ApiError&&STALE.has(error.code)))throw error;
    const fresh=await readFixList(id,group);if(!fresh.causes.some(c=>c.id===cause))throw new Error(CHANGED);
    return detailRead(id,group,fresh,cause);}
}
/** A cause's detail, loaded into it (null: nothing left to load). The row's Show evidence, the page's search. */
export const loadDetail=(c:FixCause)=>deferredEvidence.get(c)?.()||null;
/** `group`: the scheduling group's effective version the causes are classified for (P2-BE group_version_id). Opens on
 *  the lean view only; details load per cause (loadDetail). */
export const readFixList=(id:string,group?:string|null):Promise<FixList>=>{
  const path=fixPath(id,group);
  return get<FixListLean>(path+'view=lean').then(lean=>{const l=rowFacts(lean);
    const apply=(cause:FixCause,full:FixCause)=>{Object.assign(cause,full);deferredEvidence.delete(cause);};
    for(const cause of l.causes){let pending:Promise<void>|undefined;
      deferredEvidence.set(cause,()=>pending||=causeDetail(id,group,l,cause.id).then(full=>apply(cause,full)).catch(error=>{pending=undefined;throw error;}));}
    // Search includes evidence text: hydrate once for the collection, never one request per row.
    deferredLists.set(l,async()=>{const full=await get<FixList>(path+'view=full');if(full.revision!==l.revision)throw new Error(CHANGED);
      const byId=new Map(full.causes.map(c=>[c.id,c]));
      for(const cause of l.causes){const f=byId.get(cause.id);if(!f)throw new Error(CHANGED);apply(cause,f);}deferredLists.delete(l);});
    if(!group)held.set(id,l);return l;});
};
/** The Fix list this page last read for the rota, while it is still the check `s` describes (F-10: the footer popover
 *  opens on it at once instead of reading it again). */
export const heldFixList=(id:string,s:FixSummary):FixList|null=>{const l=held.get(id);
  return l&&l.summary.checked_at===s.checked_at&&l.summary.must===s.must&&l.summary.could===s.could?l:null;};
/** The rota's Fix list this page last read (whole rota), or null. */
export const lastFixList=(id:string)=>held.get(id)||null;
/** The Fix list wherever the server attached it (a rota summary, a Home item, a lifecycle reply), or null. */
export const fixListOf=(x:unknown):Partial<FixList>|null=>(x as {fix_list?:Partial<FixList>}|null|undefined)?.fix_list||null;
export const fixSummaryOf=(x:unknown):FixSummary|null=>fixListOf(x)?.summary||null;

import {LEVEL_WORDS} from './issue_marker.js';
const LEVEL_HELP:Record<FixLevel,string>={must:'Breaks a mandatory rule, so the rota cannot be submitted. Safety rules cannot be waived: assign someone or change the rule.',
  could:'Advisory goals and fairness: they never block submitting and only move the score.'};
export const actionLabel=(c:FixCause)=>c.action?(c.action.kind==='choose_cover'&&c.action.scope?.classification==='duties_during_approved_leave'?'Find cover / replace':FIX_ACTION_LABELS[c.action.kind]||'Open'):'';
/** "Checked 2 min ago", from the server's checked_at (never the client's clock for anything but the distance). */
export function checkedAgo(iso:string|null|undefined,word='Checked'){
  if(!iso)return '';const m=Math.round((Date.now()-Date.parse(iso))/6e4);
  return `${word} ${m<1?'just now':m<60?`${m} min ago`:m<1440?`${Math.round(m/60)} h ago`:formatDate(iso,departmentTimeZone())}`;
}
export const mustWords=(n:number)=>`${n} must fix`;
/** SW3-B-11: the reader's own next step on this rota, which "ready" leads to; null when they have none (no verb). */
export type FixStep='submit'|'approve'|'publish'|null;
export const couldWords=(n:number)=>`${n} could improve`;

/** What a Fix list row may do. Without `act` it reads only (Submit, the footer popover, the approver's review). */
export interface FixHooks{
  addSession?:()=>unknown;
  act?:(c:FixCause)=>unknown;
  openLine?:(l:FixRuleLine)=>unknown;
  ruleName?:(id:string)=>string;
  /** Names for ids inside evidence rows (person_id → the person). */
  label?:(key:string,id:string)=>string;
  /** A session's start (ISO), for its chip's day and time in the one issue list (causeIssues). */
  start?:(shiftId:string)=>string|undefined;
  /** R3: the Schedule in focus on this cause, stepping through `all`; offered where `shows` (it has sessions). */
  show?:(c:FixCause,all:FixCause[])=>unknown;shows?:(c:FixCause)=>boolean;
  /** The causes a row steps through in focus mode (the page's list). */
  all?:FixCause[];
  /** A cause's other fixes, shown with its evidence (never a second primary). */
  fixes?:(c:FixCause)=>HTMLElement[];
  /** FIXLIST-UX: the cause's sessions as date chips (where it is), or null (issue_marker.ts sessionChips: words, no hue). */
  chips?:(c:FixCause)=>HTMLElement|null;
  /** Set by the list that draws the rows (the panel, the page): its key, under the rows (dom.ts legend; every mark has one). */
  key?:Legend;
  /** The fix in place for the cause in focus (Assign with the best candidates for its session in focus), or null. */
  fill?:(c:FixCause)=>HTMLElement|null;
  /** A cause one Assign can fix and not in focus: its button puts it in focus. Null: the cause's own action. */
  assign?:(c:FixCause)=>(()=>void)|null;
  /** Set by the list that draws the rows: draw them again (a chip moved the focus). */
  redraw?:()=>void;
}
/** A fix kind's word comes from the contract (FIX_ACTION_LABELS, FIXLIST-BACKEND §3), only when the server offers that fix. */
export const fixLabel=(c:FixCause,kind:string,fallback:string)=>{const f=c.fixes?.find(x=>x.kind===kind);return f?FIX_ACTION_LABELS[f.kind]||fallback:fallback;};
/** The line under the sentence: the server's plain one (`detail_line`, which already says what is missing from
 *  `missing`: "5 shifts · each has 1 of 2 Consultants"). The sessions' shared pattern (`where`) leads it only when the
 *  sentence does not already say it, so the dates in the chips have their name. Never checks or score numbers (the
 *  scorer's units, not the coordinator's). */
export function fixLine(c:FixCause){const w=c.where?.label||'',line=c.detail_line||'';
  return w&&!c.sentence.toLowerCase().includes(w.toLowerCase())?[w,line].filter(Boolean).join(' · '):line;}
/** Compact numbers a finding carries ("required 3 · scheduled 2"), in this order. */
const DETAIL_NUMBERS=['required','scheduled','minimum','maximum','available','actual','limit'];
/** A cause's evidence behind ▾: its plain explanation, one row per finding (person, session, the numbers it found) and
 *  the exact rule lines. */
function evidence(c:FixCause,h:FixHooks,own?:HTMLElement|null):HTMLElement|null{
  const rows=c.evidence||[],parts:Child[]=[],zone=departmentTimeZone();
  for(const text of [...new Set(rows.flatMap(r=>[r.explanation,r.reason]).filter((x):x is string=>!!x))].slice(0,3))parts.push(el('p',{},text));
  const person=(r:FixEvidence)=>r.person_name||(r.person_id&&h.label?h.label('people',r.person_id):r.person_id||'');
  const where=(r:FixEvidence)=>[...new Set((r.sessions||[]).flatMap(x=>x.service_names?.length?x.service_names:[x.assignment_type_name||x.location_name||'']).filter(Boolean))].join(', ');
  const session=(r:FixEvidence)=>[r.shift_id&&h.label?h.label('shifts',r.shift_id):r.start?formatDate(r.start,zone):'',where(r),r.outside_rota?'outside this rota':''].filter(Boolean).join(' · ');
  const rule=(r:FixEvidence)=>r.rule_name||'';
  const found=(r:FixEvidence)=>DETAIL_NUMBERS.filter(k=>typeof r.detail?.[k]==='number').map(k=>`${k} ${r.detail![k]}`).join(' · ');
  const cols=([['Person',person],['Session',session],['Rule',rule],['Found',found]] as [string,(r:FixEvidence)=>string][]).filter(([,f])=>rows.some(r=>f(r)));
  // Rows that read the same (one per rule line or per session of one slot) are one row with their count (ME-13).
  const merged=new Map<string,{cells:string[];n:number}>();
  for(const r of rows){const cells=cols.map(([,f])=>f(r)||'—'),k=cells.join('\u0000'),m=merged.get(k);if(m)m.n++;else merged.set(k,{cells,n:1});}
  const distinct=[...merged.values()],LIMIT=50,counted=distinct.some(m=>m.n>1);
  if(cols.length){
    parts.push(el('table',{},el('thead',{},el('tr',{},...cols.map(([label])=>el('th',{},label)),counted?el('th',{},'Times'):'')),
      el('tbody',{},...distinct.slice(0,LIMIT).map(m=>el('tr',{},...m.cells.map(t=>el('td',{},t)),counted?el('td',{},m.n>1?`×${m.n}`:''):'')))));
    if(distinct.length>LIMIT)parts.push(el('p',{class:'muted'},`And ${distinct.length-LIMIT} more.`));
  }
  if(h.show&&h.shows?.(c))parts.push(el('p',{},button('Show on schedule',()=>h.show!(c,h.all||[c]),'link-button')));
  // With Assign as the row's fix, the cause's own action (a scoped run, the rule) is one of its other fixes here.
  const more=h.act?[...(own?[own]:[]),...h.fixes?.(c)||[]]:[];if(more.length)parts.push(el('div',{class:'actions fix-more-actions'},...more));
  const lines=c.rule_lines||[];
  if(lines.length)parts.push(el('p',{},...lines.flatMap((l,i)=>{const label=`${h.ruleName?.(l.rule_id)||'Rule'}${l.line!=null?` · line ${l.line+1}`:''}`;
    return [...(i?[' · ']:[]),h.openLine?button(label,()=>h.openLine!(l),'link-button'):el('span',{},label)];})));
  return parts.length?el('div',{class:'fix-detail'},...parts):null;
}
/** The Safety tag, with why it matters in its tip (a safety rule is never waived: D3). */
const safetyTag=()=>{const b=badge('Safety','neutral','fix-tag fix-safety');b.dataset.tip='Safety rule: it cannot be waived. Assign someone or change the rule.';return b;};
let rowSerial=0;
/** SW3-A-23: the causes whose details are open, kept across redraws (a refresh no longer closes what is being read). */
const opened=new Set<string>();
/** One cause (R1, FIXLIST-UX): the level mark (a triangle for must fix, a dot for could improve), one sentence with its
 *  New / Safety tags, one plain muted line (fixLine), one action, ▾ for its evidence, then where it is (its session
 *  chips) and, for the cause in focus, its fix in place (Assign). `primary`: the surface's one primary action. Without
 *  hooks.act it reads only. `extra`: a node under the row (a reason). */
export function fixRow(c:FixCause,h:FixHooks,o:{primary?:boolean;extra?:Child;detail?:boolean;aside?:Child}={}):HTMLLIElement{
  const id=`fix-cause-${++rowSerial}`;
  // The level is the issue marker's meaning (must fix, could improve): one entry each in the surface's key (every mark has one).
  const mark=el('span',{class:'fix-mark '+c.level,role:'img','aria-label':LEVEL_WORDS[c.level]},c.level==='must'?icon('alert'):el('i',{}));
  h.key?.mark(mark,c.level==='must'?'issue:error':'issue:warning',issueGlyph(c.level==='must'?'error':'warning'),LEVEL_WORDS[c.level]);
  const meta=fixLine(c);
  // Every safety cause says so (it changes what may be done: no waiver), at both levels and on every surface.
  const text=needText([c.sentence,c.new?badge('New','info','fix-tag'):'',c.safety?safetyTag():''],meta,{id,cls:'fix-meta',tip:meta});  // SW-22: one line; cut, the shared hover card says it all, and focus in the row unclamps it
  const focus=h.act?h.fill?.(c)||null:null,assign=h.act&&!focus?h.assign?.(c)||null:null;
  const own=h.act&&c.action?button(actionLabel(c),()=>h.act!(c),(o.primary&&!focus&&!assign?'primary':'secondary')+' small',{'aria-describedby':id}):null;
  // Assign is the row's fix when one assignment can fix it; the cause's own action then joins its other fixes (▾).
  const act=focus?null:assign?button(fixLabel(c,'assign','Assign someone'),assign,(o.primary?'primary':'secondary')+' small',{'aria-describedby':id,'data-busy':'Applying…'}):own;  // #1436: its save, in the footer's words
  const aside=(focus||assign)&&!ASSIGN_KINDS.has(c.action?.kind)?own:null;  // the server's action is often Assign itself
  const detail=o.detail===false?null:deferredEvidence.has(c)?el('div',{class:'fix-detail'}):evidence(c,h,aside);
  // A worded toggle (no arrow glyph): "Show evidence" / "Hide evidence", its state in aria-expanded, its cause by aria-describedby.
  const more=detail?el('button',{type:'button',class:'link-button small fix-more','aria-expanded':'false','aria-controls':id+'-d','aria-describedby':id},'Show evidence'):null;
  const said=(open:boolean)=>{more!.setAttribute('aria-expanded',String(open));more!.textContent=open?'Hide evidence':'Show evidence';};
  if(detail&&more){detail.id=id+'-d';const was=opened.has(c.id);detail.hidden=!was;said(was);
    const load=async()=>{const read=loadDetail(c);if(!read)return;
      detail.replaceChildren(skeleton('lines','Loading details…'));detail.setAttribute('aria-busy','true');
      try{await read;detail.replaceChildren(...(evidence(c,h,aside)?.childNodes||[]));}
      catch(error){detail.replaceChildren(readFailed('the details',error,()=>void load()));}
      finally{detail.removeAttribute('aria-busy');}};
    if(was)void load();
    more.addEventListener('click',()=>{const open=more.getAttribute('aria-expanded')!=='true';said(open);detail.hidden=!open;
      if(open){opened.add(c.id);void load();}else opened.delete(c.id);});}
  return el('li',{'data-cause':c.id,'data-level':c.level,...(focus?{class:'fix-focus'}:{})},needRow(mark,text,[o.aside||'',act,more]),
    h.chips?.(c)||'',focus||'',detail||'',o.extra||'');
}
/** Causes one scoped auto-schedule can take together (R1 batch): no decision needed, only people and dates. */
export const batchable=(causes:FixCause[])=>causes.filter(c=>c.action?.kind==='autoschedule_scoped');
/** The batch's one cause: the union of their people and dates, keeping existing duties (reviewed before Apply). */
export function batchCause(causes:FixCause[]):FixCause{
  const scopes=causes.map(c=>c.action!.scope||{}),days=(k:'starts_on'|'ends_on')=>scopes.map(x=>x[k]).filter(Boolean).sort() as string[];
  return {...causes[0],id:'batch',checks:causes.reduce((n,c)=>n+c.checks,0),sentence:`${causes.length} causes`,
    action:{kind:'autoschedule_scoped',scope:{person_ids:[...new Set(scopes.flatMap(x=>x.person_ids||[]))],shift_ids:[...new Set(scopes.flatMap(x=>x.shift_ids||[]))],
      starts_on:days('starts_on')[0]||null,ends_on:days('ends_on').pop()||null,keep_existing:true}}};
}
const TAIL=5;
/** A level (R1): its heading and cause count; Must fix lists every cause, Could improve its top five then "N more · Show
 *  all". With two or more causes one scoped run can take, the head holds the surface's one primary, "Auto-schedule N of
 *  these"; otherwise (`primary`) the first row's action is primary. `collapsed` (the Planner's Fix list panel): the level
 *  opens with Show, as Could improve does there. Causes the server groups by what to do (`group`) are listed under it. */
export function fixLevel(level:FixLevel,causes:FixCause[],h:FixHooks,o:{primary?:boolean;tag?:'h2'|'h3';panel?:boolean;all?:boolean;heading?:string;collapsed?:boolean;row?:(c:FixCause)=>Parameters<typeof fixRow>[2]}={}):HTMLElement{
  const id=`fix-level-${level}-${++rowSerial}`;
  const help=el('button',{type:'button',class:'icon-button bare page-info','aria-label':'About '+LEVEL_WORDS[level],'data-help':'','data-tip':LEVEL_HELP[level]},icon('help'));
  const batch=o.primary&&h.act?batchable(causes):[],many=batch.length>1;
  const run=many?withIconPrimary(`Auto-schedule ${batch.length} of these`,()=>h.act!(batchCause(batch))):'';
  const grouped=causes.some(c=>c.group?.label);
  const shown=level==='must'||o.all||grouped||causes.length<=TAIL+1?causes:causes.slice(0,TAIL);
  const row=(c:FixCause,i=-1)=>fixRow(c,h,{primary:!many&&o.primary&&i===0,...o.row?.(c)});
  const lists:HTMLElement[]=[];
  // A collapsed level draws its rows on its first Show: the key enters only marks on screen (its chips' duty hues).
  const fill=()=>{if(lists.length)return;
    if(grouped){const by=new Map<string,FixCause[]>();for(const c of shown){const k=c.group?.label||'';by.set(k,[...(by.get(k)||[]),c]);}
      let i=0;for(const [label,list] of by)lists.push(...(label?[el(o.tag==='h2'||!o.tag?'h3':'h4',{class:'fix-group'},label)]:[]),el('ul',{class:'fix-list'},...list.map(c=>row(c,i++))));}
    else lists.push(el('ul',{class:'fix-list'},...shown.map((c,i)=>row(c,i))));
    const more:HTMLElement|null=rest.length?el('div',{class:'fix-tail'},el('span',{class:'muted'},`${rest.length} more`),
      button(`Show all ${causes.length}`,()=>{lists[0].append(...rest.map(c=>row(c)));more?.remove();},'link-button')):null;
    body.append(...lists,more||'');};
  const rest=causes.slice(shown.length),body=el('div',{class:'fix-level-body',id:id+'-b'});
  const toggle=o.collapsed?el('button',{type:'button',class:'link-button fix-toggle','aria-expanded':'false','aria-controls':id+'-b'},'Show'):null;
  if(toggle){body.hidden=true;toggle.addEventListener('click',()=>{const open=body.hidden;if(open)fill();body.hidden=!open;toggle.setAttribute('aria-expanded',String(open));toggle.textContent=open?'Hide':'Show';});}
  else fill();
  return el('section',{class:(o.panel===false?'fix-section':'panel fix-panel')+(level==='could'?' fix-quiet':''),'aria-labelledby':id,'data-level':level},
    el('div',{class:'fix-level'},el(o.tag||'h2',{id},o.heading||LEVEL_WORDS[level]),...counts(level,causes,!!o.heading),o.panel===false&&!o.collapsed?'':help,el('span',{class:'grow'}),run,toggle||''),
    body);
}
/** A level's count badge; a rule group (`mixed`) holds both levels, so its red badge counts only must-fix causes and
 *  the could-improve ones get their own neutral badge, each said in words (one unit per number, R). */
const counts=(level:FixLevel,causes:FixCause[],mixed:boolean)=>{
  if(!mixed)return [badge(String(causes.length),level==='must'?'err':'neutral')];
  const must=causes.filter(c=>c.level==='must').length,could=causes.length-must;
  return [must?badge(mustWords(must),'err'):'',could?badge(couldWords(could),'neutral'):''];};
const withIconPrimary=(label:string,run:()=>unknown)=>{const b=button(label,run,'primary small');b.prepend(icon('sparkle'));return b;};
/** The gate line (R1): what stops submitting, in must-fix causes (`scoped`: how many of them are in this view), beside
 *  the places filled, the day's cleared / new, and when it was checked; "Checking…" keeps the last counts meanwhile. */
export function fixGate(s:FixSummary,lead?:Child,scoped?:{count:number;view:string},step:FixStep='submit'):HTMLElement{
  if(s.check_stop){const {lead,cause,tryAgain}=s.check_stop;
    return el('div',{class:'fix-gate warn check-stopped'},icon('clock'),
      el('span',{class:'grow'},el('strong',{},lead),cause?' '+cause:''),
      tryAgain?button(CHECK_AGAIN,tryAgain,'secondary small'):'');}
  const blocked=s.must>0||!!s.submission_issue,places=s.places_total&&!scoped?`${s.places_filled} of ${s.places_total} places filled`:'';
  // SW3-A-22: said in words ("Today: 2 cleared · 1 new"), not a bare "−2 +1".
  const since=s.since&&(s.since.cleared||s.since.new)?el('span',{class:'fix-since'},'Today: ',
    s.since.cleared?el('span',{class:'up'},`${s.since.cleared} cleared`):'',s.since.cleared&&s.since.new?' · ':'',s.since.new?el('span',{class:'down'},`${s.since.new} new`):''):'';
  return el('div',{class:'fix-gate '+(blocked?'err':'ok'),...(s.checking_since?{}:{role:'status'})},icon(blocked?'alert':'check'),
    el('span',{class:'grow'},...(lead?[lead]:s.submission_issue?[el('strong',{},s.submission_issue.message)]:blocked?[el('strong',{},mustWords(s.must)),step==='submit'?' before you submit':'']:[el('strong',{},'Nothing must be fixed'),step?` · ready to ${step}`:'']),
      scoped?[' · ',el('strong',{},String(scoped.count)),` in ${scoped.view}`]:'',places?` · ${places}`:'',
      // Who was checked is the server's explanation, not the verdict: contextual help beside it (FIXLIST-UX).
      s.population_note?el('button',{type:'button',class:'icon-button bare page-info fix-population','aria-label':'Who was checked','data-help':'','data-tip':s.population_note},icon('help')):''),
    since,s.checking?el('span',{class:'muted fix-checking'},el('span',{class:'spinner','aria-hidden':'true'}),s.checking_words||summaryCheckWords(s)||'Checking…',
      // The time ticks in place (app.ts tickCheck) and is never said: nothing is announced while the check runs.
      ...(s.checking_since?[' · ',el('span',{class:'elapsed-time','data-check-clock':String(s.checking_since),'aria-hidden':'true'},clock(Date.now()-s.checking_since))]:[])):s.checked_at?el('span',{class:'muted'},checkedAgo(s.checked_at)):'');
}
export const byLevel=(l:FixList,level:FixLevel)=>l.causes.filter(c=>c.level===level);
/** The Planner's Fix list panel (FIXLIST-UX direction A): the gate, every must-fix cause open (the one in focus with its
 *  fix in place), then Could improve collapsed. The same rows as the page, Home and the run card (fixRow). */
export function fixPanelNodes(list:FixList,h:FixHooks,step:FixStep='submit'):HTMLElement[]{
  const must=byLevel(list,'must'),could=byLevel(list,'could');h.all=list.causes;h.key=legend('Key');
  const levels=[...(must.length?[fixLevel('must',must,h,{primary:true,panel:false})]:[]),
    ...(could.length?[fixLevel('could',could,h,{primary:!must.length,panel:false,collapsed:!!must.length})]:[])];
  // The key follows the rows shown (a collapsed level, a row hidden in place): dom.ts legend `follow`.
  return [fixGate(list.summary,undefined,undefined,step),...levels,h.key.follow(levels)];
}

/** THE rota health in the footer (A2/A7; UIUX-AUDIT C3: one health for a draft and the published rota): "⚠ 3 must fix"
 *  and "○ 5 could improve" (the D78 shapes and tones: Could improve is the gold circle everywhere); with nothing to fix,
 *  "✓ Ready · score 71.4" (or `clear`: "All clear" on the published rota). Each opens the Fix list's head (fixHead), read
 *  when it opens; where there is no Fix list (the published rota) `lists` draws what each pill opens instead. A phone
 *  shows the numbers only (app.css). */
export function fixPills(s:FixSummary,o:{load?:()=>Promise<FixList>;held?:()=>FixList|null;open?:()=>void;breakdown?:()=>void;hooks?:FixHooks;step:FixStep;
  lists?:{must?:(close:()=>void)=>HTMLElement;could?:(close:()=>void)=>HTMLElement};clear?:string;
  /** Read before a list first opens (the check's cause groups): never on every draw. */before?:()=>Promise<unknown>}):HTMLElement{
  const wrap=el('div',{class:'health-pills fix-pills',role:'group','aria-label':'Rota health'});
  if(s.submission_issue){wrap.append(el('span',{class:'health-pill neutral',role:'status'},s.submission_issue.message),
    o.hooks?.addSession?button('Add session',o.hooks.addSession,'secondary small'):'');return wrap;}
  const score=s.must?null:s.score??null;
  const pill=(tone:string,glyph:Parameters<typeof icon>[0]|null,num:string,label:string,full:string)=>{
    const b=el('button',{type:'button',class:'health-pill '+tone,'aria-haspopup':'dialog','aria-expanded':'false','aria-label':full,'data-ctip':full},
      s.checking&&tone!=='neutral'?el('span',{class:'spinner','aria-hidden':'true'}):glyph?icon(glyph):'',el('span',{class:'num'},num),el('span',{class:'label'},label));
    if(s.checking)b.setAttribute('aria-busy','true');
    // maintenance: the footer's one delegated handler (dom.ts footerAct), and a popover keyed to the pill, so a redraw of the
    // footer neither drops the press nor closes what it opened: the new pill takes the open popover over.
    const key='fix-pill:'+tone;
    footerAct(b,key,async()=>{if(o.before&&b.getAttribute('aria-expanded')!=='true')await o.before().catch(()=>{});popover(b,(panel,close)=>{panel.classList.add('fix-pop');
      if(o.lists||!o.load){const list=(tone==='err'?o.lists?.must:tone.includes('could')?o.lists?.could:undefined)?.(close);panel.classList.add('issue-pop');
        panel.append(el('div',{class:'popover-head'},el('strong',{},full)),list||emptyLine('Nothing must be fixed.',null,'popover-section'));return;}
      const known=o.held?.();if(known){panel.append(...fixHead(known,{...o,open:o.open||(()=>{}),close,score}));return;}
      // The shared wait (never a bare "Loading…"): nothing for 300 ms, then lines that give way without a flash.
      const sk=skeleton('lines','Loading rota health…');sk.classList.add('popover-section');panel.append(sk);
      // A list that cannot be drawn says why, like one that cannot be read (never a skeleton left spinning).
      void o.load().then(list=>{if(panel.isConnected)void swap(panel,...fixHead(list,{...o,open:o.open||(()=>{}),close,score}));}).catch(e=>{if(panel.isConnected)void swap(panel,el('p',{class:'popover-section muted',role:'alert'},(e as Error).message));});
    },{label:'Rota health',placement:'below',key});});
    queueMicrotask(()=>adoptPopover(key,b));
    return b;};
  if(s.must)wrap.append(pill('err','alert',String(s.must),' must fix'+(s.added?` · ${s.added} new`:''),mustWords(s.must)+(s.added?` · ${s.added} new`:'')+(o.lists?'':' · open the fix list')));
  else{const word=o.clear||'Ready',ready=o.clear||(o.step?`Ready to ${o.step}`:'Ready');wrap.append(pill('ok','check','',score!=null?`${word} · score ${score}`:word,score!=null?`${ready} · score ${score}`:ready));}
  // Could improve: the issue marker's gold circle (D78), on a draft and the published rota alike.
  if(s.could)wrap.append(pill('warn could','notice',String(s.could),' could improve',couldWords(s.could)));
  return wrap;
}
/** A Fix list's causes as the one issue list's items (UIUX live check F3/F6): one item per cause (its id is its
 *  identity), its sentence and level mark, its sessions as chips by day (with the time when a day repeats). */
export function causeIssues(causes:readonly FixCause[],h:FixHooks={}):Issue[]{
  const zone=departmentTimeZone();
  return causes.flatMap(c=>{const base:Issue={severity:c.level==='must'?'error':'warning',words:c.sentence,code:'cause:'+c.id,...(c.finding_keys?.[0]?{key:c.finding_keys[0]}:{})};
    const at=(id:string)=>h.start?.(id)||'',ids=[...(c.shift_ids||[])].sort((a,b)=>at(a).localeCompare(at(b)));
    if(!ids.length)return [base];
    const labels=dayLabels(ids.map(id=>{const t=at(id);return t?{day:formatDay(t,zone).split(' ').slice(0,2).join(' '),time:formatTime(t,zone)}:{day:h.label?.('shifts',id)||'',time:''};}));
    return ids.map((shift,n)=>({...base,shift,where:labels[n]}));});
}
/** The footer popover: the one issue list of what must be fixed (else the gate, the score and what could improve), each
 *  cause's sessions as chips, ending "Open all in Fix list (N)" (F6: no third list variant). */
function fixHead(list:FixList,o:{open:()=>void;close:()=>void;score?:number|null;breakdown?:()=>void;hooks?:FixHooks;step:FixStep}):HTMLElement[]{
  const s=list.summary,must=byLevel(list,'must'),could=byLevel(list,'could');
  const go=()=>{o.close();o.open();};
  const body=el('div',{class:'popover-section fix-pop-body'});
  // The score shows beside this popover's pill whenever the rota has one (its own pill, capped and saying so): no
  // "score shows once…" line here reviewed.
  if(must.length){if(s.population_note)body.append(el('p',{},s.population_note));}
  else{body.append(fixGate({...s,checked_at:null},undefined,undefined,o.step));
    if(o.score!=null&&!s.submission_issue)body.append(el('p',{class:'score-line'},el('b',{},String(o.score)),el('span',{class:'muted'},'score')));}
  const shown=must.length?must:could;
  if(shown.length)body.append(issueList(causeIssues(shown,o.hooks),{label:must.length?mustWords(must.length):couldWords(could.length),
    ...(must.length?{}:{limit:TAIL}),done:o.close,openAll:{count:list.causes.length,go:o.open}}));
  // SX-11: beside a footer that says "Needs a check", the popover never claims the check is current.
  const stale=(s as {stale?:boolean}).stale;
  const side=must.length&&could.length?button(couldWords(could.length),go,'link-button'):!must.length&&o.breakdown?button('Score breakdown',()=>{o.close();o.breakdown!();},'link-button'):'';
  const head=el('div',{class:'popover-head'},el('div',{},el('strong',{},'Rota health'),s.checked_at?el('small',{},checkedAgo(s.checked_at)+(stale?' · the rota changed since':'')):''));
  // Nothing listed (all clear): the Fix list is still one press away.
  return side||!shown.length?[head,body,el('div',{class:'popover-foot fix-pop-foot'},side,el('span',{class:'grow'}),shown.length?'':button('Open Fix list',go,'secondary small'))]:[head,body];
}

/** A Fix list view: who it shows (people facts: rank, programme, tag) and how. Saved on the server as the collection's
 *  saved views (table key "fix-list"), so a scheduling group is a saved view (R2); the next phase's group model plugs in
 *  by giving `matches` its own test. */
export type FixView={filters:Record<string,string[]>;conditions?:Conditions|null;group:''|'rule'|'scheduling';sort:null|['new','desc']};
export interface FixPageHooks extends FixHooks{
  checks:()=>void;
  /** The people facts a filter reads, on the rota's first day (category_ids, programme_ids, tags). */
  personFacts?:(id:string)=>Record<string,string[]>;
  /** Filter fields and their choices (Rank, Programme, Tag), from the department's catalogues. */
  fields?:{key:string;label:string;choices:{value:string;label:string}[]}[];
  shareable?:boolean;
  /** P2-BE: the rota's scheduling groups (maintenance): Group › Scheduling group, the default when the causes say whose they are. */
  planning?:PlanningContext|null;
  /** SW3-B-11: the reader's next step on this rota (the gate's "ready to …"). */
  step:FixStep;
  /** The rota's whole Fix list as the page holds it (an edit's fix_list_delta applied, no read per edit); a scheduling
   *  group's view still reads its own. Unset: read it. */
  read?:()=>Promise<FixList>;
  checkSummary?:(summary:FixSummary)=>FixSummary;
}
/** Whether a cause meets a view's Filter. Its plain rows (`filters`): the rule lines' population (who) names one of a
 *  property's values, or someone in the cause has one. The rest of its tree (`conditions`) is read with the shared
 *  builder's own truth (conditions.ts treeMatches / lineMatches) person by person: the cause is in the view when one
 *  person in it meets the whole tree (never two people's values pooled into one "has all of"); the population naming a
 *  value meets a plain "is any of" line only, never a negative or an "all" line. A cause without people is read on no facts.
 *  shortcut: filtered on the client from the causes the server sent; ask fix-list?filter= once 150-person payloads are measured too big. */
export function causeMatches(c:FixCause,f:FilterState,facts?:(id:string)=>Record<string,string[]>){
  const people=c.person_ids||c.action?.scope?.person_ids||[],tree=f.conditions?.conditions.length?f.conditions:null;
  const names=(k:string,values:string[])=>c.who.some(g=>mentions(g,k,values));
  const meets=(of:(k:string)=>string[])=>treeMatches(tree!,line=>(line.op||'any')==='any'&&line.value==null&&names(line.field,line.values as string[])
    ||lineMatches(line,of(line.field),'multiple_choice'));
  return Object.entries(f.filters).every(([k,values])=>!values.length||names(k,values)||people.some(p=>facts?.(p)[k]?.some(v=>values.includes(v))))
    &&(!tree||(people.length?people.some(p=>meets(k=>facts?.(p)[k]||[])):meets(()=>[])));
}
const stateOf=(v:Pick<FixView,'filters'|'conditions'>):FilterState=>({filters:v.filters||{},conditions:v.conditions||null});
const narrows=(v:Pick<FixView,'filters'|'conditions'>)=>!!joinFilter(v.filters||{},v.conditions);
const rankOf=(c:FixCause,sort:FixView['sort'])=>sort?.[0]==='new'?(c.new?0:1):0;

/** Schedule › Fix list (A1 + R1/R2, #schedule/issues?draft=<id>): a reading column. The collection toolbar (saved view,
 *  Search, Filter by people, Group: Level / Rule, Sort: Checks cleared / Newest, then All checks as a built-in view),
 *  the rota-wide gate, the levels, and with a view on, "Outside this view". Read-only without `act`. Phones too (D10). */
export async function fixListPage(target:HTMLElement,workspaceId:string,h:FixPageHooks){
  const [list,saved]=await Promise.all([h.read&&!h.planning?.group_version_id?h.read():readFixList(workspaceId,h.planning?.group_version_id),fetchViews<FixView>('fix-list').catch(()=>[] as SavedView<FixView>[])]);
  // maintenance: with scheduling groups, the list opens grouped by them (Mine to fix · Linked · Other groups · No group).
  const planning=planningOf(list)||h.planning||null,grouped=!!planning&&attributed(list.causes),plain:FixView={filters:{},conditions:null,group:grouped?'scheduling':'',sort:null};
  let views=saved,activeId='',view:FixView={...plain},q='';
  const body=el('div',{class:'fix-body'}),key=h.key=legend('Key');
  const active=()=>views.find(v=>v.id===activeId);
  const inView=(c:FixCause,f:FilterState=stateOf(view))=>causeMatches(c,f,h.personFacts);
  const named=(k:string,v:string)=>h.fields?.find(f=>f.key===k)?.choices.find(x=>x.value===v)?.label||v;
  // A view's Filter reads each cause's who and people, which the lean list leaves out: hydrate the whole list once first
  // (the same one read Search uses), never filter the lean rows to a false 0.
  let hydrating:Promise<void>|null=null;
  const draw=():void=>{
    const full=narrows(view)?deferredLists.get(list):undefined;
    if(full){if(!hydrating){body.setAttribute('aria-busy','true');body.replaceChildren(skeleton('lines','Loading the Fix list…'));
      hydrating=full().then(()=>{hydrating=null;body.removeAttribute('aria-busy');draw();},error=>{hydrating=null;body.removeAttribute('aria-busy');
        body.replaceChildren(readFailed('the Fix list',error,draw));});}return;}
    const shown=list.causes.filter(c=>inView(c)).sort((a,b)=>rankOf(a,view.sort)-rankOf(b,view.sort));h.all=shown;key.clear();
    const must=shown.filter(c=>c.level==='must'),could=shown.filter(c=>c.level==='could'),filtered=narrows(view);
    const summary=h.checkSummary?.(list.summary)??list.summary;
    const parts:HTMLElement[]=[fixGate(summary,undefined,filtered?{count:must.length,view:active()?.name||'this view'}:undefined,h.step)];
    if(list.summary.submission_issue&&h.addSession)parts.push(button('Add session',h.addSession,'primary small'));
    if(view.group==='rule'){
      const by=new Map<string,FixCause[]>();for(const c of shown){const k=h.ruleName?.(c.rule_lines[0]?.rule_id||'')||'Other';by.set(k,[...(by.get(k)||[]),c]);}
      [...by].forEach(([name,causes],i)=>parts.push(fixLevel(causes.some(c=>c.level==='must')?'must':'could',causes,h,{heading:name,all:true,primary:i===0})));
    }else if(view.group==='scheduling'&&planning)parts.push(...groupSections(shown,planning,h,{mine:planning.group_id}));
    else{if(must.length)parts.push(fixLevel('must',must,h,{primary:true}));if(could.length)parts.push(fixLevel('could',could,h,{primary:!must.length}));}
    // R2: the gate stays rota-wide; what this view leaves out is one panel of the other views' counts.
    if(filtered){const others=views.filter(v=>v.id!==activeId&&narrows(v.definition));
      const rest=list.causes.filter(c=>c.level==='must'&&!inView(c)),elsewhere=rest.filter(c=>!others.some(v=>inView(c,stateOf(v.definition))));
      const row=(name:string,n:number,open?:()=>void)=>el('li',{class:'fix-outside'},el('strong',{},name),el('span',{class:'grow'}),badge(String(n),n?'err':'neutral','fix-count'),el('span',{class:'muted'},'must fix'),open?button('Open view',open,'link-button'):'');
      const rows=[...others.map(v=>row(v.name,rest.filter(c=>inView(c,stateOf(v.definition))).length,()=>{take(v);})),...(elsewhere.length?[row('Everyone else',elsewhere.length)]:[])];
      if(rows.length)parts.push(el('section',{class:'panel fix-panel','aria-label':'Outside this view'},el('div',{class:'fix-level'},el('h2',{},'Outside this view'),badge(String(rest.length),'neutral')),el('ul',{class:'fix-list'},...rows)));}
    if(summary.checking)body.setAttribute('aria-busy','true');else body.removeAttribute('aria-busy');
    body.replaceChildren(...parts);search();bar.sync();};
  // Search reads what each row says (its sentence, line and evidence, names included): nothing is counted again.
  let searching:Promise<void>|null=null;
  const search=()=>{const words=q.trim().toLowerCase(),read=deferredLists.get(list);
    if(words&&read){if(!searching){body.setAttribute('aria-busy','true');searching=read().then(()=>{
      // Hydrate in place: redrawing would collapse Show all and replace the reader's open rows.
      const causes=new Map(list.causes.map(c=>[c.id,c]));
      for(const li of body.querySelectorAll<HTMLElement>('.fix-list>li[data-cause]')){const cause=causes.get(li.dataset.cause||''),detail=li.querySelector('.fix-detail');
        if(cause&&detail)detail.replaceChildren(...(evidence(cause,h)?.childNodes||[]));}search();
    }).catch(error=>{
      body.prepend(readFailed('the search details',error,search));
    }).finally(()=>{searching=null;body.removeAttribute('aria-busy');});}return;}
    for(const li of body.querySelectorAll<HTMLElement>('.fix-list>li[data-cause]'))li.hidden=!!words&&!(li.textContent||'').toLowerCase().includes(words);bar.shown(words?body.querySelectorAll('.fix-list>li[data-cause]:not([hidden])').length:h.all?.length||0,list.causes.length,['issue','issues']);};
  const take=(v:SavedView<FixView>|null)=>{activeId=v?.id||'';view=v?{filters:{...v.definition.filters},conditions:v.definition.conditions||null,group:v.definition.group||'',sort:v.definition.sort||null}:{...plain};draw();};
  const same=(d:FixView)=>JSON.stringify([d.filters||{},d.conditions||null,d.group||'',d.sort||null])===JSON.stringify([view.filters,view.conditions||null,view.group,view.sort]);
  const bar=collectionToolbar({label:'Fix list',key:'fix-list',
    saved:{name:()=>active()?.name||'',defaultName:()=>'Fix list',dirty:()=>!!active()&&!same(active()!.definition),
      items:close=>savedViewItems<FixView>({key:'fix-list',noun:'causes',saved:views,activeId,current:()=>({...view}),dirty:d=>!same(d),
        apply:take,done:(next,id)=>{views=next;activeId=id;draw();},close,shareable:h.shareable,
        builtins:[{label:'Fix list',on:!activeId,apply:()=>take(null)},{label:'All checks',on:false,apply:h.checks}]})},
    search:{value:'',label:'Search the fix list',placeholder:'Person, clinic or rule',onInput:v=>{q=v;},apply:search},
    // The shared Filter (conditionFilter) over the people facts a cause reaches (Rank, Programme, Tag).
    filter:h.fields?.some(f=>f.choices.length)?conditionFilter({label:k=>h.fields?.find(f=>f.key===k)?.label||k,valueLabel:named,
      fields:()=>(h.fields||[]).filter(f=>f.choices.length).map(f=>({...f,kind:'multiple_choice' as const})),
      get:()=>stateOf(view),set:n=>{view={...view,filters:n.filters,conditions:n.conditions};draw();}}):undefined,
    group:{options:[{key:'',label:'Level'},{key:'rule',label:'Rule'},...(grouped?[{key:'scheduling',label:'Scheduling group'}]:[])],value:()=>view.group,set:k=>{view={...view,group:k as FixView['group']};draw();},active:()=>view.group!==plain.group,defaultKey:plain.group},
    sort:{options:[{key:'new',label:'Newest'}],value:()=>view.sort,set:v=>{view={...view,sort:v?['new','desc']:null};draw();},directions:false,defaultLabel:'Checks cleared',active:()=>!!view.sort}});
  // The key follows the rows shown: a redraw, a view, a search that hides rows in place (dom.ts legend `follow`).
  target.append(el('div',{class:'fix-page'},bar.root,body,key.follow([body])));
  h.redraw=draw;
  draw();
  return list;
}

/** What is reviewed once with the rota (D3 + D16; maintenance "D16: unified review"): every rota adjustment (a non-safety rule
 *  relaxed or set FOR THIS ROTA ONLY, applied in the draft at once) and every acknowledged gap, each with its
 *  justification. `review_items` is on the Fix list, the workspace detail and the lifecycle replies; `source` is the
 *  stored item (person, line, dates, amount, cause). A justification never saved is null and said so, never invented. */
export const reviewItemsOf=(...from:unknown[]):ReviewItem[]=>{for(const x of from){const r=(x as {review_items?:ReviewItem[]}|null|undefined)?.review_items;if(Array.isArray(r))return r;}return [];};
/** The review list: A5 reads it; with `decide` (R5) the approver accepts or rejects each item (a reject needs its
 *  reason), "Accept all" per section, and `changed` runs on every choice. Two sections: Changes for this rota (D16
 *  adjustments) and Acknowledged gaps (D3), each item with its justification quoted. */
export function reviewedWithRota(items:ReviewItem[],causes:FixCause[],h:FixHooks&{personName?:(id:string)=>string},o:{decide?:boolean;changed?:()=>void;
  /** maintenance/maintenance: the rota's scheduling groups; items carrying `group_id` are listed in one section per group. */groups?:GroupState[]}={}){
  const byCause=new Map(causes.map(c=>[c.id,c])),chosen=new Map<string,{accept:boolean;reason:HTMLTextAreaElement}>(),setters:(()=>void)[][]=[];
  // maintenance/maintenance (P2-BE): the justification with its author and date when the item names them.
  const quote=(why?:string|null,i?:ReviewItem)=>{const by=(i as {author_name?:string}|undefined)?.author_name,at=(i as {at?:string}|undefined)?.at;
    // The attribution sits beside the quote, not inside it (the blockquote holds only what was written).
    const q=why?el('blockquote',{class:'fix-quote'},`“${why}”`):el('p',{class:'fix-quote muted'},'No justification recorded');
    return by&&why?el('figure',{class:'fix-quoted'},q,el('figcaption',{},[by,at?formatDate(at,departmentTimeZone()):''].filter(Boolean).join(' · '))):q;};
  // Adjustment dates are stored with an exclusive end (maintenance); said as the last day included.
  const zone=departmentTimeZone(),dates=(x:Row)=>{if(!x.starts_on)return '';const last=x.ends_on?addDays(String(x.ends_on).slice(0,10),-1):x.starts_on;
    return last>x.starts_on?`${formatDate(x.starts_on,zone)} – ${formatDate(last,zone)}`:formatDate(x.starts_on,zone);};
  const NUMBER_WORDS:Record<string,string>={max:'At most',min:'At least',target:'Target'};
  const decide=(i:ReviewItem,what:string,li:HTMLElement,section:number)=>{if(!o.decide)return '';
    const id=`reject-${++rowSerial}`,reason=el('textarea',{id,rows:2,maxlength:500,placeholder:'Required'}) as HTMLTextAreaElement;
    const why=el('div',{class:'gap-reason'},el('label',{for:id},'Why reject'),reason);why.hidden=true;li.append(why);
    const seg=el('div',{class:'segmented',role:'group','aria-label':'Decision: '+what});
    const pick=(accept:boolean)=>{chosen.set(i.id,{accept,reason});why.hidden=accept;reason.required=!accept;
      for(const b of seg.querySelectorAll('button'))b.setAttribute('aria-pressed',String(b.classList.contains(accept?'accept':'reject')));o.changed?.();if(!accept)reason.focus();};
    const b=(accept:boolean)=>{const x=el('button',{type:'button',class:accept?'accept':'reject','aria-pressed':'false'},icon(accept?'check':'close'),accept?'Accept':'Reject');x.addEventListener('click',()=>pick(accept));return x;};
    seg.append(b(true),b(false));(setters[section]||=[]).push(()=>pick(true));reason.addEventListener('input',()=>o.changed?.());return seg;};
  const row=(i:ReviewItem,section:number)=>{const x=i.source||{},cause=i.kind==='acknowledged_gap'?byCause.get(String(x.cause_id||'')):undefined;
    if(cause){const li=fixRow(cause,{...h,act:undefined},{extra:quote(i.reason,i)});li.dataset.reviewItem=i.id;
      li.querySelector('.fix-row')!.append(decide(i,cause.sentence,li,section));return li;}
    // maintenance: each item names its rule (`name`); an id the catalogue knows is said by its name too.
    const rule=String((i as {name?:string}).name&&!/^[0-9a-f-]{36}$/.test(String((i as {name?:string}).name))?(i as {name?:string}).name:'')||h.ruleName?.(String(x.policy_id||''))||'A department rule',who=x.person_id?h.personName?.(String(x.person_id))||'':'';
    const what=i.kind==='acknowledged_gap'?String(x.sentence||'Acknowledged gap'):[who,rule].filter(Boolean).join(': ');
    const how=i.kind==='acknowledged_gap'?'':x.mode==='off'?'Switched off for this rota':x.amount!=null?`${i.kind==='target_override'?'Target':NUMBER_WORDS[String(x.number)]||'Set to'} ${x.amount} for this rota`:'Changed for this rota';
    const li=el('li',{'data-review-item':i.id});
    li.append(needRow(el('span',{class:'fix-mark adj','aria-hidden':'true'},icon(i.kind==='acknowledged_gap'?'alert':'edit')),
      needText(what,[how,x.was!=null?`was ${x.was}`:'',dates(x),x.clears?plural(Number(x.clears),'check')+' cleared':''].filter(Boolean).join(' · ')),decide(i,what,li,section)),quote(i.reason,i));
    // The reason field (when rejecting) stays last.
    const why=li.querySelector('.gap-reason');if(why)li.append(why);
    return li;};
  const adjustments=items.filter(i=>i.kind!=='acknowledged_gap'),gaps=items.filter(i=>i.kind==='acknowledged_gap');
  const section=(title:string,list:ReviewItem[],k:number,head?:HTMLElement)=>{if(!list.length&&!head)return null;const ul=el('ul',{class:'decide fix-list'},...list.map(i=>row(i,k)));
    // Under a group's row the head is only Accept all: with nothing to decide it is left out (the row names the group).
    return el('section',{class:'span-2','aria-label':title},head||'',head&&!(o.decide&&list.length)?'':el('div',{class:'decide-head'},head?'':el('h3',{},title),list.length?badge(String(list.length),'neutral'):'',el('span',{class:'grow'}),
      o.decide&&list.length?button('Accept all',()=>{for(const set of setters[k]||[])set();},'link-button'):''),list.length?ul:'');};
  // maintenance: one section per scheduling group (its row, then its adjustments and gaps); items of no group come last.
  const groupOf=(i:ReviewItem)=>String((i as {group_ids?:string[]}).group_ids?.[0]||(i as {group_id?:string|null}).group_id||'');
  const byGroup=!!o.groups?.length;
  const nodes=(byGroup?[...o.groups!.map((g,k)=>section(g.name,items.filter(i=>groupOf(i)===g.group_id),k,groupRow(g,{extra:groupFill(g)}))),
      section('No group',items.filter(i=>!o.groups!.some(g=>g.group_id===groupOf(i))),o.groups!.length)]
    :[section('Changes for this rota',adjustments,0),section('Acknowledged gaps',gaps,1)]).filter((n):n is HTMLElement=>!!n);
  const kind=(i:ReviewItem):ReviewDecision['kind']=>i.kind==='acknowledged_gap'?'gap':'adjustment';
  return {nodes,total:items.length,decided:()=>chosen.size,rejected:()=>[...chosen.values()].filter(c=>!c.accept).length,
    /** P2-BE: the items rejected (any rejection returns the whole rota to draft, D24). */
    rejectedIds:()=>[...chosen].filter(([,c])=>!c.accept).map(([id])=>id),
    /** Every item decided, each reject with its reason; else null. */
    decisions:():ReviewDecision[]|null=>items.length===chosen.size&&[...chosen.values()].every(c=>c.accept||c.reason.value.trim().length>=3)
      ?items.map(i=>{const c=chosen.get(i.id)!;return {item_id:i.id,kind:kind(i),accept:c.accept,...(c.accept?{}:{reason:c.reason.value.trim()})};}):null};
}

/** An edit's `fix_list_delta` (FixListDelta: cleared / new / changed cause ids, the new and changed causes without
 *  evidence, the summary after) applied to a Fix list: the server's truth at once, without reading the list again.
 *  Changed causes keep their place; new ones join the end of their level (the server ranks them on its next read). */
export type FixDelta={cleared:string[];new:string[];changed:string[];causes:FixCause[];summary?:FixSummary|null};
export function applyFixDelta(l:FixList,d:FixDelta):FixList{
  // No baseline (LEAD-SCHEDULING, 7 Nov 2026: rota_summary's before_fix is None): cleared/new/changed are empty on
  // purpose and `causes` is every cause after the edit, so it is the list itself: its rows then agree with its
  // summary, causes it cleared go (their sessions become ✓) and new ones show. An edit that changed nothing has no causes.
  if(!d.cleared.length&&!d.new.length&&!d.changed.length&&d.causes.length){
    // The server lists them by id: the ones already shown keep their place, new ones follow (the next read orders all).
    const at=new Map(l.causes.map((c,i)=>[c.id,i])),place=(c:FixCause)=>at.get(c.id)??Infinity;
    return {...l,causes:[...d.causes].sort((a,b)=>place(a)-place(b)),summary:d.summary||l.summary};}
  const gone=new Set(d.cleared),upsert=new Map(d.causes.map(c=>[c.id,c]));
  const causes=l.causes.filter(c=>!gone.has(c.id)).map(c=>upsert.get(c.id)||c);
  for(const id of d.new){const c=upsert.get(id);if(!c||causes.some(x=>x.id===id))continue;
    const last=causes.map(x=>x.level).lastIndexOf(c.level);causes.splice(last<0?(c.level==='must'?0:causes.length):last+1,0,c);}
  return {...l,causes,summary:d.summary||l.summary};
}
/** The delta an edit's reply carries, or null. `complete`: it holds the changed causes themselves (FixListDelta), so it
 *  can stand for a read; a reply with only ids (cleared / new) can still be said, not applied. */
export const fixDeltaOf=(reply:unknown):(FixDelta&{complete:boolean})|null=>{const d=(reply as {fix_list_delta?:Partial<FixDelta>|null}|null|undefined)?.fix_list_delta;
  return d?{cleared:d.cleared||[],new:d.new||[],changed:d.changed||[],causes:d.causes||[],summary:d.summary||null,complete:Array.isArray(d.causes)&&Array.isArray(d.changed)}:null;};
/** R3: what a draft edit cleared and broke, in the edit's own message ("1 cleared · 1 new: he is now over Duties per
 *  period"), from the reply's fix_list_delta (its causes name the new ones). Nothing said without it. */
export function deltaWords(reply:unknown):string{
  const d=fixDeltaOf(reply);if(!d||!(d.cleared.length||d.new.length))return '';
  const first=d.causes.find(c=>c.id===d.new[0]);
  return [d.cleared.length?`${d.cleared.length} cleared`:'',d.new.length?`${d.new.length} new${first?`: ${first.sentence.replace(/[.\s]+$/,'')}`:''}`:''].filter(Boolean).join(' · ')+'.';  // ME-20: one period
}
