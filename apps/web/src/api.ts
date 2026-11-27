import type {WorkspaceData} from './types.js';
import type {Command,IdentifierResponse,OIDCContinuation,OIDCDepartmentChoice,OIDCDepartmentChoices,OIDCDepartmentSession,LiveScope,LiveChange,CatalogueUpdating,WorkspaceDetailView,WorkspaceUndoInput} from './generated.js';
import {WEB_KEPT_READS,UNAVAILABLE_OUTCOME,GENERATION_TIME_FIELDS,GENERATION_STOP_FIELDS,WISH_PRIORITY_FIELDS} from './generated.js';
import {plainFindings,refusalText} from './findings.js';
import {track,setRetryAfresh,readTimeout,timedOutWords} from './loading.js';
import {recordError,recordRequest,clearFeedbackContext} from './feedback_context.js';

export class ApiError extends Error {
  constructor(public code:string,message:string,public status:number,public details?:unknown){super(message);}
}
/** The signed-in account changed while a request was in flight: its result is discarded silently. */
/** The result no longer applies: the account changed, or the page that asked was left (`leavePage`). Callers ignore it. */
/** An error that is not the person's to fix, so it offers "Report this problem": a server fault (5xx, an unreadable
 *  answer), a lost connection (not being offline, nor a result discarded on purpose), or a programming bug. Validation,
 *  refusals (4xx) and the messages forms throw for the person are not. */
export const unexpectedError=(e:unknown)=>e instanceof ApiError?e.status>=500&&e.code!=='RULE_CATALOGUE_BUSY'||e.code==='UNEXPECTED_RESPONSE'
  ||e.status===0&&!['NETWORK_OFFLINE','PAGE_LEFT','SESSION_CHANGED'].includes(e.code)
  :e instanceof TypeError||e instanceof ReferenceError||e instanceof RangeError||e instanceof SyntaxError;
export const sessionChanged=(e:unknown)=>e instanceof ApiError&&(e.code==='SESSION_CHANGED'||e.code==='PAGE_LEFT');
export const SESSION_CHANGED_MESSAGE='Your signed-in account changed; the previous action was discarded.';
let sessionGeneration=0,readsOk=0;
/** How many reads have come back from the server so far (the live pulse aside). */
export const readsDone=()=>readsOk;
let signingOut=false;
export const signingOutNow=()=>signingOut;
/** Changes whenever the signed-in account may have changed: caches keyed by it never cross accounts. */
export const sessionKey=()=>sessionGeneration;
function requireSession(generation:number){if(generation!==sessionGeneration)throw new ApiError('SESSION_CHANGED',SESSION_CHANGED_MESSAGE,0);}
function csrf():string {return decodeURIComponent(document.cookie.split('; ').find(v=>v.startsWith('rota_csrf='))?.split('=').slice(1).join('=') || '');}
/** D-1: the server as this tab's requests find it (app.ts footer): 'down' after no answer or a gateway error (502–504,
 *  a restart or overload), else 'ok'. Event `server-health` on change. A slow answer is not an outage (maintenance): a read
 *  that waits says so on its page (loading.ts "Still loading · 0:18"), a save in the save state (savingWords). */
let serverDown=false,health='ok';
// The request path also runs without a page (Node checks of api.ts): the event only where a window is.
function sayHealth(){const now=serverDown?'down':'ok';if(now!==health){health=now;if(typeof window!=='undefined')window.dispatchEvent(new CustomEvent('server-health',{detail:now}));}}
export const serverHealth=()=>health;
type RequestOptions=RequestInit & {question?:true};
const readMethod=(method?:string)=>!method||method==='GET'||method==='HEAD';
export function request<T>(path:string, options:RequestOptions = {}, renewed=false):Promise<T> {
  // DL-045: every read ends. One with no answer by READ_TIMEOUT is aborted (the server stops sending) and rejects
  // READ_TIMEOUT, which readFailed shows as "Didn’t load in 1:00." with Retry. Slow is not down: no health change.
  // A 202 "updating" is an answer: maintenance's own wait (projectionReady) pauses the clock; each re-read gets a full limit.
  const stop=new AbortController(),own=options.signal,limit=readTimeout(path);let late=false,timer:ReturnType<typeof setTimeout>|undefined;
  const arm=(on:boolean)=>{clearTimeout(timer);timer=on?setTimeout(()=>{late=true;stop.abort();},limit):undefined;};
  const health=(down:boolean|null,waiting?:boolean)=>{if(down!==null){serverDown=down;sayHealth();}if(waiting!==undefined)arm(!waiting);};
  if(!options.question&&!readMethod(options.method))return sent<T>(path,options,renewed,health);
  arm(true);
  if(own){if(own.aborted)stop.abort();else own.addEventListener('abort',()=>stop.abort(),{once:true});}
  return sent<T>(path,{...options,signal:stop.signal},renewed,health)
    .catch(e=>{if(late&&!own?.aborted&&e instanceof ApiError&&e.code==='PAGE_LEFT')throw new ApiError('READ_TIMEOUT',timedOutWords(limit),0);throw e;})
    .finally(()=>clearTimeout(timer));
}
/** How long a read waits for a rebuilding projection (projectionReady): the server's Retry-After, with bounded backoff. */
const catalogueRetryDelay=(header:string|null,attempt:number)=>{
  const seconds=header===null?NaN:Number(header);
  const server=Number.isFinite(seconds)&&seconds>=0?seconds*1000:0;
  return Math.max(server,Math.min(1000*2**attempt,30_000));
};
export const RULES_UPDATING='Rules are updating… Try again in a moment.';
/** maintenance: a read whose projection is being rebuilt stays pending (the page keeps its loading state) until it is ready.
 *  No older answer stands in meanwhile: this read's kept reply, every kept reply as current (keptReads) and every kept
 *  answer a catalogue change can alter (keptRead) are dropped. With a job: its live jobs change, else its lean status
 *  (awaitJob). Without one (a race): the live catalogue change, or Retry-After with bounded backoff (≥ 2 s, ≤ 30 s).
 *  Concurrent readers share this wait through the read they share (inflight, keptRead). */
async function projectionReady(path:string,busy:CatalogueUpdating,header:string|null,attempt:number,signal?:AbortSignal|null){
  const old=kept.get(path);if(old){keptChars-=old.size;kept.delete(path);}
  keptEpoch++;forgetReads(new Set(['catalogue']),null,true);
  // A page redrawing behind its old content swaps it for its skeleton (app.ts renderPage): old rules are not shown.
  window.dispatchEvent(new Event('projection-updating'));
  if(busy.job_id){
    try{await awaitJob({id:busy.job_id,status:'queued'},{every:2000,stop:()=>!!signal?.aborted});}
    catch(e){if(e instanceof ApiError&&e.code==='SESSION_CHANGED')throw e;/* the status is unreadable: the read again says */}
    return;}
  await new Promise<void>(done=>{const end=()=>{window.clearTimeout(t);window.removeEventListener('catalogue-changed',end);signal?.removeEventListener('abort',end);done();};
    const t=window.setTimeout(end,catalogueRetryDelay(header??String(busy.retry_after_seconds),attempt));
    window.addEventListener('catalogue-changed',end);signal?.addEventListener('abort',end);});
}
async function sent<T>(path:string, options:RequestOptions, renewed:boolean, answered:(down:boolean|null,waiting?:boolean)=>void, catalogueAttempt=0):Promise<T> {
  const {question,...http}=options;
  const generation=sessionGeneration,reading=question||readMethod(http.method);
  const headers=new Headers(options.headers);headers.set('Accept','application/json');
  // A JSON command or form; a FormData body sets its own multipart boundary and a File its own type (branding images).
  if(typeof options.body==='string'&&!headers.has('Content-Type'))headers.set('Content-Type','application/json');
  if(!readMethod(http.method)){headers.set('X-CSRF-Token',csrf());if(!question)inflight.clear();}
  // Big reads (catalogues, a rota's detail) revalidate with their ETag: unchanged data is not sent again.
  // Kept in memory only, cleared on sign-out; nothing is written to the browser's disk cache.
  const cached=!options.method&&kept.get(path),epoch=keptEpoch;if(cached){if(cached.tag)headers.set('If-None-Match',cached.tag);kept.delete(path);kept.set(path,cached);}
  let response:Response;
  const left=()=>{if(options.signal?.aborted)throw new ApiError('PAGE_LEFT','The page that asked for this was left.',0);};
  try {response=await fetch(path,{...http,headers,credentials:'same-origin',cache:'no-store'});}
  catch {
    if(!options.signal?.aborted)answered(true);
    requireSession(generation);left();
    // Failed requests go into a feedback report's recent errors (method, path and outcome; never the query or body).
    recordError(`${options.method||'GET'} ${path.split('?')[0]} · no response`,'network');
    const lost=(code:string,message:string)=>new ApiError(code,message,0);
    if(path==='/api/policies/check')throw lost('CHECK_NOT_RECEIVED','Rule check unavailable. No changes were saved. Try again when connected.');
    if(path==='/api/planner/preview'||path==='/api/catalogue/preview'||path==='/api/planner/staffing-suggestions')throw lost('PREVIEW_NOT_RECEIVED','The preview was not received. Previews never save changes; retry to recalculate.');
    if(reading)throw lost('READ_UNAVAILABLE','Connection lost. The server could not be reached.');
    // A write with no response may have committed: navigator.onLine read after the failure cannot prove it was not sent.
    throw lost('NETWORK_UNCERTAIN','Connection lost. A save may have committed. Retry the same operation; do not re-enter it as a new action.');
  }
  requireSession(generation);recordRequest(response.headers.get('X-Correlation-ID'));
  // maintenance: a projection being rebuilt (the rule catalogue after an approval) answers 202 {state:'updating'}: never data.
  if(response.status===202){const text=await response.text().catch(()=>'');let busy:CatalogueUpdating|null=null;
    try{const p=JSON.parse(text);if(p?.state==='updating')busy=p;}catch{/* not the envelope: read below as any 202 */}
    // Only ordinary reads wait for a rebuilt projection. Decisions and questions return control to their caller.
    if(busy&&(!reading||question)){answered(false);requireSession(generation);throw new ApiError('RULES_UPDATING',RULES_UPDATING,202,busy);}
    if(busy){answered(false,true);requireSession(generation);left();await projectionReady(path,busy,response.headers.get('Retry-After'),catalogueAttempt,options.signal);
      requireSession(generation);left();answered(null,false);return sent<T>(path,options,renewed,answered,catalogueAttempt+1);}
    response=new Response(text,{status:202,headers:response.headers});}
  // A page read that came back (fresh or revalidated): the page then shows current data (app.ts clears its stale banner).
  const read=(!options.method||options.method==='GET')&&response.ok||response.status===304;if(read&&path!=='/api/pulse')readsOk++;
  if(response.status===304&&cached){cached.at=Date.now();cached.epoch=epoch;return cached.payload as T;}
  const text=await response.text().catch(()=>'');let payload:any,parsed=true;
  try{payload=JSON.parse(text);}catch{parsed=false;payload={error:{code:'UNEXPECTED_RESPONSE',message:'The service returned an unexpected response.'}};}
  requireSession(generation);left();
  // A busy rule catalogue (503 RULE_CATALOGUE_BUSY) is a failed read with Retry, never data; it says nothing of the
  // server's health.
  const catalogueBusy=response.status===503&&payload?.error?.code==='RULE_CATALOGUE_BUSY';
  answered(catalogueBusy?null:[502,504].includes(response.status)||response.status===503);
  // maintenance: a command answered 200 with an unreadable body (a proxy's page) did not say what happened: unknown, not success.
  const unreadable=!parsed&&response.ok&&path==='/api/commands';
  if(!response.ok||unreadable) {
    // A 4xx without a body is a form problem; a 5xx is the server's, not the person's.
    const error=payload.error || (response.status>=500?{code:'SERVER_ERROR',message:'The service had a problem. Try again in a moment.'}:{code:'SCHEMA_INVALID',message:'Review the form values.'});
    recordError(`${options.method||'GET'} ${path.split('?')[0]} · ${response.status} ${error.code}`,'network');
    // A model-level check (e.g. dates in the wrong order) has no field path: its own words still say what to correct.
    // Our own checks (value_error, assertion_error) are plain sentences: said alone, without the field path or the library's
    // prefix; when every detail is one, nothing is "highlighted", so they are the whole message. ApiError.details keeps all.
    const list:{field?:string;code?:string;message?:string}[]=Array.isArray(error.details)?error.details:[];
    const own=(v:{code?:string})=>v.code==='value_error'||v.code==='assertion_error';
    const details=list.map(v=>own(v)?plainWords(v.message):detailWords(v)).filter(Boolean).join('; ');
    const said=list.length&&list.every(own)&&details?details:`${error.message}${details?' '+details:''}`;
    // ME-16: a write refused for its stale CSRF token (a tab left open over a new sign-in) renews the session's tokens and
    // is sent once more with the same body (its idempotency key too). Still refused: sign in again, the page is kept.
    if(!question&&response.status===403&&error.code==='CSRF_FAILED'&&options.method&&!['GET','HEAD'].includes(options.method)&&!path.startsWith('/auth/')&&!signingOut){
      if(!renewed&&await refreshSession()){requireSession(generation);return request<T>(path,options,true);}
      window.dispatchEvent(new CustomEvent('session-expired',{detail:{code:error.code}}));
      throw new ApiError(error.code,'Your sign-in needs renewing. Sign in again to save; what you entered stays on the page.',response.status,error.details);}
    if(!signingOut&&(response.status===401 || (response.status===403 && ['/api/me','/api/pulse'].includes(path))))window.dispatchEvent(new CustomEvent('session-expired',{detail:{code:error.code}}));
    throw new ApiError(error.code,(reading&&READ_PLAIN[error.code])||PLAIN[error.code]||`${refusalText(said,error.details)}${error.code==='INTERNAL_ERROR'&&payload.correlation_id?` Reference: ${payload.correlation_id}`:''}`,response.status,error.details);
  }
  const tag=response.headers.get('ETag')||undefined;
  // A reply without an ETag that reads the same as the one kept is the kept one (as a 304 is): a page shown from it
  // needs no redraw (keptReads).
  const digest=tag?undefined:textDigest(text);
  if(!options.method&&cached&&!tag&&cached.digest===digest){cached.at=Date.now();cached.epoch=epoch;return cached.payload as T;}
  const result=plainFindings(payload);
  if(!options.method&&parsed&&path!=='/api/pulse')keep(path,{tag,digest,payload:result,size:text.length,at:Date.now(),epoch});
  noteRevision(result);
  return result as T;
}
/* Replies kept for this tab (FE-PERF). Every read's last reply, in memory only (never the disk cache), cleared on
 * sign-out: a big one revalidates with its ETag, and a page opened again shows its kept replies at once (keptReads). */
type Kept={tag?:string;digest?:number;payload:unknown;size:number;at:number;epoch:number};
const kept=new Map<string,Kept>();
// Bounded per tab by the contract's WEB_KEPT_READS: the least recently read go first once the kept replies pass
// max_chars of JSON text or max_paths; a single reply larger than the whole budget is not kept at all.
let keptChars=0;
function keep(path:string,entry:Kept){
  const old=kept.get(path);if(old){keptChars-=old.size;kept.delete(path);}
  if(entry.size>WEB_KEPT_READS.max_chars)return;
  kept.set(path,entry);keptChars+=entry.size;
  for(const [p,k] of kept){if(keptChars<=WEB_KEPT_READS.max_chars&&kept.size<=WEB_KEPT_READS.max_paths)break;kept.delete(p);keptChars-=k.size;}
}
/** FNV-1a over the reply's text: tells an unchanged reply without an ETag from a changed one. */
function textDigest(text:string){let h=0x811c9dc5;for(let i=0;i<text.length;i++){h^=text.charCodeAt(i);h=Math.imul(h,0x01000193);}return h>>>0;}
/** Bumped by every save (command) and every live change (forgetReads): a reply kept from before it is never shown as
 *  current, only read again (with its ETag: a conditional read). */
let keptEpoch=0;
type KeptBatch={changed:boolean;error:unknown;checks:Promise<unknown>[];paths:Set<string>};
let keptBatch:KeptBatch|null=null;
/** Stale-while-revalidate for opening a page (navigation, Back): while `open` runs, each page read this tab has a reply
 *  for (since the last save) answers at once with it, and the server is asked again in the background, quietly (no
 *  progress bar). Once those answers are in, `after` runs if any reply changed (redraw quietly) or a check failed
 *  (`error`: say the page may be out of date). Reads with no kept reply, and every read outside `open`, go to the
 *  server as before. */
export function keptReads<T>(open:()=>Promise<T>,after:(error?:unknown)=>void):Promise<T>{
  const batch:KeptBatch={changed:false,error:undefined,checks:[],paths:new Set()};keptBatch=batch;
  return open().finally(()=>{if(keptBatch===batch)keptBatch=null;
    if(batch.checks.length)void Promise.allSettled(batch.checks).then(()=>{if(batch.changed||batch.error)after(batch.error);});});
}
function keptReply<T>(path:string):Promise<T>|null{
  const batch=keptBatch,k=batch&&kept.get(path);
  if(!batch||!k||k.epoch!==keptEpoch)return null;
  if(Date.now()-k.at>=WEB_KEPT_READS.fresh_ms&&!batch.paths.has(path)){batch.paths.add(path);
    // An app read: leaving the page does not abort it, so the kept reply is brought up to date either way.
    batch.checks.push(read<T>(path,'app').then(r=>{if(r!==k.payload)batch.changed=true;},e=>{if(!sessionChanged(e))batch.error=e;}));}
  return Promise.resolve(k.payload as T);
}
/** The newest revision of each rota this page has read or changed (a rota's detail, a command's reply). A command the
 *  server offers without a revision (a double booking's cancel, double_booking.ts) is sent with it, so a change made
 *  elsewhere meanwhile is refused, not overwritten. */
const revisions=new Map<string,number>(),contents=new Map<string,number>();
/** With each rota's revision, its content revision: what its sessions are. A check moves the revision, never the content. */
function noteRevision(r:unknown){const w=(r as {workspace?:{id?:unknown;revision?:unknown;content_revision?:unknown}}|null)?.workspace;
  if(typeof w?.id==='string'&&typeof w.revision==='number'&&w.revision>=(revisions.get(w.id)??0)){revisions.set(w.id,w.revision);
    if(typeof w.content_revision==='number')contents.set(w.id,w.content_revision);}}
export const knownRevision=(workspaceId:unknown)=>typeof workspaceId==='string'?revisions.get(workspaceId):undefined;
// Plain words for refusals about rotas overlapping each other; their details carry the rotas and the ways out
// (GET /api/schedule/overlaps answers the same before creating or publishing).
/** C3-F5: a read that failed changed nothing: its words never suggest a save may be lost (the server's
 *  DATABASE_UNAVAILABLE is worded for a write; a gateway's HTML page parses as UNEXPECTED_RESPONSE). Callers say what
 *  could not load and offer Retry. */
const READ_PLAIN:Record<string,string>={DATABASE_UNAVAILABLE:'The system is unavailable right now. Try again in a moment.',
  UNEXPECTED_RESPONSE:'The server is not answering properly right now. Try again in a moment.'};
const PLAIN:Record<string,string>={
  WITHDRAWAL_ALREADY_STARTED:'This record has started; end it instead.',
  RELATIONSHIP_NOT_WITHDRAWN:'This record is already present; reload its current revision.',
  ROTATION_NOT_REMOVED:'This rotation is already present; reload its current revision.',
  OVERLAPPING_PUBLICATION:'These days already have a published rota. Start this rota after it, replace it from a date, or change the published rota instead.',
  PUBLICATION_HEAD_CHANGED:'The published rota this one changes or replaces was updated meanwhile. Check the overlap again and choose how this rota fits.',
  // A quick edit (a table cell, a bulk change) has no place to list clashes: the form does, live.
  SERIES_OVERLAP_ACKNOWLEDGEMENT:'This would double-book someone. Open the recurring session (Edit) to see each clash, cancel one session of each, or save there with the acknowledgement.',
  AMENDMENT_SCOPE:'A change to a published rota keeps that rota\'s dates. To plan other days, start a new rota.',
};
/* maintenance: a command whose outcome is unknown (no answer, an unreadable answer, a gateway's 502–504, as in a rolling
 * deploy) may have committed. It is never "Not saved": the footer says "Checking whether it saved…" and the same command
 * is sent again with the SAME Idempotency-Key once the server answers again (the server replays its stored receipt, or
 * runs it once if it never arrived), with backoff, until a real answer. Still unknown after CONFIRM_WINDOW_MS: SAVE_UNCONFIRMED. */
// A 503 is a real answer only when the contract classes its code DEFINITE (refused before anything could commit:
// UNAVAILABLE_OUTCOME, the one list); every other 503 (COMMAND_WRITE_TIMEOUT included: a commit-phase failure can follow
// the save), uncoded or unlisted, is unknown and confirmed as above, never said "not saved".
const definiteRefusal=(code:string)=>(UNAVAILABLE_OUTCOME as Record<string,string>)[code]==='definite';
const unknownOutcome=(e:unknown)=>e instanceof ApiError&&(e.code==='NETWORK_UNCERTAIN'||e.code==='UNEXPECTED_RESPONSE'
  ||[502,504].includes(e.status)||e.status===503&&!definiteRefusal(e.code));
const CONFIRM_DELAYS=[1,2,4,8,15,30].map(s=>s*1000);  // then every 30 s
// The first try with no answer by COMMAND_TIMEOUT_MS is abandoned and its outcome is unknown (confirmed as above), so
// "Saving…" never hangs. A replay of a committed command answers at once with its receipt, so each replay gets only
// REPLAY_TIMEOUT_MS, and the whole confirmation ends CONFIRM_WINDOW_MS after the first try ended (an API restart in a
// rolling deploy fits in it).
// shortcut: 150 s is above the server's command_write_budget_ms (120 s, apps/api/settings.py), which is not exposed to the
// client; if that budget becomes configurable per deployment, serve it (e.g. on /api/me) and read it here.
export const COMMAND_TIMEOUT_MS=150_000,REPLAY_TIMEOUT_MS=20_000,CONFIRM_WINDOW_MS=180_000;
export const UNCONFIRMED_MESSAGE='Couldn’t confirm whether it saved. Check the rota before trying again.';
/** The unconfirmed save's words name the record only when it is one: a command on a rota says "the rota" (as Undo's
 *  STALE_UNDO does, always a rota), any other "it" (Design Lead, D-061's rule). */
export const unconfirmedWords=(kind:string)=>/^(shift|workspace|draft|issues|publication|series)\./.test(kind)?UNCONFIRMED_MESSAGE
  :'Couldn’t confirm whether it saved. Check it before trying again.';
let confirming=0;
/** The person's own saves whose outcome is being confirmed (the footer's "Checking whether it saved…"; D-4). */
export const confirmingNow=()=>confirming>0;
/** The words for a save on its way, wherever it is shown (the footer's save state, a toast whose action is saving). */
export const savingWords=()=>confirming>0?'Checking whether it saved…':'Saving…';
/** The words for a background read still on its way past SLOW_READ_MS (updatingNow): the data shown is about to change. */
export const UPDATING_WORDS='Updating…';
// The next try waits its delay, or less: the server answering another request again (server-health ok), or the device
// coming back online, sends it at once.
const untilReconnected=(ms:number)=>new Promise<void>(done=>{
  const go=(e?:Event)=>{if(e?.type==='server-health'&&(e as CustomEvent).detail!=='ok')return;clearTimeout(timer);
    window.removeEventListener('server-health',go);window.removeEventListener('online',go);done();};
  const timer=setTimeout(go,ms);window.addEventListener('server-health',go);window.addEventListener('online',go);});
/* One Idempotency-Key per command body until a real answer (success or a refusal) releases it, so a retry of the same
 * command is never a new one. Kept in this tab's sessionStorage too (cleared on sign-out), so a reload keeps it: a
 * retry after the reload is still the same command. Stored by the body's SHA-256, never the body (no rota data at rest). */
const PENDING_STORE='fmuc.pending-commands';
const pending=new Map<string,string>((()=>{try{const v=JSON.parse(globalThis.sessionStorage?.getItem(PENDING_STORE)||'[]');return Array.isArray(v)?v:[];}catch{return [];}})());
function storePending(){try{if(pending.size)sessionStorage.setItem(PENDING_STORE,JSON.stringify([...pending].slice(-50)));else sessionStorage.removeItem(PENDING_STORE);}catch{/* storage blocked: memory only */}}
/** The server's receipt for an operation key, when it committed (this account's, apps/api main.py command_status);
 *  null when none was committed or it cannot be read now. */
const committedReceipt=<T>(key:string)=>request<{status:string;result:T}>('/api/commands/'+encodeURIComponent(key),{cache:'no-store'})
  .then(r=>r?.status==='committed'?r:null,()=>null);
const bodyId=async(body:string)=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body)))].map(b=>b.toString(16).padStart(2,'0')).join('');
// Commands on their way: a live pulse that arrives meanwhile is most likely this page's own change (see onPulse).
let inFlight=0;
export const savingNow=()=>inFlight>0;
/** Commands that only read (a draft's baseline drift, checked as the Schedule draws): nothing is saved, so nothing held
 *  is dropped or read again (app.ts patchDraft) and the save state stays quiet (automatic). */
export const READ_ONLY_COMMANDS=new Set(['workspace.baseline.refresh.preview']);
/** D-4: commands that are not the person's saves (a rota check, synchronous or a validation job; a rule change's effect
 *  or trial check, a feasibility job; an auto-schedule run, a generation job, nothing saved until its proposal is applied
 *  with generation.apply, which is a save; a job's cancel, release or attach; a read marker; the draft baseline's
 *  read-only preview, asked on opening a rota): the footer's save state,
 *  savingWords and the top bar never speak for them, as they change nothing the person made. A job keeps its own
 *  progress line (maintenance). */
export const automatic=(kind:string,payload?:Record<string,unknown>)=>READ_ONLY_COMMANDS.has(kind)||['workspace.validate','department_content.seen','job.cancel','job.release','job.attach'].includes(kind)
  ||kind==='job.create'&&['validation','feasibility','generation'].includes(String(payload?.kind));
let ownInFlight=0;
/** The person's own saves on their way (the footer's "Saving…"). */
export const ownSavingNow=()=>ownInFlight>0;
/** Draft edits (each keeps the arrangement before it: `draftEdit` below) and bulk changes: the ones that change a draft's
 *  inputs, so an auto-schedule proposal made from them can no longer be applied. */
const DRAFT_EDITS=new Set(['shift.create','shift.update','shift.action','shift.copy','issues.resolve','draft.swap','workspace.target_override',
  'workspace.rule_adjust','workspace.resolve_overlap','series.expand','series.reconcile','import.apply','bulk.apply']);
// Asked before such an edit is sent (app.ts guardDraftEdits: a waiting proposal); false keeps the draft as it is.
let draftGuard:((kind:string,payload:Record<string,unknown>,target_id?:string)=>Promise<boolean>)|null=null;
export const guardDraftEdits=(fn:typeof draftGuard)=>{draftGuard=fn;};
/** A rota's decisions: each moves it along review (a reply carries its new state; app.ts patchDraft). */
export const ROTA_DECISIONS=new Set(['workspace.submit','workspace.approve','workspace.publish','workspace.reopen']);
/** One message per refusal (LEAD-UIUX, 8 Nov 2026): commands sent while a form saves (dom.ts formDialog) have their
 *  refusal said by that form's error line in the server's words, so the app's generic rota-conflict message stays
 *  quiet for them (`owned`). shortcut: a counter, so a command another part of the page sends during that save counts
 *  as the form's too; a per-command owner would be exact. */
/** A server check's sentence without the validation library's prefix ("Value error, …"): what a person reads. */
export const plainWords=(message:unknown)=>String(message||'').replace(/^(Value error|Assertion failed), /,'');
/** The contract's titles for settings fields (search_minutes → "Search time"): a refusal names a field by these. */
const FIELD_TITLES:Record<string,string>=Object.fromEntries([GENERATION_TIME_FIELDS,GENERATION_STOP_FIELDS,WISH_PRIORITY_FIELDS]
  .flatMap(m=>Object.entries(m).map(([k,v])=>[k,(v as {title:string}).title])));
/** One refusal detail in words (D-069): the field's own name — the form's label (`label`), else the contract's title —
 *  never its key or path, and the sentence through plainWords; a field with no name says the sentence alone. */
export function detailWords(v:{field?:unknown;message?:unknown},label?:(key:string)=>string|undefined):string{
  const key=String(v.field||'').split('.').pop()||'',name=key&&(label?.(key)||FIELD_TITLES[key])||'',said=plainWords(v.message);
  return name&&said?`${name}: ${said}`:said;
}
let owning=0;
export async function ownErrors<T>(run:()=>Promise<T>):Promise<T>{owning++;try{return await run();}finally{owning--;}}
export async function command<T=any>(kind:string,payload:Record<string,unknown>={},target_id?:string,expected_revision?:number,expected_fingerprint?:string):Promise<T> {
  // Declined: nothing is sent, and the empty message tells forms and reportError there is nothing to say (PAGE_LEFT).
  if(draftGuard&&DRAFT_EDITS.has(kind)&&!String(payload.reason||'').startsWith('Undo')&&!await draftGuard(kind,payload,target_id))throw new ApiError('PAGE_LEFT','',0);
  const value:Command={kind,payload,target_id,expected_revision,expected_fingerprint};
  const body=JSON.stringify(value),id=await bodyId(body),key=pending.get(id)||crypto.randomUUID(),generation=sessionGeneration;
  pending.set(id,key);storePending();const release=()=>{if(pending.get(id)===key){pending.delete(id);storePending();}};
  const own=!automatic(kind,payload);inFlight++;
  // The person's save may change what any page shows: no reply kept from before it is shown as current (keptReads).
  if(own)keptEpoch++;if(own)ownInFlight++;window.dispatchEvent(new CustomEvent('rota-saving',{detail:{kind,payload}}));
  let result!:T,checking=false,replayed=false;
  try {
    let deadline=0;  // set when the first try ends unknown: the end of the confirmation window
    for(let attempt=0;;attempt++){
      // The top bar is for the person's own saves (loading.ts track); the app's automatic commands stay silent.
      const limit=attempt?Math.min(REPLAY_TIMEOUT_MS,Math.max(0,deadline-Date.now())):COMMAND_TIMEOUT_MS;
      const stop=new AbortController(),timer=setTimeout(()=>stop.abort(),limit);
      const sent=request<T>('/api/commands',{method:'POST',body,headers:{'Idempotency-Key':key},signal:stop.signal});
      try{result=await (own?track(sent):sent);break;}
      // Abandoned at the timeout (sent() says PAGE_LEFT for an aborted request): as unknown as no answer at all.
      catch(e){if(!stop.signal.aborted&&!unknownOutcome(e)){
          // D-139: a replay refused (STALE_REVISION: the rota moved on) may be refused BECAUSE the first try committed
          // after this replay was admitted. The receipt (GET /api/commands/{key}) is the answer: committed is saved.
          const done=checking&&e instanceof ApiError&&e.status>=400&&e.status<500?await committedReceipt<T>(key):null;
          if(done){result=done.result;break;}
          throw e;}
        if(!checking){checking=true;deadline=Date.now()+CONFIRM_WINDOW_MS;if(own)confirming++;window.dispatchEvent(new CustomEvent('rota-save-checking',{detail:{kind,payload}}));}
        const wait=CONFIRM_DELAYS[Math.min(attempt,CONFIRM_DELAYS.length-1)];
        if(Date.now()+wait>=deadline)throw new ApiError('SAVE_UNCONFIRMED',unconfirmedWords(kind),0);
        await untilReconnected(wait);requireSession(generation);}  // never sent as another account
      finally{clearTimeout(timer);}
    }
    if(checking){checking=false;if(own)confirming--;replayed=true;}release();rememberUndo(kind,payload,result);}
  catch(caught) {if(checking&&own)confirming--;inFlight--;if(own)ownInFlight--;window.dispatchEvent(new CustomEvent('rota-save-settled'));let e=caught;
    // A real refusal releases the key; an unconfirmed outcome (or a server fault) keeps it for the same command again.
    if(e instanceof ApiError && e.status>0 && e.status<500)release();
    // SW3-B-03b/21: a rota decision refused because the rota moved on (returned, reopened, approved elsewhere) reads the
    // rota again, so the refusal can say what it is now and the page can show it (details.current; dom.ts settledWords).
    if(e instanceof ApiError&&e.code==='STALE_REVISION'&&target_id&&ROTA_DECISIONS.has(kind)){const stale=e;
      e=await rotaMeta(target_id).then(r=>new ApiError(stale.code,stale.message,stale.status,{...(stale.details as object||{}),current:r.workspace}),()=>stale);}
    // G-04: a refused Undo or Redo changed nothing and lost nothing: its caller says the one outcome (undoRefused), with
    // no conflict message and no "Not saved" beside it.
    if(isUndo(kind,payload))throw e;
    // An automatic command (the rota check after a change) refused because the rota moved on lost nothing the person did:
    // the newer version gets its own check, so no conflict message and no "Not saved" (walk AUTOSCHED-UX: Undo right after Apply).
    if(!own&&e instanceof ApiError&&e.code==='STALE_REVISION')throw e;
    if(e instanceof ApiError&&e.code.startsWith('STALE_'))window.dispatchEvent(new CustomEvent('rota-conflict',{detail:{code:e.code,kind,current:(e.details as {current?:unknown}|undefined)?.current,owned:owning>0}}));
    // The footer's save icon turns red until the next save goes through; a refused rota decision says so once, itself.
    if(!(e instanceof ApiError&&(e.details as {current?:unknown}|undefined)?.current))window.dispatchEvent(new CustomEvent('rota-save-failed',{detail:{kind,payload,code:(e as ApiError).code,message:(e as Error).message}}));throw e;}
  inFlight--;if(own)ownInFlight--;
  // `activity` is the pulse value this change leads to: the page already shows it, so that pulse is not a reason to refresh.
  // `replayed`: confirmed after an unknown outcome; the page reads the rota again so it shows what the server has.
  window.dispatchEvent(new CustomEvent('rota-saved',{detail:{kind,payload,result,activity:(result as {activity?:number})?.activity,replayed}}));return result;
}

/* Undo, in one place. Each reversible command kind maps to the command(s) that put it back, built from what the
 * server returned (ids and new revisions, so a record changed meanwhile is refused, never overwritten). Every save goes
 * through command() above, so every reversible save gets Undo: the next success message claims it (dom.ts notify),
 * or, if the screen shows none, a "Saved." message offers it. Rota decisions (submit, approve, publish) are deliberately
 * absent: they are recorded decisions, not edits, and so are reopening a rota (workspace.reopen) and activating or
 * retiring a review route (review_route.*), which keep their governed dialog. A revoked invitation (account.revoke_invite)
 * has no reverse (a new link is sent instead), so its button is armed. A rota link (request.link_rota) is undone by its own message
 * (requests.ts linkRota), which knows the link before it. Request, weekly-availability and swap decisions have a server undo
 * (request.undo, availability_pattern.undo, swap.undo) that is refused once the decision has been acted on. Rules are governed too, so their Undo is a
 * decision of its own, audited like any: a proposal is withdrawn (declined, kept in History); an activation is undone
 * by retiring the rule, which brings back the rules it grouped, and restoring the versions it replaced. */
type Row={id:string;revision:number;[k:string]:any};
// `done`: what the message says once undone (else the general "Undone.").
/** An undo step. A bound draft edit keeps the content revision it left, even if a later check changes the rota revision. */
type Step=Command&{edit?:number};
type Undo={said:string;undo:Step[];now?:boolean;done?:string};
export const WITHDRAWN='Withdrawn with Undo by the person who proposed it';
const back=(kind:string,payload:Record<string,unknown>,row?:Row):Command[]=>row?.id?[{kind,payload,target_id:row.id,expected_revision:row.revision}]:[];
/** The server's own reverse of a command, as it returned it (its receipt binding, e.g. `undo_of`, included): never rebuilt here. */
const served=(x:any):Command[]=>x?.undo?.kind&&(x.undo.kind!=='bulk.apply'||(x.undo.payload?.items||[]).length)?[x.undo as Command]:[];
const draftEdit=(said:string)=>(_:any,x:any):Undo=>{
  const workspace=x?.workspace,undoOf=workspace?.undo_change_id;
  const payload:WorkspaceUndoInput={reason:'Undo',...(typeof undoOf==='string'?{undo_of:undoOf}:{})};
  const undo=back('workspace.undo',payload,workspace);
  return {said,now:true,undo:undo.map(c=>payload.undo_of&&typeof workspace.content_revision==='number'
    ?{...c,edit:workspace.content_revision}:c)};
};
/** Did this Apply write? Only the writer's durable receipt `applied` says so (a retained or refused proposal wrote
 *  nothing). A lost reply is never success by itself: app.ts appliedAnyway returns APPLY_CONFIRMED only once the job
 *  reads `applied`. The one test: lives here so api.ts needs no import cycle; autoschedule_result.ts (applyNeedsReview),
 *  the live strip and the footer all read it. */
export const applyWrote=(reply:unknown)=>(reply as {reconciliation?:{status?:unknown}}|null|undefined)?.reconciliation?.status==='applied';
export const APPLY_CONFIRMED={reconciliation:{status:'applied'},confirmed:true} as const;
/** Did Delete draft keep the rota (archived, so Restore brings it back)? The server says `deleted: true` only when it
 *  removed it for good (a legacy draft with no baseline); its one signal for both the message and its Undo. No reply
 *  claims neither (rota_lifecycle.ts reads the rota again). */
export const deleteWasRecoverable=(reply:unknown)=>reply!=null&&(reply as {deleted?:unknown}).deleted!==true;
const UNDO:Record<string,(payload:any,result:any)=>Undo>={
  'shift.create':draftEdit('Session added'),'shift.update':draftEdit('Session saved'),'shift.action':draftEdit('Sessions updated'),
  'shift.copy':draftEdit('Sessions copied'),'issues.resolve':draftEdit('Issues resolved'),'draft.swap':draftEdit('Assignments exchanged'),
  // A retained or refused proposal offers no Undo (it would undo the edit before).
  'generation.apply':(p,x)=>applyWrote(x)?draftEdit('Suggestions applied to the draft')(p,x):{said:'',undo:[]},
  'workspace.target_override':draftEdit('Target saved'),'workspace.rule_adjust':draftEdit('Rule changed for this rota'),
  'workspace.resolve_overlap':draftEdit('Rota dates changed'),
  // A Redo is an edit like any other: its Undo takes it off again (SW3-A-06).
  'workspace.redo':draftEdit('Redone'),
  // Bulk draft edits keep the arrangement before them too (one server Undo for the whole batch).
  'series.expand':draftEdit('Sessions added'),'series.reconcile':draftEdit('Recurring sessions updated'),'import.apply':draftEdit('Import applied'),
  // A recurring session's date not on any rota yet: the server returns the series edit that brings it back.
  'series.occurrence.cancel':(_,x)=>({said:'Occurrence cancelled',undo:x?.undo_command?.kind?[x.undo_command as Command]:[]}),
  'workspace.archive':(_,x)=>({said:'Rota archived',undo:back('workspace.restore',{reason:'Undo archive'},x?.workspace)}),
  // A baseline-backed draft uses the delete command as a recoverable archive; legacy untouched drafts still have no reverse.
  'workspace.delete_draft':(_,x)=>deleteWasRecoverable(x)
    ?{said:'Rota removed',undo:back('workspace.restore',{reason:'Undo removal'},x.workspace)}:{said:'Rota deleted',undo:[]},
  // Score check: a removed file and a discarded import keep their evidence; the server restores them.
  'score_import.remove_source':(p,x)=>({said:'File removed',undo:back('score_import.restore_source',{import_id:p.import_id,source_id:p.source_id},x)}),
  'score_import.discard':(p,x)=>({said:'Files discarded',undo:back('score_import.restore',{import_id:p.import_id},x)}),
  // A deleted AI provider keeps its encrypted key; the server returns its restore (back disabled and not default).
  'ai_provider.delete':(_,x)=>({said:'Provider deleted',undo:served(x)}),
  // A person's contact (D68): the server's reverse, the value and revision before it.
  'person.contact.set':(_,x)=>({said:'Contact saved',undo:served(x)}),
  // A new record is switched off (records are never deleted); an edit goes back to the values before it.
  'catalogue.create':(p,x)=>({said:'Saved',undo:x?.record?.active===true?(p.catalogue==='tags'
    ?back('catalogue.update',{catalogue:'tags',value:{...p.value,active:false},...(x.added?.length?{members:{add:[],remove:x.added}}:{}),reason:'Undo'},x.record)
    :[{kind:'catalogue.bulk_update',payload:{catalogue:p.catalogue,items:[{id:x.record.id,revision:x.record.revision}],active:false,reason:'Undo'}}]):[]}),
  // A tag's metadata and membership were one save: their inverse must also commit together.
  'catalogue.update':(p,x)=>({said:'Saved',undo:x?.previous_value?back('catalogue.update',{catalogue:p.catalogue,value:x.previous_value,
    ...(p.catalogue==='tags'&&(x.removed?.length||x.added?.length)?{members:{add:x.removed||[],remove:x.added||[]}}:{}),reason:'Undo'},x.record):[]}),
  // Many records: one bulk command puts them all back (one transaction, one rate-limit hit, refused as a whole if any
  // record changed since).
  'catalogue.bulk_update':(_,x)=>({said:'Saved',undo:served(x)}),
  // Scheduling groups and their settings (maintenance): versioned rows; the server returns the version-appending reverse.
  'planning_group.create':(_,x)=>({said:'Scheduling group added',undo:served(x)}),'planning_group.update':(_,x)=>({said:'Scheduling group saved',undo:served(x)}),
  'planning_group.rollback':(_,x)=>({said:'Earlier version restored',undo:served(x)}),
  'planning.settings':(_,x)=>({said:'Saved',undo:served(x)}),'planning.settings.rollback':(_,x)=>({said:'Earlier settings restored',undo:served(x)}),
  // A bulk change: the server returns its own reverse (one bulk.apply of every item's reverse command, revision-bound).
  'bulk.apply':(_,x)=>({said:'Changed',undo:served(x)}),
  // A date set's own commands (an entry, a year's adjustment, an import, switching it off) save the whole set: the set
  // goes back to the values before them, through the same checked catalogue write. A copy is a new set: switched off.
  ...Object.fromEntries(['date_set.adjust','date_set.import','date_set.delete','date_set.entry.create','date_set.entry.update','date_set.entry.delete'].map(k=>[k,(_:unknown,x:any)=>({said:'Saved',
    undo:x?.previous_value?back('catalogue.update',{catalogue:'date_sets',value:x.previous_value,reason:'Undo'},x.record):[]})])),
  'date_set.copy_next_year':(_,x)=>({said:'Copied',undo:x?.record?.active===true?[{kind:'catalogue.bulk_update',payload:{catalogue:'date_sets',items:[{id:x.record.id,revision:x.record.revision}],active:false,reason:'Undo'}}]:[]}),
  // Tag members: the same command with added and removed swapped.
  'tag.members':(_,x)=>({said:'Tag members saved',undo:x?.added?.length||x?.removed?.length?back('tag.members',{add:x.removed,remove:x.added,reason:'Undo'},x.record):[]}),
  'availability.record':(_,x)=>({said:'Recorded',undo:served(x).length?served(x):back('availability.retract',{reason:'Undo'},x?.record)}),
  // A retracted record comes back (and a restored one is retracted again): the server returns that reverse.
  'availability.retract':(_,x)=>({said:'Retracted',done:'Undone. The record counts again.',undo:served(x)}),
  'availability.restore':(_,x)=>({said:'Restored',undo:served(x)}),
  // An ended weekly pattern is restored to exactly the state the end kept (waiting or approved), and back.
  'availability_pattern.end':(_,x)=>({said:'Weekly availability ended',done:'Undone. The weekly availability is back.',undo:back('availability_pattern.restore',{reason:'Undo'},x?.pattern)}),
  'availability_pattern.restore':(_,x)=>({said:'Weekly availability restored',undo:back('availability_pattern.end',{reason:'Undo'},x?.pattern)}),
  // Rotations planned or copied are removed again (only unchanged ones, only by who made them); removed ones come back.
  ...Object.fromEntries(Object.entries({'rotation.plan':'Rotations planned','rotation.copy':'Rotations copied',
    'rotation.import_reference':'Reference rotations imported'}).map(([k,said])=>[k,(_:unknown,x:any)=>({said,
    undo:x?.created?.length?[{kind:'rotation.undo_plan',payload:{items:x.created.map((r:Row)=>({id:r.id,revision:r.revision})),reason:'Undo'}}]:[]})])),
  'rotation.remove':(_,x)=>({said:'Rotations removed',undo:served(x).length?served(x):(x?.records||[]).map((r:Row)=>({kind:'catalogue.create',payload:{catalogue:'rotations',reason:'Undo',
    value:Object.fromEntries(['rotation_type_id','person_id','cohort_id','service_id','mentor_id','department_available','starts_on','ends_on'].map(k=>[k,r[k]]))}}))}),
  // A timeline gesture: the server's own reverse (remove what was placed, the dates before, or withdraw the request).
  'timeline.rotation':(_,x)=>({said:x?.status==='submitted'?'Change sent for approval':'Rotation saved',undo:served(x)}),
  'rotation.request_change':(_,x)=>({said:'Change requested',done:'Request withdrawn.',undo:back('request.decide',{decision:'withdraw',reason:'Undo'},x?.record)}),
  'trainee_note.create':(_,x)=>({said:'Note saved',undo:back('trainee_note.delete',{},x?.record)}),
  'trainee_note.update':(_,x)=>({said:'Note saved',undo:x?.previous_value?back('trainee_note.update',x.previous_value,x.record):[]}),
  'trainee_note.delete':(_,x)=>({said:'Note deleted',undo:back('trainee_note.restore',{},x?.record)}),
  'role.grant':(_,x)=>({said:'Role granted',undo:back('role.revoke',{reason:'Undo'},x?.record)}),
  'identity.disable':(_,x)=>({said:'Sign-in disabled',undo:back('identity.enable',{reason:'Undo'},x?.record)}),
  'department.sessions':(_,x)=>({said:'Session settings saved',undo:x?.previous?back('department.sessions',{...x.previous,reason:'Undo'},x.department):[]}),
  // Application branding: the server keeps the version before the latest change and puts it back (revision-bound).
  ...Object.fromEntries(['branding.update','branding.undo'].map(k=>[k,(_:unknown,x:any)=>({said:k==='branding.update'?'Name and logo saved':'Name and logo restored',
    undo:x?.can_undo&&x.branding?[{kind:'branding.undo',payload:{reason:'Undo'},expected_revision:x.branding.revision}]:[]})])),
  // The server's restore of the version before (department.display.restore): `previous` lists only the settings that
  // were stored, so re-sending it left a setting still at its default (names_default, rule_catalogue) at the new value.
  'department.display':(_,x)=>({said:'Display settings saved',undo:served(x).length?served(x):x?.previous?back('department.display',{...x.previous,reason:'Undo'},x.department):[]}),
  'department.display.restore':(_,x)=>({said:'Display settings restored',undo:served(x)}),
  // Feedback triage: the previous status, link, version and note come back (refused once the report changed again).
  'feedback.update':(_,x)=>({said:'Feedback updated',undo:x?.report?[{kind:'feedback.undo',payload:{id:x.report.id,revision:x.report.revision}}]:[]}),
  // Mark all as read: the server returns the exact notifications it marked, and their reverse.
  'notification.ack_all':(_,x)=>({said:'All marked as read',undo:served(x)}),
  // SW3-D-08 (D42): a notifications save is undone by restoring the version before it (the server's own rollback).
  'notification.policy.update':(p,x)=>({said:'Notifications saved',done:'Undone. The earlier notifications settings are back.',
    undo:x?.previous_version_id?[{kind:'notification.policy.undo',payload:{department_id:p.department_id,version_id:x.previous_version_id,reason:'Undo'},expected_revision:x.revision} as Command]:[]}),
  'table_view.update':(_,x)=>({said:'View updated',undo:x?.previous_view?back('table_view.update',x.previous_view,x.view):[]}),
  'table_view.delete':(_,x)=>({said:'View deleted',undo:x?.previous_view?[{kind:'table_view.create',payload:x.previous_view}]:[]}),
  'table_view.set_default':(_,x)=>({said:'Default saved',undo:x?.previous_default?back('table_view.set_default',x.previous_default,x.default):[]}),
  // Department settings are checked against the organisation's revision; the previous values carry the department's.
  'department.settings':(p,x)=>({said:p?.generation_defaults||p?.generation_controls?'Auto-schedule defaults saved':'Department settings saved',undo:x?.previous?back('department.settings',{...x.previous,reason:'Undo'},x.organisation&&{id:x.department.id,revision:x.organisation.revision}):[]}),
  'identity.enable':(_,x)=>({said:'Sign-in enabled',undo:back('identity.disable',{reason:'Undo'},x?.record)}),
  // One rank or skill added: removed again (only unchanged, only by who made it). Ended or revoked: the server's reverse.
  ...Object.fromEntries(['membership.create','grant.create'].map(k=>[k,(_:unknown,x:any)=>({said:'Saved',
    undo:x?.record?[{kind:k.replace('.create','.undo_create'),payload:{items:[{id:x.record.id,revision:x.record.revision}],reason:'Undo'}}]:[]})])),
  ...Object.fromEntries(['membership.end','eligibility.end','grant.revoke'].map(k=>[k,(_:unknown,x:any)=>({said:'Ended',done:'Undone. The earlier dates are back.',undo:served(x)})])),
  // SW3-D-06 (maintenance): a rank or clinic eligibility that has not started is withdrawn; its history stays, Undo brings it back.
  ...Object.fromEntries(['membership.withdraw','eligibility.withdraw','programme_membership.withdraw'].map(k=>[k,(_:unknown,x:any)=>({said:'Withdrawn',done:'Undone. The record is back.',undo:served(x)})])),
  'eligibility.extend':(_,x)=>({said:'Extended',done:'Undone. The earlier end is back.',undo:served(x)}),
  // Single and Bulk date changes (M1): the server's own reverse.
  ...Object.fromEntries(['membership.update_dates','eligibility.update_dates','grant.update_dates'].map(k=>[k,(_:unknown,x:any)=>({said:'Dates changed',done:'Undone. The earlier dates are back.',undo:served(x)})])),
  'availability.update':(_,x)=>({said:'Changed',done:'Undone. The record is as it was.',undo:served(x)}),
  'staff_import.apply':(_,x)=>({said:'Import applied',undo:['new_people','updated_people','rank_changes','programme_memberships','eligibilities','skills'].some(k=>x?.applied?.[k])?back('staff_import.undo',{reason:'Undo'},x.batch):[]}),
  // Restore the same wish so recurrence and linked overrides survive Undo; admission is checked again.
  'wish.create':(_,x)=>({said:'Wish saved',undo:back('wish.withdraw',{reason:'Undo'},x?.wish)}),
  'wish.update':(_,x)=>({said:'Wish saved',undo:x?.previous?back('wish.update',x.previous,x.wish):[]}),
  'wish.withdraw':(_,x)=>({said:'Wish withdrawn',undo:back('wish.restore',{reason:'Undo withdrawal'},x?.wish)}),
  'wish.restore':(_,x)=>({said:'Wish restored',undo:back('wish.withdraw',{reason:'Undo restoration'},x?.wish)}),
  'policy.create':(p,x)=>({said:'Rule proposed',done:p?.definition?.replaces_id?'Change withdrawn.':'Proposal withdrawn.',undo:back('policy.decline',{reason:WITHDRAWN},x?.record)}),
  // Calendar entries added on the way stay (they are not rules): then only the proposals could be undone, so none is.
  'policy.group':(_,x)=>({said:'Rules proposed',done:(x?.records||[]).length===1?'Proposal withdrawn.':`${(x?.records||[]).length} proposals withdrawn.`,undo:x?.calendar_entries?[]:(x?.records||[]).flatMap((r:Row)=>back('policy.decline',{reason:WITHDRAWN},r))}),
  'policy.activate':(_,x)=>activationUndo(x?.record?[x.record]:[],x?.replaced),
  // SW3-B-16 (D42): a retirement is undone by retiring what it brought back, then restoring the rule (the server refuses
  // a restore while a rule it replaced is in force). One still waiting for other approvers changed nothing: no Undo.
  'policy.retire':(_,x)=>({said:'Rule retired',done:'Retirement undone: the rule applies again.',
    undo:x?.approval&&!x.approval.complete?[]:[...(x?.restored||[]).flatMap((r:Row)=>back('policy.retire',{reason:'Undo retirement'},r)),...back('policy.restore',{reason:'Undo retirement'},x?.record)]}),
  // A catalogue rule's proposal is withdrawn by Undo (as a legacy policy.create is); a declined or withdrawn proposal
  // comes back waiting for approval (policy.restore_proposal keeps its independent approval).
  'rule.propose':(_,x)=>({said:'Rule proposed',done:'Proposal withdrawn.',undo:back('policy.decline',{reason:WITHDRAWN},x?.record)}),
  // Only a catalogue rule's: the legacy Rules page sends declines inside its own flows (a corrected proposal, its own
  // Undo), where a decline ends the undo batch rather than offering a second Undo.
  'policy.decline':(p,x)=>({said:p?.reason===WITHDRAWN?'Proposal withdrawn':'Proposal declined',done:'The proposal is waiting for approval again.',undo:x?.record?.kind==='catalogue'?back('policy.restore_proposal',{reason:'Undo'},x.record):[]}),
  // Simplify rules: one department proposal is withdrawn; its activation is undone (the previous rules apply again).
  'rule.migrate_to_catalogue':(p,x)=>({said:'Simplification proposed',done:'Proposal withdrawn.',undo:p?.version===2?back('rule.catalogue.decline',{reason:WITHDRAWN},x?.record):[]}),
  'rule.catalogue.activate':(_,x)=>({said:'Rules simplified',done:'The previous rules apply again.',undo:back('rule.catalogue.undo',{reason:'Undo activation'},x?.record)}),
  // A person's quota, target, overtime offer or calendar protection: a proposal is withdrawn; an approval or a retirement is undone.
  ...Object.fromEntries(['person.workload','person.overtime','calendar.protection'].flatMap(f=>[
    [f+'.propose',(_:unknown,x:any)=>({said:'Proposed for approval',done:'Proposal withdrawn.',undo:back(f+'.undo',{reason:WITHDRAWN},x?.fact)})],
    ...['activate','retire'].map(a=>[`${f}.${a}`,(_:unknown,x:any)=>({said:a==='activate'?'Approved':'Retired',undo:back(f+'.undo',{reason:'Undo'},x?.fact)})])])),
  'policy.activate_many':(_,x)=>activationUndo(x?.records||[],x?.replaced),
  // Governance: a self-approval permission added is ended again; a delegation recorded is revoked. One ended or revoked is
  // restored by the server from that record (its original delegator or creator, scope, route revision and dates, checked
  // again), never recreated as the person pressing Undo; an expired delegation cannot be restored.
  'self_approval.grant':(_,x)=>({said:'Permission added',done:'Permission ended again.',undo:back('self_approval.revoke',{reason:'Undo'},x?.permission)}),
  'self_approval.revoke':(_,x)=>({said:'Permission ended',done:'Permission restored.',undo:back('self_approval.restore',{reason:'Undo'},x?.permission)}),
  'review_delegation.create':(_,x)=>({said:'Delegation recorded',done:'Delegation revoked.',undo:back('review_delegation.revoke',{reason:'Undo'},x?.delegation)}),
  'review_delegation.revoke':(_,x)=>({said:'Delegation revoked',done:'Delegation restored.',
    undo:x?.delegation&&Date.parse(x.delegation.expires_at)>Date.now()?back('review_delegation.restore',{reason:'Undo'},x.delegation):[]}),
  'role.revoke':(_,x)=>({said:'Role revoked',undo:x?.record?[{kind:'role.grant',payload:{identity_id:x.record.identity_id,role:x.record.role,starts_on:x.record.starts_on,ends_on:x.record.ends_on,reason:'Undo'}}]:[]}),
  // Request and weekly-availability decisions: the server puts them back only while that is still valid (not once a rota
  // is linked to the request) and records the undo; a bulk decision is undone as one batch. A swap step is undone
  // forward: the swap closes and reopens as a new one at the earlier stage (the database keeps consent forward-only).
  'request.decide':(_,x)=>({said:'Decision saved',undo:back('request.undo',{reason:'Undo'},x?.record)}),
  'department.leave_balance_settings':(_,x)=>({said:'Leave allowances saved',undo:served(x)}),
  'request.edit':(_,x)=>({said:'Request updated',undo:served(x)}),
  'request.decide_many':(_,x)=>({said:'Decisions saved',undo:x?.batch_id?[{kind:'request.undo',payload:{batch_id:x.batch_id,reason:'Undo'}}]:[]}),
  'availability_pattern.decide':(_,x)=>({said:'Decision saved',undo:back('availability_pattern.undo',{reason:'Undo'},x?.pattern)}),
  // Once a confirmed swap is prepared into a rota change (automatically), it is withdrawn or declined instead.
  ...Object.fromEntries(['swap.reject','swap.withdraw','swap.accept','swap.counteroffer','swap.confirm'].map(k=>[k,(_:unknown,x:any)=>({said:'Swap updated',undo:x?.swap?.workspace_id?[]:back('swap.undo',{reason:'Undo'},x?.swap)})])),
};
// A rule's new version names the version it replaces (a catalogue version in merged_ids, a legacy one in replaces_id):
// undoing it brings that one back.
const replacesVersion=(r:Row)=>!!r.definition?.replaces_id||r.definition?.rule?.kind==='catalogue'&&!!r.definition?.merged_ids?.length;
const activationUndo=(made:Row[],replaced:Row[]=[]):Undo=>({said:made.length>1?`${made.length} rules activated`:'Rule activated',
  done:!replaced.length&&made.length&&made.every(replacesVersion)?`Activation undone: the previous version${made.length>1?'s apply':' applies'} again.`
    :`Activation undone: ${made.length>1?'the rules are':'the rule is'} retired${replaced.length?' and what '+(made.length>1?'they':'it')+' replaced applies again':''}.`,
  undo:[...made.flatMap(r=>back('policy.retire',{reason:'Undo activation'},r)),...replaced.flatMap(r=>back('policy.restore',{reason:'Undo activation'},r))]});
let undoBatch:Undo[]=[],undoTimer=0,undoing=false,holding=0;
// A change nobody reports within 1.2 s gets its own "Saved" message with Undo, unless a run of changes holds the batch.
const unclaimed=()=>{window.clearTimeout(undoTimer);if(holding||!undoBatch.length)return;
  undoTimer=window.setTimeout(()=>{if(undoBatch.length&&!holding)window.dispatchEvent(new CustomEvent('undo-unclaimed',{detail:undoBatch.at(-1)!.said}));},undoBatch.at(-1)!.now?0:1200);};
function rememberUndo(kind:string,payload:Record<string,unknown>,result:unknown){
  if(undoing||kind==='department_content.seen')return;
  const u=UNDO[kind]?.(payload,result);
  // Anything that cannot be put back ends the batch: Undo never skips over a later change.
  if(!u?.undo.length){undoBatch=[];window.clearTimeout(undoTimer);return;}
  undoBatch.push(u);unclaimed();
}
/** One Undo for a run of changes (a bulk action, one command per record): however slow one of them is, no change in it
 *  is claimed by its own message meanwhile, so the run's message undoes all of them. */
export async function holdUndo<T>(run:()=>Promise<T>):Promise<T>{holding++;try{return await run();}finally{holding--;unclaimed();}}
/** What an Undo did: each undo command with its reply. A reply has the shape of the forward command's (a draft undo: the
 *  rota, the sessions it changed or removed and the issues that came and went, as a Resolve issues batch returns). */
export type Undone={kind:string;result:any}[];
/** Undo applies in place: the open page patches what the replies describe (`undoInPlace`: true when it did); otherwise
 *  the app reads just that page's data again (`onUndone`). Either runs before Undo reports done. */
let afterUndo:(done:Undone)=>Promise<void>=async()=>{};
export const onUndone=(run:(done:Undone)=>Promise<void>)=>{afterUndo=run;};
let pageUndo:((done:Undone)=>boolean|Promise<boolean>)|null=null;
export const undoInPlace=(apply:(done:Undone)=>boolean|Promise<boolean>)=>{pageUndo=apply;};
/** Shows what an Undo did (also one sent directly, as Undo last edit does). */
export async function applyUndone(done:Undone){if(!await pageUndo?.(done))await afterUndo(done);}
/** Redo (SW3-A-06): a draft's Undo can be redone by the same person until the rota changes again. The rota the Undo left,
 *  or null when what was undone is not a draft edit. */
export const redoable=(done:Undone):Row|null=>{const w=done.length===1&&done[0].kind==='workspace.undo'?done[0].result?.workspace:null;return w?.id?w:null;};
/** Re-applies the rota's undone edit. The server refuses a stale one (the rota changed since) and re-checks the rest; the
 *  page patches the reply as it does an Undo's, and its "Redone." message offers Undo again. */
export async function redoDraft(w:{id:string;revision:number}):Promise<Undone>{
  const done=[{kind:'workspace.redo',result:await command('workspace.redo',{reason:'Redo'},w.id,w.revision).catch(e=>{throw undoRefused(e);})}];
  await applyUndone(done);return done;}
export const STALE_UNDO='Already undone, or the rota changed since. Nothing was undone.';
/** An Undo's (or Redo's) own commands: those of an Undo run, a draft's undo/redo, and the reverse commands (reason "Undo…"). */
const isUndo=(kind:string,payload:Record<string,unknown>)=>undoing||kind==='workspace.undo'||kind==='workspace.redo'||/^(Undo|Redo)\b/.test(String(payload.reason||''));
/** G-04: an Undo or Redo the server refused because the record moved on (STALE_*) is one plain outcome: nothing was undone. */
//  One the server marks recoverable (a check ran at the same moment; nothing was applied) can simply be tried again.
export const undoRefused=(e:unknown)=>!(e instanceof ApiError&&e.code.startsWith('STALE_'))?e:(e.details as {recoverable?:boolean}|undefined)?.recoverable
  ?Object.assign(new Error('Not undone: the rota was being checked at the same moment. Nothing changed.'),{cause:e,retry:true}):Object.assign(new Error(STALE_UNDO),{cause:e});
/** RUL-03: replies to a retire or restore that still waits for other approvers (nothing changed yet). */
export const awaitingApproval=(replies:any[]):any[]=>replies.filter(r=>r?.approval&&!r.approval.complete&&r.record?.id);
/** Withdraws the approvals those replies recorded: the whole Undo of a change that never applied. */
export async function withdrawApprovals(replies:any[]){
  for(const r of awaitingApproval(replies))await command('policy.withdraw_approval',{reason:'Undo'},r.record.id,r.record.revision);}
/** The Undo for the changes just saved, taken once (by the success message that reports them), or null. Its run answers
 *  each undo command with its reply (a governed one may come back waiting for approval: see awaitingApproval). */
export function takeUndo():{said:string;done?:string;run:()=>Promise<Undone>}|null{
  if(!undoBatch.length)return null;
  const steps=undoBatch.slice().reverse().flatMap(u=>u.undo),said=undoBatch.at(-1)!.said,done=undoBatch.length===1?undoBatch[0].done:undefined,generation=sessionGeneration;
  undoBatch=[];window.clearTimeout(undoTimer);
  return {said,done,run:async()=>{
    requireSession(generation);
    // A rota's Undo puts back its latest edit only: once the rota moved on (its Undo was already used from Recent runs or
    // the top bar, it was submitted, someone else changed it), this one is stale. Said once, with nothing sent, so no
    // conflict message and no "Not saved" for a change that never happened (G-17, G-28).
    const first=steps[0],known=first?.target_id?knownRevision(first.target_id):undefined;
    // maintenance: a draft edit's Undo is the same as Undo last edit while the rota's content is still what that edit left:
    // A check may move only the rota revision. If the content is the same, send the current revision
    // with the original edit ID. The server refuses if another edit is now next to undo.
    const sameEdit=first?.kind==='workspace.undo'&&first.edit!=null&&!!first.target_id&&contents.get(first.target_id)===first.edit;
    if(sameEdit&&known!==undefined)steps[0]={...first,expected_revision:known};
    else if(known!==undefined&&first.expected_revision!=null&&known!==first.expected_revision)throw new Error(STALE_UNDO);
    // The latest change is undone first, each change's steps in their order (an activation: retire, then restore).
    // Later steps may touch a record an earlier step just changed: carry its new revision forward.
    // A reply that kept records changed since (removed 0, kept 1) did not put the previous values back: say so.
    const seen=new Map<string,number>(),done:Undone=[];let kept='';undoing=true;
    try{for(const c of steps){
      requireSession(generation);
      const r:any=await command(c.kind,c.payload,c.target_id??undefined,(c.target_id&&seen.get(c.target_id))||c.expected_revision||undefined);done.push({kind:c.kind,result:r});
      if(Number(r?.kept)>0)kept=`${Number(r.removed??r.reverted??0)} undone; ${r.kept} changed since and kept.`;
      for(const row of [r?.record,r?.workspace,r?.department,r?.view,...(r?.records||[])])if(row?.id)seen.set(row.id,row.revision);}}
    catch(e){if(sessionChanged(e))throw e;const why=undoRefused(e) as Error;throw done.length?new Error(`${done.length} of ${steps.length} undone. Stopped: ${why.message}`):why;}
    // The page shows what was undone before Undo reports done, so nobody acts on a page that is about to change.
    finally{if(generation===sessionGeneration){undoing=false;if(done.length)await applyUndone(done).catch(()=>undefined);}}
    if(kept&&generation===sessionGeneration)throw new Error(`Not everything was undone: ${kept}`);
    return done;
  }};
}
// Identical reads already on their way are shared (the planner and the sidebar counts both ask for the rota list on load).
// A save or a refresh (readAfresh) starts reads anew, so nobody is handed data from before a change.
const inflight=new Map<string,Promise<unknown>>();
// One guard for every page: a page's reads belong to it. Leaving the page aborts them (the server stops sending, and the
// page that was left stops building: its awaits reject with PAGE_LEFT, which every caller already ignores through
// `sessionChanged`), so a late answer can never redraw or report over the page that replaced it. Reads that serve the
// whole app (the session, lists in memory, the live pulse, inbox checks, sidebar counts) use `background`.
let pageReads=new AbortController();
export const pageReadSignal=()=>pageReads.signal;
// A left page's reads end with PAGE_LEFT wherever they were awaited, including fire-and-forget loads: that is the
// intended outcome, not an error, so it is never reported as an unhandled rejection. Every other rejection still is.
if(typeof window!=='undefined')window.addEventListener('unhandledrejection',e=>{if(e.reason instanceof ApiError&&e.reason.code==='PAGE_LEFT')e.preventDefault();});
export function leavePage(){pageReads.abort();pageReads=new AbortController();pageUndo=null;for(const key of [...inflight.keys()])if(key.startsWith('page|'))inflight.delete(key);}
/** The top bar's reads reviewed. While a page the person opened is
 *  loading (app.ts renderPage: a new page or address, their Refresh), the page reads it starts are that wait, until they
 *  have all answered; a read started later (polling, a live update) or marked quiet (the whole rota behind a drawn
 *  window, D59) never lights it. */
let opening=false,openReads=0;
export function openingPage(on:boolean){opening=on||openReads>0;}
/* A background read (a refresh after a save, a live update, polling; not what an opening page waits for, which has its
 * own "Still loading · 0:18") still waiting after SLOW_READ_MS: the footer says UPDATING_WORDS, neutrally,
 * until it settles (event `reads-updating`). Never a connection state: slow is not down (maintenance). The pulse is the
 * connection probe and the live stream never ends, so neither counts. Nor is anything said twice (never what is on
 * screen already): a job's status poll is said by that job's own progress (the footer's "Checking the rota · m:ss"),
 * and while a save is on its way the save state ("Saving…", "Checking whether it saved…") speaks instead. */
// Measured on dev under load (6 Nov 2026, 3,827 reads): p50 0.23 s, p95 3.6 s; 2.6 % waited over 10 s.
export const SLOW_READ_MS=10_000;
let slowBackground=0;
export const updatingNow=()=>slowBackground>0&&!ownInFlight&&!confirming;
const sayUpdating=()=>{if(typeof window!=='undefined')window.dispatchEvent(new CustomEvent('reads-updating',{detail:slowBackground>0}));};
function read<T>(path:string,scope:'page'|'app',quiet=false,fresh=false):Promise<T>{
  // A kept reply (keptReads) answers at once: no wait for the person, so never part of the opening's top bar.
  const shown=scope==='page'&&!fresh?keptReply<T>(path):null;if(shown)return shown;
  // The same read already on its way is shared across scopes: a page joins an app read (never aborted); an app read joins
  // a page read and, if that page is left meanwhile, asks again on its own.
  const other=inflight.get((scope==='page'?'app|':'page|')+path);
  if(other)return scope==='page'?other as Promise<T>:(other as Promise<T>).catch(e=>{if(e instanceof ApiError&&e.code==='PAGE_LEFT')return read<T>(path,'app');throw e;});
  const key=scope+'|'+path;let p=inflight.get(key);
  if(!p){const waits=scope==='page'&&opening&&!quiet;if(waits)openReads++;
    // N_UPDATING: an app read started while a page opens (its saved views, the rota list) is part of that wait, which
    // its skeleton says ("Still loading · m:ss", DL-045): never "Updating…" beside it.
    let slow=false;const timer=waits||opening||/^\/api\/(pulse|events)\b|^\/api\/jobs\/[^/?]+\/status/.test(path)?undefined:setTimeout(()=>{slow=true;slowBackground++;sayUpdating();},SLOW_READ_MS);
    const q:Promise<unknown>=request<T>(path,scope==='page'?{signal:pageReads.signal}:{}).finally(()=>{if(inflight.get(key)===q)inflight.delete(key);if(waits&&--openReads===0)opening=false;
      clearTimeout(timer);if(slow){slowBackground--;sayUpdating();}});
    inflight.set(key,p=waits?track(q):q);}
  return p as Promise<T>;
}
/** A page's read. Quiet work keeps the top bar settled. A fresh read bypasses kept replies. */
export const get=<T=any>(path:string,o:{quiet?:boolean;fresh?:boolean}={}):Promise<T>=>read<T>(path,'page',!!o.quiet,!!o.fresh);
/** A rota's metadata, revision, check summary, health and review state (`?view=compact`, a few KB at department scale):
 *  for a decision, a revision or a fingerprint. Never its sessions or findings: those are the full read (WorkspaceData). */
type RotaMeta={workspace:Row;current_fingerprint:string;health:Record<string,any>};
const rotaPath=(id:string,view:Exclude<WorkspaceDetailView,'full'>)=>'/api/workspaces/'+encodeURIComponent(id)+'?view='+view;
export const rotaMeta=(id:string,reader:typeof get=get)=>reader<RotaMeta>(rotaPath(id,'compact'));
export const rotaWhole=(id:string,reader:(path:string)=>Promise<WorkspaceData>=get<WorkspaceData>)=>reader(rotaPath(id,'schedule'));
const jobWatches=new Map<string,number>();
export const watchedJobs=()=>[...jobWatches.keys()];
export function watchJob(id:string){
  jobWatches.set(id,(jobWatches.get(id)||0)+1);window.dispatchEvent(new Event('job-subscriptions-changed'));
  return ()=>{const count=jobWatches.get(id)||0;if(count<=1)jobWatches.delete(id);else jobWatches.set(id,count-1);
    window.dispatchEvent(new Event('job-subscriptions-changed'));};
}
/** A worker job waited for (a rota check, a merge): its status read every `every` ms while queued or running, each read
 *  shown to `seen`. Resolves with its final status, or null once `stop()` says the wait no longer matters. */
/** Whether background workers serve jobs (GET /health/service): a rota check and the Fix list's candidates are jobs
 *  when they do, else answered at once. `workersKnown()`: null until the probe answers. The probe only decides what
 *  to offer when the server has no check going (D-062); it is asked again on each new proof of a job (`reprobeWorkers`),
 *  so a worker started after the page opened is found without a reload. */
let workersUp:boolean|null=null,workersProbe:Promise<boolean>|null=null,workersProof='';
export const probeWorkers=()=>workersProbe??=(()=>{const mine:Promise<boolean>=fetch('/health/service').then(r=>r.ok,()=>false).then(up=>workersProbe===mine?(workersUp=up):up);return mine;})();
export const workersKnown=()=>workersUp;
/** A job appeared (`proof` names it): the remembered answer is asked again, once per proof. */
export const reprobeWorkers=(proof:string)=>{if(proof!==workersProof){workersProof=proof;workersProbe=null;}return probeWorkers();};
/** D-062: the server says a check JOB exists to look at, whatever a worker probe said: a save's provisional check
 *  (scope command_changes, maintenance), the Fix list summary's `checking`, or a validation job known queued or running.
 *  Only the job is proof that it runs: the scope is sent on any recent worker heartbeat (a push-only worker, one that
 *  just died), so the status shown is the joined job's own (rule_check_api.ts checkingWords), never this alone. */
export const serverChecking=(validation?:{scope?:string}|null,summary?:{checking?:boolean}|null,job?:{status?:string}|null)=>
  validation?.scope==='command_changes'||!!summary?.checking||!!job&&['queued','running'].includes(String(job.status));
export async function awaitJob<J extends {id:string;[k:string]:any}>(job:J,{every=1000,seen,stop}:{every?:number;seen?:(job:J)=>void;stop?:()=>boolean}={}):Promise<J|null>{
  // H-02: the worker announces every job change on the live stream (scope "jobs"), which wakes the wait at once; the
  // status read is only the fallback, so it backs off (every → 2× … ≤ 10 s) instead of asking each second for minutes.
  const release=watchJob(job.id);try{for(let n=0;['queued','running'].includes(job.status);n++){
    await new Promise<void>(done=>{const wake=(event?:Event)=>{const ids=(event as CustomEvent<string[]|null>|undefined)?.detail;if(ids&&!ids.includes(job.id))return;window.clearTimeout(t);window.removeEventListener('jobs-changed',wake);done();},
      t=window.setTimeout(wake,Math.min(every*2**n,Math.max(every,10_000)));window.addEventListener('jobs-changed',wake);});
    if(stop?.())return null;
    job=(await get<{job:J}>('/api/jobs/'+job.id+'/status')).job;seen?.(job);}
  return job;}finally{release();}
}
/** A reload whose filters can change while it is on its way (search, filters, Show more): only the newest call's answer
 *  is used. An older answer that lands later resolves to undefined instead of drawing over the newer one. */
export function newest<A extends unknown[],T>(load:(...args:A)=>Promise<T>){
  let last=0;
  // A superseded call answers undefined whether it succeeded or failed (an aborted request is not an error to show).
  return async(...args:A):Promise<T|undefined>=>{const mine=++last;
    try{const answer=await load(...args);return mine===last?answer:undefined;}catch(err){if(mine===last)throw err;return undefined;}};
}
export const background=<T=any>(path:string):Promise<T>=>read<T>(path,'app');
export const readAfresh=()=>inflight.clear();
setRetryAfresh(readAfresh);

/* Kept answers (maintenance/118): a page rebuilt while its slow reads are on their way or answered (a live pulse that cannot
 * change them, Back, a restyle, openPage's check of its kept replies) joins them instead of asking again; readAfresh
 * (every refresh) does not reach them. Each consumer passes ONE module-level options object (its group): which live
 * scopes keep its answers (`keepScopes`: only these keep them) or drop them (`scopes`: any of these drops them), and how
 * many it keeps (`cap`, the oldest go first). Keyed by the exact key (a URL, or the URL plus what else it depends on):
 * distinct keys never share. A failed read is never kept. Every group is cleared by a sign-out (onSessionReset) and by
 * the person's own save (rota-saved: the redraw after a save can come before its live change); forgetReads(scopes)
 * (app.ts refreshOnce) drops what a live change can alter. The fetch reads through request/get, so an unchanged big
 * answer still revalidates with its ETag. `queueBehindInFlight` (FE-HANG, the People timeline): a read asked after its
 * answer was dropped while the read is still on its way waits for it, then reads once for everyone who asked meanwhile;
 * it never stacks a second server read. */
// `byId`: scopes judged per record: a change names its id (a rota's), and only keys starting `<id>|` are dropped.
export type KeptReadOptions={keepScopes?:readonly LiveScope[];scopes?:readonly LiveScope[];byId?:readonly LiveScope[];cap:number;queueBehindInFlight?:boolean};
/** One kept answer: `data` once it landed (a rebuilt page paints from it at once), `stop` aborts a read nobody wants. */
export type KeptRead<T>={p:Promise<T>;data?:T;done:boolean;stop:AbortController;page:AbortSignal};
type KeptGroup={o:KeptReadOptions;entries:Map<string,KeptRead<any>>;running:Map<string,Promise<any>>;queued:Map<string,Promise<any>>};
const keptGroups=new Map<KeptReadOptions,KeptGroup>();
const groupOf=(o:KeptReadOptions)=>{let g=keptGroups.get(o);if(!g)keptGroups.set(o,g={o,entries:new Map(),running:new Map(),queued:new Map()});return g;};
/** The consumer's kept answers by key, for its own housekeeping (an alias of one answer under a second key, aborting
 *  what a draw no longer wants). */
export const keptAnswers=(o:KeptReadOptions)=>groupOf(o).entries;
export function keptRead<T>(key:string,fetch:(signal:AbortSignal)=>Promise<T>,o:KeptReadOptions):KeptRead<T>{
  const g=groupOf(o),had=g.entries.get(key),mine=pageReads.signal;
  // A page read on its way for a page since left is aborted (leavePage): the newer page's join then asks again.
  if(had)return had.done||had.page===mine?had:{...had,p:had.p.catch(e=>{
    if(e instanceof ApiError&&e.code==='PAGE_LEFT'&&!mine.aborted)return keptRead(key,fetch,o).p;throw e;})};
  const stop=new AbortController();
  const begin=()=>{const p=fetch(stop.signal);
    if(o.queueBehindInFlight){const clear=()=>{if(g.running.get(key)===p)g.running.delete(key);};p.then(clear,clear);g.running.set(key,p);}
    return p;};
  // A read on its way may predate the change that dropped its answer: wait for it, then read once.
  const ahead=o.queueBehindInFlight?g.running.get(key):undefined,next=()=>{g.queued.delete(key);return begin();};
  const p:Promise<T>=!ahead?begin():g.queued.get(key)||(()=>{
    const q=ahead.then(next,e=>{if(e instanceof ApiError&&e.code==='SESSION_CHANGED'){g.queued.delete(key);throw e;}return next();});g.queued.set(key,q);return q;})();
  const entry:KeptRead<T>={stop,page:mine,done:false,p:p.then(d=>{entry.data=d;entry.done=true;return d;},
    e=>{for(const [k,v] of g.entries)if(v===entry)g.entries.delete(k);throw e;})};  // never kept, under any alias
  g.entries.set(key,entry);while(g.entries.size>o.cap)g.entries.delete(g.entries.keys().next().value!);
  return entry;
}
/** Drop one consumer-owned answer (and any queued replacement) before a retry. Other kept-read groups and keys keep
 * sharing their own requests. The old promise may still settle if its fetch ignores AbortSignal; removing its entry is
 * the guard that prevents a replacement read from joining it or a late answer from being shown. */
export function forgetRead(key:string,o:KeptReadOptions){
  const g=keptGroups.get(o),entry=g?.entries.get(key);if(!g||!entry)return;
  g.entries.delete(key);g.running.delete(key);g.queued.delete(key);entry.stop.abort();
}
/** A refresh with the live scopes it reads (null: anything may have changed) and, when known, the changes behind them
 *  (their ids): each group drops what they can alter. */
// `answered`: drop only answers already in; a read on its way stays shared (projectionReady: it is the one waiting).
export function forgetReads(scopes:ReadonlySet<LiveScope>|null,changes:readonly LiveChange[]|null=null,answered=false){
  // Any change forgets every reply kept for openings: a reply carries more than its reader shows (/api/catalogues holds
  // people and settings), so no page's scopes can say it is unaffected. Its ETag stays: the next use is conditional.
  keptEpoch++;
  for(const g of keptGroups.values()){const {keepScopes,scopes:drops,byId=[]}=g.o;
    const clear=()=>{for(const [k,v] of [...g.entries])if(!answered||v.done)g.entries.delete(k);};
    if(!scopes){clear();continue;}
    const named=[...scopes].filter(s=>!byId.includes(s));
    if(!(keepScopes?named.every(s=>keepScopes.includes(s)):named.every(s=>!drops?.includes(s)))){clear();continue;}
    for(const scope of byId.filter(s=>scopes.has(s))){const ids=changes?changes.filter(c=>c.scope===scope).map(c=>c.id):null;
      // A change that names no id (or a refresh that does not say which) may be any record's.
      if(!ids?.length||ids.includes(null)){clear();break;}
      for(const [key,v] of [...g.entries])if((!answered||v.done)&&ids.some(id=>key.startsWith(id+'|')))g.entries.delete(key);}}
}
if(typeof window!=='undefined')window.addEventListener('rota-saved',e=>{const d=(e as CustomEvent).detail||{};
  if(!automatic(String(d.kind||''),d.payload))for(const g of keptGroups.values())g.entries.clear();});
const forgetKept=()=>{for(const g of keptGroups.values()){g.entries.clear();g.running.clear();g.queued.clear();}};
/** Any change of the signed-in identity starts with no diagnostics: a sign-out that failed or timed out leaves the
 *  previous account's trace in the feedback context (its session did not end), so a sign-in clears it too. */
const signedIn=<T>(reply:T):T=>{clearFeedbackContext();return reply;};
export const login=(username:string,password:string,departmentId?:string,rememberMe=false)=>request('/auth/login',{method:'POST',body:JSON.stringify({username,password,department_id:departmentId,remember_me:rememberMe})}).then(signedIn);
/** Which way this username or email signs in. Unknown names answer "password" (no account-exists signal). */
export const identify=(identifier:string)=>request<IdentifierResponse>('/auth/identify',{method:'POST',body:JSON.stringify({identifier})});
/** Institutional sign-in that needs a department (/auth/callback → ?signin_choice=…): the server holds the verified identity
 *  under the opaque continuation; these read its allowed departments, redeem one (cookies set then) or cancel it. */
export const oidcDepartments=(continuation:string)=>request<OIDCDepartmentChoices>('/auth/oidc/departments?'+new URLSearchParams({continuation}));
export const oidcChoose=(continuation:string,department_id:string)=>request<OIDCDepartmentSession>('/auth/oidc/department',{method:'POST',body:JSON.stringify({continuation,department_id} satisfies OIDCDepartmentChoice)}).then(signedIn);
export const oidcCancel=(continuation:string)=>request<{cancelled:boolean}>('/auth/oidc/cancel',{method:'POST',body:JSON.stringify({continuation} satisfies OIDCContinuation)});
/** Rotates the session's token pair (same session and deadline). Raw fetch: a refusal here is not a sign-out; the
 *  next ordinary read says so if the session really ended. */
export async function refreshSession(){try{return (await fetch('/auth/refresh',{method:'POST',headers:{'X-CSRF-Token':csrf(),Accept:'application/json'},credentials:'same-origin',cache:'no-store'})).ok;}catch{return false;/* the next read reports it */}}
/** maintenance: how long a sign-out waits for the server before it says it could not (the session may still be valid). */
export const SIGN_OUT_TIMEOUT_MS=20_000;
/** The server's word on a sign-out: ended (2xx, or 401 because the session had already gone), else not known to have ended. */
export type SignOutResult={ended:true;endSession?:string|null}|{ended:false};
let signOutRun:Promise<SignOutResult>|null=null;
/** The one sign-out request. Everything kept for the account is forgotten at once, before the server answers; a call while
 *  one is on its way joins it (one request). The session cookie is the server's (httpOnly): only its answer ends it.
 *  A stale CSRF token (a tab open over a token rotation) is renewed and the sign-out sent once more, as other writes do
 *  (ME-16); the time limit covers the renewal too. */
export function logout():Promise<SignOutResult>{
  if(signOutRun)return signOutRun;
  signingOut=true;clearSessionState();
  const stop=new AbortController(),timer=setTimeout(()=>stop.abort(),SIGN_OUT_TIMEOUT_MS);
  const send=()=>request<{end_session_url?:string|null}>('/auth/logout',{method:'POST',signal:stop.signal});
  const run=send().catch(async e=>{if(e instanceof ApiError&&e.code==='CSRF_FAILED'&&await refreshSession()&&!stop.signal.aborted)return send();throw e;})
    .then((r):SignOutResult=>({ended:true,endSession:r?.end_session_url}),(e):SignOutResult=>e instanceof ApiError&&e.status===401?{ended:true}:{ended:false})
    // The sign-out request is the account's last: once the session has ended, its trace leaves the feedback context too.
    .then(r=>{if(r.ended)clearFeedbackContext();return r;});
  const late=new Promise<SignOutResult>(done=>stop.signal.addEventListener('abort',()=>done({ended:false})));
  return signOutRun=Promise.race([run,late]).finally(()=>{clearTimeout(timer);signOutRun=null;signingOut=false;});
}
export async function instant(local:string):Promise<string>{return (await get<{instant:string}>(`/api/time/resolve?local=${encodeURIComponent(local)}`)).instant;}
/** Saves a server export; with no filename the server's Content-Disposition name is used. */
export async function download(path:string,filename=''){
  const generation=sessionGeneration;
  let response:Response;
  try{response=await track(fetch(path,{credentials:'same-origin',cache:'no-store'}));}catch(e){requireSession(generation);throw e;}
  requireSession(generation);
  if(!response.ok){const data=await response.json();requireSession(generation);throw new Error(data.error?.message||'Export failed');}
  const blob=await response.blob();requireSession(generation);
  const url=URL.createObjectURL(blob);const anchor=document.createElement('a');anchor.href=url;anchor.download=filename||/filename="([^"]+)"/.exec(response.headers.get('content-disposition')||'')?.[1]||'export';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
/** Module state that belongs to the signed-in person (staged Sheet cells, kept selections, cached limits): each module
 *  registers how to forget it, and a sign-out or account switch forgets it all with the rest of the session. */
const sessionResets=new Set<()=>void>();
export const onSessionReset=(forget:()=>void)=>{sessionResets.add(forget);};
export function clearSessionState(){sessionGeneration++;clearFeedbackContext();pending.clear();storePending();kept.clear();keptChars=0;forgetKept();revisions.clear();inflight.clear();undoBatch=[];window.clearTimeout(undoTimer);undoing=false;sessionResets.forEach(f=>f());}
