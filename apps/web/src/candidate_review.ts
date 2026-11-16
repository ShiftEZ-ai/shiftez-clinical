import {get} from './api.js';
import {button,el,note,readFailed,showDialog,table,dateUnits,formatDate,formatTime,PAGE_WHY} from './dom.js';
import type {Row} from './types.js';
import {EXCLUSION_WORDS} from './planning_context.js';

// shortcut: same one-liner as app.ts lastIncludedDate; move both to dom.ts once maintenance/maintenance (which edit dom.ts/app.ts) land.
const lastDay=(exclusive:string)=>new Date(Date.parse(exclusive.slice(0,10)+'T12:00:00Z')-86400000).toISOString().slice(0,10);
/** When a reason applies, as people read it: leave as day and time, a rotation as its days (its end date is exclusive). */
function reasonWhen(reason:Row,zone:string):string{
  if(reason.start&&reason.end)return `${formatDate(reason.start,zone)} ${formatTime(reason.start,zone)} – ${formatDate(reason.end,zone)} ${formatTime(reason.end,zone)}`;
  if(reason.starts_on)return `${formatDate(reason.starts_on)} – ${reason.ends_on?formatDate(lastDay(reason.ends_on)):'no end'}`;
  return '';
}

export function diagnosticContext(item:Row,name:(kind:string,id:string)=>string,zone:string){
  const content=el('section',{},el('strong',{},[item.requirement_name,item.capability_name,...(item.service_ids||[]).map((id:string)=>name('services',id))].filter(Boolean).join(' · ')||'Scheduling requirement'));
  if(item.start&&item.end)content.append(el('p',{},`${formatDate(item.start,zone)} · ${formatTime(item.start,zone)}–${formatTime(item.end,zone)}`));
  if(item.required!=null)content.append(el('p',{},`Needs ${item.required} · ${Number(item.available_upper_bound)===0?'nobody can work it':`at most ${item.available_upper_bound} can work it`}`));
  return content;
}

export function candidateReview(jobId:string,name:(kind:string,id:string)=>string,zone:string,diagnosticIndex?:number,phase?:string,diagnostic?:Row){
  showDialog('Candidates considered','Evidence from this scheduling run. Eligibility and availability alone do not establish that every scheduling rule can be met.',body=>{
    if(diagnostic)body.append(diagnosticContext(diagnostic,name,zone));
    let offset=0;
    const status=el('p',{role:'status','aria-live':'polite'}),summary=el('div'),rows=el('div'),nav=el('div',{class:'actions'});
    body.append(summary,status,rows,nav);
    const labels:Record<string,string>=EXCLUSION_WORDS;
    const load=async()=>{
      for(const control of nav.querySelectorAll('button'))control.disabled=true;
      try{
        const query=new URLSearchParams({offset:String(offset),limit:'50'});
        if(diagnosticIndex!==undefined)query.set('diagnostic_index',String(diagnosticIndex));
        if(phase)query.set('phase',phase);
        const page=await get('/api/jobs/'+jobId+'/evidence?'+query);
        if(!body.isConnected)return;
        status.textContent=page.total?`${offset+1}–${Math.min(offset+50,page.total)} of ${page.total} ${diagnostic?'people checked':'person and session checks'}`:'No candidate evidence recorded.';
        // The reasons, counted over everyone checked, before the list.
        const reasons=Object.entries((page.reasons||{}) as Record<string,number>).sort((a,b)=>b[1]-a[1]);
        summary.replaceChildren(...(reasons.length||page.eligible?[el('ul',{class:'why-list'},
          ...(page.eligible?[el('li',{},el('strong',{},String(page.eligible)),' could work it (other rules still apply)')]:[]),
          ...reasons.map(([code,n])=>el('li',{},el('strong',{},String(n)),' '+(labels[code]||code.replaceAll('_',' ').toLowerCase()))))]:[]));
        // One shortage: its time is said once above, so the column shows only where a person was checked for another interval.
        // The whole run: every row is a different session, so each says its day and time.
        const own=(c:Row)=>!!diagnostic?.start&&(!c.start||(c.start===diagnostic.start&&c.end===diagnostic.end));
        const showTime=!page.candidates.every(own);
        rows.replaceChildren(table(['Person',...(showTime?['Session']:[]),'Why'],page.candidates.map((candidate:Row)=>[
          name('people',candidate.person_id),...(showTime?[(candidate.start||diagnostic?.start)?dateUnits([formatDate(candidate.start||diagnostic!.start,zone),`${formatTime(candidate.start||diagnostic!.start,zone)}–${formatTime(candidate.end||diagnostic!.end,zone)}`]):'Interval not recorded']:[]),
          candidate.exclusions.length?el('div',{},...candidate.exclusions.map((reason:Row)=>{
            // The reason, its rule or skill by name and when it applies, in one visible line (no ids or revisions).
            const label=(labels[reason.code]||reason.code.replaceAll('_',' ').toLowerCase())+(reason.rule_id?' · '+name('policies',reason.rule_id):'')+(reason.capability_id?' · '+name('capabilities',reason.capability_id):'');
            const when=reasonWhen(reason,zone);
            return el('div',{},label,when?el('span',{class:'muted'},' · ',dateUnits([when])):'');
          })):'Eligible and available; other rules still apply'
        ])));
        nav.replaceChildren(button('Previous candidates',async()=>{offset-=50;await load();},'secondary',{disabled:offset===0,why:PAGE_WHY.first}),button('Next candidates',async()=>{offset+=50;await load();},'secondary',{disabled:!page.has_more,why:PAGE_WHY.last}));
      }catch(error){status.textContent='';rows.replaceChildren(readFailed('the evidence',error,()=>void load()));nav.replaceChildren();}
    };
    void load();
  });
}
