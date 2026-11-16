/* The session panel's Why tab: everyone in the department sorted into who is on the session, who could work it (and
 * what the score says about choosing them), who a rule stops (which rule and why, with Allow once…), who is unavailable
 * (leave as far as Configuration › Department settings shows it) and who is not eligible. "A person" shows one person across the sessions in view instead.
 * The server decides every group, reason and score effect (POST /api/planner/why: the fill-options checks and
 * packages/domain/score.py); this file only words and lists them. */
import {working} from './loading.js';
import {el,button,note,labelOf,formatTime,formatDay,toLocal,select,emptyLine,panelSearch,segmented} from './dom.js';
import {request,onSessionReset} from './api.js';
import {reasonText,findingNumbers,leavePill,leaveLabel,leaveDates} from './findings.js';
import {peopleChoices,type Context} from './forms.js';
import {COMPONENT_LABELS} from './score.js';
import type {Row,WorkspaceData} from './types.js';
import {supervisedBy} from './planner_teams.js';

/** What the Why tab remembers while its panel is open. */
export type WhyState={mode?:'session'|'person';person?:string;group?:string;q?:string};
/** Rules and exceptions, from the planner page (the rule sheet and the exception form are its canonical forms). */
export interface WhyRules{policy:(id:string)=>Row|undefined;open:(id:string,personId?:string)=>void;allowOnce:(ruleId:string,personId:string,iso:string)=>HTMLElement|null}
export interface WhyHooks{
  /** A coordinator's draft session that may change: Assign, Remove and Allow once… are offered. */
  editable:(shift:Row)=>boolean;
  code:(person:Row|undefined)=>string;
  rules?:WhyRules;
  assign:(shift:Row,row:Row)=>Promise<unknown>;
  /** An unresolved named pairing is chosen in the existing fill panel, never bypassed by Why's Assign. */
  chooseSupervisor?:(shift:Row)=>void;
  remove:(shift:Row,row:Row)=>Promise<unknown>;
  /** The mode or person changed: the panel and the grid's highlight follow. */
  redraw:()=>void;
  /** Open another session's panel on this tab. */
  open:(shift:Row)=>void;
}
const GROUPS=[['assigned','Assigned','on this session'],['free','Could work here','free, no rule stops them'],['rule','Blocked by a rule','a mandatory rule stops them'],
  ['unavailable','Unavailable','leave, rotation or another session'],['not_eligible','Not eligible','rank, membership or clinic']] as const;
const cache=new Map<string,Promise<Row>>();
onSessionReset(()=>cache.clear());
export const signed=(n:number)=>(n>0?'+':n<0?'−':'±')+Math.abs(n);

/** The score parts an effect names, in the score panel's order and words (score.ts); with the cap they sum to `points`.
 *  The cap is a change in what blocking findings hold back, not a part of the score, so it is worded as such here. */
const PARTS=['coverage','rules','preferences','fairness','stability','other','gate'] as const;
const partLabel=(c:string)=>c==='gate'?'Publication cap':c==='other'?'First assignment':COMPONENT_LABELS[c];
/** The score effect in words: each score part it moves, then (unless `wishes` is false) each wish it grants or goes against. */
export function effectWords(e:Row|undefined,wishes=true):string[]{
  return e?[...PARTS.filter(c=>e[c]).map(c=>`${partLabel(c)} ${signed(e[c])}`),
    ...(wishes?e.wishes||[]:[]).map((w:Row)=>`${w.granted?'grants':'goes against'} the wish ${w.text}`)]:[];
}

/** The next-best person for the place the person in view holds: the rota's score with them there minus with the
 *  alternative there, before the blocking-issue cap (the number the score panel shows: score.ts shownScore), so a
 *  capped draft still tells two people apart. When only the alternative clears the cap (the capped difference favours
 *  them, the uncapped one does not) that is said first. Only "A person" asks for it: each hover card would score every
 *  alternative (shortcut: add it there if planners miss it, behind the same cache). */
export function runnerUpWords(u:Row|null|undefined):string{
  if(!u)return '';
  const d=Number(u.uncapped_point_difference),n=`${Math.abs(d)} point${Math.abs(d)===1?'':'s'}`;
  const by=d>0?`${n} lower`:d<0?`${n} higher`:'the same score';
  if(Number(u.point_difference)<0&&d>=0)return `Next best: ${u.display_name}, would clear the publication cap`+(d>0?`; otherwise ${by}`:'');
  return `Next best: ${u.display_name}, ${by}`;
}

/** One person on one session, as the Why tab's person mode explains it: their row, or none. Its own cache key, since
 *  this request leaves out the runner-up that "A person" asks for. */
export function whyOne(w:Row,shiftId:string,personId:string):Promise<Row|undefined>{
  const key=[w.id,w.revision,'hover',personId,shiftId].join(':');
  if(!cache.has(key))cache.set(key,request<Row>('/api/planner/why',{method:'POST',body:JSON.stringify({workspace_id:w.id,expected_revision:w.revision,shift_ids:[shiftId],person_id:personId})}).catch(e=>{cache.delete(key);throw e;}));
  return cache.get(key)!.then(result=>(result.sessions as Row[])[0]?.rows?.find((r:Row)=>r.person_id===personId));
}

/** Why someone is or is not on a session, in a few words: the score's view of choosing them and their facts. */
function reason(r:Row,policy:WhyRules['policy']|undefined):string{
  const e=r.effect as Row|undefined;
  const score=effectWords(e);
  if(r.group==='assigned')return ['On this session',r.team_lead?'team lead':'',r.participation&&r.participation!=='staff'?r.participation:'',...score].filter(Boolean).join(' · ');
  if(r.group==='free'){
    const lead=e&&e.points<0?`Not chosen: score ${signed(e.points)}`:e&&e.points>0?`Could be chosen: score ${signed(e.points)}`:'No rule stops them';
    return [lead,...score,`${r.week_hours} h this week`,r.status==='warn'?r.findings[0]?.message:'',r.fills===false?'does not close the gap alone':''].filter(Boolean).join(' · ');
  }
  const f=(r.findings as Row[]).find(x=>x.severity==='error')||r.findings[0];
  if(!f)return reasonText({kind:'RULE'});
  // A rule says how far off it is ("8 h rest (needs 12 h)") and is named beside it; leave says as much as the
  // department shows (its message is worded by findings.ts from what the server sent).
  if(f.kind==='LEAVE')return f.leave?[leaveDates(f.leave),f.leave.note].filter(Boolean).join(' · '):f.code==='UNAVAILABLE'?f.message:leaveLabel();
  const named=f.rule_id&&policy?.(f.rule_id)?.name===f.message;
  return findingNumbers(f)||(named?reasonText({kind:f.kind}):f.message);
}

export function whyTab(ctx:Context,data:WorkspaceData,shift:Row,range:Row[],state:WhyState,hooks:WhyHooks):HTMLElement{
  const w=data.workspace,zone=ctx.me.organisation.timezone,people=new Map(ctx.catalogues.people.map(p=>[p.id,p]));
  const mode=state.mode||'session',root=el('div',{class:'why'});
  // The one panel search (Esc clears first, then the dock) and the one segmented choice (UX › controls S6, S9).
  const search=panelSearch(mode==='person'?'Search sessions':'Search people',mode==='person'?'Find a session':'Find a person or code',undefined,'wk-search');search.value=state.q||'';
  const switcher=segmented<'session'|'person'>('Look at',[['session','This session'],['person','A person']],mode,k=>{state.mode=k;state.group='';state.q='';
    if(k==='person'&&!state.person)state.person=shift.participants[0]?.person_id||'';hooks.redraw();},{cls:'why-mode'});
  root.append(el('div',{class:'why-tools'},search,switcher));
  let ids=[shift.id];
  if(mode==='person'){
    const pick=select('Person',peopleChoices(ctx),state.person||'');
    pick.input.addEventListener('change',()=>{state.person=pick.input.value;state.group='';hooks.redraw();});
    root.append(pick.wrap);
    if(!state.person){root.append(el('p',{class:'muted'},'Choose a person to see, day by day, why they are or are not on each session in view.'));return root;}
    ids=range.slice(0,200).map(s=>s.id);
    if(range.length>200)root.append(note(`The first 200 of ${range.length} sessions in view are explained. Narrow the range for the rest.`));
    if(!ids.length){root.append(emptyLine('No sessions in view.'));return root;}
  }
  const key=[w.id,w.revision,mode,state.person||'',ids.join(',')].join(':');
  if(!cache.has(key))cache.set(key,request<Row>('/api/planner/why',{method:'POST',body:JSON.stringify({workspace_id:w.id,expected_revision:w.revision,shift_ids:ids,...(mode==='person'?{person_id:state.person,runner_up:true}:{})})}).catch(e=>{cache.delete(key);throw e;}));
  const chips=el('div',{class:'why-chips',role:'group','aria-label':'Show'}),list=el('div',{class:'why-list'},
    working(mode==='person'?'Checking each session…':`Checking ${ctx.catalogues.people.length} people against every active rule…`));
  // A screen reader hears a short summary of the result, not the whole list (QA-127c); the list itself is read on demand.
  const said=el('p',{class:'sr-only',role:'status'});
  root.append(chips,said,list);
  const shifts=new Map(data.shifts.map(s=>[s.id,s])),runnerUp=new Map<string,Row>(),firstFree=new Map<string,Row>();
  const sessionLabel=(s:Row)=>{const d=toLocal(s.start,zone).slice(0,10);
    return `${formatDay(d,'UTC')} · ${s.type_snapshot.name} ${formatTime(s.start,zone)}–${formatTime(s.end,zone)}`;};
  cache.get(key)!.then(result=>{
    const rows:{r:Row;s:Row}[]=(result.sessions as Row[]).flatMap(x=>(x.rows as Row[]).map(r=>({r:{...r,source_fingerprint:result.source_fingerprint},s:shifts.get(x.shift_id)!}))).filter(x=>x.s);
    for(const {r,s} of rows)if(r.group==='free'&&!firstFree.has(s.id))firstFree.set(s.id,r);
    for(const x of result.sessions as Row[])if(x.runner_up)runnerUp.set(x.shift_id,x.runner_up);
    // The server stops comparing scores when a session's work runs out: say so rather than leave the gaps unexplained.
    const cut=(result.sessions as Row[]).some(x=>x.score_status==='work_limit');
    const render=()=>{if(!list.isConnected)return;
      const q=search.value.trim().toLowerCase();
      const match=({r,s}:{r:Row;s:Row})=>!q||(mode==='person'?sessionLabel(s).toLowerCase().includes(q):String(r.display_name).toLowerCase().includes(q)||hooks.code(people.get(r.person_id)).toLowerCase().includes(q));
      const found=rows.filter(match),count=(g:string)=>found.filter(x=>x.r.group===g).length;
      // Only groups that hold someone for this session (and the one chosen): no row of "0" chips (G-43). Which chips
      // show does not follow the search, so they never jump while typing; their counts do.
      const held=new Set(rows.map(x=>x.r.group));
      chips.replaceChildren(...[['','All',found.length] as const,...GROUPS.map(([g,name])=>[g,name,count(g)] as const)].filter(([g])=>!g||held.has(g)||state.group===g).map(([g,name,n])=>{
        const b=el('button',{type:'button',class:'button small why-chip','aria-pressed':String((state.group||'')===g)},name,' ',el('b',{class:'why-n'},String(n)));
        b.addEventListener('click',()=>{state.group=g;render();});return b;}));
      const shown=GROUPS.filter(([g])=>!state.group||state.group===g).map(([g,name,hint])=>{
        const items=found.filter(x=>x.r.group===g);
        if(!items.length)return null;
        return el('section',{class:'why-group','aria-label':`${name} (${items.length})`},
          el('h3',{class:'why-head'},el('span',{class:'why-dot '+g,'aria-hidden':'true'}),el('strong',{},name),el('span',{class:'muted'},hint),el('span',{class:'wk-count'},String(items.length))),
          el('ul',{class:'why-rows'},...items.slice(0,q?200:80).map(({r,s})=>row(r,s))));
      }).filter(Boolean) as HTMLElement[];
      said.textContent=[`${found.length} ${mode==='person'?(found.length===1?'session':'sessions'):(found.length===1?'person':'people')}`,
        ...GROUPS.map(([g,name])=>[count(g),name] as const).filter(([n])=>n).map(([n,name])=>`${n} ${name.toLowerCase()}`)].join(', ');
      list.replaceChildren(...(shown.length?shown:[q||state.group?emptyLine('Nobody matches.',button('Show everyone',()=>{search.value='';state.q='';state.group='';render();search.focus();},'secondary small')):emptyLine('Nobody to explain here.')]),
        cut?note(mode==='person'?'Some score comparisons were cut short to keep this quick. Narrow the range for the rest.':`Score effects are worked out for the first ${result.scored_limit} people listed.`):'',
        // How people are compared is help on demand, not a paragraph closing the tab (G-43).
        result.basis?el('details',{class:'wk-basis'},el('summary',{},'How people are compared'),el('p',{},result.basis)):'');
    };
    search.addEventListener('input',()=>{state.q=search.value;render();});
    requestAnimationFrame(render);
  }).catch(error=>list.replaceChildren(note((error as Error).message,'error',error),button('Try again',()=>{cache.delete(key);hooks.redraw();},'secondary small')));

  function row(r:Row,s:Row):HTMLElement{
    const f=(r.findings as Row[]).find(x=>x.severity==='error'&&x.rule_id);
    const policy=f?hooks.rules?.policy(f.rule_id):undefined;
    const link=f&&hooks.rules?(()=>{const a=el('button',{type:'button',class:'link-button small why-rule'},policy?labelOf(policy):'Scheduling fact');a.addEventListener('click',()=>hooks.rules!.open(f.rule_id,r.person_id));return a;})():null;
    const may=hooks.editable(s);
    const pairing=r.supervision&&!r.supervision.proposal;
    const act=!may?null:r.group==='assigned'?button('Remove',()=>hooks.remove(s,r),'ghost small')
      :pairing?(r.supervision.choices.length&&hooks.chooseSupervisor?button('Choose supervisor…',()=>hooks.chooseSupervisor!(s),'secondary small'):null)
      // One primary per list: the first free person (the best fit); the others are secondary (G-43).
      :r.group==='free'?button('Assign',()=>hooks.assign(s,r),firstFree.get(s.id)===r?'primary small':'secondary small')
      :r.group==='rule'&&f?hooks.rules?.allowOnce(f.rule_id,r.person_id,s.start)||null:null;
    act?.setAttribute('aria-label',`${act.textContent} ${mode==='person'?r.display_name+', '+sessionLabel(s):r.display_name}`);
    // A rule that takes no exception (a safety rule, or one set up without) says so, not an empty cell that reads as
    // still loading (SR-21).
    const none=may&&!act&&r.group==='rule'&&f?el('span',{class:'muted why-none'},'No exception allowed'):null;
    // A person's row names the person; in person mode it names the session (and opens it).
    const title=mode==='person'?(()=>{const b=el('button',{type:'button',class:'link-button why-session'},sessionLabel(s));b.addEventListener('click',()=>hooks.open(s));return b;})()
      :el('span',{},el('strong',{},r.display_name),el('span',{class:'muted why-code'},hooks.code(people.get(r.person_id))));
    const leave=(r.findings as Row[]|undefined)?.find(x=>x.leave)?.leave;
    const supervisor=supervisedBy(r,id=>people.get(id)?.display_name||'');
    return el('li',{class:'why-row'},title,act||none||el('span',{}),el('small',{class:'why-text'},leavePill(leave),leave?' ':'',supervisor?'With '+supervisor+' · ':'',reason(r,hooks.rules?.policy),link?' · ':'',link||'',
      r.group==='assigned'&&runnerUp.has(s.id)?el('span',{class:'why-runner-up'},' · '+runnerUpWords(runnerUp.get(s.id))):''));
  }
  return root;
}
