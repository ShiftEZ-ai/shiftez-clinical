import {el,PALETTE,rankColour,phoneQuery,type Child,departmentToday,ruleFacts,plural,button,iconButton,toLocal,legend} from './dom.js';
import {readFailed} from './dom.js';  // a failed read (DL-045)
import {personName} from './person_label.js';
import {get,background,ApiError,sessionChanged,onSessionReset,keptRead,keptAnswers,type KeptReadOptions} from './api.js';
import {dockPanel} from './side_panel.js';
import {skeleton} from './loading.js';
import {timelineView,timelineAgenda,timelineWindow,dayNumber,plusDays,plusMonths,spanWords,lengthWords,type Bar,type BarStyle,type Layer,type TimelineRow,type Zone} from './timeline.js';
import type {TableSpec,TimelineDraw} from './table_view.js';
import {AWAY_KINDS,awayOf,type AwayKind} from './rota_availability.js';
import type {TimelineConflict,TimelineItem,TimelineLayer,TimelinePerson,TimelineResponse,TimelineSegment} from './generated.js';

let catalogue:TimelineLayer[]=[];

/**
 * People › Timeline: the shared Timeline layout (timeline.ts) of the People list, drawn from GET /api/timeline (the
 * person timeline contract). The list keeps its toolbar, rows and groups; the server gives each person's items, duty
 * density and conflicts, and the layers (their kind, words and style), so nothing here lists layers. Offered only where
 * the endpoint answers (timelineAvailable).
 */
/** How each layer's `style_key` is drawn (presentation only; the layers themselves are the server's). Unknown keys draw
 *  as plain bars. Hues are duty-type hues; nothing here is red (red is only a conflict). */
const STYLES:Record<string,{style:BarStyle|'heat'|'conflict';hue?:string}>={rotation:{style:'block'},attachment:{style:'outline',hue:'teal'},
  leave:{style:'leave'},'time-away':{style:'leave'},academic:{style:'protected'},protected:{style:'protected'},overtime:{style:'offer'},
  eligibility:{style:'rail',hue:'teal'},grant:{style:'rail',hue:'violet'},programme:{style:'rail',hue:'violet'},rank:{style:'milestone',hue:'indigo'},
  pending:{style:'pending'},density:{style:'heat'},conflict:{style:'conflict'}};
const HUES=PALETTE.filter(c=>c!=='coral'&&c!=='slate');
const hueOf=(id:string)=>{let h=0;for(const ch of id)h=(h*31+ch.charCodeAt(0))>>>0;return HUES[h%HUES.length];};

let available:Promise<boolean>|null=null;
// Shared by every render of the page: a refresh (a pulse, a save) rebuilds People and its timeline. Per window, kept
// across rebuilds (api.ts keptRead; maintenance): a rebuild joins the read on its way or draws its answer. Only a change that
// can alter a timeline (every live scope but inbox and feedback), a save or a sign-out drops it; a read asked after that
// waits for the one on its way, then reads once (queueBehindInFlight). A department year can take the server tens of
// seconds: a fresh read per refresh never landed while changes kept coming, and piled up on the server (FE-HANG, 5 Nov
// 2026). `shown`: the last answer drawn, which a rebuilt timeline shows at once instead of a skeleton.
// shortcut: the 6 newest windows (a department year is large); older ones read again.
const WINDOW_READS:KeptReadOptions={keepScopes:['inbox','feedback'],cap:6,queueBehindInFlight:true};
let shown:{key:string;data:TimelineResponse}|null=null;
onSessionReset(()=>{available=null;catalogue=[];shown=null;});
/** Whether the server has the person timeline (GET /api/timeline): asked once per session. Only a 404 or a refusal (403) means no; any
 *  other failure (a busy server, a reload racing the session) keeps the timeline, whose own read then says what failed
 *  with Retry, instead of silently swapping a saved Timeline layout for the table. That answer is asked again next time. */
export function timelineAvailable():Promise<boolean>{
  const today=departmentToday();
  available??=background<TimelineResponse>(`/api/timeline?start=${today}&end=${plusDays(today,1)}&layers=&limit=1`).then(data=>{catalogue=data.layers;return true;},e=>{const missing=e instanceof ApiError&&(e.status===404||e.status===403);if(!missing)available=null;return !missing;});
  return available;
}

/** A conflict's days, from its instants in the department's zone (the server sends UTC), else the whole window. */
const conflictDays=(c:TimelineConflict,start:string,end:string,zone:string):[string,string]=>{
  // The end is exclusive: the day after its last included instant (an end at 00:00:30 still covers that day).
  const a=c.start?toLocal(c.start,zone).slice(0,10):start;
  const b=c.end?plusDays(toLocal(new Date(Date.parse(c.end)-1).toISOString(),zone).slice(0,10),1):end;
  return [a,b>a?b:plusDays(a,1)];
};

// ---- Where is everyone (one line per person; approved v2 + D48) ----
// The server owns the line (GET /api/timeline with `bucket`: TimelinePerson.segments):
// which stretch wins a day (outside the department › leave › academic › protected › away › worked in › home rotation),
// the areas worked and their clinics' counts. This draws its segments. Away stretches are named and, too narrow for
// words, drawn as Who's available's strip marks (rota_availability.ts `awayOf`, `AWAY_KINDS`: one mapping for both).
/** An away kind's words, the strip's key's (Leave, Academic or protected, Rotation elsewhere, Not in the department). */
const awayWord=(k:AwayKind)=>AWAY_KINDS.find(([x])=>x===k)![1];
/** Drawing-only keys in the view's `hidden`: toggling them never reads again. `legend` is the retired Legend toggle
 *  (D-147: the key is always shown, one line below the timeline, DL-063); a saved view may still list it, read past. */
const DRAW_ONLY=['short','legend'];
/** The Show toggles (View settings), saved in the view's `hidden`, with the marks they draw. `server`: a hidden one
 *  falls through on the server; Short stretches are drawing only (folded when hidden, the built-in view's default). */
const SHOW:{key:string;label:string;swatches:string[];server?:boolean;fixed?:boolean}[]=[{key:'work',label:'Worked in',swatches:['tl-block duty-teal'],server:true},
  {key:'rotation',label:'Rotations',swatches:['tl-outline duty-teal','ext'],server:true},{key:'leave',label:awayWord('leave'),swatches:['tl-leave'],server:true},
  {key:'academic',label:awayWord('protected'),swatches:['tl-protected'],server:true},{key:'draft',label:'Not published yet',swatches:['tl-block duty-teal draft']},
  // The server always sends it (rank memberships): never hidden.
  {key:'outside',label:awayWord('out'),swatches:['tl-out'],fixed:true},{key:'short',label:'Short stretches',swatches:[]}];
/** A leave type's own Show key (a view key is at most 40 characters). */
const typeKey=(id:string)=>`lt-${id}`.toLowerCase();
/** What the line asks for: no item layers, its columns, and what is hidden (hidden layers and leave types fall
 *  through the server's precedence; Not published yet off reads published work only). */
const lineQuery=(bucket:string,hidden:string[])=>{
  const hide=SHOW.filter(x=>x.server&&hidden.includes(x.key)).map(x=>x.key),types=hidden.filter(k=>k.startsWith('lt-')).map(k=>k.slice(3));
  return `&layers=&bucket=${bucket}${hide.length?`&hide=${hide.join(',')}`:''}${types.length?`&hide_leave_types=${types.join(',')}`:''}${hidden.includes('draft')?'&source=published':''}`;};
/** Area hues: one each (D48: Proc and Ophtha never share), duty hues and the added area hues; never coral (a problem),
 *  indigo (academic time) or slate (leave). */
const AREA_HUES=['teal','amber','violet','sky','pink','magenta','green','brown'];
const SOURCE:Record<TimelineSegment['source'],string>={published:'Published',draft:'Draft',usual:'Usual plan'};
/** What a stretch is, in its card and words: "worked in" is where the duties were, never a placement (TIMELINE-UX);
 *  away stretches take Who's available's words. */
const kindOf=(g:TimelineSegment)=>{const a=awayOf(g);return a?awayWord(a.kind):g.kind==='work'?'Worked in':'Rotation';};
/** One person's line: the server's segments as bars, each with its card and its words for a screen reader. Worked in is
 *  the area's tint, a rotation its tint framed, an away rotation a neutral outline, leave neutral, academic and protected
 *  time indigo; only worked-in duties can be not published yet (hatched): a rotation or leave is a record. */
/** Each mark's word and place in the key (one word each; the away words are Who's available's). */
const MARK:Record<TimelineSegment['kind']|'draft',[string,number]>={work:['Worked in',1.1],home:['Rotation',1.2],away:[awayWord('away'),1.3],
  leave:[awayWord('leave'),1.4],academic:[awayWord('protected'),1.5],protected:[awayWord('protected'),1.5],outside:[awayWord('out'),1.6],draft:['Not published yet',1.7]};
export function personLine(p:TimelinePerson,o:{full:boolean;hueOf:(key:string,colour?:string|null)=>string;sub:string;
  /** An area's place in the key: the department's area order (name order, as its hue and the server's "AA · ZZ"). */areaOrder?:(short:string)=>number}):Bar[]{
  return (p.segments||[]).map(g=>{const areas=g.areas||[],clinics=g.clinics||[];
    const leave=g.kind==='leave',style:BarStyle=leave?'leave':g.kind==='academic'||g.kind==='protected'?'protected':g.kind==='home'?'outline':g.kind==='outside'?'out':'block';
    const draft=g.kind==='work'&&g.source!=='published',external=g.kind==='away';
    const what=leave?g.leave_type||g.label:g.label,short=leave?g.leave_short_name||g.short_label:g.short_label;
    const hue=g.kind==='work'?o.hueOf(areas[0]?.short_name||short,areas[0]?.colour):g.kind==='home'?o.hueOf(short):undefined;
    const status=leave?(g.status?g.status[0].toUpperCase()+g.status.slice(1).replace(/_/g,' '):'Recorded'):g.kind==='work'?[SOURCE[g.source],(g.rotas||[]).length>1?plural(g.rotas!.length,'rota'):g.rota].filter(Boolean).join(' · '):'';
    const end=g.ends_on,dates=spanWords(g.starts_on,end);
    const facts:[string,Child][]=[['Dates',dates],['Duration',plural(dayNumber(end)-dayNumber(g.starts_on),'day')],
      ...(leave?[['Type',what] as [string,Child]]:[]),
      ...(clinics.length?[['Clinics',el('div',{class:'tl-clinics'},...clinics.map(c=>el('div',{},`${c.label} · ${c.count}`)))] as [string,Child]]:[]),
      // A stretch across several rotas lists them, one a line (their names may hold " · ").
      ...((g.rotas||[]).length>1?[['Rotas',el('div',{class:'tl-clinics'},...g.rotas!.map(r=>el('div',{},r)))] as [string,Child]]:[]),
      ...(status?[['Status',status] as [string,Child]]:[])];
    const swatch=`tl-${style}${hue?' duty-'+hue:''}${external?' ext':''}`,mark=g.kind==='academic'?'protected':g.kind;
    const keys:NonNullable<Bar['keys']>=[{key:'mark:'+mark,mark:swatch,words:MARK[g.kind][0],order:MARK[g.kind][1]},
      ...(draft?[{key:'mark:draft',mark:swatch+' draft',words:MARK.draft[0],order:MARK.draft[1]}]:[]),
      // Worked in: each area's colour, its short name with its full name.
      ...areas.map(a=>({key:'area:'+a.short_name,mark:`tl-block duty-${o.hueOf(a.short_name,a.colour)}`,words:a.short_name&&a.short_name!==a.label?`${a.short_name} ${a.label}`:a.label,
        order:o.areaOrder?.(a.short_name)??0})),
      // A bare short label (OG, Anes, Prof, EU) whose full name differs: the key gives it.
      ...(g.kind!=='work'&&g.kind!=='outside'&&!o.full&&short&&short!==what?[{key:'code:'+short,mark:'',words:what,order:2,code:short}]:[])];
    return {id:`${p.id}:${g.id}`,start:g.starts_on,end,label:o.full?what:short,short,full:what,style,hue,draft,external,code:leave?short:undefined,keys,
      aria:`${p.display_name}, ${what}, ${dates}, ${[kindOf(g),status].filter(Boolean).map(x=>x[0].toLowerCase()+x.slice(1)).join(', ')}`,
      card:()=>[el('div',{class:'tl-card-head'},el('span',{class:`tl-swatch tl-${style}${hue?' duty-'+hue:''}${draft?' draft':''}${external?' ext':''}`,'aria-hidden':'true'}),el('strong',{},what),
          !o.full&&short&&short!==what?el('span',{class:'code-chip'},short):null),
        el('div',{class:'muted'},[kindOf(g),p.display_name,o.sub].filter(Boolean).join(' · ')),ruleFacts(facts)]};});
}
/** One hue per area across the view (D48): a set colour stays, the others take the free area hues in name order. Keys are
 *  short names, so a rotation and the area it is named after (AA) share their hue. */
export function areaHues(people:TimelinePerson[]){
  const set=new Map<string,string|null>();
  for(const p of people)for(const g of p.segments||[]){
    if(g.kind==='work')for(const a of g.areas||[]){if(!set.get(a.short_name))set.set(a.short_name,a.colour||null);}
    else if((g.kind==='away'||g.kind==='home')&&!set.has(g.short_label))set.set(g.short_label,null);}
  const out=new Map<string,string>(),used=new Set<string>();
  for(const [k,c] of set)if(c&&AREA_HUES.includes(c)){out.set(k,c);used.add(c);}
  const free=AREA_HUES.filter(h=>!used.has(h)),pool=free.length?free:AREA_HUES;let i=0;
  for(const k of [...set.keys()].sort())if(!out.has(k))out.set(k,pool[i++%pool.length]);
  return (k:string,colour?:string|null)=>colour&&AREA_HUES.includes(colour)?colour:out.get(k)||AREA_HUES[0];
}
/** The key's place for each area: name order, under 1 (the legend's colours come before its marks). */
export function areaOrder(people:TimelinePerson[]){
  const names=[...new Set(people.flatMap(p=>(p.segments||[]).flatMap(g=>(g.areas||[]).map(a=>a.short_name))))].sort((a,b)=>a.localeCompare(b));
  return (short:string)=>(names.indexOf(short)+1)/(names.length+1);
}

/** People › Timeline for a dataTable of people (rows with `id` and `display_name`). */
/** `person`: one person's timeline (GET /api/people/{id}/timeline) instead of the department's. */
/** `leaveTypes`: the department's leave types ([id, name], its catalogue), the line's Show › Leave chips. */
export function peopleTimeline<T extends {id:string;display_name?:unknown}>(o:{open?:(row:T)=>void;person?:string;leaveTypes?:()=>[string,string][];
  /** The areas each person worked in changed (a line read landed): the list redraws if it filters or groups by them. */areasChanged?:()=>void}):NonNullable<TableSpec<T>['timeline']>&{worksIn:(id:string)=>string[]}{
  // One read per window, every page of it (the server pages by 200): kept (WINDOW_READS) while the list redraws.
  let layers:TimelineLayer[]=[],full=false,zone='UTC';
  // `line`: the one-line read asks only for what it draws and its columns (a week, or a month at 9 or 12 months and on
  // a phone); otherwise the shown layers (the timeline's layers= opt-in).
  const readKey=(start:string,end:string,hidden:string[],line='')=>JSON.stringify([o.person||'',start,end,[...hidden].sort(),line]);
  // `quiet`: nobody waits on it (Works in's values): an app read, without the page's progress bar.
  const fetchAll=async(start:string,end:string,hidden:string[],line:string,quiet:boolean)=>{const ask=quiet?background:get;
    if(!line&&!catalogue.length)await timelineAvailable();
    const known=layers.length?layers:catalogue,selected=known.filter(l=>!hidden.includes(l.kind)).map(l=>l.kind).join(',');
    const query=`start=${start}&end=${end}`+(line?lineQuery(line,hidden):known.length?`&layers=${encodeURIComponent(selected)}`:'');
    const first=await ask<TimelineResponse>(o.person?`/api/people/${encodeURIComponent(o.person)}/timeline?${query}`:`/api/timeline?${query}&limit=200`);
    const people=[...first.people];let next=first;
    while(next.has_more){next=await ask<TimelineResponse>(`/api/timeline?${query}&limit=200&offset=${next.offset+next.people.length}`);people.push(...next.people);}
    return {...first,people,has_more:false};};
  const read=(start:string,end:string,hidden:string[],line='',quiet=false)=>{const key=readKey(start,end,hidden,line);
    return keptRead(key,()=>fetchAll(start,end,hidden,line,quiet).then(all=>{shown={key,data:all};return all;}),WINDOW_READS).p;};
  const layerOf=(kind:string)=>layers.find(l=>l.kind===kind);
  const styleOf=(kind:string)=>STYLES[layerOf(kind)?.style_key||'']||{style:'block' as const};

  function itemBar(p:TimelinePerson,it:TimelineItem,end:string):Bar|null{
    const s=styleOf(it.kind);if(s.style==='heat'||s.style==='conflict')return null;
    const last=it.ends_on||end,d=it.details||{},layer=layerOf(it.kind)?.label||it.kind,status=it.status&&it.status!=='active'?` · ${it.status.replace(/_/g,' ')}`:'';
    const ref=String(d.rotation_type_id||d.service_id||d.programme_id||d.category_id||d.capability_id||it.kind);
    const stop=s.style==='milestone'?plusDays(it.starts_on,1):last;
    const rank=it.kind==='rank'&&d.category_id?String(d.category_id):'',hue=rank?rankColour(rank):s.hue||(s.style==='block'?hueOf(ref):undefined);  // a rank in its one tint (C21), named in the key
    return {id:`${p.id}:${it.id}`,start:it.starts_on,end:stop,label:it.label,style:s.style,hue,external:d.department_available===false,
      keys:[{key:rank?'rank:'+rank:'layer:'+it.kind,mark:`tl-${s.style}${hue?' duty-'+hue:''}`,words:rank?`${layer} · ${it.label}`:layer,order:1}],
      meta:s.style==='block'?lengthWords(it.starts_on,last):undefined,marks:it.status==='submitted'||it.status==='waiting'?['clock']:[],
      aria:`${layer}: ${it.label}, ${spanWords(it.starts_on,last)}${status}`,
      card:()=>[el('strong',{},it.label),el('div',{class:'muted'},p.display_name),ruleFacts([['What',layer],['Dates',`${spanWords(it.starts_on,last)} · ${lengthWords(it.starts_on,last)}`],
        ...(status?[['Status',it.status.replace(/_/g,' ')] as [string,string]]:[])]),el('div',{class:'tl-card-foot'},'Click to open')],
      open:()=>dockPanel({key:'timeline-item',title:it.label,sub:`${p.display_name} · ${layer}`,width:440,mark:`[data-bar="${CSS.escape(`${p.id}:${it.id}`)}"]`,build:({body})=>{
        body.append(ruleFacts([['What',layer],['Dates',`${spanWords(it.starts_on,last)} · ${lengthWords(it.starts_on,last)}`],['Status',(it.status||'—').replace(/_/g,' ')]]));
        for(const c of p.conflicts.filter(c=>{const [a,b]=conflictDays(c,it.starts_on,last,zone);return a<last&&b>it.starts_on;}))
          body.append(el('section',{class:`rb-card ${c.severity==='error'?'err':'wait'}`},el('h3',{},c.message)));
      }})};
  }
  function rowOf(r:T,p:TimelinePerson|undefined,t:TimelineDraw<T>):TimelineRow{
    const name=String(r.display_name||'');
    const open=o.open?()=>o.open!(r):undefined;
    if(!p)return {id:r.id,name,sub:t.sub(r),bars:[],open};
    const hidden=new Set(t.settings.hidden),on=(kind:string)=>!hidden.has(kind);
    const bars=p.items.filter(it=>on(it.kind)).map(it=>itemBar(p,it,t.end)).filter((b):b is Bar=>!!b);
    const zones:Zone[]=p.conflicts.map(c=>{const [a,b]=conflictDays(c,t.start,t.end,zone);return {start:a,end:b,label:c.message,tone:c.severity==='error'?'error':'approval'};});
    const errors=p.conflicts.filter(c=>c.severity==='error'&&c.shift_id).flatMap(c=>{const [a,b]=conflictDays(c,t.start,t.end,zone),out:string[]=[];for(let d=a;d<b;d=plusDays(d,1))out.push(d);return out;});
    // A density strip only where there is something to show (an empty strip says nothing), and then always with its key.
    const density=layers.find(l=>STYLES[l.style_key]?.style==='heat'),dense=density&&on(density.kind)&&(p.duty_density.length||errors.length)?density:null;
    return {id:r.id,name,sub:t.sub(r),bars,zones,flag:p.conflicts.find(c=>c.severity==='error')?.message,open,
      heat:dense?{levels:Object.fromEntries(p.duty_density.map(x=>[x.day,Math.min(3,x.count)])),errors}:undefined,
      keys:dense?[{key:'layer:'+dense.kind,mark:'tl-heat-key',words:dense.label,order:1}]:undefined};
  }
  // Works in: each person's areas (ranked, the main one first) over the line's window, from its read (TimelinePerson.areas,
  // the server's window totals). Read only once something asks (the column shown, a filter or group by it, Filter
  // opened): the line's default window, 12 months from two back (Where is everyone's own read, shared).
  let areas:Map<string,string[]>|null=null,asked=false;
  // Changed = what worksIn answers changed: before the read it answers none for everyone, so a first answer naming no
  // areas redraws nothing (it rebuilt the whole list ~200 ms after People was shown, under the reader; run 42863010208).
  const takeAreas=(data:TimelineResponse)=>{const next=new Map(data.people.map(p=>[p.id,(p.areas||[]).map(a=>a.label)]));
    const said=(m:Map<string,string[]>|null,k:string)=>(m?.get(k)||[]).join('\n');
    const same=[...new Set([...(areas?.keys()||[]),...next.keys()])].every(k=>said(areas,k)===said(next,k));areas=next;if(!same)o.areasChanged?.();};
  const worksIn=(id:string)=>{if(!areas&&!asked){asked=true;const from=plusMonths(departmentToday().slice(0,7)+'-01',-2);
    read(from,plusMonths(from,12),[],'month',true).then(d=>{if(!areas)takeAreas(d);},()=>{asked=false;});}
    return areas?.get(id)||[];};
  return {worksIn,
    layers:settings=>settings.one_line?SHOW.map((x):Omit<Layer,'on'>=>({key:x.key,label:x.label,style:'block',swatches:x.swatches,fixed:x.fixed,
      parts:x.key==='leave'?(o.leaveTypes?.()||[]).filter(([id])=>typeKey(id).length<=40).map(([id,label])=>({key:typeKey(id),label,on:!settings.hidden.includes(typeKey(id))})):undefined}))
      :[...layers.map((l):Omit<Layer,'on'>=>{const s=STYLES[l.style_key]||{style:'block'};return {key:l.kind,label:l.label,style:s.style,hue:s.hue,count:l.count,fixed:s.style==='conflict'};})],
    draw:(groups,t)=>{
      // One line: columns are weeks at 3 or 6 months, months at 9 or 12 months and on a phone.
      const line=t.settings.one_line?(phoneQuery.matches||t.zoom==='quarter'||t.zoom==='year'?'month':'week'):'';
      // Drawing-only keys (Short stretches, the retired Legend) never read again.
      const hidden=t.settings.hidden.filter(k=>!DRAW_ONLY.includes(k));
      const host=el('div',{class:'tl-host'}),key=readKey(t.start,t.end,hidden,line);
      // The key, always below the timeline (DL-063): only what this view and range draw.
      const keyFor=()=>{const k=legend('Key');k.root.classList.add('tl-legend');return k;};
      const withKey=(k:ReturnType<typeof legend>,view:HTMLElement)=>[view,k.root];
       const paintLine=(data:TimelineResponse)=>{takeAreas(data);const people=new Map(data.people.map(p=>[p.id,p])),hueOf=areaHues(data.people),order=areaOrder(data.people);
        const rows=groups.map(g=>({key:g.key,name:g.name,rows:g.rows.map((r):TimelineRow=>{const pp=people.get(r.id),name=String(r.display_name||''),sub=t.sub(r);
          // The name alone: the grade is the group's heading and in each stretch's card (approved v2).
          // A person's record is desktop work (M29): on a phone the name is not a button.
          return {id:r.id,name,label:personName(r.id,name),open:o.open&&!phoneQuery.matches?()=>o.open!(r):undefined,bars:pp?personLine({...pp,display_name:name||pp.display_name},{full:t.settings.bar_label==='name',hueOf,sub,areaOrder:order}):[]};})}));
        // People, not rows: someone with two ranks is in both groups.
        const all=new Set(rows.flatMap(g=>g.rows.map(r=>r.id)));
        // A phone too: one person per row, the months swipe under the names (no list of cards).
        const k=keyFor();
        host.replaceChildren(...withKey(k,timelineView({label:'People timeline',start:t.start,end:t.end,zoom:t.zoom,density:t.settings.density,collapsed:t.collapsed,toggle:t.toggle,oneLine:true,
          fold:t.settings.hidden.includes('short'),legend:k,weekends:false,corner:el('span',{},el('b',{},String(all.size)),' ',all.size===1?'person':'people'),groups:rows})));};
      const paint=(data:TimelineResponse)=>{layers=data.layers;zone=data.timezone;if(line){paintLine(data);return;}const people=new Map(data.people.map(p=>[p.id,p])),today=departmentToday();
        const rows=groups.map(g=>({...g,rows:g.rows.map(r=>rowOf(r,people.get(r.id),t))}));
        const all=rows.flatMap(g=>g.rows);
        if(phoneQuery.matches&&!full){const k=keyFor();host.replaceChildren(...withKey(k,timelineAgenda({label:'People',rows:all,start:t.start,end:t.end,mine:!!o.person,legend:k,full:()=>{full=true;paint(data);}})));return;}
        const k=keyFor();
        host.replaceChildren(...withKey(k,timelineView({label:'People timeline',start:t.start,end:t.end,zoom:t.zoom,density:t.settings.density,weekends:t.settings.weekends,collapsed:t.collapsed,toggle:t.toggle,legend:k,
          corner:el('span',{},el('b',{},String(all.length)),' ',all.length===1?'person':'people'),
          groups:rows.map(g=>({key:g.key,name:g.name,rows:g.rows,summary:[
            ...(()=>{const away=g.rows.filter(r=>r.bars.some(b=>(b.style==='leave'||b.style==='protected')&&b.start<=today&&b.end>today)).length;return away?[{text:`${away} away now`}]:[];})(),
            ...(()=>{const c=g.rows.filter(r=>r.zones?.some(z=>z.tone==='error')).length;return c?[{text:plural(c,'conflict'),tone:'error' as const}]:[];})()]}))})));};
      const ready=keptAnswers(WINDOW_READS).get(key)?.data as TimelineResponse|undefined;
      if(ready)paint(ready);
      else{if(shown?.key===key)paint(shown.data);else host.append(skeleton('timeline','Loading the timeline…'));  // a rebuilt page keeps its drawing
        // Painted whether or not the host is in the page yet: a page is built off-screen and swapped in once its reads
        // settle, so a read that answers first found a detached host and the skeleton stayed until the next redraw.
        const failed=(e:unknown)=>{if(!sessionChanged(e))host.replaceChildren(readFailed('the timeline',e,()=>void read(t.start,t.end,hidden,line).then(paint,failed)));};
        read(t.start,t.end,hidden,line).then(paint,failed);}
      return host;
    }};
}

/** People › a person › Schedule › Timeline: the same drawing for one person, a year at a time from two months back. */
export function personTimelineTab(p:{id:string;display_name?:unknown}):HTMLElement{
  const tl=peopleTimeline({person:p.id}),today=departmentToday(),home=plusMonths(today.slice(0,7)+'-01',-2);
  const nav=el('div',{class:'wk-nav'}),host=el('div',{}),box=el('div',{},nav,host);let from=home;
  const draw=()=>{const w=timelineWindow('year',from),go=(day:string)=>{from=day;draw();},now=button('Today',()=>go(home),'ghost small');now.hidden=from===home;
    nav.replaceChildren(iconButton('Previous','chevronLeft',()=>go(w.step(-1))),el('strong',{},w.label),iconButton('Next','chevronRight',()=>go(w.step(1))),now);
    host.replaceChildren(tl.draw([{key:'',name:'',rows:[p]}],{start:w.start,end:w.end,zoom:'year',settings:{zoom:'year',hidden:['eligibility'],properties:null,density:'comfortable',weekends:true,bar_label:'name_length',colour_by:'rotation_type',one_line:false},
      collapsed:[],toggle:()=>{},sub:()=>''}));};
  draw();return box;
}
