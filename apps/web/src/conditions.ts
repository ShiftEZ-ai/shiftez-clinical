import {el,select,multiPick,iconButton,button,armed,icon,menu as openMenu,opens,departmentToday,departmentTimeZone,toLocal,rankColour,type Choice,type IconName,type MenuItem} from './dom.js';
import type {Condition,Conditions,RelativeOperand,ConditionDateParseResult} from './generated.js';
import {CONDITION_FIELD_DEFINITIONS,CONDITION_OPERATORS,RELATIVE_TOKEN_CATALOGUE,TABLE_CONDITION_DEFINITIONS} from './generated.js';
import {openPicker} from './picker.js';
import {dateInput,parseDay,showDay,shiftDay} from './date_picker.js';
import {background,request,sessionKey,sessionChanged,onSessionReset} from './api.js';
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)

/** A field's type (schemas.ConditionFieldKind): which operators, value picker and relative values it has. */
export type ValueKind='date'|'text'|'number'|'people'|'single_choice'|'multiple_choice'|'tags';
/** Where a condition is evaluated (schemas.ConditionContext): rules and audiences per duty, the rest per viewer and day. */
export type ConditionContext='filter'|'selection'|'bulk'|'rule'|'audience';
/** Something that can be filtered on: a label, its choices, and whether new values may be typed (tags). */
export type ConditionField={key:string;label:string;choices:Choice[];creatable?:boolean;group?:string;
  /** Its type, when the key is not a contract field (a list's own date column); contract fields take theirs. */kind?:ValueKind;
  /** The contract field a list's own column stands for (a Person column: `person_ids`): which viewer values apply. */operand?:string;
  /** Several dates of one property (Leave dates · Start / End): they share this name on the field chip and the part is
   *  chosen beside it. */parent?:string;
  /** Shown in place of the values when there are none to choose ("No tags yet · Create tags"). */empty?:{text:string;action?:{label:string;run:()=>void}};
  /** Named parts of the chosen values a line may narrow to (a date set's entries: `Condition.entry_names`); none chosen
   *  means every part. */entries?:(values:string[])=>Choice[];
  /** Inactive records hidden from the list unless chosen (B3): their ids, the server's count and noun ("clinics"). */inactive?:{ids:Set<string>;count:number;noun:[string,string]}};
/** Keep a saved entry name visible and removable when it is no longer among the selected sets' entries. */
export const withUnavailableEntries=(choices:Choice[],names:string[]):Choice[]=>[...choices,
  ...[...new Set(names)].filter(n=>!choices.some(c=>c.value===n)).map(value=>({value,label:`${value} · unavailable`}))];
type Node=Condition|Conditions;
const isGroup=(n:Node):n is Conditions=>'conditions' in n;

// ---- Operands (schemas.ConditionOperand): plain literals, {kind:'absolute', value} or {kind:'relative', token, n?, unit?} ----
type Operand=NonNullable<Condition['value']> extends infer V?V extends readonly unknown[]?never:V:never;
type Token=typeof RELATIVE_TOKEN_CATALOGUE[number];
const DEFS=new Map<string,typeof CONDITION_FIELD_DEFINITIONS[number]>(CONDITION_FIELD_DEFINITIONS.map(d=>[d.key,d]));
const TOKENS=new Map<string,Token>(RELATIVE_TOKEN_CATALOGUE.map(t=>[t.token,t]));
export const isRelative=(o:unknown):o is RelativeOperand=>!!o&&typeof o==='object'&&(o as {kind?:string}).kind==='relative';
const absolute=(o:unknown)=>o&&typeof o==='object'&&'kind' in (o as object)?(o as {value:unknown}).value:o;
/** The absolute values a line names, in order: its plain `values`, or a typed operand's literal value(s) ({kind:'absolute'}
 *  or raw). A relative operand ("Me") names none here: it means something only once resolved. Every reader of a
 *  line's named values (rule mentions, facets, summaries, forms) reads them through this one place. */
export const literalValues=(c:Condition):string[]=>c.value===undefined||c.value===null?[...(c.values||[])]
  :[c.value].flat().filter(o=>!isRelative(o)).flatMap(o=>[absolute(o)].flat()).filter(v=>v!==null&&v!==undefined&&v!=='').map(String);
/** A form's own fields in a scope's lines (protected time: programme, cohorts, rotation types): each field's absolute
 *  values, plain or typed, for its picker; every other line — a relative operand of an own field included ("my cohort")
 *  — is `kept` as it is, so saving the form never drops it. */
export function ownLines(lines:Node[],own:readonly string[]){
  const mine=(c:Node):c is Condition=>!isGroup(c)&&own.includes(c.field)&&!hasRelative(c);
  return {values:(field:string)=>{const c=lines.find(x=>mine(x)&&x.field===field) as Condition|undefined;return c?literalValues(c):[];},kept:lines.filter(c=>!mine(c))};
}
/** Whether a line excludes what it names (Is not). */
export const negativeOp=(op?:string|null)=>op==='none'||op==='is_not';
/** Choice values compared as the server compares them: tags by lower() (legacy, not Unicode case folding), others as stored. */
const comparable=(field:string)=>(v:unknown)=>field==='tags'?String(v).toLowerCase():String(v);
const TYPED=new Set<string>(['date','text','number']);
/** A stored exclusive end (ends_on, end: the day after the last day) by the registry's `exclusive_last_day`, in a
 *  list's own table or the contract's fields: matched as its last day shown (the server compares the same way). */
export const exclusiveLastDay=(field:string,tableKey?:string)=>{const t=tableKey?(TABLE_CONDITION_DEFINITIONS as unknown as Record<string,Record<string,{exclusive_last_day?:boolean}>>)[tableKey]?.[field]:undefined;
  return !!(t?t.exclusive_last_day:(DEFS.get(field) as {exclusive_last_day?:boolean}|undefined)?.exclusive_last_day);};
/** A stored exclusive end as the last day shown (once, on the row's raw value; operands are never shifted). */
export const shownLastDay=(v:unknown)=>typeof v!=='string'||!/^\d{4}-\d{2}-\d{2}/.test(v)?v
  :/[T ]\d/.test(v)?toLocal(new Date(Date.parse(v)-1).toISOString(),departmentTimeZone()).slice(0,10):shiftDay(v.slice(0,10),-1);
/** A date or date-time as its day in the department's timezone (as the server reads it). */
const localDay=(v:unknown)=>{const s=String(v);return /[T ]\d/.test(s)?toLocal(new Date(s).toISOString(),departmentTimeZone()).slice(0,10):s.slice(0,10);};
const NO_VALUE=new Set<string>(['empty','not_empty']);
export const kindOf=(f:{key:string;kind?:ValueKind}|undefined,key=f?.key||''):ValueKind=>f?.kind||DEFS.get(key)?.value_kind as ValueKind||'multiple_choice';
/** A field holding dates, text or numbers (not choices): it compares, it is not chosen from a list. */
export const isTyped=(key:string)=>TYPED.has(kindOf(undefined,key));
/** Whether a line or tree holds a relative value anywhere ("This month", "Me"). */
export const hasRelative=(t:Node|null|undefined):boolean=>!!t&&(isGroup(t)?t.conditions.some(hasRelative):[t.value].flat().some(isRelative));

/** The chip colour of a who or where field, by kind (`.rp`): people blue, clinics, places and duty types teal, a
 *  programme or any other group purple (a #tag keeps its own hue, `Choice.tone`; a rank its own tint, `valueTone`). */
const PILL:Record<string,string>={person_ids:'person',usual_assignee_ids:'person',service_ids:'place',location_ids:'place',assignment_type_ids:'place'};
export const pillKind=(field:string)=>PILL[field]||'group';
/** What a field's values are, counted ("rank", "ranks"): the contract's plural; its singular drops the "s" of the
 *  first word ending in one ("clinics they can work at" → "clinic they can work at"), or "person" for "people".
 *  shortcut: English plurals in the contract all end in "s" but "people"; add a singular to the contract if one does not. */
export const fieldNoun=(field:string):readonly [string,string]|null=>{const many=(DEFS.get(field) as {plural?:string}|undefined)?.plural;
  return many?[many==='people'?'person':many.replace(/\b(\w+)s\b/,'$1'),many]:null;};
/** The chip colour of one value: a rank (any `…category_ids` field) its one rank tint (`rankColour`, C21), else its kind. */
export const valueTone=(field:string,value:unknown)=>/category_ids?$/.test(field)&&typeof value==='string'&&value&&!value.startsWith('@')?'duty-'+rankColour(value):pillKind(field);
const KIND_ICON:Record<ValueKind,IconName>={date:'calendar',text:'menu',number:'chart',people:'people',single_choice:'layers',multiple_choice:'layers',tags:'bookmark'};
const fieldIcon=(f:ConditionField)=>pillKind(f.key)==='place'?'pin' as IconName:KIND_ICON[kindOf(f)];

/** "Has all of" only means something where one person holds several values (ranks, tags, skills, programmes…) or one
 *  day sits in several date sets: never a person (one identity) nor another session field (a session is at one place,
 *  of one duty type; a session's usual people are several) — schemas.Condition says so. */
export const allowsAll=(field:string)=>field==='date_set_ids'||field==='usual_assignee_ids'||field!=='person_ids'&&!SESSION_FIELDS.includes(field);

// ---- Relative values resolved by the server (POST /api/conditions/resolve-operands): one calendar, one evaluator ----
/** What a relative value means now, for this viewer: its dates (inclusive), the record ids, or a number; `display`
 *  in words ("3 + sub-clinics", "12 people"). */
export type Resolved={label:string;display:string;from?:string|null;to?:string|null;ids?:unknown[]|null;value?:unknown};
/** Where values are resolved: the context, the list's own table (its columns), the record type and the As of day. */
export type ResolveScope={context:ConditionContext;tableKey?:string;collection?:string;asOf?:string};
const resolved=new Map<string,Resolved>(),failed=new Map<string,string>();
// Per account and day: a long session crossing midnight asks again (the browser's day only names the cache).
const resolveKey=(field:string,o:unknown,s:ResolveScope)=>[sessionKey(),departmentToday(),s.context,s.tableKey||'',s.collection||'',s.asOf||'',field,JSON.stringify(o)].join('|');
/** A value the server already resolved in this scope (undefined while it is being asked or could not be). */
export const resolvedNow=(field:string,o:unknown,s:ResolveScope)=>resolved.get(resolveKey(field,o,s));
/** Why a value could not be resolved here (the server refused or was unreachable): the list says so, with Retry, and
 *  holds back what depends on its matches; empty while it is resolved or still being asked. */
export const resolveFailure=(field:string,o:unknown,s:ResolveScope)=>failed.get(resolveKey(field,o,s))||'';
/** Ask again for values that could not be resolved. */
export const retryResolve=(items:{field:string;operand:unknown}[],s:ResolveScope)=>{for(const x of items)failed.delete(resolveKey(x.field,x.operand,s));return resolveOperands(items,s);};
/** Forget every resolved value and viewer catalogue (the viewer's facts, permissions or the calendar may have changed). */
export const forgetResolved=()=>{generation++;resolved.clear();failed.clear();asking.clear();catalogues.clear();};
/** Values the server cannot resolve without a duty or a row (rules check each duty; a record's own target). */
export const resolvable=(o:unknown)=>isRelative(o)&&!(TOKENS.get(o.token)?.requirements as readonly string[]|undefined)?.some(r=>r==='duty_day'||r==='target')
  &&(!TOKENS.get(o.token)?.needs_n||!!o.n);
const asking=new Map<string,Promise<void>>();
// Bumped by forgetResolved: a reply to a request asked before it is stale and changes nothing.
let generation=0;
/** Resolve these relative values in one request (those not known yet); a refused batch is asked one by one, so one value
 *  that means nothing here never hides the others. */
export function resolveOperands(items:{field:string;operand:unknown}[],s:ResolveScope,again=false):Promise<void>{
  const todo=[...new Map(items.filter(x=>resolvable(x.operand)&&!resolved.has(resolveKey(x.field,x.operand,s))&&!failed.has(resolveKey(x.field,x.operand,s))).map(x=>[resolveKey(x.field,x.operand,s),x])).entries()];
  if(!todo.length)return Promise.resolve();
  const batchKey=todo.map(([k])=>k).join('|');
  if(asking.has(batchKey))return asking.get(batchKey)!;
  const body=(list:typeof todo)=>JSON.stringify({context:s.context,...(s.tableKey?{table_key:s.tableKey}:{}),...(s.collection?{collection:s.collection}:{}),...(s.asOf?{as_of:s.asOf}:{}),
    operands:list.map(([,x])=>({field:x.field,operand:x.operand}))});
  const gen=generation,stale=()=>gen!==generation,session=sessionKey();
  const ask=(list:typeof todo)=>request<{results:Resolved[]}>('/api/conditions/resolve-operands',{method:'POST',body:body(list)}).then(r=>{if(!stale())list.forEach(([k],i)=>resolved.set(k,r.results[i]));});
  // A value the server could not resolve is recorded as such (never silently ignored as "matches everything").
  const fail=(list:typeof todo,e:unknown)=>{if(!sessionChanged(e)&&!stale())for(const [k] of list)failed.set(k,(e as Error)?.message||'could not be worked out');};
  const p:Promise<void>=ask(todo).catch(e=>sessionChanged(e)||stale()?undefined:todo.length>1?Promise.all(todo.map(x=>ask([x]).catch(err=>fail([x],err)))).then(()=>undefined):fail(todo,e))
    // A reply made stale by a save (forgetResolved) is dropped: ask once more, so the caller never waits on nothing.
    .then(()=>stale()&&!again&&sessionKey()===session?resolveOperands(items,s,true):undefined)
    .finally(()=>{if(asking.get(batchKey)===p)asking.delete(batchKey);});
  asking.set(batchKey,p);return p;
}
/** The first and last day (both included) of a date value: an exact one's own, a relative one's from the server (null
 *  while unknown, or for a value that depends on each duty). */
export function dateBounds(o:unknown,field='date',s?:ResolveScope):[string,string]|null{
  if(isRelative(o)){const r=s&&resolvedNow(field,o,s);return r?.from&&r.to?[r.from,r.to]:null;}
  const v=absolute(o);const [a,b]=Array.isArray(v)?v.map(String):[String(v??''),String(v??'')];
  return a&&b?[a.slice(0,10),b.slice(0,10)].sort() as [string,string]:null;
}
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
/** "18 Oct", "1–31 Oct", "29 Nov – 26 Dec", "29 Dec 2026 – 4 Jan 2027" (the year only when it is not this one). */
export function rangeWords([a,b]:[string,string],year=departmentToday().slice(0,4)):string{
  const p=(d:string)=>({d:+d.slice(8,10),m:MON[+d.slice(5,7)-1],y:d.slice(0,4)}),x=p(a),y=p(b),yr=(q:typeof x)=>q.y===year?'':' '+q.y;
  if(a===b)return `${x.d} ${x.m}${yr(x)}`;
  if(x.y!==y.y)return `${x.d} ${x.m} ${x.y} – ${y.d} ${y.m} ${y.y}`;
  return x.m===y.m?`${x.d}–${y.d} ${y.m}${yr(y)}`:`${x.d} ${x.m} – ${y.d} ${y.m}${yr(y)}`;
}
const unitWord=(n:number,unit?:string|null)=>n===1?String(unit||'days').replace(/s$/,''):String(unit||'days');
/** A value in words: "This month", "Next 2 weeks", "Within 3 days before the duty", "Thu 1 Oct 2026", "Me", "12". */
export function operandText(o:unknown,label:(value:string)=>string=v=>v):string{
  if(isRelative(o)){const n=o.n||0;
    if(o.token==='last_n'||o.token==='next_n')return `${o.token==='last_n'?'Last':'Next'} ${n} ${unitWord(n,o.unit)}`;
    if(o.token==='days_before_duty'||o.token==='days_after_duty')return `Within ${n} ${unitWord(n,'days')} ${o.token==='days_before_duty'?'before':'after'} the duty`;
    return TOKENS.get(o.token)?.label||o.token;}
  const v=absolute(o);
  if(Array.isArray(v))return v.every(x=>typeof x==='string'&&/^\d{4}-\d\d-\d\d$/.test(x))&&v.length===2?rangeWords([String(v[0]),String(v[1])]):v.map(x=>label(String(x))).join(', ');
  return typeof v==='string'&&/^\d{4}-\d\d-\d\d$/.test(v)?showDay(v):label(String(v??''));
}

/** Plain-words operator names (the operators themselves come from the contract, per field type). */
const OP_WORDS:Record<string,string>={any:'Is',all:'Has all of',none:'Is not',is:'Is',is_not:'Is not',before:'Is before',after:'Is after',
  on_or_before:'Is on or before',on_or_after:'Is on or after',between:'Is between',within:'Is within',empty:'Is empty',not_empty:'Is not empty',
  contains:'Contains',not_contains:'Does not contain',starts_with:'Starts with',ends_with:'Ends with',gt:'Is more than',gte:'Is at least',lt:'Is less than',lte:'Is at most'};
const opWord=(op:string,many=false)=>op==='any'&&many?'Is any of':OP_WORDS[op]||op;

/** The one walk of a condition tree (All / Any groups) in the browser; `line` decides each line (lineMatches, or a caller's
 *  "not known yet" leniency around it). Used by list filters (table_view holds) and the lead hint (forms leadBlock). */
export function treeMatches(n:Condition|Conditions,line:(c:Condition)=>boolean):boolean{
  return 'conditions' in n?(n.match==='any'?n.conditions.some(x=>treeMatches(x,line)):n.conditions.every(x=>treeMatches(x,line))):line(n);
}
/** Whether a row's values for one line meet it (the client's mirror of conditions._operand_line_values, for lists filtered
 *  in the browser). `got`: the row's values for the field, dates as yyyy-mm-dd. */
export function lineMatches(c:Condition,got:unknown[],kind:ValueKind=kindOf(undefined,c.field),scope?:ResolveScope,ids?:unknown[]):boolean{
  const op=c.op||'any',present=(v:unknown)=>v!==null&&v!==undefined&&v!=='',held=got.filter(present);
  // Is empty reads the row's own values (its ids when it has them), never a placeholder shown for none ("No current rank").
  if(op==='empty'||op==='not_empty'){const raw=(ids??got).filter(present);return op==='empty'?!raw.length:raw.length>0;}
  const same=comparable(c.field);
  if(c.value===undefined||c.value===null){const have=held.map(same),want=c.values.map(same);  // tags as the server reads them
    return op==='none'?!want.some(v=>have.includes(v)):op==='all'?want.length>0&&want.every(v=>have.includes(v)):!want.length||want.some(v=>have.includes(v));}
  if(!held.length)return op==='none'||op==='is_not'||op==='not_contains';
  const operands=Array.isArray(c.value)?c.value:[c.value];
  if(kind==='date'){
    // A relative value not resolved yet (or decided per duty) holds back nothing until the server answers.
    const b=operands.map(x=>dateBounds(x,c.field,scope));if(b.some(x=>!x))return true;
    const [from,to]=[b.map(x=>x![0]).sort()[0],b.map(x=>x![1]).sort().pop()!],days=held.map(localDay);
    return days.some(d=>op==='before'?d<from:op==='after'?d>to:op==='on_or_before'?d<=to:op==='on_or_after'?d>=from:op==='is_not'||op==='none'?d<from||d>to:d>=from&&d<=to);}
  if(isRelative(operands[0])){  // a viewer's value (Me, Clinics I can work at): the server's ids against the row's ids
    const r=scope&&resolvedNow(c.field,operands[0],scope);if(!r)return true;
    if(r.ids){const want=r.ids.map(String),have=(ids||held).map(String);
      // Has all of: every value the server named (as the server requires); is / is any of: one of them.
      if(op==='all')return want.length>0&&want.every(v=>have.includes(v));const hit=want.some(v=>have.includes(v));return op==='none'||op==='is_not'?!hit:hit;}
    const w=Number(r.value);return held.map(Number).some(v=>op==='gt'?v>w:op==='gte'?v>=w:op==='lt'?v<w:op==='lte'?v<=w:op==='is_not'?v!==w:v===w);}
  if(kind==='number'){const nums=operands.map(x=>Number(absolute(x))),lo=Math.min(...nums),hi=Math.max(...nums),w=nums[0];
    return held.map(Number).some(v=>op==='between'?v>=lo&&v<=hi:op==='gt'?v>w:op==='gte'?v>=w:op==='lt'?v<w:op==='lte'?v<=w:op==='is_not'?v!==w:v===w);}
  if(kind==='text'){const w=String(absolute(operands[0])).toLowerCase();
    return held.map(v=>String(v).toLowerCase()).some(v=>op==='contains'?v.includes(w):op==='not_contains'?!v.includes(w):op==='starts_with'?v.startsWith(w):op==='ends_with'?v.endsWith(w):op==='is_not'?v!==w:v===w);}
  const want=literalValues(c).map(same),have=held.map(same);
  return negativeOp(op)?!want.some(v=>have.includes(v)):op==='all'?want.length>0&&want.every(v=>have.includes(v)):want.some(v=>have.includes(v));
}

// ---- The viewer's relative values (GET /api/conditions/catalogue): hidden, not disabled, where they mean nothing ----
type Catalogue={tokens:Token[]};
const catalogues=new Map<string,Promise<Catalogue>>();
onSessionReset(forgetResolved);
// One's own saved change (a permission, a membership, the calendar) can change what "Me" or "My clinics" mean.
if(typeof window!=='undefined')window.addEventListener('rota-saved',forgetResolved);
function tokenCatalogue(context:ConditionContext,asOf='',collection='',tableKey=''):Promise<Catalogue>{
  const q=new URLSearchParams({context,...(asOf?{as_of:asOf}:{}),...(collection?{collection}:{}),...(tableKey?{table_key:tableKey}:{})}).toString(),key=sessionKey()+'|'+q;
  // A record type the server cannot measure (its target, the department average) still has the calendar's and viewer's.
  if(!catalogues.has(key)){const p=background<Catalogue>('/api/conditions/catalogue?'+q).catch(e=>!sessionChanged(e)&&(collection||tableKey)?tokenCatalogue(context,asOf):{tokens:[]});catalogues.set(key,p);}
  return catalogues.get(key)!;
}
const tokensFor=(all:Token[],f:ConditionField)=>{const kind=kindOf(f),key=f.operand||f.key;
  // A list's own column takes the viewer's values only when it says which contract field it stands for (and has its ids).
  return all.filter(t=>(t.field_kinds as readonly string[]).includes(kind)&&(!t.fields.length||(t.fields as readonly string[]).includes(key))&&(kind==='date'||DEFS.has(key)));};
const DUTY='Relative to the duty';

/** One labelled segmented choice ("Count: Together · Each", "Match: Any · All"): words on the buttons, the meaning in
 *  each tooltip; the row says the chosen one in plain words beside it (`.cond-hint`). */
function segmentedChoice<T extends string>(label:string,cls:string,options:[T,string,string][],value:T,set:(v:T)=>void):HTMLElement{
  return el('span',{class:'cond-choice'},el('span',{class:'cond-choice-label','aria-hidden':'true'},label),
    segmented(label,options.map(([v,text,tip])=>[v,text,{tip}]),value,set,{cls}));
}
/** Count (Together | Each), visible with its words reviewed. */
export function countSwitch(value:'together'|'each',set:(v:'together'|'each')=>void,noun='values'):HTMLElement{
  return segmentedChoice('Count','cond-count',[['together','Together',`One count for all ${noun}`],['each','Each',`A separate count for each of the ${noun}`]],value,set);
}
/** "all clinics counted together" / "each clinic counted separately": what Count means for these values. */
export const countHint=(each:boolean,noun:string)=>each?`each ${noun.replace(/s$/,'')} counted separately`:`all ${noun} counted together`;
/** The one mark a table shows for a who or where that counts per value (nothing for the default, together). */
export const eachMark=()=>el('span',{class:'cond-each-mark',role:'img','aria-label':'Counted for each','data-tip':'Counted for each'},icon('each'));
export const hasEach=(t:Node|null|undefined):boolean=>!!t&&(t.count==='each'&&(isGroup(t)||t.op!=='none')||isGroup(t)&&t.conditions.some(hasEach));

export type BuilderOptions={
  /** Offer "has all of" on person fields with several values each (rule lines). */allOf?:boolean;
  /** Whether a Count switch may show on a row or group filtering these fields (absent: never). It shows only where it
   *  changes the result: not on "is not", and only over more than one value (a #tag is many). */count?:(fields:string[])=>boolean;
  /** Start without a blank filter: no filter is everyone / everywhere (rule lines). */startEmpty?:boolean;
  /** The first row's word (default "Where"); a section already headed "Who" leaves it empty (F-R4). */lead?:string;
  /** Shown as it is, nothing to change (read-only roles). */readOnly?:boolean;
  /** Where the conditions are evaluated: which relative values the value picker offers (the viewer's, per the server's
   *  catalogue; rules and audiences only "relative to the duty"). Absent: exact values only. */context?:ConditionContext;
  /** The day dated facts are read on (the list's As of), the record type and the list's own table (its columns), for the
   *  viewer's values. */asOf?:()=>string;collection?:string;tableKey?:string};

/** Notion-style filters: [Where | And▾/Or▾] [field▾] [part▾] [operator▾] [value▾] [⋯], with one level of groups. One
 *  joiner per level; choosing the other joiner on a later row splits the level there into two groups (each keeps its
 *  joiner), so the meaning is never ambiguous. Generic (lists, selections, rule scopes, audiences): pass the fields, get
 *  the tree back from read(). Rows without a value are left out; read() returns null when nothing is chosen. */
export function conditionBuilder(fields:ConditionField[],given?:Conditions|null,o:BuilderOptions={}){
  const byKey=new Map(fields.map(f=>[f.key,f]));
  const blankOf=(f:ConditionField):Condition=>({field:f.key as Condition['field'],op:TYPED.has(kindOf(f))?'is':'any',values:[]});
  const blank=()=>blankOf(fields[0]);
  const copy=<T,>(t:T):T=>JSON.parse(JSON.stringify(t));
  const model:Conditions=given?.conditions?.length?normalise(copy(given)):{match:'all',conditions:o.startEmpty?[]:[blank()]};
  const root=el('div',{class:'cond-root'});
  let tokens:Token[]=[];
  // The viewer's relative values arrive once (cached per account); pickers opened before then wait for them.
  const ready:Promise<void>=o.context&&!o.readOnly?tokenCatalogue(o.context,o.asOf?.()||'',o.collection||'',o.tableKey||'').then(c=>{tokens=c.tokens;}):Promise.resolve();
  // Relative values are resolved by the server (never with this browser's clock); rules resolve them per duty.
  const scope:ResolveScope|undefined=o.context&&o.context!=='rule'&&o.context!=='audience'?{context:o.context,tableKey:o.tableKey,collection:o.collection,asOf:o.asOf?.()||''}:undefined;
  const repaint=new Set<{node:()=>HTMLElement|undefined;draw:()=>void}>();
  /** After the server answered: each value redraws with its dates or hint, unless its list is open. */
  const refresh=()=>{for(const r of repaint){const n=r.node();if(!n?.isConnected){repaint.delete(r);continue;}if(!n.querySelector('[aria-expanded=true]'))r.draw();}};
  // Redraw only when something new was resolved: a value that could not be resolved never asks (and redraws) again by itself.
  const resolve=(items:{field:string;operand:unknown}[])=>{if(!scope)return Promise.resolve();
    const before=items.filter(x=>resolvedNow(x.field,x.operand,scope)).length;
    return resolveOperands(items,scope).then(()=>{if(items.filter(x=>resolvedNow(x.field,x.operand,scope)).length>before)refresh();});};
  // Every listed value's dates or hint, in one request once the catalogue is in (the picker shows them beside each).
  const hinted=ready.then(()=>resolve(fields.flatMap(f=>relativeOf(f).filter(t=>!t.needs_n).map(t=>({field:f.key,operand:{kind:'relative',token:t.token}}))))).catch(()=>undefined);
  let loaded=!o.context||!!o.readOnly;
  const relativeOf=(f:ConditionField)=>o.context?tokensFor(tokens,f):[];
  const nodes=new WeakMap<Condition,HTMLElement>();
  const isTag=(field:string,v:string)=>byKey.get(field)?.choices.find(c=>c.value===v)?.group==='Tags';
  const fieldsOf=(n:Node):string[]=>isGroup(n)?n.conditions.flatMap(fieldsOf):[n.field];
  const many=(c:Condition)=>c.values.length>1||c.values.some(v=>isTag(c.field,v));
  const counts=(n:Node)=>!!o.count?.(fieldsOf(n))&&(isGroup(n)?n.conditions.length>1:n.op!=='none'&&n.value==null&&!NO_VALUE.has(n.op||'any')&&many(n));
  const nounOf=(n:Node)=>isGroup(n)?'filters':(byKey.get(n.field)?.label.toLowerCase()||'value')+'s';
  const setCount=(n:Node,v:'together'|'each')=>{if(v==='each')n.count='each';else delete n.count;};
  /** Choice lines keep their values in `values` (an absolute operand reads as them); is / is not are "any" / "none". */
  function normalise(t:Conditions):Conditions{
    for(const n of t.conditions){if(isGroup(n)){normalise(n);continue;}
      if(TYPED.has(kindOf(byKey.get(n.field),n.field))||isRelative(n.value)||n.value==null)continue;
      n.values=[absolute(n.value)].flat().map(String);delete n.value;if(n.op==='is')n.op='any';else if(n.op==='is_not')n.op='none';}
    return t;
  }

  function opChoices(c:Condition):Choice[]{
    const f=byKey.get(c.field),kind=kindOf(f,c.field),ops=[...(CONDITION_OPERATORS[kind]||CONDITION_OPERATORS.multiple_choice)] as string[];
    const all=o.allOf&&allowsAll(c.field);
    const list=TYPED.has(kind)?ops:ops.filter(x=>x!=='is'&&x!=='is_not'&&(x!=='all'||all));
    return list.map((v,i)=>({value:v,label:opWord(v,!!all||c.values.length>1),...(NO_VALUE.has(v)&&!NO_VALUE.has(list[i-1])?{divider:true}:{}),...(v==='within'?{hint:'a moving range'}:{})}));
  }
  /** The field chip lists each property once (several dates of one property under its name); the part chip picks the date. */
  const fieldChoices=()=>{const seen=new Set<string>();
    return fields.filter(f=>!f.parent||!seen.has(f.parent)&&!!seen.add(f.parent)).map(f=>({value:f.key,label:f.parent||f.label,group:f.group}));};

  /** The value chip of a date, number or text line: a relative value shows its clock, name and today's dates. */
  function valueChip(c:Condition,f:ConditionField,at:0|1,set:(v:Operand|null)=>void):HTMLElement{
    const v=Array.isArray(c.value)?c.value[at]:at?null:c.value;
    const b=el('button',{type:'button',class:'cond-value',...popup('dialog')}) as HTMLButtonElement;
    const bounds=v!=null&&kindOf(f)==='date'?dateBounds(v,f.key,scope):null,name=v==null?'Choose…':operandText(v);
    const known=isRelative(v)&&scope?resolvedNow(f.key,v,scope):undefined,hint=bounds?rangeWords(bounds):known?.display||'';
    if(isRelative(v)&&!known&&!(scope&&resolveFailure(f.key,v,scope)))void resolve([{field:f.key,operand:v}]);
    b.append(...(isRelative(v)?[icon('clock')]:[]),el('span',{class:'cond-value-name'+(v==null?' muted':'')},name),
      ...(isRelative(v)&&hint?[el('span',{class:'cond-value-dates'},hint)]:[]),icon('chevronDown'));
    const whole=bounds?`${showDay(bounds[0])}${bounds[0]!==bounds[1]?' – '+showDay(bounds[1]):''}`:'';
    b.setAttribute('aria-label',`${f.parent?f.parent+' · ':''}${f.label} value: ${name}${whole&&isRelative(v)?', today '+whole:''}`);
    if(isRelative(v))b.dataset.tip=bounds?`${name}: ${whole} · moves with today`:scope?`${name}${hint?': '+hint:''}`:`${name}: each duty's own day`;
    b.addEventListener('click',()=>openValue(b,c,f,set));
    return b;
  }
  /** The one value picker for dates, numbers and text: search on top, Relative first (the viewer's values, or only
   *  "relative to the duty" in rules), then the exact value (the shared date field and calendar, or what is typed). */
  function openValue(anchor:HTMLButtonElement,c:Condition,f:ConditionField,set:(v:Operand|null)=>void,query=''){
    if(!loaded){void hinted.then(()=>{if(anchor.isConnected)openValue(anchor,c,f,set,query);});return;}
    const kind=kindOf(f),rule=o.context==='rule'||o.context==='audience',within=c.op==='within',ops=new Map<string,Operand>();
    const opt=(x:Operand,hint=''):Choice=>{const value='op:'+JSON.stringify(x);ops.set(value,x);
      const b=kind==='date'?dateBounds(x,f.key,scope):null;return {value,label:operandText(x),group:rule?DUTY:'Relative',glyph:'clock',hint:hint||(b?rangeWords(b):rule?'each duty':'')};};
    // A value with an N first asks for it: picking "Next N days…" fills the search with an example to change.
    const example:Record<string,string>={last_n:'last 7 days',next_n:'next 7 days',days_before_duty:'7 days before the duty',days_after_duty:'7 days after the duty'};
    const choices=relativeOf(f).map(t=>t.needs_n?{value:'n:'+t.token,label:t.label,group:rule?DUTY:'Relative',glyph:'clock' as IconName,hint:'choose N'}:opt({kind:'relative',token:t.token} as Operand));
    const typedOne=(q:string):Choice[]=>{
      const duty=/^(?:within )?(\d{1,3}) days? (before|after)(?: the duty)?$/i.exec(q);
      if(duty&&relativeOf(f).some(t=>t.token===`days_${duty[2].toLowerCase()}_duty`))return [opt({kind:'relative',token:`days_${duty[2].toLowerCase()}_duty`,n:+duty[1]} as Operand)];
      if(within)return [];
      if(kind==='date'){const d=parseDay(q);return d?[{...opt({kind:'absolute',value:d}),group:'Exact',glyph:'calendar',hint:''}]:[];}
      if(kind==='number')return q.trim()!==''&&Number.isFinite(Number(q))?[{...opt({kind:'absolute',value:Number(q)}),label:`Use ${Number(q)}`,group:'Exact',glyph:'edit' as IconName,hint:''}]:[];
      return [{...opt({kind:'absolute',value:q}),label:`Use “${q}”`,group:'Exact',glyph:'edit' as IconName,hint:''}];};
    const lookup=async(q:string):Promise<Choice[]>=>{
      let found:Choice[]=[];
      if(kind==='date'&&!rule&&o.context){
        const r=await request<ConditionDateParseResult>('/api/conditions/parse-date',{method:'POST',body:JSON.stringify({text:q,context:o.context,...(o.asOf?.()?{as_of:o.asOf()}:{})})}).catch(()=>({matches:[]}));
        // The server's reading, with its dates (kept, so the chip shows them at once).
        found=r.matches.filter(m=>!within||isRelative(m.operand)).map(m=>{if(scope&&isRelative(m.operand))resolved.set(resolveKey(f.key,m.operand,scope),{label:m.label,display:m.display,from:m.from,to:m.to});
          return {...opt(m.operand as Operand,m.from&&m.to?rangeWords([m.from,m.to]):m.display),group:isRelative(m.operand)?'Relative':'Exact'};});}
      const own=typedOne(q).filter(x=>!found.some(y=>y.value===x.value));
      const words=q.toLowerCase();
      return [...found,...own,...(found.length||own.length?[]:choices.filter(x=>x.label.toLowerCase().includes(words)))];};
    const exact=kind==='date'&&!within?(()=>{const input=el('input',{'aria-label':`${f.label}: exact date`,value:String(absolute(Array.isArray(c.value)?null:c.value)??'').slice(0,10)}) as HTMLInputElement;
      const box=dateInput(input);input.addEventListener('change',()=>{if(input.value){closeIt();set({kind:'absolute',value:input.value} as Operand);}});
      return el('div',{class:'picker-exact'},el('div',{class:'picker-group'},el('span',{},'Exact date')),box);})():undefined;
    const footer=kind!=='date'?undefined:rule?'Rules check each duty on its own day, so only dates relative to the duty — or an exact date — can be used here.':'Last / Next N always include today.';
    const closeIt=openPicker(anchor,anchor.parentElement||root,{label:`${f.label} value`,choices,multiple:false,selected:new Set(),query,lookup,extra:exact,footer,
      emptyText:kind==='date'?'':kind==='number'?'Type a number':'Type the text to compare',
      onClear:()=>set(null),onPick:value=>{
        if(value.startsWith('n:')){const token=value.slice(2);requestAnimationFrame(()=>openValue(anchor,c,f,set,example[token]||''));return;}
        const x=ops.get(value);if(x!==undefined)set(x);}});
  }

  function line(c:Condition,remove:()=>void):HTMLElement{
    const f=byKey.get(c.field)||{key:c.field,label:c.field,choices:[]},kind=kindOf(f,c.field),typed=TYPED.has(kind);
    const parts=f.parent?fields.filter(x=>x.parent===f.parent):[];
    const field=select('Filter on',fieldChoices(),parts[0]?.key||c.field);field.wrap.classList.add('cond-field');
    field.wrap.prepend(el('span',{class:'cond-field-icon','aria-hidden':'true'},icon(fieldIcon(f))));
    const part=parts.length?select('Part',parts.map(x=>({value:x.key,label:x.label})),c.field):null;
    const op=select('Operator',opChoices(c),c.op||'any');
    const more=el('div',{class:'cond-more'});
    // A new field (or part) starts empty and opens its value, so choosing the field and its value is one motion.
    const refield=(key:string)=>{const next=blankOf(byKey.get(key)!);Object.keys(c).forEach(k=>delete (c as Record<string,unknown>)[k]);Object.assign(c,next);
      const row=line(c,remove);node.replaceWith(row);
      void hinted.then(()=>requestAnimationFrame(()=>nodes.get(c)?.querySelector<HTMLButtonElement>('.pick-button,.cond-value')?.click()));};
    field.input.addEventListener('change',()=>refield(field.input.value));
    part?.input.addEventListener('change',()=>refield(part.input.value));
    const relabel=()=>{for(const x of opChoices(c)){const opt=[...op.input.options].find(y=>y.value===x.value);if(opt)opt.textContent=x.label;}};
    let value:HTMLElement,values:ReturnType<typeof multiPick>|null=null;
    const paintTail=()=>{
      const parts:HTMLElement[]=[],hint:string[]=[];
      if(!typed&&!NO_VALUE.has(c.op||'any')&&c.value==null){
        const all=o.allOf&&allowsAll(c.field)&&c.op!=='none'&&c.values.length>1,noun=nounOf(c);
        // A date set matches a day (in every set chosen), every other field a person (holding every value chosen).
        const day=c.field==='date_set_ids';
        if(all){parts.push(segmentedChoice('Match','cond-match',[['any','Any of',day?'Matches a day in at least one of them':'Matches someone with at least one of them'],['all','All of',day?'Matches only a day in every one of them':'Matches only someone with every one of them']],c.op==='all'?'all':'any',v=>{c.op=v;op.input.value=v;relabel();paintTail();changed();}));
          hint.push(c.op==='all'?(day?'a day in every one':'must have every one'):'any one of them');}
        if(counts(c)){parts.push(countSwitch(c.count==='each'?'each':'together',v=>{setCount(c,v);paintTail();changed();},noun));hint.push(countHint(c.count==='each',noun));}
        // Named entries of the chosen values (a date set's holidays): none chosen means all of them.
        const named=withUnavailableEntries(f.entries?.(c.values)||[],c.entry_names||[]);
        if(c.values.length&&named.length){
          const pick=multiPick('Entries',named.map(x=>({...x,tone:x.tone||'all'})),c.entry_names||[],v=>{if(v.length)c.entry_names=v;else delete c.entry_names;},'All entries');
          parts.push(el('span',{class:'cond-choice cond-entries'},el('span',{class:'cond-choice-label','aria-hidden':'true'},'Only'),pick.wrap));}
      }
      more.replaceChildren(...parts,...(hint.length?[el('span',{class:'cond-hint'},hint.join(' · '))]:[]));more.hidden=!parts.length;};
    const drawValue=()=>{
      values=null;
      if(NO_VALUE.has(c.op||'any'))value=el('span',{class:'cond-novalue muted'},'no value needed');
      else if(typed){
        const setAt=(at:0|1)=>(v:Operand|null)=>{
          if(c.op==='between'){const pair=(Array.isArray(c.value)?[...c.value]:[c.value??null,null]) as (Operand|null)[];pair[at]=v;c.value=pair as Condition['value'];}
          else if(v==null)delete c.value;else c.value=v;
          // The chip is drawn anew: keyboard focus returns to it, not to the page.
          drawValue();changed();(value.matches('.cond-value')?value:value.querySelectorAll<HTMLElement>('.cond-value')[at])?.focus();};
        value=c.op==='between'?el('span',{class:'cond-values cond-between'},valueChip(c,f,0,setAt(0)),el('span',{class:'muted'},'and'),valueChip(c,f,1,setAt(1))):valueChip(c,f,0,setAt(0));
      }else{
        // Relative values (Me, My programme, Clinics I can work at) lead the list; one stands alone in a line.
        // Each says what it means now, from the server ("Clinics I can work at · 3 + sub-clinics"), in the list and on its chip.
        const rel=relativeOf(f).map(t=>{const hint=scope?resolvedNow(f.key,{kind:'relative',token:t.token},scope)?.display||'':'';
          return {value:'@'+t.token,label:hint?`${t.label} · ${hint}`:t.label,keywords:t.label,group:'Relative',glyph:'clock' as IconName,tone:'relative',exclusive:true};});
        const exact=f.choices.map(x=>({...x,tone:x.tone||valueTone(c.field,x.value),...(rel.length&&!x.group?{group:f.label}:{})}));
        const picked=isRelative(c.value)?['@'+c.value.token]:c.values;
        // Inactive clinics, places and duty types stay out of the list unless chosen; one quiet line offers them.
        const hidden=f.inactive?.count?exact.filter(x=>f.inactive!.ids.has(x.value)&&!picked.includes(x.value)):[];
        const offered=[...rel,...exact.filter(x=>!hidden.includes(x))];
        const foot=()=>hidden.length&&!offered.includes(hidden[0])?el('p',{class:'picker-foot hint'},`${f.inactive!.count} inactive ${f.inactive!.noun[f.inactive!.count===1?0:1]} not shown · `,
          button('Show',()=>{offered.push(...hidden);values!.open();},'',{class:'link-button'})):null;
        values=multiPick(f.label,offered,picked,undefined,'Choose…',!!f.creatable,foot);
        values.wrap.classList.add('cond-values');
        values.wrap.addEventListener('change',()=>{const v=values!.values(),r=v.find(x=>x.startsWith('@'));
          if(r){c.value={kind:'relative',token:r.slice(1)} as Condition['value'];c.values=[];delete c.count;delete c.entry_names;if(c.op==='all')c.op='any';}
          else{delete c.value;c.values=v;}
          relabel();paintTail();review();});
        // No values to choose (no tags yet): say so, with the way to make some, instead of an empty picker.
        const none=f.empty;
        if(none&&!f.choices.length&&!c.values.length&&!rel.length)values.wrap.replaceChildren(el('span',{class:'cond-empty muted'},none.text,...(none.action?[' · ',button(none.action.label,none.action.run,'',{class:'link-button'})]:[])));
        value=values.wrap;
      }
      slot.replaceChildren(value);paintTail();};
    repaint.add({node:()=>nodes.get(c),draw:()=>drawValue()});
    const slot=el('span',{class:'cond-slot'});
    op.input.addEventListener('change',()=>{const was=c.op||'any',now=op.input.value as NonNullable<Condition['op']>;c.op=now;
      if(NO_VALUE.has(now)){c.values=[];delete c.value;delete c.count;delete c.entry_names;}
      else if(typed){const cur=Array.isArray(c.value)?c.value[0]:c.value;
        if(c.op==='between')c.value=[cur??null,null] as unknown as Condition['value'];
        else if(c.op==='within'&&!isRelative(cur)||cur==null)delete c.value;else c.value=cur;}
      if(c.op==='none')delete c.count;
      if(NO_VALUE.has(now)||NO_VALUE.has(was)||typed||!values)drawValue();else paintTail();});
    drawValue();
    // Narrow: [field] [part] [operator] stay together, then the value wraps below as one, never apart.
    const node=el('div',{class:'cond-line'+(typed?' is-typed':'')},field.wrap,...(part?[part.wrap]:[]),op.wrap,slot,more);
    nodes.set(c,node);
    return node;
  }
  /** One level's rows: the first reads "Where", the others choose and / or for the whole level; each row ends in ⋯. */
  function level(g:Conditions,depth:number,parent?:{remove:()=>void}):HTMLElement{
    const list=el('div',{class:'cond-list'});lists.set(g,list);
    g.conditions.forEach((n,i)=>{
      const drop=()=>{g.conditions.splice(g.conditions.indexOf(n),1);if(!g.conditions.length&&parent)parent.remove();else draw();changed();};
      let join:HTMLElement;
      if(i===0)join=el('span',{class:'cond-where'},o.lead??'Where');
      else{const s=select('Joiner',[{value:'all',label:'And'},{value:'any',label:'Or'}],g.match||'all');join=s.wrap;
        s.input.addEventListener('change',()=>{joinAt(g,i,s.input.value as 'all'|'any',depth);draw();focusRow(g,i,'.cond-join select');});}
      const cell=el('div',{class:'cond-join','data-join':`${depth}-${i}`},join);
      const at=()=>g.conditions.indexOf(n);
      const items:MenuItem[]=[{label:'Duplicate',icon:'copy',run:()=>{g.conditions.splice(at()+1,0,copy(n));draw();changed();focusRow(g,at()+1,'.cond-row-menu');}},
        ...(depth===0&&!isGroup(n)?[{label:'Turn into group',icon:'layers' as const,run:()=>{g.conditions[at()]={match:'all',conditions:[n]};draw();changed();focusRow(g,at(),'.cond-row-menu');}}]:[]),
        {label:isGroup(n)?'Remove group':'Remove',icon:'trash',run:drop}];
      const menu=opens(el('button',{type:'button',class:'icon-button bare cond-row-menu','aria-label':isGroup(n)?'Group actions':'Filter actions','data-tip':'More'},icon('dots')),'menu');
      menu.addEventListener('click',()=>openMenu(menu,isGroup(n)?'Group actions':'Filter actions',items));
      list.append(el('div',{class:'cond-row'+(isGroup(n)?' has-group':'')},cell,isGroup(n)?group(n,drop):line(n,drop),el('span',{class:'cond-menu-cell'},menu)));
    });
    const add=(n:Node)=>{g.conditions.push(n);draw();changed();focusRow(g,g.conditions.length-1,'.cond-line select');};
    const adder=opens(el('button',{type:'button',class:'link-button cond-add'},'+ Add filter rule',icon('chevronDown')),'menu');
    adder.addEventListener('click',()=>openMenu(adder,'Add',[{label:'Add filter rule',icon:'plus',run:()=>add(blank())},...(depth===0?[{label:'Add filter group',icon:'layers' as const,run:()=>add({match:'all',conditions:[blank()]})}]:[])]));
    const wipe=depth===0&&g.conditions.length?armed(button('Delete filter',()=>{model.conditions=o.startEmpty?[]:[blank()];model.match='all';draw();changed();},'',{class:'link-button cond-delete'}),'Delete every rule?'):null;
    return el('div',{class:'cond-level'},list,el('div',{class:'cond-actions'},adder,...(wipe?[el('span',{class:'grow'}),wipe]:[])));
  }
  function group(g:Conditions,remove:()=>void):HTMLElement{
    const hint=el('span',{class:'cond-hint'},countHint(g.count==='each','filters'));
    const head=el('div',{class:'cond-group-head'},el('span',{class:'muted'},'Group'),el('span',{class:'grow'}),
      ...(counts(g)?[countSwitch(g.count==='each'?'each':'together',v=>{setCount(g,v);hint.textContent=countHint(v==='each','filters');changed();},'filters'),hint]:[]));
    void remove;
    return el('div',{class:'cond-group'},head,level(g,1,{remove}));
  }
  /** Row i of a level asks for the other joiner. The second row's joiner is the level's (as in Notion): the level
   *  switches, as it does inside a group. A later row's, at the top level: the rows before it and from it on each become
   *  a group keeping the old joiner, joined by the new one ("(a or b or c) and not d"). */
  function joinAt(g:Conditions,i:number,m:'all'|'any',depth:number){
    const old=g.match||'all';if(m===old)return;
    const before=g.conditions.slice(0,i),after=g.conditions.slice(i);
    const wraps=(x:Node[])=>x.length===1||x.every(n=>!isGroup(n));
    if(depth===0&&i>1&&wraps(before)&&wraps(after)){
      const wrap=(x:Node[]):Node=>x.length===1?x[0]:{match:old,conditions:x};
      g.conditions=[wrap(before),wrap(after)];
    }
    g.match=m;
  }
  const labelOfValue=(field:string,v:string)=>{const f=byKey.get(field),c=f?.choices.find(x=>x.value===v);return c?.label||(f?.creatable?v:'Selection unavailable');};
  /** Read-only roles read the filters as facts reviewed: "Where Rank is any of
   *  [R1] [R2] · each rank counted separately", never disabled pickers that look editable. */
  function facts(g:Conditions):HTMLElement{
    return el('ul',{class:'cond-facts'},...g.conditions.map((n,i)=>{
      const join=el('span',{class:'cond-fact-join'},i===0?o.lead??'Where':g.match==='any'?'Or':'And');
      if(isGroup(n))return el('li',{class:'cond-fact is-group'},join,' ',el('div',{class:'cond-fact-group'},facts(n),
        counts(n)?el('span',{class:'cond-hint'},countHint(n.count==='each','filters')):''));
      const f=byKey.get(n.field),op=(NO_VALUE.has(n.op||'any')||n.value!=null?opWord(n.op||'any'):n.op==='none'?'is not':n.op==='all'?'has all of':n.values.length>1?'is any of':'is').toLowerCase();
      const chips=n.value!=null?[n.value].flat().map(x=>el('span',{class:`rp ${isRelative(x)?'relative':valueTone(n.field,x)}`},operandText(x,v=>labelOfValue(n.field,v))))
        :n.values.map(v=>{const c=f?.choices.find(x=>x.value===v);return el('span',{class:`rp ${c?.tone||valueTone(n.field,v)}`,...(c?.label?{'data-lg-self':''}:{})},c?.label||(f?.creatable?v:'Selection unavailable'));});
      // Spaces between the parts: they read as one sentence to assistive technology and in copied text.
      const parts=[el('b',{},f?(f.parent?`${f.parent} · ${f.label}`:f.label):n.field),el('span',{class:'muted'},op),...chips,...(n.entry_names?.length?[el('span',{class:'muted'},'· only'),...n.entry_names.map(x=>el('span',{class:'rp all'},x))]:[]),
        ...(counts(n)?[el('span',{class:'cond-hint'},'· '+countHint(n.count==='each',nounOf(n)))]:[])];
      return el('li',{class:'cond-fact'},join,' ',el('span',{class:'cond-fact-line'},...parts.flatMap((x,i)=>i?[' ',x]:[x])));}));
  }
  function draw(){
    if(o.readOnly){root.classList.add('cond-readonly');root.replaceChildren(facts(model));review();return;}
    root.replaceChildren(level(model,0));review();}
  // After a redraw, focus stays on the row it was on (its joiner, or a new row's field).
  const lists=new WeakMap<Conditions,HTMLElement>();
  const focusRow=(g:Conditions,i:number,what:string)=>{const rows=lists.get(g)?.children;rows?.[Math.min(i,rows.length-1)]?.querySelector<HTMLElement>(what)?.focus();};

  /** Lines without a value are left out (Is empty needs none), a group left with one line is that line, nothing left is null. */
  function clean(t:Conditions):Conditions|null{
    const leaf=(n:Condition):Condition|null=>{const op=n.op||'any';
      if(NO_VALUE.has(op))return {field:n.field,op,values:[]};
      if(n.value!=null)return [n.value].flat().some(x=>x==null)?null:{field:n.field,op,values:[],value:copy(n.value)};
      if(TYPED.has(kindOf(byKey.get(n.field),n.field))||!n.values.length)return null;
      return {field:n.field,op,values:[...n.values],...(n.entry_names?.length?{entry_names:[...n.entry_names]}:{}),...(n.count==='each'&&op!=='none'?{count:'each' as const}:{})};};
    const conditions=t.conditions.map(n=>isGroup(n)?clean(n):leaf(n)).filter(Boolean) as Node[];
    if(!conditions.length)return null;
    return {match:t.match||'all',conditions,...(t.count==='each'&&conditions.length>1?{count:'each' as const}:{})};
  }
  const read=()=>clean(model);
  // Structural edits (a filter or group added or removed, a count) are announced like a value change. An edit made in a
  // select or a value list inside the builder is announced by its own bubbling change: announcing it again here applied
  // it twice (two Schedule redraws, the second reading everything: maintenance, 444ac3e).
  const changed=()=>root.dispatchEvent(new Event('change',{bubbles:true}));
  const review=()=>{root.dataset.reviewValue=conditionsText(read(),labelOfValue)||'No conditions';};
  root.dataset.reviewLabel='Conditions';root.addEventListener('change',review);
  draw();
  // Once the viewer's values and their hints are in, choice values gain them (Me, My programme); only the values redraw (never the rows, whose controls may be in use), unless their list is open.
  void hinted.then(()=>{loaded=true;refresh();});
  /** The fields' values changed (a lazy read landed): the rows redraw with them in place, so a row still being filled in
   *  (which read() leaves out) and its value list about to open are kept. */
  const setFields=(next:ConditionField[])=>{fields=next;byKey.clear();for(const f of next)byKey.set(f.key,f);
    // D-057: a menu open on a builder button (+ Add filter rule, a row's ⋯) closes once a redraw replaces that button;
    // the rows keep their controls and the next structural edit draws them with the new fields.
    if(!root.querySelector('[aria-expanded=true]'))draw();};
  return {root,read,setFields};
}

/** Rule scope fields: who (evaluated per person at the time of the duty) and which sessions, as the contract defines
 *  them (schemas.SCOPE_FIELDS: the catalogue-backed choices, tags and the session's own fields, the duty's date
 *  included; roles are for announcement audiences only). One list for the rule form and the rule summaries; `kind` is
 *  the catalogue the values come from ('' for free tags and the duty date). */
export const RULE_FIELDS:{key:Condition['field'];label:string;kind:string;value_kind:ValueKind}[]=CONDITION_FIELD_DEFINITIONS
  .filter(f=>f.key!=='roles'&&(!!f.kind||f.value_kind==='tags'||f.session)).map(f=>({key:f.key,label:f.label,kind:f.kind,value_kind:f.value_kind}));

/** The contract's date, text and number fields among these keys, for the shared builder (their values are typed or
 *  relative, never listed). */
export const typedFields=(keys:string[]):ConditionField[]=>CONDITION_FIELD_DEFINITIONS.filter(d=>keys.includes(d.key)&&TYPED.has(d.value_kind)).map(d=>({key:d.key,label:d.label,choices:[]}));

/** The fields that pick sessions rather than people (not allowed where only people are chosen: who may lead, balance goals). */
export const SESSION_FIELDS:string[]=CONDITION_FIELD_DEFINITIONS.filter(f=>f.session).map(f=>f.key);

/** A tree without the lines on these fields (a group left empty goes too); null when nothing is left. */
export function withoutFields(tree:Conditions|null|undefined,keys:string[]):Conditions|null{
  const conditions=(tree?.conditions||[]).map(c=>'conditions' in c?withoutFields(c,keys):keys.includes(c.field)?null:c).filter(Boolean) as (Condition|Conditions)[];
  return conditions.length?{match:tree!.match||'all',conditions}:null;
}

/** The entries a date-set line narrows to, in words: " (Eid al-Fitr, National Day)"; every entry: nothing. */
export const entriesWords=(c:Condition)=>c.entry_names?.length?` (${c.entry_names.join(', ')})`:'';
/** The word before a where: "on Public holidays" for a date set (a day, not a place), "at" for every other where. */
export const isDateCondition=(c:Condition|Conditions)=>!('conditions' in c)&&c.field==='date_set_ids'&&c.op!=='none';
export const placeWord=(tree:Conditions|null|undefined)=>{const c=tree?.conditions?.[0];return !c?'at':isDateCondition(c)?'on':!('conditions' in c)&&c.field==='date'?'when':'at';};

/** A condition tree in plain words: "Rank is any of R1, R2 · Tag has all of #a + #b · except rank Consultants ·
 *  Duty date is within The duty's week · Note is empty". */
export function conditionsText(tree:Conditions|null|undefined,label:(field:string,value:string)=>string,fieldLabel:(f:string)=>string=f=>RULE_FIELDS.find(x=>x.key===f)?.label||DEFS.get(f)?.label||f):string{
  if(!tree?.conditions?.length)return '';
  return tree.conditions.map(c=>'conditions' in c?'('+conditionsText(c,label,fieldLabel)+')'+(c.count==='each'?' each':'')
    :NO_VALUE.has(c.op||'any')?`${fieldLabel(c.field)} ${opWord(c.op!).toLowerCase()}`
    :c.value!=null?`${fieldLabel(c.field)} ${opWord(c.op||'any').toLowerCase()} ${[c.value].flat().map(x=>operandText(x,v=>label(c.field,v))).join(' and ')}`
    :(c.op==='none'?'except '+fieldLabel(c.field).toLowerCase()+' ':fieldLabel(c.field)+(c.op==='all'?' has all of ':c.values.length>1?' is any of ':' is '))
      +c.values.map(v=>label(c.field,v)).join(c.op==='all'?' + ':', ')+entriesWords(c)+(c.count==='each'&&c.op!=='none'?' each':'')).join(tree.match==='any'?' or ':' · ');
}

/** A scope in one short line, the one summary every surface shows (Recent runs, a run's setup line): one value is named
 *  ("Resident · R1"), several are counted with the field's plural from the contract ("9 ranks · 80 people"); a typed
 *  value or an empty/not-empty test reads as conditionsText does. The whole scope is conditionsText, on demand. */
export function conditionsSummary(tree:Conditions|null|undefined,label:(field:string,value:string)=>string):string{
  if(!tree?.conditions?.length)return '';
  return tree.conditions.map(c=>{
    if(isGroup(c))return conditionsSummary(c,label);
    if(NO_VALUE.has(c.op||'any')||c.value!=null||!c.values?.length)return conditionsText({match:'all',conditions:[c]},label);
    const counted=c.values.length===1?label(c.field,c.values[0]):`${c.values.length} ${(DEFS.get(c.field) as {plural?:string}|undefined)?.plural||c.field}`;
    return c.op==='none'?`except ${counted}`:counted;}).join(tree.match==='any'?' or ':' · ');
}
/** What a saved view's relative values mean today, for its menu: "1–31 Oct" for its first relative date, "per viewer"
 *  for a viewer's value (Me, My clinics); '' when it holds none (it is fixed). */
export function relativeSummary(tree:Conditions|null|undefined,scope:ResolveScope):string{
  const rel=relativeLines(tree),day=rel.find(r=>dateBounds(r.operand,r.field,scope));
  return day?rangeWords(dateBounds(day.operand,day.field,scope)!):rel.length?(rel.some(r=>(TOKENS.get(r.operand.token)?.field_kinds as readonly string[]|undefined)?.includes('date'))?'moves with the day':'per viewer'):'';
}
/** Every relative value of a tree with its field (for one resolve-operands request). */
export function relativeLines(tree:Conditions|null|undefined):{field:string;operand:RelativeOperand}[]{
  const out:{field:string;operand:RelativeOperand}[]=[];
  const walk=(t:Conditions)=>t.conditions.forEach(n=>isGroup(n)?walk(n):[n.value].flat().filter(isRelative).forEach(x=>out.push({field:n.field,operand:x})));
  if(tree)walk(tree);return out;
}

/** A rule scope's older flat fields as condition lines (all joined by and), followed by its own conditions. */
export function scopeAsConditions(scope:Record<string,any>):Conditions|null{
  const one=(many:string,single:string)=>scope[many]?.length?scope[many]:scope[single]?[scope[single]]:[];
  const lines:(Condition|Conditions)[]=([['person_ids',one('person_ids','person_id')],['category_ids',one('category_ids','category_id')],
    ['tags',scope.tags||[]],['capability_ids',scope.capability_ids||[]],['programme_ids',one('','programme_id')],['rotation_type_ids',one('','rotation_type_id')],
    ['service_ids',one('service_ids','service_id')],['location_ids',one('','location_id')],['assignment_type_ids',one('','assignment_type_id')]] as [Condition['field'],string[]][])
    .filter(([,v])=>v.length).map(([field,values])=>({field,op:'any',values}));
  if(scope.conditions?.conditions?.length){if(!lines.length)return scope.conditions;lines.push(...(scope.conditions.match==='all'?scope.conditions.conditions:[scope.conditions]));}
  return lines.length?{match:'all',conditions:lines}:null;
}
