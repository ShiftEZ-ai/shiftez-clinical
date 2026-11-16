/** maintenance: a tab left open across a deploy keeps running its old bundle, and its pages can fail against the new server.
 *  This bundle's identity is its asset directory, the same name the server's pulse sends as `build`. */
import {ApiError,unexpectedError} from './api.js';

export const OWN_BUILD=new URL('.',import.meta.url).pathname.split('/').filter(Boolean).pop()||'';
const KEY='fmuc.stale-reload';

/** What a page that failed to draw does. If the server has a newer bundle than this one, reload once onto it, or, with
 *  something unsaved on screen, offer "Reload to update". Otherwise (no newer bundle seen, or this tab already
 *  reloaded for that bundle) it fails as usual. */
export function staleChoice(o:{own:string;seen:string;failed:boolean;dirty:boolean;reloadedFor:string}):'reload'|'ask'|'none'{
  if(!o.failed||!o.own||!o.seen||o.seen===o.own||o.reloadedFor===o.seen)return 'none';
  return o.dirty?'ask':'reload';
}
/** The bundle this tab last reloaded for (sessionStorage: this tab only), the loop guard. */
export function reloadedFor(){try{return (sessionStorage.getItem(KEY)||'').split(' ')[0];}catch{return '';}}
/** Reloads onto `build`, recording it first. Without storage there is no loop guard, so it never reloads (false). */
export function reloadFor(build:string){
  try{sessionStorage.setItem(KEY,build);}catch{return false;}
  location.reload();return true;
}
/** Once, after the reload landed on the bundle it was for: "Updated to the latest version". */
export function justUpdated(){
  try{if(!OWN_BUILD||sessionStorage.getItem(KEY)!==OWN_BUILD)return false;sessionStorage.setItem(KEY,OWN_BUILD+' said');return true;}
  catch{return false;}
}

/** The bundle the server serves now, as the latest pulse said (app.ts noticeNewVersion). */
export let serverBuild='';
export const noticeServerBuild=(build:string)=>{serverBuild=build;};
/** app.ts: whether anything unsaved is open (the dirty-close guard) and how to offer the update (the update bar). */
let hooks={unsaved:()=>false,ask:()=>{}};
export const staleHooks=(h:typeof hooks)=>{hooks=h;};
/** The one step after any failure: a page that didn't load, an error message or notice, an uncaught error. A newer
 *  bundle is known: reload onto it once, or with something unsaved open, show the update bar instead ('ask').
 *  'none' also when the reload could not be guarded (no storage). */
export function updateAfter(failed:boolean):'reload'|'ask'|'none'{
  const c=staleChoice({own:OWN_BUILD,seen:serverBuild,failed,dirty:hooks.unsaved(),reloadedFor:reloadedFor()});
  if(c==='ask')hooks.ask();
  return c==='reload'&&!reloadFor(serverBuild)?'none':c;
}
/** An error message or notice, or an uncaught error: only a bug or a server fault (unexpectedError) is the old
 *  bundle's, never a lost connection or a refusal the person can act on. */
export const staleFault=(e:unknown)=>updateAfter(unexpectedError(e)&&!(e instanceof ApiError&&e.status===0));
if(typeof window!=='undefined'){
  window.addEventListener('error',e=>void staleFault(e.error));
  window.addEventListener('unhandledrejection',e=>void staleFault(e.reason));
}
