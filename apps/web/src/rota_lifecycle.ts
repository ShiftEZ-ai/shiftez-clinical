/** Archive, Restore and Delete draft: one action per rota state, offered only when the server's `allowed_actions`
 *  names it (never inferred here). Archive and Delete use the same-button confirm with a reason (optional for a draft);
 *  Archive's message offers Undo (api.ts UNDO: it restores); a baseline-backed Delete is a recoverable archive and offers
 *  Undo too, while a legacy no-baseline delete is permanent. The Archived rotas panel lists them with who, when and why,
 *  and Restore. */
import {el,button,empty,confirmAction,notify,reportError,formatDate,departmentDateTime,statusLabel,type IconName,PAGE_WHY} from './dom.js';
import {get,command,rotaMeta,ApiError,deleteWasRecoverable} from './api.js';
import {dockPanel} from './side_panel.js';
import type {Row} from './types.js';

export type Lifecycle='workspace.delete_draft'|'workspace.archive'|'workspace.restore';
const NAMES:Record<Lifecycle,[string,IconName]>={'workspace.delete_draft':['Delete draft','trash'],'workspace.archive':['Archive','inbox'],'workspace.restore':['Restore','reset']};

/** The one lifecycle action this person may take on this rota now, or null: Delete draft wins over Archive (an
 *  untouched draft has nothing to keep), and an archived rota only restores. */
export function lifecycleOf(w:Row):Lifecycle|null{
  const a:string[]=w.allowed_actions||[];
  if(w.archived_at)return a.includes('workspace.restore')?'workspace.restore':null;
  return a.includes('workspace.delete_draft')?'workspace.delete_draft':a.includes('workspace.archive')?'workspace.archive':null;
}

/** The one line that says what changes; nothing for a plain draft. */
// A baseline-backed draft is archived, not erased (server: planning.py has_baseline), so only a legacy delete is final.
const consequence=(w:Row,kind:Lifecycle)=>kind==='workspace.delete_draft'?(w.baseline?'':'This can’t be undone.')
  :w.status==='published'?'Staff will stop seeing this rota.'
  :['submitted','approved'].includes(w.status)?'Its review is withdrawn.'
  :w.purpose==='integrated'?'It stops being this period’s rota.':'';

/** Archive or delete `w`: its reason (optional for a draft), then the armed second press. */
export function lifecycleDialog(w:Row,kind:Exclude<Lifecycle,'workspace.restore'>,done:()=>unknown){
  const [label]=NAMES[kind];
  confirmAction(`${label} · ${w.name}`,consequence(w,kind),async reason=>{
    const r=await command(kind,{reason},w.id,w.revision);
    if(kind==='workspace.delete_draft'&&r==null){await done();return confirmDelete(w,done);}
    // The one message: notify claims the command's Undo (api.ts UNDO), so a recoverable delete offers it here.
    notify(kind==='workspace.archive'?`${w.name} archived.`:deleteWasRecoverable(r)?`${w.name} removed.`:`${w.name} deleted.`);
    await done();
  // Owner, 8 Nov: Archive always arms, and a baseline-backed Delete is archive-like, so every Delete arms too (Undo follows).
  },label,undefined,w.status==='draft',true);
}

/** A Delete with no reply claims neither outcome: the rota read again says which (gone, archived), else Retry. */
async function confirmDelete(w:Row,done:()=>unknown){
  const now=await rotaMeta(w.id).then(x=>x.workspace,e=>e instanceof ApiError&&e.status===404?null:undefined);
  if(now===null)notify(`${w.name} deleted.`);
  else if(now?.archived_at)notify(`${w.name} removed.`);
  else notify('Couldn’t confirm the delete','warning',{label:'Retry',run:()=>now?lifecycleDialog(now,'workspace.delete_draft',done):confirmDelete(w,done)});
}

/** Restore at once (nothing is lost, it can be archived again). */
export async function restoreRota(w:Row,done:(w:Row)=>unknown){
  const r=await command<{workspace:Row}>('workspace.restore',{reason:'Restored'},w.id,w.revision);
  notify(`${w.name} restored.`);
  await done(r.workspace);
}

/** The rota's lifecycle entry for a menu (Rota tools, Schedule tools), or null when the server offers none.
 *  `named`: several rotas in one menu say which. */
export function lifecycleItem(w:Row,item:(label:string,glyph:IconName,run:()=>unknown)=>HTMLElement,done:()=>unknown,named=false):HTMLElement|null{
  const kind=lifecycleOf(w);if(!kind)return null;
  const [label,glyph]=NAMES[kind];
  const node=item(named?`${label} ${w.name}…`:label+'…',glyph,()=>kind==='workspace.restore'?restoreRota(w,done).catch(reportError):lifecycleDialog(w,kind,done));
  if(kind!=='workspace.restore')node.classList.add('danger');
  return node;
}

/** Archived rotas: a docked list, newest first, 100 a page, each with who, when, why and Restore. */
export async function archivedRotas(me:string,restored:(w:Row)=>unknown,offset=0):Promise<void>{
  const page=await get<{workspaces:Row[];total:number;has_more:boolean}>(`/api/workspaces?limit=100&offset=${offset}&archived_only=true`),list=page.workspaces;
  const last=(d:string)=>new Date(Date.parse(d.slice(0,10)+'T12:00:00Z')-864e5).toISOString().slice(0,10);
  dockPanel({key:'archived-rotas',title:'Archived rotas',build:({body,close})=>{
    if(!list.length){body.append(empty('No archived rotas','Archived rotas appear here, ready to restore.',null,'history'));return;}
    body.append(el('ul',{class:'dock-list','aria-label':'Archived rotas'},...list.map(w=>{
      // The server leaves the name out when this person may not see it: then only "Archived".
      const by=w.archived_by===me?'Archived by you':w.archived_by_name?`Archived by ${w.archived_by_name}`:'Archived',was=w.archive_state?.status;
      return el('li',{class:'discarded-row'},
        el('span',{class:'archived-text'},el('strong',{},w.name),
          el('small',{class:'muted'},[`${formatDate(w.starts_on)} – ${formatDate(last(w.ends_on))}`,was?statusLabel(was):''].filter(Boolean).join(' · ')),
          el('small',{class:'muted'},[`${by} ${departmentDateTime(w.archived_at)}`,w.archive_reason].filter(Boolean).join(' · '))),
        lifecycleOf(w)==='workspace.restore'?button('Restore',()=>restoreRota(w,x=>{const shown=restored(x);close();return shown;}).catch(reportError),'secondary small',{'aria-label':`Restore ${w.name}`}):'');})));
    if(page.has_more||offset>0)body.append(el('div',{class:'actions'},el('span',{},`${page.total} archived rotas · page ${offset/100+1}`),
      button('Previous archived rotas',()=>{close();return archivedRotas(me,restored,Math.max(0,offset-100));},'secondary small',{disabled:offset===0,why:PAGE_WHY.first}),
      button('Next archived rotas',()=>{close();return archivedRotas(me,restored,offset+100);},'secondary small',{disabled:!page.has_more,why:PAGE_WHY.last})));
  }});
}
