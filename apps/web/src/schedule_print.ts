/** The Schedule's paper renderer (D-127 slice 1): print markup built from the view's definition and its loaded sessions,
 *  never a copy of the screen. Week (rows × dated days) and Month (one row per week) in the approved target, variant A
 *  (design reference, v3): each service in a cell is one block tinted with its pill's hue, sessions
 *  stack in start-time order, trainees hang under their supervisor, one person per line, the name's last word and its
 *  marks never part (DL-057, dom.ts `nameMarks`). Nothing is hidden or shrunk: pages are cut by measuring; a row that does
 *  not fit starts the next page (title, header row, legend and footer repeat), a row taller than a page continues there.
 *  The legend lists, by kind, only what its page draws. Planner editing state (locks, selection, add, drag) is never drawn. */
import {el,colourOf,typeColour,formatTime,toLocal,labelOf,nameMarks,markOf,pillOf,registryKey,MARK_DEFAULTS,type LegendItem,type MarkMode} from './dom.js';
import {distinctNames} from './person_label.js';
import {teamOrder,supervises} from './planner_teams.js';
import {dutyColour,weekRowLabel} from './planner_week.js';
import {isWeekend,addDays,isClosed} from './planner_logic.js';
import {dutyCode} from './schedule_item.js';
import {weekOf} from './schedule.js';
import type {Row} from './types.js';
import type {PersonLabel} from './generated.js';

export type MarkDisplay=MarkMode;
/** What the renderer draws: the view (layout, rows, person label, marks) and the sessions it shows. */
export interface PrintData{
  mode:'week'|'month';
  /** The days in view (YYYY-MM-DD), in order; week start Monday 0 … Sunday 6. */
  days:string[];weekStart:number;zone:string;
  /** The sessions the view shows (its filters and search applied). */
  shifts:Row[];
  people:Map<string,Row>;services:Map<string,Row>;locations:Map<string,Row>;types:Map<string,Row>;
  /** People with a current external programme membership (the External rotator mark). */
  external:ReadonlySet<string>;
  /** Week rows: the view's Group by among session dimensions; anything else is one row. */
  rows:'service'|'type'|'location'|'none';
  label:PersonLabel;
  /** View settings › Marks (PlannerViewSettings.marks). */
  marks:{display:MarkDisplay;keys:readonly string[]};
  /** The legend registry (GET /api/legend); absent: built from the catalogues and the mark defaults. */
  registry?:readonly LegendItem[]|null;
}

/** Which participant fact draws each mark (the registry's fixed key set). */
const HAS:Record<string,(p:Row,d:PrintData,all:readonly Row[])=>boolean>={lead:p=>!!p.team_lead,sup:(p,_,all)=>supervises(p,all),tr:p=>p.participation==='trainee',
  ot:p=>!!p.overtime,ext:(p,d)=>d.external.has(String(p.person_id))};
/** Paper-only key entries the registry has no record for: the supervision tree's connector and the weekend tint. */
const TREE:LegendItem={kind:'mark',key:'tree',label:'Trainee under supervisor',short:'',colour:'',order:99};
const WEEKEND:LegendItem={kind:'day',key:'weekend',label:'Weekend',short:'',colour:''};

const dayHead=(d:string)=>new Date(d+'T12:00:00Z').toLocaleDateString('en-GB',{weekday:'short',timeZone:'UTC'});
/** The calendar weeks holding `days`, each its 7 days from the week start (Monday 0 … Sunday 6). */
function weeksOf(days:string[],weekStart:number):string[][]{
  const starts=new Set(days.map(x=>weekOf(x,weekStart)));
  return [...starts].map(start=>Array.from({length:7},(_,i)=>addDays(start,i)));
}
/** The registry from the catalogues, until GET /api/legend answers (same shape, same sources). */
function registryOf(d:PrintData):LegendItem[]{
  const svc=[...d.services.values()].map((r,order):LegendItem=>({kind:'service',key:String(r.id),label:labelOf(r),short:String(r.short_label||r.short_name||labelOf(r)),colour:colourOf(r,String(r.id)),order}));
  const duty=[...d.types.values()].map((r,order):LegendItem=>({kind:'duty',key:String(r.id),label:labelOf(r),short:dutyCode(r as {name?:string;short_code?:string}),colour:typeColour(String(r.id),r.definition?.colour),order}));
  return [...svc,...duty,...MARK_DEFAULTS,WEEKEND];
}

/** Builds the pages. `frame` makes an empty page (its title block, the slot for rows, legend and footer); `host` lays
 *  pages out at paper size so they can be measured. */
export function printPages(d:PrintData,frame:()=>HTMLElement,host:HTMLElement,legend:boolean,sign:HTMLElement|null):HTMLElement[]{
  const reg=[...(d.registry||registryOf(d)),TREE],byKey=new Map(reg.map(e=>[e.kind+':'+e.key,e]));
  const svcOf=(s:Row)=>String((s.service_ids as string[]|undefined)?.[0]||'');
  const service=(id:string):LegendItem=>byKey.get('service:'+id)||{kind:'service',key:id,label:'No service',short:'No service',colour:'slate'};
  // A session's duty: the registry's code and colour; a snapshot older than its type keeps its own code.
  const duty=(s:Row):LegendItem=>{const e=byKey.get('duty:'+s.assignment_type_id);
    return {kind:'duty',key:String(s.assignment_type_id),label:String(e?.label||s.type_snapshot?.name||''),short:String(s.type_snapshot?.short_code||e?.short||dutyCode(String(s.type_snapshot?.name||''))),colour:e?.colour||dutyColour(s)};};
  const marks=d.marks.display==='off'?[]:reg.filter(e=>e.kind==='mark'&&e.on!==false&&HAS[e.key]&&d.marks.keys.includes(e.key));
  const sessions=[...d.shifts].sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end)||String(a.type_snapshot?.name).localeCompare(String(b.type_snapshot?.name)));
  const dayOf=(s:Row)=>toLocal(s.start,d.zone).slice(0,10);
  const byDay=new Map<string,Row[]>();for(const s of sessions){const k=dayOf(s);byDay.set(k,[...(byDay.get(k)||[]),s]);}
  const chip=(s:Row)=>{const e=duty(s);return el('span',{class:'d duty-'+e.colour,'data-k':'duty:'+e.key},e.short);};
  // One person per line: the view's label (week) or initial and surname (month); the short form kept for a name that needs 3 lines.
  // Both expand only where they clash among the people printed, as on screen (person_label.ts distinctNames).
  const printed=sessions.flatMap(s=>(s.participants as Row[]).map(p=>d.people.get(p.person_id)));
  const shortOf=distinctNames(printed,{parts:['short'],honorific:d.mode==='month'?'none':d.label.honorific}),fullOf=d.mode==='month'?shortOf:distinctNames(printed,d.label);
  const person=(p:Row,all:readonly Row[])=>{
    const r=d.people.get(p.person_id),on=marks.filter(m=>HAS[m.key](p,d,all));
    const short=r?shortOf(r):'—',full=r?fullOf(r):'—';
    const n=el('span',{class:'nm'},...nameMarks(full,on.map(m=>markOf(m,d.marks.display as Exclude<MarkMode,'off'>))));
    if(on.length)n.dataset.k=on.map(m=>'mark:'+m.key).join(' ');
    if(full!==short)n.dataset.short=short;
    return n;
  };
  /** The session's people as the supervision tree (planner_teams.ts teamOrder): trainees under their supervisor. */
  const tree=(s:Row,heads:(li:HTMLElement)=>void)=>{
    const stack:HTMLElement[]=[];
    for(const {p,depth} of teamOrder(s.participants as Row[])){
      const li=el('li',{},person(p,s.participants as Row[]));
      if(depth===0){heads(li);stack.length=0;stack.push(li);continue;}
      const parent=stack[Math.min(depth-1,stack.length-1)];
      let ul=parent.querySelector<HTMLElement>(':scope>ul.tree');if(!ul){ul=el('ul',{class:'tree','data-k':'mark:tree'});parent.append(ul);}
      ul.append(li);stack.length=depth;stack.push(li);
    }
  };
  const time=(s:Row)=>`${formatTime(s.start,d.zone)}–${formatTime(s.end,d.zone).replace(/^00:00$/,'24:00')}`;
  // Week: a session is its time and duty code, then its people, one per line.
  const weekSession=(s:Row)=>{const names=el('ul',{class:'names'});tree(s,li=>names.append(li));
    return el('div',{class:'s','data-piece':'','data-session':s.id},el('div',{class:'sh'},el('span',{class:'t'},time(s)),chip(s),isClosed(s)?el('span',{class:'t'},'Closed'):''),names);};
  // Month: each person (or supervision team) on its own line after the duty code.
  const monthEntries=(s:Row)=>{const out:HTMLElement[]=[];
    tree(s,li=>out.push(el('li',{'data-piece':'','data-session':s.id},chip(s),el('div',{},...li.childNodes))));
    if(!out.length)out.push(el('li',{'data-piece':'','data-session':s.id},chip(s),el('div',{},isClosed(s)?'Closed':'—')));
    return out;};
  // Variant A: one tinted block per service in a cell, its pill on top (except where the row already names it).
  const cell=(day:string,list:Row[],head:boolean)=>{
    const td=el('td',{class:isWeekend(day)?'we':'','data-k':isWeekend(day)?'day:weekend':''});
    if(d.mode==='month')td.append(el('div',{class:'dn'},String(Number(day.slice(8)))));
    const groups=new Map<string,Row[]>();for(const s of list){const k=svcOf(s);groups.set(k,[...(groups.get(k)||[]),s]);}
    for(const [id,group] of groups){const e=service(id);
      const blk=el('div',{class:'blk duty-'+e.colour,'data-k':'service:'+id});
      if(head)blk.append(el('div',{class:'gh'},pillOf(e)));
      if(d.mode==='month')blk.append(el('ul',{class:'en'},...group.flatMap(monthEntries)));else blk.append(...group.map(weekSession));
      td.append(blk);
    }
    return td;
  };

  // Sections: each a header row and its body rows. Week: one per 7 days (rows by the view's group); month: weeks as rows.
  type Section={head:HTMLTableRowElement;rows:HTMLTableRowElement[]};
  const sections:Section[]=[];
  if(d.mode==='month'){
    const weeks=weeksOf(d.days,d.weekStart),inView=new Set(d.days);
    const head=el('tr',{},el('th',{class:'wk',scope:'col'},'Week'),...(weeks[0]||[]).map(x=>el('th',{class:'day'+(isWeekend(x)?' we':''),scope:'col'},dayHead(x)))) as HTMLTableRowElement;
    sections.push({head,rows:weeks.map(w=>{const [wk,range]=weekRowLabel(w.filter(x=>inView.has(x))).split(' · ');
      return el('tr',{},el('th',{class:'wk',scope:'row'},el('b',{},wk),range),...w.map(x=>inView.has(x)?cell(x,byDay.get(x)||[],true):el('td',{class:'out'}))) as HTMLTableRowElement;})});
  }else{
    const keyOf=(s:Row)=>d.rows==='service'?svcOf(s):d.rows==='type'?String(s.assignment_type_id):d.rows==='location'?String(s.location_id||''):'';
    const noun={service:'Service',type:'Duty type',location:'Place',none:''}[d.rows];
    for(const week of weeksOf(d.days,d.weekStart).map(w=>w.filter(x=>d.days.includes(x)))){
      const inWeek=sessions.filter(s=>week.includes(dayOf(s))),keys=[...new Set(inWeek.map(keyOf))];
      const label=(k:string,s:Row)=>d.rows==='service'?pillOf(service(k)):d.rows==='type'?pillOf(duty(s),duty(s).label):d.rows==='location'?labelOf(d.locations.get(k)):'';
      const head=el('tr',{},el('th',{class:'lab',scope:'col'},noun),...week.map(x=>el('th',{class:'day'+(isWeekend(x)?' we':''),scope:'col'},`${dayHead(x)} ${Number(x.slice(8))}`))) as HTMLTableRowElement;
      sections.push({head,rows:keys.map(k=>{const mine=inWeek.filter(s=>keyOf(s)===k);
        return el('tr',{},el('th',{class:'lab',scope:'row'},label(k,mine[0])),...week.map(x=>cell(x,mine.filter(s=>dayOf(s)===x),d.rows!=='service'))) as HTMLTableRowElement;})});
    }
  }

  // ---- Pages, by measuring ----
  const pages:HTMLElement[]=[];
  const over=(p:HTMLElement)=>p.scrollHeight>p.clientHeight+.5;
  const fitNames=(row:HTMLElement)=>{  // a full name that would need 3 lines takes the short form ("L. Surname")
    for(const n of row.querySelectorAll<HTMLElement>('.nm[data-short]')){const lh=parseFloat(getComputedStyle(n).lineHeight)||17;
      if(n.getBoundingClientRect().height>2*lh+4){const marks=[...n.querySelectorAll('.nw>:is(abbr,.mi)')];n.replaceChildren(...nameMarks(n.dataset.short!,marks));delete n.dataset.short;}}};
  for(const sec of sections){
    let page!:HTMLElement,body!:HTMLElement;
    const open=()=>{page=frame();page.classList.add('xs');
      body=el('tbody');page.querySelector('[data-rows]')!.replaceWith(el('table',{class:'grid'+(d.mode==='month'?' month':'')},el('thead',{},sec.head.cloneNode(true)),body));
      host.replaceChildren(page);pages.push(page);};
    const keyed=()=>{if(!legend)return;const k=keyOf(page),was=page.querySelector('.legend');if(was)was.replaceWith(k);else page.querySelector('table')!.after(k);};
    const finish=()=>{const p=pages.at(-1)!;p.remove();};
    open();
    const queue=[...sec.rows];
    while(queue.length){
      const r=queue.shift()!;body.append(r);fitNames(r);keyed();
      if(!over(page))continue;
      if(body.children.length>1){r.remove();keyed();finish();open();body.append(r);keyed();if(!over(page))continue;}
      const rest=split(page,r);if(rest)queue.unshift(rest);  // a row taller than a page continues on the next
      finish();if(queue.length)open();
    }
    if(page.isConnected)finish();
  }
  if(sign&&pages.length)pages.at(-1)!.querySelector('.export-foot')?.before(sign.cloneNode(true));
  return pages;

  /** The page's key: the registry entries its grid draws, grouped by kind (dom.ts registryKey). */
  function keyOf(page:HTMLElement):HTMLElement{
    const drawn=new Set([...page.querySelectorAll<HTMLElement>('table [data-k]')].flatMap(n=>n.dataset.k!.split(' ')));
    const extra=[...drawn].filter(k=>k.startsWith('service:')&&!byKey.has(k)).map(k=>service(k.slice(8)));
    return registryKey([...reg.filter(e=>drawn.has(e.kind+':'+e.key)),...extra],d.marks.display==='letters'?'letters':'icons');
  }
  /** Moves the row's last pieces (sessions, or month entries) into a "(continued)" row until the page fits. */
  function split(page:HTMLElement,r:HTMLTableRowElement):HTMLTableRowElement|null{
    const label=r.cells[0].cloneNode(true) as HTMLElement;label.append(el('span',{class:'cont'},' (continued)'));
    const rest=el('tr',{},label,...[...r.cells].slice(1).map(c=>{const n=c.cloneNode(false) as HTMLElement;const dn=c.querySelector(':scope>.dn');if(dn)n.append(dn.cloneNode(true));return n;})) as HTMLTableRowElement;
    while(over(page)){
      const movable=[...r.querySelectorAll<HTMLElement>('[data-piece]')].filter(x=>x!==x.closest('td')!.querySelector('[data-piece]'));
      if(!movable.length)break;
      const last=movable.reduce((a,b)=>b.getBoundingClientRect().bottom>a.getBoundingClientRect().bottom?b:a);
      const td=last.closest('td')!,to=rest.cells[[...r.cells].indexOf(td as HTMLTableCellElement)],blk=last.closest<HTMLElement>('.blk')!;
      let shell=[...to.querySelectorAll<HTMLElement>(':scope>.blk')].find(b=>b.dataset.k===blk.dataset.k&&b.dataset.from===blk.dataset.k);
      if(!shell){shell=blk.cloneNode(false) as HTMLElement;shell.dataset.from=blk.dataset.k;const gh=blk.querySelector(':scope>.gh');if(gh)shell.append(gh.cloneNode(true));
        if(d.mode==='month')shell.append(el('ul',{class:'en'}));to.querySelector(':scope>.dn')?to.querySelector(':scope>.dn')!.after(shell):to.prepend(shell);}
      (shell.querySelector(':scope>.en')||shell).prepend(last);
      if(!blk.querySelector('[data-piece]'))blk.remove();
    }
    return rest.querySelector('[data-piece]')?rest:null;
  }
}
