import {el,badge,icon,button,toLocal,type IconName,emptyLine} from './dom.js';import {openFiltered} from './table_view.js';
import {closeDock} from './side_panel.js';
import {TERMS} from './terms.js';
import {purposeOf} from './config_kinds.js';
import type {Context} from './forms.js';
import {seriesGroups} from './recurring_sessions.js';
import {moreWords} from './dom.js';  // the one "+N" and its words (UX › controls S9)
import {background} from './api.js';

/** `go` is missing when this person cannot do the step (only an admin changes configuration). `kind`: the catalogue it fills. */
export interface SetupStep {id:string;title:string;why:string;icon:IconName;count:number;unit:string;optional?:boolean;go?:()=>void;goLabel:string;kind?:string;
  /** What the step still lacks although some records exist ("2 services without opening hours"): not done while set. */
  missing?:string}
/** Done: its records exist and nothing they should cover is left without them. */
export const stepDone=(s:SetupStep)=>s.count>0&&!s.missing;
/** Where each setup step is done, and whether this person may do it (app.ts setupOpeners). */
export interface SetupOpeners {catalogue:(kind:string)=>void;page:(view:string)=>void;can?:(kind:string)=>boolean}

/** Ordered department setup, in the order each step needs the one before (C-04): places and services, their hours,
 *  duty types, ranks, people, rules, the first rota; optional steps last. Status comes only from records that already exist. */
export function setupSteps(ctx:Context,open:SetupOpeners,workspaces:number|null,counts:Record<string,number>={}):SetupStep[]{
  const n=(kind:string)=>counts[kind]??(ctx.catalogues[kind]||[]).filter(r=>r.active!==false).length;
  const cat=(kind:string)=>({kind,why:purposeOf(kind),go:open.can&&!open.can(kind)?undefined:()=>open.catalogue(kind)});
  // Opening hours are done when every service in use has them, not when one calendar exists (the Data health count).
  const noHours=(ctx.catalogues.services||[]).filter(r=>r.active!==false&&!r.calendar_id).length;
  // With calendars in place, what is left is giving those services one: Services, filtered to them.
  const hoursGo=noHours&&n('operating_calendars')&&!(open.can&&!open.can('services'))?()=>{openFiltered('catalogue-services',{c1:['Not set']});open.catalogue('services');}:null;
  return [
    {id:'locations',title:'Locations',icon:'grid',count:n('locations'),unit:'locations',...cat('locations'),goLabel:'Add locations'},
    {id:'services',title:'Services',icon:'layers',count:n('services'),unit:'services',...cat('services'),goLabel:'Add services'},
    {id:'hours',title:'Opening hours',icon:'clock',count:n('operating_calendars'),unit:'calendars',...cat('operating_calendars'),...(hoursGo?{go:hoursGo}:{}),goLabel:'Set opening hours',
      ...(noHours?{missing:`${noHours} ${noHours===1?'service':'services'} without opening hours`}:{})},
    {id:'types',title:'Duty types',icon:'calendar',count:n('assignment_types'),unit:'duty types',...cat('assignment_types'),goLabel:'Add a duty type'},
    {id:'roles',title:'Ranks & skills',icon:'shield',count:n('categories'),unit:'ranks',...cat('categories'),goLabel:'Add ranks'},
    {id:'people',title:'People & eligibility',why:'Who can work which duty. Add each person, then give several a rank at once.',icon:'people',count:n('people'),unit:'people',go:()=>open.page('people'),goLabel:'Manage people'},
    {id:'rules',kind:'policies',title:'Rules',why:'Rest periods, maximum hours and fair share that every rota must respect.',icon:'rules',count:n('policies'),unit:'rules',go:()=>open.page('rules'),goLabel:'Add rules'},
    {id:'rota',title:'Create your first rota',why:'Plan a period, then let auto-schedule fill it using your rules.',icon:'sparkle',count:workspaces??0,unit:'rotas',optional:workspaces===null,go:()=>open.page('planner'),goLabel:'Open the schedule'},
    // SW3-D-10: counted as Configuration's nav and list count them (series grouped), never a second number.
    {id:'series',title:'Recurring sessions',icon:'history',count:seriesGroups(ctx,toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10)).length,unit:'recurring sessions',optional:true,...cat('recurring_series'),goLabel:'Add recurring sessions'},
    {id:'content',title:TERMS.announcements,icon:'inbox',count:n('department_content'),unit:'published',optional:true,...cat('department_content'),goLabel:TERMS.addAnnouncement},
  ];
}

const none=(ctx:Context,kind:string)=>!(ctx.catalogues[kind]||[]).some(r=>r.active!==false);
const noOpeners:SetupOpeners={catalogue:()=>{},page:()=>{}};

let counting:{key:string;read:Promise<Record<string,number>|null>}|null=null;
/** The setup counts without the deferred lists (S7): a required step's catalogue held since boot counts itself (services,
 *  so Opening hours still sees every service without a calendar); the rest come from one counts_only read, shared while
 *  it is in flight by the Configuration badge and Home. null when that read failed. */
export function setupCounts(ctx:Context):Promise<Record<string,number>|null>{
  const held=(ctx.catalogues._versions||{}) as object;
  const kinds=setupSteps(ctx,noOpeners,null).flatMap(s=>!s.optional&&s.kind&&!(s.kind in held)?[s.kind]:[]);
  if(!kinds.length)return Promise.resolve({});
  const key='/api/catalogues?counts_only=true&'+kinds.map(k=>'include='+encodeURIComponent(k)).join('&');
  if(counting?.key===key)return counting.read;
  const read:Promise<Record<string,number>|null>=background<{counts:Record<string,number>}>(key).then(r=>r.counts,()=>null)
    .finally(()=>{if(counting?.read===read)counting=null;});
  counting={key,read};
  return read;
}

/** What a rota still needs before it can be planned (H-20), in setupSteps' words: "No duty types yet", or '' when
 *  duty types and locations exist. */
export function missingSetupWords(ctx:Context):string{
  const kind=['assignment_types','locations'].find(k=>none(ctx,k)),step=kind&&setupSteps(ctx,noOpeners,null).find(s=>s.kind===kind);
  return step?`No ${step.unit} yet`:'';
}

/** A form choice with nothing to choose from yet (no duty types, locations…): said under the field, with the setup step
 *  that adds them for someone who may, or who does it. Words and destination come from setupSteps (one place). */
export function missingSetup(ctx:Context,kind:string):HTMLElement|null{
  const open=ctx.setup;if(!open||!none(ctx,kind))return null;
  const step=setupSteps(ctx,open,null).find(s=>s.kind===kind);if(!step)return null;
  const node=el('p',{class:'hint missing-setup',role:'status'},`No ${step.unit} yet. `);
  // The form cannot be saved without this choice, so going to set it up closes the form instead of leaving it behind.
  node.append(step.go?button(step.goLabel,()=>{const d=node.closest('dialog');d?.close();d?.remove();if(node.closest('.dock-panel'))closeDock();step.go!();},'link-button')
    :'An admin sets these up in Configuration.');
  return node;
}

export interface HealthItem {title:string;detail:string;count:number;fix?:()=>void;fixLabel:string}
/** What is missing or inconsistent in the setup, each with where to fix it. From records already loaded. */
/** SW3-D-11: ranks sharing a short code, as the server finds them (GET /api/catalogues/categories/duplicate-codes). */
export type RankCodeClash={short_code:string;ranks:{id:string;name:string}[]};
export function dataHealth(ctx:Context,open:SetupOpeners,clashes:RankCodeClash[]=[]):HealthItem[]{
  const all=(kind:string)=>(ctx.catalogues[kind]||[]) as {[k:string]:any}[];
  const day=toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10),current=(r:{[k:string]:any})=>r.starts_on<=day&&(!r.ends_on||r.ends_on>day)&&!r.revoked;
  const people=all('people').filter(p=>p.active!==false);
  const eligible=new Set(all('eligibilities').filter(current).map(r=>r.person_id));
  const names=(rows:{[k:string]:any}[])=>rows.slice(0,3).map(r=>r.display_name||r.name).join(', ')+(rows.length>3?' '+moreWords(rows.length-3):'');
  // Current rank: the server's fact (current_category_ids), the one Home's "without a rank" counts.
  const noRank=people.filter(p=>!(p.current_category_ids||[]).length),noClinic=people.filter(p=>!eligible.has(p.id));
  const waiting=all('policies').filter(r=>r.status==='proposed');
  // An item whose setup step is not done yet is that step's to say, not a data problem (no ranks: nobody has one).
  // Clinics without opening hours always leave the Opening hours step undone (stepDone), so the step says it, once.
  const has=(kind:string)=>all(kind).some(r=>r.active!==false);
  return [
    {title:'People without a current rank',detail:names(noRank)+' · today · duties open only to some ranks cannot use them.',count:has('categories')?noRank.length:0,fix:()=>{openFiltered('people',{rank:['No current rank'],status:['Active']});open.page('people');},fixLabel:'Show them'},
    {title:'People not eligible for any clinic',detail:names(noClinic)+' · today · add clinic eligibility where they should be assigned.',count:has('services')?noClinic.length:0,fix:()=>{openFiltered('people',{services:['No current eligibility'],status:['Active']});open.page('people');},fixLabel:'Show them'},
    {title:'Ranks sharing a short code',detail:clashes.map(c=>`${c.short_code}: ${c.ranks.map(r=>r.name).join(', ')}`).join(' · ')+' · give each rank its own code.',
      count:clashes.length,fix:open.can&&!open.can('categories')?undefined:()=>open.catalogue('categories'),fixLabel:'Open ranks'},
    {title:'Rules waiting for approval',detail:names(waiting)+' · they do not apply until an approver activates them.',count:waiting.length,fix:()=>open.page('rules'),fixLabel:'Review rules'},
  ].filter(i=>i.count>0);
}

/** The steps as a checklist beside Data health; `importStaff` adds Import spreadsheet to the People step until it is done. */
export function setupGuidePage(target:HTMLElement,steps:SetupStep[],health:HealthItem[]=[],importStaff?:()=>void){
  const required=steps.filter(s=>!s.optional),done=required.filter(stepDone).length;
  const current=steps.find(s=>!s.optional&&!stepDone(s))||null;
  // SW3-D-10: the ring counts the required steps and says so (the list also holds optional ones).
  const ring=el('div',{class:'setup-ring',role:'img','aria-label':`${done} of ${required.length} required steps complete`},el('span',{},`${done}/${required.length}`,el('small',{},'required')));
  ring.style.setProperty('--progress',String(Math.round(done/required.length*100)));
  const list=el('ol',{class:'setup-steps'});
  steps.forEach((s,i)=>{
    const isDone=stepDone(s),isCurrent=s===current;
    list.append(el('li',{class:'setup-step'+(isDone?' done':'')+(isCurrent?' current':'')},
      // An optional step is not numbered: the numbers are the required order the ring counts.
      el('span',{class:'setup-mark','aria-hidden':'true'},isDone?icon('check'):s.optional?'':String(i+1)),
      el('div',{class:'setup-body'},
        el('div',{class:'setup-title'},el('strong',{},s.title),s.optional?badge('Optional'):null,s.count?el('span',{class:'muted'},`${s.count} ${s.count===1?(s.unit==='people'?'person':s.unit.replace(/ies$/,'y').replace(/s$/,'')):s.unit}`+(s.missing?` · ${s.missing}`:'')):null),
        el('p',{},s.why),
        !s.go?el('p',{class:'muted setup-admin-only'},'An admin sets this up.'):isCurrent||!isDone?el('div',{class:'actions'},button(s.goLabel,s.go,isCurrent?'primary':'secondary'),
          // Bulk onboarding sits on the step it speeds up; once people exist, People has the same import button.
          s.id==='people'&&importStaff?button('Import spreadsheet',importStaff,'ghost'):null):el('div',{class:'actions'},button('Review',s.go,'ghost',{'aria-label':'Review '+s.title})))));
  });
  const ready=done===required.length,fixes=health.length;
  target.append(el('div',{class:'setup-layout'},
    el('div',{},el('div',{class:'setup-hero'},ring,
      el('h2',{},ready?(fixes?`Ready to plan · ${fixes} ${fixes===1?'thing':'things'} to fix`:'Ready to plan'):'Set up your department')),list),
    el('aside',{class:'setup-aside'},el('section',{class:'panel setup-health'},el('h3',{},'Data health'),
        fixes?el('ul',{class:'health-list'},...health.map(i=>el('li',{},el('div',{},el('strong',{},`${i.count} · ${i.title}`),el('small',{class:'muted'},i.detail)),i.fix?button(i.fixLabel,i.fix,'secondary small'):el('small',{class:'muted'},'An admin fixes this')))):emptyLine('Nothing missing today: everyone has a rank and a clinic and no rule is waiting.')))));
}
