/** The one client mapping of maintenance's "what changed while it ran" for a stopped rota check (B7 failed). Every screen that
 *  names why a check or run stopped reads it from here, so when the contract settles only this file moves.
 *  Shapes (mockups/B/README.md › B7 and build/contracts/maintenance, both still provisional): a refusal's details or a failed
 *  job carry `cause:{kind:'wish'|'leave'|'edit',person,date}`, or maintenance's (a check's 409 STALE_INPUTS) `changed_facts:
 *  [{kind,source_kind?,person_name?,actor:{display_name}|null,first_day|starts_on|start}]`; jobs carry `started_at`.
 *  maintenance never invents an actor: without one the person the fact is about is named. No imports beyond loading.ts. */
import {clock} from './loading.js';

export type CauseKind='wish'|'leave'|'edit';
/** `actor`: who made the change (never invented: '' when the server names none); `about`: the person it concerns. */
export interface StopCause{kind:CauseKind;actor:string;about:string;date:string}

const KINDS:Record<string,CauseKind>={wish:'wish',wishes:'wish',leave:'leave',availability:'leave',absence:'leave',request:'leave'};
const text=(v:unknown)=>typeof v==='string'?v:'';
const name=(v:unknown)=>text((v as {display_name?:unknown}|null)?.display_name)||text(v);

/** The cause in `details` (an ApiError's details or a job record), or null when the server named none. */
export function causeOf(details:unknown):StopCause|null{
  const d=(details||{}) as Record<string,unknown>,c=d.cause as Record<string,unknown>|undefined;
  if(c&&typeof c==='object')return {kind:KINDS[text(c.kind)]||'edit',actor:name(c.person),about:'',date:text(c.date)};
  const f=Array.isArray(d.changed_facts)?d.changed_facts[0] as Record<string,unknown>|undefined:undefined;
  if(f&&typeof f==='object')return {kind:KINDS[text(f.source_kind)]||KINDS[text(f.kind)]||'edit',actor:name(f.actor),about:text(f.person_name)||name(f.person),
    date:text(f.first_day)||text(f.starts_on)||text(f.start)||text(f.date)};
  return null;
}

const day=(iso:string)=>/^\d{4}-\d{2}-\d{2}/.test(iso)?new Intl.DateTimeFormat('en-GB',{weekday:'short',day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(iso.slice(0,10)+'T12:00:00Z')):'';
const DID:Record<CauseKind,[string,string,string]>={wish:['added a wish','A wish','was added'],leave:['recorded leave','Leave','was recorded'],edit:['changed the rota','A change','was made']};

/** "Dr. Felix Parker added a wish for Thu 17 Dec while it ran."; without a named actor, "A wish for Sam Lee on
 *  Fri 4 Dec was added while it ran." */
export function causeWords(c:StopCause,when='while it ran'){
  const [did,what,passive]=DID[c.kind],d=day(c.date),about=c.about&&c.about!==c.actor?c.about:'';
  const target=about?` for ${about}${d?' on '+d:''}`:d?` for ${d}`:'';
  return c.actor?`${c.actor} ${did}${target} ${when}.`:`${what}${target} ${passive} ${when}.`;
}

/** The failed-check line: its lead ("The check stopped after 2:33.") and the cause, when known. */
export function stoppedWords(ms:number,details?:unknown){
  const c=causeOf(details);
  return {lead:`The check stopped after ${clock(ms)}.`,cause:c?causeWords(c):''};
}
