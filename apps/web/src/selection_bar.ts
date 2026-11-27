import {el,icon,menu,menuSource,opens,armed,reportError,type IconName,type MenuItem} from './dom.js';
import {observeSize} from './loading.js';

/** One action on the selected records. `primary` is solid accent (one per bar), `more` goes into the ⋯ menu, `danger`
 *  comes last, red, with its own icon
 *  (a bin to remove, power to switch off). */
export interface SelectionAction{label:string;icon:IconName;run:()=>unknown;tip?:string;primary?:boolean;more?:boolean;danger?:boolean;
  /** In place but not available (DL-007): aria-disabled, its tip says why, and pressing it runs `run` (which says why). */disabled?:boolean;
  /** Same-button confirm (press twice), only for an action that cannot be undone (D42). */armed?:boolean}
export interface SelectionState{count:number;noun:[string,string];
  /** The line under "selected" (e.g. "lines in Duties per week", "issues · On leave or away"); the noun when not given. */
  detail?:string;actions:SelectionAction[];clear:()=>void;
  /** The selection the actions are for (e.g. its ids): drawn again for the same one, an armed action stays armed. */key?:string}

const run=(a:SelectionAction)=>{try{const r=a.run();if(r instanceof Promise)r.catch(reportError);}catch(e){reportError(e);}};
const live=new Set<{root:HTMLElement;clear:()=>void;place:()=>void}>();
let bars=0;
if(typeof window!=='undefined')window.addEventListener('resize',()=>{for(const b of live)b.place();});
// Esc clears the selection of the list you are in (not while a dialog, menu or field has the key).
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('keydown',e=>{
  if(e.key!=='Escape'||e.defaultPrevented||document.querySelector('dialog[open],.popover'))return;
  const t=e.target as HTMLElement;if(t.closest?.('input:not([type=checkbox]),textarea,select,[contenteditable=true]'))return;
  for(const b of live){if(!b.root.isConnected){live.delete(b);continue;}
    const host=b.root.parentElement;if(!b.root.hidden&&(t===document.body||host?.contains(t))){e.preventDefault();b.clear();return;}}
});

/**
 * The one selection bar for every collection: a dark bar floating over the bottom of its list (centred on it, never
 * wider than it), one row: a count badge, "selected" over a breakdown, the actions (icon + label), ⋯ More, the
 * destructive one last and "Esc ×". Put `root` inside the list's container; call `show` on every selection change.
 */
export function selectionBar(o:{label?:string;id?:string}={}){
  // Its own mark: more than one bar can be on the page at once (a list's and the Bulk editor's), and a kept ⋯ More
  // (D-057) finds this bar's button across redraws by it.
  const mark=String(++bars),keep=`.selection-bar[data-bar="${mark}"] .sel-more`;
  const root=el('div',{class:'selection-bar',role:'toolbar','aria-label':o.label||'Bulk actions','data-bar':mark,...(o.id?{id:o.id}:{})});root.hidden=true;
  // ⋯ More's items: the bar's extra actions as drawn now; an open More follows a redraw to the new button (dom.ts keep).
  let extra:SelectionAction[]=[];
  const moreItems=():MenuItem[]=>extra.map(a=>({label:a.label,icon:a.icon,run:a.run,armed:a.armed}));
  menuSource(root,moreItems);
  for(const b of live)if(!b.root.isConnected)live.delete(b);
  const entry={root,clear:()=>{},place:()=>place()};
  // Centred on its list: the bar is fixed above the footer, so its place follows the list's box.
  const place=()=>{const host=root.parentElement;if(!host||root.hidden)return;const r=host.getBoundingClientRect();
    root.style.setProperty('--sel-left',`${r.left+r.width/2}px`);root.style.setProperty('--sel-max',`${Math.max(0,r.width-16)}px`);fit();};
  // Never wider than its list and never scrolled sideways: when the actions do not fit (a phone), the secondary ones
  // go into ⋯ More, last first, and then "selected" gives way to the count alone.
  let state:SelectionState|null=null,folded=-1,drawnKey:string|undefined;
  const fit=()=>{if(!state||root.hidden||!root.isConnected)return;
    const over=()=>root.scrollWidth>root.clientWidth+1,foldable=state.actions.filter(a=>!a.more&&!a.danger&&!a.primary).length;
    for(let n=0;n<=foldable+1;n++){if(n!==folded)draw(state,n);if(!over())return;}};
  // The list's size changes with the window, the sidebar and docked panels: one observer follows it.
  let unwatch=()=>{},watched:Element|null=null;
  function show(s:SelectionState|null){
    root.hidden=!s||!s.count;state=s&&s.count?s:null;folded=-1;
    if(!s||!s.count){extra=[];drawnKey=undefined;root.replaceChildren();return;}
    entry.clear=s.clear;if(root.isConnected)live.add(entry);
    draw(s,0);
    const host=root.parentElement;if(host&&host!==watched){unwatch();unwatch=observeSize(host,()=>place());watched=host;}
    place();
  }
  function draw(s:SelectionState,fold:number){
    folded=fold;
    // An armed action drawn again for the same selection (a check answering between the two presses) stays armed: its
    // second press acts, as in a redrawn menu (dom.ts menu keep). A press elsewhere or leaving it has disarmed it already.
    // Read before the redraw: Chromium blurs a focused button as it is removed, and the blur disarms it.
    const was=s.key!==undefined&&s.key===drawnKey?root.querySelector<HTMLElement>('.sel-act.armed'):null,focused=!!was&&was===document.activeElement,from=was?.dataset.armedFrom;
    drawnKey=s.key;
    const word=s.count===1?s.noun[0]:s.noun[1];
    const act=(a:SelectionAction)=>{
      const b=el('button',{type:'button',class:'sel-act'+(a.primary?' primary':'')+(a.danger?' danger':''),'aria-label':a.label,'data-tip':a.tip||a.label,...(a.disabled?{'aria-disabled':'true'}:{})},icon(a.icon),el('span',{class:'label'},a.label));
      b.addEventListener('click',()=>run(a));return a.armed?armed(b):b;};
    const shown=s.actions.filter(a=>!a.more&&!a.danger),secondary=shown.filter(a=>!a.primary),gone=new Set(secondary.slice(Math.max(0,secondary.length-fold)));
    const inline=shown.filter(a=>!gone.has(a)),danger=s.actions.filter(a=>a.danger);
    extra=[...shown.filter(a=>gone.has(a)),...s.actions.filter(a=>a.more&&!a.danger)];
    root.classList.toggle('compact',fold>secondary.length);
    const more=extra.length?opens(el('button',{type:'button',class:'sel-act sel-more','aria-label':'More actions','data-tip':'More actions'},icon('dots')),'menu'):null;
    more?.addEventListener('click',()=>menu(more,'More actions',moreItems(),{placement:'above-start',keep}));
    const clear=el('button',{type:'button',class:'sel-clear','aria-label':'Clear selection','data-tip':'Clear selection (Esc)'},el('kbd',{'aria-hidden':'true'},'Esc'),icon('close'));
    clear.addEventListener('click',()=>{s.clear();});
    root.replaceChildren(
      el('span',{class:'sel-head'},el('span',{class:'sel-badge','aria-hidden':'true'},String(s.count)),
        el('span',{class:'sel-words','aria-hidden':'true'},el('strong',{},'selected'),el('small',{class:'truncate'},s.detail||word)),
        el('span',{class:'sr-only'},`${s.count} ${word} selected${s.detail?', '+s.detail:''}`)),
      el('span',{class:'sel-sep','aria-hidden':'true'}),...inline.map(act),...(more?[more]:[]),
      ...(danger.length?[el('span',{class:'sel-sep','aria-hidden':'true'}),...danger.map(act)]:[]),
      el('span',{class:'sel-sep','aria-hidden':'true'}),clear);
    const again=from&&[...root.querySelectorAll<HTMLElement>('.sel-act')].find(b=>b.getAttribute('aria-label')===from);
    if(again){again.click();if(focused)again.focus();}
  }
  return {root,show,place};
}
