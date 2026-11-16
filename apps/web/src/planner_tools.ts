import {MARKS,markTag,type Legend,phoneQuery,el,legend,icon,iconButton,withIcon,badge,popover,field,area,select,button,note,empty,sectionHead,table,formDialog,showDialog,confirmAction,notify,labelOf,formatDate,formatTime,toLocal,type Tone,hostLegend,PAGE_WHY} from './dom.js';
import {LEVEL_WORDS} from './issue_marker.js';
import {fullNameOf} from './person_label.js';
import {get,command,request} from './api.js';
import {calendarPeriod,filterShifts,touchesDay,isWeekend,absentFrom,type PlanRange} from './planner_logic.js';
import {previewFindings} from './draft_impact.js';
import {shiftForm,type Context} from './forms.js';
import type {Row,WorkspaceData} from './types.js';
import type {Command,PlannerViewDefinition} from './generated.js';
import {personPill} from './person_pill.js';
import {plannerPanels,weekGrid,dayAgenda,sessionItems,weekLayoutNow,weekLayoutSwitch,weekView,showPairs,teamLayout,drawnPeople,periodNav,periodTitle,dutyColour,coverOf,type WeekHooks,markWeekend} from './planner_week.js';
import {dataTable} from './table_view.js';
import {teamParts,teamGroups} from './planner_teams.js';
import {teamView,teamProblem,rankOn} from './team_view.js';
import {pillList,columnSizing} from './table_menu.js';
import {peopleTimeline} from './planner_people.js';
import {scoreChange} from './score.js';
import {compareDetail} from './fairness.js';
import {tag,dayTitle} from './planner_rules.js';
import {clashesOf} from './double_booking.js';
import {scheduleEmpty,scheduleEmptyWords} from './schedule.js';
import {issueGlyph,SEVERITY_WORDS} from './issue_marker.js';


function describe(ctx:Context,shift:Row|null){
  if(!shift)return 'No session';
  const zone=ctx.me.organisation.timezone;
  const people=shift.participants.map((p:Row)=>fullNameOf(ctx.catalogues.people.find(person=>person.id===p.person_id))).join(', ')||'Unstaffed';
  return `${shift.type_snapshot.name} · ${formatDate(shift.start,zone)} ${formatTime(shift.start,zone)} → ${formatDate(shift.end,zone)} ${formatTime(shift.end,zone)} · ${people} · ${shift.cancelled?'Closed · '+(shift.cancellation_reason||'Reason not recorded'):'Open'} · ${shift.locked?'Locked':'Unlocked'}`;
}

function impactTable(ctx:Context,changes:Row[]){
  return table(['Difference','Before','After','Changed fields'],changes.map(change=>[
    change.kind,describe(ctx,change.before),describe(ctx,change.after),change.fields.join(', ')||'Whole session']));
}

export function bulkImpactDialog(ctx:Context,data:WorkspaceData,ids:string[],action:string,after:()=>void){
  const unique=[...new Set(ids)];
  const title=action==='cancel'?'Close sessions':action==='release_unavailable'?'Prepare automatic cover':'Preview '+action.replaceAll('_',' ');
  const description=action==='release_unavailable'?'Remove only assignments affected by recorded absence or an external rotation. Sessions remain open; other fixed assignments stay protected. Then run auto-schedule to find eligible cover.':action==='cancel'?'Closed sessions stay visible with your reason and do not count as worked or filled duties. Do not include private leave details.':`Review the complete impact on ${unique.length} selected sessions before saving. This is a draft change, never publication.`;
  formDialog(title,description,form=>{
    const reason=area(action==='cancel'?'Closure reason':'Change reason','',3);reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    const result=el('div',{class:'span-2','aria-live':'polite'});let preview:Row|null=null,previewBody='';
    const payload=()=>({workspace_id:data.workspace.id,shift_ids:unique,action,reason:reason.input.value});
    const inspect=button('Calculate impact',async()=>{
      if(!reason.input.reportValidity())return;
      preview=null;result.replaceChildren(note('Checking the latest rota and rules. Nothing saved yet.'));
      const body=JSON.stringify(payload());
      try{
        const value=await request<Row>('/api/planner/preview',{method:'POST',body:JSON.stringify({kind:'shift.action',payload:JSON.parse(body),expected_revision:data.workspace.revision})});
        preview=value;previewBody=body;
        result.replaceChildren(note(`Not saved yet. ${value.impact.counts.added} added, ${value.impact.counts.removed} removed, ${value.impact.counts.changed} changed. ${value.validation.error_count} mandatory findings remain.`,value.validation.valid?'info':'warning'),impactTable(ctx,value.impact.changes));
        if(value.validation.findings.length)result.append(previewFindings(ctx,value.validation.findings,unique));
      }catch(error){result.replaceChildren(note((error as Error).message,'error',error));throw error;}
    },'secondary');
    reason.input.addEventListener('input',()=>{preview=null;result.replaceChildren(note('The reason changed. Calculate the impact again before saving.','warning'));});
    form.append(reason.wrap,el('div',{class:'span-2'},inspect),result,note('You can save a draft with issues, but must clear the Fix list’s Must fix before review. Changes to the rota or rules require a new preview.','warning'));
    return async()=>{
      if(!preview||previewBody!==JSON.stringify(payload()))throw new Error('Calculate the current impact before applying this bulk change.');
      await command('shift.action',payload(),undefined,data.workspace.revision,preview.source_fingerprint);
      notify('Draft changes saved and checked. Nothing published.');after();await ctx.refresh();
    };
  },'Apply previewed draft change');
}

/** Two rotas side by side: scores, score parts and the sessions that differ. Neither rota changes. The Schedule's
 *  Side by side mode draws it in place; `other` is the rota compared with (picked here too). */
export function comparePanel(ctx:Context,data:WorkspaceData,body:HTMLElement,other='',picked?:(id:string)=>void){
  {
    let workspaceOffset=0,differenceOffset=0;
    const directory=el('div',{}),result=el('div',{});body.append(directory,result);
    const choose=async()=>{
      const page=await get<{workspaces:Row[];total:number;has_more:boolean}>(`/api/workspaces?limit=50&offset=${workspaceOffset}`);
      const others=page.workspaces.filter(w=>w.id!==data.workspace.id);
      if(!others.length&&!workspaceOffset){directory.replaceChildren(empty('No other rota yet','Create or publish another rota to compare with this one.',null,'layers'));return;}
      // Opens on the rota before this one (or another rota of the same period), the usual comparison.
      const before=others.find(w=>w.id===other)||others.filter(w=>w.starts_on<=data.workspace.starts_on).sort((a,b)=>String(b.starts_on).localeCompare(String(a.starts_on)))[0]||others[0];
      const picker=select('Compare with',others.map(w=>({value:w.id,label:`${w.name} · ${w.status}`})),before?.id||'');
      const compare=async(reset=false)=>{
        if(reset)differenceOffset=0;if(!picker.input.value)return;
        const value=await get<Row>(`/api/workspaces/${data.workspace.id}/compare?other_id=${picker.input.value}&limit=50&offset=${differenceOffset}`);
        const paged=value.has_more||differenceOffset>0;
        result.replaceChildren(sectionHead(`${value.before.name} → ${value.after.name}`,`${value.counts.added} added · ${value.counts.removed} removed · ${value.counts.changed} changed · ${value.unchanged} unchanged`),
          scoreChange(value.scores?.before,value.scores?.after)||el('p',{class:'muted'},'Check both rotas to compare their schedule scores.'),
          compareDetail(value.scores?.detail,value.before.name,value.after.name)||'',
          value.scores?.before&&value.scores?.after&&!(value.scores.before.fresh&&value.scores.after.fresh)?el('p',{class:'muted'},'A score is from an older check; check the rota again for a current score.'):'');
        if(!value.scope_matches)result.append(note('These rotas cover different dates. Keep this in mind when comparing sessions.','warning'));
        if(value.ambiguous_pairing)result.append(note('Some duplicate sessions could not be matched reliably. They appear as additions and removals.','warning'));
        result.append(el('h3',{},'Sessions'),value.changes.length?impactTable(ctx,value.changes):empty('No session differences','Rota settings and approval status are not compared here.'),
          // Paging only when there is more than one page.
          paged?el('div',{class:'actions'},el('span',{},`${value.total} differences`),
            button('Previous',()=>{differenceOffset=Math.max(0,differenceOffset-50);return compare();},'small',{disabled:differenceOffset===0,why:PAGE_WHY.first}),
            button('Next',()=>{differenceOffset+=50;return compare();},'small',{disabled:!value.has_more,why:PAGE_WHY.last})):'');
      };
      // Choosing a rota compares straight away; the first one is compared on opening.
      picker.input.addEventListener('change',()=>{picked?.(picker.input.value);void compare(true).catch(error=>result.replaceChildren(note((error as Error).message,'error',error)));});
      directory.replaceChildren(el('div',{class:'toolbar'},picker.wrap),
        page.has_more||workspaceOffset>0?el('div',{class:'actions'},el('span',{},`${page.total} rotas · page ${workspaceOffset/50+1}`),
          button('Previous rotas',()=>{workspaceOffset=Math.max(0,workspaceOffset-50);return choose();},'small',{disabled:workspaceOffset===0,why:PAGE_WHY.first}),
          button('Next rotas',()=>{workspaceOffset+=50;return choose();},'small',{disabled:!page.has_more,why:PAGE_WHY.last})):'');
      await compare(true);
    };
    void choose().catch(error=>directory.replaceChildren(note((error as Error).message,'error',error)));
  }
}

/** One view engine: a layout (calendar grid, people, coverage or list) over one range of days. Month and week are the same grid. */
export function scheduleView(ctx:Context,target:HTMLElement,data:WorkspaceData,filters:PlannerViewDefinition,layout:string,range:PlanRange,page:number,onPage:(page:number)=>void,week:WeekHooks,fixed?:ReturnType<typeof calendarPeriod>,
  /** Filters applied before (the Schedule's): People then lists only the people they keep. */filtered=false){
  // The Schedule passes its own period (any days, not bounded by one rota).
  const zone=ctx.me.organisation.timezone,period=fixed||calendarPeriod(data.workspace,range,page,ctx.me.organisation.week_start);
  const v=data.workspace.validation;
  const gaps=new Set((v?.coverage||[]).filter((r:Row)=>r.status==='gap').map((r:Row)=>r.shift_id)),errs=new Set((v?.findings||[]).filter((f:Row)=>f.severity==='error'&&f.shift_id).map((f:Row)=>f.shift_id));
  const visible=filterShifts(data.shifts,ctx.catalogues,filters).filter(s=>(range==='rota'||period.days.some(d=>touchesDay(s,d,zone)))&&(!weekView.onlyOpen||gaps.has(s.id))&&(!weekView.onlyIssues||errs.has(s.id)));
  // Every planner layout registers the same panel owner; published renderers never enter this path.
  const narrowed=!!(filtered||filters.search||filters.location_id||filters.assignment_type_id||filters.person_id||weekView.onlyOpen||weekView.onlyIssues);
  const withPanels=(body:HTMLElement)=>{
    const nav=body.querySelector<HTMLElement>('.wk-nav')||target.querySelector<HTMLElement>('.wk-nav');
    if(!week||!nav)return body;
    const panelLayout=el('div',{class:'wk-layout'},el('div',{class:'wk-content'},body));
    return plannerPanels(ctx,data,period,week,nav,panelLayout).root;
  };
  if(layout==='coverage'||layout==='week'){
    // Phones read the calendar day by day (the published rota's list), with the planner's session sheet and open places;
    // the same List/Grid switch as the published rota keeps the grid one tap away.
    const phone=layout==='week'&&phoneQuery.matches;
    if(phone)target.append(weekLayoutSwitch(()=>onPage(page)));
    target.append(layout==='coverage'?withPanels(coverageMatrix(ctx,data,visible,period,onPage,week.fill,week))
      :phone&&weekLayoutNow()==='agenda'?dayAgenda(ctx,visible,period,onPage,week!,{data,add:week?.editable?day=>shiftForm(ctx,data,undefined,{day}):undefined})
      :weekGrid(ctx,data,visible,period,onPage,week!));
    return;
  }
  if(layout==='person'){
    target.append(periodNav(periodTitle(period,zone),period.unit,period,onPage,`${zone} · ${visible.length} matching sessions`));
    target.append(withPanels(peopleTimeline(ctx,data,visible,{name:week.name,code:week.code,details:week.details,view:week.view,menu:week.menu,onShowAll:week.onShowAll},narrowed,period.days)));
    return;
  }
  target.append(periodNav(periodTitle(period,zone),period.unit,period,onPage,`${zone} · ${visible.length} matching sessions`));
    // List: every session as one table row (the shared table), selectable into the planner's session bar.
    // A double booking is a state of its own (the same clashesOf the grid's mark reads), never "Covered" (S-07).
    // Someone absent on it (the grid's absent chip, absentFrom) is never "Covered" either: a printed or CSV copy must not
    // show an absent doctor covering a clinic (SW3-C-02).
    const findingsOf=new Map<string,Row[]>();for(const f of (v?.findings||[]) as Row[])if(f.shift_id)findingsOf.set(f.shift_id,[...findingsOf.get(f.shift_id)||[],f]);
    const absentOf=(s:Row)=>absentFrom(s,findingsOf.get(s.id)||[],ctx.me.person_id);
    const state=(s:Row)=>s.cancelled?'Closed':clashesOf(s).length?'Double-booked':absentOf(s).size?'Absent · needs cover':!v?'Not checked':gaps.has(s.id)?'Open places':errs.has(s.id)?LEVEL_WORDS.must:'Covered';
    const tone:Record<string,Tone|''>={Closed:'',['Not checked']:'',['Open places']:'warn',['Absent · needs cover']:'warn',[LEVEL_WORDS.must]:'err',['Double-booked']:'err',Covered:'ok'};
    // People, places and clinics by id, once per draw: every row's columns (text, facet, sort, cell) name them.
    const byId=(list:Row[]|undefined)=>new Map((list||[]).map(r=>[r.id,r]));
    const placesById=byId(ctx.catalogues.locations),servicesById=byId(ctx.catalogues.services),peopleById=byId(ctx.catalogues.people);
    const place=(s:Row)=>labelOf(placesById.get(s.location_id));
    const services=(s:Row)=>(s.service_ids as string[]).map(id=>labelOf(servicesById.get(id)));
    const nameOf=(id:string)=>week?.name(peopleById.get(id))||'';
    // One pill per supervision team (planner_teams.ts): the supervisor once, their trainees with them; the shared TL mark.
    // The List's key: each duty's colour and the TL mark, as in every other layout (S-15), stamped on the marks drawn.
    // The table draws, pages, groups and appends rows itself: the key follows what it draws now (dom.ts legend `follow`).
    const key=legend('Key'),mark=key.mark,keyed:Legend=key;
    const hue=<T extends Element>(n:T,s:Row)=>mark(n,'c:t:'+s.assignment_type_id,'duty-'+dutyColour(s),s.type_snapshot.name,0);
    const led=<T extends Element>(n:T,p:Row)=>p.team_lead?mark(n,'tag:lead',markTag('lead'),MARKS.lead[1]):n;
    const tl=(p:Row,s?:Row)=>(p.team_lead?' '+MARKS.lead[0]:'')+(s&&absentOf(s).has(p.person_id)?' (absent)':'');
    // A person display (Search › Show) leaves others out: they are counted, never dropped silently.
    const who=(s:Row)=>{const d=drawnPeople(s);return [...(showPairs()?teamParts(d.shown,nameOf,p=>tl(p,s),s.participants as Row[]):d.shown.map(p=>nameOf(p.person_id)+tl(p,s))),...(d.hidden.length?[`+${d.hidden.length} others`]:[])];};
    // The People cell: one short pill per person, a supervision team as one one-line team box (team_view.ts), the first
    // few then "+N" (the numbered list); the relationship in words stays in the box's name and the searchable text.
    // Not one pill of team prose, which made the column thousands of pixels wide.
    const fullOf=(id:string)=>fullNameOf(peopleById.get(id));
    // People per pill: a team box holds its team (pillList's "+N" counts people).
    const pillCounts=(s:Row)=>{const d=drawnPeople(s);return showPairs()?teamGroups(d.shown,s.participants as Row[]).map(t=>t.length):d.shown.map(()=>1);};
    const pills=(s:Row)=>{const d=drawnPeople(s),day=toLocal(s.start,zone).slice(0,10),errs=(pid:string)=>(findingsOf.get(s.id)||[]).filter(f=>f.person_id===pid&&f.severity==='error');
      // The one person pill (person_pill.ts, D78), in the session's tint.
      const pill=(p:Row)=>led(hue(personPill({label:nameOf(p.person_id)+tl(p,s),full:fullOf(p.person_id),hue:dutyColour(s)}),s),p);
      // A lone person keeps their ordinary pill; only a team (or a pairing problem) is a one-line group.
      return teamGroups(d.shown,s.participants as Row[]).flatMap(t=>{const problem=showPairs()?teamProblem(t,s.participants as Row[],errs):null;
        return !showPairs()||t.length===1&&!problem?t.map(e=>pill(e.p)):[hue(teamView(t,{density:'row',key:keyed,nameOf:id=>nameOf(id)||'Named supervisor',fullName:id=>fullOf(id)||'Named supervisor',
        rankOf:id=>rankOn(ctx.catalogues,id,day),problem,layout:teamLayout(),hue:dutyColour(s),
        member:e=>led(personPill({label:nameOf(e.p.person_id)+tl(e.p,s),full:fullOf(e.p.person_id)}),e.p)}),s)];});};
    // The planner's toolbar owns search, filters and saved views for every layout; this table offers Group and Sort.
    const list=dataTable<Row>({id:'planner-list',noun:['session','sessions'],rows:visible,rowId:s=>s.id,toolbar:false,pageSize:weekView.forPrint?Math.max(visible.length,1):undefined,print:weekView.forPrint,
      // The same words as every Schedule view (schedule.ts scheduleEmpty, S-01); the table's card has no button.
      empty:(e=>[e.title,e.kind==='create'?`Use ${week!.create} to add one.`:e.line] as [string,string])(scheduleEmptyWords(period.unit,{filtered:narrowed,hooks:week,published:data.workspace.status==='published'})),defaults:{sort:['when','asc']},
      selection:week?.editable?{set:week.selected,changed:week.onSelect}:undefined,
      columns:[
        {key:'when',label:'When',sort:s=>String(s.start),facet:s=>formatDate(s.start,zone),text:s=>formatDate(s.start,zone),cell:s=>el('div',{class:'swap-duty'},el('strong',{},formatDate(s.start,zone)),el('small',{class:'muted mono'},`${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`))},
        {key:'duty',label:'Duty',text:s=>s.type_snapshot.name,facet:s=>s.type_snapshot.name,cell:s=>el('span',{class:'lane-name'},hue(el('span',{class:`lane-dot duty-${dutyColour(s)}`,'aria-hidden':'true'}),s),s.type_snapshot.name)},
        {key:'place',label:'Place',text:place,facet:place,cell:place},
        {key:'service',label:'Service',text:s=>services(s).join(' '),facet:services,cell:s=>services(s).join(', ')},
        {key:'people',label:'People',text:s=>who(s).join(' '),sort:s=>s.participants.length,cell:s=>s.participants.length?pillList(pills(s),{label:['person','people'],shown:3,rest:{count:drawnPeople(s).hidden.length,open:()=>week?.details(s)},counts:pillCounts(s)}):el('span',{class:'muted'},'No one yet')},
        {key:'status',label:'Status',text:state,facet:state,cell:s=>s.locked?el('span',{class:'tag-row'},badge(state(s),tone[state(s)]),badge('Locked')):badge(state(s),tone[state(s)])},
      ],
      onOpen:s=>week?.details(s),
      // Each row is a session for the rule highlight; exception markers sit in its first cell.
      decorate:(s,n)=>{n.dataset.session=s.id;n.dataset.person=(s.participants as Row[]).map(p=>p.person_id).join(' ');n.querySelector('td:not(.dt-check)')?.classList.add('rk-mark-slot');tag(n,{kind:'session',shift:s.id,label:s.type_snapshot.name},{own:true});},
      rowActions:s=>[{label:'Open session',icon:'rules',run:()=>week?.details(s)},...(gaps.has(s.id)&&week?.fill?[{label:'Fill open place',icon:'people' as const,run:()=>week.fill!(s)}]:[])]});
    key.follow([hostLegend(list.root,key)]);  // and the rule map's exception marks in its rows are this key's
    week?.onTable?.({group:list.parts.group,sort:list.parts.sort,view:list.parts.view});
    target.append(withPanels(el('section',{class:'panel'},list.root,key.root)));
}
const dutyClass=dutyColour;
/** Week view: one row (lane) per duty type, one column per day. Each day header shows sessions and open places. */
/** Coverage view: duty types × days. Each cell shows places filled / places needed; short cells lead straight to filling. */
function coverageMatrix(ctx:Context,data:WorkspaceData,visible:Row[],period:ReturnType<typeof calendarPeriod>,onPage:(page:number)=>void,fill:((shift:Row)=>void)|undefined,hooks:WeekHooks){
  const zone=ctx.me.organisation.timezone;
  // One count of places for the Week grid and this view (planner_week coverOf: the check's places_*, R-2).
  const cover=coverOf(data),gapIds=new Set([...cover].filter(([,c])=>c.gap).map(([id])=>id));
  const checked=!!data.workspace.validation,places=new Map([...cover].map(([id,c])=>[id,{need:c.required,have:c.available}]));
  // The same issues the Week grid marks: a blocking rule finding or a double booking is never "Fully staffed" (S-35).
  const broken=new Set([...(data.workspace.validation?.findings||[]).filter((f:Row)=>f.severity==='error'&&f.shift_id).map((f:Row)=>f.shift_id),
    ...data.shifts.filter(s=>clashesOf(s).length).map(s=>s.id)]);
  const count=(items:Row[])=>items.reduce((a,s)=>{const c=places.get(s.id);return {need:a.need+(c?.need||0),have:a.have+(c?Math.min(c.need,c.have===Infinity?0:c.have):0)};},{need:0,have:0});
  // Same rows as the week grid: duty · time · place, so a short cell points at one kind of session.
  const types=new Map<string,{name:string;sub:string;colour:string;shifts:Row[]}>();
  const hhmm=(s:Row)=>`${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`;
  for(const s of visible.filter(s=>!s.cancelled)){const k=`${s.assignment_type_id}|${hhmm(s)}|${s.location_id}`;if(!types.has(k))types.set(k,{name:s.type_snapshot.name,sub:`${hhmm(s)} · ${labelOf(ctx.catalogues.locations.find(l=>l.id===s.location_id))}`,colour:dutyClass(s),shifts:[]});types.get(k)!.shifts.push(s);}
  const rows=[...types.values()].sort((a,b)=>a.name.localeCompare(b.name)||a.sub.localeCompare(b.sub));
  const weekShifts=rows.flatMap(r=>r.shifts.filter(s=>period.days.includes(toLocal(s.start,zone).slice(0,10))));
  const totalCell=(items:Row[])=>{const t=count(items),gaps=items.filter(s=>gapIds.has(s.id)).length;
    return el('td',{class:'cov-total'},!items.length?'—':checked&&t.need?el('span',{class:t.have<t.need?'short':'ok'},`${t.have}/${t.need}`):`${items.length-gaps}/${items.length}`);};
  const dayLabel=(day:string)=>new Intl.DateTimeFormat('en-GB',{weekday:'short',day:'numeric',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'));
  const strip=periodNav(periodTitle(period,zone),period.unit,period,onPage,
    checked?'People assigned against people required, per duty and day. Select a short cell to see who can fill it.':'Sessions per duty and day. Run a rota check to see places filled against places needed.');
  const table=el('table',{class:'coverage-table'},el('caption',{class:'sr-only'},'Coverage by duty type and day'),
    el('thead',{},el('tr',{},el('th',{scope:'col'},'Duty'),...period.days.map(d=>tag(el('th',{scope:'col',class:isWeekend(d)?'weekend':''},dayLabel(d)),{kind:'day',label:dayTitle(d),day:d,days:[d],shifts:weekShifts.filter(s=>toLocal(s.start,zone).slice(0,10)===d).map(s=>s.id)},{pin:true})),el('th',{scope:'col',class:'cov-total'},'Total'))));
  const body=el('tbody',{}),key=legend('Key');
  hostLegend(table,key);  // the rule map's exception marks on its day and duty heads are this key's entries
  for(const th of table.querySelectorAll('thead th.weekend'))markWeekend(key,th);  // a weekend column's shading is its entry
  rows.forEach((row,i)=>{
    // The duty's name once, on its first row; the rows after it read only their time and place (still named to a reader).
    const again=i>0&&rows[i-1].name===row.name;
    const tr=el('tr',{class:again?'cov-again':''},tag(el('th',{scope:'row',title:`${row.name} · ${row.sub}`},el('span',{class:again?'cov-name sr-only':'cov-name'},key.mark(el('span',{class:`lane-dot duty-${row.colour}`,'aria-hidden':'true'}),'t:'+row.shifts[0].assignment_type_id,'duty-'+row.colour,row.name,0),el('span',{class:'truncate'},row.name)),el('small',{class:'cov-sub truncate'},row.sub)),{kind:'row',type:row.shifts[0].assignment_type_id,location:row.shifts[0].location_id,label:row.name,sub:row.sub,shifts:row.shifts.map(s=>s.id)},{pin:true}));
    for(const day of period.days){
      const items=row.shifts.filter(s=>toLocal(s.start,zone).slice(0,10)===day),gaps=items.filter(s=>gapIds.has(s.id)).length,t=count(items);
      // Every place filled but still short: a missing team lead, skill or supervisor, never counted as an open place (TL-02).
      const roleOnly=checked&&gaps>0&&t.need>0&&t.have>=t.need;
      const text=checked&&t.need?`${t.have}/${t.need}${roleOnly?' !':''}`:`${items.length-gaps}/${items.length}`,what=(checked&&t.need?`${t.have} of ${t.need} places filled`:`${items.length-gaps} of ${items.length} sessions staffed`)+(roleOnly?' · a team lead, skill or supervisor is missing':'');
      const td=(n=>isWeekend(day)?markWeekend(key,n):n)(el('td',{class:isWeekend(day)?'weekend':''})),issue=items.some(s=>broken.has(s.id));
      // The cell is the drawn mark of its state and its issue (dom.ts legend `mark`).
      // A must fix in the cell (a broken rule or a double booking): the shared issue mark (D78) and its key entry.
      const state=(n:HTMLElement)=>{if(issue)key.mark(n,'issue:error',issueGlyph('error'),SEVERITY_WORDS.error);
        return key.mark(n,!items.length?'none':roleOnly?'short-role':gaps?'short':'full',el('span',{class:'cov-key '+(!items.length?'none':gaps?'short':'full'),'aria-hidden':'true'}),!items.length?'No session':roleOnly?'Short: needs a team lead, skill or supervisor':gaps?'Short: needs people':checked?'Fully staffed':'Sessions (not checked yet)');};
      if(!items.length){td.append(state(el('span',{class:'cov-cell none','aria-label':`${row.name}, ${formatDate(day,zone)}: no sessions`},'—')));}
      else{
        const cell=state(tag(el('button',{type:'button',class:'cov-cell '+(gaps?'short':'full')+(issue?' issue':''),'data-session':items.map(s=>s.id).join(' '),'data-person':items.flatMap(s=>(s.participants as Row[]).map(p=>p.person_id)).join(' '),'aria-label':`${row.name}, ${formatDate(day,zone)}: ${what}${issue?', must fix':''}${gaps&&fill?'. Select to fill.':''}`,'data-tip':what},text,issue?issueGlyph('error'):''),{kind:'row',type:items[0].assignment_type_id,location:items[0].location_id,label:row.name,shifts:items.map(s=>s.id)},{own:true}));
        const open=items.filter(s=>gapIds.has(s.id));
        cell.addEventListener('click',()=>{if(open.length===1&&fill){fill(open[0]);return;}popover(cell,(panel,close)=>{panel.classList.add('cov-popover');panel.append(el('div',{class:'popover-head'},el('strong',{},row.name),el('small',{},formatDate(day,zone)+' · '+what)),
          ...(fill&&open.length?[el('div',{class:'cov-fill'},...open.map(s=>button(`Fill ${formatTime(s.start,zone)}–${formatTime(s.end,zone)} · ${labelOf(ctx.catalogues.locations.find(l=>l.id===s.location_id))}`,()=>{close();fill(s);},'primary small')))]:[]),
          // The cell's sessions, each the one session item (sessionItems), with its own key; one opens the session panel.
          ...(()=>{const k=legend('Key'),item=sessionItems(ctx,hooks,{data,days:[day],key:k});
            return [el('ul',{class:'ag-list cov-list'},...items.map(s=>{const n=item(s,day);n.addEventListener('click',close);return el('li',{},n);})),k.root];})());},{label:`${row.name} sessions on ${formatDate(day,zone)}`});});
        td.append(cell);
      }
      tr.append(td);
    }
    tr.append(totalCell(row.shifts.filter(s=>period.days.includes(toLocal(s.start,zone).slice(0,10)))));
    body.append(tr);
  });
  // Totals per day and for the week.
  if(rows.length>1)body.append(el('tr',{class:'cov-sum'},el('th',{scope:'row'},'All duties'),...period.days.map(day=>totalCell(weekShifts.filter(s=>toLocal(s.start,zone).slice(0,10)===day))),totalCell(weekShifts)));
  table.append(body);
  // A fixed day matrix: nothing to sort, filter or hide, so no column menus at all (not built and then removed; X-18);
  // its columns resize like every table's, never reordered.
  columnSizing(table,{reorder:false});
  const filtered=(hooks?.unfiltered||data.shifts).some(s=>!s.cancelled&&period.days.includes(toLocal(s.start,zone).slice(0,10)));
  return el('div',{class:'cov'},strip,rows.length?el('div',{class:'table-scroll'},table):scheduleEmpty(period.unit,{filtered,hooks,published:data.workspace.status==='published'}),key.root);
}
