import {el,plural,badge,spanLabel,formatTime,departmentTimeZone} from './dom.js';

/** Current operational warnings include leave, recorded absence and unavailable rotations; not all are approvals. */
export const publishedAbsenceLabel=(count:number,own=false)=>own
  ?count===1?'Your duty overlaps unavailability':'Your duties overlap unavailability'
  :count===1?'Published duty overlaps unavailability':'Published duties overlap unavailability';
import type {Row} from './types.js';
import type {Issue} from './issue_marker.js';
import {clashText,type Clash} from './double_booking.js';
import {FINDING_MESSAGES,TEAM_PROBLEM_WORDS,type FindingCause,type FindingCauseGroup,type FindingFixHint,type RotaRepairTask} from './generated.js';
/** Plain wording for rule-check findings, by code: what is wrong and, where useful, what to do.
 *  Applied once to every server response (api.ts), so every screen says the same thing.
 *  The engine's own text stays on the finding as `technical`. */
const PLAIN:Readonly<Record<string,string>>=FINDING_MESSAGES;
/** A finding code in the contract's plain words ('' when the contract has none). */
export const plainCode=(code:unknown)=>PLAIN[String(code)]||'';
/** A finding code in a person's words (the planner's session panel, and a refused edit): what is wrong. */
export const FINDING_WORDS:Readonly<Record<string,string>>={SUPERVISOR_REQUIRED:'Needs a named supervisor on this duty before their supervision limit can be checked.',SUPERVISION_PAIRING:'This session has no named, qualified supervisor.',ATTACHMENT_PAIRING:'A trainee in this session isn’t paired with the consultant they’re attached to.',TIME_OVERLAP:'Already working another duty at this time.',UNAVAILABLE:'Recorded leave or unavailability at this time.'};
/** What to do, by the same code, when it refuses an edit (D65, DL-048). */
export const FINDING_NEXT:Readonly<Record<string,string>>={SUPERVISION_PAIRING:'Assign or restore one first.',ATTACHMENT_PAIRING:'Pair them first.',LEAD_GAP:'Set who leads that session first, or make both changes together.',SUPERVISION_GAP:'Add a supervisor first, or add both together.'};
/** Why an auto-schedule run stopped or did not start, by its error code (DL-049): the one place these words live. */
export const RUN_ERROR_WORDS={
  SOLVER_RULE_INCOMPATIBLE:{head:(n:number)=>n?`${plural(n,'rule line')} can’t be scheduled automatically`:'A rule can’t be scheduled automatically',
    body:(n:number)=>n?'Change these lines, then run auto-schedule again:':'Change the lines marked on Rules, then run auto-schedule again.',action:'Open Rules'},
  ACCESS_REVOKED:{head:'This run could not finish',body:'Check your access and try again.'},
  unknown:{head:'This run could not finish',body:'This run stopped. Run auto-schedule again. If it stops again, send feedback.',action:'Send feedback'},
} as const;
/** One rule line a run can't schedule: its rule, its line number (when the issue names one) and the server's reason. */
export type RunLine={rule_id:string;line:number|null;reason:string};
/** A run error's words and the lines it names: SOLVER_RULE_INCOMPATIBLE's issues (rule_id, field
 *  "definition.rule.lines.<n>…", message) become one line each; every other code names none. `total`: the server's full
 *  issue count (job.error_detail.total); beyond the list it adds Open Rules for the rest. */
export function runError(code:string,issues?:unknown,total?:number):{head:string;body:string;action:'rules'|'feedback'|null;lines:RunLine[]}{
  if(code==='SOLVER_RULE_INCOMPATIBLE'){
    const seen=new Map<string,RunLine>();
    for(const i of Array.isArray(issues)?issues as Record<string,unknown>[]:[]){if(!i?.rule_id)continue;
      const m=/^definition\.rule\.lines\.(\d+)/.exec(String(i.field||'')),line=m?Number(m[1]):null,key=`${i.rule_id}|${line}`;
      if(!seen.has(key))seen.set(key,{rule_id:String(i.rule_id),line,reason:String(i.message||'')});}
    const lines=[...seen.values()],w=RUN_ERROR_WORDS.SOLVER_RULE_INCOMPATIBLE;
    // A capped list (total beyond what came) still names what came, and Open Rules shows the rest: total counts issues, not
    // lines, so it never inflates the head.
    const capped=Array.isArray(issues)&&(total??0)>issues.length;
    return {head:w.head(lines.length),body:w.body(lines.length),action:!lines.length||capped?'rules':null,lines};
  }
  if(code==='ACCESS_REVOKED')return {...RUN_ERROR_WORDS.ACCESS_REVOKED,action:null,lines:[]};
  return {head:RUN_ERROR_WORDS.unknown.head,body:RUN_ERROR_WORDS.unknown.body,action:'feedback',lines:[]};
}
// shortcut: the server's generic refusal sentences, matched at the end of its message (planning.py, swaps.py); a reworded
// one is kept as a prefix, only longer. The patterns are only ever removed, never shown; each one's words say what was not done.
const GENERIC:[RegExp,string][]=[[/This edit introduces a conflict that a manual reason cannot override$/,'Not saved'],
  [/The move introduces a mandatory problem; no assignment was moved$/,'Not moved'],[/The complete swap failed validation; neither side was saved$/,'Not exchanged'],
  [/The complete duty transfer failed validation; no change was saved$/,'Not transferred'],[/The edit introduces a blocking issue$/,'Not saved']];
/** DL-048: a refusal says what is wrong and what to do. When its findings (details.findings) carry a code with both, each
 *  distinct one says them in place of the generic server sentence; a specific server prefix (the blocker it names) stays.
 *  With none, a generic sentence still names what is wrong: what was not done, then the first mandatory finding's words
 *  (the checks under it say who and when). Otherwise the server's message is unchanged. */
export function refusalText(message:string,details:unknown):string{
  const found=(details as {findings?:{code?:unknown;message?:unknown;severity?:unknown}[]}|null|undefined)?.findings,list=Array.isArray(found)?found:[];
  // What is wrong: the client's words, else the finding's own message (a lead gap is also "more leads than allowed").
  const wrong=(c:string)=>FINDING_WORDS[c]||String(list.find(f=>String(f?.code)===c)?.message||'');
  const said=message.trim().replace(/\.$/,''),generic=GENERIC.map(([g,lead])=>{const m=g.exec(said);return m?{index:m.index,lead}:null;}).find(Boolean);
  const prefix=generic?said.slice(0,generic.index).trim():message.trim(),end=(s:string)=>/[.!?]$/.test(s)?s:s+'.';
  const codes=[...new Set(list.map(f=>String(f?.code)))].filter(c=>FINDING_NEXT[c]&&wrong(c));
  if(codes.length)return [prefix,...codes.flatMap(c=>[wrong(c),FINDING_NEXT[c]])].filter(Boolean).map(end).join(' ');
  const first=generic&&list.filter(f=>f?.severity==='error').map(f=>wrong(String(f?.code))).find(Boolean);
  return first&&generic?[prefix,generic.lead,first].filter(Boolean).map(end).join(' '):message;
}

/** How leave shows (Configuration › Department settings): the server only sends `leave` as far as the setting allows
 *  (apps/api/services/leave_disclosure.py); this only words what arrived. */
export type Leave={type:string;starts_on:string;ends_on:string;note?:string};
export const LEAVE_DISPLAY_CHOICES=[{value:'none',label:'Hidden: only “on leave or away”'},{value:'type',label:'Type and dates'},{value:'details',label:'Type, dates and note'}];
let leaveDisplay='type',leaveViewer='';
export const setLeaveDisplay=(value:unknown,viewer?:string|null)=>{leaveDisplay=typeof value==='string'?value:'type';leaveViewer=viewer||'';};
/** The department's active leave types (GET /api/leave-types, every role), for the Type picker and colours. */
let leaveTypeList:Row[]=[];
export const setLeaveTypes=(list:Row[])=>{leaveTypeList=list;};
export const leaveTypes=()=>leaveTypeList;
/** A leave's type as this reader may see it: their own always; someone else's unless the department hides it. Only a
 *  recorded type (`leave_type_id`); the server's fallback words for other kinds ("Off duty") are not a type. */
export const leaveTypeName=(r:Row)=>r.leave_type_id&&r.leave_type_name&&(r.person_id===leaveViewer||leaveDisplay!=='none')?String(r.leave_type_name):'';
/** The department's setting: 'none' hides why someone is away (Home › Away today says only "Away"). */
export const leaveShown=()=>leaveDisplay;
/** The heading leave issues share (cards are grouped under it). */
export const leaveLabel=()=>leaveDisplay==='none'?'On leave or away (the reason stays private)':'On leave or away';
const day=(iso:string,month=true)=>new Intl.DateTimeFormat('en-GB',{day:'numeric',...(month?{month:'short'}:{}),timeZone:'UTC'}).format(new Date(iso+'T12:00:00Z')).replace('Sept','Sep');
/** "5 Oct", "5–9 Oct", "28 Sep – 3 Oct". */
export const leaveDates=(l:Leave)=>l.starts_on===l.ends_on?day(l.starts_on):l.starts_on.slice(0,7)===l.ends_on.slice(0,7)?`${day(l.starts_on,false)}–${day(l.ends_on)}`:`${day(l.starts_on)} – ${day(l.ends_on)}`;
/** "Annual leave · 5–9 Oct · Conference in department-local". */
export const leaveText=(l:Leave)=>[l.type,leaveDates(l),l.note].filter(Boolean).join(' · ');
/** What kind of issue a finding is, the heading issues are grouped under (auto-schedule results, Resolve issues): its
 *  rule's name, leave, or its plain message. */
export const issueKind=(f:{code?:string;message?:string;[k:string]:unknown},ruleName?:string)=>ruleName||(f.code==='UNAVAILABLE'?leaveLabel():f.code==='TIME_OVERLAP'?'Double-booked':String(f.message));
/** The leave type as a pill, beside the person on an issue or a Why row. */
export const leavePill=(l:Leave|undefined)=>l?badge(l.type,'info'):'';
const unavailable=(f:{leave?:Leave})=>f.leave?leaveText(f.leave):leaveDisplay==='none'?'On leave or away at this time (the reason stays private)':'On leave or away at this time';
type Finding={code?:unknown;message?:unknown;severity?:unknown;technical?:string;leave?:Leave;outside_rota?:boolean;window?:{start:string;end:string};reach?:'before'|'after'};
type Rota={starts_on?:unknown;ends_on?:unknown;validation?:unknown;workspace?:Rota};
type Span={starts_on?:string;ends_on?:string};
/** Rewrites findings in place (anything with a code and message that belongs to a check: it has a severity or a session; API errors are left alone).
 *  A finding whose rule window starts before its rota (server: outside_rota + window) is marked `reach` before, one wholly
 *  after it `after`, from the rota in the same response, so findingNumbers can say "look-back before this rota" on every
 *  screen; a window that starts inside the rota and runs past its end stays unmarked: "partly outside this rota" (RUL-E01). */
// A double booking names the person, both sessions and when (the server's structured clash), not only "already working".
const overlap=(f:Finding)=>f.code==='TIME_OVERLAP'?((f as Row).detail as Row|undefined)?.overlap as Clash|undefined:undefined;
/** Keys whose values never hold a finding, in any reply: a session's people, its type and template snapshots, and a
 *  check's cover rows and Fix-list context, most of a checked rota's bytes. The walk leaves them out
 *  (tests/test_client_findings_walk.py: the same result as walking them). Never `baseline`: a rule check's findings. */
export const NO_FINDINGS=new Set(['participants','type_snapshot','series_snapshot','coverage','fix_list_context']);
export function plainFindings<T>(value:T):T{
  const seen=new Set<object>();
  const walk=(node:unknown,rota?:Span)=>{
    if(!node||typeof node!=='object'||seen.has(node as object))return;
    seen.add(node as object);
    if(Array.isArray(node)){for(const item of node)walk(item,rota);return;}
    const f=node as Finding,r=node as Rota;
    rota=typeof r.workspace?.starts_on==='string'?r.workspace as Span:typeof r.starts_on==='string'&&'validation' in r?r as Span:rota;
    if(typeof f.code==='string'&&typeof f.message==='string'&&('severity' in f||'shift_id' in f)&&(PLAIN[f.code]||f.code==='UNAVAILABLE')&&f.technical===undefined){f.technical=f.message;f.message=f.code==='UNAVAILABLE'?unavailable(f):overlap(f)?clashText(overlap(f)!,true):PLAIN[f.code];}
    // Rota and window ends are exclusive days.
    if(f.outside_rota&&f.window&&rota?.starts_on&&!f.reach){const a=localDay(f.window.start);
      f.reach=a<rota.starts_on?'before':typeof rota.ends_on==='string'&&a>=rota.ends_on?'after':undefined;}
    for(const key of Object.keys(node))if(!NO_FINDINGS.has(key))walk((node as Row)[key],rota);
  };
  walk(value);
  return value;
}

/** Why a person cannot take a place, by reason kind: the server tags reasons with one vocabulary
 *  (packages/domain/solver_summary.py REASON_KIND), used by the fill panel and the auto-schedule summary.
 *  Leave is only ever "on leave" here; its type, dates and note show where the server sent them (leaveText). */
const REASON_WORDS:Record<string,string>={LEAVE:'on leave',AWAY:'away on another rotation',CLASH:'already working then',REST:'would break rest',
  HOURS:'at their limit',PROTECTED:'on protected time',SUPERVISOR:TEAM_PROBLEM_WORDS.needs.toLowerCase(),'NOT ELIGIBLE':'not eligible',RULE:'would break a rule',
  FREE:'free, but needed elsewhere by the schedule'};
export const reasonText=(r:{kind?:string;rule?:string|null})=>(REASON_WORDS[r.kind||'RULE']||REASON_WORDS.RULE)+(r.rule&&r.kind!=='LEAVE'?` (${r.rule})`:'');

/** A rule finding's numbers in words, from the validator's detail: "8 h rest (needs 12 h)", "would maintenance duties (max 5)". */
const COUNTED:Record<string,string>={MAX_HOURS:'h',MAX_DUTIES:'duties',CONSECUTIVE_DAYS:'days in a row',CONSECUTIVE_WEEKENDS:'weekends'};
const num=(n:unknown)=>String(Math.round(Number(n)*10)/10);
// Shortfalls the server folds into the one issue that causes them (detail.derived_findings): said inside that issue, not
// as more issues (one problem, one blocking issue). A cover gap a blocked person causes names its interval and counts.
const during=(x:Row)=>x.start&&x.end?`, ${formatTime(x.start,departmentTimeZone())}–${formatTime(x.end,departmentTimeZone())}`:'';
const DERIVED:Record<string,(d:Row,x:Row)=>string>={COVERAGE_TARGET:d=>`below the preferred ${num(d.target)}`,
  CAPABILITY_GAP:d=>`${num(d.available)} of ${num(d.required)} with a required skill`,
  DISTINCT_PERSON_GAP:d=>`${num(d.available)} of ${num(d.required)} different qualified people`,
  LEAD_GAP:()=>'no team lead yet',
  COVERAGE_GAP:(d,x)=>`leaves ${num(d.available)} of ${num(d.required)} needed${during(x)}`};
/** "Also leaves 0 of 1 needed, 08:00–12:00": the shortfalls folded into this issue (also beside a leave's note). */
export function alsoShort(f:Row):string{
  const derived=((((f.detail||{}) as Row).derived_findings||[]) as Row[]).map(x=>DERIVED[x.code]?.(x.detail||{},x)).filter(Boolean);
  return derived.length?'Also '+derived.join(' · '):'';
}
function counted(f:Row):string{
  const d=(f.detail||{}) as Row,also=alsoShort(f);
  if(also)return also;
  if(d.required_hours!=null&&d.actual_hours!=null&&Number(d.actual_hours)<0)return `overlaps another duty (needs ${num(d.required_hours)} h between)`;
  if(d.required_hours!=null&&d.actual_hours!=null)return `${num(d.actual_hours)} h ${f.code==='TRAVEL_BUFFER'?'between duties':'rest'} (needs ${num(d.required_hours)} h)`;
  if(d.proration&&d.actual!=null&&d.amount!=null)return `${num(d.actual)} of ${num(d.amount)}${prorated(d.proration)}`;
  // A count over a period that was not prorated: the server's own "3 of 4" (quotas.py display), never worked out here.
  if(typeof d.display==='string'&&d.display)return d.display;
  const n=d.actual??d.days??d.weekends;
  return d.maximum!=null&&n!=null?`would be ${num(n)}${COUNTED[f.code]?' '+COUNTED[f.code]:''} (max ${num(d.maximum)})`:'';
}
// shortcut: a window edge is a local midnight; the UTC day of +12 h is that local day for offsets −12…+12 h (no zone needed).
const localDay=(iso:string)=>new Date(Date.parse(iso)+43200000).toISOString().slice(0,10);
/** "From the 4-week look-back before this rota (1–28 Oct 2026)": a rule window that reaches outside the rota (plainFindings sets `reach`). */
export function outsideRota(f:Row):string{
  const w=f.outside_rota?f.window as {start:string;end:string}|undefined:undefined;
  if(!w)return '';
  const from=localDay(w.start),until=localDay(w.end),days=Math.round((Date.parse(until)-Date.parse(from))/86400000);
  const dates=spanLabel(from+'T00:00:00Z',until+'T00:00:00Z','UTC');
  const span=days%7?`${days}-day`:`${days/7}-week`;
  return f.reach?`From the ${span} ${f.reach==='before'?'look-back before':'look-ahead after'} this rota (${dates})`:`Counted over ${dates}, partly outside this rota`;
}
/** A finding's numbers and, when its rule window reaches outside the rota, that window: shown beside every issue's details. */
export const findingNumbers=(f:Row)=>[counted(f),outsideRota(f)].filter(Boolean).join(' · ');

// ---- Why a breach happened (server: packages/domain/finding_causes.py), said once for every screen ----------------
/** Names of configured records (clinics, duty types, places), given once by the app so every screen words causes alike. */
let named:(kind:string,id:string)=>string=(_,id)=>id;
export const setFindingNamer=(fn:(kind:string,id:string)=>string)=>{named=fn;};
type Hint=FindingFixHint;
/** The sessions a breach is about, from its fix hint's session selector: "UCC", "Morning duty", "Clinic A". */
export function causeWhat(hints:Hint[]=[]):string{
  const p=(hints.find(h=>h.action==='add_sessions')||hints[0])?.prefill;if(!p)return '';
  const [kind,ids]=([['services',p.service_ids||[]],['assignment_types',p.assignment_type_ids||[]],['locations',p.location_id?[p.location_id]:[]]] as [string,string[]][]).find(([,l])=>l.length)||['',[]];
  // A long list reads as its first few and a count (the sentence stays one line); every name is in the fix hint's form.
  const names=ids.map(id=>named(kind,id));
  return names.length>3?`${names.slice(0,3).join(', ')} or ${names.length-3} more`:names.join(' or ');
}
/** The days the hints cover, merged: "1 Sep – 31 Oct 2026" (ends are exclusive, as the server sends them). */
export const hintDates=(hints:Hint[]=[])=>{const p=hints.map(h=>h.prefill).filter(x=>x?.starts_on&&x?.ends_on);if(!p.length)return '';
  return spanLabel(p.map(x=>x.starts_on).sort()[0]+'T00:00:00Z',p.map(x=>x.ends_on).sort().at(-1)+'T00:00:00Z','UTC');};
const sessionsOf=(n:number,what:string)=>`${n} ${what?what+' ':''}session${n===1?'':'s'}`;
/** How a bound the server prorated by working days says so, the same everywhere (findings, quota reports, previews):
 *  " (prorated: 1 of 5 days)", or '' when it was not prorated. Never recalculated here. */
export const prorated=(p?:{working_days:number;full_working_days:number}|null)=>p?` (prorated: ${num(p.working_days)} of ${num(p.full_working_days)} days)`:'';
const needs=(c:FindingCause)=>c.proration?num(c.proration.amount)+prorated(c.proration):num(c.required);
const CAUSES:Record<string,{head:(what:string)=>string;why:(c:FindingCause,what:string,dates:string)=>string}>={
  no_candidate_sessions:{head:w=>`there are no ${w||'matching'} sessions in this rota`,why:(c,w,d)=>`Needs ${needs(c)}; ${sessionsOf(c.sessions_found,w)} exist ${d}`.trim()},
  no_eligible_people:{head:w=>`nobody who needs them can work the ${w||'matching'} sessions`,why:(c,w,d)=>`${sessionsOf(c.sessions_found,w)} exist ${d}, but nobody it applies to is eligible and free for them`},
  capacity_short:{head:w=>`too few ${w||'matching'} places for everyone`,why:(c,w,d)=>`Needs ${needs(c)}; at most ${num(c.possible??0)} possible from ${sessionsOf(c.sessions_found,w)} ${d}`.trim()},
  person_unavailable:{head:w=>`on leave or away when the ${w||'matching'} sessions run`,why:(c,w,d)=>`${sessionsOf(c.sessions_found,w)} exist ${d}, but they are on leave or away for them`},
  partial_window:{head:()=>'only part of the rule period is in this rota',why:c=>`In this rota ${num(c.days_available??0)} of ${num(c.days_in_window)} days; needs ${needs(c)}, has ${num(c.actual??0)}`},
  // The counts only: whether a run fills them is its fix (the Run auto-schedule hint), never claimed here.
  unfilled:{head:w=>`${w||'matching'} places are free but not assigned`,why:c=>`Needs ${needs(c)}; ${num(c.possible??0)} possible, ${num(c.actual??0)} assigned`},
  above_limit:{head:()=>'more than the rule allows',why:c=>`${num(c.actual??0)} assigned; at most ${needs(c)}`},
};
const words=(code:string)=>CAUSES[code]||{head:()=>'the rule is not met',why:(c:FindingCause)=>`Needs ${needs(c)}`};
/** A breach's cause as a headline: "there are no UCC sessions in this rota". */
export const causeHead=(code:string,hints?:Hint[])=>words(code).head(causeWhat(hints));
/** The one line built from the counts behind a finding's cause: "Needs 1; 0 UCC sessions exist 6–12 Sep 2026". */
export const causeWhy=(c:FindingCause,hints?:Hint[])=>words(c.code).why(c,causeWhat(hints),hintDates(hints));
/** A finding's cause in one sentence (hover cards, rule panels, check tables), or '' when the server sent none. */
export function causeText(f:object):string{
  const {cause:c,fix_hints:hints}=f as {cause?:FindingCause;fix_hints?:Hint[]};if(!c)return '';
  const head=causeHead(c.code,hints);return `${head[0].toUpperCase()}${head.slice(1)}: ${causeWhy(c,hints)}`;
}
/** One person's own numbers inside a cause group: "0 of 1 · 6–12 Sep 2026". */
const ownNumbers=(f:Row)=>{const c=f.cause as FindingCause|undefined;
  return [c&&c.actual!=null?`${num(c.actual)} of ${needs(c)}`:'',f.start&&f.end?spanLabel(f.start,f.end,departmentTimeZone()):''].filter(Boolean).join(' · ')||String(f.message);};
/** What a card stands for: the sessions and people its findings name (distinct ids), never a count of findings that
 *  several rules can give the same open place. */
export function affected(found:Row[]):string{
  const sessions=new Set(found.map(f=>f.shift_id).filter(Boolean)).size,people=new Set(found.map(f=>f.person_id).filter(Boolean)).size;
  return [sessions?plural(sessions,'session'):'',people?plural(people,'person','people'):''].filter(Boolean).join(' · ')||plural(found.length,'problem');
}
/** Findings that share a cause, as ONE card (server: RotaSummary.cause_groups): "23 sessions: there are no UCC sessions
 *  in this rota", the line why, who (each person's numbers behind Who) and the fixes. */
export function causeIssue(g:FindingCauseGroup,found:Row[],o:{who:(f:Row)=>string;rule?:Issue['rule'];actions?:Issue['actions']}):Issue{
  const first=found[0],hints=g.fix_hints.length?g.fix_hints:first?.fix_hints||[];
  return {severity:g.severity==='error'?'error':'warning',cause:g.code,words:`${affected(found)}: ${causeHead(g.code,hints)}`,
    reason:first?.cause?causeWhy(first.cause,hints):undefined,rule:o.rule,fold:found.length>3,whyTitle:`Who (${plural(found.length,'finding')})`,
    why:found.map(f=>({who:o.who(f),reason:ownNumbers(f)})),actions:o.actions};
}

/** Cause groups with the same cause over the same repair tasks (RotaSummary.repair_tasks: the open session's own cover
 *  and a "kind of person" rule on the same interval) as ONE group with both rules: one card instead of two cards with
 *  the same title. Joined by server keys; overlapping finding references remain unique. Without tasks, as they are. */
export function mergeCauseGroups(groups:FindingCauseGroup[],tasks:RotaRepairTask[]=[]):FindingCauseGroup[]{
  if(!tasks.length)return groups;
  const taskOf=new Map<string,string[]>();
  for(const t of tasks)for(const k of t.finding_keys)taskOf.set(k,[...(taskOf.get(k)||[]),t.key]);
  const out=new Map<string,FindingCauseGroup>();
  for(const g of groups){
    const id=JSON.stringify([g.code,g.severity,[...new Set(g.finding_keys.flatMap(k=>taskOf.get(k)||['finding:'+k]))].sort()]),had=out.get(id);
    out.set(id,!had?g:{...had,count:new Set([...had.finding_keys,...g.finding_keys]).size,finding_keys:[...new Set([...had.finding_keys,...g.finding_keys])],
      person_ids:[...new Set([...had.person_ids,...g.person_ids])],rule_ids:[...new Set([...had.rule_ids,...g.rule_ids])],fix_hints:[...had.fix_hints,...g.fix_hints]});
  }
  return [...out.values()];
}
/** The issue's key, as the server names it (apps/api/services/issues.py issue_key). */
export const issueKey=(f:Row)=>[...['code','rule_id','shift_id','person_id','start'].map(k=>f[k]||''),
  ...(f.code==='CATALOGUE_VALIDITY_CHANGED'?['catalogue','record_id'].map(k=>f.detail?.[k]||''):[])].join('|');
/** Whether a proposal's problem is already in the draft: 'draft' (the same issue with the same numbers), 'changed' (the
 *  same issue, other numbers: worse or better) or 'new'. */
export type Since='draft'|'changed'|'new';
/** The draft's own check compared with a proposal's, by the server's issue keys and numbers (no rule is evaluated here).
 *  Only when that check and the proposal were both made from the rota's current inputs (the server's fingerprints);
 *  otherwise null: a stale check cannot tell what the proposal brought, so nothing is said. */
export function draftComparison(check:Row|null|undefined,current:string|null|undefined,proposalInput:string|null|undefined){
  if(!check?.fingerprint||!current||check.fingerprint!==current||proposalInput!==current)return null;
  const same=(f:Row)=>JSON.stringify(f.detail??null),errors=new Map<string,string>(),open=new Map<string,number>();
  for(const f of (check.findings||[]) as Row[])if(f.severity==='error')errors.set(issueKey(f),same(f));
  for(const c of (check.coverage||[]) as Row[])if(c.status==='gap')open.set(c.shift_id,Math.max(open.get(c.shift_id)??0,Number(c.required??1)-Number(c.available_eligible??0)));
  return {
    finding:(f:Row):Since=>{const had=errors.get(issueKey(f));return had===undefined?'new':had===same(f)?'draft':'changed';},
    /** An open-place card: the session's open places in the draft against the proposal's. */
    place:(shiftId:string,openNow:number):Since=>{const had=open.get(shiftId);return had===undefined?'new':had===openNow?'draft':'changed';},
  };
}
/** A card standing for several problems: 'draft' only when every one is; otherwise how many are new or changed. */
export function sinceOfMany(list:Since[]):{since?:Since;sinceCount?:number}{
  const moved=list.filter(x=>x!=='draft');
  if(!list.length)return {};
  if(!moved.length)return {since:'draft'};
  return {since:moved.every(x=>x==='new')?'new':'changed',...(moved.length<list.length?{sinceCount:moved.length}:{})};
}
const SINCE_WORDS:Record<Since,string>={draft:'Already in the draft',changed:'Changed by this proposal',new:'New with this proposal'};
export const sinceLabel=(since:Since,count?:number)=>count!=null?`${count} new or changed by this proposal`:SINCE_WORDS[since];
/** The since badge, one place for the issue list's cards and the auto-schedule result's Fix-list row. */
export const sinceBadge=(since:Since,count?:number)=>badge(sinceLabel(since,count),since==='draft'?'neutral':'warn','issue-since');
/** What fixes an open place's main reason: add a free person, ask for an exception, or give people eligibility, a skill
 *  or a rank (People), reactivate people, or change a rule. */
export type PlaceFix='free'|'exception'|'eligibility'|'skill'|'rank'|'inactive'|'rule'|'';
// People who are not in the department now: never the main reason while anyone else is ruled out for something fixable.
const OUTSIDE=new Set(['marked inactive','without current department membership']);
/** The one reason that decides an open place, from the server's reasons (solver_summary.py: `people` could have worked it
 *  and say why not, `unusable` counts everyone ruled out by eligibility): someone free first, then why the people who could
 *  work it cannot, then why nobody can (a missing skill before a rank before clinic eligibility). */
export function placeReason(p:Row,who:(id:string)=>string):{text:string;fix:PlaceFix}{
  const people:Row[]=p.people||[],free=people.filter(x=>x.kind==='FREE');
  if(free.length)return {text:free.length===1?`${who(free[0].person_id)} could work it, but the schedule needs them elsewhere`:`${free.length} people could work it, but the schedule needs them elsewhere`,fix:'free'};
  if(people.length){
    const counts=new Map<string,number>();
    // Protected time names its rule when one rule protects them all (the server counts "restricted by <rule>").
    const restricted=Object.keys(p.unusable||{}).filter(t=>t.startsWith('restricted by ')),protectedBy=restricted.length===1?` (${restricted[0].slice(14)})`:'';
    for(const x of people){const t=x.leave?'on leave':reasonText({kind:x.kind,rule:x.rule})+(x.kind==='PROTECTED'&&!x.rule?protectedBy:'');counts.set(t,(counts.get(t)||0)+1);}
    const parts=[...counts].sort((a,b)=>b[1]-a[1]);
    return {text:people.length===1?`The only person who could work it, ${who(people[0].person_id)}, is ${parts[0][0]}`
      :`Nobody who could work it is free: ${parts.map(([t,n])=>`${n} ${t}`).join(', ')}`,fix:people.some(x=>x.rule_id)?'exception':''};
  }
  const ruled=Object.entries((p.unusable||{}) as Record<string,number>).sort((a,b)=>b[1]-a[1]);
  const inside=ruled.filter(([t])=>!OUTSIDE.has(t)),pick=(re:RegExp)=>(inside.length?inside:ruled).find(([t])=>re.test(t))?.[0];
  const skill=pick(/required skill/),rank=pick(/allowed rank/),clinic=pick(/^missing clinic eligibility/),rule=pick(/^restricted by/);
  if(skill)return {text:skill.includes(': ')?`Nobody who can work here has the skill ${skill.split(': ')[1]}`:'Nobody who can work here has the required skill',fix:'skill'};
  if(rank)return {text:`Nobody has a rank allowed for this duty${rank.includes('(')?' '+rank.slice(rank.indexOf('(')):''}`,fix:'rank'};
  if(clinic)return {text:`Nobody is eligible for ${clinic.replace(/^missing clinic eligibility( for )?/,'')||'this clinic'}`,fix:'eligibility'};
  if(rule)return {text:rule==='restricted by a rule'?'A rule rules everyone out':`${rule.replace(/^restricted by /,'')} rules everyone out`,fix:'rule'};
  const [first]=ruled[0]||[''];
  if(first==='marked inactive')return {text:'Everyone who could work it is marked inactive',fix:'inactive'};
  if(first==='without current department membership')return {text:'Nobody who could work it has a current rank in the department',fix:'rank'};
  return {text:first?`Nobody can work it: ${plural(ruled[0][1],'person','people')} ${first}`:String(p.message||'Nobody can work it'),fix:''};
}
/** An open place from the auto-schedule summary: the main reason on the card, everyone's reasons behind Why. */
export function openPlaceIssue(p:Row,who:(id:string)=>string,title:string,session:string,actions:Issue['actions']=[]):Issue{
  const people:Row[]=p.people||[];
  return {severity:'error',words:title,session,reason:p.people||p.unusable?placeReason(p,who).text:String(p.message||''),fold:true,
    why:people.map(x=>({who:who(x.person_id),reason:x.leave?leaveText(x.leave):reasonText({kind:x.kind,rule:x.rule})})),
    // The people listed by name are not counted again ("3 people unavailable"): only those ruled out by eligibility.
    more:Object.entries((p.unusable||{}) as Record<string,number>).filter(([why])=>!people.length||!/^(unavailable|away on rotation|restricted by)/.test(why)).map(([why,n])=>`${plural(n,'person','people')} ${why}`),actions};
}
