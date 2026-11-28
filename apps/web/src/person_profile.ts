/** The person hub's tabs (People › a person). Each tab reads its own permitted source; mutation forms, request
 *  decisions and rule sources stay with their canonical owners. Sections with nothing to say are left out. */
import {isolate,el,button,field,select,note,empty,badge,armed,notify,roleLabel,grants,weekdayNames,dateUnits,formatDate,formatTime,toLocal,labelOf,tabs,type Child,type IconName,emptyLine,CAPABILITY,PAGE_WHY} from './dom.js';
import {readFailed,holdsLists,alive} from './dom.js';  // a failed read: refused (403) or an error with Retry (maintenance)
import {get,command,newest,sessionChanged,sessionKey,ApiError} from './api.js';
import {MEASURES} from './fairness.js';
import type {PersonRotations,PersonAccounts,PersonAuthActivity,PersonAvailability,PersonFairness,PersonFairnessBucket,PersonScore,PersonScoreRota,PersonSession,PersonSessions,PersonWorkload} from './generated.js';
import {skeleton,observeSize} from './loading.js';
import {dateRange} from './date_picker.js';
import {addDays} from './planner_logic.js';
import {dataTable} from './table_view.js';
import {catalogueForm,type Context} from './forms.js';
import {personRequests} from './requests.js';
import {wishesPanel} from './wishes.js';
import {personLink,fullNameOf} from './person_label.js';
import {personFacts} from './person_facts.js';
import {whereWhen} from './rule_view.js';
import {personTimelineTab} from './person_timeline.js';
import type {Row,Catalogues} from './types.js';

export type PersonPanel=[string,string,()=>Node];
export type ProfileParts={
  /** Contact facts: every reader of People sees them. */contact:()=>Node;
  /** Rank, clinics, skills, programmes (and access): from the department catalogues or, for a staff member's own
   *  profile, from /api/me/profile, then read-only. */records:(source:Catalogues,readOnly:boolean)=>Node[];
  /** Rules that apply and this person's exceptions (roles that may look rules up). */rules?:()=>Node;
  /** The audit feed rows, shared with System › Audit. */feed:(events:Row[])=>Node};
const permits=(ctx:Context,...keys:string[])=>ctx.permits(...keys);
const self=(ctx:Context,p:Row)=>ctx.permits(CAPABILITY.STAFF)&&!!ctx.me.person_id&&ctx.me.person_id===p.id;
const dates=(r:Partial<Row>)=>dateUnits([r.starts_on?formatDate(r.starts_on):'From creation',r.ends_on?'– '+formatDate(addDays(r.ends_on,-1)):'– no end']);

/** One titled block of the hub: heading, an optional action on the right, then its content. */
export const profileSection=(title:string,action:Child,...content:Child[])=>
  el('section',{class:'ph-section'},el('div',{class:'ph-head'},el('h3',{},title),action||''),...content);
/** One dated record line: what, its scope or detail, its dates, a state badge when it is not simply current, actions. */
export const profileRow=(what:Child,detail:Child,when:Child,state:Child,...actions:Child[])=>
  el('li',{class:'ph-row'},el('span',{class:'ph-what'},what,detail?el('small',{class:'muted'},detail):''),el('span',{class:'ph-when'},when),
    el('span',{class:'ph-state'},state||''),actions.some(Boolean)?el('span',{class:'row-buttons'},...actions):'');

/** A part of the panel that could not load: one line with Retry inside it (P-16). */
function failedWith(what:string,e:unknown,retry:()=>void){
  return readFailed(what.toLowerCase(),e,retry,what.toLowerCase());
}
/** A section that reads on demand; an answer for an earlier session or a closed panel never paints. */
function loadingSection(label:string,build:(target:HTMLElement)=>Promise<void>):HTMLElement{
  const box=el('div',{class:'ph-load'}),generation=sessionKey();let serial=0;
  const run=async()=>{
    const mine=++serial;
    box.replaceChildren(skeleton('lines',`Loading ${label.toLowerCase()}…`));
    const content=el('div',{class:'ph'});
    try{await build(content);if(mine===serial&&generation===sessionKey()&&alive(box))box.replaceChildren(content);}
    catch(e){if(mine===serial&&generation===sessionKey()&&alive(box)&&!sessionChanged(e))box.replaceChildren(failedWith(label,e,()=>void run()));}
  };
  void run();return box;
}

export function personProfilePanels(ctx:Context,p:Row,parts:ProfileParts):PersonPanel[]{
  const full=holdsLists(ctx.catalogues),mine=self(ctx,p),own=mine&&!full;
  // The team's schedule: the contract's readers (PERMISSIONS, the same list the server checks), or the person themself.
  const calendar=grants(ctx.me.roles,'schedule.read_team')||mine;
  // A programme lead reads a trainee's requests through the server's programme scope and decides each by its can_decide
  // (SCA-05); leave records and wishes stay the department's readers' and the person's own.
  const team=permits(ctx,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),requests=team||mine||permits(ctx,CAPABILITY.PROGRAMME_LEAD),wishes=!team&&!mine&&permits(ctx,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD);
  // Workload, fairness and score: department readers (the server's fairness readers) or the person themself.
  const workload=permits(ctx,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD)||mine;
  // Sign-ins and devices: administrators, and whoever signs in as this person (any role).
  const linked=!!ctx.me.person_id&&ctx.me.person_id===p.id,access=permits(ctx,CAPABILITY.ADMIN)||linked;
  const timeline=permits(ctx,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)||mine;
  // A staff member's own records come from their own endpoint (they cannot read the department catalogues).
  let profile:Promise<Catalogues>|null=null;
  const ownRecords=()=>profile??=get<{person_id:string;catalogues:Catalogues}>('/api/me/profile').then(r=>{
    if(r.person_id!==p.id)throw new Error('The profile returned is not this person’s.');return r.catalogues;},e=>{profile=null;throw e;});
  const overview=()=>el('div',{class:'ph'},parts.contact(),...(full?parts.records(ctx.catalogues,false):[]),
    own?loadingSection('Your records',async t=>{t.append(...parts.records(await ownRecords(),true));}):'');
  return [
    ['overview','Overview',overview],
    ...(full||mine?[['schedule','Schedule',()=>el('div',{},
      // Rotations, leave, academic time and conflicts over a year, above the duties: the person timeline's readers (staff: their own).
      timeline?profileSection('Timeline','',personTimelineTab(p)):'',
      own?loadingSection('Schedule',async t=>{t.append(personWork({...ctx,catalogues:await ownRecords()},p,calendar));}):personWork(ctx,p,calendar))] as PersonPanel]:[]),
    ...(workload?[['workload','Workload',()=>personWorkload(ctx,p)] as PersonPanel]:[]),
    ...(requests||wishes?[['leave','Requests',()=>el('div',{class:'ph'},
      ...(requests?[team||mine?personLeave(ctx,p):'',loadingSection('Requests',async t=>{
        t.append(await personRequests(ctx,p,{query:new URLSearchParams(),setQuery:()=>{},showDay:(day,id)=>showDay(ctx,day,id)}));})]:[]),
      ...(wishes?[loadingSection('Wishes',async t=>{t.append(await wishesPanel(ctx,undefined,p.id));})]:[]))] as PersonPanel]:[]),
    ...(parts.rules?[['rules','Rules',parts.rules] as PersonPanel]:[]),
    // Quotas, targets and overtime offers: reviewed records on the person (the department's readers, and their own).
    ...(permits(ctx,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)||mine?[['facts','Quotas',()=>{const box=el('div',{});
      // A live update may have rebuilt the panel since this tab drew (one landing while Approve read the rotas): a change's
      // follow-up then redraws the page, read afresh, never only this tab's replaced box (one still being built is alive).
      const draw=(first=false)=>{if(!first&&!alive(box)){void ctx.refresh();return;}
        box.replaceChildren(loadingSection('Quotas & offers',t=>personFacts(ctx,p,t,()=>draw())));};draw(true);return box;}] as PersonPanel]:[]),
    ...(grants(ctx.me.roles,'audit.read')?[['history','History',()=>personHistory(ctx,p,parts.feed,permits(ctx,CAPABILITY.ADMIN)||linked)] as PersonPanel]:[]),
    ...(access?[['access','Access',()=>personAccess(ctx,p,linked)] as PersonPanel]:[]),
  ];
}

/** Tabs whose short label stands for a longer name (the name screen readers hear and the tooltip shows). */
const FULL:Record<string,string>={workload:'Workload & fairness',leave:'Leave & requests',facts:'Quotas & offers'};
const SECTION_ICON:Record<string,IconName>={schedule:'calendar',workload:'scale',leave:'leave',rules:'rules',facts:'chart',history:'history',access:'shield'};
/** The sections after Overview this reader may open for `p`, by their full names: the People row's ⋯ menu lists them.
 *  Read from personProfilePanels itself, so the menu and the panel's tabs never disagree (its builders are lazy and are
 *  not run here; `rules`: whether this reader may look rules up, as the panel's Rules part says). */
export const personSections=(ctx:Context,p:Row,rules:boolean):[key:string,name:string,icon:IconName][]=>
  personProfilePanels(ctx,p,{contact:()=>el('div'),records:()=>[],rules:rules?()=>el('div'):undefined,feed:()=>el('div')})
    .filter(([key])=>key!=='overview').map(([key,label])=>[key,FULL[key]||label,SECTION_ICON[key]||'menu']);
/** The hub's tab row: short labels fit one row at the panel's width; where it still scrolls (a phone), the edges fade
 *  on the side with more, and the open tab is brought into view. */
export function personTabs(label:string,list:PersonPanel[],shown:string,select:(key:string)=>void):HTMLElement{
  let row:HTMLElement|null=null;
  const reveal=(key:string)=>requestAnimationFrame(()=>{const t=row?.querySelector<HTMLElement>(`[id$="-${key}-tab"]`);if(!row||!t)return;
    const l=t.offsetLeft-row.offsetLeft,r=l+t.offsetWidth;if(l<row.scrollLeft)row.scrollLeft=l;else if(r>row.scrollLeft+row.clientWidth)row.scrollLeft=r-row.clientWidth;});
  const box=tabs(label,list,shown,k=>{select(k);reveal(k);});
  row=box.querySelector<HTMLElement>('[role=tablist]')!;
  for(const [key,name] of Object.entries(FULL)){const t=row.querySelector<HTMLElement>(`[id$="-${key}-tab"]`);if(t){t.setAttribute('aria-label',name);t.dataset.tip=name;}}
  const fade=()=>{row!.classList.toggle('fade-start',row!.scrollLeft>1);row!.classList.toggle('fade-end',row!.scrollLeft+row!.clientWidth<row!.scrollWidth-1);};
  row.addEventListener('scroll',fade,{passive:true});observeSize(row,fade);
  reveal(shown);return box;
}

const num=(n:number|null|undefined,d=1)=>n==null?'—':String(Math.round(n*10**d)/10**d);
const signed=(n:number,d=1)=>(n>0?'+':n<0?'−':'±')+num(Math.abs(n),d);
const personPath=(p:Row,what:string,q:Record<string,string>={})=>`/api/people/${encodeURIComponent(p.id)}/${what}`+(Object.keys(q).length?'?'+new URLSearchParams(q):'');
const zoneOf=(ctx:Context)=>ctx.me.organisation.timezone;
const dayOf=(ctx:Context,iso:string)=>toLocal(iso,zoneOf(ctx)).slice(0,10);
/** "Today 07:52", "Yesterday 18:10", or the date and time. */
function moment(ctx:Context,iso:string){
  const zone=zoneOf(ctx),today=dayOf(ctx,new Date().toISOString()),day=dayOf(ctx,iso);
  return `${day===today?'Today':day===addDays(today,-1)?'Yesterday':formatDate(iso,zone)} ${formatTime(iso,zone)}`;
}
/** "5 min", "2 h", "3 days". */
const length=(ms:number)=>{const min=Math.max(1,Math.round(ms/60000));return min<60?`${min} min`:min<1440?`${Math.round(min/60)} h`:`${Math.round(min/1440)} days`;};
/** "Active now", "5 min ago", "2 h ago", "3 days ago". */
const ago=(iso:string)=>Date.now()-Date.parse(iso)<120000?'Active now':length(Date.now()-Date.parse(iso))+' ago';
/** One measure against its aim: a bar, a marker at the aim, and "value / aim". `tone` colours an out-of-band bar. */
function aimLine(name:Child,sub:Child,value:number,aim:number|null,tone:string,unit='',said=''):HTMLElement{
  const scale=Math.max(value,aim??0,1)*1.25,bar=el('span',{class:'pr-bar'+(tone?' '+tone:''),'aria-hidden':'true'},el('span',{class:'pr-fill'}),aim==null?'':el('span',{class:'pr-aim'}));
  (bar.firstChild as HTMLElement).style.setProperty('width',Math.round(value/scale*100)+'%');
  if(aim!=null)(bar.lastChild as HTMLElement).style.setProperty('left',Math.round(aim/scale*100)+'%');
  return el('li',{class:'pr-line'},el('span',{class:'ph-what'},name,sub?el('small',{class:'muted'},sub):''),bar,
    said?el('span',{class:'pr-num pr-said'},said):el('span',{class:'pr-num mono'},`${num(value)}${aim==null?'':' / '+num(aim)}${unit}`));
}
/** A person listed on People with no tie to this department yet (no rank, role or sign-in here): the server has no
 *  record of theirs to read, which the tabs show as empty rather than as a failure. */
const gone=(e:unknown)=>e instanceof ApiError&&e.status===404;
const absent=<T,>(read:Promise<T>,none:T)=>read.catch(e=>{if(gone(e))return none;throw e;});
const NO_SESSIONS={person_id:'',sessions:[],total:0,limit:0,offset:0} as PersonSessions;
const nothing=()=>empty('Nothing in this period','Duties, goals, targets, limits and the schedule score show here once there is work to count.',null,'chart');
const COMPARE:Record<string,string>={minimum:'Minimum',maximum:'Maximum',target:'Target'};
/** Under a goal's name: what it measures and its aim, unless the name says the aim already ("Fair share"). */
const aimOf=(g:{name:string;aim:string;metric:string})=>{const aim=g.aim==='target'?'Target':'Fair share';
  return g.name.toLowerCase().includes(aim.toLowerCase())?measure(g.metric):`${aim} · ${measure(g.metric)}`;};
const measure=(m:string)=>MEASURES.find(c=>c.value===m)?.label||(m==='pair_exposure'?'Sessions together':m.replaceAll('_',' '));
/** The days a period covers, from its start to the day before its (exclusive) end; dates or instants. */
const span=(ctx:Context,a:string,b:string)=>{const day=(x:string)=>x.length>10?dayOf(ctx,x):x;return `${formatDate(day(a))} – ${formatDate(addDays(day(b),-1))}`;};

/** "2 duties", "1 duty", "3 h": a goal's unit, singular for one. */
const amount=(n:number,unit:string)=>`${num(n)} ${n===1?unit.replace(/ies$/,'y').replace(/s$/,''):unit}`;
/** Where one person stands on a balance goal, from its server row (fairness.py decides the status and the band). Said
 *  once in full on the goal's row ("1 duty · fair share 0.3 · within ±1"); the Duties card names only the aim ("fair
 *  share 0.3") and the Fairness card only the status ("Within tolerance", "Above by 2"). */
type Standing={actual:number;expected:number;high:number|null;status:string};
const aimText=(g:{aim:string},r:Standing)=>`${g.aim==='target'?'target':'fair share'} ${num(r.expected)}`;
const tolerance=(r:Standing)=>r.high==null?'':`±${num(r.high-r.expected)}`;
const verdict=(r:Standing)=>r.status==='within'?(r.high==null?'on aim':`within ${tolerance(r)}`):`${r.status} by ${num(Math.abs(r.actual-r.expected))}`;
const standing=(g:{aim:string;unit:string},r:Standing)=>`${amount(r.actual,g.unit)} · ${aimText(g,r)} · ${verdict(r)}`;
const statusText=(r:Standing)=>r.status==='within'?(r.high==null?'On aim':'Within tolerance'):`${r.status[0].toUpperCase()}${r.status.slice(1)} by ${num(Math.abs(r.actual-r.expected))}`;
const PERIODS:[string,string][]=[['this_week','This week'],['this_month','This month'],['rolling_12_weeks','Last 12 weeks']];
type Period=PersonWorkload['periods'][number];

/** Workload & fairness for the one period the switch names: a rota (the published one, or the draft for planners) or a
 *  calendar period. Every card, goal, limit and the score read that period, so no figure is set against another
 *  rota's or another period's. */
function personWorkload(ctx:Context,p:Row):HTMLElement{
  return loadingSection('Workload',async t=>{
    // A failed score read hides nothing else (P-15): the calendar periods still load; the rotas are missing, said once.
    let scoreFailed:unknown=null;
    const s=await absent(get<PersonScore>(personPath(p,'score')),{person_id:p.id,draft:null,published:null}).catch((e:unknown)=>{if(sessionChanged(e))throw e;scoreFailed=e;return {person_id:p.id,draft:null,published:null} as PersonScore;});
    const rotas=[s.draft,s.published].filter((r):r is PersonScoreRota=>!!r);
    // Opens on the period that covers today: the published rota covering today, else this calendar month (never an
    // empty future draft); planners can still choose their draft.
    const today=dayOf(ctx,new Date().toISOString()),current=rotas.find(r=>r.publication_id&&r.starts_on<=today&&today<r.ends_on);
    const pick=select('Period',[...rotas.map(r=>({value:r.workspace_id,label:r.publication_id||/\bdraft\b/i.test(r.name)?r.name:`${r.name} (draft)`,group:'Rotas'})),
      ...PERIODS.map(([value,label])=>({value,label,group:'Calendar'}))],current?.workspace_id||'this_month');
    const dates=el('small',{class:'muted'}),out=el('div',{class:'ph'});
    const show=()=>{const key=pick.input.value,rota=rotas.find(r=>r.workspace_id===key)||null;dates.textContent='';
      out.replaceChildren(loadingSection('Workload',async t=>{
        // A published rota is read on its first day; a draft by its id.
        const draft:Record<string,string>=rota&&!rota.publication_id?{workspace_id:rota.workspace_id}:{};
        const [work,fair]=await Promise.all([absent(get<PersonWorkload>(personPath(p,'workload',rota?.publication_id?{on:rota.starts_on}:draft)),null),
          rota?absent(get<PersonFairness>(personPath(p,'fairness',draft)),null):null]);
        const period=work?.periods.find(x=>x.key===(rota?'this_rota':key));
        if(!work||!period){t.append(nothing());return;}
        if(rota&&(work.workspace?.id!==rota.workspace_id||fair?.workspace&&fair.workspace.id!==rota.workspace_id))throw new Error('This rota’s numbers are not available.');
        dates.textContent=span(ctx,period.starts_on,period.ends_on);
        t.append(...periodCards(ctx,p,period,fair,rota));
      }));};
    pick.input.addEventListener('change',show);show();
    const missing=scoreFailed?readFailed('the rota scores',scoreFailed,()=>{const again=personWorkload(ctx,p);t.closest('.ph-load')?.replaceWith(again);},undefined,'Only calendar periods are offered.'):null;
    t.append(...(missing?[missing]:[]),el('div',{class:'pr-period'},pick.wrap,dates),out);
  });
}

/** The cards, goals, limits and score of one period. */
function periodCards(ctx:Context,p:Row,period:Period,fair:PersonFairness|null,rota:PersonScoreRota|null):(Node|string)[]{
  const same=(x:{starts_on:string;ends_on:string}|Row)=>x.starts_on===period.starts_on&&x.ends_on===period.ends_on;
  const named=(kind:string,id:string)=>labelOf(ctx.catalogues[kind]?.find(r=>r.id===id));
  const goals=fair?.goals.filter(g=>g.people.length||g.pairs.length)||[];
  // A card's note compares its number only with a goal or target over the same dates that counts the same thing.
  const compare=(metric:string,value:number):[string,string,string]|null=>{
    const g=goals.find(g=>g.metric===metric&&same(g)&&g.people[0]?.actual===value),r=g?.people[0];
    if(g&&r)return [aimText(g,r),'',g.name];
    const q=(period.quotas as Row[]).find(q=>q.metric===metric&&same(q)&&Number(q.actual)===value);if(!q)return null;const d=Number(q.difference);
    return [Math.abs(d)<1e-9?'on target':`${signed(d)}${metric==='hours'?' h':''} vs ${String(COMPARE[q.comparison]||'target').toLowerCase()}`,/^(above|below)/.test(String(q.status))?'warn':'ok',String(q.policy_name)];};
  // Fairness in plain words: how far this person is from the goal they are furthest from (or within every goal).
  const rows=goals.flatMap(g=>g.people.map(r=>({g,r}))),off=rows.filter(x=>x.r.status!=='within').sort((a,b)=>b.r.cost-a.r.cost)[0],top=off||rows[0];
  const fairness=top&&card('Fairness',statusText(top.r),
    [top.r.rank!=null&&top.r.peers?`rank ${top.r.rank} of ${top.r.peers}`:top.g.name,'',
      [top.g.name,`${aimText(top.g,top.r)}${tolerance(top.r)?' '+tolerance(top.r):''}`,span(ctx,top.g.starts_on,top.g.ends_on)].join(' · ')],true);
  const weekends=period.weekends[0]?.value??0;
  const cards=el('div',{class:'ov-stats pr-stats'},card('Duties',String(period.totals.duties),compare('duties',period.totals.duties)),
    card('Hours',`${num(period.totals.hours)} h`,compare('hours',period.totals.hours)),card('Weekends',String(weekends),compare('weekends',weekends)),
    // Overtime they volunteered for in this period (approved offers); shown once there is some.
    period.totals.overtime_offered_hours?card('Overtime offered',`${num(period.totals.overtime_offered_hours)} h`,null):'',fairness||'');
  // Balance goals (fair share or a set aim) and this period's targets, each against its aim, with its own dates.
  // A spread or pair-limit goal counts in parts (a week, a partner): each part under its goal, as the server counted it.
  const parts=(r:{buckets?:PersonFairnessBucket[]})=>(r.buckets||[]).filter(b=>b.bucket||b.partner_id).map(b=>{const li=aimLine(
    [b.bucket?`Week of ${formatDate(b.bucket)}`:'',b.partner_id?`with ${named('people',b.partner_id)||'a partner'}`:''].filter(Boolean).join(' '),'',b.actual,b.expected,b.cost>0?'warn':'');
    li.classList.add('pr-part');return li;});
  const lines=[...goals.flatMap(g=>[...g.people.flatMap(r=>[aimLine(g.name,`${aimOf(g)} · ${span(ctx,g.starts_on,g.ends_on)}`,r.actual,r.expected,r.status==='within'?'':'warn','',standing(g,r)),...parts(r)]),
      ...g.pairs.map(r=>aimLine(g.name,`With ${r.partner_name} · ${span(ctx,g.starts_on,g.ends_on)}`,r.actual,r.expected,r.status==='within'?'':'warn','',standing(g,r)))]),
    // A target that counts only some duties says which (its clinics and days), so "0 / 2" beside 32 duties reads right.
    ...(period.quotas as Row[]).map(q=>aimLine(String(q.policy_name),[`${COMPARE[q.comparison]||'Target'} ${num(q.amount)}`,whereWhen(q.scope,named),span(ctx,q.starts_on,q.ends_on)].filter(Boolean).join(' · '),Number(q.actual),Number(q.amount),/^(above|below)/.test(String(q.status))?'warn':''))];
  const goalList=lines.length?profileSection('Balance goals and targets','',el('ul',{class:'ph-rows'},...lines)):'';
  goalList&&goalList.classList.add('pr-card');
  // Hour and duty ceilings in this period, each in its own window (a week, or a month).
  const seen=new Set<string>(),limits=period.limits.filter(l=>!seen.has(l.policy_id+l.start+l.group)&&!!seen.add(l.policy_id+l.start+l.group));
  // One line per limit: a limit checked in many windows (a daily maximum over a month) shows its fullest window and how
  // many it checks, not a line per day.
  const byLimit=new Map<string,typeof limits>();for(const l of limits){const k=l.policy_id+'|'+l.group;byLimit.set(k,[...(byLimit.get(k)||[]),l]);}
  const ceilings=limits.length?profileSection('Limits','',el('ul',{class:'ph-rows'},
    ...[...byLimit.values()].map(ws=>{const l=ws.reduce((a,b)=>b.actual-b.maximum>a.actual-a.maximum?b:a),h=l.kind==='max_hours'?' h':'';
      const when=ws.length>1?`${ws.length} periods${l.actual>0?' · fullest '+span(ctx,l.start,l.end):''}`:span(ctx,l.start,l.end);
      return aimLine(l.policy_name,`Maximum ${num(l.maximum)}${h} · ${when}`,l.actual,l.maximum,l.actual>l.maximum?'err':l.actual===l.maximum?'warn':'',h);}))):'';
  ceilings&&ceilings.classList.add('pr-card');
  if(!rota&&!lines.length&&!limits.length&&!period.totals.duties&&!period.totals.overtime_offered_hours)return [nothing()];
  return [cards,goalList,ceilings,rota?scoreCard(ctx,p,rota):''];
}
const card=(label:string,value:string,n:[string,string,string]|null,words=false)=>el('div',{class:'ov-stat'},el('span',{class:'ov-stat-label'},label),el('strong',{class:words?'words':''},value),
  n?el('small',{class:'pr-note '+n[1],'data-tip':n[2]},n[0]):'');

/** How this person moves the score: each line's points, coloured by what it is (a broken rule, advice, a granted wish). */
function scoreCard(ctx:Context,p:Row,rota:PersonScoreRota):HTMLElement{
  const severity=new Map(rota.rule_weights.map(r=>[String(r.rule_id),String(r.severity)]));
  const counted=new Set(rota.contributions.map(c=>`${c.rule_id}|${c.shift_id}`));
  // Findings the score does not count (an older publication without per-line points) still show, without a number.
  const extra=rota.findings.filter(f=>!counted.has(`${f.rule_id}|${f.shift_id}`));
  const line=(text:string,tone:string,delta:number|null)=>{const [head,...rest]=text.split(' · ');
    return el('li',{class:'pr-score-line'},el('span',{class:'pr-dot '+tone,'aria-hidden':'true'}),el('span',{},el('strong',{},head),rest.length?el('span',{class:'muted'},' · '+rest.join(' · ')):''),
      delta==null?'':el('span',{class:'pr-num mono '+(delta<0?tone:'info')},signed(delta)));};
  const total=rota.contributions.reduce((a,c)=>a+c.score_delta,0);
  const places=rota.fresh?loadingSection('Open places',async t=>{
    const params={open_limit:'20',...(!rota.publication_id?{workspace_id:rota.workspace_id}:{})};
    const result=await get<PersonScore>(personPath(p,'score',params));
    const current=[result.draft,result.published].find(r=>r?.workspace_id===rota.workspace_id&&r.publication_id===rota.publication_id);
    if(current)t.append(...opportunityPlaces(ctx,p,current));
  }):'';
  return el('div',{class:'ph'},el('section',{class:'ph-section pr-card'},el('div',{class:'ph-head'},el('h3',{},`Score in ${rota.name}${rota.basis==='stored_draft_check'?' (draft)':''}`),
      rota.contributions.length?el('span',{class:'pr-num mono'},signed(total)):''),
    !rota.fresh?note('The draft changed since its last check; run the check again for current numbers.','warning'):'',
    rota.contributions.length||extra.length?el('ul',{class:'ph-rows'},...rota.contributions.map(c=>line(c.text,c.score_delta>0?'info':(c.rule_severity??severity.get(String(c.rule_id)))==='mandatory'?'err':'warn',c.score_delta)),
      ...extra.map(f=>line(String(f.message),f.severity==='error'?'err':'warn',null))):emptyLine('Nothing involving this person changes the score.')),places);
}

function opportunityPlaces(ctx:Context,p:Row,rota:PersonScoreRota):Node[]{
  return rota.open_places.length?[profileSection(`Open places ${fullNameOf(p)} could fill`,el('small',{class:'muted'},(rota.open_places_total??0)>rota.open_places.length?`First ${rota.open_places.length} of ${rota.open_places_total}`:''),
    el('ul',{class:'ph-rows'},...rota.open_places.map(o=>profileRow([o.session_label,...o.service_names,o.location_name].filter(Boolean).join(' · '),
      `${formatDate(o.start,zoneOf(ctx))} ${formatTime(o.start,zoneOf(ctx))}–${formatTime(o.end,zoneOf(ctx))}${o.team_lead?' · team lead':''}`,
      el('span',{class:'mono'},signed(o.score_delta)),o.status==='warn'?badge('With a warning','warn'):'',
      // The session itself, in this rota, unfiltered (an open place has nobody on it to filter by).
      button('Show in Schedule',()=>ctx.navigate(`schedule/week/${dayOf(ctx,o.start)}?edit=${encodeURIComponent(rota.workspace_id)}&shift=${encodeURIComponent(o.shift_id)}`),'ghost small')))))]:[];
}

/** Leave and weekly unavailability, past six months to six months ahead; the department's leave-reason setting decides
 *  what each says (the server words it). Requests and swaps follow from the Requests list. */
function personLeave(ctx:Context,p:Row):HTMLElement{
  const today=dayOf(ctx,new Date().toISOString()),start=addDays(today,-183),end=addDays(today,183);
  return loadingSection('Leave',async t=>{
    const a=await absent(get<PersonAvailability>(personPath(p,'availability',{start,end})),{availability:[],patterns:[],availability_partial:false} as unknown as PersonAvailability),zone=zoneOf(ctx);
    const when=(r:Row)=>{const s=dayOf(ctx,r.start),e=dayOf(ctx,r.end),whole=formatTime(r.start,zone)==='00:00'&&formatTime(r.end,zone)==='00:00';
      return dateUnits(whole?[formatDate(r.start,zone),s===addDays(e,-1)?'':'– '+formatDate(addDays(e,-1))]:[`${formatDate(r.start,zone)} ${formatTime(r.start,zone)}`,`– ${s===e?'':formatDate(r.end,zone)+' '}${formatTime(r.end,zone)}`]);};
    const leave=(a.availability as Row[]).filter(r=>!r.retracted);
    const weekly=(a.patterns as Row[]).filter(r=>['active','proposed'].includes(String(r.status)));
    t.append(profileSection('Leave and time away',el('small',{class:'muted'},`${formatDate(start)} – ${formatDate(end)}`),leave.length?el('ul',{class:'ph-rows'},...leave.map(r=>profileRow(String(r.label),r.note||'',when(r),
        r.end<=new Date().toISOString()?badge('Past'):''))):emptyLine('None in these dates.'),
      a.availability_partial?note('Showing the first 2,000 records.','warning'):''),
      weekly.length?profileSection('Weekly unavailability','',el('ul',{class:'ph-rows'},...weekly.map(r=>profileRow(String(r.label),
        `${weekdayNames(r.weekdays,true)} · ${r.start_time}–${r.end_time}`,dates(r),r.status==='proposed'?badge('Awaiting decision','info'):'')))):'');
  });
}

type Session=PersonSession;
const AUTH_ACTION:Record<string,string>={'auth.sign_in':'session.sign_in','auth.sign_out':'session.sign_out','auth.session_revoke':'session.end'};
/** "Chrome · Windows · Desktop"; with neither browser nor system known, simply "Unknown device". */
const device=(s:{device_label:string|null})=>!s.device_label||s.device_label.startsWith('Unknown browser / Unknown OS')?'Unknown device':s.device_label.replaceAll(' / ',' · ');
/** Access: the sign-in account, the last sign-in, and every session and device, with Sign out per session and for all
 *  (each a recorded command; the button asks again in red before it acts). */
function personAccess(ctx:Context,p:Row,mine:boolean):HTMLElement{
  const box=el('div',{class:'ph'});
  const draw=()=>box.replaceChildren(loadingSection('Access',async t=>{
    const [a,s]=await Promise.all([absent(get<PersonAccounts>(personPath(p,'accounts')),{person_id:p.id,accounts:[]}),absent(get<PersonSessions>(personPath(p,'sessions')),NO_SESSIONS)]);
    const accounts=a.accounts,last=accounts.map(x=>x.last_sign_in_at).filter(Boolean).sort().at(-1) as string|undefined,latest=s.sessions[0];
    const card=(label:string,value:string,note_:string,tone='')=>el('div',{class:'ov-stat'},el('span',{class:'ov-stat-label'},label),el('strong',{class:'truncate','data-tip':value},value),el('small',{class:'pr-note '+tone},note_));
    t.append(el('div',{class:'ov-stats pr-stats two'},
      card('Sign-in account',accounts.length?accounts.map(x=>x.username||x.display_name).join(', '):'None',
        accounts.length?[accounts.every(x=>x.disabled)?'disabled':'linked',...new Set(accounts.flatMap(x=>x.roles.map(r=>roleLabel(r).toLowerCase())))].join(' · '):'Not linked to a sign-in',accounts.length&&!accounts.every(x=>x.disabled)?'ok':''),
      card('Last sign-in',last?moment(ctx,last):'Never',latest?device(latest):'')));
    const signOut=(r:Session)=>armed(button('Sign out',async()=>{await command('session.revoke',{reason:'Signed out from the person record'},r.id,r.revision);notify('Signed out of that device.');draw();},'secondary small',{'aria-label':`Sign out ${r.device_label||'this session'}`}),'Sign out?');
    const others=s.sessions.filter(r=>r.can_revoke&&!r.current),open=accounts.filter(x=>!x.disabled);
    // Someone else's record: one command per sign-in ends all its sessions. One's own: every other device (this one stays).
    const all=mine?(others.length?armed(button('Sign out other devices',async()=>{const r=await command<{ended:number}>('account.end_other_sessions',{reason:'Signed out of other devices'});
        notify(`Signed out of ${r.ended===1?'1 other device':r.ended+' other devices'}.`);draw();},'secondary small'),'Sign out all?'):null)
      :open.length&&s.sessions.some(r=>r.active)?armed(button('Sign out everywhere',async()=>{let ended=0;for(const x of open)ended+=(await command<{ended:number}>('account.end_sessions',{reason:'Signed out everywhere from the person record'},x.identity_id)).ended;
        notify(`${isolate(fullNameOf(p))} was signed out everywhere (${ended===1?'1 session':ended+' sessions'}).`);draw();},'secondary small'),'Sign out everywhere?'):null;
    const status=(r:Session)=>r.current?`This device${r.expires_at===null?' · No automatic expiry':''}`:r.active?(r.expires_at===null?'No automatic expiry':`Expires in ${length(Date.parse(r.expires_at)-Date.now())}`):r.revoked?'Signed out':'Expired';
    t.append(el('section',{class:'ph-section pr-card'},el('div',{class:'ph-head'},el('h3',{},'Sessions and devices'),all),
      s.sessions.length?dataTable<Session>({id:'person-sessions',noun:['session','sessions'],rows:s.sessions,rowId:r=>r.id,toolbar:false,resultScope:()=>p.id,defaults:{wrap:['device']},
        columns:[{key:'device',label:'Device',text:r=>device(r),cell:r=>device(r)},
          {key:'network',label:'Network',text:r=>r.network_label||'',cell:r=>r.network_label||el('span',{class:'muted'},'—')},
          {key:'active',label:'Last active',sort:r=>String(r.last_active_at||r.created_at),cell:r=>r.active?ago(String(r.last_active_at||r.created_at)):el('span',{class:'muted'},moment(ctx,String(r.last_active_at||r.created_at)))},
          {key:'status',label:'Status',text:r=>status(r),cell:r=>el('span',{class:'muted'},status(r))}],
        rowButtons:r=>r.can_revoke&&!r.current?[signOut(r)]:[]}).root:emptyLine('No sessions yet.'),
      s.total>s.sessions.length?el('p',{class:'muted'},`Newest ${s.sessions.length} of ${s.total}.`):''));
  }));
  draw();return box;
}

const showDay=(ctx:Context,day:string,id:string)=>ctx.navigate(`schedule/week/${day}?edit=none&person=${encodeURIComponent(id)}`);
type Calendar={events:Row[];programme_calendar:Row[];pair_names:Record<string,string>};
/** Published duties and time away (/api/calendar, which applies the leave-reason setting), rotations, attachments and
 *  academic dates in one chosen period of up to 31 days. */
function personWork(ctx:Context,p:Row,calendar:boolean):HTMLElement{
  const zone=ctx.me.organisation.timezone,today=toLocal(new Date().toISOString(),zone).slice(0,10),start=today.slice(0,7)+'-01';
  const first=field('From','date',start,{required:true}),last=field('Through','date',addDays(addDays(start,32).slice(0,7)+'-01',-1),{required:true});
  const range=dateRange('Dates',first,last,{maxDays:31});range.dataset.lookup='';
  const out=el('div',{class:'ph'}),box=el('div',{class:'ph'},range,out);
  const read=newest((path:string)=>get<Calendar>(path)),name=(kind:string,id:string)=>labelOf(ctx.catalogues[kind]?.find(r=>r.id===id));
  const manage=permits(ctx,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.PROGRAMME_LEAD);
  const open=(kind:string,r:Row,label:string):Child=>manage?button(label,()=>catalogueForm(ctx,kind,r),'link-button'):label;
  const who=(id:string,label:string)=>personLink(id,label,ctx.navigate);
  let serial=0;
  const draw=async()=>{
    const mine=++serial,from=first.input.value,through=last.input.value,end=through?addDays(through,1):'';
    if(!from||!through||from>=end||Date.parse(end)-Date.parse(from)>31*86400000){out.replaceChildren(note('Choose up to 31 days.'));return;}
    out.replaceChildren(skeleton('lines','Loading schedule…'));
    try{
      // Rotations for exactly these dates (the catalogue holds only the last 12 months onward), as trainee or mentor.
      const [data,held]=await Promise.all([calendar?read('/api/calendar?'+new URLSearchParams({start:from,end,person_id:p.id})):null,
        ctx.catalogues.rotations?get<PersonRotations>(`/api/people/${encodeURIComponent(p.id)}/rotations?`+new URLSearchParams({start:from,end})):null]);
      if(mine!==serial||calendar&&!data)return;
      const overlapping=(r:Row)=>(!r.starts_on||r.starts_on<end)&&(!r.ends_on||r.ends_on>from);
      const sections:Node[]=[];
      if(data){
        // Rotations are listed once, from their own records below (with cohort, mentor and availability).
        const events=data.events.filter(r=>r.person_id===p.id&&r.kind!=='rotation');
        const where=(r:Row)=>[...(r.services||[]).map((s:Row)=>s.name),r.location_name].filter(Boolean).join(' · ');
        const pairs=(r:Row)=>[...(r.supervisor_person_id?[[r.supervisor_person_id,'with']]:[]),...(r.trainee_ids||[]).map((id:string)=>[id,'supervising'])];
        if(events.length)sections.push(el('section',{class:'ph-section'},el('h3',{},'Duties and time away'),dataTable<Row>({id:'person-work',noun:['record','records'],rows:events,rowId:r=>r.kind+':'+r.id,toolbar:false,
          resultScope:()=>[p.id,from,end].join('|'),
          columns:[{key:'when',label:'When',sort:r=>String(r.start),cell:r=>dateUnits([formatDate(r.start,zone)+' '+formatTime(r.start,zone),'– '+(toLocal(r.end,zone).slice(0,10)===toLocal(r.start,zone).slice(0,10)?'':formatDate(r.end,zone)+' ')+formatTime(r.end,zone)])},
            // Where and with whom read under what it is; an absence's note shows only when the leave setting allows it.
            {key:'what',label:'What',text:r=>[r.label,where(r),r.note].join(' '),cell:r=>{const w=where(r),with_=pairs(r);
              return el('span',{class:'ph-what'},String(r.label),w||with_.length?el('small',{class:'muted'},w,...with_.flatMap(([id,label],i)=>[w||i?' · ':'',label+' ',who(id,data.pair_names[id]||'Person')])):'',
                r.note?el('small',{class:'muted'},r.note):'');}}],
          rowButtons:r=>r.kind==='duty'?[button('Show in Schedule',()=>showDay(ctx,toLocal(r.start,zone).slice(0,10),p.id),'ghost small')]:[]}).root));
        const academic=data.programme_calendar;
        if(academic.length)sections.push(profileSection('Academic calendar','',el('ul',{class:'ph-rows'},...academic.map(r=>profileRow(r.name,r.programme_name,dates(r),'')))));
      }
      if(holdsLists(ctx.catalogues)){
        const rotations=(held?.rotations||[]) as Row[];
        const attachments=(ctx.catalogues.mentorships||[]).filter(r=>(r.person_id===p.id||r.mentor_id===p.id)&&overlapping(r));
        if(rotations.length)sections.push(profileSection('Rotations','',el('ul',{class:'ph-rows'},...rotations.map(r=>profileRow(
          open('rotations',r,[r.rotation_type_id?name('rotation_types',r.rotation_type_id):name('services',r.service_id),name('cohorts',r.cohort_id)].join(' · ')),
          r.person_id===p.id?(r.mentor_id?el('span',{},'Mentor ',who(r.mentor_id,name('people',r.mentor_id))):''):el('span',{},'Mentoring ',who(r.person_id,name('people',r.person_id))),
          dates(r),r.department_available?'':badge('Away'))))));
        if(attachments.length)sections.push(profileSection('Attachments','',el('ul',{class:'ph-rows'},...attachments.map(r=>{
          const other=r.person_id===p.id?r.mentor_id:r.person_id,scope=[(r.service_ids||[]).map((id:string)=>name('services',id)).join(', '),(r.assignment_type_ids||[]).map((id:string)=>name('assignment_types',id)).join(', ')].filter(Boolean).join(' · ');
          return profileRow(el('span',{},r.person_id===p.id?'With ':'Attached: ',who(other,name('people',other))),scope||'All clinics and duties',dates(r),r.strength==='required'?badge('Required','info'):'',
            manage?open('mentorships',r,'Edit'):'');}))));
        if(!calendar){  // Credentialers read the programme calendar from the catalogues they already hold.
          const memberships=(ctx.catalogues.programme_memberships||[]).filter(r=>r.person_id===p.id&&overlapping(r));
          const entries=(ctx.catalogues.programme_calendar_entries||[]).filter(r=>overlapping(r)&&memberships.some(m=>m.programme_id===r.programme_id&&m.starts_on<r.ends_on&&m.ends_on>r.starts_on));
          if(entries.length)sections.push(profileSection('Academic calendar','',el('ul',{class:'ph-rows'},...entries.map(r=>profileRow(r.name,name('programmes',r.programme_id),dates(r),'')))));
        }
      }
      out.replaceChildren(...(sections.length?sections:[empty('Nothing in these dates',calendar?'Published duties, time away, rotations and academic dates show here.':'Rotations, attachments and academic dates show here.',null,'calendar')]));
    }catch(e){if(mine===serial&&!sessionChanged(e))out.replaceChildren(failedWith('schedule',e,()=>void draw()));}
  };
  first.input.addEventListener('change',()=>void draw());last.input.addEventListener('change',()=>void draw());void draw();return box;
}

/** Changes to this person and their related records (not changes they made), newest first, 50 at a time. With
 *  `signIns` (who may read this person's sign-in activity), each sign-in, sign-out and ended session joins the feed at
 *  its time. */
function personHistory(ctx:Context,p:Row,feed:(events:Row[])=>Node,signIns:boolean):HTMLElement{
  const first=field('From','date'),last=field('Through','date');
  const range=dateRange('Dates',first,last);range.dataset.lookup='';
  const list=el('div',{'aria-live':'polite'},skeleton('lines','Loading history…')),pager=el('div',{class:'dt-pager'});
  let offset=0,auth:Promise<PersonAuthActivity>|null=null;
  const read=newest((path:string)=>get<{events:Row[];has_more:boolean}>(path));
  /** `from`: the pager button that asked; its replacement (or the other, at an end) keeps the focus. */
  const load=async(from=''):Promise<void>=>{
    const q=new URLSearchParams({person_id:p.id,people:'false',limit:'50',offset:String(offset)});
    if(first.input.value)q.set('from',first.input.value);if(last.input.value)q.set('to',last.input.value);
    list.setAttribute('aria-busy','true');
    try{const data=await read('/api/audit?'+q);if(!data)return;
      // A page holds the sign-ins between its oldest change (none older when it is the last page) and the newer page.
      // shortcut: the newest 100 sign-in events (the feed's page ceiling); page through auth-activity if older ones matter.
      if(signIns){auth??=absent(get<PersonAuthActivity>(personPath(p,'auth-activity',{limit:'100'})),{events:[]} as unknown as PersonAuthActivity).catch(e=>{auth=null;throw e;});
        const ev=data.events,hi=offset?String(ev[0]?.created_at||''):'￿',lo=data.has_more?String(ev.at(-1)?.created_at||''):'';
        const inDates=(t:string)=>(!first.input.value||dayOf(ctx,t)>=first.input.value)&&(!last.input.value||dayOf(ctx,t)<=last.input.value);
        // Worded as the feed words a device: "signed in on", "signed out on", "ended" (by an administrator, unless by themself).
        const added=(await auth).events.filter(r=>r.created_at>lo&&r.created_at<=hi&&inDates(r.created_at)).map(r=>({...r,action:AUTH_ACTION[r.action],
          actor_display_name:r.actor_id===r.detail.identity_id?fullNameOf(p):'An administrator',reason:[device(r.detail),r.detail.network_label].filter(Boolean).join(' · ')}));
        data.events=[...ev,...added].sort((a,b)=>String(b.created_at).localeCompare(String(a.created_at)));}
      list.replaceChildren(data.events.length?feed(data.events):empty(offset||first.input.value||last.input.value?'Nothing matches':'No changes yet',
        offset||first.input.value||last.input.value?'Try other dates.':'Changes to this person and their records show here.',null,'history'));
      // The pager shows only when there is more than one page.
      pager.replaceChildren(...(offset||data.has_more?[button('Newer',()=>{offset=Math.max(0,offset-50);return load('Newer');},'secondary small',{disabled:!offset,why:PAGE_WHY.first}),
        el('span',{class:'muted'},`${offset+1}–${offset+data.events.length}`),button('Older',()=>{offset+=50;return load('Older');},'secondary small',{disabled:!data.has_more,why:PAGE_WHY.last})]:[]));
      if(from&&(!document.activeElement||document.activeElement===document.body)){
        const next=[...pager.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];(next.find(b=>b.textContent===from)||next[0])?.focus({preventScroll:true});}
    }catch(e){if(!sessionChanged(e)){list.replaceChildren(failedWith('history',e,()=>load()));pager.replaceChildren();}}
    finally{list.removeAttribute('aria-busy');}
  };
  const restart=()=>{offset=0;void load();};
  first.input.addEventListener('change',restart);last.input.addEventListener('change',restart);void load();
  return el('div',{class:'ph'},range,list,pager);
}
