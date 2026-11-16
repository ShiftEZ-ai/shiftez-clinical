/** The Auto-schedule panel's intended run setup, kept on this device per account, department and rota (rota.* keys are
 *  forgotten at sign-out and when another account signs in, account_memory.ts), so a reload keeps what was chosen.
 *  Only choices are kept: never a run, its progress, a result or a continuation — the server's jobs say what is
 *  running and what can be continued. A kept value that no longer fits is checked again by the panel. */
import {AUTO_SCHEDULE_LIMITS,GENERATION_CONTROL_DEFAULTS,type Condition,type Conditions} from './generated.js';
export interface RunSetup{scope?:'period'|'open'|'selected';keep?:boolean;minutes?:number;fairness?:number;from?:string;to?:string;clinics?:string[];preset?:string;
  /** People to schedule (generation_scope.conditions); absent: everyone. pickGroup: a group was asked for and is not
   *  chosen yet (Run waits for it, never schedules everyone instead). */group?:Conditions;pickGroup?:boolean}
const DAY=/^\d{4}-\d{2}-\d{2}$/;
/** The kept setup read back, every field checked (storage is not trusted: a bad field is dropped, never guessed). */
export function parseSetup(v:unknown):RunSetup{
  if(!v||typeof v!=='object'||Array.isArray(v))return {};
  const o=v as Record<string,unknown>,out:RunSetup={};
  if(o.scope==='period'||o.scope==='open'||o.scope==='selected')out.scope=o.scope;
  if(typeof o.keep==='boolean')out.keep=o.keep;
  if(Number.isInteger(o.minutes))out.minutes=o.minutes as number;
  if(Number.isInteger(o.fairness))out.fairness=o.fairness as number;
  if(typeof o.from==='string'&&typeof o.to==='string'&&DAY.test(o.from)&&DAY.test(o.to)&&o.from<=o.to){out.from=o.from;out.to=o.to;}
  if(Array.isArray(o.clinics)&&o.clinics.every(c=>typeof c==='string'))out.clinics=o.clinics as string[];
  if(typeof o.preset==='string')out.preset=o.preset;
  const g=parseGroup(o.group);if(g)out.group=g;
  if(o.pickGroup===true)out.pickGroup=true;
  return out;
}
const key=(identity:string,department:string,workspace:string)=>`rota.autoschedule.${identity}.${department}.${workspace}`;
export function storedSetup(identity:string,department:string,workspace:string):RunSetup{
  try{return parseSetup(JSON.parse(localStorage.getItem(key(identity,department,workspace))||'null'));}catch{return {};/* nothing kept: the defaults apply */}
}
export function keepSetup(identity:string,department:string,workspace:string,s:RunSetup){
  const value:RunSetup={scope:s.scope,keep:s.keep,minutes:s.minutes,fairness:s.fairness,from:s.from,to:s.to,clinics:s.clinics,preset:s.preset,group:s.group||undefined,pickGroup:s.pickGroup||undefined};
  try{localStorage.setItem(key(identity,department,workspace),JSON.stringify(value));}catch{/* not kept */}
}
/** The running panel's suggested "Add N min": the department's saved step (generation_controls, else the generated
 *  default), never more than the room left under the run's ceiling. */
export function addTimeStep(controls:unknown,room:number):number{
  const saved=(controls as {extension_minutes?:unknown}|null|undefined)?.extension_minutes;
  const step=Number.isInteger(saved)&&(saved as number)>=AUTO_SCHEDULE_LIMITS.minimum_minutes?saved as number:GENERATION_CONTROL_DEFAULTS.extension_minutes;
  return Math.min(step,room);
}

/** Minutes a running search can still be given (job.add_time): the department's ceiling less the run's own search time
 *  (Add time's total so far, else its options, else the worker's limit, else the department's default — the server's
 *  order). 0 when none can be added: the panel then offers no Add time reviewed. */
export function addTimeRoom(job:{[key:string]:any},maximum:number,fallback:number):number{
  const own=Number(job.search_control?.minutes)||Number(job.effective_options?.search_minutes)||Number(job.progress?.time_limit_seconds)/60||fallback;
  return Number.isFinite(own)&&Number.isFinite(maximum)?Math.max(0,Math.floor(maximum-own)):0;
}

/** A group run (job input generation_scope): who it schedules and how everyone else's placements are protected. */
export type ScopeProtection='keep_existing'|'rebalance_selected';
export type ScopeCapability={fields:string[];protections:ScopeProtection[];default_protection:ScopeProtection};
const PROTECTIONS:readonly unknown[]=['keep_existing','rebalance_selected'];
/** What the server says a run accepts (generation_capability.generation_scope). Absent or malformed: no group runs
 *  here, and the selector is not shown. A default it does not offer falls back to keeping existing assignments. */
export function scopeCapability(capability:unknown):ScopeCapability|null{
  const s=(capability as {generation_scope?:{fields?:unknown;protections?:unknown;default_protection?:unknown}}|null|undefined)?.generation_scope;
  if(!s||!Array.isArray(s.fields)||!s.fields.length||!s.fields.every(f=>typeof f==='string'))return null;
  const protections=Array.isArray(s.protections)?s.protections.filter((p):p is ScopeProtection=>PROTECTIONS.includes(p)):[];
  if(!protections.includes('keep_existing'))return null;
  const given=s.default_protection as ScopeProtection;
  return {fields:s.fields as string[],protections,default_protection:protections.includes(given)?given:'keep_existing'};
}

const CHOICE_OPS=new Set(['any','all','none']);
const line=(v:unknown,fields:readonly string[]|null):Condition|null=>{
  const c=v as Record<string,unknown>;
  if(!c||typeof c!=='object'||Array.isArray(c)||typeof c.field!=='string'||fields&&!fields.includes(c.field))return null;
  if(!CHOICE_OPS.has(String(c.op??'any'))||!Array.isArray(c.values)||!c.values.length||!c.values.every(x=>typeof x==='string'))return null;
  return {field:c.field,op:(c.op??'any') as Condition['op'],values:[...c.values as string[]]};
};
/** A people choice read back from storage or a job (never trusted): choice lines, one level of groups as the builder
 *  makes them, and only these fields when given. Anything else drops the whole choice (null), never part of it. */
export function parseGroup(v:unknown,fields:readonly string[]|null=null,depth=0):Conditions|null{
  const t=v as Record<string,unknown>;
  if(!t||typeof t!=='object'||Array.isArray(t)||!Array.isArray(t.conditions)||!t.conditions.length)return null;
  const match=t.match??'all';if(match!=='all'&&match!=='any')return null;
  const out:(Condition|Conditions)[]=[];
  for(const c of t.conditions){const n=c&&typeof c==='object'&&'conditions' in c?depth?null:parseGroup(c,fields,1):line(c,fields);if(!n)return null;out.push(n);}
  return {match,conditions:out};
}
