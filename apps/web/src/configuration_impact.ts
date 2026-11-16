import {el,button,icon,iconButton,popover,note,notify,table,plural} from './dom.js';
import {request,command} from './api.js';
import type {Command,CatalogueConflictDetails,TimelineConflict} from './generated.js';
import {routeHash} from './routes.js';

/** Findings counted once per cause: the same code AND explanation (one code can carry different explanations, e.g.
 *  "Morning cover" and "Night cover"), within `by` (a rota), most frequent first. */
export function groupCauses<F extends {code?:string|null;message:string}>(found:F[],by:(f:F)=>string=()=>''):{f:F;n:number}[]{
  const causes=new Map<string,{f:F;n:number}>();
  for(const f of found){const k=JSON.stringify([by(f),f.code||'',f.message]),c=causes.get(k);if(c)c.n++;else causes.set(k,{f,n:1});}
  return [...causes.values()].sort((a,b)=>b.n-a.n);
}
/** A rotation saved over leave (allowed, but a clash): the save says so, explains it and opens the Programmes timeline,
 *  where the clash is drawn and the leave or the rotation can be moved. One message for the whole save. */
function leaveClashes(found:TimelineConflict[]){
  if(!found.length)return;
  const said=[...new Set(found.map(f=>f.message))];
  notify(`Saved with ${plural(found.length,'leave clash','leave clashes')}: ${said.join(' ')} The rotation and the leave both stay; move one of them on the Programmes timeline.`,
    'warning',{label:'Open timeline',run:()=>{location.hash=routeHash('programmes',['board']);}});
}

/** Adds an explicit read-only impact step. The exact form values are bound on apply.
 *  `read` may return several commands (the same record for many people): each is previewed and saved with its own
 *  fingerprint, in turn, and `names` labels each one in the preview. `finalize` adds what only the save carries (a
 *  recurring session's double-booking acknowledgement), so it never changes the previewed values. */
export function configurationImpact(form:HTMLFormElement,read:()=>Command|Command[],names?:()=>string[],finalize:(c:Command)=>Command=c=>c){
  const output=el('div',{class:'configuration-impact-output','aria-live':'polite'});
  const many=()=>{const r=read();return Array.isArray(r)?r:[r];};
  let captured='',fingerprints:string[]=[],harmless=false;
  const show=(result:any,into:HTMLElement)=>{
    const scope=iconButton('What this preview checks','help',()=>popover(scope,panel=>{
      panel.append(el('p',{},result.scope),el('p',{},result.message));
      if(result.inventory_only)panel.append(el('p',{},'Saving this configuration does not create or update duties.'));
    },{label:'What this preview checks'}),{class:'bare'});
    const introduced=result.impacts.flatMap((r:any)=>r.introduced.map((f:any)=>({...f,rota:r.name})));
    const errors=introduced.filter((f:any)=>f.severity==='error'),warnings=introduced.filter((f:any)=>f.severity==='warning');
    const resolved=result.impacts.reduce((n:number,r:any)=>n+r.resolved.length,0);
    const remainingErrors=result.impacts.reduce((n:number,r:any)=>n+r.after_error_count,0),remainingWarnings=result.impacts.reduce((n:number,r:any)=>n+r.after_warning_count,0);
    const checked=result.partial?'Impact not calculated.':result.impacts.length?`${plural(result.workspace_count,'active rota')} checked.`:'Configuration checked.'+(result.inventory_only?' Existing duties stay unchanged.':'');
    into.append(el('div',{class:'actions'},el('p',{class:'configuration-impact-status'},checked+
      (result.impacts.length&&!introduced.length&&!resolved?' No finding changes.':'')),scope));
    if(result.partial)into.append(note(`${plural(result.session_count,'upcoming session')} in ${plural(result.workspace_count,'rota')} are more than this check covers. Save again to apply; each rota is still validated.`,'warning'));
    if(!result.impacts.length)return;
    // Finding identity matters: replacing one mandatory error with another can leave the totals unchanged.
    if(errors.length){
      // One line per cause and rota ("November block · Book the complete configured duration · 155 sessions"), not
      // one per session: the same root cause repeated hundreds of times hid what to change.
      const conflicts=note(`${errors.length} new mandatory conflict${errors.length===1?'':'s'}. Review before saving.`,'error');
      conflicts.append(el('ul',{},...groupCauses(errors as any[],f=>f.rota).map(({f,n})=>el('li',{},`${f.rota} · ${f.message}${n>1?` · ${plural(n,'time')}`:''}`))));into.append(conflicts);
    }
    const changes=[warnings.length?`${warnings.length} new warning${warnings.length===1?'':'s'}`:'',resolved?`${resolved} finding${resolved===1?'':'s'} resolved`:''].filter(Boolean);
    if(changes.length)into.append(el('p',{class:'configuration-impact-status'},changes.join(' · ')+'.'));
    const remaining=[remainingErrors?`${remainingErrors} mandatory conflict${remainingErrors===1?' remains':'s remain'}`:'',remainingWarnings?`${remainingWarnings} warning${remainingWarnings===1?' remains':'s remain'}`:''].filter(Boolean);
    if(remaining.length)into.append(el('p',{class:'configuration-impact-status'},remaining.join(' · ')+'.'));
    // Shown, not folded away: what each rota's check was before and after, then each finding that came or went.
    const comparison=el('section',{class:'configuration-impact-comparison'},el('h4',{},'Rota comparison'),
      table(['Rota','State','Errors before → after','Warnings before → after'],result.impacts.map((r:any)=>[r.name,r.status,`${r.before_error_count} → ${r.after_error_count}`,`${r.before_warning_count} → ${r.after_warning_count}`])));
    // One row per change and cause, with how often (500 sessions of one cause are one row, not 500: AD-8).
    const grouped=(change:string,found:any[])=>groupCauses(found).map(({f,n})=>[change,n>1?`${f.message} · ${plural(n,'time')}`:f.message]);
    for(const impact of result.impacts){
      if(impact.introduced.length||impact.resolved.length)comparison.append(el('div',{},el('h4',{},impact.name+' · finding changes'),
        table(['Change','Explanation'],[...grouped('Introduced',impact.introduced),...grouped('Resolved',impact.resolved)])));
    }
    into.append(comparison);
  };
  // Calculated as the form is filled in (once it is valid, after a short pause), in every form layout; the button
  // asks again on demand. Save stays refused until the impact shown matches the current values.
  let running:Promise<void>|null=null,timer=0;
  const run=async(asked:boolean)=>{
    if(!(asked?form.reportValidity():form.checkValidity()))return;
    if(running){await running;if(captured===JSON.stringify(many()))return;}
    // An automatic run keeps the completed impact for these values; Calculate again still refreshes it.
    if(!asked&&captured===JSON.stringify(many()))return;
    running=calculateNow();try{await running;}finally{running=null;}
  };
  // A form closed before the pause ends asks nothing (AD-9).
  const soon=()=>{window.clearTimeout(timer);timer=window.setTimeout(()=>{if(form.isConnected)void run(false);},500);};
  const calculate=button('Calculate again',()=>run(true),'secondary',{class:'icon-button bare','aria-label':'Calculate again','data-tip':'Calculate again'});
  calculate.replaceChildren(icon('reset'));
  async function calculateNow(){
    const values=many(),body=JSON.stringify(values),labels=names?.()||[];calculate.disabled=true;captured='';fingerprints=[];harmless=false;
    if(!values.length){output.replaceChildren(note('Choose at least one person first.','warning'));calculate.disabled=false;return;}
    // The result never shrinks the form under the pointer (an armed button above would move away): the space it
    // took is kept until the values change. Checking is one line, like the plainest result.
    output.style.setProperty('min-height',output.offsetHeight+'px');
    output.replaceChildren(el('p',{class:'configuration-impact-status'},'Checking the impact. Nothing has been saved.'));
    try{
      const results=[];
      for(const value of values)results.push(await request<any>('/api/catalogue/preview',{method:'POST',body:JSON.stringify(value)}));
      if(JSON.stringify(many())!==body){output.replaceChildren(note('Calculating again for the values just changed. Nothing has been saved.'));soon();return;}
      captured=body;fingerprints=results.map(r=>r.source_fingerprint);
      // Nothing to read: no rota checked partly, no new conflict or warning. Save may go ahead on the click that asked.
      harmless=results.every(r=>!r.partial&&r.impacts.every((i:any)=>!i.introduced.length));
      form.dispatchEvent(new Event('impact-calculated'));
      output.replaceChildren();
      if(results.length===1)show(results[0],output);
      else{
        output.append(el('p',{class:'configuration-impact-status'},`${results.length} records will be saved.`));
        // One labelled section per record, every one shown (a record with nothing to review says so in one line).
        results.forEach((r,i)=>{const part=el('section',{class:'configuration-impact-record'},el('h4',{},labels[i]||`Record ${i+1}`));show(r,part);output.append(part);});
      }
    }catch(error){output.replaceChildren(note((error as Error).message,'error',error));}
    finally{calculate.disabled=false;}
  }
  form.append(el('section',{class:'span-2 form-section configuration-impact'},el('h3',{class:'sr-only'},'Review configuration consequences'),
    el('div',{class:'configuration-impact-body'},output,calculate)));
  // A change event without a new value (leaving a field) keeps the impact shown.
  const invalidate=()=>{if(captured&&captured===JSON.stringify(many()))return;
    output.style.removeProperty('min-height');
    if(captured){captured='';fingerprints=[];output.replaceChildren(note('Values changed: working out the impact again.'));}soon();};
  form.addEventListener('input',invalidate);form.addEventListener('change',invalidate);
  soon();  // a form that opens valid (an edit) shows its impact without a click
  return async()=>{
    const values=many();
    if(!fingerprints.length||JSON.stringify(values)!==captured){
      // Save before the impact was shown (or while it was still being worked out, as on a slow server right after the
      // last keystroke): work it out now. A harmless result for exactly these values saves on this same click; anything
      // to read (a new conflict or warning, a partial check) is shown first and needs a second click.
      window.clearTimeout(timer);await run(true);
      const current=captured===JSON.stringify(many())&&fingerprints.length===values.length;
      // Not worked out (the check itself says why, e.g. a server rule such as the one-year limit): no second, generic error.
      if(!current){output.scrollIntoView({block:'nearest'});throw new Error('');}
      if(!harmless)throw new Error('Check the configuration impact above, then save.');}
    let n=0;const clashes:TimelineConflict[]=[];
    for(const [i,value] of values.entries()){
      // Each saved record changes the shared inputs, so later records are re-checked just before saving: the only
      // change since the reviewed preview is this same batch.
      try{const fp=i===0?fingerprints[0]:(await request<any>('/api/catalogue/preview',{method:'POST',body:JSON.stringify(value)})).source_fingerprint;
        const sent=finalize(value),saved:CatalogueConflictDetails=await command(sent.kind,sent.payload,sent.target_id??undefined,sent.expected_revision??undefined,fp);n++;
        clashes.push(...saved?.rotation_leave_conflicts||[]);}
      catch(e){if(values.length===1)throw e;captured='';fingerprints=[];leaveClashes(clashes);throw new Error(`${n} of ${values.length} saved. Stopped: ${(e as Error).message}`);}
    }
    leaveClashes(clashes);
    return n;
  };
}
