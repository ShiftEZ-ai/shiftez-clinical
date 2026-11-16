import {el,icon,button,releaseToasts,type IconName} from './dom.js';
import {couldntLoad} from './dom.js';  // a failed read's words (DL-045)

export interface PaletteItem {group:string;label:string;hint?:string;icon:IconName;keywords?:string;run:()=>unknown}
export const compactSearch=(text:string)=>text.normalize('NFKD').replace(/\p{M}/gu,'').toLowerCase().replace(/[^\p{L}\p{N}]/gu,'');

/** The caller supplies permitted sources. Remote searches replace results only while this query and menu are current. */
export function openPalette(items:PaletteItem[],load?:(query:string)=>Promise<PaletteItem[]>):(next:PaletteItem[])=>void{
  const stale=document.querySelector<HTMLDialogElement>('dialog.palette');if(stale){stale.close();releaseToasts(stale);stale.remove();}
  const previous=document.activeElement as HTMLElement|null;
  const input=el('input',{type:'search','aria-label':'Search pages, people and actions',placeholder:'Search',autocomplete:'off',maxlength:'160',role:'combobox','aria-expanded':'true','aria-controls':'palette-list'});
  const list=el('ul',{class:'palette-list',id:'palette-list',role:'listbox','aria-label':'Results'});
  const status=el('div',{class:'palette-foot',role:'status','aria-live':'polite',hidden:true});
  const dialog=el('dialog',{class:'palette','aria-label':'Command menu'},
    // No key legend (N-19): the Esc chip says how to leave, on a desktop only (app.css).
    el('div',{class:'palette-search'},icon('search'),input,el('kbd',{},'Esc')),list,status);
  let active=0,shown:PaletteItem[]=[],serial=0,timer:ReturnType<typeof setTimeout>|undefined,loading=false;
  const close=()=>{serial++;clearTimeout(timer);dialog.close();releaseToasts(dialog);dialog.remove();if(previous?.isConnected)previous.focus();};
  const run=(item:PaletteItem)=>{
    close();
    // The existing dialog owner decides whether Cancel may close it. A dirty refusal must not run the action.
    for(const open of [...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].reverse()){
      if(!open.classList.contains('dialog'))return;
      open.dispatchEvent(new Event('cancel',{cancelable:true}));if(open.isConnected)return;
    }
    item.run();
  };
  const render=()=>{
    const q=input.value.trim().toLowerCase(),key=compactSearch(q);
    // Several words match each on its own, at a word's start, in any order: "synthetic 4" finds "Synthetic clinician 4".
    const split=(s:string)=>s.split(/[^\p{L}\p{N}]+/u).filter(Boolean),words=split(q);
    const every=(s:string)=>{const has=split(s);return words.length>1&&words.every(w=>has.some(x=>x.startsWith(w)));};
    const score=(item:PaletteItem)=>{
      const label=item.label.toLowerCase(),labelKey=compactSearch(label),text=[label,item.keywords||'',item.group].join(' ');
      if(!q)return 0;
      if(label===q||key&&labelKey===key)return 0;
      // A word's start counts as the start: "al" finds "Dr. Keiko Reed" as well as "Allocation order".
      if(label.startsWith(q)||key&&labelKey.startsWith(key)||label.split(/[^\p{L}\p{N}]+/u).some(w=>w&&w.startsWith(q)))return 1;
      if(every(label))return 1;
      if(label.includes(q)||key&&labelKey.includes(key))return 2;
      return text.toLowerCase().includes(q)||key&&compactSearch(text).includes(key)||every(text)?3:4;
    };
    // Groups stay together (one heading each), the group with the best match first, then in the order given.
    const scored=items.map((item,i)=>({item,i,score:score(item)})).filter(x=>x.score<4),best=new Map<string,[number,number]>();
    for(const x of scored){const b=best.get(x.item.group);if(!b||x.score<b[0])best.set(x.item.group,[x.score,b?b[1]:x.i]);}
    const rank=(x:typeof scored[number])=>best.get(x.item.group)!;
    // A search shows at most 8 of a group, so one long group (a rule's lines, many rotas) cannot hide the others.
    const per=new Map<string,number>(),fits=(g:string)=>!q||(per.set(g,(per.get(g)||0)+1).get(g)!<=8);
    shown=scored.sort((a,b)=>rank(a)[0]-rank(b)[0]||rank(a)[1]-rank(b)[1]||a.score-b.score||a.i-b.i).filter(x=>fits(x.item.group)).slice(0,40).map(x=>x.item);
    active=Math.min(active,Math.max(0,shown.length-1));list.replaceChildren();
    if(!shown.length){input.removeAttribute('aria-activedescendant');list.append(el('li',{class:'palette-empty',role:'presentation'},loading?'Searching…':'No matches. Try a page name, a person or an action.'));return;}
    let group='';
    shown.forEach((item,i)=>{
      if(item.group!==group){group=item.group;list.append(el('li',{class:'palette-group',role:'presentation'},group));}
      const row=el('li',{class:'palette-item',role:'option',id:`palette-${i}`,'aria-selected':String(i===active)},el('span',{class:'icon-chip','aria-hidden':'true'},icon(item.icon)),el('span',{class:'truncate'},item.label),item.hint?el('span',{class:'hint'},item.hint):null);
      row.addEventListener('mousemove',()=>{if(active!==i){active=i;sync();}});
      row.addEventListener('click',()=>run(item));list.append(row);
    });
    input.setAttribute('aria-activedescendant',`palette-${active}`);
  };
  const sync=()=>{if(!shown.length)return;list.querySelectorAll('.palette-item').forEach(n=>n.setAttribute('aria-selected',String(n.id===`palette-${active}`)));input.setAttribute('aria-activedescendant',`palette-${active}`);list.querySelector(`#palette-${active}`)?.scrollIntoView({block:'nearest'});};
  const search=()=>{
    if(!load)return;clearTimeout(timer);const turn=++serial,query=input.value.trim();loading=true;
    list.setAttribute('aria-busy','true');status.hidden=false;status.replaceChildren('Searching…');render();
    timer=setTimeout(async()=>{
      if(!dialog.isConnected||turn!==serial)return;
      try{const next=await load(query);if(!dialog.isConnected||turn!==serial)return;items=next;status.hidden=true;}
      catch{if(!dialog.isConnected||turn!==serial)return;status.replaceChildren(couldntLoad('some results'),' ',button('Retry search',()=>{input.focus();search();},'ghost small'));}
      finally{if(dialog.isConnected&&turn===serial){loading=false;list.removeAttribute('aria-busy');render();}}
    },150);
  };
  input.addEventListener('input',()=>{active=0;render();search();});
  input.addEventListener('keydown',e=>{
    if(e.isComposing)return;
    if(e.key==='ArrowDown'){e.preventDefault();active=Math.min(active+1,Math.max(0,shown.length-1));sync();}
    else if(e.key==='ArrowUp'){e.preventDefault();active=Math.max(active-1,0);sync();}
    else if(e.key==='Enter'&&shown[active]){e.preventDefault();run(shown[active]);}
  });
  // Search inputs otherwise consume Escape to clear the query before the dialog receives a close request.
  dialog.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.defaultPrevented&&!e.isComposing){e.preventDefault();e.stopPropagation();close();}});
  dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
  dialog.addEventListener('click',e=>{if(e.target===dialog)close();});
  document.body.append(dialog);render();dialog.showModal();input.focus();search();
  return next=>{if(dialog.isConnected){items=next;render();}};
}
