/** Score check (benchmark): every call the Upload → Check → Compare screens and Configuration › AI provider make, in ONE
 *  module. Types are the generated contracts (generated.ts); paths and command kinds are the backend's canonical ones
 *. Every change goes through /api/commands with the record's revision. */
import {brand} from './brand.js';
import {get,request,command,ApiError} from './api.js';
import {SCORE_CHECK_LIMITS} from './generated.js';
import type {ScoreImport,ImportSource,ImportCheck,CheckItem,ImportDecisionInput,ScoreImportTime,ScoreImportSaved,ScoreImportUpdateSource,
  RotaExtractionAssignment,Benchmark,BenchmarkSide,AiSettings,AiProviderProfileRecord,AiProviderProfileInput,AiCallRecord,ScoreProviderChoice,ScoreProviderChoices,ImportMatch,ImportMatches} from './generated.js';

export type {ScoreImport,ImportSource,ImportCheck,CheckItem,ImportDecisionInput,ScoreImportTime,RotaExtractionAssignment,Benchmark,BenchmarkSide,AiSettings,AiProviderProfileRecord,AiProviderProfileInput,ScoreProviderChoice,ImportMatch,ImportMatches};
export type ProviderKind=AiProviderProfileRecord['kind'];
export type Reasoning=AiProviderProfileRecord['reasoning'];

/** The destination host (with any non-default port): a full URL is parsed; a host-only value (the coordinator's provider
 *  choices send just host[:port]) is shown as given, so ports and IPv6 brackets are kept. */
const hostOf=(url:string)=>{if(!/^[a-z][a-z0-9+.-]*:\/\//i.test(url))return url;try{return new URL(url).host;}catch{return url;}};
/** A provider on the organisation's own network (Rota sends it no data outside the site). */
export function isPrivate(url:string):boolean{
  // The whole host, never a prefix: 10.example.net and localhost.example.com are public names.
  let host=hostOf(url).toLowerCase();
  host=host.startsWith('[')?host.slice(1,host.indexOf(']')):host.replace(/:\d+$/,'');
  if(host==='localhost'||host.endsWith('.localhost')||host.endsWith('.local'))return true;
  const v4=/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if(v4){const [a,b,...rest]=v4.slice(1).map(Number);if([a,b,...rest].some(x=>x>255))return false;
    return a===10||a===127||a===172&&b>=16&&b<=31||a===192&&b===168||a===169&&b===254;}
  // IPv6: loopback, unique-local fc00::/7 and link-local maintenance::/10.
  return host.includes(':')&&(host==='::1'||/^f[cd][0-9a-f]{0,2}:/.test(host)||/^fe[89ab][0-9a-f]?:/.test(host));
}
/** Where a provider sends the files: the one-line notice on Upload and in Configuration. */
export function sendsTo(p:Pick<AiProviderProfileRecord,'kind'|'base_url'>):string{
  const host=hostOf(p.base_url);
  if(isPrivate(p.base_url))return 'Files stay on '+host;
  const bare=(host.startsWith('[')?host:host.replace(/:\d+$/,'')).toLowerCase();
  const who=bare==='api.anthropic.com'?'Anthropic':bare==='api.openai.com'?'OpenAI':bare==='generativelanguage.googleapis.com'?'Google'
    :bare.endsWith('.openai.azure.com')?'Microsoft Azure':'';
  return who?`Sends files and staff names to ${who} (${host})`:`Sends files and staff names to ${host}`;
}
export const PROVIDER_KINDS:{value:ProviderKind;label:string}[]=[{value:'anthropic',label:'Anthropic'},
  {value:'openai_compatible',label:'OpenAI-compatible (OpenAI, local, other)'},{value:'gemini',label:'Google Gemini'},{value:'azure',label:'Azure OpenAI'}];
export const REASONING:{value:Reasoning;label:string}[]=[{value:'none',label:'None'},{value:'low',label:'Low'},{value:'medium',label:'Medium'},
  {value:'high',label:'High'}];

/** A paid provider is chosen only with its current revision and the acknowledged privacy line. */
const consent=(p:ScoreProviderChoice|null)=>p?{profile_id:p.id,profile_revision:p.revision,privacy_acknowledged:true}:{profile_id:null};
/** Files go as multipart (field "files"), with the provider consent and, when adding, the import's revision. */
const pendingUploads=new Map<string,string>();
const MB=1024*1024,READABLE=/\.(xlsx|csv|tsv|pdf|png|jpe?g|webp)$/i;
/** The contract's upload limits, checked before any file is read or hashed: a refusal costs nothing and says why. */
export function uploadProblem(files:File[]):string{
  if(files.length>SCORE_CHECK_LIMITS.files)return `Upload at most ${SCORE_CHECK_LIMITS.files} files at a time.`;
  const odd=files.find(f=>!READABLE.test(f.name));if(odd)return `${odd.name} is not a file ${brand().short_name} reads. Use Excel, CSV, PDF or a photo (PNG, JPEG, WebP).`;
  const big=files.find(f=>f.size>SCORE_CHECK_LIMITS.file_bytes);if(big)return `${big.name} is larger than ${SCORE_CHECK_LIMITS.file_bytes/MB} MB.`;
  const empty=files.find(f=>f.size===0);if(empty)return `${empty.name} is empty.`;
  if(files.reduce((n,f)=>n+f.size,0)>SCORE_CHECK_LIMITS.total_file_bytes)return `These files together are larger than ${SCORE_CHECK_LIMITS.total_file_bytes/MB} MB. Upload them in smaller groups.`;
  return '';
}
async function upload<T>(path:string,files:File[],fields:Record<string,string|number|boolean|null|undefined>){
  const problem=uploadProblem(files);if(problem)throw new Error(problem);
  // shortcut: WebCrypto has no streaming digest, so each accepted file (at most the contract's 10 MB, 20 MB in all) is
  // hashed whole; move to a streaming hash if the limits grow.
  const body=new FormData();for(const f of files)body.append('files',f,f.name);
  const kept=Object.entries(fields).filter(([,v])=>v!==null&&v!==undefined);for(const [k,v] of kept)body.append(k,String(v));
  // The same files (by content) to the same place with the same choices are the same upload.
  const digests=await Promise.all(files.map(async f=>[...new Uint8Array(await crypto.subtle.digest('SHA-256',await f.arrayBuffer()))].map(x=>x.toString(16).padStart(2,'0')).join('')));
  const same=JSON.stringify([path,files.map((f,i)=>[f.name,digests[i]]),kept]),key=pendingUploads.get(same)||crypto.randomUUID();
  pendingUploads.set(same,key);
  try{const r=await request<T>(path,{method:'POST',body,headers:{'Idempotency-Key':key}});pendingUploads.delete(same);return r;}
  catch(e){if(e instanceof ApiError&&e.status>0&&e.status<500)pendingUploads.delete(same);throw e;}
}

// ---- Upload
export const startImport=(files:File[],profile:ScoreProviderChoice|null)=>upload<ScoreImport>('/api/score-imports',files,profile?consent(profile):{});
export const readImport=(id:string)=>get<ScoreImport>(`/api/score-imports/${encodeURIComponent(id)}`);
export const addFiles=(i:ScoreImport,files:File[],profile:ScoreProviderChoice|null)=>upload<ScoreImport>(`/api/score-imports/${encodeURIComponent(i.id)}/sources`,
  files,{revision:i.revision,...(profile?{profile_revision:profile.revision,privacy_acknowledged:true}:{})});
export const updateSource=(i:ScoreImport,sourceId:string,change:Omit<ScoreImportUpdateSource,'import_id'|'source_id'>)=>
  command<ScoreImport>('score_import.update_source',{import_id:i.id,source_id:sourceId,...change},i.id,i.revision);
export const removeSource=(i:ScoreImport,sourceId:string)=>command<ScoreImport>('score_import.remove_source',{import_id:i.id,source_id:sourceId},i.id,i.revision);
export const setTime=(i:ScoreImport,time:ScoreImportTime|null)=>command<ScoreImport>('score_import.time',{import_id:i.id,time},i.id,i.revision);
export const chooseProfile=(i:ScoreImport,p:ScoreProviderChoice|null)=>command<ScoreImport>('score_import.profile',{import_id:i.id,...consent(p)},i.id,i.revision);
export const discardImport=(i:ScoreImport)=>command('score_import.discard',{import_id:i.id},i.id,i.revision);
// ---- Check
export const readCheck=(id:string)=>get<ImportCheck>(`/api/score-imports/${encodeURIComponent(id)}/check`);
/** The matches accepted automatically (confidence at or above the threshold), a page at a time; each can be overridden
 *  with decide (kind match or leave_out) and undone the same way. */
export const readMatches=(id:string,offset=0,limit=50)=>get<ImportMatches>(`/api/score-imports/${encodeURIComponent(id)}/matches?offset=${offset}&limit=${limit}`);
export const decide=(c:ImportCheck,itemId:string,decision:ImportDecisionInput|null)=>
  command<ImportCheck>('score_import.decide',{import_id:c.import_id,item_id:itemId,decision},c.import_id,c.revision);
/** Save as draft: the imported draft of the period; refused while an alarm is not acknowledged. */
export const saveDraft=(c:ImportCheck)=>command<ScoreImportSaved>('score_import.save',{import_id:c.import_id},c.import_id,c.revision);
// ---- Compare
export const readBenchmark=(workspaceId:string)=>get<Benchmark>(`/api/benchmark?workspace=${encodeURIComponent(workspaceId)}`);
/** Rota's draft of the same sessions; the existing generation job then fills it (Planner's auto-schedule). */
export const autoSameSessions=(workspaceId:string,revision:number)=>command<{workspace:{id:string}}>('score_check.auto',{workspace_id:workspaceId},undefined,revision);
// ---- Configuration › AI provider
export const readAiSettings=()=>get<AiSettings>('/api/ai-providers');
/** The last 100 AI calls (audit: model, tokens, time, result; never file content, names or keys). */
export const readAiCalls=()=>get<{calls:AiCallRecord[]}>('/api/ai-providers/calls').then(r=>r.calls);
export const setAiEnabled=(s:AiSettings,enabled:boolean)=>command<AiSettings>('ai_provider.enabled',{enabled},undefined,s.revision);
/** Create (no target) or update (the profile and its revision); the reply is the profile, so the settings are read again. */
export const saveProfile=async(p:AiProviderProfileRecord|null,input:AiProviderProfileInput)=>{
  await command('ai_provider.save',input,p?.id,p?.revision);return readAiSettings();};
/** Delete keeps the provider's call evidence and revokes its key (the server archives it). */
export const deleteProfile=async(p:AiProviderProfileRecord)=>{
  await command('ai_provider.delete',{reason:'Removed in Configuration › AI provider'},p.id,p.revision);return readAiSettings();};
type ExtractionJob={id:string;state:string;error_code:string|null;result:{ok?:boolean;message?:string}|null};
/** Tests are queued, audited worker jobs (a paid call never holds a lock or repeats): poll until the job finishes. */
export async function testProfile(p:AiProviderProfileRecord,still:()=>boolean=()=>true):Promise<{ok:boolean;message:string}|null>{
  const {job}=await command<{job:ExtractionJob}>('ai_provider.test',{reason:'Connection test'},p.id,p.revision);
  for(let i=0;i<90;i++){if(!still())return null;  // the page was left: stop asking
    const now=await get<ExtractionJob>(`/api/score-extraction-jobs/${encodeURIComponent(job.id)}`);
    if(now.state==='completed'||now.state==='failed')return {ok:now.state==='completed'&&!!now.result?.ok,message:now.result?.message||now.error_code||''};
    await new Promise(r=>setTimeout(r,2000));}
  return {ok:false,message:'No answer yet. Try again later.'};
}
/** The providers a coordinator may read an import with (none: CSV, TSV and Excel without AI): id, revision, name, kind, host, model, default only. */
export const providersForImport=()=>get<ScoreProviderChoices>('/api/score-imports/providers').then(s=>s.profiles);  // already filtered by the server
