import {el,button,note,table,dateUnits,formatDate,formatTime,labelOf,namesWords} from './dom.js';
import {LEVEL_WORDS} from './issue_marker.js';
import {fullNameOf} from './person_label.js';
import {request,command,ApiError} from './api.js';
import type {Command} from './generated.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';
import {findingNumbers} from './findings.js';

export function previewFindings(ctx:Context,findings:Row[],shiftIds:string[]){
  const relevant=findings.filter(f=>shiftIds.includes(f.shift_id)),other=findings.filter(f=>!shiftIds.includes(f.shift_id));
  const rows=(items:Row[])=>table(['Person','When','Issue'],items.map(f=>[
    f.person_id?fullNameOf(ctx.catalogues.people.find(p=>p.id===f.person_id)):'Rota',
    f.start?dateUnits([formatDate(f.start,ctx.me.organisation.timezone)+' '+formatTime(f.start,ctx.me.organisation.timezone)]):'Period rule',
    (f.severity==='error'?LEVEL_WORDS.must:LEVEL_WORDS.could)+': '+[f.message,findingNumbers(f)].filter(Boolean).join(' · ')]));
  // Both shown under their own heading: the checks on the sessions changed first, then the rest of the rota's.
  return el('div',{},relevant.length?el('section',{class:'preview-findings'},el('h4',{},`Checks for changed sessions · ${relevant.length}`),rows(relevant)):null,
    other.length?el('section',{class:'preview-findings'},el('h4',{},`Other rota checks · ${other.length}`),rows(other)):null);
}

/** The server executes the same mutation inside a rolled-back savepoint.
 * Async local-time resolution and form changes must finish before a preview
 * becomes applicable. A failed mutation retains api.ts's exact retry receipt.
 */
/** `focus`: the sessions this change touches, whose checks lead a refusal (H-03). `without`: the form takes these people
 *  out and saves (the session editor, after PERSON_UNAVAILABLE). */
export function draftImpact(form:HTMLFormElement,ctx:Context,read:()=>Command|Promise<Command>,live=false,focus:()=>string[]=()=>[],without?:(people:string[])=>void){
  const output=el('div',{class:'span-2','aria-live':'polite'});
  let captured='',fingerprint='',epoch=0,running:Promise<void>|null=null,saving=false;
  // H-03: the values the server refused (a swap that would double-book someone). Asking again gives the same answer, so
  // Save sends nothing and brings the refusal already shown back into view, said again to a screen reader (the same alert,
  // inserted anew), never a second copy (an empty message: the form adds none). Changed values clear it.
  let refused='';
  const stillRefused=(now:string)=>{if(refused!==now)return;
    const said=output.querySelector('[role=alert]');if(said)output.prepend(said);
    output.scrollIntoView({block:'nearest'});throw new Error('');};
  const describe=(row:Row|null)=>{
    if(!row)return 'None';
    const zone=ctx.me.organisation.timezone;
    const names=row.participants.map((p:Row)=>fullNameOf(ctx.catalogues.people.find(person=>person.id===p.person_id))+(p.segments?.length?' [presence '+p.segments.map((part:Row)=>formatTime(part.start,zone)+'–'+formatTime(part.end,zone)).join('; ')+']':'')).join(', ')||'Unassigned';
    const location=labelOf(ctx.catalogues.locations.find(item=>item.id===row.location_id));
    return `${formatDate(row.start,zone)} ${formatTime(row.start,zone)} → ${formatDate(row.end,zone)} ${formatTime(row.end,zone)} · ${location} · ${names}${row.cancelled?' · Closed':''}${row.locked?' · Locked':''}`;
  };
  // One calculation at a time: asking again while one runs queues one more run after it (overlapping runs could
  // leave the form with no usable result).
  let again=false;
  const calc=()=>{
    if(running){again=true;return running;}
    running=(async()=>{do{again=false;await run();}while(again);})().finally(()=>{running=null;});
    return running;};
  const calculate=button('Calculate draft impact',calc,'secondary');
  async function run(){
    if(!form.reportValidity())return;
    captured='';fingerprint='';refused='';const started=epoch;let body='';
    output.replaceChildren(note('Calculating from current server inputs. Nothing has been saved.'));
    try{
      const value=await read();body=JSON.stringify(value);
      const result=await request<Row>('/api/planner/preview',{method:'POST',body});
      if(started!==epoch||JSON.stringify(await read())!==body){output.replaceChildren(note('Values changed during calculation. Recalculate the draft impact.','warning'));return;}
      captured=body;fingerprint=result.source_fingerprint;
      form.dispatchEvent(new Event('impact-calculated'));
      // A preview is neutral (never success green): nothing is saved until Save.
      output.replaceChildren(note(`Not saved yet: review what this changes, then Save. ${result.impact.counts.added} added, ${result.impact.counts.removed} removed, ${result.impact.counts.changed} changed; ${result.validation.error_count} mandatory findings remain.`,result.validation.valid?'info':'warning'),
        table(['Change','Before','After','Changed fields'],result.impact.changes.map((r:Row)=>[r.kind,describe(r.before),describe(r.after),r.fields.join(', ')||'Whole session'])));
      if(result.validation.findings.length)output.append(previewFindings(ctx,result.validation.findings,result.impact.changes.flatMap((r:Row)=>[r.before_id,r.after_id].filter(Boolean))));
    }catch(error){
      // A refusal (4xx) of these values: said with the checks on the sessions changed first, the first one in the lead.
      // For the values still in the form (a stray input event during the read is no change), as a preview's result is.
      const said=error instanceof ApiError&&error.status>=400&&error.status<500&&!!body&&JSON.stringify(await Promise.resolve(read()).catch(()=>null))===body;
      const findings:Row[]=said?((error.details as {findings?:Row[]}|undefined)?.findings||[]).filter(f=>f.severity==='error'):[];
      // A clash is kept on either of its two sessions: one on the other session counts too (detail.overlap).
      // D65: a refusal sends only the findings the change introduced (no detail.overlap): when none names a changed session
      // itself (the clash sits on the other duty of the person moved), they are still the change's own checks.
      const ids=focus(),near=findings.filter(f=>ids.includes(f.shift_id)||ids.includes(f.detail?.overlap?.other_shift_id)),touching=near.length?near:findings;
      if(said)refused=body;
      // The server's refusal as sent (it names the blocker first); who and when are in the checks under it.
      output.replaceChildren(note((error as Error).message,'error',error),...saveWithout(error),...(touching.length?[previewFindings(ctx,touching,touching.map(f=>f.shift_id))]:[]));}
  }
  // RUL-E04: PERSON_UNAVAILABLE names who only the fill panel's reason can admit (details.people, in the server's order);
  // this form has no reason, so it offers to save without them.
  const saveWithout=(error:unknown)=>{const ids=without&&error instanceof ApiError&&error.code==='PERSON_UNAVAILABLE'?((error.details as {people?:string[]}|undefined)?.people||[]):[];
    return ids.length?[button(`Save without ${namesWords(ids.map(id=>fullNameOf(ctx.catalogues.people.find(p=>p.id===id))))}`,()=>without!(ids),'secondary small')]:[];};
  const invalidate=()=>{epoch++;refused='';if(captured){captured='';fingerprint='';output.replaceChildren(note('Values changed. Recalculate draft impact.','warning'));}};
  form.addEventListener('input',invalidate);form.addEventListener('change',invalidate);
  // Live mode: recalculate a moment after each change while the form is valid. Still nothing is saved.
  if(live){let timer=0;const again=()=>{window.clearTimeout(timer);timer=window.setTimeout(()=>{if(!saving&&form.isConnected&&!calculate.disabled&&form.checkValidity())calculate.click();},900);};
    form.addEventListener('input',again);form.addEventListener('change',again);form.addEventListener('participants-changed',again);window.setTimeout(again,50);}
  form.append(el('section',{class:'span-2 form-section'},el('h3',{},'What this changes'),
    note('Problems can be fixed later in the draft, but they block submitting it for review.'),calculate,output));
  return async()=>{
    // D-077: a draft edit saves on one press. The preview is optional: the server refuses what it will not admit, keeps
    // the rest as findings that block submission, and the edit is undoable. A preview on screen for exactly these values
    // sends its fingerprint (what you saw is what you save); otherwise none. Live recalculation does not start meanwhile.
    saving=true;
    try{
      const started=epoch,value=await read(),now=JSON.stringify(value);stillRefused(now);
      const seen=!running&&started===epoch&&!!fingerprint&&now===captured;
      return await command(value.kind,value.payload,value.target_id??undefined,value.expected_revision??undefined,seen?fingerprint:undefined).catch(error=>{
        // Said where a preview says it, with who to save without; an empty message: the form adds no second copy.
        const who=saveWithout(error);if(!who.length)throw error;
        output.replaceChildren(note((error as Error).message,'error',error),...who);refused=now;output.scrollIntoView({block:'nearest'});throw new Error('');});
    }finally{saving=false;}
  };
}
