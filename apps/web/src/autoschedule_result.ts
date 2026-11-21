import {el,button,icon,badge,plural,formatDay,formatTime,departmentTimeZone,formDialog,field,area,type Child,needText,needCount,needRow,legend,disabledWhy,helpButton} from './dom.js';
import {fixRow,type FixCause,type FixSummary,type FixHooks} from './fix_list.js';
import {command,applyWrote} from './api.js';
import {relaxSeed as readRelax,previewAdjustment,applyAdjustment,type Adjustment,type RankedFit} from './rule_check_api.js';
import {GENERATION_STOP_REASONS} from './generated.js';
import {shownScore,scoreDelta} from './score.js';
import {currency} from './planning_context.js';
import type {Row} from './types.js';

/** A finished auto-schedule run, said as the Fix list says a rota (mockups A3/A4, owner decisions D1, D3, D9):
 *  better or worse in must-fix causes, the open places and the score change, what is still to fix, a proven clash of
 *  two rules with the governed choices, and a proposal the rota changed under. The run's facts are read in ONE place
 *  (runFacts); everything else draws them. */

/** The result fields this view reads, from the parallel backend contracts:
 *  - maintenance: `fix_list_before`, `fix_list_after` (full Fix lists), `open_places_before/after`, `score_delta` (uncapped)
 *    and `changes_by_cause`. Until a result carries them, `before`/`after` are null and the panel keeps its earlier
 *    layout (app.ts proposalSummary; shortcut: delete that layout once every stored result has a Fix list).
 *  - maintenance: `result.conflict` (SolverConflictExplanation: strict_status, explanation_status, proven, message, causes[]
 *    {requirements, evidence {required_places, maximum_possible, complete, message}, decisions[] {kind:
 *    open_rule|propose_cap_change|review_eligibility, rule_id, line, person_ids, session_ids, starts_on, ends_on}}).
 *  - maintenance: `job.currency` (current | revalidation_required | unknown) and `job.reconciliation` (status ready | partial |
 *    content_conflict | unresolved | applied | revalidation_required, addition_indexes, removal_indexes,
 *    conflicting_placements, changed_facts, current_fingerprint). NOT YET AVAILABLE: a receipt before Apply (maintenance WIP
 *    stores it when Apply runs), a fact's time and wording field, and the count of unrelated changes. */
/** BE-FIX-B `result.generation_outcome`: the final proposal's truth (COVERAGE-STATUS.md). The headline reads it, never
 *  the strict phase's history; `strict_status` is kept as evidence only. */
export type GenerationOutcome={status:'OPTIMAL'|'FEASIBLE'|'INFEASIBLE'|'UNKNOWN';candidate:'valid'|'with_gaps'|'none';strict_status:'INFEASIBLE'|null;optimality_proven:boolean};
export interface RunFacts{scoreDelta:number|null;before:FixSummary|null;after:FixSummary|null;beforeCauses:FixCause[];afterCauses:FixCause[];changesByCause:Row[];
  conflict:Row|null;currency:string;reconciliation:Row|null;outcome:GenerationOutcome|null}
export function runFacts(job:Row):RunFacts{
  const r:Row=job.result||{},s:Row=r.summary||{};
  type Listed={summary?:FixSummary;causes?:FixCause[]};
  const list=(x:Row|undefined):Listed|null=>x?(x.summary&&typeof x.summary==='object'?x as Listed:'must' in x?{summary:x as unknown as FixSummary}:null):null;
  const before=list(r.fix_list_before??s.fix_list_before),after=list(r.fix_list_after??s.fix_list_after);
  const conflict=r.conflict??((r.diagnostics||[]) as Row[]).find(d=>d?.strict_status==='INFEASIBLE')??null;
  // maintenance: open places and the uncapped score change travel beside the Fix lists.
  const num=(x:unknown)=>x==null?undefined:Number(x),ob=num(r.open_places_before),oa=num(r.open_places_after);
  return {scoreDelta:r.score_delta??null,before:before?.summary?{...before.summary,...(ob!=null?{places_open:ob}:{})}:null,after:after?.summary?{...after.summary,...(oa!=null?{places_open:oa}:{})}:null,beforeCauses:(before?.causes||[]) as FixCause[],afterCauses:(after?.causes||[]) as FixCause[],
    changesByCause:Array.isArray(r.changes_by_cause??s.changes_by_cause)?(r.changes_by_cause??s.changes_by_cause):[],
    conflict,currency:String(job.currency||''),reconciliation:job.reconciliation??r.reconciliation??null,outcome:r.generation_outcome??null};
}

/** The immutable solver receipt shown on both the run card and its result dialog. The status is the proposal's final
 *  truth (generation_outcome); the model's own status, when it differs, is named as the model's in the help, and only
 *  an OPTIMAL outcome may say "proven best". */
export function solverReceipt(job:Row):HTMLElement|null{
  if(job.kind&&job.kind!=='generation')return null;
  const r:Row=job.result||{},model=String(r.solver_status||job.solver_status||''),status=String(r.generation_outcome?.status||model||'UNKNOWN');
  // D-126: refused before the solver ran (cp_sat.py invalid_inputs): say so in words; no solver status, error or score.
  if(r.status==='invalid_inputs'){const n=Array.isArray(r.issues)?r.issues.length:0;
    return el('p',{class:'solver-receipt',role:'status'},`Not run: the draft has ${n?plural(n,'conflict'):'conflicts'} the run must keep`);}
  const code=String(r.search_stop?.reason||job.search_stop?.reason||r.stop_reason||job.stop_reason||'unknown');
  // An unproven 'optimal' has no contract word, so its clause is left out; an untyped code is said as stored.
  const stop=stopWords(r,code)||(code==='optimal'?'':code);
  // A stopped run's saved summary holds a capped score, never its final one.
  const saved=r.score??job.score??(r.score_completed===false?null:r.summary?.score_saved??r.summary?.score);
  const value=typeof saved==='number'?saved:saved&&typeof saved==='object'?(saved.score??saved.uncapped_score):null;
  const score=value==null
    ?(r.score_completed===false&&String(r.status).startsWith('candidate_')?'score unfinished':'no score: nothing placed')
    :`score: ${value}`;
  return el('p',{class:'solver-receipt',role:'status'},[`Solver status: ${status}`,stop&&`Stop reason: ${stop}`,score].filter(Boolean).join(' · '),
    ...(model&&model!==status?[' ',helpButton('About Solver status',`The solver's own model ended ${model}, but the final proposal after checks is ${status}${status==='OPTIMAL'?'':', so it is not proven best'}.`)]:[]));
}

/** D16 "Relax for this rota" reviewed:
 *  a non-safety limit raised for this draft only, on its exact binding line, person and dates (exclusive end), with a
 *  required reason. It takes effect at once in the draft's checks, is approved with the rota at submission, and is
 *  undone as a draft edit. Never offered for a safety kind (D7: callers filter those out and the server refuses them).
 *  The clash card relaxes inline (R4); the Fix list's adjust_rota for several people opens adjustForRotaDialog (one
 *  person: the rule line panel, rule_catalogue.ts relaxForPerson). The maintenance calls are rule_check_api.ts's. */
export type AdjustScope={rule_id:string;line_id?:string|null;slot?:string|number|null;person_ids?:string[];starts_on?:string|null;ends_on?:string|null};
type Preview={adjustment:Adjustment|null;effect:{cleared:number;added:number}|null;fit:RankedFit[]};
/** The relax seed for one person (maintenance /relax: exact scope and binding number) and what raising it by one would do to
 *  this draft (adjustment-effect, nothing saved), with `fit` ranked by the server. Null when the server offers none. */
export async function relaxPreview(d:AdjustScope,person:string|undefined,w:{id:string;revision:number},fit:string[]=[]):Promise<Preview|null>{
  if(!d.line_id||!person)return null;
  const seed=await readRelax({rule_id:d.rule_id,person_id:person,binding_line_id:d.line_id,number:d.slot!=null?String(d.slot):undefined,
    week_from:d.starts_on||undefined,week_to:d.ends_on||undefined,workspace_id:w.id}).catch(()=>null);
  const a=seed?.adjustment;if(!a)return null;
  // The preview needs a reason as the command does; it saves nothing, so a placeholder is never stored.
  const r=a.amount==null?null:await previewAdjustment(w.id,w.revision,a,Number(a.amount)+1,'Effect preview',fit).catch(()=>null);
  return {adjustment:a,effect:r?{cleared:r.effect.clears,added:r.effect.adds}:null,fit:r?.ranked_fit?.candidates||[]};
}
/** The one rule_adjust for a relax: the seed's exact scope when the server gave one, else the scope named by `d`. */
export function relaxCommand(d:AdjustScope,person:string|undefined,seed:Adjustment|null,w:{id:string;revision:number},amount:number,reason:string){
  if(seed)return applyAdjustment(w.id,w.revision,seed,amount,reason);
  const dates=d.starts_on&&d.ends_on?{starts_on:d.starts_on,ends_on:d.ends_on}:{};
  return command('workspace.rule_adjust',{policy_id:d.rule_id,...(d.line_id?{line_id:d.line_id,...(d.slot!=null?{number:String(d.slot)}:{}),...(person?{person_id:person}:{})}:{}),...dates,
    mode:'set',amount,reason},w.id,w.revision);
}
export function adjustForRotaDialog(d:AdjustScope,w:{id:string;revision:number},h:{ruleName:(id:string)=>string;person:(id:string)=>string;done:()=>Promise<unknown>}){
  const person=d.person_ids?.length===1?d.person_ids[0]:undefined;
  formDialog('Allow for this rota',[person?h.person(person):'',h.ruleName(d.rule_id)].filter(Boolean).join(' · '),form=>{
    const amount=field('New limit','number','',{required:true,min:0,step:'any'}),why=area('Reason','',2);
    why.input.required=true;why.input.minLength=3;form.append(amount.wrap,why.wrap);
    let seed:Adjustment|null=null;
    void relaxPreview(d,person,w).then(r=>{seed=r?.adjustment||null;if(seed&&!amount.input.value&&seed.amount!=null)amount.input.value=String(seed.amount);});
    return async()=>{await relaxCommand(d,person,seed,w,Number(amount.input.value),why.input.value.trim());await h.done();};
  },'Allow');
}

/** The rota changed under this proposal and the server checks it again on Apply (maintenance), or already did and some of it
 *  still fits: it stays reviewable instead of being thrown away. A run a newer one replaced is never re-checked (its
 *  earlier receipt included): Apply refuses it. */
export const rechecked=(job:Row)=>job.currency!=='superseded'&&(job.currency==='revalidation_required'||['ready','partial'].includes(String((job.reconciliation??job.result?.reconciliation)?.status)));
/** Inputs drifted since the run and nothing was reconciled yet (maintenance/maintenance): said plainly, never "N still fit" before
 *  the server has checked it. */
export const drifted=(job:Row)=>job.currency==='revalidation_required'&&!['ready','partial'].includes(String((job.reconciliation??job.result?.reconciliation)?.status));
export const driftNote=(job:Row)=>drifted(job)
  ?el('p',{class:'auto-stale',role:'status'},icon('alert'),'The rota changed since this run · Apply checks it against the current rota first'):null;

/** Strictly current: the server says the run fits the rota as read now (planning_context currency ''), with no re-check
 *  pending. app.ts currentGeneration also lets a re-checked run be reviewed and applied (A4); only a strictly current run
 *  may claim its proof or that it is ready to submit (AGENTS §1.4). */
export const strictlyCurrent=(job:Row,fingerprint:string|undefined)=>!currency(job,fingerprint)&&!rechecked(job);
/** The run's proof words, only for a strictly current (`current`), valid, non-diagnostic candidate: FEASIBLE and OPTIMAL
 *  stay distinct, and nothing else is ever called the best. */
export const candidateOptimality=(job:Row,current:boolean)=>current&&job.result?.status==='candidate_feasible'&&job.result.validation?.valid===true&&!job.diagnostic_proposal&&
  !job.result.diagnostic_proposal?(job.result.optimality_proven===true?'Best proposal for these priorities':job.result.optimality_proven===false?['Proposal ready · a better one may exist',searchEnded(job.result)].filter(Boolean).join(' · '):''):'';
/** The panel header's readiness words for a run that still applies: "Current rota is ready for review" (a valid candidate
 *  with no change) and the proof words only when strictly current; otherwise '' and the caller says its A4 label
 *  ("Ready for review"), which is about review, not the rota or submitting. */
export function readyWords(job:Row,reviewable:boolean,fingerprint:string|undefined):string{
  const r:Row=job.result||{},strict=strictlyCurrent(job,fingerprint);
  return strict&&r.status==='candidate_feasible'&&r.validation?.valid===true&&!r.additions?.length&&!r.removals?.length?'Current rota is ready for review'
    :candidateOptimality(job,reviewable&&strict);
}

/** Apply's receipt (maintenance): checked against the rota now, none of the proposal fits, so nothing was written. Said where
 *  Apply was pressed, never a silent close. */
export const nothingApplied=(reply:Row|null|undefined)=>['content_conflict','unresolved'].includes(String(reply?.reconciliation?.status));
/** A successful Apply is the writer's durable receipt, not merely a 200 response (api.ts applyWrote).
 * Revalidation keeps the proposal, so the review must stay visible until a later Apply says `applied`. */
export const applyNeedsReview=(reply:Row|null|undefined)=>!applyWrote(reply);
export const APPLY_NOT_CONFIRMED=['Suggestions were not applied','The rota changed while this proposal was being reviewed. Review the updated proposal before trying again.'] as const;
export const NOTHING_APPLIED=['Nothing was applied','None of its changes fit the rota as it is now. Run auto-schedule again for the current rota.'] as const;

/** What happened, said once: a tone (with its icon: colour is never the only signal), one sentence, then what is needed. */
export function outcome(tone:'ok'|'warn'|'err'|'neutral',title:string,...next:Child[]):HTMLElement{
  return el('div',{class:'auto-outcome '+tone,role:'status'},
    el('span',{class:'auto-outcome-icon','aria-hidden':'true'},icon(tone==='ok'?'check':tone==='neutral'?'clock':'alert')),
    el('div',{class:'auto-outcome-text'},el('strong',{},title),...next.filter(Boolean).map(n=>el('p',{},n))));
}
const round=(n:number)=>Math.round(n*10)/10;
/** "−2 from 3", "±0", "+1": the change from the rota now; `lowerIsBetter` colours it. */
export function change(after:number,before:number|null|undefined,lowerIsBetter=true):Child{
  if(before==null)return '';const d=after-before;
  if(!d)return el('span',{class:'muted'},' ±0');
  return el('span',{class:'score-delta '+((d<0)===lowerIsBetter?'up':'down')},` ${d>0?'+':'−'}${Math.abs(d)}`,el('span',{class:'muted'},` from ${before}`));
}
export const tile=(label:Child,value:string,extra:Child,tone='')=>el('div',{class:'auto-stat '+tone},el('small',{},label),el('div',{},el('b',{},value),extra));
/** "+2.5 from 68.9": a change from the saved rota now; `better` colours it. */
export const scoreChange=(n:number,better:boolean,from:string)=>el('span',{class:'score-delta '+(better?'up':'down')},` ${n>0?'+':'−'}${round(Math.abs(n))}`,el('span',{class:'muted'},` from ${from}`));
/** The Score tile with About score: the run's OWN score (summary.score_saved, else summary.score), the footer's number
 *  once applied, with its change from the saved rota now. Without a score of its own the run has no Score tile: the
 *  saved rota's score (`rotaNow`, workspace.validation.score) is a separate "Score now" tile (A-03), never the run's
 *  number, never a bare dash. */
/** `strict` false: the rota drifted under this run (not strictly current), so neither the run's own score nor its
 *  change "from the saved rota now" is a claim it can still make: the rota's score is said as "Score now", no change. */
export function scoreTile(s:Row,rotaNow?:Row|null,strict=true):HTMLElement{
  const own=strict?s.score_saved??s.score:null,before=s.score_before;
  if(own==null){const now=rotaNow?shownScore(rotaNow):null;return tile('Score now',now!=null?String(now):'–','');}
  const score=shownScore(own),delta=scoreDelta(before,own);
  return tile(el('button',{type:'button',class:'link-button small','aria-label':'About score','data-help':'',
      'data-tip':'The schedule score this rota will have once you apply this result: the same number as the footer and Insights. The change is from the saved rota now.'},'Score',icon('help')),
      score!=null?String(score):'–',delta?scoreChange(delta,delta>0,String(shownScore(before))):'');
}
/** A proven strict clash of mandatory rules that still stands in the proposal (never a diagnostic run's own FEASIBLE,
 *  never an UNKNOWN search). R-5: a proposal whose mandatory checks all pass is never headed "can't all be met" — the
 *  final candidate decides (generation_outcome; before it, the after Fix list), the strict phase is history. */
export const isClash=(f:RunFacts)=>f.conflict?.strict_status==='INFEASIBLE'&&(f.outcome?f.outcome.candidate!=='valid':!(f.after&&!f.after.must));
const policies=(c:Row|undefined)=>((c?.requirements||[]) as Row[]).filter(x=>x.kind==='policy');

/** The contract's words for a stop reason (GENERATION_STOP_REASONS), read by the receipt and the panel header alike.
 *  The worker sends 'optimal' when every model step was proven; it says "proven best" only when the final proposal is
 *  (generation_outcome OPTIMAL), else ''. */
/** The final proposal is proven best: its outcome's status is OPTIMAL (an outcome stored before it had a status says
 *  optimality_proven); without an outcome, the result's own optimality_proven. Undefined when the result does not say. */
const proven=(r:Row):boolean|undefined=>{const o=r.generation_outcome;
  return o?(o.status?o.status==='OPTIMAL':o.optimality_proven===true):r.optimality_proven;};
export function stopWords(r:Row,code:string):string{
  if(code==='optimal'&&proven(r)===false)return '';
  return (GENERATION_STOP_REASONS as Record<string,string>)[code]||'';
}
/** Why the search ended, as one short clause: the result's typed `search_stop.reason` in the contract's words
 *  (GENERATION_STOP_REASONS); a result stored before it existed is read from its own receipts (the objectives' and the
 *  fair share search's); '' when the result does not say (never guessed). */
export function searchEnded(r:Row|null|undefined):string{
  if(!r)return '';
  const typed=stopWords(r,String(r.search_stop?.reason||''));if(typed)return typed;
  if(proven(r)===true)return GENERATION_STOP_REASONS.optimal;
  const rows=(r.objectives||[]) as Row[],reason=String(r.fair_share_search?.stop_reason||rows.map(x=>x.search?.stop_reason).find(Boolean)||'');
  if(reason==='operator_stop')return 'stopped by you';
  if(reason==='plateau'||rows.some(x=>x.stopped_without_improvement))return 'stopped when it stopped improving';
  if(reason==='time_limit'||reason==='slice_limit')return 'search time used up';
  return '';
}
/** A run continued past a rule conflict (on_conflict continue_flagged): its breaks are must-fixes, said where the result is. */
export const breaksNote=(r:Row|null|undefined)=>{const n=r?.on_conflict==='continue_flagged'?((r.breaks||[]) as Row[]).length:0;
  return n?el('p',{class:'auto-stale auto-breaks',role:'status'},icon('alert'),`Continued with ${plural(n,'break')} · each must be fixed before publishing`):'';};
/** A score change as the other tiles say a change: "+3.2", "−1", and "±0" when nothing moved. */
const scoreChangeWords=(d:number)=>{const r=round(d);return r?`${r>0?'+':'−'}${Math.abs(r)}`:'±0';};
/** The run in Fix-list terms (A3): one outcome sentence and three numbers. Null when the result has no Fix list yet.
 *  `worse`: a new must-fix cause, so Discard becomes the primary. `optimality`: the run's proven-or-not words
 *  (candidateOptimality), given only for a strictly current, valid, non-diagnostic candidate: without them a run never
 *  says it is ready to submit, whatever its Must fix count; with them they are the third node, after the numbers. */
export function resultHead(f:RunFacts,o:{changes:number;scoreDelta:number|null;stale?:boolean;optimality?:string;/** Not strictly current: no number that compares the run with the rota now. */strict?:boolean}):{nodes:HTMLElement[];worse:boolean}|null{
  const a=f.after,b=f.before;if(!a)return null;
  const clash=isClash(f),open=a.places_open,first=f.afterCauses.find(c=>c.level==='must');
  const cleared=b?Math.max(0,b.must-a.must):0,worse=!!b&&a.must>b.must;
  type Said=[tone:'ok'|'warn'|'err'|'neutral',title:string,...next:Child[]];
  const said:Said=clash?['err',policies(f.conflict!.causes?.[0]).length===2?'Two rules can’t both be met':f.conflict!.proven?'These rules can’t all be met':'The rules can’t all be met',
      open?`Everything else is filled. ${plural(open,'place')} stay${open===1?'s':''} open.`:'',f.conflict!.proven?'':'The clash has not been identified yet.']
    :worse?['warn',`Worse: ${plural(a.must-b!.must,'new must-fix cause')}`,'Discard it, or apply it and fix them in the Fix list.']
    // Nothing proposed and nothing to fix: never "once applied" when there is nothing to apply.
    :!o.changes&&!a.must?['ok','Nothing to change',open?`${plural(open,'open place')} left; nothing must be fixed.`:'Every place is filled and every mandatory rule is met.']
    :!a.must?[o.optimality?'ok':'neutral',b?.must?`Better: all ${plural(b.must,'must-fix cause')} cleared`:'Nothing must be fixed',o.optimality?'Ready to submit once applied.':'']
    :cleared?['ok',`Better: ${cleared} of ${plural(b!.must,'must-fix cause')} cleared`,a.must===1&&first?`Still to fix: ${first.sentence.replace(/\.$/,'')}.`:`${a.must} still need you.`]
    :['neutral','No must-fix cause cleared',`${plural(a.must,'cause')} still need${a.must===1?'s':''} you.`];
  // The rota changed since the run (driftNote says so above): the outcome is the run's, never "ready once applied".
  const box=o.stale&&!clash?outcome('neutral','When it ran: '+said[1].replace(/^(Better|Worse): /,'').replace(/^./,c=>c.toLowerCase())):outcome(...said);
  // D76 (run card): four numbers, the Fix list's two levels first.
  const stats=el('div',{class:'auto-stats four',role:'group','aria-label':'Result'},
    tile('Must fix',String(a.must),change(a.must,b?.must),a.must?'err':''),
    tile('Could improve',String(a.could),change(a.could,b?.could),''),
    tile('Open places',String(open),change(open,b?.places_open),''),
    // A7: the standalone score stays hidden while anything must be fixed; a change between two versions is always valid.
    clash||o.strict===false||(f.scoreDelta??o.scoreDelta)==null?tile('Changes',String(o.changes),''):tile('Score',scoreChangeWords((f.scoreDelta??o.scoreDelta)!),el('span',{class:'muted'},' vs now')));
  return {nodes:[box,stats,...(o.optimality?[el('p',{class:'hint',role:'status'},o.optimality)]:[])],worse};
}

/** "Still to fix": the one next step (D76), the top must-fix cause left after this run as its Fix-list row with its one
 *  action; the rest are counted, with the way to them. */
export function stillToFix(f:RunFacts,h:FixHooks,more?:()=>void):HTMLElement|string{
  const must=f.afterCauses.filter(c=>c.level==='must');if(!must.length)return '';
  const rest=must.length-1,own=h.key?null:legend('Key');  // the row's level mark, in a shared key (every mark has one)
  return el('section',{class:'auto-still','aria-label':'Still to fix'},el('h3',{},'Still to fix'),el('ul',{class:'fix-list'},fixRow(must[0],own?{...h,key:own}:h,{detail:false})),own?own.root:'',
    rest&&more?el('p',{class:'hint'},button(`${plural(rest,'more cause')} in the Fix list`,more,'link-button')):'');
}
/** The changes grouped by the cause each works on (maintenance changes_by_cause: {cause_id, changes}). SW-04: each row is named
 *  by its cause from the before Fix list (sentence and meta, one source); changes on sessions no cause named are "Other
 *  changes", last. The count is the number of changes, said in words. */
export function changesByCause(f:RunFacts,total:number):HTMLElement|string{
  if(!f.changesByCause.length)return '';
  const causes=new Map(f.beforeCauses.map(c=>[c.id,c])),known=(x:Row)=>x.sentence||x.label||causes.get(String(x.cause_id))?.sentence;
  const rows=[...f.changesByCause].sort((a,b)=>Number(!known(a))-Number(!known(b)));
  return el('section',{class:'auto-still','aria-label':'Changes'},el('h3',{},plural(total,'change')),el('ul',{class:'fix-list'},...rows.map(x=>{
    const c=causes.get(String(x.cause_id)),n=Number(x.changes??0),meta=x.meta||c?.meta;
    return el('li',{},needRow(needCount(n),needText(String(known(x)||'Other changes'),[plural(n,'change'),meta].filter(Boolean).join(' · '))));})));
}

export interface ClashHooks{
  ruleName:(id:string)=>string;person:(id:string)=>string;
  openRule:(ruleId:string)=>unknown;
  /** People who fail only on eligibility: the credentialer hand-off (People, filtered). */
  eligibility:(personIds:string[])=>unknown;
  /** The draft the clash belongs to, and what to do once a relax is saved (re-read the rota, footer and Fix list). */
  workspace:{id:string;revision:number};done:()=>Promise<unknown>;
  /** A clinic's name, for where a cause is. */
  clinic:(id:string)=>string;
  /** The person exception flow for one person_exception decision (null where the rule allows none). */
  exception?:(d:Row)=>HTMLElement|null;
  /** Leave a cause's places open: the run that fills everything else (offered only where a run can start). */
  leaveOpen?:()=>unknown;
}
const day=(d:string)=>formatDay(d+'T12:00:00Z','UTC',true);
const span=(a?:string|null,b?:string|null)=>a&&b?(a===b?day(a):`${day(a)} – ${day(b)}`):'';
/** Where and when a cause is: its clinics and dates ("ED, Clinic 2 · 4 – 9 Oct"). */
const where=(c:Row,h:ClashHooks)=>{const ids=(c.service_ids||[]) as string[];
  return [ids.length>3?plural(ids.length,'clinic'):ids.map(h.clinic).filter(Boolean).join(', '),span(c.starts_on,c.ends_on)].filter(Boolean).join(' · ');};
/** A cause's smallest fix, as one button: an exception for one person, else this rota's cap relaxed (never a safety
 *  rule), else its places left open, else its rule opened. Null when the server offers none of them. */
function smallestFix(c:Row,h:ClashHooks):HTMLElement|null{
  const ds=(c.decisions||[]) as Row[],safe=new Set(policies(c).filter(r=>r.safety).map(r=>r.rule_id));
  const ex=ds.find(d=>d.kind==='person_exception'&&!safe.has(d.rule_id)),allow=ex&&h.exception?.(ex);if(allow)return allow;
  const cap=ds.find(d=>d.kind==='propose_cap_change'&&!safe.has(d.rule_id));
  if(cap)return button('Allow for this rota…',()=>adjustForRotaDialog(cap as unknown as AdjustScope,h.workspace,h),'secondary small');
  if(h.leaveOpen&&ds.some(d=>d.kind==='leave_open'))return button('Leave as open place',()=>h.leaveOpen!(),'secondary small');
  const open=ds.find(d=>d.kind==='open_rule');
  return open?button('Open rule',()=>h.openRule(open.rule_id),'secondary small',{'aria-label':`Open ${h.ruleName(open.rule_id)}`}):null;
}
let relaxSerial=0;
/** "Relax the cap for this rota" (R4, D16), inline: the cap's words, the people it could be relaxed for (ranked by the
 *  server's fit once known: open places they could take, checks cleared), a required reason, and the effect BEFORE
 *  anything applies ("Checking…" while the server works it out, never a guess). `caps`: the server's
 *  propose_cap_change decisions for one non-safety line, one per person. The new limit is one more than the binding
 *  number from the relax seed; without a seed it is typed. */
function relaxCard(caps:Row[],h:ClashHooks):HTMLElement{
  const first=caps[0],id=`relax-${++relaxSerial}`;
  const words=el('small',{}),effect=el('p',{class:'relax-effect',role:'status'});
  const reason=el('textarea',{id:id+'-why',rows:2,required:true,minlength:3,class:'relax-reason'}) as HTMLTextAreaElement;
  const go=button('Relax for this rota',()=>save(),'secondary small',{disabled:true,why:'Give an amount and a reason first.'});
  const typed=el('input',{type:'number',id:id+'-n',min:0,step:'any',required:true}) as HTMLInputElement,typedWrap=el('div',{class:'relax-number'},el('label',{for:id+'-n',class:'relax-label'},'New limit'),typed);
  const person=(d:Row)=>(d.person_ids||[])[0] as string|undefined;
  let chosen:Row=caps[0],seed:Preview|null=null,checking=false,turn=0;
  const now=()=>seed?.adjustment?.amount,amount=()=>now()!=null?Number(now())+1:typed.value===''?NaN:Number(typed.value);
  const sync=()=>{const a=now();typedWrap.hidden=checking||a!=null;
    words.textContent=[h.ruleName(first.rule_id),span(first.starts_on,first.ends_on),a!=null?`≤ ${Number(a)+1} instead of ≤ ${a} for one person`:'one person may work more'].filter(Boolean).join(' · ');
    const e=seed?.effect;
    effect.replaceChildren(...(checking?[el('span',{class:'spinner','aria-hidden':'true'}),'Checking…']
      :e?[icon(e.added?'alert':'check'),el('span',{},el('strong',{},`Clears ${plural(e.cleared,'check')}`),' · ',e.added?plural(e.added,'new must fix','new must fix'):'no new must fix',' · approved with the rota')]
      :[el('span',{},'Approved with the rota')]));
    effect.classList.toggle('bad',!!e?.added);
    disabledWhy(go,checking?'Checking…':!(amount()>=0)?'Give an amount first.':reason.value.trim().length<3?'Give a reason (3 characters or more).':'');};
  // The first read also ranks every person in the clash (the same adjustment for each); later reads only re-check one.
  const read=(rank=false)=>{const mine=++turn;checking=true;seed=null;sync();
    void relaxPreview({...chosen,rule_id:String(chosen.rule_id)} as AdjustScope,person(chosen),h.workspace,rank&&caps.length>1?caps.map(person).filter((x):x is string=>!!x):[])
      .then(r=>{if(mine!==turn)return;seed=r;checking=false;if(r?.fit.length)ranked(r.fit);sync();});};
  async function save(){if(!(amount()>=0))return;
    await relaxCommand(chosen as unknown as AdjustScope,person(chosen),seed?.adjustment||null,h.workspace,amount(),reason.value.trim());await h.done();}
  reason.addEventListener('input',sync);typed.addEventListener('input',sync);
  const labels=caps.map((d,i)=>{
    const r=el('input',{type:'radio',name:id,value:String(i),...(i===0?{checked:true}:{})}) as HTMLInputElement;
    r.addEventListener('change',()=>{if(r.checked){chosen=d;read();}});
    return {pid:person(d),label:el('label',{class:'relax-person'},r,el('span',{},h.person(person(d)||'')),el('small',{}))};});
  const people=el('fieldset',{class:'relax-people'},el('legend',{class:'sr-only'},'Relax it for'),...labels.map(x=>x.label));
  /** The server's ranking: best fit first, each with what it would take and clear; the people it excluded last. */
  function ranked(fit:RankedFit[]){const rank=new Map(fit.map((f,i)=>[f.person_id,i]));
    for(const x of [...labels].sort((a,b)=>(rank.get(a.pid||'')??fit.length)-(rank.get(b.pid||'')??fit.length))){
      const f=fit.find(y=>y.person_id===x.pid);
      x.label.querySelector('small')!.textContent=f?[`${plural(f.eligible_open_places,'open place')} they can take`,f.cleared_checks?`clears ${f.cleared_checks}`:''].filter(Boolean).join(' · '):'Not a fit';
      people.append(x.label);}
    // The best fit becomes the choice (its own effect is read again) unless the coordinator already chose.
    const best=labels.find(x=>x.pid===fit[0].person_id),radio=best?.label.querySelector('input');
    if(radio&&!radio.checked&&turn===1){radio.checked=true;radio.dispatchEvent(new Event('change'));}}
  read(true);
  return el('li',{class:'relax'},el('div',{class:'relax-head'},el('strong',{},'Relax the cap for this rota'),words),people,
    typedWrap,el('label',{for:id+'-why',class:'relax-label'},'Reason'),reason,el('div',{class:'relax-foot'},effect,go));
}
/** The clash card (A3 under R4): the rules that cannot all hold side by side, the server's arithmetic when it proved
 *  it, then what you can do: relax the non-safety cap for this rota (D16), make more people eligible (only when people
 *  fail on eligibility alone), or open the two rules. A safety rule in the clash is said once: its places cannot be
 *  submitted open (D3). Only choices the server offers; never a relax for a safety kind (D7). */
export function clashCard(conflict:Row,h:ClashHooks):HTMLElement{
  const cause:Row|undefined=(conflict.causes||[])[0],rules=policies(cause);
  if(!cause||!conflict.proven)return el('section',{class:'auto-clash','aria-label':'Clash'},el('p',{class:'clash-math'},String(conflict.message||'The strict requirements cannot all be met; the clash has not been identified yet.')));
  // The rule's name once: the small line says only what the title does not (another rule name, the line).
  const card=(r:Row)=>{const title=String(r.name||h.ruleName(r.rule_id)),over=[h.ruleName(r.rule_id)||r.name,r.line!=null&&r.line>=0?`line ${r.line+1}`:''].filter(x=>x&&x!==title).join(' · ');
    return el('div',{class:'clash-rule'},over?el('small',{},over):'',
    el('strong',{},title),
    el('span',{},...[span(r.starts_on,r.ends_on)?el('span',{class:'rule-num muted'},span(r.starts_on,r.ends_on)+' '):'',badge('Mandatory','ok'),r.safety?badge('Safety','neutral'):''].filter(Boolean)));};
  const pair=rules.length===2?el('div',{class:'clash'},card(rules[0]),el('span',{class:'clash-vs'},'and'),card(rules[1])):el('div',{class:'clash clash-many'},...rules.map(card));
  const ev:Row=cause.evidence||{};
  const math=[ev.complete&&ev.required_places!=null&&ev.maximum_possible!=null?`${plural(Number(ev.required_places),'place')} needed, ${ev.maximum_possible} possible.`:'',String(ev.message||cause.message||'')].filter(Boolean).join(' ');
  const safe=rules.filter(r=>r.safety),decided=(cause.decisions||[]) as Row[],decisions:HTMLElement[]=[];
  const row=(title:string,sub:string,act:HTMLElement)=>el('li',{},el('span',{},el('strong',{},title),sub?el('small',{},sub):''),act);
  // One relax card per non-safety line (its people are the server's cap decisions for that line).
  const lines=new Map<string,Row[]>();
  for(const d of decided)if(d.kind==='propose_cap_change'&&!safe.some(r=>r.rule_id===d.rule_id)){const k=`${d.rule_id}|${d.line_id??d.line}`;lines.set(k,[...(lines.get(k)||[]),d]);}
  for(const caps of lines.values())decisions.push(relaxCard(caps,h));
  // The server's review_eligibility decision: the people in the clash, for a credentialer to check who may work it.
  const elig=decided.find(d=>d.kind==='review_eligibility'),who=(elig?.person_ids||[]) as string[];
  if(who.length)decisions.push(row('Review who is eligible',`${plural(who.length,'person','people')} in this clash · a credentialer decides`,
    button('Open People',()=>h.eligibility(who),'secondary small')));
  // One person's exception (the existing exception flow) and leaving the places open, where the server offers them.
  for(const d of decided)if(d.kind==='person_exception'&&!safe.some(r=>r.rule_id===d.rule_id)){const allow=h.exception?.(d);
    if(allow)decisions.push(row('Allow an exception',[h.person(String(d.person_id||(d.person_ids||[])[0]||'')),span(d.starts_on,d.ends_on)].filter(Boolean).join(' · '),allow));}
  if(h.leaveOpen&&decided.some(d=>d.kind==='leave_open'))decisions.push(row('Leave as open place','Everything else is filled; these places stay open for you',
    button('Leave as open place',()=>h.leaveOpen!(),'secondary small')));
  const open=decided.filter(d=>d.kind==='open_rule');
  if(open.length)decisions.push(row(open.length===2?'Open the two rules':'Open the rules','A permanent change goes through approval',
    el('span',{class:'actions'},...open.map(d=>button(open.length>1&&d.line!=null?`Line ${d.line+1}`:'Open',()=>h.openRule(d.rule_id),'secondary small',{'aria-label':`Open ${h.ruleName(d.rule_id)}${d.line!=null?' line '+(d.line+1):''}`})))));
  // Every further cause: what it is, where and when, and its smallest fix.
  const others=((conflict.causes||[]) as Row[]).slice(1).map(c=>{const at=where(c,h);
    return el('li',{},el('span',{},el('strong',{},String(c.message||c.evidence?.message||'')),at?el('small',{},at):''),smallestFix(c,h)||'');});
  const at=where(cause,h);
  return el('section',{class:'auto-clash','aria-label':'Clash'},pair,at?el('p',{class:'clash-where muted'},at):'',math?el('p',{class:'clash-math'},math):'',
    decisions.length?el('h3',{},'What you can do'):'',decisions.length?el('ul',{class:'decisions'},...decisions):'',
    ...safe.map(r=>el('p',{class:'clash-safety muted'},icon('shield'),`${r.name||h.ruleName(r.rule_id)} is a safety rule, so these places can’t be submitted open.`)),
    others.length?el('h3',{},`${plural(others.length,'more cause')}`):'',others.length?el('ul',{class:'decisions clash-causes'},...others):'');
}

/** A proposal the rota changed under (A4, maintenance): what changed, then how much of it still fits after the server checked it
 *  against the latest rota. Null when there is no receipt to show. */
export function inputsChanged(job:Row,f:RunFacts,h:{person:(id:string)=>string;session:(id:string)=>string}):{nodes:HTMLElement[];kept:number;additions:number[];removals:number[]}|null{
  const rec=f.reconciliation;if(!rec||!['ready','partial'].includes(rec.status))return null;
  const r:Row=job.result||{},total=(r.additions||[]).length+(r.removals||[]).length;
  const additions=(rec.addition_indexes||[]) as number[],removals=(rec.removal_indexes||[]) as number[],kept=additions.length+removals.length;
  const facts=(rec.changed_facts||[]) as Row[],zone=departmentTimeZone();
  // "Felix Parker added a wish: Tue 6 Oct", or without a known author "Leave added: Dr Olivia Parker, 20–22 Dec".
  const factText=(x:Row)=>{const what=String(x.description||x.summary||(x.kind==='wish'&&x.wish_kind?`${String(x.wish_kind).replaceAll('_',' ')} wish`:String(x.kind||'record').replaceAll('_',' ')));
    const who=String(x.person_name||(x.person_id?h.person(x.person_id):'')),by=x.actor?.display_name?String(x.actor.display_name):'';
    const verb=x.change==='added'?'added':x.change==='removed'?'removed':'changed',when=span(x.starts_on||x.first_day,x.ends_on||x.last_day);
    return by?`${by} ${verb} ${/^[aeiou]/i.test(what)?'an':'a'} ${what}${who&&who!==by?` for ${who}`:''}${when?`: ${when}`:''}`
      :`${what.replace(/^./,c=>c.toUpperCase())} ${verb}${[who,when].filter(Boolean).length?': '+[who,when].filter(Boolean).join(', '):''}`;};
  const at=(x:Row)=>x.at||x.changed_at;
  const list=facts.length?el('ul',{class:'changed-list','aria-label':'What changed'},...facts.slice(0,8).map(x=>el('li',{},at(x)?el('span',{class:'mono'},formatTime(at(x),zone)):'',
    el('span',{},factText(x))))):'';
  const more=facts.length>8||Number(rec.changed_fact_count)>facts.length?el('p',{class:'muted'},`${plural(Math.max(Number(rec.changed_fact_count)||0,facts.length)-Math.min(8,facts.length),'more change')}.`):'';
  const out=((rec.conflicting_placements||[]) as Row[]),left=Math.max(0,total-kept);
  const named=out.slice(0,3).map(c=>{const i=(c.addition_indexes||[])[0],p=i!=null?r.additions?.[i]?.participant?.person_id:(c.removal_indexes||[]).map((k:number)=>r.removals?.[k]?.person_id)[0];
    return [p?h.person(p):'',c.shift_id?h.session(c.shift_id):''].filter(Boolean).join(' on ');}).filter(Boolean);
  return {kept,additions,removals,nodes:[outcome('warn','The rota changed during the run','The proposal was checked again against the latest rota.'),
    ...(list?[list]:[]),...(more?[more]:[]),
    outcome(left?'warn':'ok',`${kept} of ${plural(total,'change')} still fit`,left?`${left} ${left===1?'is':'are'} left out${named.length?': '+named.join(', '):''}.`:'')]};
}
