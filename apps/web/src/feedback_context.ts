/** What a feedback report says about where and how it happened: the pages visited just
 *  before, the last errors this page showed or threw and the server's reference for each recent request. Kept in memory only, never stored; nothing here
 *  imports the app, so api.ts and dom.ts can record into it. */
import {FEEDBACK_LIMITS,type FeedbackError} from './generated.js';

const MAX=20,errors:FeedbackError[]=[],requests:string[]=[],routes:string[]=[];
const clip=(text:string,n:number)=>text.length>n?text.slice(0,n-1)+'…':text;
/** The server's links whose last part is itself a credential: /invite/{token}, /reset/{token},
 *  /api/account-links/{token} and a calendar subscription's /calendar/{token}.ics. */
const CREDENTIAL_PATH=/(\/(?:invite|reset|calendar|api\/account-links)\/)[^\s/?#"'<>]+/gi;
/** The text with every credential in such a link replaced: what the page remembers, and what a feedback picture shows. */
export const redactCredentials=(text:string)=>text.replace(CREDENTIAL_PATH,'$1[redacted]');
/** The text without bidi isolate marks (U+2066–2069, dom.ts isolate): what is recorded, reported or compared is always
 *  the raw words (INN-043: isolates are render only). The one definition; dom.ts re-exports it. */
export const unisolated=(text:string)=>text.replace(/[\u2066-\u2069]/g,'');
/** An error this page showed (a toast, a failed save) or threw (console, an unhandled rejection). */
export function recordError(message:unknown,source:string){
  const text=clip(redactCredentials(unisolated(String((message as Error)?.message??message??''))).replace(/\s+/g,' ').trim(),300);if(!text)return;
  const last=errors.at(-1);if(last&&last.message===text&&last.source===source)return;  // a repeat says nothing new
  errors.push({at:new Date().toISOString(),message:text,source:clip(redactCredentials(source),100)});if(errors.length>MAX)errors.shift();
}
/** The server's X-Correlation-ID of a request, so a report points at its log lines. */
export function recordRequest(id:string|null){if(!id||!/^[A-Za-z0-9_.:-]{1,100}$/.test(id))return;requests.push(id);if(requests.length>MAX)requests.shift();}
export const recentErrors=():FeedbackError[]=>errors.map(e=>({...e}));
export const recentRequests=():string[]=>[...requests];
/** A page visited (its hash route, as the report's own route is sent).
 *  The server removes queries and record identifiers before storing them. */
export function recordRoute(route:string){
  route=redactCredentials(route);
  if(route.length>FEEDBACK_LIMITS.route||!/^#(?:[A-Za-z0-9_-]|\/(?!\/))/.test(route)||/[\x00-\x1f\\]/.test(route)){return;}
  // A change of query alone (a filter, an opened row) stays the same visit.
  if(routes.at(-1)?.split('?')[0]===route.split('?')[0])routes.pop();
  routes.push(route);if(routes.length>FEEDBACK_LIMITS.recent_routes+1)routes.shift();
}
export const recentRoutes=():string[]=>[...routes];
export function clearFeedbackContext(){errors.length=0;requests.length=0;routes.length=0;}

if(typeof window!=='undefined'){
  const visit=()=>recordRoute(location.hash.length>1?location.hash:'#home');
  window.addEventListener('hashchange',visit);visit();
  // In-app navigation rewrites the address with history.replaceState, which fires no hashchange: follow it here.
  for(const m of ['pushState','replaceState'] as const){
    const original=history[m].bind(history);
    history[m]=(...args:Parameters<History['replaceState']>)=>{original(...args);visit();};
  }
  window.addEventListener('error',e=>recordError(e.message||e.error,'uncaught error'));
  window.addEventListener('unhandledrejection',e=>{const r=e.reason as {code?:string}|undefined;if(r?.code!=='PAGE_LEFT'&&r?.code!=='SESSION_CHANGED')recordError(e.reason,'unhandled rejection');});
  // console.error keeps working as before; it is only also remembered.
  const original=console.error.bind(console);
  console.error=(...args:unknown[])=>{recordError(args.map(a=>a instanceof Error?a.message:typeof a==='string'?a:'').filter(Boolean).join(' '),'console');original(...args);};
}
