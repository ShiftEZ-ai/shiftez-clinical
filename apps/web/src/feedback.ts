/** Feedback from inside Rota: anyone, on any page and in any state, sends a problem, a
 *  suggestion, something confusing or a question with the page captured as it was and the details that help follow it
 *  up; Developers triage the reports here (System › Feedback) and reporters see where theirs stand (My feedback). */
import {brand} from './brand.js';
import {working} from './loading.js';
import {el,icon,button,iconButton,showDialog,formInto,area,check,note,notify,empty,sectionHead,statusBadge,badge,plural,
  formatDate,formatTime,select,field,holders,setProblemReporter,phoneQuery,reportError,type Child,type IconName,emptyLine} from './dom.js';
import {request,get,command,newest} from './api.js';
import {dataTable,openTableState,tableState,openFiltered,tableSearch,tableSearchOptions,type Column} from './table_view.js';
import {searchParam,type SearchOptions} from './search.js';
import {dockPanel,rowMark,closeDock,type PanelPosition} from './side_panel.js';
import {recentErrors,recentRequests,recentRoutes,redactCredentials} from './feedback_context.js';
import {snapshot,render,captureScreen,encode,warmCapture,type Snapshot} from './feedback_capture.js';
import {APP_VERSION,FEEDBACK_KINDS,FEEDBACK_STATUSES,FEEDBACK_LIMITS,type FeedbackCreate,type FeedbackCreated,type FeedbackRow,
  type FeedbackList,type FeedbackExportResult,type FeedbackMark} from './generated.js';
import {WEB_COMMIT} from './build.js';
import {theme} from './theme.js';
import {parseRoute} from './routes.js';
import type {Me} from './types.js';
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)
import {readFailed} from './dom.js';  // a failed read (DL-045)

type Kind=typeof FEEDBACK_KINDS[number];type Status=typeof FEEDBACK_STATUSES[number];
/** The shell's side of feedback: who is signed in and how to move around (set once by app.ts). */
export interface FeedbackHost{me:()=>Me|null;navigate:(target:string)=>void;zone:()=>string;pageLabel:(route:string)=>string}
let host:FeedbackHost|null=null;

/** Each kind: its name, icon, what the message box asks, and its hint. */
export const KINDS:Record<Kind,{label:string;icon:IconName;ask:string;hint:string}>={
  problem:{label:'Problem',icon:'alert',ask:'What went wrong?',hint:'What were you doing, and what happened?'},
  suggestion:{label:'Suggestion',icon:'sparkle',ask:'What would make this better?',hint:'Describe the idea and who it would help.'},
  confusing:{label:'Confusing',icon:'edit',ask:'What is confusing?',hint:'Which text, layout or wording? Mark the spot on the screenshot.'},
  question:{label:'Question',icon:'help',ask:'Your question',hint:'Ask anything about this page.'},
};
export const STATUS_WORDS:Record<Status,string>={new:'New',triaged:'Triaged',in_progress:'In progress',fixed:'Fixed',wont_fix:'Won’t fix',duplicate:'Duplicate'};
const statusOf=(s:string)=>statusBadge(s,STATUS_WORDS[s as Status]||s);
const kindBadge=(k:string)=>{const d=KINDS[k as Kind];return el('span',{class:'fb-kind'},icon(d?.icon||'message'),d?.label||k);};
// Stored routes are canonical ("/access/people"); older reports kept the hash ("#access/people"): the app's own address either way.
const ADDRESS=(route:string)=>route.replace(/^#?\/?/,''),PATH=(route:string)=>ADDRESS(route).split('?')[0];

/** Sets up every way in: the error reporter on toasts and notices, and the keyboard shortcut. */
export function installFeedback(h:FeedbackHost){
  host=h;
  setProblemReporter({available:()=>!!host?.me()&&!document.querySelector('.fb-dialog'),report:error=>openFeedback({kind:'problem',error})});
  // The stylesheet and fonts the picture needs are read while the browser is idle (the browser tests await __fbWarm).
  const warm=()=>{(window as unknown as {__fbWarm?:Promise<boolean>}).__fbWarm=warmCapture();};
  if('requestIdleCallback' in window)requestIdleCallback(warm,{timeout:4000});else setTimeout(warm,2000);
  document.addEventListener('keydown',e=>{
    if(!host?.me()||!(e.ctrlKey||e.metaKey)||!e.shiftKey||e.altKey||e.key.toLowerCase()!=='f')return;
    e.preventDefault();openFeedback();
  });
}
export const SHORTCUT='Ctrl Shift F';

// ---- What a report carries ----
/** Only navigation: which page and tab, the page's list view (filters, grouping, sort, layout, saved view), whether a
 *  panel is open and how many rows are selected. No search text, no row, no form value (the server refuses record
 *  contents too). `active_view` reads "page/tab:list" so the list can be put back as it was. */
export function viewState():Record<string,unknown>{
  const main=document.querySelector<HTMLElement>('#main');
  const page=[main?.dataset.loadedView,main?.dataset.loadedTab].filter(Boolean).join('/');
  const list=document.querySelector<HTMLElement>('#main [data-table]')?.dataset.table||'';
  const s=list?tableState(list):null;
  const state:Record<string,unknown>={active_view:page+(s?':'+list:'')};
  if(s){const filters=Object.fromEntries(Object.entries(s.filters).filter(([,v])=>v.length).slice(0,20).map(([k,v])=>[k,v.slice(0,20).map(x=>x.slice(0,80))]));
    if(Object.keys(filters).length)state.filters=filters;if(s.group)state.group_by=s.group;if(s.sort)state.sort=s.sort;
    if(s.layout!=='table')state.layout=s.layout;if(s.view_id)state.view_id=s.view_id;}
  if(document.querySelector('.dock-panel,.side-panel:not(.slot-covered)'))state.open_panel='open';
  const selected=[...document.querySelectorAll<HTMLInputElement>('#main .bulk-check:checked')].filter(b=>!/^Select all/.test(b.getAttribute('aria-label')||'')).length;
  if(selected)state.selection_count=selected;
  return state;
}
type Context=Omit<FeedbackCreate,'kind'|'message'|'expected'|'screenshot'|'mark'>;
function contextNow(error?:string):Context{
  const me=host!.me()!;
  // The error being reported comes first (the server matches repeats on it), then the newest.
  const errors=recentErrors().reverse();
  if(error){const i=errors.findIndex(e=>e.message===error.slice(0,300));const first=i>=0?errors.splice(i,1)[0]:{at:new Date().toISOString(),message:error.slice(0,300),source:'message shown'};errors.unshift(first);}
  const route=redactCredentials(location.hash.length>1?location.hash:'#home');  // as recordRoute keeps the earlier ones
  // The pages before this one: the current page is already the report's Page.
  const before=recentRoutes();if(before.at(-1)?.split('?')[0]===route.split('?')[0])before.pop();
  return {route:route.slice(0,FEEDBACK_LIMITS.route),recent_routes:before.slice(-FEEDBACK_LIMITS.recent_routes),view_state:viewState(),page_title:document.title.slice(0,160),app_version:APP_VERSION,build_commit:WEB_COMMIT||'',
    environment:me.deployment==='production'?'production':'dev',role_names:me.roles as Context['role_names'],
    viewport:{w:innerWidth,h:innerHeight},theme:theme(),user_agent:navigator.userAgent.slice(0,300),
    recent_errors:errors.slice(0,FEEDBACK_LIMITS.recent_errors),request_ids:recentRequests().reverse().slice(0,FEEDBACK_LIMITS.request_ids)};
}
/** The details as words: the "Details sent" list and a report's panel read the same way. */
function viewWords(v:Record<string,any>|undefined):string{
  if(!v)return '—';
  const parts=[String(v.active_view||'').split(':')[0],...Object.entries((v.filters||{}) as Record<string,string[]>).map(([k,x])=>`${k}: ${x.join(', ')}`),
    v.group_by?`grouped by ${v.group_by}`:'',Array.isArray(v.sort)?`sorted by ${v.sort[0]} ${v.sort[1]==='desc'?'descending':'ascending'}`:'',
    v.layout?`${v.layout} layout`:'',v.view_id?'a saved view':'',v.open_panel?'a side panel open':'',v.selection_count?plural(v.selection_count,'row')+' selected':''];
  return parts.filter(Boolean).join(' · ')||'—';
}
function contextFacts(c:Partial<FeedbackRow>,zone:string):[string,Child][]{
  const errors=c.recent_errors||[],ids=c.request_ids||[],visited=c.recent_routes||[];
  return [
    ['Page',`${host?.pageLabel(c.route||'')||''} (${c.route})`],
    ['Visited before',countThen(visited.length,el('ol',{class:'fb-list'},...visited.map(r=>el('li',{},`${host?.pageLabel(r)||''} `,el('span',{class:'mono muted'},r)))))],
    ['View',viewWords(c.view_state as Record<string,any>)],
    ['Version',[c.app_version,c.build_commit].filter(Boolean).join(' · ')||'—'],
    ['Environment',c.environment==='production'?'Production':'Test'],
    ['Roles',(c.role_names||[]).join(', ')||'—'],
    ['Screen',c.viewport?`${c.viewport.w} × ${c.viewport.h} · ${c.theme} theme`:'—'],
    ['Browser',c.user_agent?el('span',{title:c.user_agent,'data-tip':c.user_agent},browserName(c.user_agent)):'—'],
    ['Recent errors',countThen(errors.length,el('ol',{class:'fb-list'},...errors.map(e=>el('li',{},el('span',{class:'mono muted'},formatTime(e.at,zone)+' '),e.message,el('span',{class:'muted'},` · ${e.source}`)))))],
    ['Request references',countThen(ids.length,el('span',{class:'mono fb-ids'},ids.join(' ')))],
  ];
}
/** A count first; the list itself one "Show" further. */
const countThen=(n:number,list:HTMLElement):Child=>n?el('details',{class:'fb-count-list'},el('summary',{},`${n} · Show`),list):'None';
/** "Chrome 153 · Windows" from a user agent (the full string stays on hover and in the report). */
export function browserName(ua:string){
  const v=(re:RegExp)=>ua.match(re)?.[1];
  const name=v(/Edg\/(\d+)/)?`Edge ${v(/Edg\/(\d+)/)}`:v(/Firefox\/(\d+)/)?`Firefox ${v(/Firefox\/(\d+)/)}`:v(/Chrome\/(\d+)/)?`Chrome ${v(/Chrome\/(\d+)/)}`
    :v(/Version\/(\d+).*Safari/)?`Safari ${v(/Version\/(\d+).*Safari/)}`:'Browser';
  const os=/Windows/.test(ua)?'Windows':/iPhone|iPad/.test(ua)?'iOS':/Android/.test(ua)?'Android':/Mac OS X/.test(ua)?'macOS':/Linux/.test(ua)?'Linux':'';
  return [name,os].filter(Boolean).join(' · ');
}
const reporterOf=(r:FeedbackRow)=>r.reporter_name||'Unknown';
const factList=(facts:[string,Child][])=>el('dl',{class:'request-facts fb-facts'},...facts.flatMap(([k,v])=>[el('dt',{},k),el('dd',{},v)]));

// ---- Send feedback ----
type Box={kind:'mark'|'hide';x:number;y:number;w:number;h:number};type Area=Box&{node:HTMLElement};
const MIN_SIDE=0.02;
/** Opens Send feedback. The dialog opens at once; the page (with the error message still on it, the dialog left out) is
 *  copied on the next frame, so the key press or click that opened it is never held up by a copy of a long page. */
export function openFeedback(o:{kind?:Kind;error?:string}={}){
  if(!host?.me()||document.querySelector('.fb-dialog'))return;
  let shot:Snapshot|null=null,shotError='';
  // Report this problem (a click on an error message, which then goes) copies the page now, while the message is on it.
  if(o.error){try{shot=snapshot();}catch(e){shotError=(e as Error).message;}}
  const context=contextNow(o.error);
  // One key per dialog: sending again after a lost connection cannot file the report twice.
  const key=crypto.randomUUID();
  let kind:Kind=o.kind||'problem';
  const zone=host.zone();
  const d=showDialog('Send feedback','',(body,close,tryClose)=>formInto(body,close,tryClose,form=>{
    form.classList.add('fb-form');
    if(o.error)form.append(el('div',{class:'notice error fb-error',role:'note'},el('strong',{},'Error: '),o.error));
    // Kind: one choice, arrow keys move between them.
    const kinds=segmented('Kind of feedback',FEEDBACK_KINDS.map(k=>[k,KINDS[k].label,{icon:KINDS[k].icon}]),kind,k=>{kind=k;paint();},{cls:'fb-kinds'});
    const message=area(KINDS[kind].ask,'',4),expected=area('What did you expect? (optional)','',2);
    message.input.required=true;message.input.maxLength=FEEDBACK_LIMITS.message;expected.input.maxLength=FEEDBACK_LIMITS.expected;
    message.input.name='feedback-message';expected.input.name='feedback-expected';
    const paint=()=>{message.wrap.querySelector('label')!.firstChild!.textContent=KINDS[kind].ask;message.input.placeholder=KINDS[kind].hint;expected.wrap.hidden=kind!=='problem';};
    form.append(el('div',{class:'span-2 fb-kind-field'},el('span',{class:'pick-label',id:'fb-kind-label'},'What is it?'),kinds),message.wrap,expected.wrap);
    kinds.setAttribute('aria-labelledby','fb-kind-label');
    paint();

    // The screenshot: drawn from the copy taken before this dialog opened; Mark a spot and Hide area work on it.
    const include=check('Include screenshot',true,'');
    const frame=el('div',{class:'fb-frame'}),overlay=el('div',{class:'fb-overlay','aria-label':'Screenshot areas'});
    const caption=el('p',{class:'hint fb-caption'});
    const status=el('div',{class:'fb-shot-status',role:'status'});
    const areas:Area[]=[];let canvas:HTMLCanvasElement|null=null,tool:'mark'|'hide'|null=null;
    // Undo and redo keep the whole list of boxes: each draw, move, resize or removal is one step.
    const past:Box[][]=[],future:Box[][]=[];let before:Box[]|null=null;
    const boxes=():Box[]=>areas.map(({kind,x,y,w,h})=>({kind,x,y,w,h}));
    const begin=()=>{before??=boxes();};
    const commit=()=>{if(!before)return;if(JSON.stringify(before)!==JSON.stringify(boxes())){past.push(before);if(past.length>50)past.shift();future.length=0;}before=null;paintHistory();};
    const restore=(list:Box[])=>{areas.splice(0).forEach(a=>a.node.remove());for(const b of list)add(b.kind,b.x,b.y,b.w,b.h);paintCaption();};
    const step=(from:Box[][],to:Box[][],said:string)=>{commit();const next=from.pop();if(!next)return;to.push(boxes());restore(next);paintHistory();announce(said);};
    const undo=()=>step(past,future,'Undone'),redo=()=>step(future,past,'Redone');
    // A tool button arms drawing on the picture; from the keyboard it adds a box in the middle to move with the arrows.
    const toolButton=(k:'mark'|'hide',label:string,glyph:IconName,tip:string)=>{
      const b=el('button',{type:'button',class:'button secondary small','aria-pressed':'false','data-tip':tip},icon(glyph),label);
      b.addEventListener('click',e=>{if(e.detail===0){pick(null);begin();const a=add(k,0.35,0.35,0.3,0.3);commit();a.node.focus();}else pick(tool===k?null:k);});return b;};
    const markBtn=toolButton('mark','Mark a spot','target','Draw a box around what you mean');
    const hideBtn=toolButton('hide','Hide area','eyeOff','Cover something private before sending');
    const undoBtn=iconButton('Undo','undo',undo,{class:'small','data-tip':'Undo (Ctrl Z)'}),redoBtn=iconButton('Redo','redo',redo,{class:'small','data-tip':'Redo (Ctrl Shift Z)'});
    const paintHistory=()=>{undoBtn.disabled=!past.length;redoBtn.disabled=!future.length;};paintHistory();
    const screen=typeof navigator.mediaDevices?.getDisplayMedia==='function'&&!phoneQuery.matches
      ?withTip(button('Capture screen',()=>takeScreen(),'ghost small'),'camera','Use the browser’s screen capture instead of the drawn copy'):null;
    const tools=el('div',{class:'fb-tools',role:'toolbar','aria-label':'Screenshot tools'},markBtn,hideBtn,undoBtn,redoBtn,screen);
    const figure=el('figure',{class:'fb-shot'},frame,caption);
    const section=el('section',{class:'span-2 fb-shot-field','aria-label':'Screenshot'},el('div',{class:'fb-shot-head'},include.wrap,tools),status,figure);
    form.append(section);
    // Ctrl Z / Ctrl Shift Z (or Ctrl Y) anywhere in the screenshot section; the message box keeps its own text undo.
    section.addEventListener('keydown',e=>{
      if(!(e.ctrlKey||e.metaKey)||e.altKey||tools.hidden)return;const k=e.key.toLowerCase();
      if(k==='z'){e.preventDefault();if(e.shiftKey)redo();else undo();}else if(k==='y'){e.preventDefault();redo();}});
    function pick(next:'mark'|'hide'|null){
      tool=next;markBtn.setAttribute('aria-pressed',String(tool==='mark'));hideBtn.setAttribute('aria-pressed',String(tool==='hide'));
      overlay.classList.toggle('drawing',!!tool);
    }
    function place(a:Area){for(const [k,v] of [['left',a.x],['top',a.y],['width',a.w],['height',a.h]] as const)a.node.style.setProperty(k,`${v*100}%`);}
    function add(k:'mark'|'hide',x:number,y:number,w:number,h:number):Area{
      if(k==='mark')for(const old of areas.filter(a=>a.kind==='mark'))drop(old);
      const label=k==='mark'?'Marked spot':'Hidden area';
      const remove=el('button',{type:'button',class:'fb-area-remove','aria-label':`Remove ${label.toLowerCase()}`,'data-tip':'Remove'},icon('close'));
      const corners=(['nw','ne','sw','se'] as const).map(c=>el('span',{class:`fb-area-handle ${c}`,'data-corner':c,'aria-hidden':'true'}));
      const node=el('div',{class:`fb-area ${k}`,tabindex:'0',role:'group','aria-label':`${label}. Drag or use the arrow keys to move it, its corners or Shift with arrows to resize it, Delete to remove it.`},
        el('span',{class:'fb-area-label'},icon(k==='mark'?'target':'eyeOff'),label),remove,...corners);
      const a:Area={kind:k,x,y,w,h,node};areas.push(a);place(a);overlay.append(node);
      const removeIt=()=>{begin();drop(a);commit();markBtn.focus();announce(`${label} removed`);};
      remove.addEventListener('click',removeIt);
      node.addEventListener('keydown',e=>{
        if(e.target!==node)return;
        const s=e.altKey?0.002:0.02,dx=e.key==='ArrowLeft'?-s:e.key==='ArrowRight'?s:0,dy=e.key==='ArrowUp'?-s:e.key==='ArrowDown'?s:0;
        if(e.key==='Delete'||e.key==='Backspace'){e.preventDefault();removeIt();return;}
        if(!dx&&!dy)return;e.preventDefault();begin();
        if(e.shiftKey){a.w=clamp(a.w+dx,MIN_SIDE,1-a.x);a.h=clamp(a.h+dy,MIN_SIDE,1-a.y);}else{a.x=clamp(a.x+dx,0,1-a.w);a.y=clamp(a.y+dy,0,1-a.h);}
        place(a);});
      // One held arrow key is one undo step.
      node.addEventListener('keyup',commit);node.addEventListener('blur',commit);
      // Drag the box to move it; drag a corner to resize it.
      node.addEventListener('pointerdown',e=>{
        e.stopPropagation();if(e.button!==0||remove.contains(e.target as Node))return;e.preventDefault();node.focus();
        const corner=(e.target as HTMLElement).dataset.corner||'',r=overlay.getBoundingClientRect(),sx=e.clientX,sy=e.clientY,o={...a};
        node.setPointerCapture(e.pointerId);begin();
        const move=(m:PointerEvent)=>{const dx=(m.clientX-sx)/r.width,dy=(m.clientY-sy)/r.height;
          if(!corner){a.x=clamp(o.x+dx,0,1-o.w);a.y=clamp(o.y+dy,0,1-o.h);}
          else{
            if(corner.includes('w')){a.x=clamp(o.x+dx,0,o.x+o.w-MIN_SIDE);a.w=o.x+o.w-a.x;}else a.w=clamp(o.w+dx,MIN_SIDE,1-o.x);
            if(corner.includes('n')){a.y=clamp(o.y+dy,0,o.y+o.h-MIN_SIDE);a.h=o.y+o.h-a.y;}else a.h=clamp(o.h+dy,MIN_SIDE,1-o.y);
          }
          place(a);};
        const up=()=>{node.removeEventListener('pointermove',move);node.removeEventListener('pointerup',up);node.removeEventListener('pointercancel',up);commit();};
        node.addEventListener('pointermove',move);node.addEventListener('pointerup',up);node.addEventListener('pointercancel',up);
      });
      paintCaption();return a;
    }
    function drop(a:Area){a.node.remove();areas.splice(areas.indexOf(a),1);paintCaption();}
    // Drawing: press, drag, release on the picture.
    overlay.addEventListener('pointerdown',e=>{
      if(!tool||e.button!==0)return;e.preventDefault();overlay.setPointerCapture(e.pointerId);
      const r=overlay.getBoundingClientRect(),fx=(v:number)=>clamp((v-r.left)/r.width,0,1),fy=(v:number)=>clamp((v-r.top)/r.height,0,1);
      begin();const x0=fx(e.clientX),y0=fy(e.clientY),a=add(tool,x0,y0,0.001,0.001);
      const move=(m:PointerEvent)=>{const x1=fx(m.clientX),y1=fy(m.clientY);a.x=Math.min(x0,x1);a.y=Math.min(y0,y1);a.w=Math.max(Math.abs(x1-x0),0.001);a.h=Math.max(Math.abs(y1-y0),0.001);place(a);};
      const up=()=>{overlay.removeEventListener('pointermove',move);overlay.removeEventListener('pointerup',up);overlay.removeEventListener('pointercancel',up);
        // A click without a drag marks a small box around that point.
        if(a.w<0.01&&a.h<0.01){a.x=clamp(x0-0.06,0,0.88);a.y=clamp(y0-0.05,0,0.9);a.w=0.12;a.h=0.1;place(a);}
        commit();pick(null);};
      overlay.addEventListener('pointermove',move);overlay.addEventListener('pointerup',up);overlay.addEventListener('pointercancel',up);
    });
    function paintCaption(){
      const hidden=areas.filter(a=>a.kind==='hide').length,marked=areas.some(a=>a.kind==='mark');
      caption.replaceChildren(...[shot?.masked?`${plural(shot.masked,'private field')} hidden automatically`:'Private fields are hidden automatically',
        marked?'1 spot marked':'',hidden?`${plural(hidden,'area')} hidden by you`:''].filter(Boolean).join(' · '));
    }
    function showCanvas(c:HTMLCanvasElement){canvas=c;c.classList.add('fb-canvas');c.setAttribute('role','img');c.setAttribute('aria-label','Screenshot of the page');
      frame.replaceChildren(c,overlay);status.replaceChildren();tools.hidden=false;figure.hidden=!include.input.checked;paintCaption();}
    function failed(why:string){canvas=null;frame.replaceChildren();figure.hidden=true;tools.hidden=!screen;markBtn.hidden=hideBtn.hidden=undoBtn.hidden=redoBtn.hidden=true;
      status.replaceChildren(note(`The page could not be drawn${why?` (${why})`:''}.${screen?' Use Capture screen to share the real screen instead, or send without a screenshot.':' You can still send without a screenshot.'}`,'warning'));}
    async function takeScreen(){
      const dialog=form.closest('dialog')!;dialog.classList.add('fb-hiding');
      try{const c=await captureScreen();markBtn.hidden=hideBtn.hidden=undoBtn.hidden=redoBtn.hidden=false;areas.splice(0).forEach(a=>a.node.remove());past.length=future.length=0;paintHistory();showCanvas(c);}
      catch(e){if((e as Error).name!=='NotAllowedError')notify('Screen capture did not work. '+(e as Error).message,'warning');}
      finally{dialog.classList.remove('fb-hiding');}
    }
    include.input.addEventListener('change',()=>{const on=include.input.checked;figure.hidden=!on||!canvas;tools.hidden=!on;status.hidden=!on;});
    tools.hidden=true;figure.hidden=true;
    status.replaceChildren(el('div',{class:'fb-shot-wait'},el('span',{class:'spinner','aria-hidden':'true'}),'Preparing the screenshot…'));
    // After the dialog is drawn (next frame): the copy is the page as it was, without this dialog (feedback_capture skips it).
    const capture=()=>{
      if(!form.isConnected)return;
      if(!shot&&!shotError){try{shot=snapshot();}catch(e){shotError=(e as Error).message;}}
      const dialog=form.closest('dialog');
      if(dialog){dialog.dataset.captured='yes';if(shot)dialog.dataset.masked=String(shot.masked);}
      (window as unknown as {__fbSnapshot?:Snapshot|null}).__fbSnapshot=shot;  // the browser tests read the copy (masking)
      if(!shot){failed(shotError);return;}
      const taken=shot,started=performance.now(),copyMs=taken.ms;
      render(taken).then(c=>{if(!form.isConnected)return;showCanvas(c);
        if(dialog){dialog.dataset.captureMs=String(Math.round(copyMs+performance.now()-started));dialog.dataset.copied=String(taken.copied);}},
        e=>{if(form.isConnected)failed((e as Error)?.message||'');});
    };
    requestAnimationFrame(()=>window.setTimeout(capture));

    // Exactly what goes with the message, listed (nothing else is sent).
    const n=context.request_ids?.length||0,errs=context.recent_errors?.length||0;
    const summary=['Page, view, version, browser',errs?plural(errs,'recent error'):'',n?plural(n,'request reference'):''].filter(Boolean).join(', ');
    form.append(el('details',{class:'span-2 fb-details'},el('summary',{},el('strong',{},'Details sent: '),summary,el('span',{class:'fb-show'},' · Show details')),
      el('p',{class:'hint'},`Sent with your message so the report can be followed up. Only ${holders('feedback.read_all')} and you can read it.`),factList(contextFacts(context,zone))));

    return async()=>{
      let screenshot:FeedbackCreate['screenshot']=null,mark:FeedbackMark|null=null;
      if(include.input.checked&&canvas){
        const out=document.createElement('canvas');out.width=canvas.width;out.height=canvas.height;const g=out.getContext('2d')!;g.drawImage(canvas,0,0);
        // Hidden areas are covered in the picture itself: what is under them never leaves this browser.
        g.fillStyle='#64748B';for(const a of areas.filter(a=>a.kind==='hide'))g.fillRect(Math.floor(a.x*out.width),Math.floor(a.y*out.height),Math.ceil(a.w*out.width)+1,Math.ceil(a.h*out.height)+1);
        {const {media_type,data}=await encode(out,FEEDBACK_LIMITS.screenshot_bytes);screenshot={media_type,data};}
        const m=areas.find(a=>a.kind==='mark');
        if(m){const r=(v:number)=>Math.round(v*10000)/10000;mark={x:r(m.x),y:r(m.y),w:Math.min(r(m.w),1-r(m.x)),h:Math.min(r(m.h),1-r(m.y))};}
      }
      const sent=await request<FeedbackCreated>('/api/feedback',{method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify({...context,kind,message:message.input.value.trim(),
        expected:kind==='problem'&&expected.input.value.trim()?expected.input.value.trim():null,screenshot,mark} satisfies FeedbackCreate)});
      queueMicrotask(()=>notify(sent.matched_report_id?'Thank you. This was reported before; your report is linked to it, so it gets more attention.':'Thank you. Your feedback was sent.',
        'success',{label:'View my feedback',run:()=>host!.navigate('feedback?open='+encodeURIComponent(sent.id))}));
    };
  },'Send'));
  d.dialog.classList.add('fb-dialog');
  d.dialog.querySelector<HTMLElement>('textarea')?.focus();
}
const clamp=(v:number,lo:number,hi:number)=>Math.min(hi,Math.max(lo,v));
const announce=(text:string)=>{const live=document.querySelector('#sr-live');if(live){live.textContent='';setTimeout(()=>{live.textContent=text;},30);}};
function withTip(b:HTMLButtonElement,glyph:IconName,tip:string){b.prepend(icon(glyph));b.dataset.tip=tip;return b;}

// ---- My feedback and System › Feedback ----
const PAGE=FEEDBACK_LIMITS.page_size;
/** One collection for both: `mine` lists the reports this person sent; otherwise every report (Developers triage, Auditors read). */
export async function feedbackPage(target:HTMLElement,o:{mine:boolean;query:URLSearchParams;triage:boolean;github:boolean}){
  const zone=host!.zone();
  // The search and its options (JSON `search_options`) go to the server: they span every report before it counts and pages.
  const load=newest((page:number,q:string,so?:SearchOptions)=>{const opts=searchParam(so);
    return get<FeedbackList>(`/api/feedback?${new URLSearchParams({...(o.mine?{mine:'true'}:{}),page:String(page),...(q?{q}:{}),...(opts?{search_options:opts}:{})})}`);});
  const tableId=o.mine?'feedback-mine':'feedback';
  if(o.triage&&!o.mine&&!o.query.has('open'))openFiltered(tableId,{status:['New','Triaged','In progress']},tableSearch(tableId));
  // The first read asks for the search the list shows (kept across a redraw or a phone/desktop switch), as other server lists do.
  let q=tableSearch(tableId).trim(),so=tableSearchOptions(tableId),page=1,data=(await load(1,q,so))!,rows:FeedbackRow[]=data.rows;
  const when=(r:FeedbackRow)=>`${formatDate(r.created_at,zone)} ${formatTime(r.created_at,zone)}`;
  const firstLine=(r:FeedbackRow)=>r.message.split('\n')[0];
  const update=async(r:FeedbackRow,change:Partial<Pick<FeedbackRow,'status'|'duplicate_of'|'resolved_in'|'triage_note'>>)=>{
    const reply=await command<{report:FeedbackRow}>('feedback.update',{id:r.id,revision:r.revision,...change});
    rows=rows.map(x=>x.id===reply.report.id?reply.report:x);return reply.report;};
  const columns:Column<FeedbackRow>[]=[
    {key:'report',label:'Report',text:r=>`${r.message} ${r.expected||''}`,sort:false,cell:r=>el('span',{class:'fb-row-title'},el('span',{class:'fb-row-msg'},firstLine(r)),
      r.has_screenshot?el('span',{class:'fb-has-shot','data-tip':'Has a screenshot','aria-label':'Has a screenshot',role:'img'},icon('camera')):null)},
    {key:'kind',label:'Kind',facet:r=>KINDS[r.kind]?.label||r.kind,options:FEEDBACK_KINDS.map(k=>KINDS[k].label),cell:r=>kindBadge(r.kind)},
    {key:'status',label:'Status',facet:r=>STATUS_WORDS[r.status],options:FEEDBACK_STATUSES.map(s=>STATUS_WORDS[s]),ordered:FEEDBACK_STATUSES.map(s=>STATUS_WORDS[s]),
      sort:r=>FEEDBACK_STATUSES.indexOf(r.status),cell:r=>statusOf(r.status),
      edit:o.triage?{kind:'select',value:r=>r.status,options:()=>FEEDBACK_STATUSES.filter(s=>s!=='duplicate').map(s=>({value:s,label:STATUS_WORDS[s]})),
        save:async(list,value)=>{const out:FeedbackRow[]=[];for(const r of list)out.push(await update(r,{status:value as Status}));return out;},afterSave:async()=>{redraw();}}:undefined},
    {key:'page',label:'Page',facet:r=>host!.pageLabel(r.route),text:r=>host!.pageLabel(r.route)+' '+PATH(r.route),cell:r=>el('span',{class:'fb-page'},host!.pageLabel(r.route))},
    {key:'occurrences',label:'Occurrences',num:true,sort:r=>r.occurrences,text:r=>String(r.occurrences),cell:r=>el('span',{class:r.occurrences>1?'fb-count':'','data-tip':'Reported this many times'},String(r.occurrences))},
    {key:'reporter',label:'Reporter',facet:reporterOf,cell:reporterOf},
    {key:'version',label:'Version',facet:r=>r.app_version||'—',cell:r=>el('span',{class:'mono muted'},r.app_version||'—')},
    {key:'sent',label:'Sent',sort:r=>r.created_at,text:when,cell:r=>el('span',{class:'nowrap'},when(r))},
  ];
  const list=el('div',{});
  const more=el('div',{class:'dt-pager fb-more'});
  const count=el('span',{class:'muted'});
  const openRow=(r:FeedbackRow,position?:PanelPosition)=>reportPanel(r,{triage:o.triage,github:o.github,mine:o.mine,position,update,redraw});
  function redraw(){
    list.replaceChildren(dataTable<FeedbackRow>({id:tableId,noun:['report','reports'],rows,rowId:r=>r.id,columns,serverPaged:true,
      searchLabel:'Search feedback',searchPlaceholder:'Message, note or page',searchIn:'feedback',onSearch:(text,options)=>{q=text;so=options;page=1;return load(1,q,so).then(next=>{if(next){data=next;rows=next.rows;redraw();}},e=>{reportError(e);return false;});},
      defaults:{sort:['sent','desc'],hidden:o.mine?['occurrences','version','reporter']:[]},
      views:o.triage?()=>[{label:'Open',filters:{status:['New','Triaged','In progress']}},{label:'New',filters:{status:['New']}},{label:'All reports',filters:{status:[]}}]:undefined,
      empty:o.mine?['No feedback yet',`Send feedback from any page: the account menu, the footer, or ${SHORTCUT}.`]:['No feedback yet',`Reports people send from inside ${brand().short_name} appear here.`],
      onOpen:openRow,
      card:r=>({title:firstLine(r),line:el('span',{class:'fb-card-line'},kindBadge(r.kind),statusOf(r.status),el('span',{class:'muted'},when(r)))}),
    }).root);
    count.textContent=`${plural(rows.length,'report')}${data.total>rows.length?` of ${data.total}`:''}`;
    more.replaceChildren(...(data.total>rows.length?[button(`Show ${Math.min(PAGE,data.total-rows.length)} more`,async()=>{
      const next=await load(page+1,q,so);if(!next)return;page++;data=next;rows=[...rows,...next.rows.filter(n=>!rows.some(r=>r.id===n.id))];redraw();},'secondary small')]:[]));
  }
  redraw();
  target.append(o.mine?sectionHead('My feedback','',button('Send feedback',()=>openFeedback(),'primary',{'data-tip':SHORTCUT})):'',
    el('section',{class:'panel fb-panel'},list,more));
  const open=o.query.get('open');
  if(open){const r=rows.find(x=>x.id===open)||await get<FeedbackRow>('/api/feedback/'+encodeURIComponent(open)).catch(()=>null);if(r)openRow(r);}
}

/** A report's side panel: what was sent, the screenshot with its marked spot, where it happened, and (Developers) triage. */
function reportPanel(r:FeedbackRow,o:{triage:boolean;github:boolean;mine:boolean;position?:PanelPosition;update:(r:FeedbackRow,c:Partial<FeedbackRow>)=>Promise<FeedbackRow>;redraw:()=>void}){
  const zone=host!.zone();
  dockPanel({key:'feedback',className:'fb-dock',title:`${KINDS[r.kind]?.label||r.kind} · ${formatDate(r.created_at,zone)}`,position:o.position,mark:rowMark(r.id),
    build:d=>{
      const back=button('Open where it happened',()=>reopen(r),'primary small',{'data-tip':'Opens the page with the view, filters and grouping the report was sent from'});
      withTip(back,'external','Opens the page with the view, filters and grouping the report was sent from');
      const actions=el('div',{class:'fb-actions'},back,o.github?withTip(button('Create GitHub issue…',()=>githubDialog(r),'secondary small'),'external','Shows the issue text first; nothing private is included'):null,
        r.github_issue_url?el('a',{class:'link-button',href:r.github_issue_url,target:'_blank',rel:'noopener noreferrer'},icon('external'),'GitHub issue'):null);
      const shot=r.has_screenshot?screenshotView(r):emptyLine('No screenshot was sent.',null,'fb-no-shot');
      const said=el('section',{class:'fb-said'},el('div',{class:'fb-said-head'},statusOf(r.status),r.occurrences>1?badge(`Reported ${r.occurrences} times`,'warn'):null),
        el('p',{class:'fb-message','data-private':''},r.message),r.expected?el('div',{},el('h3',{class:'fb-sub'},'Expected'),el('p',{class:'fb-message','data-private':''},r.expected)):null);
      const reply=r.triage_note||r.resolved_in?factList([...(r.resolved_in?[['Fixed in',r.resolved_in] as [string,Child]]:[]),...(r.triage_note?[['Note',el('span',{class:'fb-message'},r.triage_note)] as [string,Child]]:[])]):null;
      // The original and the reports linked to this one are read from the server (they may be on no loaded page).
      const links=el('div',{class:'fb-links'}),triage=o.triage?triageForm(r,o,d):el('div',{});
      if(r.duplicate_of)void get<FeedbackRow>('/api/feedback/'+encodeURIComponent(r.duplicate_of)).then(x=>links.prepend(el('p',{},'Same as ',link(x,o))),()=>links.prepend(el('p',{class:'muted'},'Same as an earlier report')));
      if(!o.mine)void get<FeedbackList>(`/api/feedback?duplicate_of=${encodeURIComponent(r.id)}&page_size=100`).then(l=>{if(l.rows.length)links.append(el('h3',{class:'fb-sub'},`Linked reports (${l.total})`),el('ul',{class:'fb-list'},...l.rows.map(x=>el('li',{},link(x,o)))));},()=>{});
      d.body.replaceChildren(said,actions,shot,...(reply?[el('h3',{class:'fb-sub'},o.triage?'Reply to the reporter':'From the team'),reply]:[]),links,triage,
        el('h3',{class:'fb-sub'},'Details'),factList([...(o.mine?[]:[['Reporter',reporterOf(r)] as [string,Child]]),['Sent',`${formatDate(r.created_at,zone)} ${formatTime(r.created_at,zone)}`],...contextFacts(r,zone)]));
    }});
}
const link=(x:FeedbackRow,o:Parameters<typeof reportPanel>[1])=>{const b=el('button',{type:'button',class:'link-button'},`${KINDS[x.kind]?.label||x.kind}: ${x.message.split('\n')[0].slice(0,80)}`);
  b.addEventListener('click',()=>reportPanel(x,{...o,position:undefined}));return b;};
/** Every report, for the Same as picker (the shared searchable picker).
 *  shortcut: reads up to 10 pages of 100 (1,000 reports); a server-searched picker if a department ever has more. */
async function allReports(){
  const out:FeedbackRow[]=[];
  for(let page=1;page<=10;page++){const l=await get<FeedbackList>(`/api/feedback?page=${page}&page_size=100`);out.push(...l.rows);if(out.length>=l.total||!l.rows.length)break;}
  return out;
}

/** Triage in the panel: status, the linked original for a duplicate, the version it was fixed in and a note the reporter
 *  sees. One save; Undo puts the previous values back. */
function triageForm(r:FeedbackRow,o:Parameters<typeof reportPanel>[1],d:{close:()=>void;tryClose:()=>void}){
  const box=el('section',{class:'fb-triage','aria-label':'Triage'});
  box.append(el('h3',{class:'fb-sub'},'Triage'));
  // After a save the panel is drawn again from the saved report (once the form counts as saved, so nothing asks).
  let saved:FeedbackRow|null=null;
  formInto(box,()=>{if(!saved)return;o.redraw();reportPanel(saved,o);},d.tryClose,form=>{
    const status=select('Status',FEEDBACK_STATUSES.map(s=>({value:s,label:STATUS_WORDS[s]})),r.status);
    // Same as lists every other report: read only once Duplicate is chosen (opening a report reads none of them).
    const sameAs=(all:FeedbackRow[])=>select('Same as',all.filter(x=>x.id!==r.id&&!x.duplicate_of).map(x=>({value:x.id,label:`${KINDS[x.kind]?.label}: ${x.message.split('\n')[0].slice(0,60)}`,hint:`${reporterOf(x)} · ${formatDate(x.created_at,host!.zone())}`,keywords:`${x.message} ${host!.pageLabel(x.route)}`})),r.duplicate_of||'',false,'Not a duplicate');
    let original=sameAs([]),others:Promise<void>|null=null;
    const readOthers=()=>others??=allReports().then(all=>{const fresh=sameAs(all);original.wrap.replaceWith(fresh.wrap);original=fresh;sync();},
      e=>{others=null;original.wrap.replaceWith(original.wrap=readFailed('the reports for Same as',e,()=>void readOthers()));});
    const fixed=field('Fixed in version','text',r.resolved_in||'',{maxlength:80,placeholder:'e.g. 0.8.1'}),fixedWrap=fixed.wrap;
    const noteField=area('Note to the reporter',r.triage_note||'',2);noteField.input.maxLength=2000;noteField.input.name='feedback-triage-note';
    const sync=()=>{const dup=status.input.value==='duplicate';if(dup)void readOthers();original.wrap.hidden=!dup;original.input.required=dup;fixedWrap.hidden=status.input.value!=='fixed';};
    status.input.addEventListener('change',sync);sync();
    form.append(status.wrap,original.wrap,fixedWrap,noteField.wrap);
    return async()=>{
      const change:Partial<FeedbackRow>={};const s=status.input.value as Status;
      if(s!==r.status)change.status=s;
      const dup=s==='duplicate'?original.input.value||null:null;if(dup!==r.duplicate_of)change.duplicate_of=dup;
      const v=s==='fixed'?fixed.input.value.trim()||null:r.resolved_in;if(v!==r.resolved_in)change.resolved_in=v;
      const n=noteField.input.value.trim()||null;if(n!==r.triage_note)change.triage_note=n;
      if(!Object.keys(change).length){notify('Nothing changed.','info');return;}
      const next=await o.update(r,change);saved=next;notify(`Saved. ${STATUS_WORDS[next.status]}${next.status!==r.status?' (the reporter is told in their Inbox)':''}.`);
    };
  },'Save');
  return box;
}

/** The screenshot, with the spot the reporter marked; a click shows it full size. Each look is recorded in the audit log. */
function screenshotView(r:FeedbackRow){
  const src=`/api/feedback/${encodeURIComponent(r.id)}/screenshot`;
  const img=el('img',{src,alt:'Screenshot sent with the report',class:'fb-img',loading:'lazy'}) as HTMLImageElement;
  const frame=el('div',{class:'fb-frame'},img);
  if(r.mark){const m=el('span',{class:'fb-area mark static','aria-hidden':'true'});for(const [k,v] of [['left',r.mark.x],['top',r.mark.y],['width',r.mark.w],['height',r.mark.h]] as const)m.style.setProperty(k,`${v*100}%`);frame.append(m);}
  const open=el('button',{type:'button',class:'fb-shot-open','aria-label':'Show the screenshot full size'},frame);
  open.addEventListener('click',()=>{const d=showDialog('Screenshot','',body=>body.append(frame.cloneNode(true)));d.dialog.classList.add('fb-full');});
  img.addEventListener('error',()=>open.replaceWith(el('p',{class:'muted fb-no-shot'},'The screenshot is no longer kept (screenshots are deleted after 180 days).')));
  return el('figure',{class:'fb-shot'},open,r.mark?el('figcaption',{class:'hint'},'The box shows the spot the reporter marked.'):null);
}

/** Opens the page the report came from with the list views it had (filters, grouping, sort, layout). */
export function reopen(r:FeedbackRow){
  const v=(r.view_state||{}) as Record<string,any>,list=String(v.active_view||'').split(':')[1];
  if(list)openTableState(list,{filters:v.filters||{},group:v.group_by||'',sort:Array.isArray(v.sort)?[v.sort[0],v.sort[1]==='desc'?'desc':'asc']:null,layout:v.layout||'table'});
  closeDock();
  host!.navigate(ADDRESS(r.route));
}

/** Create GitHub issue: the sanitised text first (no names, no message unless ticked, no screenshot), then create or copy. */
function githubDialog(r:FeedbackRow){
  showDialog('Create GitHub issue',`This text is all that leaves ${brand().short_name}. Names, links and private text are removed; the screenshot never goes.`,(body,close)=>{
    const include=check('Include the message',false,'Off by default: the message may name people or patients.');
    const preview=el('div',{class:'fb-issue',role:'region','aria-label':'Issue preview','aria-live':'polite'});
    const actions=el('div',{class:'form-footer'});
    let result:FeedbackExportResult|null=null;
    const load=async()=>{preview.replaceChildren(working('Preparing the issue text…'));
      try{result=await command<FeedbackExportResult>('feedback.export_github',{id:r.id,include_message:include.input.checked,create_issue:false});}
      catch(e){preview.replaceChildren(note((e as Error).message,'error',e));return;}
      preview.replaceChildren(el('div',{class:'fb-issue-title'},el('span',{class:'muted'},'Title '),el('strong',{},result.title)),el('pre',{class:'fb-issue-body'},result.body));
      const copy=button('Copy text',async()=>{try{await navigator.clipboard.writeText(`${result!.title}\n\n${result!.body}`);notify('Issue text copied.');}catch{notify('Copying is blocked here. Select the text and press Ctrl+C.','info');}},'secondary');
      const create=result.configured&&!result.github_issue_url?button('Create issue',async()=>{
        const made=await command<FeedbackExportResult>('feedback.export_github',{id:r.id,include_message:include.input.checked,create_issue:true});
        close();notify('GitHub issue created.','success',made.github_issue_url?{label:'Open issue',run:()=>window.open(made.github_issue_url!,'_blank','noopener')}:undefined);},'primary'):null;
      actions.replaceChildren(button('Close',close,'ghost'),copy,...(create?[create]:[]),
        result.github_issue_url?el('a',{class:'button secondary',href:result.github_issue_url,target:'_blank',rel:'noopener noreferrer'},'Open issue'):'');
      if(!result.configured){const connect=el('a',{href:'#configuration/integrations'},'connect GitHub in Configuration › Integrations');
        connect.addEventListener('click',()=>close());
        preview.append(el('p',{class:'hint'},'GitHub is not connected, so copy the text and create the issue by hand, or ',connect,'.'));}};
    include.input.addEventListener('change',()=>void load());
    body.append(include.wrap,preview,actions);void load();
  });
}
