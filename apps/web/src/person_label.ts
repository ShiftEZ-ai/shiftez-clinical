/** How a person is named (SSOT, D75): every screen names people through `personLabel` — the Schedule with its view's
 *  label, every other screen with the effective default (`personName`) — and every name's words come from `nameWords`
 *  (dom.ts) under the department's title and particle rules. The full name is always in the accessible name. */
import {bidi,button,el,labelOf,nameWords,uniqueCodes,autoCode} from './dom.js';
import {LABEL_PARTS,LABEL_PART_DEFINITIONS,LABEL_SEPARATORS,type PersonLabel} from './generated.js';
import type {Row} from './types.js';

export type LabelPart=typeof LABEL_PARTS[number];
type Facts={level?:string;code?:string;programme?:string};
/** Registered once by app.ts: its catalogue lists (people, memberships, ranks), a person by id, and the label a screen
 *  without its own view uses (the person's own choice, else the department's). The facts a label reads (current rank,
 *  unique code) are worked out here from those lists (audit C19: the whole name pipeline lives in this module). */
let source:{rows:(kind:string)=>Row[];facts:(person:Row|undefined)=>Facts;person:(id:string)=>Row|undefined;label:()=>PersonLabel}=
  {rows:()=>[],facts:p=>labelFacts(p),person:()=>undefined,label:()=>({parts:['full']})};
export const useNames=(next:Partial<typeof source>)=>{source={...source,...next};};
/** A person's unique short code: their own, else the automatic one (first + middle + family initial, or first + two
 *  family letters, ignoring a leading "Al"; dom.ts uniqueCodes), worked out once per people list. */
let codeCache:{people:Row[];codes:Map<string,string>}|null=null;
export function shortCode(person:Row|undefined):string{
  if(!person)return '—';
  const people=source.rows('people');if(codeCache?.people!==people)codeCache={people,codes:uniqueCodes(people as {id:string}[])};
  return codeCache.codes.get(person.id)||String(person.short_code||'')||autoCode(fullNameOf(person))||'—';
}
/** A person's current rank, for the Level part of a label (read once per catalogue load). */
let levelCache:{rows:Row[];levels:Map<string,string>}|null=null;
function levelNow(id:string){
  const list=source.rows('memberships');
  if(levelCache?.rows!==list){const today=new Date().toISOString().slice(0,10),ranks=new Map(source.rows('categories').map(c=>[c.id,labelOf(c)]));
    levelCache={rows:list,levels:new Map(list.filter(m=>(!m.starts_on||m.starts_on<=today)&&(!m.ends_on||m.ends_on>today)).map(m=>[m.person_id,ranks.get(m.category_id)||'']))};}
  return levelCache.levels.get(id)||'';
}
/** Forget the worked-out codes and ranks (a new session, or the department's code rules changed). */
export const forgetNames=()=>{codeCache=null;levelCache=null;};
/** The facts a label part reads: the person's current rank and their unique code. */
export function labelFacts(person:Row|undefined):Facts{return {level:person?levelNow(person.id):'',code:person?shortCode(person):''};}
/** Every person name opens the one person hub (app.ts registers it); without it, People opens on them. */
let opener:((personId:string,label:string)=>void)|null=null;
export const onPersonOpen=(open:(personId:string,label:string)=>void)=>{opener=open;};
/** What a screen reader hears for a shown name: the shown words, then the full name when they do not already say it
 *  (WCAG 2.5.3: the accessible name starts with the visible label). */
export const spokenName=(shown:string,full:string)=>!full||shown.includes(full)?shown:`${shown}, ${full}`;
/** A person's name as a link to their hub; a label that is not the full name carries it in its accessible name and tip. */
export function personLink(personId:string,label:string,navigate:(path:string)=>unknown,known=''){
  // `known`: the full name the reply carried, for a reader whose lists do not hold this person.
  const full=fullNameOf(source.person(personId)||{id:'',revision:0,display_name:known||label}),b=button(label,()=>opener?opener(personId,full):navigate('people?open='+encodeURIComponent(personId)),'',{class:'link-button person-link'});
  b.replaceChildren(bidi(label));
  if(spokenName(label,full)!==label){b.setAttribute('aria-label',spokenName(label,full));b.dataset.tip=full;}
  return b;
}
/** Each part's name and each separator's text, from the contract (LABEL_PART_DEFINITIONS, LABEL_SEPARATORS; D75). */
export const LABEL_NAMES=Object.fromEntries(Object.entries(LABEL_PART_DEFINITIONS).map(([k,d])=>[k,d.label])) as Record<LabelPart,string>;
export const SEPARATORS=Object.fromEntries(Object.entries(LABEL_SEPARATORS).map(([k,d])=>[k,[d.text,d.label]])) as Record<PersonLabel['separator']&string,[string,string]>;
/** What a part shows for this person: facts the caller knows (their current rank, their unique code) fill the rest.
 *  `expand` widens the short part where it clashes in a view (`distinctNames`): 1 says the given and family name, 2 the full name. */
export function labelPart(person:Row|undefined,part:LabelPart,honorific:PersonLabel['honorific'],facts:Facts=source.facts(person),expand=0):string{
  const n=nameWords(fullNameOf(person)),title=honorific==='none'||!n.title?'':n.title+' ';
  switch(part){
    case 'short':return title+(expand>1?n.bare:n.given.length?`${expand?n.given[0]:n.given[0][0]+'.'} ${n.family}`:n.family);
    case 'full':return title+n.bare;
    case 'level':return facts.level||'';
    case 'pager':return String(person?.pager||'');
    case 'code':return facts.code||String(person?.short_code||'');
    case 'number':return String(person?.staff_number||'');
    case 'first':return n.given.join(' ');
    case 'programme':return facts.programme||'';
  }
}
/** The label the page shows people with now (the Schedule registers its view's), for keys that explain codes. */
let current:()=>PersonLabel=()=>source.label();
export const useLabel=(next:()=>PersonLabel)=>{current=next;};
/** No name part is shown (only codes, numbers, ranks or pagers): keys list who each code is. */
export const namesHidden=()=>!current().parts.some(p=>p==='short'||p==='full');
/** "Dr L. Haddad · R4": the parts that have a value, in order. Nothing to show falls back to the code, then the name. */
export function personLabel(person:Row|undefined,label:PersonLabel,facts:Facts=source.facts(person),expand=0):string{
  if(!person)return '—';
  const text=label.parts.map(p=>labelPart(person,p,label.honorific,facts,expand)).filter(Boolean).join(SEPARATORS[label.separator||'dot'][0]);
  return text||facts.code||fullNameOf(person);
}
/** Every schedule renderer names people through this (hooks.name): the page's label now (a view's Person label). */
export const displayName=(person:Row|undefined,label=current())=>person?personLabel(person,label):fullNameOf(person);
/** The names of one view (a grid, a printed page): each person's label, except that people whose labels say the same
 *  words in this view expand their short part, first to given and family name, then to the full name where that still
 *  clashes. Everyone else keeps the label; the tip and accessible name keep the full name as before. */
export function distinctNames(people:Iterable<Row|undefined>,label:PersonLabel=current()):(person:Row|undefined)=>string{
  const expand=new Map<string,number>(),unique=new Map<string,Row>();
  for(const p of people)if(p)unique.set(p.id,p);
  let left=[...unique.values()];
  for(let level=0;level<2&&left.length>1&&label.parts.includes('short');level++){
    const said=new Map<string,Row[]>();
    for(const p of left){const t=personLabel(p,label,undefined,level);said.set(t,[...(said.get(t)||[]),p]);}
    left=[...said.values()].filter(g=>g.length>1).flat();
    for(const p of left)expand.set(p.id,level+1);
  }
  return person=>person?personLabel(person,label,undefined,expand.get(person.id)||0):fullNameOf(person);
}
/** A name pill's narrower forms (LEAD-UIUX live audit L7): the short form (initial and family name, no title) and the
 *  person's code. The pill shows them only when its cell is too narrow for `shown` (person_pill.ts, app.css @container
 *  pill); the full name stays its tip and accessible name. Empty where a form says nothing new. */
export function narrowNames(id:string,shown:string):{narrow:string;tight:string}{
  const person=source.person(id);if(!person)return {narrow:'',tight:''};
  const narrow=labelPart(person,'short','none'),tight=labelPart(person,'code','none');
  return {narrow:narrow&&narrow.length<shown.length?narrow:'',tight:tight&&tight.length<Math.min(shown.length,narrow.length||shown.length)?tight:''};
}
/** A person named with the effective default label, for screens without a view of their own: by id from the
 *  catalogue, else the name the reply carried (`known`), formatted the same way. */
export function personName(id:string|null|undefined,known:string|null|undefined='',label=source.label()):string{
  known=String(known||'');const person=(id&&source.person(id))||(known?{id:id||'',revision:0,display_name:known} as Row:undefined);
  return person?personLabel(person,label):known||'—';
}
/** A person's full name, from their row (the name everything else is formatted from). */
export const fullNameOf=(person:Row|undefined)=>String(person?.display_name||person?.name||'Unavailable record');
/** The full name for an accessible name or a tooltip. */
export function fullName(id:string|null|undefined,known:string|null|undefined=''){const person=id?source.person(id):undefined;return person?fullNameOf(person):String(known||'');}
/** A person's name as text: the label shown, the full name as its tip and, when the label does not say it, read out
 *  after it for screen readers. */
export function nameText(id:string|null|undefined,known:string|null|undefined='',o:{label?:PersonLabel;class?:string;missing?:string}={}):HTMLElement{
  // Nobody to name (no record, no name in the reply): the placeholder as it is, never formatted as a name.
  if(!(id&&source.person(id))&&!known)return el('span',{class:o.class},o.missing??'—');
  const shown=personName(id,known,o.label),full=fullName(id,known)||shown,extra=spokenName(shown,full)!==shown;
  return el('span',{class:o.class,'data-tip':extra?full:undefined},bidi(shown),extra?el('span',{class:'sr-only'},', '+full):'');
}
