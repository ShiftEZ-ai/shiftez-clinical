import {TERMS} from './terms.js';
import {openPicker,LONG_LIST} from './picker.js';
import type {NotPerson,Catalogues} from './types.js';
import {ApiError,get,sessionChanged,takeUndo,unexpectedError,savingWords,awaitingApproval,withdrawApprovals,redoable,redoDraft,ownErrors,detailWords,type Undone} from './api.js';
import {fieldRules,dateInput,dateTimeInput,timeInput,dayOf,showDay} from './date_picker.js';
import {busy,LIVE_CLEAR_MS,observeSize,retrying,say,READ_TIMEOUT} from './loading.js';
import {tableColumnMenus} from './table_menu.js';
import {recordError,unisolated} from './feedback_context.js';
import {BULK_COLLECTIONS,CAPABILITIES,PERMISSIONS,ROLE_NAMES,ROLE_LABELS,HONORIFIC_DEFAULTS,NAME_PARTICLE_DEFAULTS} from './generated.js';
import {staleFault} from './stale_bundle.js';
import {brand,brandImage,paintWordmark} from './brand.js';
const iconPaths = {
  // Something deviates from the general rule here (an exception, an override, a rota-only change).
  fork:'M6 3v12M18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm12-12a9 9 0 0 1-9 9',
  // Switch off (a destructive action that keeps the record: power, not a bin).
  power:'M12 3v8M6.4 6.4a8 8 0 1 0 11.2 0',
  calendar:'M8 3v4m8-4v4M4 10h16M5 5h14a1 1 0 0 1 1 1v14H4V6a1 1 0 0 1 1-1Z',
  team:'M8 3v4m8-4v4M4 10h16M5 5h14a1 1 0 0 1 1 1v14H4V6a1 1 0 0 1 1-1ZM8 14h.01M12 14h.01M16 14h.01M8 17h.01M12 17h.01',
  route:'M6 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Zm12 12a3 3 0 1 0 0 6 3 3 0 0 0 0-6ZM6 9v3a3 3 0 0 0 3 3h6',
  grid:'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  clock:'M12 8v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  // Something to drag (a palette chip).
  grip:'M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01',
  // The Timeline layout: bars of different lengths on rows.
  timeline:'M4 6h9M8 12h10M6 18h8',
  // Leave and other time away (lucide "plane").
  plane:'M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2Z',
  people:'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0Zm4-3.87a4 4 0 0 1 0 7.75',
  rules:'m4 6 2 2 3-4m3 3h8M4 13l2 2 3-4m3 3h8M4 20l2 2 3-4m3 3h8',
  settings:'M4 7h16M4 17h16M8 4v6m8 4v6',
  inbox:'M4 4h16l2 13v3H2v-3L4 4Zm-2 13h6l2 3h4l2-3h6',
  upload:'M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5',
  chart:'M4 3v18h17M8 16v-5m5 5V7m5 9V4',
  scale:'M12 3v18M8 21h8M5 7h14M5 7l-3 7a3 3 0 0 0 6 0L5 7Zm14 0-3 7a3 3 0 0 0 6 0l-3-7Z',
  swap:'M4 7h16m-4-4 4 4-4 4M20 17H4m4-4-4 4 4 4',
  book:'M12 5v16M3 3h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v16h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3Z',
  shield:'M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Zm-4 9 3 3 5-6',
  history:'M3 3v6h6M3 9a9 9 0 1 1 0 6m9-8v5l3 2',
  plus:'M12 4v16M4 12h16',
  filter:'M4 6h16M7 12h10m-7 6h4',
  sort:'M8 3v18m-4-4 4 4 4-4M16 21V3m-4 4 4-4 4 4',
  chevronLeft:'m15 5-7 7 7 7',
  chevronRight:'m9 5 7 7-7 7',
  edit:'M4 20h4L19 9l-4-4L4 16v4M13.5 6.5l4 4',
  lock:'M7 10V7a5 5 0 0 1 10 0v3M5 10h14v11H5z',
  check:'m4 12 5 5L20 6',
  play:'m7 4 14 8-14 8Z',
  pause:'M8 5v14M16 5v14',
  close:'m6 6 12 12M6 18 18 6',
  search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  sun:'M12 4V2M12 22v-2M4.9 4.9 3.5 3.5M20.5 20.5l-1.4-1.4M4 12H2M22 12h-2M4.9 19.1l-1.4 1.4M20.5 3.5l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  moon:'M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z',
  monitor:'M3 5h18v11H3zM8 20h8M12 16v4',
  phone:'M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1ZM11 18h2',
  pin:'M9 4h6l-1 6 4 3v2H6v-2l4-3-1-6ZM12 15v6',
  chevronUp:'m7 15 5-5 5 5',
  chevronDown:'m6 9 6 6 6-6',
  logout:'M15 4h4v16h-4M10 8l-4 4 4 4M6 12h10',
  bell:'M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0',
  keyboard:'M3 6h18v12H3zM7 10h.01M11 10h.01M15 10h.01M7 14h10',
  help:'M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6M12 17h.01',
  star:'M12 3l2.5 5 5.5.8-4 3.9.9 5.5L12 15.6 7.1 18.2l.9-5.5-4-3.9 5.5-.8Z',
  sliders:'M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4',
  bookmark: 'M6 3h12v18l-6-4-6 4z',
  layers:'M12 3 2 8l10 5 10-5-10-5ZM2 16l10 5 10-5',
  sparkle:'M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1',
  menu:'M4 6h16M4 12h16M4 18h16',
  dots:'M5 12h.01M12 12h.01M19 12h.01',
  alert:'M12 9v4M12 17h.01M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  // The could-improve issue marker (issue_marker.ts): a circle, so its shape differs from must fix's triangle (WCAG 1.4.1).
  notice:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 8v5M12 16.5h.01',
  printer:'M6 9V3h12v6M6 18H4v-7h16v7h-2M8 14h8v7H8z',
  download:'M12 4v11m-5-5 5 5 5-5M5 20h14',
  trash:'M4 7h16M10 11v6m4-6v6M9 7V4h6v3M6 7l1 13h10l1-13',
  send:'M21 3 10 14M21 3l-7 18-4-7-7-4Z',
  directory:'M4 4h16v16H4zM8 9h8M8 13h8M8 17h5',
  leave:'M3 7h18M7 3v4m10-4v4M5 5h14v15H5zM9 13l2 2 4-4',
  eye:'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12ZM15 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  eyeOff:'M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-3.2 4M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7c1.6 0 3-.4 4.3-1M9.9 9.9a3 3 0 0 0 4.2 4.2',
  copy:'M8 8h12v12H8zM4 16V4h12',
  clipboard:'M9 3h6v4H9zM9 5H5v16h14V5h-4',
  reset:'M3 3v6h6M3 9a9 9 0 1 1 0 6',
  undo:'M9 14 4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3',
  redo:'m15 14 5-5-5-5m5 5H9a5 5 0 0 0 0 10h3',
  // Test environment (footer): a lab flask.
  flask:'M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 0 0 6 21h12a1.7 1.7 0 0 0 1.5-2.5L14 9V3M7 15h10',
  palette:'M12 21a9 9 0 1 1 9-9c0 2-1.5 3-3 3h-2a2 2 0 0 0-1 3.7A1.5 1.5 0 0 1 12 21ZM7.5 11h.01M10 7.5h.01M14.5 7.5h.01',
  // Count (conditions.ts): one count for all the values (a pill holding them), or one count for each (separate boxes).
  together:'M8.5 6h7a6 6 0 0 1 0 12h-7a6 6 0 0 1 0-12ZM8 12h.01M12 12h.01M16 12h.01',
  each:'M2.5 8h5v8h-5zM9.5 8h5v8h-5zM16.5 8h5v8h-5z',
  // Feedback: a speech bubble (send feedback), a marked spot, a camera (capture the screen), a link out (GitHub).
  message:'M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12Z',
  target:'M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M9 12h6M12 9v6',
  camera:'M3 8h4l2-3h6l2 3h4v12H3zM12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z',
  external:'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6',
} as const;
export type IconName=keyof typeof iconPaths;
/** An SVG element with its attributes and children (the small charts: Fairness, the auto-schedule run). */
export function svg<K extends keyof SVGElementTagNameMap>(tag:K,attrs:Record<string,string|number>={},...kids:Node[]):SVGElementTagNameMap[K]{
  const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,String(v));n.append(...kids);return n;
}
/** A sparkline of scores: the line and its last point, on 0–100 unless a range is given; the label says the numbers. */
export function sparkline(values:number[],label:string,lo=0,hi=100):SVGSVGElement{
  const w=120,h=32,pad=3,x=(i:number)=>values.length<2?w/2:pad+i*(w-2*pad)/(values.length-1),y=(v:number)=>h-pad-((Math.max(lo,Math.min(hi,v))-lo)/((hi-lo)||1))*(h-2*pad);
  const s=svg('svg',{viewBox:`0 0 ${w} ${h}`,class:'fx-spark',role:'img','aria-label':label,focusable:'false'});
  if(values.length>1)s.append(svg('polyline',{points:values.map((v,i)=>`${x(i)},${y(v)}`).join(' '),class:'fx-spark-line'}));
  if(values.length)s.append(svg('circle',{cx:x(values.length-1),cy:y(values[values.length-1]),r:3,class:'fx-spark-dot'}));
  return s;
}
export function icon(name: IconName): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
  for (const [key,value] of Object.entries({class:'icon',viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',focusable:'false','data-icon':name})) svg.setAttribute(key,value);
  const path = document.createElementNS(svg.namespaceURI,'path');
  path.setAttribute('d',iconPaths[name]);svg.append(path);
  return svg;
}

export type Child = Node | string | number | null | undefined | false;
/** Bidi-safe names (W3C i18n "Inline markup and bidirectional text", Unicode
 *  isolates): a name typed in another script never reorders the words, dots or numbers around it. In markup a name is
 *  a <bdi> (`bidi`); in a sentence that becomes text (a toast) it is marked with `isolate`, which el() draws as a <bdi>,
 *  so FSI/PDI never reach the page text, a search key, a sort key, an export, the clipboard or the server. Screen
 *  readers read logical order, so accessible names (aria-label) need neither. */
const FSI='\u2068',PDI='\u2069',MARKED=/\u2068([^\u2069]*)\u2069/g,DOT=' · ';
/** A name inside a display-only sentence: "Saved عيادة 1." with the name marked (render sinks only; el() and notify draw it as <bdi>). */
export const isolate=(name:string)=>FSI+name+PDI;
/** The text without isolate marks: what a sentence says when it leaves the screen (error log, report). One definition in
 *  feedback_context.ts, where recordError strips them at the root. */
export {unisolated};
/** Text as nodes: each isolate()d name, and with `dots` each " · " part of a label ("د. سنبلة · R2"), in its own <bdi>
 *  (W3C: isolate the name and the number separately). */
export function bidiRuns(text:string,dots=true):Node[]{
  const out:Node[]=[],plain=(t:string)=>{if(!dots||!t.includes(DOT)){if(t)out.push(document.createTextNode(t));return;}
    t.split(DOT).forEach((part,i)=>{if(i)out.push(document.createTextNode(DOT));if(part)out.push(el('bdi',{},part));});};
  let at=0;for(const m of text.matchAll(MARKED)){plain(text.slice(at,m.index));out.push(el('bdi',{},m[1]));at=m.index!+m[0].length;}
  plain(text.slice(at));return out;
}
/** The one element a shown name or label sits in: a <bdi>, its parts isolated when it has several. */
export const bidi=(text:string,attrs:Record<string,unknown>={})=>el('bdi',attrs,...(text.includes(DOT)||text.includes(FSI)?bidiRuns(text):[text]));
export function el<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Record<string, unknown> = {}, ...children: (Child | Child[])[]): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false || key === 'activation') continue;
    if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    else if (key === 'class') node.className = String(value);
    else if (['value', 'checked', 'disabled', 'selected', 'multiple', 'required'].includes(key)) (node as unknown as Record<string, unknown>)[key] = value;
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) {
    if (typeof child === 'string' && child.includes(FSI)) node.append(...bidiRuns(child,false));
    else node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  if(['tablist','radiogroup','menu'].includes(String(attrs.role)))compositeKeys(node,attrs.activation as Activation|undefined);
  return node;
}
/** How arrow keys treat a tablist or radiogroup built with el() (WAI-ARIA APG):
 *  - 'manual': arrows move focus only; Enter or Space activates. Default for tablists, and for any group whose
 *    activation re-renders a page or loads data (planner views, session editor tabs, density, days shown).
 *  - 'auto': arrows move focus and activate. Default for radiogroups; only for cheap, local changes.
 *  Menus only move focus. Pass it as el(tag,{role:'tablist',activation:'auto'}). */
export type Activation='manual'|'auto';
/** Shared ARIA button groups; native inputs, pickers and grids keep their own keyboard handling. */
function compositeKeys(group:HTMLElement,activation?:Activation){
  const role=group.getAttribute('role'),selector=role==='tablist'?'[role=tab]':role==='radiogroup'?'[role=radio]':'[role^=menuitem]';
  const items=()=>[...group.querySelectorAll<HTMLElement>(selector)].filter(n=>n.closest('[role=tablist],[role=radiogroup],[role=menu]')===group&&!n.matches(':disabled,[aria-disabled=true]')&&!n.closest('[hidden]'));
  const sync=(focus?:HTMLElement)=>{const list=items(),active=focus||list.find(n=>n.matches('[aria-selected=true],[aria-checked=true]'))||list[0];for(const n of list)n.tabIndex=n===active?0:-1;};
  queueMicrotask(()=>sync());
  group.addEventListener('focusin',e=>{const n=(e.target as HTMLElement).closest<HTMLElement>(selector);if(n&&items().includes(n))sync(n);});
  group.addEventListener('click',e=>{const n=(e.target as HTMLElement).closest<HTMLElement>(selector);if(n&&items().includes(n))sync(n);});
  group.addEventListener('keydown',e=>{
    if(e.defaultPrevented||e.ctrlKey||e.metaKey||e.altKey)return;
    const list=items(),i=list.indexOf(e.target as HTMLElement);if(i<0)return;
    const keys=role==='menu'||group.getAttribute('aria-orientation')==='vertical'?['ArrowUp','ArrowDown']:role==='radiogroup'?['ArrowLeft','ArrowRight','ArrowUp','ArrowDown']:['ArrowLeft','ArrowRight'];
    // Menus: a typed letter moves to the next item whose words start with it (APG Menu, type-ahead).
    if(role==='menu'&&e.key.length===1&&e.key.trim()){const k=e.key.toLowerCase(),order=[...list.slice(i+1),...list.slice(0,i+1)];
      const hit=order.find(n=>(n.textContent||'').trim().toLowerCase().startsWith(k));if(hit){e.preventDefault();sync(hit);hit.focus();}return;}
    if(!keys.includes(e.key)&&!['Home','End'].includes(e.key))return;
    e.preventDefault();e.stopPropagation();
    const index=e.key==='Home'?0:e.key==='End'?list.length-1:(i+(['ArrowLeft','ArrowUp'].includes(e.key)?-1:1)+list.length)%list.length;
    const next=list[index];sync(next);next.focus();if(role!=='menu'&&(activation||(role==='radiogroup'?'auto':'manual'))==='auto')next.click();
  });
}
/** Department name rules (display settings, set by app.ts): the titles that may lead a name, the family-name particles
 *  ("Cooper"), whether codes leave the particle out and keep codes unique. Every name format reads these; until the
 *  department's settings are read, the contract's defaults (D75). */
export const codeRules={ignoreAl:true,unique:true,titles:[...HONORIFIC_DEFAULTS] as string[],particles:[...NAME_PARTICLE_DEFAULTS] as string[]};
const esc=(s:string)=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
let ruleCache:{key:string;title:RegExp;particle:Set<string>;stems:RegExp[]}|null=null;
function rules(){
  const key=JSON.stringify([codeRules.titles,codeRules.particles]);
  if(ruleCache?.key!==key){
    const ps=codeRules.particles.filter(Boolean);
    ruleCache={key,title:new RegExp(`^(?:${codeRules.titles.filter(Boolean).map(t=>esc(t.replace(/\.$/,''))).join('|')||'(?!)'})\\.?\\s+`,'iu'),
      particle:new Set(ps.map(p=>p.toLocaleLowerCase())),
      // A particle joined to the family word: "Nico Bennett" and "Nico Bennett" (6+ letters, so Allen keeps it); a script without
      // case ("السنبلي") drops it before two or more letters.
      stems:ps.flatMap(p=>p.toLocaleLowerCase()===p.toLocaleUpperCase()?[new RegExp(`^${esc(p)}(?=\\p{L}{2,})`,'u')]
        :[new RegExp(`^${esc(p)}-?(?=\\p{Lu})`,'u'),new RegExp(`^${esc(p)}(?=\\p{Ll}{4,})`,'u')])};
  }
  return ruleCache;
}
/** A display name in its parts, by the department's rules: the leading title as written ("Dr."), the name without it,
 *  the given names and the family name with its particle ("Keiko Cooper" → given Keiko, family Cooper). The one
 *  parser every name format, initial and code uses. */
export function nameWords(displayName: string): {title: string; bare: string; given: string[]; family: string} {
  const r=rules(),full=displayName.trim(),m=r.title.exec(full),title=m?m[0].trim():'',bare=m?full.slice(m[0].length).trim():full;
  const w=bare.split(/\s+/).filter(Boolean),k=w.length>2&&r.particle.has(w[w.length-2].toLocaleLowerCase())?2:1;
  return {title,bare,given:w.slice(0,Math.max(0,w.length-k)),family:w.slice(Math.max(0,w.length-k)).join(' ')};
}
const familyStem=(name:string)=>rules().stems.reduce((n,re)=>n.replace(re,''),name);
/** The words a code is made from: no title, letters only, and the family word without its particle — joined
 *  (Nico Bennett), hyphenated (Nico Bennett) or spaced (Nico Bennett) — when the department leaves it out. */
function codeWords(displayName: string): {given: string[]; last: string; fam: string} {
  const n=nameWords(displayName),w=[...n.given,...n.family.split(' ')].filter(x=>/^\p{L}/u.test(x));
  if (codeRules.ignoreAl && w.length>2 && rules().particle.has(w[w.length-2].toLocaleLowerCase())) w.splice(w.length-2,1);
  const last = w.pop()||'';
  return {given: w, last, fam: codeRules.ignoreAl ? familyStem(last) : last};
}
/** Automatic 3-letter person code: first + middle + family initial, or first + two family letters; no title or particle. */
export function autoCode(displayName: string): string {
  const {given,last,fam} = codeWords(displayName);
  if (!fam) return '';
  // Letters of any script (an Arabic-only name gets Arabic initials); a saved code is still A–Z and 0–9.
  return (given.length>=2 ? given[0][0]+given[1][0]+fam[0] : (given[0]||last)[0]+(fam[0]||'')+(fam[1]||'')).toUpperCase().replace(/[^\p{L}\p{N}]/gu,'');
}
/** The saved-code contract's length, so an automatic code is one a person could also save. */
const CODE_MAX=BULK_COLLECTIONS.people.fields.short_code.schema.maxLength;
/** Clash candidates in order: the base, a later family letter, a digit, then the first letter and a number as long as
 *  the contract allows (about 100,000 per initial). Lazy: a crowd of look-alike names costs only what it uses. */
function* codeCandidates(base: string, fam: string) {
  const two = base.slice(0,2);
  yield base; for (const c of fam.slice(2)) yield two+c; for (const d of '23456789') yield two+d;
  for (let n = 10; 1+String(n).length <= CODE_MAX; n++) yield base[0]+n;
}
/** Codes for a whole department: saved codes win, then automatic codes, with clashes resolved by using a later
 * family letter (Pavel Cooper RAL, Clara Lewis RAS) and, failing that, a number. */
export function uniqueCodes(people: {id: string; display_name?: string; short_code?: string}[]): Map<string,string> {
  const out = new Map<string,string>(), taken = new Set<string>();
  for (const p of people) if (p.short_code) { out.set(p.id, p.short_code); taken.add(p.short_code); }
  for (const p of [...people].filter(p=>!p.short_code).sort((a,b)=>String(a.display_name).localeCompare(String(b.display_name)))) {
    const base = autoCode(p.display_name||''); if (!base) continue;
    // The same family word autoCode used, so a clash tries every later letter of it.
    const fam = codeWords(p.display_name||'').fam.toUpperCase().replace(/[^\p{L}\p{N}]/gu,'');
    let code = base;
    if (codeRules.unique) for (const c of codeCandidates(base, fam)) if (!taken.has(c)) { code = c; break; }
    out.set(p.id, code); taken.add(code);
  }
  return out;
}
/** Colour palette shared by duty types, services, staff roles, people and tags. */
export const PALETTE=['teal','blue','violet','amber','coral','indigo','pink','slate'] as const;
/** A record's chosen colour, or a stable automatic one from its id (so everything has a colour from day one). */
// Automatic colours skip slate so every record gets a real hue, and coral, which reads as a problem (red is for
// problems only: owner, 21 Oct 2026); both stay available to pick.
const AUTO=PALETTE.filter(c=>c!=='slate'&&c!=='coral');
export function colourOf(row:{id?:string;colour?:string}|undefined, seed=''):string{
  if(row?.colour&&(PALETTE as readonly string[]).includes(row.colour))return row.colour;
  const key=String(row?.id||seed);let h=0;for(const ch of key)h=(h*31+ch.charCodeAt(0))>>>0;return AUTO[h%AUTO.length];
}
const liveTypeColours=new Map<string,string>();
/** Duty type colours come from the current catalogue (a colour edit shows at once), else the session snapshot; '' is automatic. */
/** Weekday names by server number (0 = Monday … 6 = Sunday). */
export const WEEKDAYS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
let firstWeekday=6,departmentZone='';
/** The department's week start and time zone (from /api/me). */
export function setWeekStart(day:number,zone=departmentZone){firstWeekday=day;departmentZone=zone;}
export const departmentTimeZone=()=>departmentZone||'UTC';
/** Today in the department's time zone, as yyyy-mm-dd. */
export const departmentToday=()=>departmentZone?toLocal(new Date().toISOString(),departmentZone).slice(0,10):new Date().toISOString().slice(0,10);
/** Weekday numbers in the department's display order (Sunday first here). */
export const weekOrder=()=>Array.from({length:7},(_,i)=>(firstWeekday+i)%7);
export const weekdayOptions=()=>weekOrder().map(i=>({value:String(i),label:WEEKDAYS[i]}));
/** Chosen weekdays (server numbers) named in the department's week order, whatever order they were stored in
 *  ("Sunday, Monday" for a Sunday-first week); `short` gives "Sun, Mon". */
export const weekdayNames=(days:readonly number[],short=false)=>weekOrder().filter(d=>days.includes(d)).map(d=>short?WEEKDAYS[d].slice(0,3):WEEKDAYS[d]).join(', ');
/** The department's weekend days by server number (Monday 0 … Sunday 6), from /api/me; Friday and Saturday until then.
 *  One array, updated in place, so every calendar that shades weekends reads the same setting. */
export const WEEKEND_DAYS=[4,5];
export function setWeekend(days:number[]|undefined){if(days)WEEKEND_DAYS.splice(0,WEEKEND_DAYS.length,...days);}
/** Duty type colours, the one assignment every view and key reads (typeColour): a chosen colour stays; the others
 *  get the hues no chosen colour uses, one each in creation order, so a department's duty types are told apart (they
 *  repeat only when there are more types than free hues). Types not in the list (old snapshots) fall back to colourOf. */
type Hued={id:string;created_at?:string;colour?:string;definition?:{colour?:string}};
function assignHues(into:Map<string,string>,list:Hued[],palette:readonly string[]=AUTO){
  into.clear();
  const chosen=(t:Hued)=>{const c=t.colour??t.definition?.colour??'';return (PALETTE as readonly string[]).includes(c)?c:'';};
  const taken=new Set(list.map(chosen).filter(Boolean)),free=palette.filter(c=>!taken.has(c)),hues=free.length?free:palette;
  const byAge=[...list].sort((a,b)=>String(a.created_at||'').localeCompare(String(b.created_at||''))||a.id.localeCompare(b.id));
  for(const t of list)if(chosen(t))into.set(t.id,chosen(t));
  // Ranks (a palette of their own, which no chosen colour uses) keep their place among ALL ranks, archived and chosen
  // included, so choosing a colour for, archiving or hiding one never moves another's tint (C21).
  if(palette!==AUTO){byAge.forEach((t,i)=>{if(!chosen(t))into.set(t.id,palette[i%palette.length]);});return;}
  byAge.filter(t=>!chosen(t)).forEach((t,i)=>into.set(t.id,hues[i%hues.length]));
}
export const setTypeColours=(types:Hued[])=>assignHues(liveTypeColours,types);
export const typeColour=(id:string,snapshot=''):string=>colourOf({id,colour:liveTypeColours.has(id)?liveTypeColours.get(id):snapshot});
/** The rank palette (C21): tints of their own (`.duty-rank1…12`, app.css), clear of every hue that means something
 *  else — clinics and duty types, groups, people, areas, status and the accent (tests/test_web_rank_hues.py). */
export const RANK_PALETTE=Array.from({length:12},(_,i)=>'rank'+(i+1));
/** Hues that read as a problem; ranks never offer them as a new colour. */
export const PROBLEM_HUES:readonly string[]=['coral','pink'];
const liveRankColours=new Map<string,string>();
// shortcut: a 13th automatic rank repeats the first tint (its chip still names it); add tints when a department passes 12.
/** Rank tints reviewed, the one assignment every view and key reads: the rank's own colour in the ranks
 *  catalogue (categories) stays; the others get the rank palette by their place among all the department's ranks in
 *  creation order (archived included: pass the full catalogue, never a filtered list), so a new, reordered, archived or
 *  hidden rank never moves another's. Set when the catalogue loads, so a colour edit shows at once. */
export const setRankColours=(ranks:Hued[])=>assignHues(liveRankColours,ranks,RANK_PALETTE);
/** A rank's one tint everywhere (person pills, rank chips, Rules pills and pickers, timelines, legends). No rank
 *  (`''`) is neutral; a rank missing from the catalogue (an old record) keeps a stable rank tint. */
export const rankColour=(id:string):string=>!id?'slate':liveRankColours.get(id)
  ||RANK_PALETTE[[...id].reduce((h,ch)=>(h*31+ch.charCodeAt(0))>>>0,0)%RANK_PALETTE.length];
/** A small coloured tag chip, Notion style: the same tag always gets the same colour. */
// A tag chip names itself (its words are its meaning): exempt from the legend guard.
export const tagChip=(text:string)=>el('span',{class:`tag-chip duty-${colourOf(undefined,text.toLowerCase())}`,'data-lg-self':''},bidi(text));
/** The short tags drawn on an assignment: one map for the chip, its spoken name and the legend. */
export const MARKS={tr:['TR','Trainee'],sup:['SUP','Supervisor'],lead:['TL','Team lead'],ot:['OT','Overtime']} as const;
export type MarkKey=keyof typeof MARKS;
export const markTag=(k:MarkKey)=>el('abbr',{class:'wk-tag '+k,'data-tip':MARKS[k][1]},MARKS[k][0]);
/** One legend registry entry (docs: design/legends/LEGEND-REGISTRY-SPEC.md; `GET /api/legend`, `LegendEntry` in
 *  generated.ts). `short`: a service's pill label, a duty code, a mark's one letter. `colour`: a palette token name. */
export type LegendKind='service'|'duty'|'mark'|'day'|'status';
export type LegendItem={kind:LegendKind;key:string;label:string;short:string;colour:string;on?:boolean;order?:number};
export const LEGEND_KIND_ORDER:LegendKind[]=['service','duty','mark','day','status'];
const KIND_TITLE:Record<LegendKind,string>={service:'Services',duty:'Duty',mark:'Marks',day:'Days',status:'Status'};
// shortcut: the registry's mark defaults until LEGEND_KINDS / GET /api/legend land on this branch; then read from there.
export const MARK_DEFAULTS:LegendItem[]=[['lead','L','Team lead','indigo'],['sup','S','Supervisor','teal'],['tr','T','Trainee','violet'],
  ['ot','O','Overtime','amber'],['ext','X','External rotator','slate']].map(([key,short,label,colour],order)=>({kind:'mark',key,short,label,colour,on:true,order}));
export type MarkMode='icons'|'letters'|'off';
/** The one mark: a 9 px circle in the hue's ink with the letter in its tint (both themes pass, spec §6), or the letter as
 *  plain text. Its accessible name is the mark's label. */
export const markOf=(m:LegendItem,mode:Exclude<MarkMode,'off'>)=>el('abbr',{class:(mode==='icons'?'mi duty-':'ml duty-')+m.colour,'aria-label':m.label,title:m.label},m.short);
/** A service (or duty type) as its pill: the registry's short label on its hue. */
export const pillOf=(e:LegendItem,text=e.short||e.label)=>el('span',{class:'badge duty-'+e.colour},text);
/** A registry entry's symbol as its legend shows it: a service pill, a duty code chip, a mark, a weekend swatch. */
export function legendSymbol(e:LegendItem,mode:Exclude<MarkMode,'off'>='icons'):HTMLElement{
  if(e.key==='tree')return el('span',{class:'tree-key','aria-hidden':'true'});  // a mark with no letter: the grid's connector
  if(e.kind==='mark')return markOf(e,mode);
  if(e.kind==='service')return pillOf(e);
  if(e.kind==='duty')return el('span',{class:'d duty-'+e.colour},e.short);
  return el('span',{class:'sw '+e.key});
}
/** The grouped key for registry entries (Services · Duty · Marks · Days · Status), only the entries given (what is drawn),
 *  each kind once. Words beside a service pill only when they add something. */
export function registryKey(entries:readonly LegendItem[],mode:Exclude<MarkMode,'off'>='icons'):HTMLElement{
  const group=(k:LegendKind)=>{const list=entries.filter(e=>e.kind===k).sort((a,b)=>(a.order??0)-(b.order??0));
    return list.length?el('span',{class:'lg'},el('b',{},KIND_TITLE[k]),...list.map(e=>el('span',{class:'li'},legendSymbol(e,mode),e.kind==='service'&&e.label===e.short?'':e.label))):'';};
  return el('div',{class:'legend','aria-label':'Key'},...LEGEND_KIND_ORDER.map(group));
}
/** A name and its marks (DL-057): the name's last word (after its last space or hyphen) and the marks share one no-break
 *  span, so a mark never starts a line; the name breaks before its last word. The one helper for screen and export. */
export function nameMarks(name:string,marks:readonly Node[]):(string|HTMLElement)[]{
  if(!marks.length)return [name];
  const at=Math.max(name.lastIndexOf(' '),name.lastIndexOf('-'))+1;
  return [name.slice(0,at),el('span',{class:'nw'},name.slice(at),...marks)];
}
/** A chip whose tooltip says why (maintenance): the why is its accessible name too, after its visible word (WCAG 2.5.3 label in
 *  name), so a screen reader hears it without the tooltip ("Closed" → "Closed: every usual assignee is away · …"). */
export function reasoned<T extends HTMLElement>(chip:T,reason:string):T{
  const word=(chip.textContent||'').trim();chip.dataset.tip=reason;
  if(reason&&reason!==word)chip.setAttribute('aria-label',reason.toLowerCase().startsWith(word.toLowerCase())?reason:`${word}: ${reason}`);
  return chip;
}
/** A view's key (SSOT for every legend). Renderers add an entry for each colour, mark or code as they draw it, so the key
 *  lists only what is on screen now; it is hidden while empty. `mark`: classes for a swatch, or a drawn element.
 *  `order`: colours 0, marks 1, codes 2 (codes sort by key, so a code is easy to look up). */
type Paired={person_id:string;supervisor_person_id?:string|null};
/** Who a participant is paired with on a session (SSOT): the supervisor they name, and the trainees who name them. */
export const pairsOf=(participants:readonly Paired[],pid:string)=>({supervisor:participants.find(p=>p.person_id===pid)?.supervisor_person_id||null,
  trainees:participants.filter(p=>p.supervisor_person_id===pid).map(p=>p.person_id)});
/** "with Dr X" / "supervising A, B" ('' when unpaired), in every view, label and panel. */
export const pairWords=(participants:readonly Paired[],pid:string,nameOf:(id:string)=>string)=>{const {supervisor,trainees}=pairsOf(participants,pid);
  return [supervisor?'with '+nameOf(supervisor):'',trainees.length?'supervising '+trainees.map(nameOf).join(', '):''].filter(Boolean).join(' · ');};
/** Each key's own id, so a mark names its entry in its own key (a page may hold several keys). */
let legendSeq=0;
/** The one key component reviewed. Every mark a view draws goes through
 *  `mark(node,…)`, which stamps the drawn element with its entry (`data-lg`) and adds the entry, so a mark cannot be
 *  drawn without its key; the CI guard (tests/legend_guard.py) checks that every stamped mark on screen has its entry
 *  and every entry a mark on screen. */
/** An entry a reader can use (Fairness's people): hovering or focusing it highlights its marks (`hover`), choosing it
 *  acts (`act`, with `aria` as its name); `note`: a short figure after its words (a total). */
export type LegendUse={hover?:(on:boolean)=>void;act?:()=>void;note?:string;aria?:string};
const legendFits=new WeakMap<Element,()=>void>();
function fitLegends(scope:ParentNode){
  for(const key of scope.querySelectorAll('.view-legend'))legendFits.get(key)?.();
}
// What a key lists is chosen in its collection's one View settings (Properties, Colour by, Layers): the key has no
// settings button of its own (D-163: one View settings per collection).
export function legend(label='Legend'){
  const id='k'+(++legendSeq);
  const root=el('ul',{class:'view-legend','aria-label':label,'data-lg-key':id});root.hidden=true;
  const items=new Map<string,{mark:HTMLElement;label:string;order:number;use?:LegendUse}>();let queued=false;
  const entry=(i:{mark:HTMLElement;label:string;use?:LegendUse}):Node[]=>{
    const words=[i.mark,el('span',{class:'lg-words'},i.label),...(i.use?.note?[el('small',{class:'lg-note'},i.use.note)]:[])],u=i.use;
    if(!u?.hover&&!u?.act)return words;
    const b=el('button',{type:'button',class:'lg-act',...(u.aria?{'aria-label':u.aria}:{})},...words);
    if(u.hover)for(const [on,off] of [['mouseenter','mouseleave'],['focus','blur']] as const){b.addEventListener(on,()=>u.hover!(true));b.addEventListener(off,()=>u.hover!(false));}
    if(u.act)b.addEventListener('click',u.act);
    return [b];};
  const paint=()=>{queued=false;root.hidden=!items.size;
    root.replaceChildren(...[...items.entries()].sort(([ka,a],[kb,b])=>a.order-b.order||(a.order>1?ka.localeCompare(kb):0)).map(([k,i])=>el('li',{'data-lg-entry':`${id}:${k}`,'data-lg-k':k},...entry(i))));fit();};
  // One line below its view reviewed: the entries that don't fit, the last first, go behind the one "+N",
  // which lists every entry. The first entry always stays; print shows them all (app.css).
  let width=-1;
  const fit=()=>{
    width=root.hidden||!root.isConnected?0:root.clientWidth;
    if(!width)return;
    root.querySelector(':scope>.lg-more')?.remove();
    const lis=[...root.querySelectorAll<HTMLElement>(':scope>li[data-lg-entry]')];for(const l of lis)l.hidden=false;
    const list=()=>lis.map(l=>{const i=items.get(l.dataset.lgK||'');return i?el('span',{class:'lg-pop'},...entry({...i,mark:i.mark.cloneNode(true) as HTMLElement})):'';});
    for(let i=lis.length-1,n=1;i>0&&root.scrollWidth>root.clientWidth+1;i--,n++){lis[i].hidden=true;root.querySelector(':scope>.lg-more')?.remove();
      const more=overflowMore(n,['legend item','legend items'],{all:items.size,title:label,list});
      more.replaceChildren(el('span',{class:'lg-more-mark','aria-hidden':'true'},more.textContent));
      root.append(el('li',{class:'lg-more'},more));}};
  legendFits.set(root,fit);
  observeSize(root,()=>{if(root.clientWidth!==width)fit();});
  const add=(key:string,mark:string|HTMLElement,text:string,order=1,use?:LegendUse)=>{
    if(items.has(key))return;
    items.set(key,{mark:typeof mark==='string'?el('span',{class:'lg-swatch '+mark,'aria-hidden':'true'}):mark,label:text,order,use});
    if(!queued){queued=true;queueMicrotask(paint);}
  };
  /** Draws `node` as entry `key` (its swatch `mark`, its words `text`): stamps it and adds the entry. Returns `node`. */
  // One element may stand for several entries (a bar: its area's colour, worked in, not published yet): space-separated.
  // What each node was stamped as: `follow` rebuilds the key from the nodes still drawn.
  const stamps=new WeakMap<Element,[string,string|HTMLElement,string,number,LegendUse|undefined][]>();
  const mark=<T extends Element>(node:T,key:string,swatch:string|HTMLElement,text:string,order=1,use?:LegendUse):T=>{
    const was=(node.getAttribute('data-lg')||'').split(' ').filter(Boolean),me=`${id}:${key}`;
    if(!was.includes(me)){node.setAttribute('data-lg',[...was,me].join(' '));stamps.set(node,[...(stamps.get(node)||[]),[key,swatch,text,order,use]]);}
    add(key,swatch,text,order,use);return node;};
  // No general `add`: an entry exists only through the mark it explains (`mark`), so a key never lists what is not drawn.
  return {root,mark,
    /** A virtualised view (the timeline) only: an entry for a row laid out but not yet drawn; that row's marks stamp the
     *  same entry as they scroll in, which the guard checks by scrolling through. */
    laidOut:add,
    /** Draws `node` as a registry entry (dom.ts LegendItem): its symbol and words come from the registry, grouped by kind. */
    entry<T extends Element>(node:T,e:LegendItem):T{return mark(node,e.kind+':'+e.key,legendSymbol(e),e.label,LEGEND_KIND_ORDER.indexOf(e.kind)+(e.order??0)/100);},
    /** Forget every entry (a view that redraws part of itself in place). */
    clear(){items.clear();paint();},
    /** Views that redraw, page, group, append, search or collapse on their own (a table, the Fix list): the key follows
     *  what they show now, rebuilt from their stamped nodes after each change, including one hidden or shown in place
     *  (`shown`: which count; by default none inside [hidden]). Returns the key's root. */
    follow(views:readonly Element[],shown=(n:Element)=>!n.closest('[hidden]')){
      const sync=()=>{items.clear();for(const v of views)for(const n of v.querySelectorAll('[data-lg]'))if(shown(n))for(const a of stamps.get(n)||[])add(...a);paint();};
      const o=new MutationObserver(sync);for(const v of views)o.observe(v,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']});sync();return root;},
    /** A short tag (TL, OT…) for a chip, entered in the key as it is drawn. */
    tag(k:MarkKey){return mark(markTag(k),'tag:'+k,markTag(k),MARKS[k][1]);}};
}
export type Legend=ReturnType<typeof legend>;
/** Views that others decorate (the rule map's exception marks, planner_rules.ts) say which key they draw into: a mark
 *  added later inside the view is drawn as an entry of that key, never of a second one. */
const hosts=new WeakMap<Element,Legend>();
export const hostLegend=<T extends Element>(root:T,key:Legend):T=>{hosts.set(root,key);return root;};
/** The key of the view `node` is drawn in (its nearest hosting ancestor), or null outside any keyed view. */
export const legendAt=(node:Element):Legend|null=>{for(let n:Element|null=node;n;n=n.parentElement){const k=hosts.get(n);if(k)return k;}return null;};
export const text = (value: unknown) => value == null ? '—' : String(value);
/** One collator for every list's sort and value order (numbers in text compare as numbers: "Room 2" before "Room 10"). */
export const collator = new Intl.Collator(undefined, {numeric: true});
/** Choices by name: the order of every list of people, the same as a collection's sort by Name. */
export const byLabel = (a: {label: string}, b: {label: string}) => collator.compare(a.label, b.label);
/** A record's name (a place, service, rank, rule…). A person is not a record: their row's type is refused here, so
 *  names go through person_label.ts (`personName`, `fullNameOf`, `nameText`); tests/test_name_guard.py covers the paths
 *  the compiler cannot see. */
export const labelOf = (row: ({name?: string; display_name?: string} & NotPerson) | undefined) => row?.name || row?.display_name || 'Unavailable record';
/** A noun's English plural ("consultant" → "consultants", "match" → "matches", "entry" → "entries"): the default of
 *  `plural`, and a plain name's plural (a rank). Irregular words pass their own ("person", "people"). */
export const pluralWord=(w:string)=>/(s|x|z|ch|sh)$/.test(w)?w+'es':/[^aeiou]y$/.test(w)?w.slice(0,-1)+'ies':w+'s';
/** "1 duty", "2 duties": the one place counts are worded. */
export const plural=(n:number,one:string,many=pluralWord(one))=>`${n} ${n===1?one:many}`;
/** Names as one phrase: "A", "A and B", "A, B and C", past three "A, B, C and 2 others". */
export const namesWords=(names:string[])=>new Intl.ListFormat('en-GB',{type:'conjunction'}).format(names.length>3?[...names.slice(0,3),plural(names.length-3,'other')]:names);
/** A duration as people read it, one format everywhere (run times, search time used and left, first schedule, an age
 *  on System › Operations, an AI call): "< 1 s", "45 s", "2 min 4 s", "55 min", "1 h 5 min", "3 days". Seconds only
 *  while they matter (under 10 minutes); days from two days on. */
export function duration(seconds:number):string{
  const s=Math.max(0,seconds);if(s<1)return s?'< 1 s':'0 s';
  const t=Math.round(s),h=Math.floor(t/3600),m=Math.floor(t%3600/60),r=t%60;
  if(h>=48)return plural(Math.round(t/86400),'day');
  if(h)return m?`${h} h ${m} min`:`${h} h`;
  if(t<60)return `${t} s`;
  if(t<600&&r)return `${m} min ${r} s`;
  return `${m} min`;
}
/** Avatar letters: the first letter of the first two words that start with one ("Dr" left out): "RA", "CD". */
/** The one avatar (audit C19): a person's initials in a disc, beside their name (so hidden from screen readers).
 *  `cls`: its variant (app.css .avatar.duty, .muted, .me). tests/test_name_guard.py keeps it the only one. */
export const avatar=(name:string,cls='')=>el('span',{class:'avatar'+(cls?' '+cls:''),'aria-hidden':'true'},initials(name));
export const initials=(value:string)=>{const n=nameWords(value);return [...n.given,n.family].filter(w=>/^\p{L}/u.test(w)).map(w=>w[0]).slice(0,2).join('').toUpperCase()||'?';};
/** The verb that agrees with a count: "1 place needs", "2 places need" (FD-07). */
export const agree=(n:number,one='needs',many='need')=>n===1?one:many;
/** A small labelled pill, the only place one is built (tests/test_web_ssot_guards.py). `tone` comes from the one status
 *  palette (Tone) or a duty hue; a record's state goes through statusBadge. `cls` adds a layout class. Options: `icon`
 *  before the words, `tip` (the shared tooltip), `as:'button'` for a badge that opens something (pass `onclick`), and
 *  any other attribute (`data-*`, `aria-*`, `id`). */
export type BadgeTone=Tone|''|`duty-${string}`;
export type BadgeOptions={icon?:IconName|null;tip?:string;as?:'button'}&Record<string,unknown>;
export const badge=(value:string,tone:BadgeTone='',cls='',{icon:glyph,tip,as,...attrs}:BadgeOptions={})=>
  el(as||'span',{...(as?{type:'button'}:{}),...attrs,class:['badge',tone,cls].filter(Boolean).join(' '),'data-tip':tip||undefined},glyph?icon(glyph):null,value.replaceAll('_',' '));
export const selfApprovedBadge=()=>badge('Self-approved','warn','audit-self');
/** The Needs-you row's words (UIUX-AUDIT C9; DESIGN › Fix list: one shared row shape). */
export function needText(title:Child|Child[],line?:Child|Child[],o:{id?:string;tip?:string;cls?:string}={}):HTMLElement{
  return el('span',{class:'home-need-text',...(o.id?{id:o.id}:{})},el('strong',{},...[title].flat()),
    line!==undefined&&line!==''&&line!==null?el('small',{...(o.cls?{class:o.cls}:{}),...(o.tip?{'data-ctip':o.tip}:{})},...[line].flat()):'');
}
/** THE Needs-you row (UIUX-AUDIT C9, wave 2): its lead (the tone count pill, or a cause's level mark), its words
 *  (needText), then what ends it (its action, a state), in that order by construction (DESIGN › Home: count, title,
 *  line, action). One container for Home, the Fix list, its review items, the drafts merge and a run's changes; `tag`
 *  and `cls` keep a surface's own list item and layout (Home's li). */
export function needRow(lead:Child,text:HTMLElement,end:Child|Child[]='',o:{tag?:'div'|'li';cls?:string;attrs?:Record<string,unknown>}={}):HTMLElement{
  return el(o.tag||'div',{class:o.cls??'fix-row',...o.attrs},lead,text,...[end].flat());
}
/** The Needs-you row's count pill (999+ past a thousand); content may be an icon. */
export function needCount(content:number|Child,tone:BadgeTone='neutral',o:{cls?:string;hidden?:boolean}={}):HTMLElement{
  return el('span',{class:['badge',tone,'home-count',o.cls].filter(Boolean).join(' '),...(o.hidden?{'aria-hidden':'true'}:{})},
    typeof content==='number'?(content>999?'999+':String(content)):content);
}
/** One status palette (SSOT) for every badge, pill and dot that shows a record's state:
 *  waiting for someone gold · done or in force green · a problem red · running blue · a draft teal · ended grey. */
export type Tone='ok'|'warn'|'err'|'info'|'draft'|'neutral';
const STATUS_TONES:Record<string,Tone>={
  submitted:'warn',proposed:'warn',pending:'warn',waiting:'warn',reply:'warn',queued:'warn',reviewing:'warn',offered:'warn',counteroffered:'warn',accepted:'warn',invited:'warn',attention:'warn',partly_applied:'warn',
  // A rota whose review is out of date (the server's stale_inputs): waiting for its coordinator to resubmit.
  outdated:'warn',preview:'info',
  approved:'ok',published:'ok',active:'ok',succeeded:'ok',applied:'ok',covered:'ok',current:'ok',met:'ok',
  rejected:'err',failed:'err',gap:'err',error:'err',declined:'err',
  running:'info',open:'info',draft:'draft',
  // Feedback reports (feedback.ts): new waits for triage; being worked on is running; fixed is done.
  new:'warn',triaged:'info',in_progress:'info',fixed:'ok',wont_fix:'neutral',duplicate:'neutral',
  superseded:'neutral',cancelled:'neutral',retired:'neutral',withdrawn:'neutral',expired:'neutral',inactive:'neutral',archived:'neutral',undone:'neutral'};
export const statusTone=(state:string):Tone=>STATUS_TONES[state]||'neutral';
/** One wording (SSOT) for a record waiting on someone else's decision: a submitted request or rota, a proposed rule or
 *  exception and a swap in review all read "Waiting for approval". Other states keep their own word. */
const STATUS_LABELS:Record<string,string>={submitted:'Waiting for approval',proposed:'Waiting for approval',reviewing:'Waiting for approval',outdated:'Needs resubmitting'};
export const statusLabel=(state:string)=>STATUS_LABELS[state]||state.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
/** one verdict per rota. A detail read (compact, window or whole) carries the server's review_progress.stale_inputs
 *  and decides; only a rota list row with no detail read falls back to the list's own `outdated`. */
export const rotaOutdated=(w:Record<string,unknown>,detail?:{review_progress?:{stale_inputs?:boolean}|null}|null)=>
  ['submitted','approved'].includes(String(w.status))&&(detail?.review_progress?!!detail.review_progress.stale_inputs:!!w.outdated);
/** Labelled facts (a definition list): "Rules · 27 → 9". */
export const ruleFacts=(facts:[string,Child][])=>el('dl',{class:'rule-facts'},...facts.map(([label,value])=>el('div',{},el('dt',{},label),el('dd',{},value))));
/** A badge for a record's state, coloured from the one status palette; `label` overrides the wording. */
export const statusBadge=(state:string,label=statusLabel(state))=>badge(label,statusTone(state));
/** Facts in words on one line ("Independent approval · 0 of 1 approvals · due Thu 5 Nov"): each fact is its own item and
 *  the separator is drawn by CSS before it, clipped at the start of a line, so a wrap never leaves a dangling "·". */
export const factLine=(facts:Child[],cls='')=>el('span',{class:['fact-line',cls].filter(Boolean).join(' ')},
  // The spaces between items are not drawn (flex) but keep the words apart for a screen reader and textContent.
  el('span',{class:'fact-items'},...facts.filter(f=>f!==''&&f!=null&&f!==false).flatMap((f,i)=>[...(i?[' ']:[]),el('span',{class:'fact'},f)])));
/** "Report this problem" on an error toast or error notice given the error itself when that error is unexpected (a 5xx,
 *  a lost connection, a bug: unexpectedError in api.ts), while someone is signed in (feedback.ts sets it): it opens Send
 *  feedback as a Problem with that error attached, the page captured as it is. Errors the person can fix (validation,
 *  refusals) and messages with no error do not offer it. */
export const REPORT_PROBLEM='Report this problem';
let problemReporter:{available:()=>boolean;report:(message:string)=>void}|null=null;
export const setProblemReporter=(r:typeof problemReporter)=>{problemReporter=r;};
const reporter=(error:unknown)=>problemReporter?.available()&&unexpectedError(error)?problemReporter:null;
/** A notice's tone: its own four variants (app.css .notice.*), distinct from the badge Tone ('warn' is not a notice). */
export type NoticeTone='info'|'warning'|'error'|'success';
/** Contextual help: a plain explanation behind an ⓘ beside what it explains, read on hover,
 *  focus or press (the shared tooltip, `data-help`). Never for what changes what happens: that stays on screen. */
export const helpButton = (label: string, text: string, cls = '') => el('button', {type: 'button', class: `icon-button bare ${cls}`.trim(), 'aria-label': label, 'data-help': '', 'data-tip': text}, icon('help'));
export const note = (message: string, tone: NoticeTone = 'info', error?: unknown) => {
  // An error's message sits in its own span, so the message is still found on its own beside Report this problem.
  const n=el('div', {class: `notice ${tone}`, role: tone === 'error' ? 'alert' : 'note'}, tone==='error'?el('span',{},message):message);
  if(tone==='error'){recordError(message,'notice');staleFault(error);const r=reporter(error);
    if(r){const b=el('button',{type:'button',class:'link-button notice-report'},REPORT_PROBLEM);b.addEventListener('click',()=>r.report(message));n.append(' ',b);}}
  return n;
};
/** Whether the department's lists (the people catalogue among them) are held: only a /api/catalogues reply carries
 *  `_versions`. Staff hold none: their `{people:[]}` is a placeholder, never "no people" (maintenance). */
export const holdsLists=(c:Catalogues)=>'_versions' in c;
/** The words for a read the server refused this role (403): information, never an empty state or an error. */
export const ROLE_REFUSED='Not available for your role.';
export const refused=(e:unknown)=>e instanceof ApiError&&e.status===403;
/** An error's own words when they are a sentence for people (the server's problem detail, our own connection words),
 *  else '': a bare status phrase ("Service unavailable"), a code or one word says nothing the notice does not
 *  (DESIGN.md › Interaction contract). */
const STATUS_PHRASE=/^(bad request|unauthori[sz]ed|forbidden|not found|method not allowed|request time-?out|conflict|gone|too many requests|(internal )?server error|not implemented|bad gateway|service (temporarily )?unavailable|gateway time-?out)\.?$/i;
export const errorWords=(e:unknown)=>{const m=e instanceof Error?e.message.trim():'';return /\s/.test(m)&&!STATUS_PHRASE.test(m)?m:'';};
/** DL-045: the words for a read that failed, "Couldn’t load <name>." and the error's own words (errorWords): `name` is
 *  the area's own name ("the score review", "rotas"), never "data" or "this section". The only place these words are
 *  written (tests/test_web_read_errors.py); a toast for a failed read says them too. */
export const couldntLoad=(name:string,e?:unknown)=>{const said=errorWords(e);return `Couldn’t load ${name}.${said?' '+said:''}`;};
/** The one note for a read that failed (maintenance): a refusal says ROLE_REFUSED (info, no Retry); a read that timed out
 *  (api.ts READ_TIMEOUT) says only "Didn’t load in 1:00." in the warning tone; any other failure says couldntLoad(name,
 *  e) in the error tone, then `after` (what that leaves, "The list is grouped by family."). Both with Retry inside the
 *  message, said as they happen. `what`: the area's name for its Retry when several can show at once ("Retry loading
 *  rotas"). Never empty(): "none yet" is only what an answered read says. */
export const readFailed=(name:string,e:unknown,retry?:()=>void,what?:string,after?:string)=>
  failedNote(couldntLoad(name,e)+(after?' '+after:''),e,retry,what);
/** The same note for a check that failed rather than a read ("Couldn’t check who can take this duty."): `failed` is the
 *  whole sentence in the same voice; a refusal and a timeout read as for readFailed. */
export const failedNote=(failed:string,e:unknown,retry?:()=>void,what?:string)=>{
  if(refused(e))return note(ROLE_REFUSED,'info');
  const late=e instanceof ApiError&&e.code==='READ_TIMEOUT',message=late?(e as Error).message:failed;
  const n=note(message,late?'warning':'error',e);n.classList.add('read-failed');
  if(late)say(message);  // the error tone is an alert already
  if(retry)n.firstChild!.after(' ',button('Retry',()=>{const host=n.parentElement;retrying(retry)();if(host)focusWhenLoaded(host,n);},'secondary small',what?{'aria-label':`Retry loading ${what}`}:{}));
  return n;
};
/** DL-045: after a Retry, focus goes to the area once it has loaded (its skeleton gone), or to the next Retry if it
 *  failed again; never left on a button that was removed. */
function focusWhenLoaded(host:HTMLElement,failed:HTMLElement){
  const watch=new MutationObserver(()=>{
    if(!host.isConnected){stop();return;}
    if(failed.isConnected||host.querySelector('.loading[aria-busy="true"]'))return;
    stop();const at=document.activeElement;if(at&&at!==document.body&&at.isConnected)return;  // placed already by the area
    const again=host.querySelector<HTMLElement>('.read-failed button');
    if(again)again.focus();else{if(!host.hasAttribute('tabindex'))host.tabIndex=-1;focusQuietly(host);}});
  const end=setTimeout(()=>stop(),READ_TIMEOUT*2),stop=()=>{watch.disconnect();clearTimeout(end);};
  watch.observe(host,{childList:true,subtree:true,attributes:true,attributeFilter:['aria-busy']});
}
// The title is an h2: empty states sit directly under the page's h1 (or a dialog's h2 title, which is harmless).
/** The one phone breakpoint, the width of app.css `@media (max-width:720px)`. Scripts read this,
 *  never their own query. */
// Read on first use, not at import: the client's modules also load in Node (tests/test_client_projection.py), with no window.
let phoneList:MediaQueryList|null=null;
const phoneMedia=()=>phoneList||=typeof matchMedia==='function'?matchMedia('(max-width: 720px)'):null;
export const phoneQuery={get matches(){return !!phoneMedia()?.matches;},addEventListener:(type:'change',listener:()=>void)=>phoneMedia()?.addEventListener(type,listener),
  removeEventListener:(type:'change',listener:()=>void)=>phoneMedia()?.removeEventListener(type,listener)};
export const empty = (title: string, description: string, action?: Node|null, glyph: IconName = 'plus') => el('div', {class:'empty-state'}, el('span',{class:'empty-icon','aria-hidden':'true'},icon(glyph)), el('div',{class:'empty-text'},el('h2',{},title),description?el('p',{},description):null),action);
/** "Best on a desktop" for a page made for a computer, on a phone: one line naming the page, then
 *  the page's way on (Back to Home, Continue anyway) when it has one. The only builder of this notice. */
export const desktopNotice=(page:string,actions?:Node|null)=>el('section',{class:'desktop-only','aria-labelledby':'desktop-only-title'},
  el('span',{class:'desktop-only-icon','aria-hidden':'true'},icon('monitor')),el('h2',{id:'desktop-only-title'},'Best on a desktop'),
  el('p',{},`${page} is built for a larger screen.`),actions);
/** The compact empty() for a card, panel, popover or list: one short sentence and at most one next action,
 *  such as Add, the setup step from `missingSetup`, or Clear search. `cls` keeps a screen's own hook. The only
 *  muted "No ..." line; tests/test_web_empty_states.py refuses hand-written ones. */
export const emptyLine=(text:string,action?:Node|null,cls='')=>el('p',{class:'muted empty-line'+(cls?' '+cls:'')},text,...(action?[' ',action]:[]));
export const sectionHead = (title: string, description?: string, ...actions: Child[]) => el('div',{class:'section-head'},el('div',{},el('h2',{},title),description ? el('p',{class:'muted'},description) : null),el('div',{class:'actions'},...actions.map(a=>a instanceof Element?compact(a):a)));
/** The icon for an action, from its first word: every header and toolbar button gets one from this single list. */
const ACTION_ICONS:[RegExp,IconName][]=[
  [/^(add|new|create|record|give|grant|authorise|invite|propose|request)\b/i,'plus'],[/^(submit|send|post|publish)\b/i,'send'],
  [/^(approve|confirm|validate|check|mark)\b/i,'check'],[/^(reject|decline|withdraw|disable|revoke|end|cancel)\b/i,'close'],
  [/^(remove|delete|retire)\b/i,'trash'],[/^(edit|rename|change|adjust|update)\b/i,'edit'],[/^(export|download)\b/i,'download'],
  [/^(import|upload)\b/i,'upload'],[/^print\b/i,'printer'],[/^today\b/i,'calendar'],[/^filter\b/i,'filter'],[/^set\b/i,'clock'],[/^(auto|generate)/i,'sparkle'],
  [/^(swap|exchange|offer)\b/i,'swap'],[/^(copy|duplicate)\b/i,'layers'],[/^(history|audit)\b/i,'history'],[/^(open|view|review)\b/i,'search'],
];
export const actionIcon=(label:string)=>ACTION_ICONS.find(([r])=>r.test(label.trim()))?.[1];
/** A header or toolbar button that shrinks to its icon when space runs out (CSS container queries decide when).
 *  The text moves into .label; the accessible name stays the full text and a tooltip shows it only while shrunk. */
export function compact<T extends Element>(b:T,name?:IconName):T{
  if(!(b instanceof HTMLElement)||!b.classList.contains('button')||b.querySelector('.label,svg'))return b;
  const text=(b.textContent||'').trim(),chosen=name||actionIcon(text);if(!chosen||!text)return b;
  b.replaceChildren(icon(chosen),el('span',{class:'label'},text));
  if(!b.hasAttribute('aria-label'))b.setAttribute('aria-label',text);
  if(!b.dataset.tip)b.dataset.ctip=text;
  return b;
}
/** Shrink a row of compact() buttons to icons only while it would not fit: secondary ones first, then all. */
// Weakly: an entry (its stop closes over the caller's `clipped`, and so the whole view that built the row) must never
// keep a page left behind alive until the next fitRow call sweeps it (live refreshes grew by an old view each, HEAP).
const fittedRows=new Set<WeakRef<HTMLElement>>(),fitting=new WeakMap<HTMLElement,{born:number;seen?:boolean;stop:()=>void}>();
export function fitRow(row:HTMLElement,steps=['tight','tighter'],clipped=()=>row.scrollWidth>row.clientWidth+1){
  // Rows from earlier pages stop being watched: on the page once (laid out, or attached by the end of the task that
  // built them) and now gone, or never on it within half a minute. A page left before its first frame (a busy
  // browser) is never laid out, so being attached counts too.
  const now=performance.now();
  for(const ref of fittedRows){const r=ref.deref(),f=r&&fitting.get(r);
    if(!r||!f)fittedRows.delete(ref);else if(!r.isConnected&&(r.dataset.fitted||f.seen||now-f.born>30000)){f.stop();fittedRows.delete(ref);fitting.delete(r);}}
  const fit=()=>{row.classList.remove(...steps);for(const step of steps){if(!clipped())return;row.classList.add(step);}};
  // Any report means the row's container was laid out (shown), including the last one when the page removed it.
  const kids=new MutationObserver(fit);
  // A ResizeObserver reports every observed element once after its first layout: the first fit happens then, without
  // forcing a layout of the page that is still being built.
  // Relabelled or newly visible actions must refit too; ignore the fitting classes to avoid observing our own work.
  const unsize=observeSize(row.parentElement||row,()=>{row.dataset.fitted='1';if(row.isConnected)fit();});kids.observe(row,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['hidden']});
  const entry={born:now,seen:row.isConnected,stop:()=>{unsize();kids.disconnect();}};fitting.get(row)?.stop();fitting.set(row,entry);fittedRows.add(new WeakRef(row));
  if(!entry.seen)setTimeout(()=>{entry.seen=row.isConnected;},0);
}
/** The latest reading of a typed or pasted text (Paste names, Paste from Excel): `start()` begins one and outdates
 *  every earlier one at once (call it on each input, before any debounce, and when a reading begins); `current(t)`
 *  holds while reading t is the latest and the text is still the one it read, so a late reply never counts. */
export function latestReading(source:{value:string}){let turn=0,text='';
  return {start:()=>{text=source.value;return ++turn;},current:(t:number)=>t===turn&&text===source.value};}
/** Change a button's text without losing its icon. */
export function relabel(b:HTMLElement,text:string){const l=b.querySelector('.label');if(l){l.textContent=text;if(b.dataset.ctip)b.dataset.ctip=text;b.setAttribute('aria-label',text);}else b.textContent=text;}
/** A button's attributes. A disabled button always says why: `disabled` needs `why`, the type
 *  refuses one without it (tests/test_web_rules_review_shared.py). Later changes go through `disabledWhy`. */
export type ButtonAttrs=Record<string,unknown>&({disabled?:undefined;why?:undefined}|{disabled:boolean;why:string});
export function button(label: string, action: () => unknown | Promise<unknown>, kind = 'secondary', attrs: ButtonAttrs = {}) {
  const {disabled,why,...rest}=attrs as Record<string,unknown>;
  const b = el('button',{type:'button',class:`button ${kind}`,...rest},label);
  if(disabled!==undefined){b.disabled=!!disabled;because(b,String(why??''));}
  // Disabled while its async work runs (no double press); work that takes longer than 300 ms shows busy (loading.ts).
  // The action starts before the button is disabled: what it opens sees the button still focused (a panel opened from
  // inside another panel keeps its "Back to …").
  b.addEventListener('click',async () => {if(b.disabled)return;
    let r:unknown;try {r=action();} catch(e) {reportError(e);return;}
    if(!(r instanceof Promise))return;
    const hadFocus=document.activeElement===b;let restore=hadFocus;
    // Native disabling blurs the button. Return only unclaimed focus; a new field, dialog, navigation or click wins.
    const moved=(e:FocusEvent)=>{if(e.target!==b&&e.target!==document.body)restore=false;};
    const pointed=(e:PointerEvent)=>{if(!b.contains(e.target as Node))restore=false;};
    if(hadFocus){document.addEventListener('focusin',moved,true);document.addEventListener('pointerdown',pointed,true);}
    b.disabled=true;const done=busy(b);
    try {await r;} catch(e) {reportError(e);} finally {
      await done();if(hadFocus){document.removeEventListener('focusin',moved,true);document.removeEventListener('pointerdown',pointed,true);}
      if(b.isConnected){b.disabled=false;if(restore&&document.activeElement===document.body)b.focus({preventScroll:true});}}});
  return b;
}
/** Icon-only button: accessible name and hover/focus tooltip come from `label`. */
/** A text button with a leading icon (the one way buttons get icons). */
export const withIcon=(node:HTMLButtonElement,name:IconName)=>{node.prepend(icon(name));return node;};
export function iconButton(label: string, name: IconName, action: (event: MouseEvent) => unknown, attrs: Record<string, unknown> = {}) {
  const {class:extra,...rest}=attrs;
  const b = el('button',{type:'button','aria-label':label,'data-tip':label,...rest,class:`icon-button ${extra||''}`},icon(name));
  b.addEventListener('click',event=>{try{const r=action(event);if(r instanceof Promise)r.catch(e=>{reportError(e);});}catch(e){reportError(e);}});
  return b;
}
/** Decorative beside the app's name (the link or heading it sits in names it). */
export const brandEmblem=()=>brandImage('brand-mark brand-emblem',brand().emblem_url,brand().emblem_alt);
/** The product's short name in a heading drawn before the branding may have answered: it follows the branding. */
export const brandShort=()=>el('span',{'data-brand-short':''},brand().short_name);
/** The start-up screen's mark (index.html draws the same before the app runs; failures keep it): the emblem, centred. */
export const bootEmblem=()=>brandImage('boot-emblem brand-emblem',brand().emblem_url,brand().emblem_alt);
/** The app's name with an optional wordmark on sign-in, account linking and department selection. */
export const brandRow=()=>{
  const row=el('div',{class:'brand login-brand'},el('strong',{'data-brand-name':''},brand().app_name));
  paintWordmark(row);
  return row;
};
/** Production or test, as an icon (the footer, sign-in): keyed on the deployment, not ROTA_ENV (DEV runs ROTA_ENV=production
 *  for strict security but is a test deployment). `text` is its name and tooltip; test is gold, production plain.
 *  Not a Tab stop: it does nothing (maintenance, WCAG 2.4.3); its name is read with the footer. */
export const envBadge=(production:boolean,text:string)=>el('span',{class:'env-badge status-icon'+(production?' production':''),role:'img','aria-label':text,'data-tip':text},
  icon(production?'shield':'flask'),el('span',{class:'sr-only'},production?'Production':'Test environment'));
let openPopover: {node: HTMLElement; anchor: HTMLElement; close: () => void; tryClose: () => void; key?: string; place: () => void; rehome: () => boolean} | null = null;
/** The bottom edge of the usable viewport: the status footer's top (its height is --status-footer-height).
 *  Every popover, menu and picker treats it as the screen's bottom, so nothing opens under the footer. */
export const viewportBottom=()=>innerHeight-(parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--status-footer-height'))||0);
export type Placement='below'|'above-start'|'below-end'|'center'|'beside';
const tethers=new WeakMap<HTMLElement,{arrow:HTMLElement;anchor:HTMLElement;mark:string}>();
/** Anything that opens stays visibly tied to what opened it: the trigger carries `mark` (is-open: accent ring and tint;
 *  is-pointed: a ring, for hover cards) and a small arrow on the panel's edge points at it, on whichever side the panel
 *  opened (below, above, left or right). Call again after re-placing; untether() when the panel closes. */
export function tether(panel:HTMLElement,anchor:HTMLElement,mark='is-open'){
  let t=tethers.get(panel);
  if(!t){t={arrow:el('span',{class:'pop-arrow','aria-hidden':'true'}),anchor,mark};tethers.set(panel,t);}
  if(t.anchor!==anchor)t.anchor.classList.remove(t.mark);
  t.anchor=anchor;anchor.classList.add(mark);
  // The panel's laid-out box: its opening animation (a small shift and scale about the centre) is taken back out.
  const cs=getComputedStyle(panel),r=panel.getBoundingClientRect(),m=cs.transform&&cs.transform!=='none'?new DOMMatrix(cs.transform):null;
  const w=panel.offsetWidth,h=panel.offsetHeight,left=r.left+r.width/2-(m?.e||0)-w/2,top=r.top+r.height/2-(m?.f||0)-h/2,right=left+w,bottom=top+h;
  const a=anchor.getBoundingClientRect(),arrow=t.arrow;
  const side=top>=a.bottom-2?'top':bottom<=a.top+2?'bottom':left>=a.right-2?'left':right<=a.left+2?'right':'';
  if(!side){arrow.remove();return;}
  const clamp=(v:number,lo:number,hi:number)=>Math.max(lo,Math.min(hi,v));
  const across=side==='top'||side==='bottom';
  arrow.className='pop-arrow '+side;
  arrow.style.setProperty('left',(across?clamp(a.left+a.width/2,left+14,right-14):side==='left'?left:right)+'px');
  arrow.style.setProperty('top',(across?(side==='top'?top:bottom):clamp(a.top+a.height/2,top+14,bottom-14))+'px');
  arrow.style.setProperty('z-index',String((parseInt(cs.zIndex)||2200)+1));
  if(arrow.previousElementSibling!==panel)panel.after(arrow);
  // A panel in the top layer (a native popover) needs its arrow there too, shown after it so it paints on top.
  if(panel.matches(':popover-open')&&!arrow.matches(':popover-open')){arrow.setAttribute('popover','manual');(arrow as HTMLElement&{showPopover:()=>void}).showPopover();}
}
export function untether(panel:HTMLElement){const t=tethers.get(panel);if(!t)return;t.anchor.classList.remove(t.mark);t.arrow.remove();tethers.delete(panel);}
/** How a panel sits against what opened it (placeNear). `placement`: below (default), below-end (right edges aligned),
 *  above-start (above while it fits), center (centred under or over it: hover help) or beside (right, else left, else
 *  centred: rich hover cards). `gap` from the anchor; `width:{min}` makes it at least the anchor's width and `min`;
 *  `room:[floor,cap]` bounds its max-height; below is kept while `enough` px fit there (default: all of it);
 *  `mark` rings the anchor and draws the arrow (tether), null for neither. */
export type PlaceOptions={placement?:Placement;gap?:number;width?:{min:number};room?:[number,number];enough?:number;mark?:string|null};
/** The right edge of what is visible of `n` across: the nearest ancestor that scrolls sideways clips it at the right of
 *  its content box (its own vertical scrollbar excluded). Infinity when nothing does. */
function scrollerRight(n:Element):number{
  for(let p=n.parentElement;p&&p!==document.body;p=p.parentElement){
    if(p.scrollWidth<=p.clientWidth)continue;
    const x=getComputedStyle(p).overflowX;
    if(x==='auto'||x==='scroll'||x==='hidden'){const r=p.getBoundingClientRect();return r.left+p.clientLeft+p.clientWidth;}
  }
  return Infinity;
}
/** The one placement engine: every popover, menu, picker and hover card is placed here, so flipping, the status footer's
 *  ceiling, the viewport clamp and the arrow behave the same everywhere. The panel is placed against `anchor` now and
 *  again whenever its anchor moves or it resizes (one frame later), until `alive()` says no or dispose() is called.
 *  A phone bottom sheet (`.sheet`) is laid out by CSS: only its anchor is marked.
 *  shortcut: measured in JS (CSS anchor positioning is not Baseline yet); move to position-try-fallbacks when it is. */
export function placeNear(panel:HTMLElement,anchor:HTMLElement|(()=>HTMLElement),o:PlaceOptions={},alive:()=>boolean=()=>panel.isConnected){
  const target=typeof anchor==='function'?anchor:()=>anchor,mark=o.mark===undefined?'is-open':o.mark,gap=o.gap??6,pad=8;
  // `at`: the anchor's box the panel was last placed against; `dirty`: the panel resized since.
  let at='',dirty=false,follow=0,done=false;
  const box=(a:DOMRect)=>`${a.left},${a.top},${a.right},${a.bottom}`;
  const place=()=>{
    // The visible width: innerWidth includes a classic vertical scrollbar, which a panel must not slide under. An anchor
    // inside a horizontal scroller (a wide table) also keeps the panel within that scroller's visible right edge.
    const anchor=target(),a=anchor.getBoundingClientRect(),vw=document.documentElement.clientWidth,right=Math.min(vw,scrollerRight(anchor))-pad;
    // A button off screen (its screen is redrawing; adoptPopover hands the panel to the new one) has no position:
    // measuring it put the panel in the top-left corner. The panel stays where it is until it has a button again.
    if(!anchor.isConnected||(!a.width&&!a.height))return;
    at=box(a);dirty=false;
    if(panel.classList.contains('sheet')){if(mark)anchor.classList.add(mark);return;}
    if(o.width)panel.style.setProperty('width',Math.min(Math.max(a.width,o.width.min),vw-pad*2)+'px');
    panel.style.removeProperty('max-height');
    // The panel's layout size, not its painted box: its opening animation starts scaled down, which under-measured
    // tall panels by a few pixels and let them overlap what they open above.
    const w=panel.offsetWidth,natural=panel.offsetHeight,[floor,cap]=o.room||[120,Infinity],h=Math.min(natural,cap);
    // An anchor inside the footer (rota health) may open over it; everything else stays above the footer. Opened upwards
    // from the footer, it ends above the footer's top edge, not just above its button (whose inset in the footer follows
    // font metrics: by ~1.5px on some machines it let the panel cover the footer).
    const foot=anchor.closest('.app-footer'),edge=foot?innerHeight:viewportBottom();
    const ceiling=foot?Math.min(a.top-gap,foot.getBoundingClientRect().top):a.top-gap;
    const below=edge-pad-a.bottom-gap,above=ceiling-pad;
    let left:number,top:number,side:string;
    const besideLeft=o.placement==='beside'?a.right+gap+w<=right?a.right+gap:a.left-gap-w>=pad?a.left-gap-w:null:null;
    if(besideLeft!=null){
      // Beside its anchor, top-aligned and kept on screen; scrolls inside when taller than the room.
      const room=Math.max(1,edge-pad*2);panel.style.setProperty('max-height',room+'px');
      left=besideLeft;top=Math.max(pad,Math.min(a.top,edge-Math.min(h,room)-pad));side=besideLeft>a.left?'right':'left';
    }else{
      // Below its button, or above when there is more room there (e.g. from the status footer); never over the button:
      // a panel taller than the room scrolls inside, always capped (maintenance) so content that grows before the next frame
      // re-places it still scrolls inside the room.
      const up=o.placement==='above-start'?h<=above||above>below:below<Math.min(h,o.enough??Infinity)&&above>below;
      const room=Math.min(cap,Math.max(floor,up?above:below)),height=Math.min(h,room);
      panel.style.setProperty('max-height',room+'px');
      left=o.placement==='below-end'?a.right-w:o.placement==='center'||o.placement==='beside'?a.left+(a.width-w)/2:a.left;
      top=Math.max(pad,up?ceiling-height:a.bottom+gap);side=up?'above':'below';
    }
    left=Math.max(pad,Math.min(left,right-w));
    panel.style.setProperty('left',left+'px');panel.style.setProperty('top',top+'px');
    // Its opening animation moves away from the button, so an upward panel is never painted lower than it is placed.
    panel.dataset.side=side;
    if(mark)tether(panel,anchor,mark);
  };
  // Content added after opening (a filter row, a validation message) re-places it (observeSize: in the next frame).
  let ungrow=()=>{};
  // The button can move without either box changing size (a phone banner or a restored scroll lands after a redraw
  // handed the panel over; the page scrolls): no observer reports that, so while open each frame compares where the
  // button is now with where the panel was last placed against, and re-places on any difference. Comparing with the
  // last placement (not the last frame) catches a hand-over placed against a box the new button has already left.
  const track=()=>{if(done)return;if(!alive()){dispose();return;}
    if(dirty||box(target().getBoundingClientRect())!==at)place();follow=requestAnimationFrame(track);};
  function dispose(){if(done)return;done=true;ungrow();cancelAnimationFrame(follow);removeEventListener('resize',place);if(mark)untether(panel);}
  place();ungrow=observeSize(panel,()=>{dirty=true;});addEventListener('resize',place);follow=requestAnimationFrame(track);
  return {place,dispose};
}
let explained:Element[]=[];
/** Side panels: the item a panel explains stays highlighted (where it is drawn) while the panel is open. */
export const panelHighlight={
  /** `scroll`: bring the first one into view (a side panel's ↑ ↓ moving through a list). Rows and cards are outlined. */
  highlight(elements:Iterable<Element>,scroll=false){panelHighlight.clear();explained=[...elements];for(const n of explained)n.classList.add('is-explained');
    if(scroll)explained[0]?.scrollIntoView({block:'nearest'});},
  clear(){for(const n of explained)n.classList.remove('is-explained');explained=[];},
};
/** Anchored popover. Closes on Escape, outside click or when another opens; focus returns to the anchor.
 *  A form inside it with unsaved changes is guarded (see guard()): outside click / Escape asks first.
 *  With a `key`, a screen that redraws itself can hand the open popover to its new button (adoptPopover).
 *  With `keep` (menu()), the popover itself finds the button that replaced its own after a redraw, or closes. */
export function popover(anchor: HTMLElement, build: (panel: HTMLElement, close: () => void) => void, options: {label: string; placement?: Placement; key?: string; keep?: () => HTMLElement | null} = {label: 'Menu'}) {
  // maintenance: a panel taken off the page without close() (a shell redraw) leaves no state behind to toggle "closed".
  if (openPopover && !openPopover.node.isConnected) openPopover.close();
  if (openPopover?.anchor === anchor) { openPopover.tryClose(); return; }
  // Replacing a popover removes its contents: keep the outer opener as the new panel's visible anchor.
  if (openPopover?.node.contains(anchor)) anchor = openPopover.anchor;
  openPopover?.close();
  const panel = el('div',{class:'popover',role:'dialog','aria-label':options.label});
  const g = guard(panel, close, true);
  // preventDefault: a popover inside a modal dialog closes alone (a cancelled Escape keydown is no close request for the dialog).
  // A menu opened from inside this panel (childMenu) closes first, on its own.
  // A search box with text in it (panelSearch) takes Esc first: it clears, the panel stays.
  const onKey = (e: KeyboardEvent) => { if (e.key !== 'Escape' || (e.target as HTMLInputElement | null)?.matches?.('input[type=search]') && (e.target as HTMLInputElement).value) return; e.preventDefault(); e.stopPropagation(); if (childMenu) childMenu.close(true); else g.tryClose(); };
  const onDown = (e: Event) => {
    const t = e.target as Node;
    // Its own button toggles it (menu()); anywhere else closes it first.
    if (childMenu) { if (childMenu.node.contains(t) || childMenu.anchor.contains(t)) return; childMenu.close(false); }
    if (!panel.contains(t) && !state.anchor.contains(t)) g.tryClose();
  };
  const opened = anchor.isConnected;
  let placer: {place: () => void; dispose: () => void} | null = null;
  function close() {
    if (openPopover?.node !== panel) return;
    const had = panel.contains(document.activeElement), to = state.anchor;
    childMenu?.close(false); g.dispose(); placer?.dispose();
    untether(panel); panel.remove(); to.setAttribute('aria-expanded','false'); openPopover = null;
    document.removeEventListener('keydown', onKey, true); document.removeEventListener('pointerdown', onDown, true);
    // Its button may have been redrawn meanwhile (or is a moment later): the focus goes to its replacement, as for a dialog.
    // maintenance (WCAG 2.4.3): focus left on something no longer shown (a field of a closed list) comes back too.
    if (had || focusLost()) returnFocus(openerOf(to));
  }
  // D-057 (a redraw never loses what the person has open): its button redrawn away, the panel moves to the one `keep`
  // finds for the same record, named open (aria-expanded) and placed against it. False: nothing took its place.
  const rehome = () => {
    if (state.anchor.isConnected) return true;
    const next = options.keep?.(); if (!next?.isConnected) return false;
    state.anchor = next; next.setAttribute('aria-expanded','true'); placer?.place(); return true;
  };
  const state = {node: panel, anchor, close, tryClose: g.tryClose, key: options.key, place: () => {}, rehome};
  // maintenance: a build that throws still opens, saying so, and reports the error: never a press that does nothing.
  try { build(panel, close); } catch (e) { panel.replaceChildren(el('p',{class:'popover-section muted',role:'alert'},'This could not be shown. '+((e as Error)?.message||''))); reportError(e); }
  // A modal dialog is in the top layer: a panel opened from inside it must live in it, or it renders underneath.
  (anchor.closest('dialog') ?? document.body).append(panel); anchor.setAttribute('aria-expanded','true'); openPopover = state;
  // Placed by the one engine; while open it follows its button. SW3-D-02: an action menu without a key cannot be handed
  // to a redrawn button, so once its button is gone (its list redrew) it closes: left floating over the new rows, its
  // items acted on the previous render's item. Panels with fields stay (closing them would lose what is being typed).
  // One with `keep` moves to its record's new button instead, and closes only when that record is gone.
  placer = placeNear(panel, () => state.anchor, {placement: options.placement}, () => {
    if (openPopover?.node !== panel) return false;
    if (options.keep) {
      if (opened && !rehome() && !(awaited && awaited.key === state.key)) { close(); return false; }
      return true;
    }
    if (!state.key && opened && !state.anchor.isConnected && (panel.matches('[role=menu]') || panel.querySelector('[role=menu]'))) { close(); return false; }
    return true;
  });
  state.place = placer.place;
  document.addEventListener('keydown', onKey, true); document.addEventListener('pointerdown', onDown, true);
  // The first field, selects included: focusing a later checkbox scrolled a tall panel past its first filters. A panel
  // whose first button acts on everything (Mark all as read) marks its safe start with data-autofocus.
  const entry=panel.querySelector<HTMLElement>('[data-autofocus]')||panel.querySelector<HTMLElement>('[aria-checked=true],button,a,input,select,textarea');
  const checked=entry?.closest('[role=radiogroup]')?.querySelector<HTMLElement>('[role=radio][aria-checked=true]');
  // N_FOCUS (APG dialog): with nothing to focus inside (a note, a skeleton), the panel itself takes the focus, so
  // Escape always hands it back to its button (the adopted one after a redraw), never leaves it where it was.
  const first=checked||entry;
  if(first)first.focus();else{panel.tabIndex=-1;panel.focus({preventScroll:true});}
  return close;
}
/** A page change (a nav item, Back, a link) closes any open popover, even one on the frame (account menu, inbox). */
export function closePopover() { openPopover?.close(); }
let pendingAdopt: {key: string; anchor: HTMLElement} | null = null;
let awaited: {key: string} | null = null;
/** After a redraw: a popover whose button is gone moves to the new button with the same key, or closes
 *  (a stale popover must not float over the new screen). One whose button comes after a read (awaitAdoption) waits for it. */
export function closeOrphanPopover() {
  const next = pendingAdopt; pendingAdopt = null;
  if (!openPopover || openPopover.anchor.isConnected || openPopover.rehome()) return;
  // Only an awaited key: a keyless popover (undefined) never matches no adoption awaited.
  if (awaited && awaited.key === openPopover.key && !(next && next.key === openPopover.key && next.anchor.isConnected)) { pendingAdopt = next; return; }
  if (next && next.key === openPopover.key && next.anchor.isConnected) adoptPopover(next.key, next.anchor);
  else openPopover.close();
}
/** A redraw that builds the button for `key` only after a read (a side panel's skeleton first): an open popover with
 *  that key stays open for it instead of closing as an orphan when the page swaps in. Call the returned function once
 *  the new button is on the page (or will not come): an open popover still without a button then closes. */
export function awaitAdoption(key: string): () => void {
  if (openPopover?.key !== key) return () => {};
  const mine = {key}; awaited = mine;
  return () => {
    if (awaited !== mine) return;
    awaited = null;
    // After the new button's own hand-over (a microtask queued when it was built); one waiting for a frame is pendingAdopt's.
    queueMicrotask(() => { if (openPopover?.key === key && !openPopover.anchor.isConnected && pendingAdopt?.key !== key && !openPopover.rehome()) openPopover.close(); });
  };
}
/** A screen redrew itself while its popover was open: the popover stays open and now belongs to the new button.
 *  While the old screen is still shown (a redraw that swaps in when ready), or the new button is not on screen yet
 *  (built before it is mounted), the hand-over waits for closeOrphanPopover or the next frame. */
export function adoptPopover(key: string, anchor: HTMLElement) {
  if (!openPopover || openPopover.key !== key || openPopover.anchor === anchor) return;
  if (openPopover.anchor.isConnected || !anchor.isConnected) {
    pendingAdopt = {key, anchor};
    requestAnimationFrame(() => { if (pendingAdopt?.anchor === anchor && anchor.isConnected && openPopover && !openPopover.anchor.isConnected) { pendingAdopt = null; adoptPopover(key, anchor); } });
    return;
  }
  openPopover.anchor = anchor; anchor.setAttribute('aria-expanded','true'); openPopover.place();
}
export function menuItem(label: string, name: IconName, action: () => unknown, attrs: Record<string, unknown> = {}, hint = '') {
  const {class:extra,...rest}=attrs;
  const b = el('button',{type:'button',role:'menuitem',...rest,class:`menu-item ${extra||''}`},icon(name),el('span',{},label),hint?el('span',{class:'hint'},hint):null);
  b.addEventListener('click',()=>{try{const r=action();if(r instanceof Promise)r.catch(e=>{reportError(e);});}catch(e){reportError(e);}});
  return b;
}
/** The shared reason for a control that cannot be used now. */
export const PAGE_WHY={first:'This is the first page.',last:'This is the last page.'} as const;
const quietWhy=(why:string)=>why===PAGE_WHY.first||why===PAGE_WHY.last||why===TERMS.nothingChanged||why===TERMS.nothingToPropose||why===TERMS.keepBestStopped;
const whyOf=new WeakMap<HTMLElement,HTMLElement>();
function sayWhy(b:HTMLElement,why:string,more:Child[]=[],at?:HTMLElement){
  let hint=at||whyOf.get(b);if(!hint){hint=el('small',{class:'why-unavailable'});whyOf.set(b,hint);}
  hint.id||=`why-${++fieldSequence}`;
  const ids=(b.getAttribute('aria-describedby')||'').split(' ').filter(x=>x&&x!==hint!.id);
  hint.replaceChildren(...(why?[why,...more]:[]).filter((x):x is Node|string=>x instanceof Node||typeof x==='string'&&!!x));
  // A reason anyone can see from the control itself (a pager's end, an untouched form) is its description only, never words beside it.
  if(!at){hint.hidden=!why;hint.classList.toggle('sr-only',quietWhy(why));}
  if(why)ids.push(hint.id);if(ids.length)b.setAttribute('aria-describedby',ids.join(' '));else b.removeAttribute('aria-describedby');
  if(at)return;
  const place=()=>{if(!hint!.isConnected&&b.parentElement)b.after(hint!);};place();
  // The shared observer only sees a button in the document: once placed, re-read a state set while it was detached.
  if(!hint.isConnected)queueMicrotask(()=>{place();if(b.isConnected&&b instanceof HTMLButtonElement&&reasons.has(b))sayReason(b);});
}
const reasons=new WeakMap<HTMLButtonElement,{why:string;more:Child[];at?:HTMLElement}>();
let reasonObserver:MutationObserver|null=null;
const ensureReasonObserver=()=>{if(reasonObserver||typeof document==='undefined'||!document.body)return;
  reasonObserver=new MutationObserver(records=>{for(const r of records){const b=r.target instanceof HTMLButtonElement?r.target:null;if(b&&reasons.has(b))sayReason(b);}});
  reasonObserver.observe(document.body,{subtree:true,attributes:true,attributeFilter:['disabled','aria-busy']});};
const sayReason=(b:HTMLButtonElement)=>{const r=reasons.get(b)!;sayWhy(b,b.disabled&&b.getAttribute('aria-busy')!=='true'?r.why:'',r.more,r.at);};
function because<T extends HTMLButtonElement>(b:T,why:string,more:Child[]=[],at?:HTMLElement):T{
  ensureReasonObserver();
  reasons.set(b,{why,more,at});sayReason(b);return b;
}
/** Each held control's reason now: a press says the words it is described with at that moment. */
const heldWhy=new WeakMap<HTMLElement,string>();
/** A control in place but not usable yet (DL-007; D59: the whole rota still loading): aria-disabled, so it keeps its
 *  focus and its tip, with `why` as its description; a press only says why again and never runs later. A field is
 *  read-only meanwhile. Drawn again once usable, so it is never released here. */
export function notYet<T extends HTMLElement>(n:T,why:string):T{
  n.setAttribute('aria-disabled','true');n.dataset.tip=why;if(n instanceof HTMLInputElement)n.readOnly=true;
  // A menu item's reason is its tip and its press (a menu holds only items).
  if(!n.matches('[role^=menuitem]')){sayWhy(n,why);whyOf.get(n)!.classList.add('sr-only');}
  // Held again (its reason moved on, e.g. the check started): the words change, the one press listener stays.
  const held=heldWhy.has(n);heldWhy.set(n,why);if(held)return n;
  // Capture, on the control itself: before its own click handler (and the footer's delegated one), which never runs.
  n.addEventListener('click',e=>{e.preventDefault();e.stopImmediatePropagation();notify(heldWhy.get(n)!,'info');},true);  // visible, and said (a toast is a status)
  return n;
}
/** A disabled button's reason is visible beside it and referenced by aria-describedby. */
export function disabledWhy<T extends HTMLButtonElement>(b:T,why:string,more:Child[]=[],at?:HTMLElement):T{
  b.disabled=!!why;return because(b,why,more,at);
}
/** One menu item as data (menu() builds it with menuItem); `armed`: the same-button confirm. An element built by
 *  menuItem() may stand in for one when the caller updates it after opening (a count arriving later). */
export type MenuItem={label:string;icon:IconName;run:()=>unknown;hint?:string;danger?:boolean;armed?:boolean;disabled?:boolean};
export type MenuEntry=MenuItem|HTMLElement|'separator'|{group:string;items:MenuItem[]};
let childMenu:{node:HTMLElement;anchor:HTMLElement;close:(back:boolean)=>void}|null=null,openAtEnd=false;
const menuSources=new WeakMap<Element,(anchor:HTMLElement)=>MenuEntry[]>();
/** The menus opened inside `host` (a list, a grid) that stay open across a redraw (menu()'s `keep`): `source` gives a
 *  menu button's items for its record as it is now, [] when it has none. The host on the page answers, so a kept menu
 *  never runs an item built from an earlier draw's record. */
export function menuSource(host:Element,source:(anchor:HTMLElement)=>MenuEntry[]){menuSources.set(host,source);}
/** An item of a kept popover (popover/menu `keep`) pressed in the instant between a redraw replacing its button and the
 *  hand-over: the hand-over happens now and the same item of the menu as it is now runs, or the menu closes. True when
 *  the press was handled so (the caller then runs nothing of its own, built from an earlier draw). */
export function handOver(item:HTMLElement):boolean{
  const open=openPopover;if(!open?.node.contains(item)||open.anchor.isConnected)return false;
  const again=open.rehome()?[...open.node.querySelectorAll<HTMLElement>('[role^=menuitem]')].find(b=>b.textContent===item.textContent):null;
  if(again&&again!==item)again.click();else open.close();return true;
}
/** After a kept menu's items were built again: the focus back on the item named `at` (the first one if it is gone). */
export function refocusItem(box:HTMLElement,at:string|null){
  if(at===null)return;const all=[...box.querySelectorAll<HTMLElement>('[role^=menuitem]:not(:disabled)')];(all.find(b=>b.textContent===at)||all[0])?.focus();
}
/** The one action menu (WAI-ARIA APG Menu Button): a list of items that act and close it. The popup is the menu itself
 *  (role=menu; ↑ ↓ Home End, a letter jumps, Enter acts, Esc closes back to the button, Tab closes); with `head` (a
 *  visible title such as "Show their… · 3 people") it is a dialog holding the title and the menu. Opened from inside an
 *  open popover (a row's ⋯ in a filter builder) it is that popover's child: Esc and a click elsewhere close it alone.
 *  Its button says what it opens (opens()). `items` may be a function, read when it opens.
 *  `keep` (D-057): a selector for this menu's button that holds across redraws (its record's id, e.g. a row's
 *  `[data-row="…"] .dt-more`). When a redraw replaces the button, the menu stays open on the one now matching, with
 *  the items its host gives for the record as it is now (menuSource) and the focus on the same item; it closes when
 *  nothing matches or the record has no actions left. */
export function menu(anchor:HTMLElement,label:string,items:MenuEntry[]|(()=>MenuEntry[]),o:{placement?:Placement;key?:string;head?:Child;keep?:string}={}):()=>void{
  opens(anchor,o.head?'dialog':'menu');
  const atEnd=openAtEnd;openAtEnd=false;
  let box:HTMLElement|null=null,shut=()=>{};
  const keep=o.keep?()=>{
    const next=document.querySelector<HTMLElement>(o.keep!);let source:((anchor:HTMLElement)=>MenuEntry[])|undefined;
    for(let n:Element|null=next;n&&!source;n=n.parentElement)source=menuSources.get(n);
    const entries=next&&source?source(next):[];if(!box||!entries.length)return null;
    const at=box.contains(document.activeElement)?document.activeElement!.textContent:null,was=box.querySelector<HTMLElement>('.armed')?.dataset.armedFrom;
    items=entries;box.replaceChildren(...list(shut));refocusItem(box,at);
    // An armed confirm stays armed in the redrawn menu: its second press still confirms (not a fresh first press).
    const again=was?[...box.querySelectorAll<HTMLElement>('[role^=menuitem]')].find(b=>(b.getAttribute('aria-label')||b.textContent||'').trim()===was):undefined;
    if(again){again.click();again.focus();}
    return next;
  }:undefined;
  const item=(x:MenuItem,close:()=>void)=>{
    const m:HTMLElement=menuItem(x.label,x.icon,()=>{
      if(keep&&handOver(m))return;
      close();return x.run();},{...(x.danger?{class:'danger'}:{}),...(x.disabled?{disabled:true}:{})},x.hint||'');
    return x.armed?armed(m):m;};
  // A group is named in words above its items (role=group; the visible name is its accessible name).
  const list=(close:()=>void)=>(typeof items==='function'?items():items).map(x=>
    x==='separator'?el('hr',{class:'menu-sep'}):x instanceof HTMLElement?x
    :'group' in x?el('div',{role:'group','aria-label':x.group,class:'menu-group'},el('div',{class:'menu-heading','aria-hidden':'true'},x.group),...x.items.map(i=>item(i,close)))
    :item(x,close));
  const last=(box:HTMLElement)=>{const all=[...box.querySelectorAll<HTMLElement>('[role^=menuitem]:not(:disabled)')];all[all.length-1]?.focus();};
  if(childMenu?.anchor===anchor){childMenu.close(true);return ()=>undefined;}
  const parent=openPopover?.node.contains(anchor)?openPopover.node:null;
  if(!parent){
    const close=popover(anchor,(panel,close)=>{
      shut=close;
      if(o.head){panel.append(el('div',{class:'dt-pop-label'},o.head),box=el('div',{class:'menu-list',role:'menu','aria-label':label},...list(close)));return;}
      // A plain action menu is the popup itself, not a dialog holding one.
      panel.setAttribute('role','menu');panel.classList.add('menu-list');compositeKeys(panel);panel.append(...list(close));box=panel;},{label,placement:o.placement,key:o.key,keep});
    if(atEnd&&openPopover?.anchor===anchor)last(openPopover.node);
    return close||(()=>undefined);
  }
  childMenu?.close(false);
  const panel=el('div',{class:'popover menu-list',role:'menu','aria-label':label});
  const close=(back:boolean)=>{if(childMenu?.node!==panel)return;childMenu=null;placer.dispose();panel.remove();anchor.setAttribute('aria-expanded','false');if(back)anchor.focus();};
  panel.append(...list(()=>close(false)));
  panel.addEventListener('keydown',e=>{if(e.key==='Tab'){e.preventDefault();close(true);}});
  (parent.parentElement||document.body).append(panel);anchor.setAttribute('aria-expanded','true');
  childMenu={node:panel,anchor,close};
  // Its button redrawn away (the builder redrew): the menu goes too, as an orphaned popover menu does (SW3-D-02).
  const placer=placeNear(panel,anchor,{placement:o.placement},()=>{if(childMenu?.node!==panel)return false;if(!anchor.isConnected){close(false);return false;}return true;});
  if(atEnd)last(panel);else panel.querySelector<HTMLElement>('[role^=menuitem]:not(:disabled)')?.focus();
  return ()=>close(false);
}
/** The one search box inside a panel or form: named, `type=search`; Esc clears what is typed first (the panel or
 *  dialog stays), and only an empty box lets Esc through to close it. `onInput` gets the trimmed text as it changes. */
export function panelSearch(label:string,placeholder:string,onInput?:(q:string)=>void,cls=''):HTMLInputElement{
  const input=el('input',{type:'search',class:cls,'aria-label':label,placeholder,autocomplete:'off'});
  input.addEventListener('keydown',e=>{if(e.key!=='Escape'||!input.value)return;e.preventDefault();e.stopPropagation();input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));});
  if(onInput)input.addEventListener('input',()=>onInput(input.value.trim()));
  return input;
}
/** The rest of a list cut short, in a sentence: "Sun 1, Mon 2 and 3 more". */
export const moreWords=(n:number)=>`and ${n} more`;
/** What a "+N" counts: [one, many] ("session", "sessions"), or one word for any count (a list's label). */
export type MoreNoun=string|readonly [string,string];
/** How a "+N" is named: "N more ‹noun›[ ‹of›][: ‹action›]" ("2 more sessions: show all 5", "1 more session"). */
export type MoreWords={noun:MoreNoun;of?:string;action?:string};
export const moreName=(n:number,w:MoreWords)=>
  `${n} more ${typeof w.noun==='string'?w.noun:w.noun[n===1?0:1]}${w.of?' '+w.of:''}${w.action?': '+w.action:''}`;
/** Paints (and repaints, when a row is fitted again) a "+N": it shows "+N"; a button carries the full name; a plain mark
 *  says it in hidden words; a mark inside a control that names everything (aria-hidden) only shows "+N". */
export function paintMore<T extends HTMLElement>(m:T,n:number,w:MoreWords):T{
  if(m.getAttribute('aria-hidden')==='true')m.textContent=`+${n}`;
  else if(m.tagName==='BUTTON'){m.textContent=`+${n}`;m.setAttribute('aria-label',moreName(n,w));}
  else m.replaceChildren(el('span',{'aria-hidden':'true'},`+${n}`),el('span',{class:'sr-only'},moreName(n,w)));
  return m;
}
/** The one "+N" for hidden items (DESIGN D8: footer, grid cells, chip rows, name lists show the first few, then "+N"):
 *  it shows "+N" and is named in full by moreName. `all`: the action is "show all M" (and its tip). `list` builds the
 *  numbered list it opens (a popover); `open` shows them elsewhere instead (a panel); neither: the caller's own handler.
 *  `title`: that list's title when it holds more than the noun names. `as`: 'span' a plain mark, 'hidden' a mark inside
 *  a control that already names everything. */
export function overflowMore(n:number,noun:MoreNoun,o:{all?:number;of?:string;action?:string;list?:()=>Child[];open?:()=>void;title?:string;
  cls?:string;as?:'button'|'span'|'hidden';attrs?:Record<string,string>}={}):HTMLElement{
  const w={noun,of:o.of,action:o.action??(o.all!=null?`show all ${o.all}`:undefined)},cls=o.cls??'pill-more';
  if(o.as==='span'||o.as==='hidden')return paintMore(el('span',{class:cls,...(o.as==='hidden'?{'aria-hidden':'true'}:{}),...o.attrs}),n,w);
  const b=paintMore(el('button',{type:'button',class:cls,...(o.list?popup('dialog'):{}),...(o.all!=null?{'data-tip':`Show all ${o.all}`}:{}),...o.attrs}),n,w);
  const many=o.title??(typeof noun==='string'?noun:noun[1]);  // the opened list's title
  if(o.list||o.open)b.addEventListener('click',e=>{e.stopPropagation();if(o.open){o.open();return;}
    popover(b,panel=>{panel.classList.add('pill-pop');panel.append(el('div',{class:'dt-pop-label'},`${many} · ${o.all}`),el('ol',{},...o.list!().map(x=>el('li',{},x))));},{label:`All ${many}`});});
  return b;
}
/** One step of a step rail: its title; done (a check, and "completed" in its name) or current (aria-current=step);
 *  `go` makes it a button (a step that cannot be reached yet is `disabled`, or plain text without `go`); `controls`:
 *  the id of the section it jumps to. */
export type Step={title:string;state?:'done'|'current';go?:()=>void;disabled?:boolean;controls?:string};
/** The one step rail (forms, the Change flow, Score check): an ordered list, numbered, the current step marked
 *  (WAI-ARIA aria-current="step"; W3C WAI Tutorials, multi-page forms). `cls` picks the look (stepper, step-rail). */
export function stepper(steps:Step[],o:{label?:string;cls?:string}={}):HTMLOListElement{
  return el('ol',{class:o.cls||'stepper','aria-label':o.label||'Steps'},...steps.map((s,i)=>{
    const parts=[el('span',{class:'step-num','aria-hidden':'true'},s.state==='done'?icon('check'):String(i+1)),el('span',{class:'truncate'},s.title),
      s.state==='done'?el('span',{class:'sr-only'},' (completed)'):null];
    const attrs={class:'step-link'+(s.state==='current'?' current':s.state==='done'?' passed':''),'aria-current':s.state==='current'?'step':'false',
      ...(s.controls?{'aria-controls':s.controls}:{})};
    if(!s.go)return el('li',{},el('span',attrs,...parts));
    const b=el('button',{type:'button',...attrs,disabled:!!s.disabled},...parts);b.addEventListener('click',s.go);
    return el('li',{},b);}));
}
/** One option of a segmented choice: its value, its words (always its name), and an icon, a tooltip, `disabled` (the
 *  tooltip says why) or `iconOnly` (words in the name and tooltip only, where a phone toolbar has no room). */
export type Segment<T extends string>=[value:T,text:string,o?:{icon?:IconName;tip?:string;disabled?:boolean;iconOnly?:boolean}];
/** The one segmented choice (WAI-ARIA APG Radio Group): role=radiogroup of role=radio buttons, one Tab stop, arrows move
 *  (`activation` 'auto' also chooses; 'manual' for a choice that redraws a page or loads data, chosen with Space).
 *  `value` null: nothing chosen yet (Tab lands on the first). onChange runs on a new choice only; `.set(v)` shows a
 *  choice made elsewhere. Named by `label`, or by a visible label's id (`labelledBy`). */
export function segmented<T extends string>(label:string,options:Segment<T>[],value:T|null,onChange:(v:T)=>void,
  o:{activation?:Activation;labelledBy?:string;cls?:string}={}):HTMLElement&{set:(v:T|null)=>void}{
  const box=el('div',{class:['segmented',o.cls].filter(Boolean).join(' '),role:'radiogroup',activation:o.activation,
    ...(o.labelledBy?{'aria-labelledby':o.labelledBy}:{'aria-label':label})});
  const set=(v:T|null)=>{value=v;for(const b of box.querySelectorAll<HTMLElement>('[role=radio]'))b.setAttribute('aria-checked',String(b.dataset.value===v));};
  for(const [v,text,x={}] of options){
    const b=el('button',{type:'button',role:'radio','data-value':v,disabled:!!x.disabled,...(x.iconOnly?{'aria-label':text}:{}),...(x.tip||x.iconOnly?{'data-tip':x.tip||text}:{})},
      x.icon?icon(x.icon):null,x.iconOnly?null:text);
    b.addEventListener('click',()=>{if(value===v)return;set(v);onChange(v);});
    box.append(b);
  }
  set(value);
  return Object.assign(box,{set});
}
/** A group's header is itself its toggle (owner rule: no disclosure arrow anywhere; DESIGN › Disclosure): a button
 *  with aria-expanded showing the group's name and how many it holds; collapsed, the count says so in words
 *  ("12 hidden"), on the same solid muted chip. `lead`: a mark before the name (a duty swatch, a topic square);
 *  `extra`: facts after the count; `cls`: the surface's own layout class. WAI-ARIA APG Disclosure. */
export function groupToggle(name:Child,count:number|string,open:boolean,onToggle:(open:boolean)=>void,
  o:{lead?:Child;extra?:Child[];cls?:string;attrs?:Record<string,unknown>}={}):HTMLButtonElement{
  const chip=el('span',{class:'gt-count'});
  // Spaces between the parts: a flex button shows none, but its accessible name reads "Consultant 12", not "Consultant12".
  const b=el('button',{type:'button',...o.attrs,class:['group-toggle',o.cls].filter(Boolean).join(' ')},o.lead,name,' ',chip,...(o.extra||[]).flatMap(x=>[' ',x]));
  const show=(on:boolean)=>{open=on;b.setAttribute('aria-expanded',String(on));chip.textContent=on?String(count):`${count} hidden`;};
  show(open);b.addEventListener('click',()=>{show(!open);onToggle(open);});
  return b;
}
/** What a button opens, said before it is used (WAI-ARIA: aria-haspopup is the popup's role; aria-expanded starts false
 *  and the opener keeps it): spread into its attributes, el('button',{...popup('menu')}). A picker or any panel is a
 *  dialog; only menu() opens a menu. Never write aria-haspopup by hand (tests/test_web_controls_guard.py). */
export const popup=(kind:'menu'|'dialog')=>({'aria-haspopup':kind,'aria-expanded':'false'});
/** popup() for a button already built. Idempotent. */
export function opens<T extends HTMLElement>(trigger:T,kind:'menu'|'dialog'):T{
  trigger.setAttribute('aria-haspopup',kind);if(!trigger.hasAttribute('aria-expanded'))trigger.setAttribute('aria-expanded','false');
  return trigger;
}
// ↓ or ↑ on a closed menu button opens it at its first or last item (APG Menu Button), however the button was built.
// Delegated and last: a list that moves between rows on ↓ (table_view roveRows) has already taken the key.
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('keydown',e=>{
  if((e.key!=='ArrowDown'&&e.key!=='ArrowUp')||e.altKey||e.ctrlKey||e.metaKey||e.defaultPrevented)return;
  const t=e.target instanceof HTMLElement&&e.target.matches('[aria-haspopup=menu]:not([aria-expanded=true])')?e.target:null;
  if(!t)return;e.preventDefault();openAtEnd=e.key==='ArrowUp';t.click();openAtEnd=false;});
export type HoverContent={build:()=>Node[];delay:number;plain?:boolean};
const hoverCards=new WeakMap<HTMLElement,HoverContent>();
let hoverTimer=0,hoverLeaveTimer=0,hoverSequence=0,hoverNode:HTMLElement|null=null,hoverTarget:HTMLElement|null=null;
let hoverRedraw:(()=>void)|null=null,hoverWatch:MutationObserver|null=null,dismissedHover:HTMLElement|null=null;
/** Help opened by keyboard focus alone: Escape hides it and still reaches the panel or dialog it sits in. */
let helpFromFocus=false;
/** A redraw removed the hovered element without a mouseleave: drop its card too. */
/** Focus given back by the app (a panel closed) opens no help card on that control until focus or the pointer leaves it
 *  (SI-01: the card stayed over the grid after Esc closed the session panel). */
export function focusQuietly(n:HTMLElement){dismissedHover=n;n.focus({preventScroll:true});}
export function dropHoverCard(){
  window.clearTimeout(hoverTimer);window.clearTimeout(hoverLeaveTimer);hoverWatch?.disconnect();hoverWatch=null;
  if(hoverNode){
    const ids=(hoverTarget?.getAttribute('aria-describedby')||'').split(/\s+/).filter(id=>id&&id!==hoverNode!.id);
    if(ids.length)hoverTarget?.setAttribute('aria-describedby',ids.join(' '));else hoverTarget?.removeAttribute('aria-describedby');
    untether(hoverNode);hoverNode.remove();
  }
  hoverNode=null;hoverTarget=null;hoverRedraw=null;
}
/** Data the open card was waiting for has arrived (e.g. the planner's rule map): build its content again in place. */
export function refreshHoverCard(){if(hoverNode?.isConnected)hoverRedraw?.();}
/** Hover/focus card for dense grids. Content is built lazily; never steals focus. */
export function hoverCard(target:HTMLElement,build:()=>Node[],delay=280){
  hoverCards.set(target,{build,delay});
}
/** One resolver for every card inside `root` (a planner grid): asked for each element the pointer or focus is on, it
 *  builds the card from what that element names (ids), read when shown, so no element holds a card of its own. */
const hoverRoots=new WeakMap<HTMLElement,(n:HTMLElement)=>HoverContent|null>();
export function hoverWithin(root:HTMLElement,resolve:(n:HTMLElement)=>HoverContent|null){hoverRoots.set(root,resolve);}
/** An expanded control, or a mark inside the open popover's control (a rule cell's strength badge under its editor),
 *  shows no help: help floating over that popover would also take its Esc (a redraw under a still pointer hovers it). */
const shut=(n:HTMLElement)=>n.getAttribute('aria-expanded')==='true'||!!openPopover?.anchor.contains(n);
/** The shared classes that cut text with an ellipsis (app.css); hover shows what they cut. */
export const CUT_TEXT='.truncate,.clamp-2';
/** Nearest help wins: a mark inside a rich card's trigger explains the mark, never two overlapping tooltips. */
function hoverContent(from:EventTarget|null):({target:HTMLElement}&HoverContent)|null{
  let resolve:((n:HTMLElement)=>HoverContent|null)|undefined;
  for(let r=from instanceof Element?from:null;r&&!resolve;r=r.parentElement)if(r instanceof HTMLElement)resolve=hoverRoots.get(r);
  for(let n=from instanceof Element?from:null;n;n=n.parentElement){
    if(!(n instanceof HTMLElement))continue;
    if(shut(n))return null;
    const rich=hoverCards.get(n)||resolve?.(n);if(rich)return {target:n,...rich};
    let text=n.dataset.tip;
    if(!text&&n.dataset.ctip){
      // CSS is the source of whether a compact label is hidden. Truncated headings/vertical labels have no .label.
      const label=n.querySelector<HTMLElement>('.label')||n.firstElementChild||n;
      if(getComputedStyle(label).display==='none'||label.scrollWidth>label.clientWidth+1||label.scrollHeight>label.clientHeight+1)text=n.dataset.ctip;
    }
    // maintenance (WCAG 1.4.12, 1.4.13): every text cut by the shared truncation classes shows its full text here while it is
    // cut (text spacing, a narrow column), as a compact tip does; one with its own title or tip keeps that.
    if(!text&&n.matches(CUT_TEXT)&&!n.title&&(n.scrollWidth>n.clientWidth+1||n.scrollHeight>n.clientHeight+1))text=n.textContent?.trim()||'';
    // Built when shown, from its source as it is then: a tip that went away meanwhile shows no box (never stale words).
    if(text){const target=n,cut=!n.dataset.tip&&!n.dataset.ctip;
      // A tip's " · " parts (a name and its rank) are isolated as on the page; a plain sentence stays one text.
      return {target,build:()=>{const t=cut?target.textContent?.trim()||'':target.dataset.tip||target.dataset.ctip||'';return t.includes(DOT)?bidiRuns(t):[document.createTextNode(t)];},delay:280,plain:true};}
  }
  return null;
}
/** Something else is drawn over the middle of this control (a menu or panel opened over it): its help would float above
 *  that layer, cut off from the control, as an unexplained box. */
function covered(target:HTMLElement){
  const r=target.getBoundingClientRect();if(!r.width||!r.height)return false;
  const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
  return !!hit&&!target.contains(hit)&&!hit.contains(target)&&!hoverNode?.contains(hit);
}
function showHover(content:ReturnType<typeof hoverContent>,fromFocus=false){
  window.clearTimeout(hoverLeaveTimer);
  if(!content||content.target===dismissedHover||content.target===hoverTarget)return;
  helpFromFocus=fromFocus;
  dropHoverCard();const {target,build,delay,plain}=content;hoverTarget=target;
  hoverTimer=window.setTimeout(()=>{
    if(!target.isConnected||shut(target)||covered(target)){dropHoverCard();return;}
    const parts=build();if(!parts.some(n=>n.nodeType===1||n.textContent?.trim())){dropHoverCard();return;}  // never an empty box
    const card=el('div',{class:plain?'help-tooltip':'hovercard',role:'tooltip',id:`hover-help-${++hoverSequence}`},...parts);
    (target.closest('dialog')||document.body).append(card);hoverNode=card;
    // Like the shared picker: escape ancestor clipping in the top layer, with fixed positioning as the fallback.
    if('showPopover' in card){card.setAttribute('popover','manual');card.showPopover();}
    target.setAttribute('aria-describedby',[target.getAttribute('aria-describedby'),card.id].filter(Boolean).join(' '));
    // The one placement engine: rich cards beside their element (else centred under or over it), plain help centred;
    // a hoverable card never covers its own opener on a phone and scrolls long content in the room left.
    const placer=placeNear(card,target,{placement:plain?'center':'beside',gap:7,room:[1,Infinity],mark:plain?null:'is-pointed'},()=>hoverNode===card);
    const place=()=>{placer.place();
      // Cards never block the chips under them; only a card cut short takes the pointer, so it can be scrolled.
      card.classList.toggle('scrolls',card.scrollHeight>card.clientHeight+1);
    };
    place();hoverRedraw=()=>{if(hoverNode===card&&target.isConnected){card.replaceChildren(...build());place();}};
    // Only the visible tooltip is observed: a redraw or opening a menu must not leave stale help above the new UI.
    let hitTest=0;
    hoverWatch=new MutationObserver(records=>{
      if(!target.isConnected||shut(target)){dropHoverCard();return;}
      // A menu or panel opened over the control: its help goes rather than float over the new layer. Only added elements
      // can cover it, and the hit test waits for the next frame (once per frame), so a page that keeps building while
      // help is shown is not laid out again for every change.
      if(!hitTest&&records.some(r=>r.type==='childList'&&!card.contains(r.target)&&[...r.addedNodes].some(n=>n.nodeType===1)))
        hitTest=requestAnimationFrame(()=>{hitTest=0;if(hoverNode===card&&covered(target))dropHoverCard();});
      if(plain&&records.some(r=>r.target===target&&r.type==='attributes')){
        if(hoverContent(target)?.target!==target){dropHoverCard();return;}
        hoverRedraw?.();return;
      }
      if(records.some(r=>card.contains(r.target)))place();
    });
    // Scoped, not body-wide (a live patch moving hundreds of chips elsewhere costs nothing): the target's ancestors (its
    // removal, or a layer added beside it) and the card's own content.
    for(let n=target.parentElement;n;n=n.parentElement)hoverWatch.observe(n,{childList:true});
    hoverWatch.observe(card,{childList:true,subtree:true});hoverWatch.observe(target,{attributes:true,attributeFilter:['aria-expanded','data-tip','data-ctip']});
  },delay);
}
let tooltipsInstalled=false;
/** One hover/focus lifecycle for labels, compact controls and lazy cards, including elements added by later pages. */
export function enhanceTooltips(){
  if(tooltipsInstalled)return;tooltipsInstalled=true;
  // Focus keeps help only when focus is what shows it: keyboard focus, or an explanation-only button. A control focused by
  // its own click keeps hover help only under the pointer, so help never opens or stays after the pointer has left.
  const focusHolds=()=>{const a=document.activeElement;return !!hoverTarget&&!!a&&hoverTarget.contains(a)&&(a.matches(':focus-visible')||hoverTarget.matches('button[data-help]'));};
  const interested=()=>!!hoverTarget&&(focusHolds()||hoverTarget.matches(':hover')||!!hoverNode?.matches(':hover'));
  const leave=()=>{window.clearTimeout(hoverLeaveTimer);hoverLeaveTimer=window.setTimeout(()=>{if(!interested())dropHoverCard();},120);};
  document.addEventListener('pointerover',e=>{
    // A touch is a tap, not a hover: no card on top of what the tap opens (S-11). Help buttons still answer the tap.
    if(e.pointerType==='touch')return;
    if(hoverNode?.contains(e.target as Node)){window.clearTimeout(hoverLeaveTimer);return;}
    const content=hoverContent(e.target);if(content)showHover(content);
  });
  document.addEventListener('pointerout',e=>{
    if(dismissedHover&&!dismissedHover.contains(e.relatedTarget as Node))dismissedHover=null;
    // Help still waiting out its delay has no card to move onto: the pointer left its control, so it never opens.
    if(!hoverNode&&hoverTarget&&!hoverTarget.contains(e.relatedTarget as Node)&&!focusHolds())dropHoverCard();else leave();
  });
  // Keyboard focus only (:focus-visible): focus moved by a click or by the app (panel ↑↓, a closing menu) shows no help.
  // :focus-visible alone also matches focus the app sets before any key is pressed (a page load, focus handed back after a
  // popover closed by a tap: SW3-C-16), so the last input must have been a key.
  let keyed=false;
  document.addEventListener('keydown',()=>{keyed=true;},true);document.addEventListener('pointerdown',()=>{keyed=false;},true);
  document.addEventListener('focusin',e=>{const content=keyed&&e.target instanceof Element&&e.target.matches(':focus-visible')?hoverContent(e.target):null;if(content)showHover(content,true);else if(!interested())dropHoverCard();});
  document.addEventListener('focusout',e=>{if(dismissedHover&&!dismissedHover.contains(e.relatedTarget as Node))dismissedHover=null;leave();});
  const dismiss=()=>{dismissedHover=hoverTarget||dismissedHover;dropHoverCard();};
  // Explanation-only buttons activate help on touch and keyboard; ordinary commands still dismiss it.
  const helpOnly=(from:EventTarget|null)=>{const content=hoverContent(from);return content?.target.matches('button[data-help]')?content:null;};
  document.addEventListener('pointerdown',e=>{if(!hoverNode?.contains(e.target as Node)&&!helpOnly(e.target))dismiss();},true);
  document.addEventListener('click',e=>{
    const help=helpOnly(e.target);if(help){dismissedHover=null;helpFromFocus=false;help.target.focus({preventScroll:true});if(hoverTarget!==help.target||!hoverNode){dropHoverCard();showHover({...help,delay:0});}return;}
    if(!hoverNode?.contains(e.target as Node))dismiss();
  },true);
  window.addEventListener('keydown',e=>{if(e.key==='Escape'&&hoverNode){
    const modal=e.target instanceof Element?e.target.closest('dialog[open]'):null;
    // A passive label in the focused modal must not take an Escape from its close/unsaved-changes guard.
    const passiveModalHelp=!!modal&&hoverNode.classList.contains('help-tooltip')&&!!hoverTarget&&modal.contains(hoverTarget)&&!hoverTarget.matches('button[data-help]');
    if(!helpFromFocus&&!passiveModalHelp){e.preventDefault();e.stopImmediatePropagation();}dismiss();
  }},true);
  window.addEventListener('resize',dismiss);
  window.addEventListener('scroll',e=>{
    const tray=document.querySelector('#toast-tray');if(tray?.childElementCount)stackToasts(tray);
    if(e.target instanceof Node&&hoverNode?.contains(e.target))return;
    // Focus or a hover can scroll its control into view before help opens (the scroll event comes a frame later). Keep
    // that pending help only while the control is still focused or under the pointer, and visible.
    if(!hoverNode&&hoverTarget&&(focusHolds()||hoverTarget.matches(':hover'))){
      const r=hoverTarget.getBoundingClientRect(),hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
      if(hit&&hoverTarget.contains(hit))return;
    }
    dismiss();
  },true);
}
/** RUL-03: a retire or restore that needs more approvers than have agreed: who approved so far (`me`: the signed-in
 *  identity) and how many are still needed (the rule is unchanged until then). Empty once complete or when one is enough. */
export const approvalPending=(a:any,me='')=>{if(!a||a.complete)return '';
  const ids=(a.approved_by||[]) as string[],others=ids.filter(id=>id!==me).length,left=a.approvals_required-a.approvals_received;
  const who=[me&&ids.includes(me)?'you':'',others?plural(others,ids.length>others?'other approver':'approver'):''].filter(Boolean).join(' and ');
  return `${a.action==='restore'?'Restoring':'Retiring'} approved by ${who} (${a.approvals_received} of ${a.approvals_required}) · ${plural(left,'more approval')} needed from a different approver`;};
/** Governed replies still waiting for other approvers are said as waiting, never as done; that message's Undo withdraws
 *  the approvals just given (`after` redraws once withdrawn). False when nothing waits (the caller says what applied). */
export function notifyWaiting(replies:any[],me='',after?:()=>unknown){
  const waiting=awaitingApproval(replies);if(!waiting.length)return false;
  notify(approvalPending(waiting[0].approval,me),'info',{label:'Undo',run:()=>withdrawApprovals(waiting).then(async()=>{
    notify('Approval withdrawn. The rule is unchanged.','info');await after?.();},e=>reportError(e))});
  return true;
}
/** A toast; with an action (e.g. Undo) it stays a little longer and offers one button. A success message right after a
 *  reversible save offers Undo by itself (see takeUndo in api.ts). Call it before awaiting a refresh: the save is
 *  already committed, and its message must claim its Undo before any later command or fallback notice. */
/** A clause the next success message carries (the Fix list's knock-on, "1 cleared · 1 new: …", R3): set by a draft edit's
 *  reply, said by that edit's own message with its Undo.
 *  shortcut: matched by time (the next success message within 5 s); a command-scoped message id would be exact. */
let clause:{text:string;at:number}|null=null;
export const toastClause=(text:string)=>{clause=text?{text,at:Date.now()}:null;};
export function notify(message: string, tone = 'success', action?: {label:string; run:()=>unknown; saves?:boolean}, error?: unknown, next?: {label:string; run:()=>unknown}) {
  // An action the person declined (a draft guard's Keep) or left (PAGE_LEFT), or another account's: nothing to say, as
  // reportError. Without this, every save path that notifies its own error showed an empty red message.
  if (tone === 'error' && sessionChanged(error)) return;
  if (clause && tone === 'success' && Date.now() - clause.at < 5000) message = `${message} ${clause.text}`;
  if (tone === 'success') clause = null;
  const undo = !action && tone === 'success' ? takeUndo() : null;
  // G-04: one outcome; a refusal that changed nothing and may be tried again (undoRefused) offers Try again.
  const runUndo=():Promise<unknown>=>undo!.run().then(done=>notifyWaiting(done.map(d=>d.result))||notifyUndone(done,undo!.done||'Undone. The previous values are back.'),
    e=>notify((e as Error).message,'error',(e as {retry?:boolean}).retry?{label:'Try again',run:runUndo,saves:true}:undefined,e));
  if (undo) action = {label:'Undo', run:runUndo};
  // maintenance: a save that could not be confirmed offers to read the rota again (app.ts), never a blind retry.
  if (tone === 'error' && !action && (error as {code?:string}|undefined)?.code === 'SAVE_UNCONFIRMED') action = {label:'Check the rota', run:()=>window.dispatchEvent(new CustomEvent('rota-recheck'))};
  if (tone === 'error') {recordError(message,'message shown');staleFault(error);const r=reporter(error);if(!action&&r)action={label:REPORT_PROBLEM,run:()=>r.report(unisolated(message))};}
  // A labelled region: landmark navigation (and F6 in screen readers) reaches the messages and their Undo.
  const tray = document.querySelector('#toast-tray') || document.body.appendChild(el('div',{id:'toast-tray','aria-live':'polite'}));
  if (!tray.hasAttribute('role')) { tray.setAttribute('role','region'); tray.setAttribute('aria-label','Messages'); }
  placeTray(tray as HTMLElement);
  const item = el('div',{class:`toast ${tone}${action||next?' with-action':''}`,role: tone === 'error' ? 'alert' : 'status'},tone === 'error' || tone === 'warning' ? icon('alert') : null,el('span',{},message));
  // WCAG 2.4.3: a message closing (Undo, Dismiss, its timeout) with focus in it gives focus back to where it came from.
  const done=()=>{const had=item.contains(document.activeElement);item.remove();stackToasts(tray);if(had)backFromTray();};
  // Report this problem captures the page with this message still on it, then the message goes.
  // maintenance: an action that saves (`saves`; every Undo and Redo) keeps its message open, saying what the footer says
  // (savingWords), until the save settles; its outcome is then said by the message that follows. Never closed first.
  // Other actions (Retry a read, Open…) close their message at once, as before.
  let working=false;const SAVE_EVENTS=['rota-saving','rota-save-checking','rota-saved'];
  if(action){const run=action.run,first=action.label===REPORT_PROBLEM,b=button(action.label,()=>{if(first){const r=run();done();return r;}
    if(!(action!.saves??['Undo','Redo'].includes(action!.label))){done();return run();}
    const r=run();if(!(r instanceof Promise)){done();return r;}
    const had=item.contains(document.activeElement);
    working=true;window.clearTimeout(timer);b.remove();item.classList.add('working');if(had)backFromTray();
    const said=item.querySelector(':scope>span')!,word=()=>{said.textContent=savingWords();};word();
    said.before(el('span',{class:'spinner','aria-hidden':'true'}));
    for(const t of SAVE_EVENTS)window.addEventListener(t,word);
    return r.finally(()=>{for(const t of SAVE_EVENTS)window.removeEventListener(t,word);done();});},'toast-action');
    b.classList.remove('button');item.append(b);}
  // A next step beside the Undo (e.g. "Assign A in the fill panel" after saving without A): it closes the message.
  if(next){const b=button(next.label,()=>{done();return next.run();},'toast-action');b.classList.remove('button');item.append(b);}
  item.append(iconButton('Dismiss message','close',()=>done(),{class:'bare toast-close'}));
  // The same message again (a redraw, a repeated refusal) replaces the one showing instead of stacking a copy.
  if (!action) for (const t of tray.querySelectorAll<HTMLElement>(':scope>.toast:not(.with-action)')) if (t.className === item.className && t.textContent === item.textContent) t.remove();
  tray.prepend(item); stackToasts(tray); followLayout(tray as HTMLElement);
  // WCAG 2.2.1 (timing adjustable): the time left pauses while the pointer is over the message or focus is in it.
  let left = tone === 'error' ? 14000 : action ? 10000 : 7000, since = Date.now(), timer = window.setTimeout(done, left), paused = false;
  const pause = () => { if (paused) return; paused = true; window.clearTimeout(timer); left -= Date.now() - since; };
  const resume = () => { if (working || !paused || item.matches(':hover') || item.contains(document.activeElement)) return;
    paused = false; since = Date.now(); left = Math.max(left, 3000); timer = window.setTimeout(done, left); };
  item.addEventListener('mouseenter', pause); item.addEventListener('focusin', pause);
  item.addEventListener('mouseleave', resume); item.addEventListener('focusout', () => setTimeout(resume));
  return done;
}
/** What an Undo did; a draft's Undo offers Redo (SW3-A-06), whose own message then offers Undo again. */
export function notifyUndone(done:Undone,message:string){
  const w=redoable(done);notify(message,'success',w?{label:'Redo',run:()=>redoDraft(w).catch(e=>reportError(e))}:undefined);}
/** Toasts live in the top-most open modal dialog (a modal makes the rest of the page inert, so an Undo behind it could
 *  not be pressed, e.g. a change made from the People side panel), else in the page. Dialogs open in document order. */
function trayHost():HTMLElement{const open=[...document.querySelectorAll('dialog')].filter(d=>d.open&&d.matches(':modal'));return open[open.length-1]||document.body;}
/** Moves the tray to the top-most host. While it sits in a dialog, the dialog's closing or removal by any path (a redraw,
 *  page change) hands the tray back, so its toasts and their Undo survive. */
let trayWatch:MutationObserver|null=null;
function placeTray(tray:HTMLElement){
  const host=trayHost();if(tray.parentElement===host)return;
  trayWatch?.disconnect();trayWatch=null;host.append(tray);
  // F18: on a phone a sheet fills the screen, so a plain message about the page behind it ("Swap offered") would only
  // cover the sheet's fields; it goes. Messages with an action (Undo) stay.
  if(host!==document.body&&phoneQuery.matches)for(const t of tray.querySelectorAll(':scope>.toast:not(.with-action)'))t.remove();
  // Moved into or out of a dialog: lift it above that host's own bottom bars (a dialog's form footer), not the last host's.
  stackToasts(tray);
  if(host===document.body||!host.parentElement)return;
  trayWatch=new MutationObserver(()=>{if(!host.isConnected||!(host as HTMLDialogElement).open)placeTray(tray);});
  trayWatch.observe(host.parentElement,{childList:true});trayWatch.observe(host,{attributes:true,attributeFilter:['open']});
}
/** A closing dialog hands its toasts and the live regions (announce) to whatever is underneath (call after close(),
 *  before remove(); every path that closes or removes a modal calls it). */
export function releaseToasts(dialog:HTMLElement){const tray=dialog.querySelector<HTMLElement>('#toast-tray');if(tray)placeTray(tray);
  for(const live of liveRegions.values())if(live.parentElement===dialog)topModal().append(live);}
/** An action failed: say why, unless the signed-in account changed meanwhile (then the result is simply dropped). */
// Only a discarded result (PAGE_LEFT, SESSION_CHANGED) is dropped; anything else thrown (no message, not an Error) is still said.
export function reportError(e:unknown,prefix=''){if(!sessionChanged(e))notify(settledWords(e)??prefix+((e as Error|undefined)?.message||String(e)),'error',undefined,e);}
/** A rota decision refused because the rota moved on meanwhile (api.ts re-read it into details.current): what happened,
 *  in one sentence, instead of the generic "another session changed this record". null for any other error. */
export function settledWords(e:unknown):string|null{
  const w=((e as {details?:{current?:Record<string,any>}})?.details)?.current;if(!w)return null;
  const note=w.return_note as Record<string,any>|null,when=(at:unknown)=>typeof at==='string'?' at '+departmentDateTime(at):'';
  // A return and a submission carry their time; a reopen, an approval or an edit carry none on the rota (meanwhile).
  const what=w.status==='draft'?(note?`${note.by_name||'An approver'} returned it to the coordinator${when(note.at)}`:'It was reopened as a draft meanwhile')
    :w.status==='submitted'?`It was submitted for review${when(w.submitted_at)}`
    :w.status==='approved'?'It was approved meanwhile'
    :['published','superseded'].includes(w.status)?'It was published meanwhile'
    :'It changed meanwhile';
  return `${what}. Nothing was saved; the page shows it as it is now.`;
}
/** Newest toast on top, at most 2 (phone) or 3 showing. Older plain messages go; older Undo toasts wait, hidden,
 *  until they expire or a slot frees up. The tray sits above any bar pinned to the bottom of the screen. */
function stackToasts(tray:Element){
  const max=phoneQuery.matches?2:3,items=[...tray.children] as HTMLElement[];
  let shown=0;
  // The message focus is in stays shown (WCAG 2.2.1), even when newer ones arrive.
  for(const t of items){if(shown<max||t.contains(document.activeElement)){t.hidden=false;shown++;}else if(t.classList.contains('with-action')){if(!t.hidden)t.hidden=true;}else t.remove();}
  // Only the bars of the tray's own host count: in a dialog its footer, on the page the page's bars (not a dialog's).
  const host=tray.parentElement||document.body,inDialog=host!==document.body;
  let lift=0;
  for(const bar of host.querySelectorAll<HTMLElement>('.selection-bar,.app-footer,.unsaved-bar,.form-footer,.auto-foot')){
    if(!inDialog&&bar.closest('dialog'))continue;
    const r=bar.getBoundingClientRect();
    if(r.height&&r.top>innerHeight/2&&r.top<innerHeight)lift=Math.max(lift,innerHeight-r.top);
  }
  (tray as HTMLElement).style.setProperty('bottom',lift?`${lift+8}px`:'');
  // Any side panel open beside the page (the shared docked one or a page's own, such as Auto-schedule): the messages sit
  // over the page column, never over the panel's controls. A phone's full-width sheet starts at the left edge: not beside.
  let left=innerWidth;
  if(!inDialog)for(const p of document.querySelectorAll<HTMLElement>('.side-panel:not([hidden],.slot-covered)')){
    const l=p.getBoundingClientRect().left;if(!p.closest('dialog')&&p.getClientRects().length&&l>innerWidth/3)left=Math.min(left,l);}
  (tray as HTMLElement).style.setProperty('right',left<innerWidth?`${innerWidth-left+20}px`:'');
}
/** While messages show, a panel docking or opening or a bar appearing (a redraw) moves them out of the way, once per frame. */
let layoutWatch:MutationObserver|null=null;
function followLayout(tray:HTMLElement){
  if(layoutWatch)return;let queued=false;
  layoutWatch=new MutationObserver(()=>{if(queued)return;queued=true;requestAnimationFrame(()=>{queued=false;
    if(!tray.isConnected||!tray.childElementCount){layoutWatch?.disconnect();layoutWatch=null;return;}stackToasts(tray);});});
  // A page's own panel opens by losing its hidden attribute, not by being added.
  layoutWatch.observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['hidden']});
}
/** Ctrl/⌘ Z outside a text field undoes the newest change that offers Undo (its message's own button), as in any editor
 *  (owner decision OD-8); Ctrl/⌘ Shift Z and Ctrl Y redo (SW3-A-06): the newest message's Redo, else the page's Redo
 *  button (`data-shortcut="redo"`). Inside a field they stay the field's own. */
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('keydown',e=>{
  const key=e.key.toLowerCase(),redo=key==='z'&&e.shiftKey&&(e.ctrlKey||e.metaKey)||key==='y'&&e.ctrlKey&&!e.shiftKey&&!e.metaKey;
  if(!redo&&(key!=='z'||!(e.ctrlKey||e.metaKey)||e.shiftKey)||e.altKey||e.defaultPrevented)return;
  const t=e.target as HTMLElement|null;
  if(t?.closest?.('input,textarea,select,[contenteditable=""],[contenteditable=true]'))return;
  const label=redo?'Redo':'Undo';
  const b=[...document.querySelectorAll<HTMLButtonElement>('#toast-tray .toast:not([hidden]) .toast-action')].find(b=>b.textContent===label)
    // The page's Redo only while no dialog covers the page (it would act behind the dialog).
    ||(redo&&!document.querySelector('dialog[open]')?document.querySelector<HTMLButtonElement>('button[data-shortcut="redo"]:not([disabled],[aria-disabled=true])'):null);
  if(!b)return;
  e.preventDefault();announce(redo?'Redoing the last undone change':'Undoing the last change');b.click();
});
/** The messages' key (issue 1408; WCAG 2.1.1, 2.4.3): from anywhere it focuses the newest message's action (its Undo),
 *  else the newest message's Dismiss; Escape in the messages, Undo or Dismiss go back to the control focus came from.
 *  Not F6 (the browser's own move to its address bar) nor Ctrl Shift with a letter (taken by browsers). In a text field
 *  only a key that types nothing (Alt M on Windows and Linux; Option M types µ on a Mac, so there it works outside one). */
export const MESSAGES_KEY='Alt M';
/** Where focus was before it entered the messages (by this key or by Tab). */
let trayFrom:Opener|null=null;
function backFromTray(){const o=trayFrom;trayFrom=null;if(o)returnFocus(o);else if(focusLost())document.querySelector<HTMLElement>('#main')?.focus({preventScroll:true});}
if(typeof window!=='undefined'&&typeof document!=='undefined'&&typeof document.addEventListener==='function'){
  document.addEventListener('focusin',e=>{const tray=document.querySelector('#toast-tray'),from=e.relatedTarget as HTMLElement|null;
    if(!tray||!from)return;const inside=tray.contains(e.target as Node);
    // Only a keyboard arrival (:focus-visible): a click on Undo never scrolls the page back to a control focused earlier.
    if(inside&&!tray.contains(from))trayFrom=(e.target as Element).matches(':focus-visible')?openerOf(from):null;
    else if(!inside&&tray.contains(from))trayFrom=null;});
  window.addEventListener('keydown',e=>{
    const tray=document.querySelector('#toast-tray');
    // Before a dialog's or panel's own Escape: the person leaves the messages, not the dialog.
    if(e.key==='Escape'&&trayFrom&&tray?.contains(e.target as Node)){e.preventDefault();e.stopImmediatePropagation();backFromTray();return;}
    if(!e.altKey||e.ctrlKey||e.metaKey||e.shiftKey||e.defaultPrevented||e.code!=='KeyM'&&e.key.toLowerCase()!=='m')return;
    if((e.target as HTMLElement|null)?.closest?.('input,textarea,select,[contenteditable=""],[contenteditable=true]')&&e.key.toLowerCase()!=='m')return;
    e.preventDefault();
    const to=tray?.querySelector<HTMLElement>('.toast:not([hidden]) .toast-action')||tray?.querySelector<HTMLElement>('.toast:not([hidden]) .toast-close');
    if(!to){announce('No messages');return;}
    // Chromium does not count a key with Alt as keyboard use for :focus-visible, so the key shows the ring and keeps
    // where focus came from itself (focusin above only sees a plain focus here).
    const from=document.activeElement as HTMLElement|null;
    const ring:FocusOptions&{focusVisible?:boolean}={focusVisible:true};to.focus(ring);
    if(from&&!tray!.contains(from))trayFrom=openerOf(from);
  },true);
}
/** The screen's own status for the footer (left side): rota health, counts, a score. A page puts it anywhere in its
 *  content; the shell moves it into the footer when that page is shown, so a page that finished loading after you left
 *  never writes into another page's footer. Keep the node to update it later. An empty one keeps the footer clear.
 *  Without one, a page with a single list shows that list's row count there. */
export const footerStatus=(...children:Child[])=>el('div',{class:'footer-status'},...children);
/** The page the shell is building, detached until it is shown (app.ts renderPage): its reads may answer before then. */
let pageBuilding:HTMLElement|null=null;
export const buildingPage=(content:HTMLElement|null)=>{
  // The legend can paint off-page. Fit it on mount before the first resize callback.
  if(!content&&pageBuilding?.isConnected)fitLegends(pageBuilding);
  pageBuilding=content;
};
/** Whether a late answer may still draw into `node`: it is on screen, or in the page being built. Never `isConnected`
 *  alone, which drops an answer that lands before the page is shown; a replaced page's detached nodes stay gone. */
export const alive=(node:Node|null|undefined)=>!!node&&(node.isConnected||!!pageBuilding?.contains(node));
/* maintenance: footer controls (rota health, score, overlap, proposal, Needs a check) and the Schedule's Auto-schedule button
 * act through one delegated handler, never a listener of their own. The footer and the top bar redraw often (a check finishing, a live update, a save): a press that began on a
 * pill the redraw replaced still acts, on the same control (`key`) drawn again under the pointer, whether or not the
 * browser still sends a click; and a control lent to the "+N" list keeps its action. */
const footerActs=new WeakMap<Element,(e:MouseEvent)=>unknown>();
let footerWired=false;
export function footerAct<T extends HTMLElement>(b:T,key:string,act:(e:MouseEvent)=>unknown):T{
  footerActs.set(b,act);b.dataset.footerAct=key;
  if(!footerWired){footerWired=true;wireFooterActs();}
  return b;
}
function wireFooterActs(){
  const at=(n:EventTarget|null)=>n instanceof Element?n.closest<HTMLElement>('[data-footer-act]'):null;
  const run=(b:HTMLElement|null,e:MouseEvent)=>{const act=b&&!(b as HTMLButtonElement).disabled&&b.getAttribute('aria-disabled')!=='true'?footerActs.get(b):undefined;
    if(!act)return false;try{const r=act(e);if(r instanceof Promise)r.catch(reportError);}catch(error){reportError(error);}return true;};
  // `ran`: the press already acted on pointer-up; the click the same input may still send (it follows in the same task) is not a second press.
  let down:{node:HTMLElement;key:string}|null=null,ran=false;
  document.addEventListener('pointerdown',e=>{const b=at(e.target);down=b?{node:b,key:b.dataset.footerAct||''}:null;},true);
  document.addEventListener('pointerup',e=>{const d=down;down=null;if(!d||d.node.isConnected)return;
    const b=at(document.elementFromPoint(e.clientX,e.clientY));if(b?.dataset.footerAct===d.key&&run(b,e)){ran=true;setTimeout(()=>{ran=false;});}},true);
  document.addEventListener('click',e=>{if(ran){ran=false;return;}run(at(e.target),e);});
}
/** A screen's key in the footer: one "? Key" button that opens the explanation above it (never a long legend in the row).
 *  Status items that do not fit the footer also go behind it ("? +N", see fitFooter); without a key it is that only. */
export function footerKey(build?:(panel:HTMLElement)=>void,label='Key'){
  const b=el('button',{type:'button',class:'footer-key','aria-haspopup':'dialog','aria-expanded':'false','data-tip':'What the marks mean'},icon('help'),el('span',{},label));
  b.dataset.label=build?label:'';
  b.addEventListener('click',()=>popover(b,panel=>{panel.classList.add('footer-key-pop');lendOverflow(b,panel);build?.(panel);},{label:build?label:'More status',placement:'above-start'}));
  return b;
}
/** Called by the shell when a page is shown. */
export function liftFooterStatus(content:HTMLElement){
  const slot=document.querySelector<HTMLElement>('.app-footer .page-status');if(!slot)return;
  const own=content.querySelector<HTMLElement>('.footer-status'),lists=content.querySelectorAll('.dt');
  // A page's one list says its count in the footer only (E13), first, before the page's own status.
  const count=lists.length===1?lists[0].querySelector<HTMLElement>('.dt-count'):null;
  // A list's count travels with what follows it there (after a bulk change: Undo last change and History).
  const extra=count?.parentElement?.querySelector<HTMLElement>(':scope>.dt-foot-extra');
  if(own&&count&&!own.querySelector('.dt-count'))own.prepend(count);
  slot.replaceChildren(...(own?[own]:count?[footerStatus(count,extra)]:[]));
  watchFooter(slot);
}
/* Footer overflow reviewed: the screen's status shows every item inline while it fits; as the
 * width shrinks, items that no longer fit (the last first) go behind the key button, which then reads "? +N" and lists
 * them, the same nodes with their icons, words and actions, above its key. A phone keeps the first item and the button.
 * Real widths: each step measures the row at its natural width (`.measuring`). */
const OVER='footer-over',watched=new WeakSet<HTMLElement>(),FOOTER_MORE:MoreWords={noun:['status item','status items']};
// The first item (a list's count, a rota's health pills as one) always stays.
const statusItems=(status:HTMLElement)=>[...status.children].flatMap(n=>n.classList.contains('footer-key')?[]:n.classList.contains('health-pills')?[...n.children]:[n])
  .filter(n=>n.children.length||n.textContent?.trim()) as HTMLElement[];
function fitFooter(slot:HTMLElement){
  const status=slot.querySelector<HTMLElement>(':scope>.footer-status');
  if(!status||status.dataset.lending!==undefined)return;
  const items=statusItems(status);items.forEach(i=>i.classList.remove(OVER));
  let key=status.querySelector<HTMLElement>(':scope>.footer-key');
  const focused=!!key&&document.activeElement===key;  // re-measuring hides the key for a moment: keep its focus
  const paintKey=(n:number)=>{
    if(!key&&!n)return;
    key??=status.appendChild(footerKey());
    const label=key.dataset.label||'';key.hidden=!n&&!label;
    key.lastElementChild!.textContent=n?`+${n}`:label;  // the button carries the "+N"'s full name
    if(n)key.setAttribute('aria-label',moreName(n,{...FOOTER_MORE,of:label?'and the '+label.toLowerCase():''}));else key.removeAttribute('aria-label');
    key.dataset.tip=n?`${n} more${label?' · '+label:''}`:'What the marks mean';
  };
  // A phone keeps the main status: the first item, or a rota's health pills.
  const phone=phoneQuery.matches,main=(n:HTMLElement)=>n===items[0]||!!n.parentElement?.classList.contains('health-pills');
  const rest=items.slice(1).reverse();let hidden=0;
  for(const n of rest)if(phone&&!main(n)){n.classList.add(OVER);hidden++;}
  paintKey(hidden);
  status.classList.add('measuring');
  for(const n of rest){if(status.offsetWidth<=slot.clientWidth)break;if(!n.classList.contains(OVER)){n.classList.add(OVER);paintKey(++hidden);}}
  status.classList.remove('measuring');
  if(focused&&key&&!key.hidden)key.focus({preventScroll:true});
}
/** The key button's popover lists the hidden items (moved in while it is open, back in place when it closes). */
function lendOverflow(key:HTMLElement,panel:HTMLElement){
  const status=key.closest<HTMLElement>('.footer-status'),over=status?statusItems(status).filter(i=>i.classList.contains(OVER)):[];
  if(!status||!over.length)return;
  const spots=over.map(n=>{const spot=document.createComment('');n.before(spot);return spot;});
  panel.append(el('ul',{class:'footer-over-list','aria-label':'More status'},...over.map(n=>el('li',{},n))));
  status.dataset.lending='';
  const back=new MutationObserver(()=>{if(panel.isConnected)return;back.disconnect();
    over.forEach((n,i)=>spots[i].replaceWith(n));delete status.dataset.lending;const slot=status.parentElement;if(slot)fitFooter(slot);});
  queueMicrotask(()=>{if(panel.parentNode)back.observe(panel.parentNode,{childList:true});});
}
// One observer follows the current footer's status (counts and health update in place); the key's own "+N" is not a
// change. Width changes come through the status footer's ResizeObserver (trackStatusFooter).
let footerFrame=0;
const fitLater=()=>{cancelAnimationFrame(footerFrame);footerFrame=requestAnimationFrame(()=>{
  const slot=document.querySelector<HTMLElement>('.app-footer .page-status');if(slot)fitFooter(slot);footerChanges?.takeRecords();});};
// The key's own "+N" and the auto-schedule run strip's ticking clock (fixed widths) are not changes to fit again.
const inKey=(n:Node)=>!!(n instanceof Element?n:n.parentElement)?.closest('.footer-key,.asl-strip');
const footerChanges=typeof MutationObserver==='undefined'?null:new MutationObserver(records=>{if(records.some(r=>!inKey(r.target)))fitLater();});
function watchFooter(slot:HTMLElement){
  footerChanges?.disconnect();footerChanges?.observe(slot,{childList:true,subtree:true,characterData:true});
  fitFooter(slot);
}
let statusFooterStops:(()=>void)[]=[];
/** One observer follows the current shell's wrapped header, footer and action bars. */
export function trackStatusFooter(footer:HTMLElement|null){
  for(const stop of statusFooterStops)stop();statusFooterStops=[];
  if(!footer){document.documentElement.style.removeProperty('--status-footer-height');document.documentElement.style.removeProperty('--bottom-actions-height');document.documentElement.style.removeProperty('--topbar-height');document.documentElement.style.removeProperty('--sticky-key-height');document.documentElement.style.removeProperty('--planner-toolbar-height');return;}
  const topbar=document.querySelector<HTMLElement>('.topbar'),bars=[...document.querySelectorAll<HTMLElement>('.selection-bar,.auto-foot')];
  // The Planner's key, pinned at the bottom (app.css .page-planner .view-legend): what is scrolled into view stops above it.
  const key=[...document.querySelectorAll<HTMLElement>('.page-planner .view-legend')].find(k=>getComputedStyle(k).position==='sticky');
  // The Planner's toolbar, pinned under the top bar while the page scrolls: the schedule's own sticky heads stack under it.
  const tools=document.querySelector<HTMLElement>('.page-planner .planner-main>.planner-toolbar');
  const place=()=>{fitLater();document.documentElement.style.setProperty('--status-footer-height',`${footer.getBoundingClientRect().height}px`);
    if(topbar)document.documentElement.style.setProperty('--topbar-height',`${topbar.getBoundingClientRect().height}px`);
    document.documentElement.style.setProperty('--bottom-actions-height',`${Math.max(0,...bars.map(bar=>bar.getBoundingClientRect().height))}px`);
    document.documentElement.style.setProperty('--sticky-key-height',`${key?.isConnected?key.getBoundingClientRect().height:0}px`);
    document.documentElement.style.setProperty('--planner-toolbar-height',`${tools?.isConnected?tools.getBoundingClientRect().height:0}px`);
    const tray=document.querySelector('#toast-tray');if(tray)stackToasts(tray);};
  // The observer's first report (after layout) places them; measuring now would force a layout of the whole new page.
  // Several boxes report in one frame: placed once, after them all.
  let owed=false;const placeOnce=()=>{if(owed)return;owed=true;queueMicrotask(()=>{owed=false;place();});};
  statusFooterStops=[footer,...bars,...(topbar?[topbar]:[]),...(key?[key]:[]),...(tools?[tools]:[])].map(node=>observeSize(node,placeOnce));
}
// A reversible save the screen did not report: say so, with Undo.
if (typeof window !== 'undefined') window.addEventListener('undo-unclaimed', e => notify(String((e as CustomEvent).detail) + '.'));
/** The live regions announce() keeps (one each; a removed one is put back) and where they belong: the top modal, else the page. */
const liveRegions = new Map<string, HTMLElement>();
const topModal = (): HTMLElement => { const modals = document.querySelectorAll<HTMLElement>('dialog:modal'); return modals[modals.length - 1] || document.body; };
/** Say something to screen readers only; `polite` waits for what is being said (a form's problem after its focus move). */
export function announce(message: string, polite = false) {
  const id = polite ? 'sr-live-polite' : 'sr-live';
  // An open modal dialog makes the rest of the page inert, so each region lives in the top one, as toasts do (trayHost),
  // and goes back as it closes (releaseToasts). One node per region: one a removal took is put back, never a second.
  const live = liveRegions.get(id) || document.getElementById(id) || el('div',{id,class:'sr-only','aria-live':polite?'polite':'assertive'});
  liveRegions.set(id, live);
  const into = topModal(); if (live.parentElement !== into) into.append(live);
  live.textContent = ''; window.setTimeout(() => { live.textContent = message; }, 30);
  // SW3-D-10: cleared once said ("Press again to confirm: Discard" no longer lingers after the page moved on).
  window.setTimeout(() => { if (live.textContent === message) live.textContent = ''; }, LIVE_CLEAR_MS);
}
/** A settings page's one save control: a bar pinned above the status footer while `save` (the page's Save, disabled
 *  until something changes) is enabled, so a long form (Auto-schedule & scoring on a phone) saves from anywhere and
 *  shows it is unsaved. Save opens the page's reason dialog; Discard (same-button confirm) draws the page again. */
export function saveBar(save:HTMLButtonElement,discard:()=>unknown):HTMLElement{
  save.classList.add('small');
  const bar=el('div',{class:'form-footer save-bar',role:'region','aria-label':'Unsaved changes'},el('span',{class:'save-bar-state'},el('span',{class:'dirty-dot','aria-hidden':'true'}),'Unsaved changes'),
    armed(button('Discard',discard,'ghost small'),'Discard?'),save);
  const sync=()=>{bar.hidden=save.disabled;};sync();
  new MutationObserver(sync).observe(save,{attributes:true,attributeFilter:['disabled']});
  return bar;
}
/** Same-button confirm, only for an action that cannot be undone (D42; a reversible one acts at once with Undo): the
 *  first press arms it (solid red, "Confirm?"), the second press acts. No time limit (WCAG 2.2.1): it stays armed until
 *  the mouse pointer moves off it, focus leaves it, Escape, or a tap elsewhere; it keeps at least its width so a shorter
 *  "Confirm?" never slides out from under the pointer. Works on any button, menu item or submit button. `when`: for a
 *  button whose press is irreversible only sometimes (a preview found a loss), it arms only while this says so. */
export function armed<T extends HTMLElement>(b: T, confirmLabel = 'Confirm?', when?: () => boolean): T {
  let saved: Node[] | null = null, name: string | null = null, tip: string | undefined;
  // A tap anywhere else disarms it: a touch browser may not move focus off a tapped button (iOS Safari never focuses one).
  const elsewhere = (e: Event) => { if (!b.contains(e.target as Node | null)) disarm(); };
  const disarm = () => {
    if (!saved) return;
    document.removeEventListener('pointerdown', elsewhere, true); b.replaceChildren(...saved); saved = null; b.classList.remove('armed'); b.style.removeProperty('min-width'); delete b.dataset.armedFrom;
    if (name === null) b.removeAttribute('aria-label'); else b.setAttribute('aria-label', name);
    if (tip !== undefined) b.dataset.tip = tip;
  };
  b.addEventListener('click', e => {
    if (saved) { disarm(); return; }  // the second click: let the action run
    if (when && !when()) return;
    // A submit button with an incomplete form lets the browser say what is missing first.
    if (b instanceof HTMLButtonElement && b.type === 'submit' && b.form && !b.form.checkValidity()) return;
    e.stopImmediatePropagation(); e.preventDefault();
    name = b.getAttribute('aria-label'); tip = b.dataset.tip;
    const plain = (name || b.textContent || '').trim();
    b.style.setProperty('min-width', b.getBoundingClientRect().width + 'px');
    saved = [...b.childNodes];
    b.replaceChildren(icon('alert'), el('span', {class: 'armed-label'}, confirmLabel));
    // "Confirm retract?" already names the action: its name is not "Confirm retract? Retract".
    const said = confirmLabel.toLowerCase().includes(plain.toLowerCase()) ? confirmLabel : `${confirmLabel} ${plain}`;
    b.classList.add('armed'); b.setAttribute('aria-label', said); b.dataset.armedFrom = plain;
    if (tip !== undefined) b.dataset.tip = said;
    announce(`Press again to confirm: ${plain}`);
    document.addEventListener('pointerdown', elsewhere, true);
    // F16 (T2): no time limit; a press that quietly undid itself read as nothing happening. Leaving, focus or Escape disarm.
  }, {capture: true});
  // Leaving means changing one's mind. Touch has no hover: a finger lifting is not the pointer leaving. A leave at the
  // pointer's last position is the button moving (a layout change above or below it), not the user: it stays armed.
  let at = '';
  const track = (e: PointerEvent) => { at = `${e.clientX},${e.clientY}`; };
  b.addEventListener('pointermove', track); b.addEventListener('pointerdown', track);
  b.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse' && `${e.clientX},${e.clientY}` !== at) disarm(); });
  b.addEventListener('focusout', e => { if (!b.contains(e.relatedTarget as Node | null)) disarm(); });
  b.addEventListener('keydown', e => { if (e.key === 'Escape' && saved) { e.preventDefault(); e.stopPropagation(); disarm(); } });
  return b;
}
/** The field's own minlength (one number, the input's), counted without outer spaces; the words are problemWords'. */
export const reasonProblem=(value:string,min:number)=>value&&value.trim().length<min?`At least ${min} characters`:'';
/** RUL-E04: one person's reason to be assigned against a rule, then the same-button armed confirm: the fill panel's
 *  "Break rule and assign", one command with one reason for one person. */
export function reasonForm(name:string,act:(reason:string,form:HTMLFormElement)=>void):HTMLFormElement{
  const reason=el('input',{type:'text',required:true,minlength:3,maxlength:500,'aria-label':`Reason to assign ${name} against the rule`,placeholder:'Reason (required)'}) as HTMLInputElement;
  // The reason counts without its outer spaces (as the server reads it): spaces alone are neither armed nor sent as no reason.
  reason.addEventListener('input',()=>reason.setCustomValidity(reasonProblem(reason.value,reason.minLength)));
  const go=armed(button('Break rule and assign',()=>{},'danger small',{type:'submit'}),'Confirm?');
  const form=el('form',{class:'wk-override'},reason,go,el('p',{class:'wk-override-error',role:'alert'})) as HTMLFormElement;
  form.addEventListener('submit',e=>{e.preventDefault();if(form.reportValidity())act(reason.value.trim(),form);});
  return form;
}
type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
const isControl = (t: Element): t is Control => t instanceof HTMLInputElement || t instanceof HTMLSelectElement || t instanceof HTMLTextAreaElement;
const valueOf = (c: Control) => c instanceof HTMLInputElement && (c.type === 'checkbox' || c.type === 'radio') ? String(c.checked)
  : c instanceof HTMLSelectElement && c.multiple ? [...c.selectedOptions].map(o => o.value).join('\n') : c.value;
/** Unsaved-changes guard for a dialog or popover. tryClose() closes at once when nothing changed since it opened;
 *  otherwise the panel shakes and a red bar asks "Not saved. Close anyway?" with Keep editing / Discard changes.
 *  Changes: a field whose value differs from when it opened, a picker choice, or an Add…/Remove…/toggle button
 *  (search boxes and row selection do not count).
 *  A successful form save counts as its new starting point; unrelated commands never clear unsaved edits.
 *  `formsOnly` counts only what is inside a <form> (popovers whose other controls apply at once, like filters). */
export function guard(root: HTMLElement, close: () => void, formsOnly = false) {
  const initial = new Map<Element, string>(), touched = new Set<Element>();
  let bar: HTMLElement | null = null, afterClose=close;
  // Searching, ticking rows to act on and lookup choices ([data-lookup], e.g. Which rule applies?) change nothing.
  const counts = (t: Element) => (!formsOnly || !!t.closest('form')) && !t.matches('[type=search],.bulk-check,[data-lookup],[data-lookup] *');
  const snap = () => { for (const c of root.querySelectorAll<Control>('input,select,textarea')) if (!initial.has(c) && counts(c)) initial.set(c, valueOf(c)); };
  const reset = (within:Element=root) => {
    for(const c of initial.keys())if(within===root||within.contains(c))initial.delete(c);
    for(const t of touched)if(within===root||within.contains(t))touched.delete(t);
    snap(); bar?.remove(); bar = null; afterClose=close;
  };
  snap(); requestAnimationFrame(snap);
  root.addEventListener('focusin', snap);  // fields drawn later are remembered before they are edited
  const mark = (e: Event) => { const t = e.target as Element; if (counts(t)) touched.add(t); };
  root.addEventListener('input', mark); root.addEventListener('change', mark);
  root.addEventListener('click', e => {
    const b = (e.target as Element).closest('button');
    // Opening a separate form (e.g. Add rank from a person's panel) is not an edit to this panel.
    const opened=document.activeElement?.closest('dialog[open],.dock-panel');
    if(opened&&!root.contains(document.activeElement))return;
    // A form's submit button ("Add location type") saves; pressing it, even when the browser refuses an empty form, is no edit.
    if (b && root.contains(b) && counts(b) && b.type !== 'submit' && (b.hasAttribute('aria-pressed') || /^(add|remove|move)\b/i.test(b.getAttribute('aria-label') || b.textContent?.trim() || ''))) touched.add(b);
  });
  const saved=(e:Event)=>{e.stopPropagation();if(e.target instanceof Element)reset(e.target);};
  root.addEventListener('rota-form-saved',saved);
  const dispose=()=>root.removeEventListener('rota-form-saved',saved);
  const dirty = () => [...touched].some(t => !t.isConnected || !isControl(t) || !initial.has(t) || valueOf(t) !== initial.get(t));
  function tryCloseThen(next:()=>void) {
    afterClose=next;
    if (!dirty()) { afterClose=close; next(); return; }
    root.classList.remove('shake'); void root.offsetWidth; root.classList.add('shake');
    if (bar) { bar.querySelector('button')?.focus(); return; }
    const made = unsavedBar(' Close anyway?',
      () => { bar?.remove(); bar = null; afterClose=close; ([...touched].find(t => t.isConnected && t instanceof HTMLElement) as HTMLElement | undefined)?.focus(); },
      () => { bar?.remove(); bar = null; const next=afterClose; afterClose=close; next(); });
    bar = made.bar; root.append(bar); made.keep.focus();
  }
  return {tryClose:()=>tryCloseThen(close), tryCloseThen, dirty, reset, dispose};
}
/** The one unsaved-changes choice, for a dialog or panel (guard) and for leaving a page (leaveGuard): "Not saved." and
 *  what happens, Keep editing (first, focused) and Discard changes — or Leave, when what is unsaved is kept anyway. */
function unsavedBar(text: string, keep: () => void, discard: () => void, leaveLabel = 'Discard changes') {
  const k = el('button', {type: 'button', class: 'button secondary small'}, 'Keep editing');
  const d = el('button', {type: 'button', class: `button ${leaveLabel === 'Discard changes' ? 'danger' : 'secondary'} small`}, leaveLabel);
  k.addEventListener('click', keep); d.addEventListener('click', discard);
  return {bar: el('div', {class: 'unsaved-bar', role: 'alert'}, icon('alert'), el('span', {}, el('strong', {}, 'Not saved.'), text), k, d), keep: k};
}
/** Leaving a page with unsaved changes (Settings, Rules drafts): the same choice as a dialog's guard, in a small modal that
 *  names where you were going. `kept` (the page keeps its draft, e.g. Rules) offers Leave instead of Discard changes. */
export function leaveGuard(where: string, go: () => void, detail = '', kept = false) {
  let keep: HTMLElement | null = null;
  const d = showDialog('Leave this page?', '', (body, close) => {
    const made = unsavedBar(' ' + (detail || `Your changes on this page are lost if you go to ${where}.`), close, () => { close(); go(); }, kept ? `Go to ${where}` : 'Discard changes');
    keep = made.keep; body.append(made.bar);
  });
  d.dialog.classList.add('leave-dialog'); keep!.focus();
  return d;
}
export interface Field<T extends HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement = HTMLInputElement> {wrap: HTMLElement; input: T}
let fieldSequence = 0;
/** Every form's problems (WCAG 3.3.1, 1.4.1, 4.1.3): what the browser would show as a passing bubble shows as a message
 *  under its field instead (aria-invalid, aria-describedby), the first problem gets focus and is said politely, and the
 *  message goes as soon as the field is right. A value left wrong is said on leaving the field (maintenance); an empty
 *  required field waits for Save, so tabbing through a form raises nothing. Typing is never blocked. Installed once by
 *  the first field built; quiet checks (checkValidity) stay quiet. */
let errorsWatched = false, quiet = 0, firstProblem: Control | null = null, problems = 0;
function watchErrors() {
  if (errorsWatched || typeof document === 'undefined') return; errorsWatched = true;
  // shortcut: checkValidity fires the same 'invalid' event as reportValidity; a counter tells them apart.
  for (const P of [HTMLInputElement, HTMLSelectElement, HTMLTextAreaElement, HTMLFormElement, HTMLFieldSetElement]) {
    const own = P.prototype.checkValidity;
    P.prototype.checkValidity = function (this: HTMLElement) { quiet++; try { return own.call(this); } finally { quiet--; } };
  }
  document.addEventListener('invalid', e => {
    const c = e.target as Element;
    if (quiet || !isControl(c)) return;
    e.preventDefault(); showProblem(c); problems++;
    // Every problem of this turn (a whole form, or several checks in a row) is shown; the first on the page gets focus.
    if (!firstProblem) { firstProblem = c; setTimeout(() => { const f = firstProblem, n = problems; firstProblem = null; problems = 0;
      if (f?.isConnected) { focusProblem(f); announce(n > 1 ? `${n} fields to fix. ${problemSaid(f)}` : problemSaid(f), true); } }); }
    else if (firstProblem.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_PRECEDING) firstProblem = c;
  }, true);
  const recheck = (e: Event) => { const c = e.target as Element; if (isControl(c) && c.getAttribute('aria-invalid') === 'true') { if (c.checkValidity()) clearProblem(c); else showProblem(c); } };
  document.addEventListener('input', recheck); document.addEventListener('change', recheck);
  document.addEventListener('focusout', e => { const c = e.target as Element;
    if (!isControl(c) || !c.closest('form,.field') || c.type === 'checkbox' || c.type === 'radio' || c.getAttribute('aria-invalid') === 'true' || c.checkValidity() || c.validity.valueMissing) return;
    showProblem(c); announce(problemSaid(c), true); });
}
const problemHost = (c: Control) => (c.closest('.date-range,.field,.checkbox,.pick-field,fieldset') || c.parentElement) as HTMLElement;
/** A control wrapped in its <label> (check(), a date inside a labelled row) keeps its message inside that label, so the
 *  layout around it never gains a cell (a grid of dates, a row of boxes); the control is then named by the label's own
 *  parts (aria-labelledby, pinName), so the message is only ever its description, never part of its name (WCAG 4.1.2). */
const namingLabel = (c: Control) => { const l = c.closest('label'); return l?.control === c ? l : null; };
/** A wrapped control's name made explicit: every visible part of its label in order, except the control, other controls
 *  (a date's "Choose date" button), messages and hidden parts (loose words get a span to be referred to). Live: a part's
 *  text can still change. The date picker pins its field as soon as it is built, so the name is the same before and
 *  after a problem. */
export function pinName(c: Control, label: HTMLElement | null = namingLabel(c)) {
  if (!label) return;
  if (c.hasAttribute('aria-label') || c.hasAttribute('aria-labelledby')) return;
  const ids: string[] = [];
  const walk = (n: Element) => { for (let k of [...n.childNodes]) {
    if (k === c) continue;
    if (k.nodeType === Node.TEXT_NODE) { if (!k.textContent?.trim()) continue; const s = el('span', {}); k.replaceWith(s); s.append(k); k = s; }
    if (!(k instanceof HTMLElement) || k.hidden || k.classList.contains('field-error') || k.getAttribute('aria-hidden') === 'true') continue;
    if (!k.contains(c) && k.matches('button,input,select,textarea,a[href],[role=button],[tabindex]')) continue;
    if (k.contains(c)) walk(k); else ids.push(k.id ||= `field-name-${++fieldSequence}`);
  } };
  walk(label);
  if (ids.length) c.setAttribute('aria-labelledby', ids.join(' '));
}
function problemNote(c: Control): HTMLElement | null {
  const own = (c.getAttribute('aria-describedby') || '').split(' ').map(id => id ? document.getElementById(id) : null).find(n => n?.classList.contains('field-error'));
  return own || (namingLabel(c) || problemHost(c))?.querySelector<HTMLElement>(':scope > .field-error[data-auto]') || null;
}
function showProblem(c: Control) {
  const label = namingLabel(c);
  pinName(c, label);
  let message = problemNote(c);
  if (!message) { message = el('small', {class: 'field-error', id: `field-error-${++fieldSequence}`, 'data-auto': ''}); (label || problemHost(c))?.append(message); }
  if (!(c.getAttribute('aria-describedby') || '').split(' ').includes(message.id)) c.setAttribute('aria-describedby', [c.getAttribute('aria-describedby'), message.id].filter(Boolean).join(' '));
  message.textContent = problemWords(c); message.hidden = false; c.setAttribute('aria-invalid', 'true');
}
/** A field's name as its label says it, without a qualifier ("Private reason · no patient information" → "Private reason")
 *  or the form's (optional)/(required) cue. */
function fieldName(c: Control): string {
  const label = (c.id && document.querySelector(`label[for="${CSS.escape(c.id)}"]`)) || problemHost(c)?.querySelector(':scope>label,:scope>legend,:scope>.pick-label,:scope>.field-label');
  return (c.getAttribute('aria-label') || label?.textContent || '').split(' · ')[0].replace(/\s*\((optional|required)\)\s*$/i, '').trim();
}
/** A problem said aloud: the field's name, then what is wrong ("Email: Enter an email address, like name@example.org"). */
const problemSaid = (c: Control) => { const name = fieldName(c), words = problemWords(c); return name && !words.toLowerCase().includes(name.toLowerCase()) ? `${name}: ${words}` : words; };
/** A field's problem in the field's own words: "Choose a leave type", "Add a reason", "At least 3
 *  characters", not the browser's generic bubble text. A message the form set itself (setCustomValidity) stays as it is. */
function problemWords(c: Control): string {
  const v = c.validity;
  if (v.customError) return c.validationMessage;
  const name = fieldName(c);
  // Mid-sentence the label starts lower case, unless it starts with an acronym ("GMC number").
  const noun = name && !/^[A-Z]{2}/.test(name) ? name.charAt(0).toLowerCase() + name.slice(1) : name;
  const article = noun ? (/^[aeiou]/i.test(noun) ? 'an ' : 'a ') + noun : '';
  const choice = c instanceof HTMLSelectElement || c.type === 'radio' || c.type === 'checkbox' || c.type === 'hidden' || !!c.closest('.pick-field,.pick');
  if (v.valueMissing) return c.type === 'checkbox' && !c.closest('.pick-field,.pick') ? `Tick ${name ? '“' + name + '”' : 'this box'} to continue` : noun ? `${choice ? 'Choose' : 'Add'} ${article}` : c.validationMessage;
  if (v.tooShort) return `At least ${c.getAttribute('minlength')} characters`;
  if (v.tooLong) return `At most ${c.getAttribute('maxlength')} characters`;
  if (v.rangeUnderflow) return `From ${c.getAttribute('min')}`;
  if (v.rangeOverflow) return `Up to ${c.getAttribute('max')}`;
  if (v.badInput || v.typeMismatch) return c.type === 'email' ? 'Enter an email address, like name@example.org' : c.type === 'url' ? 'Enter a web address, like https://example.org'
    : c.type === 'number' ? 'Enter a number' : c.type === 'date' ? 'Enter a date' : c.validationMessage;
  if (v.patternMismatch && c.title) return c.title;
  return c.validationMessage;
}
function clearProblem(c: Control) {
  c.removeAttribute('aria-invalid');
  const message = problemNote(c);
  if (message?.dataset.auto === undefined) return;  // a rule's own message (inlineCheck) keeps itself up to date
  // A corrected field no longer describes itself with the old problem; others in the same group may still use it.
  c.setAttribute('aria-describedby', (c.getAttribute('aria-describedby') || '').split(' ').filter(id => id && id !== message.id).join(' '));
  if (!c.getAttribute('aria-describedby')) c.removeAttribute('aria-describedby');
  if (![...problemHost(c).querySelectorAll('[aria-invalid=true]')].length) { message.hidden = true; message.textContent = ''; }
}
/** The first problem: opened (a closed section, a hidden step is opened by its own form) and focused; a hidden value holder
 *  (a multiple-choice field) gives the focus to its button. */
function focusProblem(c: Control) {
  for (let d = c.closest('details'); d; d = d.parentElement?.closest('details') ?? null) d.open = true;
  const target = c.offsetParent || c === document.activeElement ? c : c.closest('.pick')?.querySelector<HTMLElement>('button') || problemHost(c)?.querySelector<HTMLElement>('input:not([type=hidden]),select,textarea,button');
  target?.focus(); target?.scrollIntoView({block: 'nearest'});
}
export function field(label: string, type = 'text', value: string | number = '', options: Record<string, unknown> = {}): Field {
  watchErrors();
  const id = `field-${++fieldSequence}`;
  const input = el('input',{id,name: options.name || label, type,value,...options});
  // Every date field is the shared date picker (date_picker.ts); its .value stays ISO yyyy-mm-dd. A date and time
  // ('datetime-local') is the same date field with a 24-hour time beside it; .value stays local ISO yyyy-mm-ddTHH:MM.
  const wrap = el('div',{class:'field'},el('label',{for:id},label),type==='date'?dateInput(input):type==='datetime-local'?dateTimeInput(input,label):type==='time'?timeInput(input):input);
  return {wrap,input};
}
/** A value the app suggests for a field (a rota name from its dates, a title from a rule's settings) until the person
 *  edits it: their typing owns the field (clearing it hands it back), and a late suggestion never mixes with it. One
 *  landing while the whole text is selected keeps it selected, so the typing that follows replaces it rather than
 *  joining it. Returns the setter: true when the suggestion was put in. */
export function suggested(input: HTMLInputElement, owned = !!input.value.trim()) {
  input.addEventListener('input', e => { if (e.isTrusted) owned = !!input.value.trim(); });
  return (value?: string) => {
    if (owned || !value || input.value === value) return false;
    const all = document.activeElement === input && input.selectionStart === 0 && input.selectionEnd === input.value.length;
    input.value = value; if (all) input.select();
    return true;
  };
}
/** A rule checked beside its field as it is typed (not only after Save): the message shows under the field, is its
 *  accessible description and its validity, so Save stops there too. Returns a re-check for when another field changes. */
export function inlineCheck<T extends HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>(f:Field<T>,validate:()=>string){
  const id=`field-error-${++fieldSequence}`,message=el('small',{class:'field-error',id,'aria-live':'polite'});
  const label=namingLabel(f.input),host=f.input.closest('.date-range')||label||f.wrap;pinName(f.input,label);host.append(message);
  f.input.setAttribute('aria-describedby',[f.input.getAttribute('aria-describedby'),id].filter(Boolean).join(' '));
  // After the field's own checks (a date that is not a date says so first).
  const run=()=>{const m=validate();if(m||f.input.validationMessage===message.textContent)f.input.setCustomValidity(m);message.textContent=f.input.validationMessage;message.hidden=!message.textContent;
    // The field is marked invalid while its message shows (assistive technology hears it with the field).
    if(message.textContent)f.input.setAttribute('aria-invalid','true');else if(f.input.validity.valid)f.input.removeAttribute('aria-invalid');};
  // A date field re-checks itself on every new day and on leaving it: it keeps this rule in that check.
  if(f.input instanceof HTMLInputElement&&f.input.classList.contains('date-text'))fieldRules.set(f.input,validate);
  f.input.addEventListener('input',run);f.input.addEventListener('change',run);queueMicrotask(run);
  return run;
}
export function area(label: string, value = '', rows = 3): Field<HTMLTextAreaElement> {
  watchErrors();
  const id = `field-${++fieldSequence}`, input = el('textarea',{id,name:label,rows,value});
  return {wrap:el('div',{class:'field span-2'},el('label',{for:id},label),input),input};
}
/** Every single-choice field. Options with a group are grouped; long lists get a search box that narrows them. */
/** Every single-choice dropdown. A native <select> holds the value (forms, tests and screen readers rely on it);
 *  clicking or typing on it opens the shared searchable picker (see picker.ts). */
export function select(label: string, choices: Choice[], value = '', required = true, emptyLabel = 'Not specified'): Field<HTMLSelectElement> {
  watchErrors();choices=distinct(choices);
  const id=`field-${++fieldSequence}`,input=el('select',{id,name:label,required});
  if(!required&&!choices.some(c=>c.value===''))input.append(el('option',{value:''},emptyLabel));
  // Required with nothing chosen: a "Choose…" placeholder, so an untouched form cannot quietly save the first option.
  // A single possible answer is chosen for you; the placeholder stays out of the picker's list.
  const open=choices.filter(c=>!c.disabled);
  if(required&&!value&&open.length===1)value=open[0].value;
  if(required&&!value&&!choices.some(c=>c.value===''))input.append(el('option',{value:'',disabled:true,hidden:true,selected:true},'Choose…'));
  const groups=new Map<string,HTMLElement>();
  for(const c of choices){
    // A group with a configured place (a person's rank) is carried on the option, never a section: one flat list.
    const placed=c.group&&c.groupOrder!==undefined;
    const option=el('option',{value:c.value,selected:c.value===value,disabled:!!c.disabled,...(c.hint?{'data-hint':c.hint}:{}),...(c.divider?{'data-divider':''}:{}),
      ...(placed?{'data-group':c.group!,'data-group-order':String(c.groupOrder)}:{})},c.label);
    if(!c.group||placed){input.append(option);continue;}
    if(!groups.has(c.group)){const g=el('optgroup',{label:c.group});groups.set(c.group,g);input.append(g);}
    groups.get(c.group)!.append(option);
  }
  if(value)input.value=value;
  return {wrap:el('div',{class:'field'},el('label',{for:id},label),input),input};
}
/** A duration as typed: "15 min", "45 s", "1 h 5 min", "90 sec"; a bare number is minutes (one unit for every time
 *  field). Seconds, or NaN when it is not a duration. */
export function parseDuration(text:string):number{
  const t=text.trim().toLowerCase();if(!t)return NaN;
  if(/^\d+(\.\d+)?$/.test(t))return Number(t)*60;
  const parts=[...t.matchAll(/(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)\b/g)];
  // Every character is part of a number with its unit (or a separator): "15 min x" is not a duration.
  if(!parts.length||t.replace(/(\d+(?:\.\d+)?)\s*(h|hrs?|hours?|m|mins?|minutes?|s|secs?|seconds?)\b/g,'').replace(/\band\b|[\s,·]+/g,''))return NaN;
  return parts.reduce((n,[,v,u])=>n+Number(v)*(u.startsWith('h')?3600:u.startsWith('s')?1:60),0);
}
/** The one input for a length of time (LEAD-UIUX L10): written and read in `duration` words ("15 min", "45 s"), stored in
 *  the contract field's unit (`unit`); its bounds (in that unit) and whole units are checked and said in the same words.
 *  `value()` is the number in that unit (NaN while not valid), `set` writes one, `bound` moves a limit (a maximum that
 *  follows another field). */
export type DurationField=Field&{value:()=>number;set:(v:number)=>void;bound:(o:{min?:number|null;max?:number|null})=>void};
export function durationField(label:string,unit:'minutes'|'seconds',value:number,o:{min?:number|null;max?:number|null;required?:boolean}={}):DurationField{
  const per=unit==='minutes'?60:1,limits={min:o.min??null,max:o.max??null};
  const f=field(label,'text',Number.isFinite(value)?duration(value*per):'',{required:o.required!==false,inputmode:'text',autocomplete:'off',spellcheck:'false'});
  const check=()=>{const secs=parseDuration(f.input.value),n=secs/per;
    f.input.setCustomValidity(!f.input.value.trim()&&o.required===false?'':Number.isNaN(secs)?'Write a time such as 15 min or 45 s.'
      :!Number.isInteger(n)?`Use whole ${unit}.`:limits.min!=null&&n<limits.min||limits.max!=null&&n>limits.max
      ?`Between ${duration((limits.min??0)*per)} and ${limits.max!=null?duration(limits.max*per):'any length'}.`:'');
    return n;};
  // Once left, a valid time is written back in the same words ("90" becomes "1 h 30 min").
  f.input.addEventListener('input',check);
  f.input.addEventListener('change',()=>{const n=check();if(f.input.validity.valid&&Number.isFinite(n))f.input.value=duration(n*per);});
  check();
  return {...f,value:()=>{const n=check();return f.input.validity.valid?n:NaN;},
    set:v=>{f.input.value=duration(v*per);check();},bound:x=>{Object.assign(limits,x);check();}};
}
/** A yes/no option. On its own in a form it shows as a full-width switch row (label, optional hint, switch on the right);
 *  inside a list of choices it stays a plain checkbox. */
export function check(label: string, checked=false, hint=''): Field {
  const id=hint?'hint-'+Math.random().toString(36).slice(2):'';
  const input=el('input',{type:'checkbox',name:label,checked,...(hint?{'aria-label':label,'aria-describedby':id}:{})});
  return {wrap:el('label',{class:'checkbox'},input,el('span',{class:'check-text'},el('span',{},label),hint?el('span',{class:'hint',id},hint):'')),input};
}
/** One option of any dropdown: `hint` shows beside it, `keywords` are searched but not shown. */
export type Choice={value:string;label:string;group?:string;hint?:string;keywords?:string;disabled?:boolean;
  /** The group is a fact about the row with a configured place (a person's rank, in the department's rank order): a
   *  single choice lists such rows flat in their own order with the group beside each; a multiple choice sections
   *  them in this order (picker.ts). */groupOrder?:number;
  /** Chip colour in a multiple-choice field, by kind (`.rp`): person, group (rank, programme…), place (clinic, duty
   *  type), all (grey, the default), or a tag's duty-<hue>. */tone?:string;
  /** An icon before the label (a relative value's clock). */glyph?:IconName;
  /** A line above this one in the list (Is empty after the type's own operators). */divider?:boolean;
  /** Multiple choice: this value stands alone (a relative "Me"): choosing it clears the others and another clears it. */exclusive?:boolean};
/** Choices that read alike told apart (two people with the same name), for every field and picker: each repeated
 *  label gains what differs between them (their hint, such as a short code, else their group, such as a rank, else
 *  their place in the order by value), and the repeats take that one stable order by value. Unique labels are unchanged
 *  (the same list comes back), so applying it again changes nothing. */
export function distinct(choices:Choice[]):Choice[]{
  const same=new Map<string,Choice[]>();
  for(const c of choices)if(c.value!==''){const k=c.label.trim().toLocaleLowerCase();same.set(k,[...(same.get(k)||[]),c]);}
  const repeats=[...same.values()].filter(xs=>xs.length>1);if(!repeats.length)return choices;
  const next=new Map<Choice,Choice>();
  for(const xs of repeats){
    const ordered=[...xs].sort((a,b)=>a.value<b.value?-1:a.value>b.value?1:0);
    const tells=(f:(c:Choice)=>string|undefined)=>xs.every(c=>f(c))&&new Set(xs.map(f)).size===xs.length;
    const by=tells(c=>c.hint)?'hint':tells(c=>c.group)?'group':'';
    // A hint moved into the label is not said twice beside it.
    xs.forEach((c,i)=>{const o=ordered[i];next.set(c,{...o,label:`${o.label} · ${by==='hint'?o.hint:by==='group'?o.group:i+1}`,...(by==='hint'?{hint:undefined}:{})});});
  }
  return choices.map(c=>next.get(c)||c);
}
/** Every multi-choice field. Up to 7 options show inline; longer lists become one compact picker. */
export function multi(label: string, choices: Choice[], values: string[]=[], emptyText='None selected'): {wrap:HTMLElement; values:()=>string[]; labels:()=>string[]} {
  choices=distinct(choices);
  if(choices.length>LONG_LIST){const p=multiPick(label,choices,values,undefined,emptyText);return {wrap:el('div',{class:'pick-field'},el('span',{class:'pick-label'},label),p.wrap),values:p.values,labels:p.labels};}
  // A choice that can't be taken is shown disabled with why (its hint), never left out (maintenance).
  const checks=choices.map(c=>{const field=check(c.label,values.includes(c.value)&&!c.disabled,c.disabled?c.hint||'':'');field.input.disabled=!!c.disabled;return {value:c.value,label:c.label,field};});
  for(const c of checks)c.field.input.value=c.value;  // readable like the long list's hidden <select multiple>
  return {wrap:el('fieldset',{class:'multi-field',name:label},el('legend',{},label),el('div',{class:'check-grid'},...checks.map(c=>c.field.wrap))), values:()=>checks.filter(c=>c.field.input.checked).map(c=>c.value),labels:()=>checks.filter(c=>c.field.input.checked).map(c=>c.label)};
}
/** Every multiple-choice field reviewed: one boxed field, as tall as any
 *  dropdown, showing the chosen values as chips (`.rp`, coloured by `Choice.tone`) — as many as fit its real width, then
 *  "+N" (its tooltip lists the rest) — and a caret. Click, Enter, Space or ↓ opens the shared picker with search,
 *  groups, "Select all shown" and "Clear" (see picker.ts). A hidden <select multiple> holds the value, so forms, the
 *  unsaved-changes guard and tests read it like any other field; the button carries the whole list as its name. */
export function multiPick(label:string,choices:Choice[],values:string[]=[],onChange?:(values:string[])=>void,emptyText='None selected',creatable=false,
  /** Below the list each time it opens ("5 inactive clinics not shown · Show"). */extra?:()=>HTMLElement|null){
  choices=distinct(choices);const chosen=new Set(values);
  const trigger=el('button',{type:'button',class:'pick-button','aria-haspopup':'dialog','aria-expanded':'false'}) as HTMLButtonElement;
  // The trigger's name lists every value, so its "+N" only shows (the one "+N", aria-hidden).
  const box=el('span',{class:'pick-values'}),more=overflowMore(0,label,{as:'hidden',cls:'rp all pick-more'});
  const holder=el('select',{multiple:true,'aria-hidden':'true',tabindex:'-1','data-native':''});holder.hidden=true;
  trigger.append(box,icon('chevronDown'));
  const wrap=el('div',{class:'pick','data-review-label':label},trigger,holder);
  function paint(){
    const picked=choices.filter(c=>chosen.has(c.value)),names=picked.map(c=>c.label);
    wrap.dataset.reviewValue=names.join(', ')||emptyText;
    holder.replaceChildren(...choices.map(c=>el('option',{value:c.value,selected:chosen.has(c.value)},c.label)));
    const full=names.length>20?`${names.slice(0,20).join(', ')} and ${names.length-20} more`:names.join(', ');
    box.replaceChildren(...(!picked.length?[el('span',{class:'pick-text muted'},emptyText)]
      :[...picked.map(c=>el('span',{class:`rp pick-chip ${c.tone||'all'}`,...(c.tone?.startsWith('duty-')?{'data-lg-self':''}:{})},c.label)),more]));
    trigger.setAttribute('aria-label',`${label}: ${picked.length?full:emptyText}`);
    if(picked.length)trigger.title=full;else trigger.removeAttribute('title');
    fit();
  }
  // Real widths, not character counts: every chip is measured at its natural width, then as many as fit are shown.
  const lower=label.toLowerCase(),noun={one:lower,many:lower==='person'?'people':/s$/.test(lower)?lower:lower+'s'};
  function fit(){
    const chips=[...box.querySelectorAll<HTMLElement>('.pick-chip')];if(!chips.length)return;
    const n=chips.length;
    box.classList.add('measuring');chips.forEach(c=>c.hidden=false);more.hidden=false;paintMore(more,n-1,{noun:label});
    const room=box.clientWidth,gap=parseFloat(getComputedStyle(box).columnGap)||0,w=chips.map(c=>c.offsetWidth),mw=more.offsetWidth;
    box.classList.remove('measuring');
    if(!room){more.hidden=true;return;}  // not laid out yet: every chip, no count, until the observer fits it
    let shown=n;
    if(w.reduce((a,b)=>a+b,0)+gap*(n-1)>room){shown=1;let used=w[0];while(shown<n-1&&used+gap+w[shown]+gap+mw<=room)used+=gap+w[shown++];}
    // Never a stub: a first chip that cannot show about 12 characters gives way to a count ("2 clinics"), the names
    // in its tooltip reviewed.
    if(room-(shown<n?gap+mw:0)<Math.min(w[0],STUB)){
      chips.forEach(c=>c.hidden=true);more.hidden=false;more.textContent=`${n} ${n===1?noun.one:noun.many}`;more.title=chips.map(c=>c.textContent).join(', ');return;}
    chips.forEach((c,i)=>c.hidden=i>=shown);
    more.hidden=shown===n;if(shown===n)more.textContent='';else paintMore(more,n-shown,{noun:label});more.title=chips.slice(shown).map(c=>c.textContent).join(', ');
  }
  fits.set(box,fit);watchPick(box);
  let close:(()=>void)|null=null;
  // Announce the change like a native control, so forms that listen for 'change' keep working.
  const changed=()=>{paint();onChange?.([...chosen]);holder.dispatchEvent(new Event('change',{bubbles:true}));};
  const open=()=>{close=openPicker(trigger,wrap,{label,choices,multiple:true,selected:chosen,creatable,onPick:()=>{},onToggle:changed,extra:extra?.()||undefined,
    onCreate:text=>{if(!choices.some(c=>c.value===text))choices.push({value:text,label:text});chosen.add(text);changed();}});};
  trigger.addEventListener('click',()=>{if(trigger.getAttribute('aria-expanded')==='true')close?.();else open();});
  trigger.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault();open();}});
  paint();
  return {wrap,values:()=>[...chosen],labels:()=>choices.filter(c=>chosen.has(c.value)).map(c=>c.label),open};
}
/** The narrowest a first chip may be cut to (about 12 characters and its padding) before a count shows instead. */
const STUB=96;
/** One observer re-fits every multiple-choice field when its width changes (a resized panel, window or column). */
const fits=new WeakMap<Element,()=>void>();
// Held weakly: a field on a page that is gone (or never laid out, e.g. in a closed panel) must not keep that page alive
// (perf audit PL-01). Each new field sweeps the ones that were on the page and have left it.
const watching=new Set<WeakRef<Element>>(),shown=new WeakSet<Element>(),unwatchPick=new WeakMap<Element,()=>void>();
const refit=(b:Element)=>{shown.add(b);fits.get(b)?.();};
function watchPick(box:Element){
  for(const ref of watching){const b=ref.deref();if(b?.isConnected)shown.add(b);else if(!b||shown.has(b)){if(b)unwatchPick.get(b)?.();watching.delete(ref);}}
  unwatchPick.get(box)?.();watching.add(new WeakRef(box));unwatchPick.set(box,observeSize(box,refit));
}
// A web font can change the text widths without resizing their containers.
if(typeof document!=='undefined')document.fonts?.addEventListener('loadingdone',()=>{
  document.querySelectorAll('.pick-values').forEach(b=>fits.get(b)?.());
  fitLegends(document);
  const slot=document.querySelector<HTMLElement>('.app-footer .page-status');if(slot)fitFooter(slot);});
/** Plain data tables: numeric columns align right, and an empty table says so instead of showing bare headings. */
export function table(headings: Child[], rows: Child[][], emptyText='Nothing to show yet.'): HTMLElement {
  const numeric=headings.map((_,i)=>rows.length>0&&rows.every(r=>{const c=r[i];return typeof c==='number'||(typeof c==='string'&&/^[−-]?\d[\d,.]*( ?(h|%))?$/.test(c.trim()));}));
  const body=rows.length?rows.map(row=>el('tr',{},...row.map((c,i)=>el('td',{class:numeric[i]?'num':''},c)))):[el('tr',{class:'empty-row'},el('td',{colspan:String(headings.length)},emptyText))];
  const grid=el('table',{class:'plain-table'},el('thead',{},el('tr',{},...headings.map((h,i)=>el('th',{scope:'col',class:numeric[i]?'num':''},h)))),el('tbody',{},...body));
  tableColumnMenus(grid,true,true);  // only a table that can wrap gets column menus (X-18)
  return el('div',{class:'table-scroll',tabindex:0,'aria-label':'Scrollable data table'},grid);
}
/** Each open dialog's own close, so leaving a page closes it the same way (closeDialogs). */
const closers=new WeakMap<HTMLDialogElement,()=>void>();
/** Each open dialog's unsaved-changes guard, so leaving a page asks a changed one first (askDirtyDialog). */
const guards=new WeakMap<HTMLDialogElement,ReturnType<typeof guard>>();
/** A modal dialog. `close` (given to build and returned) closes at once, for use after saving; Close, Escape and a
 *  click on the backdrop go through the unsaved-changes guard (`tryClose`). `onClose` runs once, as it closes by any
 *  way (Cancel, Escape, the browser, leaving the page): the native `close` event comes a task later, after the next
 *  page may already own the state a dialog puts back. */
export function showDialog(title: string, description: string, build: (body:HTMLElement, close:()=>void, tryClose:()=>void, onClose:(run:()=>void)=>void) => void) {
  const opener=openerOf(document.activeElement as HTMLElement|null);
  const titleId=`dialog-title-${++fieldSequence}`;
  const dialog=el('dialog',{class:'dialog','aria-labelledby':titleId});
  const closing:(()=>void)[]=[];let closed=false;
  // Its toasts (and their Undo) move to whatever is underneath before the dialog goes.
  const close=()=>{if(closed)return;closed=true;g.dispose();if(dialog.open)dialog.close();releaseToasts(dialog);dialog.remove();returnFocus(opener);for(const run of closing)run();};
  closers.set(dialog,close);
  const g=guard(dialog,close);guards.set(dialog,g);
  const body=el('div',{class:'dialog-body'});
  dialog.append(el('header',{class:'dialog-head'},el('div',{},el('h2',{id:titleId},title),description?el('p',{class:'muted'},description):''),iconButton('Close dialog','close',g.tryClose,{class:'bare'})),body);
  // A build that throws leaves no closed, orphaned dialog behind: it goes, and the error reaches the caller's reportError.
  document.body.append(dialog); try{build(body,close,g.tryClose,run=>{closing.push(run);});}catch(e){close();throw e;}
  // Escape is handled here (lists and pickers inside handle their own first), so the browser never closes it unasked.
  dialog.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.defaultPrevented){e.preventDefault();g.tryClose();}});
  dialog.addEventListener('cancel',e=>{e.preventDefault();g.tryClose();});
  // The browser may close a modal by itself (a repeated Escape, light dismiss): with unsaved changes it opens again and
  // asks, as Close does (LIVE-57).
  dialog.addEventListener('close',()=>{if(!dialog.isConnected)return;if(g.dirty()){dialog.showModal();g.tryClose();}else close();});
  // A click on the backdrop (pressed and released outside the panel) closes like Close.
  const outside=(e:MouseEvent)=>{const r=dialog.getBoundingClientRect();return e.target===dialog&&(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom);};
  let downOutside=false;
  dialog.addEventListener('pointerdown',e=>{downOutside=outside(e);});
  dialog.addEventListener('click',e=>{if(downOutside&&outside(e))g.tryClose();downOutside=false;});
  dialog.showModal();focusFirst(dialog,body);
  return {dialog,body,close,tryClose:g.tryClose,tryCloseThen:g.tryCloseThen};
}
/** Going to another page closes the dialogs left open on the previous one, each through its own close (its `onClose`
 *  runs now, before the next page reads the state). */
export function closeDialogs(){for(const d of document.querySelectorAll<HTMLDialogElement>('dialog[open]'))(closers.get(d)||(()=>{d.close();releaseToasts(d);d.remove();}))();}
/** Leaving a page (Back, a link, the menu) while an open form dialog holds unsaved changes: that dialog asks its own
 *  "Close anyway?" and nothing else happens; Discard changes closes it and goes on to `then`. Windows above it, unchanged,
 *  close first so the question can be answered. False when no open form has unsaved changes (leaving closes them all). */
export function askDirtyDialog(then?:()=>void){
  // Only a form loses what was entered (a window of kept settings, Print or export, closes as before). A form whose save
  // is under way (its Save busy) is on its way to the server: its own save may go on to the next page.
  const open=[...document.querySelectorAll<HTMLDialogElement>('dialog[open]')].reverse(),
    at=open.findIndex(d=>d.querySelector('form')&&guards.get(d)?.dirty()&&!d.querySelector('button[type=submit][aria-busy]'));
  if(at<0)return false;
  for(const d of open.slice(0,at))closers.get(d)?.();
  const d=open[at];guards.get(d)!.tryCloseThen(()=>{closers.get(d)?.();then?.();});
  return true;
}
/** A dialog opens on what it is for (WCAG 2.4.3): an [autofocus] control, else its first field, else its primary button;
 *  never on the header's Close, which the browser would pick. Fields a form draws a moment later still get the focus,
 *  unless the person already moved it. */
const FIRST_FIELD='input:not([type=hidden]):not([disabled]),select:not([disabled]),textarea:not([disabled]),.pick-button';
function focusFirst(dialog:HTMLDialogElement,body:HTMLElement){
  const visible=(n:HTMLElement)=>!!n.offsetParent||n.getClientRects().length>0;
  const pick=()=>[...dialog.querySelectorAll<HTMLElement>('[autofocus]')].find(visible)
    ||[...body.querySelectorAll<HTMLElement>(FIRST_FIELD)].find(n=>visible(n)&&!n.closest('[inert],[hidden]'))
    ||[...body.querySelectorAll<HTMLElement>('button.primary:not([disabled])')].find(visible);
  const close=dialog.querySelector<HTMLElement>('.dialog-head .icon-button');
  const settle=()=>{if(!dialog.open)return false;const target=pick();if(!target)return false;
    if(document.activeElement===close||!dialog.contains(document.activeElement)||document.activeElement===dialog)target.focus();return true;};
  if(!settle())requestAnimationFrame(()=>{settle();});
}
/** Who opened a dialog, noted when it opens: the control, its row key, the record row it sits in and its name. */
type Opener={el:HTMLElement,key:string|null,row:string|null,name:string,tag:string};
const nameOf=(n:Element)=>(n.getAttribute('aria-label')||n.textContent||'').trim();
function openerOf(previous:HTMLElement|null):Opener|null{
  if(!previous||previous===document.body)return null;
  return {el:previous,key:previous.getAttribute('data-rk'),row:previous.closest('[data-row]')?.getAttribute('data-row')??null,name:nameOf(previous),tag:previous.tagName};
}
/** Focus back on the control that opened a dialog. A redraw may have replaced it meanwhile (a polled panel, a saved
 *  list): then its replacement takes the focus, found by the same row key, else by its name inside the same record
 *  row. Never another record's equally named control: without a record row, only a name nothing else shares. */
/** Focus given back to an opener. One in an always-shown bar (top bar, status footer, sidebar) is never scrolled to:
 *  html's scroll-padding counts those bars as covering, so the browser would scroll the page under them. */
function focusBack(n:HTMLElement){n.focus({preventScroll:!!n.closest('.app-footer,.topbar,.sidebar')});}
/** On the page, laid out and not inert: somewhere focus may rest. */
const restable=(n:Element)=>n.isConnected&&n.getClientRects().length>0&&!n.closest('[inert]');
/** No focus, focus on the page itself, or on something no longer shown (maintenance: a closed panel's select). */
const focusLost=()=>{const a=document.activeElement;return !a||a===document.body||!restable(a);};
/** maintenance (WCAG 2.4.3): an opener that is gone or hidden hands the focus to its nearest shown focusable region (a
 *  [tabindex] ancestor), else the page's main region: never the body, never a hidden element. */
function focusRegion(from:Element):HTMLElement|null{
  for(let n=from.parentElement;n;n=n.parentElement)if(n.hasAttribute('tabindex')&&restable(n))return n;
  return document.querySelector<HTMLElement>('#main');
}
function returnFocus(o:Opener|null){
  if(!o)return;
  const previous=o.el;
  const named=(scope:ParentNode)=>[...scope.querySelectorAll<HTMLElement>(o.tag)].filter(n=>n!==previous&&!n.closest('dialog:not([open])')&&nameOf(n)===o.name);
  const twin=():HTMLElement|undefined=>{
    if(o.key){const k=document.querySelector<HTMLElement>(`[data-rk="${CSS.escape(o.key)}"]`);if(k)return k;}
    if(!o.name)return undefined;
    if(o.row)return [...document.querySelectorAll(`[data-row="${CSS.escape(o.row)}"]`)].flatMap(named)[0];
    const all=named(document);return all.length===1?all[0]:undefined;};
  let region:HTMLElement|null=null;
  const lost=()=>focusLost()||document.activeElement===region;
  const t0=[previous.isConnected?previous:null,twin()].find(n=>n&&restable(n));
  if(t0)focusBack(t0);
  if(!t0||document.activeElement!==t0){region=focusRegion(previous);if(region){region.focus({preventScroll:true});if(document.activeElement!==region)region=null;}}
  // The opener may be redrawn a moment later (a polled panel, a list refreshed after a save): follow it to its
  // replacement, as long as the person has not moved the focus meanwhile.
  const watch=new MutationObserver(()=>{if(restable(previous)||!lost())return;const t=twin();if(!t)return;watch.disconnect();focusBack(t);});
  watch.observe(document.body,{childList:true,subtree:true});window.setTimeout(()=>watch.disconnect(),15000);
  document.addEventListener('focusin',()=>watch.disconnect(),{once:true});
}
/** One version of a row-versioned setting, as the shared history shows it. `by`: a name the caller already has; else
 *  `actor_id` is named from Access (or "—" for someone who may not read it). `restore`: the setting's own restore. */
export type HistoryVersion={version:number;saved:string;actor_id?:string;by?:string;value:Child;reason:string;restore?:(close:()=>void)=>void};
/** SW3-D-07: the one history of configuration (row-versioned: who, when, what, why and Restore), newest first, a page at
 *  a time. Every Configuration section shows its versions through this, so they read and behave the same. */
export function historyDialog(title:string,valueLabel:string,read:(offset:number)=>Promise<{versions:HistoryVersion[];more:boolean}>,description=''){
  return showDialog(title,description,(body,close)=>{
    const out=el('div',{}),rows:Child[][]=[];let offset=0,names:Promise<Map<string,string>>|null=null;
    const failed=(e:unknown)=>{if(!sessionChanged(e))out.replaceChildren(note((e as Error).message,'error',e));};
    const more=button('Show more',()=>void page().catch(failed),'secondary small');
    const page=async()=>{more.hidden=true;const r=await read(offset);offset+=r.versions.length;
      const who=r.versions.some(v=>!v.by&&v.actor_id)?await (names||=get<{identities:{id:string;display_name?:string}[]}>('/api/access')
        .then(a=>new Map(a.identities.map(i=>[i.id,String(i.display_name||'')])),()=>new Map<string,string>())):new Map<string,string>();
      rows.push(...r.versions.map(v=>[String(v.version),dateUnits([v.saved]),v.by||who.get(v.actor_id||'')||'—',v.value,v.reason,
        v.restore?button('Restore',()=>v.restore!(close),'secondary small',{'aria-label':`Restore version ${v.version}`}):'']));
      out.replaceChildren(table(['Version','Saved','By',valueLabel,'Reason',''],rows,'No changes yet.'));more.hidden=!r.more;};
    body.append(out,more);void page().catch(failed);});
}
type RefusalAction=(error:unknown,close:()=>void)=>HTMLButtonElement|undefined;
export function formDialog(title: string, description: string, build: (form:HTMLFormElement)=>()=>Promise<void>, submitLabel='Save', refusalAction?:RefusalAction) {
  return showDialog(title,description,(body,close,tryClose)=>formInto(body,close,tryClose,build,submitLabel,refusalAction));
}
/** A refusal in the server's own words. A schema refusal ("Review the highlighted fields") names no field this form can
 *  highlight, so it says each field's message instead (maintenance, WCAG 3.3.1). */
export function refusalWords(e:unknown,form?:ParentNode):string{
  const d=(e as {code?:string;details?:unknown}).details;
  // The form's own label for a field it holds (its name attribute is the field's key); markFields' cue is CSS, not text.
  const label=(key:string)=>(form?.querySelector<HTMLInputElement>(`[name="${CSS.escape(key)}"]`)?.labels?.[0]?.textContent||'').trim()||undefined;
  const said=(e as {code?:string}).code==='SCHEMA_INVALID'&&Array.isArray(d)?[...new Set(d.map(x=>detailWords(x as {field?:unknown;message?:unknown},label).trim()).filter(Boolean))]:[];
  return said.length?said.slice(0,3).join(' · '):(e as Error).message;
}
/** Which fields of a form are marked, in words (WCAG 3.3.2): the fewer kind. When most
 *  fields are required, the optional ones say "(optional)"; otherwise the required ones say "(required)". A switch or a
 *  choice group that always holds a value is neither; a read-only field is not counted. Pure, for Node tests. */
export function fieldCue(required:number,optional:number):'optional'|'required'|null{
  return !required?null:optional&&required>optional?'optional':'required';
}
const CUE_LABELS='.field label,.date-range>.field-label,fieldset>legend,.pick-field>.pick-label';
/** Marks a form's fields as fieldCue decides; the labels that already say so by hand count and are left as they are.
 *  Run it again when a field's required state changes (a duty type that needs a clinic): the earlier cues go first. */
export function markFields(root:HTMLElement){
  for(const label of root.querySelectorAll('[data-cue]')){label.removeAttribute('data-cue');
    const said=label.nextElementSibling;if(!said?.id.startsWith('field-cue-'))continue;
    for(const c of root.querySelectorAll(`[aria-describedby~="${said.id}"]`)){const rest=(c.getAttribute('aria-describedby')||'').split(' ').filter(x=>x&&x!==said.id).join(' ');
      if(rest)c.setAttribute('aria-describedby',rest);else c.removeAttribute('aria-describedby');}
    said.remove();}
  const rows:{label:Element;controls:Control[];required:boolean;said:boolean}[]=[];
  for(const label of root.querySelectorAll(CUE_LABELS)){
    const host=label.parentElement;if(!host)continue;
    const legend=label.tagName==='LEGEND',controls=[...host.querySelectorAll<Control>(legend?'input[type=radio]':'input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea')]
      .filter(c=>!c.disabled&&!(c as HTMLInputElement).readOnly);
    if(!controls.length)continue;
    const required=controls.some(c=>c.required);
    if(legend&&!required)continue;  // radios always hold one of their values
    rows.push({label,controls,required,said:/\((optional|required)\b|optional\)/i.test(label.textContent||'')});
  }
  const cue=fieldCue(rows.filter(r=>r.required).length,rows.filter(r=>!r.required).length);
  // The words in parentheses stay out of the field's name (W3C Understanding 2.5.3, "Text in parentheses"), so the name
  // is the label: "(required)" is the field's own required state (1.3.1); "(optional)" is its description. The cue is
  // drawn by CSS ([data-cue]::after, empty alternative text): the label's own text stays exactly its name, so voice
  // control and label lookups ("Reason") still find the field.
  for(const r of rows)if(cue&&!r.said&&r.required===(cue==='required')){
    r.label.setAttribute('data-cue',cue);
    if(cue!=='optional')continue;
    const id=`field-cue-${++fieldSequence}`;r.label.after(el('span',{class:'sr-only',id},'optional'));
    for(const c of r.controls)c.setAttribute('aria-describedby',[c.getAttribute('aria-describedby'),id].filter(Boolean).join(' '));
  }
}
/** The one form body (fields, error line, Cancel and Save, steps): in a modal dialog (formDialog) or a docked side panel. */
export function formInto(body:HTMLElement,close:()=>void,tryClose:()=>void,build:(form:HTMLFormElement)=>()=>Promise<void>,submitLabel='Save',refusalAction?:RefusalAction){
    const form=el('form',{class:'form-grid'}),error=el('div',{class:'span-2','aria-live':'assertive'});
    const save=build(form),submit=el('button',{class:'button primary',type:'submit'},submitLabel);
    markFields(form);
    form.append(error,el('div',{class:'form-footer span-2'},button('Cancel',tryClose,'secondary'),submit));
    if(!buildSteps(form))addStepRail(form,body);
    form.addEventListener('input',()=>error.replaceChildren());
    // An invalid field in a closed section: open it, so the browser can focus the field and say what is wrong.
    form.addEventListener('invalid',e=>{for(let d=(e.target as Element).closest('details');d;d=d.parentElement?.closest('details')??null)d.open=true;},true);
    form.addEventListener('impact-calculated',()=>error.replaceChildren());
    form.addEventListener('submit',async event=>{
      event.preventDefault(); if(submit.disabled||!form.reportValidity())return;
      // Disabling the focused Save drops the focus to the page; a refused save gives it back (only if nothing else took it).
      // The one busy button (loading.ts): disabled at once, a spinner and "Saving…" only past 300 ms (no flash).
      const hadFocus=document.activeElement===submit,done=busy(submit,submit.dataset.busy||'Saving…');error.replaceChildren();
      // The form says any refusal itself (its error line below): the app's generic conflict message stays quiet (ownErrors).
      try {await ownErrors(save);form.dispatchEvent(new Event('rota-form-saved',{bubbles:true}));close();} catch(e) {
        // The rota moved on (returned, reopened, decided elsewhere): this decision no longer applies, so the dialog closes
        // and one message says what happened (the page redraws in the new state: app.ts rota-conflict).
        if(settledWords(e)){close();reportError(e);return;}
        if((e as Error).message){const said=note(refusalWords(e,form),'error',e);
          // D-4: a refusal the server marks recoverable (the rota moved during preparation) is one press away: Retry sends
          // this form again, with everything entered kept.
          if((e as {details?:{recoverable?:boolean}}).details?.recoverable)said.append(' ',button('Retry',()=>form.requestSubmit(),'secondary small'));
          const action=refusalAction?.(e,close);
          if(action)said.append(' ',action);
          error.replaceChildren(said); error.scrollIntoView({block:'nearest'});
          if(action&&hadFocus)action.focus({preventScroll:true});}} /* an empty message: the form already shows why */ finally {await done();
        if(hadFocus&&submit.isConnected&&(!document.activeElement||document.activeElement===document.body))submit.focus({preventScroll:true});}
    });body.append(form);
}
/** Forms in steps. Nodes marked data-step (or, failing that, each .form-section after the basics) become steps, then
 * what follows the last one (the Review part). Every form shows all on one page reviewed: the steps are
 * jump links that bring their part into view. Fields never leave the form, so saving and server validation are unchanged. */
function buildSteps(form:HTMLFormElement):boolean{
  const kids=[...form.children] as HTMLElement[];
  const footer=kids.find(n=>n.classList.contains('form-footer')),error=footer?.previousElementSibling as HTMLElement|null;
  const content=kids.filter(n=>n!==footer&&n!==error);
  const marked=content.some(n=>n.dataset.step);
  const sections=content.filter(n=>n.classList.contains('form-section')&&n.querySelector('h3'));
  if(!marked&&sections.length<3)return false;
  const steps:{title:string;nodes:HTMLElement[]}[]=[],review:HTMLElement[]=[];
  if(marked){
    for(const n of content){
      const t=n.dataset.step;
      if(t==='Review'){review.push(n);continue;}
      if(t&&!steps.some(x=>x.title===t))steps.push({title:t,nodes:[]});
      if(!steps.length)steps.push({title:'Details',nodes:[]});
      (t?steps.find(x=>x.title===t)!:steps[steps.length-1]).nodes.push(n);
    }
  }else{
    const last=content.lastIndexOf(sections[sections.length-1]);
    steps.push({title:'Basics',nodes:[]});
    content.forEach((n,i)=>{if(i>last){review.push(n);return;}if(sections.includes(n))steps.push({title:(n.querySelector('h3')!.textContent||'Step').replace(/^Review (configuration|draft) consequences$/,'Impact check'),nodes:[n]});else steps[steps.length-1].nodes.push(n);});
    if(!steps[0].nodes.length)steps.shift();
  }
  const panels=steps.map(x=>el('div',{class:'span-2 form-step'},el('div',{class:'form-grid'},...x.nodes)));
  const reviewTitle=form.dataset.reviewTitle||'Review';
  const reviewHeading=el('h3',{},reviewTitle);reviewHeading.hidden=reviewTitle==='Review';
  const reviewPanel=el('div',{class:'span-2 form-step'},reviewHeading,el('div',{class:'form-grid'},...review));reviewPanel.hidden=review.length===0;
  // A step may rename itself (e.g. Settings → Limits for a quota): its marked node's title wins.
  const titleOf=(x:{title:string;nodes:HTMLElement[]})=>x.nodes.find(n=>n.dataset.step)?.dataset.step||x.title;
  // SW3-D-12: a Review step is listed only when something follows the last step (an empty one led to nothing).
  const labels=stepper([...steps.map(titleOf),...(review.length?[reviewTitle]:[])].map((t,i)=>{
    const panel=i<steps.length?panels[i]:reviewPanel;
    return {title:t,go:()=>{panel.scrollIntoView({behavior:'smooth',block:'start'});panel.querySelector<HTMLElement>('input,select,textarea,button')?.focus({preventScroll:true});}};}));
  form.insertBefore(el('div',{class:'span-2 stepper-head'},labels),form.firstChild);
  for(const panel of [...panels,reviewPanel])form.insertBefore(panel,error||footer||null);
  form.classList.add('stepped');return true;
}
/** Long forms: a numbered step rail that jumps to each section and tracks the one in view. Fields stay in place. */
function addStepRail(form:HTMLFormElement,body:HTMLElement){
  const sections=[...form.querySelectorAll<HTMLElement>(':scope > .form-section')].filter(s=>s.querySelector('h3'));
  if(sections.length<3)return;
  const rail=stepper(sections.map((section,i)=>{
    section.id||=`form-step-${++fieldSequence}`;
    return {title:section.querySelector('h3')!.textContent||`Step ${i+1}`,controls:section.id,
      go:()=>{section.scrollIntoView({behavior:'smooth',block:'start'});(section.querySelector('input,select,textarea,button') as HTMLElement|null)?.focus({preventScroll:true});}};
  }),{label:'Form steps',cls:'step-rail'});
  const items=[...rail.querySelectorAll<HTMLElement>('.step-link')];
  // It scrolls with the body (of a dialog or a docked panel); CSS keeps it at the body's top edge.
  const scroller=body;
  const sync=()=>{
    const top=scroller.getBoundingClientRect().top;
    let current=0;sections.forEach((s,i)=>{if(s.getBoundingClientRect().top-top<180)current=i;});
    items.forEach((b,i)=>{b.classList.toggle('current',i===current);b.classList.toggle('passed',i<current);b.setAttribute('aria-current',i===current?'step':'false');});
  };
  scroller.addEventListener('scroll',sync,{passive:true});
  const wrap=el('div',{class:'span-2 step-rail-wrap'},rail);form.prepend(wrap);
  requestAnimationFrame(sync);
}
/** Confirm an action. Governed decisions need a typed reason; routine ones (`routine`) take an optional note.
 *  `final` (irreversible, e.g. publish) also makes the last button a two-step armed() confirm. */
export function confirmAction(title: string, description: string, run:(reason:string)=>Promise<void>, label='Confirm', intro?: Node, routine=false, final=false, reasonLabel='Reason', preserveNote=false) {
  const d=formDialog(title,description,form=>{if(intro)form.append(intro);const reason=area(reasonLabel,'',routine?2:3);reason.input.maxLength=500;
    // The label says what to write ("Why are you withdrawing? (optional)"); an optional note says so once, in its placeholder.
    if(routine)reason.input.placeholder='Optional note, recorded in the history';else{reason.input.required=true;reason.input.minLength=3;}
    form.append(reason.wrap);return()=>run(preserveNote?reason.input.value.trim():routineReason(reason.input.value,routine));},label);
  if(final){const submit=d.dialog.querySelector<HTMLButtonElement>('button[type=submit]');if(submit)armed(submit,`Confirm ${label.toLowerCase()}?`);}
}
/** The reason sent for a routine action when the person leaves the note empty. */
export const routineReason=(text:string,routine=true)=>routine&&text.trim().length<3?'Routine update':text.trim();
// Date formatting is hot on big rotas (tens of thousands of calls): formatters are built once per zone and
// results are remembered, bounded so memory stays small.
const formatters=new Map<string,Intl.DateTimeFormat>();
const formatter=(kind:string,zone:string,options:Intl.DateTimeFormatOptions,locale:string)=>{const key=kind+'|'+zone;let f=formatters.get(key);if(!f){f=new Intl.DateTimeFormat(locale,{...options,timeZone:zone});formatters.set(key,f);}return f;};
const results=new Map<string,string>();
const remember=(key:string,make:()=>string)=>{let v=results.get(key);if(v===undefined){if(results.size>50000)results.clear();v=make();results.set(key,v);}return v;};
/** "Sun 4 Oct" (or "Sun 4 Oct 2026" with `year`), built from parts so it reads the same in every browser: some ICU
 * versions put a comma after the weekday ("Sun, 4 Oct") or use "Sept" instead of "Sep". Months use three letters.
 * A plain day ("2026-10-04") is that calendar day. */
export function formatDay(value: string, zone=departmentTimeZone(), year=false): string {
  if(value.length===10)zone='UTC';  // a plain day is a calendar day: zones apply to instants only (UTC+14 would show the next day)
  return remember('w|'+zone+'|'+year+'|'+value,()=>{
    const parts=formatter(year?'dayYear':'day',zone,{weekday:'short',day:'numeric',month:'short',...(year?{year:'numeric'}:{})},'en-GB')
      .formatToParts(new Date(value.length===10?value+'T12:00:00Z':value)),part=(t:string)=>parts.find(p=>p.type===t)?.value||'';
    return [part('weekday'),part('day'),part('month').slice(0,3),year?part('year'):''].filter(Boolean).join(' ');});
}
export function formatDate(value: string, zone=departmentTimeZone()): string {
  if(value.length===10)zone='UTC';  // a plain day keeps its calendar day in every zone
  return remember('d|'+zone+'|'+value,()=>{  // "4 Sep 2026": three-letter months, as formatDay (some ICU versions say "Sept")
    const parts=formatter('date',zone,{day:'numeric',month:'short',year:'numeric'},'en-GB').formatToParts(new Date(value.length===10?value+'T12:00:00Z':value)),part=(t:string)=>parts.find(p=>p.type===t)?.value||'';
    return `${part('day')} ${part('month').slice(0,3)} ${part('year')}`;});
}
/** Calendar days, both included: "4 Oct 2026", "4–10 Oct 2026", "28 Dec 2026 – 3 Jan 2027". */
export const dayRange=(first:string,last:string)=>first===last?formatDate(first):first.slice(0,7)===last.slice(0,7)
  ?`${Number(first.slice(8))}–${formatDate(last)}`:`${formatDate(first)} – ${formatDate(last)}`;
/** When a finding applies: "4 Oct 2026 08:00–12:00"; whole days (midnight to midnight) as a date range, "4–10 Oct 2026". */
export function spanLabel(start:string,end:string|undefined|null,zone:string):string{
  const a=toLocal(start,zone),b=end?toLocal(end,zone):'';
  if(!b)return formatDate(start,zone)+' '+formatTime(start,zone);
  if(a.slice(11)==='00:00'&&b.slice(11)==='00:00'&&b>a){
    return dayRange(a.slice(0,10),new Date(Date.parse(b.slice(0,10)+'T12:00:00Z')-864e5).toISOString().slice(0,10));
  }
  return a.slice(0,10)===b.slice(0,10)?`${formatDate(start,zone)} ${formatTime(start,zone)}–${formatTime(end!,zone)}`
    :`${formatDate(start,zone)} ${formatTime(start,zone)} – ${formatDate(end!,zone)} ${formatTime(end!,zone)}`;
}
/** Visual dates may wrap between complete units; plain text formatters remain unchanged for labels and search. */
export const dateUnits=(parts:string[],className=''):HTMLElement=>el('span',{class:'date-units'+(className?' '+className:'')},
  ...parts.flatMap((part,i)=>[i?' ':'',el('span',{},part)]));
/** A stored instant as the department reads it: "1 Nov 2026 00:30" in its own timezone (never UTC). */
export const departmentDateTime=(value:string)=>{const zone=departmentTimeZone();return formatDate(value,zone)+' '+formatTime(value,zone);};
export function formatTime(value: string, zone:string): string {return remember('t|'+zone+'|'+value,()=>formatter('time',zone,{hour:'2-digit',minute:'2-digit',hour12:false},'en-GB').format(new Date(value)));}
export function toLocal(value:string,zone:string):string {
  return remember('l|'+zone+'|'+value,()=>{
    const parts=formatter('local',zone,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'},'en-CA').formatToParts(new Date(value));
    const get=(key:string)=>parts.find(p=>p.type===key)!.value;return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;});
}

/** How a role name reads on screen ("operator" → "Developer"), from the contracts. */
export const roleLabel=(r:string)=>ROLE_LABELS[r as keyof typeof ROLE_LABELS]??r;
/** A role's holders as a group ("Coordinators", "Staff"). shortcut: a mass-noun label (Staff) is the one exception kept
 *  here; when the contract gives ROLE_LABELS a plural, read it instead. */
export const rolePlural=(r:string)=>{const l=roleLabel(r);return l==='Staff'?l:pluralWord(l);};
/** One capability of the shared permission map (packages/contracts PERMISSIONS). */
export type Permission=keyof typeof PERMISSIONS;
export type Capability=keyof typeof CAPABILITIES;
export type RoleName=typeof ROLE_NAMES[number];
/** Access gates use generated capability keys; role names live only in the contract data. */
export const CAPABILITY={
  STAFF:'ui.staff',COORDINATOR:'ui.coordinator',APPROVER:'ui.approver',CREDENTIALER:'ui.credentialer',
  ADMIN:'ui.admin',AUDITOR:'ui.auditor',PROGRAMME_LEAD:'ui.programme_lead',OPERATOR:'ui.operator',
  PUBLICATIONS_READ_TEAM:'publications.read_team',ACCESS_SYSTEM_ROLES:'access.system_roles',ACCESS_INSTALLATION_ADMIN:'access.installation_admin',
  SYSTEM_OPERATIONS:'system.operations',AUDIT_READ:'audit.read',FEEDBACK_READ_ALL:'feedback.read_all',CATALOGUE_READ:'catalogue.read',
  RULES_PAGE:'ui.rules',RULES_READ:'rules.read',
} as const satisfies Record<string,Capability>;
/** Whether these roles grant the capability: the same map the server checks. */
export const grants=(roles:readonly string[],p:Capability)=>CAPABILITIES[p].some(r=>roles.includes(r));
/** Accept generated registry role lists as well as capability keys without introducing another permission source. */
export const hasAccess=(roles:readonly string[],key:string)=>Object.prototype.hasOwnProperty.call(CAPABILITIES,key)?grants(roles,key as Capability):roles.includes(key);
/** Raw role value for data contracts whose fields carry role names (audiences, review steps). */
export const roleForCapability=(key:Capability)=>CAPABILITIES[key][0] as RoleName;
/** Capability key for a role value received in a data contract (review step, request decider). */
export const capabilityForRole=(role:string)=>`ui.${role}` as Capability;
/** Who holds a capability, for help text ("Developers and Auditors"). */
export const holders=(p:Permission)=>CAPABILITIES[p].map(rolePlural).join(' and ');

/** Tabs over panels, each built the first time it opens (arrows move focus; Enter/Space or a click selects). */
/** The one tab set (WAI-ARIA APG Tabs): a tablist and lazily built panels. `activation` 'auto' for cheap local panels
 *  (default manual: arrows move, Enter/Space opens); `cls` / `panelsCls`: the surface's own classes. */
export function tabs(label:string,items:[string,Child,()=>Node][],initial='',onSelect?:(key:string)=>void,
  o:{activation?:Activation;cls?:string;panelsCls?:string}={}):HTMLElement{
  const id='tabs-'+(++fieldSequence),list=el('div',{class:o.cls||'rule-tabs page-tabs',role:'tablist',activation:o.activation,'aria-label':label}),body=el('div',{class:o.panelsCls||'tab-panels'});
  const entries=items.map(([key,text,build])=>{
    const tab=el('button',{type:'button',role:'tab',id:`${id}-${key}-tab`,'aria-controls':`${id}-${key}`,'aria-selected':'false'},text);
    const panel=el('div',{role:'tabpanel',id:`${id}-${key}`,'aria-labelledby':`${id}-${key}-tab`,tabindex:0,hidden:true});
    list.append(tab);body.append(panel);return {key,tab,panel,build,built:false};});
  const show=(key:string)=>{for(const e of entries){const on=e.key===key;e.tab.setAttribute('aria-selected',String(on));e.tab.tabIndex=on?0:-1;e.panel.hidden=!on;
    if(on&&!e.built){e.panel.append(e.build());e.built=true;}}onSelect?.(key);};
  for(const e of entries)e.tab.addEventListener('click',()=>show(e.key));
  show(entries.some(e=>e.key===initial)?initial:entries[0].key);
  return el('div',{class:'page-tabs-wrap'},list,body);
}
