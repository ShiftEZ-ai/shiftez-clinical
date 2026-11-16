/** The one client mapping of the published-rota change contract (maintenance, build/contracts/maintenance: replace / swap / mark
 *  absent / move / add / cancel on a published rota, its one hidden change list (D12), urgent routing (D5) and staff
 *  "Can't make it" (D13)). Every screen imports from here, so when the contract changes only this file moves. */
import {command,get,request} from './api.js';
import {fillOptions} from './candidates.js';
import type {Row} from './types.js';
import {ABSENCE_CATEGORIES,ABSENCE_CATEGORY_LABELS,type AbsenceReportInput} from './generated.js';

export type ChangeKind='replace'|'swap'|'absence'|'cancel'|'move'|'add';
export type ChangeStatus='draft'|'submitted'|'approved'|'published'|'withdrawn';
export type Urgency='urgent'|'routine';
/** One change of a publication's list. The changed sessions (and the changed people's other duties) are what is checked,
 *  never the whole published rota (D11). */
export interface PublishedChange{id:string;kind:ChangeKind;shift_id:string;assignment_id?:string|null;person_id?:string|null;
  target_shift_id?:string|null;target_assignment_id?:string|null;absence_reason?:string;reason?:string;status:ChangeStatus;workspace_id:string;created_at:string;
  /** The sessions it changed, with who was on them before and after (still readable after an urgent subset publishes). */
  sessions?:{id:string;start:string;end:string;location_id:string;service_ids:string[];before_person_ids:string[];after_person_ids:string[];cancelled:boolean}[];
  /** The change's own Must fix: mandatory findings of the scoped check on its sessions or people (the finding shape). */
  must_fix?:Row[];
  /** How a submitted change was sent and when (maintenance); null once withdrawn, absent on older rows. */
  urgency?:Urgency|null;submitted_at?:string|null;
  /** SW-06 (maintenance): a sent change an approver returned — who, when and why. Still `submitted` until the coordinator takes
   *  the set back (withdraw), which makes it editable and undoable again. */
  returned?:{by_name?:string|null;at:string;reason:string}|null}
/** Whether an urgent approval route is configured (D5): Urgent is offered only when `active`. */
export interface UrgentRoute{active:boolean;approver_count:number}
/** The publication's hidden change list: its draft workspace (null before the first change) and its changes. */
export interface ChangeSet{workspace:Row|null;changes:PublishedChange[];urgent_route:UrgentRoute}
export interface ChangeCommand{kind:ChangeKind;shift_id:string;assignment_id?:string;person_id?:string;target_shift_id?:string;
  target_assignment_id?:string;absence_reason?:string;reason:string}
/** A change's reply: the hidden workspace (its revision binds the next change), the scoped check (summary) and the
 *  findings it added. */
export interface ChangeReply{workspace:Row;change:PublishedChange;changes:PublishedChange[];validation?:Row;issues?:{added:Row[]};urgent_route:UrgentRoute}

const at=(publication:string)=>`/api/publications/${encodeURIComponent(publication)}`;
/** Kept out of shared helpers: the hidden workspace's revision is the change list's (expected_revision). */
const revisionOf=(set:{workspace?:Row|null})=>set.workspace?.revision as number|undefined;

export const pendingChanges=(publication:string)=>get<ChangeSet>(at(publication)+'/pending-changes');
/** One change; the server creates or reuses the publication's hidden draft (D12). The first change needs no revision. */
export const makeChange=(publication:string,change:ChangeCommand,set?:{workspace?:Row|null})=>
  command<ChangeReply>('publication.change',{...change},publication,set?revisionOf(set):undefined);
export const undoChange=(publication:string,id:string,set:{workspace?:Row|null},reason='Undo')=>
  command<{workspace:Row;changes:PublishedChange[]}>('publication.change.undo',{change_id:id,reason},publication,revisionOf(set));
/** Sends changes for approval: omitted ids send every unsent change; a subset must be urgent (D12). */
export const submitChanges=(publication:string,o:{ids?:string[];urgency:Urgency;reason:string},set:{workspace?:Row|null})=>
  command<{workspace:Row;changes:PublishedChange[]}>('publication.changes.submit',{...(o.ids?{change_ids:o.ids}:{}),urgency:o.urgency,reason:o.reason},publication,revisionOf(set));
/** Takes the set under review back into the list (unsent again); one set per publication is in review at a time.
 *  Its reply is the pending list (the hidden workspace again). */
export const withdrawChanges=(publication:string,set:{workspace?:Row|null},reason='Withdrawn by the coordinator')=>
  command<ChangeSet>('publication.changes.withdraw',{reason},publication,revisionOf(set));
/** A session's slot (type, place, start, end, services): how the server pairs an issued session with its copy. */
const slot=(r:Row)=>[r.assignment_type_id,r.location_id,Date.parse(r.start),Date.parse(r.end),[...(r.service_ids||[])].sort().join(',')].join('|');
/** Who can take over (or join) a published session: `publication.changes.prepare` makes (or reuses) the hidden draft and
 *  returns its sessions; the issued session and person are found in it by slot (as the server pairs them), then the
 *  ordinary fill-options answers for that copy (candidates carry `override_allowed`). The change itself still names the
 *  issued ids. Returns the candidates and the prepared set (its revision binds the change). */
export async function replaceOptions(publication:string,issued:Row,assignment:Row|undefined,set:{workspace?:Row|null}){
  const ready=await command<ChangeSet&{shifts:Row[]}>('publication.changes.prepare',{reason:'Prepare a change to the published rota'},publication,revisionOf(set));
  const copies=ready.shifts.filter(r=>slot(r)===slot(issued));
  if(copies.length!==1)throw new Error('This session changed since it was published. Reload the schedule.');
  const copy=copies[0],same=(a:string,b:string)=>Date.parse(a)===Date.parse(b);
  const who=assignment?(copy.participants as Row[]).find(p=>p.person_id===assignment.person_id&&same(p.start||copy.start,assignment.start||issued.start)&&same(p.end||copy.end,assignment.end||issued.end)):undefined;
  if(assignment&&!who)throw new Error('This person is no longer on the session. Reload the schedule.');
  const result=await fillOptions({workspace_id:ready.workspace!.id,shift_id:copy.id,
    expected_revision:ready.workspace!.revision,...(who?{replace_assignment_id:who.id}:{})}) as Row;
  return {result,set:ready as ChangeSet};
}
/** Staff "Can't make it" on their own published duty (D13): a request the coordinators see. Its `category` is what
 *  deciders may read (department `leave_reason_display`); `reason` stays private. */
export const reportAbsence=(value:Pick<AbsenceReportInput,'publication_id'|'assignment_id'|'reason'|'category'|'note'|'document_reference'>)=>
  command<{record:Row;warnings:Row[];message:string}>('request.create',{kind:'absence_report',...value});
export type AbsenceCategory=typeof ABSENCE_CATEGORIES[number];
/** A category as the contract words it (ABSENCE_CATEGORY_LABELS). */
export const absenceCategoryWord=(c:AbsenceCategory)=>ABSENCE_CATEGORY_LABELS[c];
/** An absence's reason and note as the server disclosed them to this reader, one line: a contract category (any case)
 *  reads as its word, any other text as sent; what the server withheld leaves no separator. */
export const absenceWords=(reason?:string|null,note?:string|null)=>{
  const said=(reason||'').trim(),category=ABSENCE_CATEGORIES.find(c=>c===said.toLowerCase());
  return [category?absenceCategoryWord(category):said,(note||'').trim()].filter(Boolean).join(' · ');};
export const absenceReportSettings=()=>get<{document_required:boolean;revision:number}>('/api/absence-report-settings');
