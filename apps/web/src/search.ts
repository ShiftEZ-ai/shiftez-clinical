/**
 * Search options (generated contract: SearchOptions, SEARCH_MODES, SEARCH_SCOPES, SEARCH_DEFAULTS, SEARCH_COLLECTIONS) and
 * the one record matcher for every list searched in the browser. What a search matches is text or a picked record: a
 * record's own fields ("self") or the records it explicitly references ("related": a person, programme, tag, clinic).
 * It is never proof that a rule applies to someone or that someone is eligible; Which rule applies (the server) answers
 * that. The server matches the same way (apps/api/services/search.py text_matches): only the query's ends are trimmed,
 * case folds by Unicode lowercase unless case_sensitive, exact compares one whole field, no fuzzy or accent folding.
 */
import {get} from './api.js';
import {BULK_COLLECTIONS,SEARCH_COLLECTIONS,SEARCH_DEFAULTS,SEARCH_ENTITY_KINDS,SEARCH_MODES,SEARCH_SCOPES,type SearchEntityKind,type SearchOptions,type SearchReference} from './generated.js';

export type {SearchEntityKind,SearchOptions,SearchReference};
export type SearchScope=NonNullable<SearchOptions['scope']>;
/** What a list can search: the scopes it offers and the kinds of record a reference can name (empty: none). */
export type SearchCapability={scopes:readonly SearchScope[];kinds:readonly SearchEntityKind[];collection?:string};

/** A collection's registry entry (generated), or null when the server has none for it. */
export const searchCollection=(key:string|undefined)=>key&&Object.hasOwn(SEARCH_COLLECTIONS,key)?SEARCH_COLLECTIONS[key as keyof typeof SEARCH_COLLECTIONS]:null;
/** A server-searched list's capability, straight from the registry; the list's own record kind can be referenced too. */
export function serverCapability(key:string|undefined):SearchCapability|null{
  const c=searchCollection(key);if(!c)return null;
  return {scopes:c.scopes,kinds:[...new Set([...ownKind(key),...c.reference_kinds as readonly SearchEntityKind[]])],collection:key};
}
/** The list's own record kind when references can name it (People, Programmes, Tags…). */
export const ownKind=(key:string|undefined):SearchEntityKind[]=>(SEARCH_ENTITY_KINDS as readonly string[]).includes(key||'')?[key as SearchEntityKind]:[];

/** The options as sent and saved: only what differs from SEARCH_DEFAULTS, in one key order; undefined for the defaults.
 *  Absent options keep every list's older plain search (the server's too), so resetting never opts an old query in. */
export function normalSearch(o:SearchOptions|null|undefined):SearchOptions|undefined{
  if(!o)return undefined;
  const out:SearchOptions={};
  if(o.match&&o.match!==SEARCH_DEFAULTS.match)out.match=o.match;
  if(o.scope&&o.scope!==SEARCH_DEFAULTS.scope)out.scope=o.scope;
  if(o.case_sensitive)out.case_sensitive=true;
  if(o.reference?.kind&&o.reference.id)out.reference={kind:o.reference.kind,id:o.reference.id};
  return Object.keys(out).length?out:undefined;
}
export const sameSearch=(a:SearchOptions|null|undefined,b:SearchOptions|null|undefined)=>JSON.stringify(normalSearch(a)??null)===JSON.stringify(normalSearch(b)??null);
/** The JSON query parameter (feedback, workspaces, notifications), or undefined to send none. */
export const searchParam=(o:SearchOptions|null|undefined)=>{const n=normalSearch(o);return n?JSON.stringify(n):undefined;};
const label=<T extends {value:string;label:string}>(list:readonly T[],value:string|undefined)=>list.find(x=>x.value===value)?.label||'';
const modeLabel=(o:SearchOptions|undefined)=>label(SEARCH_MODES,o?.match||SEARCH_DEFAULTS.match);
const scopeLabel=(o:SearchOptions|undefined)=>label(SEARCH_SCOPES,o?.scope||SEARCH_DEFAULTS.scope);
/** A record kind's name (BULK_COLLECTIONS: "People", "Programmes", "Tags"). */
export const kindLabel=(kind:string)=>(BULK_COLLECTIONS as Record<string,{label:string}>)[kind]?.label||kind;
/** How the options narrow the search, in words ("Exact text · Own text"); '' for the defaults. A picked record is said
 *  by its own token beside the box. */
export function searchWords(o:SearchOptions|null|undefined):string{
  const n=normalSearch(o);if(!n)return '';
  return [n.match?modeLabel(n):'',n.scope?scopeLabel(n):'',n.case_sensitive?'Case-sensitive':''].filter(Boolean).join(' · ');
}

/** The words of a collection toolbar's filtered strip ("Filtered: 9 of 43 people shown · Exact text"), or null when no
 *  search or filter beyond the view's own hides anything. `of` is what the view holds before them; a list the server
 *  searches or filters has none, so it says only how many are shown. None left still says it (the way back). */
export function filteredWords(o:{searching:boolean;filters:number;count:number;of?:number;noun:[string,string];options?:string}):string|null{
  if(!o.searching&&o.filters<1||o.of!==undefined&&o.count>=o.of)return null;
  const what=o.of!==undefined?`${o.count} of ${o.of} ${o.noun[1]}`:`${o.count} ${o.count===1?o.noun[0]:o.noun[1]}`;
  return `Filtered: ${what} shown${o.searching&&o.options?' · '+o.options:''}`;
}

/** A test of one field's text against the typed query; null when nothing is typed. Only the query's ends are trimmed;
 *  `exact` compares the whole field, not a word inside it. */
export function fieldMatcher(q:string,o:SearchOptions|null|undefined={}):((text:string)=>boolean)|null{
  const sensitive=!!o?.case_sensitive,fold=(x:string)=>sensitive?x:x.toLowerCase(),want=fold(q.trim());
  if(!want)return null;
  return o?.match==='exact'?text=>fold(text)===want:text=>fold(text).includes(want);
}
/** Whether a search with these options reads a record's own fields (`linked` false) or what it references (true). */
export const searchReads=(o:SearchOptions|null|undefined,linked:boolean)=>(o?.scope||'all')!==(linked?'self':'related');

/** A record as search reads it: each of its own field values, each related record's name (one entry per name, never a
 *  joined "who" sentence), its id and the ids it explicitly references. */
export type SearchRecord={own:()=>readonly unknown[];related?:()=>readonly unknown[];id?:string;refs?:()=>readonly unknown[]};
/** The one record test for local lists; null when nothing is searched. A picked record replaces the text: "self" is the
 *  record itself, "related" the records that reference it (ids are unique across kinds). */
export function recordMatcher(q:string,o:SearchOptions|null|undefined):((r:SearchRecord)=>boolean)|null{
  const n=normalSearch(o),ref=n?.reference;
  if(ref){const self=searchReads(n,false),rel=searchReads(n,true);
    return r=>self&&r.id===ref.id||rel&&!!r.refs?.().some(x=>x===ref.id);}
  const m=fieldMatcher(q,n);if(!m)return null;
  const own=searchReads(n,false),rel=searchReads(n,true),hit=(x:unknown)=>x!==null&&x!==undefined&&x!==''&&m(String(x));
  return r=>own&&r.own().some(hit)||rel&&!!r.related?.().some(hit);
}

/** Records a reference can name, found by name on the server (GET /api/search/references): bounded, never the whole
 *  directory (an empty query asks nothing), within what the reader may see. A page read: leaving the page drops it. */
export type ReferenceMatch={kind:SearchEntityKind;id:string;label:string};
export function findReferences(collection:string,kind:SearchEntityKind,q:string,limit=20):Promise<{items:ReferenceMatch[];has_more:boolean}>{
  const text=q.trim();
  if(!text)return Promise.resolve({items:[],has_more:false});
  return get(`/api/search/references?${new URLSearchParams({collection,kind,q:text,limit:String(limit)})}`);
}
