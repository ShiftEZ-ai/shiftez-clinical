/** A picture of the page as the person sees it, with no library.
 *
 *  `snapshot()` copies what is in view NOW, synchronously, on the frame after the feedback dialog opens (the dialog itself
 *  is skipped): the visible elements, the
 *  values typed in fields, scroll positions and stuck headers. Private fields are masked in the copy itself, so their
 *  text never reaches the picture. `render()` then draws the copy, with the page's own stylesheets and fonts, as an SVG
 *  foreignObject image onto a canvas. Only the viewport is drawn: anything outside it (or scrolled out of its box) is
 *  replaced by an empty box of the same size, which keeps a department-scale Schedule quick to copy.
 *  shortcut: CSS pseudo-states (:hover,:focus) and canvas/iframe content are not reproduced; upgrade path is the
 *  "Capture screen" button (getDisplayMedia), which takes the real pixels. */

import {redactCredentials} from './feedback_context.js';

/** What is hidden in every picture: fields marked private, passwords, and the reasons and notes people type. */
export const PRIVATE='[data-private],input[type=password],[autocomplete*=password],:is(input,textarea)[name*=reason i],:is(input,textarea)[name*=note i],:is(input,textarea)[name*=private i],:is(input,textarea)[aria-label*=reason i]';
/** A field holding a link the app made as a credential (an invitation, a password reset, a calendar subscription) is
 *  private even when its owner did not mark it. */
const holdsCredential=(from:Element)=>(from instanceof HTMLInputElement||from instanceof HTMLTextAreaElement)&&redactCredentials(from.value)!==from.value;
/** Never copied: scripts, frames, the hover help, and anything marked to stay out (the feedback controls' own state). */
const SKIP=new Set(['SCRIPT','NOSCRIPT','IFRAME','TEMPLATE','LINK','STYLE','OBJECT','EMBED','VIDEO','AUDIO','CANVAS']);
const SKIP_SELECTOR='.help-tooltip,.hovercard,.pop-arrow,.fb-dialog,[data-feedback-skip]';
type Box={l:number;t:number;r:number;b:number};
export type Snapshot={root:HTMLElement;width:number;height:number;theme:'light'|'dark';rootStyle:string;rootClass:string;
  /** Elements copied and masked, and the time the copy took (ms). */copied:number;masked:number;ms:number};

const meet=(a:Box,b:Box):Box=>({l:Math.max(a.l,b.l),t:Math.max(a.t,b.t),r:Math.min(a.r,b.r),b:Math.min(a.b,b.b)});
const outside=(r:DOMRect,c:Box)=>r.right<=c.l||r.left>=c.r||r.bottom<=c.t||r.top>=c.b;
const px=(n:number)=>`${Math.round(n*100)/100}px`;

/** Masks a copied element: its text becomes dots of the same length and its box a hatched block. */
function mask(copy:Element){
  copy.setAttribute('data-fb-mask','');
  if(copy instanceof HTMLInputElement||copy instanceof HTMLTextAreaElement){copy.setAttribute('value','');copy.removeAttribute('placeholder');copy.textContent='';return;}
  const walk=document.createTreeWalker(copy,NodeFilter.SHOW_TEXT);const texts:Text[]=[];
  for(let n=walk.nextNode();n;n=walk.nextNode())texts.push(n as Text);
  for(const t of texts)t.data=t.data.replace(/\S/g,'•');
  for(const field of copy.querySelectorAll('input,textarea')){field.setAttribute('value','');field.removeAttribute('placeholder');field.textContent='';}
  for(const option of copy.querySelectorAll('option'))option.textContent='•••';
}
/** The copy shows what was typed and chosen, not what the page first had. */
function keepValue(from:Element,copy:Element){
  if(from instanceof HTMLInputElement&&copy instanceof HTMLInputElement){
    if(from.type==='checkbox'||from.type==='radio'){if(from.checked)copy.setAttribute('checked','');else copy.removeAttribute('checked');}
    else if(from.type!=='file')copy.setAttribute('value',from.value);
  }else if(from instanceof HTMLTextAreaElement)copy.textContent=from.value;
  else if(from instanceof HTMLSelectElement){const chosen=new Set([...from.selectedOptions].map(o=>o.index));
    [...copy.querySelectorAll('option')].forEach((o,i)=>{if(chosen.has(i))o.setAttribute('selected','');else o.removeAttribute('selected');});}
}

export function snapshot(): Snapshot{
  const started=performance.now(),de=document.documentElement;
  const W=de.clientWidth||innerWidth,H=innerHeight,view:Box={l:0,t:0,r:W,b:H};
  let copied=0,masked=0;
  const sticky:{from:HTMLElement;copy:HTMLElement;at:DOMRect}[]=[],scrolled:{copy:HTMLElement;x:number;y:number}[]=[],layered:{copy:HTMLElement;at:DOMRect;modal:boolean}[]=[];
  // One walk over the page and its copy together; a subtree out of view is not walked at all.
  function copyOf(from:Element,clip:Box):Node|null{
    if(SKIP.has(from.tagName)||from.matches(SKIP_SELECTOR))return null;
    const cs=getComputedStyle(from);
    if(cs.display==='none')return null;
    const copy=from.cloneNode(false) as HTMLElement;copied++;
    keepValue(from,copy);
    // A picture needs no link targets, and a link may carry a credential (often encoded inside another site's address).
    if(copy instanceof HTMLAnchorElement)copy.removeAttribute('href');
    const top=(from instanceof HTMLDialogElement&&from.matches(':modal'))||from.matches(':popover-open');
    const stuck=cs.position==='sticky'&&from instanceof HTMLElement;
    // A source leaf needs its rectangle only in the top/sticky layer; all nodes and private masking stay in the copy.
    const r=from.hasChildNodes()||top||stuck?from.getBoundingClientRect():null;
    // The top layer (a modal dialog, a popover) is drawn above everything, wherever it sits in the page.
    if(top){layered.push({copy,at:r!,modal:from instanceof HTMLDialogElement});clip=view;}
    else if(cs.position==='fixed')clip=view;
    if(stuck)sticky.push({from:from as HTMLElement,copy,at:r!});
    // Out of view: an empty box of the same size (table parts and inline text keep their place as they are).
    // A table's rows out of view are boxes too (one empty row of their height), so a long list is not copied cell by cell.
    const layoutBox=/^(block|flex|grid|flow-root|list-item|inline-block|inline-flex|inline-grid|table-row|table-row-group)$/.test(cs.display);
    if(r&&outside(r,clip)&&layoutBox&&r.width>0&&r.height>0&&from.childNodes.length){
      copy.style.setProperty('width',px(r.width));copy.style.setProperty('height',px(r.height));
      copy.style.setProperty('min-width','0');copy.style.setProperty('min-height','0');copy.style.setProperty('flex','none');copy.style.setProperty('box-sizing','border-box');
      return copy;
    }
    if(from.matches(PRIVATE)||holdsCredential(from)){
      for(const child of from.childNodes){const c=child.nodeType===1?copyOf(child as Element,clip):child.nodeType===3?child.cloneNode():null;if(c)copy.append(c);}
      mask(copy);masked++;return copy;
    }
    if(from.scrollTop||from.scrollLeft)scrolled.push({copy,x:from.scrollLeft,y:from.scrollTop});
    const clips=cs.overflowX!=='visible'||cs.overflowY!=='visible';
    const inner=clips&&r?meet(clip,{l:r.left,t:r.top,r:r.right,b:r.bottom}):clip;
    for(const child of from.childNodes){
      if(child.nodeType===1){const c=copyOf(child as Element,inner);if(c)copy.append(c);}
      else if(child.nodeType===3)copy.append(redactCredentials((child as Text).data));
    }
    return copy;
  }
  const body=copyOf(document.body,view) as HTMLElement;
  // Stuck headers: where each would be unstuck, measured in one pass, gives the offset that puts it where it is now.
  if(sticky.length){
    sticky.forEach(s=>s.from.classList.add('fb-unstick'));de.classList.add('fb-measuring');
    const natural=sticky.map(s=>s.from.getBoundingClientRect());
    de.classList.remove('fb-measuring');sticky.forEach(s=>s.from.classList.remove('fb-unstick'));
    sticky.forEach((s,i)=>{s.copy.style.setProperty('position','relative');s.copy.style.setProperty('inset','auto');
      s.copy.style.setProperty('top',px(s.at.top-natural[i].top));s.copy.style.setProperty('left',px(s.at.left-natural[i].left));});
  }
  // A scrolled box: its content moves by what was scrolled (the copy itself cannot scroll).
  for(const s of scrolled)for(const child of s.copy.children)(child as HTMLElement).style?.setProperty('translate',`${px(-s.x)} ${px(-s.y)}`);
  for(const l of layered){const st=l.copy.style;
    for(const [k,v] of [['position','fixed'],['inset','auto'],['left',px(l.at.left)],['top',px(l.at.top)],['width',px(l.at.width)],['height',px(l.at.height)],['margin','0'],['max-width','none'],['max-height','none'],['z-index','2147483000']])st.setProperty(k,v);
    if(l.modal){const shade=document.createElement('div');shade.className='fb-backdrop';l.copy.before(shade);}
  }
  body.style.setProperty('position','relative');body.style.setProperty('top',px(-scrollY));body.style.setProperty('left',px(-scrollX));
  body.style.setProperty('width',px(W));body.style.setProperty('margin','0');
  const root=document.createElement('div');root.className='fb-viewport';
  root.style.setProperty('width',px(W));root.style.setProperty('height',px(H));root.append(body);
  const theme=de.dataset.theme==='dark'||(de.dataset.theme!=='light'&&matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';
  return {root,width:W,height:H,theme,rootStyle:de.getAttribute('style')||'',rootClass:de.className,copied,masked,ms:performance.now()-started};
}

// Every stylesheet the page links, in the page's own order (the cascade depends on it: schedule_teams.css comes before
// app.css), each with its fonts inlined (an SVG image may not load anything), read once per page.
let styles:Promise<string>|null=null;
const dataUrl=(blob:Blob)=>new Promise<string>((ok,fail)=>{const r=new FileReader();r.onload=()=>ok(String(r.result));r.onerror=()=>fail(r.error);r.readAsDataURL(blob);});
/** Each @font-face in `css` with its family and whether every source is inlined (a data URL). */
export const fontFaces=(css:string)=>(css.match(/@font-face\s*\{[^}]*\}/g)||[]).map(face=>({
  family:(face.match(/font-family:\s*(["']?)([^;"'}]+)\1/)||[])[2]||'',inlined:!/url\(\s*(?!["']?data:)/.test(face)}));
async function inlined(sheet:string):Promise<string>{
  let css=await (await fetch(sheet,{credentials:'same-origin'})).text();
  for(const url of new Set(css.match(/url\(([^)]+)\)/g)||[])){
    const href=url.slice(4,-1).replace(/^["']|["']$/g,'');if(href.startsWith('data:'))continue;
    try{css=css.replaceAll(url,`url(${await dataUrl(await (await fetch(new URL(href,sheet),{credentials:'same-origin'})).blob())})`);}catch{/* the fallback font draws it */}
  }
  return css;
}
export function appStyles():Promise<string>{
  return styles??=(async()=>{
    const sheets=[...document.querySelectorAll<HTMLLinkElement>('link[rel=stylesheet]')].map(l=>l.href);
    const css=(await Promise.all(sheets.map(inlined))).join('\n');
    // Still and as shown: no animation is caught half-way; masked fields are hatched; a modal's backdrop is drawn.
    return css.replaceAll(']]>','')+`
*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}
.fb-viewport{position:relative;overflow:hidden;transform:translateZ(0);background:var(--bg)}
[data-fb-mask]{color:transparent!important;background:repeating-linear-gradient(135deg,var(--sub) 0 5px,var(--line-2) 5px 10px)!important;border-radius:4px}
[data-fb-mask] *{color:transparent!important;background:transparent!important;border-color:transparent!important}
.fb-backdrop{position:fixed;inset:0;background:var(--overlay);z-index:2147482999}`;
  })().catch(e=>{styles=null;throw e;});
}

/** Reads the stylesheet and fonts ahead (once, when the browser is idle after sign-in), so the first capture is quick. */
export const warmCapture=()=>appStyles().then(()=>true,()=>false);

/** Draws a snapshot onto a canvas at `scale` image pixels per CSS pixel (default: the screen's density, 1–2×). The SVG
 *  itself is sized in image pixels (its viewBox in CSS pixels), so text is laid out once and drawn sharp at any scale. */
export async function render(s:Snapshot,scale=Math.min(2,Math.max(1,devicePixelRatio||1))):Promise<HTMLCanvasElement>{
  const t0=performance.now(),css=await appStyles(),t1=performance.now();
  const xhtml=new XMLSerializer().serializeToString(s.root);
  const esc=(v:string)=>v.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  const W=Math.round(s.width*scale),H=Math.round(s.height*scale);
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${s.width} ${s.height}" data-theme="${s.theme}" class="${esc(s.rootClass)}" style="${esc(s.rootStyle)}"><style><![CDATA[${css}]]></style><foreignObject x="0" y="0" width="100%" height="100%">${xhtml}</foreignObject></svg>`;
  const img=new Image();img.decoding='async';img.src='data:image/svg+xml;charset=utf-8,'+encodeURIComponent(svg);
  const t2=performance.now();await img.decode();const t3=performance.now();
  const canvas=document.createElement('canvas');canvas.width=W;canvas.height=H;
  const g=canvas.getContext('2d');if(!g)throw new Error('The picture is too large for this browser.');
  g.drawImage(img,0,0,W,H);
  // Where the time went (the browser tests keep the capture within its budget).
  canvas.dataset.timing=[`copy ${Math.round(s.ms)}`,`styles ${Math.round(t1-t0)}`,`serialise ${Math.round(t2-t1)}`,`decode ${Math.round(t3-t2)}`,`draw ${Math.round(performance.now()-t3)}`,`svg ${Math.round(svg.length/1024)} KB`].join(' · ');
  return canvas;
}

/** The real pixels of this tab, chosen by the person in the browser's own share prompt (when the drawn copy failed). */
export async function captureScreen():Promise<HTMLCanvasElement>{
  const stream=await navigator.mediaDevices.getDisplayMedia({video:{displaySurface:'browser'},audio:false,preferCurrentTab:true} as DisplayMediaStreamOptions);
  try{
    const video=document.createElement('video');video.muted=true;video.srcObject=stream;await video.play();
    await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
    const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
    canvas.getContext('2d')!.drawImage(video,0,0);return canvas;
  }finally{stream.getTracks().forEach(t=>t.stop());}
}

/** The picture to send: WebP (PNG where WebP cannot be made), at most `limit` bytes, smaller and lighter until it fits. */
export async function encode(canvas:HTMLCanvasElement,limit:number):Promise<{media_type:'image/webp'|'image/png';data:string;bytes:number}>{
  const blobOf=(c:HTMLCanvasElement,type:string,q?:number)=>new Promise<Blob|null>(ok=>c.toBlob(ok,type,q));
  let source=canvas;
  for(let i=0;i<6;i++){
    for(const q of [0.85,0.7]){
      const b=await blobOf(source,'image/webp',q);
      const blob=b&&b.type==='image/webp'?b:await blobOf(source,'image/png');
      if(blob&&blob.size<=limit)return {media_type:blob.type==='image/webp'?'image/webp':'image/png',data:(await dataUrl(blob)).split(',')[1],bytes:blob.size};
      if(blob?.type==='image/png')break;
    }
    const next=document.createElement('canvas');next.width=Math.round(source.width*0.75);next.height=Math.round(source.height*0.75);
    next.getContext('2d')!.drawImage(source,0,0,next.width,next.height);source=next;
  }
  throw new Error('The screenshot is too large to send. Turn it off, or hide part of it.');
}
