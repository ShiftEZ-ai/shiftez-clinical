import {plural,formatDate} from './dom.js';
import {shiftDay} from './date_picker.js';
import {treeText,numbersText,daysText,validText,type Namer} from './rule_catalogue.js';
import {staffNeeded,SETTING_LABELS} from './forms.js';
import {RULE_CATALOGUE,RULE_CATALOGUE_V2,type RuleLine} from './generated.js';
import type {Row} from './types.js';

/* A record's structured or coded value in words, as the app's own screens say it — never JSON or ids: a who / where as
 * the rule catalogue reads it, a rule line as its line words, weekdays by name, a rule type by its catalogue name, a
 * duty type's hours and staff needed, an audience by whom it reaches. One formatter for the Bulk editor's Sheet, the
 * child lists of Show their… and the Change form (its Now values and Preview). */

const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-/i;
const isObject=(v:unknown):v is Record<string,unknown>=>!!v&&typeof v==='object'&&!Array.isArray(v);
const blank=(v:unknown)=>v==null||v===''||v===false||Array.isArray(v)&&!v.length||isObject(v)&&!Object.keys(v).length;
const words=(k:string)=>SETTING_LABELS[k]||k.replace(/_ids?$/,'').replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());

/** Whether a value needs this formatter (a structure, or a code its screens show by name). */
export const isStructured=(key:string,v:unknown)=>key==='catalogue_id'||key==='weekdays'||key==='who'||key==='where'||isObject(v)||Array.isArray(v)&&v.some(x=>x&&typeof x==='object');

/** `name`: a record's name by its id (undefined when unknown: it then reads "Record", never the id). */
export function recordText(key:string,v:unknown,name:(id:string)=>string|undefined):string{
  // No who is everyone, no where every clinic (as a rule line says).
  if(v==null&&(key==='who'||key==='where'))return key==='where'?'All clinics':'Everyone';
  if(v==null||v===''||Array.isArray(v)&&!v.length)return '—';
  // A day as every list shows it; an end stored as the day after (ends_on) as the last day included.
  const id=(x:string)=>/^\d{4}-\d{2}-\d{2}$/.test(x)?formatDate(key==='ends_on'?shiftDay(x,-1):x,'UTC'):name(x)||(UUID.test(x)?'Record':/^[a-z]+(_[a-z]+)+$/.test(x)?x.replaceAll('_',' '):x);
  const names={label:(field:string,x:string)=>{const n=name(x);return n?(field==='tags'?'#'+n:n):UUID.test(x)?'Record':x;},tag:()=>undefined} as unknown as Namer;
  if(key==='catalogue_id'&&typeof v==='string')return (RULE_CATALOGUE_V2.find(e=>e.id===v)||RULE_CATALOGUE.find(e=>e.id===v))?.name||words(v);
  if(key==='weekdays'&&Array.isArray(v))return daysText({weekdays:v} as RuleLine);
  if(typeof v!=='object')return typeof v==='boolean'?(v?'Yes':'No'):typeof v==='string'?id(v):String(v);
  const line=(l:Record<string,any>)=>[treeText(l.who,names,'Everyone'),treeText(l.where,names,'All clinics'),daysText(l as RuleLine),
    numbersText(l as RuleLine)||'no number',validText(l as RuleLine)].join(' · ');
  if(Array.isArray(v)){
    if(!v.every(isObject))return v.map(x=>recordText(key,x,name)).join(', ');
    const items=v as Record<string,any>[];
    if(key==='lines')return plural(items.length,'line')+': '+items.slice(0,3).map(line).join('; ')+(items.length>3?'; …':'');
    if(items.every(x=>x.person_id))return items.map(x=>id(x.person_id)).join(', ');
    if(items.every(x=>typeof x.name==='string'))return items.map(x=>x.name).join(', ');
    if(items.every(x=>x.length&&x.unit))return items.map(x=>x.length+(x.unit==='weeks'?' wk':' mo')).join(', ');
    return plural(items.length,'item');
  }
  const o=v as Record<string,any>;
  if(Array.isArray(o.conditions))return treeText(o as never,names,key==='where'?'All clinics':'Everyone');
  if(key==='numbers')return numbersText({numbers:o} as RuleLine)||'—';
  if(o.numbers&&('who' in o||'where' in o))return line(o);
  // An audience: everyone, or whom its condition and lists reach.
  if('everyone' in o)return o.everyone?'Everyone':[o.where?.conditions?.length?treeText(o.where,names,'Everyone'):'',
    ...(o.roles||[]).map(words),...Object.entries(o).filter(([k,x])=>/_ids$/.test(k)&&Array.isArray(x)).flatMap(([,x])=>(x as string[]).map(id)),
    ...(o.tags||[]).map((t:string)=>'#'+t)].filter(Boolean).join(', ')||'No one';
  // A duty type's details: as its Configuration list reads them (session hours, staff needed).
  if('requirements' in o||'purpose' in o)return [o.default_timing?`${o.default_timing.start_time}–${o.default_timing.end_time}`:'',staffNeeded({definition:o} as unknown as Row)].filter(Boolean).join(' · ');
  return Object.entries(o).filter(([k,x])=>!blank(x)&&k!=='schema_version').map(([k,x])=>`${words(k)}: ${recordText(k,x,name)}`).join(' · ')||'—';
}
