/** The one client mapping of the governance/rules contract (maintenance, build/contracts/maintenance; README B3–B5 until a field
 *  is published): a line's relation to the broader line with the same where, its effect on the drafts its dates overlap,
 *  reach over the line's own dates, the relax seed, the approver's review and one-step activation. Screens import from
 *  here, so a contract change moves only this file. */
import {get,request,command,serverChecking,rotaMeta,rotaWhole} from './api.js';
import type {RuleLine,RuleLineEffectInput,PromoteAdjustmentInput,PromoteAdjustmentSeed,RotaSummary} from './generated.js';
import type {Row} from './types.js';

/** How a line stands against the broader line with the same where: it takes over (`overrides`), or both apply and why. */
export type Relation={line_id:string;line_no:number;overrides:boolean;both_apply?:boolean;reason:null|'strength_differs'|'settings_differ'|'where_differs'|'safety';
  fix:null|'match_strength'|'match_settings';default_severity?:Record<string,string>;
  /** The stricter value that applies to this line when both lines remain active. */
  effective_numbers?:Record<string,number|null>;
  /** maintenance: with `fix: match_settings`, the broader line's effective settings this line may take (copied as they are). */default_settings?:Record<string,unknown>};
/** One draft whose dates overlap the line: what problems the change adds or clears. `status` and `started_at` come from the
 *  check job (elapsed time only, never an estimate). */
export type Effect={workspace_id:string;name:string;status?:string;clears?:number;adds?:number;effect?:'clears'|'adds'|'no_change';
  job_id?:string|null;started_at?:string|null;created_at?:string|null;revision?:number;
  /** The number of problems in the approval comparison, which also checks staffing and candidate availability. */
  new_or_worse_count?:number;summary?:string|null;requires_acknowledgement?:boolean;activation_blocker?:boolean};
/** The rule check (POST /api/rules/catalogue/{id}/check): `relations` by edited line id. */
export type Checked={relations?:Record<string,Relation>};
export const relationOf=(c:Checked|null|undefined,lineId:string):Relation|null=>c?.relations?.[lineId]??null;
/** An unsaved line's effect on the drafts its dates overlap (maintenance POST /api/rules/catalogue/{id}/effect): that one line
 *  over the active rule, nothing saved; one Effect row per draft. */
type EffectRow=Omit<Effect,'effect'>&{effect:{status:string;clears:number;adds:number;effect:Effect['effect']}};
export const lineEffect=(catalogueId:string,body:RuleLineEffectInput)=>request<{effects:Record<string,EffectRow[]>}>(
  `/api/rules/catalogue/${encodeURIComponent(catalogueId)}/effect`,{method:'POST',body:JSON.stringify(body)})
  .then(r=>(r.effects[body.line.id]||[]).map(x=>({...x,...x.effect}) as Effect));

const RUNNING=['queued','running','checking'];
export const effectRunning=(x:Effect)=>RUNNING.includes(x.status||'');
/** The one wording of a check that has not started (a job in the queue, or one a panel has just asked for): it waits.
 *  Only a check that is running reads as checking. Empty for every other status. */
export const waitingWord=(status?:string|null)=>status==='queued'?'Waiting to check':'';
/** The job workers as the rota's summary reports them (GET /summary): alive or not, and the heartbeat window (the
 *  server's setting) within which a live worker picks a queued check up. */
export type CheckWorker=Pick<RotaSummary,'worker_liveness'|'worker_heartbeat_max_age_seconds'>;
/** How long a queued check may wait before it reads as not started: the heartbeat window while a worker runs; none when
 *  no worker answers (nothing can start it); no bound while the workers are not known (it waits). */
export const checkBoundMs=(worker?:CheckWorker|null)=>!worker?Infinity:worker.worker_liveness==='running'?worker.worker_heartbeat_max_age_seconds*1000:0;
/** A check nobody is running: its job failed (or was refused), or it has waited in the queue past the bound. */
export const checkUnfinished=(status?:string|null,waitedMs=0,worker?:CheckWorker|null)=>status==='failed'||status==='queued'&&waitedMs>=checkBoundMs(worker);
/** Does a check line (the first check's, or a re-check's beside the kept counts: the same line) offer Cancel? Only while
 *  the check is a job the server can stop (queued or running); never the synchronous check, which has no job (UX-FIX-A). */
export const checkCancellable=(job?:{status:string}|null)=>job?.status==='queued'||job?.status==='running';
/** What a check nobody is running offers, beside its words. */
export const CHECK_AGAIN='Check again';
/** DL-018 + Master 10 Nov 2026, the one automatic fallback: should the page take a check over (stop its queued job,
 *  then run the check itself)? Only a job still queued at the bound (at once when no worker answers), and once: never
 *  a check that is running (it is being served), never one that ended, never again for a job or rota version already
 *  taken over (`attempted`). */
export const checkTakeover=(status:string|undefined,waitedMs:number,attempted:boolean,worker?:CheckWorker|null)=>!attempted&&status==='queued'&&checkUnfinished(status,waitedMs,worker);
/** The reason of the job.cancel a take-over sends, kept in the audit log: fixed and recognisable, so Operations can
 *  count how often no worker started a check in time (the fallback must not hide that). */
export const CHECK_TAKEN_OVER='Check not started in time: run by the page';
/** The take-over's stop: the job is set aside only if it is still queued (job.cancel only_if_queued, audited with
 *  CHECK_TAKEN_OVER). `cancelled: false`: a worker started it meanwhile; the page follows that job and runs no check. */
export const takeOverJob=(id:string)=>command<{cancelled:boolean;job:Row}>('job.cancel',{reason:CHECK_TAKEN_OVER,only_if_queued:true},id);
/** A check refused for the person's own access (the command's 403, or a job the worker failed for it): the reason is
 *  said and no "Check again" is offered, since the person cannot run one (DL-018). */
export const checkRefused=(code?:string,status?:number)=>status===403||code==='FORBIDDEN'||code==='ACCESS_REVOKED';
/** The ONE map of a failed check job's error code to its words: what the rule review's row says, then why, and whether
 *  the banner above the grid says that why too. It does only when the why is a fact the banner's "Check again" does not
 *  already say (DL-018 Rule 1): "run it again" is that button, so the banner drops it. Every screen that words a job's
 *  code reads it here (a new code is added once). */
const CHECK_CODE_WORDS=new Map<string,[said:string,why:string,banner:boolean]>([['ACCESS_REVOKED',['Check failed','your role cannot run it',true]],
  ['WORKER_LEASE_EXPIRED',['Check interrupted','run it again',false]]]);
/** The rule review's row for a failed check job whose code is known ("Check failed · your role cannot run it"), else ''. */
export const checkCodeLabel=(code?:string)=>{const w=CHECK_CODE_WORDS.get(code||'');return w?`${w[0]} · ${w[1]}`:'';};
/** The line above the grid for a check JOB that stopped (DL-018, one state one verb: the pill says "Check stopped"):
 *  "The check stopped · <the code's why>" when the why adds a fact the Check again beside it does not (every access
 *  refusal reads as ACCESS_REVOKED: no Check again then), else "The check stopped." alone. The synchronous check's
 *  refusal keeps the server's own words. */
export const checkStoppedWords=(code?:string,status?:number)=>{const w=CHECK_CODE_WORDS.get(checkRefused(code,status)?'ACCESS_REVOKED':code||'');
  return w?.[2]?`The check stopped · ${w[1]}`:'The check stopped.';};
/** The id of the line above the grid that says why a check stopped: a pill disabled for it points here (DL-007). */
export const CHECK_STOPPED_WHY='check-stopped-why';
/** DL-024: did a check JOB that ended stop the rota's check? Failed: yes. Cancelled by anyone but this person (their
 *  Cancel, or the take-over from any page on the rota, CHECK_TAKEN_OVER, a reason the page may not be told): only when
 *  the rota's check, read after it ended, is not `current`; a current one means nothing stopped (its result shows, no
 *  banner, no pill). */
export const checkEndedStopped=(status?:string,current=false)=>status==='failed'||status==='cancelled'&&!current;
/** DL-024: is the rota's check current, read after its job was set aside? A failed read is "not current": the page cannot
 *  show the rota as checked, so it says the check stopped and offers Check again (Design Lead, 10 Nov 2026). */
export const rotaCheckCurrent=(id:string)=>rotaWhole(id)
  .then(d=>d.workspace?.validation_current===true,()=>false);
/** A rota's check in words, from its JOB's own status (Master, 10 Nov 2026; never from a reply's scope). DL-018, one
 *  state one verb: queued → "Waiting to check" (the same queued rule), past the bound → it hasn't started (the person's
 *  view, not the system's "picked up"); running → "Checking the rota"; failed (refused included) → "Check stopped", as
 *  the line above the grid says ("The check stopped after m:ss"). A cancelled job has no words of its own: whether it
 *  stopped is decided by the caller (app.ts: the person's own Cancel reads "Not checked"; any other, checkEndedStopped),
 *  which words a stopped one as `checkingWords('failed')`, so the words stay here alone. Empty when no job state is known: nothing claims a check.
 *  `waitedMs`: time in the queue. */
export const checkingWords=(status?:string|null,waitedMs=0,worker?:CheckWorker|null)=>status==='failed'?'Check stopped'
  :status==='queued'?(checkUnfinished(status,waitedMs,worker)?CHECK_NOT_STARTED:waitingWord(status)):status==='running'?'Checking the rota':'';
/** A summary's check (the Fix list's, Home's: check_status and check_queued_at) in the same words. Only when the summary
 *  claims a check (api.ts serverChecking decides); no workers are known there, so a queued one waits. */
export const summaryCheckWords=(s?:{checking?:boolean;check_status?:string|null;check_queued_at?:string|null}|null)=>
  serverChecking(null,s)?checkingWords(s!.check_status,s!.check_queued_at?Date.now()-Date.parse(s!.check_queued_at):0):'';
/** A check that never started, said once: checkingWords past the bound, and a take-over that failed too. */
export const CHECK_NOT_STARTED="Check hasn't started";
/** A draft's effect in words with its badge tone; null while it is still being checked (the caller shows elapsed time). */
export function effectWords(x:Effect):{text:string;tone:'ok'|'warn'|'err'|'neutral'}|null{
  if(effectRunning(x))return null;
  if(x.status==='failed')return {text:'Check failed',tone:'err'};
  if(x.status==='not_checked')return {text:'Not checked',tone:'neutral'};
  if(x.status==='unresolved')return {text:'Check incomplete',tone:'warn'};
  // A stopped check established nothing (never "No change").
  if(x.status==='cancelled')return {text:'Check stopped',tone:'warn'};
  const adds=x.new_or_worse_count||x.adds;
  if(adds)return {text:`Adds ${adds} problem${adds===1?'':'s'}`,tone:'err'};
  if(x.clears)return {text:`Clears ${x.clears} problem${x.clears===1?'':'s'}`,tone:'ok'};
  return {text:'No change',tone:'neutral'};
}
/** Activation may go once every overlapping draft's effect is known (D6: failed or unfinished checks never authorize it). */
export const effectReady=(rows:Effect[])=>rows.every(x=>!x.status||x.status==='ready');

/** The relation as one sentence around its link ("line 4"): `lead` + link + `tail`, its tone and its one fix.
 *  `subject`: who this line names ("Marta Turner"); `broader`: the broader line in words ("Internship ≥ 1"); `strength`: the
 *  number whose strength differs, both strengths in words; `still`: what the broader line still asks ("Marta Turner still needs
 *  1 duty"). */
export function relationText(r:Relation,o:{subject:string;broader:string;strength?:{label:string;theirs:string;mine:string}|null;still?:string}):
  {warn:boolean;lead:string;link:string;tail:string;fix:string|null}{
  const link=`line ${r.line_no}`;
  if(r.overrides)return {warn:false,lead:'Overrides ',link,tail:` for ${o.subject} · ${o.broader} at the same clinics`,fix:null};
  if(r.reason==='safety')return {warn:true,lead:'Does not override ',link,tail:': safety lines all apply, so the stricter number binds.',fix:null};
  const why=r.reason==='strength_differs'&&o.strength?`its ${o.strength.label} is ${o.strength.theirs}, this one ${o.strength.mine}`
    :r.reason==='settings_differ'?'its settings differ':r.reason==='where_differs'?'it names other clinics':'they differ';
  const fix=r.fix==='match_strength'&&o.strength?`Make it ${o.strength.theirs}`:r.fix==='match_settings'&&r.default_settings?`Use line ${r.line_no}’s settings`:null;
  return {warn:true,lead:'Does not override ',link,tail:`: ${why}. Both apply${o.still?`, so ${o.still}`:''}.`,fix};
}

/** D8: the strength a new line's number starts with — the most specific broader line with the same where, days and hours
 *  that sets that number (the server applies the same default to an omitted level: rule_lines.default_line_strengths). */
export function broaderStrength(line:RuleLine,lines:readonly RuleLine[],key:string,tier:(l:RuleLine)=>number):string|null{
  const same=(a:RuleLine,b:RuleLine)=>JSON.stringify(a.where||null)===JSON.stringify(b.where||null)
    &&JSON.stringify([...(a.weekdays?.length?a.weekdays:[0,1,2,3,4,5,6])].sort())===JSON.stringify([...(b.weekdays?.length?b.weekdays:[0,1,2,3,4,5,6])].sort())
    &&(a.start_time||'00:00')===(b.start_time||'00:00')&&(a.end_time||'24:00')===(b.end_time||'24:00');
  const t=tier(line);
  const broader=lines.filter(l=>l.id!==line.id&&tier(l)<t&&l.numbers?.[key]&&same(l,line)).sort((a,b)=>tier(b)-tier(a));
  return broader[0]?(broader[0].numbers[key].level??'mandatory'):null;
}

// ---- Reach over the line's own dates (B3) ----
/** A saved line's reach over its own dates: the server reads them from the line when no period is given (maintenance). */
export const lineReachPath=(policyId:string,lineId:string,day:string)=>
  `/api/policies/reach?details=true&policy_id=${encodeURIComponent(policyId)}&line_id=${encodeURIComponent(lineId)}&day=${day}`;

// ---- Relax for this person (B4) ----
export type RelaxTarget={rule_id:string;person_id:string;person_name?:string;binding_line_id:string;binding_line_no?:number;
  /** The blocker's number slot when the line has several (the server infers it only when unambiguous). */number?:string;
  week_from?:string;week_to?:string;workspace_id:string;workspace_name?:string;workspace_revision?:number;relax_allowed?:boolean};
/** D16: the rota-scoped adjustment (workspace.rule_adjust, line/person/period scope); the person supplies the amount. */
export type Adjustment={policy_id:string;mode:'set'|'off'|'clear';amount?:number;line_id:string;number:string;person_id?:string;starts_on?:string;ends_on?:string};
/** The read-only prefill: the person line from the binding line (its where, settings and strength; the person; the
 *  blocker dates) for the department rule, and D16's `adjustment` seed for this rota (the default action). */
export type RelaxSeed={catalogue_id:string;catalogue_version:number;expected_policy_id:string;expected_revision:number;
  binding_line_id:string;binding_line_no:number;line:RuleLine;lines:RuleLine[];needs_approval:boolean;scope?:'rota';adjustment?:Adjustment};
/** A rota adjustment as a department proposal (ROTA-RULES-BE): the adjusted line with this rota's number, the rule's lines
 *  with it applied, and its relation; read-only (nothing is proposed until the line panel proposes it), stricter-only for
 *  a safety kind. */
export const promoteSeed=(policyId:string,input:PromoteAdjustmentInput)=>request<PromoteAdjustmentSeed>(`/api/policies/${encodeURIComponent(policyId)}/promote-adjustment`,
  {method:'POST',body:JSON.stringify(input)});
export const relaxSeed=(t:RelaxTarget)=>request<RelaxSeed>(`/api/policies/${encodeURIComponent(t.rule_id)}/relax`,{method:'POST',
  body:JSON.stringify({workspace_id:t.workspace_id,person_id:t.person_id,binding_line_id:t.binding_line_id,number:t.number,starts_on:t.week_from,ends_on:t.week_to})});

/** Applies the adjustment to the draft at once (checks and auto-schedule use it); reviewed with the rota (D16). */
export const applyAdjustment=(workspaceId:string,revision:number,a:Adjustment,amount:number,reason:string)=>command('workspace.rule_adjust',
  {...a,mode:'set',amount,reason},workspaceId,revision);
/** One person the same adjustment could be made for, ranked by the server (a checked greedy lower bound, never a proof). */
export type RankedFit={person_id:string;display_name:string;eligible_open_places:number;shift_ids:string[];cleared_checks:number;new_must_checks:number};
export type AdjustmentPreview={revision:number;effect:{status:'ready'|'unresolved';clears:number;adds:number;effect:string;summary:string};
  ranked_fit?:{candidates:RankedFit[];excluded:{person_id:string;code:string}[]}};
/** D16: what the adjustment would do to this draft, nothing saved (POST /api/workspaces/{id}/adjustment-effect); with
 *  `fit`, the same adjustment ranked for each of those people. */
export const previewAdjustment=(workspaceId:string,revision:number,a:Adjustment,amount:number,reason:string,fit:string[]=[])=>
  request<AdjustmentPreview>(`/api/workspaces/${encodeURIComponent(workspaceId)}/adjustment-effect`,
    {method:'POST',body:JSON.stringify({expected_revision:revision,adjustment:{...a,mode:'set',amount,reason},...(fit.length?{fit_person_ids:fit}:{})})});
/** The preview as the one Effect row of the rota it applies to. */
export const adjustmentEffect=(workspaceId:string,name:string,revision:number,a:Adjustment,amount:number,reason:string)=>
  previewAdjustment(workspaceId,revision,a,amount,reason)
  .then(r=>[{workspace_id:workspaceId,name,revision:r.revision,status:r.effect.status,clears:r.effect.clears,adds:r.effect.adds,
    effect:(r.effect.adds?'adds':r.effect.clears?'clears':'no_change') as Effect['effect']}] as Effect[]);
/** The workspace's revision when the caller has none (the adjustment is saved against it). */
// The compact detail: the whole rota (megabytes on a real month) is never read for one number.
export const workspaceRevision=(id:string)=>rotaMeta(id).then(r=>r.workspace.revision);

// ---- Rule pickers: active choices only, with how many are hidden (maintenance) ----
export type ActiveChoices={services?:{id:string}[];locations?:{id:string}[];assignment_types?:{id:string}[];
  inactive_count?:{services?:number;locations?:number;assignment_types?:number}};
let active:Promise<ActiveChoices>|null=null;
export const activeChoices=(read:typeof get=get)=>active??=read<ActiveChoices>('/api/catalogues?active_only=true&include=services&include=locations&include=assignment_types').catch(err=>{active=null;throw err;});
export const forgetActiveChoices=()=>{active=null;};

// ---- The approver's decision (B5) ----
/** A proposal's relation and effect, only for drafts whose dates overlap its changed lines (rule_effects.review). */
export type Review={policy_id:string;relations:Record<string,Relation>;effect:Effect[];ready:boolean;
  /** A decision may be made when no check is running and there is no hard blocker. Added problems or an uncertain
   *  effect need the approver's explicit acknowledgement and reason. */can_decide?:boolean;requires_acknowledgement?:boolean;
  /** Why no check was needed (maintenance: lines only of people who left before them), said instead of an empty effect. */summary?:string|null};
export const proposalReview=(policyId:string)=>get<Review>(`/api/policies/${encodeURIComponent(policyId)}/review`);
/** Starts the effect check of one overlapping draft (the existing feasibility job, this rule's change only). */
export const checkEffect=(policyId:string,revision:number,x:Effect)=>command('job.create',
  {workspace_id:x.workspace_id,kind:'feasibility',rule_changes:[{policy_id:policyId,expected_revision:revision}]},undefined,x.revision);
/** Approve and activate in one step (the existing policy.activate; its effect check jobs are the evidence). An
 *  independent approval sends no reason unless one is given (maintenance optional note); self-approval passes its reason. */
export const approveAndActivate=(policyId:string,revision:number,reason:string|null,rows:Effect[],acknowledge=false)=>command('policy.activate',
  {...(reason?{reason}:{}),check_job_ids:rows.map(x=>x.job_id).filter(Boolean),...(acknowledge?{acknowledge_unresolved:true}:{})},policyId,revision);
/** What the Schedule footer says of a rota's check: the ONE decision (app.ts draws each kind). Only a check that is not
 *  current may say "Needs a check" (`stale` keeps the last counts beside Check now, `check-now` is that button alone,
 *  `needs-check` the read-only pill). A current check says its counts, or "Checked" while no Fix list summary is held
 *  (a check's finding counts are another unit than the footer's causes, so none stand in). A provisional check
 *  (scope command_changes) with its job listed says that job's words (`provisional`); with none listed it is "Waiting to
 *  check" until the rota's job list is read (`partial`, `jobsRead`); read with no live check job in it, nothing proves a
 *  check (D-062) and it is not current. `ended`: this page saw the rota's check end (stopped, refused, or its take-over
 *  failed): no job is waited for, so the pill says that end (Check now's words), never "Waiting to check".
 *  `whole`: the whole rota is read (a window keeps its counts). */
export type FooterCheck='stalled'|'checking'|'provisional'|'not-checked'|'counts'|'waiting'|'checked'|'stale'|'check-now'|'needs-check';
export function footerCheck(s:{stalled:boolean;checking:boolean;provisional:boolean;checked:boolean;counts:boolean;
  current:boolean;partial:boolean;jobsRead:boolean;ended:boolean;whole:boolean;draft:boolean}):FooterCheck{
  if(s.stalled)return 'stalled';
  if(s.checking)return 'checking';
  if(s.provisional)return 'provisional';
  if(!s.checked)return 'not-checked';
  if(s.partial&&!s.jobsRead&&!s.ended)return 'waiting';
  if(s.counts&&(s.current&&!s.partial||!s.whole))return 'counts';
  if(s.current&&!s.partial)return 'checked';
  return s.draft?s.counts?'stale':'check-now':'needs-check';
}
