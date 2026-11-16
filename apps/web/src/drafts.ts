/** Private drafts, sharing and merging reviewed.
 *
 *  - One shared rota per period (`purpose: integrated`) that every group edits in its own slice.
 *  - A private draft (`visibility: private, purpose: sandbox`) is a copy only its creator sees; nothing in it notifies
 *    anyone or counts anywhere. The client opts in explicitly (maintenance: omitted fields keep legacy shared alternatives).
 *  - Sharing is one-way and shows its audience first. Others can view and copy a shared draft, never edit it.
 *  - Merge into rota… previews a three-way merge (base, rota now, draft) and applies the chosen changes at once
 *    (`workspace.merge.preview` / `workspace.merge.apply`, P2-BE). Merging shares the draft and archives it. Both run as
 *    worker jobs (PERF-5): waited for in the panel, cancelled with job.cancel; a failed one keeps the draft and choices. */
import {el,icon,button,badge,note,plural,notify,confirmAction,rolePlural,reportError,type Child,needText,needCount,needRow} from './dom.js';import {get,command,sessionChanged,awaitJob,ApiError} from './api.js';
import {elapsed,working} from './loading.js';
import {dockPanel,closeDock} from './side_panel.js';
import {outcome,tile,change} from './autoschedule_result.js';
import {CAPABILITIES} from './generated.js';
import type {Row} from './types.js';
import type {PlanningSettingsRead} from './scheduling_groups.js';

export type DraftKind='rota'|'private'|'mine_shared'|'shared'|'alternative';
/** What a workspace is to this person, from its access facts (maintenance serialization: visibility, purpose, owner). */
export function draftKind(w:Row,me:string):DraftKind{
  if(w.purpose==='integrated')return 'rota';
  if(w.purpose!=='sandbox')return 'alternative';
  return w.visibility==='private'?'private':w.owner_identity_id===me?'mine_shared':'shared';
}
/** A sandbox only its creator edits; everyone else views and copies it (maintenance require_edit). */
export const editableDraft=(w:Row,me:string)=>w.purpose!=='sandbox'||w.owner_identity_id===me;
/** The drafts list's groups, in order (maintenance): the period's rota, your private drafts, shared drafts, other drafts, in review. */
export const DRAFT_GROUPS=['Rota','Your private drafts','Shared drafts','Drafts','In review'];
export function draftGroup(w:Row,me:string):string{
  const k=draftKind(w,me);
  return w.status!=='draft'?'In review':k==='rota'?'Rota':k==='private'?'Your private drafts':k==='alternative'?'Drafts':'Shared drafts';
}
export const draftHint=(w:Row,me:string)=>({private:'Private',mine_shared:'Yours · shared',shared:'View only',rota:'',alternative:''})[draftKind(w,me)];
export const draftGlyph=(w:Row,me:string)=>({private:'lock',mine_shared:'eye',shared:'eye',rota:undefined,alternative:undefined} as const)[draftKind(w,me)];
/** The top bar's stage pill for a sandbox: "Private draft" with a lock, "Shared draft" with an eye. */
export function sandboxPill(w:Row,me:string):HTMLElement|null{
  const k=draftKind(w,me);if(k==='rota'||k==='alternative')return null;
  return el('span',{class:'rota-status sandbox','data-tip':k==='private'?'Only you can see this draft':k==='mine_shared'?'Planners can view and copy it; only you change it':'View only · copy it to change it'},
    icon(k==='private'?'lock':'eye'),k==='private'?'Private draft':'Shared draft');
}
/** The period's shared rota a draft merges into: an integrated rota still being planned that overlaps it. */
export const rotaFor=(w:Row,list:Row[])=>list.find(r=>r.purpose==='integrated'&&r.id!==w.id&&r.status==='draft'&&!r.archived_at&&r.starts_on<w.ends_on&&w.starts_on<r.ends_on)||null;

/** New private draft (maintenance): a fork of the rota (or draft) shown (P2-BE `fork_from`: people, rule adjustments and target
 *  overrides, with the lineage a merge needs), named "<name> · draft N", opened at once. No dialog. */
export async function newPrivateDraft(source:Row,mine:Row[]):Promise<Row>{
  const base=String(source.name).replace(/ · draft \d+$/,''),n=mine.filter(x=>String(x.name).startsWith(base+' · draft ')).length+1;
  const r=await command<{workspace:Row}>('workspace.create',{name:`${base} · draft ${n}`,starts_on:source.starts_on,ends_on:source.ends_on,
    visibility:'private',purpose:'sandbox',fork_from:{workspace_id:source.id,revision:source.revision}});
  notify(`${r.workspace.name} created. Only you can see it.`);
  return r.workspace;
}
/** Share with schedulers… (D18): who will see it, said before; one-way, so the button is armed. */
export function shareDraft(w:Row,done:()=>unknown){
  const who=new Intl.ListFormat('en').format(CAPABILITIES['planning.read'].map(r=>rolePlural(r).toLowerCase()));
  confirmAction(`Share ${w.name}`,'',async reason=>{await command('workspace.share',{reason},w.id,w.revision);notify(`${w.name} is shared. Planners can view and copy it.`);await done();},
    'Share',el('p',{class:'span-2'},`${who.replace(/^./,c=>c.toUpperCase())} can view it and copy it. Only you can change it. A shared draft can’t be made private again.`),true,true);
}
/** Make this the rota (maintenance workspace.designate): a configured Rota Coordinator makes a shared draft the period's rota. */
export function designateDraft(w:Row,done:()=>unknown){
  confirmAction(`Make ${w.name} the rota`,'',async reason=>{await command('workspace.designate',{reason},w.id,w.revision);notify(`${w.name} is now the rota for its period.`);await done();},
    'Make it the rota',el('p',{class:'span-2'},'Every scheduling group edits its own people in it. Other drafts stay as they are.'));
}
let coordinators:Promise<string[]>|null=null;
/** The configured Rota Coordinators (planning settings), read once per page load; the reply's one type is the contract's. */
export const rotaCoordinators=()=>coordinators||=get<PlanningSettingsRead>('/api/planning-settings')
  .then(r=>r.definition.coordinator_identity_ids).catch(e=>{coordinators=null;if(sessionChanged(e))throw e;return [];});
window.addEventListener('rota-saved',e=>{if(String((e as CustomEvent).detail?.kind||'').startsWith('planning.settings'))coordinators=null;});

// ---- maintenance · Merge into rota (P2-BE `workspace.merge.preview` / `.apply`) ----
type Choice='rota'|'yours'|'left_out'|'not_merged';
/** One merge bundle (P2-BE contract): a person's changed assignments (or one adjustment) as one atomic unit, its conflict
 *  kind and the choices it allows. `facts.sentence`/`facts.detail` (wiring point) name it; without them it is said by
 *  its person and number of changes. */
export type MergeBundle={id:string;kind:'fits'|'both_changed'|'protected_coupling'|'input_changed'|'out_of_scope';allowed_choices:Choice[];
  changes?:{shift_id?:string;person_id?:string|null}[];person_id?:string|null;
  facts?:{sentence?:string;detail?:string;shift_ids?:string[];[k:string]:unknown};changed_inputs?:{at?:string;by_name?:string;what?:string}[]};
/** `workspace.merge.preview` reply's `merge`: `after` is the canonical Fix-list summary of the default batch (fits);
 *  `can_apply` false when that batch would add or worsen a must-fix observation. */
export type MergePreview={id:string;target_revision:number;bundles:MergeBundle[];can_apply?:boolean;
  after:{must:number;could?:number;places_open?:number;submit_blocked?:boolean};new_or_worse_findings?:{message?:string}[];copied_at?:string|null};
const KIND:Record<MergeBundle['kind'],{glyph:Parameters<typeof icon>[0];label:string;sentence:string}>={
  fits:{glyph:'check',label:'Fits',sentence:'Changes that fit'},
  both_changed:{glyph:'swap',label:'Both changed',sentence:'You and the rota both changed this'},
  protected_coupling:{glyph:'lock',label:'Kept for another group',sentence:'Another group relies on this'},
  input_changed:{glyph:'calendar',label:'Changed since you copied',sentence:'Leave or availability changed since you copied'},
  out_of_scope:{glyph:'people',label:'Another group',sentence:'Another group’s people stay in this draft'}};
const CHOICE_WORDS:Record<Choice,string>={rota:'Rota’s',yours:'Yours',left_out:'Left out',not_merged:'Not merged'};
/** SX-26: a change is a session changed (a replace is one, not its removal and addition rows). */
const count=(b:MergeBundle)=>Math.max(1,new Set((b.changes||[]).map(c=>c.shift_id||'')).size);
const STAGE:Record<string,string>={checking:'Checking your changes against the rota',saving:'Saving'};
/** Why a merge job ended without a result, in plain words (a failed job carries only its code). */
function mergeFailed(j:Row){const code=String(j.error_code||j.status.toUpperCase());
  return new ApiError(code,j.status==='cancelled'?'The merge was cancelled. Nothing changed.'
    :/STALE|MERGE_RULE_CHANGED/.test(code)?'The draft or the rota changed meanwhile. Preview the merge again.'
    :code==='MERGE_CONFLICT'?'Merging would add or worsen a must-fix cause. Preview the merge again.'
    :`The merge could not finish. Reference: ${code}`,409);}
/** A merge command queued as a worker job (PERF-5): its wait shown (Waiting to start, then its stage, with the elapsed
 *  time and Cancel), its result the former command reply. Null when `stop()` ends the wait; a failed job throws.
 *  shortcut: a reload during the wait does not reattach to the job; a preview is asked again, an apply's outcome shows in the rota. */
async function mergeJob<T>(kind:string,payload:Record<string,unknown>,rota:Row,revision:number,show:(line:HTMLElement)=>void,stop?:()=>boolean):Promise<T|null>{
  const job=(await command<{job:Row}>(kind,payload,rota.id,revision)).job,since=Date.now();let said='';
  const cancel=()=>void command('job.cancel',{reason:'Cancelled the merge'},job.id).catch(e=>{if(!sessionChanged(e))reportError(e);});
  const seen=(j:Row)=>{const t=j.status==='queued'?'Waiting to start':STAGE[j.progress?.stage]||STAGE.checking;if(t!==said){said=t;show(elapsed(t,since,cancel));}};
  seen(job);const last=await awaitJob(job,{seen,stop});if(!last)return null;
  if(last.status!=='succeeded')throw mergeFailed(last);
  return (await get<{job:{result:T}}>('/api/jobs/'+job.id)).job.result;
}

export interface MergeHooks{rota:Row;time:(iso:string)=>string;day:(iso:string)=>string;ring:(shiftIds:string[])=>void;done:(rotaId:string)=>unknown;
  person:(id:string)=>string;/** The rota's Fix-list summary now (the merge's "from"). */before?:{must:number;places_open:number}|null}
/** Merge into rota… (maintenance): the docked panel, the same as the auto-schedule result (a merge applies a proposal). */
export async function mergePanel(w:Row,h:MergeHooks,kept=new Map<string,Choice>()){
  // A new preview reads the rota as it is now: a failed merge may mean the rota moved on since this page read it.
  const again=async(choices:Map<string,Choice>)=>{const now=await get<{revision:number}>(`/api/workspaces/${h.rota.id}/summary`);await mergePanel(w,{...h,rota:{...h.rota,revision:now.revision}},choices);};
  dockPanel({key:'merge',className:'merge-panel',title:'Merge into rota',sub:`${w.name} → ${h.rota.name}`,width:480,build:({body,close})=>{
    body.append(working('Checking your changes against the rota…'));
    mergeJob<{merge:MergePreview}>('workspace.merge.preview',{source_workspace_id:w.id,reason:'Preview merge into the rota'},h.rota,h.rota.revision,line=>body.replaceChildren(line),()=>!body.isConnected)
      .then(r=>{if(r)draw(r.merge);},e=>{if(sessionChanged(e))return;body.replaceChildren(note((e as Error).message,'error',e),el('div',{class:'actions'},button('Try again',()=>again(kept),'secondary small')));});
    function draw(p:MergePreview){
      const fits=p.bundles.filter(b=>b.kind==='fits'),look=p.bundles.filter(b=>b.kind!=='fits');
      // Choices made before a failed merge are kept for its new preview (bundle ids are stable), where still allowed.
      const chosen=new Map<string,Choice>(look.map(b=>[b.id,b.allowed_choices.find(c=>c===kept.get(b.id))||b.allowed_choices[0]]));
      const total=p.bundles.reduce((n,b)=>n+count(b),0),fit=fits.reduce((n,b)=>n+count(b),0);
      const inputs=[...new Map(p.bundles.flatMap(b=>b.changed_inputs||[]).map(x=>[`${x.at}|${x.what}`,x])).values()];
      const head=look.length?outcome('warn',`${fit} of ${plural(total,'change')} fit · the rest need a look`,inputs.length&&p.copied_at?`The rota changed since you copied it on ${h.day(p.copied_at)}, ${h.time(p.copied_at)}.`:'')
        :outcome('ok',`All ${plural(total,'change')} fit`,'');
      const changed=inputs.length?el('ul',{class:'changed-list','aria-label':'What changed in the rota'},...inputs.slice(0,8).map(x=>el('li',{},x.at?el('span',{class:'mono'},h.time(x.at)):'',el('span',{},[x.by_name,x.what].filter(Boolean).join(' '))))):'';
      const merging=()=>fit+look.filter(b=>chosen.get(b.id)==='yours').reduce((n,b)=>n+count(b),0);
      // Its wait is the line beside it (stage, time, Cancel), not a second spinner on the button.
      const go=button('',()=>void apply(),'primary',{type:'button'});const label=()=>{go.replaceChildren(icon('fork'),el('span',{class:'label'},`Merge ${plural(merging(),'change')}`));go.disabled=blocked||!merging();};
      // A row: its kind as an icon pill (a change that fits: how many), the sentence and the choices it allows.
      // Assignment changes in the same unit as the counts above, naming who they touch (SX-26: the server's sentence
      // counts assignment rows); a rota adjustment keeps the server's sentence.
      const who=(b:MergeBundle)=>b.person_id?[b.person_id]:[...new Set((b.changes||[]).map(c=>c.person_id).filter((x):x is string=>!!x))];
      const named=(b:MergeBundle)=>b.changes?.length?[who(b).map(h.person).join(', '),plural(count(b),'change')].filter(Boolean).join(' · '):b.facts?.sentence||KIND[b.kind].sentence;
      const row=(b:MergeBundle)=>{const k=KIND[b.kind],sentence=named(b),fitting=b.kind==='fits';
        const end:Child=fitting?'':b.allowed_choices.length>1?segment(b,()=>label()):badge(CHOICE_WORDS[b.allowed_choices[0]]||k.label,'neutral');
        const li=el('li',{'data-bundle':b.id},needRow(needCount(fitting?count(b):icon(k.glyph),fitting?'ok':'warn',{cls:'g-ipill',hidden:true}),
          needText(sentence,b.facts?.detail?String(b.facts.detail):k.label),end));
        const shifts=b.facts?.shift_ids||(b.changes||[]).map(c=>String(c.shift_id||'')).filter(Boolean);
        if(shifts.length)li.addEventListener('mouseenter',()=>h.ring(shifts));
        return li;};
      const segment=(b:MergeBundle,changed:()=>void)=>{const seg=el('div',{class:'segmented',role:'radiogroup','aria-label':'Keep: '+named(b)});
        for(const c of b.allowed_choices){const x=el('button',{type:'button',role:'radio','aria-checked':String(chosen.get(b.id)===c)},CHOICE_WORDS[c]);
          x.addEventListener('click',()=>{chosen.set(b.id,c);for(const y of seg.querySelectorAll('button'))y.setAttribute('aria-checked',String(y===x));changed();});seg.append(x);}
        return seg;};
      const before=h.before,blocked=p.can_apply===false,UNDO='You can undo',slot=el('span',{class:'muted'},UNDO),dismiss=button('Cancel',close,'ghost');
      const after=el('section',{'aria-label':'After merging'},el('h3',{},'After merging'),el('div',{class:'auto-stats',role:'group','aria-label':'After merging'},
        tile('Must fix',String(p.after.must),change(p.after.must,before?.must),p.after.must?'err':''),
        p.after.places_open!=null?tile('Open places',String(p.after.places_open),change(p.after.places_open,before?.places_open),''):'',
        p.after.could!=null?tile('Could improve',String(p.after.could),'',''):''));
      // A merge may never add a must-fix or make one worse (D20): said, and Merge stays off.
      const worse=blocked?outcome('err','Merging would add or worsen a must-fix cause',...(p.new_or_worse_findings||[]).slice(0,3).map(f=>String(f.message||''))):'';
      const disclose=w.visibility==='private'?el('p',{class:'g-held'},icon('eye'),'Merging shares this draft and its history with department planners'):'';
      body.replaceChildren(worse||head,changed,
        look.length?el('section',{'aria-label':'Need a look'},el('div',{class:'decide-head'},el('h3',{},'Need a look'),badge(String(look.length),'warn')),el('ul',{class:'fix-list merge-look'},...look.map(row))):'',
        after,
        fits.length?el('section',{'aria-label':'Changes that fit'},el('h3',{},`${plural(fit,'change')} that fit${fit===1?'s':''}`),el('ul',{class:'fix-list merge-fits'},...fits.map(row))):'',
        disclose,
        el('footer',{class:'form-footer merge-foot'},go,dismiss,el('span',{class:'grow'}),slot));
      label();
      async function apply(){go.disabled=true;dismiss.hidden=true;body.querySelector('.merge-error')?.remove();
        // The changes that fit and the conflicts taken as yours; every conflict's choice is sent.
        const ids=[...fits.map(b=>b.id),...look.filter(b=>chosen.get(b.id)==='yours').map(b=>b.id)];
        const payload={merge_id:p.id,bundle_ids:ids,choices:Object.fromEntries(chosen),reason:`Merge ${w.name} into ${h.rota.name}`};
        try{const result=await mergeJob('workspace.merge.apply',payload,h.rota,p.target_revision,line=>slot.replaceChildren(line));
          // The job's result is the merge's reply: announced as the command's was (the rota's list row, the toast's delta).
          window.dispatchEvent(new CustomEvent('rota-saved',{detail:{kind:'workspace.merge.apply',payload,result}}));
          closeDock();notify(`${plural(merging(),'change')} merged into ${h.rota.name}. ${w.name} is archived.`);await h.done(h.rota.id);}
        catch(e){label();dismiss.hidden=false;slot.replaceChildren(UNDO);
          // Said where the button is; the draft and these choices stay, and a new preview keeps them.
          if(!sessionChanged(e))body.querySelector('.merge-foot')?.before(el('div',{class:'merge-error'},note((e as Error).message,'error',e),
            el('div',{class:'actions'},button('Preview again',()=>again(chosen),'secondary small'))));}}
    }
  }});
}
