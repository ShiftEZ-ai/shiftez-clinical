import {brand} from './brand.js';
import {skeleton,swap} from './loading.js';
import {needRow,nameWords,el,grants,button,iconButton,badge,popover,menuItem,area,formDialog,notify,icon,statusBadge,empty,emptyLine,note,sectionHead,footerStatus,legend,formatDate,toLocal,formatTime,formatDay,plural,withIcon,colourOf,table,type Child,type Legend,type BadgeTone,needText,needCount,CAPABILITY} from './dom.js';
import {refused,ROLE_REFUSED,alive,readFailed} from './dom.js';  // a refused read is the role's (maintenance)
import {couldntLoad} from './dom.js';  // a failed read's words (DL-045)
import {rotaOutdated,statusLabel} from './dom.js';  // one rota verdict
import {openFiltered} from './table_view.js';
import {setupSteps,missingSetupWords,stepDone,setupCounts} from './setup_guide.js';
import {workspaceForm,type Context} from './forms.js';
import {upcoming,type Upcoming} from './agenda.js';
import {get,command,sessionChanged,ApiError,pageReadSignal} from './api.js';
import {summaryCheckWords} from './rule_check_api.js';
import {publishedAbsenceLabel,leaveDates,leaveShown,leaveTypes} from './findings.js';
import {fixSummaryOf,checkedAgo,couldWords} from './fix_list.js';
import type {Row} from './types.js';
import {teamView} from './team_view.js';
import {personPill} from './person_pill.js';
import {personLink,nameText,personName} from './person_label.js';
import {isClosed,addDays} from './planner_logic.js';
import {CONTENT_PAGE_SIZE,ABSENCE_REPORTER_CATEGORIES,ABSENCE_REPORT_LABEL,type PublishedOperationalAction,type DepartmentContentPage,type RequestListPage} from './generated.js';
import {canCreateRequest,factWords,requestTitle,requestWhen,requestStageWord,isAbsenceReport,withdrawReport,withdrawRequest} from './requests.js';
import {reportAbsence,absenceWords,absenceCategoryWord} from './published_changes_api.js';
import {SUPERSEDED} from './planning_context.js';
import {actsOn} from './schedule.js';  // D-162: only a rota this reader acts on shows its planner state
import {UNLINKED_STAFF,unlinkedStaff} from './terms.js';
import {openAmendment} from './published_amendment.js';
import {offerRow,offerAlertsAfterReport} from './pwa.js';
import {menu,opens} from './dom.js';  // the shared menu (UX › controls S2b)
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)

/** What Home needs from the app shell (state that lives in app.ts). */
export interface HomeHooks {
  /** Opens a Configuration list (admins) or its form. */
  openCatalogue:(kind:string)=>void;
  /** Plain words for an audit action (the audit log's own wording). */
  auditWords:(action:string,n:number)=>{chip:string;verb:string;what:string;colour:string};
  /** The self-approved decisions this reader may see (the department's for audit readers, else their own), in the audit log's words. */
  selfApproved:()=>void;
  /** The calendar subscription panel (Insights › Exports has the same one). */
  subscription:()=>Promise<HTMLElement>;
  /** False once another page render started (a late reply must not touch the new page's title). */
  allowed:(page:string)=>boolean;
  /** Drawn for a phone: the employee Home, whatever else the account does; Needs you keeps
   *  what is answered or decided in Requests, a person's own duty and a draft's health (its Fix list opens on a phone too, D10). */
  phone:boolean;
  /** A draft's Fix list: the Planner's panel on a desktop, the page on a phone (FIXLIST-UX). */
  fixList?:(rota:string)=>void;
}

interface Item {key:string;count:number;classification?:string;rota_id?:string;rota_name?:string;rotas?:number;checked_at?:string;
  /** A draft row (home.py): false while its check is out of date or running; its fix_list is then the last check's. */checked?:boolean;sample?:Row[];link_target?:string;fix_list?:{summary?:unknown};action?:PublishedOperationalAction|null;
  /** ME-28: the first rota to review amends a published one: its changes and their reasons (Sick, Leave…). */
  amendment?:{changes:number;reasons:string[]}}
interface HomeData {attention:Item[];rotas:number|null;my_requests:Row[];recent_changes:Row[];review_refresh_in_ms?:number}
/** One row of Needs you: a count pill (tone and number), a title, one muted line of context and one action. */
type Need={tone:BadgeTone;count:number;title:string;context:string;action:string;go:()=>void;to?:string;/** Shown on a phone too (a person's own published duty, a draft's blocking tasks and open places). */phone?:boolean;/** The server's key (a test and agent hook: data-need) */key?:string;
  /** G-02: several duties under one title, each line with its own action (then the row has none of its own). */items?:NeedItem[]};
type NeedItem={context:string;action:string;go:()=>void};

/** Whatever arrives while Home is still off-screen is kept; a visible skeleton settles first (loading.ts). */
const put=(host:HTMLElement,content:Node[])=>host.isConnected?swap(host,...content):host.replaceChildren(...content);
const link=(label:string,go:()=>void,attrs:Record<string,unknown>={})=>button(label,go,'',{class:'link-button',...attrs});

/** Home: what needs you, the rota months,
 *  today by clinic, your next duties, who is away and the department's notices. Counts come from GET /api/home
 *  (role-scoped COUNT/LIMIT reads); every other section reads one bounded window and loads, fails and retries alone. */
export async function homePage(ctx:Context,target:HTMLElement,hooks:HomeHooks){
  const me=ctx.me,zone=me.organisation.timezone,can=(...keys:string[])=>ctx.permits(...keys);
  greet(ctx);offline.clear();offlineError=null;homeRoot=target;
  // The counts (/api/home) and every card's own read start together (H-09): only Needs you and the setup card wait for
  // the counts. Leaving Home aborts them all (api.ts leavePage).
  const reading=get<HomeData>('/api/home',{fresh:can(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD)})
    .then(d=>d,(e:Error)=>e);
  const today=toLocal(new Date().toISOString(),zone).slice(0,10);
  const phone=hooks.phone;
  const team=!phone&&can(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.CREDENTIALER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.OPERATOR);
  const planner=!phone&&can(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD);
  const glance=!phone&&can(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD);
  // A coordinator's phone keeps Today (a short list) and Away today: who covers what now (B6, D10).
  const showToday=glance||phone&&can(CAPABILITY.COORDINATOR);

  // One primary action: a coordinator starts a rota; anyone who asks for time off starts a request.
  const primary=!phone&&can(CAPABILITY.COORDINATOR)?withIcon(button('New rota',()=>workspaceForm(ctx),'primary'),'plus')
    :canCreateRequest(ctx)&&hooks.allowed('requests')?(phone
      // A phone: one icon in the title row, in reading order before the bell, instead of a row of its own.
      ?iconButton('New request','plus',()=>ctx.navigate('requests?new=1'),{class:'primary phone-primary'})
      :withIcon(button('New request',()=>ctx.navigate('requests?new=1'),'primary'),'plus')):null;
  const head=sectionHead('Home','',...(primary?[primary]:[]));target.append(head);
  // A rota cannot be planned before duty types and places exist: say what is missing instead of offering New rota (H-20).
  // Duty types and locations are boot lists: no deferred list is read for this (S7).
  if(primary&&!phone&&can(CAPABILITY.COORDINATOR)){const gap=missingSetupWords(ctx);if(gap&&alive(primary))
    primary.replaceWith(el('span',{class:'muted home-missing'},gap+(can(CAPABILITY.ADMIN)?'':' · An admin sets these up')));}
  const status=footerStatus('');
  // One line: on a narrow footer it ends in "…" with the whole status as its tooltip. A card's own status (the rota a
  // planner drafts) says it even when the counts failed (H-14).
  let said=false;
  const say=(text:string)=>{if(text){said=true;status.replaceChildren(el('span',{class:'truncate',title:text},text));}};
  const main=el('div',{class:'home-main'}),side=el('div',{class:'home-side'});

  // Get the app, then Turn on alerts: offered on a phone until Not now, on this device only (P1–P3, D31).
  if(phone)main.append(offerRow());
  const duties=me.person_id&&hooks.allowed('schedule')?dutyPanels(ctx,hooks,!team,reading):null;
  if(duties?.hero)main.append(duties.hero);
  // Whoever watches the service keeps its health one click away, healthy or not.
  const needs=needsCard(ctx,hooks,phone,reading,!phone&&grants(me.roles,'system.operations')?link('Service health',()=>ctx.navigate('system/operations')):null,
    text=>{if(!said)status.replaceChildren(el('span',{class:'truncate',title:text},text));});
  main.append(needs.node);
  // A staff sign-in without a staff profile: say why no duties or requests show, and who links it (SCA-02); after Needs
  // you, never the page's first element (maintenance).
  if(unlinkedStaff(me))main.append(el('section',{class:'panel home-card home-unlinked'},empty(UNLINKED_STAFF.title,UNLINKED_STAFF.text,null,'lock')));
  void reading.then(data=>{
    if(data instanceof Error)return;
    // Setup counts read deferred lists (rules, opening hours): the card waits for them, never showing false gaps.
    if(!phone&&can(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER))void setupCounts(ctx).then(counts=>{if(!counts)return;const card=setupCard(ctx,counts,data.rotas,can(CAPABILITY.ADMIN),can(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),can(CAPABILITY.COORDINATOR),new Set(data.attention.map(i=>i.key)));if(!card)return;if(head.parentNode===target)head.after(card);else target.prepend(card);},()=>{});
  });
  if(planner)main.append(rotaStrip(ctx,today,say));
  if(showToday)main.append(todayTable(ctx,today,phone));
  // One useful card for the roles whose Home was nearly empty (UX review H-04).
  if(!phone&&can(CAPABILITY.CREDENTIALER))main.append(expiringCard(ctx,today));
  if(!phone&&can(CAPABILITY.APPROVER)&&!can(CAPABILITY.COORDINATOR)&&hooks.allowed('requests'))main.append(decidedCard(ctx));
  const recent=el('div',{class:'home-slot'}),mine=el('div',{class:'home-slot'});
  if(!phone)main.append(recent);
  void reading.then(data=>{if(data instanceof Error)return;
    if(!phone&&data.recent_changes.length)recent.replaceWith(recentChanges(data.recent_changes,hooks,zone,ctx));
    const waiting=data.my_requests.filter(r=>r.stage==='waiting'||r.stage==='reply');
    if(waiting.length)mine.replaceWith(myRequests(ctx,waiting,data.my_requests.length));});

  if(duties)side.append(duties.list);else if(me.person_id)side.append(calendarCard(hooks));
  side.append(mine);
  if(showToday)side.append(awayToday(ctx,today));
  // A notice to acknowledge is work for the reader: Needs you lists it too (H-18).
  side.append(noticesCard(pending=>needs.notices(pending)));

  const parts=[main,side].filter(p=>p.childElementCount);
  target.append(parts.length>1?el('div',{class:'home-grid'},...parts):parts[0]||empty('Nothing here yet','Your department has nothing for you today.',null,'grid'),status);
  // Cards read before Home's grid exists (H-09): failures that came first are said once it is drawn.
  const early=offlineError;offlineError=null;if(early)showOffline(early);
}

/** The greeting is the page title (top bar); the date and department sit beside it. */
function greet(ctx:Context){
  const me=ctx.me,zone=me.organisation.timezone,now=new Date();
  const hour=Number(new Intl.DateTimeFormat('en-GB',{hour:'numeric',hour12:false,timeZone:zone}).format(now));
  // An account name like "rota-coordinator" is not a first name.
  const words=nameWords(String(me.display_name||'')),first=[words.given[0]||words.family].filter(n=>/^\p{Lu}\p{L}*$/u.test(n)||/^[؀-ۿ]+$/.test(n))[0]||'';
  const title=document.querySelector('.topbar .page-title');
  if(!title)return;
  title.querySelector('h1')!.textContent=`Good ${hour<12?'morning':hour<18?'afternoon':'evening'}${first?', '+first:''}`;
  title.querySelector('.page-info')?.remove();title.querySelector('.page-sub')?.remove();
  title.append(el('span',{class:'page-sub'},new Intl.DateTimeFormat('en-GB',{weekday:'long',day:'numeric',month:'long',timeZone:zone}).format(now)+' · '+me.department.name));
}

/** Setup progress for those who can act on its next step (UX review H-02): an admin continues it; a coordinator or
 *  approver opens the next step when it is on their own pages (people, rules; announcements and the first rota for a
 *  coordinator). Anyone else sees no card: an admin's step is not their work. A step Needs you already asks for (clinics
 *  without opening hours) is not named again here (H-03); Continue setup still leads to it. */
function setupCard(ctx:Context,counts:Record<string,number>,rotas:number|null,admin:boolean,planner:boolean,coordinator:boolean,needs:Set<string>){
  const steps=setupSteps(ctx,ctx.setup||{catalogue:()=>{},page:()=>{}},rotas,counts).filter(s=>!s.optional),done=steps.filter(stepDone).length;
  if(done===steps.length)return null;
  const next=steps.find(s=>!stepDone(s))!,bar=el('span',{class:'setup-bar','aria-hidden':'true'},el('span',{}));
  // Catalogue steps carry `go` only for someone who may add them; People and Rules are a coordinator's and an approver's
  // pages; the first rota is the coordinator's New rota at the top of Home.
  // One line and the step's own button (H-07); the reasons stay in the Setup guide.
  const asked=next.id==='hours'&&needs.has('no_hours');
  // The first rota opens its form here too (on a phone the top bar has no New rota).
  const action=next.id==='rota'?(coordinator?button('New rota',()=>void workspaceForm(ctx),'secondary'):admin?button('Continue setup',()=>ctx.navigate('configuration/setup'),'secondary'):null)
    :!asked&&next.go&&(admin||next.kind||planner)?button(next.goLabel,next.go,'secondary'):admin?button('Continue setup',()=>ctx.navigate('configuration/setup'),'secondary'):null;
  if(action===null)return null;
  (bar.firstChild as HTMLElement).style.setProperty('width',Math.round(done/steps.length*100)+'%');
  return el('section',{class:'panel setup-card'},el('div',{},el('h2',{},`Setup ${done} of ${steps.length}`+(asked?'':` · Next: ${next.title.replace(/^Create your /,'').toLowerCase()}`)),bar),action);
}

/** A Home card: heading, a right-hand part (`aside`, filled by the loader when it depends on the data) and a body that
 *  loads alone — skeleton, then content, or the error with Retry. */
function card(cls:string,title:string,load:(aside:HTMLElement)=>Promise<Node[]>,aside:Child='',rendered?:(body:HTMLElement)=>void){
  const id=`home-${cls}-title`,right=el('span',{class:'home-aside'},aside),body=el('div',{class:'home-body'}),heading=el('h2',{id,tabindex:-1},title);
  const node=el('section',{class:`panel home-card home-${cls}`,'aria-labelledby':id},el('div',{class:'home-head'},heading,right),body);
  // A Retry keeps the reader's place (H-12): the heading once the card loaded, the new Retry if it failed again.
  const run=(retried=false)=>{body.replaceChildren(skeleton('lines',`Loading ${title.toLowerCase()}…`));
    load(right).then(async content=>{recovered(cls);await put(body,content);if(content.every(n=>n.parentNode===body))rendered?.(body);if(retried)heading.focus();},(e:Error)=>{if(sessionChanged(e))return;
      node.hidden=false;const again=loadFailed(title,e,retry,cls);void put(body,[again]);if(retried)again.querySelector('button')?.focus();});};
  const retry=()=>run(true);
  run();
  return node;
}
/** One failed card (H-13): "Couldn't load." with Retry inside the message; "Report this problem" only for a server fault.
 *  Two or more cards failing (a lost connection or a server fault) say so once, at the top of Home, with one Retry for
 *  all (N-10); each card's own note turns quiet. */
function loadFailed(title:string,e:Error,retry:()=>void,key:string){
  if(refused(e))return note(ROLE_REFUSED,'info');  // the role's, not a fault: no Retry, not counted offline (maintenance)
  const name=title.toLowerCase(),box=readFailed(name,e,retry,name);
  if(e instanceof ApiError&&e.code==='READ_TIMEOUT')return box;  // slow, not offline: the shared timeout
  if(!(e instanceof ApiError&&e.status>=500))box.querySelector('.notice-report')?.remove();  // only a server fault is reported
  box.dataset.offline='';offline.set(key,retry);showOffline(e);if(homeRoot?.querySelector('.home-offline'))quiet(box);
  return box;
}
/** Under the one page-wide line a card's own failure is said quietly: one alert, not one per card. */
const quiet=(n:HTMLElement)=>{n.classList.replace('error','info');n.setAttribute('role','note');n.querySelector('.notice-report')?.remove();};  // the line reports it
/** The cards whose read failed, by card: a card failing again replaces its own Retry. */
const offline=new Map<string,()=>void>();
let offlineError:Error|null=null;  // a second failure that came before Home's grid was drawn
// Home's own page: app.ts attaches it to #main only after its skeleton settles, so lookups never go through the document.
let homeRoot:HTMLElement|null=null;
/** A read that failed loaded again: once none is left failing, the page-wide line goes too. */
function recovered(key:string){offline.delete(key);if(!offline.size)homeRoot?.querySelector('.home-offline')?.remove();}
function showOffline(e:Error){
  const grid=homeRoot?.querySelector<HTMLElement>('.home-grid, .home-main');if(offline.size<2)return;
  if(!grid){offlineError=e;return;}
  homeRoot!.querySelectorAll<HTMLElement>('[data-offline].notice.error').forEach(quiet);  // the card that failed first
  if(homeRoot!.querySelector('.home-offline'))return;
  // A lost connection has nothing to report; a server fault keeps "Report this problem".
  const lost=e instanceof ApiError&&e.status===0,line=note(lost?'Can’t reach the server.':couldntLoad('parts of Home'),'error',lost?undefined:e);line.classList.add('home-offline');
  line.firstChild!.after(' ',button('Retry',()=>{line.remove();const all=[...offline.values()];offline.clear();all.forEach(run=>run());},'secondary small'));
  grid.before(line);
}

// ---- Needs you ----

/** The server's items in its priority order, worded; a draft's broken rules and open places are one row. */
export function needsOf(ctx:Context,hooks:HomeHooks,home:HomeData):Need[]{
  const zone=ctx.me.organisation.timezone,items=home.attention;
  const rota=(i:Item)=>()=>{ctx.setWorkspace(i.rota_id!);ctx.navigate('schedule');};
  const resolve=(i:Item)=>()=>hooks.fixList?hooks.fixList(i.rota_id!):ctx.navigate(`schedule/issues?draft=${encodeURIComponent(i.rota_id!)}`);
  const named=(i:Item)=>i.rota_name?i.rota_name+((i.rotas||1)>1?` and ${plural(i.rotas!-1,'other')}`:''):'';
  const span=(r:Row)=>leaveDates({type:'',starts_on:toLocal(r.start,zone).slice(0,10),ends_on:toLocal(new Date(Date.parse(r.end)-1).toISOString(),zone).slice(0,10)});
  // A day in another year says its year (SW3-C-01: "Tue 7 Sep" ten months away).
  const year=toLocal(new Date().toISOString(),zone).slice(0,4);
  const when=(r:Row)=>formatDay(r.start,zone,toLocal(String(r.start),zone).slice(0,4)!==year)+' '+formatTime(r.start,zone);
  const people=(filter:Record<string,string[]>)=>()=>{openFiltered('people',{...filter,status:['Active']});ctx.navigate('people');};
  const s=(n:number,one:string,many:string)=>n===1?one:many;
  const out:Need[]=[],done=new Set<Item>();
  // G-02 / G-05: one duty is one row with its action; several are one row listing each sampled duty with its own action,
  // and a last line for those past the sample (`rest`: where the whole list is).
  const each=(i:Item,row:Omit<Need,'count'|'context'|'action'|'go'|'items'>,item:(x:Row)=>NeedItem,rest:Omit<NeedItem,'context'>)=>{
    const list=(i.sample||[]).map(item),more=i.count-list.length;
    if(i.count===1&&list.length===1)return out.push({...row,count:1,...list[0],key:i.key});
    const items=[...list,...more>0?[{...rest,context:list.length?`${more} more`:plural(more,'duty','duties')}]:[]];
    out.push({...row,count:i.count,context:'',action:'',go:()=>{},items,key:i.key});};
  const enc=encodeURIComponent,dayOf=(x:Row)=>toLocal(String(x.start),zone).slice(0,10);
  for(const i of items){
    if(done.has(i))continue;
    const n=i.count,x=(i.sample||[])[0];
    const open=(action:string,go=rota(i))=>({action,go,context:named(i)});
    // The item's own address (home.py link_target, "#rules/<id>"), else its sample's when it names one, else `fallback`.
    const linked=(item:Item,fallback:string)=>()=>{const to=item.link_target||(item.sample||[])[0]?.link_target;ctx.navigate(to?String(to).replace(/^#/,''):fallback);};
    let row:Omit<Need,'count'>|null=null;
    switch(i.key){
      // One rule for a draft's row, checked or not (coordinator decision, 7 Nov 2026): its name, Fix list, and a phone
      // shows it too. `unchecked` (no current check and nothing must be fixed at the last) counts drafts, not causes.
      case 'blocking':case 'open_places':case 'unchecked':{
        // One row per draft (A2): its Fix list's must-fix causes, the number the footer, Submit and the review say too.
        const other=items.find(o=>o!==i&&(o.key==='blocking'||o.key==='open_places')&&o.rota_id===i.rota_id&&o.rotas===1&&i.rotas===1);
        if(other)done.add(other);
        const unchecked=i.key==='unchecked',fix=fixSummaryOf(i)||fixSummaryOf(other),must=fix?fix.must:unchecked?0:n;
        row={tone:must?'err':'neutral',title:named(i)||'Draft',action:'Fix list',go:resolve(i),
          // GEN-FIX: a draft's row stays while its check is out of date or running (checked: false), with the last
          // check's summary, so a phone always reaches its Fix list; it says so before those counts: a check in hand in
          // its own job's words (check_status, rule_check_api.ts summaryCheckWords), else "Needs a check".
          context:[summaryCheckWords(fix)||(i.checked===false||unchecked?'Needs a check':''),must?'Must fix before submitting':fix?.could?couldWords(fix.could):'',checkedAgo(fix?.checked_at||i.checked_at,'checked')].filter(Boolean).join(' · ')};
        // A phone shows it too, and its Fix list opens there (D10).
        out.push({...row,count:unchecked?n:must||fix?.could||0,key:i.key,phone:true});continue;
      }
      // Approved leave over a duty in a draft (home.py leave_conflicts): who, when, which rota. Replace opens that session's
      // replacement list for the person on leave (?replace= names their assignment, home.py); the rest, the Fix list.
      case 'leave_conflicts':{const who=(x:Row)=>String(x.assignment_id||x.person_id||'');
        each(i,{tone:'err',title:s(n,'Duty during approved leave','Duties during approved leave')},
          x=>({action:'Replace',context:[x.person,when(x),x.rota_name].filter(Boolean).join(' · '),
            go:()=>ctx.navigate(`schedule/week/${dayOf(x)}?edit=${enc(x.rota_id)}&shift=${enc(x.shift_id)}${who(x)?'&replace='+enc(who(x)):''}`)}),
          {action:'Fix list',go:()=>{const r=(i.sample||[]).at(-1)?.rota_id;if(r&&hooks.fixList)hooks.fixList(r);else ctx.navigate(r?`schedule/issues?draft=${enc(r)}`:'schedule');}});
        continue;}
      // An approved absence now overlaps an issued duty (published_operational.py): the publication and its approval stay
      // as issued; the duty still needs cover. A coordinator opens the existing amendment, or starts one of that
      // publication; everyone else sees the duty itself. Nothing is changed or cancelled from here.
      case 'published_absence_conflicts':{
        // SW-29: one cause, one row — a single overlap that is someone's approved "Can't make it" is that row (with its
        // Replace). Several are left to the server's counts (shortcut: a server-side merge when both lists are long).
        const same=(r:Row)=>!!x&&r.status==='approved'&&r.person_id===x.person_id&&Date.parse(r.start)===Date.parse(x.start);
        if(n===1&&items.some(o=>o.key==='absence_reported'&&(o.sample||[]).some(same)))continue;
        const a=i.action,own=!!x&&x.person_id===ctx.me.person_id&&!a,person=x&&!own?(ctx.catalogues.people||[]).find(p=>p.id===x.person_id)?.display_name:'';
        // SW3-C-01: Needs you holds what the reader can act on. Their own duty with no action for them is the
        // coordinator's to cover; the duty itself says so on the Schedule (its footer and session).
        if(own)continue;
        const day=x?toLocal(x.start,zone).slice(0,10):'';
        // Replace acts on that publication; Show opens the week as published now (a pinned ?source= is left on a superseded
        // version once an amendment is published, SW3-C-13).
        const show=()=>ctx.navigate(x?`schedule/week/${day}?${a?`source=${encodeURIComponent(x.publication_id)}&`:''}shift=${encodeURIComponent(x.shift_id)}`:'schedule');
        // An amendment already open is continued; otherwise Replace opens the published session's own Replace (B1, D13),
        // which a phone can finish (D10).
        const amend=a?.amendment_workspace_id?()=>openAmendment(a,path=>ctx.navigate(path)):null,replace=!!a&&!amend;
        row={tone:a?'err':'warn',title:publishedAbsenceLabel(n),
          action:amend?'Open amendment':replace?'Replace':'Show',go:amend||show,phone:replace,
          context:[person,x?when(x):'',n>1?`${n-1} more`:''].filter(Boolean).join(' · ')};break;}
      case 'failed_notifications':row={tone:'err',title:s(n,'Notification not sent','Notifications not sent'),action:'Open',go:()=>ctx.navigate('system/operations'),context:'Delivery failed'};break;
      case 'failed_runs':row={tone:'err',title:s(n,'Auto-schedule run failed','Auto-schedule runs failed'),action:'Open',go:()=>ctx.navigate('system/operations'),context:'In the last 24 hours'};break;
      case 'my_swaps':{const other=x?(x.proposer_id===ctx.me.person_id?x.recipient:x.proposer):'';
        row={tone:'info',title:s(n,'Swap offer for you','Swap offers for you'),action:'Answer',go:()=>ctx.navigate('requests?type=swap'),to:'requests',
          context:x?`${other||'A colleague'} · ${when(x)}`:'Waiting for your reply'};break;}
      // ME-28: an amendment says what it is: "Amendment to review · 1 change · sick · <rota>".
      case 'reviews':{const a=n===1?i.amendment:undefined;
        // D25 (maintenance): the review opens at once, on a phone too (Schedule ?approve=<rota>).
        row={tone:'warn',title:a?'Amendment to review':s(n,'Rota to review','Rotas to review'),...open('Review',()=>{ctx.setWorkspace(i.rota_id!);ctx.navigate('schedule?approve='+encodeURIComponent(i.rota_id!));}),phone:!!i.rota_id};
        if(a)row.context=[a.changes?plural(a.changes,'change'):'',a.reasons.map(r=>absenceWords(r)).filter(Boolean).join(', ').toLowerCase(),row.context].filter(Boolean).join(' · ');break;}
      case 'requests':{const leave=(i.sample||[]).every(r=>r.kind==='leave')&&n<=(i.sample||[]).length;
        row={tone:'warn',title:leave?s(n,'Leave request','Leave requests'):s(n,'Request to decide','Requests to decide'),action:'Review',go:()=>ctx.navigate('requests?status=waiting'),to:'requests',
          context:x?`${x.person} ${span(x)}`+(n>1?` · ${n-1} more`:''):'Waiting for your decision'};break;}  // one short line: the first and how many more
      case 'resubmit':row={tone:'warn',title:s(n,'Rota to resubmit','Rotas to resubmit'),...open('Open'),context:[named(i),'review out of date'].filter(Boolean).join(' · ')};break;
      case 'overlap':row={tone:'warn',title:s(n,'Overlapping rota','Overlapping rotas'),...open('Open')};break;
      // F7 (maintenance): a staff "Can't make it" reaches the coordinator at once; pending is said, approved asks for cover.
      // Approved: Replace opens on that duty (replace_action); still waiting: its week, the session marked. Each report is its own line (G-02).
      case 'absence_reported':
        each(i,{tone:'warn',title:ABSENCE_REPORT_LABEL,phone:true},x=>{const a=x.replace_action as {shift_id?:string;assignment_id?:string}|undefined,
          replace=!!a?.shift_id&&!!a.assignment_id&&x.status==='approved';
          return {action:replace?'Replace':'Open',context:[x.person,when(x),x.status==='approved'?'needs cover':'waiting for approval'].filter(Boolean).join(' · '),
            go:()=>ctx.navigate(`schedule/week/${dayOf(x)}?edit=none${a?.shift_id?'&shift='+enc(a.shift_id):''}${replace?'&replace='+enc(a!.assignment_id!):''}`)};},
          {action:'Show',go:()=>ctx.navigate('requests?type=other')});
        continue;
      case 'patterns':row={tone:'warn',title:'Weekly availability',action:'Review',go:()=>ctx.navigate('requests?status=waiting'),to:'requests',context:'Waiting for approval'};break;
      case 'swaps_to_apply':row={tone:'info',title:s(n,'Swap to apply','Swaps to apply'),action:'Review',go:()=>ctx.navigate('requests?type=swap'),to:'requests',
        context:x?`${x.proposer||'Someone'} ↔ ${x.recipient||'someone'} · ${when(x)}`:'Agreed by both'};break;
      case 'no_eligibility':row={tone:'warn',title:s(n,'Person without clinics','People without clinics'),action:'Show',go:people({services:['No current eligibility']}),context:'Today · no clinic to be assigned to'};break;
      case 'no_rank':row={tone:'warn',title:s(n,'Person without a rank','People without a rank'),action:'Show',go:people({rank:['No current rank']}),context:'Today · not for duties open only to some ranks'};break;
      case 'no_hours':row={tone:'warn',title:s(n,'Clinic without opening hours','Clinics without opening hours'),action:'Set hours',go:()=>hooks.openCatalogue('services'),context:'Sessions cannot be planned there'};break;
      case 'feedback':row={tone:'warn',title:s(n,'New feedback report','New feedback reports'),action:'Triage',go:()=>ctx.navigate('system/feedback'),context:`Sent from inside ${brand().short_name}`};break;
      case 'to_publish':row={tone:'ok',title:'Ready to publish',...open('Open')};break;
      // SW3-B-04 (maintenance home.py `returned`): a rota or amendment the approver returned, with who and why; Open lands on it
      // (its banner and the returned changes are there).
      case 'returned':{const id=String(x?.rota_id||i.rota_id||'');
        row={tone:'warn',title:s(n,'Returned to you','Rotas returned to you'),action:'Open',go:()=>{if(id)ctx.setWorkspace(id);ctx.navigate('schedule');},
          context:x?[x.rota_name,x.by,x.reason?`“${x.reason}”`:''].filter(Boolean).join(' · ')+(n>1?` · ${n-1} more`:''):named(i)};break;}
      // The server's link_target opens the proposal or the exceptions waiting (maintenance); without it, the Rules page.
      case 'rules':row={tone:'duty-violet',title:s(n,'Rule change','Rule changes'),action:'Review',go:linked(i,'rules'),
        context:x?[x.name,x.by?'proposed by '+x.by:'',n>1?`${n-1} more`:''].filter(Boolean).join(' · '):'Waiting for approval'};break;
      case 'scheduling_facts':row={tone:'duty-violet',title:s(n,'Person fact to approve','Person facts to approve'),action:'Review',
        go:()=>ctx.navigate(n===1&&x?.link_target?String(x.link_target).slice(1):'requests?status=waiting'),to:'requests',
        context:x?[x.person_name||x.calendar_entry_name,requestTitle(x),...factWords(x),requestWhen(x,zone),'proposed by '+x.proposer_name].filter(Boolean).join(' · ')+(n>1?` · ${n-1} more`:''):'Waiting for approval'};break;
      // The first waiting request (its id is in the server's link): who, which rule and its dates, when the catalogues
      // this reader may see have them; otherwise only that it waits.
      case 'exceptions':{const id=new URLSearchParams(String(i.link_target||'').split('?')[1]||'').get('exception'),e=id?(ctx.catalogues.exceptions||[]).find(r=>r.id===id):undefined;
        const label=(kind:string,rid:string)=>String((ctx.catalogues[kind]||[]).find(r=>r.id===rid)?.[kind==='people'?'display_name':'name']||'');
        row={tone:'duty-violet',title:s(n,'Rule exception','Rule exceptions'),action:'Review',go:linked(i,'rules'),
          context:e?[label('people',e.person_id),label('policies',e.policy_id),leaveDates({type:'',starts_on:e.starts_on,ends_on:addDays(e.ends_on,-1)}),n>1?`${n-1} more`:''].filter(Boolean).join(' · '):'Waiting for approval'};break;}
      case 'ready_to_submit':row={tone:'draft',title:'Ready to submit',...open('Open')};break;
      case 'in_review':row={tone:'info',title:'In review',...open('Open')};break;
      case 'rotations_soon':row={tone:'info',title:s(n,'Rotation changing','Rotations changing'),action:'Open',go:()=>ctx.navigate('programmes'),context:'Starts or ends in the next 2 weeks'};break;
      case 'self_approved':row={tone:'info',title:s(n,'Self-approved decision','Self-approved decisions'),action:'Open',go:hooks.selfApproved,context:'In the last 30 days'};break;
    }
    if(row)out.push({...row,count:n,key:i.key});  // an unknown key (an older client) is skipped
  }
  return out;
}

/** On a phone: what is answered or decided in Requests (the approvals list), and the rows marked `phone`; the rest
 *  waits on a desktop (and still counts there). */
export const onPhone=(n:Need)=>n.to==='requests'||!!n.phone;
/** Needs you: the server's items (/api/home) and the notices waiting for this reader's acknowledgement (H-18), never
 *  "All clear" while one waits. Its Retry reads /api/home again, nothing else (H-13). No count in the head or the footer: the
 *  rows are there (D-021). */
function needsCard(ctx:Context,hooks:HomeHooks,phone:boolean,reading:Promise<HomeData|Error>,extra:HTMLElement|null,status:(text:string)=>void){
  const heading=el('h2',{id:'home-needs-title',tabindex:-1},'Needs you'),body=el('div',{class:'home-body'});
  const node=el('section',{class:'panel home-card home-needs','aria-labelledby':'home-needs-title'},el('div',{class:'home-head'},heading,el('span',{class:'home-aside home-links'},extra||'')),body);
  // away: items a phone leaves to a desktop; they still count, so a phone never says All clear while one waits.
  let rows:Need[]|null=null,acks:Need[]=[],away:Need[]=[];
  const draw=()=>{if(!rows)return;const all=[...acks,...rows];
    // D-021 (ruling 10 Nov, reverses H-05): the rows and "N items need a desktop" are on the card; the footer never
    // repeats their count, nor All clear (ME-17).
    // maintenance: it names them ("1 item needs a desktop: Rule change"), so a phone never hides what waits behind a count.
    const elsewhere=away.length?el('p',{class:'muted home-elsewhere'},`${plural(away.length,all.length?'more item':'item')} need${away.length===1?'s':''} a desktop: ${away.map(n=>n.title).join(', ')}`):null;
    // Drawn at once (not faded in): the counts and the notices both redraw it and must not race (loading.ts swap).
    body.replaceChildren(...[all.length?el('ul',{class:'home-needs-list'},...all.map((n,k)=>{const id=`home-need-${k}`;
      // The count pill is its number; its row's title says what it counts (its tone is the one status palette).
      return needRow((b=>{b.dataset.lgSelf='';return b;})(needCount(n.count,n.tone)),
        needText(n.title,n.context,{id,cls:'truncate',tip:n.context}),
        n.items?el('ul',{class:'home-need-items'},...n.items.map((x,j)=>el('li',{},el('small',{class:'truncate',id:`${id}-${j}`,title:x.context},x.context),
          button(x.action,x.go,'secondary small',{'aria-describedby':`${id} ${id}-${j}`})))):button(n.action,n.go,'secondary small',n.action==='Review'
          ? {'aria-label':[n.action,n.title,n.context].filter(Boolean).join(', ')} : {'aria-describedby':id}),
        {tag:'li',cls:'',attrs:{'data-need':n.key||''}});})):elsewhere?'':el('p',{class:'home-clear'},'All clear ',el('span',{'aria-hidden':'true'},'✓')),elsewhere||'']);};
  // A delegation boundary can change Needs you without a write or a live notification.
  const lifetime=pageReadSignal();
  let reviewTimer:number|undefined;
  lifetime.addEventListener('abort',()=>clearTimeout(reviewTimer),{once:true});
  const take=(data:HomeData|Error,retried=false)=>{
    clearTimeout(reviewTimer);
    if(lifetime.aborted||!alive(node))return;
    if(data instanceof Error){if(sessionChanged(data))return;status('What needs you is unavailable');
      const again=()=>read(true);
      const failed=loadFailed('what needs you',data,again,'needs');
      void put(body,[failed]);if(retried)failed.querySelector('button')?.focus();return;}
    recovered('needs');const every=needsOf(ctx,hooks,data);rows=every.filter(n=>!phone||onPhone(n));away=every.filter(n=>phone&&!onPhone(n));draw();if(retried)heading.focus();
    if(typeof data.review_refresh_in_ms==='number'){
      const delay=Math.min(Math.max(1,data.review_refresh_in_ms),2**31-1);
      reviewTimer=window.setTimeout(()=>{if(node.isConnected&&!lifetime.aborted)read(false,true);},delay);
    }};
  // A failure's Retry (and a timeout's: api.ts READ_TIMEOUT) reads /api/home again.
  const read=(retried:boolean,quiet=false)=>{
    if(!quiet)void put(body,[skeleton('lines','Loading what needs you…')]);
    void get<HomeData>('/api/home',{fresh:true,quiet}).then(d=>d,(e:Error)=>e).then(d=>take(d,retried));};
  body.append(skeleton('lines','Loading what needs you…'));void reading.then(d=>take(d));
  return {node,notices:(pending:Row[])=>{acks=pending.map(n=>({key:'acknowledge',tone:'warn',count:1,title:'Notice to acknowledge',context:String(n.name||''),action:'Read',
    go:()=>{const at=document.querySelector<HTMLElement>(`#main [data-notice-id="${CSS.escape(String(n.id))}"]`);at?.scrollIntoView({block:'center'});at?.querySelector<HTMLElement>('.home-notice-ack')?.focus();}}));draw();}};
}

// ---- Rota: the months around today ----

// Three letters ("Sep", not en-GB's "Sept"): four months side by side on a phone.
const monthName=(day:string,style:'short'|'long'='short')=>{const n=new Intl.DateTimeFormat('en-GB',{month:style,timeZone:'UTC'}).format(new Date(day+'T12:00:00Z'));return style==='short'?n.slice(0,3):n;};
const shortDay=(day:string)=>new Intl.DateTimeFormat('en-GB',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(day+'T12:00:00Z')).replace('Sept','Sep');
/** First day of the month `n` months from `day`'s. */
const monthStart=(day:string,n:number)=>{const d=new Date(day.slice(0,7)+'-01T12:00:00Z');d.setUTCMonth(d.getUTCMonth()+n);return d.toISOString().slice(0,10);};

/** Last month, this month and the next two: the days published (teal, where they fall, named as date ranges), the
 *  rota being planned (gold, its sessions filled) or not started yet (Start opens New draft for that month). From the
 *  live and published rotas of those months (one bounded read). */
function rotaStrip(ctx:Context,today:string,say:(text:string)=>void){
  const coordinator=ctx.permits(CAPABILITY.COORDINATOR);
  return card('rota','Rota',async()=>{
    const first=monthStart(today,-1),after=monthStart(today,3);
    const gap=coordinator?missingSetupWords(ctx):'';
    // The Home timeline read (home.py rota_strip): dates, health and, for a coordinator, a draft's waiting proposal.
    const rotas:Row[]=(await get<{workspaces:Row[]}>('/api/home/rotas?'+new URLSearchParams({start:first,end:after,limit:'100'}))).workspaces;
    const published=rotas.filter(w=>w.status==='published'),live=rotas.filter(w=>w.status!=='published').sort((a,b)=>a.starts_on.localeCompare(b.starts_on));
    const covers=(day:string)=>published.find(w=>w.starts_on<=day&&day<w.ends_on);
    // Places filled of places needed (the rota summary's units), from a check that is still current.
    // A draft with no places to fill has no percentage (it is not "100% filled").
    const filled=(w:Row)=>{const s=w.health?.fresh&&w.health.summary;return s&&s.places_needed?Math.floor(s.places_filled/s.places_needed*100):null;};
    // Never "no places to fill" over a draft whose check found open places or sessions that need staff (H-01).
    // Places (headcount) and sessions needing staff (e.g. a missing lead) are different units, each worded as itself.
    const NO_MINIMUM='no staffing minimum set';
    const noPercent=(w:Row)=>{const s=w.health?.fresh&&w.health.summary;if(!s)return w.health?.checked?'needs a check':'not checked';
      const places=Number(s.places_open||0),sessions=Number(s.sessions_with_gaps||0);
      // No minimum configured anywhere is not "complete": the draft says no minimum is set (CA-04, HOME-02).
      return places?`${places} open`:sessions?`${plural(sessions,'session')} with open places`:NO_MINIMUM;};
    // No rota at all yet (a new department): one line instead of four empty months (H-25).
    if(!rotas.length)return [emptyLine('No rota yet.',null,'home-none')];
    let footerDraft='',amendment='';
    const months=[0,1,2,3].map(k=>{
      const start=monthStart(today,k-1),end=monthStart(today,k),length=Math.round((Date.parse(end)-Date.parse(start))/864e5);
      // The published days as ranges ([first, last] included): a gap or a late start shows where it is.
      const ranges:[string,string][]=[];
      for(let d=start;d<end;d=addDays(d,1))if(covers(d)){const r=ranges.at(-1);if(r&&r[1]===addDays(d,-1))r[1]=d;else ranges.push([d,d]);}
      const shown=ranges.reduce((n,[a,b])=>n+(Date.parse(b)-Date.parse(a))/864e5+1,0);
      // Several drafts over one month: the first is named with how many others (H-03); Open leads to the draft picker.
      // D-162: only a rota this reader acts on shows its fill, check and Open; another reader sees only that one is in work.
      const inWork=live.filter(w=>w.starts_on<end&&w.ends_on>start),drafts=inWork.filter(w=>actsOn(w.status,ctx.me.roles)),draft=drafts[0],pct=draft?filled(draft):null;
      const at=(day:string)=>`${(Date.parse(day)-Date.parse(start))/864e5/length*100}%`;
      const fill=(from:string,width:string,cls:string)=>{const f=el('span',{class:'fill '+cls});f.style.setProperty('--from',from);f.style.setProperty('--fill',width);return f;};
      // A month already published shows its published days (a draft over it, e.g. an amendment, is named beside them).
      // A draft whose share filled is unknown is a hatched gold bar, never the empty bar of "Not started" (H-03).
      const bar=el('span',{class:'home-bar','aria-hidden':'true'},...(draft&&!shown?[pct===null?fill('0%','100%','draft unknown'):fill('0%',`${pct}%`,'draft')]
        :ranges.map(([a,b])=>fill(at(a),`${((Date.parse(b)-Date.parse(a))/864e5+1)/length*100}%`,'published'))));
      if(start<=today&&today<end){const mark=el('span',{class:'home-now',title:'Today, '+shortDay(today)});mark.style.setProperty('--at',`${((Date.parse(today)-Date.parse(start))/864e5+.5)/length*100}%`);bar.append(mark);}
      let word:string,action:HTMLElement|string='',state:HTMLElement|string='';
      const pubWord=shown===length?'Published':shown?'Published '+ranges.map(([a,b])=>leaveDates({type:'',starts_on:a,ends_on:b})).join(', '):'';
      const stateWord=(w:Row)=>w.status==='draft'?(pubWord?'draft':'Draft'):rotaOutdated(w)?statusLabel('outdated'):w.status==='submitted'?'In review':'Approved';  // the list's server verdict
      if(draft){
        word=[pubWord,stateWord(draft),String(draft.name||'')+(drafts.length>1?` +${plural(drafts.length-1,'draft')}`:''),
          pct!==null?`${pct}% filled`:coordinator&&!(draft.health?.fresh&&draft.health.summary)?'':noPercent(draft)].filter(Boolean).join(' · ');
        // Open lands on this month with its named draft in edit (the Schedule's draft switcher holds the others), never on
        // whichever month the Schedule showed last (review maintenance item 11).
        // A waiting auto-schedule proposal (the server's: current, not applied or discarded) is said here and Review opens
        // it on that draft (Auto-schedule › Review); the run stays the server's to check again.
        // A proposal a newer run replaced is not ready: its state (the shared badge) and Review opens the latest run.
        const replaced=draft.status==='draft'&&draft.proposal?.currency==='superseded',ready=draft.status==='draft'&&!!draft.proposal&&!replaced;
        const open=()=>{ctx.setWorkspace(draft.id);ctx.navigate(`schedule/month/${start}?edit=${encodeURIComponent(draft.id)}${ready||replaced?'&review=proposal':''}`);};
        if(ready)word+=' · proposal ready';
        if(replaced)state=badge(SUPERSEDED.state);
        action=link(replaced?SUPERSEDED.next:ready?'Review':'Open',open,{'aria-label':replaced?`${SUPERSEDED.next} for ${draft.name}`:ready?`Review the auto-schedule proposal for ${draft.name}`:drafts.length>1?`Open the ${monthName(start,'long')} drafts`:`Open ${draft.name}`});
        // The footer speaks of this month's rota or the next one, never a past month's draft.
        // F-03: the rota being planned, not an amendment of a published month (said only when nothing else is planned).
        // D-161: the footer keeps the rota's status; only "no staffing minimum set" is said once, in Needs you.
        const np=pct===null?noPercent(draft):'',tail=pct!==null?pct+'%':np&&np!==NO_MINIMUM?'· '+np:'';
        const words=[monthName(start,'long'),draft.status==='draft'?'draft':'rota',tail].filter(Boolean).join(' ');
        if(end>today){if(draft.base_publication_id)amendment||=words;else footerDraft||=words;}
      }else if(inWork.length)word=[pubWord,stateWord(inWork[0])].filter(Boolean).join(' · ');
      else if(shown)word=pubWord;
      // A past month says "Not published" only when a later month was published (else nothing was ever published: H-25).
      else{word=end<=today?(published.some(w=>w.ends_on>end)?'Not published':'No rota'):'Not started';
        if(coordinator&&end>today&&!gap)action=link('Start',()=>workspaceForm(ctx,undefined,{name:`${monthName(start,'long')} rota`,starts_on:start,ends_on:end}),
          {'aria-label':`Start the ${monthName(start,'long')} rota`});}
      return el('div',{class:'home-month','data-month':start.slice(0,7)},el('div',{class:'home-month-head'},el('strong',{},monthName(start)),el('span',{class:'muted home-month-state'},...word.split(' · ').flatMap((v,i)=>[i?' · ':'',el('span',{},v)])),action),state,bar);
    });
    // Only whoever plans the draft hears its status (D-099); the others read the months above. No dates in the footer
    // reviewed: the months show them.
    if(coordinator)say(footerDraft||amendment);
    return [el('div',{class:'home-months'},...months)];
  });
}

// ---- Today by clinic ----

/** Today's published sessions, including any still running from yesterday: a row per clinic, a column per session time
/** Today's published sessions, including any still running from yesterday: a row per clinic, a column per session time
 *  as the rota sets it ("08:30–12:00"; one that began yesterday names its day), each cell "N on duty" (owner decision
 *  D14: no ratios), with open places as a dashed "1 open" chip only when it is short; a gap that is not headcount
 *  (a lead, a skill) is named by the check's own finding.
 *  Names, with their TL and OT tags, come behind Show people. On a phone (B6) a short list: every short session, then
 *  what is on now or next, then how many other clinics are all filled. One read of /api/schedule/sessions for today. */
function todayTable(ctx:Context,today:string,phone=false){
  const zone=ctx.me.organisation.timezone;
  return card('today','Today',async()=>{
    const data=await get<Row>('/api/schedule/sessions?'+new URLSearchParams({start:today,end:addDays(today,1)}));
    const shifts=(data.shifts as Row[]).filter(s=>!isClosed(s)).sort((a,b)=>a.start.localeCompare(b.start)||a.end.localeCompare(b.end));
    if(!shifts.length)return [emptyLine('No published sessions today.',null,'home-none')];
    const key=legend('Tags');
    const name=(id:string)=>data.people[id]?.display_name||'Someone';
    const clinic=(s:Row)=>(s.service_ids as string[]).map(id=>data.services[id]?.name).filter(Boolean).join(' + ')||data.locations[s.location_id]?.name||s.type_snapshot.name;
    const weekday=(iso:string)=>new Intl.DateTimeFormat('en-GB',{weekday:'short',timeZone:zone}).format(new Date(iso));
    const part=(s:Row)=>(toLocal(s.start,zone).slice(0,10)<today?weekday(s.start)+' ':'')+`${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`;
    // Open places are headcount short of the minimum; a gap with enough people (a missing lead or skill) is named by the
    // check's first blocking finding on that session, never counted as places.
    const open=new Map<string,number>(),named=new Map<string,string>();
    for(const c of data.coverage as Row[])if(c.status==='gap'){const short=(Number(c.required)||0)-(Number(c.scheduled)||0);
      if(short>0)open.set(c.shift_id,Math.max(open.get(c.shift_id)||0,short));else named.set(c.shift_id,'');}
    for(const f of (data.findings||[]) as Row[])if(named.get(f.shift_id)===''&&f.severity==='error'&&f.message)named.set(f.shift_id,String(f.message));
    const columns=[...new Set(shifts.map(part))];  // in start order (shifts are sorted)
    const rows=new Map<string,Row[][]>();
    for(const s of shifts){const r=rows.get(clinic(s))||columns.map(()=>[]);r[columns.indexOf(part(s))].push(s);rows.set(clinic(s),r);}
    const where=(s:Row)=>`${s.type_snapshot.name} · ${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`;
    // The server's staffing per session (maintenance: on_duty, places_open); before it, the stored check's coverage.
    const short=(s:Row)=>Number(s.staffing?s.staffing.places_open:open.get(s.id)||0);
    const openChip=(s:Row)=>short(s)>0?el('span',{class:'rp open',title:where(s)},`${short(s)} open`)
      :named.has(s.id)?el('span',{class:'rp open',title:`${where(s)} · ${named.get(s.id)||'Open place'}`},named.get(s.id)||'Open place'):null;
    const heads=(list:Row[])=>list.reduce((n,s)=>n+Number(s.staffing?.on_duty??(s.participants as Row[]).length),0);
    // A short figure for the eye, its words for a screen reader (a span's aria-label is not reliably read).
    const said=(cls:string,shown:string,words:string)=>el('span',{class:cls,title:words},el('span',{'aria-hidden':'true'},shown),el('span',{class:'sr-only'},words));
    const isShort=(s:Row)=>short(s)>0||named.has(s.id);
    const count=(list:Row[])=>{if(!list.length)return el('span',{class:'muted'},'—');const on=heads(list);
      return el('div',{class:'home-chips'},...list.map(openChip).filter((x):x is HTMLSpanElement=>!!x),said('home-filled mono',String(on),`${on} on duty`));};
    if(phone){const now=new Date().toISOString();
      const groups=[...rows].flatMap(([name,cells])=>cells.filter(l=>l.length).map(list=>({name,list,start:String(list[0].start),end:String(list[0].end),
        open:list.reduce((n,s)=>n+short(s),0),short:list.some(isShort)})));
      const gaps=groups.filter(g=>g.short&&g.end>now),shown=[...gaps,...groups.filter(g=>!g.short&&g.end>now).sort((a,b)=>a.start.localeCompare(b.start))].slice(0,Math.max(3,gaps.length));
      const rest=new Set(groups.filter(g=>!shown.some(x=>x.name===g.name)).map(g=>g.name)).size;
      return [el('ul',{class:'home-duty-list home-today-list'},...shown.map(g=>el('li',{},el('span',{class:'home-clinic',title:g.name},g.name),el('span',{class:'mono muted'},part(g.list[0]).split('–')[0]),
        g.open?el('span',{class:'rp open'},`${g.open} open`):g.short?openChip(g.list.find(isShort)!)!:el('span',{class:'muted'},`${heads(g.list)} on duty`))),
        rest?el('li',{class:'muted home-today-more'},`${plural(rest,'more clinic')} · all filled`):null)];}
    const people=(list:Row[])=>list.length?el('div',{class:'home-chips'},...list.flatMap(s=>[
      ...(s.participants as Row[]).slice().sort((a,b)=>name(a.person_id).localeCompare(name(b.person_id))).map(p=>personChip(p.person_id,data.people[p.person_id]?.display_name||'',p,key,`${formatTime(s.start,zone)}–${formatTime(s.end,zone)} · ${s.type_snapshot.name}`)),
      openChip(s)])):el('span',{class:'muted'},'—');
    const sorted=[...rows].sort(([a],[b])=>a.localeCompare(b)),head=()=>['Clinic',...columns.map(c=>el('span',{class:'mono'},c))];
    const grid=(fill:(list:Row[])=>HTMLElement)=>table(head(),sorted.map(([name,cells])=>[el('span',{class:'rp place',title:name},name),...cells.map(fill)]));
    // The names are built when first shown: a department-scale day stays a small table until someone asks.
    const names=el('div',{class:'home-today-people',id:'home-today-people',hidden:true});
    const show=button('Show people',()=>{const on=names.hidden;if(on&&!names.childElementCount)names.append(grid(people),key.root);
      names.hidden=!on;show.setAttribute('aria-expanded',String(on));show.textContent=on?'Hide people':'Show people';},'',{class:'link-button','aria-expanded':'false','aria-controls':'home-today-people'});
    return [grid(count),el('div',{class:'home-today-tools'},show),names];
  },phone?'':link('Open in Schedule →',()=>ctx.navigate(`schedule/day/${today}`)));
}

/** A person on duty: the department's label, the full name with the duty in its tip and for screen readers. */
const personChip=(id:string,known:string,p:Row,key:Legend,tip:string)=>el('span',{class:'home-person'},(n=>{n.dataset.tip=`${known||'Someone'} · ${tip}`;return n;})(nameText(id,known,{class:'rp person',missing:'Someone'})),
  p.team_lead?key.tag('lead'):null,p.overtime?key.tag('ot'):null);

// ---- My next duties (Coming up) and the staff hero ----

/** The person's next duties from one read: staff get the next one as the hero card and up to eight after it as Coming up;
 *  planners get My next duties. No footer status: the hero says it (D-111). */
function dutyPanels(ctx:Context,hooks:HomeHooks,staff:boolean,reading:Promise<HomeData|Error>){
  const zone=ctx.me.organisation.timezone;
  const day=(iso:string)=>formatDay(iso,zone);
  const time=(e:Row)=>`${formatTime(e.start,zone)}–${formatTime(e.end,zone)}`;
  const clinic=(e:Row)=>(e.services as Row[]||[]).map(s=>s.name).join(' + ')||e.location_name||e.label;
  const hero=staff?el('section',{class:'panel home-hero','aria-labelledby':'home-hero-title'}):null;
  if(hero)hero.hidden=true;
  // Add to calendar sits on the hero when there is one, else beside All.
  const subscribe=link('Add to calendar',()=>subscribeAfter(list,subscribe,hooks),{'aria-expanded':'false'});
  const list:HTMLElement=card('duties',staff?'Coming up':'My next duties',async()=>{
    const {duties,pairs}:Upcoming=await upcoming(ctx);
    const next=duties[0];
    if(hero&&next){drawHero(ctx,hooks,hero,next,pairs,day(next.start),time(next),clinic(next),reading);subscribe.hidden=true;}
    else if(!next)subscribe.hidden=true;  // L18: nothing to add to a calendar yet
    const rest=(staff?duties.slice(1):duties).slice(0,8),key=legend('Tags');  // Coming up takes the column's height (LIVE-53)
    if(!rest.length)return [emptyLine(staff&&next?'Nothing else in the next two weeks.':'You have no duties coming up.',null,'home-none')];
    // SW-05: every duty still ahead has the hero's actions behind its ⋯ (one swap-recipient read for the whole list).
    const offerable=staff?canOffer():null;
    return [el('ul',{class:'home-duty-list'},...rest.map(e=>el('li',{},el('span',{class:'home-day'},day(e.start)),el('span',{class:'mono muted'},time(e)),
      el('span',{class:'home-clinic',title:[e.label,e.location_name].filter(Boolean).join(' · ')},clinic(e)),e.team_lead?key.tag('lead'):null,e.overtime?key.tag('ot'):null,
      offerable?dutyMenu(ctx,e,clinic(e),offerable,reading):null))),key.root];
  },el('span',{class:'home-links'},subscribe,
    // Staff-only accounts reach Insights (their hours and fairness) from here, not the sidebar.
    staff&&hooks.allowed('insights')?link('My hours',()=>ctx.navigate('insights'),{'aria-label':'My hours & fairness'}):null,
    link('All',()=>ctx.navigate('schedule'),{'aria-label':'All my duties'})));
  return {hero,list};
}

/** The calendar subscription panel (shared with Insights › Exports) opens below `anchor`; its button stays and closes
 *  it again (LIVE-52). */
async function subscribeAfter(anchor:HTMLElement,trigger:HTMLElement,hooks:HomeHooks){
  const open=anchor.nextElementSibling?.matches('.subscription')?anchor.nextElementSibling:null;
  if(open){open.remove();trigger.setAttribute('aria-expanded','false');return;}
  anchor.after(await hooks.subscription());trigger.setAttribute('aria-expanded','true');}

/** A linked person who cannot open the Schedule still subscribes to their duties from Home. */
function calendarCard(hooks:HomeHooks){
  const node=el('section',{class:'panel home-card','aria-labelledby':'home-calendar-title'},el('div',{class:'home-head'},el('h2',{id:'home-calendar-title'},'My calendar')));
  const add=button('Add to calendar',()=>subscribeAfter(node,add,hooks),'secondary small',{'aria-expanded':'false'});
  node.append(add);
  return node;
}

function drawHero(ctx:Context,hooks:HomeHooks,hero:HTMLElement,e:Row,pairs:Record<string,string>,day:string,time:string,clinic:string,reading:Promise<HomeData|Error>){
  const zone=ctx.me.organisation.timezone,today=toLocal(new Date().toISOString(),zone).slice(0,10);
  const days=Math.round((Date.parse(toLocal(e.start,zone).slice(0,10))-Date.parse(today))/864e5);
  const when=Date.parse(e.start)<=Date.now()?'On duty now':`Next duty · ${days===0?'today':days===1?'tomorrow':`in ${days} days`}`;
  // The duty's supervision team as the Schedule draws it (team_view.ts): you with your supervisor, or with your trainees.
  // Both at once (rare) stays in words.
  const me=ctx.me.person_id||'',kids=(e.trainee_ids||[]) as string[],sup=e.supervisor_person_id as string|undefined;
  // The server's dated ranks for you and your pairs (Home duties carry `rank` and `pair_ranks`), and whether your
  // named supervisor is on this session (`supervisor_not_on_session`): then the team says "Not on this session".
  const ranks=(e.pair_ranks||{}) as Record<string,string>,notHere=!!sup&&!!e.supervisor_not_on_session;
  const entry=(id:string,depth:number)=>({p:{person_id:id,rank:ranks[id]??(id===me?String(e.rank||''):'')},depth,missing:false,trainees:0});
  const team=!me||sup&&kids.length?null:notHere?[entry(me,0)]:sup?[entry(sup,0),entry(me,1)]:kids.length?[entry(me,0),...kids.map(id=>entry(id,1))]:null;
  const withWho=team?'':[sup?'with '+(pairs[sup]||'your supervisor'):'',kids.length?'supervising '+kids.map(id=>pairs[id]||'a trainee').join(', '):''].filter(Boolean).join(' · ');
  // Names with the label (the full name in the box's accessible name and each tip); the sentence above keeps the full one.
  const fullName=(id:string)=>id===me?'You':pairs[id]||(id===sup?'Your supervisor':'A trainee');
  const nameOf=(id:string)=>id!==me&&pairs[id]?personName(id,pairs[id]):fullName(id);
  // A colleague's name opens their card (the one person hub). The hero's team and tags are entered in its own key.
  const key=legend('Tags');
  const box=team&&teamView(team,{density:'card',nameOf,fullName,problem:notHere?{kind:'elsewhere',supervisor:sup!}:null,key,
    // The one person pill (D78): a colleague's opens their card; yours is drawn.
    member:x=>{const id=x.p.person_id;return id!==me&&pairs[id]?personPill({base:personLink(id,nameOf(id),ctx.navigate,pairs[id]),label:nameOf(id)}):personPill({label:nameOf(id),full:fullName(id)});}});
  const calendar=button('Add to calendar',()=>subscribeAfter(hero,calendar,hooks),'secondary small',{'aria-expanded':'false'});
  const [offer,cant]=dutyButtons(ctx,e,clinic,canOffer());
  const facts=el('p',{},[clinic,withWho].filter(Boolean).join(' · '),e.team_lead?key.tag('lead'):null,e.overtime?key.tag('ot'):null);
  hero.replaceChildren(el('small',{class:'home-hero-when'},when),el('h2',{id:'home-hero-title'},`${day} · ${time}`),facts,...(box?[box]:[]),
    el('div',{class:'actions'},offer,calendar,cant),key.root);
  // Already reported (their request waits for cover): the hero says so instead of offering the duty's actions.
  void reading.then(d=>{if(d instanceof Error||!reportedOn(d,e))return;  // also before the hero is on the page
    // The report's time is when it was made; whether anyone was told yet is not known here, so it is not claimed.
    hero.replaceChildren(el('small',{class:'home-hero-when'},`${days===0?'Today':days===1?'Tomorrow':day} · you can’t make it`),el('h2',{id:'home-hero-title'},`${day} · ${time}`),facts,...(box?[box]:[]),
      el('div',{class:'home-hero-status'},badge('Reported · waiting for approval','warn','',{icon:'clock'})),key.root);});
  hero.hidden=false;
}

/** Offer swap is always offered for a duty ahead: whether a colleague can receive it is said by the swap sheet itself
 *  (offerFields), so Home never reads the swap options (one of the slowest reads, every Home load: SW3-C-03/P01). */
const canOffer=()=>Promise.resolve(true);
/** Their "Can't make it" already waits for cover on this duty (Home's request summary carries the duty's dates, maintenance). */
const reportedOn=(d:HomeData,e:Row)=>d.my_requests.some(r=>isAbsenceReport(r)&&r.stage==='waiting'&&Date.parse(r.start)===Date.parse(e.start)&&Date.parse(r.end)===Date.parse(e.end));

/** SW-05 (B6, D13): what a person can do about one of their own published duties still ahead — Offer swap (before it
 *  starts) and Can't make it (until it ends; the coordinator is told and finds cover). One definition for Home's hero,
 *  each Coming up row and the Schedule's duty panel. `e` is the calendar duty: its id is the assignment, source_id the
 *  publication. */
function dutyActs(ctx:Context,e:Row,clinic:string){
  const zone=ctx.me.organisation.timezone,day=formatDay(e.start,zone),time=`${formatTime(e.start,zone)}–${formatTime(e.end,zone)}`;
  const own=!!ctx.me.person_id&&ctx.permits(CAPABILITY.STAFF);
  return {offer:own&&ctx.me.staff_swap_access&&Date.parse(e.start)>Date.now()?()=>ctx.navigate('requests?new=1&type=swap&duty='+encodeURIComponent(e.id)):null,
    cant:own&&canCreateRequest(ctx)&&Date.parse(e.end)>Date.now()?()=>cantMakeIt(ctx,e,day,time,clinic):null};
}
/** Duties reported this visit (the reply is in before any list is read again). */
const reportedNow=new Set<string>();
/** SX-19: the person's own Can't make it already covers this duty (waiting or approved): it is never offered twice. */
export const reportedDuty=(ctx:Context,e:Row)=>get<{items:Row[]}>('/api/requests/all?type=other&status=waiting,approved&limit=100').then(p=>p.items.some(r=>isAbsenceReport(r)&&r.person_id===ctx.me.person_id
    &&Date.parse(r.start)===Date.parse(e.start)&&Date.parse(r.end)===Date.parse(e.end)),()=>false);
/** The duty actions as buttons [Offer swap, Can't make it] (hero, Schedule panel); Offer swap is always offered; the swap sheet
 *  says when nobody can receive it (canOffer). `reported`: once the duty is reported, Can't make it says Reported. */
export function dutyButtons(ctx:Context,e:Row,clinic:string,offerable=canOffer(),reported?:Promise<boolean>):(HTMLElement|null)[]{
  const a=dutyActs(ctx,e,clinic);
  const offer=a.offer?button('Offer swap',a.offer,'secondary small'):null;
  if(offer)void offerable.then(ok=>{if(!ok)offer.remove();});
  if(!a.cant)return [offer,null];
  const ask=button(ABSENCE_REPORT_LABEL,a.cant,'ghost small'),said=badge('Reported','warn');
  // The server's list decides (a report withdrawn elsewhere offers it again).
  void reported?.then(yes=>{const now=[ask,said].find(alive),next=yes?said:ask;if(!yes)reportedNow.delete(String(e.id));if(now&&now!==next)now.replaceWith(next);});
  return [offer,reportedNow.has(String(e.id))?said:ask];
}
/** A Coming up row's ⋯ with the same actions; a duty already reported says so instead. */
function dutyMenu(ctx:Context,e:Row,clinic:string,offerable:Promise<boolean>,reading:Promise<HomeData|Error>){
  const a=dutyActs(ctx,e,clinic);if(!a.offer&&!a.cant)return null;
  const when=`${formatDay(e.start,ctx.me.organisation.timezone)} ${formatTime(e.start,ctx.me.organisation.timezone)}`;
  // Whether a swap may be offered is read with Home; the menu opens once it is known (it usually already is).
  const b=opens(iconButton(`Actions for ${when}`,'dots',()=>void offerable.then(ok=>menu(b,`Actions for ${when}`,[
    ...(ok&&a.offer?[{label:'Offer swap',icon:'swap' as const,run:()=>a.offer!()}]:[]),
    ...(a.cant?[{label:ABSENCE_REPORT_LABEL,icon:'leave' as const,run:()=>a.cant!()}]:[])],{placement:'below-end'})),{class:'bare home-duty-more'}),'menu');
  if(reportedNow.has(String(e.id)))return badge('Reported','warn');
  // Home's read may land before this row is put on the page: replace it then too (no-op once the row is gone).
  void reading.then(d=>{if(!(d instanceof Error)&&reportedOn(d,e))b.replaceWith(badge('Reported','warn'));});
  return b;
}

/** "Can't make it?" (B6, D13): the reason (the contract's reporter categories) and an optional note, sent as the person's own
 *  report on this duty (published_changes_api.ts reportAbsence). The coordinator is told; Undo withdraws it. */
function cantMakeIt(ctx:Context,e:Row,day:string,time:string,clinic:string){
  // What a reporter may choose (DL-035: never leave, which is a Leave request): the contract's own list.
  type Choice=typeof ABSENCE_REPORTER_CATEGORIES[number];
  const REASONS=ABSENCE_REPORTER_CATEGORIES.map(c=>[c,absenceCategoryWord(c)] as [Choice,string]);
  let reason=REASONS[0][0];
  formDialog(ABSENCE_REPORT_LABEL+'?',`${day} · ${time} · ${clinic}`,form=>{
    const id='cant-reason';
    const note=area('Note for the coordinator','',2);note.input.placeholder='Optional';note.input.maxLength=500;
    form.append(el('div',{class:'field span-2'},el('span',{class:'field-label',id},'Reason'),segmented('Reason',REASONS.map(([value,label])=>[value,label]),reason,v=>{reason=v as Choice;},{labelledBy:id})),note.wrap);
    queueMicrotask(()=>{const b=form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');if(b)withIcon(b,'send');});
    return async()=>{
      // The duty's publication and assignment (the calendar's source_id and participant id), as maintenance asks.
      const {record,message}=await reportAbsence({publication_id:String(e.source_id),assignment_id:String(e.id),reason,category:reason,note:note.input.value.trim()||undefined});
      reportedNow.add(String(e.id));
      notify(message,'success',{label:'Undo',run:()=>withdrawRequest({id:record.id,revision:record.revision},'Undo: reported by mistake')
        .then(()=>{reportedNow.delete(String(e.id));notify('Report withdrawn.');void ctx.refresh();},(err:Error)=>notify(err.message,'error',undefined,err))});
      void offerAlertsAfterReport();
      void ctx.refresh();};
  },'Tell the coordinator');
}

// ---- Away today ----

/** People away today (leave, absence, a rotation elsewhere), one line each, a dot coloured by kind. The reason shows as
 *  far as Department settings › Leave on planning issues allows ("Away" when hidden). */
function awayToday(ctx:Context,today:string){
  const zone=ctx.me.organisation.timezone;
  return card('away','Away today',async aside=>{
    const data=await get<Row>('/api/calendar?'+new URLSearchParams({start:today,end:addDays(today,1)}));
    const names=new Map((data.people as Row[]).map(p=>[p.id,p.display_name] as [string,string]));
    const seen=new Map<string,Row>();
    for(const e of data.events as Row[])if(e.counts_as_unavailable!==false&&e.kind!=='duty'&&!(e.kind==='rotation'&&e.state==='Assignable')&&!seen.has(e.person_id))seen.set(e.person_id,e);
    const away=[...seen.values()].sort((a,b)=>(names.get(a.person_id)||'').localeCompare(names.get(b.person_id)||''));
    aside.replaceChildren(away.length?plural(away.length,'person','people'):'');
    if(!away.length)return [emptyLine('Nobody is away today.',null,'home-none')];
    // The leave type's name and colour where the server sent it (the setting allows it, or it is the reader's own).
    const kind=(e:Row)=>e.kind==='rotation'?'Rotation':e.leave_type_name||(leaveShown()==='none'?'Away':(String(e.label).split(' · ')[1]||String(e.label).replace(/^./,c=>c.toUpperCase())));
    const hue=(e:Row,k:string)=>colourOf(e.leave_type_id?leaveTypes().find(t=>t.id===e.leave_type_id):undefined,k.toLowerCase());
    const last=(e:Row)=>toLocal(new Date(Date.parse(e.end)-1).toISOString(),zone).slice(0,10);
    const LIMIT=8,key=legend('Key');
    return [el('ul',{class:'home-away-list'},...away.slice(0,LIMIT).map(e=>{const k=kind(e),until=last(e),dot=()=>el('span',{class:`home-dot duty-${hue(e,k)}`,'aria-hidden':'true'});
      return el('li',{},key.mark(dot(),'k:'+k,dot(),k),nameText(e.person_id,names.get(e.person_id),{class:'truncate',missing:'Someone'}),
        el('small',{class:'muted'},`${k} · ${until<=today?'today':'to '+leaveDates({type:'',starts_on:until,ends_on:until})}`));}),
      away.length>LIMIT?el('li',{},link(`and ${plural(away.length-LIMIT,'more person','more people')}`,()=>ctx.navigate(`schedule/people/${today}?leave=1`))):null),key.root];
  });
}

// ---- Notices and recent changes ----

/** Both the page mount and a later card reply can make notices visible. Send each rendered list once. */
export function seeNotices(host:HTMLElement){
  if(!host.isConnected)return;
  const list=host.querySelector<HTMLElement>('.home-notice-list');
  if(!list||list.dataset.seen)return;
  const ids=[...list.querySelectorAll<HTMLElement>('[data-notice-id]')].map(n=>n.dataset.noticeId!);
  if(!ids.length)return;
  list.dataset.seen='1';
  // Marking read bumps each receipt's revision; an acknowledgement waits for it and uses the fresh receipt.
  seenReceipts.set(list,command<{receipts:Record<string,Row>}>('department_content.seen',{ids}).then(r=>r.receipts||{},()=>({})));
}
const seenReceipts=new WeakMap<HTMLElement,Promise<Record<string,Row>>>();

/** "I acknowledge" on a notice that requires it; afterwards a quiet "Acknowledged". */
function acknowledgeNotice(item:Row,after=()=>{}){
  const done=()=>el('small',{class:'muted home-notice-ack'},'Acknowledged');
  if(!item.acknowledgement_required)return null;
  if(item.receipt?.acknowledged_at)return done();
  const b=button('I acknowledge',async()=>{
    const list=b.closest<HTMLElement>('.home-notice-list');
    const receipt=(list&&await seenReceipts.get(list))?.[item.id]||item.receipt;
    if(!receipt)throw new Error('This notice could not be acknowledged yet. Reload the page and try again.');
    await command('notification.acknowledge',{reason:'Acknowledged by recipient'},receipt.id,receipt.revision);
    item.receipt={...receipt,acknowledged_at:new Date().toISOString()}; // "Show all" re-renders from this row
    b.replaceWith(done());after();
  },'secondary small',{'aria-label':'I acknowledge: '+item.name,class:'button secondary small home-notice-ack'});
  return b;
}

/** The department's notices for this person: drawn only once there are some (no skeleton for an optional card, H-11),
 *  an error with Retry when they could not load. Those still waiting for this reader's acknowledgement are handed to
 *  Needs you (`pending`). */
function noticesCard(pending:(rows:Row[])=>void){
  const node:HTMLElement=card('notices','Notices',async()=>{
    // One page of the server's largest size: Home shows four, but every pending acknowledgement and "Show all" need the rest.
    const items=(await get<DepartmentContentPage>(`/api/content?page_size=${CONTENT_PAGE_SIZE}`)).items;
    node.hidden=!items.length;
    const waiting=()=>pending(items.filter(item=>item.acknowledgement_required&&!item.receipt?.acknowledged_at));waiting();
    // Notices that must be acknowledged come first (the API orders them); the badge says which ask for it (H-17).
    const list=(rows:Row[])=>el('ul',{class:'home-notice-list'},...rows.map(item=>{const important=item.importance==='important'||item.importance==='must_acknowledge';
      const ask=item.acknowledgement_required&&!item.receipt?.acknowledged_at;
      return el('li',{class:important||ask?'important':'','data-notice-id':item.kind==='announcement'?item.id:undefined},
      el('strong',{},ask?badge('Read and acknowledge','warn'):important?badge('Important','warn'):'',item.name),
      item.description?el('p',{},item.description):null,item.url?el('a',{href:item.url,rel:'noopener noreferrer','aria-label':`Open link: ${item.name}`},'Open link'):null,acknowledgeNotice(item,waiting));}));
    // Four at a glance, but a notice still waiting for acknowledgement is never hidden behind the limit.
    const shown=items.filter((item,i)=>i<4||(item.acknowledgement_required&&!item.receipt?.acknowledged_at));
    const first=list(shown);
    if(shown.length===items.length)return [first];
    const more=link(`Show all ${items.length}`,()=>{const all=list(items);all.tabIndex=-1;first.replaceWith(all);more.remove();all.focus();seeNotices(node);},{'aria-label':`Show all ${items.length} notices`});
    return [first,more];
  },'',seeNotices);
  node.hidden=true;
  return node;
}

/** A credentialer's Home: eligibility and skill grants whose last day falls in the next 30 days, soonest first, from the
 *  department lists (read once). ends_on is exclusive: the last day is the day before. */
function expiringCard(ctx:Context,today:string){
  const until=addDays(today,31);
  return card('expiring','Expiring in 30 days',async aside=>{
    await ctx.ready();
    const c=ctx.catalogues,name=(kind:string,id:string)=>(c[kind]||[]).find(r=>r.id===id)?.name||(c[kind]||[]).find(r=>r.id===id)?.display_name||'';
    const soon=(r:Row)=>r.ends_on&&r.ends_on>today&&r.ends_on<=until&&!r.revoked;
    const rows=[...(c.eligibilities||[]).filter(soon).map(r=>({r,what:`Can work at ${name('services',r.service_id)||'a clinic'}`})),
      ...(c.grants||[]).filter(soon).map(r=>({r,what:name('capabilities',r.capability_id)||'A skill'}))].sort((a,b)=>a.r.ends_on.localeCompare(b.r.ends_on));
    // A count only when something ends (H-08). L14: one row per last day and clinic or skill, with how many people (a
    // name only when it is one person), never a department-scale list of names.
    if(rows.length)aside.append(`${rows.length} ending`,' ',link('Open People',()=>ctx.navigate('people')));
    if(!rows.length)return [emptyLine('Nothing ends in the next 30 days.',null,'home-none')];
    const groups=new Map<string,{r:Row;what:string;n:number}>();
    for(const x of rows){const k=x.r.ends_on+'|'+x.what,g=groups.get(k);if(g)g.n++;else groups.set(k,{...x,n:1});}
    return [el('ul',{class:'home-request-list'},...[...groups.values()].slice(0,6).map(({r,what,n})=>el('li',{},
      needText(what,n===1?name('people',r.person_id)||'Someone':plural(n,'person','people'),{cls:'muted'}),el('small',{},'Last day '+formatDate(addDays(r.ends_on,-1))))))];
  });
}

/** An approver's Home: leave and other requests someone decided (approved or declined), latest decision first. Recorded
 *  leave and wish outcomes are not decisions, so they are left out. */
function decidedCard(ctx:Context){
  const zone=ctx.me.organisation.timezone;
  return card('decided','Decided requests',async()=>{
    const page=await get<RequestListPage>('/api/requests/all?type=leave,other&status=approved,declined&sort_by=decided&sort_direction=desc&limit=20');
    const decided=page.items.filter(r=>r.source==='request'&&r.decided_by).slice(0,5);
    if(!decided.length)return [emptyLine('No recent decisions to show.',null,'home-none')];
    return [el('ul',{class:'home-request-list'},...decided.map(r=>el('li',{},needText([...(r.person_name?[nameText(r.person_id,r.person_name),' · ']:[]),requestTitle(r)],requestWhen(r,zone),{cls:'muted'}),statusBadge(r.stage,requestStageWord(r,ctx)))))];
  },link('All requests',()=>ctx.navigate('requests')));
}

/** The person's own requests waiting for someone else's decision: status, not work for them (so not under Needs you). */
function myRequests(ctx:Context,waiting:Row[],shown:number){
  const zone=ctx.me.organisation.timezone;
  return el('section',{class:'panel home-card home-requests','aria-labelledby':'home-requests-title'},
    el('div',{class:'home-head'},el('h2',{id:'home-requests-title'},'My requests'),
      el('span',{class:'home-aside home-links'},waiting.length===shown&&shown>=5?'5+ waiting':`${waiting.length} waiting`,link('All',()=>ctx.navigate('requests'),{'aria-label':'All my requests'}))),
    el('ul',{class:'home-request-list'},...waiting.slice(0,3).map(r=>el('li',{},needText(requestTitle(r),requestWhen(r,zone),{cls:'muted'}),statusBadge(r.stage,requestStageWord(r,ctx)),
      // A "Can't make it" waiting for cover is withdrawn here too (B6).
      isAbsenceReport(r)?withdrawReport(r,()=>void ctx.refresh()):null))));
}

function recentChanges(events:Row[],hooks:HomeHooks,zone:string,ctx:Context){
  return el('section',{class:'panel home-card ov-recent','aria-labelledby':'home-recent-title'},
    el('div',{class:'home-head'},el('h2',{id:'home-recent-title'},'Recent changes'),el('span',{class:'home-aside'},link('Audit log',()=>ctx.navigate('system/audit')))),
    el('ul',{class:'ov-changes'},...events.slice(0,6).map(e=>{const w=hooks.auditWords(e.action,e.count||1);
      return el('li',{},el('span',{class:'role-chip',title:w.chip},el('span',{class:'chip-text'},w.chip)),el('span',{class:'ov-change-text'},el('strong',{},e.actor_display_name||'Someone'),` ${w.verb} ${w.what}`),
        el('small',{class:'muted'},formatDate(e.created_at,zone)+' '+formatTime(e.created_at,zone)));})));
}
