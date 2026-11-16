/** Schedule › Compare your rota with Rota (score check, PROPOSAL.md §5, mockup v3 on the "Rota Score check" canvas):
 *  Upload → Check → Compare at #schedule/benchmark/<step>. Upload reads the department's own rota files into an imported
 *  draft; Check asks only what the reading was unsure about; Compare scores that draft against Rota's auto-schedule with
 *  the same rules. Every server call goes through scoreCheckApi.ts. */
import {brand} from './brand.js';
import {LEVEL_WORDS} from './issue_marker.js';
import {mustWords} from './fix_list.js';
import {el,badge,icon,button,iconButton,withIcon,note,notify,empty,plural,popover,menuItem,field,footerStatus,tabs,showDialog,table,
  formatDay,announce,departmentToday,type Child,type Choice,PAGE_WHY} from './dom.js';
import {ApiError} from './api.js';
import {columnSizing} from './table_menu.js';
import {openPicker} from './picker.js';
import * as api from './scoreCheckApi.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)

export type Step='upload'|'check'|'compare';
import {dockPanel} from './side_panel.js';  // lists are docked panels (UX › controls C26)
export interface ScoreCheckHooks{
  step:Step;query:URLSearchParams;zone:string;
  /** Coordinators upload, check and submit; approvers, programme leads and auditors read Compare. */
  canEdit:boolean;canRead:boolean;canAddMissing:boolean;
  go:(step:Step,query:Record<string,string>)=>void;leave:()=>void;
  setLeave:(dirty:(()=>boolean|string)|null)=>void;
  /** Names Rota knows, for Match… (people, duty types, clinics, locations). */
  choices:(kind:api.CheckItem['kind']|'location')=>Choice[];
  openIssues:(workspaceId:string)=>void;submit:(workspaceId:string)=>void;
  /** Rota's same-session draft of an imported rota (score_check.auto), then its Auto-schedule; rerun opens an existing one. */
  autoSchedule:(importedId:string)=>void;rerun:(autoId:string)=>void;
  openRules:(workspaceId:string)=>void;openScoring:()=>void;
}

// Without AI only spreadsheets are read (deterministic CSV, TSV and Excel); an AI provider also reads PDFs and photos.
const SHEETS='.xlsx,.csv,.tsv,text/csv,text/tab-separated-values',ANY=SHEETS+',.pdf,.png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp,application/pdf';
const EXT:Record<api.ImportSource['format'],string>={xlsx:'XLSX',csv:'CSV',pdf:'PDF',image:'IMG'};
const day=(iso:string,o:Intl.DateTimeFormatOptions)=>new Date(iso.slice(0,10)+'T12:00:00Z').toLocaleDateString('en-GB',{...o,timeZone:'UTC'});
const monthLabel=(iso:string)=>day(iso,{month:'long',year:'numeric'});
const firstOfNext=(iso:string)=>new Date(Date.UTC(+iso.slice(0,4),+iso.slice(5,7),1)).toISOString().slice(0,10);
/** A period in words: a whole month by its name, else its first and last day. Rota periods end exclusively, so the day
 *  shown last is the one before `end` (November is 2026-11-01 → 2026-12-01). */
export function periodLabel(start:string|null,end:string|null){
  if(!start||!end)return 'Dates unknown';
  start=start.slice(0,10);end=end.slice(0,10);
  const last=new Date(Date.parse(end+'T12:00:00Z')-86400000).toISOString().slice(0,10);
  if(start.endsWith('-01')&&end===firstOfNext(start))return monthLabel(start);
  return start===last?day(start,{day:'numeric',month:'short',year:'numeric'}):`${day(start,{day:'numeric',month:'short'})} – ${day(last,{day:'numeric',month:'short',year:'numeric'})}`;
}
/** The page's name (its heading, the Schedule's action and command): the product named by its short name. */
export const scoreCheckTitle=()=>`Compare your rota with ${brand().short_name}`;
/** A side in words: the department's own rota and Rota's, never their draft names. */
export const sideName=(s:Pick<api.BenchmarkSide,'origin'|'name'>)=>s.origin==='import'?'Your rota':s.origin==='auto'?brand().short_name:s.name;
/** The answer first, without overclaiming: a winner only when both scores are known, they differ, and the higher one
 *  has no more blocking issues than the other. Otherwise a tie, an unknown, or no clear winner. */
export function verdict(theirs:api.BenchmarkSide,ours:api.BenchmarkSide):{winner:0|1|null;text:string}{
  if(theirs.score===null||ours.score===null)return {winner:null,text:'The scores are not available yet'};
  if(theirs.score===ours.score)return {winner:null,text:'Both rotas score the same'};
  const w:0|1=ours.score>theirs.score?1:0,win=w?ours:theirs,lose=w?theirs:ours;
  if(win.blocking>lose.blocking)return {winner:null,text:'No clear winner: the higher score has more to fix'};
  const who=w?`${brand().short_name}’s rota`:'Your rota';
  return {winner:w,text:win.blocking===0?`${who} scores higher, with nothing to fix`:`${who} scores higher`};
}
/** A change made from a picker or popover (outside button(), which reports its own errors): a refusal is said in the
 *  shared toast; a stale revision offers Reload, which reads the record again. */
function act(run:()=>Promise<unknown>,reload?:()=>Promise<unknown>){
  return ()=>{void run().catch(e=>{
    const stale=e instanceof ApiError&&(e.code.startsWith('STALE_')||e.status===409);
    notify(stale?'This changed elsewhere meanwhile. Reload to see the latest, then try again.':(e as Error).message,'error',
      stale&&reload?{label:'Reload',run:reload}:undefined,e);});};
}

function steps(at:Step,h:ScoreCheckHooks,importId:string){
  const list:[Step,string][]=[['upload','Upload'],['check','Check'],['compare','Compare']],i=list.findIndex(([s])=>s===at);
  return el('ol',{class:'step-rail bm-steps','aria-label':'Steps'},...list.map(([s,label],n)=>{
    const passed=n<i,num=el('span',{class:'step-num'},passed?icon('check'):String(n+1));
    const cls='step-link'+(n===i?' current':passed?' passed':'');
    // Steps already passed can be revisited; the current and later ones are plain text.
    return el('li',{},passed&&importId&&s!=='compare'?el('button',{type:'button',class:cls,onclick:()=>h.go(s,{import:importId})},num,label)
      :el('span',{class:cls,...(n===i?{'aria-current':'step'}:{})},num,label));}));
}
/** The page's header, lifted into the top bar (app.ts liftHeader): back to the Planner and the steps beside the page
 *  title, the actions on the right. No heading of its own: the page's h1 is the top bar's. */
function head(h:ScoreCheckHooks,at:Step,importId:string,...actions:Child[]){
  const back=el('a',{class:'back bm-back',href:'#schedule'},icon('chevronLeft'),'Planner');
  return el('div',{class:'section-head'},el('div',{class:'bm-context','data-page-context':''},back,steps(at,h,importId)),el('div',{class:'actions'},...actions));
}
const title=(text:string,sub='')=>el('div',{class:'bm-head'},el('h2',{class:'bm-title'},text),sub?el('p',{class:'bm-sub'},sub):null);
const restricted=(t:HTMLElement,h:ScoreCheckHooks,what:string)=>{t.append(empty('Restricted page',what,button('Go to the schedule',h.leave,'secondary'),'lock'));};
/** A button that opens the shared picker and shows its value (inline edits and the Fix… fields). */
function pickerButton(cls:string,label:string,value:string,aria:string,choices:()=>Choice[],current:string,pick:(v:string)=>void){
  const b=el('button',{type:'button',class:cls,...popup('dialog'),'aria-label':`${aria}: ${value}. Change`},value);
  b.addEventListener('click',()=>openPicker(b,b.parentElement||document.body,{label,multiple:false,choices:choices(),selected:new Set([current]),recent:false,onPick:pick}));
  return b;
}

export async function scoreCheckPage(target:HTMLElement,h:ScoreCheckHooks){
  if(h.step==='compare'){if(!h.canRead&&!h.canEdit)return restricted(target,h,'Comparing rotas is for the people who plan and review rotas.');return comparePage(target,h);}
  if(!h.canEdit)return restricted(target,h,'Uploading and checking a department rota is for coordinators.');
  return h.step==='check'?checkPage(target,h):uploadPage(target,h);
}

// ---------------------------------------------------------------- Upload
async function uploadPage(target:HTMLElement,h:ScoreCheckHooks){
  const id=h.query.get('import')||'';
  const providers=await api.providersForImport();
  let imp=id?await api.readImport(id):null,timer=0;
  const col=el('div',{class:'bm-col'});target.append(col);
  const picker=el('input',{type:'file',multiple:true,accept:SHEETS,class:'bm-file-input',tabindex:-1,'aria-hidden':'true'}) as HTMLInputElement;
  const choose=()=>picker.click();
  // The provider that reads the files: chosen (and its notice shown) BEFORE any file is sent; then the import's own.
  let chosen=providers.find(p=>p.is_default)||null;
  const profile=()=>imp?providers.find(p=>p.id===imp!.profile_id)||null:chosen;
  /** The import's provider is no longer offered (switched off or removed): its earlier files did go there. */
  const gone=()=>!!imp?.profile_id&&!providers.some(p=>p.id===imp!.profile_id);
  const reload=async()=>{if(imp){imp=await api.readImport(imp.id);update();}};
  let sending=false;
  const take=async(files:File[])=>{if(!files.length||sending)return;sending=true;
    try{if(!imp){const created=await api.startImport(files,profile());h.go('upload',{import:created.id});return;}
      imp=await api.addFiles(imp,files,profile());update();}finally{sending=false;}};
  picker.addEventListener('change',()=>{act(()=>take([...picker.files||[]]),reload)();picker.value='';});
  /** A drop zone is a real button (keyboard and screen readers) that also takes dropped files. */
  const dropZone=(big:boolean)=>{const zone=el('button',{type:'button',class:'bm-drop'+(big?'':' compact'),onclick:choose},
      el('span',{class:'bm-drop-ico','aria-hidden':'true'},icon('upload')),
      el('b',{},big?'Drop your rota files here':'Drop more files here'),
      el('small',{},big?(profile()?'Excel, CSV, PDF or photos.':'Excel or CSV files.')+' Several files at once is fine.':'or choose files'),
      big?el('span',{class:'button small','aria-hidden':'true'},'Choose files'):null);
    zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('over');});
    zone.addEventListener('dragleave',()=>zone.classList.remove('over'));
    zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('over');act(()=>take([...e.dataTransfer?.files||[]]),reload)();});
    return zone;};
  const reading=()=>!!imp?.sources.some(s=>s.status==='reading');
  // While files are read, the import is read again every 2 s and only what changed is patched (update), so focus,
  // an open picker and the screen reader's place survive.
  const poll=()=>{window.clearTimeout(timer);if(!reading()||!imp)return;
    timer=window.setTimeout(async()=>{if(!col.isConnected)return;const next=await api.readImport(imp!.id).catch(()=>null);
      if(next&&col.isConnected){imp=next;update();}else poll();},2000);};
  const edit=(s:api.ImportSource,patch:{group?:string;period_start?:string;period_end?:string})=>act(async()=>{imp=await api.updateSource(imp!,s.id,patch);update();announce('Saved.');},reload);
  /** Months two years back and one ahead, plus the file's own period when it is not a whole month in that range. */
  const months=(s:api.ImportSource)=>{const out:Choice[]=[],now=new Date();
    for(let k=-24;k<=12;k++){const start=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth()+k,1)).toISOString().slice(0,10);
      out.push({value:start+'/'+firstOfNext(start),label:monthLabel(start)});}
    const own=`${s.period_start}/${s.period_end}`;
    if(s.period_start&&s.period_end&&!out.some(c=>c.value===own))out.unshift({value:own,label:periodLabel(s.period_start,s.period_end),hint:'As read'});
    return out;};
  const status=(s:api.ImportSource):HTMLElement=>s.status==='ready'?el('span',{class:'bm-ready'},icon('check'),'Ready')
    :s.status==='needs_check'?badge(plural(s.unclear_cells,'cell')+' to check','warn','',{icon:'alert'})
    :s.status==='failed'?badge('Not readable','err','',{icon:'alert',tip:s.error||''})
    :el('span',{class:'bm-reading',role:'status'},el('span',{class:'bm-progress','aria-hidden':'true'},el('i',{})),'Reading');
  /** What was understood about a file, as inline edits. The buttons stay the same elements for the file's life (a poll
   *  only relabels them, fillSaid), so an open Group or Month picker keeps its anchor while the file is read. */
  const src=(id:string)=>imp!.sources.find(x=>x.id===id);
  const said=(s0:api.ImportSource)=>{const id=s0.id;
    const group=el('button',{type:'button',class:'bm-edit','data-edit':'group','aria-haspopup':'listbox'});
    group.addEventListener('click',()=>{const s=src(id);if(!s)return;openPicker(group,group.parentElement||document.body,{label:'Group',multiple:false,recent:false,
      choices:imp!.groups.map(g=>({value:g,label:g})),selected:new Set([s.group]),onPick:g=>edit(s,{group:g})()});});
    const month=el('button',{type:'button',class:'bm-edit','data-edit':'month','aria-haspopup':'listbox'});
    month.addEventListener('click',()=>{const s=src(id);if(!s)return;openPicker(month,month.parentElement||document.body,{label:'Month',multiple:false,recent:false,
      choices:months(s),selected:new Set([`${s.period_start}/${s.period_end}`]),onPick:v=>{const [a,b]=v.split('/');edit(s,{period_start:a,period_end:b})();}});});
    const wrap=el('div',{class:'bm-said'},group,el('span',{class:'bm-sep','aria-hidden':'true'},'·'),month,el('span',{class:'bm-sep','aria-hidden':'true'},'·'),el('span',{'data-people':''}));
    fillSaid(wrap,s0);return wrap;};
  const relabel=(b:Element|null,text:string,aria:string)=>{if(!b)return;if(b.textContent!==text)b.textContent=text;const name=`${aria}: ${text}. Change`;if(b.getAttribute('aria-label')!==name)b.setAttribute('aria-label',name);};
  const fillSaid=(wrap:Element,s:api.ImportSource)=>{relabel(wrap.querySelector('[data-edit=group]'),s.group||'No group','Group');
    relabel(wrap.querySelector('[data-edit=month]'),periodLabel(s.period_start,s.period_end),'Month');
    const people=wrap.querySelector('[data-people]')!,text=plural(s.people_count,'person','people');if(people.textContent!==text)people.textContent=text;};
  const remove=(s:api.ImportSource)=>iconButton('Remove '+s.file_name,'close',act(async()=>{
    imp=await api.removeSource(imp!,s.id);update();notify(s.file_name+' removed.');},reload));
  const item=(s:api.ImportSource)=>el('div',{class:'bm-item','data-source':s.id},el('span',{class:'bm-ext','aria-hidden':'true'},EXT[s.format]),
    el('div',{class:'bm-item-main'},el('div',{class:'bm-name'},s.file_name),said(s)),el('div',{class:'bm-status'},status(s)),remove(s));
  /** The time button lives as long as the list: a poll relabels it only when the time changed (timeLabel), so an open
   *  Time popover keeps its anchor and focus. The popover reads the time when it opens. */
  const timeLabel=()=>{const t=imp!.time;return t?`${t.work_hours} hours by ${plural(t.people,'person','people')}`:'Add time';};
  const timeButton=()=>{
    const b=button(timeLabel(),()=>{const t=imp!.time;
      popover(b,(panel,close)=>{const hours=field('Work hours in total','number',t?.work_hours??'',{min:0,step:.5,required:true}),
        people=field('People who made them','number',t?.people??'',{min:1,step:1,required:true});
        const form=el('form',{class:'bm-time-form'},hours.wrap,people.wrap,el('div',{class:'actions'},
          t?button('Clear',async()=>{imp=await api.setTime(imp!,null);close();update();},'ghost small'):null,
          el('button',{type:'submit',class:'button primary small'},'Save')));
        form.addEventListener('submit',e=>{e.preventDefault();if(!form.reportValidity())return;
          act(async()=>{imp=await api.setTime(imp!,{work_hours:Number(hours.input.value),people:Number(people.input.value)});close();update();},reload)();});
        panel.append(form);hours.input.focus();},{label:'Time it took'});},'small',{...popup('dialog'),'data-time':''});
    return b;};
  const readBy=()=>{if(!providers.length&&!gone())return [el('span',{class:'bm-privacy'},icon('lock'),'Read without AI: CSV and Excel in the standard layout'),el('span',{})];
    const p=profile(),NONE='none';
    // Choosing a provider acknowledges where the files go: each choice says it.
    const b=button('',()=>openPicker(b,b.parentElement||document.body,{label:'Read by',multiple:false,recent:false,selected:new Set([p?.id||NONE]),
      choices:[...providers.map(x=>({value:x.id,label:x.name,hint:api.sendsTo(x)})),{value:NONE,label:'Without AI',hint:'CSV and Excel in the standard layout'}],
      onPick:v=>{const next=providers.find(x=>x.id===v)||null;if(!imp){chosen=next;update();return;}
        act(async()=>{imp=await api.chooseProfile(imp!,next);update();},reload)();}}),'ghost small',{...popup('dialog')});
    b.replaceChildren(el('span',{class:'bm-muted'},'Read by '),p?.name||(gone()?'Unavailable provider':'Without AI'),icon('chevronDown'));
    return [el('span',{class:'bm-privacy'},icon('lock'),p?api.sendsTo(p):gone()?'Earlier files were read by a provider that is no longer available':`Files stay in ${brand().short_name}`),b];};
  const cancel=button('Cancel',async()=>{if(imp){await api.discardImport(imp);notify('Files discarded.');}h.setLeave(null);h.leave();},'ghost');
  const cont=button('Continue',()=>{h.setLeave(null);h.go('check',{import:imp!.id});},'primary');
  let shape='';
  /** Patch what changed; rebuild only when the set of files or the provider changed. Each row is keyed by its file. */
  function update(){
    const files=imp?.sources||[],busy=reading();
    cont.disabled=!files.length||busy;cont.textContent=busy?`Reading ${plural(files.filter(s=>s.status==='reading').length,'file')}…`:'Continue';
    // Leaving keeps the uploaded files in this import (not lost): the guard says so instead of "Discard changes".
    h.setLeave(files.length?()=>'Your files stay in this import, but it is not saved as a draft until you finish Check.':null);
    picker.accept=profile()?ANY:SHEETS;  // PDFs and photos need an AI reader; spreadsheets are read without one
    const next=[files.map(s=>s.id).join(','),profile()?.id||'',gone()].join('|');
    if(next!==shape||!col.querySelector('.bm-list')&&files.length){shape=next;draw();}
    else for(const s of files){const row=col.querySelector<HTMLElement>(`[data-source="${CSS.escape(s.id)}"]`);if(!row)continue;
      const sig=JSON.stringify([s.status,s.unclear_cells,s.group,s.period_start,s.period_end,s.people_count]);if(row.dataset.sig===sig)continue;row.dataset.sig=sig;
      row.querySelector('.bm-status')!.replaceChildren(status(s));  // no control lives in the status cell
      fillSaid(row.querySelector('.bm-said')!,s);}
    const t=col.querySelector('[data-time]');if(t&&imp&&t.textContent!==timeLabel())t.textContent=timeLabel();
    poll();
  }
  function draw(){
    const files=imp?.sources||[];
    col.replaceChildren(title(scoreCheckTitle(),'Upload the rota files your department made for one month.'),picker);
    if(!files.length){col.append(dropZone(true),...(providers.length?[el('div',{class:'bm-foot bm-read-by'},...readBy())]:[]));return;}
    const rows=files.map(item);rows.forEach((r,i)=>{const s=files[i];r.dataset.sig=JSON.stringify([s.status,s.unclear_cells,s.group,s.period_start,s.period_end,s.people_count]);});
    col.append(el('section',{class:'bm-list','aria-label':'Your files'},...rows,el('div',{class:'bm-foot'},...readBy())),dropZone(false),
      el('div',{class:'bm-ask'},el('span',{class:'bm-q'},'How long did making these take?'),el('span',{class:'bm-muted'},'Optional'),el('span',{class:'grow'}),timeButton()),
      footerStatus(el('span',{class:'save-state dirty'},'Not saved')));
  }
  target.prepend(head(h,'upload',imp?.id||'',cancel,cont));
  update();
}

// ---------------------------------------------------------------- Check
const sections=():{key:api.CheckItem['section'];title:string}[]=>[{key:'missing',title:'Missing from their files'},{key:'unknown',title:'Not in '+brand().short_name},
  {key:'unsure',title:'Unsure matches'},{key:'cell',title:'Cells not read clearly'}];
const SHOWN=4;
async function checkPage(target:HTMLElement,h:ScoreCheckHooks){
  const id=h.query.get('import')||'';
  if(!id){target.append(head(h,'check',''),empty('Nothing to check','Upload your rota files first.',button('Upload files',()=>h.go('upload',{}),'primary'),'upload'));return;}
  let [c,imp]=await Promise.all([api.readCheck(id),api.readImport(id)]);const open=new Set<api.CheckItem['section']>();
  /** A clinic added from the files exists from the earliest day they cover (their periods are plain department dates),
   *  so the imported month's sessions there are valid; today (department zone) only when no period is known. */
  const effectiveFrom=()=>imp.sources.map(s=>s.period_start).filter((d):d is string=>!!d).sort()[0]?.slice(0,10)||departmentToday();
  const col=el('div',{class:'bm-col'});target.append(col);
  const reload=async()=>{[c,imp]=await Promise.all([api.readCheck(id),api.readImport(id)]);draw();};
  const save=button('Save as draft',async()=>{const r=await api.saveDraft(c);h.setLeave(null);notify('Saved as a draft of the period.');h.go('compare',{draft:r.workspace_id});},'primary',{'aria-describedby':'bm-alarms'});
  const decide=async(item:api.CheckItem,decision:api.ImportDecisionInput|null)=>{c=await api.decide(c,item.id,decision);draw();
    announce(decision?`${item.as_written}: ${c.items.find(x=>x.id===item.id)?.decision?.label||'decided'}.`:`${item.as_written}: undone.`);};
  // From a button (which reports its own errors) and from a picker (act: errors said, Reload on a conflict).
  const setDecision=(item:api.CheckItem,decision:api.ImportDecisionInput|null)=>()=>decide(item,decision);
  const pickDecision=(item:api.CheckItem,decision:api.ImportDecisionInput|null)=>act(()=>decide(item,decision),reload)();
  const kindWord:Record<string,string>={person:'person',duty_code:'duty code',clinic:'clinic'};
  /** A clinic is a service at a location: after the service, its location is chosen too. */
  const withLocation=(item:api.CheckItem,anchor:HTMLElement,decision:api.ImportDecisionInput)=>item.kind!=='clinic'?pickDecision(item,decision)
    :void openPicker(anchor,anchor.parentElement||document.body,{label:'Location of '+item.as_written,multiple:false,choices:h.choices('location'),selected:new Set(),
      recentKey:'score-check-location',onPick:loc=>pickDecision(item,{...decision,location_id:loc})});
  const matchPicker=(item:api.CheckItem,anchor:HTMLElement)=>openPicker(anchor,anchor.parentElement||document.body,{label:'Match '+item.as_written,multiple:false,
    choices:h.choices(item.kind),selected:new Set(item.suggested?[item.suggested.id]:[]),recentKey:'score-check-match',
    onPick:v=>withLocation(item,anchor,{kind:item.section==='unknown'?'match':'confirm',target_id:v})});
  /** A cell not read clearly: what it says, the names chosen from Rota's own lists (the server checks the reading). */
  const fixCell=(item:api.CheckItem,anchor:HTMLElement)=>popover(anchor,(panel,close)=>{
    const value:Record<string,string>={person:'',duty_code:'',clinic:''};
    const choose=(key:string,label:string,kind:api.CheckItem['kind'])=>{const wrap=el('div',{class:'field'},el('span',{class:'label'},label));
      const put=()=>{const b=pickerButton('select-btn',label,value[key]||'Choose',label,()=>h.choices(kind).map(x=>({...x,value:x.label})),value[key],v=>{value[key]=v;put();});
        wrap.querySelector('button')?.remove();wrap.append(b);};put();return wrap;};
    const date=field('Date','date','',{required:true}),start=field('Start','time',''),end=field('End','time','');
    const err=el('p',{class:'bm-error',role:'alert'});
    const form=el('form',{class:'bm-cell-form'},choose('person','Person','person'),date.wrap,choose('duty_code','Duty code','duty_code'),choose('clinic','Clinic','clinic'),start.wrap,end.wrap,err,
      el('div',{class:'actions'},el('button',{type:'submit',class:'button primary small'},'Confirm')));
    form.addEventListener('submit',e=>{e.preventDefault();
      if(!value.person||!value.duty_code){err.textContent='Choose the person and the duty code.';return;}
      if(!form.reportValidity())return;
      void decide(item,{kind:'confirm',reading:{...item.reading_context!,person:value.person,date:date.input.value,duty_code:value.duty_code,
        ...(value.clinic?{clinic:value.clinic}:{}),start:start.input.value||null,end:end.input.value||null}})
        .then(close,er=>{err.textContent=(er as Error).message;});});  // stays open on a refusal, saying why
    panel.append(form);(form.querySelector('button') as HTMLButtonElement).focus();},{label:'What cell '+(item.reading_context?.cell_ref||'')+' says'});
  /** Add a person or a clinic from what was written: its name (editable) and, for a clinic, where it is. */
  const addRecord=(item:api.CheckItem,anchor:HTMLElement)=>popover(anchor,(panel,close)=>{
    const name=field(item.kind==='person'?'Display name':'Name','text',item.as_written,{required:true,maxLength:160});
    let location='';const err=el('p',{class:'bm-error',role:'alert'});
    const where=item.kind==='clinic'?(()=>{const wrap=el('div',{class:'field'},el('span',{class:'label'},'Location'));
      const put=()=>{wrap.querySelector('button')?.remove();wrap.append(pickerButton('select-btn','Location',h.choices('location').find(c=>c.value===location)?.label||'Choose','Location',
        ()=>h.choices('location'),location,v=>{location=v;put();}));};put();return wrap;})():null;
    const form=el('form',{class:'bm-add-form'},name.wrap,where,err,el('div',{class:'actions'},el('button',{type:'submit',class:'button primary small'},'Add')));
    form.addEventListener('submit',e=>{e.preventDefault();if(!form.reportValidity())return;
      if(item.kind==='clinic'&&!location){err.textContent='Choose where this clinic is.';return;}
      const n=name.input.value.trim();
      // The import as it is now (its periods may have changed since the page opened): the clinic starts on its earliest day.
      void (async()=>{if(item.kind==='clinic')imp=await api.readImport(id);
        const value=item.kind==='person'?{display_name:n}:{name:n,starts_on:effectiveFrom()};
        await decide(item,{kind:'add',value,...(location?{location_id:location}:{})});})()
        .then(close,async er=>{
          // Changed elsewhere meanwhile: read both again so a second Add uses the current revision and dates.
          if(er instanceof ApiError&&(er.code.startsWith('STALE_')||er.status===409)){
            try{await reload();err.textContent='This import changed meanwhile and was reloaded. Press Add again.';}
            catch(again){err.textContent='This import changed meanwhile and could not be reloaded: '+(again as Error).message;}}
          else err.textContent=(er as Error).message;});});
    panel.append(form);name.input.focus();},{label:`Add ${item.as_written}`});
  const actions=(item:api.CheckItem):Child[]=>{
    if(item.decision)return [badge(item.decision.label,item.section==='missing'?'ok':'','',{icon:item.section==='missing'?'check':null}),
      button('Undo',setDecision(item,null),'ghost small')];
    switch(item.section){
      case 'missing':return [button('Acknowledge',setDecision(item,{kind:'acknowledge'}),'small')];
      case 'unknown':{const m=button('Match…',()=>matchPicker(item,m),'small',{'aria-haspopup':'listbox'});
        // Add opens the record's own form (it holds what a person, duty type or clinic needs); Match… then picks it.
        // A duty type needs hours and staffing an admin sets: a duty code is matched or left out, never added here.
        const add:HTMLButtonElement|null=item.kind==='duty_code'||!h.canAddMissing?null:button('Add',():void=>{addRecord(item,add!);},'small',{'aria-haspopup':'dialog','aria-label':`Add ${item.as_written} as ${kindWord[item.kind]||'record'}`});
        return [add,m,button('Leave out',setDecision(item,{kind:'leave_out'}),'ghost small')];}
      case 'cell':{const fix=button('Fix…',()=>fixCell(item,fix),'small',{'aria-haspopup':'dialog',disabled:!item.reading_context,why:'This cell has no place in the file to correct; skip it.'});
        return [fix,button('Skip',setDecision(item,{kind:'skip'}),'ghost small')];}
      default:{const ok=button('Confirm',()=>withLocation(item,ok,{kind:'confirm',target_id:item.suggested?.id||null}),'small',{disabled:!item.suggested,why:'Nothing suggested to confirm.'});
        return [ok,button('Leave out',setDecision(item,{kind:'leave_out'}),'ghost small')];}}
  };
  const row=(item:api.CheckItem)=>{
    const what=item.section==='missing'?el('div',{class:'bm-what'},el('b',{},item.as_written),el('small',{},item.detail))
      :el('div',{class:'bm-what'},el('span',{class:'bm-written'},item.as_written),el('small',{},item.detail));
    const cells:Child[]=[];
    if(item.section==='unsure'){
      const sugg=el('button',{type:'button',class:'bm-suggested',...popup('dialog'),'aria-label':`Match for ${item.as_written}: ${item.suggested?.label||'none'}. Change`},
        el('span',{},item.suggested?.label||'Choose'),icon('chevronDown'));
      sugg.addEventListener('click',()=>matchPicker(item,sugg));
      cells.push(el('span',{class:'bm-arrow','aria-hidden':'true'},'→'),sugg);
      if(item.confidence!==null){const pct=Math.round(item.confidence*100),bar=el('i',{});bar.style.setProperty('--w',pct+'%');
        cells.push(el('div',{class:'bm-conf'},el('span',{},pct+'% sure'),el('span',{class:'bm-bar','aria-hidden':'true'},bar)));}
      else cells.push(el('span',{}));
    }
    return el('div',{class:`bm-row ${item.section}${item.decision?' done':''}`},
      item.section==='missing'?el('span',{class:'bm-alarm',role:'img','aria-label':'Alarm'},icon('alert')):null,what,...cells,el('div',{class:'bm-acts'},...actions(item)));
  };
  /** Review what was matched automatically: a page of 50 at a time; Change… or Leave out moves a match into the
   *  decisions above (with Undo there), like any other Check decision. */
  const PAGE=50;
  const reviewMatches=()=>dockPanel({key:'matched-automatically',title:'Matched automatically',build:({body})=>{let shown=0,token=0;
    const list=el('div',{class:'bm-matches','aria-live':'polite'});body.append(list);
    const asItem=(x:api.ImportMatch):api.CheckItem=>({id:x.id,section:'unsure',kind:x.kind,as_written:x.as_written,detail:'',suggested:x.matched,confidence:x.confidence,decision:null});
    const load=async(at=shown):Promise<void>=>{const mine=++token,page=await api.readMatches(id,at,PAGE);
      if(mine!==token||!list.isConnected)return;  // a later request was made meanwhile: this answer is stale
      if(!page.items.length&&page.offset>0)return load(Math.max(0,page.offset-PAGE));
      shown=page.offset;
      const rows=page.items.map(x=>{const item=asItem(x);
        const change=button('Change…',()=>openPicker(change,change.parentElement||document.body,{label:'Match '+x.as_written,multiple:false,choices:h.choices(x.kind),
          selected:new Set([x.matched.id]),recentKey:'score-check-match',onPick:v=>withLocation(item,change,{kind:'match',target_id:v})}),'small',{'aria-haspopup':'listbox'});
        return [el('span',{class:'bm-written'},x.as_written),x.matched.label,`${Math.round(x.confidence*100)}%`,el('span',{class:'muted'},x.file_name),
          el('div',{class:'bm-acts'},change,button('Leave out',()=>decide(item,{kind:'leave_out'}),'ghost small'))];});
      list.replaceChildren(page.total?table(['As written','Matched to','Sure','File',''],rows):empty('Nothing matched automatically','',null,'check'),
        page.total>PAGE?el('div',{class:'bm-pager'},el('span',{class:'muted'},`${page.offset+1}–${page.offset+page.items.length} of ${page.total}`),
          button('Previous',()=>load(Math.max(0,page.offset-PAGE)),'small',{disabled:page.offset===0,why:PAGE_WHY.first}),
          button('Next',()=>load(page.offset+PAGE),'small',{disabled:!page.has_more,why:PAGE_WHY.last})):'');};
    afterPick=()=>{if(list.isConnected)void load().catch(e=>notify((e as Error).message,'error',undefined,e));};
    void load().catch(e=>notify((e as Error).message,'error',undefined,e));}});
  let afterPick=()=>{};
  function draw(){
    afterPick();
    const left=c.items.filter(x=>!x.decision).length,alarmsLeft=c.items.filter(x=>x.section==='missing'&&!x.decision).length;
    save.disabled=alarmsLeft>0;h.setLeave(()=>'Your decisions are kept in this import, but it is not saved as a draft yet.');
    col.replaceChildren(left?title(`${plural(left,'thing')} need${left===1?'s':''} your decision`,'Nothing is used until you decide.'):title('Everything is decided'));
    for(const s of sections()){const items=c.items.filter(x=>x.section===s.key);if(!items.length)continue;
      const undecided=items.filter(x=>!x.decision).length,all=open.has(s.key)||items.length<=SHOWN+1,shown=all?items:items.slice(0,SHOWN);
      const count=s.key==='missing'?badge(alarmsLeft?`${alarmsLeft} to acknowledge before saving`:'All acknowledged',alarmsLeft?'err':'ok','',{id:'bm-alarms'})
        :badge(String(undecided),'neutral');
      col.append(el('section',{class:'bm-sec','aria-labelledby':'bm-s-'+s.key},
        el('div',{class:'bm-sec-head'},el('h3',{id:'bm-s-'+s.key},s.title),count,el('span',{class:'grow'}),
          all?null:button(`Show all ${items.length}`,()=>{open.add(s.key);draw();},'ghost small bm-link')),
        ...shown.map(row)));}
    col.append(el('div',{class:'bm-matched'},icon('check'),el('span',{},`${plural(c.matched,'name and code','names and codes')} matched with ${Math.round(c.threshold*100)}% confidence or more`),
        c.matched?el('span',{class:'bm-sep','aria-hidden':'true'},'·'):null,c.matched?button('Review',reviewMatches,'ghost small bm-link'):null),
      footerStatus(el('span',{class:'save-state dirty'},'Not saved')));
  }
  target.prepend(head(h,'check',id,save));
  draw();
}

// ---------------------------------------------------------------- Compare
const ROW_KEYS:{label:string;value:(s:api.BenchmarkSide)=>number|null;show:(s:api.BenchmarkSide)=>string;bad?:boolean;note?:(s:api.BenchmarkSide)=>string}[]=[
  {label:LEVEL_WORDS.must,value:s=>s.blocking,show:s=>String(s.blocking),bad:true},
  {label:'Places not covered',value:s=>s.not_covered,show:s=>String(s.not_covered),bad:true},
  {label:'Fairness gap',value:s=>s.fairness_gap,show:s=>s.fairness_gap===null?'—':String(s.fairness_gap)},
  {label:'Time it took',value:s=>s.time_hours,show:s=>s.time_hours===null?'Not entered':`${s.time_hours} hours`,note:s=>s.time_basis==='entered'?'entered by you':s.time_basis==='estimated'?'estimated':''}];
async function comparePage(target:HTMLElement,h:ScoreCheckHooks){
  const draftId=h.query.get('draft')||'';
  if(!draftId){target.append(head(h,'compare',''),empty('No rota to compare','Upload your department’s rota first.',h.canEdit?button('Upload files',()=>h.go('upload',{}),'primary'):null,'upload'));return;}
  const b=await api.readBenchmark(draftId),[theirs,ours]=b.sides;
  const period=periodLabel(b.period_start,b.period_end);
  let chosen=ours?1:0;
  const col=el('div',{class:'bm-col'});target.append(col);
  const adjust=button('Adjust & re-run',()=>popover(adjust,(panel,close)=>{
    const item=(label:string,name:Parameters<typeof menuItem>[1],run:()=>void)=>menuItem(label,name,()=>{close();run();});
    panel.append(item('Rules in this rota','rules',()=>h.openRules(draftId)),item('Auto-schedule & scoring','sliders',h.openScoring),
      ...(ours?[item('Re-run auto-schedule','reset',()=>h.rerun(ours.workspace_id))]:[]));},{label:'Adjust and re-run'}),'ghost',{'aria-haspopup':'menu'});
  adjust.append(icon('chevronDown'));
  const primary=el('span',{class:'bm-primary'});
  const drawPrimary=()=>{const side=b.sides[chosen];if(!h.canEdit||!side){primary.replaceChildren();return;}
    primary.replaceChildren(side.blocking>0?withIcon(button(`Resolve ${mustWords(side.blocking)}`,()=>h.openIssues(side.workspace_id),'primary'),'alert')
      :withIcon(button('Submit for review…',()=>h.submit(side.workspace_id),'primary'),'send'));};
  if(!ours){
    target.prepend(head(h,'compare','',...(h.canEdit?[withIcon(button('Auto-schedule now',()=>h.autoSchedule(draftId),'primary'),'sparkle')]:[])));
    col.append(title('Your rota is ready to compare',`${period}. ${plural(b.sessions,'session')}.`),
      empty('No auto-schedule draft yet',`${brand().short_name} builds its own rota for the same sessions, people and rules.`,null,'sparkle'));return;}
  const v=verdict(theirs,ours),unit=b.fairness_unit||'';
  const pick=(i:number,s:api.BenchmarkSide)=>{const input=el('input',{type:'radio',name:'bm-rota',checked:i===chosen,'aria-describedby':'bm-pick-note'}) as HTMLInputElement;
    input.addEventListener('change',()=>{chosen=i;drawPrimary();duel.querySelectorAll('.bm-pick').forEach((n,k)=>n.classList.toggle('on',k===chosen));});
    return el('label',{class:'bm-pick'+(i===chosen?' on':'')},input,el('span',{},sideName(s),el('span',{class:'bm-meta'},s.detail)),
      i===v.winner?badge('Better','ok','',{icon:'check'}):null);};
  const better=(k:typeof ROW_KEYS[number],i:number)=>{const a=k.value(theirs),o=k.value(ours);if(a===null||o===null||a===o)return false;return i===(o<a?1:0);};
  const mark=()=>el('span',{class:'bm-win',role:'img','aria-label':'Better'},icon('check'));
  const cell=(k:typeof ROW_KEYS[number],s:api.BenchmarkSide,i:number)=>el('td',{class:i?'r':'l'},el('span',{class:'bm-val'+(k.bad&&(k.value(s)||0)>0?' bad':'')},
    i&&better(k,i)?mark():null,k.show(s),k.label==='Fairness gap'&&unit&&s.fairness_gap!==null?' '+unit:'',k.note?.(s)?el('small',{},k.note(s)):null,!i&&better(k,i)?mark():null));
  const bar=(s:api.BenchmarkSide,i:number)=>{const fill=el('i',{});fill.style.setProperty('--w',Math.max(0,Math.min(100,s.score??0))+'%');return el('div',{class:'bm-score-bar '+(i?'r':'l'),'aria-hidden':'true'},fill);};
  const scoreCell=(s:api.BenchmarkSide,i:number)=>el('td',{class:i?'r':'l'},el('span',{class:'bm-score'},s.score===null?'—':String(s.score)),
    el('span',{class:'bm-score-note'+(s.publishable?' ok':'')},s.publishable?'Ready to submit':'Not publishable'),bar(s,i));
  const duel=el('section',{class:'bm-duel','aria-label':`${sideName(theirs)} compared with ${sideName(ours)}`},el('span',{id:'bm-pick-note',class:'sr-only'},'Rota to submit'),
    el('table',{},el('thead',{},el('tr',{},el('th',{scope:'col'},pick(0,theirs)),el('th',{scope:'col',class:'mid'},el('span',{class:'sr-only'},'Measure'),el('span',{'aria-hidden':'true'},'vs')),el('th',{scope:'col'},pick(1,ours)))),
      el('tbody',{},el('tr',{class:'bm-score-row'},scoreCell(theirs,0),el('th',{scope:'row',class:'mid'},'Schedule score',el('br'),el('span',{},'out of 100')),scoreCell(ours,1)),
        ...ROW_KEYS.map(k=>el('tr',{},cell(k,theirs,0),el('th',{scope:'row',class:'mid'},k.label),cell(k,ours,1))))));
  const count=(n:number,mand:boolean)=>n===0?el('span',{class:'bm-zero'},'0'):badge(String(n),mand?'err':'warn');
  const sideHeads=():Child[]=>b.sides.map(s=>el('th',{scope:'col',class:'num'},sideName(s)));
  const table=(heads:Child[],rows:Child[])=>{const grid=el('table',{class:'bm-grid'},el('thead',{},el('tr',{},...heads)),el('tbody',{},...rows));columnSizing(grid);return el('div',{class:'panel flush bm-table'},grid);};
  const panes:Record<string,()=>HTMLElement>={
    rules:()=>table([el('th',{scope:'col'},'Rule'),el('th',{scope:'col'},'Kind'),...sideHeads()],
      b.rules.map(r=>el('tr',{},el('td',{},r.number?el('span',{class:'mono muted bm-no'},r.number):null,r.name),el('td',{class:'muted'},r.mandatory?'Mandatory':'Advisory'),
        ...r.counts.map(n=>el('td',{class:'num'},count(n,r.mandatory)))))),
    // Within or outside the configured fair-share tolerance, as the server evaluated it (never a fixed tolerance here).
    fairness:()=>table([el('th',{scope:'col'},'Person'),el('th',{scope:'col',class:'num'},'Fair share'),...sideHeads()],
      b.fairness.map(f=>el('tr',{},el('td',{},f.name),el('td',{class:'num'},f.fair_share===null?'—':String(f.fair_share)),...f.counts.map((n,i)=>{
        const within=f.within?.[i];
        return el('td',{class:'num'},n===null?'—':String(n),' ',within===true?badge('Within','ok'):within===false?badge('Outside','warn'):null);})))),
    gaps:()=>table([el('th',{scope:'col'},'Session'),el('th',{scope:'col'},'Day'),...sideHeads()],
      b.gaps.map(g=>el('tr',{},el('td',{},g.name),el('td',{},formatDay(g.start,h.zone)),...g.open.map(n=>el('td',{class:'num'},n?badge(`${n} open`,'err'):el('span',{class:'muted'},'Covered')))))),
    all:()=>{const rows:Child[]=[],grp=(t:string)=>rows.push(el('tr',{class:'group'},el('td',{colspan:b.sides.length+1},el('strong',{},t))));
      const r=(label:string,val:(s:api.BenchmarkSide)=>string)=>rows.push(el('tr',{},el('th',{scope:'row',class:'bm-row-label'},label),...b.sides.map(s=>el('td',{},val(s)))));
      grp('Score');r('Schedule score',s=>s.score===null?'—':s.publishable?String(s.score):`${s.score}, capped while anything must be fixed${s.before_cap!==null?` (${s.before_cap} before the cap)`:''}`);
      for(const [k,part] of theirs.parts.entries())r(part.label,s=>{const x=s.parts[k]?.value;return x===null||x===undefined?'No baseline':x.toFixed(1).replace('-','−');});
      grp('Checks');r('Warnings',s=>String(s.warnings));r('Within fair share',s=>s.within_fair===null?'—':`${s.within_fair} of ${s.people} people`);r('Wishes met',s=>s.wishes_met===null?'—':s.wishes_met+'%');
      grp('Time');r('Time it took',s=>s.time_detail||'—');
      if(b.import_summary){const x=b.import_summary;grp('From your files');
        r('Import',s=>s.origin==='import'?`${x.added} added, ${x.left_out} left out, ${x.acknowledged} alarms acknowledged`:'Same sessions copied');}
      return table([el('th',{scope:'col'},'Measure'),...sideHeads()],rows);},
  };
  const tabLabel=(text:string,n:number|null)=>el('span',{class:'bm-tab'},text,n!==null?badge(String(n),'','qualifier'):null);
  const details=tabs('Details',[['rules',tabLabel('Rules',b.rules.length),panes.rules],['fairness',tabLabel('Fairness',b.fairness.length?ours.people:null),panes.fairness],
    ['gaps',tabLabel('Gaps',b.gaps_total),panes.gaps],['all',tabLabel('All details',null),panes.all]]);
  drawPrimary();
  target.prepend(head(h,'compare','',...(h.canEdit?[adjust]:[]),primary));
  col.append(title(v.text,`${period}. Same ${plural(b.sessions,'session')}, same people, same rules.`),duel,el('div',{class:'bm-details'},details));
  if(!h.canEdit)col.prepend(note('Read only: coordinators submit a rota for review.','info'));
}
