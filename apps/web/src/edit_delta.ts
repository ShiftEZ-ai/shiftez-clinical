import type {Row,WorkspaceData} from './types.js';
import type {LiveChange} from './generated.js';

/** Another person's edit carried on a workspace pulse change (PERF remote-watcher 1f08c8059): the same compact fields
 *  as the edit's own reply, so it is applied through the same path (applyEditDelta via app.ts patchDraft, which needs the
 *  held draft at the delta's base revision). Without its scoped findings and coverage it is partial: null, read whole. */
export function remoteEdit(c:LiveChange):Row|null{
  const x=c as unknown as Row,d=x.delta as EditDelta|undefined;
  return c.scope==='workspace'&&!!c.id&&d&&x.workspace&&x.health&&Array.isArray(d.findings?.items)&&Array.isArray(d.coverage?.items)?x:null;
}

/** A pulse's changes with this page's own rota check taken out (PERF slice c: the worker names the check's job on its
 *  workspace change, and settle applies that job's delta). `own` says whether a change is that check landing. `skip`:
 *  the check was all the pulse said and nobody's activity moved, so nothing is read; otherwise the remaining changes
 *  (someone's edit to the same rota among them) are handled as usual. */
export function withoutOwnCheck(changes:LiveChange[]|null,own:(c:LiveChange)=>boolean,moved:boolean):{changes:LiveChange[]|null;skip:boolean}{
  if(!changes)return {changes,skip:false};
  const rest=changes.filter(c=>!own(c));
  return {changes:rest,skip:rest.length<changes.length&&!rest.length&&!moved};
}

/** A draft edit's delta (edit, Undo and Redo replies: build/handoff/EDIT-DELTA.md): what changed from `base_revision`
 * to `revision`. Findings and coverage are replaced inside their scope, never merged by guess. */
export interface EditDelta {
  base_revision:number;revision:number;stale?:boolean;
  shifts?:Row[];removed_shift_ids?:string[];
  assignments?:Row[];removed_assignment_ids?:string[];
  findings?:{shift_ids?:string[];person_ids?:string[];items:Row[]};
  coverage?:{shift_ids?:string[];person_ids?:string[];items:Row[]};
  current_fingerprint?:string;current_input_revision?:number;current_availability_revision?:number;
}

export interface CompleteCheckDelta {
  kind:'complete_check';workspace_id:string;revision:number;stale?:boolean;partial?:boolean;
  current_fingerprint?:string;validation:Row;
}

/** Applies a terminal complete-check envelope to the held full rota. A partial,
 * stale or missing envelope must use the authoritative workspace read. */
export function applyCheckDelta(held:WorkspaceData|undefined,reply:Row|null|undefined):WorkspaceData|null{
  const d=reply?.delta as CompleteCheckDelta|undefined;
  if(!held||held.partial||!d||d.kind!=='complete_check'||d.stale||d.partial||!d.validation
    ||String(d.workspace_id)!==String(held.workspace.id)||Number(d.revision)!==Number(held.workspace.revision)
    ||!Array.isArray(d.validation.findings)||!Array.isArray(d.validation.coverage))return null;
  return {...held,workspace:{...held.workspace,validation:d.validation},
    current_fingerprint:d.current_fingerprint||d.validation.fingerprint||held.current_fingerprint};
}

/** The draft as the server now has it, from the copy held and the reply's delta; null when it cannot be applied exactly
 *  (no delta, stale, a different revision, only a window held, no current fingerprint): the caller reads the rota whole. */
export function applyEditDelta(held:WorkspaceData|undefined,reply:Row|null|undefined):WorkspaceData|null{
  const d=reply?.delta as EditDelta|undefined,w=reply?.workspace as Row|undefined;
  // An amendment's change list (change_diff) is the server's to recompute: read it whole.
  if(!held||held.partial||held.workspace.base_publication_id||!d||d.stale||!w||!d.current_fingerprint)return null;
  if(Number(held.workspace.revision)!==Number(d.base_revision)||Number(w.revision)!==Number(d.revision))return null;
  const gone=new Set(d.removed_shift_ids||[]),byId=new Map<string,Row>();
  for(const s of held.shifts)if(!gone.has(String(s.id)))byId.set(String(s.id),s);
  for(const s of d.shifts||[])byId.set(String(s.id),s);
  // Assignments on their own: each lands in its session (by shift_id); a session not held means the copy is incomplete.
  const leaving=new Set(d.removed_assignment_ids||[]);
  const joining=new Map<string,Row[]>();for(const a of d.assignments||[]){const k=String(a.shift_id);joining.set(k,[...(joining.get(k)||[]),a]);}
  for(const k of joining.keys())if(!byId.has(k))return null;
  // A session sent whole is taken as it is: assignment patches for it are not applied on top (EDIT-DELTA.md).
  const whole=new Set((d.shifts||[]).map(s=>String(s.id)));
  if(leaving.size||joining.size)for(const [id,s] of byId){if(whole.has(id))continue;
    const add=joining.get(id),drop=(s.participants||[]).some((p:Row)=>leaving.has(String(p.id)));if(!add&&!drop)continue;
    const ids=new Set((add||[]).map(a=>String(a.id)));
    byId.set(id,{...s,participants:[...(s.participants||[]).filter((p:Row)=>!leaving.has(String(p.id))&&!ids.has(String(p.id))),...(add||[])]});
  }
  // The server's order (snapshots.workspace_shifts): start, then id.
  const shifts=[...byId.values()].sort((a,b)=>String(a.start).localeCompare(String(b.start))||String(a.id).localeCompare(String(b.id)));
  const old=held.workspace.validation||{};
  const inScope=(scope:{shift_ids?:string[];person_ids?:string[]}|undefined)=>{const s=new Set(scope?.shift_ids||[]),p=new Set(scope?.person_ids||[]);
    return (r:Row)=>s.has(String(r.shift_id))||!!r.person_id&&p.has(String(r.person_id));};
  const replaced=(held:Row[]|undefined,part:{shift_ids?:string[];person_ids?:string[];items:Row[]}|undefined)=>{
    if(!part)return held;const hit=inScope(part);return [...(held||[]).filter(r=>!hit(r)),...part.items];};
  const findings=replaced(old.findings,d.findings),coverage=replaced(old.coverage,d.coverage);
  // The reply's workspace carries the check as a summary (counts, fingerprint); the held findings and coverage, patched, stay.
  // (serialization.compact_reply puts it on the workspace, or beside it as `validation`).
  const summary=(w.validation||reply!.validation||{}) as Row;
  const validation={...old,...summary,findings,coverage};
  return {...held,workspace:{...held.workspace,...w,validation},shifts,current_fingerprint:d.current_fingerprint,
    current_input_revision:d.current_input_revision??held.current_input_revision,current_availability_revision:d.current_availability_revision??held.current_availability_revision};
}
