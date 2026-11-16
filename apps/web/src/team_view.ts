import {el,labelOf,MARKS,overflowMore,type Legend} from './dom.js';
import {TEAM_PROBLEM_WORDS,VIEW_PROPERTY_DEFINITIONS} from './generated.js';
import {issueMarker,issueLine,type Issue} from './issue_marker.js';
import {personPill} from './person_pill.js';
import type {TeamEntry} from './planner_teams.js';
import type {Catalogues,Row} from './types.js';

/** The supervision team (D66, redrawn as D78): one renderer for every place a session shows people.
 *  Team order stays planner_teams.ts's (teamGroups); this only draws one team: the supervisor's pill (weight 600), then
 *  the trainees' pills one indent in, joined by one straight guide line (layout "tree") or inside a hairline outline
 *  ("box", View › Supervision teams). A problem is the shared issue marker (issue_marker.ts) in the supervisor's slot,
 *  never a line of words in a dense view; words show only where `words` asks (the session panel, the person card). */
type Paired={person_id?:string;supervisor_person_id?:string|null;participation?:string;[key:string]:unknown};
export type TeamProblem={kind:'needs'}|{kind:'elsewhere';supervisor:string}|{kind:'unpaired'}|{kind:'over';count:number;limit:number};
/** card: Week, panels, agenda, Home; compact: Month and the compact Week; row: one line (Day bar, List). */
export type TeamDensity='card'|'compact'|'row';
/** View › Supervision teams: the contract's forms for the "supervisor" property; its first is the built-in default. */
export type TeamLayout=typeof VIEW_PROPERTY_DEFINITIONS.supervisor.forms[number];
export const TEAM_LAYOUT_DEFAULT:TeamLayout=VIEW_PROPERTY_DEFINITIONS.supervisor.forms[0];
type Finding={code?:string;severity?:string;detail?:Record<string,unknown>|null;[key:string]:unknown};

const CAPACITY=new Set(['SUPERVISOR_CAPACITY','SUPERVISOR_SCOPED_CAPACITY']);
/** What is wrong with one team (teamGroups' entry), from the recorded pairing and the rota check's findings on this
 *  session (`errors`: a person's error findings; none on a published or unchecked session, so only what the pairing
 *  itself shows). The limit is the finding's own (the rule's number), never a constant. */
export function teamProblem<T extends Paired>(team:readonly TeamEntry<T>[],roster:readonly Paired[],errors:(personId:string)=>readonly Finding[]=()=>[]):TeamProblem|null{
  const head=team[0]?.p;if(!head?.person_id)return null;
  if(team[0].missing)return {kind:'elsewhere',supervisor:head.supervisor_person_id!};
  const found=errors(head.person_id);
  const over=found.find(f=>CAPACITY.has(String(f.code))&&f.severity!=='warning');
  if(over&&team.length>1){const d=over.detail||{},limit=Number(d.permitted_maximum??d.maximum),count=Number(d.scheduled);
    if(Number.isFinite(limit)&&Number.isFinite(count))return {kind:'over',count,limit};}
  if(team.length>1||head.participation!=='trainee'||head.supervisor_person_id&&head.supervisor_person_id!==head.person_id)return null;
  if(found.some(f=>f.code==='SUPERVISION_PAIRING'))return {kind:'needs'};
  // A supervisor is on the session, but this trainee names no one.
  return roster.some(p=>p.person_id!==head.person_id&&p.participation==='supervisor')?{kind:'unpaired'}:null;
}

/** "A, B and C" (the group's accessible name lists people the way a sentence does). */
const and=(xs:string[])=>xs.length<2?xs.join(''):xs.slice(0,-1).join(', ')+' and '+xs.at(-1);
/** The group's full accessible name: "Dr X supervising A (R2) and B (R1)", with the problem said in words. */
export function teamLabel<T extends Paired>(team:readonly TeamEntry<T>[],nameOf:(id:string)=>string,rankOf:(id:string)=>string=()=>'',problem:TeamProblem|null=null):string{
  const said=(e:TeamEntry<T>)=>{const id=e.p.person_id!,r=e.depth||team.length===1?rankOf(id):'';return nameOf(id)+(r?` (${r})`:'');};
  const [head,...kids]=team;if(!head)return '';
  const base=kids.length?`${said(head)} supervising ${and(kids.map(said))}`:said(head);
  switch(problem?.kind){
    case 'needs':return `${TEAM_PROBLEM_WORDS.needs}: ${base}`;
    case 'unpaired':return `${TEAM_PROBLEM_WORDS.unpaired}: ${base}`;
    case 'elsewhere':return `${base}, with ${nameOf(problem.supervisor)}, ${TEAM_PROBLEM_WORDS.elsewhere.toLowerCase()}`;
    case 'over':return `${base}; ${overWords(problem)}`;
    default:return base;
  }
}

/** A person's rank on a day (the rank catalogue's own short code, else its name, as the server's dated `rank` is):
 *  memberships are dated, so a trainee who moves up mid-rota shows the rank they hold on that duty. Indexed once per
 *  memberships list. */
const rankIndex=new WeakMap<Row[],Map<string,Row[]>>();
export function rankOn(catalogues:Catalogues,personId:string,day:string):string{
  const list=catalogues.memberships||[];let byPerson=rankIndex.get(list);
  if(!byPerson){byPerson=new Map();for(const m of list){const k=String(m.person_id);byPerson.set(k,[...(byPerson.get(k)||[]),m]);}rankIndex.set(list,byPerson);}
  const m=byPerson.get(personId)?.find(r=>(!r.starts_on||r.starts_on<=day)&&(!r.ends_on||r.ends_on>day));
  const c=m&&(catalogues.categories||[]).find(x=>x.id===m.category_id);
  return c?String(c.short_code||labelOf(c)):'';
}

/** Must fix: Needs supervisor and "3 · limit N"; could improve: Not on this session and Unpaired. */
const SEVERITY={needs:'error',over:'error',elsewhere:'warning',unpaired:'warning'} as const;
/** The box's problems that are a staffing shortfall (a trainee without a supervisor, a supervisor over their limit): the
 *  coverage counts them as open places (places_open), so the session's own open-place badge leaves them to the box. */
export const statesShortfall=(p:TeamProblem|null|undefined)=>p?.kind==='needs'||p?.kind==='over';
/** Session findings a team already says (its issue marker), so the session's own marks do not say them again. */
export const BOX_CODES=new Set(['SUPERVISION_PAIRING','SUPERVISION_GAP',...CAPACITY]);
/** "3 · limit 2": the contract's one wording (TEAM_PROBLEM_WORDS) with the finding's numbers. */
const overWords=(p:{count:number;limit:number})=>TEAM_PROBLEM_WORDS.over.replace('{n}',String(p.count)).replace('{limit}',String(p.limit));
/** The short words a problem says (the contract's only); Not on this session names the supervisor as its `tip`. */
export function problemWords(problem:TeamProblem,nameOf:(id:string)=>string):{text:string;tip:string}{
  switch(problem.kind){
    case 'over':return {text:overWords(problem),tip:''};
    case 'elsewhere':return {text:TEAM_PROBLEM_WORDS.elsewhere,tip:nameOf(problem.supervisor)};
    default:return {text:TEAM_PROBLEM_WORDS[problem.kind],tip:''};
  }
}
/** A team's problem as the shared marker's issue. */
export function teamIssue(problem:TeamProblem,nameOf:(id:string)=>string):Issue{
  const w=problemWords(problem,nameOf);return {severity:SEVERITY[problem.kind],words:w.text,...(w.tip?{where:'with '+w.tip}:{})};
}

/** One team (D78). `member` draws a person (the Week's chip, a person link); by default a drawn pill. A member that is
 *  a pill is the clean name (a rank only where the Person label adds it). A lone person without a problem is not a team: their member is
 *  returned. `limit`: at most that many trainee pills, then a quiet "+N" pill (`more` opens everyone). `act`: the
 *  marker's Fix / Why (opens the session). */
// A team's "+N" opens its session: one click listener for all of them (capture: before the grid's own handlers).
const mores=new WeakMap<HTMLElement,()=>void>();
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('click',e=>{const b=e.target instanceof Element?e.target.closest<HTMLElement>('.tm-more'):null;
  const run=b&&mores.get(b);if(run){e.stopPropagation();run();}},true);
export function teamView<T extends Paired>(team:readonly TeamEntry<T>[],o:{density:TeamDensity;nameOf:(id:string)=>string;
  rankOf?:(id:string)=>string;member?:(e:TeamEntry<T>)=>HTMLElement;problem?:TeamProblem|null;
  /** Inside a control (an agenda item, a bar): the control says it in words, so the team is drawn only. */presentational?:boolean;
  /** The full names (tips and the accessible name) when the pills show short ones. */fullName?:(id:string)=>string;
  limit?:number;more?:()=>void;layout?:TeamLayout;
  /** Where there is room (session panel, person card): the problem's words beside its marker. */words?:boolean;
  act?:()=>void;/** The duty hue for drawn pills (duty-* class) where no surrounding cell gives it. */hue?:string;
  /** The view's key: the team and its marker are drawn as their entries (dom.ts legend; every mark has one). */key?:Legend}):HTMLElement{
  // The session's own dated rank (the server's, for every reader of the duty) first; else the caller's lookup.
  const given=new Map(team.filter(e=>typeof e.p.rank==='string').map(e=>[e.p.person_id!,String(e.p.rank)]));
  const rank=(id:string)=>given.get(id)??(o.rankOf?.(id)||''),problem=o.problem||null,full=o.fullName||o.nameOf;
  const [head,...rest]=team,sup=team.length>1&&!problem||problem?.kind==='over';
  // A pill is the clean name reviewed: a rank shows only where the view's or the department's Person label adds
  // it (its Rank part, before or after the name). The group's accessible name still says every trainee's rank.
  const drawn=(e:TeamEntry<T>,isSup:boolean)=>{const id=e.p.person_id!;return personPill({label:o.nameOf(id),full:full(id),sup:isSup,key:o.key,person:id});};
  const person=(e:TeamEntry<T>,isSup:boolean)=>{
    if(!o.member)return drawn(e,isSup);
    const m=o.member(e),pill=m.matches('.pchip')?m:m.querySelector<HTMLElement>('.pchip');
    if(isSup)pill?.classList.add('sup');
    return m;};
  if(team.length===1&&!problem)return person(head,false);
  const issue=problem?teamIssue(problem,full):null;
  const groupName=teamLabel(team,full,rank,problem);
  const marker=issue?(o.words?issueLine([issue],{title:groupName,act:o.act,key:o.key}):issueMarker([issue],{title:groupName,act:o.act,inside:o.presentational,key:o.key})):null;
  // The supervisor's slot: their pill and the team's marker; with no supervisor here, the marker heads the guide.
  const headRow=el('div',{class:'tm-row tm-sup'},sup?person(head,true):null,marker);
  const kids=sup?rest:team,shown=o.limit!=null&&kids.length>o.limit?kids.slice(0,o.limit):kids;
  const rows=shown.map(e=>el('div',{class:'tm-row tm-tr'},person(e,false)));
  if(shown.length<kids.length){const n=kids.length-shown.length,go=!!o.more&&!o.presentational;
    const m=overflowMore(n,['trainee','trainees'],{of:`with ${full(head.p.person_id!)}`,action:go?'open the session':undefined,cls:'pchip more tm-more',
      as:go?'button':'hidden'});  // not a button: the team's group name already says every trainee
    if(go)mores.set(m,o.more!);
    rows.push(el('div',{class:'tm-row tm-tr'},m));}
  const layout=o.layout||TEAM_LAYOUT_DEFAULT;
  const box=el('div',{class:`team-box ${layout} ${o.density}${o.hue?' duty-'+o.hue:''}${problem?' tm-'+problem.kind:''}`,
    ...(o.presentational?{'aria-hidden':'true'}:{role:'group','aria-label':groupName})},headRow,el('div',{class:'tm-kids'},...rows));
  // Its swatch: a one-line team of the same layout (a supervisor, then one trainee).
  o.key?.mark(box,'team',el('span',{class:`team-box ${layout} row lg-mark`,'aria-hidden':'true'},el('span',{class:'tm-row tm-sup'},el('span',{class:'pchip sup'},MARKS.sup[0])),
    el('span',{class:'tm-kids'},el('span',{class:'tm-row tm-tr'},el('span',{class:'pchip'},MARKS.tr[0])))),'Supervision team: a supervisor and their trainees');
  return box;
}
