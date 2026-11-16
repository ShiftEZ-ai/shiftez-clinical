import type {LiveChange,LiveScope} from './generated.js';
import {onSessionReset} from './api.js';

/** The live scopes that can move a draft's baseline drift count: the server compares rules (policies, catalogue lines),
 *  duties (types, services, locations, series: the 'catalogue' scope) and the department's score, auto-schedule and
 *  display settings (apps/api/workspace_baselines.py SOURCE_KEYS; scopes from apps/api/live.py COMMAND_SCOPES). Person
 *  and availability facts are live in a draft, never drift; a draft's own edits ('workspace') never are either. */
export const DRIFT_SCOPES:readonly LiveScope[]=['rules','catalogue','settings'];

let epoch=0,seenInputs:number|null=null,owed=false;
// Per account: a new sign-in starts afresh, never comparing against the last account's input revision.
onSessionReset(()=>{epoch=0;seenInputs=null;owed=false;});

/** Every pulse once, as it arrives: a change in a drift scope, or a pulse that names nothing (anything may have
 *  changed) with a moved input revision, means the drift count must be asked for again once the pulse is applied. */
export function noteDriftInputs(changes:readonly LiveChange[]|null|undefined,inputRevision?:number){
  const inputsMoved=seenInputs!==null&&typeof inputRevision==='number'&&inputRevision!==seenInputs;
  if(typeof inputRevision==='number')seenInputs=inputRevision;
  if(Array.isArray(changes)?changes.some(c=>DRIFT_SCOPES.includes(c.scope)):inputsMoved)owed=true;
}

/** The pulse is applied (app.ts onPulse): a drift noted since asks again. A pulse kept for later (a popover open) asks
 *  nothing until then, so a filter change under it reads nothing (maintenance). */
export function applyDriftInputs(){if(owed){owed=false;epoch++;}}

/** The one place that decides when the page asks for a draft's drift count: once per draft and baseline, and again
 *  only after department inputs moved (noteDriftInputs). A redraw or the draft's own save keeps the same key. */
export function baselineDriftKey(w:{id:string;baseline?:{snapshot_hash?:unknown}|null}){
  return `${w.id}:${String(w.baseline?.snapshot_hash||'')}:${epoch}`;
}
