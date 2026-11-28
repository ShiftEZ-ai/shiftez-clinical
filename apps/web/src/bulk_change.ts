import {el,icon,button,notify,armed,note,select,field,area,multiPick,badge,menu,opens,phoneQuery,plural,routineReason,departmentDateTime,type Choice,type Child,type Tone} from './dom.js';
import {working} from './loading.js';
import {request,command,instant,applyUndone,sessionChanged,sessionKey,ApiError,onSessionReset} from './api.js';
import {dockPanel} from './side_panel.js';
import {dateInput,dateTimeInput,lastDayIncluded,shiftDay,showDay} from './date_picker.js';
import {dataTable,type Column} from './table_view.js';
import {conditionBuilder} from './conditions.js';
import {CONDITION_FIELD_DEFINITIONS} from './generated.js';
import {registry,nounOf,bulkContext,knownName,markRows,ruleHasDraft,markProposing,bulkWork,lineSelection,LAST_DAY,actingList,type FieldSpec,type ActionSpec,type SelectionValue} from './bulk.js';
import {recordText,isStructured} from './record_text.js';
import {countingWindowEditor,settingWords} from './rule_catalogue.js';
import {BULK_DEFAULTS,BULK_COLLECTIONS} from './generated.js';
import {stepper} from './dom.js';  // the one step rail (UX › controls S7)

/* One flow for every bulk change: Change → Preview → Results in the docked
 * panel. Preview is the server's dry run of the same handlers; Apply is one `bulk.apply` (one transaction, one audit
 * entry, one Undo, which the server returns as `undo`). A clean small change skips the panel (the light path). */

export type BulkItem={target_id?:string;expected_revision?:number;item_kind?:string;payload?:Record<string,unknown>;changes?:BulkChange[]};
export type BulkChange={field?:string;how?:string;value?:unknown;item_kind?:string;payload?:Record<string,unknown>};
export type BulkBody={item_kind?:string;common?:Record<string,unknown>;items?:BulkItem[];changes?:BulkChange[];selection?:SelectionValue;
  reason:string;resolutions?:Record<string,string>;only?:number[];apply_ready_only?:boolean;staged_id?:string};
type Resolution={id:string;label:string;best?:boolean};
type Issue={code:string;message:string;fixable_here:boolean;group_key:string;handoff?:{role:string;people?:Record<string,unknown>[];route:string}|null;resolutions?:Resolution[]};
// Preview: ready / unchanged / problem / waiting (a ready row that carries its issue was fixed); apply: changed / skipped /
// refused / waiting / unchanged / staged (kept to resolve, shown as Skipped).
type Outcome='ready'|'unchanged'|'problem'|'waiting'|'fixed'|'skipped'|'changed'|'refused'|'staged';
type Row={index:number;target_id?:string|null;item_kind?:string|null;result:Outcome;before?:Record<string,unknown>;after?:Record<string,unknown>;issues?:Issue[];record?:Record<string,any>};
type Preview={source_fingerprint:string;counts:Record<string,number>;items?:Row[];undoable?:boolean};
type Reply={batch_id:string;count:number;items:Row[];counts:Record<string,number>;undo?:{kind:string;payload:Record<string,unknown>};staged?:{id:string;expires_at:string}|null;staged_count:number;message?:string};

export const previewBulk=(body:BulkBody)=>request<Preview>('/api/commands/preview',{method:'POST',body:JSON.stringify({kind:'bulk.apply',payload:body})});
export const applyBulk=(body:BulkBody,fingerprint?:string)=>command<Reply>('bulk.apply',body as unknown as Record<string,unknown>,undefined,undefined,fingerprint);
let limits:Promise<{bulk_direct_max:number}>|null=null;
const settings=()=>limits??=request<{settings:{bulk_direct_max:number}}>('/api/bulk/settings').then(r=>({...BULK_DEFAULTS,...r.settings})).catch(e=>{limits=null;if(sessionChanged(e))throw e;return {...BULK_DEFAULTS,bulk_direct_max:0};});  // unread: fail closed (every change opens Preview); another account: nothing continues

/** What a bulk change is about: its collection, the records, how they are named, and what it may do. */
export type BulkRun={
  collection:string;noun:[string,string];
  /** The records: ids (with revisions, when the list knows them) or a server selection. */
  ids?:string[];revision?:(id:string)=>number|undefined;selection?:SelectionValue;
  /** A change already chosen (a shortcut, a form of its own): straight to Preview. */body?:BulkBody;
  /** Rows of the universal form to start with (e.g. the field a shortcut changes). */preset?:BulkChange[];
  title?:string;destructive?:boolean;governed?:boolean;
  /** The destructive verb, for the armed Apply: "End" → "Confirm end 88?". */verb?:string;
  /** The page shows what changed (redraws its list). */onDone?:(reply:Reply)=>unknown;
  /** The changed rows, to mark "Changed" in the list (table id). */table?:string;
  /** Where the change was made, when not the Change form (the Bulk editor's sheet): Preview's Back goes there, and
   *  editing there re-checks this preview. */back?:{label:string;run:()=>void};
  /** How the list names a record (its row's first column), for Preview and Results. */labelOf?:(id:string)=>string;
  /** The list's row of a record (as read from the server): the Change form's "now" values and Group by person /
   *  clinic / tag in Preview and Results. */rowOf?:(id:string)=>Record<string,unknown>|undefined;
  /** What the toast says once applied ({n}: how many changed), instead of "N records changed". */message?:string;
  /** Always opens Preview, however small and clean the change (rows added from a paste are checked before they are saved). */review?:boolean};

// ---- Words and tones (one each) ----
const PREVIEW:Record<Outcome,[string,Tone]>={ready:['Ready','draft'],fixed:['Fixed','ok'],problem:['Problem','err'],waiting:['Waiting for approval','warn'],skipped:['Skipped','neutral'],unchanged:['No change','neutral'],
  changed:['Changed','ok'],refused:['Refused','err'],staged:['Skipped','neutral']};
type Result='changed'|'skipped'|'refused'|'waiting'|'unchanged';
const RESULT:Record<Result,[string,Tone]>={changed:['Changed','ok'],skipped:['Skipped','neutral'],refused:['Refused','err'],waiting:['Waiting for approval','warn'],unchanged:['No change','neutral']};
const humane=(k:string)=>k.replace(/_ids?$/,'').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
const fieldLabel=(collection:string,key:string)=>registry(collection)?.fields[key]?.label||registry(collection)?.columns?.[key]?.label||humane(key);
function valueText(collection:string,key:string,v:unknown):string{
  if(v===null||v===undefined||v===''||(Array.isArray(v)&&!v.length))return '—';
  if(isStructured(key,v))return recordText(key,v,knownName);
  if(Array.isArray(v))return v.map(x=>valueText(collection,key,x)).join(', ');
  if(typeof v==='boolean')return v?'Yes':'No';
  const s=String(v),spec=registry(collection)?.fields[key];
  const named=spec&&choicesOf(collection,key,spec).find(c=>c.value===s)?.label;if(named)return named;
  // ends_on is stored as the day after the last day: shown as the last day included, as every date field does.
  if(/^\d{4}-\d{2}-\d{2}$/.test(s))return showDay(LAST_DAY.has(key)?shiftDay(s,-1):s);
  if(/^\d{4}-\d{2}-\d{2}T/.test(s))return departmentDateTime(s);
  return s;
}
/** "Last day 30 Nov 2026 → 31 Dec 2026 · Tags #a → #a, #b" (only what changes). */
const changeText=(collection:string,r:Row)=>Object.keys({...r.before,...r.after}).map(k=>[fieldLabel(collection,k),valueText(collection,k,r.before?.[k]),valueText(collection,k,r.after?.[k])]).filter(([,a,b])=>a!==b);
const META=/^(id|revision|department_id|schema_version|object_revision|.*_by|.*_at)$/;
/** What a new record is created with, as [label, value]: a related record created beside the row (a person's grant) is
 *  read from its own fields; blanks, bookkeeping and the row's own record (its person_id) are left out. */
const newValues=(collection:string,r:Row):[string,string][]=>{
  const values=Object.values(r.after!),rec=(values.length===1&&values[0]&&typeof values[0]==='object'&&!Array.isArray(values[0])?values[0]:r.after) as Record<string,unknown>;
  return Object.entries(rec).filter(([k,v])=>!META.test(k)&&v!==null&&v!==undefined&&v!==''&&v!==false&&!(Array.isArray(v)&&!v.length)&&v!==r.target_id)
    .map(([k,v])=>[LAST_DAY.has(k)?'Last day':Object.values(registry(collection)?.actions||{}).map(a=>a.fields?.[k]?.label).find(Boolean)||fieldLabel(collection,k),isStructured(k,v)||typeof v!=='string'||!/^[0-9a-f]{8}-/i.test(v)?valueText(collection,k,v):knownName(v)||'Record']);};
/** A rule proposed from chosen lines (rule.propose): each line it changes, by whom and where it applies, with what
 *  changes on it ("Counting starts 1 Oct 2026 → 1 Nov 2026"), from the rule in force (`record`) and the proposal. */
type RuleLineRow=Record<string,any>;
const linesOf=(x:unknown)=>(x as {definition?:{rule?:{lines?:RuleLineRow[]}}}|null|undefined)?.definition?.rule?.lines;
function lineChanges(collection:string,r:Row):{line:string;parts:string[][]}[]{
  const now=linesOf(r.record),next=linesOf(r.after?.['rule.propose']);if(!now||!next)return [];
  const was=new Map(now.map(l=>[l.id,l])),same=(a:unknown,b:unknown)=>JSON.stringify(a??null)===JSON.stringify(b??null);
  return next.flatMap(l=>{const o=was.get(l.id);if(!o)return [];
    const parts=Object.keys({...o,...l}).filter(k=>k!=='settings'&&!same(o[k],l[k])).map(k=>[fieldLabel(collection,k),valueText(collection,k,o[k]),valueText(collection,k,l[k])]);
    const os=o.settings||{},ns=l.settings||{};
    for(const kind of Object.keys({...os,...ns}))for(const k of Object.keys({...os[kind],...ns[kind]}))if(!same(os[kind]?.[k],ns[kind]?.[k]))
      parts.push([settingWords(kind,k),valueText(collection,k,os[kind]?.[k]),valueText(collection,k,ns[kind]?.[k])]);
    return parts.length?[{line:[recordText('who',l.who,knownName),recordText('where',l.where,knownName)].join(' · '),parts}]:[];});
}
/** What changes on a row as plain words (the column's search and sort). */
const changeWords=(collection:string,r:Row)=>{const lines=lineChanges(collection,r);
  return lines.length?lines.map(x=>x.line+': '+x.parts.map(p=>p.join(' ')).join(' · ')).join('; '):changeText(collection,r).map(x=>x.join(' ')).join(' ');};
const baParts=(parts:string[][])=>parts.flatMap(([f,a,b],i)=>[i?el('span',{class:'muted'},' · '):null,
  el('span',{class:'muted'},f+' '),el('span',{class:'ba-before'},a),el('span',{class:'ba-arrow','aria-label':'changes to'},' → '),el('span',{class:'ba-after'},b)]);
const ba=(collection:string,r:Row):Child=>{
  const lines=lineChanges(collection,r);
  if(lines.length)return el('div',{class:'ba ba-lines'},...lines.map(x=>el('div',{class:'ba-line'},el('span',{},x.line+': '),...baParts(x.parts))));
  // A new record (a grant, a leave record, a proposal): "New" and the values it is created with (Skill, Starts, Last
  // day, Evidence…), not its ids, revision or the record it hangs off (the row already names that).
  if(r.after&&Object.keys(r.after).length&&Object.values(r.before||{}).every(v=>v===null||v===undefined))return el('span',{class:'ba'},el('span',{class:'ba-after'},'New'),
    ...newValues(collection,r).flatMap(([f,v])=>[el('span',{class:'muted'},' · '+f+' '),el('span',{class:'ba-after'},v)]));
  const parts=changeText(collection,r);return parts.length?el('span',{class:'ba'},...baParts(parts)):el('span',{class:'muted'},'—');};
/** Group by person, clinic or tag in Preview and Results: from the record the server returned, else the list's row. */
const related=(run:BulkRun,r:Row)=>{const rec={...(r.target_id?run.rowOf?.(String(r.target_id)):undefined),...r.record} as Record<string,unknown>,ctx=bulkContext();
  const person=run.collection==='people'?nameOf(run,r):rec.person_id?ctx.labelOf('people',String(rec.person_id)):String(rec['person.display_name']||'');
  const clinic=run.collection==='services'?nameOf(run,r):rec.service_id?ctx.labelOf('services',String(rec.service_id)):String(rec['service.name']||'');
  const tags=[...(Array.isArray(rec.tags)?rec.tags:[]),...(Array.isArray(rec['person.tags'])?rec['person.tags'] as unknown[]:[])].map(t=>'#'+String(t).replace(/^#/,''));
  return {person,clinic,tags};};
const relatedColumns=(run:BulkRun,rows:Row[])=>{const seen=rows.map(r=>related(run,r));
  return ([['person','Person',(r:Row)=>related(run,r).person],['clinic','Clinic',(r:Row)=>related(run,r).clinic],['tag','Tag',(r:Row)=>related(run,r).tags]] as const)
    .filter(([k])=>seen.some(x=>k==='tag'?x.tags.length:x[k as 'person'|'clinic']))
    .map(([key,label,facet])=>({key,label,hidden:true,cell:()=>null,text:(r:Row)=>([] as string[]).concat(facet(r)).join(' '),facet:(r:Row)=>facet(r) as string|string[]}));};
/** A row's record by its identity: the list's own name for it, else the record's name or code, else whom and when it is
 *  for (a leave record: "Dr A · 3 Nov 2026 – 5 Nov 2026"); never a generic "Record". */
const nameOf=(run:BulkRun,r:Row)=>{const id=r.target_id||r.record?.id,ctx=bulkContext();
  const rec={...(id?run.rowOf?.(String(id)):undefined),...r.record} as Record<string,unknown>;
  const known=(c:string,x:unknown)=>{const l=x?ctx.labelOf(c,String(x)):'';return l==='Record'?'':l;};
  const own=(id&&run.labelOf?.(String(id)))||rec.display_name||rec.name||rec.short_code||rec.code||known(run.collection,id);
  if(own)return String(own);
  const from=rec.starts_on??rec.starts_at,to=rec.ends_on??rec.ends_at;
  const when=from?valueText(run.collection,from===rec.starts_on?'starts_on':'starts_at',from)+(to?' – '+valueText(run.collection,to===rec.ends_on?'ends_on':'ends_at',to):''):'';
  return [known('people',rec.person_id)||String(rec['person.display_name']||''),when].filter(Boolean).join(' · ')||`Row ${r.index+1}`;};
function choicesOf(collection:string,key:string,f:FieldSpec):Choice[]{
  const own=f.choices?.map(c=>({value:String(c.value),label:String(c.label)}));if(own?.length)return own;
  const given=bulkContext().choices(f,key,collection);if(given?.length)return given;
  const e=(f.schema?.enum||f.schema?.anyOf?.find((x:any)=>x.enum)?.enum) as string[]|undefined;
  return e?e.filter(x=>x!=='').map(x=>({value:x,label:humane(x)})):[];
}

// ---- Entry points ----
/** A change chosen elsewhere (a shortcut or a form of its own): checked on the server, then applied at once when it
 *  is clean, small, not destructive and not governed (the light path), otherwise opened in Preview. */
/** Saved rule lines proposed while their rule has unsaved edits on the Rules page: the proposal would drop those edits,
 *  so every path that proposes from saved lines (Change…, the Sheet, Set a rule number) asks for them first. */
// Checked when the change opens and again before every request that proposes (Preview, a fix's re-check, Apply): the
// rule may be edited on the Rules page while the panel is open.
/** The rules (policy ids) a change proposes: a rule's own records are the rule; its lines name theirs. */
function rulesOf(run:BulkRun,body:BulkBody|null|undefined=run.body){
  const ids=new Set<string>();if(run.collection!=='policy_lines'&&run.collection!=='policies')return ids;
  const add=(x:unknown)=>{if(typeof x==='string'&&x)ids.add(x);};
  for(const id of run.ids||[])add(run.collection==='policies'?id:run.rowOf?.(id)?.policy_id);
  for(const sel of [run.selection,body?.selection])for(const p of sel?.where?.filters?.policy_id||[])add(p);
  // A first version (a rule not in use yet) has no policy: it is named by its catalogue entry (bulk.ts ruleKey).
  for(const i of body?.items||[])if((i.item_kind||body?.item_kind)==='rule.propose'){add(i.target_id);
    const entry=(i.payload as {catalogue_id?:unknown}|undefined)?.catalogue_id;if(!i.target_id&&typeof entry==='string')add('catalogue:'+entry);}
  return ids;
}
function draftedRules(run:BulkRun,body:BulkBody|null|undefined=run.body){
  if(![...rulesOf(run,body)].some(ruleHasDraft))return false;
  notify('This rule has unsaved edits on the Rules page: propose or discard them first.','warning');return true;
}
export const runBulk=(run:BulkRun)=>bulkWork(()=>runBulkNow(run));
async function runBulkNow(run:BulkRun){
  if(draftedRules(run))return;
  // Every step after an await belongs to the session it started in: a change begun by another account is dropped.
  const session=sessionKey(),body=run.body!;const p=await previewBulk(body);
  const quick=await light(run,p);if(sessionKey()!==session)return;
  if(quick)return draftedRules(run)?undefined:applyNow(run,body,p);
  openFlow(run,{step:2,body,preview:p});
}
/** The universal Change form: any field or action of the collection, for every selected record. */
export function openChange(run:BulkRun){
  if(phoneQuery.matches){notify('Bulk changes need a larger screen. Use a computer to change several records together.','warning');return;}
  if(draftedRules(run))return;
  // A type whose fields change by proposing (rules) is governed wherever its Change opens (Propose, waiting for approval).
  openFlow({...run,governed:run.governed??!!vehicle(registry(run.collection))?.governed},{step:1,body:null,preview:null});
}
/** Reopen a change kept to resolve (its skipped or held-back rows), or one sent for independent review, re-checked. */
export async function resolveStaged(run:Omit<BulkRun,'body'>|null,stagedId:string){
  const r=await request<{staged:{id:string;payload:BulkBody;expires_at:string;status?:string;review_role?:string|null};preview:Preview|null;expired?:boolean}>(`/api/bulk/staged/${encodeURIComponent(stagedId)}/reopen`,{method:'POST'});
  if(r.expired||!r.preview){notify(`These kept rows expired on ${departmentDateTime(String(r.staged.expires_at))} and were dropped; History has a note.`,'warning');return;}
  const body={...r.staged.payload,staged_id:stagedId};delete body.only;delete body.apply_ready_only;
  // Opened from a notification: the change names its own collection (its selection, catalogue or item kind).
  const collection=run?.collection||collectionOf(body);
  // An open panel gives way through its own unsaved-changes guard (a finished change's Results holds nothing unsaved).
  openFlow({...(run||{collection,noun:nounOf(collection)}),body,...(r.staged.status==='waiting'?{title:'Review a change sent for approval'}:{})},{step:2,body,preview:r.preview});
}
/** A notification's change sent for independent review (`staged_id`), opened in the same Resolve flow. */
export const openStaged=(stagedId:string)=>resolveStaged(null,stagedId);
const collectionOf=(body:BulkBody):string=>{
  const catalogue=typeof body.common?.catalogue==='string'?body.common.catalogue:'',kind=body.item_kind||body.items?.find(i=>i.item_kind)?.item_kind||body.changes?.find(c=>c.item_kind)?.item_kind;
  const all=Object.keys(BULK_COLLECTIONS);
  return body.selection?.collection||(catalogue&&all.find(k=>registry(k)?.catalogue===catalogue))||
    (kind&&(all.find(k=>Object.values(registry(k)?.actions||{}).some(a=>a.kind===kind))))||'people';};
/** A list's notice of its kept rows (changes held back to resolve, owner's own, until they expire): "Resolve N". */
// Whether this person has kept rows: the session's own replies say so, and the signed-in summary (`stagedCount`) for
// rows kept earlier; only then is the list of kept changes read (a list opens with no extra request otherwise).
let keptHere=false;
onSessionReset(()=>{limits=null;keptHere=false;});
// Department settings saved (or undone): the limits are read again on the next change.
if(typeof window!=='undefined')window.addEventListener('rota-saved',e=>{if((e as CustomEvent).detail?.kind==='department.settings')limits=null;});
export function stagedNotice(run:Omit<BulkRun,'body'>):HTMLElement{
  const box=el('div',{class:'bulk-staged-note',role:'status',hidden:true}),ctx=bulkContext();
  // One's own kept rows, or another's change sent for review that this person may review (a different qualified person).
  if(!keptHere&&!(ctx.stagedCount?.())&&!(ctx.reviewCount?.()))return box;
  const kinds=new Set((registry(run.collection) as {item_kinds?:string[]}|undefined)?.item_kinds||[]);
  request<{items:{id:string;payload:BulkBody;expires_at:string;status?:string;owner_id?:string}[]}>('/api/bulk/staged').then(r=>{
    const here=r.items.filter(x=>[x.payload.item_kind,...(x.payload.items||[]).map(i=>i.item_kind),...(x.payload.changes||[]).map(c=>c.item_kind)].some(k=>k&&kinds.has(k))
      &&(!x.payload.common?.catalogue||x.payload.common.catalogue===run.collection));
    const me=ctx.identity?.()||'',reviews=here.filter(x=>x.status==='waiting'&&x.owner_id!==me),mine=here.filter(x=>x.status!=='waiting'&&(!me||x.owner_id===me));
    const rows=(list:typeof here)=>list.reduce((a,x)=>a+(x.payload.items?.length||0),0);
    const parts:Node[]=[];
    if(mine.length)parts.push(el('span',{},`${plural(rows(mine),'row')} of an earlier change kept to resolve · until ${departmentDateTime(mine[0].expires_at)}`),
      ...mine.map(x=>button(mine.length>1?`Resolve ${x.payload.items?.length||0}`:'Resolve',()=>resolveStaged(run,x.id),'secondary small')));
    if(reviews.length)parts.push(el('span',{},`${plural(reviews.length,'change')} sent for independent review · until ${departmentDateTime(reviews[0].expires_at)}`),
      ...reviews.map((x,i)=>button(reviews.length>1?`Review ${i+1}`:'Review',()=>resolveStaged(run,x.id),'secondary small')));
    if(!parts.length)return;
    box.hidden=false;box.replaceChildren(icon('alert'),...parts);
  }).catch(()=>undefined);
  return box;
}
const rowsOf=(p:Preview|null)=>(p?.items||[]).map(r=>r.result==='ready'&&r.issues?.length?{...r,result:'fixed' as const}:r);
const counted=(rows:Row[])=>{const c={ready:0,unchanged:0,problem:0,waiting:0,fixed:0,skipped:0,changed:0,refused:0,staged:0} as Record<Outcome,number>;for(const r of rows)c[r.result]++;return c;};
async function light(run:BulkRun,p:Preview,destructive=!!run.destructive){
  const rows=rowsOf(p),c=counted(rows),{bulk_direct_max}=await settings();
  return !c.problem&&!c.waiting&&!c.skipped&&c.ready>0&&rows.length<=bulk_direct_max&&!destructive&&!run.governed&&!run.review;
}
/** Apply and tell: the toast says what happened, with the server's one Undo (claimed by notify). */
/** While a rule proposal is being sent, the page's rule editors wait: an edit made then would be dropped when the new
 *  proposal arrives (a draft never outlives a proposal of its rule). The page is inert and busy until the reply is drawn. */
// The rules stay marked while the request is out (another page drawn meanwhile reads them read-only, rule_catalogue.ts);
// `settled` unmarks them once the reply is in (before the page is drawn again), `thaw` also frees the page it began on.
function freezeRules(run:BulkRun,body:BulkBody){
  const rules=rulesOf(run,body),page=rules.size?document.querySelector<HTMLElement>('#main'):null;
  markProposing(rules,true);
  if(page){page.inert=true;page.setAttribute('aria-busy','true');}
  // Each request settles its rules once, however often settled()/thaw() are called.
  // A reply that arrives after a sign-out belongs to the old account: its marks went with that session (reset), and it
  // never releases the next account's.
  const session=sessionKey();
  let done=false;const settled=()=>{if(done)return;done=true;if(sessionKey()===session)markProposing(rules,false);};
  return {settled,thaw:()=>{settled();if(page){page.inert=false;page.removeAttribute('aria-busy');}}};
}
async function applyNow(run:BulkRun,body:BulkBody,p:Preview){
  const hold=freezeRules(run,body);
  try{const reply=await applyBulk(body,p.source_fingerprint);hold.settled();
    markChanged(run,reply);notify(said(run,reply));await run.onDone?.(reply);return reply;}
  finally{hold.thaw();}
}
const changedIds=(reply:Reply)=>reply.items.filter(r=>r.result==='changed'||r.result==='ready'||r.result==='fixed').map(r=>String(r.target_id||r.record?.id||'')).filter(Boolean);
function said(run:BulkRun,reply:Reply){
  const changed=reply.items.filter(r=>r.result==='changed'||r.result==='ready'||r.result==='fixed').length,waiting=reply.items.filter(r=>r.result==='waiting').length,kept=reply.staged_count||0;
  return [changed?run.message?run.message.replace('{n}',String(changed)):`${plural(changed,run.noun[0],run.noun[1])} changed`:'',waiting?`${waiting} waiting for approval`:'',kept?`${kept} kept to resolve`:''].filter(Boolean).join(' · ')||'Nothing needed changing.';
}
function markChanged(run:BulkRun,reply:Reply){if(reply.staged_count)keptHere=true;const table=run.table||actingList;if(table)markRows(table,changedIds(reply),(reply.undo?.payload?.items as unknown[]|undefined)?.length?reply.undo:undefined);}

// ---- The panel ----
type State={step:1|2|3;body:BulkBody|null;preview:Preview|null;reply?:Reply;rows?:Row[];filter?:string;stale?:boolean;error?:string;cause?:unknown;busy?:boolean;wide?:boolean;fixes?:Record<string,string>;
  /** Dry runs: `gen` numbers each (only the latest reply is shown, over every row asked since), `checking` while one is out. */gen?:number;checking?:boolean;asked?:Set<number>};
function openFlow(run:BulkRun,s:State){
  const n=run.ids?.length;
  const title=run.title||`Change ${n?plural(n,run.noun[0],run.noun[1]):run.noun[1]}`;
  s.fixes={...(s.body?.resolutions||{})};s.rows=rowsOf(s.preview);
  dockPanel({key:'bulk-change',title,width:s.step===1?620:820,className:'bulk-panel',build:d=>{
    let rail:HTMLElement=stepper([]),stage=el('div',{class:'bulk-stage'});
    d.body.append(rail,stage);
    const form=s.step===1||run.preset||!run.body?changeForm(run):null;
    const go=(step:1|2|3)=>{s.step=step;draw();(stage.querySelector('h3') as HTMLElement|null)?.focus();};
    // Destructive: the run's own (a shortcut such as Switch off) or the change the form last read (End, Retract…).
    const ending=()=>!!run.destructive||!!form?.destructive,verb=()=>run.verb||form?.verb||'change';
    // A committed change leaves nothing unsaved: the panel's guard starts afresh (no "Not saved" on Esc or ×).
    const committed=()=>d.body.parentElement?.dispatchEvent(new Event('rota-form-saved',{bubbles:true}));
    const width=()=>{d.panel.style.setProperty('--panel-w',s.step===1?'620px':'820px');d.panel.classList.toggle('bulk-wide',!!s.wide);};
    function draw(){
      width();
      const next=stepper(['Change','Preview','Results'].map((t,i)=>{const k=(i+1) as 1|2|3;
        return {title:t,state:k===s.step?'current' as const:k<s.step?'done' as const:undefined,go:()=>go(k),disabled:k>s.step||k===1&&!form||s.step===3&&k<3};}),
        {cls:'stepper bulk-steps'});
      rail.replaceWith(next);rail=next;
      stage.replaceChildren(...(s.step===1?[form!.root]:s.step===2?previewStep():resultsStep()).filter(Boolean) as (Node|string)[]);
    }
    // Step 1 → 2: the dry run; a clean small change applies at once (the light path) and the panel closes.
    if(form)form.next=()=>bulkWork(async()=>{
      // The mode the clicked button showed: settings arriving while the dry run is out never turn Preview into Apply.
      const applying=form.applies();
      // Whether it ends records comes from the body just read (read() sets it), never from fields changed during the dry run.
      const body=await form.read(),destructive=ending();if(draftedRules(run,body))return;s.body=body;s.fixes={};s.error='';
      const session=sessionKey(),mine=s.gen=(s.gen||0)+1;
      // The light path only behind a button that says Apply: a button labelled Preview never applies.
      const p=await previewBulk(body),quick=applying&&await light(run,p,destructive);
      if(sessionKey()!==session||s.gen!==mine)return;
      if(quick){if(draftedRules(run,body))return;await applyNow(run,body,p);if(d.body.isConnected)d.close();return;}
      s.preview=p;s.rows=rowsOf(p);s.stale=false;s.filter=counted(s.rows).problem?'problem':'';go(2);
    });
    // A fix re-checks only the rows it touches (dry run `only`); the counts update in place.
    // A newer fix re-checks every row asked since the last reply, so an older reply (slower, or reversed) is dropped and
    // Apply waits for the latest one: its rows and fingerprint are what Apply sends.
    // `gen` only grows (every dry run takes the next one); `checking` says one is in flight.
    const recheck=(indexes:number[])=>bulkWork(async()=>{
      if(draftedRules(run,s.body))return;
      const mine=s.gen=(s.gen||0)+1;s.checking=true;s.asked=new Set([...(s.asked||[]),...indexes]);draw();
      const body={...s.body!,resolutions:{...s.fixes},only:[...s.asked]};
      let p:Preview;
      try{p=await previewBulk(body);}catch(e){if(!sessionChanged(e)&&s.gen===mine){s.checking=false;s.asked=undefined;s.error=(e as Error).message;s.cause=e;draw();}return;}
      if(s.gen!==mine)return;
      s.checking=false;s.asked=undefined;
      const by=new Map(rowsOf(p).map(r=>[r.index,r]));
      s.rows=s.rows!.map(r=>by.get(r.index)||r);s.preview={...p,items:s.rows};draw();
    });
    const fix=(keys:{index?:number;group?:string}[],id:string|null)=>{
      // Undo fix on a row fixed with its group (a group's fix is kept under the group's key): the group's fix is undone.
      if(!id)keys=keys.flatMap(k=>{const g=k.index===undefined?undefined:s.rows!.find(r=>r.index===k.index)?.issues?.map(i=>i.group_key).find(x=>x in s.fixes!);return g?[k,{group:g}]:[k];});
      for(const k of keys){const key=k.group??String(k.index);if(id)s.fixes![key]=id;else delete s.fixes![key];}
      const idx=new Set<number>();for(const k of keys){if(k.index!==undefined)idx.add(k.index);else for(const r of s.rows!)if(r.issues?.some(i=>i.group_key===k.group))idx.add(r.index);}
      return recheck([...idx]);
    };
    function previewStep():Child[]{
      const rows=s.rows!,c=counted(rows),here=rows.filter(r=>r.result==='problem'&&r.issues?.some(i=>i.fixable_here)).length,other=c.problem-here;
      // What Apply sends: ready and fixed rows, and proposals (they go for approval on their own).
      const ready=c.ready+c.fixed,total=rows.length,send=ready+c.waiting;
      const tile=(key:string,label:string,count:number,glyph:Parameters<typeof icon>[0],tone:Tone)=>{
        const b=el('button',{type:'button',class:`bulk-tile ${tone}`,'aria-pressed':String(s.filter===key)},icon(glyph),el('strong',{},String(count)),el('span',{},label));
        b.addEventListener('click',()=>{s.filter=s.filter===key?'':key;draw();});return b;};
      const pick=(f:string)=>rows.filter(r=>!f||(f==='ready'?r.result==='ready'||r.result==='fixed':f==='problem'?r.result==='problem'&&r.issues?.some(i=>i.fixable_here)
        :f==='waiting'?r.result==='waiting'||r.result==='problem'&&!r.issues?.some(i=>i.fixable_here):r.result===f));
      // A tile that no longer holds a row (the last problem fixed) gives way to every row, so the fix is seen.
      if(s.filter&&!pick(s.filter).length)s.filter='';
      const shown=pick(s.filter||'');
      const problemOf=(r:Row)=>r.issues?.[0];
      const groupOf=(r:Row)=>r.result==='fixed'?'Fixed · '+(problemOf(r)?.message||''):r.result==='problem'?problemOf(r)!.message:r.result==='waiting'?'Waiting for approval':r.result==='skipped'?'Skipped':r.result==='unchanged'?'No change needed':'Ready';
      const fixMenu=(anchor:HTMLElement,keys:{index?:number;group?:string}[],issue:Issue)=>menu(anchor,'Fixes',
        (issue.resolutions||[]).map(x=>({label:x.label,icon:x.id==='skip'?'close' as const:'check' as const,run:()=>fix(keys,x.id)})),{placement:'below-end'});
      const table=dataTable<Row>({id:'bulk-preview',noun:['row','rows'],rows:shown,rowId:r=>String(r.index),pageSize:100,groupLimit:20,
        defaults:{group:'problem'},
        columns:[...relatedColumns(run,rows),
          {key:'status',label:'Status',text:r=>PREVIEW[r.result][0],facet:r=>PREVIEW[r.result][0],cell:r=>badge(PREVIEW[r.result][0],PREVIEW[r.result][1])},
          {key:'record',label:'Record',text:r=>nameOf(run,r),cell:r=>{const id=r.target_id;const open=bulkContext().open;
            return id&&open?button(nameOf(run,r),()=>open(run.collection,String(id)),'',{class:'link-button'}):nameOf(run,r);}},
          {key:'change',label:'What changes',text:r=>changeWords(run.collection,r),cell:r=>ba(run.collection,r)},
          {key:'problem',label:'Problem',text:groupOf,facet:groupOf,ordered:true,cell:r=>{const i=problemOf(r);if(!i||r.result==='ready')return '';
            return el('span',{},i.message,...(i.handoff?[' ',el('a',{href:i.handoff.route,class:'link-button'},'See who approves')]:[]));}},
          {key:'fix',label:'Fix',sort:false,cell:r=>{const i=problemOf(r);if(r.result==='fixed')return button('Undo fix',()=>fix([{index:r.index}],null),'ghost small');
            if(r.result!=='problem'||!i?.resolutions?.length)return '';const b=button('Fix',()=>fixMenu(b,[{index:r.index}],i),'secondary small',{'aria-haspopup':'menu','aria-label':'Fix '+nameOf(run,r)});b.append(icon('chevronDown'));return b;}},
        ],
        // Selected rows take one fix together (the fixes they share).
        bulk:ids=>{const picked=rows.filter(r=>ids.includes(String(r.index))&&r.result==='problem'),shared=(picked[0]?.issues?.[0]?.resolutions||[]).filter(x=>picked.every(r=>r.issues?.[0]?.resolutions?.some(y=>y.id===x.id)));
          return shared.map((x,i)=>({label:x.label,icon:x.id==='skip'?'close' as const:'check' as const,primary:i===0,run:(chosen:string[],done:()=>void)=>fix(chosen.map(id=>({index:Number(id)})),x.id).then(done)}));},
        // A problem group's header: its best fix as a split button (others in its menu) and Skip all.
        groupAction:(_,group)=>{const i=group[0]?.result==='problem'?group[0].issues?.[0]:undefined;if(!i?.resolutions?.length||!group.every(r=>r.issues?.[0]?.group_key===i.group_key))return null;
          const best=i.resolutions.find(x=>x.best)||i.resolutions.find(x=>x.id!=='skip');const keys=[{group:i.group_key}];
          const more=opens(el('button',{type:'button',class:'button secondary small bulk-split-more','aria-label':'Other fixes'},icon('chevronDown')),'menu');
          more.addEventListener('click',()=>fixMenu(more,keys,i));
          return el('span',{class:'bulk-group-fix'},...(best?[el('span',{class:'bulk-split'},button(best.label,()=>fix(keys,best.id),'primary small'),more)]:[]),
            ...(i.resolutions.some(x=>x.id==='skip')?[button('Skip all',()=>fix(keys,'skip'),'ghost small')]:[]));}});
      const apply=button(s.busy?'Applying…':run.governed?`Propose ${send}`:`Apply ready rows (${send}) → Results`,()=>bulkWork(applyStep),'primary',{disabled:!send||s.busy||s.stale||!!s.checking,why:s.busy?'Applying…':s.checking?'Checking…':s.stale?'The records changed: check again first.':'No ready rows to send.'});
      if(ending()&&send)armed(apply,`Confirm ${verb().toLowerCase()} ${send}?`);
      const back=run.back?button(`← ${run.back.label}`,()=>{d.close();run.back!.run();},'secondary'):form?button('← Back',()=>go(1),'secondary'):null;
      return [el('h3',{tabindex:-1},'Preview — nothing saved yet'),
        el('div',{class:'bulk-tiles',role:'group','aria-label':'Results of the check'},tile('ready','ready',ready,'check','draft'),tile('unchanged','no change needed',c.unchanged,'close','neutral'),
          tile('problem','to resolve here',here,'alert','err'),tile('waiting','waiting for someone else',c.waiting+other,'clock','warn')),
        el('p',{class:'bulk-sum muted',role:'status'},`${ready} ready + ${c.unchanged} no change + ${here} to resolve + ${c.waiting+other} waiting${c.skipped?` + ${c.skipped} skipped`:''} = ${total}`),
        ...(!c.problem&&total?[el('p',{class:'bulk-clear'},icon('check'),'All clear — nothing to resolve')]:[]),
        ...(s.preview?.undoable===false?[note('undo_partial' in s.preview&&s.preview.undo_partial?'Undo will not restore removed records or erased personal details. Other changes can be undone.':'This change cannot be undone once applied.','warning')]:[]),
        ...(s.stale?[el('div',{class:'bulk-stale'},note('Things changed since the preview.','warning'),button('Preview again',()=>again(),'secondary small'))]:[]),
        ...(s.error?[note(s.error,'error',s.cause)]:[]),
        ...(run.back?[el('p',{class:'muted bulk-keep'},`Editing the ${run.back.label.replace(/^Back to /i,'')} re-checks this preview.`)]:[]),
        ...(s.busy?[el('div',{class:'bulk-progress',role:'status'},el('div',{class:'bulk-progress-bar'}),el('span',{},`Applying ${plural(send,'change')}… you can close this panel; you will be told when it is done.`))]:[]),
        table.root,
        el('div',{class:'bulk-foot'},back,el('span',{class:'grow'}),button(s.wide?'Narrower':'Wider',()=>{s.wide=!s.wide;width();},'ghost small'),apply)];
    }
    const again=()=>bulkWork(async()=>{if(draftedRules(run,s.body))return;s.stale=false;s.error='';const mine=s.gen=(s.gen||0)+1;s.checking=true;s.asked=undefined;draw();
      try{const p=await previewBulk({...s.body!,resolutions:{...s.fixes}});if(s.gen!==mine)return;s.preview=p;s.rows=rowsOf(p);}
      catch(e){if(sessionChanged(e)||s.gen!==mine)return;s.error=(e as Error).message;s.cause=e;}
      s.checking=false;draw();});
    async function applyStep(){
      if(draftedRules(run,s.body))return;
      const rows=s.rows!,held=rows.some(r=>r.result==='problem'||r.result==='skipped');
      const body={...s.body!,resolutions:s.fixes,...(held?{apply_ready_only:true}:{})};
      s.busy=true;s.error='';draw();
      let reply:Reply;const hold=freezeRules(run,body);
      try{reply=await applyBulk(body,s.preview!.source_fingerprint);}
      catch(e){hold.thaw();s.busy=false;if(sessionChanged(e))return;
        if(e instanceof ApiError&&(e.code==='STALE_PREVIEW'||e.code.startsWith('STALE_')))s.stale=true;else{s.error=(e as Error).message;s.cause=e;}
        if(d.body.isConnected)draw();else notify((e as Error).message,'error',undefined,e);return;}
      s.busy=false;s.reply=reply;markChanged(run,reply);committed();
      hold.settled();notify(said(run,reply));try{await run.onDone?.(reply);}finally{hold.thaw();}
      // A reviewed change always ends on Results (with its Undo), however clean: the panel never closes by itself.
      // The dock reuses its aside for another bulk-change review (d.panel stays connected): only our own body says we are still shown.
      if(!d.body.isConnected)return;
      s.filter='';go(3);  // Results start with every row (a Preview tile's filter means nothing there)
    }
    function resultsStep():Child[]{
      const reply=s.reply!,by=new Map(reply.items.map(r=>[r.index,r]));
      const outcome=(r:Row):Result=>{const x=by.get(r.index);// Kept as a staged change (Resolve reopens it) reads Skipped; Refused is only what the server refused outright.
        return !x||x.result==='problem'||x.result==='refused'?'refused':x.result==='changed'||x.result==='ready'||x.result==='fixed'?'changed':x.result==='waiting'?'waiting':x.result==='skipped'||x.result==='staged'?'skipped':'unchanged';};
      const all=s.rows!.map(r=>({...r,...(by.get(r.index)||{}),out:outcome(r)}));
      const counts=Object.fromEntries((Object.keys(RESULT) as Result[]).map(k=>[k,all.filter(r=>r.out===k).length]));
      const filter=(k:Result)=>{const b=el('button',{type:'button',class:`bulk-tile ${RESULT[k][1]}`,'aria-pressed':String(s.filter===k)},el('strong',{},String(counts[k])),el('span',{},RESULT[k][0]));
        b.addEventListener('click',()=>{s.filter=s.filter===k?'':k;draw();});return b;};
      const rows=all.filter(r=>!s.filter||r.out===s.filter);
      const resolve=reply.staged?.id?button(`Resolve ${reply.staged_count}`,()=>resolveStaged(run,reply.staged!.id),'secondary small'):null;
      const table=dataTable<typeof all[number]>({id:'bulk-results',noun:['row','rows'],rows,rowId:r=>String(r.index),pageSize:100,
        columns:[...relatedColumns(run,rows) as Column<typeof all[number]>[],
          {key:'status',label:'Status',text:r=>RESULT[r.out][0],facet:r=>RESULT[r.out][0],cell:r=>badge(RESULT[r.out][0],RESULT[r.out][1])},
          {key:'record',label:'Record',text:r=>nameOf(run,r),cell:r=>{const id=r.target_id;const open=bulkContext().open;
            return id&&open?button(nameOf(run,r),()=>open(run.collection,String(id)),'',{class:'link-button','aria-label':'Open '+nameOf(run,r)}):nameOf(run,r);}},
          {key:'change',label:'Before → after',text:r=>changeWords(run.collection,r),cell:r=>ba(run.collection,r)},
          {key:'why',label:'Why',text:r=>r.out==='refused'||r.out==='skipped'?r.issues?.[0]?.message||'':'',cell:r=>r.out==='refused'||r.out==='skipped'?r.issues?.[0]?.message||'Kept to resolve':''},
        ]});
      const undo=reply.undo?.payload&&(reply.undo.payload.items as unknown[]|undefined)?.length?button('Undo the whole change',async()=>{
        const r=await command('bulk.apply',reply.undo!.payload);await applyUndone([{kind:'bulk.apply',result:r}]).catch(()=>undefined);notify('Undone. The previous values are back.');if(d.body.isConnected)d.close();},'secondary'):null;
      return [el('h3',{tabindex:-1},run.governed?'Proposed · waiting for approval':'Results'),
        el('div',{class:'bulk-tiles',role:'group','aria-label':'What happened'},...(Object.keys(RESULT) as Result[]).map(filter)),
        ...(resolve?[el('div',{class:'bulk-staged-note'},icon('alert'),el('span',{},`${plural(reply.staged_count,'row')} kept to resolve.`),resolve)]:[]),
        table.root,
        el('div',{class:'bulk-foot'},undo,el('a',{href:'#system/audit',class:'link-button'},'Audit entry'),el('span',{class:'grow'}),button('Close',()=>d.close(),'primary'))];
    }
    draw();
  }});
}

// ---- The universal Change form (step 1) ----
const HOW:Record<string,string>={set:'Set to',clear:'Clear',move_by:'Move by days',add:'Add',remove:'Remove',give:'Give',end:'End',extend:'Extend'};
/** "Now": the spread of a field's current values among the selected records, as the list read them from the server
 *  ("30 Nov 2026 · 88  Not set · 3"), the most common first; nothing when the list does not hold the field. A blank last
 *  day reads Until changed, as a relationship without an end does. */
function nowValues(run:BulkRun,key:string):[string,number][]{
  const rows=(run.ids||[]).map(id=>run.rowOf?.(id)).filter((r):r is Record<string,unknown>=>!!r&&key in r);if(!rows.length)return [];
  const m=new Map<string,number>();for(const r of rows){const v=valueText(run.collection,key,r[key]);const t=v==='—'?LAST_DAY.has(key)?'Until changed':'Not set':v;m.set(t,(m.get(t)||0)+1);}
  const all=[...m.entries()].sort((a,b)=>b[1]-a[1]);return all.length>4?[...all.slice(0,3),[`${all.length-3} other values`,all.slice(3).reduce((a,x)=>a+x[1],0)]]:all;
}
/** The kind that carries field changes (the collection's edit / update / update-dates command); records whose one
 *  action is a proposal (rule lines) change their fields by proposing, approved by another person. */
const vehicle=(spec:ReturnType<typeof registry>)=>{const all=Object.values(spec?.actions||{});
  // Rules and their lines change their fields by proposing (the type's own `propose`, as the Sheet's edits do).
  return all.find(a=>a.kind==='catalogue.update'||/\.update(_dates)?$/.test(a.kind))||(spec?.actions.propose?.kind==='rule.propose'?spec.actions.propose:undefined);};
/** Whether the Change form has an input for a field: a free-form object (a rule's Settings, Attributes) has none, so it
 *  is not offered (its own screen edits it); who / where conditions and rule numbers have theirs. */
// A structured value (a Definition, a rule line, entries or participants: a $ref, an object, a list of objects) has no
// input here (its own editor changes it); only Conditions and line numbers have theirs.
// A quota line's Counting window has its own editor (the line panel's, rule_catalogue.ts countingWindowEditor).
const formEditable=(f:FieldSpec,key:string)=>{const refs=JSON.stringify(f.schema||{});
  if(key==='counting'||refs.includes('#/$defs/Conditions')||refs.includes('#/$defs/LineNumber'))return true;
  const plain=f.schema?.anyOf?.find((x:any)=>x.type!=='null')||f.schema||{},items=plain.items||{};
  return !plain.$ref&&plain.type!=='object'&&!(plain.type==='array'&&(items.$ref||items.type==='object'));};
const actionLabel=(key:string,a:ActionSpec)=>a.label||({authorise:'Authorise also at…',rank:'Give a rank',grant:'Grant a skill',record_leave:'Record leave',rule_number:'Set a rule number…',programme:'Add to a programme'} as Record<string,string>)[key]||humane(key);
/** Whether field changes switch records off (active → No): destructive, so Apply is the armed confirm (Change form and Sheet). */
export const switchesOff=(changes:{field?:string|null;value?:unknown}[])=>changes.some(c=>c.field==='active'&&c.value===false);
const IDENTITY=new Set(['name','display_name','short_code','staff_number']);
function changeForm(run:BulkRun){
  const spec=registry(run.collection)!,ctx=bulkContext(),edit=vehicle(spec);
  const can=(a?:ActionSpec)=>!!a&&ctx.permits(...a.roles);
  // A name, short code or staff number says which record it is: changed one record at a time, never for several at once
  // (UX review P-01), unless a shortcut presets it. shortcut: the identity fields are named here; a registry flag
  // (`identity: true` from the contracts) replaces the list once another collection needs one.
  const several=!run.ids||run.ids.length>1,preset=new Set((run.preset||[]).map(p=>p.field));
  const fields=can(edit)?Object.entries(spec.fields).filter(([k,f])=>formEditable(f,k)&&(!several||!IDENTITY.has(k)||preset.has(k))):[];
  // A record with its own Tags field (people) changes tags there: the tag-members action is the same idea, not offered
  // twice (P-07). The server keeps both commands in step (tags.py).
  const acts=Object.entries(spec.actions).filter(([,a])=>a!==edit&&can(a)&&!a.governed&&!(a.kind==='tag.members'&&fields.some(([k])=>k==='tags')));
  const choices:Choice[]=[...fields.map(([k,f])=>({value:'f:'+k,label:f.label||humane(k),group:`Fields of a ${run.noun[0]}`})),
    ...acts.map(([k,a])=>({value:'a:'+k,label:actionLabel(k,a),group:'Actions'}))];
  type Line={what:string;read:()=>Promise<BulkChange|null>};
  const lines:Line[]=[];
  const list=el('div',{class:'cond-list bulk-changes'}),root=el('form',{class:'bulk-form',novalidate:true});
  const reason=area('Reason','',2);reason.input.placeholder=run.destructive?'Required — why this change is made':'Optional note';if(run.destructive)reason.input.required=true;
  const error=el('div',{'aria-live':'assertive'});
  // Whether the change last read ends or removes records (End, Revoke, Retract, Switch off) and its verb: read anew
  // each time, so going Back and choosing another change disarms Apply.
  const out={root,destructive:false,verb:'',next:async()=>{},applies:()=>applies(),read:async():Promise<BulkBody>=>{
    const changes=(await Promise.all(lines.map(l=>l.read()))).filter(Boolean) as BulkChange[];
    if(!changes.length)throw new Error('Choose what to change.');
    // A destructive action (End, Revoke, Retract, Switch off) makes Apply the armed confirm and needs a reason.
    const ending=changes.map(c=>acts.find(([,a])=>a.kind===c.item_kind)).find(x=>x?.[1].destructive)||(switchesOff(changes)?['off',{label:'Switch off'} as ActionSpec] as const:undefined);
    out.destructive=!!ending;out.verb=ending?ending[0]==='off'?'Switch off':actionLabel(ending[0],ending[1] as ActionSpec):'';
    if(ending){
      if(reason.input.value.trim().length<3)throw new Error('Give a reason: this change ends or removes records.');}
    // Saved rule lines propose one version per rule: chosen lines go as these lines of their rule versions.
    const chosenLines=run.collection==='policy_lines'&&edit?.kind==='rule.propose'&&run.ids?lineSelection(run.ids.map(id=>String(run.rowOf?.(id)?.policy_id||'')).filter(Boolean),run.ids):null;
    const fieldChanges=changes.filter(c=>c.field),items=chosenLines?undefined:run.ids?.map(id=>({target_id:id,...(run.revision?.(id)!==undefined?{expected_revision:run.revision(id)}:{})}));
    return {...(fieldChanges.length&&edit?{item_kind:edit.kind}:{}),...(spec.catalogue?{common:{catalogue:spec.catalogue}}:{}),
      ...(items?{items}:{selection:chosenLines||run.selection}),changes,reason:routineReason(reason.input.value,!run.destructive&&!out.destructive)};}};
  function line(start?:BulkChange){
    const what=select('What to change',choices,start?.field?'f:'+start.field:start?.item_kind?'a:'+(acts.find(([,a])=>a.kind===start.item_kind)?.[0]||''):'');
    const how=el('span',{class:'bulk-how'}),value=el('span',{class:'bulk-value cond-rest'});
    let read:()=>Promise<BulkChange|null>=async()=>null;
    const me:Line={what:what.input.value,read:()=>read()};
    const remove=el('button',{type:'button',class:'icon-button bare cond-x','aria-label':'Remove this change'},icon('close'));
    const now=el('div',{class:'now-values',hidden:true});
    const row=el('div',{class:'cond-row'},el('div',{class:'cond-join'},el('span',{class:'cond-where'},lines.length?'and':'Change')),el('div',{class:'cond-line'},what.wrap,how,value,remove,now));
    remove.addEventListener('click',()=>{lines.splice(lines.indexOf(me),1);row.remove();relabel();});
    const paint=()=>{
      const [type,key]=what.input.value.split(/:(.*)/s);me.what=what.input.value;
      // Nothing chosen yet (a new line starts empty): the select's own "Choose…" asks for it.
      if(!key){how.replaceChildren();value.replaceChildren();now.hidden=true;read=async()=>null;return;}
      const spread=type==='f'?nowValues(run,key):[];now.hidden=!spread.length;
      now.replaceChildren(el('span',{class:'muted'},'Now'),...spread.map(([v,n])=>el('span',{class:'now-chip'},v,el('span',{class:'sr-only'},': '),el('b',{},String(n)))));
      if(type==='a'){const a=spec.actions[key];how.replaceChildren();
        const inputs=Object.entries(a.fields||{}).map(([k,f])=>[k,widget(run.collection,k,f,'set',!f.required,a.kind)] as const);
        value.replaceChildren(...inputs.map(([,w])=>w.node),...(inputs.length?[]:[el('span',{class:'muted'},a.kind.endsWith('.withdraw')?"Each record is withdrawn: it won't apply. Published rotas keep it.":a.destructive?'Each record ends; Apply asks to confirm.':'No settings needed.')]));
        read=async()=>{const payload:Record<string,unknown>={};for(const [k,w] of inputs){const v=await w.read();if(v!==undefined)payload[k]=v;}return {item_kind:a.kind,payload};};return;}
      const f=spec.fields[key],ways=(f.how||['set']).filter(h=>h!=='clear'||!f.required);
      const h=select('How',ways.map(x=>({value:x,label:HOW[x]||humane(x)})),start?.field===key&&start.how?start.how:ways[0]);
      const draw=()=>{const w=widget(run.collection,key,f,h.input.value);value.replaceChildren(w.node);
        read=async()=>{const v=await w.read();return {field:key,how:h.input.value,...(h.input.value==='clear'?{}:{value:v})};};};
      h.input.addEventListener('change',draw);how.replaceChildren(h.wrap);draw();
    };
    what.input.addEventListener('change',paint);paint();
    lines.push(me);list.append(row);return row;
  }
  const relabel=()=>{list.querySelectorAll('.cond-where').forEach((n,i)=>n.textContent=i?'and':'Change');label();};
  const add=button('+ Add another change',()=>{const r=line();r.querySelector<HTMLElement>('select')?.focus();},'',{class:'link-button'});
  if(!choices.length)root.append(note('You may not change these records.','info'));
  else{for(const p of run.preset?.length?run.preset:[undefined])line(p);}
  // Checking says so; a failed check keeps the form as it is, with Try again.
  // The button says what it does: "Apply N changes" when a clean result applies at once (the light path: N records, at
  // most the department's Most applied directly, not governed or destructive), else "Preview →", which only previews.
  let max=-1;
  const applies=()=>!!run.ids&&run.ids.length<=max&&!run.governed&&!run.destructive&&!lines.some(l=>{const [t,k]=l.what.split(/:(.*)/s);return t==='a'&&!!spec.actions[k]?.destructive;});
  const direct=el('span',{class:'muted bulk-direct',hidden:true},'Anything to resolve opens Preview first.');
  const label=()=>{const on=applies();next.textContent=on?`Apply ${plural(run.ids!.length,'change')}`:'Preview →';direct.hidden=!on;};
  // Checking says so; a failed check keeps the form as it is, with Try again.
  const next=button('Preview →',async()=>{error.replaceChildren();if(!root.reportValidity())return;
    error.replaceChildren(working('Checking the latest records…'));
    try{await out.next();error.replaceChildren();}
    catch(e){if(sessionChanged(e))return;error.replaceChildren(note((e as Error).message,'error',e),button('Try again',()=>next.click(),'secondary small'));}},'primary',{disabled:!choices.length,why:!choices.length?'Nothing here can be changed.':''});
  settings().then(s=>{max=s.bulk_direct_max;label();}).catch(()=>undefined);
  root.addEventListener('change',label);
  const count=run.ids?plural(run.ids.length,run.noun[0],run.noun[1])+' selected':'';
  root.append(el('h3',{tabindex:-1},'Change'),list,el('div',{class:'cond-actions'},add),el('p',{class:'muted bulk-keep'},'Everything else keeps its current value.'),reason.wrap,error,
    el('div',{class:'bulk-foot'},el('span',{class:'muted'},count),direct,el('span',{class:'grow'}),next));
  root.addEventListener('submit',e=>{e.preventDefault();next.click();});
  return out;
}
/** Who fields pick people, where fields pick sessions (the contract's condition fields). */
const conditionKeysFor=(key:string)=>CONDITION_FIELD_DEFINITIONS.filter(d=>d.key!=='roles'&&(key==='where')===d.session).map(d=>d.key as string);
/** A field's standard shared input for one way of changing it ("Move by days" is a number of days). */
/** `optional` (an action's input it may leave out): left empty, it is not sent. */
function widget(collection:string,key:string,f:FieldSpec,how:string,optional=false,action?:string):{node:HTMLElement;read:()=>Promise<unknown>}{
  const label=f.label||humane(key),type=f.schema?.type||f.schema?.anyOf?.find((x:any)=>x.type!=='null')?.type,format=f.schema?.format||f.schema?.anyOf?.find((x:any)=>x.format)?.format;
  if(how==='clear')return {node:el('span',{class:'muted'},'Left empty'),read:async()=>null};
  const refs=JSON.stringify(f.schema||{});
  // A who / where (rule lines): the shared condition builder.
  if(refs.includes('#/$defs/Conditions')){const b=conditionBuilder(bulkContext().conditionFields(conditionKeysFor(key)),null,{startEmpty:true,context:'rule'});
    return {node:b.root,read:async()=>b.read()};}
  // A rule line's numbers: Min / Max / Target (blank keeps each line's own).
  if(refs.includes('#/$defs/LineNumber')){const parts=(['min','max','target'] as const).map(k=>{const n=field(humane(k),'number','',{step:'any',placeholder:'keep'});n.input.setAttribute('aria-label',humane(k));return [k,n] as const;});
    return {node:el('span',{class:'bulk-numbers'},...parts.map(([,n])=>n.wrap)),read:async()=>{const out:Record<string,{value:number}>={};for(const [k,n] of parts)if(n.input.value!=='')out[k]={value:Number(n.input.value)};
      if(!Object.keys(out).length)throw new Error('Give at least one number.');return out;}};}
  // A quota line's counting window: the line panel's own editor; what is left empty keeps each line's own.
  if(key==='counting'){const c=countingWindowEditor({},true);
    return {node:c.node,read:async()=>{const v=c.read();if(!Object.keys(v).length)throw new Error('Choose counting dates, repeat or the block anchor.');return v;}};}
  if(how==='move_by'){const d=field('Days','number','7',{required:true,step:1});d.input.setAttribute('aria-label',label+': move by days');return {node:d.input,read:async()=>Number(d.input.value)};}
  if(f.kind==='multi'){const whole=f.schema?.items?.type==='integer',m=multiPick(label,choicesOf(collection,key,f),[],undefined,'Choose…',!whole);
    return {node:m.wrap,read:async()=>{const v=m.values();if(!v.length){if(optional)return undefined;throw new Error(`Choose ${label.toLowerCase()}.`);}return whole?v.map(Number):v;}};}
  if(f.kind==='date'&&format==='date-time'){
    const availability=collection==='availability'||action==='availability.record'||action==='availability.update';
    const allDay=availability&&(key==='start'||key==='end')?key:undefined;
    const input=el('input',{'aria-label':label,required:!optional}) as HTMLInputElement,node=dateTimeInput(input,label,{allDay});
    return {node,read:async()=>!input.value&&optional?undefined:instant(input.value)};
  }
  if(f.kind==='date'){const input=el('input',{'aria-label':label,required:!optional,placeholder:optional?label+' (optional)':label}) as HTMLInputElement,node=dateInput(input);if(LAST_DAY.has(key))lastDayIncluded(input);return {node,read:async()=>{if(!input.value){if(optional)return undefined;throw new Error(`Choose ${label.toLowerCase()}.`);}return input.value;}};}
  if(type==='boolean'){const options=[{value:'true',label:'Yes'},{value:'false',label:'No'}];
    const s=select(label,optional?[{value:'',label:'Not set'},...options]:options,optional?'':String(f.schema?.default??true),!optional);
    s.input.setAttribute('aria-label',label);return {node:s.input,read:async()=>optional&&s.input.value===''?undefined:s.input.value==='true'};}
  const options=choicesOf(collection,key,f);
  if(options.length){const s=select(label,optional?[{value:'',label:'Not set'},...options]:options,optional?'':options[0].value,!optional);s.input.setAttribute('aria-label',label);return {node:s.input,read:async()=>s.input.value||(optional?undefined:s.input.value)};}
  const t=field(label,type==='integer'||type==='number'?'number':'text','',{required:!!f.required,...(f.schema?.maxLength?{maxLength:f.schema.maxLength}:{})});t.input.setAttribute('aria-label',label);
  t.input.required=!optional&&!!f.required;return {node:t.input,read:async()=>t.input.value===''&&optional?undefined:type==='integer'||type==='number'?Number(t.input.value):t.input.value};
}
