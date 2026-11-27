import {el,legend,icon,button,iconButton,empty,badge,formatDate,formDialog,field,area,select,check,multi,notify,labelOf,plural,phoneQuery,departmentToday,reportError,menuItem,tabs,popover,confirmAction,ruleFacts,footerStatus,type Child,emptyLine,CAPABILITY} from './dom.js';
import {readFailed} from './dom.js';  // a failed read (DL-045)
import {personName,fullNameOf} from './person_label.js';
import {get,command,instant,request,sessionChanged} from './api.js';
import type {Change,DragSpec,Drop} from './timeline_drag.js';
import {collectionToolbar,conditionFilter,holds,joinFilter,savedViewItems,fetchViews,openingView,type FilterState,type SavedView,type SortValue} from './table_view.js';
import {dockPanel} from './side_panel.js';
import {recordMatcher,type SearchOptions} from './search.js';
import {catalogueForm,peopleChoices,rotationBlocks,type Context,type RotationBlock} from './forms.js';
import {dateRange,lastDayIncluded} from './date_picker.js';
import {addDays} from './planner_logic.js';
import {skeleton,settle} from './loading.js';
import {HUES,timelineView,timelineAgenda,timelineSettings,timelineWindow,timelineMini,lengthWords,dayNumber,dayOf,plusMonths,type Bar,type BarLabel,type Layer,type TimelineGroup,type TimelineRow,type Zone,type Zoom} from './timeline.js';
import type {Row} from './types.js';
import {TABLE_CONDITION_DEFINITIONS,REFERENCE_ROTATION_FILE_LIMIT,type ReferenceRotationImport,type ReferenceRotationReview,type RotationBoardChoices,type TableViewDefinition,type TimelineViewSettings,type TimelinePreview,type TimelineRotationInput,type TimelineRotationResult} from './generated.js';
import {menu,opens} from './dom.js';  // the shared menu (UX › controls S2b)
import {panelSearch} from './dom.js';  // the one search box in a panel (UX › controls S10)

/**
 * Programmes › Rotation board: a saved Timeline view of the trainees (timeline.ts, approved mockup 21 Oct 2026). Each
 * rotation is a bar in its type's hue (↗ external), leave and protected time are blocks in the same lane, a waiting
 * change is a dashed gold ghost with a clock, months a member has nothing planned read "+ Plan rotations", and two
 * rotations that overlap get a red conflict zone. The shared toolbar: period, Search, Filter, Group, Sort, View settings
 * (zoom, layers as the legend, name column, density) and Saved views. Programme leads and coordinators plan from blocks
 * or a template, copy a year forward, decide change requests and keep notes; a trainee sees their own rotations (on a
 * phone, as an agenda) and asks for a change. The server (GET /api/rotation-board) decides who is shown and what leave says.
 */
type Board={start:string;end:string;people:Row[];memberships:Row[];rotations:Row[];leave:Row[];changes:Row[];
  names:Record<'programmes'|'cohorts'|'rotation_types'|'services',Record<string,string>>;choices:RotationBoardChoices;can_plan:boolean;can_note:boolean;reference_leave_conflicts?:string[]};
type Group=''|'programme'|'cohort'|'now';
type Density='comfortable'|'compact';
/** `filters` + `conditions`: the shared Filter (table_view.ts conditionFilter) over the board's properties; the rest is
 *  the Timeline's display, saved with a view (TableViewDefinition.timeline). */
type View={from:string;zoom:Zoom;group:Group;sort:SortValue;hidden:string[];props:string[];density:Density;weekends:boolean;collapsed:string[];viewId:string;
  /** Bar label and Colour rotations by (TimelineViewSettings bar_label, colour_by): saved with a view too. */label:BarLabel;colourBy:ColourBy;
  /** The Rotations palette is open (planners, desktop). */palette:boolean}&FilterState;
type ColourBy=NonNullable<TimelineViewSettings['colour_by']>;
const COLOUR_BY:[ColourBy,string][]=[['rotation_type','Rotation type'],['service','Service'],['cohort','Cohort']];
/** The board's properties, from the contract (one definition): the Filter offers them and `matches` reads them. */
const TABLE='rotation-board';
const PROPS=Object.values(TABLE_CONDITION_DEFINITIONS[TABLE]).map(f=>({key:f.key,label:f.label}));
const TRAINEES:Record<string,string>={local:'Local trainees',external:'External rotators'};
const SHOWN_PROPS=[{key:'programme',label:'Programme'},{key:'cohort',label:'Cohort'}];
export type BoardDeps={ctx:Context;zone:string;mine:boolean};

const KEY='rota.board.view';
/** "1 Oct – 31 Oct 2026" (the year once when both ends share it); `b` is the day after the last. */
const span=(a:string,b:string)=>{const last=addDays(b,-1),first=formatDate(a);return `${a.slice(0,4)===last.slice(0,4)?first.replace(/ \d{4}$/,''):first} – ${formatDate(last)}`;};
/** The retired Legend layer (D-147): a saved view may still list it in `hidden`; it is read past, never removed. */
const RETIRED_LEGEND='legend';
const DEFAULTS:Omit<View,'from'>={zoom:'year',group:'programme',sort:null,hidden:[],props:SHOWN_PROPS.map(p=>p.key),density:'comfortable',weekends:true,collapsed:[],viewId:'',palette:true,filters:{},conditions:null,label:'name_length',colourBy:'rotation_type'};
const view:View=(()=>{const d:View={from:'',...DEFAULTS};
  try{const {programmes,cohorts,types,who,months,from,...kept}=JSON.parse(localStorage.getItem(KEY)||'{}');
    // A board kept before the shared Filter: its picks become the same filter rows (and "type" is now "rotation_type").
    const old=Object.fromEntries(([['programme',programmes],['cohort',cohorts],['rotation_type',types],['trainees',who?[who]:[]]] as [string,string[]|undefined][]).filter(([,x])=>x?.length));
    const filters={...(kept.filters||old)};if(filters.type){filters.rotation_type=filters.type;delete filters.type;}
    // Conditions saved then too (Or groups, is not): their lines on "type" are "rotation_type" now.
    const renamed=(n:any):any=>n&&('conditions' in n?{...n,conditions:n.conditions.map(renamed)}:n.field==='type'?{...n,field:'rotation_type'}:n);
    if(kept.conditions)kept.conditions=renamed(kept.conditions);
    if(typeof kept.colourBy==='string')kept.colourBy=kept.colourBy.replace(/s$/,'');  // kept as 'rotation_types' before the contract
    // A board kept before the Timeline: its months shown become a zoom, its "yyyy-mm" a day.
    const zoom=kept.zoom||({3:'week',6:'month',12:'year'} as Record<number,Zoom>)[months]||d.zoom;
    return {...d,...kept,zoom,filters,from:from?String(from).length===7?from+'-01':String(from):''};}catch{return d;/* private mode: defaults */}})();
const keep=()=>{try{localStorage.setItem(KEY,JSON.stringify(view));}catch{/* not kept */}};
let search='',searchOptions:SearchOptions|undefined,full=false;

type Saved=SavedView<TableViewDefinition>;
const definition=():TableViewDefinition=>({layout:'timeline',filters:view.filters,...(view.conditions?{conditions:view.conditions}:{}),group:view.group,sort:view.sort,
  timeline:{zoom:view.zoom,hidden:view.hidden,properties:view.props,density:view.density,weekends:view.weekends,bar_label:view.label,colour_by:view.colourBy}});
/** A saved view against what is shown, by what a view keeps (the server fills in the parts it does not use). */
const sameAs=(d:TableViewDefinition)=>{const n=(x:TableViewDefinition)=>{const t=x.timeline||{};
  return JSON.stringify([x.filters||{},x.conditions||null,x.group||'',x.sort||null,t.zoom||'year',t.hidden||[],t.properties??null,t.density||'comfortable',t.weekends!==false,t.bar_label||'name_length',t.colour_by||'rotation_type']);};
  return n(d)===n(definition());};

export async function rotationBoard(target:HTMLElement,deps:BoardDeps){
  const {ctx}=deps,today=departmentToday();
  const win=timelineWindow(view.zoom,view.from||today),{start,end}=win;
  // The footer's status is one node: the shell lifts it into the footer, and each draw updates it there.
  const box=el('div',{class:'rb-page'}),foot=footerStatus();target.append(box,foot);
  const again=()=>{target.replaceChildren();void rotationBoard(target,deps);};
  const loading=skeleton('timeline','Loading the rotation board…');box.append(loading);
  let board:Board;
  try{board=await get<Board>(`/api/rotation-board?start=${start}&end=${end}`);}
  catch(e){loading.remove();if(sessionChanged(e))return;
    box.append(readFailed('the rotation board',e,again));return;}
  await settle(loading);loading.remove();
  const named=(kind:keyof Board['names'],id:string|null|undefined)=>id?board.names[kind][id]||'Unavailable':'';
  // Indexed once per load (PI-1): every lookup below was a scan of the whole board, about 3.2M comparisons per draw at
  // 150 trainees × 12 rotations.
  const index=<T extends Row>(list:T[],key:(x:T)=>string)=>{const by=new Map<string,T[]>();for(const x of list){const k=key(x);const at=by.get(k);if(at)at.push(x);else by.set(k,[x]);}return by;};
  const peopleBy=new Map(board.people.map(p=>[p.id as string,p])),rotationsBy=index(board.rotations,r=>r.person_id),membershipsBy=index(board.memberships,m=>m.person_id);
  const leaveBy=index(board.leave,x=>x.person_id),changesBy=new Map(board.changes.map(c=>[c.rotation_id as string,c])),none:Row[]=[];
  const person=(id:string)=>peopleBy.get(id);
  const rotationsOf=(id:string)=>rotationsBy.get(id)||none;
  const membershipsOf=(id:string)=>membershipsBy.get(id)||none;
  const leaveOf=(id:string)=>leaveBy.get(id)||none;
  const external=(id:string)=>membershipsOf(id).some(m=>m.external);
  const changeOf=(rotationId:string)=>changesBy.get(rotationId);
  const referenceConflicts=new Set(board.reference_leave_conflicts);
  const rotationName=(r:Row)=>named('rotation_types',r.rotation_type_id)||named('services',r.service_id)||'Rotation';
  // Each value of Colour by shown (a rotation type, service or cohort) gets its own hue, in name order, so neighbouring
  // blocks differ; by rotation type, a rotation with none takes its service's. A rotation with neither is slate.
  // shortcut: the hue follows the values shown in these months; give them a stored colour if it must never move.
  const byName=(m:Record<string,string>)=>Object.entries(m).sort((x,y)=>x[1].localeCompare(y[1])).map(([id])=>id);
  // Types offered in the palette too, so a chip and the bar it becomes share their hue.
  const kinds=view.colourBy==='rotation_type'?[...byName({...board.choices.rotation_types,...board.names.rotation_types}),...byName({...board.choices.services,...board.names.services})]:byName(board.names[view.colourBy==='service'?'services':'cohorts']);
  const keyOf=(r:Row)=>view.colourBy==='rotation_type'?r.rotation_type_id||r.service_id:view.colourBy==='service'?r.service_id:r.cohort_id;
  const colour=(r:Row)=>{const i=kinds.indexOf(keyOf(r));return i<0?'slate':HUES[i%HUES.length];};
  /** A rotation's colour in the key (the timeline's legend): what its hue stands for, in the hues' order. */
  const colourKey=(r:Row):NonNullable<Bar['keys']>=>{const k=keyOf(r);
    // A rotation with nothing to colour it by is slate: that, too, is in the key.
    if(!k)return [{key:'colour:none',mark:'tl-block duty-slate',words:view.colourBy==='rotation_type'?'No rotation type':view.colourBy==='service'?'No clinic':'No cohort',order:0.999}];
    const words=view.colourBy==='rotation_type'?(r.rotation_type_id?named('rotation_types',r.rotation_type_id):named('services',r.service_id)):named(view.colourBy==='service'?'services':'cohorts',k);
    return [{key:'colour:'+k,mark:`tl-block duty-${colour(r)}`,words,order:(kinds.indexOf(k)+1)/(kinds.length+1)}];};
  const nowDay=today>=start&&today<end?today:start;
  const current=(id:string)=>rotationsOf(id).find(r=>r.starts_on<=nowDay&&r.ends_on>nowDay);
  const away=(id:string,day=today)=>leaveOf(id).some(x=>x.starts_on<=day&&x.ends_on>day);
  const redraw=()=>{box.replaceChildren();draw();};
  const reload=again;
  const on=(layer:string)=>!view.hidden.includes(layer);
  /** Days two rotations of one trainee share: a trainee is on one rotation at a time. */
  const overlaps=(r:Row)=>rotationsOf(r.person_id).filter(o=>o.id!==r.id&&o.starts_on<r.ends_on&&o.ends_on>r.starts_on)
    .map(o=>({other:o,start:o.starts_on>r.starts_on?o.starts_on as string:r.starts_on as string,end:o.ends_on<r.ends_on?o.ends_on as string:r.ends_on as string}));

  // ---- Which rows: search and filters over the trainees the server sent ----
  const valuesOf=(p:Row)=>(k:string):string[]=>k==='programme'?membershipsOf(p.id).map(m=>m.programme_id):k==='cohort'?rotationsOf(p.id).map(r=>r.cohort_id)
    :k==='rotation_type'?rotationsOf(p.id).map(r=>r.rotation_type_id):k==='trainees'?[external(p.id)?'external':'local']:[];
  const matches=(p:Row)=>{const t=joinFilter(view.filters,view.conditions);
    const m=searchOptions&&recordMatcher(search,searchOptions);
    return (searchOptions?!m||m({own:()=>[p.display_name]}):!search||String(p.display_name).toLowerCase().includes(search.toLowerCase()))&&(!t||holds(t,valuesOf(p),()=>'multiple_choice'));};
  const choicesOf=(k:string)=>k==='trainees'?Object.entries(TRAINEES):Object.entries(board.names[({programme:'programmes',cohort:'cohorts',rotation_type:'rotation_types'} as const)[k as 'programme']]||{});
  const groupOf=(p:Row):string=>view.group==='programme'?named('programmes',membershipsOf(p.id)[0]?.programme_id)||'No programme'
    :view.group==='cohort'?named('cohorts',rotationsOf(p.id)[0]?.cohort_id)||'No cohort'
    :view.group==='now'?(()=>{const r=current(p.id);return r?rotationName(r):'Not on a rotation';})():'';
  const sortKey=(p:Row,k:string)=>k==='programme'?named('programmes',membershipsOf(p.id)[0]?.programme_id):k==='cohort'?named('cohorts',rotationsOf(p.id)[0]?.cohort_id):String(p.display_name);
  const ordered=(list:Row[])=>{const [k,dir]=view.sort||['name','asc'];return [...list].sort((a,b)=>(sortKey(a,k).localeCompare(sortKey(b,k))||String(a.display_name).localeCompare(String(b.display_name)))*(dir==='desc'?-1:1));};

  // ---- Saved views (the server's, as every collection) ----
  // They arrive after the board is drawn: only the toolbar's Saved views button changes (nothing the reader looks at).
  let saved:Saved[]=[],sync=()=>{};
  if(!deps.mine)void fetchViews<TableViewDefinition>(TABLE).then(v=>{saved=v;const open=openingView(TABLE,saved);if(open!==undefined&&(open?.id||'')!==view.viewId){applyView(open);return;}if(view.viewId&&!saved.some(x=>x.id===view.viewId)){view.viewId='';keep();}sync();}).catch(()=>undefined);
  const applyView=(v:Saved|null)=>{const d=v?.definition,t=d?.timeline;
    Object.assign(view,v&&d?{filters:d.filters||{},conditions:d.conditions||null,group:(d.group||'') as Group,sort:d.sort||null,zoom:t?.zoom||'year',hidden:t?.hidden||[],
      props:t?.properties??DEFAULTS.props,density:t?.density||'comfortable',weekends:t?.weekends!==false,label:t?.bar_label||DEFAULTS.label,colourBy:t?.colour_by||DEFAULTS.colourBy}:{...DEFAULTS,from:view.from},{viewId:v?.id||''});keep();reload();};

  // ---- One trainee as a timeline row ----
  // Built once per trainee and kept across redraws (search, filter, group, sort, density, collapse) until what a row
  // shows changes: its layers, colours or name-column properties (PI-1).
  let rowsFor='';const rowCache=new Map<string,TimelineRow>();
  const rowOf=(p:Row):TimelineRow=>{const key=JSON.stringify([view.hidden,view.colourBy,view.props,deps.mine?'':view.group]);if(key!==rowsFor){rowsFor=key;rowCache.clear();}
    const kept=rowCache.get(p.id);if(kept)return kept;const row=buildRow(p);rowCache.set(p.id,row);return row;};
  const buildRow=(p:Row):TimelineRow=>{
    const rots=rotationsOf(p.id),bars:Bar[]=[],zones:Zone[]=[];
    // Each layer on its own: a change waiting shows with Rotations hidden, as long as "Changes waiting" is on.
    for(const r of rots){const change=changeOf(r.id),label=rotationName(r),ext=r.department_available===false;
      if(on('rotations'))bars.push({id:r.id,start:r.starts_on,end:r.ends_on,label,style:'block',hue:colour(r),external:ext,meta:lengthWords(r.starts_on,r.ends_on),marks:change?['clock']:[],keys:colourKey(r),
        aria:`${label}, ${span(r.starts_on,r.ends_on)}${ext?', away from department duties':''}${change?', change requested':''}`,open:()=>rotationPanel(r),card:()=>rotationCard(r)});
      const v=change?.proposed||{};
      if(change&&on('changes')&&v.starts_on&&v.ends_on){const what=named('rotation_types',v.rotation_type_id)||named('services',v.service_id)||label;
        bars.push({id:'change:'+change.id,start:v.starts_on,end:v.ends_on,label:`${what} · proposed`,short:'Proposed',style:'pending',keys:[{key:'mark:changes',mark:'tl-pending',words:'Changes waiting',order:1.3}],
          aria:`Change waiting: ${what}, ${span(v.starts_on,v.ends_on)}`,open:()=>rotationPanel(r)});}}
    leaveOf(p.id).forEach((x,i)=>{const kind=x.kind==='protected_time'?'protected':'leave';if(!on(kind))return;
      const label=x.label+(x.stage==='waiting'?' · waiting':'');
      bars.push({id:`leave:${p.id}:${i}`,start:x.starts_on,end:x.ends_on,label,style:kind,aria:`${label}, ${span(x.starts_on,x.ends_on)}`,
        keys:[kind==='leave'?{key:'mark:leave',mark:'tl-leave',words:'Leave',order:1.1}:{key:'mark:protected',mark:'tl-protected',words:'Academic & protected time',order:1.2}],
        card:()=>[el('strong',{},label),el('div',{class:'muted'},`${span(x.starts_on,x.ends_on)} · ${lengthWords(x.starts_on,x.ends_on)}`)]});});
    // Overlapping days, joined where they touch: one zone and one badge per stretch.
    const shared=rots.flatMap(r=>overlaps(r).filter(o=>r.id<o.other.id)).sort((a,b)=>a.start.localeCompare(b.start));
    for(const o of shared){const last=zones.at(-1);if(last&&o.start<=last.end){if(o.end>last.end)last.end=o.end;}else zones.push({start:o.start,end:o.end,label:'',tone:'error'});}
    for(const z of zones)z.label=`Overlap · ${dayNumber(z.end)-dayNumber(z.start)} d`;
    if(board.can_plan&&!deps.mine&&on('plan'))for(const [a,b] of unplanned(p.id))
      bars.push({id:`plan:${p.id}:${a}`,start:a,end:b,label:`Plan rotations · ${span(a,b)} not planned`,short:'Plan',style:'plan',aria:`Plan rotations for ${span(a,b)}`,
        keys:[{key:'mark:plan',mark:'tl-plan',words:'Months not planned',order:1.4}],open:()=>planDialog([p.id],{from:a})});
    // What the group heading already says is not repeated under each trainee (LIVE-08): grouped by programme, only
    // their other programmes; grouped by cohort, only their other cohorts.
    const heading=deps.mine?'':groupOf(p),others=(names:string[])=>names.filter(n=>n!==heading).join(', ');
    const sub=view.props.map(k=>k==='programme'?others(membershipsOf(p.id).map(m=>named('programmes',m.programme_id))):k==='cohort'?others([...new Set(rots.map(r=>named('cohorts',r.cohort_id)))]):'').filter(Boolean).join(' · ');
    const home=membershipsOf(p.id).find(m=>m.external);
    return {id:p.id,name:String(p.display_name),label:personName(p.id,String(p.display_name)),sub,bars,zones,tags:[...(home?[{text:'EXT',tip:'External rotator · '+(home.home_programme||'another programme'),key:{key:'tag:ext',mark:'wk-tag ext',code:'EXT',words:'External rotator',order:2}}]:[]),
      ...(p.notes?[{text:String(p.notes),tip:plural(p.notes,'note'),icon:'edit' as const,class:'rb-notes'}]:[])],flag:zones.length?'Two rotations overlap':undefined,open:deps.mine?undefined:()=>traineePanel(p)};
  };
  /** Stretches of four weeks or more in a membership, within the days shown, with no rotation planned. */
  function unplanned(id:string):[string,string][]{
    const out:[string,string][]=[],taken=rotationsOf(id).map(r=>[r.starts_on,r.ends_on] as [string,string]).sort((a,b)=>a[0].localeCompare(b[0]));
    for(const m of membershipsOf(id)){let a:string=m.starts_on>start?m.starts_on:start;const z:string=m.ends_on<end?m.ends_on:end;
      for(const [s,e] of taken){if(e<=a)continue;if(s>=z)break;if(s>a)out.push([a,s]);if(e>a)a=e;}
      if(a<z)out.push([a,z]);}
    return out.filter(([a,b])=>dayNumber(b)-dayNumber(a)>=28);
  }

  function draw(){
    const shown=ordered(board.people.filter(matches)),rows=new Map(shown.map(p=>[p.id,rowOf(p)]));
    const conflicts=[...rows.values()].filter(r=>r.zones?.length).length,waiting=board.changes.filter(c=>rows.has(c.person_id)).length;
    const toolbar=collectionToolbar({key:'rotation-board',label:'Rotation board',
      saved:deps.mine?undefined:{name:()=>saved.find(x=>x.id===view.viewId)?.name||'',dirty:()=>{const a=saved.find(x=>x.id===view.viewId);return !!a&&!sameAs(a.definition);},
        items:close=>savedViewItems<TableViewDefinition>({key:TABLE,noun:'trainees',saved,activeId:view.viewId,current:definition,dirty:d=>!sameAs(d),close,apply:applyView,
          done:(next,id)=>{saved=next;view.viewId=id;keep();toolbar.sync();},reset:()=>applyView(null)})},
      scope:[periodNav()],
      search:deps.mine?undefined:{value:search,label:'Search trainees',placeholder:'Search trainees',wait:150,onInput:v=>{search=v;},apply:()=>{redraw();box.querySelector<HTMLInputElement>('.dt-search')?.focus();},
        // A trainee's own name only (Contains or Exact text).
        options:{value:()=>searchOptions,set:o=>{searchOptions=o;},can:{scopes:['all'],kinds:[]}}},
      filter:deps.mine?undefined:conditionFilter({label:k=>PROPS.find(x=>x.key===k)?.label||k,valueLabel:(k,x)=>Object.fromEntries(choicesOf(k))[x]||'Unavailable',result:()=>`${shown.length} of ${plural(board.people.length,'trainee')}`,
        fields:()=>PROPS.map(x=>({...x,kind:'multiple_choice' as const,choices:choicesOf(x.key).map(([value,label])=>({value,label}))})).filter(x=>x.choices.length),
        get:()=>({filters:view.filters,conditions:view.conditions}),set:n=>{view.filters=n.filters;view.conditions=n.conditions;keep();redraw();}}),
      group:deps.mine?undefined:{options:[{key:'programme',label:'Programme'},{key:'cohort',label:'Cohort'},{key:'now',label:'Rotation now'}],value:()=>view.group,defaultKey:'programme',
        active:()=>view.group!==DEFAULTS.group,set:k=>{view.group=k as Group;keep();redraw();}},
      sort:deps.mine?undefined:{options:[{key:'name',label:'Name'},...SHOWN_PROPS],value:()=>view.sort,set:v=>{view.sort=v;keep();redraw();},defaultLabel:'Name'},
      view:{label:()=>`Timeline, ${view.zoom}`,active:()=>view.zoom!==DEFAULTS.zoom||view.hidden.some(k=>k!==RETIRED_LEGEND)||view.density!==DEFAULTS.density||view.label!==DEFAULTS.label||view.colourBy!==DEFAULTS.colourBy,
        tip:'View settings (zoom, layers, name column, density, bar label, colour)',
        build:panel=>timelineSettings(panel,{zoom:view.zoom,setZoom:z=>{view.zoom=z;keep();reload();},layers:layers(),
          setLayer:(k,v)=>{view.hidden=v?view.hidden.filter(x=>x!==k):[...view.hidden,k];keep();redraw();},
          properties:deps.mine?undefined:SHOWN_PROPS.map(p=>({...p,on:view.props.includes(p.key)})),setProperty:(k,v)=>{view.props=SHOWN_PROPS.map(p=>p.key).filter(x=>x===k?v:view.props.includes(x));keep();redraw();},
          density:view.density,setDensity:d=>{view.density=d;keep();redraw();},weekends:view.weekends,setWeekends:v=>{view.weekends=v;keep();redraw();},
          barLabel:{value:view.label,set:v=>{view.label=v;keep();redraw();}},
          colourBy:{label:'Colour rotations by',options:COLOUR_BY,value:view.colourBy,set:v=>{view.colourBy=v as ColourBy;keep();reload();}}})},
      actions:actions()});
    box.append(toolbar.root);sync=toolbar.sync;toolbar.shown(shown.length,board.people.length,['trainee','trainees']);
    // No dead ends (P-17): one primary that follows the setup order, a programme first, then its members.
    if(!board.people.length){const first=!programmes().length;box.append(empty(deps.mine?'No rotations in these months':first?'No programmes yet':`No trainees in ${win.label}`,
      deps.mine?'Your programme lead plans your rotations; they show here once saved.':first?'Create a programme, add its trainees, then plan their rotations.':'Add people to a programme, then plan their rotations.',
      board.can_plan?first?button('New programme',()=>catalogueForm(ctx,'programmes'),'primary'):button('Add programme members',()=>catalogueForm(ctx,'programme_memberships'),'primary'):null,'calendar'));return;}
    // Show all is the toolbar's filtered strip, just above.
    if(!shown.length){box.append(empty('No one matches',`None of ${plural(board.people.length,'trainee')} match${search?` “${search}” and`:''} these filters.`,null,'filter'));return;}
    // The key, always below the view (DL-063, D-147: no Legend toggle): only what this view draws, on a phone too.
    const key=legend('Key');key.root.classList.add('tl-legend');
    if(phoneQuery.matches&&(deps.mine||!full))box.append(timelineAgenda({label:'Rotations',rows:[...rows.values()],start,end,today,mine:deps.mine,legend:key,full:()=>{full=true;redraw();}}),key.root);
    else{
      const groups:TimelineGroup[]=[];
      if(!view.group||deps.mine)groups.push({key:'',name:'',rows:[...rows.values()]});
      else{const by=index(shown,groupOf);
        for(const [name,list] of [...by].sort((a,b)=>a[0].localeCompare(b[0]))){const rs=list.map(p=>rows.get(p.id)!),n=(f:(p:Row)=>boolean)=>list.filter(f).length,ids=new Set(list.map(p=>p.id));
          const c=rs.filter(r=>r.zones?.length).length,w=board.changes.filter(x=>ids.has(x.person_id)).length,off=n(p=>away(p.id));
          groups.push({key:name,name,rows:rs,summary:[{text:`${n(p=>!!current(p.id))} on rotation now`},...(off?[{text:`${off} on leave now`}]:[]),
            ...(c?[{text:plural(c,'conflict'),tone:'error' as const}]:[]),...(w?[{text:plural(w,'change')+' waiting',tone:'warn' as const}]:[])]});}}
      const planning=board.can_plan&&!deps.mine&&!phoneQuery.matches;  // planning by hand is for wide screens (phones: the agenda)
      const tl=timelineView({label:'Rotation timeline',start,end,zoom:view.zoom,groups,weekends:view.weekends,density:view.density,today,collapsed:view.collapsed,barLabel:view.label,drag:drag(),legend:key,
        corner:el('span',{},el('b',{},String(shown.length)),shown.length<board.people.length?` of ${plural(board.people.length,'trainee')}`:` ${shown.length===1?'trainee':'trainees'}`),
        toggle:k=>{view.collapsed=view.collapsed.includes(k)?view.collapsed.filter(x=>x!==k):[...view.collapsed,k];keep();redraw();}});
      box.append(planning?el('div',{class:'rb-plan'},palette(),tl):tl,key.root);
    }
    const week=dayOf(dayNumber(today)+7),leaveWeek=shown.filter(p=>leaveOf(p.id).some(x=>x.kind!=='protected_time'&&x.starts_on<week&&x.ends_on>today)).length;
    foot.replaceChildren(...[el('span',{class:'muted'},plural(shown.length,'trainee')),
      conflicts?el('span',{class:'health-pill err'},icon('alert'),plural(conflicts,'conflict')):null,
      waiting?el('span',{class:'health-pill warn'},icon('clock'),plural(waiting,'change')+' waiting'):null,
      leaveWeek?el('span',{class:'muted'},`${leaveWeek} on leave this week`):null].filter(Boolean) as HTMLElement[]);
  }
  function layers():Layer[]{
    const n=(k:string)=>board.leave.filter(x=>(x.kind==='protected_time'?'protected':'leave')===k).length;
    return [{key:'rotations',label:`Rotations · by ${COLOUR_BY.find(([k])=>k===view.colourBy)![1].toLowerCase()}`,style:'block',hue:HUES[0],count:board.rotations.length,on:on('rotations')},
      {key:'leave',label:'Leave',style:'leave',count:n('leave'),on:on('leave')},
      {key:'protected',label:'Academic & protected time',style:'protected',count:n('protected'),on:on('protected')},
      {key:'changes',label:'Changes waiting',style:'pending',count:board.changes.length,on:on('changes')},
      ...(board.can_plan&&!deps.mine?[{key:'plan',label:'Months not planned',style:'plan' as const,on:on('plan')}]:[]),
      {key:'conflicts',label:'Conflicts',style:'conflict',on:true,fixed:true}];
  }

  function periodNav(){
    const go=(day:string)=>{view.from=day;keep();reload();};
    const now=button('Today',()=>go(today),'ghost small');now.hidden=today>=start&&today<end;
    return el('div',{class:'wk-nav rb-nav'},iconButton('Previous','chevronLeft',()=>go(win.step(-1))),el('strong',{},win.label),iconButton('Next','chevronRight',()=>go(win.step(1))),now);
  }

  function actions():Child[]{
    if(deps.mine)return [button('Request leave',()=>ctx.navigate('requests?new=1&type=leave'),'secondary')];
    if(!board.can_plan)return [];
    const more=opens(iconButton('More rotation actions','dots',event=>menu(event.currentTarget as HTMLElement,'Rotation actions',[
      {label:'Copy rotations forward',icon:'copy',run:copyDialog,hint:'Recreate a year for the same trainees'},
      {label:'Import reference rotations',icon:'upload',run:referenceImportDialog},
      {label:'Add programme members',icon:'people',run:()=>catalogueForm(ctx,'programme_memberships'),hint:'Local trainees or external rotators'},
      {label:'Rotation templates',icon:'layers',run:()=>ctx.navigate('programmes/rotation_templates'),hint:'Prebuilt plans to reuse'}],{placement:'below-end'})),'menu');
    // Plan rotations needs trainees: until then it says why (and the empty board holds the one primary). A missing
    // cohort is said in the dialog itself, which every Plan action opens, with the step that creates one.
    const why=!board.people.length?'Add programme members first':'';
    // aria-disabled (not disabled): it keeps focus and its tooltip, and pressing it says why.
    const plan=button('Plan rotations',()=>why?notify(why+'.','info'):planDialog(),why?'secondary':'primary',why?{'aria-disabled':'true','data-tip':why}:{});
    return [plan,more];
  }
  /** The department's programmes this board can plan for (none yet: the board's first step is to create one). */
  function programmes(){return (ctx.catalogues.programmes||[]).filter(p=>p.active!==false);}

  // ---- Planning by hand: the palette, drag and drop, governed commands (timeline_drag.ts, POST /api/timeline/*) ----
  const hueOfType=(id:string)=>HUES[Math.max(0,kinds.indexOf(id))%HUES.length];
  const templates=()=>(ctx.catalogues.rotation_templates||[]).filter(t=>t.active!==false);
  /** A type's usual length: its most common block in the templates, else four weeks. */
  const lengthOf=(id:string):[number,'weeks'|'months']=>{const seen=new Map<string,number>();
    for(const t of templates())for(const b of (t.blocks||[]) as RotationBlock[])if(b.rotation_type_id===id){const k=`${b.length}|${b.unit}`;seen.set(k,(seen.get(k)||0)+1);}
    const top=[...seen].sort((a,b)=>b[1]-a[1])[0]?.[0];if(!top)return [4,'weeks'];const [n,u]=top.split('|');return [Number(n),u as 'weeks'|'months'];};
  // The chip's label only (approximate weeks for mixed units); its end comes from its blocks (timeline_drag dropEnd).
  const templateLength=(blocks:RotationBlock[]):[number,'weeks'|'months']=>blocks.every(b=>b.unit==='months')?[blocks.reduce((a,b)=>a+b.length,0),'months']
    :[Math.round(blocks.reduce((a,b)=>a+(b.unit==='months'?b.length*4.345:b.length),0)),'weeks'];
  const lengthWords_=([n,u]:[number,string])=>`${n} ${u==='months'?'mo':'wk'}`;
  function palette(){
    if(!view.palette)return el('div',{class:'rb-palette closed'},iconButton('Show the rotations palette','chevronRight',()=>{view.palette=true;keep();redraw();}),el('span',{'aria-hidden':'true'},'Rotations'));
    const chip=(drop:Drop,extra:Node|null=null)=>el('button',{type:'button',class:`rb-chip${drop.hue?' duty-'+drop.hue:''}`,'data-drop':JSON.stringify(drop),'aria-roledescription':'draggable',
      'aria-label':`${drop.label}, ${lengthWords_(drop.length)}: drag onto a row, or press Enter to place it on the row last focused`},
      icon('grip'),drop.kind==='type'?el('span',{class:'tl-dot'}):null,el('span',{class:'rb-chip-name'},drop.label),el('span',{class:'rb-chip-len'},lengthWords_(drop.length)),extra);
    const types=Object.entries(board.choices.rotation_types).map(([id,label])=>chip({kind:'type',id,label,length:lengthOf(id),hue:hueOfType(id)}));
    const plans=templates().map(t=>{const blocks=(t.blocks||[]) as RotationBlock[],stripe=el('span',{class:'rb-stripe','aria-hidden':'true'},...blocks.map(b=>{
        const i=el('i',{class:'duty-'+hueOfType(b.rotation_type_id||b.service_id||'')});i.style.setProperty('--n',String(b.unit==='months'?b.length*4.345:b.length));return i;}));
      return chip({kind:'template',id:t.id,label:labelOf(t),length:templateLength(blocks),blocks:blocks.map(b=>[b.length,b.unit])},stripe);});
    const find=panelSearch('Find a rotation or template','Find a rotation or template',q=>{for(const c of [...types,...plans])c.hidden=!c.textContent!.toLowerCase().includes(q.toLowerCase());},'dt-search');
    // The palette is its own key: each type chip names itself in its hue, and a template's stripe uses those hues.
    return el('aside',{class:'rb-palette','aria-label':'Rotations to place on the board','data-lg-self':''},
      el('div',{class:'rb-palette-head'},el('strong',{},'Rotations'),iconButton('Hide the rotations palette','chevronLeft',()=>{view.palette=false;keep();redraw();},{class:'bare'})),
      el('label',{class:'dt-search-wrap'},icon('search'),find),
      el('h3',{},'Rotation types'),...types,
      ...(plans.length?[el('h3',{},'Templates'),...plans]:[]));
  }
  /** The trainee's cohort for a placement [start, end): one covering it (the server's ROTATION_WINDOW) in a programme they
   *  belong to then, the nearest rotation's cohort first when it covers; '' when none does. */
  function cohortFor(person:string,start:string,end:string):string{
    const programmes=new Set(membershipsOf(person).filter(m=>m.starts_on<end&&(!m.ends_on||m.ends_on>start)).map(m=>m.programme_id));
    const fits=(ctx.catalogues.cohorts||[]).filter(c=>programmes.has(c.programme_id)&&c.starts_on<=start&&end<=c.ends_on);
    const near=rotationsOf(person).filter(r=>r.cohort_id).sort((a,b)=>Math.abs(dayNumber(a.starts_on)-dayNumber(start))-Math.abs(dayNumber(b.starts_on)-dayNumber(start)));
    return (near.map(r=>fits.find(c=>c.id===r.cohort_id)).find(Boolean)||fits[0])?.id||'';
  }
  const REASONS:Record<Change['action'],string>={create:'Planned on the rotation board',template:'Planned from a template on the rotation board',
    move:'Moved on the rotation board',resize:'Dates changed on the rotation board',copy:'Copied on the rotation board'};
  function input(c:Change):TimelineRotationInput{
    const r=c.bar?board.rotations.find(x=>x.id===c.bar):undefined,base={action:c.action,person_id:c.row,starts_on:c.start,reason:REASONS[c.action]};
    if(c.action==='create')return {...base,ends_on:c.end,rotation_type_id:c.drop!.id,cohort_id:cohortFor(c.row,c.start,c.end)};
    if(c.action==='template')return {...base,template_id:c.drop!.id,cohort_id:cohortFor(c.row,c.start,c.end)};
    return {...base,rotation_id:r!.id,expected_revision:r!.revision,ends_on:c.end};
  }
  function drag():DragSpec|undefined{
    if(!board.can_plan&&!deps.mine||phoneQuery.matches)return undefined;  // phones read; changes are made on a wide screen
    return {movable:(bar,row)=>board.rotations.some(r=>r.id===bar)&&(board.can_plan&&!deps.mine||row===ctx.me.person_id),
      preview:async c=>{if((c.action==='create'||c.action==='template')&&!cohortFor(c.row,c.start,c.end))return {ok:false,lines:[{text:'No cohort for this trainee then: add them to a programme cohort first',tone:'error'}]};
        const x=await request<TimelinePreview>('/api/timeline/preview',{method:'POST',body:JSON.stringify(input(c))});
        return {ok:x.can_apply,lines:[...x.conflicts.map(f=>({text:f.message,tone:f.severity==='error'?'error' as const:'warn' as const})),
          ...(x.requires_approval?[{text:'Goes to your programme lead to approve',tone:'warn' as const}]:[])]};},
      apply:async c=>{try{const x=await command<TimelineRotationResult>('timeline.rotation',input(c) as unknown as Record<string,unknown>);notify(x.message);await ctx.refresh();}  // said before the refresh (see notify, dom.ts): it claims its Undo however slow the read
        catch(e){reportError(e);}}};
  }

  // ---- One rotation: hover card and docked panel ----
  const typeWords=(r:Row)=>r.department_available===false?'External: away from department duties':'Internal: duties allowed when eligible';
  function rotationCard(r:Row):Node[]{const change=changeOf(r.id);
    return [el('div',{class:'tl-card-head'},el('span',{class:`tl-swatch tl-block duty-${colour(r)}`,'aria-hidden':'true'}),el('strong',{},rotationName(r)),change?badge('Change waiting','warn'):null),
      el('div',{class:'muted'},fullNameOf(person(r.person_id))),
      ruleFacts([['Dates',`${span(r.starts_on,r.ends_on)} · ${lengthWords(r.starts_on,r.ends_on)}`],['Type',typeWords(r)],['Service',named('services',r.service_id)||'Not set'],['Cohort',named('cohorts',r.cohort_id)||'—']]),
      el('div',{class:'tl-card-foot'},'Click to open')];}
  function rotationPanel(r:Row){
    const p=person(r.person_id),change=changeOf(r.id),own=!!ctx.me.person_id&&r.person_id===ctx.me.person_id,list=rotationsOf(r.person_id),i=list.findIndex(x=>x.id===r.id);
    dockPanel({key:'rotation',title:rotationName(r),sub:fullNameOf(p),width:480,mark:`[data-bar="${CSS.escape(r.id)}"]`,
      position:{index:i,total:list.length,noun:'rotation',go:k=>rotationPanel(list[k])},build:({body,close})=>{
      const clashes=overlaps(r),referenceConflict=referenceConflicts.has(r.id),conflictCount=clashes.length+Number(referenceConflict);
      const conflictCards=()=>[
        ...(referenceConflict?[el('section',{class:'rb-card err'},el('h3',{},icon('alert'),'Leave conflict at import'),
          el('p',{},'This historical rotation was recorded for reference with an existing leave conflict.'))]:[]),
        ...clashes.map(o=>el('section',{class:'rb-card err'},el('h3',{},icon('alert'),`Overlaps ${rotationName(o.other)} · ${plural(dayNumber(o.end)-dayNumber(o.start),'day')}`),
        el('p',{},`${span(o.start,o.end)} are in both. A trainee is on one rotation at a time: change the dates of one of them.`),
        board.can_plan?el('div',{class:'actions'},withStar(button('Change dates',()=>{close();catalogueForm(ctx,'rotations',r);},'secondary small'))):null))];
      // Dates: the months around the rotation (one before, one after), it and the days it shares with another.
      const from=plusMonths(r.starts_on,-1),until=plusMonths(addDays(r.ends_on,-1),2);
      const overview=()=>el('div',{class:'rb-overview'},el('h3',{},'Dates'),
        timelineMini({label:`${rotationName(r)}, ${span(r.starts_on,r.ends_on)}${clashes.length?`, ${plural(clashes.length,'overlap')}`:''}`,start:from,end:until,today,
          bars:[{id:r.id,start:r.starts_on,end:r.ends_on,label:rotationName(r),style:'block',hue:colour(r),external:r.department_available===false,meta:lengthWords(r.starts_on,r.ends_on),aria:''}],
          zones:clashes.map(o=>({start:o.start,end:o.end,label:'',tone:'error' as const}))}),
        ruleFacts([['Trainee',fullNameOf(p)],['Dates',`${span(r.starts_on,r.ends_on)} · ${lengthWords(r.starts_on,r.ends_on)}`],['Rotation type',named('rotation_types',r.rotation_type_id)||'Not set'],
          ['Service',named('services',r.service_id)||'Not set'],['Cohort',named('cohorts',r.cohort_id)||'—'],['Department duties',typeWords(r)]]),
        ...conflictCards(),
        change?(()=>{const v=change.proposed||{};
          return el('section',{class:'rb-card wait'},el('h3',{},icon('clock'),'Change waiting'),
            el('p',{},[named('rotation_types',v.rotation_type_id),named('services',v.service_id)].filter(Boolean).join(' · ')||rotationName(r),' · ',span(v.starts_on,v.ends_on)),
            decides(change)?el('div',{class:'actions'},button('Approve',()=>decide(change,true,close),'primary small'),button('Decline',()=>decide(change,false,close),'secondary small'))
              :el('p',{class:'muted'},own?'Waiting for your programme lead.':'A programme lead decides it in Requests.'));})():null);
      const count=(text:string,n:number)=>el('span',{},text,' ',el('span',{class:'tab-count'},String(n)));
      const tabList:[string,Child,()=>Node][]=[['overview','Overview',overview],
        ['conflicts',count('Conflicts',conflictCount),()=>conflictCount?el('div',{},...conflictCards()):emptyLine('No conflicts: this rotation shares no days with another.')]];
      if(board.can_note&&p)tabList.push(['notes',count('Notes',Number(p.notes)||0),()=>{const host=el('section',{class:'rb-notes-list','data-private':''});drawNotes(p,host);return host;}]);  // never in a feedback screenshot
      if(!deps.mine)tabList.push(['history','History',()=>historyOf(r)]);
      body.append(tabs('Rotation',tabList));
      const acts:HTMLElement[]=[];
      if(own&&!change)acts.push(button('Request a change',()=>{close();changeDialog(r);},'primary'));
      if(board.can_plan){
        acts.push(button('Change dates',()=>{close();catalogueForm(ctx,'rotations',r);},'secondary'));
        const remove=button('Remove',async()=>{const x=await command<{message:string;removed:number}>('rotation.remove',{items:[{id:r.id,revision:r.revision}],reason:'Removed from the rotation board'});
          notify(x.message,x.removed?'success':'warning');close();await ctx.refresh();},'ghost danger');
        if(!change)acts.push(remove);
      }
      if(acts.length)body.append(el('div',{class:'actions rb-panel-foot'},...acts));
    }});
  }
  /** A rotation's saved versions, oldest first (the catalogue's definition history): when, its dates then, and why. */
  function historyOf(r:Row):HTMLElement{
    const host=el('div',{class:'rb-history'});
    const load=async()=>{const sk=skeleton('lines','Loading the history…');host.replaceChildren(sk);
      try{const {versions}=await get<{versions:Row[]}>(`/api/catalogues/rotations/${r.id}/history`);await settle(sk);
        host.replaceChildren(versions.length?el('ul',{class:'rb-list'},...versions.map(v=>el('li',{},el('strong',{},formatDate(v.created_at,deps.zone)),` · Version ${v.object_revision}`,
          v.snapshot?.starts_on&&v.snapshot?.ends_on?` · ${span(v.snapshot.starts_on,v.snapshot.ends_on)}`:'',v.reason?` · ${v.reason}`:''))):emptyLine('No saved versions yet.'));}
      catch(e){if(!sessionChanged(e)&&host.isConnected)host.replaceChildren(readFailed('the history',e,()=>void load()));}};
    void load();return host;
  }
  /** The fix a conflict card recommends (★). */
  const withStar=(b:HTMLButtonElement)=>{b.classList.add('recommended');b.prepend(icon('star'));return b;};
  const decides=(c:Row)=>c.created_by!==ctx.me.identity_id&&ctx.permits(CAPABILITY.PROGRAMME_LEAD,CAPABILITY.APPROVER)&&!deps.mine;
  const decide=(c:Row,approve:boolean,done:()=>void)=>confirmAction(approve?'Approve rotation change':'Decline rotation change',
    approve?'The rotation changes at once. Rotas already published keep their duties: arrange cover through a reviewed amendment.':'The rotation stays as it is. Say why: the trainee is told.',
    async reason=>{const x=await command<{message:string}>('request.decide',{decision:approve?'approve':'reject',reason},c.id,c.revision);notify(x.message);done();await ctx.refresh();},
    approve?'Approve':'Decline',undefined,approve);

  function changeDialog(r:Row){
    formDialog('Request a rotation change',`${rotationName(r)} · ${span(r.starts_on,r.ends_on)}. Your programme lead decides; nothing changes until then.`,form=>{
      // Every active type and service can be asked for (not only those on the board); the current one stays even if retired.
      const options=(kind:'rotation_types'|'services',current:string)=>[...(current&&!(current in board.choices[kind])?[{value:current,label:board.names[kind][current]||current}]:[]),
        ...Object.entries(board.choices[kind]).map(([value,label])=>({value,label}))];
      const type=select('Rotation type',options('rotation_types',r.rotation_type_id||''),r.rotation_type_id||'',false);
      const service=select('Service',options('services',r.service_id||''),r.service_id||'',false);
      const from=field('From','date',r.starts_on,{required:true}),until=field('Until (included)','date',r.ends_on,{required:true});
      lastDayIncluded(until.input);
      const why=area('Why',' ',3);why.input.value='';why.input.required=true;why.input.minLength=3;why.wrap.classList.add('span-2');
      why.wrap.setAttribute('data-private','');  // never in a feedback screenshot
      form.append(type.wrap,service.wrap,dateRange('Dates',from,until),why.wrap);
      return async()=>{const x=await command<{message:string}>('rotation.request_change',{rotation_id:r.id,rotation_type_id:type.input.value||null,service_id:service.input.value||null,
        starts_on:from.input.value,ends_on:until.input.value,private_reason:why.input.value});notify(x.message);await ctx.refresh();};
    },'Send request');
  }

  // ---- One trainee: programmes, rotations, leave, notes ----
  function traineePanel(p:Row){
    dockPanel({key:'trainee',title:fullNameOf(p),sub:membershipsOf(p.id).map(m=>named('programmes',m.programme_id)).join(', '),width:560,mark:`[data-row="${CSS.escape(p.id)}"]`,build:({body})=>{
      body.append(el('h3',{},'Programmes'),el('ul',{class:'rb-list'},...membershipsOf(p.id).map(m=>el('li',{},el('strong',{},named('programmes',m.programme_id)),' · ',span(m.starts_on,m.ends_on),
        m.external?badge('External rotator'+(m.home_programme?' · from '+m.home_programme:''),'warn'):null))));
      const list=rotationsOf(p.id);
      body.append(el('h3',{},'Rotations in these months'),list.length?el('ul',{class:'rb-list'},...list.map(r=>{const b=el('button',{type:'button',class:'link-button'},rotationName(r));b.addEventListener('click',()=>rotationPanel(r));
        return el('li',{},el('span',{class:`rb-swatch duty-${colour(r)}`,'aria-hidden':'true'}),b,' · ',span(r.starts_on,r.ends_on),changeOf(r.id)?badge('Change requested','warn'):null);})):emptyLine('None.'));
      const leave=leaveOf(p.id);
      body.append(el('h3',{},'Leave and protected time'),leave.length?el('ul',{class:'rb-list'},...leave.map(x=>el('li',{},x.label,' · ',span(x.starts_on,x.ends_on),x.stage==='waiting'?badge('Waiting','warn'):null))):emptyLine('None in these months.'));
      if(board.can_plan)body.append(el('div',{class:'actions'},button('Plan rotations',()=>planDialog([p.id]),'secondary'),button('Record protected time',()=>protectedDialog(p),'secondary')));
      if(board.can_note){const notes=el('section',{class:'rb-notes-list','data-private':''});  // never in a feedback screenshot
        body.append(notes);drawNotes(p,notes);}
    }});
  }
  // The note being written stays while existing notes are edited or deleted; only its own save clears it.
  function drawNotes(p:Row,host:HTMLElement){
    const list=el('div',{}),text=area('Add a note','',3);text.wrap.classList.add('span-2');
    const composer=el('div',{},el('div',{class:'form-grid'},text.wrap));
    const refresh=()=>ctx.refresh();
    // Every note save redraws the list, Undo's included (the dock keeps this panel across the page's refresh).
    const saved=(e:Event)=>{if(!host.isConnected){window.removeEventListener('rota-saved',saved);return;}
      if(String((e as CustomEvent).detail?.kind).startsWith('trainee_note.'))void drawList();};
    window.addEventListener('rota-saved',saved);
    // The box is read-only while it saves, so nothing typed meanwhile is cleared with the saved text.
    const add=button('Save note',async()=>{if(!text.input.value.trim())throw new Error('Write the note first.');
      text.input.readOnly=true;
      try{await command('trainee_note.create',{person_id:p.id,body:text.input.value});}finally{text.input.readOnly=false;}
      notify('Note saved.');text.input.value='';composer.dispatchEvent(new Event('rota-form-saved',{bubbles:true}));await refresh();},'secondary');
    composer.append(el('div',{class:'actions'},add));
    // A failed read says so in the list, with Retry (the composer stays usable).
    async function drawList(){
      let notes:Row[];
      try{({notes}=await get<{notes:Row[]}>(`/api/people/${p.id}/trainee-notes`));}
      catch(e){if(!sessionChanged(e)&&host.isConnected)list.replaceChildren(readFailed('the notes',e,()=>void drawList()));return;}
      list.replaceChildren(...notes.map(n=>el('article',{class:'rb-note'},el('p',{},n.body),el('small',{class:'muted'},`${n.author_name} · ${formatDate(n.updated_at||n.created_at,deps.zone)}`),
        n.mine?el('div',{class:'actions'},button('Edit',()=>formDialog('Edit note','',form=>{const t=area('Note',n.body,4);t.wrap.classList.add('span-2');form.append(t.wrap);
          return async()=>{await command('trainee_note.update',{body:t.input.value},n.id,n.revision);notify('Note saved.');await refresh();};}),'ghost small'),
          button('Delete',async()=>{await command('trainee_note.delete',{},n.id,n.revision);notify('Note deleted.');await refresh();},'ghost small danger')):null)));
    }
    host.append(el('h3',{},'Notes'),el('p',{class:'hint'},'Only programme leads read these. The trainee never sees them.'),list,composer);
    void drawList();
  }
  function protectedDialog(p:Row){
    formDialog('Record protected time',`${fullNameOf(p)} is kept off duties at this time (teaching, study or their home programme's own days).`,form=>{
      const day=field('Day','date',today,{required:true}),from=field('From','time','08:00',{required:true}),until=field('Until','time','12:00',{required:true});
      const why=field('Note','text','',{required:true,minlength:3,maxlength:200,placeholder:'e.g. home programme academic day'});why.wrap.classList.add('span-2');
      form.append(day.wrap,from.wrap,until.wrap,why.wrap);
      return async()=>{const [a,b]=await Promise.all([instant(`${day.input.value}T${from.input.value}`),instant(`${day.input.value}T${until.input.value}`)]);
        const x=await command<{message:string}>('availability.record',{person_id:p.id,kind:'protected_time',start:a,end:b,private_reason:why.input.value});notify(x.message);await ctx.refresh();};
    },'Record');
  }

  // ---- Plan and copy ----
  /** `kept`: what was set up before (a gap's own first day; the form as it was before New cohort). */
  function planDialog(people:string[]=[],kept:{from?:string;template?:string;blocks?:RotationBlock[]}={}){
    const templates=(ctx.catalogues.rotation_templates||[]).filter(t=>t.active!==false);
    formDialog('Plan rotations','Each trainee gets the blocks in order from the start date. Blocks they already have a rotation for are skipped.',form=>{
      const cohorts=(ctx.catalogues.cohorts||[]);
      const template=select('Start from a template',templates.map(t=>({value:t.id,label:labelOf(t)})),kept.template||'',false,'No template');
      const cohort=select('Cohort',cohorts.map(c=>({value:c.id,label:labelOf(c)+' · '+span(c.starts_on,c.ends_on)})),cohorts.find(c=>c.starts_on<=today&&c.ends_on>today)?.id||cohorts[0]?.id||'');
      const members=()=>{const c=cohorts.find(x=>x.id===cohort.input.value);return [...new Set((ctx.catalogues.programme_memberships||board.memberships).filter(m=>c&&m.programme_id===c.programme_id&&m.starts_on<c.ends_on&&m.ends_on>c.starts_on&&m.person_id!==ctx.me.person_id).map(m=>m.person_id))];};
      const trainees=()=>{const w=multi('Trainees',peopleChoices(ctx),people.length?people:members(),'Choose one or more trainees');w.wrap.classList.add('span-2');return w;};
      let who=trainees();
      // Another cohort may be another programme: its own members become the trainees (unless the trainee was chosen).
      if(!people.length)cohort.input.addEventListener('change',()=>{const next=trainees();who.wrap.replaceWith(next.wrap);who=next;});
      const from=field('First block starts','date',kept.from||start,{required:true});
      const stagger=check('Stagger the blocks',!people.length,'Trainee 1 starts at block 1, trainee 2 at block 2, and so on, so the same block is not taken by everyone at once.');
      const host=el('div',{class:'span-2'});let blocks=rotationBlocks(ctx,kept.blocks||[]);host.append(blocks.wrap);
      template.input.addEventListener('change',()=>{const t=templates.find(x=>x.id===template.input.value);blocks=rotationBlocks(ctx,(t?.blocks||[]) as RotationBlock[]);host.replaceChildren(blocks.wrap);});
      // No cohort to choose (P-17): say so under the field, with the step that creates one (the form closes for it).
      if(!cohorts.length)cohort.wrap.append(el('p',{class:'hint missing-setup',role:'status'},'No cohorts yet. ',
        // The plan opens again after the cohort is saved, with what was filled in here.
        button('New cohort',()=>{let held:RotationBlock[]=[];try{held=blocks.read();}catch{/* an unfinished block row is not kept */}
          const again={from:from.input.value,template:template.input.value,blocks:held};const d=cohort.wrap.closest('dialog');d?.close();d?.remove();
          catalogueForm(ctx,'cohorts',undefined,undefined,{note:'',run:()=>planDialog(people,again)});},'link-button')));
      form.append(template.wrap,cohort.wrap,who.wrap,from.wrap,stagger.wrap,host);
      return async()=>{if(!cohorts.length)throw new Error('Create a cohort first.');
        if(!who.values().length)throw new Error('Choose at least one trainee.');
        const t=templates.find(x=>x.id===template.input.value),c=cohorts.find(x=>x.id===cohort.input.value);
        if(t?.programme_id&&t.programme_id!==c?.programme_id)throw new Error('This template belongs to another programme. Choose its cohort or another template.');
        const x=await command<{message:string;created:Row[];skipped:{starts_on:string;ends_on:string;why:string}[]}>('rotation.plan',{cohort_id:cohort.input.value,person_ids:who.values(),starts_on:from.input.value,blocks:blocks.read(),
          template_id:template.input.value||null,stagger:stagger.input.checked,reason:'Planned on the rotation board'});
        // Nothing planned is not a success: say which blocks were skipped and why, in the cohort's own dates.
        if(!x.created?.length&&x.skipped?.length){const why=[...new Set(x.skipped.map(s=>s.why==='outside_cohort'?`outside the cohort (${c?span(c.starts_on,c.ends_on):'its dates'})`:'they already have a rotation then'))];
          notify(`No rotation planned: ${plural(x.skipped.length,'block')} skipped, ${why.join('; ')}. First block ${span(x.skipped[0].starts_on,x.skipped[0].ends_on)}.`,'warning');}
        else notify(x.message);await ctx.refresh();};
    },'Plan rotations');
  }
  function referenceImportDialog(){
    formDialog('Import reference rotations','Choose a JSON file with a rotations list. Each row needs person_id, cohort_id, starts_on, ends_on, and a rotation_type_id or service_id. The end date is exclusive.',form=>{
      const file=field('Reference file','file','',{required:true,accept:'.json,application/json'});
      const reason=area('Reason','');reason.input.required=true;
      form.append(file.wrap,reason.wrap);
      return async()=>{
        const picked=file.input.files?.[0];
        if(!picked)throw new Error('Choose a reference file.');
        if(picked.size>REFERENCE_ROTATION_FILE_LIMIT)throw new Error('Choose a file up to '+REFERENCE_ROTATION_FILE_LIMIT/1000+' KB.');
        const payload:ReferenceRotationImport={file_text:await picked.text(),reason:reason.input.value};
        const review=await request<ReferenceRotationReview>('/api/rotation-board/reference-preview',{
          method:'POST',body:JSON.stringify(payload)});
        formDialog('Review reference rotations',plural(review.count,'rotation')+' to record for reference.',confirmation=>{
          if(review.conflicts.length)confirmation.append(el('ul',{class:'span-2'},...review.conflicts.map(conflict=>
            el('li',{},'Rotation '+conflict.row+'. '+conflict.messages.join(' ')))));
          return async()=>{
            await command('rotation.import_reference',{...payload,expected_fingerprint:review.expected_fingerprint});
            await ctx.refresh();
          };
        },'Import rotations');
      };
    },'Review file');
  }

  function copyDialog(){
    formDialog('Copy rotations forward','Recreate rotations for the same trainees, moved by a number of months. Ones that would overlap an existing rotation are skipped.',form=>{
      const from=field('Rotations starting from','date',start,{required:true}),until=field('Until (included)','date',end,{required:true});lastDayIncluded(until.input);
      const months=field('Move forward by · months','number',12,{required:true,min:1,max:60});
      const cohorts=ctx.catalogues.cohorts||[];
      const cohort=select('Into cohort',cohorts.map(c=>({value:c.id,label:labelOf(c)+' · '+span(c.starts_on,c.ends_on)})),'',false,'Keep each rotation’s cohort');
      form.append(dateRange('Copy',from,until),months.wrap,cohort.wrap);
      return async()=>{const x=await command<{message:string}>('rotation.copy',{starts_on:from.input.value,ends_on:until.input.value,months:Number(months.input.value),
        cohort_id:cohort.input.value||null,reason:'Copied forward on the rotation board'});notify(x.message);await ctx.refresh();};
    },'Copy rotations');
  }

  draw();
}
