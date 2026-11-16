/** Staff wishes. Staff and coordinators add and change them on the Requests page (wishFields, in its New request dialog);
 * wishesPanel (a month calendar with a points meter and the list with outcomes) is where programme leads and auditors,
 * who have no Requests page, read everyone's (Insights › Fairness).
 * The server enforces the budget, sums each budget period and locks the days of a submitted rota (apps/api/services/wishes.py). */
import {withIcon,el,field,select,table,button,badge,note,notify,formDialog,iconButton,labelOf,formatDate,toLocal,weekdayNames,type Tone,CAPABILITY} from './dom.js';
import {readFailed} from './dom.js';  // a failed read (DL-045)
import {overflowMore} from './dom.js';  // the one "+N" (DESIGN D8); the day's name says every wish
import {fullNameOf} from './person_label.js';
import {MAX_WISH_POINTS,WISH_KINDS,PATTERN_WISH_KINDS,WISH_RANGE_MAX_DAYS,type WishTypeSettings} from './generated.js';
import {dateRange} from './date_picker.js';
import {get,command,newest,ApiError} from './api.js';
import {dataTable} from './table_view.js';
import {peopleChoices,type Context} from './forms.js';
import type {Row} from './types.js';
import {isWeekend} from './planner_logic.js';

export type WishList={wishes:Row[];budget_points:number;period:'week'|'month'|'quarter';week_start:number;max_points:number;
  usage:{start:string;end:string;used:number}[];multipliers:Record<string,number>;duty_types:{id:string;name:string}[];
  /** Per kind: whether staff may ask for it and its default points (Configuration › Department settings). */
  type_settings:Record<string,WishTypeSettings>;
  /** The settled rotas over the days read (ends_on exclusive), by name: their days take no wish. */
  settled?:{name:string;starts_on:string;ends_on:string}[]};
type WishKind=typeof WISH_KINDS[number];
/** Words per kind: its name, a short chip and the planner's words ({limit} filled in, as score.py's WISH_WORDS).
 *  Keyed by the contract's WISH_KINDS, so a new kind fails the build here instead of showing "undefined". */
export const WISH_KIND_WORDS:Record<WishKind,[string,string,string]>={prefer_off:['Day off','Off','day off'],prefer_on:['Day on duty','On','on duty'],
  prefer_shift_type:['A particular duty','Duty','wants'],avoid_shift_type:['Not a particular duty','Not','not'],
  max_days_in_row:['Most days in a row','Run','at most {limit} days in a row'],min_days_off_together:['Fewest days off together','Off run','at least {limit} days off together'],
  max_weekends_in_row:['Most weekends in a row','Weekends','at most {limit} weekends in a row'],whole_weekends:['Whole weekends','Weekends','whole weekends']};
const isPattern=(kind:string)=>(PATTERN_WISH_KINDS as readonly string[]).includes(kind);
/** The kinds the one-off editor adds (dated; pattern kinds have no form yet). */
const DATED_KINDS=WISH_KINDS.filter(k=>!isPattern(k));
export const wishKindLabel=(kind:string,short=false)=>WISH_KIND_WORDS[kind as WishKind]?.[short?1:0]||kind;
/** A wish in a few words for the planner: "day off (2 pts)", "wants Night (1 pt)", "at most 3 days in a row (1 pt)". */
export const wishWords=(w:Row,types:Row[])=>`${(WISH_KIND_WORDS[w.kind as WishKind]?.[2]||w.kind).replace('{limit}',String(w.limit??''))}${w.assignment_type_id?' '+(labelOf(types.find(t=>t.id===w.assignment_type_id))||'a duty type'):''} (${w.points} pt${w.points>1?'s':''})`;
const OUTCOME:Record<string,[string,Tone]>={granted:['Granted','ok'],partly:['Partly granted','warn'],declined:['Not granted','err']};
const iso=(d:Date)=>d.toISOString().slice(0,10);
const shift=(day:string,n:number)=>iso(new Date(Date.parse(day+'T12:00:00Z')+n*86400000));
// One page visit keeps its month and person (not saved).
const state={month:'',person:null as string|null};

/** `changed`: called instead of redrawing after a save (the Requests page redraws itself and this calendar). */
export async function wishesPanel(ctx:Context,changed?:()=>Promise<void>,personId?:string){
  const me=ctx.me,today=toLocal(new Date().toISOString(),me.organisation.timezone).slice(0,10);  // the department's date, not UTC
  const forAnyone=ctx.permits(CAPABILITY.COORDINATOR),readsAll=forAnyone||ctx.permits(CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR);
  const ownWishes=ctx.permits(CAPABILITY.STAFF)&&!!me.person_id;
  if(personId&&!readsAll&&!(ownWishes&&personId===me.person_id))return note('You can view only your own wishes.');
  // A profile's fixed person/month must not change the separate department wishes view.
  const view=personId?{month:today.slice(0,7)+'-01',person:personId}:state;
  if(!view.month)view.month=today.slice(0,7)+'-01';
  if(view.person===null)view.person=(forAnyone||ownWishes)&&me.person_id?me.person_id:'';
  // In a person's hub the tab already names it: no panel frame or heading.
  const section=el('section',{class:personId?'wishes':'panel wishes','aria-label':'Wishes'});
  // A person change then Next month at once: only the newest answer draws, never a late one for the old month.
  const read=newest((path:string)=>get<WishList>(path));
  const draw=async()=>{
    const month=view.month,next=shift(month,40).slice(0,7)+'-01';
    const person=readsAll?view.person!:me.person_id||'';
    const data=await read(`/api/wishes?start=${shift(month,-7)}&end=${shift(next,7)}${readsAll&&person?'&person_id='+person:''}`);
    if(!data)return;
    const mine=data.wishes.filter(w=>!person||w.person_id===person);
    const canAdd=forAnyone||ownWishes&&person===me.person_id;
    const editable=(w:Row)=>!w.outcome&&!w.locked&&(forAnyone||ownWishes&&w.person_id===me.person_id);
    const changeable=(w:Row)=>editable(w)&&oneOffWish(w);
    // The meter shows the budget period of today (in the shown month) or of the month's first day, summed by the server.
    const focus=today>=month&&today<next?today:month,period=data.usage.find(p=>p.start<=focus&&focus<p.end),used=period?.used||0;
    const periodName=data.period==='month'?new Intl.DateTimeFormat('en-GB',{month:'long',timeZone:'UTC'}).format(new Date(month+'T12:00:00Z'))
      :period?`${data.period} from ${formatDate(period.start)}`:`this ${data.period}`;
    const who=(id:string)=>fullNameOf((ctx.catalogues.people||[]).find(p=>p.id===id))||'Someone';
    const typeName=(id:string)=>data.duty_types.find(t=>t.id===id)?.name||'duty';
    const words=(w:Row)=>w.assignment_type_id?`${wishKindLabel(w.kind,true)} ${typeName(w.assignment_type_id)}`:wishKindLabel(w.kind)+(w.limit!=null?` (${w.limit})`:'');
    const when=(w:Row)=>wishWhenParts(w).join(' ');
    const after=changed||draw;
    const open=(day:string,existing?:Row)=>editor(ctx,data,existing?.person_id||person,forAnyone&&!person&&!existing,day,existing,after);
    if(!data.budget_points){section.replaceChildren(el('h2',{class:'panel-title'},'Wishes'),note('Wishes are switched off for this department.'));return;}
    const meter=el('meter',{min:0,max:data.budget_points,value:used,low:Math.ceil(data.budget_points*0.7),high:data.budget_points,optimum:0,'aria-label':`${used} of ${data.budget_points} wish points used for ${periodName}`});
    const nav=(n:number,label:string)=>iconButton(label,n<0?'chevronLeft':'chevronRight',()=>{view.month=shift(month,n<0?-1:40).slice(0,7)+'-01';redraw();});
    const whoPick=readsAll&&!personId?select('Whose wishes',[{value:'',label:'Everyone'},...peopleChoices(ctx)],person,false):null;
    whoPick?.input.addEventListener('change',()=>{view.person=whoPick.input.value;redraw();});
    const boost=person?data.multipliers[person]||1:1;
    const decided=mine.filter(w=>w.outcome),granted=decided.filter(w=>w.outcome==='granted').length;
    // Calendar: weeks from the department's first weekday; a day shows its wishes, or offers quick add.
    const start=shift(month,-(((new Date(month+'T12:00:00Z').getUTCDay()+6)%7-data.week_start+7)%7));
    // A group of day buttons (each names its full date and wishes), not an ARIA grid: weekday headings are visual only.
    const grid=el('div',{class:'wish-cal',role:'group','aria-label':'Wishes by day'});
    for(let i=0;i<7;i++)grid.append(el('div',{class:'wish-dow'+(isWeekend(shift(start,i))?' weekend':''),'aria-hidden':'true'},new Intl.DateTimeFormat('en-GB',{weekday:'short',timeZone:'UTC'}).format(new Date(shift(start,i)+'T12:00:00Z'))));
    for(let day=start;day<next||grid.children.length%7!==0;day=shift(day,1)){
      const here=mine.filter(w=>!w.repeat&&w.first_day<=day&&day<=w.last_day),inMonth=day.slice(0,7)===month.slice(0,7);
      const own=person?here.find(oneOffWish):undefined,action=own?(changeable(own)?'change':''):canAdd?'add':'';
      const label=`${formatDate(day)}${here.length?' · '+here.map(w=>(person?'':who(w.person_id)+': ')+words(w)+(w.outcome?` (${OUTCOME[w.outcome][0].toLowerCase()})`:w.locked?' (settled)':'')).join(', '):''}${action==='add'?' · add a wish':''}`;
      const cell=el('button',{type:'button',class:'wish-day'+(inMonth?'':' other-month')+(isWeekend(day)?' weekend':'')+(day===today?' today':'')+(here.length?' has-wish':''),'aria-label':label,'data-tip':label,disabled:!action},
        el('span',{class:'wish-date'},String(Number(day.slice(8)))),
        ...here.slice(0,2).map(w=>el('span',{class:'wish-chip '+w.kind+(w.outcome?' '+w.outcome:'')},person?`${wishKindLabel(w.kind,true)} ${w.points}`:who(w.person_id).split(' ').pop()!)),
        here.length>2?overflowMore(here.length-2,['wish','wishes'],{as:'hidden',cls:'wish-more'}):'');
      cell.addEventListener('click',()=>open(day,action==='change'?own:undefined));
      grid.append(cell);
    }
    const withdraw=async(r:Row)=>{await command('wish.withdraw',{reason:'Withdrawn'},r.id,r.revision);notify('Wish withdrawn. Its points are free again.');await after();};
    const list=dataTable<Row>({id:personId?'person-wishes':'wishes',noun:['wish','wishes'],rows:mine,rowId:r=>r.id,searchLabel:'Search wishes',searchPlaceholder:'Person, day or note',controls:whoPick?[whoPick.wrap]:undefined,
      // The list is this month's: wishes in other months are not "none yet".
      empty:[`No wishes in ${new Intl.DateTimeFormat('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(month+'T12:00:00Z'))}`,canAdd?'Pick a day in the calendar to ask for it off, on duty, or for a particular duty.':'Wishes staff add appear here.'],
      columns:[
        {key:'person',label:'Person',search:'related',hidden:!!person,text:r=>who(r.person_id),facet:r=>who(r.person_id),cell:r=>el('strong',{},who(r.person_id))},
        {key:'when',label:'When',text:when,sort:r=>String(r.first_day),cell:when},
        {key:'wish',label:'Wish',text:words,facet:r=>wishKindLabel(r.kind),cell:words},
        {key:'points',label:'Points',sort:r=>r.points,cell:r=>String(r.points)},
        {key:'note',label:'Note',text:r=>String(r.note||''),cell:r=>r.note||el('span',{class:'muted'},'—')},
        {key:'outcome',label:'Outcome',facet:r=>r.outcome?OUTCOME[r.outcome][0]:r.locked?'Settled':'Open',
          cell:r=>r.outcome?badge(...OUTCOME[r.outcome]):r.locked?el('span',{class:'muted','data-tip':'The rota for these days has been submitted; the outcome comes with publication.'},'Settled · rota submitted'):el('span',{class:'muted'},'Open')},
      ],
      rowButtons:r=>changeable(r)?[button('Change',()=>open(r.first_day,r),'ghost small')]:[],
      rowActions:r=>editable(r)?[{label:'Withdraw wish',icon:'close',run:()=>withdraw(r)}]:[]});
    section.replaceChildren(
      personId?'':el('div',{class:'section-head'},el('div',{},el('h2',{},'Wishes'),el('p',{class:'muted'},canAdd?'Days off, on duty or a particular duty. Not guaranteed; change them until the rota for those days is submitted.':'Days off, on duty or particular duties staff asked for.')),
        // Secondary: the page's one primary action is in the top bar (Submit a request / Record leave).
        el('div',{class:'actions'},canAdd?withIcon(button('Add a wish',()=>open(focus),'secondary'),'plus'):'')),
      el('div',{class:'wish-bar'},nav(-1,'Previous month'),el('strong',{class:'wish-month'},new Intl.DateTimeFormat('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(month+'T12:00:00Z'))),nav(1,'Next month'),
        person?el('span',{class:'wish-meter'},meter,el('span',{},`${used} of ${data.budget_points} points used · ${periodName}`)):'',
        person&&decided.length?el('span',{class:'muted'},`${granted} of ${decided.length} decided wish${decided.length===1?'':'es'} granted`):'',
        person&&boost>1?badge(`Next wishes count ×${boost}`,'warn','',{tip:'Wishes that could not be granted count for more next time.'}):''),
      grid,list.root);
  };
  // A month or person change that fails says so instead of leaving the old month on screen.
  const redraw=()=>void draw().catch(e=>{section.querySelector(':scope>.read-failed')?.remove();section.prepend(readFailed('the wishes',e,redraw));});
  await draw();
  return section;
}

/** Whether the one-off editor (wishFields) may change this wish. It supports dated kinds only, so it must not strip recurrence or pattern fields.
 *  shortcut: standing preferences can only be withdrawn here until their own editor ships; it replaces this check. */
export const oneOffWish=(w:Row)=>!w.repeat&&!isPattern(w.kind);
/** Standing rows are definitions; their anchor is not a dated occurrence. */
export const wishWhenParts=(w:Row):string[]=>w.repeat?
  [`Every ${weekdayNames(w.repeat.weekdays)}`,'· from '+formatDate(w.repeat.starts_on),
    ...(w.repeat.ends_on?['through '+formatDate(w.repeat.ends_on)]:[]),...(w.repeat.number?['· up to '+w.repeat.number+' per budget period']:[])]:
  [formatDate(w.first_day),...(w.last_day!==w.first_day?['– '+formatDate(w.last_day)]:[])];
/** A budget period by name ("October", "the week of 5 Oct 2026", "the quarter from Oct 2026"), never just "this month":
 *  a wish for December spends December's points. */
export const periodName=(period:string,start?:string)=>!start?`this ${period}`:period==='month'?new Intl.DateTimeFormat('en-GB',{month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(start+'T12:00:00Z'))
  :period==='week'?`the week of ${formatDate(start)}`:`the ${period} from ${new Intl.DateTimeFormat('en-GB',{month:'short',year:'numeric',timeZone:'UTC'}).format(new Date(start+'T12:00:00Z'))}`;
function editor(ctx:Context,data:WishList,person:string,choosePerson:boolean,day:string,existing:Row|undefined,done:()=>Promise<void>){
  formDialog(existing?'Change wish':'Add a wish',`Wish points: ${data.budget_points} per ${data.period}. One point for a preference; more for something important. For leave you need, submit a leave request instead.`,
    form=>wishFields(ctx,form,data,person,choosePerson,day,existing,done),existing?'Save wish':'Add wish');
}
/** Configuration › Auto-schedule & scoring › Staff preferences: per kind, whether staff may ask for it, the points a new
 *  wish starts at and (dated kinds) whether it may repeat weekly. Approval metadata (not used yet, P5) is kept as stored. */
export function wishTypeSettingsFields(stored:Record<string,WishTypeSettings>|undefined){
  const rows=WISH_KINDS.map(kind=>{const s=stored?.[kind]||{},label=WISH_KIND_WORDS[kind][0];
    const allowed=el('input',{type:'checkbox',class:'switch','aria-label':label,checked:s.allowed!==false}) as HTMLInputElement;
    const points=el('input',{type:'number','aria-label':`Default points for ${label.toLowerCase()}`,value:String(s.default_points||1),required:true,min:1,max:MAX_WISH_POINTS,step:1}) as HTMLInputElement;
    // Pattern kinds take a date range, never weekday repeats (StaffWishInput).
    const repeat=isPattern(kind)?null:el('input',{type:'checkbox',class:'switch','aria-label':`${label} may repeat weekly`,checked:s.may_repeat!==false}) as HTMLInputElement;
    const sync=()=>{points.disabled=!allowed.checked;if(repeat)repeat.disabled=!allowed.checked;};allowed.addEventListener('change',sync);sync();
    return {kind,s,label,allowed,points,repeat};});
  const panel=el('fieldset',{class:'score-grid wish-kinds'},el('legend',{},'Kinds of wish'),
    table(['Kind','Staff may ask','Default points','Weekly repeat'],rows.map(r=>[r.label,r.allowed,r.points,r.repeat||el('span',{class:'muted'},'—')])));
  return {panel,value:()=>({types:{...stored,...Object.fromEntries(rows.map(r=>[r.kind,{...r.s,allowed:r.allowed.checked,default_points:Number(r.points.value),...(r.repeat?{may_repeat:r.repeat.checked}:{})}]))}})};
}
/** Budgets up to this many points offer a dropdown of named choices; larger ones a number field. */
const POINT_CHOICES=20;
/** The wish list, budget and duty types around one day, for one person (theirs only, for staff). */
export const wishData=(day:string,person:string)=>get<WishList>(`/api/wishes?start=${shift(day,-40)}&end=${shift(day,40)}${person?'&person_id='+person:''}`);
/** The fields of one wish built into `form`, with a points meter for the budget period of its first day; returns the save.
 *  Shared by the editor here and the Requests page's New request dialog. */
export function wishFields(ctx:Context,form:HTMLElement,data:WishList,person:string,choosePerson:boolean,day:string,existing:Row|undefined,done:()=>Promise<void>){
  const most=Math.min(data.max_points,data.budget_points);
    const who=choosePerson?select('Person',peopleChoices(ctx),'',true,'Choose a person'):null;
    // Kinds the department allows (a saved wish keeps its own kind); a new wish starts at its kind's default points.
    const kinds=DATED_KINDS.filter(value=>data.type_settings?.[value]?.allowed!==false||value===existing?.kind);
    // Every kind switched off: nothing to ask for (the New request dialog then has no Save; the editor says why).
    if(!kinds.length){const why='No kind of wish is open to staff. An admin chooses them in Configuration › Auto-schedule & scoring.';form.append(note(why));return async()=>{throw new Error(why);};}
    const kind=select('I would like',kinds.map(value=>({value,label:WISH_KIND_WORDS[value][0]})),existing?.kind||kinds[0]);
    const points0=(k:string)=>String(Math.max(1,Math.min(data.type_settings?.[k]?.default_points||1,most)));
    const type=select('Duty type',data.duty_types.map(t=>({value:t.id,label:t.name})),existing?.assignment_type_id||'');
    const first=field('From','date',existing?.first_day||day,{required:true}),last=field('To (included)','date',existing?.last_day||day,{required:true});
    // A short range names each choice; a large budget is a number field, never one option per point.
    const start=existing?String(Math.min(existing.points,most)):points0(kind.input.value);
    const points=most<=POINT_CHOICES?select('Points',Array.from({length:most},(_,i)=>i+1).map(n=>({value:String(n),label:n===1?'1 · a preference':n===most&&n>1?`${n} · most important`:String(n)})),start)
      :field('Points','number',start,{required:true,min:1,max:most,step:1});
    const text=field('Note (optional, seen by planners)','text',existing?.note||'',{maxlength:200});
    const sync=()=>{const typed=kind.input.value.endsWith('shift_type');type.wrap.hidden=!typed;type.input.required=typed;type.input.disabled=!typed;};
    kind.input.addEventListener('change',()=>{sync();if(!existing){points.input.value=points0(kind.input.value);showBudget();}});sync();
    // Points left in the budget period of the first day (the server sums and enforces the same periods).
    const meter=el('meter',{min:0,max:data.budget_points,low:Math.ceil(data.budget_points*0.7),high:data.budget_points,optimum:0}) as HTMLMeterElement;
    const left=el('span',{}),budget=el('div',{class:'span-2 wish-meter wish-budget'},meter,left);
    const showBudget=()=>{const p=person&&data.usage.find(u=>u.start<=first.input.value&&first.input.value<u.end);budget.hidden=!p;if(!p)return;
      const used=p.used-(existing&&p.start<=existing.first_day&&existing.first_day<p.end?existing.points:0),wanted=Number(points.input.value);
      meter.value=used+wanted;meter.setAttribute('aria-label',`${used+wanted} of ${data.budget_points} wish points with this wish`);
      left.textContent=`${used} of ${data.budget_points} points used in ${periodName(data.period,p.start)}${used+wanted>data.budget_points?' · not enough points left':` · ${data.budget_points-used-wanted} left after this wish`}`;};
    // SW3-C-05: the days chosen are read (the same /api/wishes): their budget period's points and any settled rota there,
    // named, are said as soon as they are picked, by the rule the server refuses with (services/wishes.py `locked`).
    let settled=data.settled||[];
    const closedBy=()=>{const a=first.input.value,b=last.input.value;return a&&b?settled.find(r=>r.starts_on<=b&&r.ends_on>a):undefined;};
    const closedWords=(name:string)=>`The rota for these days (${name}) is already settled, so wishes for them are closed. Ask for leave or swap a duty instead.`;
    const closed=el('div',{class:'span-2',id:'wish-closed','aria-live':'polite'});
    // data-held: the New request sheet's own relabel keeps Send off too.
    const paintClosed=(again=true)=>{const r=closedBy();closed.replaceChildren(...(r?[note(closedWords(r.name),'warning')]:[]));
      const b=form.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');if(!b){if(again)setTimeout(()=>paintClosed(false));return;}  // fields built before the sheet attaches them
      if(r){b.toggleAttribute('data-held',true);b.disabled=true;b.setAttribute('aria-describedby','wish-closed');}
      else if(b.hasAttribute('data-held')){b.removeAttribute('data-held');b.removeAttribute('aria-describedby');b.disabled=b.hasAttribute('aria-busy');}};
    const around=newest((a:string,b:string)=>get<WishList>(`/api/wishes?start=${a}&end=${b}${person?'&person_id='+person:''}`));
    const reread=async()=>{const a=first.input.value,b=last.input.value;paintClosed();if(!a||!b||b<a)return;
      const fresh=await around(a,shift(b,1)<shift(a,WISH_RANGE_MAX_DAYS)?shift(b,1):shift(a,WISH_RANGE_MAX_DAYS)).catch(()=>undefined);
      if(!fresh||first.input.value!==a||last.input.value!==b)return;
      data.usage=fresh.usage;settled=fresh.settled||[];showBudget();paintClosed();};
    first.input.addEventListener('change',()=>{if(last.input.value<first.input.value)last.input.value=first.input.value;showBudget();void reread();});
    last.input.addEventListener('change',()=>void reread());
    points.input.addEventListener('change',showBudget);showBudget();paintClosed();
    text.wrap.classList.add('span-2');form.append(...(who?[who.wrap]:[]),kind.wrap,type.wrap,dateRange('Dates',first,last),closed,points.wrap,budget,text.wrap);
    return async()=>{
      const typed=kind.input.value.endsWith('shift_type');
      const payload={person_id:who?who.input.value:existing?.person_id||person,first_day:first.input.value,last_day:last.input.value,kind:kind.input.value,assignment_type_id:typed?type.input.value:null,points:Number(points.input.value),note:text.input.value};
      const p=data.usage.find(u=>u.start<=first.input.value&&first.input.value<u.end);
      // WISH_LOCKED: a rota for these days is already settled (submitted, approved or published); its days are said
      // the same way whichever it is, with the requests that still apply.
      const shut=closedBy();if(shut)throw new Error(closedWords(shut.name));
      // The server's refusal keeps its context (the rota's name).
      await command(existing?'wish.update':'wish.create',payload,existing?.id,existing?.revision).catch(e=>{if(e instanceof ApiError&&e.code==='WISH_LOCKED')
        throw new Error(`${e.message}. Ask for leave or swap a duty instead.`);throw e;});
      notify(p?`Wish saved for ${periodName(data.period,p.start)}.`:'Wish saved.');await done();};
}
