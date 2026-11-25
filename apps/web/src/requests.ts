import {hasAccess,CAPABILITY,capabilityForRole,roleForCapability} from './dom.js';
import {readFailed,alive} from './dom.js';  // a failed read: refused (403) or an error with Retry (maintenance)
/** Requests: leave, unavailability, weekly availability, wishes, swaps and other requests in one list with one status flow
 *. Staff see their own; coordinators and approvers review everyone's, waiting
 * first. The list reads GET /api/requests/all (paged and filtered on the server, with each request's schedule impact);
 * every change is an existing command, so rules, audit and Undo are unchanged. */
import {skeleton} from './loading.js';
import {statusLabel,footerStatus,el,icon,button,armed,badge,note,notify,plural,field,area,select,multi,check,withIcon,iconButton,sectionHead,formDialog,confirmAction,
  dateUnits,formatDate,formatTime,formatDay,toLocal,statusBadge,weekdayOptions,labelOf,phoneQuery,showDialog,grants,type IconName,emptyLine} from './dom.js';
import {get,background,command,newest,sessionKey,sessionChanged,holdUndo,onSessionReset,rotaMeta} from './api.js';
import {dateRange,dateList,lastDayIncluded} from './date_picker.js';
import {dataTable,openFiltered,tableFilters,tableFiltered,tableAsOf,tableSearch,tableSearchOptions,tableSort,tableServerConditions,type RowAction} from './table_view.js';
import {dockPanel,rowMark,type Dock,type PanelPosition} from './side_panel.js';
import {editRoles,type BulkAction} from './bulk.js';
import {runBulk,openChange} from './bulk_change.js';
import {availabilityFields,availabilityEditForm,calendarProtectionPanel,peopleChoices,catalogueEntryOf,type Context} from './forms.js';
import {daysText,numbersText,numberLabel,noNumberText,amountWords} from './rule_catalogue.js';
import {wishData,wishFields,wishesPanel,periodName,oneOffWish,wishKindLabel,wishWhenParts} from './wishes.js';
import {swapActions,swapNext,swapOptions,offerFields} from './governance.js';
import {addDays} from './planner_logic.js';
import {personLink,personName,fullNameOf} from './person_label.js';
import {REQUEST_STAGES,REQUEST_TYPES,TERMS,UNLINKED_STAFF,unlinkedStaff,type RequestStage,type RequestType} from './terms.js';
import {REQUEST_SORT_FIELDS,MAX_REQUEST_DECISIONS,ABSENCE_REPORT_LABEL,type REQUEST_KINDS,type RuleLine} from './generated.js';
import {ruleReview} from './rule_review.js';
import {levelWords} from './rule_view.js';
import {leaveTypeName} from './findings.js';
import type {Row} from './types.js';
import {mayDecide} from './independence.js';
import {absenceWords} from './published_changes_api.js';
import {moreWords,markFields,weekdayNames,dayRange,selfApprovedBadge} from './dom.js';  // the one "+N" and its words (UX › controls S9)

/** What the page needs from the shell: the address query, a way to change it, and the published rota at a day. */
export interface RequestHooks{query:URLSearchParams;setQuery:(q:URLSearchParams)=>void;showDay:(day:string,personId:string)=>void}
type WishPoints={budget_points:number;period:string;start?:string;end?:string;used?:number;multiplier?:number};
type Page={items:Row[];total:number;offset:number;limit:number;has_more:boolean;counts:{to_decide:number;stages?:Record<string,number>};wishes:WishPoints|null};
type Kind='leave'|'record'|'weekly'|'wish'|'swap'|'other'|'overtime';

const TABLE='requests',SIZE=100;
const TYPE_ICON:Record<RequestType,IconName>={leave:'leave',weekly:'clock',wish:'star',swap:'swap',other:'inbox'};
// Waiting first, then what is in play, then what is decided: the order of status views and groups (the server pages in it).
const ORDER:RequestStage[]=['waiting','reply','accepted','open','approved','declined','withdrawn','expired'];
// Waiting for you: a built-in view's server scope (requests/all?for_me=1), not a column.
const FOR_ME='for_me',FOR_ME_LABEL='Waiting for you';
const STAGES:Record<string,string>={waiting:statusLabel('submitted'),...REQUEST_STAGES};
const stageWord=(s:string)=>STAGES[s]||s;
/** A swap's status word: whose turn it is, "Your turn" for the reader (the line under it names the step). */
export function requestStageWord(r:Row,ctx?:Context){
  // SX-08 (maintenance absence_progress): waiting for approval until it is approved, then finding cover; the same word for everyone.
  if(isAbsenceReport(r)&&r.absence_progress)return ABSENCE_PROGRESS[String(r.absence_progress)]||stageWord(r.stage);
  if(r.stage!=='reply')return stageWord(r.stage);
  if(r.effective_status==='offered'&&r.open_offer&&!r.recipient_person_id){
    if(r.audience_failed)return 'Could not find colleagues';
    // SW-03: nobody could be told, so it never "waits for a colleague".
    if(r.no_eligible_recipients)return 'Nobody can take it';
    return r.audience_pending?'Finding colleagues…':'Waiting for a colleague';
  }
  const offered=r.effective_status==='offered',ids=offered?[r.recipient_person_id]:[r.proposer_person_id,r.proposer_id];
  return ctx&&ids.some(id=>id&&(id===ctx.me.person_id||id===ctx.me.identity_id))?'Your turn':`Waiting for ${offered?r.recipient_name:r.proposer_name}`;
}
const typeWord=(t:string)=>REQUEST_TYPES[t as RequestType]||t;
// A scheduling fact is listed under Other requests but named for what it is: a person's, or the calendar's (no person).
const kindWord=(r:Row)=>r.source==='scheduling_fact'?(r.person_id?'Person fact':'Calendar fact'):typeWord(r.type);
const keyOf=(map:Record<string,string>,label:string)=>Object.keys(map).find(k=>map[k]===label);
const RECORD_KINDS:Record<string,string>={recorded_leave:'Leave',approved_leave:'Approved leave',off_duty:'Day off',actual_absence:'Actual absence',external_commitment:'External commitment',protected_time:'Protected time'};
/** Keyed by the contract's REQUEST_KINDS, so a new kind fails the build here. A rotation change is asked for from the
 *  rotation (rotation.request_change), not through the request form, so the contract's list does not carry it. */
const REQUEST_KIND_WORDS:Record<typeof REQUEST_KINDS[number]|'rotation_change',string>={leave:'Leave',clinic_change:'Clinic change',meeting:'Meeting',administrative:'Administrative',overtime_availability:'Available for overtime',rotation_change:'Rotation change',absence_report:ABSENCE_REPORT_LABEL};
/** Staff "Can't make it" on a published duty (D13). Its reason stays private request evidence (maintenance), so it is named by
 *  its kind only. */
export const isAbsenceReport=(r:Row)=>r.source==='request'&&r.kind==='absence_report';
export const absenceReason=(_r:Row)=>REQUEST_KIND_WORDS.absence_report;
const ABSENCE_PROGRESS:Record<string,string>={waiting_for_approval:statusLabel('submitted'),finding_cover:'Finding cover'};
// An overtime offer is consent to extra duties, decided by a coordinator; it never blocks duties.
const isOvertime=(r:Row)=>r.source==='request'&&r.kind==='overtime_availability';
const decider=(r:Row)=>r.source==='pattern'||isOvertime(r)?roleForCapability(CAPABILITY.COORDINATOR):roleForCapability(CAPABILITY.APPROVER);
// A person's scheduling fact (quota, target, overtime offer, calendar protection) waiting for an independent approver.
const FACT_FAMILY:Record<string,string>={workload:'person.workload',target:'person.workload',overtime_offer:'person.overtime',protected_time:'calendar.protection'};
/** A person fact's numbers in the Rules' line sentence (amountWords, read aloud as words, never "≤"/"→"): "At most 3 duties
 *  per calendar week · Mandatory"; a number it clears as the Rules' None ("No maximum"). */
function factNumbers(r:Row):string{
  const e=catalogueEntryOf({kind:'catalogue',id:r.catalogue_id,catalogue_version:2})||catalogueEntryOf({kind:'catalogue',id:r.catalogue_id});
  const l={numbers:r.numbers||{},measure:r.measure??null,period:r.period??null} as RuleLine;
  const cleared=Object.entries(l.numbers).filter(([k,n])=>n.value==null&&k!=='apply').map(([k])=>noNumberText(e?numberLabel(e,k,true):k));
  const a=e&&(Object.values(l.numbers).some(n=>n.value!=null)||l.numbers.apply)?amountWords(e,l):null;
  // shortcut: a personal target's goal metric is not on the row, so the sentence would name the rule ("About 4 fair
  // share"); it says "Target 4" until the server sends the metric, then amountWords with the goal's settings.
  const goal=r.kind==='target'&&!l.measure&&l.numbers.target?.value!=null&&e?{text:`${numberLabel(e,'target',true)} ${l.numbers.target.value}`,level:a?.level}:a;
  const said=goal?goal.text.replace(/^./,c=>c.toUpperCase())+(goal.level===undefined?'':' · '+levelWords(goal.level)):e?'':numbersText(l);
  return [said,...cleared].filter(Boolean).join(' · ');
}
/** A person fact's details from its structured fields: an offer's or protection's days and times (the department's week
 *  order), a quota's or target's numbers; its dates are the Dates column's, its calendar entry the Person column's. */
export const factWords=(r:Row)=>(['overtime_offer','protected_time'].includes(r.kind)?[daysText({weekdays:r.weekdays,start_time:r.start_time,end_time:r.end_time})]:[factNumbers(r)]).filter(Boolean);
/** A person fact's requested days: ends_on is exclusive, so the last day shown is the day before it. */
function factWhenParts(r:Row):string[]{
  const last=r.ends_on?addDays(r.ends_on,-1):'';
  if(r.starts_on&&last)return [dayRange(r.starts_on,last)];
  return r.starts_on?['From '+formatDate(r.starts_on)]:last?['Until '+formatDate(last)]:[];
}
/** Leave whose type does not count as unavailable: shown, never blocking (captured when it was asked for or recorded). */
const nonBlocking=(r:Row)=>(r.source==='request'||r.source==='availability')&&r.counts_as_unavailable===false&&!isOvertime(r);
const NOT_BLOCKING='Does not block duties';
const shortDay=(day:string)=>formatDay(day,'UTC');
// Rows shown on this visit: "Show more" adds a page; a refresh reloads as many as were shown.
const pageState={shown:SIZE,loaded:'',loadedSession:-1};
// Filters from the address (#requests?type=swap&status=waiting): applied on top of the person's own filters, shown as
// their own chips and never saved, so the saved view comes back once they are cleared.
type RequestLink={type:string[];status:string[];person:string};
let panel:{id:string;close:()=>void}|null=null;

/** Search, filters and supported sorts read the full authorized history; grouping arranges the returned rows.
 *  A link's filter narrows what is read; the person's own filters still apply to the rows (both hold). */
function serverQuery(ctx:Context,table:string,link:RequestLink,fixedPerson:boolean,defaults:Record<string,string[]>={},scoped=true){
  // Before the list is first drawn its defaults are its filters (Waiting for you, for deciders).
  const f=tableFiltered(table)?tableFilters(table):defaults,types=link.type.length?link.type:(f.type||[]).map(l=>keyOf(REQUEST_TYPES,l)).filter(Boolean),
    stages=link.status.length?link.status:(f.status||[]).map(l=>keyOf(STAGES,l)).filter(Boolean);
  const q=new URLSearchParams(),text=tableSearch(table).trim();
  if(types.length)q.set('type',types.join(','));if(stages.length)q.set('status',stages.join(','));if(text)q.set('q',text);
  if(link.person)q.set('person_id',link.person);
  // Waiting for you (maintenance): the server's own "waiting for you" union, never a client rule (the view's scope, no column).
  if(scoped&&f[FOR_ME]?.length)q.set('for_me','1');
  const people=(ctx.catalogues.people||[]).filter(p=>(f.person||[]).includes(String(p.display_name))).map(p=>p.id);
  if(!fixedPerson&&f.person?.length)q.set('person_ids',people.join(',')||'00000000-0000-0000-0000-000000000000');
  // Older saved views may name a derived column that no longer offers sorting. The table clears it on draw.
  const sort=tableSort(table);if(sort&&REQUEST_SORT_FIELDS.some(key=>key===sort[0])){q.set('sort_by',sort[0]);q.set('sort_direction',sort[1]);}
  // Or / is-not conditions go to the server too (in its codes: the columns' `server`), before it counts and pages.
  // The search's options too (filter.search_options), for every source and stage count before paging.
  const conditions=tableServerConditions(table),so=tableSearchOptions(table);
  if(conditions||so)q.set('filter',JSON.stringify({...(conditions?{conditions}:{}),...(so?{search_options:so}:{}),...(conditions&&tableAsOf(table)?{as_of:tableAsOf(table)}:{})}));
  return q.toString();
}
async function load(n:number,q:string,start=0):Promise<Page>{
  let page:Page|null=null;
  for(let offset=start;offset<start+n;offset+=200){
    const p=await get<Page>(`/api/requests/all?${q?q+'&':''}limit=${Math.min(200,start+n-offset)}&offset=${offset}`);
    page=page?{...p,items:[...page.items,...p.items]}:p;if(!p.has_more)break;
  }
  return page!;
}

/** A fixed profile scope uses the same paged collection, detail panel and decisions as Requests. */
export async function requestsPage(ctx:Context,target:HTMLElement,hooks:RequestHooks,person?:Row){
  const me=ctx.me,team=ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER),zone=me.organisation.timezone;
  const scope=person?.id||hooks.query.get('person')||'';
  // The staff API deliberately substitutes self for a foreign filter. Never label that reply as a colleague's history.
  // A programme lead reads a person's requests through the server's programme scope (their trainees; others come back empty).
  if(scope&&!team&&!ctx.permits(CAPABILITY.PROGRAMME_LEAD)&&!(ctx.permits(CAPABILITY.STAFF)&&scope===me.person_id)){
    target.append(note('You can view only your own requests.'));return;
  }
  const visit=sessionKey(),key=person?'person-requests':TABLE,state=person?{shown:SIZE,loaded:'',loadedSession:-1}:pageState;
  // Profiles share a view, but its legacy Person filter must not follow the reader to a different person.
  if(person){const filters={...tableFilters(key)};if('person' in filters){delete filters.person;openFiltered(key,filters,tableSearch(key));}}
  let query=new URLSearchParams(hooks.query);
  const setQuery=(change:(q:URLSearchParams)=>void)=>{query=new URLSearchParams(query);change(query);hooks.setQuery(query);};
  const creating=query.get('new')?query.get('type')||'':null;  // #requests?new=1&type=wish: the type is the new request's, not a filter
  // Consumed before the first await: an overlapping redraw of this page (a refresh during the load) must not open a
  // second New request sheet from the same link (SW3-C-04).
  const duty=query.get('duty')||undefined;if(creating!==null)setQuery(q=>{q.delete('new');q.delete('type');q.delete('duty');});
  const listed=(key:string,known:Record<string,string>)=>(query.get(key)||'').split(',').filter(k=>k in known);
  let link:RequestLink={type:creating===null?listed('type',REQUEST_TYPES):[],status:listed('status',STAGES),person:scope};
  // maintenance: whoever decides requests opens on Waiting for you (the first built-in view, the list's default).
  const decides=!person&&grants(me.roles,'request.decide'),forMe:Record<string,string[]>=decides?{[FOR_ME]:['1']}:{};
  // A link names what to show (Home's Show, an older address): it opens on the whole list, not inside Waiting for you.
  if(decides&&(link.type.length||link.status.length||link.person)){const f=tableFiltered(key)?tableFilters(key):forMe;
    if(f[FOR_ME]){const rest={...f};delete rest[FOR_ME];openFiltered(key,rest,tableSearch(key));}}
  const queryNow=()=>serverQuery(ctx,key,link,!!person,forMe),firstQuery=queryNow();
  if(firstQuery!==state.loaded||state.loadedSession!==sessionKey())state.shown=SIZE;
  state.loaded=firstQuery;state.loadedSession=sessionKey();
  // The page draws from its list; whether this person may offer overtime (a slow read) adds that kind when it lands.
  const offered=person?Promise.resolve(false):overtimeOffered(ctx);
  let page=await load(state.shown,firstQuery),requested=firstQuery;
  // Show more on the page this one replaces, asked while this read was on its way (state.shown grew): read that window
  // too, else this draw would drop it. The page left discards its own answer once replaced.
  const end=page.offset+page.limit;
  if(page.has_more&&end<state.shown){const rest=await load(state.shown-end,firstQuery,end);page={...rest,items:[...page.items,...rest.items]};}
  let kinds=person?[]:creatable(ctx,page,false);
  // A New request opened before that answer is handed the kinds it leads to (newRequest adds them in place).
  const landed=person?null:offered.then(yes=>yes?kinds=creatable(ctx,page,true):kinds);
  const open=(preset?:string,duty?:string)=>newRequest(ctx,kinds,preset,duty,landed);
  // A phone: the primary is one icon in the title row (as on Home), not a row of its own.
  if(!person)target.append(sectionHead('Requests','',!kinds.length?null:phoneQuery.matches?iconButton(TERMS.newRequest,'plus',()=>open(),{class:'primary phone-primary'})
    :withIcon(button(TERMS.newRequest,()=>open(),'primary'),'plus')));
  const wishes=!person&&page.wishes?wishControls(ctx,page.wishes):null;
  const list=el('div',{class:'requests'});
  target.append(person?list:el('section',{class:'panel'},list));
  // Keep the table and rows array, so a server search or next page never detaches its open controls.
  const rows:Row[]=[],byId=new Map<string,Row>();
  // The server orders by stage before paging, so a later page never belongs above rows already shown.
  const fill=()=>{rows.splice(0,rows.length,...page.items);byId.clear();for(const r of rows)byId.set(r.id,r);};
  fill();
  // Whose request it is, for every reader of other people's requests: a row is the reader's own only when it names them
  // (a calendar fact names no person and still needs its subject shown). A decider's first page is Waiting for you,
  // often empty: the whole list they open next still names each person.
  const readsOthers=!person&&(team||ctx.permits(CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR));
  const many=readsOthers&&(decides||page.items.some(r=>!r.person_id||r.person_id!==me.person_id));
  const more=el('div',{class:'request-more'}),readStatus=el('div');
  // More than the rows shown: the server total is the screen's status (footer), not the table's count of loaded rows.
  // A profile's count is on its views button; the page's total is the footer status.
  const total=person?el('div',{},el('span',{class:'dt-count'})):footerStatus(el('span',{class:'dt-count'}));
  if(!person)target.append(total);
  const showMore=()=>{more.hidden=!page.has_more;more.replaceChildren(...(page.has_more?[el('span',{class:'muted'},`Showing ${page.items.length} of ${page.total}`),
    button('Show more',()=>reload(true),'secondary small')]:[]));
    total.querySelector('.dt-count')?.replaceChildren(plural(page.total,'request'));};  // "Showing n of N" ends the list (C14)
  const read=newest(load);
  // The newest read is on its way: an empty list meanwhile is loading, not empty (LIVE-48). Only the newest counts: an
  // obsolete read that settles later (newest() gives it undefined) must not hold the skeleton over a newer empty answer.
  let reading=0,latest=0;
  // The page is built detached (an answer can land before it is shown): gone only once replaced (dom.ts alive).
  const gone=()=>!alive(list);
  const reload=async(append=false)=>{
    if(visit!==sessionKey()||state.loadedSession!==visit||gone())return;
    const q=queryNow(),keep=append&&q===state.loaded&&q===requested;requested=q;
    readStatus.replaceChildren();const mine=reading=++latest;
    try{
      // Expired swaps may be omitted after the server takes its slice: advance by the consumed window, not row count.
      const offset=keep?page.offset+page.limit:0;
      // A concurrent page refresh must include the window explicitly requested by Show more, even before it arrives.
      if(keep)state.shown=Math.max(state.shown,offset+SIZE);
      const next=await read(keep?SIZE:state.shown,q,offset).finally(()=>{if(reading===mine)reading=0;});
      if(!next||q!==queryNow()||gone())return;
      page=keep?{...next,items:[...page.items,...next.items]}:next;state.loaded=q;state.shown=Math.max(SIZE,next.offset+next.limit);
      fill();table.refresh(!keep);showMore();
    }catch(e){if(q===queryNow()&&!gone()&&!sessionChanged(e))readStatus.replaceChildren(
      readFailed('requests',e,()=>void reload(append),'requests','The list has not been updated.'));return false;}
  };
  // Returns the read (false once a failure is reported), so a failed search clears the list's busy rows.
  const refilter=()=>{if(queryNow()!==requested){state.shown=SIZE;return reload();}};
  // The link's filters as chips of their own; removing one restores the saved view (nothing of the link was saved).
  const linkChips=el('span',{class:'request-link-chips'});
  const drawLink=()=>linkChips.replaceChildren(...([['type','Type',typeWord],['status','Status',stageWord],['person','Person',(id:string)=>String(ctx.catalogues.people?.find(p=>p.id===id)?.display_name||'Selected person')]] as const).flatMap(([key,label,word])=>{
    const values=key==='person'?link.person&&!person?[link.person]:[]:link[key];if(!values.length)return [];
    const shownAs=values.map(word).join(', '),chip=el('button',{type:'button',class:'dt-chip request-link-chip','aria-label':`Remove filter ${label}: ${shownAs} (from the link)`,
      'data-tip':'From the link you opened; not saved to your view'},el('span',{class:'muted'},label+':'),shownAs,icon('close'));
    chip.addEventListener('click',()=>{link={...link,[key]:key==='person'?'':[]};setQuery(q=>q.delete(key));drawLink();state.shown=SIZE;void reload();});
    return [chip];}));
  drawLink();
  // A row's details are worked out once per row read (each draw asked up to three times). Its buttons are built for each
  // drawn copy: grouped by Person, a swap is drawn under both people, and one shared node would move to the last copy.
  const details=new WeakMap<Row,string>();
  const detailOf=(r:Row):string=>{const known=details.get(r);if(known!==undefined)return known;const d=String(detail(r,zone,ctx)||'');details.set(r,d);return d;};
  // What the server searches: people's names, the type and wish/weekly/swap notes, never private reasons (services/availability.py).
  // The whole list's stage counts while Waiting for you is shown (its own read counts only its rows): one read per query.
  let every:{q:string;stages:Record<string,number>}|null=null;
  const everyStages=()=>{const q=serverQuery(ctx,key,link,!!person,forMe,false);
    if(every?.q!==q){const asked={q,stages:every?.stages||{}};every=asked;
      void load(1,q).then(p=>{if(every===asked){asked.stages=p.counts.stages||{};table.refresh(false);}},()=>undefined);}
    return every.stages;};
  const table=dataTable<Row>({id:key,noun:['request','requests'],rows,rowId:r=>r.id,searchLabel:'Search requests',searchPlaceholder:readsOthers?'Person or type':'Type, e.g. annual or wish',
    // Read-only readers (auditor, programme lead) see only proposed person facts here, never leave: nothing to ask for.
    empty:team?['Nothing to review','Requests from staff appear here: leave, weekly availability, wishes and swaps.']
      :unlinkedStaff(me)?[UNLINKED_STAFF.title,UNLINKED_STAFF.text]:!canCreateRequest(ctx)?['No person facts waiting','Proposed person facts appear here while they wait for approval.']:['No requests yet',kinds.length?`Ask for leave, a wish or a swap with ${TERMS.newRequest}.`:''],
    // Details, schedule impact and the swap's next step wrap by default: unwrapped, a reviewer's table (with Person and
    // the decision buttons) was wider than a 1440 screen and pushed Status out of sight under the row actions.
    defaults:{group:team?'status':'',wrap:['detail','impact','status'],filters:forMe},serverPaged:true,  // Show more
    bulk:team?ids=>{const picked=ids.map(id=>byId.get(id)!).filter(Boolean);return [...recordedChange(ctx,picked,key),...bulkDecide(ctx,picked)];}:undefined,
    onFilter:refilter,onSearch:refilter,searchIn:'requests',onSort:refilter,resultScope:queryNow,loading:()=>reading>0,narrowed:()=>!!(link.type.length||link.status.length||link.person&&!person),
    groupTotal:(by,group)=>by==='status'?page.counts.stages?.[keyOf(STAGES,group)||'']:undefined,
    where:r=>(!link.type.length||link.type.includes(r.type))&&(!link.status.length||link.status.includes(r.stage)),
    controls:link.type.length||link.status.length||link.person&&!person?[linkChips]:undefined,
    actions:wishes?[wishes.actions]:undefined,
    // Status views (counted on the server, under the type filter): All, then each stage that has requests; what waits
    // on this person stays on the views button.
    // A decider's first view is Waiting for you (counted as their queue, counts.to_decide); the others are counted on the
    // whole list, so while Waiting for you is shown their counts come from one light read without it.
    views:()=>{const mine=decides&&!!tableFilters(key)[FOR_ME]?.length,n=mine?everyStages():page.counts.stages||{},decide=person?0:page.counts.to_decide;  // the waiting-for-you count is department-wide
      const stages=[{label:'All requests',filters:{},count:Object.values(n).reduce((a,x)=>a+x,0)},
        ...ORDER.filter(s=>n[s]||s==='waiting'||(tableFilters(key).status||[]).includes(stageWord(s))).map(s=>({label:stageWord(s),filters:{status:[stageWord(s)]},count:n[s]||0,
          warn:s==='waiting'&&decide&&!decides?`${decide} waiting for you`:undefined}))];
      return decides?[{label:FOR_ME_LABEL,filters:forMe,count:page.counts.to_decide,warn:decide&&!mine?`${decide} waiting for you`:undefined,empty:['Nothing waiting for you','']},...stages]:stages;},
    onOpen:(r,position)=>showPanel(ctx,r,hooks,setQuery,r.id,position),
    columns:[
      {key:'type',label:'Type',server:{field:'type',value:l=>keyOf(REQUEST_TYPES,l)||l},sort:r=>typeWord(r.type),text:r=>`${title(r)} ${typeWord(r.type)} ${calendarOnly(r)}`,facet:r=>typeWord(r.type),options:Object.values(REQUEST_TYPES),
        cell:r=>el('span',{class:'request-type'},el('span',{class:'request-icon','aria-hidden':'true'},icon(TYPE_ICON[r.type as RequestType]||'inbox')),
          // Leave shows only its type ("Annual", LIVE-05): the icon says leave, so its word is still read out (hidden icon).
          el('span',{class:'request-title'},el('strong',{},title(r)),r.type==='leave'&&title(r)!==kindWord(r)?el('span',{class:'sr-only'},' '+kindWord(r)):null,(()=>{const sub=[title(r)!==kindWord(r)&&r.type!=='leave'?kindWord(r):'',calendarOnly(r),nonBlocking(r)?NOT_BLOCKING:''].filter(Boolean).join(' · ');return sub?el('small',{class:'muted'},sub):null;})()))},
      {key:'person',label:'Person',server:{field:'person'},hidden:!many,text:r=>String(r.person_name||''),facet:person?undefined:r=>r.source==='swap'?[r.proposer_name,r.recipient_name].filter(Boolean):String(r.person_name||''),
        ids:r=>r.source==='swap'?[r.proposer_person_id,r.recipient_person_id].filter(Boolean):r.person_id,options:!person&&team?(ctx.catalogues.people||[]).map(p=>String(p.display_name)):undefined,cell:r=>requestPeople(ctx,r,true)},
      {key:'when',label:'Dates',text:r=>when(r,zone),sort:r=>sortKey(r),cell:r=>el('span',{class:'request-when'},requestWhenNode(r,zone)),
        // Filtered by its first and last day (relative: This month, Next 2 weeks; Is empty), as the Filter's Dates · Start / End.
        filter:{kind:'date',parts:[{key:'start',label:'Start',server:{field:'start'},value:r=>firstDay(r,zone)},{key:'end',label:'End',server:{field:'end'},value:r=>lastDay(r,zone)}]}},
      // When it was asked for or proposed (the server's proposed_at): a filter and sort of its own, never the Dates.
      {key:'proposed_at',label:'Proposed',hidden:true,server:{field:'proposed_at'},sort:r=>proposedAt(r),cell:()=>'',
        filter:{kind:'date',value:r=>proposedAt(r)&&toLocal(proposedAt(r),zone).slice(0,10)}},
      // Drawn while a loaded row has details (Show more or a search can bring one); what the server withheld leaves no column.
      {key:'detail',label:'Details',sort:false,text:detailOf,whenAny:r=>!!detailOf(r),cell:r=>detailOf(r)?el('span',{class:'request-detail','data-private':''},detailOf(r)):''},
      // Your own request says you are on duty then (neutral); a reviewer reads what approving opens (gold).
      {key:'impact',label:'Schedule',sort:false,hidden:!rows.some(r=>r.impact_count!==undefined),text:r=>impactWords(r,ownRequest(ctx,r)),
        cell:r=>{if(!impactWords(r,ownRequest(ctx,r)))return '';const own=ownRequest(ctx,r)&&affects(r);
          return el('span',{class:'request-impact'+(affects(r)&&!own?' has-impact':'')},icon(own?'calendar':affects(r)?'alert':'check'),impactWords(r,ownRequest(ctx,r)));}},
      {key:'status',label:'Status',server:{field:'stage',value:l=>keyOf(STAGES,l)||l},facet:r=>stageWord(r.stage),options:ORDER.map(stageWord),ordered:ORDER.map(stageWord),
        // A swap says who it waits on in the row (it moves between two people); other kinds keep the reason in the tip.
        cell:r=>{const word=requestStageWord(r,ctx),b=statusBadge(r.stage,word),why=stageDetail(r,ctx);
          // A decision the person made on their own request under a self-approval permission says so (as the audit does).
          if(r.self_approved)return el('span',{class:'request-stage'},b,selfApprovedBadge());
          // One line: a long name ends in an ellipsis and the whole status is the tooltip.
          if(!why){b.setAttribute('data-tip',word);return b;}if(r.source==='swap'){b.setAttribute('data-tip',word);return el('div',{class:'request-stage'},b,el('small',{class:'muted'},why));}
          b.setAttribute('data-tip',`${word}: ${why}`);return b;}},
    ],
    rowButtons:r=>actions(ctx,r,hooks).buttons,
    rowActions:r=>actions(ctx,r,hooks).menu});
  showMore();
  // The search or filters may have changed while the first page was read (an earlier render applied a saved view's
  // search to the shared view state); the table records them as its own, so read what it now shows.
  refilter();
  // The footer says the server total; the table's own count of loaded rows ("100 requests" of 3000) would contradict it.
  table.root.querySelector('.dt-count')?.setAttribute('hidden','');
  if(wishes)table.root.querySelector('.dt-toolbar')!.after(wishes.calendar);
  list.append(readStatus,table.root,more);
  // #requests?open=<id> (notification links) opens one request; #requests?new=1[&type=…] opens New request (Home links to it).
  const id=query.get('open');
  if(id)showPanel(ctx,page.items.find(r=>r.id===id)||null,hooks,setQuery,id);
  // #requests?new=1&type=swap&duty=<assignment>: Home's Offer swap opens the offer on that duty.
  // A link to an overtime offer waits for that answer, so the kind is there to be chosen.
  if(creating==='overtime')await offered;
  if(creating!==null&&kinds.length)open(creating||undefined,duty);
}

/** A person's requests in their panel: a compact list (type, dates, status; schedule impact under it), newest 50 in the
 *  Requests order; each opens the same detail and decisions, and "Open in Requests" has the whole collection. */
export async function personRequests(ctx:Context,person:Row,hooks:RequestHooks):Promise<HTMLElement>{
  const zone=ctx.me.organisation.timezone,page=await get<Page>(`/api/requests/all?person_id=${encodeURIComponent(person.id)}&limit=50`);
  const items=[...page.items].sort((a,b)=>ORDER.indexOf(a.stage)-ORDER.indexOf(b.stage));
  const all=el('a',{href:'#requests'+(ctx.me.person_id===person.id&&!ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)?'':'?person='+encodeURIComponent(person.id)),class:'link-button'},'Open in Requests →');
  const row=(r:Row)=>{
    const open=el('button',{type:'button',class:'pr-request-open'},el('span',{class:'request-icon','aria-hidden':'true'},icon(TYPE_ICON[r.type as RequestType]||'inbox')),
      el('span',{class:'ph-what'},el('strong',{},title(r)),el('small',{class:'muted'},when(r,zone)),impactWords(r,ownRequest(ctx,r))?el('small',{class:'muted'},impactWords(r,ownRequest(ctx,r))):''));
    open.addEventListener('click',()=>showPanel(ctx,r,hooks,()=>{},r.id));
    const status=statusBadge(r.stage,requestStageWord(r,ctx));
    return el('li',{class:'pr-request','data-row':r.id},open,r.self_approved?el('span',{class:'request-stage'},status,selfApprovedBadge()):status);};
  return el('section',{class:'ph-section'},el('div',{class:'ph-head'},el('h3',{},'Requests'),all),
    items.length?el('ul',{class:'ph-rows pr-requests'},...items.map(row)):emptyLine('No requests.'),
    page.has_more?el("p",{class:"muted"},`Showing 50 of ${page.total}.`):'');
}

/** The kinds this person may ask for (or, as a coordinator, record). */
export const canCreateRequest=(ctx:Context)=>ctx.permits(CAPABILITY.COORDINATOR)||!!(ctx.me.person_id&&ctx.permits(CAPABILITY.STAFF));
type KindChoice=[Kind,string,IconName];
function creatable(ctx:Context,page:Page,overtime:boolean):KindChoice[]{
  const canAsk=canCreateRequest(ctx),coordinator=ctx.permits(CAPABILITY.COORDINATOR);
  // Icon and name only: who decides is said by the request's status once it is sent.
  return [
    // One way to enter leave: staff ask for it (an approver decides); a coordinator records it, for anyone.
    ...(coordinator?[['record','Leave or absence','leave'] as KindChoice]:canAsk?[['leave','Leave','leave'] as KindChoice]:[]),
    ...(canAsk?[['weekly','Unavailable every week','clock'] as KindChoice]:[]),
    ...(canAsk&&overtime?[['overtime','Available for overtime','plus'] as KindChoice]:[]),
    ...(canAsk&&page.wishes?[['wish','Wish','star'] as KindChoice]:[]),
    ...(ctx.me.staff_swap_access?[['swap','Swap a duty','swap'] as KindChoice]:[]),
    ...(canAsk?[['other','Other request','inbox'] as KindChoice]:[]),
  ];
}

/** New request: choose the kind first; its own fields (the same forms used everywhere) appear below. */
function newRequest(base:Context,kinds:KindChoice[],preset?:string,duty?:string,landed?:Promise<KindChoice[]>|null){
  // A send waits for its command only: the dialog closes, then the page refreshes (never inside the save).
  const ctx:Context=Object.assign(Object.create(base),{refresh:()=>{window.setTimeout(()=>void base.refresh());return Promise.resolve();}});
  const coordinator=ctx.permits(CAPABILITY.COORDINATOR);
  const labels:Record<Kind,string>={leave:'Send request',overtime:'Send offer',other:'Send request',record:'Record leave',weekly:coordinator?'Save weekly availability':'Send for approval',wish:'Add wish',swap:'Send offer'};
  // A swap needs a published duty of your own: asked once when the dialog opens, so the kind says so before it is chosen.
  const swaps=kinds.some(k=>k[0]==='swap')?swapOptions():null;
  const d=formDialog(TERMS.newRequest,'',form=>{
    // Once a kind is chosen, a phone keeps only it (and Change) above the fields, so they start at the top. The other
    // kinds are hidden then, so arrow keys reach them only after Change (acceptable on a phone).
    const change=button('Change',()=>{group.classList.add('choosing');group.querySelector<HTMLInputElement>('input:checked')?.focus();},'link-button request-kind-change',{'aria-label':'Change kind'});
    const option=([k,label,glyph]:KindChoice)=>el('label',{class:'request-kind'},el('input',{type:'radio',name:'request-kind',value:k,required:true}),
      el('span',{class:'request-icon','aria-hidden':'true'},icon(glyph)),el('span',{class:'request-kind-text'},el('strong',{},label)));
    const group=el('fieldset',{class:'request-kinds'},el('legend',{},'What do you need?'),...kinds.map(option),change);
    // A kind whose answer lands after the dialog opened (overtime eligibility) joins in its place; a choice made stays.
    const shown=(k:Kind)=>group.querySelector(`input[value="${k}"]`)?.closest('.request-kind');
    void landed?.then(all=>all.forEach((choice,i)=>{if(!shown(choice[0]))(all.slice(i+1).map(c=>shown(c[0])).find(Boolean)||change).before(option(choice));}),()=>{});
    void swaps?.then(o=>{if(o.assignments.length)return;
      const input=group.querySelector<HTMLInputElement>('input[value=swap]'),why=el('small',{class:'muted',id:'request-kind-swap-why'},'No upcoming published duty to offer');
      if(!input||input.checked)return;input.disabled=true;input.setAttribute('aria-describedby',why.id);input.closest('.request-kind')!.classList.add('disabled');input.closest('.request-kind')!.querySelector('.request-kind-text')!.append(why);},()=>{});
    const details=el('div',{class:'form-grid request-details'});
    let save:(()=>Promise<void>)|null=null,serial=0,label='Send request',submitting=false;
    // While it sends, the form's busy button (loading.ts) owns the label and the spinner.
    const relabel=()=>{const b=form.querySelector<HTMLButtonElement>('button[type=submit]');if(b){b.disabled=submitting||!save||b.hasAttribute('aria-busy')||b.hasAttribute('data-held');if(!b.hasAttribute('aria-busy'))b.textContent=label;}};
    const pick=async(k:Kind)=>{
      // The person chosen so far stays when the kind changes (LIVE-47): a single choice or the first of several.
      const held=details.querySelector<HTMLSelectElement>('select[name="Person"]')?.value||details.querySelector<HTMLSelectElement>('.pick select[multiple]')?.selectedOptions[0]?.value
        ||details.querySelector<HTMLInputElement>('fieldset[name="People"] input:checked')?.value||'';
      const mine=++serial;label=labels[k];save=null;form.querySelector('button[type=submit]')?.removeAttribute('data-held');
      relabel();queueMicrotask(relabel);  // the dialog adds its submit button after this first call
      details.replaceChildren(skeleton('panel','Loading the request…'));
      const box=el('div',{class:'form-grid'});
      try{
        const today=toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
        const who=held?{person_id:held}:{};
        const build=k==='leave'?availabilityFields(ctx,box,true,{kinds:['leave'],...who}):k==='other'?availabilityFields(ctx,box,true,{kinds:['clinic_change','meeting','administrative'],...who})
          :k==='record'?availabilityFields(ctx,box,false,who):k==='weekly'?weeklyFields(ctx,box,held):k==='overtime'?await overtimeFields(ctx,box,held)
          :k==='wish'?await (async()=>{const data=await wishData(today,ctx.me.person_id||'');
            if(!data.budget_points){box.append(note('Wishes are switched off for this department.'));return async()=>{throw new Error('Wishes are switched off.');};}
            return wishFields(ctx,box,data,ctx.me.person_id||'',coordinator,today,undefined,ctx.refresh);})()
          :offerFields(ctx,box,await swapOptions(),duty,kinds.some(x=>x[0]==='other')?()=>{const other=group.querySelector<HTMLInputElement>('input[value=other]');if(other){other.checked=true;void pick('other');}}:undefined);
        if(mine!==serial)return;
        // Required fields are marked as the dialog marks its own.
        markFields(box);
        details.replaceChildren(box);
        // A kind with nothing to fill in (no duty or colleague to swap with, wishes switched off) cannot be sent.
        save=box.querySelector('input:not([type=hidden]),select,textarea')?build:null;relabel();
      }catch(e){if(mine===serial&&!sessionChanged(e)){  // SW3-C-20: tried again in place; the sheet and its kind stay
        details.replaceChildren(readFailed(k==='swap'?'the swap options':'the request form',e,()=>void pick(k)));relabel();}}
    };
    group.addEventListener('change',e=>{const k=(e.target as HTMLInputElement).value as Kind;group.classList.remove('choosing');void pick(k);});
    form.append(group,details);
    const first=kinds.find(k=>k[0]===preset)||(kinds.length===1?kinds[0]:null);
    if(first){group.querySelector<HTMLInputElement>(`input[value="${first[0]}"]`)!.checked=true;void pick(first[0]);}
    queueMicrotask(relabel);
    // Guard submission itself too (Enter/requestSubmit), then restore this kind's state after the shared form's finally.
    return async()=>{
      if(submitting||!save){setTimeout(relabel);throw new Error(submitting?'This request is already being sent.':'Choose a request whose details have finished loading.');}
      submitting=true;group.disabled=true;relabel();
      try{await save();}finally{submitting=false;group.disabled=false;setTimeout(relabel);}
    };
  },'Send request');
  if(!preset)d.dialog.querySelector<HTMLInputElement>('input[name=request-kind]')?.focus();
}

/** Weekly availability: a recurring time someone cannot work. Staff propose their own; coordinators record for anyone. */
function weeklyFields(ctx:Context,form:HTMLElement,chosen=''){
  const coordinator=ctx.permits(CAPABILITY.COORDINATOR);
  // Coordinators set the same pattern (e.g. teaching every Tuesday afternoon) for several people at once.
  const people=multi('People',peopleChoices(ctx),chosen?[chosen]:[],'Choose one or more people');
  const days=multi('Days',weekdayOptions());
  const from=field('Not available from','time','13:00',{required:true}),until=field('Until','time','17:00',{required:true});
  const start=field('Starting','date',toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10),{required:true}),end=field('Ending (included, optional)','date');lastDayIncluded(end.input);
  const text=field('Note','text','',{maxLength:200,placeholder:'e.g. school run, teaching'});
  people.wrap.classList.add('span-2');text.wrap.classList.add('span-2');
  form.append(...(coordinator?[people.wrap]:[]),days.wrap,from.wrap,until.wrap,dateRange('Dates',start,end),text.wrap);
  return async()=>{const weekdays=days.values().map(Number);if(!weekdays.length)throw new Error('Choose at least one day.');
    const ids=coordinator?people.values():[ctx.me.person_id!];if(!ids.length)throw new Error('Choose at least one person.');
    const common={weekdays,start_time:from.input.value,end_time:until.input.value,starts_on:start.input.value,ends_on:end.input.value||null,note:text.input.value};
    if(ids.length>1){await runBulk({collection:'availability_patterns',noun:['weekly availability','weekly availability'],message:'Weekly availability saved for {n} people.',onDone:()=>ctx.refresh(),
      body:{item_kind:'availability_pattern.propose',common,items:ids.map(id=>({payload:{person_id:id}})),reason:'Weekly availability for several people'}});return;}
    const r=await command<{message:string}>('availability_pattern.propose',{person_id:ids[0],...common});notify(r.message);await ctx.refresh();};
}

/** Overtime eligibility over the next three months (the server's resolver; staff only see the kind when a line applies).
 *  Coordinators record for anyone, and the server checks each person when the offer is sent. Each showing of Requests
 *  (a visit or a refresh) asks afresh, so a changed rule or a failed read never sticks; its dialog reuses that answer. */
type Choice={id:string;name:string};
/** choices: what the person's lines allow, from the server's resolver; null allows any (staff have no catalogues). */
type Eligibility={eligible:boolean;offers:{starts_on:string;where:Row|null}[];choices:{services:Choice[]|null;assignment_types:Choice[]|null}};
let eligibility:{key:number;value:Promise<Eligibility|null>}|null=null;
/** Names of the clinics and duty types staff may choose, for their own offers' summaries (staff have no catalogues).
 *  shortcut: covers what their lines allow now; an offer naming one no longer allowed shows without it. Upgrade: the
 *  requests API returns each offer's clinic and duty type names. */
const choiceNames=new Map<string,string>();
onSessionReset(()=>choiceNames.clear());
function overtimeEligibility(ctx:Context,fresh=false){
  if(!ctx.me.person_id||!ctx.permits(CAPABILITY.STAFF))return Promise.resolve(null);
  if(!fresh&&eligibility?.key===sessionKey())return eligibility.value;
  const today=toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
  // A background read: never a kept page reply from the last visit (api.ts keptReads), which may predate a new line.
  const mine:{key:number;value:Promise<Eligibility|null>}={key:sessionKey(),value:background<Eligibility>(
    `/api/requests/overtime-eligibility?starts_on=${today}&ends_on=${addDays(today,92)}`).then(e=>{
      for(const r of [...e.choices?.services||[],...e.choices?.assignment_types||[]])choiceNames.set(r.id,r.name);return e;})
    .catch(()=>{if(eligibility===mine)eligibility=null;return null;})};
  eligibility=mine;return mine.value;
}
const overtimeOffered=async(ctx:Context)=>ctx.permits(CAPABILITY.COORDINATOR)||!!(await overtimeEligibility(ctx,true))?.eligible;
/** Available for overtime: chosen dates or every week in a period, optionally only some hours, one clinic or duty type. */
async function overtimeFields(ctx:Context,form:HTMLElement,chosen=''){
  const coordinator=ctx.permits(CAPABILITY.COORDINATOR),zone=ctx.me.organisation.timezone;
  const today=toLocal(new Date().toISOString(),zone).slice(0,10);
  // Staff choose among what their own overtime lines allow (the server's answer); coordinators record for anyone from the
  // department's lists, and the server checks the person when the offer is sent.
  const allowed=coordinator?null:(await overtimeEligibility(ctx))?.choices;
  const choices=(list:'services'|'assignment_types')=>{const own=allowed?.[list];
    return own?own.map(r=>({value:r.id,label:r.name})):(ctx.catalogues[list]||[]).filter(r=>r.active!==false).map(r=>({value:r.id,label:labelOf(r)}));};
  const person=coordinator?select('Person',peopleChoices(ctx),chosen,true,'Choose a person'):null;
  const mode=select('Offer',[{value:'dates',label:'On chosen dates'},{value:'weekly',label:'Every week'}],'dates',true);
  // The one list of separate dates (chips, calendar, Shift-click ranges, paste), as everywhere else.
  const picked=dateList('Dates',[today]),dates=picked.wrap;
  const days=multi('Days',weekdayOptions());
  const start=field('Starting','date',today,{required:true}),end=field('Ending (included)','date',addDays(today,27),{required:true});lastDayIncluded(end.input);
  const period=dateRange('Period',start,end);
  const allDay=check('All day',true),from=field('From','time','08:00',{required:true}),until=field('Until','time','16:00',{required:true});
  const clinic=select('Clinic',choices('services'),'',false,'Any clinic allowed'),duty=select('Duty type',choices('assignment_types'),'',false,'Any duty type allowed');
  const text=area('Note (optional, seen by coordinators)','',2);text.input.maxLength=1000;
  text.wrap.classList.add('span-2');allDay.wrap.classList.add('span-2');
  const show=()=>{const weekly=mode.input.value==='weekly';dates.hidden=weekly;days.wrap.hidden=period.hidden=!weekly;
    start.input.required=end.input.required=weekly;
    from.wrap.hidden=until.wrap.hidden=allDay.input.checked;from.input.required=until.input.required=!allDay.input.checked;};
  mode.input.addEventListener('change',show);allDay.input.addEventListener('change',show);show();
  form.append(...(person?[person.wrap]:[]),mode.wrap,dates,days.wrap,period,allDay.wrap,from.wrap,until.wrap,clinic.wrap,duty.wrap,text.wrap);
  return async()=>{
    const weekly=mode.input.value==='weekly';
    const chosen=picked.values();
    const weekdays=days.values().map(Number);
    if(weekly&&!weekdays.length)throw new Error('Choose at least one day.');
    if(!weekly&&!chosen.length)throw new Error('Choose at least one date.');
    const person_id=person?person.input.value:ctx.me.person_id!;if(!person_id)throw new Error('Choose a person.');
    // Days no overtime line covers are named before sending (LIVE-45): the server would refuse the whole offer.
    const asked=weekly?dateRangeDays(start.input.value,end.input.value).filter(d=>weekdays.includes((new Date(d+'T12:00:00Z').getUTCDay()+6)%7)):chosen;
    const can=asked.length?await get<Eligibility>(`/api/requests/overtime-eligibility?starts_on=${asked[0]}&ends_on=${addDays(asked[asked.length-1],1)}${person?'&person_id='+encodeURIComponent(person_id):''}`).catch(()=>null):null;
    const missing=can?asked.filter(d=>!can.offers.some(o=>o.starts_on===d)):[];
    if(missing.length)throw new Error(`${person_id===ctx.me.person_id?'You':fullNameOf(ctx.catalogues.people?.find(p=>p.id===person_id))} can’t be offered overtime on ${missing.slice(0,3).map(d=>formatDate(d)).join(', ')}${missing.length>3?` and ${missing.length-3} more days`:''}: no overtime rule covers ${missing.length>1?'these days':'that day'}.`);
    const r=await command<{message:string}>('request.create',{kind:'overtime_availability',person_id,
      starts_on:weekly?start.input.value:chosen[0],ends_on:weekly?end.input.value:addDays(chosen[chosen.length-1],1),
      ...(weekly?{weekdays}:{dates:chosen}),...(allDay.input.checked?{}:{start_time:from.input.value,end_time:until.input.value}),
      service_id:clinic.input.value||null,assignment_type_id:duty.input.value||null,note:text.input.value});
    notify(r.message);await ctx.refresh();};
}
/** Each day from `start` up to (not including) `end`. */
function dateRangeDays(start:string,end:string){const out:string[]=[];for(let d=start;d&&end&&d<end&&out.length<400;d=addDays(d,1))out.push(d);return out;}
/** An offer's days, hours and scope in words ("Tue, Thu · 16:00–22:00 · Urgent Care"). */
function overtimeWords(ctx:Context,r:Row){
  const o=r.overtime||{},name=(list:string,id:string|null)=>{if(!id)return '';const r=ctx.catalogues[list]?.find(x=>x.id===id);return r?labelOf(r):choiceNames.get(id)||labelOf(undefined);};
  const days=o.weekdays?.length?'Every '+weekdayNames(o.weekdays,true):plural((o.dates||[]).length,'date');
  const hours=o.start_time&&!(o.start_time==='00:00'&&o.end_time==='24:00')?`${o.start_time}–${o.end_time}`:'all day';
  return [days,hours,name('services',o.service_id),name('assignment_types',o.assignment_type_id)].filter(Boolean).join(' · ');
}

// ---- Words for a row ----
/** The panel's subtitle: type and person, without repeating the title ("Leave" over "Leave · Alex"). */
/** The kind without its leave type ("Approve leave", not "Approve annual"). */
const kindTitle=(r:Row)=>title({...r,leave_type_id:null});
const panelSub=(r:Row,own=false)=>[kindWord(r),own?'':r.person_name||r.calendar_entry_name].filter(x=>x&&x!==title(r)).join(' · ');
/** The reader's own request (not a swap, which always has two people). */
const ownRequest=(ctx:Context,r:Row)=>r.source!=='swap'&&!!ctx.me.person_id&&r.person_id===ctx.me.person_id;
function title(r:Row){
  if(r.source==='scheduling_fact')return String(r.title);
  // Leave reads as its type ("Annual") as far as the department's leave setting allows the reader.
  if(isAbsenceReport(r))return absenceReason(r);
  if(r.source==='request')return (r.kind==='leave'&&leaveTypeName(r))||REQUEST_KIND_WORDS[r.kind as keyof typeof REQUEST_KIND_WORDS]||'Request';
  if(r.source==='availability')return leaveTypeName(r)||RECORD_KINDS[r.kind]||'Unavailable';
  if(r.source==='pattern')return 'Every week';
  if(r.source==='wish')return r.duty_type?`${wishKindLabel(r.kind,true)==='Not'?'Not':'On'} ${r.duty_type}`:wishKindLabel(r.kind)+(r.limit!=null?` (${r.limit})`:'');
  if(r.open_offer&&!r.recipient_person_id)return r.kind==='give'?'Open give-away':'Open swap';
  return `${r.proposer_name} and ${r.recipient_name}`;
}
/** Whole days read as dates ("20 – 21 Oct 2026 · all day"); anything else keeps its times. */
function spanParts(start:string,end:string,zone:string):string[]{
  const a=toLocal(start,zone),b=toLocal(end,zone);
  if(a.slice(11,16)==='00:00'&&b.slice(11,16)==='00:00'){const last=addDays(b.slice(0,10),-1);return [formatDate(a.slice(0,10),zone),...(last===a.slice(0,10)?[]:['– '+formatDate(last,zone)]),'· all day'];}
  return a.slice(0,10)===b.slice(0,10)?[formatDate(start,zone),`· ${formatTime(start,zone)}–${formatTime(end,zone)}`]:[formatDate(start,zone),formatTime(start,zone),'– '+formatDate(end,zone),formatTime(end,zone)];
}
function whenParts(r:Row,zone:string):string[]{
  // Offered dates read as the dates themselves, not the period around them.
  if(isOvertime(r)&&r.overtime){const o=r.overtime,d=o.dates?.length?[...o.dates as string[]].sort():[o.starts_on,addDays(o.ends_on,-1)];
    return [formatDate(d[0]),...(d[d.length-1]!==d[0]?['– '+formatDate(d[d.length-1])]:[])];}
  if(r.source==='request'||r.source==='availability')return spanParts(r.start,r.end,zone);
  if(r.source==='pattern')return ['From '+formatDate(r.starts_on),...(r.ends_on?['to '+formatDate(addDays(r.ends_on,-1))]:[])];
  if(r.source==='wish')return wishWhenParts(r);
  if(r.source==='scheduling_fact')return factWhenParts(r);
  return r.offered_assignment?spanParts(r.offered_assignment.start,r.offered_assignment.end,zone):[];
}
const when=(r:Row,zone:string)=>whenParts(r,zone).join(' ');
/** A request's first and last day (both included) in the department's calendar: what Dates · Start / End filter on. */
const firstDay=(r:Row,zone:string)=>{const v=r.start||r.offered_assignment?.start;return v?toLocal(v,zone).slice(0,10):r.first_day||r.starts_on||'';};
const lastDay=(r:Row,zone:string)=>{const v=r.end||r.offered_assignment?.end;
  if(v){const t=toLocal(v,zone);return t.slice(11,16)==='00:00'?addDays(t.slice(0,10),-1):t.slice(0,10);}
  if(r.source==='pattern'||r.source==='scheduling_fact')return r.ends_on?addDays(r.ends_on,-1):'';  // ends_on is exclusive; none: End is empty
  return r.last_day||firstDay(r,zone);};
export const requestWhenNode=(r:Row,zone:string)=>dateUnits(whenParts(r,zone));
export {title as requestTitle,when as requestWhen,firstDay as requestFirstDay,lastDay as requestLastDay,sortKey as requestSortKey};
// Dates never act on the day a fact was proposed: undated rows have no Start, End or date to sort on (the server's
// sort_by=when reads a fact's starts_on too, undated last); Proposed is its own property.
const proposedAt=(r:Row)=>String(r.proposed_at||r.created_at||'');
const sortKey=(r:Row)=>String(r.start||r.first_day||r.starts_on||r.offered_assignment?.start||'');
function detail(r:Row,zone:string,ctx?:Context){
  if(isOvertime(r))return ctx?overtimeWords(ctx,r):'';
  if(r.source==='scheduling_fact')return [...factWords(r),'proposed by '+r.proposer_name].join(' · ');
  if(r.source==='pattern')return `${weekdayNames(r.weekdays,true)} · ${r.start_time}–${r.end_time}${r.note?' · '+r.note:''}`;
  if(r.source==='wish')return `${plural(r.points,'point')}${r.note?' · '+r.note:''}`;
  if(r.source==='swap'){const duty=(a:Row|null)=>a?`${a.type} ${formatDate(a.start,zone)}`:'a duty in return';return `${duty(r.offered_assignment)}${r.kind==='give'?' given away':' for '+duty(r.counteroffered_assignment)}${r.reason?' · '+r.reason:''}`;}
  // Recorded leave: its note when the department shows reviewers the details (the server decides); its type only when
  // the title does not already say it ("Annual" / "Details: Annual" said it twice).
  if(r.source==='availability')return [leaveTypeName(r)===title(r)?'':leaveTypeName(r),r.private_reason].filter(Boolean).join(' · ');
  if(r.source==='request'&&r.kind==='rotation_change')return r.summary||'';
  // SX-08: a Can't make it says what the server disclosed to this reader (leave_reason_display): its category, its note.
  if(isAbsenceReport(r))return absenceWords(r.details?.absence_reason,r.details?.note);
  if(r.source==='request'&&r.rota_status)return 'Linked rota: '+statusLabel(r.rota_status).toLowerCase();
  // An approved clinic change or meeting needs a coordinator to put it in a rota (Link rota) until one is linked.
  if(r.source==='request'&&r.stage==='approved'&&['clinic_change','meeting'].includes(r.kind))return 'Scheduling action pending';
  return '';
}
/** Duties on rotas still being planned that the request overlaps (schedule_impact: sent to coordinators and approvers). */
const draftDuties=(r:Row)=>Number(r.schedule_impact?.draft_count)||0;
/** Whether approving it changes any rota, published or still being planned. */
const affects=(r:Row)=>!!r.impact_count||draftDuties(r)>0;
/** What the request does to the rotas, in words: the published places it opens and the first days, then the duties on
 *  rotas still being planned (so a reviewer never approves leave blind to a draft it breaks). */
/** Your own request on days you are on duty: said to you plainly ("You're on duty Thu 8 Oct"), not as the reviewer's
 *  "Approving opens 1 place". '' when it touches none of your duties. */
function ownDuty(r:Row){
  const days=[...new Set(((r.schedule_impact?.duties||r.impact||[]) as Row[]).map(i=>i.day))].sort();
  return days.length?`You're on duty ${days.slice(0,2).map(shortDay).join(', ')}${days.length>2?' '+moreWords(days.length-2):''}`:'';
}
function impactWords(r:Row,own=false){
  if(r.stage!=='waiting'&&r.stage!=='approved')return '';
  const n=r.impact_count;if(n===undefined)return '';
  if(own&&ownDuty(r))return ownDuty(r);
  const drafts=draftDuties(r);if(!n&&!drafts)return r.stage!=='waiting'?'':r.schedule_impact?'No duty affected':'No published duty affected';
  const days=[...new Set((r.impact as Row[]).map(i=>i.day))],list=days.slice(0,2).map(shortDay).join(', ')+(days.length>2?' '+moreWords(days.length-2):'');
  const duties=(r.schedule_impact?.duties||[]) as Row[];
  const rotaNames=(published:boolean)=>[...new Set(duties.filter(d=>Boolean(d.publication_id)===published&&d.rota_name).map(d=>String(d.rota_name)))].join(', ');
  const publishedRotas=n?rotaNames(true):'',published=statusLabel('published');
  return [n?(r.stage==='waiting'?`Approving opens ${plural(n,'place')} · ${list}`:`${plural(n,'place')} to cover · ${list}`):'',
    n?(publishedRotas?`${publishedRotas} (${published})`:published):'',
    drafts?`${plural(drafts,'duty','duties')} in ${rotaNames(false)||'a rota being planned'} (not yet published)`:''].filter(Boolean).join(' · ');
}
/** Every rota duty it overlaps, published or still being planned (staff see only their published ones). */
function scheduleWords(s:Row){
  if(!s.count)return 'No duty affected';
  return `Affects ${plural(s.count,'duty','duties')} · `+(s.draft_count?`${s.published_count} published, ${s.draft_count} not yet published`:'published');
}
const publishedCover=(r:Row)=>r.source==='request'&&r.impact_count?'Approving records the absence. The coordinator arranges cover for its published duties.':'';
/** Why a request is where it is (the status tip and the side panel). */
function stageDetail(r:Row,ctx?:Context){
  if(r.source==='swap')return swapNext(r,ctx);
  if(r.source==='wish')return r.stage==='open'?(r.locked?'The rota for these days has been submitted; the outcome comes with publication.':'Auto-schedule tries to grant it; the outcome comes with publication.')
    :r.outcome==='partly'?'Partly granted':r.stage==='approved'?'Granted':r.stage==='declined'?'Not granted; later wishes count for more':'';
  if(r.source==='availability')return r.stage==='withdrawn'?'Retracted; it no longer counts'
    :r.counts_as_unavailable===false?'Recorded; does not block duty assignment':'Recorded; counts in every rota check';
  if(r.source==='scheduling_fact')return r.proposed_by===ctx?.me.identity_id&&!r.can_decide?'You proposed it; another approver decides'
    :r.can_decide||!ctx?.permits(CAPABILITY.APPROVER)?'An approver decides':'Another approver decides: you cannot approve a change about yourself';
  // Your own request is decided by someone else (an independent decision), so it offers you no Approve.
  if(r.stage==='waiting'&&ctx&&!mayDecide(ctx.me,'request.decide',ownDecision(ctx,r))&&hasAccess(ctx.me.roles,capabilityForRole(decider(r)))) return `Another ${decider(r)} decides: you can't approve your own request`;
  if(r.stage==='waiting')return decider(r)===roleForCapability(CAPABILITY.COORDINATOR)?'A coordinator decides':r.kind==='rotation_change'?'A programme lead decides':'An approver decides';
  if(isOvertime(r)&&r.stage==='approved')return 'Overtime may be offered on these days, within the department’s limits. It does not promise a duty.';
  return r.decision_reason||'';
}

// ---- Actions ----
/** A person's own request taken back (request.decide withdraw; maintenance: the same for a "Can't make it" report). */
export const withdrawRequest=(r:{id:string;revision:number},reason='Withdrawn')=>command('request.decide',{decision:'withdraw',reason},r.id,r.revision);
/** Withdraw on a "Can't make it" report waiting for cover (Home's My requests and this list): one press, the coordinator
 *  is told. */
export function withdrawReport(r:Row,after:()=>void){
  // Home's summary row has no revision: the request's own row is read first.
  return button('Withdraw',async()=>{const revision=r.revision??(await get<{items:Row[]}>('/api/requests/all?id='+encodeURIComponent(r.id))).items[0]?.revision;
    await withdrawRequest({id:r.id,revision});notify('Report withdrawn.');after();},'ghost small',{'aria-label':`Withdraw: ${absenceReason(r)}`});
}
const mine=(ctx:Context,r:Row)=>r.created_by===ctx.me.identity_id||(!!ctx.me.person_id&&r.person_id===ctx.me.person_id);
// Approval follows the request's subjects. Authorship also matters when withdrawing.
const ownDecision=(ctx:Context,r:Row)=>ownRequest(ctx,r)||(!!ctx.me.person_id&&r.source==='request'&&r.details?.swap?.person_id===ctx.me.person_id);
/** Requests, weekly availability and facts this person may decide now: the server says so per row (can_decide), with the
 *  same check the decision enforces (approver or programme-lead scope, coordinator for patterns, self-approval permission). */
function decidable(r:Row){
  return r.can_decide===true&&(r.source==='scheduling_fact'||r.stage==='waiting');
}
const decide=(r:Row,approve:boolean,reason:string)=>r.source==='request'?command('request.decide',{decision:approve?'approve':'reject',reason},r.id,r.revision)
  :command('availability_pattern.decide',{decision:approve?'approve':'decline',reason},r.id,r.revision);
/** Recorded leave and availability (coordinators): the universal Change… (retype, re-date, retract) for the records
 *  selected, previewed and applied as one change with one Undo. Operator bulk is desktop only. */
function recordedChange(ctx:Context,rows:Row[],table:string):BulkAction[]{
  const recorded=rows.filter(r=>r.source==='availability');
  if(!recorded.length||!ctx.permits(CAPABILITY.COORDINATOR)||phoneQuery.matches)return [];
  return [{label:recorded.length<rows.length?`Change ${recorded.length} recorded…`:'Change…',icon:'edit',primary:true,
    run:(_:string[],done:()=>void)=>openChange({collection:'availability',noun:['leave record','leave records'],ids:recorded.map(r=>r.id),
      revision:id=>recorded.find(r=>r.id===id)?.revision,table,onDone:async()=>{done();await ctx.refresh();}})}];
}
/** Approve or decline several atomically with one reason and one Undo (request.decide_many); each has its own audit event. */
function bulkDecide(ctx:Context,rows:Row[]):BulkAction[]{
  // A fact is approved after its own feasibility review, so it is decided one at a time.
  const open=rows.filter(r=>r.source!=='scheduling_fact'&&decidable(r));if(!open.length)return [];
  return ([true,false] as const).map(approve=>({label:`${approve?'Approve':'Decline'} ${open.length}`,icon:approve?'check' as const:'close' as const,danger:!approve,
    run:(_:string[],done:()=>void)=>{
      if(open.length>MAX_REQUEST_DECISIONS){notify(`Select at most ${MAX_REQUEST_DECISIONS} requests for one decision.`,'error');return;}
      // One Decline many at every width (reason, schedule impact, armed red confirm); Approve takes an optional note,
      // as a single Approve does.
      if(!approve){declineMany(ctx,open,done);return;}
      confirmAction(`Approve ${plural(open.length,'request')}`,'Each person is told.',
        reason=>holdUndo(async()=>{const reply=await command<{records:Row[]}>('request.decide_many',{
          items:open.map(r=>({id:r.id,expected_revision:r.revision,source:r.source})),decision:'approve',reason});
          done();notify(`${reply.records.length} requests approved.`);await ctx.refresh();}),'Approve all',undefined,true,false,'Reason',true);}}));
}
/** Decline several: the required reason, what it does to the schedule, and the armed confirm; a dialog on a desktop and
 *  a bottom sheet on a phone. */
function declineMany(ctx:Context,open:Row[],done:()=>void){
  const {dialog}=showDialog(`Decline ${plural(open.length,'request')}`,'Each person is told.',(body,close,tryClose)=>{
    const reason=area('Reason','',3);reason.input.required=true;reason.input.minLength=3;
    const places=open.reduce((n,r)=>n+(Number(r.impact_count)||0),0),affected=open.filter(affects).length;
    const impact=el('div',{class:'bulk-sheet-impact',role:'status'},icon(affected?'alert':'check'),
      affected?`${plural(affected,'request')} touch the schedule${places?` · ${plural(places,'place')}`:''}. Declining keeps those duties as they are.`:'No duty is affected.');
    // One decision in flight: the button says Saving… and cannot send it twice.
    const go=armed(button(`Decline ${open.length}`,async()=>{if(!reason.input.reportValidity())return;
      go.disabled=true;go.replaceChildren('Saving…');
      try{await holdUndo(async()=>{const reply=await command<{records:Row[]}>('request.decide_many',{items:open.map(r=>({id:r.id,expected_revision:r.revision,source:r.source})),decision:'reject',reason:reason.input.value});
        close();done();notify(`${reply.records.length} requests declined.`);await ctx.refresh();});}
      finally{if(go.isConnected){go.disabled=false;go.replaceChildren(`Decline ${open.length}`);}}},'danger'),`Confirm decline ${open.length}?`);
    body.append(reason.wrap,impact,el('div',{class:'form-footer'},button('Cancel',tryClose,'secondary'),go));reason.input.focus();});
  if(phoneQuery.matches)dialog.classList.add('bulk-sheet');
}
/** A row's next step for this person (buttons) and the rest (the ⋯ menu); the side panel shows both. */
function actions(ctx:Context,r:Row,hooks:RequestHooks):{buttons:HTMLElement[];menu:RowAction[]}{
  const coordinator=ctx.permits(CAPABILITY.COORDINATOR),buttons:HTMLElement[]=[],menu:RowAction[]=[];
  const who=r.person_name||'They';
  if(r.source==='scheduling_fact'&&decidable(r)){
    const family=FACT_FAMILY[r.kind],zone=ctx.me.organisation.timezone,said=[when(r,zone),detail(r,zone)].filter(Boolean).join(' · ');
    buttons.push(button('Approve',async()=>{const {fact}=await get<{fact:Row}>(`/api/requests/all?id=${encodeURIComponent(r.id)}`);
      await ruleReview({...fact,name:title(r)},true,true,ctx.refresh,(kind,id)=>labelOf(ctx.catalogues[kind]?.find(x=>x.id===id)),ctx.me.organisation.timezone,false,[],title(r),said,ctx,family);},'secondary small'),
      button('Decline',()=>confirmAction(`Decline ${title(r).toLowerCase()}`,`${r.person_name||r.calendar_entry_name||''} · ${said}. Nothing changes; the proposal stays in the history.`,
        async reason=>{await command(`${family}.decline`,{reason},r.id,r.revision);notify('Declined.');void ctx.refresh();},'Decline'),'ghost small'));
  }else if(decidable(r)){
    // A decision's dialog closes once it is saved; the list reloads behind it (LIVE-57), never under a "Saving…" dialog.
    const self=ownDecision(ctx,r),cover=publishedCover(r);
    const words=r.source==='pattern'?'counts as unavailable at these times in every rota check':isOvertime(r)?'may be offered overtime on these days, within the department’s limits':impactWords(r)||'is told';
    buttons.push(button('Approve',()=>confirmAction(`Approve ${kindTitle(r).toLowerCase()}`,`${who} · ${when(r,ctx.me.organisation.timezone)}. ${words[0].toUpperCase()+words.slice(1)}.${cover?' '+cover:''}${r.source==='request'&&draftDuties(r)?' On rotas being planned, each overlapping duty shows as a conflict to replace.':''}`,
      async reason=>{await decide(r,true,reason);notify(r.source==='pattern'?'Weekly availability approved.':`Approved · ${who} is told.`);void ctx.refresh();},'Approve',self?selfApprovedBadge():undefined,!self,false,'Reason',true),'secondary small'),
      button('Decline',()=>confirmAction(`Decline ${kindTitle(r).toLowerCase()}`,'Say why, so they can plan around it.',async reason=>{await decide(r,false,reason);notify(r.source==='pattern'?'Weekly availability declined.':`Declined · ${who} is told.`);void ctx.refresh();},'Decline',self?selfApprovedBadge():undefined),'ghost small'));
  }
  // An approved (or later retired) calendar protection is managed (Retire, Undo, Restore) in its own panel, reached from its
  // request in the Decided history: an approver needs no Programmes access for it (JC-01). The panel offers only what this role may do.
  if(r.source==='scheduling_fact'&&r.kind==='protected_time'&&['active','retired'].includes(r.status)&&r.calendar_entry_id&&ctx.permits(CAPABILITY.APPROVER))
    buttons.push(button('Manage protection',()=>calendarProtectionPanel(ctx,ctx.catalogues.programme_calendar_entries?.find(e=>e.id===r.calendar_entry_id)||{id:r.calendar_entry_id,name:r.calendar_entry_name} as Row),'secondary small'));
  if(r.source==='swap'){const s=swapActions(ctx,r);buttons.push(...s.buttons);menu.push(...s.menu);}
  const editableWish=r.source==='wish'&&r.stage==='open'&&!r.locked&&(coordinator||r.person_id===ctx.me.person_id);
  if(editableWish){
    if(oneOffWish(r))buttons.push(button('Change',async()=>{const data=await wishData(r.first_day,r.person_id);
      formDialog('Change wish',`Wish points: ${data.budget_points} per ${data.period}.`,form=>wishFields(ctx,form,data,r.person_id,false,r.first_day,r,ctx.refresh),'Save wish');},'ghost small'));
    menu.push({label:'Withdraw wish',icon:'close',danger:true,run:async()=>{await command('wish.withdraw',{reason:'Withdrawn'},r.id,r.revision);notify('Wish withdrawn. Its points are free again.');await ctx.refresh();}});
  }
  // Their own "Can't make it" waiting for cover: Withdraw is one press on the row (the coordinator is told), with Undo.
  if(isAbsenceReport(r)&&r.stage==='waiting'&&mine(ctx,r))buttons.push(withdrawReport(r,()=>void ctx.refresh()));
  else if(r.source==='request'&&(r.stage==='waiting'||isOvertime(r)&&r.stage==='approved')&&r.created_by===ctx.me.identity_id)menu.push({label:isOvertime(r)?'Withdraw offer':'Withdraw request',icon:'close',danger:true,run:()=>confirmAction(isOvertime(r)?'Withdraw offer':'Withdraw request',`${title(r)} · ${when(r,ctx.me.organisation.timezone)}. ${isOvertime(r)?`${r.person_id===ctx.me.person_id?'You':r.person_name||'They'} will no longer be offered overtime on these days.`:'It will no longer wait for a decision.'}`,
    async reason=>{await command('request.decide',{decision:'withdraw',reason},r.id,r.revision);notify(isOvertime(r)?'Offer withdrawn.':'Request withdrawn.');await ctx.refresh();},isOvertime(r)?'Withdraw offer':'Withdraw request',undefined,true)});
  if(r.source==='request'&&r.stage==='approved'&&coordinator&&!isOvertime(r))menu.push({label:r.workspace_id?'Change rota link':'Link rota',icon:'calendar',run:()=>linkRota(ctx,r)});
  // A rare correction: in the menu, not a button on every row.
  const record=r.source==='availability'?(r.stage==='approved'?{id:r.id,revision:r.revision}:null):r.record_id&&!r.record_retracted?{id:r.record_id,revision:r.record_revision}:null;
  // Single edit of what the Bulk editor changes (M1): the record's own edit command and roles (editRoles).
  if(r.source==='availability'&&r.stage==='approved'&&ctx.permits(...(editRoles('availability')||[])))menu.push({label:'Change leave record',icon:'edit',run:()=>availabilityEditForm(ctx,r,ctx.refresh)});
  if(record&&coordinator)menu.push({label:'Retract leave record',icon:'close',danger:true,run:()=>confirmAction('Retract leave record','A correction: the original stays in the history. Coverage is checked again.',
    async reason=>{await command('availability.retract',{reason},record.id,record.revision);notify('Record retracted.');await ctx.refresh();},'Retract',undefined,true)});
  if(r.source==='pattern'&&['waiting','approved'].includes(r.stage)&&(coordinator||r.person_id===ctx.me.person_id))menu.push({label:'End this pattern',icon:'close',danger:true,run:()=>confirmAction('End weekly availability','From now on these times are no longer blocked.',
    async reason=>{await command('availability_pattern.end',{reason},r.id,r.revision);notify('Weekly availability ended.');await ctx.refresh();},'End pattern',undefined,true)});
  const first=(r.schedule_impact?.duties||r.impact||[])[0];
  if(first)menu.push({label:'Show in Schedule',icon:'calendar',run:()=>hooks.showDay(first.day,r.person_id)});
  return {buttons,menu};
}

/** Link the draft or amendment handling an approved request (traceability; review and publication stay separate). */
async function linkRota(ctx:Context,request:Row){
  const zone=ctx.me.organisation.timezone,start=toLocal(request.start,zone).slice(0,10),end=addDays(toLocal(request.end,zone).slice(0,10),1);
  const data=await get(`/api/workspaces?start=${start}&end=${end}`);
  const work:Row[]=data.workspaces.filter((w:Row)=>w.status!=='superseded');
  if(request.workspace_id&&!work.some(w=>w.id===request.workspace_id))work.unshift((await rotaMeta(request.workspace_id)).workspace);
  formDialog('Link request to rota','Link the draft or amendment handling this request. Its duties still need review and publication.',form=>{
    const workspace=select('Related rota',[{value:'',label:'No linked rota'},...work.map(w=>({value:w.id,label:`${w.name} · ${formatDate(w.starts_on)} · ${statusLabel(w.status)}`}))],request.workspace_id||'',false);
    const reason=area('Link reason','');reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    form.append(workspace.wrap,reason.wrap);
    if(data.has_more)form.append(note('Showing the latest 100 rotas in this period.','warning'));
    return async()=>{const before=request.workspace_id||null;
      const r=await command<{record:Row}>('request.link_rota',{workspace_id:workspace.input.value||null,reason:reason.input.value},request.id,request.revision);
      // No server reverse: Undo links the rota it had before (maintenance).
      const undo=()=>command('request.link_rota',{workspace_id:before,reason:'Undo'},r.record.id,r.record.revision).then(()=>{notify('Undone. The previous link is back.');return ctx.refresh();},e=>notify((e as Error).message,'error',undefined,e));
      notify('Related rota saved.','success',{label:'Undo',run:undo});await ctx.refresh();};
  },'Save link');
}

// ---- Wish points ----
/** Wish calendar and its help belong to the collection toolbar; staff keep their current points visible.
 *  The calendar opens below its controls (not a dialog, so Undo stays reachable) and stays open across redraws. */
let calendarOpen=false;
function wishControls(ctx:Context,w:WishPoints){
  const own=w.used!==undefined;
  const box=el('div',{id:'request-wish-calendar',class:'request-wish-calendar'}),summary=own?wishSummary(w):null;
  // Open, the calendar's own meter (for the month it shows) is the one shown.
  const show=()=>{box.hidden=!calendarOpen;if(summary)summary.hidden=calendarOpen;calendar.setAttribute('aria-expanded',String(calendarOpen));if(!calendarOpen){box.replaceChildren();return;}
    box.replaceChildren(skeleton('lines'));
    wishesPanel(ctx,ctx.refresh).then(n=>box.replaceChildren(n),e=>box.replaceChildren(readFailed('the wish calendar',e,show)));};
  // An icon tool like View settings (name and tooltip "Wish calendar"); the open calendar shows the points.
  const calendar=el('button',{type:'button',class:'dt-tool','aria-controls':'request-wish-calendar','aria-label':'Wish calendar','data-tip':'Wish calendar'},icon('calendar'));
  calendar.addEventListener('click',()=>{calendarOpen=!calendarOpen;show();});
  show();
  return {actions:el('div',{class:'request-wishes',role:'group','aria-label':own?'Wish points':'Wishes'},summary,calendar),calendar:box};
}
function wishSummary(w:WishPoints){
  return el('span',{class:'request-wish-summary'},el('meter',{min:0,max:w.budget_points,value:w.used,low:Math.ceil(w.budget_points*0.7),high:w.budget_points,optimum:0,'aria-label':`${w.used} of ${w.budget_points} wish points used in ${periodName(w.period,w.start)}`}),
    // maintenance: the line names what it counts (the request type's own word); "N left" would say the numbers twice (N-12).
    el('span',{class:'request-wish-words'},`${REQUEST_TYPES.wish} points: ${w.used} of ${w.budget_points} used in ${periodName(w.period,w.start)}`),
    (w.multiplier||1)>1?badge(`Next wishes count ×${w.multiplier}`,'warn'):null);
}

// ---- Side panel ----
/** Both swap parties remain individually reachable, including a received request's proposer. A list cell names them
 *  with the department's label (`labelled`); the panel's facts keep the full name. */
/** A calendar fact belongs to a calendar entry, not a person: its name is said with the type. */
const calendarOnly=(r:Row)=>r.source==='scheduling_fact'&&!r.person_id?String(r.calendar_entry_name||''):'';
function requestPeople(ctx:Context,r:Row,labelled=false):Node|string{
  if(r.source==='scheduling_fact'&&!r.person_id)return '—';
  const people=r.source==='swap'?[[r.proposer_person_id,r.proposer_name],[r.recipient_person_id,r.recipient_name]].filter(([id])=>id):[[r.person_id,r.person_name]];
  return el('span',{},...people.flatMap(([id,name],i)=>[...(i?[' · ']:[]),id?personLink(id,labelled&&name?personName(id,name):name||'Person',ctx.navigate):name||'—']));
}
const EVENTS:Record<string,string>={'request.link_rota':'Rota linked','request.create':'Submitted','request.withdraw':'Withdrawn','wish.create':'Added',
  'availability.record':'Recorded','availability.update':'Changed','availability.restore':'Restored','availability.undo_create':'Removed','availability_pattern.propose':'Proposed','availability_pattern.restore':'Restored','availability.retract':'Retracted','availability_pattern.end':'Ended','wish.update':'Changed','wish.withdraw':'Withdrawn','wish.restore':'Restored',
  'swap.offered':'Offered','swap.accept':'Accepted','swap.counteroffer':'Offered a duty in return','swap.confirm':'Confirmed','swap.prepare':'Rota change prepared',
  'swap.prepare_refused':'Not prepared automatically · needs a coordinator','swap.publish_refused':'Not published automatically · needs a coordinator','swap.published':'Published',
  'swap.withdraw':'Withdrawn','swap.withdrawn':'Withdrawn','swap.reject':'Declined','swap.rejected':'Declined',
  'request.undo':'Decision undone','availability_pattern.undo':'Decision undone','swap.undo':'Step undone','swap.reopened':'Open again'};
/** A decision's own word comes from where the request now stands; once a decision was undone, that is no longer known. */
const FACT_EVENTS:Record<string,string>={propose:'Proposed',activate:'Approved',decline:'Declined',undo:'Undone',retire:'Retired'};
export function eventWord(r:Row,action:string,undone=false){
  const fact=/^(?:person\.workload|person\.overtime|calendar\.protection)\.(\w+)$/.exec(action);if(fact)return FACT_EVENTS[fact[1]]||'Changed';
  if(action==='created'&&r.source==='scheduling_fact')return 'Proposed';
  if(action==='created')return r.source==='availability'?'Recorded':r.source==='wish'?'Added':r.source==='pattern'?'Proposed':'Submitted';
  if(action.endsWith('.decide')&&(undone||r.stage==='waiting'))return 'Decided';
  if(action==='request.decide')return stageWord(r.stage);
  if(action==='availability_pattern.decide')return r.stage==='declined'?'Declined':'Approved';
  // maintenance: a verb this list does not know reads as a plain change, never the raw command name.
  return EVENTS[action]||'Changed';
}
/** One request in full: status, facts, schedule impact, actions and history. Its address is #requests?open=<id>. */
function showPanel(ctx:Context,row:Row|null,hooks:RequestHooks,setQuery:(change:(q:URLSearchParams)=>void)=>void,id=row?.id||'',position?:PanelPosition){
  dockPanel({key:'request',className:'request-panel',title:row?title(row):'Request',sub:row?panelSub(row,ownRequest(ctx,row)):'',position,mark:rowMark(id),
    // Closed here (not by leaving the page): the address no longer opens it.
    onClose:leaving=>{if(panel?.id===id){panel=null;if(!leaving)setQuery(q=>q.delete('open'));}},
    build:d=>fillPanel(ctx,id,hooks,setQuery,d)});
}
function fillPanel(ctx:Context,id:string,hooks:RequestHooks,setQuery:(change:(q:URLSearchParams)=>void)=>void,d:Dock){
  const zone=ctx.me.organisation.timezone;
  d.body.append(skeleton('lines'));
  const self={id,close:d.close};panel=self;
  setQuery(q=>q.set('open',id));
  const fill=async()=>{
    let data:{item:Row;history:Row[]};
    try{data=await get(`/api/requests/all?id=${encodeURIComponent(id)}`);}
    catch(e){if(panel===self)d.body.replaceChildren(readFailed('the request',e,fill));return;}
    if(panel!==self)return;
    const r=data.item,{buttons,menu}=actions(ctx,r,hooks),own=ownRequest(ctx,r);
    d.setHead(title(r),panelSub(r,own));
    // Your own request does not name you again; a reviewer sees whose it is.
    const facts:[string,Node|string][]=[...(own?[]:[[r.source==='swap'?'People':'Person',requestPeople(ctx,r)] as [string,Node]]),['Dates',requestWhenNode(r,zone)],...(nonBlocking(r)?[['Duties',NOT_BLOCKING] as [string,string]]:[]),...(detail(r,zone,ctx)?[['Details',detail(r,zone,ctx)] as [string,string]]:[]),
      ...(r.private_reason&&r.source!=='availability'?[['Reason (visible to approvers)',r.private_reason] as [string,string]]:[]),...(r.decision_reason?[['Decision reason',r.decision_reason] as [string,string]]:[])
    ];  // a swap's message is in its Details
    // "An approver decides" says nothing to the person who decides: their decision buttons are below.
    const why=decidable(r)&&r.source!=='scheduling_fact'?'':stageDetail(r,ctx);
    // schedule_impact (published and draft duties) when the server sends it; otherwise the publication-only impact.
    const s:Row|undefined=r.schedule_impact,duties:Row[]=s?s.duties:r.impact||[],impact:number|undefined=s?s.count:r.impact_count;
    const cover=decidable(r)?publishedCover(r):'';
    d.body.replaceChildren(
      el('div',{class:'request-status'},statusBadge(r.stage,requestStageWord(r,ctx)),r.self_approved?selfApprovedBadge():null,why?el('span',{class:'muted'},why):null),
      el('dl',{class:'request-facts'},...facts.flatMap(([k,v])=>[el('dt',{},k),el('dd',/^(Details|Reason|Decision)/.test(k)?{'data-private':''}:{},v)])),
      ...(impact!==undefined?[el('section',{class:'request-section'},el('h3',{},'Schedule'),el('p',{},own&&ownDuty(r)?ownDuty(r):s?scheduleWords(s):impactWords(r,own)),
        duties.length?el('ul',{class:'request-impact-list'},...duties.map(i=>el('li',{},el('span',{},[shortDay(i.day),i.start?`${formatTime(i.start,zone)}–${formatTime(i.end,zone)}`:'',i.type,i.rota_name].filter(Boolean).join(' · '),
            i.publication_id||i.status?statusBadge(i.publication_id?'published':i.status):null),
          button('Show in Schedule',()=>{d.close();hooks.showDay(i.day,r.person_id);},'ghost small',{'aria-label':`Show ${shortDay(i.day)} in Schedule`})))):null,
        impact>duties.length?el('p',{class:'muted'},`and ${impact-duties.length} more`):null,
        cover?el('p',{},cover):null)]:[]),
      el('section',{class:'request-section'},el('h3',{},'History'),el('ol',{class:'request-history'},...data.history.filter(e=>!(e.action==='created'&&data.history.some(x=>x!==e&&eventWord(r,x.action)===eventWord(r,'created')))).map(e=>el('li',{},
        el('span',{class:'request-history-what'},el('strong',{},eventWord(r,e.action,data.history.some(x=>x.action.endsWith('.undo')))),e.actor?` · ${e.actor}`:'',e.self_approved?selfApprovedBadge():''),
        // An undo's reason is the generic "Undo": "Decision undone" says it all.
        el('small',{class:'muted'},`${formatDate(e.at,zone)} ${formatTime(e.at,zone)}`),e.reason&&!(e.action.endsWith('.undo')&&/^Undo\b/.test(e.reason))?el('p',{class:'request-history-why'},e.reason):null)))),
      ...(buttons.length||menu.length?[el('div',{class:'request-actions request-foot'},...buttons,...menu.filter(a=>a.label!=='Show in Schedule').map(a=>{
        const b=button(a.label,()=>a.run(),a.danger?'danger small':'secondary small');return a.armed?armed(b):b;}))]:[]));
  };
  void fill();
}
