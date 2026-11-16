import {overflowMore,el,icon,hoverCard,popover,button,badge,type IconName,type Legend} from './dom.js';
import {FINDING_MESSAGES,ISSUE_LIST_LIMIT} from './generated.js';
import {issueKey,leavePill,sinceBadge,type Leave,type Since} from './findings.js';
import type {Row} from './types.js';

/** The one issue marker reviewed: every entity with an issue (a person, a team, a
 *  session, a clinic row, a day) shows the same icon, a triangle for must fix (the contract's `error`) or a circle for
 *  could improve (`warning`), so shape as well as colour says it (WCAG 1.4.1), with a count when there are several.
 *  Its words (the contract's TEAM_PROBLEM_WORDS and FINDING_MESSAGES) are its accessible name, its hover/focus card and
 *  its popover (a click or tap pins it; Esc or a tap outside closes it), which holds one action: Fix, or Why when
 *  nothing must be fixed. Words are drawn on the page only where there is room (issueLine: the session panel, the
 *  person card, the Fix list). */
export type Severity='error'|'warning';
export type Issue={severity:Severity;words:string;/** Where it is, when the marker rolls several places up. */where?:string;
  /** The session it is on: activating the issue shows that session (issueList). */shift?:string;
  /** The rota check finding's key (findings.ts issueKey): the Fix list cause that holds it. */key?:string;
  /** What it is (a finding's code, a Fix list cause's id): with its words, the item's identity (sameIssue). */code?:string}&IssueCard;
/** The card form of an issue (UIUX-AUDIT C1: the one item, richer where there is room: a footer health list, a run's
 *  issues, the auto-schedule review, a save's new issues). Any of these makes the item a card: the session, detail and
 *  rule lines; the one deciding `reason`; who could have done it and why not (`why`, behind Why when `fold`); `more`
 *  facts; its own `actions`; the leave `pill`; the server's `cause`; and for a proposal, `since` (draftComparison). */
export type IssueCard={pill?:Leave;session?:string;detail?:string;rule?:{name:string;mandatory:boolean};reason?:string;fold?:boolean;more?:string[];
  whyTitle?:string;why?:{who:string;reason:string}[];actions?:(HTMLElement|null|undefined|false)[];cause?:string;since?:Since;sinceCount?:number};
export const SEVERITY_WORDS:Record<Severity,string>={error:'Must fix',warning:'Could improve'};
/** The same two words keyed by a Fix list cause's level (one map: SEVERITY_WORDS). Never "Blocking", "Rule issue",
 *  "Rule broken", "Needs review" or "need staff" for them (tests/test_web_copy.py). */
export const LEVEL_WORDS={must:SEVERITY_WORDS.error,could:SEVERITY_WORDS.warning} as const;
const GLYPH:Record<Severity,IconName>={error:'alert',warning:'notice'};
type Finding={readonly [key:string]:unknown};
/** A rota check finding as an issue: the contract's short words for its code, else the server's own message. */
export const findingIssue=(f:Finding,where=''):Issue=>({severity:f.severity==='error'?'error':'warning',
  words:FINDING_MESSAGES[String(f.code) as keyof typeof FINDING_MESSAGES]||String(f.message||f.code||''),...(where?{where}:{}),
  ...(f.shift_id?{shift:String(f.shift_id)}:{}),key:issueKey(f as Row),...(f.code?{code:String(f.code)}:{})});
export const worstOf=(xs:readonly Issue[]):Severity=>xs.some(i=>i.severity==='error')?'error':'warning';
/** An issue's identity (UIUX live check F1): what it is and what it says, never its severity, so one finding that is
 *  both must fix and could improve (a mandatory and an advisory line of one rule) is one item, with the worse mark. */
const sameIssue=(i:Issue)=>(i.code||'')+'\u0000'+i.words;
/** Issues as items, worst first: the same identity is one item (its sessions as chips); with `cardsApart` a card stays
 *  its own item (it is already one cause, with its own actions). Must fix first, then the order given. */
function groupIssues(xs:readonly Issue[],cardsApart=false):Issue[][]{
  const groups=new Map<string,Issue[]>();
  for(const [n,i] of xs.entries()){const k=cardsApart&&isCard(i)?'card\u0000'+n:sameIssue(i);groups.set(k,[...(groups.get(k)||[]),i]);}
  return [...groups.values()].sort((a,b)=>Number(worstOf(b)==='error')-Number(worstOf(a)==='error'));
}
/** Chip labels for sessions (UIUX live check F7): the day ("Sun 11"), with its time when two or more share that day
 *  ("Sun 11 · 08:00"), so no two chips read the same unless they are the same day and time. */
export function dayLabels(xs:readonly {day:string;time:string}[]):string[]{
  const days=new Map<string,number>();for(const x of xs)days.set(x.day,(days.get(x.day)||0)+1);
  return xs.map(x=>x.day&&(days.get(x.day)||0)>1&&x.time?`${x.day} · ${x.time}`:x.day);
}
/** "Sun 8. 4 issues. Must fix: X (3 sessions); Could improve: Y (Clinic A)": the accessible name. The same issue is one
 *  part with its worse severity, as in issueList (UIUX-AUDIT L8, live check F1), must fix first; past ISSUE_LIST_LIMIT
 *  parts, "and N more". */
export function issueLabel(xs:readonly Issue[],title=''):string{
  const all=groupIssues(xs);
  const part=(g:Issue[])=>{const places=new Set(g.map(x=>x.shift||x.where||'')),n=places.size,i=g[0];
    const at=n>1?` (${n} ${g.every(x=>x.shift)?'sessions':'places'})`:i.where?` (${i.where})`:'';
    return `${SEVERITY_WORDS[worstOf(g)]}: ${i.words}${at}`;};
  const rest=all.length-ISSUE_LIST_LIMIT,said=all.slice(0,ISSUE_LIST_LIMIT).map(part).join('; ')+(rest>0?`; and ${rest} more`:'');
  return [title,xs.length>1?`${xs.length} issues`:'',said].filter(Boolean).join('. ');
}
/** The icon and its count only (aria-hidden): for a legend, or a mark inside a control that says the words itself. */
export function issueGlyph(severity:Severity,count=1):HTMLElement{
  return el('span',{class:`iss-mark ${severity}`,'aria-hidden':'true'},icon(GLYPH[severity]),count>1?el('span',{class:'iss-n'},String(count)):null);
}
export type MarkerOptions={
  /** Whose issues these are ("Dr A. Oddfellow", "Teaching clinic", "Sun 8"): the popover's heading, the name's start. */
  title:string;
  /** The popover's one action: Fix (any must fix), else Why. Unset: no action (a reader with nothing to open). */
  act?:()=>void;
  /** A roll-up with a Fix list behind it, where the reader has no Fix list here (issueList offers none): "Show all M in Fix list". */
  all?:()=>void;
  /** Inside a control (a person's pill): drawn only, the control says the words and opens the popover (openIssues). */
  inside?:boolean;
  /** The issues as they are when the card or popover opens (a planner grid's held draft), not as drawn. */
  now?:()=>readonly Issue[];
  /** The view's key (dom.ts `legend`): the marker is drawn as its entry, Must fix or Could improve (every mark has one). */
  key?:Legend;
};
const data=new WeakMap<HTMLElement,{xs:readonly Issue[];o:MarkerOptions}>();
const card=(xs:readonly Issue[],o:MarkerOptions)=>[el('div',{class:'hc-head'},el('div',{},el('strong',{},o.title),el('small',{},xs.length>1?`${xs.length} issues`:SEVERITY_WORDS[xs[0].severity]))),
  issueList(xs,{limit:ISSUE_LIST_LIMIT,interactive:false})];
/** Where an activated issue goes, registered once by the app (setIssueNav): `go` shows its session (moving to its week or
 *  day when needed), focused and highlighted; `fixList` opens the Fix list at exactly the causes holding these issues;
 *  `canFix` says whether this reader has a Fix list for them here. Without it the list reads only. */
export type IssueNav={go:(i:Issue)=>void;fixList:(xs:readonly Issue[])=>void;canFix:(xs:readonly Issue[])=>boolean};
let nav:IssueNav|null=null;
export const setIssueNav=(n:IssueNav|null)=>{nav=n;};
const CHIPS=8;
/** One session chip (UIUX-AUDIT C10): its short label ("Fri 11", or where it is), its whole name for the tip and the
 *  accessible name, whether it is the one in focus (`pressed`; unset: no pressed state), whether it is fixed (`done`,
 *  with who), and where it goes. */
export type SessionChip={label:string;full:string;pressed?:boolean;done?:{who?:string};go?:()=>void};
/** THE session chips (the Fix list's cause rows and the issue list's repeated sentences alike): one chip per session,
 *  ✓ with who once fixed, the one in focus pressed. Past `limit` (8), "+N more" shows the rest: in place, or through
 *  `onExpand` when the caller keeps that state across redraws (`expanded`). The last two wrap together, so no chip or
 *  "+N more" is ever alone on a line. No colour mark: a chip says its date or place in words. */
export function sessionChips(chips:readonly SessionChip[],o:{label?:string;limit?:number;expanded?:boolean;onExpand?:()=>void}={}):HTMLElement|null{
  if(!chips.length)return null;
  const limit=o.limit??CHIPS,shown=o.expanded||chips.length<=limit+1?chips:chips.slice(0,limit);
  const chip=(x:SessionChip)=>{const done=x.done,who=done?.who||'';
    const attrs={class:'fix-chip'+(done?' done':'')+(x.pressed?' on':''),...(x.pressed!==undefined?{'aria-pressed':String(!!x.pressed)}:{}),
      'aria-label':done?`${x.full}: fixed${who?`, ${who} assigned`:''}`:x.full,'data-tip':x.full};
    const kids=[done?icon('check'):'',x.label,who?el('span',{class:'fix-chip-who'},who):''];
    if(!x.go)return el('span',attrs,...kids);
    const b=el('button',{type:'button',...attrs},...kids);b.addEventListener('click',x.go);return b;};
  const box=el('div',{class:'fix-chips',role:'group','aria-label':o.label||'Sessions'});
  const draw=(list:readonly SessionChip[])=>{const nodes:HTMLElement[]=list.map(chip);
    // The one "+N" control (dom.ts overflowMore, L-FIXLIST-4): it shows the rest in place, named for what it shows.
    if(list.length<chips.length)nodes.push(overflowMore(chips.length-list.length,['session','sessions'],
      {all:chips.length,cls:'pill-more fix-chip more',open:()=>{if(o.onExpand)o.onExpand();else draw(chips);}}));
    if(nodes.length>2)nodes.splice(-2,2,el('span',{class:'fix-chip-pair'},...nodes.slice(-2)));
    box.replaceChildren(...nodes);};
  draw(shown);
  return box;
}
const isCard=(i:Issue)=>!!(i.session||i.detail||i.rule||i.reason||i.why?.length||i.more?.length||i.pill||i.since||i.actions?.some(Boolean));
/** An item as a card: the D78 icon (shape and colour) and its words, then its lines, reason, Why and actions. People with
 *  the same reason share one Why line ("Dr A, Dr B — at their limit"). */
function cardItem(i:Issue):HTMLElement{
  const groups=new Map<string,string[]>();
  for(const w of i.why||[])groups.set(w.reason,[...(groups.get(w.reason)||[]),w.who]);
  const rule=i.rule?`Rule: ${i.rule.name} (${i.rule.mandatory?'mandatory':'advisory'})`:'';
  const actions=(i.actions||[]).filter(Boolean) as HTMLElement[];
  const lines=[...[...groups].map(([reason,who])=>`${who.join(', ')} — ${reason}`),...(i.more||[])];
  const why=!lines.length?'':i.fold?el('details',{class:'issue-why issue-fold'},el('summary',{},i.whyTitle||'Why'),el('ul',{},...lines.map(t=>el('li',{},t))))
    :el('div',{class:'issue-why'},el('b',{},i.whyTitle||'Why nobody fits'),el('ul',{},...lines.map(t=>el('li',{},t))));
  return el('li',{class:`finding issue-card ${i.severity}`+(i.severity==='error'?' error':''),...(i.cause?{'data-cause':i.cause}:{})},
    el('div',{class:'issue-head'},el('span',{class:`iss-glyph ${i.severity}`,role:'img','aria-label':SEVERITY_WORDS[i.severity]},icon(GLYPH[i.severity])),
      el('div',{class:'issue-text'},el('strong',{},i.words,i.pill?' ':'',leavePill(i.pill)),
        i.since?sinceBadge(i.since,i.sinceCount):'',...[i.session,i.detail,rule].filter(Boolean).map(t=>el('small',{},t!)),
        i.reason?el('p',{class:'issue-main'},i.reason):'')),
    why,actions.length?el('div',{class:'issue-actions'},...actions):'');
}
/** THE issue list reviewed.
 *  Wherever issues are listed: a marker's popover and hover card, the session panel and form, the footer's health lists,
 *  a run's issues and prechecks, the auto-schedule review, a save's new issues. The same sentence is one item, its sessions
 *  as chips (the Fix list's cause and chips model); an item with card fields is one card. An item on one session, and each
 *  chip, is a button that shows its session; the list ends with "Open all in Fix list (N)" when the reader has one.
 *  `limit`: items before "N more" (`expand`: a "Show all N" button instead); `label`: the list's accessible name;
 *  `done`: runs before an action (a popover closes); `interactive:false`: words only (a hover card). */
export function issueList(xs:readonly Issue[],o:{limit?:number;expand?:boolean;label?:string;interactive?:boolean;done?:()=>void;
  /** Where the list stands for a whole Fix list (the footer's Rota health, a run's review): "Open all in Fix list (count)"
   *  opens it whole, instead of at these causes. */openAll?:{count:number;go:()=>void}}={}):HTMLElement{
  const live=o.interactive!==false&&!!nav;
  const all=groupIssues(xs,true);
  const shown=o.limit?all.slice(0,o.limit):all,rest=all.length-shown.length;
  const goes=(i:Issue)=>live&&!!i.shift;
  const go=(i:Issue)=>()=>{o.done?.();nav!.go(i);};
  // The same words twice on one session and place (two rule lines) are one chip: chips are places to go.
  // A session with both a must fix and a could improve of the same issue is one chip, the must fix, in time order.
  const item=(all:Issue[])=>{if(isCard(all[0]))return cardItem(all[0]);const worst=worstOf(all),mixed=all.some(x=>x.severity!==worst);
    const at=new Map<string,Issue>();
    for(const x of all){const k=`${x.shift||''}|${x.where||''}`,was=at.get(k);if(!was||x.severity==='error'&&was.severity!=='error')at.set(k,x);}
    const g=[...at.values()],i=g[0],one=g.length===1;
    const words=[icon(GLYPH[worst]),el('span',{class:'iss-text'},i.words,one&&i.where?el('small',{},' · '+i.where):!one?el('small',{},` · ${g.length}`):null)];
    const head=one&&goes(i)?el('button',{type:'button',class:'iss-go'},...words):el('span',{class:'iss-go'},...words);
    if(one&&goes(i))head.addEventListener('click',go(i));
    const chips=one?null:sessionChips(g.map(x=>{const label=x.where||SEVERITY_WORDS[x.severity];
      return {label,full:`${x.words}: ${label}${mixed?` · ${SEVERITY_WORDS[x.severity].toLowerCase()}`:''}`,...(goes(x)?{go:go(x)}:{})};}),{label:i.words});
    return el('li',{class:`iss-row ${worst}`},head,chips);};
  const open=o.openAll&&o.interactive!==false?button(`Open all in Fix list (${o.openAll.count})`,()=>{o.done?.();o.openAll!.go();},'link-button iss-all')
    :live&&nav!.canFix(xs)?button(`Open all in Fix list (${xs.length})`,()=>{o.done?.();nav!.fixList(xs);},'link-button iss-all'):null;
  const cards=all.some(g=>isCard(g[0])),ul=el('ul',{class:cards?'issue-list':'iss-lines',...(o.label?{'aria-label':o.label}:{})},...shown.map(item));
  const more=rest<=0?null:o.expand?button(`Show all ${all.length}`,()=>{ul.append(...all.slice(shown.length).map(item));more?.remove();},'ghost small iss-more')
    :el('p',{class:'muted iss-more'},`${rest} more`);
  return el('div',{class:'iss-list'},ul,more,open);
}
/** The marker: a button (its popover on click or tap), or with `inside` a drawn mark the enclosing control opens. */
export function issueMarker(xs:readonly Issue[],o:MarkerOptions):HTMLElement|null{
  if(!xs.length)return null;
  const tone=worstOf(xs),parts=[icon(GLYPH[tone]),xs.length>1?el('span',{class:'iss-n','aria-hidden':'true'},String(xs.length)):null];
  const m=o.inside?el('span',{class:`iss-mark ${tone}`,'aria-hidden':'true'},...parts)
    :el('button',{type:'button',class:`iss-mark ${tone}`,'aria-label':issueLabel(xs,o.title),'aria-haspopup':'dialog','aria-expanded':'false'},...parts);
  data.set(m,{xs,o});
  o.key?.mark(m,'issue:'+tone,issueGlyph(tone),SEVERITY_WORDS[tone]);
  hoverCard(m,()=>{const now=current(m);return now?card(now.xs,now.o):[];});
  return m;
}
const current=(m:HTMLElement)=>{const d=data.get(m);if(!d)return null;const xs=d.o.now?.()??d.xs;return xs.length?{xs,o:d.o}:null;};
// One click listener for every marker (not one each, rebuilt on every grid draw): a marker button opens its popover; a
// drawn mark inside a control that openMarkerFrom named opens it against that control instead of the control's own click.
// Capture: it runs before the grid's and the control's handlers, and the click stops here.
const openers=new WeakSet<HTMLElement>();
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('click',e=>{
  const m=e.target instanceof Element?e.target.closest<HTMLElement>('.iss-mark'):null;if(!m||!data.has(m))return;
  if(!data.get(m)!.o.inside){e.stopPropagation();openIssues(m);return;}
  for(let c=m.parentElement;c;c=c.parentElement)if(openers.has(c)){e.preventDefault();e.stopPropagation();openIssues(m,c);return;}
},true);
/** Opens a marker's popover against `anchor` (the marker itself, or the control a drawn marker sits in). */
export function openIssues(marker:HTMLElement,anchor:HTMLElement=marker){
  const d=current(marker);if(!d)return;
  const {xs,o}=d,tone=worstOf(xs);
  popover(anchor,(panel,close)=>{const list=issueList(xs,{limit:ISSUE_LIST_LIMIT,done:close}),open=!!list.querySelector('.iss-all'),more=!!list.querySelector('.iss-more');
    panel.append(el('div',{class:'iss-pop'},el('strong',{class:'iss-pop-title'},o.title),list,
      // Without a Fix list here (a phone, a published rota), a roll-up's own way to all of them.
      !open&&more&&o.all?button(`Show all ${xs.length} in Fix list`,()=>{close();o.all!();},'',{class:'link-button iss-all'}):null,
      // One action: Open all already fixes a must fix; a roll-up whose Fix is its Fix list offers it as Show all; Why stays.
      o.act&&!(open&&tone==='error')&&!(more&&o.all===o.act)?button(tone==='error'?'Fix':'Why',()=>{close();o.act!();},'secondary small iss-act'):null));},{label:'Issues: '+o.title});
}
/** A control that holds a drawn marker (a pill): a click or tap on the mark opens its popover instead of the control. */
export function openMarkerFrom(control:HTMLElement){openers.add(control);}
/** The marker followed by its words: only where there is room (the session panel, the person card, the Fix list). */
export function issueLine(xs:readonly Issue[],o:MarkerOptions):HTMLElement|null{
  const m=issueMarker(xs,o);if(!m)return null;
  return el('div',{class:'iss-line'},m,el('span',{class:`iss-words ${worstOf(xs)}`},xs.map(i=>i.words).join(' · ')));
}
