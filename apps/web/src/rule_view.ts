/* How the Rules page reads one rule: its numbers (minimum, maximum, target) as a pill, a range bar and cards; who and
 * when in one sentence; its exceptions (lines for a person or a programme); and how it fares in the current draft.
 * The numbers each kind has come from the server (RULE_NUMBERS, generated); who matches is never decided here. */
import {formatDay,el,icon,plural,pluralWord,formatDate,badge,field,select,button,iconButton,armed,announce,compact,WEEKDAYS,weekOrder,avatar} from './dom.js';
import {dateRange,lastDayIncluded} from './date_picker.js';
import {addDays} from './planner_logic.js';
import {RULE_NUMBERS,STRENGTH_LABELS} from './generated.js';
import {quotaUnits} from './quotas.js';
import {SESSION_FIELDS,RULE_FIELDS,scopeAsConditions,conditionsText} from './conditions.js';
import type {Row} from './types.js';
import type {PersonAmount} from './generated.js';
import type {MapRule} from './planner_rules.js';
import {goalSentence} from './balance_goal.js';
import {isOrder,catalogueEntryOf} from './forms.js';

type Name=(kind:string,id:string)=>string;
export type Limits={min:number|null;max:number|null;target:number|null;unit:string;strength?:string|null};
const num=(v:unknown)=>v==null||v===''||Number.isNaN(Number(v))?null:Number(v);

/** A rule's minimum, maximum and target (null: not set) and their unit; null for rules without a number. */
export function ruleLimits(rule:Row):Limits|null{
  if(rule.kind==='block_quota'){
    const c=rule.comparison;
    return {min:['minimum','range'].includes(c)?num(rule.amount):null,max:c==='maximum'?num(rule.amount):c==='range'?num(rule.maximum_amount):null,
      target:c==='target'?num(rule.amount):num(rule.target_amount),unit:String(quotaUnits[rule.metric]||rule.metric||'').toLowerCase(),
      strength:c!=='target'&&rule.target_amount!=null?rule.target_strength||'medium':null};
  }
  const n=RULE_NUMBERS[rule.kind];
  if(!n)return null;
  const [field,unit,stricter]=n,v=num(rule[field]);
  return {min:stricter==='higher'?v:null,max:stricter==='lower'?v:null,target:null,unit:unit||''};
}
// One of a unit: duties → duty, weekends → weekend, session units → session unit.
const shortUnit=(unit:string,n=2)=>/hours$/.test(unit)?'h':unit==='minutes'?'min':unit==='people'?'':n===1?unit.replace(/ies$/,'y').replace(/([^y])s$/,'$1'):unit;
type Span=[number,number];
/** Each number's lowest and highest across a rule and its lines (null: none sets it). */
function spans(ls:Limits[]):{min:Span|null;max:Span|null;target:Span|null}{
  const span=(k:'min'|'max'|'target'):Span|null=>{const v=ls.map(l=>l[k]).filter((x):x is number=>x!=null);return v.length?[Math.min(...v),Math.max(...v)]:null;};
  return {min:span('min'),max:span('max'),target:span('target')};
}
const spanText=(s:Span)=>s[0]===s[1]?String(s[0]):`${s[0]}–${s[1]}`;
/** A rule's limits and those of its lines that set their own numbers (a rule that applies only through its lines,
 *  each with its own number: just theirs). */
export function allLimits(definition:Row):Limits[]{
  const own=ruleLimits(definition.rule);
  if(!own)return [];
  const lines:Row[]=definition.per_person||[],theirs=lines.filter(l=>l.amount!=null||l.settings?.maximum_amount!=null||l.settings?.target_amount!=null);
  const out=[...(!definition.listed_only||lines.some(l=>l.amount==null)?[own]:[]),...theirs.map(l=>lineLimits(definition.rule,l)!)];
  return out.length?out:[own];
}
/** One piece of a rule's numbers: its text ("3", "2↰", "1–2"), its strength (`level`: "mandatory", an advisory level,
 *  null taken from the broader line; absent on a separator) and, for a number taken from a broader line, that line. */
export type NumberPart={text:string;from?:string;level?:string|null};
/** A strength in words: "Mandatory", "Advisory · high", "Inherited strength". */
export const levelWords=(level:string|null|undefined)=>level==='mandatory'?'Mandatory':level==='advisory'?'Advisory':level==='off'?'Off':level?`Advisory · ${(STRENGTH_LABELS[level as keyof typeof STRENGTH_LABELS]||level).toLowerCase()}`:'Inherited strength';
/** Rule numbers, one style everywhere: min – max → target as "1 – 2", "≥ 1", "≤ 5",
 *  "2 – 5 → 4" ("↰" inherited), in parts so an inherited number can be drawn muted; the strength is a labelled badge. */
export function rangeParts(min:NumberPart|null,max:NumberPart|null,target:NumberPart|null):NumberPart[]{
  const out:NumberPart[]=[],at=(x:NumberPart,before:string)=>({...x,text:before+x.text});
  if(min&&max)out.push(min,{text:' – '},max);else if(min)out.push(at(min,'≥ '));else if(max)out.push(at(max,'≤ '));
  if(target)out.push(...(out.length?[{text:' '}]:[]),at(target,'→ '));
  return out;
}
/** The strengths of numbered parts: one when they agree (shown once, after them), else null (shown after each). */
const oneLevel=(parts:NumberPart[])=>{const ls=[...new Set(parts.filter(x=>x.level!=null).map(x=>x.level))];return ls.length===1?{level:ls[0]}:ls.length?null:undefined;};
/** "≥ 11 · Mandatory", "≤ 6 duties per week · Mandatory", "2 – 5 mandatory → 4 advisory · medium": numbers (then
 *  what they count, `unit`) with their strength in words (titles, search, sentences). */
export function partsText(parts:NumberPart[],unit=''):string{
  const one=oneLevel(parts),u=unit?' '+unit:'';
  if(one===undefined)return parts.map(x=>x.text).join('')+u;
  if(one)return parts.map(x=>x.text).join('')+u+' · '+levelWords(one.level);
  return parts.map((x,i)=>x.level==null||!runEnds(parts,i)?x.text:`${x.text} ${levelWords(x.level).toLowerCase()}`).join('')+u;
}
/** Numbers that differ in strength: a strength is said once after each run of numbers that share it ("2 – 5
 *  mandatory → 4 advisory · medium"). */
const runEnds=(parts:NumberPart[],i:number)=>{const next=parts.slice(i+1).find(x=>x.level!=null);return !next||next.level!==parts[i].level;};
/** The strength as a small labelled badge (never a "!" to decode): solid accent for Mandatory, outline for Advisory. */
/** What an advisory strength weighs in the schedule score (Configuration › Auto-schedule & scoring), once read. */
let weights:Record<string,number>={};
export const setAdvisoryWeights=(w:Record<string,number>|undefined)=>{weights={...w};
  // Badges drawn before the weights arrived say them too.
  for(const b of document.querySelectorAll<HTMLElement>('.rc-level[data-level=advisory][data-strength]'))b.dataset.tip=levelTip(b.dataset.strength);};
/** A strength badge's tooltip: what the word does ("Counts 3× a low advisory rule in the schedule score"). */
export const levelTip=(level:string|null|undefined)=>level==='mandatory'?'Must hold: the auto-schedule never breaks it'
  :!level||level==='off'||level==='advisory'?'':weights[level]&&weights.low?`Counts ${Math.round(weights[level]/weights.low)}× a low advisory rule in the schedule score`
  :'Its weight in the schedule score is set in Configuration › Auto-schedule & scoring';
export const levelBadge=(level:string|null|undefined)=>{const tip=levelTip(level);
  return badge(levelWords(level),'','rc-level',{tip,'data-level':level==='mandatory'?'mandatory':level?'advisory':'inherit','data-strength':level||undefined});};
/** Rule numbers drawn: plain text in the UI font, tabular numerals, weight 500 (`.rule-num`; never a pill, never mono);
 *  an inherited number muted, its tooltip naming the line it comes from (`from`); the strength badge after them. */
export function numbersEl(parts:NumberPart[],from:(id:string)=>string=()=>'a broader line'):HTMLElement{
  if(!parts.length)return el('span',{class:'rule-num muted'},'—');
  const one=oneLevel(parts),num=(x:NumberPart)=>x.from?inheritedEl(x.text,from(x.from)):x.text;
  return el('span',{class:'rule-num'},...parts.flatMap((x,i)=>one===null&&x.level!=null&&runEnds(parts,i)?[num(x),' ',levelBadge(x.level)]:[num(x)]),...(one?[' ',levelBadge(one.level)]:[]));
}
const inheritedEl=(text:string,source:string)=>el('span',{class:'rc-inh','data-tip':`Inherited from ${source}`,'aria-label':`${text.replace('↰','')}, inherited from ${source}`},text);
/** A checked copy's numbers (Which rule applies?): "≤ 5" and its strength. */
export function copyNumbers(rule:Row,severity:string):HTMLElement{
  const l=ruleLimits(rule),level=severity==='mandatory'?'mandatory':'advisory',p=(v:number|null|undefined)=>v==null?null:{text:String(v),level};
  return numbersEl(l?rangeParts(p(l.min),p(l.max),p(l.target)):[]);
}
/** The list's numbers across a rule and its lines, the unit last: "≥ 2", "≤ 6 days", "≥ 11 h", "≤ 1–2" (lines differ),
 *  "2 – 6 → 4 duties" (a range and a target), "≥ 2 → 3" (a minimum and a target), "→ 3". */
export function limitPill(ls:Limits[]):string{
  if(!ls.length)return '';
  const {min,max,target}=spans(ls),p=(s:Span|null)=>s&&{text:spanText(s)};
  // The unit agrees with the largest number shown: "≤ 1 weekend", "≤ 1–2 weekends".
  const top=Math.max(...[min,max,target].filter((x):x is Span=>!!x).map(x=>x[1])),u=shortUnit(ls[0].unit,top);
  const text=rangeParts(p(min),p(max),p(target)).map(x=>x.text).join('');
  return text&&u?`${text} ${u}`:text;
}
/** A count over a window: "per day", "per week", "in any 7 days" (a one-day window: "per day"). */
const windowWords=(rule:Row)=>rule.window==='rolling'?(Number(rule.window_days)===1?'per day':`in any ${rule.window_days} days`):rule.window==='day'?'per day':'per week';
/** A quota's period: "each week", "each month", "every 14 days", "in any 7 days", "each day", "over the whole period". */
export function quotaPeriod(rule:Row):string{
  const days=Number(rule.window_days);
  if(rule.window_alignment==='calendar_week')return 'each week';
  if(rule.window_alignment==='calendar_month')return 'each month';
  if(!rule.window_days)return 'over the whole period';
  return days===1?'each day':rule.window_alignment==='fixed'?`every ${days} days`:`in any ${days} days`;
}
/** What a number is counted per: "per session", "each week", "per day". */
export function perWords(rule:Row):string{
  if(rule.kind==='block_quota')return quotaPeriod(rule);
  if(['minimum_cover','maximum_participants'].includes(rule.kind))return 'per session';
  if(['max_hours','max_duties'].includes(rule.kind))return windowWords(rule);
  if(rule.kind==='min_rest')return 'between duties';
  if(rule.kind==='supervision_limit')return 'per supervisor';
  return '';
}
/** The numbers of a rule and its lines: a range bar (0 … minimum … target … maximum or "no limit") and a card each.
 *  Lines with their own numbers make each a lowest–highest ("0–3", "varies by line"); no bar when every number is 0. */
export function numbersBlock(ls:Limits[],per:string):HTMLElement{
  const {min,max,target}=spans(ls),top=Math.max(0,...[min,max,target].flatMap(s=>s||[]));
  const end=(max?top*1.25:top*1.6)||1,at=(v:number)=>Math.min(100,Math.max(0,v/end*100));
  const words=(label:string,s:Span|null)=>s?`${label} ${s[0]===s[1]?s[0]:`${s[0]} to ${s[1]}`}`:`no ${label}`;
  const range=el('div',{class:'rl-range',role:'img','aria-label':[words('minimum',min),words('maximum',max),words('target',target)].join(', ').replace(/^./,c=>c.toUpperCase())});
  const fill=el('span',{class:'rl-fill'});fill.style.setProperty('--from',String(at(min?.[0]??0)));fill.style.setProperty('--to',String(max?at(max[1]):100));
  range.append(el('span',{class:'rl-track'}),fill,el('span',{class:'rl-end start'},'0'),...(max?[]:[el('span',{class:'rl-end'},'no limit')]));
  for(const [kind,s] of [['min',min],['target',target],['max',max]] as [string,Span|null][])for(const v of new Set(s||[])){
    if(v===0)continue;  // the bar starts at 0
    const tick=el('span',{class:'rl-tick '+kind},el('span',{class:'rl-tick-label'},String(v)));tick.style.setProperty('--at',String(at(v)));range.append(tick);
  }
  const what=[ls[0].unit,per].filter(Boolean).join(' '),strength=ls.find(l=>l.strength)?.strength;
  const card=(label:string,s:Span|null,unset:string,set=what)=>el('div',{class:'rl-num'+(s?'':' unset')},el('span',{},label),el('strong',{},s?spanText(s):'—'),
    el('small',{},!s?unset:s[0]!==s[1]?`${set} · varies by line`:set));
  return el('div',{class:'rl-numbers'},...(top>0?[range]:[]),el('div',{class:'rl-num-cards'},card('Minimum',min,'no lower limit'),card('Maximum',max,'no upper limit'),
    card('Target',target,min&&!max?'aims for the minimum':'no target',strength?`${what} · ${strength} strength`:what)));
}
/** Weekdays in plain words, in the department's week order: "every day", "Sunday to Thursday", "Friday, Saturday";
 *  `short`: "Sun–Thu". */
export function daysWords(days:number[]|undefined,short=false):string{
  if(!days||days.length===7)return 'every day';
  const order=weekOrder().filter(d=>days.includes(d)),runs:number[][]=[];
  for(const d of order){const run=runs[runs.length-1];if(run&&weekOrder().indexOf(d)===weekOrder().indexOf(run[run.length-1])+1)run.push(d);else runs.push([d]);}
  const day=(d:number)=>short?WEEKDAYS[d].slice(0,3):WEEKDAYS[d];
  return runs.map(r=>r.length>2?`${day(r[0])}${short?'–':' to '}${day(r[r.length-1])}`:r.map(day).join(', ')).join(', ');
}
const PERSON=(field:string)=>!SESSION_FIELDS.includes(field);
const kindOf=(field:string)=>RULE_FIELDS.find(f=>f.key===field)?.kind||'';
/** The top-level "is any of" lines of a scope by field, when the scope is that simple (else null: read it as conditions). */
function simpleScope(scope:Row):Map<string,string[]>|null{
  const tree=scopeAsConditions(scope||{}),out=new Map<string,string[]>();
  if(!tree)return out;
  if(tree.match==='any'&&tree.conditions.length>1)return null;
  for(const c of tree.conditions){if('conditions' in c||c.op==='none'||c.value!=null||c.op==='empty'||c.op==='not_empty')return null;out.set(c.field,[...(out.get(c.field)||[]),...c.values]);}
  return out;
}
const names=(name:Name,field:string,ids:string[])=>ids.map(v=>kindOf(field)?name(kindOf(field),v):v);
/** Who a rule is about, short: "Residents, Rotators +2", "Everyone"; complex conditions read in full. `shown`: how many
 *  names before "+N" (Infinity: all of them). */
export function whoShort(scope:Row,name:Name,shown=2):string{
  const parts=simpleScope(scope);
  if(!parts){const tree=scopeAsConditions(scope||{});
    return conditionsText({match:tree!.match,conditions:tree!.conditions.filter(c=>'conditions' in c||PERSON(c.field))},(f,v)=>kindOf(f)?name(kindOf(f),v):v)||'Everyone';}
  const all=[...parts].filter(([f])=>PERSON(f)).flatMap(([f,ids])=>names(name,f,ids));
  return !all.length?'Everyone':all.length>shown?`${all.slice(0,shown).join(', ')} +${all.length-shown}`:all.join(', ');
}
/** Up to two names joined ("A & B"), more as "A, B +3". */
const few=(xs:string[])=>xs.length>2?`${xs.slice(0,2).join(', ')} +${xs.length-2}`:xs.join(' & ');
/** Who and where a rule's scope names, for its sentence: a single rank as a noun ("consultants", "1 consultant"), else
 *  "people"; and its duty types, clinics or locations ("on Day shift at Physical UCC & Virtual UCC"). */
function scopeWords(scope:Row|undefined,name:Name):{who:(n:unknown)=>string;where:string}{
  const parts=scope?simpleScope(scope):null,person=(n:unknown)=>Number(n)===1?'person':'people';
  if(!parts)return {who:person,where:''};
  const of=(f:string)=>names(name,f,parts.get(f)||[]),who=[...parts].filter(([f])=>PERSON(f));
  const rank=who.length===1&&who[0][0]==='category_ids'&&who[0][1].length===1?of('category_ids')[0]:'';
  // shortcut: English plural of a plain rank name ("Consultant" → "consultants"); other names read "people" (the row's
  // Who column names them).
  const noun=/^[A-Z][a-z]+( [a-z]+)*$/.test(rank)?rank[0].toLowerCase()+rank.slice(1):'';
  const types=of('assignment_type_ids'),clinics=of('service_ids'),places=of('location_ids');
  return {who:n=>noun?(Number(n)===1?noun:pluralWord(noun)):person(n),
    where:[types.length?`on ${few(types)}`:'',clinics.length?`at ${few(clinics)}`:places.length?`in ${few(places)}`:''].filter(Boolean).join(' ')};
}
/** Where and on which days a rule's scope counts, short ("at Pediatrics & Virtual · Thu, Fri"); '' for everywhere, every day. */
export function whereWhen(scope:Row|undefined,name:Name):string{
  const days=scope?.weekdays as number[]|undefined;
  return [scopeWords(scope,name).where,days?.length&&days.length<7?daysWords(days,true):''].filter(Boolean).join(' · ');
}
const QUOTA_UNITS:Record<string,string>={duties:'duties',session_units:'session units',scheduled_hours:'planned hours',reserved_hours:'reserved hours',workload_credit:'workload credits',training_credit:'training credits',lead_turns:'team lead turns'};
/** A quota's number with its unit and period: "4 duties each month", "1 duty each day". */
export function quotaAmount(rule:Row,amount:unknown=rule.amount):string{
  const unit=QUOTA_UNITS[rule.metric]||rule.metric;
  return `${amount} ${Number(amount)===1?unit.replace(/ies$/,'y').replace(/s$/,''):unit} ${quotaPeriod(rule)}`;
}
/** The sessions a line names: its clinics and duty types ("Procedure clinics"), or its people when it names no sessions. */
export const lineSessions=(l:Record<string,any>,name:Name)=>[...(l.service_ids||[]).map((id:string)=>name('services',id)),...(l.assignment_type_ids||[]).map((id:string)=>name('assignment_types',id))].join(', ')
  ||[l.person_id?name('people',l.person_id):'',l.programme_id?name('programmes',l.programme_id):''].filter(Boolean).join(' · ')||'Any session';
/** An allocation order's lines, first to last. */
export const orderedLines=(definition:Row):Row[]=>[...(definition.per_person||[])].sort((a,b)=>Number(b.amount??0)-Number(a.amount??0));
/** The kinds whose sentence names where they apply (duty types, clinics, locations). */
const WHERE_IN_SENTENCE=['minimum_cover','maximum_participants','restriction','allocation_overtime'];
/** One short plain sentence for what a rule asks (the Rules row, the sheet's title, the schedule's rule panel, history):
 *  its numbers, who (a rank by name, else "people") and where (duty types, clinics). Numbers may be ranges ("1–2")
 *  when lines differ. How it counts in detail is in ruleDetails. `definition` gives the scope and the lines. */
export function ruleSentence(rule:Row,definition:Row|undefined,name:Name):string{
  const {who,where}=scopeWords(definition?.scope,name),at=where?' '+where:'';
  const types=(ids:string[]=[])=>few(ids.map(id=>name('assignment_types',id)));
  const ranks=(ids:string[]=[])=>ids.map(id=>name('categories',id)).join(', ');
  switch(rule.kind){
    case 'min_rest':return `At least ${plural(rule.hours,'hour')} rest between duties.`;
    case 'max_hours':return `At most ${plural(rule.hours,'hour')} ${windowWords(rule)}.`;
    case 'max_duties':return `At most ${plural(rule.maximum,'duty','duties')}${rule.group_by==='supervisor'?' with each named supervisor':rule.group_by==='weekday'?' on each weekday':''} ${windowWords(rule)}.`;
    case 'max_consecutive_days':return `At most ${plural(rule.maximum,'day')} in a row.`;
    case 'max_consecutive_weekends':return `At most ${plural(rule.maximum,'weekend')} in a row.`;
    case 'minimum_cover':return `At least ${rule.minimum} ${who(rule.minimum)}${where?at+(rule.aggregation==='service_group'?' together':''):' per session'}.`;
    case 'maximum_participants':return `${rule.overflow_maximum?'Normally at most':'At most'} ${rule.maximum} ${who(rule.maximum)}${at} ${rule.aggregation==='service'?'per clinic at a time':'per session'}.`;
    case 'supervision_limit':return rule.mode==='allowed_ranks'?`Supervisors supervise only ${ranks(rule.trainee_category_ids)||'no ranks'}.`
      :`${rule.overflow_maximum?'Normally at most':'At most'} ${plural(rule.maximum,'trainee')} per supervisor${rule.trainee_category_ids?.length?` (${ranks(rule.trainee_category_ids)})`:''}.`;
    case 'spread_evenly':return `${rule.mode==='maximum_gap'?`Keep holder loads within ${rule.maximum_gap}`:'Level holder loads'} across ${rule.holder||'the supported holders'} per ${rule.within||'session'}.`;
    case 'forbidden_transition':return `No ${types(rule.to_type_ids)} the day after ${types(rule.from_type_ids)}.`;
    case 'restriction':{const days=daysWords(rule.weekdays);return `No duties${at} ${days==='every day'?days:'on '+days}, ${rule.start_time}–${rule.end_time}.`;}
    case 'travel_buffer':return `At least ${plural(rule.minutes,'minute')} to travel from ${name('locations',rule.from_location_id)} to ${name('locations',rule.to_location_id)}.`;
    case 'block_quota':{
      const counted=rule.assignment_type_ids?.length?types(rule.assignment_type_ids):'',amount=(a:unknown)=>{const q=quotaAmount(rule,a);
        return !counted?q:rule.metric==='duties'?q.replace(/^(\S+) /,`$1 ${counted} `):q.replace(/ (each|every|in any|over) /,` of ${counted} $1 `);};
      const aim=rule.target_amount!=null&&rule.comparison!=='target'?` · aim for ${rule.target_amount}`:'';
      // A range reads as one: "Between 6 and 8 session units each week · aim for 7".
      if(rule.comparison==='range')return `Between ${rule.amount} and ${amount(rule.maximum_amount)}${aim}.`;
      return `${({minimum:'At least',maximum:'At most',target:'Aim for'} as Record<string,string>)[rule.comparison]||''} ${amount(rule.amount)}${aim}.`;}
    case 'run_length':return `Prefer ${[rule.minimum?`at least ${rule.minimum}`:'',rule.maximum?`at most ${rule.maximum}`:''].filter(Boolean).join(' and ')} working days in a row.`;
    case 'days_off_run':return `Prefer days off in blocks of at least ${plural(rule.minimum,'day')}.`;
    case 'weekend_shape':return rule.shape==='complete'?'Prefer whole weekends on or off.':'Prefer the same duty on each weekend day.';
    case 'shift_succession':return rule.mode==='forward'?`Forward rotation: ${(rule.type_order||[]).map((id:string)=>name('assignment_types',id)).join(' → ')}.`
      :rule.mode==='avoid'?`Avoid ${types(rule.to_type_ids)} within ${plural(rule.within_days,'day')} after ${types(rule.from_type_ids)}.`
      :`After ${rule.series_length}+ days in a row of ${types(rule.from_type_ids)}, prefer ${plural(rule.rest_days,'day')} off.`;
    case 'together_apart':return `Prefer ${(rule.person_ids||[]).map((id:string)=>name('people',id)).join(', ')} ${rule.mode==='apart'?'not ':''}on duty ${rule.level==='session'?'in the same session':'on the same day'}.`;
    case 'allocation_preference':if(definition&&isOrder(definition)){const steps=orderedLines(definition).map(l=>lineSessions(l,name));
      return `Fills sessions in this order: ${steps.slice(0,5).join(' → ')}${steps.length>5?` → … (${steps.length-5} more)`:''}.`;}
      return `Allocation priority ${rule.priority}${rule.session_priority?` · session priority ${rule.session_priority}`:''}${rule.overtime_priority?` · overtime priority ${rule.overtime_priority}`:''}${rule.supervisor_relationship==='mentor'?' · assigned mentor only':''}.`;
    case 'balance_goal':return goalSentence(rule,name);
    case 'allocation_overtime':return `Offer overtime${at} after regular candidates.`;
    // A catalogue rule says what its catalogue entry is for; its lines carry the numbers.
    case 'catalogue':return catalogueEntryOf(rule as unknown as {kind:string;id?:string;catalogue_version?:number})?.description||`${String(rule.id||'Catalogue rule').replaceAll('_',' ')}.`;
    default:return String(rule.kind).replaceAll('_',' ')+'.';
  }
}
/** What a rule's sentence leaves out: how it counts (overtime, weekends, time zone), for the rule sheet. */
export function ruleDetails(rule:Row,zone:string):string[]{
  const overtime=rule.include_overtime==null?'':rule.include_overtime?'Planned overtime counts.':'Planned overtime does not count.';
  const weekend=rule.weekend_start!=null?`The weekend starts on ${WEEKDAYS[rule.weekend_start]} and lasts ${plural(rule.weekend_days,'day')}.`:'';
  return ({
    max_hours:[overtime],max_duties:[overtime],max_consecutive_days:['Planned overtime counts.'],
    max_consecutive_weekends:[weekend,rule.overtime_only?'Applies when the run includes planned overtime.':''],weekend_shape:[weekend],
    forbidden_transition:['Minimum rest is checked separately.',rule.overtime_only?'Applies when either duty is planned overtime.':''],
    restriction:[`Times are in ${zone}.`],
    block_quota:[rule.overtime==='exclude'?'Planned overtime does not count.':rule.overtime==='only'?'Counts planned overtime only.':''],
    shift_succession:[rule.mode==='forward'?'Avoids an earlier duty the day after a later one.':''],
    allocation_preference:['Safety and eligibility rules still apply.'],allocation_overtime:['All limits and clinical checks still apply.'],
  } as Record<string,string[]>)[rule.kind]?.filter(Boolean)||[];
}
/** Which sessions, short: "Physical UCC, Virtual UCC, together · Day shift · Sun–Thu · 08:00–16:00". */
export function whereShort(scope:Row,rule:Row,name:Name):string{
  // The rule's sentence already names its clinics and duty types: not again.
  const parts=WHERE_IN_SENTENCE.includes(rule.kind)?new Map():simpleScope(scope)||new Map(),list=(f:string)=>names(name,f,parts.get(f)||[]).join(', ');
  const days=scope?.weekdays&&scope.weekdays.length<7?daysWords(scope.weekdays,true):'';
  const hours=(scope?.start_time||'00:00')!=='00:00'||(scope?.end_time||'24:00')!=='24:00'?`${scope.start_time||'00:00'}–${scope.end_time||'24:00'}`:'';
  const clinics=list('service_ids');
  return [clinics&&rule.aggregation==='service_group'?clinics+', together':clinics,list('location_ids'),list('assignment_type_ids'),days,hours].filter(Boolean).join(' · ');
}
/** Who and when in one sentence, the named things in bold. */
export function whoWhen(scope:Row,rule:Row,name:Name,listedOnly=false):HTMLElement{
  const p=el('p',{class:'rl-sentence'}),parts=simpleScope(scope);
  const bold=(xs:string[])=>xs.flatMap((x,i)=>[i?(i===xs.length-1?' and ':', '):'',el('b',{},x)]);
  const days=daysWords(scope?.weekdays),hours=(scope?.start_time||'00:00')!=='00:00'||(scope?.end_time||'24:00')!=='24:00'?`${scope.start_time||'00:00'}–${scope.end_time||'24:00'}`:'all day';
  const counts=['minimum_cover','maximum_participants'].includes(rule.kind);
  if(!parts){
    p.append('Applies where these conditions hold: ',el('b',{},conditionsText(scopeAsConditions(scope),(f,v)=>kindOf(f)?name(kindOf(f),v):v)),`, on ${days}, ${hours}.`);
    return p;
  }
  const of=(f:string)=>names(name,f,parts.get(f)||[]);
  const types=of('assignment_type_ids'),clinics=of('service_ids'),places=of('location_ids');
  p.append(...(types.length?bold(types):['All']),' sessions',...(clinics.length?[' at ',...bold(clinics)]:[]),...(places.length?[' in ',...bold(places)]:[]),
    ...(rule.aggregation==='service_group'?[', counted together']:[]),', on ',el('b',{},days),`, ${hours}. `);
  const who=[...parts].filter(([f])=>PERSON(f)).flatMap(([f,ids])=>names(name,f,ids));
  if(listedOnly)p.append('Applies only to the people and programmes listed below.');
  else p.append(counts?'Counts ':'Applies to ',...(who.length?bold(who):['everyone']),counts&&rule.kind==='minimum_cover'?' who are eligible and available.':'.');
  return p;
}
/** Lines a plain list can show: a person's or a programme's own numbers and dates, nothing else. */
export function plainLines(d:Row):boolean{
  const extra=['category_ids','weekdays','start_time','end_time','service_ids','assignment_type_ids'];
  return d.rule.kind!=='allocation_overtime'&&d.line_number!=='session_priority'&&(d.per_person||[]).every((l:Row)=>(l.person_id||l.programme_id)
    &&extra.every(k=>l[k]==null||(Array.isArray(l[k])&&!l[k].length))&&Object.entries(l.settings||{}).every(([k,v])=>v==null||(Array.isArray(v)&&!v.length)||['maximum_amount','target_amount'].includes(k)));  // the server sends every setting, unset ones null
}
const datesOf=(l:Row,zone:string)=>l.starts_on&&l.ends_on?`${formatDate(l.starts_on,zone)} – ${formatDate(addDays(l.ends_on,-1),zone)}`
  :l.ends_on?`until ${formatDate(addDays(l.ends_on,-1),zone)}`:l.starts_on?`from ${formatDate(l.starts_on,zone)}`:'';
/** One exception's numbers in words: "minimum 1 · target 3"; "the rule's numbers" when it has none of its own. */
export function lineNumbers(rule:Row,l:Row):string{
  const own=l.amount!=null||l.settings?.maximum_amount!=null||l.settings?.target_amount!=null,n=own?lineLimits(rule,l):null;
  return n?[n.min!=null?`minimum ${n.min}`:'',n.max!=null?`maximum ${n.max}`:'',n.target!=null?`target ${n.target}`:''].filter(Boolean).join(' · '):'the rule’s numbers';
}
/** Who an exception is for, as "Person" or "Programme", with their name. */
export const lineWhoName=(l:Row,name:Name):[string,string]=>l.person_id?['Person',name('people',l.person_id)]:['Programme',name('programmes',l.programme_id)];
/** The exceptions as a list: who, Person/Programme, their numbers and dates; `edit` adds an Edit button per row. */
export function exceptionList(rule:Row,lines:Row[],name:Name,zone:string,edit?:(index:number)=>void):HTMLElement{
  return el('ul',{class:'rl-excs','aria-label':'Lines'},...lines.map((l,i)=>{
    const [kind,who]=lineWhoName(l,name);
    const b=edit?el('button',{type:'button',class:'icon-button bare','aria-label':`Edit line for ${who}`,'data-tip':'Edit'},icon('edit')):null;
    b?.addEventListener('click',()=>edit!(i));
    return el('li',{class:'rl-exc'},l.person_id?avatar(who,'muted'):el('span',{class:'rl-exc-icon','aria-hidden':'true'},icon('book')),
      el('div',{},el('strong',{},who),el('small',{},[kind,lineNumbers(rule,l),datesOf(l,zone)].filter(Boolean).join(' · '))),b||'');}));
}
/** The same numbers for one line: its own number and settings, else the rule's. */
export function lineLimits(rule:Row,line:Row):Limits|null{
  const field=rule.kind==='block_quota'?'amount':RULE_NUMBERS[rule.kind]?.[0];
  if(!field)return null;
  const own=(k:string)=>line.settings?.[k]??rule[k];
  return ruleLimits({...rule,[field]:line.amount??rule[field],...(rule.kind==='block_quota'?{maximum_amount:own('maximum_amount'),target_amount:own('target_amount')}:{})});
}
/** The numbers a line may set for this rule: [label, where the value lives]. A quota line sets its own minimum or maximum
 *  (its number), the maximum of a range and a target; any other rule its one number. */
function lineFields(rule:Row):[string,'amount'|'maximum_amount'|'target_amount'][]{
  if(rule.kind==='block_quota')return [[rule.comparison==='maximum'?'Maximum':rule.comparison==='target'?'Target':'Minimum','amount'],
    ...(rule.comparison==='range'?[['Maximum','maximum_amount'] as [string,'maximum_amount']]:[]),...(rule.comparison!=='target'?[['Target','target_amount'] as [string,'target_amount']]:[])];
  const n=RULE_NUMBERS[rule.kind];
  return n?[[n[2]==='higher'?'Minimum':'Maximum','amount']]:[];
}
/** The change dialog's Lines step: a person's or a programme's own numbers and dates as a list. One row opens at a
 *  time ("Numbers for them", "When"); Save, Cancel and Remove act on that row only, and nothing is saved before the
 *  rule is proposed. `programmes`: a rule that applies only through its lines may also list programmes. */
export function exceptionsEditor(rule:()=>Row,given:Row[],choices:{people:{value:string;label:string}[];programmes:{value:string;label:string}[]},name:Name,zone:string,programmes:boolean){
  const rows:Row[]=given.map(r=>({...r,settings:r.settings?{...r.settings}:null}));
  let open=-1,draft:Row|null=null;
  const list=el('ul',{class:'rl-excs editable','aria-label':'Lines'});
  const wrap=el('div',{class:'span-2 rl-exc-editor','data-step':'Lines'},el('div',{},el('h3',{},'Lines'),
    el('p',{class:'hint'},'Give a person'+(programmes?' or a programme':'')+' its own numbers or dates. Everyone else uses the rule’s numbers.')),list);
  const changed=()=>{wrap.dataset.reviewValue=rows.length?rows.map(r=>`${lineWhoName(r,name)[1]}: ${lineNumbers(rule(),r)}${datesOf(r,zone)?' · '+datesOf(r,zone):''}`).join('; '):'None';
    wrap.dispatchEvent(new Event('change',{bubbles:true}));};
  wrap.dataset.reviewLabel='Lines';
  const close=()=>{open=-1;draft=null;draw();};
  function editor(r:Row,i:number){
    const [kind,who]=r.person_id||r.programme_id?lineWhoName(r,name):[r.programme_id===''?'Programme':'Person',''];
    const pick=!who?select(kind,kind==='Person'?choices.people:choices.programmes,'',true,`Choose a ${kind.toLowerCase()}`):null;
    const nums=lineFields(rule()).map(([label,key])=>{const own=key==='amount'?r.amount:r.settings?.[key];
      const f=field(label,'number',own??'',{min:0,max:100000,step:'any',placeholder:'Same'});f.input.classList.add('num');return {key,f};});
    const from=field('From','date',r.starts_on||''),until=field('Until (included)','date',r.ends_on||'');lastDayIncluded(until.input);
    const save=button('Save line',()=>{
      const bad=[pick?.input,...nums.map(n=>n.f.input),from.input,until.input].find(x=>x&&!x.checkValidity());if(bad){bad.reportValidity();return;}
      if(from.input.value&&until.input.value&&until.input.value<=from.input.value){until.input.setCustomValidity('Until must be on or after From.');until.input.reportValidity();until.input.setCustomValidity('');return;}
      const next:Row={...r,...(pick?{[kind==='Person'?'person_id':'programme_id']:pick.input.value}:{}),starts_on:from.input.value||null,ends_on:until.input.value||null};
      const settings:Record<string,number>={...(r.settings||{})};
      for(const {key,f} of nums){const v=f.input.value===''?null:Number(f.input.value);if(key==='amount')next.amount=v;else if(v==null)delete settings[key];else settings[key]=v;}
      next.settings=Object.keys(settings).length?settings:null;
      if(i<0)rows.push(next);else rows[i]=next;
      close();changed();announce(`Exception for ${lineWhoName(next,name)[1]} saved.`);},'primary small');
    const remove=i<0?'':armed(button('Remove line',()=>{rows.splice(i,1);close();changed();announce('Line removed.');},'ghost small danger'),'Remove?');
    const node=el('li',{class:'rl-exc open'},el('div',{class:'rl-exc-title'},who?el('strong',{},who):pick!.wrap,badge(kind)),
      el('div',{class:'rl-exc-fields'},...(nums.length?[el('fieldset',{class:'rl-exc-set'},el('legend',{},'Numbers for them'),...nums.map(n=>n.f.wrap))]:[]),
        dateRange('When',from,until)),
      el('p',{class:'hint'},'Empty means the rule’s own value'+(nums.length?'':'s')+' and dates.'),
      el('div',{class:'rl-exc-actions'},remove,el('span',{class:'grow'}),button('Cancel',close,'secondary small'),save));
    requestAnimationFrame(()=>node.querySelector<HTMLElement>('select,input')?.focus());
    return node;
  }
  function draw(){
    list.replaceChildren(...rows.map((r,i)=>{
      if(i===open)return editor(r,i);
      const [kind,who]=lineWhoName(r,name);
      const edit=iconButton(`Edit line for ${who}`,'edit',()=>{open=i;draft=null;draw();},{class:'bare','data-tip':'Edit'});
      const drop=iconButton(`Remove line for ${who}`,'trash',()=>{rows.splice(i,1);if(open>i)open--;draw();changed();announce(`Line for ${who} removed.`);},{class:'bare','data-tip':'Remove'});
      return el('li',{class:'rl-exc'},el('div',{},el('div',{class:'rl-exc-title'},el('strong',{},who),badge(kind)),
        el('small',{},[lineNumbers(rule(),r),datesOf(r,zone)].filter(Boolean).join(' · '))),edit,drop);}),
      ...(draft?[editor(draft,-1)]:[]));
    list.hidden=!list.childElementCount;
  }
  const adder=(kind:'Person'|'Programme')=>compact(button(`Add a ${kind.toLowerCase()}`,()=>{open=-1;draft=(kind==='Person'?{person_id:''}:{programme_id:''}) as unknown as Row;draw();},'secondary small'),'plus');
  wrap.append(el('div',{class:'rl-add'},adder('Person'),...(programmes?[adder('Programme')]:[]),el('span',{class:'hint'},'Ranks, clinics and duty types belong in Who & when.')));
  draw();changed();
  return {wrap,read:():PersonAmount[]=>rows.map(r=>Object.fromEntries(Object.entries(r).filter(([,v])=>v!=null&&v!=='')) as unknown as PersonAmount),
    edit:(i:number)=>{open=i;draw();},add:()=>{draft={person_id:''} as unknown as Row;draw();}};
}
const dayLabel=(day:string)=>formatDay(day,'UTC');
/** How the rule fares in a draft: a bar per day it touches (met, short, advisory miss), "19 of 21 days met" and where it
 *  is short. `fresh` false: the draft changed since its check, so the days are not judged. */
export function healthStrip(entry:MapRule,days:string[],fresh:boolean,people:number|null):HTMLElement{
  const why=(d:number)=>{const m=short.get(d)![3];return m&&m!==entry.name?` (${m})`:'';};
  const short=new Map((entry.short_days||[]).map(d=>[d[0],d]));
  const touched=entry.days.filter(d=>d<days.length);
  const counts=people==null?'':` Counts ${plural(people,'person','people')} today.`;
  if(!touched.length)return el('p',{class:'hint'},`It applies to no day of this draft.${counts}`);
  if(!fresh||entry.status.state==='unchecked')return el('p',{class:'hint'},`Not checked since the draft last changed: check the draft to see where it is met.${counts}`);
  const bars=el('div',{class:'rl-days'},...touched.map(d=>{const s=short.get(d);
    return el('span',{class:'rl-day'+(s?(s[1]?' short':' warn'):''),title:`${dayLabel(days[d])}: ${s?s[3]:'met'}`});}));
  const met=touched.filter(d=>!short.has(d)).length,missed=touched.filter(d=>short.has(d));
  bars.setAttribute('role','img');bars.setAttribute('aria-label',`Met on ${met} of ${touched.length} days${missed.length?`; short on ${missed.map(d=>dayLabel(days[d])).join(', ')}`:''}`);
  const where=missed.slice(0,3).flatMap((d,i)=>[i?(i===Math.min(missed.length,3)-1?' and ':', '):'',el('b',{},dayLabel(days[d])),why(d)]);
  return el('div',{class:'rl-health'},el('div',{class:'rl-health-row'},bars,el('span',{class:'rl-health-count'},el('b',{},`${met} of ${touched.length}`),' days met')),
    el('p',{class:'hint'},...(missed.length?['Short on ',...where,missed.length>3?` and ${missed.length-3} more days.`:'.']:['Met on every day it applies.']),counts));
}
