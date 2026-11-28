import {type Legend,tagChip,rankColour,colourOf,legend,MARKS,el,icon,popover,hoverCard,formatDate,formatTime,toLocal,labelOf,empty,button,CAPABILITY,hostLegend,readFailed} from './dom.js';
import {SEVERITY_WORDS,LEVEL_WORDS} from './issue_marker.js';
import type {LiveHost} from './autoschedule_live.js';
import {fullNameOf} from './person_label.js';
import {get,onSessionReset,sessionChanged} from './api.js';
import {addDays,isWeekend,LAYER_DEFAULTS} from './planner_logic.js';
import {dutyColour,attachMenu,weekView,coverOf,openPlaces,openAssignment,scheduleWidths,colourRule,type WeekHooks,markOpen,markWeekend,markToday,roving} from './planner_week.js';
import {propertiesOf,sessionCode,itemParts,itemNodes,itemWords,itemHead,itemSaysWords,ITEM_DRAWS} from './schedule_item.js';
import {teamOf} from './planner_teams.js';
import {wishWords} from './wishes.js';
import type {Context} from './forms.js';
import type {Row,WorkspaceData} from './types.js';
import {tag,ruleSummary,dayTitle,addsToSelection,selectSession} from './planner_rules.js';
import {leaveTypeName,causeText} from './findings.js';
import {issueMarker,issueLabel,issueList,findingIssue,openMarkerFrom} from './issue_marker.js';

type Group='rank'|'tag'|'team'|'none';type Sort='hours-desc'|'hours-asc'|'name'|'name-desc';
// Viewer choices for this page visit (not saved). The planner's collection toolbar sets find, group and sort.
const state={find:'',group:'rank' as Group,sort:'hours-desc' as Sort,collapsed:new Set<string>(),/** Schedule's Leave & wishes layer. */leave:LAYER_DEFAULTS.leave};
export const peopleView=state;
export const PEOPLE_GROUPS=[{key:'rank',label:'Rank'},{key:'tag',label:'Tag'},{key:'team',label:'Supervision team'}];
export const PEOPLE_SORTS=[{key:'hours',label:'Hours'},{key:'name',label:'Name'}];
/** Whether the People search finds a person (`q` trimmed, lower case): their name, code or one of their tags. Print or
 *  export reads the same when it lists a People view's sessions as data. */
export const findsPerson=(p:Row,q:string,code:(p:Row)=>string)=>fullNameOf(p).toLowerCase().includes(q)||code(p).toLowerCase().includes(q)||((p.tags||[]) as string[]).some(t=>t.toLowerCase().includes(q));
let redrawPeople:(()=>void)|null=null;
/** Redraw the open People view after its find, group or sort changed (no reload). */
export function refreshPeople(){redrawPeople?.();}
let leaveCache:{key:string;rows:Row[]}|null=null;
onSessionReset(()=>{leaveCache=null;});

const HOURS=new Set(['reserved_hours','scheduled_hours']);
// Wishes to work (on duty, a wanted duty) and wishes for time off (a day off, a duty to avoid) are marked apart.
const WISH_TONE=(kind:string)=>['prefer_on','prefer_shift_type'].includes(kind)?'on':'off';
/** Leave and wishes on a person × day grid (D80): People × Days (its Layers switch) and the codes/counts grid (the view's
 *  Leave and wishes property). Leave comes from recorded availability, never a private reason, read once per availability
 *  revision; a cell drawn before it arrives is marked when it does (even while the page is still built off-screen; a page
 *  that was left had this read aborted, so nothing stale is marked). Wishes are the rota's own. `wish` before a cell's
 *  chips, `leave` after them (it adds the leave to their names). Auditors and programme leads may not read availability. */
export function leaveLayer(ctx:Context,data:WorkspaceData,key:Legend,zone:string,on:boolean){
  const mayRead=on&&ctx.permits(CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER);
  const leaveKey=`${ctx.me.identity_id}:${data.workspace.id}:${data.current_availability_revision}`;
  let leaveOf=new Map<string,Row[]>();
  const setLeave=(rows:Row[])=>{leaveOf=new Map();for(const a of rows)leaveOf.set(a.person_id,[...(leaveOf.get(a.person_id)||[]),a]);};
  setLeave(mayRead&&leaveCache?.key===leaveKey?leaveCache.rows:[]);
  const cells=new Map<string,HTMLElement>();
  // The day's leave: a blocking entry first; leave whose type does not block duties is shown apart, never as blocking.
  const awayOn=(away:Row[],d:string)=>{const on=away.filter(a=>!a.retracted&&toLocal(a.start,zone).slice(0,10)<=d&&d<=toLocal(new Date(Date.parse(a.end)-1).toISOString(),zone).slice(0,10));
    return on.find(a=>a.counts_as_unavailable!==false)||on[0];};
  // Its type shows as far as the department's leave setting allows (findings.ts leaveTypeName).
  const markLeave=(cell:HTMLElement,a:Row)=>{
    const block=a.counts_as_unavailable!==false,name=leaveTypeName(a);
    const words=block?'On leave or away'+(name?' · '+name:''):`${name||'Leave'} · does not block duties`;
    cell.classList.add(block?'leave':'leave-soft');cell.setAttribute('data-tip',words);
    // The cell is the drawn mark (its pattern), stamped as its key entry (dom.ts legend `mark`).
    if(block)key.mark(cell,'leave',el('span',{class:'pg-leave-key','aria-hidden':'true'}),'Leave or away');else key.mark(cell,'leave-soft',el('span',{class:'pg-leave-key soft','aria-hidden':'true'}),'Leave that does not block duties');
    const chips=cell.querySelectorAll('.pg-chip');chips.forEach(c=>c.setAttribute('aria-label',c.getAttribute('aria-label')+' · '+(block?'on leave'+(name?' ('+name+')':''):words)));
    if(!chips.length)cell.prepend(el('span',{class:'sr-only'},words));};
  // A failed read says so beside the grid with Retry (unmarked cells would read as available); it is never cached. A
  // session change or a page left stays silent.
  const status=el('div',{class:'pg-leave-status'});
  const read=():Promise<void>=>{status.replaceChildren();return get<{availability:Row[]}>('/api/availability').then(r=>{leaveCache={key:leaveKey,rows:r.availability};setLeave(r.availability);
    for(const [id,cell] of cells){const [pid,d]=id.split('|'),away=leaveOf.get(pid),a=away&&awayOn(away,d);if(a)markLeave(cell,a);}},
    e=>{if(!sessionChanged(e))status.replaceChildren(readFailed('leave',e,()=>void read(),'leave','People may show as available.'));});};
  if(mayRead&&leaveCache?.key!==leaveKey)void read();
  const wishesOf=new Map<string,Row[]>();for(const x of on?data.wishes||[]:[])wishesOf.set(x.person_id,[...(wishesOf.get(x.person_id)||[]),x]);
  return {
    /** Where a failed leave read is said: placed beside the grid by the view. */
    status,
    clear:()=>cells.clear(),
    /** A staff wish on this day: a small corner mark (kept by auto-schedule when it can; hover for what was asked). */
    wish(cell:HTMLElement,pid:string,d:string){
      const wished=(wishesOf.get(pid)||[]).filter(x=>x.first_day<=d&&d<=x.last_day);if(!wished.length)return;
      const text='Wish: '+wished.map(x=>wishWords(x,ctx.catalogues.assignment_types||[])).join('; ');
      const mark=el('span',{class:'pg-wish '+wished[0].kind,role:'img','aria-label':text,'data-tip':text});
      // One corner mark stands for every wish of the day: stamped as each tone's entry.
      for(const x of wished)key.mark(mark,'wish:'+WISH_TONE(x.kind),el('span',{class:'pg-wish-key '+x.kind,'aria-hidden':'true'}),WISH_TONE(x.kind)==='on'?'Wish to work':'Wish for time off',1);
      cell.append(mark);},
    /** The person's leave on this day, now and when leave arrives (`id`: the cell's key, person and day first). */
    leave(cell:HTMLElement,pid:string,d:string,id=pid+'|'+d){if(!on)return;cells.set(id,cell);const away=leaveOf.get(pid),a=away&&awayOn(away,d);if(a)markLeave(cell,a);},
  };
}

/** People view: one row per person across the rota, grouped by rank, with leave, rule issues and hours against target. */
export function peopleTimeline(ctx:Context,data:WorkspaceData,visible:Row[],hooks:Pick<WeekHooks,'name'|'code'|'details'|'view'|'menu'|'onShowAll'>,filtered:boolean,range?:string[]){
  const zone=ctx.me.organisation.timezone,w=data.workspace;
  const root=el('div',{class:'ppl'});
  // The view's range (one page of days); without one, the whole rota up to 62 days.
  const days:string[]=range?[...range]:[];if(!range)for(let d=String(w.starts_on).slice(0,10);d<String(w.ends_on).slice(0,10)&&days.length<62;d=addDays(d,1))days.push(d);
  // Each duty says what the view's properties say (the one item renderer); a grid cell starts from the code alone.
  const props=propertiesOf(weekView.props,'chip');
  const today=toLocal(new Date().toISOString(),zone).slice(0,10);
  const byPerson=new Map<string,{shift:Row;p:Row}[]>();
  for(const s of visible)for(const p of s.participants){if(!byPerson.has(p.person_id))byPerson.set(p.person_id,[]);byPerson.get(p.person_id)!.push({shift:s,p});}
  // A person's must-fix findings on a session, as the one issue item (issue_marker.ts; UIUX-AUDIT L5: no "!" chip of its own).
  const issues=new Map<string,Row[]>();for(const f of (w.validation?.findings||[]) as unknown as Row[])if(f.person_id&&f.shift_id&&f.severity==='error'){const k=f.person_id+':'+f.shift_id;issues.set(k,[...(issues.get(k)||[]),f]);}
  // A breach of the person's own rule (a quota) with its cause: said on their row in words, the cause on hover and in
  // the rules panel the row opens (findings.ts causeText).
  const breaches=new Map<string,string[]>();
  for(const f of (w.validation?.findings||[]) as unknown as Row[])if(f.person_id&&!f.shift_id&&f.cause){const t=causeText(f);breaches.set(f.person_id,[...new Set([...(breaches.get(f.person_id)||[]),t])]);}
  const breachMark=(list:string[])=>key.mark(el('span',{class:'pg-breach',role:'img','aria-label':'Rule breach: '+list.join('; '),'data-tip':list.join(' · ')},icon('alert'),'Breach'),
    'breach',el('span',{class:'pg-breach lg-mark','aria-hidden':'true'},icon('alert'),'Breach'),'Breach of their own rule (hover for the cause)');
  const targets=new Map<string,{amount:number;comparison:string}>();
  for(const r of w.validation?.quota_report?.rows||[])if(HOURS.has(r.metric)&&!targets.has(r.person_id))targets.set(r.person_id,{amount:r.amount,comparison:r.comparison});
  const current=(r:Row)=>(!r.starts_on||r.starts_on<=days.at(-1)!)&&(!r.ends_on||r.ends_on>days[0]);
  const roleRow=(pid:string)=>{const m=(ctx.catalogues.memberships||[]).find(r=>r.person_id===pid&&current(r));return m?(ctx.catalogues.categories||[]).find(c=>c.id===m.category_id):undefined;};
  const rankOf=(pid:string)=>roleRow(pid)?labelOf(roleRow(pid)):'No rank';
  // A rank group's band is its rank's one tint (C21), the same key entry as Colour by Rank (c:r:, colourRule); other groups are neutral.
  const groupRank=new Map<string,string>();
  // `only`: the sessions of one supervision team (Group by Supervision team counts each team's own duties).
  const hoursOf=(pid:string,only?:Set<string>)=>(byPerson.get(pid)||[]).filter(x=>!x.shift.cancelled&&(!only||only.has(x.shift.id))).reduce((a,x)=>a+(Date.parse(x.p.end)-Date.parse(x.p.start))/3600000,0);
  const maxHours=Math.max(1,...ctx.catalogues.people.map(p=>hoursOf(p.id)));
  // The key lists the duty codes and marks the drawn rows show (collapsed groups and search change it).
  const key=legend('Duty key'),colours=colourRule(ctx,days,key);
  const gridWrap=el('div',{class:'week-scroller',tabindex:0,'aria-label':'People timeline · scrollable'});
  root.append(gridWrap,key.root);

  // Leave and wishes (the Layers switch here): one helper with the codes/counts grid (leaveLayer).
  const layer=leaveLayer(ctx,data,key,zone,state.leave);gridWrap.before(layer.status);

  // Schedule: the days around the draft are the published layer (grey); an accent edge marks where the draft starts.
  const ctxCls=(d:string)=>data.context?((d<w.starts_on||d>=w.ends_on)?' sd-ctx':'')+(d===w.starts_on?' sd-edge':''):'';
  let drawSerial=0;
  // The group whose toggle was just pressed: its redrawn toggle takes the focus back (unless the person moved it).
  let refocus='';
  // Each session's local day, once per draw (it was worked out per person and day).
  const days0=new Map<Row,string>(),dayOf=(s:Row)=>{let d=days0.get(s);if(!d){d=toLocal(s.start,zone).slice(0,10);days0.set(s,d);}return d;};
  function draw(){
    const jobs:(()=>void)[]=[];
    let want:HTMLElement|null=null;
    const settle=()=>{if(!want?.isConnected)return;const a=document.activeElement;if(!a||a===document.body)want.focus({preventScroll:true});want=null;};
    const q=state.find.trim().toLowerCase();
    // Everyone by id, once per draw: team heads and trainees' supervisors are named from it (not a scan per duty).
    const personById=new Map(ctx.catalogues.people.map(p=>[p.id,p]));
    let people=ctx.catalogues.people.filter(p=>byPerson.has(p.id)||(!filtered&&p.active!==false));
    if(q)people=people.filter(p=>findsPerson(p,q,hooks.code));
    const hours=new Map(people.map(p=>[p.id,hoursOf(p.id)]));
    people.sort((a,b)=>state.sort==='name'?fullNameOf(a).localeCompare(fullNameOf(b)):state.sort==='name-desc'?fullNameOf(b).localeCompare(fullNameOf(a)):state.sort==='hours-asc'?hours.get(a.id)!-hours.get(b.id)!:hours.get(b.id)!-hours.get(a.id)!);
    const groups=new Map<string,Row[]>(),groupName=new Map<string,string>();
    // Supervision teams (CA-03) come from each assignment's recorded supervisor on that same session (planner_teams.ts
    // teamOf): a trainee supervised by B one day and C the next is in both teams, each with only its own duties. Team
    // lead and rank never imply supervision. `scoped`: a person's sessions within one team.
    const NONE='No named supervision',scoped=new Map<string,Set<string>>();
    const teamsOf=(pid:string)=>{const keys:string[]=[];
      for(const {shift,p} of byPerson.get(pid)||[]){const lead=teamOf(shift.participants as Row[],p),k=lead?'team:'+lead:NONE;
        if(!groupName.has(k))groupName.set(k,lead?fullNameOf(personById.get(lead))||'Named supervisor':NONE);
        if(!keys.includes(k))keys.push(k);const at=k+'|'+pid;if(!scoped.has(at))scoped.set(at,new Set());scoped.get(at)!.add(shift.id);}
      return keys.length?keys:[NONE];};
    for(const p of people){const keys=state.group==='rank'?[rankOf(p.id)]:state.group==='team'?teamsOf(p.id):state.group==='tag'?(((p.tags||[]) as string[]).length?[(p.tags as string[])[0]]:['No tag']):['Everyone'];for(const k of keys){if(!groups.has(k)){groups.set(k,[]);groupRank.set(k,state.group==='rank'?roleRow(p.id)?.id||'':'');}groups.get(k)!.push(p);}}
    const nameOfGroup=(k:string)=>groupName.get(k)||k;
    // One team's sessions of a person (null: all of them, every grouping but Supervision team).
    const onlyIn=(k:string,pid:string)=>state.group==='team'?scoped.get(k+'|'+pid)||new Set<string>():null;
    layer.clear();key.clear();
    const grid=el('div',{class:'people-grid'+(itemSaysWords(props)?' it-words':''),role:'grid','aria-label':'Duties by person and day'});grid.style.setProperty('--days',String(days.length));
    // A chip is its duty type's code and hue, and Colour by's entry (dom.ts legend): one stamp for this grid's own chips and
    // the live auto-schedule view's (LiveHost.stamp; null: an open place), so a live chip is keyed like a drawn one.
    const stampChip=(n:Element,shift:Row,pid:string)=>{const code=weekView.colourBy==='type'?dutyColour(shift):'slate';
      key.mark(n,'t:'+shift.assignment_type_id,el('span',{class:`pg-chip duty-${code}`,'aria-hidden':'true'},sessionCode(ctx,shift)),shift.type_snapshot.name,0);
      colours.codeMark(shift,n);return weekView.colourBy==='type'?code:colours.seenChip(shift,pid,n);};
    const openKey=(n:Element)=>markOpen(key,n,el('span',{class:'pg-chip pg-open lg-mark','aria-hidden':'true'},el('b',{},'+1')));
    const shiftOf=new Map(data.shifts.map(x=>[x.id,x]));
    const placeOf=new Map((ctx.catalogues.locations||[]).map(l=>[l.id,l]));  // each chip names its place (QA-127a)
    hostLegend(grid,key);  // the rule map's exception marks in this grid are this key's entries
    (grid as LiveHost).stamp=(n,s,p)=>{if(s===null){openKey(n);return;}const x=shiftOf.get(s);if(x)stampChip(n,x,p);};
    grid.append(el('div',{class:'pg-row pg-head',role:'row'},el('div',{class:'pg-name',role:'columnheader'},`${people.length} people`),
      ...days.map(d=>{const we=isWeekend(d);return tag((n=>(we&&markWeekend(key,n),d===today&&markToday(key,n),n))(el('div',{class:'pg-day'+(we?' weekend':'')+(d===today?' today':'')+ctxCls(d),role:'columnheader','aria-label':formatDate(d,zone)},el('small',{},new Intl.DateTimeFormat('en-GB',{weekday:days.length>10?'narrow':'short',timeZone:'UTC'}).format(new Date(d+'T12:00:00Z'))),el('span',{},String(Number(d.slice(8)))))),{kind:'day',label:dayTitle(d),day:d,days:[d],shifts:visible.filter(s=>toLocal(s.start,zone).slice(0,10)===d).map(s=>s.id)},{pin:true});}),
      el('div',{class:'pg-hours',role:'columnheader'},'Hours')));
    scheduleWidths(grid,{row:grid.querySelector<HTMLElement>(':scope>.pg-head>.pg-name'),col:grid.querySelector<HTMLElement>(':scope>.pg-head>.pg-day'),colKey:'date'});
    // Open places, a row under the dates: where a place filled by auto-schedule visibly comes from (dashed, as the
    // Week view's "+ 1 open place"). Only while some day has one.
    const cover=coverOf(data),open=new Map<string,number>();
    for(const s of visible)if(!s.context&&!s.cancelled){const n=openPlaces(cover.get(s.id));if(n)open.set(dayOf(s),(open.get(dayOf(s))||0)+n);}
    if(open.size)grid.append(el('div',{class:'pg-row pg-open-row',role:'row'},el('div',{class:'pg-name',role:'rowheader'},el('strong',{class:'truncate'},'Open places')),
      ...days.map(d=>{const n=open.get(d)||0;return el('div',{class:'pg-cell'+ctxCls(d),role:'gridcell','data-day':d,...(n?{'aria-label':`${n} open place${n>1?'s':''}`}:{})},
        n?openKey(el('span',{class:'pg-chip pg-open','data-open':n,'aria-hidden':'true'},el('b',{},'+'+n))):'');}),el('div',{class:'pg-hours',role:'gridcell'})));
    if(!people.length)grid.append(el('div',{class:'wk-empty'},empty(q?'No one matches':filtered?'Hidden by filters':'No people in this view',q?'Try another name, code or tag.':filtered?'People here do not match the filters or search.':'Nobody works these days.',filtered&&hooks.onShowAll?button('Show all',hooks.onShowAll,'secondary'):null,filtered?'filter':'people')));
    for(const [name,list] of [...groups.entries()].sort((a,b)=>nameOfGroup(a[0]).localeCompare(nameOfGroup(b[0])))){
      const scopeOf=(pid:string)=>onlyIn(name,pid),hoursIn=(pid:string)=>{const only=scopeOf(pid);return only?hoursOf(pid,only):hours.get(pid)!;};
      const shut=state.collapsed.has(name),total=list.reduce((a,p)=>a+hoursIn(p.id),0);
      if(state.group!=='none'){
        const rank=groupRank.get(name)||'',hue=rank?rankColour(rank):'';
        const t=el('button',{type:'button',class:'wk-toggle','aria-expanded':String(!shut)},icon(shut?'chevronRight':'chevronDown'),hue?el('span',{class:`wk-swatch duty-${hue}`,'aria-hidden':'true'}):null,el('strong',{class:'truncate'},nameOfGroup(name)),el('span',{class:'wk-count'},`${list.length} ${list.length===1?'person':'people'} · ${Math.round(total)} h`));
        t.addEventListener('click',()=>{shut?state.collapsed.delete(name):state.collapsed.add(name);refocus=name;draw();});
        const head=el('div',{class:'pg-row pg-group',role:'row'},hue?key.mark(el('div',{class:`pg-group-head wk-group-head duty-${hue}`,role:'rowheader'},t),'c:r:'+rank,'duty-'+hue,nameOfGroup(name),0):el('div',{class:'pg-group-head wk-group-head',role:'rowheader'},t));
        jobs.push(()=>{grid.append(head);if(refocus===name){refocus='';want=t;}});
        if(shut)continue;
      }
      for(const person of list)jobs.push(()=>{
        const only=scopeOf(person.id),mine=(byPerson.get(person.id)||[]).filter(x=>!only||only.has(x.shift.id)),h=hoursIn(person.id);
        // A team's row counts that team's duties only, so the person's whole-rota target is not set against it.
        const target=only&&only.size<(byPerson.get(person.id)||[]).length?undefined:targets.get(person.id);
        // A tag chip's words are its whole meaning (its hue only repeats the tag): no key entry of its own.
        const tags=((person.tags||[]) as string[]).slice(0,2).map(t=>{const c=tagChip(t);c.dataset.lgSelf='';return c;});
        // Colour by person: the dot is that hue's mark (Colour by person reads no session).
        const dot=weekView.colourBy==='person'?el('span',{class:'person-dot','aria-hidden':'true'}):null;dot?.classList.add('duty-'+colours.seenChip({} as Row,person.id,dot));
        const row=el('div',{class:'pg-row person-group',role:'row','data-row-person':person.id},attachMenu(tag(el('div',{class:'pg-name',role:'rowheader'},el('strong',{class:'truncate'},dot,hooks.name(person)),
          el('small',{class:'truncate'},breaches.has(person.id)?breachMark(breaches.get(person.id)!):'',hooks.code(person),...tags)),{kind:'person',person:person.id,label:fullNameOf(person),sub:[rankOf(person.id),hooks.code(person)].join(' · '),days,shifts:mine.map(x=>x.shift.id)},{pin:true}),hooks.menu,{person_id:person.id,day:days[0]},'Actions for '+fullNameOf(person)));
        const onDay=new Map<string,typeof mine>();for(const x of mine){const d=dayOf(x.shift);onDay.set(d,[...(onDay.get(d)||[]),x]);}
        for(const d of days){
          const items=onDay.get(d)||[];
          const cell=el('div',{class:'pg-cell'+ctxCls(d),role:'gridcell','data-day':d});
          layer.wish(cell,person.id,d);
          for(const {shift,p} of items){
            const problem=(issues.get(person.id+':'+shift.id)||[]).map(f=>findingIssue(f));
            // A trainee on the same session as their named supervisor is paired (↳ and the supervisor's code); without one, set apart.
            const trainee=p.participation==='trainee',lead=trainee&&p.supervisor_person_id&&(shift.participants as Row[]).some(x=>x.person_id===p.supervisor_person_id)?personById.get(p.supervisor_person_id):undefined;
            const parts=itemParts(ctx,shift,person.id,zone,props,{only:ITEM_DRAWS.chip}),words=itemWords(parts);
            const chip=el('button',{type:'button','data-session':shift.id,'data-person':person.id,class:`pg-chip${shift.cancelled?' cancelled':''}${shift.context?' sd-greychip':''}${trainee&&!lead?' alone':''}`,
              'aria-label':`${fullNameOf(person)} · ${shift.type_snapshot.name}${shift.cancelled?' · cancelled':''}${parts.some(x=>x.key==='location')?'':' · '+labelOf(placeOf.get(shift.location_id))} · ${formatDate(shift.start,zone)} ${formatTime(p.start,zone)}–${formatTime(p.end,zone)}${lead?' · with '+fullNameOf(lead):trainee?' · without their supervisor':''}${words.length?' · '+words.join(' · '):''}${problem.length?' · '+issueLabel(problem):''}`},
              // The view's properties in its order and form (TL and OT as the key's tags by default); the head in bold.
              ...itemNodes(parts,key,x=>x===itemHead(parts)?'pg-chip-head':x.key==='time'?'pg-chip-time':'pg-chip-meta',{head:true}),...(lead?[key.mark(el('span',{class:'pr'},'↳ '+hooks.code(lead)),'pair',el('span',{class:'vs-pair','aria-hidden':'true'},'↳'),'On the same session as their supervisor',1)]:[]));
            // The chip is the drawn mark of its code, its hue and each state it shows (dom.ts legend `mark`).
            chip.classList.add('duty-'+stampChip(chip,shift,person.id));
            // The shared issue marker (D78) at the chip's end, drawn inside it (the chip says the words); its key entry is
            // Must fix, and a click on the mark opens its popover.
            const mark=problem.length?issueMarker(problem,{title:fullNameOf(person),inside:true,key}):null;if(mark){chip.append(mark);openMarkerFrom(chip);}
            if(shift.cancelled)key.mark(chip,'closed',el('span',{class:'pg-chip cancelled','aria-hidden':'true'},sessionCode(ctx,shift)),'Session closed');
            if(shift.context)key.mark(chip,'ctx',el('span',{class:'pg-chip sd-greychip lg-mark','aria-hidden':'true'},sessionCode(ctx,shift)),'Published, read-only',4);
            if(trainee&&!lead)key.mark(chip,'alone',el('span',{class:'pg-chip alone','aria-hidden':'true'},'TR'),'Trainee without their supervisor',1);
            tag(chip,{kind:'assignment',person:person.id,shift:shift.id,day:d,label:fullNameOf(person),sub:shift.type_snapshot.name},{own:true});
            hoverCard(chip,()=>[el('div',{class:'hc-head'},el('span',{class:`hc-badge duty-${dutyColour(shift)}`},sessionCode(ctx,shift)),el('div',{},el('strong',{},shift.type_snapshot.name),el('small',{},`${formatDate(shift.start,zone)} · ${formatTime(p.start,zone)}–${formatTime(p.end,zone)} · ${labelOf(ctx.catalogues.locations.find(l=>l.id===shift.location_id))}`))),
              problem.length?issueList(problem,{interactive:false}):el('ul',{class:'hc-lines'},el('li',{class:'hc-line ok'},el('span',{class:'hc-icon'},'✓'),el('span',{},el('strong',{},'No broken rules for this person here')))),
              ...ruleSummary({kind:'assignment',person:person.id,shift:shift.id,day:d,label:fullNameOf(person)})],400);
            // The planner opens the person on that session in its one panel (REQ-PANEL); a published session around the
            // draft opens read-only, as the Week and Timeline grids do (one session panel, never a second card).
            chip.addEventListener('click',e=>{if(!shift.context&&addsToSelection(e)){selectSession(shift);return;}if(!shift.context&&openAssignment(shift,p.id))return;(shift.context?hooks.view||hooks.details:hooks.details)(shift);});
            cell.append(chip);
          }
          layer.leave(cell,person.id,d,person.id+'|'+d+'|'+name);
          row.append(cell);
        }
        const over=!!target&&target.comparison!=='minimum'&&h>target.amount;
        const bar=el('span',{class:'pg-bar'+(over?' over':''),'aria-hidden':'true'},el('span',{}));
        if(over)key.mark(bar,'over',el('span',{class:'pg-bar over lg-bar','aria-hidden':'true'},el('span',{})),'Hours above target');
        // Against the target when there is one, otherwise against the busiest person shown.
        const of=Math.max(1,target?target.amount:maxHours);
        (bar.firstChild as HTMLElement).style.setProperty('--fill',String(Math.min(1,h/of)));
        // data-*: the auto-schedule live view moves the hours as duties move (autoschedule_live.ts).
        row.append(el('div',{class:'pg-hours',role:'gridcell','data-hours':h,'data-of':of,'data-target':target?target.amount:'','aria-label':target?`${Math.round(h)} of ${target.amount} target hours`:`${Math.round(h)} hours`},
          el('span',{},mine.length?`${Math.round(h)}${target?' / '+target.amount:''} h`:'—'),mine.length?bar:''));
        grid.append(row);
      });
    }
    // The first rows draw at once; the rest follow in small batches, so a department-size grid never blocks input
    // for long (one 150-person draw was a ~220 ms task).
    const serial=++drawSerial,batch=(from:number)=>{if(serial!==drawSerial)return;for(const job of jobs.slice(from,from+40))job();settle();if(from+40<jobs.length)setTimeout(()=>batch(from+40));};
    batch(0);
    gridWrap.replaceChildren(grid);roving(grid,gridWrap);settle();
  }
  redrawPeople=()=>{if(root.isConnected)draw();};
  draw();
  return root;
}
