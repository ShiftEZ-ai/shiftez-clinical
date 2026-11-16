/** The Schedule grid: any dimension as rows; days, weeks, weekdays, months or another dimension as columns; nested
 *  groups (Group by → Then by), collapsible and saved with the view; cells as items or counts. One renderer for the
 *  Timeline and every Counts layout; its items are the one item renderer's (schedule_item.ts), as every layout's are.
 *  Dimensions: schedule_dimensions.ts. */
import {el,legend,popover,formatDate,formatTime,toLocal,plural,hoverCard,labelOf,icon,WEEKDAYS} from './dom.js';
import {coverOf,dutyColour,weekView,collapsed,placeRow,groupWay,scheduleWidths,shownGroups,colourRule,type WeekHooks,markWeekend,markToday,roving} from './planner_week.js';
import {propertiesOf,sessionCode,itemParts,itemNodes,itemWords,itemHead,itemSaysWords,ITEM_DRAWS} from './schedule_item.js';
import {leaveLayer} from './planner_people.js';
import {addDays,isClosed,isWeekend} from './planner_logic.js';
import {dimOf,personDim,isTime,dimensionReader,byValue,type Dim,type Column,type TimeAxis} from './schedule_dimensions.js';
import {scheduleEmpty} from './schedule.js';
import type {Context} from './forms.js';
import type {Row,WorkspaceData} from './types.js';
import {clashTag,clashText,clashesOf} from './double_booking.js';
import type {LiveHost} from './autoschedule_live.js';
import {groupToggle} from './dom.js';  // a group's header is its toggle (UX › controls S5)
import {overflowMore} from './dom.js';  // the one "+N" (DESIGN D8)

export type Cells='codes'|'counts';
export type Axes={rows:Dim;columns:Column;group:Dim|'none';subgroup:Dim|'none';cells:Cells};
type Bucket={key:string;head:string;sub:string;label:string;weekend:boolean;today?:boolean};

/** Time columns: each day; each week (from the view's week start); each weekday shown, all weeks folded; each month. */
export function bucketsOf(days:string[],columns:TimeAxis,weekStart:number,zone:string,today=''):{list:Bucket[];of:(day:string)=>string}{
  const dow=(d:string)=>(new Date(d+'T12:00:00Z').getUTCDay()+6)%7;  // Monday 0 … Sunday 6
  const fmt=(d:string,o:Intl.DateTimeFormatOptions)=>new Intl.DateTimeFormat('en-GB',{...o,timeZone:'UTC'}).format(new Date(d+'T12:00:00Z'));
  if(columns==='week'){
    const first=(d:string)=>addDays(d,-((dow(d)-weekStart+7)%7)),keys=[...new Set(days.map(first))];
    return {of:first,list:keys.map(k=>({key:k,head:'Week of',sub:fmt(k,{day:'numeric',month:'short'}),label:`Week of ${formatDate(k,zone)}`,weekend:false}))};
  }
  if(columns==='weekday'){
    const keys=[...new Set(days.map(dow))].sort((a,b)=>(a-weekStart+7)%7-(b-weekStart+7)%7);
    return {of:d=>String(dow(d)),list:keys.map(k=>({key:String(k),head:WEEKDAYS[k].slice(0,3),sub:'',label:`Every ${WEEKDAYS[k]}`,weekend:false}))};
  }
  if(columns==='month'){
    const keys=[...new Set(days.map(d=>d.slice(0,7)))];
    return {of:d=>d.slice(0,7),list:keys.map(k=>({key:k,head:fmt(k+'-15',{month:'short'}),sub:k.slice(0,4),label:fmt(k+'-15',{month:'long',year:'numeric'}),weekend:false}))};
  }
  return {of:d=>d,list:days.map(d=>({key:d,head:fmt(d,{weekday:days.length>10?'narrow':'short'}),sub:String(Number(d.slice(8))),label:formatDate(d,zone),weekend:isWeekend(d),today:d===today}))};
}

/** One unit of the grid: a session, or one person's duty on it when any axis is a person's (rows, columns or groups). */
type Unit={s:Row;pid:string;day:string};
type Line={key:string;name:string;colour?:string;pid?:string;units:Unit[]};

export function scheduleGrid(ctx:Context,data:WorkspaceData,visible:Row[],days:string[],axes:Axes,weekStart:number,hooks:WeekHooks,window?:{start:string;end:string}|null):HTMLElement{
  const zone=ctx.me.organisation.timezone,key=legend('Key'),cover=coverOf(data),checked=!!data.workspace.validation,colours=colourRule(ctx,days,key);
  const people=new Map(ctx.catalogues.people.map(p=>[p.id,p])),locations=new Map((ctx.catalogues.locations||[]).map(l=>[l.id,l]));
  const dayOf=(s:Row)=>toLocal(s.start,zone).slice(0,10),clock=(iso:string)=>toLocal(iso,zone).slice(11,16),inRange=new Set(days);
  const today=toLocal(new Date().toISOString(),zone).slice(0,10),counts=axes.cells==='counts';
  const broken=new Set(((data.workspace.validation?.findings||[]) as unknown as Row[]).filter(f=>f.severity==='error'&&f.shift_id).map(f=>f.shift_id));
  const short=(s:Row)=>{const c=cover.get(s.id);return !!c&&c.available<c.required;};
  // A group or Then by naming the rows' or columns' dimension (an older view) is left out.
  const group=axes.group!=='none'&&axes.group!==axes.rows&&axes.group!==axes.columns?axes.group:null;
  const sub=group&&axes.subgroup!=='none'&&![axes.rows,axes.columns,group].includes(axes.subgroup)?axes.subgroup:null;
  const byPerson=[axes.rows,axes.columns,group,sub].some(k=>!!k&&personDim(k));
  const read=dimensionReader(ctx.catalogues,hooks.name);
  // Published sessions around or under a draft show as grey context chips; they are never counted (the draft's are).
  const shown=visible.filter(s=>inRange.has(dayOf(s))&&!isClosed(s)&&!(counts&&data.context&&s.context)&&(!weekView.onlyOpen||short(s))&&(!weekView.onlyIssues||broken.has(s.id))
    &&(!window||clock(s.end)>window.start&&clock(s.start)<window.end));
  const units:Unit[]=shown.flatMap(s=>{const day=dayOf(s);return byPerson?(s.participants as Row[]).map(p=>({s,pid:String(p.person_id),day})):[{s,pid:'',day}];});
  const values=(k:Dim,u:Unit)=>read(k,u.s,u.pid,u.day);
  const time=isTime(axes.columns)?bucketsOf(days,axes.columns,weekStart,zone,today):null;
  const colsOf=(u:Unit)=>time?[time.of(u.day)]:values(axes.columns as Dim,u);
  // A session's own values (duty type, service, location) come from every session shown, held or not, so an open duty
  // has its column or row (and an auto-schedule run filling it has a place to show it); a person's need someone on it.
  // Published context sessions are read-only (never filled by a run), so only the draft's count.
  const open:Unit[]=byPerson?shown.filter(s=>!s.context).map(s=>({s,pid:'',day:dayOf(s)})):[];
  const columns:Bucket[]=time?time.list:[...new Set([...units,...(personDim(axes.columns)?[]:open)].flatMap(colsOf))].sort(byValue).map(v=>({key:v,head:v,sub:'',label:v,weekend:false}));
  // Group › Then by › row; a unit with several values (two tags) is in each.
  const tree=new Map<string,Map<string,Map<string,Line>>>();
  const put=(u:Unit,held:boolean)=>{for(const g of group?values(group,u):[''])for(const sg of sub?values(sub,u):[''])
    for(const r of axes.rows==='person'?[u.pid]:values(axes.rows,u)){
      let a=tree.get(g);if(!a)tree.set(g,a=new Map());let b=a.get(sg);if(!b)a.set(sg,b=new Map());
      let l=b.get(r);if(!l)b.set(r,l={key:r,name:axes.rows==='person'?hooks.name(people.get(r)):r,...(axes.rows==='type'?{colour:dutyColour(u.s)}:{}),...(axes.rows==='person'?{pid:r}:{}),units:[]});
      if(held)l.units.push(u);
    }};
  for(const u of units)put(u,true);
  // With people rows, an open session still opens its group and Then by of session values (a heading and the group's
  // Staffed / needed, no person line), so a run filling it has a heading to add the person under. Other rows of a
  // person's value (rank, programme, tag) would leave an empty heading nothing can fill live, so they get none.
  const openIn=new Map<string,Unit[]>();
  for(const u of open){
    if(![axes.rows,group,sub].some(k=>!!k&&personDim(k))){put(u,false);continue;}
    if(axes.rows!=='person'||group&&personDim(group))continue;
    for(const g of group?values(group,u):['']){
      let a=tree.get(g);if(!a)tree.set(g,a=new Map());let o=openIn.get(g);if(!o)openIn.set(g,o=[]);o.push(u);
      if(sub&&!personDim(sub))for(const sg of values(sub,u))if(!a.has(sg))a.set(sg,new Map());
    }
  }
  const hoursOf=(u:Unit)=>{const p=(u.s.participants as Row[]).find(x=>x.person_id===u.pid);return (Date.parse(p?.end||u.s.end)-Date.parse(p?.start||u.s.start))/36e5;};
  const sessionsOf=(list:Unit[])=>[...new Set(list.map(u=>u.s))];
  const fill=(list:Row[])=>({need:list.reduce((a,s)=>a+(cover.get(s.id)?.required||0),0),have:list.reduce((a,s)=>{const c=cover.get(s.id);return a+(c?Math.min(c.required,c.available):0);},0)});
  // A count of places the live auto-schedule view keeps up to date: the sessions it adds up.
  const tally=(n:HTMLElement,own:Row[])=>{if(own.length){n.dataset.count='';n.dataset.sessions=own.map(s=>s.id).join(' ');}return n;};
  const rowsWord=dimOf(axes.rows),dimWord=(k:Dim)=>dimOf(k).label;
  const cls=counts?'people-grid tl-grid ct-grid sg-grid':'people-grid tl-grid sg-grid';
  const grid=el('div',{class:cls+(time?'':' sg-cat'),role:'grid','aria-label':`${counts?'Counts':'Timeline'}, ${rowsWord.many} by ${time?axes.columns:dimWord(axes.columns as Dim).toLowerCase()}, ${formatDate(days[0],zone)} to ${formatDate(days.at(-1)!,zone)}`});
  grid.style.setProperty('--days',String(Math.max(1,columns.length)));
  if(!counts&&itemSaysWords(propertiesOf(weekView.props,'chip')))grid.classList.add('it-words');
  // A weekend or today column's shading is that entry in the key, on its head and every cell (every mark has one).
  const shade=<E extends Element>(n:E,b:Bucket):E=>{if(b.weekend)markWeekend(key,n);if(b.today)markToday(key,n);return n;};
  const ctxCls=(b:Bucket)=>(b.weekend?' weekend':'')+(b.today?' today':'')+(time&&axes.columns==='day'&&data.context&&(b.key<data.workspace.starts_on||b.key>=data.workspace.ends_on)?' sd-ctx':'');
  const lastHead=axes.rows==='person'?'Hours':'Total';
  grid.append(el('div',{class:'pg-row pg-head',role:'row'},el('div',{class:'pg-name',role:'columnheader'},[group&&dimWord(group),sub&&dimWord(sub),rowsWord.label].filter(Boolean).join(' · ')),
    ...columns.map(c=>shade(el('div',{class:'pg-day'+ctxCls(c)+(time?'':' sg-cat-head'),role:'columnheader','aria-label':c.label},time?el('small',{},c.head):null,el('span',{class:time?'':'truncate',title:time?undefined:c.label},time?c.sub:c.head)),c)),
    el('div',{class:'pg-hours',role:'columnheader'},lastHead)));
  scheduleWidths(grid,{row:grid.querySelector<HTMLElement>(':scope>.pg-head>.pg-name'),col:grid.querySelector<HTMLElement>(':scope>.pg-head>.pg-day'),colKey:time?'date':'cell'});
  // A count cell is the drawn mark of what it counts (dom.ts legend `mark`).
  const counted=(n:HTMLElement)=>byPerson?key.mark(n,'duties',el('b',{class:'lg-mark','aria-hidden':'true'},'2'),'Duties',0)
    :checked?key.mark(n,'fill',el('b',{class:'tl-short lg-mark','aria-hidden':'true'},'2/3'),'Places filled / needed (red when short)',0)
    :key.mark(n,'sessions',el('b',{class:'lg-mark','aria-hidden':'true'},'3'),'Sessions (places once the rota is checked)',0);
  // Every chip and "+N" opens through one listener on the grid (no listener per cell at department scale).
  const sessions=new Map(shown.map(s=>[String(s.id),s])),more=new WeakMap<HTMLElement,{units:Unit[];title:string}>();
  // The view's properties for a grid cell (the one item renderer): what each chip says, in the view's order and form.
  const props=propertiesOf(weekView.props,'chip'),name=(id:string)=>hooks.name(people.get(id));
  const partsOf=(u:Unit)=>itemParts(ctx,u.s,u.pid,zone,props,{only:ITEM_DRAWS.chip,name});
  /** A chip's accessible name (the live auto-schedule view names its chips the same way); its duty type goes in the key,
   *  stamped on `node`, the chip drawn. */
  const chipName=(s:Row,extra:string,clash:boolean,node:Element)=>{
    const enter=(k:string,swatch:HTMLElement,words:string,order:number)=>key.mark(node,k,swatch,words,order);
    // Duty types in the key, each with its code; their hue is the duty type's only when the view colours by it.
    enter('t:'+s.assignment_type_id,el('span',{class:`pg-chip duty-${weekView.colourBy==='type'?dutyColour(s):'slate'}`,'aria-hidden':'true'},sessionCode(ctx,s)),s.type_snapshot.name,0);
    if(s.context)enter('ctx',el('span',{class:'pg-chip sd-greychip lg-mark','aria-hidden':'true'},sessionCode(ctx,s)),'Published, read-only',4);
    return `${extra}${s.type_snapshot.name}, ${formatDate(s.start,zone)} ${formatTime(s.start,zone)}–${formatTime(s.end,zone)}${clash?', double-booked':''}`;};
  const moreKey=(node:Element)=>{key.mark(node,'more',el('b',{class:'tl-more lg-mark','aria-hidden':'true'},'+2'),'More here: select to see all',3);};
  const cellLabel=new WeakMap<HTMLElement,string>();
  const chip=(u:Unit,extra='',parts=partsOf(u))=>{
    const s=u.s,clash=clashTag(s,u.pid||undefined),words=itemWords(parts);
    const b=el('button',{type:'button',class:`pg-chip${s.context?' sd-greychip':''}`,'data-session':s.id,'data-person':u.pid},
      ...itemNodes(parts,key,x=>x===itemHead(parts)?'pg-chip-head':x.key==='time'?'pg-chip-time':'pg-chip-meta',{head:true}),clash);
    // The chip is the drawn mark of its code and its hue (dom.ts legend `mark`).
    // The place is in its name unless a shown property says it (QA-127a): a row may be a person, not a place.
    const place=parts.some(x=>x.key==='location')?'':', '+labelOf(locations.get(s.location_id));
    b.setAttribute('aria-label',chipName(s,extra,!!clash,b)+place+(words.length?', '+words.join(', '):''));
    const c=weekView.colourBy==='type'?dutyColour(s):colours.seenChip(s,u.pid,b);b.classList.add('duty-'+c);
    hoverCard(b,()=>[el('div',{class:'hc-head'},el('span',{class:`hc-badge duty-${c}`},sessionCode(ctx,s)),el('div',{},el('strong',{},s.type_snapshot.name),el('small',{},`${formatDate(s.start,zone)} · ${formatTime(s.start,zone)}–${formatTime(s.end,zone)} · ${labelOf(locations.get(s.location_id))}`))),
      ...clashesOf(s,u.pid||undefined,true).map(x=>el('p',{class:'clash-tag'},clashText(x)))]);
    return b;};
  const openSession=(s:Row)=>s.context?(hooks.view||hooks.details)(s):hooks.open?hooks.open(s):hooks.details(s);
  grid.addEventListener('click',e=>{
    const t=e.target as Element,m=t.closest<HTMLElement>('.sg-more'),c=t.closest<HTMLElement>('[data-session]');
    if(m&&more.has(m)){const {units:list,title}=more.get(m)!;popover(m,panel=>panel.append(el('div',{class:'popover-head'},el('strong',{},title),el('small',{},plural(list.length,'session'))),
      el('ul',{class:'tl-day'},...list.map(u=>el('li',{},chip(u),el('span',{class:'grow'},u.s.type_snapshot.name),el('span',{class:'muted mono'},`${formatTime(u.s.start,zone)}–${formatTime(u.s.end,zone)}`))))),{label:title,placement:'below'});return;}
    const s=c&&sessions.get(c.dataset.session||'');if(s)openSession(s);
  });
  // Leave and wishes (the view's property, D80) on a person's day cells: people rows by day only.
  const leaves=axes.rows==='person'&&axes.columns==='day'&&propertiesOf(weekView.props).some(p=>p.key==='leave'&&p.on)?leaveLayer(ctx,data,key,zone,true):null;
  const cell=(list:Unit[],label:string,b:Bucket,pid='')=>{const n=drawCell(list,label,b);if(leaves&&pid){leaves.wish(n,pid,b.key);leaves.leave(n,pid,b.key);}return n;};
  const drawCell=(list:Unit[],label:string,b:Bucket)=>{
    const node=shade(el('div',{class:'pg-cell'+(counts?' sg-count':'')+ctxCls(b),role:'gridcell','data-col':b.key}),b);
    cellLabel.set(node,label);
    if(byPerson)node.dataset.units=[...list].sort((x,y)=>String(x.s.start).localeCompare(String(y.s.start))).map(u=>`${u.s.id}~${u.pid}`).join(' ');
    if(!list.length){node.setAttribute('aria-label',`${label}: none`);return node;}
    if(counts){
      counted(node);
      if(byPerson){node.setAttribute('aria-label',`${label}: ${plural(list.length,'duty','duties')}`);node.textContent=String(list.length);return node;}
      const own=sessionsOf(list);
      if(!checked){node.setAttribute('aria-label',`${label}: ${plural(own.length,'session')}`);node.textContent=String(own.length);return node;}
      const {need,have}=fill(own);if(have<need)node.classList.add('short');tally(node,own);
      node.setAttribute('aria-label',`${label}: ${have} of ${need} places filled`);node.textContent=`${have}/${need}`;return node;
    }
    const sorted=[...list].sort((a,b)=>String(a.s.start).localeCompare(String(b.s.start)));
    // Stacked chips, one per thing they say with its count ("2×M"); a repeated one opens the list. Past two, "+N".
    const bySaid=new Map<string,{parts:ReturnType<typeof partsOf>;units:Unit[]}>();
    for(const u of sorted){const parts=partsOf(u),k=parts.map(x=>x.key+':'+x.text).join('|')||'t:'+u.s.assignment_type_id;const l=bySaid.get(k);if(l)l.units.push(u);else bySaid.set(k,{parts,units:[u]});}
    const codes=[...bySaid.values()],drawn=codes.length>2&&!weekView.forPrint?codes.slice(0,1):codes;  // print shows every one
    for(const {parts,units} of drawn){
      if(units.length===1){node.append(chip(units[0],label+', ',parts));continue;}
      const c=chip(units[0],'',parts);c.classList.add('sg-more');c.setAttribute('aria-label',`${units.length} ${units[0].s.type_snapshot.name} sessions, ${label}`);
      const head=c.querySelector<HTMLElement>(':scope>b');if(head)head.textContent=`${units.length}×${head.textContent}`;else c.prepend(el('b',{},`${units.length}×`));
      more.set(c,{units,title:label});node.append(c);
    }
    const rest=codes.slice(drawn.length).flatMap(x=>x.units);
    if(rest.length){
      const m=overflowMore(rest.length,['session','sessions'],{of:`in ${label}`,action:`show all ${sorted.length}`,cls:'link-button tl-more sg-more'});
      moreKey(m);more.set(m,{units:sorted,title:label});node.append(m);
    }
    return node;};
  // The groups' fill per day: only when rows are people and the group is of sessions (a session's places are no rank's).
  const coverRow=!!time&&axes.rows==='person'&&!(group&&personDim(group))&&weekView.staffing;
  const coverLine=(list:Unit[])=>{
    const by=new Map<string,Row[]>();for(const s of sessionsOf(list))if(!s.context){const k=time!.of(dayOf(s));by.set(k,[...(by.get(k)||[]),s]);}
    return el('div',{class:'pg-row tl-cover',role:'row'},el('div',{class:'pg-name',role:'rowheader'},el('small',{},checked?'Staffed / needed':'Sessions')),
      ...columns.map(b=>{const own=by.get(b.key)||[],{need,have}=fill(own),isShort=checked&&have<need;
        const n=tally(shade(el('div',{class:'pg-cell sg-count'+(isShort?' short':'')+ctxCls(b),role:'gridcell','aria-label':`${b.label}: ${!own.length?'no sessions':checked?`${have} of ${need} places filled`:plural(own.length,'session')}`},!own.length?'':checked?`${have}/${need}`:String(own.length)),b),checked?own:[]);
        return isShort?key.mark(n,'short',el('b',{class:'tl-short lg-mark','aria-hidden':'true'},'2/3'),'Short of staff',2):n;}),
      el('div',{class:'pg-hours',role:'gridcell'}));};
  const total=(line:Line)=>{
    if(lastHead==='Hours')return `${Math.round(line.units.reduce((a,u)=>a+hoursOf(u),0))} h`;
    if(byPerson)return String(line.units.length);
    const own=sessionsOf(line.units);return checked?(({need,have})=>`${have}/${need}`)(fill(own)):String(own.length);};
  const order=(lines:Line[])=>lines.map(l=>({l,hours:l.units.reduce((a,u)=>a+hoursOf(u),0),code:l.pid?hooks.code(people.get(l.pid)):''}))
    .sort((a,b)=>(axes.rows!=='person'?0:weekView.sortBy==='hours'?b.hours-a.hours:weekView.sortBy==='code'?a.code.localeCompare(b.code):0)||byValue(a.l.name,b.l.name)).map(x=>x.l);
  // A group's heading by its id, with the rows it counts (the live auto-schedule view adds a person's row to them).
  const heads=new Map<string,{keys:Set<string>;count:HTMLElement}>();
  const head=(name:string,id:string,keys:Set<string>,level:number)=>{
    const shut=collapsed.has(id);
    const t=groupToggle(el('strong',{class:'clamp-2',title:name},name),plural(keys.size,rowsWord.one,rowsWord.many),!shut,
      ()=>{shut?collapsed.delete(id):collapsed.add(id);root.replaceWith(scheduleGrid(ctx,data,visible,days,axes,weekStart,hooks,window));},{cls:'wk-toggle'});
    // The live auto-schedule view counts rows into an open group's chip (a collapsed group adds none, below).
    heads.set(id,{keys,count:t.querySelector<HTMLElement>('.gt-count')!});
    grid.append(el('div',{class:'pg-row pg-group'+(level?' sg-sub':''),role:'row','data-group':id},el('div',{class:'pg-group-head wk-group-head',role:'rowheader','aria-level':String(level+1)},t)));
    return shut;};
  // The live auto-schedule view (below) finds a row by its group and key; its last cell carries what it adds up.
  const rowAt=new Map<string,HTMLElement>();
  const lineRow=(line:Line,id:string)=>{
    const by=new Map<string,Unit[]>();for(const u of line.units)for(const k of colsOf(u))by.set(k,[...(by.get(k)||[]),u]);
    const last=el('div',{class:'pg-hours',role:'gridcell'},total(line));
    if(lastHead==='Hours')last.dataset.hours=String(line.units.reduce((a,u)=>a+hoursOf(u),0));
    else if(byPerson)last.dataset.duties=String(line.units.length);
    else if(checked)tally(last,sessionsOf(line.units));
    const row=el('div',{class:'pg-row',role:'row','data-group':id,...(line.pid?{'data-row-person':line.pid}:{})},el('div',{class:'pg-name'+(line.colour?' duty-'+line.colour:''),role:'rowheader',...(line.colour?{'data-lg-self':true}:{})},el('strong',{class:'truncate',title:line.name},line.name)),
      ...columns.map(b=>cell(by.get(b.key)||[],`${line.name}, ${b.label}`,b,line.pid)),last);
    rowAt.set(id+'\n'+line.key,row);return row;};
  const lines=(map:Map<string,Line>,id:string)=>{for(const line of order([...map.values()]))grid.append(lineRow(line,id));};
  const keysOf=(m:Map<string,Map<string,Line>>)=>new Set([...m.values()].flatMap(x=>[...x.keys()]));
  // Collapse all / Expand all (the toolbar's Group) reach every head drawn here.
  shownGroups.length=0;
  for(const [g,subs] of [...tree.entries()].sort((a,b)=>byValue(a[0],b[0],group?groupWay(group):1))){
    if(group){shownGroups.push(g);if(sub)for(const sg of subs.keys())shownGroups.push(`${g} › ${sg}`);}
    if(group&&head(g,g,keysOf(subs),0))continue;
    if(coverRow)grid.append(coverLine([...subs.values()].flatMap(m=>[...m.values()].flatMap(l=>l.units)).concat(openIn.get(g)||[])));
    for(const [sg,map] of [...subs.entries()].sort((a,b)=>byValue(a[0],b[0],sub?groupWay(sub):1))){
      if(sub&&head(sg,`${g} › ${sg}`,new Set(map.keys()),1))continue;
      lines(map,sub?`${g} › ${sg}`:g);
    }
  }
  // The auto-schedule live view's map of this layout (autoschedule_live.ts LiveGrid): the cells a person's duty is drawn
  // in; their first duty in a group adds their row (people rows only).
  (grid as LiveHost).live={cover:id=>checked?cover.get(id):undefined,more:moreKey,
    name:(id,cell,n,chip)=>{const s=sessions.get(id),label=cellLabel.get(cell)||'';if(!s)return label;
      if(n<2)return chipName(s,label+', ',false,chip);chipName(s,'',false,chip);return `${n} ${s.type_snapshot.name} sessions, ${label}`;},
    cells:(id,pid,make)=>{
    const s=sessions.get(id);if(!s||!byPerson)return [];
    const u={s,pid,day:dayOf(s)},cols=colsOf(u);
    return (group?values(group,u):['']).flatMap(g=>(sub?values(sub,u):['']).flatMap(sg=>{
      const gid=sub?`${g} › ${sg}`:g;
      // shortcut: a group, Then by, row or column of a person's value (rank, programme, tag) nobody shown has yet, and a
      // Then by of session values under a person's group, stay out until Apply redraws the grid; a session's values are
      // drawn from open sessions as columns, rows, and groups and Then by above people rows (`open`), and people rows are
      // added below.
      if(group&&!heads.has(gid)||collapsed.has(g)||sub&&collapsed.has(gid))return [];
      return (axes.rows==='person'?[pid]:values(axes.rows,u)).flatMap(r=>{
        let row=rowAt.get(gid+'\n'+r);
        if(!row&&make&&axes.rows==='person'){const name=hooks.name(people.get(pid));row=lineRow({key:pid,name,pid,units:[]},gid);row.dataset.livePreview='';placeRow(grid,gid,row,name);
          for(const h of [heads.get(g),sub?heads.get(gid):undefined])if(h&&!h.keys.has(pid)){h.keys.add(pid);h.count.textContent=plural(h.keys.size,rowsWord.one,rowsWord.many);}}
        return row?cols.flatMap(k=>{const x=row!.querySelector<HTMLElement>(`:scope>.pg-cell[data-col="${CSS.escape(k)}"]`);return x?[x]:[];}):[];});}));}};
  const body=tree.size
    ?el('div',{class:'week-scroller',tabindex:0,'aria-label':`${counts?'Counts':'Timeline'} · scrollable`},grid)
    :scheduleEmpty('period',{filtered:(hooks.unfiltered||data.shifts).some(s=>inRange.has(dayOf(s))),hooks,published:data.workspace.status==='published'});
  if(tree.size)roving(grid,body);
  const root=el('div',{class:'tl'+(counts?' ct':'')},...leaves?[leaves.status]:[],body,key.root);
  return root;
}
