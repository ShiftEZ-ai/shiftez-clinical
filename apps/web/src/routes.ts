import {CAPABILITY} from './dom.js';
/** Addresses: one place that knows every page, its tabs and every old address (SSOT for redirects).
 * An address is `#page[/tab or record…][?query]`. Old pages that were merged redirect here, so bookmarks,
 * notification links, saved links and tests that use them keep working. */

/** What an old address needs to know to say where it goes now (the Schedule's addresses carry a day and a role's view). */
export interface AliasContext {today:string;planner:boolean;publication:string}
type Alias=string|((parts:string[],c:AliasContext)=>string);

/** Old Planner layout.range (or a saved view `v.<id>`) → the Schedule view. */
export function scheduleView(shown=''):string{
  if(shown.startsWith('v.'))return shown;
  const [mode,range]=(shown||'week.week').split('.');
  return mode==='year'?'year':mode==='person'?'people':mode==='coverage'?'coverage':mode==='list'?'list':range==='month'?'month':range==='day'?'day':'week';
}

/** Old address → current address: ONE map for every merged page. Read before routing, by navigate(), first load and the
 *  hashchange handler. A function entry also carries the old address's parts (e.g. a rota id and view) to the new page. */
export const ROUTE_ALIASES:Record<string,Alias>={
  overview:'home',today:'home',
  availability:'requests',swaps:'requests?type=swap',
  directory:'people',
  fairness:'insights/fairness',reports:'insights/workload',
  setup:'configuration/setup',
  governance:'access/routes',
  data:'system/imports',operations:'system/operations',audit:'system/audit',
  // Planner, Rota calendar, Team calendar and Published rota became the Schedule.
  // #planner/<rota>/<layout.range | v.saved> opens that draft in that view, on the draft's own days.
  planner:([rota,shown])=>rota?routeHash('schedule',[scheduleView(shown)],{edit:rota}).slice(1):'schedule/'+scheduleView(),
  timeline:(_,c)=>routeHash('schedule',['year',c.today]).slice(1),
  calendar:(_,c)=>routeHash('schedule',['people',c.today],{leave:'1'}).slice(1),
  // A publication link opens that version read-only; otherwise today's published duties (planners: the month).
  published:(_,c)=>routeHash('schedule',[c.planner?'month':'week',...(c.publication?[]:[c.today])],{source:c.publication}).slice(1),
};

/** Pages made of tabs: each tab keeps the roles of the page it replaced. A page is open to anyone who may see one tab. */
/** `phone`: the tab opens on a phone (phone scope, maintenance); a tabbed page fits a phone through its phone tabs only. */
export const PAGE_TABS:Record<string,{key:string;label:string;roles:string[];phone?:boolean}[]>={
  insights:[
    {key:'fairness',label:'Fairness',roles:[CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.ADMIN,CAPABILITY.AUDITOR,CAPABILITY.PROGRAMME_LEAD]},
    {key:'workload',label:'Workload',roles:[CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR]},
    {key:'exports',label:'Exports',roles:[CAPABILITY.STAFF,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.AUDITOR]},
  ],
  access:[
    {key:'people',label:'People & roles',roles:[CAPABILITY.ADMIN,CAPABILITY.ACCESS_SYSTEM_ROLES]},
    // Deciding a proposed route (Activate, Retire) is an approval: phones keep it; access administration stays desktop.
    {key:'routes',label:'Review routes',roles:[CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR],phone:true},
    {key:'history',label:'Role history',roles:[CAPABILITY.ADMIN]},
  ],
  system:[
    {key:'imports',label:'Imports',roles:[CAPABILITY.COORDINATOR,CAPABILITY.CREDENTIALER]},
    // The Bulk editor: any record type as a Sheet (the same collection, selection, preview and Undo as every list).
    {key:'bulk',label:'Bulk editor',roles:[CAPABILITY.CATALOGUE_READ]},
    {key:'operations',label:'Operations',roles:[CAPABILITY.SYSTEM_OPERATIONS]},
    {key:'audit',label:'Audit log',roles:[CAPABILITY.AUDIT_READ]},
    {key:'feedback',label:'Feedback',roles:[CAPABILITY.FEEDBACK_READ_ALL]},
  ],
};

export interface Route {page:string;parts:string[];query:URLSearchParams}

/** Follows aliases (at most a few hops) and splits the address. Unknown pages are left for the caller to refuse. */
export function parseRoute(hash:string,c:AliasContext={today:'',planner:false,publication:''}):Route{
  let address=hash.replace(/^#/,'');
  for(let hop=0;hop<4;hop++){
    const [path,search='']=splitQuery(address),[head,...rest]=path.split('/'),alias=ROUTE_ALIASES[decodeURIComponent(head)];
    if(!alias)break;
    // A string alias keeps the old address's remaining parts; a function alias decides what they mean on the new page.
    const target=typeof alias==='string'?alias:alias(rest.map(decodeURIComponent),c),keep=typeof alias==='string'?rest:[];
    // The old address's own query follows it to the new page.
    const [toPath,toSearch='']=splitQuery(target),merged=new URLSearchParams(toSearch);
    new URLSearchParams(search).forEach((v,k)=>merged.set(k,v));
    address=[toPath,...keep].join('/')+(merged.toString()?'?'+merged:'');
  }
  const [path,search='']=splitQuery(address),[page='',...parts]=path.split('/').map(decodeURIComponent);
  return {page,parts:parts.filter(Boolean),query:new URLSearchParams(search)};
}

/** Builds an address: `#page/part…?query` (empty parts and query are dropped). */
export function routeHash(page:string,parts:string[]=[],query?:URLSearchParams|Record<string,string>):string{
  const q=query instanceof URLSearchParams?query:new URLSearchParams(Object.entries(query||{}).filter(([,v])=>v));
  return '#'+[page,...parts.filter(Boolean)].map(encodeURIComponent).join('/')+(q.toString()?'?'+q:'');
}

/** A rule notice's words and address (maintenance), one function for the inbox item and where it leads: the rule's current
 *  name from the policies the client already holds (never a read per notice), else the name the notice was sent with
 *  (`policy_name`), and the Rules page's open-a-rule address (`?source=<policy id>`: the rule's page with its panel).
 *  Without a name it is "a rule"; without a policy id it opens Rules. A raw id is never shown; the words are text. */
export function ruleNotice(policyId:string|undefined,policy:(id:string)=>{name?:unknown}|undefined,sentName?:unknown):{text:string;hash:string}{
  const name=[policyId?policy(policyId)?.name:'',sentName].map(x=>String(x??'').trim()).find(Boolean)||'';
  return {text:`A proposal or decision on ${name||'a rule'}`,hash:policyId?routeHash('rules',[],{source:policyId}):routeHash('rules')};
}

function splitQuery(address:string):[string,string]{const i=address.indexOf('?');return i<0?[address,'']:[address.slice(0,i),address.slice(i+1)];}
