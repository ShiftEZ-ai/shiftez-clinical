/* Who's available over one rota's dates (the rules panel's second tab).
 * The server decides each day (GET /api/timeline, its whereabouts projection: outside › leave › academic › protected
 * › away rotation); work is hidden on purpose, the duties are on the schedule beside it. This file only draws the days. */
import {el,hoverCard,plural,formatDate,note,legend,emptyLine} from './dom.js';
import {get,ApiError} from './api.js';
import {collectionToolbar,builtinViewItems} from './table_view.js';
import {working} from './loading.js';
import {personLink} from './person_label.js';
import {units,shortDay,plusDays} from './timeline.js';
import type {TimelinePerson,TimelineResponse,TimelineSegment} from './generated.js';

export type AwayKind='out'|'leave'|'protected'|'away';
export type Stretch={kind:AwayKind;from:number;to:number;label:string};
/** `rank`: the rank (id and name) the person holds on the rota's first day here, from the server's rank items. */
/** `eligible`: whether the person can work at least one of the rota's sessions (the server's qualified() check); null
 *  when the server did not say (a rota with no sessions yet). */
export type Availability={id:string;name:string;group:string|null;rank:{id:string;name:string}|null;eligible:boolean|null;stretches:Stretch[];away:number;state:'all'|'part'|'none';words:string};
/** The rank a person is listed under: the one held on the rota's first day, else the first held during it. Grouped by
 *  its id, never its name (two ranks may be named alike, and a name is not a key). */
export function rankOf(items:TimelinePerson['items']|undefined,first:string):{id:string;name:string}|null{
  const ranks=(items||[]).filter(x=>x.kind==='rank'&&x.status!=='revoked'&&x.details?.category_id).sort((a,b)=>a.starts_on.localeCompare(b.starts_on));
  const r=[...ranks].reverse().find(x=>x.starts_on<=first&&(!x.ends_on||x.ends_on>first))||ranks[0];
  return r?{id:String(r.details?.category_id),name:r.label}:null;
}

const addDay=(d:string)=>{const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+1);return x.toISOString().slice(0,10);};
/** The rota's days, first to last included (`end` is the day after, as rotas store it). */
export function daysOf(start:string,end:string){const out:string[]=[];for(let d=start;d<end&&out.length<800;d=addDay(d))out.push(d);return out;}

/** What each away kind is called: the strip's key, and People › Where is everyone's cards and View settings key. */
export const AWAY_KINDS:[AwayKind,string][]=[['leave','Leave'],['protected','Academic or protected'],['away','Rotation elsewhere'],['out','Not in the department']];
/** A server segment as an away mark and its words (null: the person is here, working or on a home rotation). The one
 *  mapping for the strip and the People timeline's line, so both name a stretch the same way. */
export function awayOf(g:Pick<TimelineSegment,'kind'|'label'|'leave_type'>):{kind:AwayKind;words:string}|null{
  const k:AwayKind|null=g.kind==='outside'?'out':g.kind==='leave'?'leave':g.kind==='academic'||g.kind==='protected'?'protected':g.kind==='away'?'away':null;
  return k?{kind:k,words:k==='leave'?g.leave_type||g.label:k==='away'?`Rotation elsewhere · ${g.label}`:g.label}:null;
}
/** One person over the days: which stretches they are away and why, and the result in words. The server owns
 *  department membership and precedence, so its segments never overlap. */
export function summarise(p:Pick<TimelinePerson,'id'|'display_name'|'group'|'items'|'segments'|'rota_eligible'>,days:string[]):Availability{
  const n=days.length,kind:(AwayKind|'')[]=Array(n).fill(''),label:string[]=Array(n).fill('');
  const at=(d:string)=>{const i=days.indexOf(d);return i<0?(d<days[0]?0:n):i;};
  const mark=(from:string,to:string|null|undefined,k:AwayKind,words:string)=>{for(let i=at(from),e=to?at(to):n;i<e;i++)if(!kind[i]){kind[i]=k;label[i]=words;}};
  for(const g of p.segments||[]){const a=awayOf(g);if(a)mark(g.starts_on,g.ends_on,a.kind,a.words);}
  const stretches:Stretch[]=[];
  kind.forEach((k,i)=>{if(!k)return;const s=stretches[stretches.length-1];if(s&&s.to===i&&s.kind===k&&s.label===label[i])s.to=i+1;else stretches.push({kind:k,from:i,to:i+1,label:label[i]});});
  const away=kind.filter(Boolean).length,state=!away?'all':away===n?'none':'part';
  const outOnly=stretches.length===1&&stretches[0].kind==='out';
  // Outside the department for the whole rota: the server's reason ("Not started yet"), never a plain "Away".
  const words=state==='all'?'Every day':state==='none'?(outOnly?stretches[0].label:'Away')
    :outOnly&&stretches[0].from===0?`Starts ${formatDate(days[stretches[0].to])}`
    :outOnly&&stretches[0].to===n?`Last day ${formatDate(days[stretches[0].from-1])}`:`Away ${away} d`;
  return {id:p.id,name:p.display_name,group:p.group??null,rank:rankOf(p.items,days[0]),eligible:p.rota_eligible??null,stretches,away,state,words};
}
const spanWords=(s:Stretch,days:string[])=>{const a=formatDate(days[s.from]),b=formatDate(days[s.to-1]);return a===b?a:`${a} – ${b}`;};
/** Every stretch and why, in words: the strip's accessible name and its card. */
export const awayWords=(a:Availability,days:string[])=>a.state==='all'?'Available every day':a.stretches.map(s=>`${s.label} ${spanWords(s,days)}`).join('; ');

/** The built-in views. Who's available answers "who could I schedule in this rota?": by default the people who can work
 *  at least one of its sessions (the server's eligibility, never a client rule), and within them who is away; Everyone in
 *  the department is kept for completeness. A rota with no sessions yet has no eligibility: everyone shows. */
export type View='eligible'|'free'|'part'|'none'|'everyone';
const VIEW_WORDS:Record<View,string>={eligible:'Can work in this rota',free:'Available every day',part:'Partly away',none:'Away every day',everyone:'Everyone in the department'};
export const viewsOf=(eligibility:boolean):View[]=>eligibility?['eligible','free','part','none','everyone']:['everyone','free','part','none'];
/** The people a view shows: those who can work in the rota (when it says), then by how much they are away. */
export function viewPeople(all:Availability[],view:View,eligibility:boolean):Availability[]{
  const pool=eligibility&&view!=='everyone'?all.filter(a=>a.eligible===true):all;
  return view==='free'?pool.filter(a=>a.state==='all'):view==='part'||view==='none'?pool.filter(a=>a.state===view):pool;
}
const keep:{view:View;q:string}={view:'eligible',q:''};
type Read={people:TimelinePerson[];eligibility:boolean};
// One read per rota version and availability revision (the server pages by 200).
const reads=new Map<string,Promise<Read>>();
async function readAll(start:string,end:string,rota:string):Promise<Read>{
  const q=`/api/timeline?start=${start}&end=${end}&bucket=day&hide=work&layers=rank&group_by=rank&limit=200&eligible_workspace_id=${encodeURIComponent(rota)}`;
  let page=await get<TimelineResponse>(q);const people=[...page.people],first=page;
  while(page.has_more){page=await get<TimelineResponse>(`${q}&offset=${page.offset+page.people.length}`);people.push(...page.people);}
  return {people,eligibility:!!first.rota_eligibility&&first.rota_eligibility.sessions>0};
}

/** The tab: a toolbar (views with their counts, search), a strip per person over the rota's dates, and the key. */
/** `code`: a rank's short code, said beside its name only when two ranks in the list are named alike. */
export function availabilityPanel(w:{id:string;starts_on?:unknown;ends_on?:unknown},revision:unknown,code:(rankId:string)=>string=()=>''):HTMLElement{
  const start=String(w.starts_on).slice(0,10),end=String(w.ends_on).slice(0,10),days=daysOf(start,end);
  const root=el('div',{class:'ra'},working('Reading who is away…'));
  const key=[w.id,start,end,String(revision??'')].join('|');
  if(!reads.has(key))reads.set(key,readAll(start,end,w.id).catch(e=>{reads.delete(key);throw e;}));
  reads.get(key)!.then(r=>draw(root,r.people.map(p=>summarise(p,days)),days,code,r.eligibility)).catch(e=>{
    const refused=e instanceof ApiError&&(e.status===403||e.status===404);
    root.replaceChildren(refused?note('Your role can’t see other people’s availability.','info'):note(`Who’s available can’t be shown: ${(e as Error).message}`,'error',e));});
  return root;
}
function draw(root:HTMLElement,all:Availability[],days:string[],code:(rankId:string)=>string,eligibility:boolean){
  const views=viewsOf(eligibility),first=views[0];
  // A view this rota does not have (Can work in this rota, on a rota with no sessions yet) falls back to its first.
  if(!views.includes(keep.view))keep.view=first;
  const count=(v:View)=>viewPeople(all,v,eligibility).length;
  const list=el('div',{class:'ra-list'});
  // The shared key (dom.ts legend): only the marks the rows in this view draw, in the strip's order.
  const awayWords_=(k:AwayKind)=>AWAY_KINDS.find(([x])=>x===k)![1];
  const key=legend('Key');key.root.classList.add('ra-key');
  const bar=collectionToolbar({label:'People',
    search:all.length>8?{value:keep.q,label:'Find a person',placeholder:`Search ${plural(all.length,'person','people')}`,onInput:q=>{keep.q=q;},apply:()=>render()}:undefined,
    saved:{name:()=>keep.view===first?'':VIEW_WORDS[keep.view],defaultName:()=>VIEW_WORDS[first],dirty:()=>false,
      items:close=>builtinViewItems(views.map(k=>({label:VIEW_WORDS[k],count:count(k),on:keep.view===k,apply:()=>{keep.view=k;render();bar.sync();}})),close)}});
  bar.root.classList.add('rk-tools');
  // The date axis is the timeline's (timeline.ts units): department weeks, the rota's first and last cut at its ends, each
  // labelled with its first day ("4 Oct"); every strip carries the same week lines beneath its marks (CSS: --n days a
  // strip, the first line --o days in). A cut week under 4 days keeps its line but no label, which would not fit.
  const at=(d:string)=>{const i=days.indexOf(d);return i<0?days.length:i;};
  const weeks=units(days[0],plusDays(days[days.length-1],1),'week');
  const axis=(s:HTMLElement)=>{s.style.setProperty('--n',String(days.length));s.style.setProperty('--o',String(at(weeks[0][1])%7));return s;};
  const scale=el('div',{class:'ra-row ra-scale','aria-hidden':'true'},axis(el('span',{class:'ra-strip ra-ticks'},
    ...weeks.map(([a,b])=>{const t=el('span',{},at(b)-at(a)>=4?shortDay(a):'');t.style.setProperty('--a',String(at(a)+1));t.style.setProperty('--b',String(at(b)+1));return t;}))));
  const row=(a:Availability)=>{
    const strip=axis(el('span',{class:'ra-strip',role:'img',tabindex:0,'aria-label':`${a.name}: ${awayWords(a,days)}`}));
    // Each stretch is stamped as its kind's key entry as it is drawn (dom.ts legend `mark`; every mark has one).
    for(const s of a.stretches){const i=key.mark(el('i',{class:'ra-'+s.kind}),s.kind,el('span',{class:'ra-mark ra-'+s.kind,'aria-hidden':'true'}),awayWords_(s.kind),1+AWAY_KINDS.findIndex(([k])=>k===s.kind)/10);
      i.style.setProperty('--a',String(s.from+1));i.style.setProperty('--b',String(s.to+1));strip.append(i);}
    hoverCard(strip,()=>[el('strong',{},a.name),...(a.state==='all'?[el('div',{class:'muted'},'Available every day')]:a.stretches.map(s=>el('div',{},`${s.label} · ${spanWords(s,days)}`)))]);
    return el('div',{class:'ra-row','data-ra':a.state},el('span',{class:'ra-name truncate'},personLink(a.id,a.name,p=>{location.hash=p;})),strip,el('span',{class:'ra-words'+(a.state==='all'?' ok':'')},a.words));};
  function render(){
    key.clear();
    const q=keep.q.trim().toLowerCase();
    const inView=viewPeople(all,keep.view,eligibility),shown=inView.filter(a=>!q||a.name.toLowerCase().includes(q)||(a.rank?.name||'').toLowerCase().includes(q));bar.shown(shown.length,inView.length,['person','people']);
    const out=groupByRank(shown,code).flatMap(([label,rows])=>[el('h3',{class:'rk-h3'},label),...rows.map(row)]);
    list.replaceChildren(...(shown.length?[scale,...out]:[emptyLine(q?'No one matches that search.':'No one in this view.',null,'rk-none')]));
  }
  render();
  root.replaceChildren(bar.root,list,key.root);
}
/** People under one heading per rank id, in the order the server sorted them (rank order), each rank once even when its
 *  people are not next to each other; no rank last. Two ranks named alike (ignoring case and spaces) carry their codes. */
export function groupByRank(people:Availability[],code:(rankId:string)=>string=()=>''):[string,Availability[]][]{
  const groups=new Map<string,{name:string;rows:Availability[]}>();
  for(const a of people){const k=a.rank?.id||'';const g=groups.get(k);if(g)g.rows.push(a);else groups.set(k,{name:a.rank?.name.trim()||'No rank',rows:[a]});}
  const norm=(x:string)=>x.trim().replace(/\s+/g,' ').toLowerCase(),seen=new Map<string,number>();
  for(const [k,g] of groups)if(k)seen.set(norm(g.name),(seen.get(norm(g.name))||0)+1);
  const out=[...groups].filter(([k])=>k).map(([k,g]):[string,Availability[]]=>[seen.get(norm(g.name))!>1&&code(k)?`${g.name} · ${code(k)}`:g.name,g.rows]);
  const none=groups.get('');
  return none?[...out,['No rank',none.rows]]:out;
}
