import {request,awaitJob,command,probeWorkers} from './api.js';
import type {Row as Reply} from './types.js';

type Row=Record<string,any>;

/** One candidate check owns one server consumer token. A shared job is never cancelled globally. */
export type AskDeps={post:(body:Row)=>Promise<Row>;wait:(job:Row,o:{seen:(job:Row)=>void;stop:()=>boolean})=>Promise<Row|null>;
  release:(job:Row,token:string)=>unknown};

/** Merge provisional pages by person while retaining the newest facts for each person. */
export function mergePage(held:Row|null,page:Row):Row{
  const by=new Map<string,Row>((held?.candidates||[]).map((c:Row)=>[String(c.person_id),c]));
  for(const c of page.candidates||[])by.set(String(c.person_id),c);
  return {...held,...page,candidates:[...by.values()]};
}

/** Queue or await one consumer. Late answers are ignored after the consumer is inactive. */
export async function askCandidates(body:Row,deps:AskDeps,o:{onPage:(rows:Row)=>void;stopped:()=>boolean;
  held?:(release:()=>void)=>void}):Promise<Row>{
  const queued=await deps.post(body);
  if(o.stopped()){
    const job=queued.job as Row|undefined,token=String(queued.consumer_token||'');
    if(job&&token)void deps.release(job,token);
    throw Object.assign(new Error('Another session was chosen.'),{code:'STOPPED'});
  }
  if(Array.isArray(queued.candidates))return {...queued,provisional_page:false};
  const job=queued.job as Row,token=String(queued.consumer_token||'');
  let held:Row|null=null,seen:unknown=null,done=!token;
  const release=()=>{if(done)return;done=true;void deps.release(job,token);};
  o.held?.(release);
  const last=await deps.wait(job,{stop:o.stopped,seen:j=>{
    if(o.stopped())return;
    const page=j.progress?.result;
    if(page&&page!==seen&&page.provisional_page){seen=page;held=mergePage(held,page);if(!o.stopped())o.onPage(held);}
  }});
  if(!last||o.stopped()){release();throw Object.assign(new Error('Another session was chosen.'),{code:'STOPPED'});}
  done=true;
  if(last.status!=='succeeded'||!last.result)throw Object.assign(new Error(last.error_code==='STALE_INPUTS'
    ?'The rota changed while who can work it was being checked. Nothing was assigned.'
    :'Who can work it could not be checked.'),{code:String(last.error_code||'FILL_OPTIONS_FAILED')});
  return {...last.result,provisional_page:false};
}

export const assignFingerprint=(candidate:Row,result:Row)=>String(candidate.source_fingerprint||result.source_fingerprint||'');

export function fillOptions(body:Row,o:{onPage?:(rows:Row)=>void;stopped?:()=>boolean;held?:(release:()=>void)=>void}={}):Promise<Reply>{
  return <Promise<Reply>>askCandidates(body,{post:b=>probeWorkers().then(up=>request<Row>('/api/planner/fill-options'+(up?'?async_options=true':''),{method:'POST',body:JSON.stringify(b)})),
    wait:(job,x)=>awaitJob(job as {id:string},x) as Promise<Row|null>,
    release:(job,token)=>command('job.release',{token},String(job.id)).catch(()=>undefined)},
    {onPage:o.onPage||(()=>{}),stopped:o.stopped||(()=>false),held:o.held});
}

export type HeldAsk={started:number;page:Reply|null;on:Set<(rows:Reply)=>void>;final:Promise<Reply>;release?:()=>void};
type Consumer={active:boolean;done:boolean;entry:Entry;view:HeldAsk;releaseFromServer?:()=>void;surface:string};
type Entry={key:string;active:boolean;started:number;page:Reply|null;on:Set<(rows:Reply)=>void>;consumers:Set<Consumer>};

/**
 * Keep one logical key shared between surfaces while each surface owns a separate server consumer token.
 * Replacements get a new entry, so old POSTs, pages and ISSUE_GONE fallbacks cannot revive them.
 */
export function askRegistry(ask:(body:Row,o:{onPage:(rows:Row)=>void;stopped:()=>boolean;held:(release:()=>void)=>void})=>Promise<Reply>){
  const surfaces=new Map<string,Consumer>(),entries=new Map<string,Entry>();
  const current=(entry:Entry)=>entry.active&&entries.get(entry.key)===entry;
  const stopConsumer=(consumer:Consumer)=>{
    if(!consumer.active)return;
    consumer.active=false;
    consumer.entry.consumers.delete(consumer);
    if(surfaces.get(consumer.surface)===consumer)surfaces.delete(consumer.surface);
    consumer.releaseFromServer?.();
    if(!consumer.entry.consumers.size){
      consumer.entry.active=false;
      if(entries.get(consumer.entry.key)===consumer.entry)entries.delete(consumer.entry.key);
    }
  };
  const make=(entry:Entry,surface:string,body:(finding:string)=>Row,finding:string):Consumer=>{
    const consumer={active:true,done:false,entry,surface} as Consumer;
    consumer.view={started:entry.started,page:entry.page,on:entry.on,final:Promise.resolve(null as unknown as Reply)};
    const run=(nextFinding:string):Promise<Reply>=>ask(body(nextFinding),{
      stopped:()=>!consumer.active||!current(entry),
      onPage:rows=>{if(!consumer.active||!current(entry))return;entry.page=mergePage(entry.page,rows) as Reply;entry.on.forEach(fn=>fn(entry.page!));},
      held:release=>{consumer.releaseFromServer=release;if(!consumer.active)release();},
    }).catch(error=>{
      if(consumer.active&&current(entry)&&nextFinding&&(error as {code?:string}).code==='ISSUE_GONE')
        return run('').then(result=>({...result,issue_gone:true}));
      throw error;
    });
    consumer.view.final=run(finding).then(result=>{
      if(!consumer.active||!current(entry))throw Object.assign(new Error('Another session was chosen.'),{code:'STOPPED'});
      consumer.done=true;return result;
    }).catch(error=>{consumer.done=true;throw error;});
    return consumer;
  };
  const held=(surface:string,key:string,body:(finding:string)=>Row,finding=''):HeldAsk=>{
    const prior=surfaces.get(surface);
    if(prior&&prior.entry.key===key&&prior.active)return prior.view;
    if(prior)stopConsumer(prior);
    let entry=entries.get(key);
    if(!entry||!entry.active){entry={key,active:true,started:Date.now(),page:null,on:new Set(),consumers:new Set()};entries.set(key,entry);}
    const consumer=make(entry,surface,body,finding);
    entry.consumers.add(consumer);surfaces.set(surface,consumer);
    return consumer.view;
  };
  return {
    held,
    has:(key:string)=>{const entry=entries.get(key);return !!entry&&entry.active&&entry.consumers.size>0;},
    forget:(key:string)=>{const entry=entries.get(key);if(!entry)return;for(const consumer of [...entry.consumers])stopConsumer(consumer);},
    stop:(surface:string)=>{const consumer=surfaces.get(surface);if(consumer)stopConsumer(consumer);},
    reset:()=>{for(const consumer of [...surfaces.values()])stopConsumer(consumer);entries.clear();surfaces.clear();},
  };
}
