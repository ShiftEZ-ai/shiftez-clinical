/** The Rules page as the rule catalogue (behind display_settings.rule_catalogue):
 *  one rule per catalogue entry, its lines saying who, where, when and the numbers. The Rules list is one database
 *  (rules_tree.ts: families › rules › lines, and Needs attention); a rule's own page (#rules/<id>) is a lines database
 *  with a line editor docked beside it. Every change goes into one local draft per rule, proposed together. */
import {bidi,departmentTimeZone,notifyWaiting,statusTone,disabledWhy,el,icon,iconButton,menuItem,formatTime,area,badge,grants,ruleFacts,departmentToday,button,compact,check,confirmAction,multiPick,relabel,note,empty,field,footerStatus,legend,dateUnits,formatDate,formDialog,labelOf,notify,plural,popover,sectionHead,select,showDialog,spanLabel,statusBadge,weekOrder,armed,WEEKDAYS,type Child,CAPABILITY,typeColour} from './dom.js';
import {readFailed,refused,failedNote,couldntLoad} from './dom.js';  // a failed read: refused (403) or an error with Retry (maintenance)
import {fullNameOf} from './person_label.js';
import {dateRange,lastDayIncluded} from './date_picker.js';
import {LINE_ENDING_DAYS,TERMS} from './terms.js';
import {openPicker} from './picker.js';
import {skeleton,busy,elapsed,working} from './loading.js';
import {relationOf,lineEffect,effectRunning,waitingWord,effectWords,effectReady,effectRecheckable,relationText,broaderStrength,relaxSeed,promoteSeed,adjustmentEffect,proposalReview,checkEffect,approveAndActivate,type Relation,type Effect,type RelaxTarget,type Checked,type Adjustment,type ActiveChoices,activeChoices,forgetActiveChoices,applyAdjustment,workspaceRevision,type Review} from './rule_check_api.js';
import {setRuleDraftCheck,ruleProposing,ruleKey,redrawWhenSettled,lineSelection,type BulkAction} from './bulk.js';
import {openChange,runBulk} from './bulk_change.js';
import {get,background,command,request,readAfresh,sessionChanged,sessionKey,onSessionReset,ApiError,type Undone} from './api.js';
import {dataTable,optionMenu,type Column,type EditSpec} from './table_view.js';
import {pillList,columnSizing,type PillItem} from './table_menu.js';
import {dockPanel,closeDock,rowMark,type Dock,type PanelPosition} from './side_panel.js';
import {RULE_FIELDS,SESSION_FIELDS,fieldNoun,conditionBuilder,conditionsText,valueTone,hasEach,eachMark,entriesWords,isDateCondition,placeWord,literalValues,negativeOp,hasRelative,operandText,type ConditionField} from './conditions.js';
import {lineReach,savedReach,thisWeek,readsReach,lineDates,type LineReach} from './rule_line_reach.js';
import {highlightRule} from './planner_rules.js';
import {tagRef,membersOf} from './tags.js';
import {ruleConditionFields,safetyRule,catalogueEntryOf,type Context} from './forms.js';
import {rulesDatabase,ruleTreeViews} from './rules_tree.js';
import {departmentRules,forgetDepartmentRules,type CapturedRulesSource} from './rules_explained.js';
import {conversions,readsSimplify} from './rule_simplify.js';
import {rangeParts,numbersEl,partsText,levelBadge,levelWords,setAdvisoryWeights,lineNumbers,lineSessions,ruleSentence,type NumberPart} from './rule_view.js';
import {ruleReview} from './rule_review.js';
import {RULE_FAMILIES,RULE_CATALOGUE,RULE_CATALOGUE_V2,MAX_SCORE_INPUT,NEVER_OVERRIDDEN,type Conditions,type Condition,type RuleCatalogueEntry,type RuleLine,type WorkloadPeriod,type RuleLineValidity,type RuleLineReplacement,type RuleAttentionCounts} from './generated.js';
import type {Row} from './types.js';
import {mayDecide} from './independence.js';
import {goalMeasure,METRIC_WORDS} from './balance_goal.js';
import {menu,opens,refocusItem} from './dom.js';  // the shared menu (UX › controls S2b)
import {popup} from './dom.js';  // what a button opens (UX › controls S4)
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)
import {moreWords,selfApprovedBadge} from './dom.js';  // the one "+N" and its words (UX › controls S9); the one Self-approved mark
import {routeHash} from './routes.js';

export type Entry=RuleCatalogueEntry&{rule:Row|null;proposed:(Row&{diff:Row})|null;reach:Record<string,number>;people:number;overlaps:number;
  /** Line id → the lines it overlaps: who both hold today and which line applies. */line_overlaps?:Record<string,{line_id:string;people:number;winner:string|null}[]>;
  /** Line id → the numbers it leaves blank and takes from a broader line. */inherited?:Record<string,Inherited>;
  /** Line id → how specific its who is (the server's rule_lines.tier). */line_specificity?:Record<string,number>;
  /** Team leads with no version yet: duty types still setting their own lead, to convert first (the server's plan). */legacy_leads?:number;
  /** Winning line id → the broader line and the numbers it replaces (rule_lines.outranking_by_number). */line_replaces?:Record<string,RuleLineReplacement[]>;
  /** Line id → whether it is in force on the catalogue's day, its rule's own dates included (rule_lines.line_validity). */line_validity?:Record<string,RuleLineValidity>};
/** One fix of an issue (rule_lines.attention): line ops for the draft; none = keep. */
export type Fix={kind:string;recommended:boolean;numbers?:string[];ops:{line_id:string;remove?:boolean;numbers?:RuleLine['numbers'];ends_on?:string}[];line_id?:string;workspace_id?:string;workspace?:string};
export type Issue={id:string;kind:string;rule_id:string;catalogue_id:string;lines:number[];line_ids:string[];people:number|null;facts:Row;fixes:Fix[];
  /** Who acknowledged it for everyone, while its lines stay as they are (rule.acknowledge). */
  fingerprint:string;acknowledged:{by:string;by_id:string;at:string;note:string}|null};
type RulesSourceOption={workspace_id:string|null;label:string;captured_at:string|null;changed:number|null};
export type Catalogue={families:{id:string;name:string}[];entries:Entry[];day:string;attention:Issue[];catalogue_version?:number;
  source_options?:RulesSourceOption[];source?:CapturedRulesSource;captured_other_rules?:Row[];
  /** Unique rules and lines with an unacknowledged issue (rule_catalogue.attention_counts), hidden lines included. */attention_counts?:RuleAttentionCounts};
/** A line with the rule it belongs to (the rule page). */
export type LineRow={entry:Entry;line:RuleLine;n:number};
export type Options={parts:string[];openTag:(tag:Row)=>void;navigate:(view:string)=>void;
  /** The dirty-close guard: leaving asks first while this says there are changes not yet proposed. */setLeave?:(dirty:(()=>boolean|string)|null)=>void;
  /** Rules › Which rule applies? (app.ts), for one person when given. */whichRule?:(personId?:string)=>void;
  /** Existing dated exception requests and their independent approval. */exceptions?:()=>void;
  /** Shared full rule detail, including source and history. */openRule?:(rule:Row)=>void;
  /** One row of a rule: a catalogue rule's line, or an older rule's person row. */openLine?:(ruleId:string,at:{lineId?:string;personId?:string})=>void;
  /** "In <draft>": how the rule in force fares in a draft being planned (app.ts draftHealth). */inDraft?:(rule:Row)=>HTMLElement;
  source?:{workspace:string};
  frame?:()=>void;
  /** The Rules list's catalogue read, started at boot (readRulesAhead): taken by the first draw instead of asking again. */ahead?:Promise<Catalogue>|null};

export const WAITING='Waiting for approval',ENDS_SOON=`Ends within ${LINE_ENDING_DAYS} days`,CURRENT='Current or later';
export const linesOf=(e:Entry):RuleLine[]=>(e.rule?.definition?.rule?.lines||[]) as RuleLine[];
export const isOn=(e:Entry)=>!!e.rule&&e.rule.definition.rule.on!==false;

// ---- One draft per rule, shared by the Rules database and a rule's page (changes accumulate until proposed) ----
export type Settings=Record<string,Record<string,any>>;
/** `seen`: the line ids the draft started from. A default seeded meanwhile keeps the rule's id and revision, so the
 *  proposal names this baseline (not a refreshed entry's) and the server refuses it as stale. */
export type Draft={on:boolean;lines:RuleLine[];settings:Settings;balance:boolean;base:number|null;baseId:string|null;seen:string[]};
const drafts=new Map<string,Draft>(),draftEntries=new Map<string,Entry>();
// Another account in this tab starts with no rule edits of the one before (also where the Rules page is not opened).
onSessionReset(()=>{drafts.clear();draftEntries.clear();movedDrafts.clear();});
let draftSession=-1,draftAccount='';
const clone=<T>(x:T):T=>JSON.parse(JSON.stringify(x));
/** Unsent drafts whose rule was replaced or proposed meanwhile, by rule: never applied or counted, kept to compare
 *  until discarded. Each still names the version it was made on (baseId, base). */
const movedDrafts=new Map<string,Draft>();
/** A draft's base is checked against the server, never against a reply kept by this tab (api.ts keptReads): the
 *  redraw that would correct a kept reply waits while there are unsent edits, so it would never come. */
const ask=<T>(path:string)=>drafts.size?background<T>(path):get<T>(path);
/** A draft made on another version of this rule (replaced or proposed meanwhile) stops being its draft. */
function settle(e:Entry){const d=drafts.get(e.id);if(!d||sameBase(e,d)&&!e.proposed)return;discardDraft(e.id);movedDrafts.set(e.id,d);}
/** Says the rule changed under an unsent draft and shows that draft against the rule as it is now. */
export function movedNote(e:Entry,names:Namer,redraw:()=>void):HTMLElement|''{
  const d=movedDrafts.get(e.id);if(!d)return '';
  const box=note(TERMS.changedWhileEditing(e.name),'warning');
  // Discarding cannot be undone (the draft lives in this tab only): the armed same-button confirm.
  box.append(el('p',{},TERMS.unsentChangeKept),changeList(e,linesOf(e),d.lines,names,d.settings,[TERMS.ruleNow,TERMS.yourChange]),
    armed(button(TERMS.discardUnsentChange,()=>{movedDrafts.delete(e.id);redraw();},'secondary small')));
  return box;
}
/** The Rules list: a notice per rule up to two; from three, one notice that opens them (DL-023). */
export function movedNotes(entries:Entry[],names:Namer,redraw:()=>void):(HTMLElement|'')[]{
  const moved=entries.filter(e=>movedDrafts.has(e.id));if(moved.length<3)return moved.map(e=>movedNote(e,names,redraw));
  const box=note('','warning'),open=button(TERMS.changedWhileEditing(plural(moved.length,'rule')),()=>box.replaceWith(...moved.map(e=>movedNote(e,names,redraw))),'',{class:'link-button'});
  box.append(open);return [box];
}
const sameBase=(e:Entry,d:Draft)=>d.base===(e.rule?.revision??null)&&d.baseId===(e.rule?.id??null);
/** The rule's draft once changed, else what is in force. A draft made on another version of the rule (proposed or
 *  approved meanwhile) is set aside to compare (movedNote), never edited further. */
export function draftOf(e:Entry):Draft{
  const d=drafts.get(e.id),base=e.rule?.revision??null;
  if(d&&sameBase(e,d)&&!e.proposed)return d;
  settle(e);
  return {on:isOn(e),lines:clone(linesOf(e)),settings:clone(settingsOf(e)),balance:balanceOf(e),base,baseId:e.rule?.id??null,seen:linesOf(e).map(l=>l.id)};
}
/** What the Rules list shows for a rule: its kept draft, else what is in force, read only (never cloned: the list reads
 *  this ~10 times per rule per draw). Anything that edits takes draftOf, which copies. */
export function draftView(e:Entry):{on:boolean;lines:readonly RuleLine[]}{
  const d=drafts.get(e.id);
  return d&&sameBase(e,d)&&!e.proposed?d:{on:isOn(e),lines:linesOf(e)};
}
/** Keep a changed draft (it then counts on the Rules page and survives moving between it and the rule's page). */
export const keepDraft=(e:Entry,d:Draft)=>{drafts.set(e.id,d);draftEntries.set(e.id,e);};
export const discardDraft=(id:string)=>{drafts.delete(id);draftEntries.delete(id);};
/** A proposal's draft as it was proposed (by proposal id): Undo of the proposal (it withdraws it) brings the draft back,
 *  unless the rule has a new draft by then. shortcut: in memory for this tab only, like the drafts themselves. */
const proposedDrafts=new Map<string,{e:Entry;d:Draft}>();
onSessionReset(()=>{proposedDrafts.clear();pendingLines.clear();schema=null;});
export const rememberProposed=(id:string,e:Entry,d:Draft)=>{proposedDrafts.set(id,{e,d});};
export function restoreProposedDrafts(done:Undone){
  for(const x of done){const id=x.kind==='policy.decline'?String(x.result?.record?.id||''):'',kept=id&&proposedDrafts.get(id);
    if(!kept)continue;proposedDrafts.delete(id);if(!drafts.has(kept.e.id))keepDraft(kept.e,kept.d);}
}
/** What a draft changes: lines added, removed or changed, on/off (a rule in force), settings and Balance between people. */
export function changesIn(e:Entry,d:Draft):number{
  if(e.proposed)return 0;
  return lineChanges(linesOf(e),d.lines).length+(d.on!==isOn(e)?1:0)+(JSON.stringify(d.settings)!==JSON.stringify(settingsOf(e))?1:0)+((d.balance??balanceOf(e))!==balanceOf(e)?1:0);
}
export const changesOf=(e:Entry)=>{const d=drafts.get(e.id);return d&&sameBase(e,d)?changesIn(e,d):0;};
setRuleDraftCheck(key=>[...draftEntries.values()].some(e=>ruleKey(e)===key&&changesOf(e)>0));
/** Every changed rule, in catalogue order. */
export const changedRules=(entries:Entry[])=>entries.filter(e=>changesOf(e)>0);

// ---- Words for a line (one place: the Rules database, Needs attention and the rule page) ----
type Num={value?:number|null;level?:string|null};
type LineNumbers=Pick<RuleLine,'numbers'>&{mandatory_reasons?:Record<string,string>};
const displayedStrength=(line:LineNumbers|undefined,key:string,level:string|null|undefined)=>{
  const reason=line?.mandatory_reasons?.[key];return {level:reason?'mandatory':level??null,reason};
};
/** Blank inherits: each number a line leaves blank, the value it takes from a broader
 *  line and that line (the server's `inherited`). */
export type Inherited=Record<string,Num&{from:string}>;
/** One number: "3" or "None" (its strength is shown beside it in words, `levelWords`). */
export const numberText=(n:Num)=>n.value==null?'None':String(n.value);
const shown=numberText;
/** A line's numbers in parts: "2 – 5 → 4" (min – max → target), "≥ 2", "≤ 3", "→ 4", "applies", "priority 3"
 *  (`rangeParts`, the one style), each with its strength (`level`). A blank number taken from a broader line is marked
 *  "↰" (`inherited`); an explicit None is no number. */
export function numberParts(line:LineNumbers|undefined,inh?:Inherited,applies='applies'):NumberPart[]{
  const own=(line?.numbers||{}) as Record<string,Num>;
  const get=(k:string):NumberPart|null=>{const o=own[k],i=inh?.[k];if(o)return o.value==null&&k!=='apply'?null:{text:shown(o),...displayedStrength(line,k,o.level??i?.level)};
    return i&&i.value!=null?{text:shown(i)+'↰',from:i.from,...displayedStrength(line,k,i.level)}:null;};
  const out=rangeParts(get('min'),get('max'),get('target'));
  for(const [k,x] of Object.entries(own)){if(['min','max','target'].includes(k)||x.value==null&&k!=='apply')continue;
    out.push(...(out.length?[{text:' · '}]:[]),{text:k==='apply'?applies:`${k.replace('_',' ')} ${shown(x)}`,...displayedStrength(line,k,x.level)});}
  return out;
}
export const numbersText=(line:LineNumbers|undefined,inh?:Inherited,applies?:string,unit='')=>{const p=numberParts(line,inh,applies);return p.length?partsText(p,unit):'';};
/** The same, drawn in the one style (`numbersEl`): an inherited number muted, its tooltip naming the line it comes from. */
export const numbersNode=(line:LineNumbers,inh:Inherited|undefined,from:(id:string)=>string,applies?:string,key?:RuleViewKey):HTMLElement=>{
  const n=numbersEl(numberParts(line,inh,applies),from);return key?key.numbers(n):n;};
/** A prohibition is distinct from a zero workload quota. */
/** A line's reach in a table cell: a count, "—" when not known, or flagged when the line reaches no one today. Reach is
 *  today's: a line that starts later or has ended says that (its Valid facts), never "No one". */
export const reachNode=(n:number|null|undefined,line?:RuleLine,today=departmentToday()):Child=>{if(n==null)return '—';if(n)return String(n);
  const valid=line?validFacets(line,today):[];
  if(valid.includes('Starts later')||valid.includes('Ended'))return ruleMark(el('span',{class:'muted'},valid.includes('Ended')?'Ended':'Starts later'),valid.includes('Ended')?'ended':'starts_later');
  const b=ruleMark(badge('⚠ No one','warn'),'no_one');b.dataset.tip='No active person matches its who and where today: change who or where, or end the line';return b;};
export const appliesWord=(e:RuleCatalogueEntry)=>e.modes?.includes('no_duties')?'no duties':undefined;
/** "Everyone · All clinics": a line named for an inherited number's tooltip. */
export const sourceWords=(lines:()=>RuleLine[],names:Namer)=>(id:string)=>{const l=lines().find(x=>x.id===id);return l?`${countWords(l.who,names,'Everyone')} · ${countWords(l.where,names,'All clinics')}`:'a broader line';};
/** Who a line names, in the order the most specific wins: a person (3) › a group or #tag (2) › everyone (1): the
 *  server's `line_specificity` (rule_lines.tier after tag expansion), by who, from the catalogue and every check. A who
 *  not yet checked reads as a group (only "more specific than everyone" matters until its check answers). */
const specificity=new Map<string,number>();
const whoKey=(l:Pick<RuleLine,'who'>)=>JSON.stringify(l.who?.conditions.length?l.who:null);
const learn=(lines:RuleLine[],tiers:Record<string,number>|undefined)=>{for(const l of lines)if(tiers?.[l.id])specificity.set(whoKey(l),tiers[l.id]);};
export const tier=(l:RuleLine)=>l.who?.conditions.length?specificity.get(whoKey(l))??2:1;
/** One order wherever a rule's lines are listed (the Rules list and a rule's page): the most specific who first, as
 *  it wins (a person, then a group or #tag, then everyone); all clinics before a set where. Sort ascending. */
export const specificFirst=(l:RuleLine)=>-tier(l)*2+(l.where?.conditions?.length?1:0);
/** A line that sets a safety number (D7, C1): one of its numbers fills a slot of a safety kind (the contract's
 *  NEVER_OVERRIDDEN: restriction / no duties, min_rest, forbidden_transition…), as schemas.safety_rule judges it. */
export const setsSafety=(e:Pick<RuleCatalogueEntry,'numbers'>,l:Pick<RuleLine,'numbers'>)=>Object.keys(l.numbers||{}).some(k=>(e.numbers[k]||[]).some(slot=>NEVER_OVERRIDDEN.includes(slot.kind)));
/** The lock a safety line carries wherever its numbers are read (the Rules list and a rule's page). */
export const safetyLock=()=>ruleMark(el('span',{class:'rk-lockmark',role:'img','aria-label':TERMS.safetyLineProtection,'data-tip':TERMS.safetyLineProtection},icon('lock')),'safety');
/** one where's lines read as "a group line with its exceptions", the one layout of the Rules list and a rule's
 *  page. The anchor is the single broadest line (Everyone, else one group line); every other line sits under it, most
 *  specific first. A safety rule (whoWins: every matching line applies, the strictest binds), or lines with no single
 *  broadest one, stay flat. In a mixed-safety rule its safety lines stay flat after the tree (`always`: "Always
 *  applies"). Layout only: what a line replaces is the server's (replacedWords). */
export type LineShape<T>={item:T;under:boolean;always:boolean};
export function exceptionTree<T>(e:Pick<RuleCatalogueEntry,'safety'|'mixed_safety'|'numbers'>,items:readonly T[],lineOf:(t:T)=>RuleLine):LineShape<T>[]{
  const sorted=[...items].sort((a,b)=>specificFirst(lineOf(a))-specificFirst(lineOf(b)));
  if(e.safety&&!e.mixed_safety)return sorted.map(item=>({item,under:false,always:false}));
  const always=e.mixed_safety?sorted.filter(t=>setsSafety(e,lineOf(t))):[];
  return [...countTree(sorted.filter(t=>!always.includes(t)),lineOf),...always.map(item=>({item,under:false,always:true}))];
}
function countTree<T>(sorted:T[],lineOf:(t:T)=>RuleLine):LineShape<T>[]{
  const flat=sorted.map(item=>({item,under:false,always:false}));
  if(sorted.length<2)return flat;
  const tiers=sorted.map(t=>tier(lineOf(t))),low=Math.min(...tiers);
  if(low>=3||tiers.filter(t=>t===low).length>1)return flat;
  const at=tiers.indexOf(low);
  // Every more specific line is an exception to the anchor, whatever its strength (its own badge says that); the rule
  // page's "Exception:" label follows `under`.
  return [{item:sorted[at],under:false,always:false},...sorted.filter((_,i)=>i!==at).map(item=>({item,under:true,always:false}))];
}
/** "(instead of …)" words for a line, only from the server's line_replaces (the resolver's own precedence): the one broader
 *  line it replaces, only the numbers the server lists, in the measure words. '' when the server lists nothing for it, or
 *  more than one broader line (no single "instead of" is true then). */
export function replacedWords(e:RuleCatalogueEntry,l:RuleLine,lines:readonly RuleLine[],replaces:Record<string,RuleLineReplacement[]>|undefined,rule:Settings={}):string{
  const r=replaces?.[l.id];if(!r||r.length!==1||!r[0].numbers.length)return '';
  const b=lines.find(x=>x.id===r[0].replaced_line_id);if(!b)return '';
  // The values alone: a strength word is never part of what the line replaces.
  const numbers=Object.fromEntries(r[0].numbers.filter(k=>b.numbers?.[k]).map(k=>[k,{value:b.numbers[k].value}])),text=numbersText({numbers} as RuleLine);
  return text?[text,unitText(e,b,rule)||unitWords(e,b)].filter(Boolean).join(' '):'';
}

const kindOf=(field:string)=>RULE_FIELDS.find(f=>f.key===field)?.kind||'';
/** Names for record ids (catalogue rows by id, built once per draw: 1,000 lines stay fast) and tags. */
export function namer(ctx:Context){
  const cache=new Map<string,Map<string,Row>>(),tags=new Map((ctx.catalogues.tags||[]).map(t=>[t.id,t]));
  const of=(kind:string)=>{let m=cache.get(kind);if(!m){m=new Map((ctx.catalogues[kind]||[]).map(r=>[r.id,r]));cache.set(kind,m);}return m;};
  const label=(field:string,value:string)=>{const tag=tags.get(value);if(tag)return '#'+tag.name;const kind=kindOf(field);return kind?labelOf(of(kind).get(value)):value;};
  return {label,tag:(value:string)=>tags.get(value)};
}
/** A who or where value in words: a clinic in a who is the people who can work there, never the clinic itself. */
const valueText=(names:Namer,field:string,v:string)=>(field==='eligible_service_ids'?'Can work at ':'')+names.label(field,v);
export type Namer=ReturnType<typeof namer>;
/** A who or where in plain words ("Everyone", "Interns · #exceptional_trainee"), for search, filters and tooltips. */
/** `max`: a list longer than this names its first ones and "+N more" (a diff line naming 30 clinics). */
export function treeText(tree:Conditions|null|undefined,names:Namer,none:string,max=Infinity):string{
  if(!tree?.conditions?.length)return none;
  // A date set after another where reads "Clinic A · on Public holidays (Eid al-Fitr)"; first, the caller says "on".
  // A compared value (Duty date is within 7 days before the duty, Is empty) reads as the shared sentence.
  return tree.conditions.map((c,i)=>'conditions' in c?'('+treeText(c,names,none,max)+')':c.value!=null||c.op==='empty'||c.op==='not_empty'?conditionsText({match:'all',conditions:[c]},(f,v)=>valueText(names,f,v))
    :(c.op==='none'?'except ':i&&isDateCondition(c)?'on ':'')+c.values.slice(0,max).map(v=>valueText(names,c.field,v)).join(c.op==='all'?' + ':', ')+(c.values.length>max?' '+moreWords(c.values.length-max):'')+entriesWords(c)).join(tree.match==='any'?' or ':' · ');
}
/** A who or where as pills, one per value, on one line: the first `shown` and "+N" opening the numbered list. */
export function pills(ctx:Context,tree:Conditions|null|undefined,names:Namer,none:string,openTag:((tag:Row)=>void)|undefined,label:string,shown=2,key?:RuleViewKey):HTMLElement{
  // A rank pill names itself; a person, group or clinic pill's hue is in the view's key.
  const plain=(text:string,kind:string,dot='')=>{const p=el('span',{class:`rp ${kind}`,title:text,...(kind.startsWith('duty-')?{'data-lg-self':''}:{})},dot?el('span',{class:'lg-swatch '+dot,'aria-hidden':'true'}):'',bidi(text));if(/\bexcept\b/.test(kind))ruleMark(p,'not');return key&&(HUED.has(kind)||kind.startsWith('duty-'))?key.mark(p,HUED.has(kind)?kind:'rank'):p;};
  if(!tree?.conditions?.length)return plain(none,'all');
  const items:PillItem[]=[],nouns:(readonly [string,string]|null)[]=[];
  const one=tree.match!=='any'||tree.conditions.length===1;
  for(const c of tree.conditions){
    if('conditions' in c||!one||c.op==='all'&&c.values.length>1||c.entry_names?.length||c.value!=null||c.op==='empty'||c.op==='not_empty'){const tree1:Conditions='conditions' in c?c:{match:'all',conditions:[c]};items.push(plain(treeText(tree1,names,none),...joinedTone(tree1)));nouns.push(null);continue;}
    for(const v of c.values){const t=names.tag(v);nouns.push(fieldNoun(t?'tags':c.field));
      items.push(t?()=>{const r=ruleMark(tagRef(ctx,t,openTag),'tag');if(c.op==='none'){r.querySelector('.chip-text')?.prepend('not ');r.classList.add('rc-not');r.setAttribute('aria-label','Not '+r.getAttribute('aria-label'));ruleMark(r,'not');}return key?key.stamp(r):r;}:plain((c.op==='none'?'not ':'')+valueText(names,c.field,v),`${fieldTone(c.field,v)[0]}${c.op==='none'?' except':''}`,fieldTone(c.field,v)[1]));}
  }
  const list=pillList(items,{label,shown,nouns});list.classList.add('rc-pills');
  if(hasEach(tree)){const m=eachMark();list.append(key?key.mark(m,'each'):m);}
  return key?key.stamp(list):list;
}
/** A pill's look from its condition's field (the design rules: who/where): a rank its own tint (rankColour), a
 *  programme, tag group or skill purple, a person sky, a clinic or place teal, a duty type its own hue as a swatch on a
 *  plain pill (as the Schedule shows it); Everyone (grey) only for an empty who. [kind, swatch class]. */
const fieldTone=(field:string,value:string):[string,string]=>field==='assignment_type_ids'?['dotted','duty-'+typeColour(value)]:[valueTone(field,value),''];
/** Several values in one pill: their one look, else ranks of several tints as a plain pill with the ranks swatch, else a group. */
const joinedTone=(tree:Conditions):[string,string]=>{const v=valuesOf(tree),t=v.map(x=>fieldTone(x.field,x.value));
  if(new Set(t.map(x=>x[0]+'|'+x[1])).size===1&&t.length)return t[0];
  if(new Set(t.map(x=>x[0])).size===1&&t[0]?.[0]==='dotted')return ['dotted',''];
  return v.length&&v.every(x=>/category_ids?$/.test(x.field))?['dotted','lg-ranks']:['group',''];};
/** The values a who or where names (flat; nested groups as one). */
export const valuesOf=(tree:Conditions|null|undefined):{field:string;value:string;op:string}[]=>(tree?.conditions||[]).flatMap(c=>'conditions' in c?valuesOf(c):literalValues(c).map(value=>({field:c.field,value,op:c.op||'any'})));
const KIND_NOUN:Record<string,[string,string]>={person_ids:['person','people'],category_ids:['rank','ranks'],programme_ids:['programme','programmes'],capability_ids:['skill','skills'],
  rotation_type_ids:['rotation','rotations'],cohort_ids:['cohort','cohorts'],tags:['tag','tags'],eligible_service_ids:['clinic they can work at','clinics they can work at'],
  service_ids:['clinic','clinics'],location_ids:['place','places'],assignment_type_ids:['duty type','duty types'],usual_assignee_ids:['usual assignee','usual assignees']};
/** "6 ranks", "Dr H. Faris", "R1, R2", "Everyone": a who or where counted in words (names up to two values). */
export function countWords(tree:Conditions|null|undefined,names:Namer,none:string):string{
  const values=valuesOf(tree);
  if(!tree?.conditions?.length)return none;
  // Filters in groups (a or b, and not c) are counted as filters: their words are the panel's, not a title's.
  if(tree.conditions.some(c=>'conditions' in c))return plural(tree.conditions.flatMap(c=>'conditions' in c?c.conditions:[c]).length,'filter');
  // One or two values read as their names ("Physical Urgent Care · Evening shift"): a count would hide which.
  if(values.length<=2)return treeText(tree,names,none);
  const kinds=[...new Set(values.map(v=>names.tag(v.value)?'tags':v.field))];
  return kinds.length===1?plural(values.length,...(KIND_NOUN[kinds[0]]||['value','values'])):plural(values.length,'value');
}
/** Who or where across a rule's lines, counted by kind, broad to specific ("everyone, 4 ranks, 30 people"): a line
 *  naming none (or only exceptions) counts as `none`. */
const SIDE_ORDER=['category_ids','programme_ids','tags','capability_ids','rotation_type_ids','cohort_ids','eligible_service_ids','usual_assignee_ids','person_ids','service_ids','location_ids','assignment_type_ids'];
const PEOPLE_IN_WHERE='usual_assignee_ids';
function sideCounts(lines:readonly RuleLine[],side:'who'|'where',names:Namer,none:string):string{
  let any=false;const kinds=new Map<string,Set<string>>();
  for(const l of lines){const v=(side==='who'?[...valuesOf(l.who),...valuesOf(l.where).filter(x=>x.field===PEOPLE_IN_WHERE)]:valuesOf(l.where).filter(x=>x.field!==PEOPLE_IN_WHERE)).filter(x=>x.op!=='none');if(!v.length)any=true;
    for(const x of v){const k=names.tag(x.value)?'tags':x.field;let set=kinds.get(k);if(!set)kinds.set(k,set=new Set());set.add(x.value);}}
  const at=(k:string)=>{const i=SIDE_ORDER.indexOf(k);return i<0?SIDE_ORDER.indexOf('person_ids')-.5:i;};
  return [...(any?[none]:[]),...[...kinds].sort((a,b)=>at(a[0])-at(b[0])).map(([k,set])=>plural(set.size,...(KIND_NOUN[k]||['value','values'])))].join(', ');
}
/** A rule row's one count line: "41 lines · 40 in force · who: everyone, 4 ranks, 30 people · where: all clinics,
 *  3 clinics"; "in force" only when it differs (the rule on). Counts the lines the list shows. */
export function ruleCounts(lines:readonly RuleLine[],names:Namer,day:string,on:boolean,at=(line:RuleLine)=>effectiveOf(line,day)):string{
  if(!lines.length)return 'no lines';
  const inForce=lines.filter(l=>at(l)==='active').length;
  return [plural(lines.length,'line'),...(on&&inForce!==lines.length?[`${inForce} in force`]:[]),`who: ${sideCounts(lines,'who',names,'everyone')}`,`where: ${sideCounts(lines,'where',names,'all clinics')}`].join(' · ');
}
/** A rule's strength, said once on its row: its one level when every number shares it, else Mandatory and Advisory. */
// shortcut: a blank (inherited) strength reads as the rule's fallback level, not the broader line it inherits from, so a badge
// can show the fallback where the effective level differs; upgrade when the catalogue sends each line's effective level (a server field), then read that.
export function ruleLevels(e:RuleCatalogueEntry,lines:readonly RuleLine[]):string[]{
  const all=[...new Set(lines.flatMap((l:LineNumbers)=>Object.entries(l.numbers||{}).map(([k,n])=>l.mandatory_reasons?.[k]?'mandatory':n?.level??fallbackLevel(e,k))))].filter(v=>v!=='off');
  return all.length<=1?all:[...new Set(all.map(v=>v==='mandatory'?'mandatory':'advisory'))];
}
/** Captures show the stored strength, including blank values that inherit from broader lines. */
export function capturedRuleLevels(lines:readonly LineNumbers[]):(string|null)[]{
  return [...new Set(lines.flatMap(line=>Object.entries(line.numbers)
    .filter(([key,number])=>key==='apply'||number?.value!=null)
    .map(([key,number])=>displayedStrength(line,key,number?.level).level)))];
}
/** The days a line holds on: none set (or an empty list) is every day, as the server reads it. */
const daysOf=(l:Pick<RuleLine,'weekdays'>)=>l.weekdays?.length?l.weekdays:[0,1,2,3,4,5,6];
export function daysText(l:Pick<RuleLine,'weekdays'|'start_time'|'end_time'>):string{
  const days=daysOf(l),order=weekOrder(),short=(d:number)=>WEEKDAYS[d].slice(0,3);
  const run=days.length>2&&order.filter(d=>days.includes(d)).every((d,i,a)=>!i||order.indexOf(d)===order.indexOf(a[i-1])+1);
  const sorted=order.filter(d=>days.includes(d));
  const text=run?`${short(sorted[0])}–${short(sorted[sorted.length-1])}`:sorted.map(short).join(', ');
  return text+(l.start_time&&l.start_time!=='00:00'||l.end_time&&l.end_time!=='24:00'?` ${l.start_time||'00:00'}–${l.end_time||'24:00'}`:'');
}
/** What a line's own settings limit it to, in words: a restriction's weekdays and hours and its window around
 *  leave, read from its effective settings (the entry's defaults › the rule's › the line's, as the line editor reads
 *  them). A setting with no words yet reads "Conditions apply", never nothing. '' when none limit it. */
export function conditionWords(e:RuleCatalogueEntry,l:RuleLine,rule:Settings={}):string{
  const kinds=[...new Set(Object.keys(l.numbers||{}).flatMap(k=>(e.numbers[k]||[]).map(s=>s.kind)))],out:string[]=[];
  for(const kind of kinds.filter(k=>k==='restriction')){
    const s={...e.default_settings?.[kind],...rule[kind],...l.settings?.[kind]} as Record<string,unknown>,days=Array.isArray(s.weekdays)?s.weekdays as number[]:[];
    const hours=!!(s.start_time&&s.start_time!=='00:00'||s.end_time&&s.end_time!=='24:00');
    if(days.length&&days.length<7||hours)out.push(daysText({weekdays:days.length?days:null,start_time:s.start_time as string|undefined,end_time:s.end_time as string|undefined} as RuleLine));
    if(s.leave_buffer_days)out.push(`within ${plural(Number(s.leave_buffer_days),'day')} of leave`);
    // shortcut: only a restriction's own conditions are worded; any other of its settings set says "Conditions apply".
    if(Object.keys(s).some(f=>!['weekdays','start_time','end_time','leave_buffer_days'].includes(f)&&s[f]!=null&&s[f]!==''&&!(Array.isArray(s[f])&&!(s[f] as unknown[]).length)))out.push('Conditions apply');
  }
  return out.join(' · ');
}
/** When a line holds, in words: its own days (unless every day) and what its settings limit it to; else its days. */
export const whenWords=(e:RuleCatalogueEntry,l:RuleLine,rule:Settings={})=>{const d=daysText(l),c=conditionWords(e,l,rule),all=daysText({} as RuleLine);
  return [d!==all?d:'',c].filter(Boolean).join(' · ')||d;};
const dayBefore=(iso:string)=>new Date(Date.parse(iso+'T12:00:00Z')-86400000).toISOString().slice(0,10);
const addDays=(iso:string,n:number)=>new Date(Date.parse(iso+'T12:00:00Z')+n*86400000).toISOString().slice(0,10);
/** When a line holds: "Always", "1 Sep → 30 Nov" (the end shown is the last day included), "From 1 Sep", "Until 30 Nov". */
function validParts(l:Pick<RuleLine,'starts_on'|'ends_on'>):string[]{
  const a=l.starts_on&&formatDate(l.starts_on),b=l.ends_on&&formatDate(dayBefore(l.ends_on));
  return a&&b?[a,`→ ${b}`]:a?[`From ${a}`]:b?[`Until ${b}`]:['Always'];
}
export const validText=(l:Pick<RuleLine,'starts_on'|'ends_on'>):string=>validParts(l).join(' ');
/** Wrap between complete dates when a column is wrapped, never between a day, month and year. */
export const validNode=(l:RuleLine):HTMLElement=>dateUnits(validParts(l),'rule-valid');
/** The rule page's Valid cell: as validNode, and "From" / "Until" may also wrap before their date (a narrow column). */
const validCell=(l:RuleLine):HTMLElement=>dateUnits(validParts(l).flatMap(p=>/^(From|Until) /.test(p)?[p.slice(0,p.indexOf(' ')),p.slice(p.indexOf(' ')+1)]:[p]),'rule-valid');
/** The server's validity of each saved line, with the dates it was worked out from. A line whose dates were changed in
 *  a draft (not saved yet) is read from its own dates; every saved line is the server's word. */
const savedValidity=new Map<string,{v:RuleLineValidity;day:string;starts_on?:string|null;ends_on?:string|null}>();
onSessionReset(()=>savedValidity.clear());
export function learnValidity(e:Entry,day:string){for(const l of linesOf(e)){const v=e.line_validity?.[l.id];if(v)savedValidity.set(l.id,{v,day,starts_on:l.starts_on,ends_on:l.ends_on});}}
/** Whether a line is in force on `day` (the catalogue's day): active, ended or future. */
export function effectiveOf(l:RuleLine,day:string):RuleLineValidity['effective']{
  const s=savedValidity.get(l.id);if(s&&s.day===day&&s.starts_on===l.starts_on&&s.ends_on===l.ends_on)return s.v.effective;
  return l.ends_on&&l.ends_on<=day?'ended':l.starts_on&&l.starts_on>day?'future':'active';
}
/** The Valid filter's values a line has today. */
export function validFacets(l:RuleLine,today:string,at=effectiveOf(l,today)):string[]{
  const out=[l.ends_on?'Has an end date':'No end date'];
  if(at==='ended')out.push('Ended');
  else{out.push(CURRENT);if(l.ends_on&&l.ends_on<=addDays(today,LINE_ENDING_DAYS+1))out.push(ENDS_SOON);}
  if(at==='future')out.push('Starts later');
  return out;
}

// ---- Proposing (every change is a proposal another person approves) ----
export const newLine=(e:RuleCatalogueEntry):RuleLine=>({id:crypto.randomUUID(),...JSON.parse(JSON.stringify(e.default_line||{})),numbers:{...(e.default_line?.numbers||{})},
  // A Workload line counts something over a period: the entry's first measure, per calendar week (its default settings).
  ...(e.measures?.length?{measure:e.measures[0] as RuleLine['measure'],period:{kind:'calendar_week'}}:{})});
/** A new line whose own metric just became one the catalogue has a suggestion for (an overtime fair share: quarter,
 *  shared by overtime offered): the suggestion fills the settings this edit left as they were. Saved lines and the
 *  rule's own settings are never rewritten. */
export function suggestedSettings(e:RuleCatalogueEntry,before:Settings,value:Settings):Settings{
  const out=clone(value);
  for(const [kind,v] of Object.entries(value)){
    const s=v?.metric!==before[kind]?.metric?e.suggested_lines?.[String(v?.metric)]?.default_settings?.[kind]:undefined;if(!s)continue;
    for(const [f,x] of Object.entries(s))if(JSON.stringify(v[f])===JSON.stringify(before[kind]?.[f]))out[kind][f]=x;}
  return out;
}
/** The fields of a workload line's counting settings that its measure and period stand for. */
const COUNTED=['metric','window','window_days','window_alignment','window_anchor_on','prorate_partial_windows','repeat','starts_on','ends_on'],COUNT_KINDS=['block_quota','max_duties','max_hours'];
/** Lines as the server takes them: the measure, period and mode it derives from a Workload line's settings go back only
 *  when changed (they then replace what those settings counted), so the stored settings stay the one representation. */
export function wire(lines:RuleLine[],saved:RuleLine[]):RuleLine[]{
  const was=new Map(saved.map(l=>[l.id,l]));
  return lines.map((l:RuleLine&LineNumbers)=>{const {mandatory_reasons:_reasons,mode:_mode,measure,period,...rest}=l,old=was.get(l.id);
    if(!measure||!period||'apply' in (l.numbers||{})||old&&JSON.stringify([old.measure,old.period,Object.keys(old.numbers).sort()])===JSON.stringify([measure,period,Object.keys(l.numbers).sort()]))return rest as RuleLine;
    const settings=Object.fromEntries(Object.entries(rest.settings||{}).map(([k,v]):[string,Record<string,unknown>]=>[k,COUNT_KINDS.includes(k)?Object.fromEntries(Object.entries(v).filter(([f])=>!COUNTED.includes(f))):v])
      .filter(([k,v])=>Object.keys(v).length||k==='block_quota'));
    return {...rest,settings,measure,period};});
}
/** Propose a new version of a catalogue rule. A more specific line winning over a broader one is the rule working as
 *  meant: nothing to acknowledge (the server keeps acknowledged_overlaps only as an ignored legacy field). */
/** A line proposed from the line panel (relax or promote): the rule's other lines with it, and the rule's own on or off,
 *  settings and balance as the draft holds them. Adding a line never switches a rule in force on or off; the first line
 *  of a rule never in force (no version yet) turns it on. */
export const lineProposal=(draft:Draft,lines:RuleLine[]):Body=>({on:draft.on||draft.baseId===null&&lines.length>0,lines,settings:draft.settings,balance:draft.balance,seen:draft.seen});
export async function propose(e:Entry,body:Body,reason:string):Promise<Row>{
  const payload={catalogue_id:e.id,catalogue_version:e.catalogue_version,expected_policy_id:e.rule?.id??null,expected_revision:e.rule?.revision??null,on:body.on,settings:body.settings??settingsOf(e),balance_between_people:body.balance??balanceOf(e),
    expected_line_ids:body.seen??linesOf(e).map(l=>l.id),lines:wire(body.lines,linesOf(e)),acknowledged_overlaps:[],reason};
  return (await command('rule.propose',payload)).record;
}
// ---- The page ----
/** Catalogue v2 (one rule per idea) once the department holds no rule in the old representation; the server
 *  resolves it (the same rule as its DEPARTMENT_CONVERSION_REQUIRED admission). */
export const catalogueVersion=(ctx:Context)=>ctx.me.catalogue_version;
/** The v2 rule a v1 rule id was folded into (the contracts' replacement_id), when it is not a v2 rule itself. */
export const folded=(id:string|undefined)=>{if(!id||RULE_CATALOGUE_V2.some(e=>e.id===id))return undefined;
  const to=RULE_CATALOGUE.find(e=>e.id===id)?.replacement_id;return to&&RULE_CATALOGUE_V2.some(e=>e.id===to)?to:undefined;};
/** Who edits rules here (drafts, the pickers' active choices). */
const editsRules=(ctx:Context)=>ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.ADMIN);
/** Catalogue v2 reads every line, ended ones too (a proposal is the whole rule); the list hides what is not current. */
const catalogueUrl=(version:number|undefined)=>version===2?'/api/rules/catalogue?version=2&include_hidden=true':'/api/rules/catalogue';
/** FE-PERF (scale): the Rules list's own reads, started together as soon as the account is known (app.ts boot), beside
 *  the lists. None of them needs the lists or another read's answer; before, they waited for the lists, then for each
 *  other (4–5 serial waves on a reload). App reads (`background`): the first page draw must not abort them. Each is
 *  read where the page reads it (its memo, kept read or saved-view cache; the catalogue through Options.ahead), so every
 *  request is still sent once. The questions are asked for the department's today, the day the catalogue answers with;
 *  if it answers another day (midnight), the page asks for that day as before. Same conditions as the page's own reads. */
export function readRulesAhead(ctx:Context):Promise<Catalogue>{
  const quiet=<T,>(p:Promise<T>)=>{p.catch(()=>undefined);return p;};  // each is awaited (and its failure handled) where the page uses it
  void quiet(capabilities(background));
  if(editsRules(ctx))void quiet(activeChoices(background));
  void quiet(departmentRules(departmentToday(),background));
  void quiet(ruleTreeViews());
  if(readsSimplify(ctx))void quiet(conversions(true,background));
  return quiet(background<Catalogue>(catalogueUrl(catalogueVersion(ctx))));
}
/** Who may read a rule's combined detail (GET /api/rules/catalogue/{id}/detail); others read the whole catalogue. */
export async function ruleCataloguePage(ctx:Context,target:HTMLElement,o:Options){
  const account=`${ctx.me.identity_id}.${ctx.me.department.id}`;
  if(draftSession!==sessionKey()||draftAccount!==account){drafts.clear();draftEntries.clear();draftSession=sessionKey();draftAccount=account;}
  const version=catalogueVersion(ctx);
  // An old link to a rule catalogue v2 folded into another (keep_off → workload_limit) opens that rule; Back is not trapped.
  const moved=version===2?folded(o.parts[0]):undefined;
  if(moved){o={...o,parts:[moved,...o.parts.slice(1)]};history.replaceState(history.state,'',routeHash('rules',o.parts,new URLSearchParams(location.hash.split('?')[1]||'')));}
  // What an advisory strength weighs (the department's, over the defaults), for the strength badges' tooltips.
  void capabilities().then(r=>setAdvisoryWeights({...r.generation_defaults?.scoring?.strengths,...ctx.me.department.generation_defaults?.scoring?.strengths})).catch(()=>undefined);
  const canEdit=editsRules(ctx);
  if(!canEdit)drafts.clear();else loadActive();
  const redraw=async()=>{await ctx.refresh();};
  // A rule or line opened by link: one read with the rule, its check and the line's reach (today), not the whole catalogue.
  if(o.parts[0]&&version===2&&ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR)){
    // This week of the rota that holds today, as the rule page counts it (one light read of the rotas first).
    const today=departmentToday(),week=readsReach(ctx)?await thisWeek(today):null;
    const q=`&day=${today}`+(o.parts[1]?`&line_id=${encodeURIComponent(o.parts[1])}`:'')+(week?`&workspace_id=${week.id}&starts_on=${week.from}&ends_on=${week.to}`:'');
    const detail=(query:string)=>ask<{entry:Entry;check:Check|null;reach:Row|null;day:string}>(`/api/rules/catalogue/${encodeURIComponent(o.parts[0])}/detail?version=2${query}`);
    const plain=`&day=${today}`+(o.parts[1]?`&line_id=${encodeURIComponent(o.parts[1])}`:'');
    // A rule switched on after this week's rota was published is not in that rota's rules (404): read it for today
    // (as rule_line_reach.ts does) before the whole catalogue, which also says why when it really is not there.
    let forWeek=!!week;
    const d=await detail(q).catch(err=>{if(sessionChanged(err))throw err;forWeek=false;return week?detail(plain).catch(again=>{if(sessionChanged(again))throw again;return null;}):null;});
    if(d){settle(d.entry);learn(linesOf(d.entry),d.entry.line_specificity);learnValidity(d.entry,d.day);if(d.entry.proposed)learn(d.entry.proposed.definition.rule.lines||[],d.entry.proposed.line_specificity);
      rulePage(ctx,target,{families:RULE_FAMILIES.map(([id,name])=>({id,name})),entries:[d.entry],day:d.day,attention:[],catalogue_version:2},d.entry,o,canEdit,redraw,{check:d.check,reach:d.reach,period:forWeek&&week?week.name:'today'});return;}
  }
  const learnLive=(data:Catalogue)=>{for(const e of data.entries){settle(e);learn(linesOf(e),e.line_specificity);learnValidity(e,data.day);if(e.proposed)learn(e.proposed.definition.rule.lines||[],e.proposed.line_specificity);}};
  let data=await (o.ahead||ask<Catalogue>(catalogueUrl(version)));
  learnLive(data);
  const entry=o.parts[0]&&data.entries.find(e=>e.id===o.parts[0]);
  if(entry){rulePage(ctx,target,data,entry,o,canEdit,redraw);return;}
  if(o.parts[0]){target.append(note('This rule is not available. Choose one from Rules.','warning'));return;}
  // The Rules list by question A–F reads the server's department projection. If that read fails the page still lists
  // and edits the rules, grouped by family, under the shared failure note with Retry.
  // maintenance: Retry asks the server again itself, once (readAfresh: never a held read), and redraws the list in place. It
  // never goes through the app's refresh, which joins a refresh already on its way and so could send nothing at all.
  let drawSerial=0;
  const source=o.source||{workspace:''};
  const sourceLabel=(option:RulesSourceOption)=>option.workspace_id
    ? `${option.label}, as captured${option.captured_at?' '+formatDate(option.captured_at.slice(0,10)):''}` : 'Live rules';
  const chooseSource=(id:string)=>{if(id!==source.workspace){source.workspace=id;retry(!id);}};
  const sourceControl=()=>{
    const sources=data.source_options||[];
    if(sources.length<2)return source.workspace?button('Live rules',()=>chooseSource(''),'secondary'):'';
    const selected=sources.find(option=>(option.workspace_id||'')===source.workspace)||sources[0];
    const pick:HTMLButtonElement=opens(button(sourceLabel(selected),()=>openPicker(pick,pick.parentElement!,{
      label:'Rules',multiple:false,selected:new Set([source.workspace]),
      choices:sources.map(option=>({value:option.workspace_id||'',label:sourceLabel(option),
        hint:option.changed?`${option.changed} changed since`:''})),
      onPick:chooseSource,
    }),'secondary'),'dialog');
    pick.append(icon('chevronDown'));
    return pick;
  };
  const retry=(reloadLive=false)=>{
    readAfresh();
    void draw(reloadLive).catch(err=>{if(!sessionChanged(err))reportError(err);});
  };
  const draw=async(reloadLive=false)=>{
    const serial=++drawSerial;
    const workspace=source.workspace;
    let current=data;
    if(workspace||reloadLive){
      target.replaceChildren(sectionHead('Rules',''),skeleton('lines','Loading rules…'));
      o.frame?.();
      const path=catalogueUrl(version);
      try{
        current=await (workspace?get<Catalogue>(`${path}${path.includes('?')?'&':'?'}workspace_id=${encodeURIComponent(workspace)}`):ask<Catalogue>(path));
      }catch(err){
        if(sessionChanged(err))throw err;
        if(serial===drawSerial){
          target.replaceChildren(sectionHead('Rules',''),sourceControl(),
            readFailed(workspace?'captured rules':'live rules',err,()=>retry(reloadLive)));
          o.frame?.();
        }
        return;
      }
      if(serial!==drawSerial)return;
      if(!workspace){data=current;learnLive(data);}
    }
    let failed:unknown=null;
    const byQuestion=await departmentRules(current.day,get,current.source).catch(err=>{if(sessionChanged(err))throw err;failed=err;return null;});
    if(serial!==drawSerial)return;
    const selected=data.source_options?.find(option=>option.workspace_id===workspace);
    target.replaceChildren();
    rulesDatabase(ctx,target,current,o,canEdit&&!workspace,redraw,byQuestion,sourceControl,
      selected&&sourceLabel({...selected,captured_at:current.source?.captured_at??selected.captured_at}));
    o.frame?.();
    // A refusal (403) is the role's, not a fault: the list by family is the whole answer, with nothing to retry (B128).
    if(!failed||refused(failed))return;
    // The shared failure note: the error's own words and reference, Retry, and Report this problem remain visible.
    const n=readFailed('the rules’ questions',failed,
      ()=>{if(!workspace)forgetDepartmentRules(current.day);retry();},undefined,'The list is grouped by family.');
    target.querySelector(':scope>.rt')?.before(n);
  };
  await draw();
}
/** Whose line counts, in words (contextual help, never a key entry: it is a rule of reading, not a mark). */
export const whoWins=(e:Pick<RuleCatalogueEntry,'safety'|'mixed_safety'>)=>e.safety&&!e.mixed_safety?'Every matching line applies; the strictest binds.'
  :'Among lines with the same where, the most specific who wins: a person, then a rank, programme, tag or skill, then everyone.';
/** The marks a rule view draws, with their swatch and words (one place: the Rules list, a rule's page, Needs attention). */
// A who or where pill's hue by its condition's field (fieldTone, the design rules: who/where) as a swatch: people sky, programmes,
// tag groups and skills purple, clinics teal. A rank pill is its own rank tint and names itself, as a duty type's swatch does.
const pillHue=(name:string)=>()=>el('span',{class:`lg-swatch duty-${name}`,'aria-hidden':'true'});
const levelSwatch=(l:string|null)=>()=>{const b=levelBadge(l);b.classList.add('lg-mark');return b;};
const RULE_MARKS={numbers:[()=>numbersEl([{text:'2 – 5 → 4'}]),'min – max → target'],
  mandatory:[levelSwatch('mandatory'),'must hold'],advisory:[levelSwatch('advisory'),'preferred, by weight'],inherit:[levelSwatch(null),'strength from a broader line'],
  inherited:[()=>el('b',{},'↰'),'from a broader line'],person:[pillHue('sky'),'Person'],group:[pillHue('violet'),'Group'],rank:[()=>el('span',{class:'lg-swatch lg-ranks','aria-hidden':'true'}),'Rank, each in its own tint'],place:[pillHue('teal'),'Clinic'],
  each:[eachMark,'counted for each'],tag:[()=>el('span',{class:'tag-chip lg-mark duty-teal'},'#tag'),'a tag, in its own colour'],not:[()=>el('span',{class:'rp all except lg-mark'},'not ...'),'everyone except these'],
  proposed:[()=>{const b=badge('Proposed','warn','lg-mark');b.prepend(icon('clock'));return b;},'a line waiting for approval'],starts_later:[()=>el('span',{class:'muted'},'Starts later'),'not in force yet'],ended:[()=>el('span',{class:'muted'},'Ended'),'no longer in force'],
  no_one:[()=>badge('No one','warn','lg-mark'),'reaches no active person today'],gives_way:[()=>badge('Gives way','warn','lg-mark'),'a more specific line wins for some people'],overlaps:[()=>badge('Overlaps','warn','lg-mark'),'another line covers the same people'],
  overlap_seen:[()=>badge('Overlap seen','ok','lg-mark'),'an overlap marked as intended'],refused:[()=>badge('Refused','err','lg-mark'),'cannot be proposed as it is'],same:[()=>badge('Same line','err','lg-mark'),'repeats another line'],both:[()=>badge('Both apply','warn','lg-mark'),'two lines apply to the same people'],inherits:[()=>badge('Inherits a conflict','err','lg-mark'),'its broader line conflicts'],weaker:[()=>badge('Weaker','warn','lg-mark'),'weaker than another line'],
  safety:[()=>el('span',{class:'rk-lockmark lg-mark','aria-hidden':'true'},icon('lock')),TERMS.safetyRuleProtection],waiting:[()=>badge(WAITING,'info','lg-mark',{icon:'clock'}),'read only until approved or declined'],attention:[()=>badge('Needs attention',statusTone('attention'),'lg-mark',{icon:'alert'}),'open the issues to fix'],must_fix:[()=>badge('Must fix','err','lg-mark'),'blocks the rule until fixed'],consolidate:[()=>badge('Consolidate','warn','lg-mark'),'could be merged with other lines'],off:[()=>el('span',{class:'lg-off lg-mark','aria-hidden':'true'}),'Off: not in force'],
  instead:[()=>badge('replaces','neutral'),'the line above, for its people'],also:[()=>el('span',{class:'rt-rel rt-also'},'also applies'),'with the line above'],applies:[()=>badge('applies','ok'),'what holds here'],replaced:[()=>el('span',{class:'rt-rel rt-also'},'replaced'),'a more specific line applies']} satisfies Record<string,[()=>HTMLElement,string]>;
export type RuleMarkKey=keyof typeof RULE_MARKS;
/** The pill kinds whose hue is a mark ("not …" pills are muted and say it in words; "Everyone" is grey words). */
const HUED=new Set(['person','group','place']);
/** A key for a view whose table draws, pages, groups, filters and opens rows itself (the shared `legend`), shown after that view: marks are stamped as they are drawn (`mark`, by their entry in
 *  `marks`: swatch, words); the key follows what the view draws now (dom.ts legend `follow`). */
export function tableKey(marks:Record<string,[()=>HTMLElement,string]>){
  const key=legend('Key'),swatches=new Map<string,HTMLElement>();
  const mark=<T extends Element>(n:T,k:string):T=>{const [sw,words]=marks[k];if(!swatches.has(k))swatches.set(k,sw());return key.mark(n,k,swatches.get(k)!,words);};
  // A pill behind "+N" counts only while its column is wrapped and shows it.
  const shown=(n:Element)=>!n.closest('[hidden]')&&(!n.closest('.pill-over')||!!n.closest('.dt-wrap'));
  return {root:key.root,mark,
    /** Keep the key to what `views` draw now (dom.ts legend `follow`); returns the key (placed outside them). */
    watch(...views:HTMLElement[]){return key.follow(views,shown);}};
}
/** A rule view's key (the Rules list, a rule's page, Needs attention): `pills`, `numbersNode` and `numberCell` take it. */
export function ruleViewKey(){
  const k=tableKey(RULE_MARKS);
  return {...k,
    /** A numbers element (numbersEl): its strengths, inherited numbers and, where it reads min – max → target, its notation. */
    numbers(n:HTMLElement,notation=true){if(notation&&/[–≥≤→]/.test(n.textContent||''))k.mark(n,'numbers');
      for(const b of n.querySelectorAll<HTMLElement>('.rc-level'))k.mark(b,b.dataset.level==='mandatory'?'mandatory':b.dataset.level==='advisory'?'advisory':'inherit');
      for(const i of n.querySelectorAll('.rc-inh'))k.mark(i,'inherited');return n;},
    stamp<T extends Child|Child[]>(nodes:T):T{for(const n of [nodes].flat())if(n instanceof Element)for(const m of [n,...n.querySelectorAll('[data-rule-mark]')])
      if(m instanceof HTMLElement)for(const x of (m.dataset.ruleMark||'').split(' '))if(x in RULE_MARKS)k.mark(m,x);return nodes;}};
}
export const ruleMark=<T extends Element>(node:T,key:RuleMarkKey):T=>{node.setAttribute('data-rule-mark',[node.getAttribute('data-rule-mark'),key].filter(Boolean).join(' '));return node;};
export type RuleViewKey=ReturnType<typeof ruleViewKey>;
/** A rule's own page (#rules/<id>[/<line id>]): a lines database, grouped by who kind (or rank, person, clinic…). Days,
 *  numbers, dates and notes edit in place; a line opens in the line editor docked beside the table (↑ ↓ through it);
 *  selected lines change together from the selection bar. Every edit goes into the rule's draft ("N changes not yet
 *  proposed", kept while you move between this page and the Rules list) until Propose sends one version. */
function rulePage(ctx:Context,target:HTMLElement,data:Catalogue,e:Entry,o:Options,canEdit:boolean,refresh:()=>Promise<void>,primed?:{check:Check|null;reach:Row|null;period:string}){
  const family=data.families.find(f=>f.id===e.family)?.name||e.family,names=namer(ctx),reach=e.people;
  // Opening a rule changes nothing: a rule not in force stays Off with no lines until you turn it on or add a line.
  // A proposal of this rule on its way (bulk Change…): read only until it is in, so no edit is lost to it.
  const sending=ruleProposing(ruleKey(e)),editable=canEdit&&!e.proposed&&!sending;
  if(sending){target.append(note(`A change to ${e.name} is being proposed. You can edit it again once that is done.`,'info'));
    // Sent or failed, the page is drawn again then (editable again, or showing the new proposal).
    redrawWhenSettled(target,refresh);}
  const saved=linesOf(e),draft=draftOf(e);
  const rows:LineRow[]=[];
  // The draft's changes are worked out once per change (changed() moves the version), not per number cell: every cell
  // asked, each comparing every line (a 331-line rule took 3 s to open and 3 s per Keep change).
  let draftVersion=0,known=-1,count=0,lineCount=draft.lines.length;
  const changes=()=>{if(!editable)return 0;if(known!==draftVersion){count=changesIn(e,draft);known=draftVersion;}return count;};
  // A saved line's text, once: whether a line still reads as the saved one is asked per cell.
  const savedJSON=new Map(saved.map(l=>[l.id,JSON.stringify(l)]));
  o.setLeave?.(()=>{const n=changes();return n>0&&`${plural(n,'change')} not yet proposed. Propose ${n===1?'it':'them'}, or leave again (the Rules page keeps ${n===1?'it':'them'} until you discard ${n===1?'it':'them'}).`;});
  const proposeBtn=proposeButton(()=>proposeDraft());
  const blocked=!canEdit||editable?'':sending?`A change to ${e.name} is being proposed`:`${e.name} already has a change waiting for approval`;
  const openIt=()=>blocked&&!sending?[' · ',button('Open it',()=>decideProposal(),'',{class:'link-button'})]:[];
  // Same-button confirm, as on the Rules list: the draft goes back to the version in force (shown while there are changes).
  const discardBtn=armed(button('Discard changes',()=>{discardDraft(e.id);o.setLeave?.(null);void refresh();},'secondary'),'Discard all?');discardBtn.hidden=true;
  const status=el('span',{class:'footer-total'}),issues=el('div',{class:'rc-checkbar',role:'status'});
  let onlyAffected=false;
  // Checked as you edit (300 ms after the last change; an older answer never draws over a newer one: the request is
  // shared with other callers (checkLines) and not aborted, so the version guard drops it) and again by the server
  // when you propose. Overlaps acknowledged here need no second confirmation.
  let found:Check|null=null,timer=0,inFlight:AbortController|null=null,checkVersion=0,checkFailed=false;
  // An answer is for the lines it was asked about: after any change none shows until the new one comes.
  // The deep link's read already checked the saved lines: used once, while the draft is still exactly them.
  let primedCheck=primed?.check??null;
  const recheck=()=>{window.clearTimeout(timer);inFlight?.abort();const version=++checkVersion;checkFailed=false;if(!editable)return;
    if(primedCheck&&!changes()){found=primedCheck;primedCheck=null;learn(draft.lines,found.line_specificity);return;}
    primedCheck=null;
    timer=window.setTimeout(()=>{const controller=new AbortController();inFlight=controller;void checkLines(e,clone(draft),controller.signal).then(r=>{
      if(version!==checkVersion||controller.signal.aborted)return;found=r;learn(draft.lines,r.line_specificity);table.refresh();paint();summary();panel.sync();
    }).catch(err=>{if(version!==checkVersion||controller.signal.aborted||sessionChanged(err))return;checkFailed=true;summary();panel.sync();});},300);};
  const words=checkWords(e,()=>draft.lines,names);
  /** An acknowledgement holds for the overlap as it was seen: the same line giving way to the same lines. */
  /** Every issue of the check, by line: refused, the same line twice, narrower but weaker, both apply. */
  const issuesOf=(id:string)=>{if(!found)return [];const n=draft.lines.findIndex(l=>l.id===id);
    return [...found.issues.filter(x=>lineOfIssue(x)===n).map(x=>({kind:'refused',title:'This line is refused',message:x.message,other:''})),
      ...found.duplicates.filter(d=>d.line_ids.includes(id)).map(d=>({kind:'same',title:'The same line twice',message:words.same(d),other:(d.line_ids as string[]).find(x=>x!==id)||''})),
      ...found.warnings.filter(w=>w.line_id===id||w.kind==='both'&&w.other_id===id).map(w=>({kind:String(w.kind),
        title:WARNING_TITLES[w.kind]||'Narrower but weaker',message:words.warn(w),other:String(w.line_id===id?w.other_id:w.line_id)}))];};
  /** One compact bar above the table: how many lines have each issue, and a way to show only those lines. */
  function summary(){
    if(!found){issues.replaceChildren(...(checkFailed?[el('span',{},'⚠ The lines could not be checked now; they are checked again when you propose.')]:[]));return;}
    const ids=(pick:(id:string)=>boolean)=>draft.lines.filter(l=>pick(l.id)).length;
    const same=ids(id=>found!.duplicates.some(d=>d.line_ids.includes(id)));
    const refused=ids(id=>issuesOf(id).some(x=>x.kind==='refused')),general=found.issues.filter(x=>lineOfIssue(x)<0);
    const count=(kind:string)=>found!.warnings.filter(w=>w.kind===kind).length,weaker=count('weaker'),both=count('both'),inherits=count('inherits'),short=count('pool_shortfall');
    // SW3-B-22: why a line is refused is said where it is counted (it was only behind the ⚠'s click).
    const why=[...new Set(draft.lines.flatMap(l=>issuesOf(l.id).filter(x=>x.kind==='refused').map(x=>x.message)))];
    const parts=[refused?plural(refused,'line')+' refused'+(why.length===1?`: ${why[0]}`:''):'',same?plural(same,'duplicate line'):'',inherits?`${inherits} inherit${inherits===1?'s':''} a conflict`:'',
      weaker?`${weaker} weaker`:'',both?`${both} both apply`:'',short?`${short} cannot be met with the eligible pool`:'',general.length?plural(general.length,'other problem'):''].filter(Boolean);
    if(!parts.length){issues.replaceChildren();if(onlyAffected){onlyAffected=false;table.refresh(true);}return;}
    const toggle=button(onlyAffected?'Show all lines':'Show these lines',()=>{onlyAffected=!onlyAffected;table.refresh(true);summary();},'',{class:'link-button'});
    const more:HTMLButtonElement|null=general.length?button('Details',()=>popover(more!,(panel)=>panel.append(el('ul',{class:'rc-diff rc-twin'},...general.map(x=>el('li',{},x.message)))),{label:'Other problems'}),'',{class:'link-button'}):null;
    issues.replaceChildren(el('span',{},'⚠ '+parts.join(' · ')),toggle,more||'');
  }
  const paint=()=>{const n=changes();
    headFacts.textContent=[family,isOn(e)?`reaches ${plural(reach,'person','people')}`:''].filter(Boolean).join(' · ');
    const refusedLines=found?draft.lines.filter(l=>issuesOf(l.id).some(x=>x.kind==='refused')).length:0;
    relabel(proposeBtn,n?`Propose ${plural(n,'change')}`:'Propose');proposeTip(proposeBtn,n);discardBtn.hidden=!n;
    const refusedWhy=refusedLines?`${plural(refusedLines,'line')} refused: change or remove ${refusedLines===1?'it':'them'} first.`:'';
    if(blocked||refusedWhy)disabledWhy(proposeBtn,blocked||refusedWhy,openIt());else disabledWhy(proposeBtn,n?'':TERMS.nothingToPropose);
    status.textContent=[n?`${plural(n,'change')} not yet proposed`:''].filter(Boolean).join(' · ');};
  /** After any change: the draft is kept (it counts on the Rules page too), the table, the check and the panel follow. */
  const changed=(keep=true)=>{draftVersion++;
    // The first line of a rule never in force turns it on (switch it off to keep it off); its last line removed turns
    // it back off, so an empty draft proposes nothing. The Rules list's New line opens its line here too.
    const had=lineCount>0,has=draft.lines.length>0;lineCount=draft.lines.length;
    if(!e.rule&&had!==has&&draft.on!==has){draft.on=has;showOn();}
    if(keep&&editable)keepDraft(e,draft);found=null;recheck();rows.splice(0,rows.length,...draft.lines.map((line,n)=>({entry:e,line,n})),...proposedRows);table.refresh();paint();summary();panel.sync();};
  // B5: lines a waiting proposal adds sit in the table as "Proposed" (clock: waiting for someone); they open its decision.
  const proposedAll=(e.proposed?.definition?.rule?.lines||[]) as RuleLine[],proposedIds=new Set(proposedAll.filter(l=>!saved.some(x=>x.id===l.id)).map(l=>l.id));
  const proposedRows:LineRow[]=proposedAll.flatMap((line,n)=>proposedIds.has(line.id)?[{entry:e,line,n}]:[]);
  /** The line a relation names ("line 4"), tinted in the table while the panel shows it. */
  let pointed:string|null=null;
  const put=(targets:LineRow[],change:(l:RuleLine)=>void)=>{const ids=new Set(targets.map(r=>r.line.id));for(const l of draft.lines)if(ids.has(l.id))change(l);changed();};
  const edit=(editor:(r:LineRow,finish:(save:boolean)=>void)=>{node:HTMLElement;read:()=>unknown},apply:(l:RuleLine,v:any)=>void):EditSpec<LineRow>|undefined=>
    editable?{kind:'custom',quiet:true,value:()=>Symbol(),editor,save:async(targets,v)=>put(targets,l=>apply(l,v))}:undefined;
  const g=groupings(ctx,names,data.day);
  // Blank inherits: as the check last said (else as the rule in force reads), each line's inherited numbers.
  /** People a line reaches today, the same number its panel shows: the open panel's, else the latest check's (a new or
   *  changed line), else the rule in force's. */
  let panelReach:(id:string)=>number|null=()=>null;const shownReach=new Map<string,number|null>();
  const reachOf=(l:RuleLine):number|null=>{const open=panelReach(l.id);if(open!=null)return open;
    const rows=found?.reach_details?.lines.filter(x=>x.line_id===l.id);
    if(rows?.length)return new Set(rows.flatMap(x=>(x.people||[]).map((p:Row)=>p.person_id))).size;
    const was=savedJSON.get(l.id);return was!==undefined&&was===JSON.stringify(l)?e.reach[l.id]??null:null;};
  const inhOf=(id:string)=>(found?.inherited??(!changes()?e.inherited:undefined)??{})[id],from=sourceWords(()=>draft.lines,names);
  const marks=ruleViewKey();
  // each where's lines as an anchor and its exceptions (exceptionTree, as the Rules list): worked out once per draft.
  let shapeAt=-1,shapeFound:unknown,shapes=new Map<string,{under:boolean;always:boolean;instead:string;at:number}>();
  // Again for each draft and each check answer: what a line replaces comes with the check (its line_replaces).
  const shapeOf=(l:RuleLine)=>{if(shapeAt!==draftVersion||shapeFound!==found){shapes=new Map();shapeAt=draftVersion;shapeFound=found;const by=new Map<string,RuleLine[]>();
      for(const x of draft.lines){const k=x.where?.conditions?.length?JSON.stringify(x.where):'';by.set(k,[...(by.get(k)||[]),x]);}
      for(const ls of by.values())exceptionTree(e,ls,x=>x).forEach((s,at)=>shapes.set(s.item.id,{under:s.under,always:s.always,instead:replacedWords(e,s.item,draft.lines,found?.line_replaces??(!changes()?e.line_replaces:undefined),draft.settings),at}));}
    return shapes.get(l.id);};
  const goalCell=(r:LineRow)=>{const m=goalOf(e,r.line,draft.settings).metric;return METRIC_WORDS[m as keyof typeof METRIC_WORDS]||String(m);};
  const columns:Column<LineRow>[]=[
    // Who and where open the line in the editor beside the table (a click on the row); "+N" lists them all.
    {key:'who',label:'Who',search:'related',text:r=>treeText(r.line.who,names,'Everyone'),cell:r=>{const s=proposedIds.has(r.line.id)?undefined:shapeOf(r.line);
      const p=s?.under?el('span',{class:'rt-exc-who'},el('span',{class:'sr-only'},'Exception: '),pills(ctx,r.line.who,names,'Everyone',o.openTag,'Who',2,marks),s.instead?el('small',{class:'muted rt-instead'},`(instead of ${s.instead})`):'')
        :s?.always?el('span',{class:'rt-exc-who'},marks.stamp(safetyLock()),pills(ctx,r.line.who,names,'Everyone',o.openTag,'Who',2,marks),el('small',{class:'muted rt-always-word'},'Always applies')):pills(ctx,r.line.who,names,'Everyone',o.openTag,'Who',2,marks);
      if(!proposedIds.has(r.line.id))return p;const b=ruleMark(badge('Proposed','warn'),'proposed');b.prepend(icon('clock'));return marks.stamp(el('span',{class:'rc-proposed'},p,b));}},
    // The line's stored number, as the Rules list's rail, the line panel and the check's messages say it (View settings shows it).
    {key:'no',label:'No.',num:true,class:'rc-col-narrow',sort:r=>r.n,text:r=>String(r.n+1),cell:r=>String(r.n+1)},
    {key:'where',label:'Where',search:'related',text:r=>treeText(r.line.where,names,'All clinics'),cell:r=>pills(ctx,r.line.where,names,'All clinics',o.openTag,'Where',1,marks)},  // F-R2: one place +N, so Valid and Reach fit at 1440
    // One rule per idea: a Workload line's mode, measure and period, a Rest-between-duties line's mode (edited in its panel).
    // a measured rule's mode is its number phrase and Measure already ("No duties", "Duties per …"): no Mode column.
    ...(e.modes&&!e.measures?[{key:'mode',label:'Mode',text:(r:LineRow)=>modeText(e,r.line,draft.settings),cell:(r:LineRow)=>modeText(e,r.line,draft.settings)}]:[]),
    // what a line counts and over which period read as one phrase ("Duties per calendar week"), one column.
    ...(e.measures?[{key:'measure',label:'Measure',text:(r:LineRow)=>[measureText(r.line),periodText(r.line)].filter(Boolean).join(' '),cell:(r:LineRow)=>[measureText(r.line),periodText(r.line)].filter(Boolean).join(' ')||'—'}]:[]),
    // A Fair share line counts the rule's measure or its own (Supervision, Overtime hours…).
    ...(e.default_settings?.balance_goal?[{key:'measure',label:'Measure',text:goalCell,cell:goalCell}]:[]),
    {key:'days',label:'Days',class:'rc-col-narrow',text:r=>whenWords(e,r.line,draft.settings),cell:r=>whenWords(e,r.line,draft.settings),edit:edit(r=>daysEditor(r.line),(l,v)=>{l.weekdays=v;})},
    ...Object.keys(e.numbers).map(k=>({key:'n_'+k,label:numberLabel(e,k),num:k!=='apply',text:(r:LineRow)=>shownNumber(r.line,k,inhOf(r.line.id)),cell:(r:LineRow)=>{if(k==='apply'&&'apply' in (r.line.numbers||{})&&appliesWord(e)==='no duties')
          // one lock per line: beside its who with "Always applies" when it has that, else here.
          return el('span',{class:'rule-num'},...(!proposedIds.has(r.line.id)&&shapeOf(r.line)?.always?[]:[marks.stamp(safetyLock()),' ']),'No duties');
        // each cell keeps its strength, as its accessible name says it (only the Rules list says one strength once per rule).
        return numberCell(r.line,k,inhOf(r.line.id),from,marks);},
      edit:edit((r,finish)=>numberEditor(e,k,r.line,finish),(l,v)=>setNumber(l,k,v))})),
    {key:'valid',label:'Valid',class:'rc-col-narrow',text:r=>validText(r.line),sort:r=>r.line.ends_on||'9999',cell:r=>validCell(r.line),
      edit:edit(r=>validEditor(r.line),(l,v)=>{l.starts_on=v.starts_on||null;l.ends_on=v.ends_on||null;})},
    {key:'reach',label:'Reach',num:true,sort:r=>reachOf(r.line)??0,text:r=>String(reachOf(r.line)??''),cell:r=>marks.stamp(reachNode(reachOf(r.line),r.line))},
    // H-14: a click on the note opens the line's panel (its whole note, edited there), never a free-text edit in place.
    {key:'note',label:'Note',text:r=>r.line.note||'',cell:r=>r.line.note||''},
    // all clinics first, then each where as exceptionTree lays it out (its anchor, then its exceptions).
    {key:'specific',label:'Most specific first',hidden:true,cell:()=>'',sort:r=>(r.line.where?.conditions?.length?1e6:0)+(shapeOf(r.line)?.at??1e5+specificFirst(r.line))},
    // Group by (and filter): each a hidden property. Person lines sit under their rank; a #tag stays one line under it.
    {key:'kind',label:'Who kind',hidden:true,ordered:true,cell:()=>'',facet:r=>g.kind(r.line)},
    {key:'rank',label:'Rank',hidden:true,cell:()=>'',facet:r=>g.rank(r.line)},
    {key:'person',label:'Person',hidden:true,cell:()=>'',facet:r=>g.person(r.line)},
    {key:'programme',label:'Programme',hidden:true,cell:()=>'',facet:r=>g.of(r.line.who,['programme_ids'],'No programme')},
    {key:'tag',label:'Tag',hidden:true,cell:()=>'',facet:r=>g.tags(r.line)},
    // Lines grouped by place: a line at two clinics is listed once, under its first (Filter still matches each clinic).
    // shortcut: grouped under its first clinic, not its where as a whole as the Rules list does; upgrade with a where facet registered in the rule-line-db contract.
    {key:'clinic',label:'Clinic',hidden:true,ordered:true,groupFirst:true,cell:()=>'',facet:r=>g.of(r.line.where,['service_ids','location_ids','assignment_type_ids'],'All clinics')},
    {key:'clinic_type',label:'Clinic type',hidden:true,cell:()=>'',facet:r=>g.clinicType(r.line)},
    {key:'building',label:'Building',hidden:true,cell:()=>'',facet:r=>g.building(r.line)},
    {key:'days_group',label:'Days',hidden:true,cell:()=>'',facet:r=>daysText(r.line)},
    {key:'level',label:'Mandatory / advisory',hidden:true,cell:()=>'',facet:r=>levelOf(r.line)},
    {key:'valid_group',label:'Valid',hidden:true,cell:()=>'',facet:r=>validFacets(r.line,data.day)},
    {key:'overlaps',label:'Overlaps',hidden:true,cell:()=>'',facet:r=>(found?issuesOf(r.line.id).some(x=>x.kind==='both'):(e.line_overlaps?.[r.line.id]||[]).some(o=>o.winner==null))?'Overlaps':'No overlap'},
    ...(editable?[{key:'check',label:'Check',class:'rc-col-narrow',cell:(r:LineRow)=>marks.stamp(checkCell(r.line))}]:[]),
  ];
  /** R6c: a line that gives way, or wins, shows ⚠; its popover says who is covered twice, which line wins and the fixes. */
  function checkCell(line:RuleLine):Child{
    const own=issuesOf(line.id);
    if(own.length){const x=own[0],b=badge(x.kind==='refused'?'⚠ Refused':x.kind==='same'?'⚠ Same line':x.kind==='both'?'⚠ Both apply':x.kind==='inherits'?'⚠ Inherits a conflict':'⚠ Weaker',
        x.kind==='weaker'||x.kind==='both'?'warn':'err','rc-overlap',{as:'button',...popup('dialog'),tip:x.message,'data-rule-mark':x.kind});
      b.addEventListener('click',ev=>{ev.stopPropagation();popover(b,(pop,close)=>pop.append(el('div',{class:'rc-twin'},
        ...own.flatMap(i=>[el('strong',{},i.title),el('p',{},i.message+'.')]),
        el('div',{class:'actions'},...(x.other?[button('Show the other line',()=>{close();show(x.other);},'secondary small')]:[])),
        el('small',{class:'muted'},'Checked locally as you edit; full impact is checked when you propose or activate.'))),{label:x.title});});
      return b;}
    // A winning exception and its anchor are the rule working as meant: no warning and nothing to acknowledge. Lines
    // that both apply (equal tiers) warn above, as "Both apply".
    return '';
  }
  const table=dataTable<LineRow>({id:'rule-line-db',noun:['line','lines'],rows,rowId:r=>r.line.id,searchLabel:'Search lines',searchPlaceholder:'Person, rank, clinic or #tag',
    // Catalogue v2 opens on the lines that apply now or later; clearing the Valid filter shows the ended ones.
    defaults:{group:'clinic',sort:['specific','asc'],hidden:['no'],...(data.catalogue_version===2?{filters:{valid_group:[CURRENT]}}:{})},columns,empty:['No lines yet',editable?'Add a line: who, where and its numbers.':''],
    decorate:(r,node)=>{node.dataset.line=r.line.id;node.classList.toggle('is-pointed',r.line.id===pointed);
      if(!proposedIds.has(r.line.id)&&(saved.every(l=>l.id!==r.line.id)||JSON.stringify(saved.find(l=>l.id===r.line.id))!==JSON.stringify(r.line)))node.classList.add('rc-changed');},
    onOpen:(r,at)=>proposedIds.has(r.line.id)?decideProposal():panel.open(r.line.id,at),
    where:r=>!onlyAffected||issuesOf(r.line.id).length>0,
    bulk:editable?ids=>bulkActions(e,ids,draft,changed,refresh):undefined});
  const add:HTMLButtonElement|null=editable?button('+ New line',()=>newLinePicker(ctx,e,add!,draft,changed,id=>panel.open(id)),'',{class:'rc-new-line','aria-label':'New line: a person, rank, clinic or #tag'}):null;
  const onText=el('b',{},draft.on?'On':'Off'),headFacts=el('span',{class:'muted'});
  let onBox:HTMLInputElement|null=null;
  const showOn=()=>{onText.textContent=draft.on?'On':'Off';if(onBox)onBox.checked=draft.on;};
  const settingsBtn:HTMLButtonElement=button('Settings',()=>settingsPopover(ctx,e,settingsBtn,draft.settings,editable,(value,balance)=>{draft.settings=value;if(balance!==undefined)draft.balance=balance;changed();},
    draft.lines.filter(l=>ownSettings(l,e)).length,balances(e)?draft.balance:undefined,
    value=>checkLines(e,{on:draft.on,lines:draft.lines,settings:value}).then(r=>settingIssues(r,'rule.catalogue.settings.'))),'secondary');
  const fromSaved=e.rule?savedReach(ctx,e.rule.id,data.day,primed):()=>Promise.resolve(null);
  // The latest check of the draft as it stands (the detail read's until something changes).
  const countingOf=(id:string)=>{const c=found??primedCheck;return c?(c.counting_attention||[]).find(x=>x.line_id===id)||null:undefined;};
  const panel=lineEditor(ctx,e,draft,names,o,{family,saved,day:data.day,fromSaved,editable,changed,counting:countingOf,current:()=>found??primedCheck,inherited:inhOf,from,checkState:()=>found?'checked':checkFailed?'unavailable':editable?'checking':'saved',issues:id=>[...issuesOf(id).map(x=>x.kind==='refused'?`${x.title}: ${x.message}`:x.title)],
    order:()=>table.ids(),position:id=>table.position(id),reachChanged:(id,n)=>{if(shownReach.get(id)!==n){shownReach.set(id,n);table.refresh();}},
    openLine:id=>show(id),point:id=>{if(pointed!==id){pointed=id;table.refresh();}}});  // the open line is outlined by the dock (its mark) after each draw
  panelReach=panel.reachOf;
  const show=(id:string)=>{if(!rows.some(r=>r.line.id===id))return;panel.open(id);};
  // Retire, as on every rule (an approver's action): the rule stops applying to rotas being planned.
  const live=e.rule?.status==='active'?e.rule:null;
  // A revised rule's retirement brings back the version(s) it replaced (the server's merged_ids), as on app.ts's rule sheet.
  const merged=(live?.definition?.merged_ids||[]).length;
  const retire=live&&grants(ctx.me.roles,'policy.retire')?button('Retire rule',()=>confirmAction(`Retire ${e.name}`,
    merged?`This version stops applying and the ${merged===1?'version':plural(merged,'rule')} it replaced ${merged===1?'applies':'apply'} again, as approved. Published rotas keep the rules they were approved under.`
      :'It stops applying to rotas being planned. Published rotas keep the rules they were approved under.',async reason=>{
      const r=await command<{restored_ids?:string[];approval?:Row}>('policy.retire',{reason},live.id,live.revision);
      // A first of several approvals: the rule stays active; the toast says who else must approve and offers withdrawal.
      if(notifyWaiting([r],ctx.me.identity_id,refresh)){await refresh();return;}
      o.setLeave?.(null);
      notify(r.restored_ids?.length?`${e.name} retired. The ${r.restored_ids.length===1?'version':plural(r.restored_ids.length,'rule')} it replaced ${r.restored_ids.length===1?'applies':'apply'} again.`:`${e.name} retired. It no longer applies to rotas being planned.`);await refresh();},'Retire rule'),'ghost'):null;
  const waiting=e.proposed?proposalNote(ctx,e,names,refresh,o.openTag,decideProposal,marks):null;
  const reviewAction=live&&ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER)
    ?compact(button(TERMS.checkRuleAgainstRotas,()=>{void proposalReview(live.id).catch(()=>undefined);void ruleReview(live,true,false,refresh,
      (kind,id)=>labelOf((ctx.catalogues[kind]||[]).find(r=>r.id===id)),departmentTimeZone(),false,[],e.name,'',ctx);},'secondary'),'shield'):'';
  target.append(sectionHead(e.name,'',...(balances(e)||Object.keys(e.line_settings).some(k=>settingFields(k,e).length)?[settingsBtn]:[]),moreActions('More actions for this rule',[compact(button('History',()=>historyDialog(ctx,e,names),'secondary'),'clock'),reviewAction,retire?compact(retire,'close'):'']),...(canEdit?[discardBtn,proposeBtn]:[])),
    el('div',{class:'rc-crumb'},button('← Rules',()=>{o.setLeave?.(null);o.navigate('rules');},'',{class:'link-button'}),el('h2',{},e.name),
      el('button',{type:'button',class:'rc-info','aria-label':`About ${e.name}: ${e.description} ${whoWins(e)}`,'data-tip':`${e.description} ${whoWins(e)}`},'i')),
    el('div',{class:'rc-summary'},e.proposed?el('span',{class:'rc-now'},'In force now:'):'',draftSwitch(),onText,headFacts),
    // Whether "Allow once…" can ever appear on the Schedule for this rule, and what it is checked against.
    // Two short facts on one line (label beside value), not four stacked lines before the rule's lines.
    // Person lines go through approval like any line; a safety rule's never weaken it.
    live?el('div',{class:'rc-facts'},ruleFacts([['Person lines',e.safety&&!e.mixed_safety?'Allowed, never weaker, with approval':'Allowed, with approval'],['Checked against','Rotas being planned']])):'',
    movedNote(e,names,()=>void refresh()),waiting||'',issues,
    editable&&e.legacy_leads?leadsNote(e.legacy_leads):'',
    table.root,marks.watch(table.root,...(waiting?[waiting]:[])),add?el('div',{class:'rc-add'},add):'',
    live&&o.inDraft?o.inDraft(live):'',
    footerStatus(status));
  function decideProposal(){proposalPanel(ctx,e,names,refresh);}
  changed(false);
  if(o.parts[1]){  // An exact source link never substitutes another current line.
    // SW3-B-15: a new line picked from the Rules list (+ New line in <rule>) arrives unsaved (pendingLines), not in the draft.
    if(draft.lines.some(line=>line.id===o.parts[1])||pendingLines.get(e.id)?.id===o.parts[1])panel.open(o.parts[1]);
    else target.append(note('This line is not available in the current rule. Choose one from the list.','warning'));
  }
  function leadsNote(n:number){const box=note(`${plural(n,'duty type')} still ${n===1?'sets its':'set their'} own team lead. `,'info');
    box.append(button('Convert into this rule…',()=>convertLeads(e,names,refresh),'',{class:'link-button'}));return box;}
  function draftSwitch(){
    // A reader who cannot propose reads On / Off (the word beside it), not a switch they cannot use.
    if(!canEdit)return '';
    const box=onBox=el('input',{type:'checkbox',role:'switch',class:'switch','aria-label':`${e.name} on or off`,checked:draft.on,disabled:!editable}) as HTMLInputElement;
    box.addEventListener('change',()=>{draft.on=box.checked;showOn();
      // Turning on a rule with no lines starts it from its usual first line (then edit it before proposing).
      if(draft.on&&!draft.lines.length&&Object.keys(e.default_line?.numbers||{}).length){const l=newLine(e);draft.lines.push(l);changed();panel.open(l.id);return;}
      changed();});return box;}
  async function proposeDraft(){
    if(draft.on&&!draft.lines.length){notify('A rule that is on needs at least one line.','warning');return;}
    if(draft.lines.some(l=>!Object.keys(l.numbers||{}).length)){notify('Every line needs a number (or Applies).','warning');return;}
    const missing=missingNeeds(e,draft.settings,draft.lines);if(draft.on&&missing.length){notify(`Choose ${missing.join(', ')} in Settings first.`,'warning');return;}
    // The server's check first: the same line twice or a line it refuses stops here.
    window.clearTimeout(timer);const [now]=await checkBeforeProposing([{e,body:draft}],proposeDraft)||[];if(!now)return;
    const stop=[...now.duplicates,...now.issues];
    if(stop.length){notify([...now.duplicates.map(d=>words.same(d)+'.'),...now.issues.map(x=>x.message)].join(' '),'error');return;}
    const settingsChanged=JSON.stringify(draft.settings)!==JSON.stringify(settingsOf(e));
    const turned=draft.on!==isOn(e);
    const intro=el('div',{class:'span-2'},...(turned?[el('p',{},el('b',{},'~ '),draft.on?'Turns the rule on':'Turns the rule off')]:[]),...(settingsChanged?[settingsChange(ctx,names,settingsOf(e),draft.settings)||'']:[]),
      ...(draft.balance!==balanceOf(e)?[el('p',{},el('b',{},'~ '),'Balance between people: '+balanceWords(draft.balance))]:[]),
      lineChanges(saved,draft.lines).length||!settingsChanged&&!turned&&draft.balance===balanceOf(e)?changeList(e,saved,draft.lines,names,draft.settings):'');
    confirmAction(`Propose ${plural(changes(),'change')} to ${e.name}`,'One new version, applied once another authorised person approves it. Retiring it later brings back this version.',async reason=>{
      const record=await propose(e,draft,reason).catch(err=>{
        // Duty types still set their own lead: the first version must be their conversion (offered, never guessed).
        if(err instanceof ApiError&&err.code==='LEAD_CONVERSION_REQUIRED')notify(err.message,'warning',{label:'Convert duty type leads',run:()=>convertLeads(e,names,refresh)});
        throw err;});rememberProposed(String(record.id),e,draft);discardDraft(e.id);o.setLeave?.(null);
      // F-maintenance: a line now inside a pending proposal is not left open for editing.
      if(document.querySelector('.rc-line-panel'))closeDock();
      notify('Proposed; it applies once another authorised person approves it.');await refresh();},'Propose',intro);
  }
}
/** The server's equivalent conversion of duty types' own lead settings (POST …/team_leads/check with a date). */
type LeadPlan=Check&{lines:RuleLine[];legacy_lead_conversion:Row|null;differences?:string[];normalizations?:string[]};
/** Team leads' first version from the duty types' own lead settings: you choose the date, the server's equivalent lines
 *  show read only and are proposed exactly as shown (on, no rule settings); another person approves it as any change.
 *  Duty type leadership changed meanwhile: the preview reloads and the proposal waits for another look. */
function convertLeads(e:Entry,names:Namer,refresh:()=>Promise<void>){
  const on=field('Effective from','date',departmentToday(),{required:true});
  const shown=el('div',{role:'status','aria-live':'polite'});
  let plan:LeadPlan|null=null,asked=0,error='';
  const load=async()=>{const n=++asked,day=on.input.value;plan=null;error='';shown.replaceChildren(working('Checking…'));
    if(!day){shown.replaceChildren();return;}
    try{const r=await request<LeadPlan>(`/api/rules/catalogue/${e.id}/check`,{method:'POST',body:JSON.stringify({catalogue_version:e.catalogue_version,on:true,settings:{},lines:[],legacy_effective_on:day})});
      if(n!==asked)return;plan=r;}
    catch(err){if(n!==asked||sessionChanged(err))return;error=(err as Error).message;shown.replaceChildren(note(error,'error',err));return;}
    const stop=[...plan.duplicates.map(d=>checkWords(e,()=>plan!.lines,names).same(d)+'.'),...plan.issues.map(x=>String(x.message)),...(plan.differences||[])];
    shown.replaceChildren(...(!plan.legacy_lead_conversion?[note('No duty type sets its own lead now. Add lines to this rule instead.','info')]:[
      el('p',{class:'dt-pop-label'},`${plural(plan.lines.length,'line')} from ${formatDate(day)}`),
      el('ul',{class:'rc-diff'},...plan.lines.map(l=>el('li',{},lineWords(e,l,names)))),
      ...(plan.normalizations||[]).map(x=>el('small',{class:'muted'},x)),
      ...stop.map(x=>note(x,'error'))]));};
  on.input.addEventListener('change',()=>void load());
  confirmAction(`Convert duty type leads into ${e.name}`,'The lines below replace each duty type’s own lead setting once another authorised person approves them.',async reason=>{
    if(!plan||plan.lines.some(l=>l.starts_on!==on.input.value))await load();
    if(!plan)throw new Error(error||'The conversion could not be previewed. Try again.');
    if(!plan.legacy_lead_conversion)throw new Error('Nothing to convert.');
    if(plan.duplicates.length||plan.issues.length||plan.differences?.length)throw new Error('This conversion is refused as shown.');
    try{await command('rule.propose',{catalogue_id:e.id,catalogue_version:e.catalogue_version,expected_policy_id:null,expected_revision:null,
      on:true,settings:{},lines:plan.lines,legacy_lead_conversion:plan.legacy_lead_conversion,reason});}
    catch(err){if(err instanceof ApiError&&['STALE_LEAD_CONVERSION','LEAD_CONVERSION_CHANGED'].includes(err.code)){await load();
      throw new Error('Duty type leadership changed. Review the updated lines, then propose again.');}throw err;}
    notify('Proposed; it applies once another authorised person approves it.');await refresh();},'Propose',el('div',{class:'span-2'},on.wrap,shown));
  void load();
}
/** Group-by values of a line (the rule page's hidden properties). */
export function groupings(ctx:Context,names:Namer,today:string){
  const on=(r:Row)=>(!r.starts_on||r.starts_on<=today)&&(!r.ends_on||r.ends_on>today);
  const ranks=new Map<string,string[]>();
  for(const m of ctx.catalogues.memberships||[])if(on(m))ranks.set(m.person_id,[...(ranks.get(m.person_id)||[]),names.label('category_ids',m.category_id)]);
  const people=(tag:string)=>membersOf(ctx,tag).filter(m=>m.kind==='people').map(m=>m.id);
  const named=(l:RuleLine)=>valuesOf(l.who).filter(v=>v.op!=='none');
  const places=new Map((ctx.catalogues.locations||[]).map(r=>[r.id,r]));
  const top=(id:string)=>{let r=places.get(id),n=0;while(r?.parent_id&&places.has(r.parent_id)&&n++<20)r=places.get(r.parent_id);return r?labelOf(r):'No building';};
  return {
    kind:(l:RuleLine)=>{const v=named(l);if(!l.who?.conditions.length)return 'Everyone';if(!v.length||l.who.conditions.some(c=>'conditions' in c))return 'Combined filters';if(tier(l)===3)return 'People';
      const k=new Set(v.map(x=>names.tag(x.value)?'Tags':x.field==='category_ids'?'Ranks':x.field==='programme_ids'?'Programmes':x.field==='person_ids'?'People':'Other groups'));
      return k.size===1?[...k][0]:'Several kinds';},
    rank:(l:RuleLine)=>{const v=named(l);if(!l.who||!v.length)return 'All ranks';
      return [...new Set(v.flatMap(x=>{if(names.tag(x.value)){const r=new Set(people(x.value).flatMap(p=>ranks.get(p)||[]));return r.size===1?[...r]:['Several ranks'];}
        return x.field==='category_ids'?[names.label(x.field,x.value)]:x.field==='person_ids'?ranks.get(x.value)||['No rank']:['Other groups'];}))];},
    person:(l:RuleLine)=>{const v=named(l).filter(x=>x.field==='person_ids'&&!names.tag(x.value));return v.length?v.map(x=>names.label(x.field,x.value)):'Not a person';},
    tags:(l:RuleLine)=>{const t=[...valuesOf(l.who),...valuesOf(l.where)].filter(x=>names.tag(x.value)).map(x=>names.label(x.field,x.value));return t.length?t:'No tag';},
    of:(tree:Conditions|null|undefined,fields:string[],none:string)=>{const v=valuesOf(tree).filter(x=>fields.includes(x.field)||names.tag(x.value));return v.length?v.map(x=>names.label(x.field,x.value)):none;},
    clinicType:(l:RuleLine)=>{if(!l.where)return 'All clinics';const services=new Map((ctx.catalogues.services||[]).map(r=>[r.id,r]));
      return valuesOf(l.where).map(x=>names.tag(x.value)?names.label(x.field,x.value):x.field==='service_ids'?words(String(services.get(x.value)?.mode||'unspecified')):'Other places');},
    building:(l:RuleLine)=>{const v=valuesOf(l.where).filter(x=>x.field==='location_ids');return v.length?v.map(x=>top(x.value)):'No building';},
  };
}
export const levelOf=(l:RuleLine)=>{const v=Object.values(l.numbers||{}).map(n=>n.level==='mandatory');return !v.length?'No number':v.every(Boolean)?'Mandatory':v.some(Boolean)?'Mandatory and advisory':'Advisory';};

/** R8 / K1f: one line in the shared docked item panel (`dockPanel`, beside the page, resizable from its edge; a sheet on
 *  phones) — "<rule> › Line 1 of 20", its title in words with a lone ✓ once the check finds no duplicate or overlap;
 *  the rule's family (its description on the (i)) and "This line says"; Who and Where as Notion-style filters
 *  (conditions.ts) whose counts open their lists (rule_line_reach.ts); Numbers, When, Note; Remove line · Cancel ·
 *  Keep change. ↑ ↓ move through the table's order (the unsaved-changes guard asks first). */
function lineEditor(ctx:Context,e:Entry,draft:Draft,names:Namer,o:Options,s:{editable:boolean;
  /** The line's counting-end warning from the check the page holds (undefined: none yet). */counting:(id:string)=>CountingEnd|null|undefined;
  /** The page's check of the draft as it stands (null: none yet). */current?:()=>Check|null;changed:()=>void;inherited:(id:string)=>Inherited|undefined;from:(id:string)=>string;checkState:()=>string;issues:(id:string)=>string[];position:(id:string)=>PanelPosition|undefined;order:()=>string[];
  family:string;saved:RuleLine[];day:string;reachChanged?:(id:string,people:number|null)=>void;fromSaved:(lineId?:string,line?:RuleLine)=>Promise<{lines:Row[];counted:boolean;period:string}|null>;
  /** The relation's "line N": open that line (the rule page), and tint its row while the relation names it. */openLine?:(id:string)=>void;point?:(id:string|null)=>void;
  /** B4: relax for one person from a rota blocker. `rota` (D16, the default): the adjustment applies to this draft at once
   *  with its justification and is reviewed with the rota; the department rule change (propose → approve) stays the
   *  secondary path, and the only one when the server sends no adjustment. `done` after either. */
  relax?:{done:()=>void;rota?:{adjustment:Adjustment;workspace_id:string;name?:string;revision?:number}}}){
  let id:string|null=null,dock:Dock|null=null,position:PanelPosition|undefined;
  let reached:LineReach|null=null,editing:RuleLine|null=null;
  let checkedLine:{key:string;messages:string[]}|null=null;
  const lineMessages=(l:RuleLine)=>JSON.stringify(l)===JSON.stringify(draft.lines.find(x=>x.id===l.id))&&s.checkState()==='checked'?s.issues(l.id):
    checkedLine?.key===JSON.stringify(l)?checkedLine.messages:[];
  const title=(l:RuleLine)=>{const said=lineMessages(l),current=checkedLine?.key===JSON.stringify(l)||
    JSON.stringify(l)===JSON.stringify(draft.lines.find(x=>x.id===l.id))&&s.checkState()==='checked',ok=current&&!said.length;
    const side=(k:'who'|'where',none:string)=>(l[k]?.conditions.some(c=>'conditions' in c)&&reached?.now()?reached.noun(k):countWords(l[k],names,none))+(hasEach(l[k])?' (each)':'');
    return el('span',{},`${side('who','Everyone')} at ${side('where','all clinics')} · ${numbersText(l,s.inherited(l.id),appliesWord(e),unitText(e,l,draft.settings))||'no number'}`,ok?el('span',{class:'rc-lp-ok',role:'img','aria-label':'No duplicate or overlap','data-tip':'No duplicate or overlap'},icon('check')):'');};
  const status=(lineId:string)=>{const l=editing?.id===lineId?editing:draft.lines.find(x=>x.id===lineId),said=l?lineMessages(l):s.issues(lineId);return said.length?el('span',{class:'warn-text',role:'alert'},'⚠ '+said.join(' · ')):s.checkState()==='unavailable'?el('span',{class:'muted'},'Check unavailable'):'';};
  /** The table's order when the panel was not opened from a row (a link, "Show the other line"). */
  // Over all of the rule's lines (every page of the table), as a row click counts them.
  const positionOf=(lineId:string):PanelPosition=>{const at=s.position(lineId);if(at)return at;const order=s.order();return {index:Math.max(0,order.indexOf(lineId)),total:order.length||draft.lines.length,noun:'line',go:i=>{const next=s.order()[i];if(next)open(next);}};};
  const canCheck=ctx.permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER);
  /** A line in the draft, or the new line being written (only in the panel until it has a number). */
  const lineOf=(lineId:string)=>draft.lines.find(l=>l.id===lineId)||(pendingLines.get(e.id)?.id===lineId?pendingLines.get(e.id):undefined);
  function open(next:string,at?:PanelPosition){
    const line=lineOf(next);if(!line){closeDock();return;}
    const fresh=!draft.lines.includes(line);
    if(fresh&&!at){const n=s.order().length;at={index:n,total:n+1,noun:'line',go:i=>{const x=s.order()[i];if(x)open(x);}};}
    // The line's own number (its stored order), as the Rules list's rail and the check's messages number it.
    position={...(at||positionOf(next)),prefix:e.name+' › ',number:fresh?draft.lines.length+1:draft.lines.indexOf(line)+1};
    const w=clone(line),read:(()=>void)[]=[];
    dockPanel({key:'rule-line',className:'rc-line-panel',title:title(line),sub:status(next),width:560,position,mark:rowMark(next),
      // A new line closed before it has a number holds nothing: it goes, without a question.
      onClose:leaving=>{if(id===next){id=null;dock=null;reached=null;editing=null;checkedLine=null;s.point?.(null);}if(pendingLines.get(e.id)?.id===next)pendingLines.delete(e.id);
        // A line link (#rules/<rule>/<line>) ends with the panel: a reload or a shared link shows the rule, not a closed line.
        // Closed by navigating (leaving), the address is the destination's: a line link followed from here keeps its line.
        // A redraw (a live update) replaces the panel with the same line's: that is not an end, so the link stays.
        if(lineShown===next)lineShown=null;
        if(!leaving)queueMicrotask(()=>{if(lineShown!==next&&location.hash.startsWith(`#rules/${e.id}/`))history.replaceState(history.state,'',`#rules/${e.id}`);});},
      build:d=>{
        id=next;dock=d;lineShown=next;
        const section=(head:Child,...body:Child[])=>el('section',{class:'rc-lp-section'},el('h3',{class:'rc-lp-title'},head),...body);
        const live=e.rule?.status==='active'&&ctx.permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR);
        const reach=reached=lineReach(ctx,{day:s.day,line:()=>w,lines:()=>fresh?[...draft.lines,w]:draft.lines.map(l=>l.id===w.id?w:l),saved:s.saved.find(l=>l.id===next),plain:e.reach[next],fromSaved:s.fromSaved,
          check:s.editable&&canCheck?lines=>checkLines(e,{on:true,lines,settings:draft.settings}):undefined,label:names.label,
          whichRule:o.whichRule?pid=>o.whichRule!(pid):undefined,onSchedule:live?()=>{closeDock();highlightRule(e.rule!.id);o.navigate('schedule');}:undefined});
        const heads={who:el('span',{class:'rc-lp-count'}),where:el('span',{class:'rc-lp-count'})};
        const says=el('p',{class:'rc-says-text'});
        editing=w;
        const paint=()=>{for(const k of ['who','where'] as const){const x=reach.link(k);keepSame(heads[k],x?[' · ',x]:[]);}
          keepSame(says,lineSays(e,w,names,{who:reach.link('who'),where:reach.link('where')},s.inherited(next),draft.settings));
          d.setHead(title(w),status(next));};
        reach.onChange(()=>{paint();sync();s.reachChanged?.(next,reach.now()?.people.size??null);});
        const side=(key:'who'|'where')=>{
          const none=key==='who'?'Everyone':'All clinics',sessions=key==='where';
          if(!s.editable&&!w[key]?.conditions.length)return section(el('span',{},key==='who'?'Who':'Where',heads[key]),el('p',{class:'muted rc-lp-none'},none));
          const b=conditionBuilder(ruleConditionFields(ctx).filter(f=>SESSION_FIELDS.includes(f.key)===sessions).map(withInactive),w[key],
            {allOf:true,startEmpty:true,readOnly:!s.editable||!!s.relax?.rota,count:()=>e.count_sides.includes(key),context:'rule',...(key==='who'?{lead:''}:{})});
          b.root.addEventListener('change',()=>{const v=b.read();if(v)w[key]=v;else if(line[key]===null)w[key]=null;else delete w[key];paint();reach.changed();relate();});
          return section(el('span',{},key==='who'?'Who':'Where',heads[key]),b.root);
        };
        const bound=Object.keys(w.source_bindings||{}).length>0;
        const mode=e.modes?select('Mode',e.modes.map(v=>({value:v,label:words(v)})),modeText(e,w,draft.settings).toLowerCase().replaceAll(' ','_'),true):null;
        // The section says Mode; its field label stays for assistive technology only.
        if(mode){mode.input.disabled=!s.editable||bound;mode.wrap.querySelector('label')?.classList.add('sr-only');}
        let numberRead:(()=>void)[]=[];
        const numbers=el('div',{}),drawNumbers=()=>{numberRead=[];
          const keys=bound||w.numbers.min&&w.numbers.apply?Object.keys(w.numbers):!mode?Object.keys(e.numbers)
            :['no_duties','next_day_combination','avoid_within_days'].includes(mode.input.value)?['apply']
            :e.measures?Object.keys(e.numbers).filter(k=>k!=='apply'):['min'];
          numbers.replaceChildren(numberFields({...e,numbers:Object.fromEntries(keys.map(k=>[k,e.numbers[k]]))},w,s.editable,numberRead,s.inherited(next),s.from,
            fresh?k=>broaderStrength(w,draft.lines,k,tier):undefined));};
        drawNumbers();read.push(()=>{for(const r of numberRead)r();});
        // B3: what this line does to the broader line with the same where (one row under "This line says") and its effect
        // on the drafts its dates overlap, from the server's check of the lines as edited (the same request reach makes).
        const saysBox=el('div',{class:'rc-says',role:'status','aria-live':'polite'},el('span',{class:'rc-says-label'},'This line says'),says);
        const relation=el('p',{class:'rc-relation'});relation.hidden=true;saysBox.append(relation);
        const effectList=el('ul',{class:'rc-lp-list rc-effect'}),effectSec=section(el('span',{},'Effect',effectHelp(w)),effectList);  // in the panel only once the check has an effect
        const withW=()=>fresh?[...draft.lines,w]:draft.lines.map(l=>l.id===w.id?w:l);
        let relTimer=0,relAsked=0;
        const relate=(again=false)=>{if(!s.editable||!canCheck)return;window.clearTimeout(relTimer);
          relTimer=window.setTimeout(()=>{const n=++relAsked,key=JSON.stringify(w);
            // A line not edited here is the draft the page has checked (a link's detail read): no second check.
            const held=!again&&!fresh&&JSON.stringify(w)===JSON.stringify(line)?s.current?.()??null:null;
            void (held?Promise.resolve(held):checkLines(e,{on:true,lines:withW(),settings:draft.settings},undefined,again)).then(async c=>{
            if(n!==relAsked||!d.body.isConnected||key!==JSON.stringify(w))return;
            // maintenance: a new line takes the settings and strengths the server resolves for it (its nearest broader line's),
            // where it has none of its own yet.
            const canon=fresh?((c as {lines?:RuleLine[]}).lines||[]).find(l=>l.id===w.id):undefined;
            if(canon){if(!w.settings||!Object.keys(w.settings).length)w.settings=canon.settings;
              for(const [k,x] of Object.entries(canon.numbers||{}))if(w.numbers?.[k]&&!w.numbers[k].level&&x?.level)w.numbers[k]={...w.numbers[k],level:x.level};}
            checkedLine={key:JSON.stringify(w),messages:[...c.issues.filter(x=>lineOfIssue(x)===withW().findIndex(l=>l.id===w.id)).map(x=>String(x.message)),
              ...c.duplicates.filter(x=>x.line_ids.includes(w.id)).map(x=>checkWords(e,withW,names).same(x)),
              ...c.overlaps.filter(x=>x.line_id===w.id||x.winner_ids.includes(w.id)).map(x=>checkWords(e,withW,names).over(x)),
              ...c.warnings.filter(x=>x.line_id===w.id||x.kind==='both'&&x.other_id===w.id).map(x=>checkWords(e,withW,names).warn(x))]};
            paint();
            paintRelation(relationOf(c,w.id));
            // F-R7: a line without a number yet has no effect to ask for (the server would refuse it with a 422).
            for(const x of numberRead)x();const complete=Object.values(w.numbers||{}).some(x=>x?.value!=null&&String(x.value)!=='');
            const rows=s.relax?.rota?await rotaEffect():!complete?null:await lineEffect(e.id,{expected_policy_id:e.rule?.id??null,expected_revision:e.rule?.revision??null,
              catalogue_version:e.catalogue_version as 1|2,line:wire([w],linesOf(e))[0]}).catch(()=>null);if(n!==relAsked||!d.body.isConnected)return;if(!rows?.length)effectSec.remove();else if(!effectSec.isConnected)saysBox.after(effectSec);effectList.replaceChildren(...effectRows(rows||[]));
            if(rows?.some(effectRunning))relate(true);}).catch(()=>undefined);},again?3000:300);};  // a running effect is asked again until it is known
        /** D16: the adjustment's effect on this rota (nothing saved), once it has a number; the justification typed so far
         *  is part of the request, as the command takes it. */
        async function rotaEffect(){const r=s.relax!.rota!,a=r.adjustment;for(const x of numberRead)x();const amount=w.numbers[a.number]?.value;
          if(amount==null)return null;
          return adjustmentEffect(r.workspace_id,r.name||'This rota',r.revision??await workspaceRevision(r.workspace_id),
            {...a,...(w.starts_on&&w.ends_on?{starts_on:w.starts_on,ends_on:w.ends_on}:{})},Number(amount),why?.input.value.trim().length!>=3?why!.input.value.trim():'Effect preview').catch(()=>null);}
        function paintRelation(r:Relation|null){
          const other=r&&draft.lines.find(l=>l.id===r.line_id);
          relation.hidden=!r||!other;saysBox.classList.toggle('is-warn',!!r&&!!other&&!r.overrides);s.point?.(other?other.id:null);
          if(!r||!other)return;
          relation.replaceChildren(...(relationParts(e,names,r,w,other,()=>s.openLine?s.openLine(other.id):o.navigate(`rules/${e.id}/${other.id}`),
            // The one fix: this line's numbers take the broader line's strengths (D8's default, chosen now).
            s.editable?()=>{for(const x of numberRead)x();
              // F-R6 (maintenance): only the settings differ, so this line takes the broader line's settings as they are, then is checked again.
              // A typed workload line takes the broader line's measure and period too, or its own would apply again (maintenance).
              if(r.fix==='match_settings'&&r.default_settings){w.settings=JSON.parse(JSON.stringify(r.default_settings)) as RuleLine['settings'];
                if(other.measure!==undefined)w.measure=other.measure;if(other.period!==undefined)w.period=other.period?JSON.parse(JSON.stringify(other.period)):other.period;
                drawNumbers();follow();relate();return;}
              for(const [key,level] of Object.entries(r.default_severity||{}))if(w.numbers[key])w.numbers[key]={...w.numbers[key],level:level as RuleLine['numbers'][string]['level']};
              drawNumbers();follow();}:undefined) as (Node|string)[]));
        }
        const counting=canCheck?()=>checkLines(e,{on:true,lines:fresh?[...draft.lines,w]:draft.lines.map(l=>l.id===w.id?w:l),settings:draft.settings})
          .then(r=>(r.counting_attention||[]).find(x=>x.line_id===w.id)||null):undefined;
        const counts=countFields(e,w,s.editable,read,counting,s.counting(next)),countSection=counts?section('Counts',counts):null;
        mode?.input.addEventListener('change',()=>{for(const r of numberRead)r();
          if(mode.input.value==='no_duties')w.numbers={apply:{value:null,level:'mandatory'}};
          else if(mode.input.value==='count')w.numbers={};
          else if(mode.input.value==='elapsed_rest'){w.numbers={};delete w.settings?.forbidden_transition;delete w.settings?.shift_succession;}
          else{const slot=e.numbers.apply[e.modes!.indexOf(mode.input.value)-1];w.numbers={apply:{value:null,level:slot.advisory?'medium':'mandatory'}};
            const settings={...(w.settings||{})};for(const s of e.numbers.apply)delete settings[s.kind];settings[slot.kind]={...slot.fixed};w.settings=settings;}
          drawNumbers();settingsSection.replaceWith(settingsSection=lineSettings());follow();});
        const days=daysEditor(w),valid=validEditor(w),noteField=area('Note',w.note||'',2);noteField.input.maxLength=500;
        for(const b of [...days.node.querySelectorAll<HTMLButtonElement>('button')])b.disabled=!s.editable;noteField.input.readOnly=!s.editable;
        const dates=[...valid.node.querySelectorAll<HTMLInputElement>('input')];for(const input of dates)input.readOnly=!s.editable;
        // Read-only: an open end reads as words, not as an example to type.
        if(!s.editable&&dates.length===2){dates[0].placeholder=dates[1].value?'From the start':'Always';dates[1].placeholder=dates[0].value?'Ongoing':'';
          const arrow=valid.node.querySelector<HTMLElement>('.date-arrow');if(arrow)arrow.hidden=!dates[0].value&&!dates[1].value;}
        read.push(()=>{w.weekdays=days.read();const v=valid.read();w.starts_on=v.starts_on;w.ends_on=v.ends_on;w.note=noteField.input.value;});
        function lineSettings():HTMLElement{
          const own=w.settings||{},rows=Object.entries(own).flatMap(([kind,v])=>Object.entries(v||{}).filter(([f])=>settingFields(kind,e,true).includes(f)).map(([f,x])=>({kind,f,x})));
          if(!Object.keys(e.line_settings).some(k=>settingFields(k,e,true).length)||!rows.length&&!s.editable)return el('span',{hidden:true});
          // A condition tree (or an ordered list of them, "1. … 2. …") reads in words like a line's who.
          const rule=draft.settings,shownValue=(f:string,x:unknown)=>settingValue(ctx,names,f,x);
          const list=el('ul',{class:'rc-lp-settings'},...rows.map(r=>el('li',{},el('span',{},settingWords(r.kind,r.f,e)),el('b',{},shownValue(r.f,r.x)),
            // The rule's own value beside it, only where the rule sets one.
            rule[r.kind]?.[r.f]==null?'':el('span',{class:'muted'},`rule: ${shownValue(r.f,rule[r.kind][r.f])}`))));
          // Checked as the rule would be with this line's settings (its issues name lines.<n>.settings).
          const withLine=(value:Settings)=>{const lines=fresh?[...draft.lines,{...w,settings:value}]:draft.lines.map(l=>l.id===w.id?{...w,settings:value}:l);
            return checkLines(e,{on:true,lines,settings:draft.settings}).then(r=>settingIssues(r,`rule.catalogue.lines.${lines.findIndex(l=>l.id===w.id)}.settings.`));};
          const edit:HTMLButtonElement=button(s.editable?'Edit…':'Details…',()=>settingsPopover(ctx,e,edit,own,s.editable,value=>{w.settings=fresh?suggestedSettings(e,own,value):value;d.body.dispatchEvent(new Event('change',{bubbles:true}));settingsSection.replaceWith(settingsSection=lineSettings());paint();reach.changed();},
            0,undefined,s.editable&&canCheck?withLine:undefined,true,draft.settings),'secondary small');
          const reset=s.editable&&rows.length?button('Use the rule’s settings',()=>{delete w.settings;settingsSection.replaceWith(settingsSection=lineSettings());d.body.dispatchEvent(new Event('change',{bubbles:true}));paint();reach.changed();},'ghost small'):null;
          const sec=section(el('span',{},'Settings for this line'),rows.length?list:el('p',{class:'muted'},'Uses the rule’s settings.'),el('div',{class:'rc-lp-tools-row'},edit,reset));
          return sec;
        }
        let settingsSection=lineSettings();
        // A new line is kept only once it has a number: until then Keep change says what it needs.
        const numbered=()=>Object.keys(w.numbers||{}).length>0;
        const keepBtn=button('Keep change',()=>{const invalid=[...d.body.querySelectorAll<HTMLInputElement|HTMLSelectElement>('input,select')].find(x=>!x.disabled&&!x.closest('[hidden]')&&!x.checkValidity());
          if(invalid){invalid.reportValidity();return;}for(const r of read)r();if(fresh&&!numbered())return;keep(w,read);d.close();open(next,fresh?undefined:position);},'primary');
        const paintKeep=()=>{const off=fresh&&!numbered();keepBtn.setAttribute('aria-disabled',String(off));keepBtn.classList.toggle('is-disabled',off);
          if(off)keepBtn.dataset.tip='Add a minimum, maximum or target';else delete keepBtn.dataset.tip;};
        const remove=s.editable&&!fresh?armed(button('Remove line',()=>{draft.lines=draft.lines.filter(l=>l.id!==next);d.close();s.changed();},'ghost danger'),'Remove this line?'):null;
        d.body.classList.add('rc-lp-body');
        // Read-only: values read as text, not as empty or dimmed fields that look editable.
        d.body.classList.toggle('is-readonly',!s.editable);
        const info=el('button',{type:'button',class:'rc-info','aria-label':`${s.family}: ${e.description}`,'data-tip':e.description},'i');
        // B4: the justification, required (reviewed with the rota, or with the department rule change).
        const rota=s.relax?.rota;
        const why=s.relax?area(rota?'Justification':'Reason','',2):null;if(why){why.input.required=true;why.input.minLength=3;why.input.maxLength=500;why.wrap.querySelector('label')?.classList.add('sr-only');}
        const valid_=()=>{const invalid=[...d.body.querySelectorAll<HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement>('input,select,textarea')].find(x=>!x.disabled&&!x.closest('[hidden]')&&!x.checkValidity());
          if(invalid){invalid.reportValidity();return false;}for(const r of read)r();return numbered();};
        const applyBtn:HTMLButtonElement|null=rota?button('Apply to this rota',()=>applyLine(applyBtn!),'primary'):null;
        const proposeBtn:HTMLButtonElement|null=s.relax?button(rota?'Change the department rule…':'Propose',()=>rota?confirmAction(`Change ${e.name}`,
          `A new line for ${countWords(w.who,names,'everyone')}, applied once another authorised person approves it.`,reason=>proposeLine(proposeBtn!,reason),'Propose'):proposeLine(proposeBtn!,why!.input.value),rota?'secondary':'primary'):null;
        if(!rota)proposeBtn?.prepend(icon('send'));
        /** D16: the new number for this person and dates, in this draft only; its Undo is the draft's (api.ts). */
        async function applyLine(b:HTMLButtonElement):Promise<void>{
          if(!valid_())return;const a=rota!.adjustment,amount=w.numbers[a.number]?.value;
          if(amount==null){notify(`Enter the new ${(LONG_LABELS[a.number]||numberLabel(e,a.number)).toLowerCase()}.`,'warning');return;}
          const done=busy(b);
          try{await applyAdjustment(rota!.workspace_id,rota!.revision??await workspaceRevision(rota!.workspace_id),
            {...a,...(w.starts_on&&w.ends_on?{starts_on:w.starts_on,ends_on:w.ends_on}:{})},amount,why!.input.value);}
          finally{await done();}
          pendingLines.delete(e.id);d.close();s.relax!.done();
        }
        async function proposeLine(b:HTMLButtonElement,reason:string):Promise<void>{
          if(!valid_())return;
          const body=lineProposal(draft,withW().map(clone));
          const done=busy(b);
          try{const [now]=await checkBeforeProposing([{e,body}],()=>proposeLine(b,reason))||[];if(!now)return;
            if(now.duplicates.length||now.issues.length){notify([...now.duplicates.map(x=>checkWords(e,()=>body.lines,names).same(x)+'.'),...now.issues.map(x=>String(x.message))].join(' '),'error');return;}
            await propose(e,body,reason);}
          finally{await done();}
          pendingLines.delete(e.id);d.close();notify('Proposed. It applies once another person approves it.');s.relax!.done();
        }
        d.body.append(el('div',{class:'rc-lp-kind'},el('span',{class:'rp all'},s.family),info),
          saysBox,...(why?[section(rota?'Justification':'Reason',why.wrap)]:[]),
          ...(mode?[section('Mode',mode.wrap)]:[]),side('who'),side('where'),section('Numbers',numbers),countSection||'',settingsSection,
          ...(bound?[section('Source obligations',el('ul',{class:'rc-lp-settings'},...Object.entries(w.source_bindings||{}).map(([k,b])=>el('li',{},
            el('span',{},NUMBER_LABELS[k]||k),el('b',{},words(b.primitive_kind)),el('span',{class:'muted'},`Revision ${b.revision}`))))),
            note('This line retains its converted source and numeric slots. Add a new line for a different obligation.')]:[]),
          section('When',el('div',{class:'field'},el('label',{},'Days'),days.node),valid.node),section('Note',noteField.wrap),
          // Keep change puts it in the draft and starts the panel afresh (nothing left unsaved); Cancel closes it.
          proposeBtn?el('footer',{class:'rc-lp-foot'},button('Cancel',()=>d.close(),'ghost'),el('span',{class:'grow'}),proposeBtn,applyBtn||'')
            :s.editable?el('footer',{class:'rc-lp-foot'},remove,el('span',{class:'grow'}),button('Cancel',()=>d.close(),'secondary'),keepBtn):'');
        const syncCounts=()=>{if(countSection){countSection.hidden='apply' in (w.numbers||{});counts!.querySelectorAll<HTMLInputElement|HTMLSelectElement>('input,select').forEach(x=>x.disabled=!s.editable||countSection.hidden);}};
        const follow=()=>{if(!s.editable)return;for(const r of read)r();syncCounts();paint();paintKeep();reach.changed();relate();};
        syncCounts();
        paintKeep();
        for(const [node,type] of [[numbers,'input'],[numbers,'change'],[days.node,'click'],[valid.node,'change'],[valid.node,'input'],...(counts?[[counts,'change'],[counts,'input']] as const:[])] as const)node.addEventListener(type,follow);
        paint();relate();
      }});
  }
  function keep(w:RuleLine,read:(()=>void)[]){
    if(!s.editable)return;for(const r of read)r();
    const n=draft.lines.findIndex(l=>l.id===w.id);
    if(n<0&&pendingLines.get(e.id)?.id===w.id){pendingLines.delete(e.id);draft.lines.push(clone(w));s.changed();return;}
    if(n>=0&&JSON.stringify(w)!==JSON.stringify(draft.lines[n])){draft.lines[n]=clone(w);s.changed();}
  }
  /** After any change: a removed line closes the panel; otherwise its header follows the check (fields untouched). */
  const sync=()=>{if(!id||!dock)return;const l=lineOf(id);if(!l){closeDock();return;}dock.setHead(title(editing?.id===id?editing:l),status(id));};
  /** The reach the open panel shows for a line (its "85 people"), for the table's Reach column. */
  const reachOf=(lineId:string)=>id===lineId?reached?.now()?.people.size??null:null;
  return {open,sync,reachOf};
}
/** Rule pickers list active clinics, places and duty types only (the server's active-only source, maintenance): the others
 *  are hidden unless chosen, counted by the server. Read once; until it answers, every choice is listed. */
const INACTIVE_KINDS:Record<string,[keyof NonNullable<ActiveChoices['inactive_count']>,[string,string]]>={service_ids:['services',['clinic','clinics']],location_ids:['locations',['place','places']],assignment_type_ids:['assignment_types',['duty type','duty types']]};
let activeNow:ActiveChoices|null=null;
onSessionReset(()=>{activeNow=null;forgetActiveChoices();});
const loadActive=()=>{void activeChoices().then(a=>{activeNow=a;}).catch(()=>undefined);};
function withInactive(f:ConditionField):ConditionField{
  const k=INACTIVE_KINDS[f.key],count=k&&activeNow?.inactive_count?.[k[0]];if(!k||!count)return f;
  const live=new Set((activeNow![k[0]]||[]).map(r=>r.id));
  return {...f,inactive:{ids:new Set(f.choices.filter(c=>c.group!=='Tags'&&!live.has(c.value)).map(c=>c.value)),count,noun:k[1]}};
}
/** The relation row's parts (B3–B5): ✓ "Overrides line 4 for Jules Foster · Internship ≥ 1 at the same clinics", or ⚠
 *  "Does not override line 4: its minimum is Mandatory, this one Advisory. Both apply, so …" with its one fix. */
function relationParts(e:RuleCatalogueEntry,names:Namer,r:Relation,w:RuleLine,other:RuleLine,open:()=>void,fix?:()=>void):Child[]{
  const subject=countWords(w.who,names,'everyone'),keys=Object.keys(w.numbers||{}).filter(k=>other.numbers?.[k]);
  const lv=(l:RuleLine,k:string)=>(l.numbers[k]?.level??'mandatory')==='mandatory'?'Mandatory':'Advisory';
  const k=keys.find(k=>lv(w,k)!==lv(other,k));
  const v=(key:string)=>r.effective_numbers?.[key]??other.numbers[key]?.value,unit=(n:number)=>e.measures&&other.measure?measureWords(other.measure,n):n===1?ONE[e.unit||'']||e.unit||'':e.unit||'';
  // SW-10: when both apply the stricter number binds, so the broader line's number is said only where it is the stricter
  // one (a higher minimum, a lower maximum); a stricter number on this line is what binds and needs no sentence.
  const mine=(key:string)=>w.numbers?.[key]?.value,stricter=(key:'min'|'max')=>v(key)!=null&&(mine(key)==null||(key==='min'?v(key)!>mine(key)!:v(key)!<mine(key)!));
  const still=stricter('min')?`${subject} still needs ${v('min')} ${unit(v('min')!)}`:stricter('max')?`${subject} still has at most ${v('max')} ${unit(v('max')!)}`:'';
  const t=relationText(r,{subject,broader:`${treeText(other.who,names,'Everyone')} ${numbersText(other,undefined,appliesWord(e))}`.trim(),still,
    strength:k?{label:(LONG_LABELS[k]||numberLabel(e,k)).toLowerCase(),theirs:lv(other,k),mine:lv(w,k)}:null});
  const link=el('button',{type:'button',class:'rc-reach-link'},t.link);link.addEventListener('click',open);
  return [icon(t.warn?'alert':'check'),el('span',{},t.lead,link,t.tail),t.fix&&fix?button(t.fix,fix,'secondary small'):''];
}
/** "Effect"'s help: only the drafts the line's dates overlap are checked (D6). */
const effectHelp=(l:RuleLine)=>{const text=`Only drafts whose dates overlap ${lineDates(l)||'this line'} are checked. Other drafts are not affected.`;
  return el('button',{type:'button',class:'icon-button bare','aria-label':text,'data-help':'','data-tip':text},icon('help'));};
/** The markup a repaint compares: a reach link's open state and the list it opened (picker.ts mounts it beside the
 *  link, with its arrow from dom.ts tether) are not content. */
const paintedAs=(nodes:Iterable<Node>)=>[...nodes].filter(n=>!(n instanceof Element&&n.matches('.picker,.pop-arrow'))).map(n=>{
  if(!(n instanceof Element))return n.textContent;const c=n.cloneNode(true) as Element;
  for(const b of [c,...c.querySelectorAll('.rc-reach-link')])if(b.matches('.rc-reach-link')){b.setAttribute('aria-expanded','false');b.classList.remove('is-open');}
  return c.outerHTML;}).join('\u0000');
/** Repaints `box` only when its content changes: a check that changes nothing keeps the reach list a reader just
 *  opened (it lives inside `box`), where replacing the nodes would close it under their click. */
export function keepSame(box:HTMLElement,kids:(Node|string)[]){
  const next=el('span',{},...kids);if(paintedAs(box.childNodes)!==paintedAs(next.childNodes))box.replaceChildren(...next.childNodes);
}
/** One row per overlapping draft: its name and the outcome as a toned badge, or "Waiting to check · 0:14" / "Checking · 0:14" (elapsed, no ETA). */
function effectRows(rows:Effect[],none='No rota being planned overlaps its dates'):HTMLElement[]{
  if(!rows.length)return [el('li',{class:'muted'},none)];
  return rows.map(x=>{const w=effectWords(x);
    // A check that has not started (a job in the queue, or one this panel has just asked for: started_at is the asking)
    // says it waits, timed from then; only a running one reads Checking.
    const since=x.started_at||x.created_at;
    const end=w?badge(w.text,w.tone):elapsed(waitingWord(x.status)||'Checking',since?Date.parse(since):Date.now());
    if(w?.tone==='ok')end.prepend(icon('check'));
    const more=x.status==='ready'?(x.clears&&(x.new_or_worse_count||x.adds)?`Clears ${plural(x.clears,'problem')}`:'')
      :x.summary&&x.summary!==w?.text?x.summary:'';
    let problems:Child='';
    if(x.new_or_worse_count&&x.job_id){
      const jobId=x.job_id,content=el('div',{});
      const details=el('details',{},el('summary',{'aria-label':`View problems for ${x.name}`},'View problems'),content);
      const load=async()=>{const body=el('div',{},skeleton('lines','Loading problems…'));content.replaceChildren(body);
        try{const {job}=await get(`/api/jobs/${encodeURIComponent(jobId)}`);
          const findings=(job.result?.new_or_worse_findings||[]) as Row[];
          if(job.check_stale||job.result?.comparison_complete!==true||!findings.length){body.replaceChildren(note('Reopen the review for a fresh check.','warning'));return;}
          const zone=departmentTimeZone();
          body.replaceChildren(el('ul',{class:'review-findings'},...findings.map(f=>el('li',{class:'review-finding'},
            el('span',{},String(f.message),f.start&&typeof f.start==='string'?` · ${spanLabel(f.start,f.end,zone)}`:'')))));}
        catch(err){body.replaceChildren(readFailed('the problems',err,()=>void load()));}};
      details.addEventListener('toggle',()=>{if(details.open)void load();});
      problems=details;
    }
    return el('li',{},el('div',{class:'rc-effect-what'},el('span',{class:'rc-effect-name',title:x.name},x.name),
      more?el('small',{class:'muted rc-effect-summary'},more):'',problems),end);});
}
/** One of a rule's units ("1 duty", "1 person"). */
const ONE:Record<string,string>={people:'person',duties:'duty',hours:'hour',days:'day',weekends:'weekend',minutes:'minute',trainees:'trainee',units:'unit'};
/** "at most 6 duties per week", "between 2 and 5 people per session, aiming for 4", "keep off applies": a line's numbers
 *  in words, with the number the breakdown repeats. */
export function amountWords(e:RuleCatalogueEntry,l:RuleLine,inh?:Inherited,rule:Settings={}):{main:number|null;text:string;level:string|null|undefined}{
  // A number the line leaves blank is the broader line's (blank inherits); None is no number.
  const own=(k:string)=>l.numbers?.[k],n=(k:string)=>own(k)?own(k)!.value??null:inh?.[k]?.value??null;
  const min=n('min'),max=n('max'),target=n('target'),main=max??min??target,key=max!=null?'max':min!=null?'min':'target';
  const level=strengthOf(e,key,l,inh);
  if(main==null)return {main:null,level:l.numbers?.apply?strengthOf(e,'apply',l,inh):undefined,text:l.numbers?.apply?(e.modes?.includes('no_duties')?'no duties':`${e.name.toLowerCase()} applies ${goalUnit(e,l,rule)}`.trim()):`${e.name.toLowerCase()} ${numbersText(l)||'— no number yet'}`.trim()};
  // A maximum counted over a run says the run ("60 hours in any 14 days"), not the rule's name ("per period").
  const lower=e.name.toLowerCase(),unit=e.unit||'',window=key==='max'?windowText(e,l,rule):'',rest=window||(unit&&lower.startsWith(unit)?lower.slice(unit.length).trim():lower);
  // A Workload line counts its measure over its period; elapsed rest is hours between duties.
  // Team leads count leads, not people: "1 team lead", and one number when the fewest and most agree.
  const leads=Object.values(e.numbers).flat().some(x=>x.kind==='team_lead_requirement');
  const what=(v:number)=>leads?`${v} team lead${v===1?'':'s'}`:e.measures&&l.measure?`${v} ${measureWords(l.measure,v)} ${periodText(l)}`.trim():e.modes&&key==='min'?`${v} hour${v===1?'':'s'} between duties`
    :[v,v===1?ONE[unit]||unit:unit,rest].filter(x=>x!=='').join(' ');
  const range=leads&&min!=null&&min===max?what(max):min!=null&&max!=null?`between ${min} and ${what(max)}`:max!=null?`at most ${what(max)}`:min!=null?`at least ${what(min)}`:`about ${what(target!)}`;
  return {main,level,text:[range+(target!=null&&(min!=null||max!=null)?`, aiming for ${target}`:''),togetherWords(l,rule)].filter(Boolean).join(' ')};
}
const andList=(xs:string[])=>xs.length<2?xs.join(''):xs.slice(0,-1).join(', ')+' and '+xs[xs.length-1];
/** What Each splits, row by row: "6 in total across A and B, 6 at each of C" (nothing when everything counts together). */
function breakdown(tree:Conditions|null|undefined,n:number|null,names:Namer,at:string):string{
  if(n==null||!hasEach(tree))return '';
  const each=tree!.count==='each';
  const parts=tree!.conditions.filter(c=>'conditions' in c||!negativeOp(c.op)).map(c=>{
    // Absolute values by name; a relative one ("My programme") in its own words, never as an id.
    const words='conditions' in c?[]:[...literalValues(c).map(v=>valueText(names,c.field,v)),...[c.value].flat().filter(o=>hasRelative({...c,value:o} as never)).map(o=>operandText(o))];
    const text='conditions' in c?`(${treeText(c,names,'')})`:andList(words)+entriesWords(c);
    return each||c.count==='each'?`${n} ${at} each of ${text}`:'conditions' in c||words.length>1?`${n} in total across ${text}`:`${n} ${at} ${text}`;});
  return ' — '+andList(parts);
}
/** "This line says", in words, with the reach links: workload "Each of 15 people: at most 6 duties per week at 9 clinics —
 *  6 in total across …"; staffing "At least 1 person per session from R1, R2 at 3 clinics — 1 from each of R1 and R2". */
/** The line's subject from the reach count (plural()'s "15 people"): "Each of 15 people", "That person", "No one"; the
 *  link keeps its list. A filter not counted yet reads "Each of <filter>". */
const eachOf=(who:Node|string,named=''):(Node|string)[]=>{
  const m=/^(\d+) (?:person|people)$/.exec(typeof who==='string'?who:who.textContent||'');if(!m)return named?[named]:['Each of ',who];
  const n=Number(m[1]);if(n>1&&!named)return ['Each of ',who];
  // SW3-D-16: a line naming one person reads with their name whatever the count found ("No one" read as a line for nobody).
  // F-R5: the count is over the line's dates, so an empty one is "No one", never "No one today".
  const text=named||(n===1?'That person':'No one');if(typeof who==='string')return [text];who.textContent=text;return [who];};
function lineSays(e:RuleCatalogueEntry,l:RuleLine,names:Namer,links:{who:Node|string|null;where:Node|string|null},inh?:Inherited,rule:Settings={}):(Node|string)[]{
  // Where names its values while they are few (every filter said: "Physical Urgent Care · Evening shift"); the reach
  // link ("9 clinics") stands in for a long list. The Where heading always carries the link.
  const few=(t:Conditions)=>valuesOf(t).length<=3&&!t.conditions.some(c=>'conditions' in c);
  const a=amountWords(e,l,inh,rule),who=l.who?.conditions.length?links.who||treeText(l.who,names,'everyone'):'everyone',where=l.where?.conditions.length?(few(l.where)?treeText(l.where,names,''):links.where||treeText(l.where,names,'')):'all clinics';
  const at=l.where?.conditions.length?placeWord(l.where):'at';
  const levels=Object.entries({...inh,...l.numbers}).filter(([k,v])=>v.value!=null||k==='apply').map(([k])=>({key:k,level:strengthOf(e,k,l,inh)}));
  const strengths=new Set(levels.map(v=>v.level)).size>1?` (${levels.map(v=>`${(LONG_LABELS[v.key]||numberLabel(e,v.key)).toLowerCase()} ${strength(v.level).slice(2,-1)}`).join('; ')})`:strength(a.level);
  // Rest between duties: which duty types may not (or should not) follow which.
  const avoid=modeText(e,l,rule)===words(e.modes?.[2]||''),kind=avoid?'shift_succession':'forbidden_transition';
  const order={...rule[kind],...l.settings?.[kind]};
  if(e.modes&&!e.modes.includes('no_duties')&&l.numbers?.apply&&order){const types=(ids:unknown)=>((ids||[]) as string[]).map(id=>names.label('assignment_type_ids',id)).join(', ')||'any duty';
    const transition=avoid?`avoid ${types(order.from_type_ids)} followed by ${types(order.to_type_ids)} within ${order.within_days??1} day${(order.within_days??1)===1?'':'s'}`
      :`no ${types(order.from_type_ids)} followed by ${types(order.to_type_ids)} the next day`;
    a.text=l.numbers?.min?`${a.text}, and ${transition}`:transition;}
  if(['staffing','together'].includes(e.family))
    return [el('b',{},a.text.replace(/^./,c=>c.toUpperCase())),' from ',who,` ${at} `,where,breakdown(l.who,a.main,names,'from'),breakdown(l.where,a.main,names,at),when(l),strengths,'.'];
  // A line for one named person reads with their name ("Dr. A: at most 5…"), not "That person".
  const only=l.who?.conditions.length===1?l.who.conditions[0]:null;
  const named=only&&!('conditions' in only)&&only.field==='person_ids'&&!negativeOp(only.op)&&!hasRelative(only)&&literalValues(only).length===1?valueText(names,only.field,literalValues(only)[0]):'';
  return [...(l.who?.conditions.length?eachOf(who,named):['Everyone']),': ',el('b',{},a.text),` ${at} `,where,breakdown(l.where,a.main,names,at),when(l),strengths,'.'];
}
/** ", Sun–Thu 08:00–16:00, from 1 Oct" — only what narrows the line (every day, always: nothing). */
function when(l:RuleLine):string{
  const days=(l.weekdays?.length||7)<7||l.start_time&&l.start_time!=='00:00'||l.end_time&&l.end_time!=='24:00'?daysText(l):'',valid=validText(l);
  return [days,valid==='Always'?'':valid.replace(/^./,c=>c.toLowerCase())].filter(Boolean).map(x=>', '+x).join('');
}
/** " (mandatory)", " (advisory · high)", " (strength from the broader line)"; no number: nothing. */
function strength(level:string|null|undefined):string{
  return level===undefined?'':level==='mandatory'?' (mandatory)':level?` (advisory · ${level})`:' (strength from the broader line)';
}
/** Each number keeps its own strength. A target whose slots are advisory can never become mandatory. */
const mandatoryNumber=(e:RuleCatalogueEntry,k:string)=>e.levels.includes('mandatory')&&!(e.numbers[k]||[]).every(s=>s.advisory);
/** A blank strength with no broader line to take it from: what the server applies (rule_lines._levels' fallback). */
const fallbackLevel=(e:RuleCatalogueEntry,k:string)=>mandatoryNumber(e,k)?'mandatory':'medium';
/** A number's strength as it applies: its own, else the broader line's, else the fallback (null: the broader line's is blank too). */
function strengthOf(e:RuleCatalogueEntry,k:string,line:RuleLine,inh?:Inherited):string|null{
  return displayedStrength(line,k,line.numbers?.[k]?.level??(inh?.[k]?inh[k].level:fallbackLevel(e,k))).level;
}
const hardSafetyNumber=(e:RuleCatalogueEntry,k:string,line?:RuleLine)=>{
  const slots=(e.numbers[k]||[]) as Array<RuleCatalogueEntry['numbers'][string][number]&{mandatory_reason?:string}>;
  const chosen=line&&slots.find(s=>s.kind in (line.settings||{})),candidates=chosen?[chosen]:slots;
  return candidates.every(s=>s.mandatory_reason)?candidates[0]?.mandatory_reason:undefined;
};
const defaultNumberLevel=(e:RuleCatalogueEntry,k:string)=>hardSafetyNumber(e,k)?'mandatory':e.levels.includes('medium')?'medium':mandatoryNumber(e,k)?'mandatory':e.levels[0];
/** K1f Numbers: one row of fields (Minimum | Maximum | Target, or the rule's own numbers), each with its strength as a
 *  "!" chip beside it, the table's own notation: solid "!" mandatory, outline "!" advisory, "↰" taken from the broader
 *  line. The chip opens the shared menu of the strengths that number may have (Mandatory, Advisory · low … critical,
 *  Inherit), the current one ticked; a level never touched is kept as it is. "None" (don't inherit) shows only where a
 *  number is or could be inherited. */
const LONG_LABELS:Record<string,string>={min:'Minimum',max:'Maximum',target:'Target'};
/** A number a line sets to None: "No maximum" (its None chip, a cleared personal number). */
export const noNumberText=(label:string)=>'No '+label.toLowerCase();
const INHERIT='inherit';
export function numberFields(e:RuleCatalogueEntry,w:RuleLine,editable:boolean,read:(()=>void)[],inh:Inherited|undefined,from:(id:string)=>string,
  /** D8: a new number's strength from the broader line with the same where (else the rule's default). */broader?:(k:string)=>string|null):HTMLElement{
  const cells=Object.keys(e.numbers).map(k=>{
    const own=w.numbers?.[k],got=inh?.[k],label=numberLabel(e,k,true);
    // The strengths this number may have: the rule's levels (mandatory only where its slots allow), and Inherit.
    const options=[...e.levels.filter(v=>v!=='mandatory'||mandatoryNumber(e,k)).map(v=>({key:v,label:v==='mandatory'?'Mandatory':v==='off'?'Off · not applied':`Advisory · ${v}`})),
      ...(tier(w)>1||own?.level===null?[{key:INHERIT,label:'Same as the broader line ↰'}]:[])];
    const safety=hardSafetyNumber(e,k,w),taken=broader?.(k);
    let level:string|null=safety?'mandatory':own?own.level??null:taken&&options.some(o=>o.key===taken)?taken:defaultNumberLevel(e,k);
    // A reader who cannot edit gets the strength as a fact (text), never a disabled menu button.
    const chip=editable?el('button',{type:'button',class:'rc-strength',...popup('menu')}):el('span',{class:'rc-strength'});
    const paintChip=()=>{const m=level==='mandatory',i=level===null;chip.dataset.level=i?'inherit':m?'mandatory':'advisory';
      const bare=i&&!got,lv=bare?fallbackLevel(e,k):level,what=i&&!bare?'strength from the broader line':lv==='mandatory'?'mandatory':lv==='off'?'off, not applied':`advisory · ${lv}`;
      const text=i?'↰ '+levelWords(bare?lv:got?.level??null):levelWords(lv),name=`${label}: ${what}`;
      if(editable){chip.textContent=text;chip.setAttribute("aria-label",name);}
      else chip.replaceChildren(el('span',{'aria-hidden':'true'},text),el('span',{class:'sr-only'},name));
      chip.dataset.tip=what[0].toUpperCase()+what.slice(1);};
    if(editable)chip.addEventListener('click',()=>popover(chip,(panel,close)=>panel.append(...optionMenu(`${label} strength`,options,level??INHERIT,key=>{
      close();level=key===INHERIT?null:key;paintChip();chip.dispatchEvent(new Event('change',{bubbles:true}));chip.focus();})),{label:`${label} strength`}));
    paintChip();
    // Read-only: a strength only beside a number the line has (an empty number has no strength to read).
    const hasNumber=editable||!!own&&(k==='apply'||own.value!=null);
    const strength=!hasNumber?null:safety?levelBadge('mandatory',safety):options.length>1?chip:null;
    if(k==='apply'){
      const c=check(label,!!own);c.input.disabled=!editable||!!w.source_bindings?.[k];
      return {node:el('div',{class:'rc-lp-num rc-lp-num-apply'},el('div',{class:'rc-lp-num-box'},c.wrap,strength)),read:()=>c.input.checked?{value:null,level}:null,k};
    }
    const f=field(label,'number',own?.value==null?'':String(own.value),{step:'any',placeholder:got?.value!=null?`${shown(got)}↰`:'—'});f.input.readOnly=!editable;
    if(got?.value!=null)f.input.title=`↰ ${shown(got)} from ${from(got.from)}`;
    const box=el('div',{class:'rc-lp-num-box'});f.input.before(box);box.append(f.input,...(strength?[strength]:[]));
    let none=!!own&&own.value==null;
    const inherits=got?.value!=null;
    const toggle=el('button',{type:'button',class:'chip-toggle rc-none','aria-pressed':String(none),disabled:!editable,'data-tip':inherits?`${noNumberText(label)} for this line: don't take ${shown(got!)}`:`${noNumberText(label)} for this line`},'None');
    // A strength belongs to a number: with none typed (and none inherited) the chip waits, its place kept.
    const sync=()=>{toggle.setAttribute('aria-pressed',String(none));f.input.disabled=none;if(none)f.input.value='';strength?.classList.toggle('is-idle',!none&&f.input.value===''&&!inherits);};
    f.input.addEventListener('input',sync);
    toggle.addEventListener('click',()=>{none=!none;sync();});sync();
    if((own||inherits)&&(editable||none))f.wrap.append(toggle);
    f.wrap.classList.add('rc-lp-num');
    return {node:f.wrap,read:()=>none?{value:null,level}:f.input.value===''?null:{value:Number(f.input.value),level},k};
  });
  read.push(()=>{const numbers:RuleLine['numbers']={};for(const x of cells){const v=x.read();if(v)numbers[x.k]=v as RuleLine['numbers'][string];
    else if(w.source_bindings?.[x.k])numbers[x.k]={value:null,level:w.numbers[x.k]?.level??null};}w.numbers=numbers;});
  return el('div',{class:'rc-lp-numbers'},...cells.map(x=>x.node));
}
export type Check=Checked&{duplicates:Row[];overlaps:Row[];warnings:Row[];issues:Row[];line_replaces?:Record<string,RuleLineReplacement[]>;
  /** Lines whose counting period ends before they stop applying (rule_lines.counting_attention). */counting_attention?:(CountingEnd&{line_id:string})[];inherited?:Record<string,Inherited>;line_specificity?:Record<string,number>;
  /** Who and where each line reaches today (rule_map.reach_details; rule_line_reach.ts reads it). */reach_details?:{lines:Row[]}};
export const ackKey=(x:Row)=>`${x.line_id}>${(x.winner_ids as string[]).join(',')}`;
/** The check in words (the server numbers lines in the order they were added, which a sorted table does not show). */
export function checkWords(e:RuleCatalogueEntry,lines:()=>RuleLine[],names:Namer){
  const said=(id:string)=>{const l=lines().find(x=>x.id===id);return l?`“${lineWords(e,l,names)}”`:'A line';};
  // A line in brief, as the relation row says the broader one (who and number, no strength); in full where two read alike.
  const brief=(l:RuleLine)=>`${treeText(l.who,names,'Everyone',4)} ${numberParts(l,undefined,appliesWord(e)).map(x=>x.text).join('')}`.trim();
  const short=(id:string)=>{const all=lines(),l=all.find(x=>x.id===id);if(!l)return 'A line';const b=brief(l);return all.some(x=>x!==l&&brief(x)===b)?said(id):b;};
  return {same:(d:Row)=>`${said(d.line_ids[0])} is there twice: what it counts, who, where, days and dates overlap`,
    over:(x:Row)=>`${said(x.line_id)} gives way to ${(x.winner_ids as string[]).map(said).join(' and ')}${(x.numbers as string[]|undefined)?.length?` on its ${(x.numbers as string[]).join(' and ')}`:''} for ${plural(x.people,'person','people')} today`,
    /** "Everyone ≤ 5 gives way to Senior grade ≤ 6 for 11 people": an overlap in one short line, as information. */
    gives:(x:Row)=>`${short(x.line_id)} gives way to ${(x.winner_ids as string[]).map(short).join(' and ')} for ${plural(x.people,'person','people')}`,
    warn:(w:Row)=>w.kind==='pool_shortfall'?String(w.message):w.kind==='both'?`${said(w.other_id)} and ${said(w.line_id)} both apply to ${plural(w.people,'person','people')} today: neither is more specific, so each of their numbers holds`
      :w.kind==='inherits'?`${said(w.line_id)} inherits ${Object.entries(w.inherited as Record<string,number>).map(([k,v])=>`${k} ${v}`).join(' and ')} from ${said(w.other_id)}, which contradicts its own numbers: give it None`
      :`${said(w.line_id)} is narrower but weaker than ${said(w.other_id)}: safety lines all apply, so the broader one still binds`};
}
/** The check's warnings by kind, as a line's issue title. */
const WARNING_TITLES:Record<string,string>={both:'Both apply',weaker:'Narrower but weaker',inherits:'Inherits a conflict',pool_shortfall:'Rule cannot be met'};
/** The line an issue names ("definition.rule.lines.3…", "Line 4: …"), or -1 for the rule as a whole. */
const lineOfIssue=(x:Row)=>{const m=/lines\.(\d+)/.exec(String(x.field||''))||/^Line (\d+):/.exec(String(x.message||''));return m?Number(m[1])-(m[0].startsWith('Line')?1:0):-1;};
/** What the server says about these lines before anything is saved (duplicates, overlaps, warnings, issues). */
type Body={on:boolean;lines:RuleLine[];settings?:Settings;balance?:boolean;seen?:string[]};
/** A check's issues on settings under `prefix` (rule-level or one line's), as "kind.field" for the Settings popover. */
const settingIssues=(r:Check,prefix:string):SettingIssue[]=>r.issues.filter(x=>String(x.field||'').startsWith(prefix))
  .map(x=>({field:String(x.field).slice(prefix.length),message:String(x.message)}));
// A check asked while the same one is on its way, or just answered (1 s), shares it; later ones ask again.
const checked=new Map<string,{settled:number;answer:Promise<Check>}>();
onSessionReset(()=>checked.clear());
/** The server's check of a rule's lines. The same lines asked together share one request (a Keep change used to send
 *  three at once: the page, the line's reach and its counting); a failed check is not kept. `signal`: the caller ignores
 *  a late answer itself, so one aborted caller never cancels the request another still waits for. */
export const checkLines=(e:Entry,body:Body,_signal?:AbortSignal,fresh=false)=>{
  const sent=checkBody(e,body),key=e.id+' '+sent,now=Date.now();
  for(const [k,v] of checked)if(v.settled&&now-v.settled>1000)checked.delete(k);
  // `fresh`: a check that gates a proposal never takes a settled answer (another rule may have changed meanwhile).
  let hit=checked.get(key);if(fresh&&hit?.settled)hit=undefined;
  if(!hit){const mine:{settled:number;answer:Promise<Check>}={settled:0,answer:request<Check>(`/api/rules/catalogue/${e.id}/check`,{method:'POST',body:sent})};
    checked.set(key,mine);hit=mine;
    mine.answer.then(()=>{mine.settled=Date.now();},()=>{if(checked.get(key)===mine)checked.delete(key);});}
  return hit.answer.then(r=>{learn(body.lines,r.line_specificity);return r;});
};
const checkBody=(e:Entry,body:Body)=>JSON.stringify({catalogue_version:e.catalogue_version,on:body.on,lines:wire(body.lines,linesOf(e)),settings:body.settings??settingsOf(e),balance_between_people:body.balance??balanceOf(e)});
/** Withdraw or decline one proposal, with Undo (it comes back waiting for approval). A catalogue rule's Undo is the
 *  shared one (api.ts); a legacy rule's is offered here, so the legacy page's own declines (inside its corrections and
 *  their Undo) still end its undo batch instead of offering a second Undo. */
async function declineProposal(p:Row,reason:string,said:string,after:()=>Promise<unknown>){
  const rec=(await command<{record:Row}>('policy.decline',{reason},p.id,p.revision)).record;
  notify(said,'success',rec&&rec.kind!=='catalogue'?{label:'Undo',run:async()=>{
    await command('policy.restore_proposal',{reason:'Undo'},rec.id,rec.revision);notify('The proposal is waiting for approval again.','info');await after();}}:undefined);
  await after();
}
/** The server's check of every rule about to be proposed. A check that cannot run (offline, refused, rate-limited,
 *  server error) says so with Retry and returns null: nothing was proposed. */
export async function checkBeforeProposing(items:{e:Entry;body:Body}[],retry:()=>unknown):Promise<Check[]|null>{
  try{return await Promise.all(items.map(x=>checkLines(x.e,x.body,undefined,true)));}
  catch(err){if(sessionChanged(err))throw err;
    notify(`Nothing was proposed: the changes could not be checked. ${(err as Error).message}`,'error',{label:'Retry',run:retry},err);return null;}
}
/** The rule's settings by old kind: the version in force, else the entry's defaults. */
export const settingsOf=(e:Entry):Settings=>e.rule?.definition.rule.settings??e.default_settings??{};
/** Balance between people (on by default): everyone under the rule is compared together for fairness; off, each line
 *  on its own. It changes quota targets only, so only those rules show it (Fair share has its Groups setting). */
export const balanceOf=(e:Entry):boolean=>e.rule?.definition.balance_between_people??true;
export const balances=(e:RuleCatalogueEntry)=>Object.values(e.numbers).flat().some(x=>x.kind==='block_quota');
export const balanceWords=(on:boolean)=>on?'On — everyone under this rule is compared together':'Off — each line is balanced on its own';

// ---- A rule's settings (by old kind): fields from the rule schemas the server publishes, one popover ----
let schema:Promise<Row>|null=null;
const capabilities=(read:typeof get=get)=>schema??=read<Row>('/api/policies/capabilities').catch(err=>{schema=null;throw err;});
const definitionSchema=()=>capabilities().then(r=>r.definition_schema);
/** Never in Settings: numbers and line dates (lines carry them), per-person amounts and supervisor lists (their own editors). */
const NOT_SETTINGS=new Set(['starts_on','ends_on','maximum_amount','target_amount','target_strength','weekdays','start_time','end_time','person_amounts','supervisor_ids']);
/** A Workload line's measure and period stand for its counting fields, so Settings never shows them twice. */
/** `forLine`: a quota line's own counting dates (settings.block_quota.starts_on / ends_on, apart from its Valid dates)
 *  are its settings too; a Workload line's are its Counts (countFields), so only entries without measures show them. */
const LINE_COUNTING=['starts_on','ends_on'];
const settingFields=(kind:string,e:RuleCatalogueEntry,forLine=false)=>(e.line_settings[kind]||[]).filter(f=>(!NOT_SETTINGS.has(f)||(forLine&&kind==='block_quota'||kind==='balance_goal')&&LINE_COUNTING.includes(f))&&!(e.measures&&COUNTED.includes(f)));
/** A line sets its own value for something Settings shows (a Workload line's counting fields are its measure and period). */
const ownSettings=(l:RuleLine,e:RuleCatalogueEntry)=>Object.entries(l.settings||{}).some(([kind,v])=>Object.keys(v||{}).some(f=>settingFields(kind,e).includes(f)));
/** A setting's label in plain words, and (SETTING_HELP) one line on what it does where the label alone is not enough. */
const SETTING_WORDS:Record<string,string>={include_overtime:'Counts overtime',overtime:'Overtime',window:'Counted over',window_days:'Days in the run',window_alignment:'Periods start',
  metric:'Counts',weekend_start:'Weekend starts on',weekend_days:'Days in a weekend',overtime_only:'Only overtime counts',group_by:'The limit applies',type_order:'Duty order',from_type_ids:'After these duty types',
  to_type_ids:'Not these duty types',within_days:'Within days',series_length:'After a run of',rest_days:'Days off after',from_location_id:'From place',to_location_id:'To place',person_ids:'People',
  when_person_ids:'Only when these people are on duty',mode:'Together or apart',level:'On the same',aggregation:'Counted',assignment_type_ids:'Duty types',service_ids:'Clinics',trainee_category_ids:'Trainee ranks',
  volunteer_required:'Only when they volunteer',overflow_maximum:'Extra places',overflow_category_ids:'Ranks that may use extra places',holder:'Across',maximum_gap:'Maximum gap',required_policy_ids:'Under the limits of',shape:'Shape',participation:'Who counts',rounding:'Round a share',
  prorate_department_days:'Adjust for time in the department',reference_days:'Days for the full number',leave_buffer_days:'Days around leave',period:'Period',period_days:'Days back',aim:'Aim',share_by:'Share by',pairing:'A pair counts when',partners:'Partners',
  priority:'Priority',session_priority:'Session order',overtime_priority:'Overtime order',supervisor_relationship:'Supervisor',balance_by:'Balance',balance_weight:'Balance importance',
  balance_window:'Balance over',balance_window_days:'Days in the run',balance_target_mode:'Mode to aim for',balance_target_share:'Share to aim for (0–1)',
  share_adjustment:'Share of a fair amount',tolerance_percent:'Leeway around the aim (%)',tolerance_units:'Leeway of at least',
  credit_conditions:'Counts only alongside others',credit_minimum_others:'Others needed on the duty',selection_order:'Most preferred first',selection_period:'Choose the group for',
  overtime_extension_days:'Extra days with overtime',distribution:'Share out',comparison:'Groups',period_months:'Months back',weight:'Weight',optimize_greedy:'Also used by the basic scheduler',within:'Duties in the share',
  target_share:'Share to aim for (0–1)',distribution_period:'Spread over',continuity:'Leads through the duty'};
/** A field that means something different in two kinds of one rule reads differently in each (QA, 13 Oct 2026: a
 *  quota's counting block and a limit's run were both "Days in the run", side by side with different values). */
const KIND_SETTING_WORDS:Record<string,string>={'block_quota.window_days':'Days in each counting block','block_quota.window_alignment':'Counting blocks start',
  'block_quota.starts_on':'Counting starts','block_quota.ends_on':'Counting ends (last day)','block_quota.window_anchor_on':'Blocks start on','block_quota.repeat':'Repeats while the line is valid',
  'max_consecutive_days.include_overtime':'Overtime days count toward the run','balance_goal.starts_on':'From','balance_goal.ends_on':'Until (included)'};
/** `e`: the catalogue entry, whose own labels (`setting_labels`) come first. */
export const settingWords=(kind:string,f:string,e?:Pick<RuleCatalogueEntry,'setting_labels'>)=>e?.setting_labels?.[f]||KIND_SETTING_WORDS[`${kind}.${f}`]||SETTING_WORDS[f]||words(f);
const SETTING_HELP:Record<string,string>={continuity:'The same lead: one person leads from start to end. Hand over: people present for part of the duty may each lead their part.',repeat:'With no counting end, counting follows the line’s Valid dates.',window_anchor_on:'Blocks repeat from this date. Empty: from the first counting date.',prorate_department_days:'Someone in the department for part of the period gets a matching share of the number.',
  reference_days:'Someone in the department this many days gets the whole number.',rounding:'How a part share becomes a whole number.',
  window_days:'Used when counted over any run of days.',balance_window_days:'Used when balanced over any run of days.',period_days:'Days or months back, not both.',period_months:'Days or months back, not both.',
  leave_buffer_days:'Also keep this many days free before and after leave.',overflow_maximum:'Places allowed above the number, for the ranks below.',
  series_length:'Days in a row of these duties before the days off.',priority:'Higher is chosen first for a session.',session_priority:'Higher fills matching sessions earlier.',
  overtime_priority:'Higher is offered overtime first.',volunteer_required:'On: only within the person’s approved Available for overtime request. Off: whenever they are free.',balance_weight:'How much the balance counts against other preferences.',balance_target_share:'For example 0.5: half the duties in that mode.',
  share_adjustment:'A percentage of a fair share (50% is half), or an amount more or less than it in the rule’s unit.',
  tolerance_percent:'Being this close to the aim costs nothing.',tolerance_units:'The leeway is never smaller than this, in the rule’s unit. Empty: 1 (4 for hours).',
  credit_conditions:'These people count only when enough others who always count are on the same duty.',credit_minimum_others:'Set together with the people above; 0 turns it off.',
  selection_order:'Only the first group with someone available counts. None: everyone counts.',selection_period:'Each week keeps one group for all its duties.',
  overtime_extension_days:'With overtime, a run may go this many days past the limit. 0 turns it off.',
  weight:'How much this goal counts against other goals; 1 is usual.',optimize_greedy:'The basic scheduler weighs this goal too.',
  within:'Needed for a share; the duties each person should do this share of.',partners:'Needed when it counts duties shared with a partner: who the partners are (for example consultants).',target_share:'For example 0.5: half of each person’s duties.',distribution_period:'Used for spread and pair limits.'};
/** When a setting ("kind.field") or one of its choices ("kind.field=value") applies, by the rule's other settings
 *  (field → allowed values). The contract says it (`x-applies-to` on a field, `x-choice-metrics` on its choices, in
 *  /api/policies/capabilities); hidden otherwise, and a hidden setting is not kept. `need`: required while shown.
 *  shortcut: the Fair share entries below copy BalanceGoal `coherent` checks the contract does not mark yet; delete each
 *  once it does (the contract's marks win). The server's check at Keep stays the authority. */
type Applies={[field:string]:string[]|true|undefined;need?:true};
const APPLIES_UNMARKED:Record<string,Applies>={'balance_goal.period_days':{period:['rolling']},
  'balance_goal.starts_on':{period:['custom','since'],need:true},'balance_goal.ends_on':{period:['custom'],need:true},'balance_goal.comparison':{distribution:['people']},
  'balance_goal.within':{distribution:['share']},'balance_goal.target_share':{distribution:['share']},'balance_goal.distribution_period':{distribution:['pair_limit']},
  'balance_goal.distribution=share':{metric:['duties']},'balance_goal.distribution=period_spread':{metric:['duties']},'balance_goal.distribution=pair_limit':{metric:['pair_exposure']},
  'balance_goal.aim=min_per_pair':{metric:['pair_exposure']},'balance_goal.aim=workload_spread':{metric:['workload']}};
/** The contract's applicability marks for one kind's fields, as APPLIES_UNMARKED keys. */
const appliesMarked=(kind:string,props:Record<string,Row>):Record<string,Applies>=>Object.fromEntries(Object.entries(props).flatMap(([f,p])=>[
  ...(p['x-applies-to']?[[`${kind}.${f}`,p['x-applies-to']]]:[]),
  ...Object.entries((p['x-choice-metrics']||{}) as Record<string,string[]>).map(([v,metrics])=>[`${kind}.${f}=${v}`,{metric:metrics}])]));
/** Engine kinds as plain section headings: shown only when a rule's Settings has more than one. */
const KIND_WORDS:Record<string,string>={block_quota:'Counts over dates or months',max_duties:'Duties per day, week or run of days',max_hours:'Hours per day, week or run of days',
  restriction:'No duties',minimum_cover:'Fewest people',maximum_participants:'Most people',supervision_limit:'Supervision',spread_evenly:'Spread evenly',max_consecutive_weekends:'Weekends in a row',weekend_shape:'Weekend shape',
  shift_succession:'Duty order',forbidden_transition:'A duty not allowed after another',travel_buffer:'Travel time',together_apart:'Together or apart',allocation_preference:'Who is chosen first',balance_goal:'Fair share',min_rest:'Rest'};
/** A setting's choices in plain words (the stored values are engine names: "assignment", "service_group"). */
const CHOICE_WORDS:Record<string,Record<string,string>>={continuity:{whole_duty:'The same lead for the whole duty',continuous:'A lead at every moment (may hand over)'},aggregation:{assignment:'Each session on its own',service:'Across the clinic',service_group:'Across the clinic group'},
  group_by:{all:'To all duties together',supervisor:'Separately with each supervisor',weekday:'Separately for each weekday'},
  overtime:{include:'Counted',exclude:'Not counted',only:'Only overtime is counted'},rounding:{down:'Down',nearest:'To the nearest',up:'Up'},
  window:{day:'A day',week:'A calendar week',rolling:'Any run of days'},window_alignment:{rolling:'On any day (rolling)',fixed:'Every few days from the first date',calendar_week:'Each calendar week',calendar_month:'Each calendar month'},
  metric:METRIC_WORDS,
  shape:{complete:'The whole weekend or none of it',identical_types:'The same duty type each day'},mode:{together:'Together',apart:'Apart'},
  supervisor_relationship:{any:'Any qualified supervisor',mentor:'Their assigned mentor'},
  balance_by:{none:'Nothing',duties:'Duties',supervisor:'Time with each supervisor',supervisor_group:'Time with the chosen supervisors',service_mode:'In-person and virtual mix'},
  balance_window:{week:'A calendar week',rolling:'Any run of days',rota:'The rota'},balance_target_mode:{physical:'In person',virtual:'Virtual'},
  pairing:{present:'They share a session',supervised:'One supervises the other'},
  period:{rota:'This rota',rolling:'The last few days or months',since:'Since a date',all_history:'All history',month:'Calendar month',quarter:'Calendar quarter',year:'Calendar year',block:'Each person’s rotation block',custom:'Chosen dates'},
  aim:{equal_share:'An equal share',target:'Each person’s target',min_per_pair:'At least once per pair',max_min:'Raise the lowest',workload_spread:'An even workload spread'},share_by:{available_days:'Days each person is available',equal:'Equally',offered:'Overtime each person offered',supervisable_sessions:'Sessions each person can supervise'},
  distribution:{people:'Evenly between people',share:'A share of each person’s duties',period_spread:'Evenly across weeks',pair_limit:'A limit per pair'},
  distribution_period:{week:'Each week',rota:'The whole rota'},selection_period:{duty:'Each duty',week:'Each week'}};
/** A setting that is a condition tree (or an ordered list of them): edited with the shared condition builder. */
const isTree=(p:Record<string,any>)=>[p,...(p.anyOf||[])].some((x:Record<string,any>)=>String(x.$ref||'').endsWith('/Conditions'));
const isTreeList=(p:Record<string,any>)=>p.type==='array'&&String(p.items?.$ref||'').endsWith('/Conditions');
/** Which conditions a tree setting may use: a share picks duties and people; the others pick people (schemas LEAD_FIELDS). */
const treeFields=(ctx:Context,f:string)=>ruleConditionFields(ctx).filter(x=>f==='within'||!SESSION_FIELDS.includes(x.key));
/** An ordered list of condition trees ("most preferred first"): one shared builder per choice, numbered; + Add next choice. */
function orderedTrees(label:string,fields:ConditionField[],given:Conditions[]){
  const items=given.map(t=>conditionBuilder(fields,t,{allOf:true}));
  const list=el('ol',{class:'rc-tree-list'});
  const draw=()=>list.replaceChildren(...items.map((b,i)=>el('li',{class:'rc-tree-item'},el('span',{class:'rc-tree-rank','aria-hidden':'true'},String(i+1)),b.root,
    iconButton(`Remove choice ${i+1}`,'trash',()=>{items.splice(i,1);draw();},{class:'bare'}))));
  const add=button('+ Add next choice',()=>{items.push(conditionBuilder(fields,null,{allOf:true}));draw();},'',{class:'link-button'});
  draw();
  return {wrap:el('div',{class:'field rc-tree-field',role:'group','aria-label':label},el('span',{class:'rc-tree-label'},label),list,add),
    read:()=>items.map(b=>b.read()).filter((t):t is Conditions=>!!t)};
}
/** An empty list that means "all of them" (a filter), not "none". */
const ALL_WHEN_EMPTY:Record<string,string>={assignment_type_ids:'All duty types',service_ids:'All clinics'};
/** A list setting in words: the names it picks, or what an empty list means (Settings and the line panel say it alike). */
const idWords=(ctx:Context,f:string,ids:unknown)=>{const rows=ctx.catalogues[listFor(f)]||[];
  return ((ids||[]) as string[]).map(id=>{const r=rows.find(x=>x.id===id);return r?labelOf(r):id;}).join(', ')||ALL_WHEN_EMPTY[f]||'None';};
/** The catalogue's own option words first (setting_option_labels: Groups, Count leaders), then the editor's. */
const OPTION_WORDS:Record<string,Record<string,string>>=Object.assign({},...RULE_CATALOGUE_V2.map(e=>e.setting_option_labels||{}));
const choiceWords=(f:string,v:string)=>OPTION_WORDS[f]?.[v]||CHOICE_WORDS[f]?.[v]||words(v);
const words=(s:string)=>s.replaceAll('_',' ').replace(/^./,c=>c.toUpperCase());
/** A Fair share line's share in words: "150% of a fair share", "2 more than a fair share". */
/** A setting's value in words (a condition tree as its sentence, dates, ids as names, choices as their words). */
function settingValue(ctx:Context,names:Namer,f:string,x:unknown):string{
  const tree=(t:unknown)=>conditionsText(t as Conditions,names.label)||'—';
  return Array.isArray(x)&&x.some(v=>v&&typeof v==='object')?x.map((t,i)=>`${i+1}. ${tree(t)}`).join(' '):
    f==='share_adjustment'&&x&&typeof x==='object'?shareWords(x as {mode?:string;value?:number}):typeof x==='string'&&/^\d{4}-\d{2}-\d{2}$/.test(x)?formatDate(f==='ends_on'?dayBefore(x):x):x&&typeof x==='object'&&!Array.isArray(x)?tree(x):Array.isArray(x)?idWords(ctx,f,x):x===null||x===undefined?'None':x===''?'—':typeof x==='boolean'?(x?'Yes':'No'):choiceWords(f,String(x));
}
/** A rule's settings change, one row per setting: what it was and what it becomes (RU-22: never just "Settings changed"). */
function settingsChange(ctx:Context,names:Namer,before:Settings,after:Settings):HTMLElement|null{
  const rows=[...new Set([...Object.keys(before),...Object.keys(after)])].flatMap(kind=>[...new Set([...Object.keys(before[kind]||{}),...Object.keys(after[kind]||{})])]
    .filter(f=>JSON.stringify(before[kind]?.[f]??null)!==JSON.stringify(after[kind]?.[f]??null)).map(f=>({kind,f})));
  if(!rows.length)return null;
  return el('table',{class:'rc-settings-diff'},el('thead',{},el('tr',{},el('th',{},'Setting'),el('th',{},'Was'),el('th',{},'Now'))),
    el('tbody',{},...rows.map(({kind,f})=>el('tr',{},el('td',{},settingWords(kind,f)),el('td',{class:'muted'},settingValue(ctx,names,f,before[kind]?.[f])),el('td',{},settingValue(ctx,names,f,after[kind]?.[f]))))));
}
const shareWords=(d:{mode?:string;value?:number})=>(d.mode||'percent')==='percent'?`${d.value??100}% of a fair share`
  :`${Math.abs(d.value??0)} ${(d.value??0)<0?'less':'more'} than a fair share`;
/** Which list an id field picks from (people, duty types, places, ranks, clinics), by its name. */
const listFor=(field:string)=>/person/.test(field)?'people':/type_ids|type_order/.test(field)?'assignment_types':/location/.test(field)?'locations':/category/.test(field)?'categories':/service/.test(field)?'services':'';
/** The settings the department must choose that are still empty. A line's own settings count for that line (the
 *  server merges the rule's settings under each line's: packages/domain/rule_lines.py `_settings`), so a rule whose
 *  lines all carry them (a converted "Not after" or "Companion cover") needs nothing at the rule level. */
export const missingNeeds=(e:Entry,settings:Settings,lines:RuleLine[]=[])=>{
  const empty=(v:unknown)=>v==null||v===''||Array.isArray(v)&&!v.length;
  return Object.entries(e.needs||{}).flatMap(([kind,fields])=>fields.filter(f=>empty(settings[kind]?.[f])&&(!lines.length||lines.some(l=>empty(l.settings?.[kind]?.[f]))))
    .map(f=>settingWords(kind,f,e)));};
/** `balance`: the rule's Balance between people (the rule's own Settings; a line's have none). */
/** `validate`: the server's rule check of these settings, as `{field: "kind.field", message}` issues; Keep shows each
 *  beside its setting (a pair set halfway, a share without its duties) and keeps the popover open. */
export type SettingIssue={field:string;message:string};
export async function settingsPopover(ctx:Context,e:Entry,anchor:HTMLElement,current:Settings,editable:boolean,keep:(value:Settings,balance?:boolean)=>void,overriding=0,balance?:boolean,
  validate?:(value:Settings)=>Promise<SettingIssue[]>,forLine=false,inherited:Settings={}){
  const defs=await definitionSchema(),mapping=defs.properties.rule.discriminator.mapping as Record<string,string>;
  const read:(()=>[string,string,unknown])[]=[],slots=new Map<string,HTMLElement>(),readers=new Map<string,()=>[string,string,unknown]>();
  const kinds=Object.keys(e.line_settings).filter(k=>settingFields(k,e,forLine).length);
  const section=(kind:string)=>{
    const props=(defs.$defs[mapping[kind]?.split('/').pop()||'']?.properties||{}) as Settings,now=current[kind]||{},need=e.needs?.[kind]||[];
    const rows=settingFields(kind,e,forLine).flatMap(f=>{const reads=read.length,out=(():(HTMLElement|string)[]=>{
      const p=props[f];if(!p)return [];
      // A nullable number (Days around leave) is {anyOf:[{type:'integer'},{type:'null'}]}.
      const type=p.type??p.anyOf?.find((x:Row)=>x.type&&x.type!=='null')?.type;
      const label=settingWords(kind,f,e)+(need.includes(f)?' (needed)':''),value=now[f],enumOf=p.enum||p.anyOf?.find((x:Row)=>x.enum)?.enum,help=e.setting_help?.[f]||SETTING_HELP[f];
      const list=listFor(f),choices=list?(ctx.catalogues[list]||[]).map(r=>({value:r.id,label:list==='people'?fullNameOf(r):labelOf(r as Row)})):[];
      const usual=p.default!=null?' (usual)':'';
      // An explicit empty (`null` or `[]`) stays explicit when left empty; absent inherits. A line says which with lineChoice.
      const cleared=!forLine&&(value===null||Array.isArray(value)&&!value.length)?value:undefined;
      // What "None for this line" stores: [] for a list, null where the schema allows it, else no such choice.
      const noneValue=type==='array'||isTreeList(p)?[]:(p.anyOf||[]).some((x:Row)=>x.type==='null')?null:undefined;
      const lineFact=!forLine?null:value===undefined?'Same as rule':noneValue!==undefined&&JSON.stringify(value)===JSON.stringify(noneValue)?ALL_WHEN_EMPTY[f]||'None for this line':null;
      // A line's setting is "Same as rule" (absent), "None for this line" (null / []) or its own value: what the
      // control shows is what Keep saves. Emptying the own value goes back to the rule's, never a silent "none".
      const own=(wrap:HTMLElement)=>{if(!forLine)return wrap;
        const r=read.pop()!,id='ls-'+Math.random().toString(36).slice(2);
        const names:[string,string][]=[['inherit','Same as rule'],...(noneValue!==undefined?[['none',ALL_WHEN_EMPTY[f]||'None for this line'] as [string,string]]:[]),['own','Own value']];
        let mode=value===undefined?'inherit':lineFact?'none':'own';
        const choice=segmented(label,names,mode,k=>{mode=k;wrap.hidden=k!=='own';},{labelledBy:id}),mark=()=>choice.set(mode);
        const sync=()=>{if(!wrap.hidden){mode=r()[2]===undefined?'inherit':'own';mark();}};
        for(const t of ['input','change','click'])wrap.addEventListener(t,()=>queueMicrotask(sync));
        wrap.hidden=mode!=='own';
        wrap.querySelector(':scope>label,:scope>.rc-tree-label,.check-text>span:first-child')?.classList.add('sr-only');
        read.push(()=>{sync();return [kind,f,mode==='own'?r()[2]:mode==='none'?noneValue:undefined];});
        return el('div',{class:'field rc-tree-field rc-line-setting'},el('span',{class:'rc-tree-label',id},label),
          choice,wrap);};
      const withHelp=(wrap:HTMLElement)=>{if(help)wrap.append(el('small',{class:'hint'},help));return [own(wrap)];};
      // Condition trees: the shared builder (read-only roles read its facts).
      if(isTree(p)||isTreeList(p)){
        const fields=treeFields(ctx,f),trees=(isTreeList(p)?value||[]:value?[value]:[]) as Conditions[];
        if(!editable)return [el('div',{class:'rc-setting-fact'},el('span',{class:'muted'},label),
          lineFact?el('b',{},lineFact):trees.length?el('div',{},...trees.map(t=>conditionBuilder(fields,t,{readOnly:true}).root)):el('b',{},'Not set'),help?el('small',{class:'hint'},help):'')];
        if(isTreeList(p)){const o=orderedTrees(label,fields,trees);read.push(()=>{const v=o.read();return [kind,f,v.length?v:cleared];});return withHelp(o.wrap);}
        const b=conditionBuilder(fields,trees[0]||null,{allOf:true,startEmpty:true});
        read.push(()=>[kind,f,b.read()??cleared]);
        return withHelp(el('div',{class:'field rc-tree-field',role:'group','aria-label':label},el('span',{class:'rc-tree-label'},label),b.root));}
      // Only what the editor can show (a list of ids needs its list: "Who counts" has none).
      if(f==='share_adjustment'){const now=(value||{}) as {mode?:string;value?:number},shown=shareWords(now);
        if(!editable)return [el('div',{class:'rc-setting-fact'},el('span',{class:'muted'},label),el('b',{},lineFact??shown),help?el('small',{class:'hint'},help):'')];
        const mode=select('As',[{value:'percent',label:'Percent of a fair share'},{value:'amount',label:'More or less than a fair share'}],now.mode||'percent');
        const amount=field(label,'number',now.value==null?'':String(now.value),{placeholder:(now.mode||'percent')==='percent'?'100':'0',step:'any',min:now.mode==='amount'?-MAX_SCORE_INPUT:0,max:MAX_SCORE_INPUT});
        mode.input.addEventListener('change',()=>{amount.input.placeholder=mode.input.value==='percent'?'100':'0';amount.input.min=String(mode.input.value==='percent'?0:-MAX_SCORE_INPUT);});
        read.push(()=>{const m=mode.input.value,v=amount.input.value===''?(m==='percent'?100:0):Number(amount.input.value);
          return [kind,f,forLine&&amount.input.value===''?undefined:!forLine&&m==='percent'&&v===100&&value===undefined?undefined:{mode:m,value:v}];});
        return withHelp(el('div',{class:'rc-share-dial'},amount.wrap,mode.wrap));}
      // A date (a quota's counting dates, its blocks' anchor): the shared date field; an end shows its last day.
      if(p.format==='date'||p.anyOf?.some((x:Row)=>x.format==='date')){
        if(!editable)return [el('div',{class:'rc-setting-fact'},el('span',{class:'muted'},label),el('b',{},lineFact??(value?formatDate(f==='ends_on'?dayBefore(String(value)):String(value)):'Not set')),help?el('small',{class:'hint'},help):'')];
        const d=field(label,'date',value?String(value):'');if(f==='ends_on')lastDayIncluded(d.input);
        read.push(()=>[kind,f,d.input.value||cleared]);return withHelp(d.wrap);}
      if(!(type==='boolean'||type==='integer'||type==='number'||enumOf||f==='weekend_start'||list&&(type==='array'||type==='string')))return [];
      // Read-only: the value as a fact, never a disabled control that looks editable.
      if(!editable){
        const shown=value??p.default;
        const text=type==='boolean'?(shown?'Yes':'No'):f==='weekend_start'&&shown!=null?WEEKDAYS[Number(shown)]:enumOf&&shown!=null?choiceWords(f,String(shown))
          :type==='array'&&list?idWords(ctx,f,value):type==='string'&&list?(value?idWords(ctx,f,[value]):'Not set'):shown!=null&&shown!==''?String(shown):'Not set';
        return [el('div',{class:'rc-setting-fact'},el('span',{class:'muted'},label),el('b',{},lineFact??text),help?el('small',{class:'hint'},help):'')];}
      if(type==='boolean'){const c=check(label,value??p.default??false,help);read.push(()=>[kind,f,!forLine&&c.input.checked===(p.default??false)&&value===undefined?undefined:c.input.checked]);return [own(c.wrap)];}
      if(f==='weekend_start'){const s=select(label,weekOrder().map(d=>({value:String(d),label:WEEKDAYS[d]})),String(value??p.default??''),false);read.push(()=>[kind,f,s.input.value===''?cleared:Number(s.input.value)]);return withHelp(s.wrap);}
      if(enumOf){const s=select(label,enumOf.map((v:string)=>({value:v,label:choiceWords(f,v)})),String(value??''),false,p.default!=null?choiceWords(f,String(p.default))+usual:'Not set');read.push(()=>[kind,f,s.input.value||cleared]);return withHelp(s.wrap);}
      if(type==='integer'||type==='number'){const n=field(label,'number',value==null?'':String(value),{placeholder:p.default!=null?String(p.default):'',step:type==='integer'?'1':'any',
        // The schema's bounds, checked before Keep (an exclusive minimum of 0 is "more than 0").
        ...(p.minimum!=null?{min:p.minimum}:p.exclusiveMinimum!=null?{min:p.exclusiveMinimum+(type==='integer'?1:0.0001)}:{}),...(p.maximum!=null?{max:p.maximum}:{})});read.push(()=>[kind,f,n.input.value===''?cleared:Number(n.input.value)]);return withHelp(n.wrap);}
      if(type==='array'&&list){const m=multiPick(label,choices,(value||[]) as string[],undefined,ALL_WHEN_EMPTY[f]||'None');read.push(()=>[kind,f,m.values().length?m.values():cleared]);return withHelp(el('div',{class:'field'},el('label',{},label),m.wrap));}
      if(type==='string'&&list){const s=select(label,choices,String(value??''),false,'Not set');read.push(()=>[kind,f,s.input.value||cleared]);return withHelp(s.wrap);}
      return [];})();
      // Where the check's message for this setting goes.
      if(out[0] instanceof HTMLElement)slots.set(`${kind}.${f}`,out[0]);if(read.length>reads)readers.set(`${kind}.${f}`,read[read.length-1]);return out;});
    return rows.length?[...(kinds.length>1?[el('div',{class:'dt-pop-label'},KIND_WORDS[kind]||words(kind))]:[]),...rows]:[];};
  popover(anchor,(panel,close)=>{
    panel.classList.add('dt-cell-pop','rc-settings');
    const keepBtn=button('Keep',()=>keepNow(),'primary small');
    if(overriding)panel.append(el('p',{class:'muted rc-settings-note'},`For every line. ${plural(overriding,'line has its','lines have their')} own, in the line’s panel.`));
    // Balance between people: a switch for those who propose, a fact for everyone else.
    const hint='Compare everyone under this rule together for fairness. Off: each line is balanced on its own.';
    const between=balance!==undefined&&editable?check('Balance between people',balance,hint):null;
    between?.input.setAttribute('role','switch');
    if(balance!==undefined)panel.append(between?el('div',{class:'switch-group rc-balance'},between.wrap)
      :el('p',{class:'rc-balance'},'Balance between people: ',el('b',{},balance?'On':'Off'),el('small',{class:'hint'},balance?'Everyone under this rule is compared together for fairness.':'Each line is balanced on its own.')));
    panel.append(...kinds.flatMap(section),el('div',{class:'actions rc-settings-actions'},button(editable?'Cancel':'Close',close,'ghost small'),
      ...(editable?[keepBtn]:[])));
    // A setting as chosen here, else the line's rule, else the entry's default, else the contract's.
    const valueOf=(kind:string,f:string)=>{const r=readers.get(`${kind}.${f}`);
      return String((r?r()[2]:current[kind]?.[f])??inherited[kind]?.[f]??e.default_settings?.[kind]?.[f]??defs.$defs[mapping[kind]?.split('/').pop()||'']?.properties?.[f]?.default);};
    const applies={...APPLIES_UNMARKED,...Object.assign({},...kinds.map(k=>appliesMarked(k,defs.$defs[mapping[k]?.split('/').pop()||'']?.properties||{})))} as Record<string,Applies>;
    const fit=()=>{for(const [key,when] of Object.entries(applies)){
      const [kf,choice]=key.split('='),[kind,f]=kf.split('.'),slot=slots.get(kf);if(!slot)continue;
      const ok=Object.entries(when).every(([k,allowed])=>k==='need'||(allowed as string[]).includes(valueOf(kind,k)));
      if(choice===undefined){slot.hidden=!ok;
        // A required date or number: a line whose rule has none sets its own; the browser checks it at Keep.
        if(when.need&&ok){const own=slot.querySelector<HTMLElement>('[role=radio]:last-child');if(own?.getAttribute('aria-checked')==='false'&&inherited[kind]?.[f]==null)own.click();}
        slot.querySelectorAll<HTMLInputElement>('input:not([type=radio])').forEach(x=>{if(when.need)x.required=ok;});continue;}
      const s=slot.querySelector('select'),o=s?.querySelector<HTMLOptionElement>(`option[value="${choice}"]`);if(!s||!o)continue;
      o.hidden=o.disabled=!ok;if(!ok&&s.value===choice)s.value='';}};
    for(const t of ['change','click'])panel.addEventListener(t,()=>queueMicrotask(fit));fit();
    function showIssues(issues:SettingIssue[]){
      panel.querySelectorAll('.rc-setting-error').forEach(x=>x.remove());
      panel.querySelectorAll('[aria-invalid]').forEach(x=>x.removeAttribute('aria-invalid'));
      let first:HTMLElement|null=null;
      for(const x of issues){const slot=slots.get(x.field)||null,id='err-'+Math.random().toString(36).slice(2);
        const msg=el('p',{class:'field-error rc-setting-error',id,role:'alert'},x.message);
        if(slot&&!slot.hidden){slot.append(msg);const input=slot.querySelector<HTMLElement>('input,select,button');input?.setAttribute('aria-invalid','true');input?.setAttribute('aria-describedby',id);}
        else panel.querySelector('.rc-settings-actions')!.before(msg);  // a setting this popover does not show
        first??=slot&&!slot.hidden?slot:msg;}
      first?.scrollIntoView({block:'nearest'});}
    async function keepNow(){const bad=[...panel.querySelectorAll<HTMLInputElement>('input')].find(x=>!x.closest('[hidden]')&&!x.checkValidity());if(bad){bad.reportValidity();return;}
      const out:Settings={};for(const r of read){const [kind,f,v]=r();if(v!==undefined&&!slots.get(`${kind}.${f}`)?.hidden)(out[kind]??={})[f]=v;}
      // Settings the editor does not show (block dates written by the server) stay as they were.
      for(const [kind,v] of Object.entries(current))for(const [f,x] of Object.entries(v))if(!settingFields(kind,e,forLine).includes(f))(out[kind]??={})[f]=x;
      // Pairs that belong together (conditional credit, a share and its duties) are the server check's to refuse.
      // Busy, not disabled: a disabled button drops focus, which would close the popover.
      if(validate){if(keepBtn.getAttribute('aria-busy')==='true')return;keepBtn.setAttribute('aria-busy','true');
        try{const issues=await validate(out);if(issues.length){showIssues(issues);return;}}
        catch(err){if(sessionChanged(err))throw err;}  // the check could not run: Propose checks again
        finally{keepBtn.removeAttribute('aria-busy');}}
      close();keep(out,between?between.input.checked:undefined);}
    },{label:`${e.name} settings`});
}
/** Why Propose is (not) available, on the button itself: a disabled button is neutral, never accent (M11). */
/** The overlap tick of a Propose dialog: Propose stays disabled, saying why, until it is ticked (never the browser's
 *  "Please tick this box" bubble). Call once the dialog is open; no tick in it (no overlap) leaves Propose as it is. */
export function tickGates(box:HTMLInputElement){
  const submit=box.closest('dialog')?.querySelector<HTMLButtonElement>('button[type=submit]');if(!submit)return;
  const why='Tick “I have read which lines give way” first';let pending=false;
  // The tick's own validity guards the submit too: formInto re-enables Propose after a failed command, so a tick
  // cleared while that command ran must still stop the retry. While a command runs Propose stays disabled.
  const sync=()=>{box.setCustomValidity(box.checked?'':why);if(pending)return;submit.disabled=!box.checked;if(box.checked)delete submit.dataset.tip;else submit.dataset.tip=why;};
  submit.form?.addEventListener('submit',()=>{pending=submit.disabled;});  // formInto has disabled it: a command is in flight
  new MutationObserver(()=>{if(pending&&!submit.disabled){pending=false;sync();}}).observe(submit,{attributes:true,attributeFilter:['disabled']});
  box.addEventListener('change',sync);sync();
}
export const proposeTip=(b:HTMLButtonElement,n:number)=>{b.dataset.tip=n?`Send ${plural(n,'change')} to an approver. Nothing changes until someone else approves.`:TERMS.nothingToPropose;};
/** Propose on the Rules list and a rule's page: no icon, since it sends for approval and adds nothing (its words
 *  sit in `.label`, so the header's compact() gives it none); disabled, its reason is its tip and description only. */
export function proposeButton(run:()=>unknown):HTMLButtonElement{
  const b=button('',run,'primary',{disabled:true,why:TERMS.nothingToPropose});b.append(el('span',{class:'label'},'Propose'));proposeTip(b,0);return b;}
/** A rule's unit in words for a line's number when the line itself names none ("people", "1 person"); '' with no number. */
export const unitWords=(e:RuleCatalogueEntry,l:RuleLine)=>{const n=l.numbers?.max?.value??l.numbers?.min?.value??l.numbers?.target?.value;
  return n==null?'':n===1?ONE[e.unit||'']||e.unit||'':e.unit||'';};
export const NUMBER_LABELS:Record<string,string>={min:'Min',max:'Max',target:'Target',priority:'Priority',session_priority:'Session priority',apply:'Applies'};
/** One number of a line: its own ("3!"), "None", inherited ("2↰") or "—". */
/** One number of a line in a table cell: its own ("3"), "None", inherited ("2↰") or "—", with its strength. */
const numberPart=(l:RuleLine,k:string,inh?:Inherited):NumberPart|null=>{const n=l.numbers?.[k],i=inh?.[k];
  if(n)return n.value==null&&k!=='apply'?{text:shown(n)}:{text:k==='apply'?'Yes':shown(n),...displayedStrength(l,k,n.level??i?.level)};
  return i&&i.value!=null?{text:shown(i)+'↰',from:i.from,...displayedStrength(l,k,i.level)}:null;};
const shownNumber=(l:RuleLine,k:string,inh?:Inherited)=>{const x=numberPart(l,k,inh);return x?partsText([x]):'—';};
// One number per column (Min, Max…): its strength and ↰ are marks, the min – max → target notation is not drawn here.
const numberCell=(l:RuleLine,k:string,inh:Inherited|undefined,from:(id:string)=>string,key:RuleViewKey)=>{const x=numberPart(l,k,inh);
  return !x||x.level===undefined?el('span',{class:'rule-num muted'},x?.text||'—'):key.numbers(numbersEl([x],from),false);};
export function setNumber(l:RuleLine,k:string,v:{value:number|null;level:string}|null){
  const numbers={...l.numbers};if(!v||(k!=='apply'&&v.value===null)){if(l.source_bindings?.[k])numbers[k]={value:null,level:(v?.level??numbers[k]?.level??null) as 'mandatory'|null};else delete numbers[k];}
  else numbers[k]={value:k==='apply'?null:v.value,level:v.level as 'mandatory'};l.numbers=numbers;}
/** Lines added, removed or changed between two versions, by line id (History, Propose, an approver's review). */
export type Change={kind:'added'|'removed'|'changed';line:RuleLine;was?:RuleLine};
// JSON object key order is not a line setting; retain every field and array order when comparing versions.
const lineJSON=(l:unknown)=>JSON.stringify(l,(_key,value)=>value&&typeof value==='object'&&!Array.isArray(value)
  ?Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b))):value);
export function lineChanges(before:RuleLine[],after:RuleLine[]):Change[]{
  const old=new Map(before.map(l=>[l.id,l])),now=new Set(after.map(l=>l.id));
  return [...after.flatMap((l):Change[]=>!old.has(l.id)?[{kind:'added',line:l}]:lineJSON(l)!==lineJSON(old.get(l.id)!)?[{kind:'changed',line:l,was:old.get(l.id)}]:[]),
    ...before.filter(l=>!now.has(l.id)).map((l):Change=>({kind:'removed',line:l}))];
}
/** "Everyone at #VIP_Clinics · Sun–Sat · 2 – 5! → 4 · 1 Sep → 30 Nov": one line in a sentence. */
export const lineWords=(e:RuleCatalogueEntry,l:RuleLine,names:Namer,max=4,rule:Settings={})=>`${treeText(l.who,names,'Everyone',max)} ${placeWord(l.where)} ${treeText(l.where,names,'all clinics',max)} · ${daysText(l)} · ${numbersText(l,undefined,appliesWord(e),e.measures&&l.numbers.apply?'':unitText(e,l,rule))||'no number'} · ${validText(l)}`;
/** A long list's first items and "Show N more", which shows the rest in place (a move into the catalogue can list
 *  hundreds of lines or rules). */
function capped(items:HTMLElement[],noun:[string,string],limit=20):HTMLElement[]{
  if(items.length<=limit)return items;
  const rest=items.slice(limit),more:HTMLElement=el('li',{},button(`Show ${rest.length} more ${rest.length===1?noun[0]:noun[1]}`,()=>more.replaceWith(...rest),'',{class:'link-button'}));
  return [...items.slice(0,limit),more];
}
/** A change in words, [was, now]. A changed who or where reads in full on both sides, so a change past the shortened
 *  lists (the fifth person E → F) shows even when a number changed too. */
export function changeWords(e:RuleCatalogueEntry,c:Change,names:Namer,rule:Settings={}):[string|undefined,string]{
  const max=c.was&&(lineJSON(c.was.who)!==lineJSON(c.line.who)||lineJSON(c.was.where)!==lineJSON(c.line.where))?Infinity:4;
  return [c.was&&lineWords(e,c.was,names,max,rule),lineWords(e,c.line,names,max,rule)];
}
/** A changed line read once, only its changed parts as was → now ("Everyone at … · Sun–Sat · ≤ 7 → ≤ 8 · Mandatory · …");
 *  the whole line on both sides when its parts do not line up. */
const delta=(was:string,now:string,sides?:[string,string])=>{const a=was.split(' · '),b=now.split(' · ');
  // `sides` names both values where an arrow would read as the rule's own history (a draft set aside, movedNote).
  const pair=(x:string,y:string)=>sides?`${sides[0]} ${x} · ${sides[1]} ${y}`:`${x} → ${y}`;
  return a.length===b.length?b.map((part,i)=>part===a[i]?part:pair(a[i],part)).join(' · '):pair(was,now);};
/** The changes as a list: + added, − removed, ~ changed (what it was → what it is). */
export function changeList(e:RuleCatalogueEntry,before:RuleLine[],after:RuleLine[],names:Namer,rule:Settings={},sides?:[string,string]):HTMLElement{
  const list=lineChanges(before,after);
  return el('ul',{class:'rc-diff'},...(list.length?capped(list.map(c=>{const [was,now]=changeWords(e,c,names,rule);
    return el('li',{class:'rc-diff-'+c.kind},el('b',{},c.kind==='added'?'+ ':c.kind==='removed'?'− ':'~ '),was&&was!==now?delta(was,now,sides):now+(c.kind==='changed'?' (note or settings)':''));}),['change','changes']):
    [el('li',{class:'muted'},'No line changed')]));
}
/** A waiting proposal in brief reviewed: what it changes in one
 *  line ("1 line added · 0 changed · 0 removed"), a small table of only those lines (who and where as chips with +N,
 *  numbers, days, valid; a changed cell says what it was), the unchanged lines only on demand, the check's warnings as
 *  separate items, who proposed it and the decision. What changed is the server's diff (`proposed.diff`, by line id). */
/** A waiting proposal's effect on the rule's switch ("Turns the rule on"), or '': said by the note and the panel title. */
const switchWords=(e:Entry,on:boolean)=>!e.rule?(on?'Turns the rule on':'Rule stays off'):on!==isOn(e)?(on?'Turns the rule on':'Turns the rule off'):'';
function proposalNote(ctx:Context,e:Entry,names:Namer,refresh:()=>Promise<void>,openTag:(tag:Row)=>void,review:()=>void,key?:RuleViewKey){
  const p=e.proposed!,rule=p.definition.rule,now=(rule.lines||[]) as RuleLine[],before=linesOf(e),mine=p.created_by===ctx.me.identity_id;
  const canDecide=ctx.permits(CAPABILITY.APPROVER,CAPABILITY.COORDINATOR,CAPABILITY.ADMIN);
  const selfApproves=mine&&ctx.permits(CAPABILITY.APPROVER)&&mayDecide(ctx.me,'rule.activate',true);
  const decide=(kind:string,title:string,text:string,label:string,reason?:string)=>reason?command(kind,{reason},p.id,p.revision).then(refresh)
    :confirmAction(title,text,async why=>{await command(kind,{reason:why},p.id,p.revision);await refresh();},label);
  const actions=[
    ...(ctx.permits(CAPABILITY.APPROVER)&&mayDecide(ctx.me,'rule.activate',mine)?[button('Review and approve',review,'primary small')]:[]),
    ...(!mine&&canDecide?[button('Decline',()=>decide('policy.decline',`Decline the change to ${e.name}`,'The version in force stays; the proposal is kept in History.','Decline'),'secondary small')]:[]),
    // A withdrawal is governed too: collect its reason before writing the history entry.
    ...(mine&&canDecide?[button('Withdraw',()=>decide('policy.decline',`Withdraw ${e.name}`,'The proposal stays in History; Undo puts it back waiting.','Withdraw'),'secondary small')]:[])];
  // The server's diff when it sends one; else worked out here (a proposal read some other way).
  const d=p.diff as {added?:string[];removed?:string[];changed?:string[];settings?:unknown}|undefined,was=new Map(before.map(l=>[l.id,l])),is=new Map(now.map(l=>[l.id,l]));
  const changes:Change[]=d?.added?[...(d.added||[]).flatMap(id=>is.has(id)?[{kind:'added' as const,line:is.get(id)!}]:[]),
    ...(d.changed||[]).flatMap(id=>is.has(id)?[{kind:'changed' as const,line:is.get(id)!,was:was.get(id)}]:[]),
    ...(d.removed||[]).flatMap(id=>was.has(id)?[{kind:'removed' as const,line:was.get(id)!}]:[])]:lineChanges(before,now);
  const n=(k:string)=>changes.filter(c=>c.kind===k).length,touched=new Set(changes.map(c=>c.line.id));
  const unchanged=now.filter(l=>!touched.has(l.id));
  const on=p.definition.balance_between_people??true;
  // A rule not in force yet that is proposed On is switched on by its activation: the approver is told, as the proposer was.
  const extra=[switchWords(e,rule.on!==false),
    on!==balanceOf(e)?'Balance between people '+(on?'on':'off'):'',
    ].filter(Boolean);
  const settings=settingsChange(ctx,names,settingsOf(e),(rule.settings||{}) as Settings);
  const summary=el('p',{class:'rc-prop-summary'},[n('added')?`${plural(n('added'),'line')} added`:'',n('changed')?`${plural(n('changed'),'line')} changed`:'',
    n('removed')?`${plural(n('removed'),'line')} removed`:'',...extra,settings?'Settings':''].filter(Boolean).join(' · ')||'No change to its lines');
  // What the check says about the waiting version, as its proposer saw it: the approver decides on the same.
  const c=p.check as Check|undefined,words=checkWords(e,()=>now,names);
  const said=c?[...c.duplicates.map(words.same),...c.issues.map(x=>String(x.message)),...c.warnings.map(words.warn)]:[];  // a winning exception is no warning; equal tiers come as warnings
  // Which line each proposed line gives way to: the approver still sees it, as information (no alert, nothing to tick).
  const gives=c?c.overlaps.map(words.gives):[];
  // The plain lines first, apart from the warnings' label and count, so they never read as warnings.
  const checked=said.length||gives.length?el('div',{class:'rc-prop-check'},
    ...(gives.length?[el('ul',{class:'rc-prop-gives'},...capped(gives.map(t=>el('li',{},t)),['line','lines']))]:[]),
    ...(said.length?[el('p',{class:'dt-pop-label'},`The check · ${plural(said.length,'warning')}`),
      el('ul',{class:'rc-prop-warnings'},...capped(said.map(t=>el('li',{},icon('alert'),el('span',{},t+'.'))),['warning','warnings']))]:[])):'';
  const table=changes.length?changeTable(ctx,e,changes,unchanged,names,openTag,(rule.settings||{}) as Settings,key):'';
  const who=el('p',{class:'rc-prop-who muted'},mine?'You proposed it':`Proposed by ${p.created_by_name||'another person'}`,p.created_at?` · ${formatDate(String(p.created_at),departmentTimeZone())}`:'',  // the department's day, not UTC's
    p.source?el('span',{class:'rc-prop-reason',title:String(p.source)},` · “${String(p.source)}”`):'',
    // As Simplify rules says it: the proposer who may self-approve is told the activation is recorded as self-approved.
    mine?(selfApproves?' · Your self-approval permission lets you activate it with a reason; it is recorded as self-approved.':' · Another person approves it.'):'');
  return el('section',{class:'notice warning rc-waiting','aria-label':'Proposed change, waiting for approval'},
    el('div',{class:'rc-waiting-head'},el('strong',{},'Proposed change · waiting for approval'),el('span',{class:'grow'}),...actions),
    summary,table,settings||'',checked,who);
}
/** B5: the approver's decision in the docked panel: what each proposed line says and how it stands against the broader
 *  line, the proposer's reason, and the effect on the drafts its dates overlap only (D6). "Approve and activate" is one
 *  step and waits while an effect is being checked; "Decline…" asks why. */
/** How many drafts' effect checks the decision panel keeps queued at once (maintenance: it used to queue all of them, one
 *  after another, and go on after it closed; the server runs them one by one). */
const CHECKS_AT_ONCE=2;
export function proposalPanel(ctx:Context,e:Entry,names:Namer,refresh:()=>Promise<void>){
  const p=e.proposed!,now=(p.definition.rule.lines||[]) as RuleLine[],mine=p.created_by===ctx.me.identity_id,zone=departmentTimeZone();
  // The server's diff when it sends one (the proposal's own lines are normalised), else worked out here.
  const diff=p.diff as {added?:string[];changed?:string[]}|undefined,byId=new Map(now.map(l=>[l.id,l]));
  const focus:Change[]=diff?.added?[...(diff.added||[]).flatMap(id=>byId.has(id)?[{kind:'added' as const,line:byId.get(id)!}]:[]),
    ...(diff.changed||[]).flatMap(id=>byId.has(id)?[{kind:'changed' as const,line:byId.get(id)!}]:[])]:lineChanges(linesOf(e),now).filter(c=>c.kind!=='removed');
  const one=focus.length===1?focus[0]:null,whom=one?countWords(one.line.who,names,'everyone'):'';
  const on=p.definition.rule.on!==false,turns=switchWords(e,on);
  const change=one?`${one.kind==='added'?'Add':'Change'} a line for ${whom}`:`Change to ${e.name}`;
  const title=turns?`${change} · ${turns.toLowerCase()}`:change;
  const when=p.created_at?`${formatDate(String(p.created_at),zone)} ${formatTime(String(p.created_at),zone)}`:'';
  const canApprove=ctx.permits(CAPABILITY.APPROVER)&&mayDecide(ctx.me,'rule.activate',mine),canCheck=ctx.permits(CAPABILITY.APPROVER,CAPABILITY.COORDINATOR);
  // Checks started here, with when: shown as checking at once, before the server has queued them.
  const started=new Map<string,string>();
  // maintenance: the panel opens at once and its effect review (a server read that can be slow) loads into it, with the shared
  // long wait and Retry: Decline needs no review and is ready at once; Approve waits for the review it decides on.
  // The drafts' effect checks this panel queues: at most CHECKS_AT_ONCE at a time (the server runs them one by one), and
  // none once it is closed; a coordinator's close also cancels the ones it queued (approvers may not cancel jobs).
  const life={closed:false,jobs:new Set<string>()},mayStop=ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.OPERATOR);
  dockPanel({key:'rule-proposal',className:'rc-line-panel',title,width:560,
    sub:[p.created_by_name||(mine?'You':'Another person'),when].filter(Boolean).join(' · '),
    onClose:()=>{life.closed=true;
      if(mayStop&&life.jobs.size)void command('bulk.apply',{item_kind:'job.cancel',reason:'Closed the rule review before its checks finished',
        items:[...life.jobs].map(id=>({target_id:id}))}).catch(()=>undefined);},
    build:d=>{
      const section=(head:Child,...body:Child[])=>el('section',{class:'rc-lp-section'},el('h3',{class:'rc-lp-title'},head),...body);
      // What each line says now; how it stands against the broader line once the review has answered.
      const says=el('div',{class:'rc-says-list'});
      const drawSays=(relations:Record<string,Relation>)=>says.replaceChildren(...focus.slice(0,3).map(c=>{const r=relations[c.line.id],other=r&&now.find(l=>l.id===r.line_id);
        const box=el('div',{class:'rc-says'+(r&&other&&!r.overrides?' is-warn':'')},el('span',{class:'rc-says-label'},'This line says'),
          el('p',{class:'rc-says-text'},...lineSays(e,c.line,names,{who:null,where:null},undefined,(p.definition.rule.settings||{}) as Settings)));
        if(r&&other)box.append(el('p',{class:'rc-relation'},...(relationParts(e,names,r,c.line,other,()=>{d.close();o2(other.id);}) as (Node|string)[])));
        return box;}));
      drawSays({});
      const list=el('ul',{class:'rc-lp-list rc-effect'});
      const waitsWhy='Waits until the effect of this change is known',approveWhy=el('small',{class:'why-unavailable'});
      const protection=note(TERMS.reducesSafetyProtection,'warning');
      protection.id='rc-safety-protection-'+String(p.id);protection.hidden=true;
      const selfApproval=canApprove&&mine?badge('Self-approved','warn'):null;
      const approve=button('Approve and activate',async()=>{
        // A press before the effect is known (still loading, or its read failed) says why, never nothing.
        if(!review){notify(waitsWhy,'info');return;}const ack=review.requires_acknowledgement&&understood.input.checked;
        // Your own change under a self-approval permission needs your reason and is marked Self-approved (§3): the one
        // Reason of this panel, asked once beside the decision (it also answers an incomplete check), sent as given.
        const done=busy(approve);try{await approveAndActivate(String(p.id),Number(p.revision),mine||ack?why.input.value.trim():null,rows,!!ack);}finally{await done();}
        d.close();notify(`${e.name}: ${on?'the change is active.':'the rule is off.'}`);await refresh();},'primary');approve.prepend(icon('check'));
      let review:Review|null=null,rows:Effect[]=[];
      const incompleteLabel='Approve without a complete check of these drafts';
      const understood=check(incompleteLabel),why=area('Reason','',2);why.input.maxLength=500;
      const ackLabel=understood.wrap.querySelector('.check-text>span')!;
      const acknowledgement=el('div',{class:'rc-ack'},understood.wrap,why.wrap);
      for(const x of [understood.input,why.input])x.addEventListener(x===why.input?'input':'change',()=>paint());
      // A check still under way (running, or started here and not picked up yet) shows its elapsed time, never the
      // acknowledgement, which is offered once the checks stop and the approver can decide.
      const checking=()=>rows.some(effectRunning)||rows.some(x=>x.status==='not_checked'&&started.has(x.workspace_id));
      // SW3-B-19: Check / Check again starts the drafts' checks on request (a refused start is said, never left as a check
      // that never ends); Stop cancels a check that hangs (job.cancel: coordinators). Approving without a complete check
      // is offered once no check runs, with a reason, as the server requires (rule_effects can_decide).
      let startError='',pollError='',timer=0;
      const failure=el('div',{'aria-live':'polite'});
      const start=async(xs:Effect[])=>{if(life.closed)return;startError='';const at=new Date().toISOString();for(const x of xs)started.set(x.workspace_id,at);if(xs.length)paint();
        // In the order the drafts are listed (what the person sees first), CHECKS_AT_ONCE at a time; none after close.
        const queue=[...xs];
        const worker=async()=>{for(let x=queue.shift();x&&!life.closed;x=queue.shift()){const one=x;
          await checkEffect(String(p.id),Number(p.revision),one).then(r=>{const id=(r as Row|undefined)?.job?.id;if(id)life.jobs.add(String(id));},
            err=>{started.delete(one.workspace_id);if(!sessionChanged(err))startError='Some drafts couldn’t start their check.';});}};
        await Promise.all(Array.from({length:Math.min(CHECKS_AT_ONCE,queue.length)},worker));
        for(const x of queue)started.delete(x.workspace_id);};  // closed before they were started
      const reread=async()=>{const r=await proposalReview(String(p.id));review=r;rows=r.effect||[];paint();};
      const recheck=button('Check',async()=>{understood.input.checked=false;const done=busy(recheck);try{await start(rows.filter(x=>!effectRunning(x)&&effectRecheckable(x)));await reread();void load();}
        catch(err){reportError(err);}finally{await done();}},'secondary small');
      const stop=mayStop?button('Stop',async()=>{const done=busy(stop!);
        try{await command('bulk.apply',{item_kind:'job.cancel',reason:'Stopped the rule check',
          items:rows.filter(effectRunning).filter(x=>x.job_id).map(x=>({target_id:String(x.job_id)}))});await reread();}
        catch(err){reportError(err);}finally{await done();}},'secondary small'):null;
      const needsFirstCheck=(x:Effect)=>x.status==='not_checked'&&effectRecheckable(x)&&!started.has(x.workspace_id);
      let paintedEffects='';
      const paint=()=>{if(!review){disabledWhy(approve,waitsWhy,[],approveWhy);recheck.hidden=true;if(stop)stop.hidden=true;return;}
        const effects=rows.map(x=>x.status==='not_checked'&&started.has(x.workspace_id)?{...x,status:'queued',started_at:started.get(x.workspace_id)}:x);
        // Keep the evidence open when only the acknowledgement controls repaint.
        const effectKey=JSON.stringify([effects,review.summary]);
        if(effectKey!==paintedEffects){list.replaceChildren(...effectRows(effects,review.summary||undefined));paintedEffects=effectKey;}
        const fresh=rows.some(needsFirstCheck);
        recheck.textContent=fresh?'Check':'Check again';recheck.hidden=!canCheck||checking()||!rows.some(effectRecheckable);
        if(stop)stop.hidden=!rows.some(effectRunning);
        failure.replaceChildren(...(startError?[note(startError,'warning')]:[]),
          // maintenance: a review read that fails is said, with Retry, and polling goes on (it used to stop for good while the
          // elapsed time kept counting: the sweep's "Checking · 12:51").
          ...(pollError?[(n=>{n.append(' ',button('Retry',()=>{window.clearTimeout(timer);void poll();},'secondary small'));return n;})(note(`Couldn't check · ${pollError}`,'warning'))]:[]));
        const ask=!!review.requires_acknowledgement&&review.can_decide!==false&&!checking();understood.wrap.hidden=!ask;acknowledgement.hidden=!(ask||mine);
        ackLabel.textContent=rows.some(x=>x.new_or_worse_count)?(rows.some(x=>x.status!=='ready')?
          'Approve despite added or worsened problems and incomplete checks':'Approve despite the added or worsened problems'):incompleteLabel;
        why.wrap.hidden=!(mine||understood.input.checked);why.input.required=mine||ask&&understood.input.checked;
        const decidable=review.can_decide??effectReady(rows);
        protection.hidden=!review.weakens_safety;
        const needsReviewer=mine&&!!review.weakens_safety;
        if(selfApproval)selfApproval.hidden=needsReviewer;
        const descriptions=(approve.getAttribute('aria-describedby')||'').split(' ').filter(id=>id&&id!==protection.id);
        if(!protection.hidden)descriptions.push(protection.id);
        if(descriptions.length)approve.setAttribute('aria-describedby',descriptions.join(' '));
        else approve.removeAttribute('aria-describedby');
        const blocker=rows.find(x=>x.activation_blocker);
        const blockedWhy=blocker?.activation_blocker==='RULE_CHECK_UNRESOLVED'?'A safety rule needs a complete check of every draft'
          :blocker&&effectWords(blocker)?.text;
        disabledWhy(approve,needsReviewer?'Ask a different approver to activate this proposal':checking()?waitsWhy:!decidable?(blockedWhy||'A draft’s check failed; check it again')
          :ask&&!understood.input.checked?'Tick the box above first':(mine||ask)&&why.input.value.trim().length<3?'Give a reason (3 characters or more)':'',[],approveWhy);};
      // An effect not checked yet is checked now (each overlapping draft once), then read again until every one is known.
      const load=async()=>{if(!d.body.isConnected)return;
        if(canCheck)await start(rows.filter(needsFirstCheck));paint();
        window.clearTimeout(timer);if(checking()||pollError)timer=window.setTimeout(()=>void poll(),3000);};
      const poll=()=>proposalReview(String(p.id)).then(r=>{pollError='';review=r;rows=r.effect||[];drawSays(r.relations||{});paint();return load();},
        err=>{if(sessionChanged(err)||!d.body.isConnected)return;pollError=(err as Error).message;paint();window.clearTimeout(timer);timer=window.setTimeout(()=>void poll(),3000);});
      const decline=!mine?button('Decline…',()=>confirmAction(`Decline the change to ${e.name}`,'The version in force stays; the proposal is kept in History.',async reason=>{
        await command('policy.decline',{reason},p.id,p.revision);d.close();notify('Proposal declined.');await refresh();},'Decline'),'secondary'):null;
      // The review: the shared skeleton while it is read ("Still loading · 0:18" past 15 s), then the effect; a failed
      // read says so in words with Retry (its error goes to Report this problem, never shown raw).
      const effect=el('div',{class:'rc-effect-box',id:'rc-effect-'+String(p.id)});let asked=0;
      // Approve waits for the review it decides on: the shared busy state at once, described by the effect area (its long
      // wait), and disabled with that reason until the review answers.
      approve.setAttribute('aria-describedby',effect.id);approve.dataset.tip=waitsWhy;
      // A Retry retires only the ask: hold the first busy state until the current answer or error releases it.
      let waiting:(()=>Promise<void>)|null=null,settling=false;
      // Held, not disabled: a press while it waits reaches the button and says why (aria-disabled keeps it out of use).
      const hold=()=>{if(waiting)return;waiting=busy(approve,'Checking the effect…');approve.disabled=false;approve.setAttribute('aria-disabled','true');};
      const release=async()=>{retireRetry();const done=waiting;waiting=null;await done?.();approve.removeAttribute('aria-disabled');};
      const again=()=>{if(!settling)ask();};
      // The current answer retires the live Retry before the release; focus inside it stays in the effect area (WCAG 2.4.3).
      const retireRetry=()=>{settling=true;const held=effect.contains(document.activeElement);effect.replaceChildren(skeleton('lines','Checking the effect…'));
        if(held){effect.tabIndex=-1;effect.focus({preventScroll:true});}};
      const ask=()=>{const mine=++asked;effect.replaceChildren(skeleton('lines','Checking the effect…',{retry:again}));
        proposalReview(String(p.id)).then(async r=>{if(mine!==asked)return;await release();if(mine!==asked||!d.body.isConnected)return;review=r;rows=r.effect||[];drawSays(r.relations||{});
            effect.replaceChildren(list,failure,el('div',{class:'actions'},recheck,stop||''),canApprove?acknowledgement:'');
            approve.removeAttribute('aria-describedby');delete approve.dataset.tip;paint();void load();},
          async err=>{if(mine!==asked)return;await release();if(mine!==asked||!d.body.isConnected)return;if(sessionChanged(err)){d.close();return;}approve.setAttribute('aria-disabled','true');
            effect.replaceChildren(failedNote(couldntLoad('the effect of this change'),err,first));/* maintenance: never the server's raw words */});};
      const first=()=>{settling=false;hold();ask();};
      d.body.classList.add('rc-lp-body');
      d.body.append(says,protection,...(p.source?[section('Reason',el('p',{class:'rc-quote'},String(p.source)))]:[]),
        section(el('span',{},'Effect',effectHelp(one?.line||now[0]||{} as RuleLine)),effect),
        // Using a self-approval permission is said before deciding, as it is recorded.
        el('footer',{class:'rc-lp-foot rc-decide-foot'},decline||'',el('span',{class:'grow'}),selfApproval||'',canApprove?approveWhy:'',canApprove?approve:''));
      paint();first();
    }});
  const o2=(id:string)=>{location.hash=`#rules/${e.id}/${id}`;};
}
/** B4: "Relax for <name>…" — the line panel docked beside the page, prefilled from the binding line (who = the person;
 *  where, settings and strength = the binding line's; dates = the short week), then the justification and "Apply to
 *  this rota" (D16: in this draft at once, reviewed with the rota), or "Change the department rule…" (propose →
 *  approve). Never offered for a safety rule (D7; the server refuses it too). */
export async function relaxForPerson(ctx:Context,t:RelaxTarget){
  loadActive();const seed=await relaxSeed(t);
  await openSeed(ctx,seed,{done:()=>void ctx.refresh(),
    ...(seed.adjustment?{rota:{adjustment:seed.adjustment,workspace_id:t.workspace_id,name:t.workspace_name,revision:t.workspace_revision}}:{})});
}
/** Make a rota's change the department default (the selection panel): the server's seed opens in the same line panel,
 *  whose one way out is the department proposal (propose → a different approver → activate). */
export async function promoteAdjustment(ctx:Context,policyId:string,workspaceId:string,adjustmentId:string){
  loadActive();const seed=await promoteSeed(policyId,{workspace_id:workspaceId,adjustment_id:adjustmentId});
  await openSeed(ctx,seed,{done:()=>void ctx.refresh()});
}
/** A line seed (relax or promote) in the rule line panel: the rule's other lines as the draft, the seed's line open. */
async function openSeed(ctx:Context,seed:{catalogue_id:string;line:RuleLine;lines:RuleLine[]},relax:{done:()=>void;rota?:{adjustment:Adjustment;workspace_id:string;name?:string;revision?:number}}){
  const d=await get<{entry:Entry;day:string}>(`/api/rules/catalogue/${encodeURIComponent(seed.catalogue_id)}/detail?version=2&day=${departmentToday()}`);
  const e=d.entry,names=namer(ctx);learn(linesOf(e),e.line_specificity);learnValidity(e,d.day);
  const draft:Draft={on:isOn(e),lines:seed.lines.filter(l=>l.id!==seed.line.id),settings:clone(settingsOf(e)),balance:balanceOf(e),
    base:e.rule?.revision??null,baseId:e.rule?.id??null,seen:linesOf(e).map(l=>l.id)};
  pendingLines.set(e.id,clone(seed.line));
  lineEditor(ctx,e,draft,names,{parts:[],openTag:()=>undefined,navigate:ctx.navigate},{editable:true,family:RULE_FAMILIES.find(([id])=>id===e.family)?.[1]||e.family,
    saved:linesOf(e),day:d.day,fromSaved:e.rule?savedReach(ctx,e.rule.id,d.day):()=>Promise.resolve(null),changed:()=>undefined,counting:()=>undefined,
    inherited:id=>e.inherited?.[id],from:sourceWords(()=>draft.lines,names),checkState:()=>'saved',issues:()=>[],position:()=>undefined,
    order:()=>draft.lines.map(l=>l.id),relax}).open(seed.line.id);
}
/** B4: a Must-fix person row's ⋯ entries: Find a duty for <name>, Relax for <name>… (never for a safety rule or where
 *  the server says no) and Open line N. Shared by Resolve issues and the Fix list. */
export function personFixItems(ctx:Context,t:RelaxTarget&{policy?:Row},o:{findDuty?:()=>void;openLine?:()=>void},close:()=>void=()=>undefined):HTMLElement[]{
  const name=t.person_name||'this person',then=(run:()=>unknown)=>()=>{close();return run();};
  const relax=t.relax_allowed!==false&&!safetyLine(t.policy?.definition?.rule,t.binding_line_id)&&ctx.permits(CAPABILITY.COORDINATOR);
  return [...(o.findDuty?[menuItem(`Find a duty for ${name}`,'calendar',then(o.findDuty))]:[]),
    ...(relax?[menuItem(`Relax for ${name}…`,'fork',then(()=>relaxForPerson(ctx,t)))]:[]),
    ...(o.openLine?[menuItem(t.binding_line_no?`Open line ${t.binding_line_no}`:'Open rule','rules',then(o.openLine))]:[])];
}
/** D7: whether a rule line is a safety one. A rule mixing safety and other kinds (Workload limit: a count,
 *  or "no duties", a restriction) is judged by the line's own numbers, as schemas.safety_rule does. */
export function safetyLine(rule:Row|undefined,lineId:string):boolean{
  const r=rule as {kind:string;id?:string;catalogue_version?:number}|undefined,entry=catalogueEntryOf(r);
  if(!entry?.mixed_safety)return safetyRule(r);
  const line=((rule!.lines||[]) as RuleLine[]).find(l=>l.id===lineId);
  return !line||setsSafety(entry,line);
}
/** The ⋯ button holding them (one shared menu). */
export function personFixMenu(ctx:Context,t:RelaxTarget&{policy?:Row},o:{findDuty?:()=>void;openLine?:()=>void}):HTMLButtonElement{
  const label=`Actions for ${t.person_name||'this person'}`;
  let close=()=>{};
  const b:HTMLButtonElement=opens(iconButton(label,'dots',()=>{close=menu(b,label,()=>personFixItems(ctx,t,o,()=>close()),{placement:'below-end'});},{class:'bare'}),'menu');
  return b;
}
/** The changed lines of a proposal as a small table: Change, Who, Where (chips, +N), Numbers, Days, Valid; a changed
 *  line's differing cells carry "was …" beneath. "Show N unchanged lines" adds the rest, muted, on demand. */
function changeTable(ctx:Context,e:RuleCatalogueEntry,changes:Change[],unchanged:RuleLine[],names:Namer,openTag:(tag:Row)=>void,rule:Settings={},key?:RuleViewKey):HTMLElement{
  const unitCell=(l:RuleLine)=>{const u=e.measures&&l.numbers.apply?'':unitText(e,l,rule);return u?el('small',{class:'muted'},' '+u):'';};
  const KIND={added:['Added','ok'],changed:['Changed','warn'],removed:['Removed','err']} as const;
  const wasText=(now:string,then:string|undefined)=>then!==undefined&&then!==now?el('small',{class:'rc-prop-was'},'was '+then):'';
  const row=(l:RuleLine,kind:Change['kind']|'same',was?:RuleLine)=>{
    const t=(f:(x:RuleLine)=>string)=>wasText(f(l),was&&f(was));
    const same=was&&['who','where','numbers','weekdays','start_time','end_time','starts_on','ends_on'].every(k=>JSON.stringify((l as Record<string,unknown>)[k]??null)===JSON.stringify((was as Record<string,unknown>)[k]??null));
    return el('tr',{class:'rc-prop-'+kind},
      el('td',{},kind==='same'?el('span',{class:'muted'},'Unchanged'):badge(KIND[kind][0],KIND[kind][1]),same?el('small',{class:'rc-prop-note'},'note or settings'):''),
      el('td',{},pills(ctx,l.who,names,'Everyone',openTag,'Who',2,key),t(x=>treeText(x.who,names,'Everyone'))),
      el('td',{},pills(ctx,l.where,names,'All clinics',openTag,'Where',2,key),t(x=>treeText(x.where,names,'All clinics'))),
      el('td',{},numbersNode(l,undefined,()=>'a broader line',appliesWord(e),key),unitCell(l),t(x=>numbersText(x,undefined,appliesWord(e),unitText(e,x,rule)))),
      el('td',{},el('span',{},daysText(l)),t(daysText)),
      el('td',{},validNode(l),t(validText)));};
  // A large proposal (a move into the catalogue) lists its first 20 changes, the rest on request.
  const LIMIT=20,first=changes.slice(0,LIMIT),later=changes.slice(LIMIT);
  const body=el('tbody',{},...first.map(c=>row(c.line,c.kind,c.was)));
  const moreChanges:HTMLButtonElement|null=later.length?button(`Show ${plural(later.length,'more change')}`,()=>{
    const at=body.querySelector('tr.rc-prop-same');const rows=later.map(c=>row(c.line,c.kind,c.was));if(at)rows.forEach(r=>at.before(r));else body.append(...rows);moreChanges!.remove();},'',{class:'link-button rc-prop-more'}):null;
  const head=el('thead',{},el('tr',{},...['Change','Who','Where','Numbers','Days','Valid'].map(h=>el('th',{scope:'col'},h))));
  const grid=el('table',{class:'rc-prop-table'},head,body),table=el('div',{class:'rc-prop-scroll'},grid);columnSizing(grid,{reorder:false});
  if(!changes.length)body.append(el('tr',{},el('td',{colspan:'6',class:'muted'},'No line changed')));
  if(!unchanged.length)return moreChanges?el('div',{class:'rc-prop-lines'},table,moreChanges):table;
  let shown=false;const rest=unchanged.map(l=>row(l,'same'));
  const label=()=>shown?'Hide unchanged lines':`Show ${plural(unchanged.length,'unchanged line')}`;
  const more:HTMLButtonElement=button(label(),()=>{shown=!shown;if(shown)body.append(...rest);else rest.forEach(r=>r.remove());
    relabel(more,label());more.setAttribute('aria-expanded',String(shown));},'',{class:'link-button rc-prop-more','aria-expanded':'false'});
  return el('div',{class:'rc-prop-lines'},table,el('div',{class:'rc-prop-tools'},moreChanges||'',more));
}
// ---- Cell editors (anchored to the cell; Enter saves, Esc cancels) ----
function numberEditor(e:RuleCatalogueEntry,k:string,l:RuleLine,finish:(save:boolean,move?:1|-1)=>void){
  if(Object.keys(l.source_bindings||{}).length&&!l.source_bindings?.[k])return {node:note('This converted line keeps its source numeric slots. Add a new line for a different obligation.'),read:()=>null};
  const n=l.numbers?.[k],mandatory=mandatoryNumber(e,k),safety=hardSafetyNumber(e,k,l);
  let level:string|null=safety?'mandatory':n?n.level??null:defaultNumberLevel(e,k);
  const input=el('input',{type:'number',step:'any','aria-label':NUMBER_LABELS[k]||k,value:n?.value??''}) as HTMLInputElement;
  input.addEventListener('keydown',ev=>{if(ev.key==='Enter'){ev.preventDefault();finish(true);}if(ev.key==='Tab'){ev.preventDefault();finish(true,ev.shiftKey?-1:1);}});
  const kinds=[['mandatory','Mandatory'],['medium','Advisory'],...(tier(l)>1||level===null?[['','Inherit']]:[])].filter(([v])=>v==='mandatory'?mandatory:v==='medium'?e.levels.some(x=>x!=='mandatory'):true);
  // Advisory keeps the line's own advisory level (high, medium, low); choosing it fresh is medium.
  const levels=safety?levelBadge('mandatory',safety):segmented('Strength',kinds.map(([v,label])=>[v,label]),level===null?'':level==='mandatory'?'mandatory':'medium',
    v=>{level=v===''?null:v==='mandatory'?'mandatory':level&&level!=='mandatory'?level:'medium';});
  const node=el('div',{class:'rc-number-edit'},...(k==='apply'?[]:[input]),levels,el('small',{class:'muted'},k==='apply'?'Esc cancels':'Enter saves · Esc cancels · Tab to the next cell · empty removes it'));
  return {node,read:()=>k==='apply'?{value:null,level}:{value:input.value===''?null:Number(input.value),level}};
}
export function daysEditor(l:RuleLine){
  const on=new Set(daysOf(l));
  const node=el('div',{class:'rc-days',role:'group','aria-label':'Days'},...weekOrder().map(d=>{
    const b=el('button',{type:'button','aria-pressed':String(on.has(d)),'aria-label':WEEKDAYS[d]},WEEKDAYS[d].slice(0,3));
    b.addEventListener('click',()=>{if(on.has(d)&&on.size>1)on.delete(d);else on.add(d);b.setAttribute('aria-pressed',String(on.has(d)));});return b;}));
  return {node,read:()=>[...on].sort()};
}
export function validEditor(l:Pick<RuleLine,'starts_on'|'ends_on'>,label='Valid'){
  const from=field('From','date',l.starts_on||''),until=field('Last day (included)','date',l.ends_on||'');lastDayIncluded(until.input);
  return {node:dateRange(label,from,until),read:()=>({starts_on:from.input.value||null,ends_on:until.input.value||null})};
}
// ---- One rule per idea (catalogue v2): a Workload line's mode, measure and period; Rest between duties' modes ----
/** "duty"/"duties", "scheduled hour"/"scheduled hours": a measure (the server's ids) in words. */
export const measureWords=(m:string,n=2)=>{const base=m.replaceAll('_',' ');return n===1?base.replace(/ies$/,'y').replace(/s$/,''):base.endsWith('s')?base:base+'s';};
export const measureText=(l:Pick<RuleLine,'measure'|'numbers'>)=>l.measure&&!('apply' in (l.numbers||{}))?words(l.measure):'';
/** "per calendar week", "in any 7 days", "per 14-day block", "from 1 Sep to 30 Sep" (the end shown is the last day). */
export const periodText=(l:Pick<RuleLine,'period'|'numbers'>)=>{const p=l.period;if(!p||'apply' in (l.numbers||{}))return '';
  return p.kind==='rolling'?`in any ${p.days} days`:p.kind==='fixed'?`per ${p.days}-day block`
    :p.kind==='block'?(p.starts_on&&p.ends_on?`from ${formatDate(p.starts_on)} to ${formatDate(dayBefore(p.ends_on))}`:'per block'):`per ${words(p.kind).toLowerCase()}`;};
/** A line's mode in words, from the entry's modes: Workload counts or keeps people off (its Applies); Rest between duties
 *  is elapsed rest (its minimum), or a next-day combination or a preferred spacing (a duty-order setting). shortcut:
 *  reads the slot from the settings present, as slot_for_number does; send it from the server if modes grow. */
export function modeText(e:RuleCatalogueEntry,l:RuleLine,rule:Settings={}):string{
  const modes=e.modes||[],apply='apply' in (l.numbers||{});
  const source=l.source_bindings?.apply?.primitive_kind;
  const mode=modes.includes('no_duties')?(apply?'no_duties':'count'):!apply?modes[0]:source?(source==='shift_succession'?modes[2]:modes[1]):l.settings?.forbidden_transition?modes[1]:(l.settings?.shift_succession||rule.shift_succession)?modes[2]:modes[1];
  return mode?words(mode):'';
}
/** Over what a line's maximum counts when the rule lets it choose (a limit's run: max_hours, max_duties), its own
 *  setting, else the rule's, else the entry's: "per day", "per week", "in any 14 days" (SW3-B-13: "60 hours per period"
 *  hid a 14-day run). '' when the run is fixed by the rule itself (Duties per week says it in its name) or absent. */
export function windowText(e:RuleCatalogueEntry,l:Pick<RuleLine,'settings'>,rule:Settings={}):string{
  const slot=(e.numbers?.max||[]).find(x=>x.kind==='max_hours'||x.kind==='max_duties');if(!slot||slot.fixed?.window)return '';
  const s={...e.default_settings?.[slot.kind],...rule[slot.kind],...l.settings?.[slot.kind]},w=String(s.window??'week');
  return w==='rolling'?`in any ${s.window_days??7} days`:`per ${w}`;
}
/** What a line's numbers count, after them in a list: "duties per calendar week", "hours between duties", "no duties",
 *  "hours in any 14 days". */
export const unitText=(e:RuleCatalogueEntry,l:RuleLine,rule:Settings={})=>e.measures?(('apply' in (l.numbers||{}))?'no duties':[measureText(l).toLowerCase(),periodText(l)].filter(Boolean).join(' '))
  :e.modes?('min' in (l.numbers||{})?'hours between duties':modeText(e,l,rule).toLowerCase())
  :l.numbers?.max?.value!=null&&windowText(e,l,rule)?`${e.unit||''} ${windowText(e,l,rule)}`.trim():goalUnit(e,l,rule)||leadUnit(l,rule);
/** Team leads counted across concurrent duties together (count_scope), after the line's numbers: "1 team lead across
 *  these duties together". '' when counted per duty. */
const togetherWords=(l:RuleLine,rule:Settings)=>({...rule.team_lead_requirement,...l.settings?.team_lead_requirement}).count_scope==='concurrent_duties'?'across these duties together':'';
const leadUnit=(l:RuleLine,rule:Settings)=>{const t=togetherWords(l,rule),n=l.numbers?.max?.value??l.numbers?.min?.value;return t?`team lead${n===1?'':'s'} ${t}`:'';};
/** A Fair share line's goal: the entry's defaults under the rule's settings under the line's own. */
const goalOf=(e:RuleCatalogueEntry,l:RuleLine,rule:Settings)=>({...e.default_settings?.balance_goal,...rule.balance_goal,...l.settings?.balance_goal});
/** A Fair share line with its own measure: "to trainee sessions supervised over the calendar month" after Applies. */
const goalUnit=(e:RuleCatalogueEntry,l:RuleLine,rule:Settings)=>{if(!l.settings?.balance_goal?.metric)return '';
  const to=l.numbers?.apply&&!['min','max','target'].some(k=>l.numbers?.[k]?.value!=null)?'to ':'';
  return to+goalMeasure(goalOf(e,l,rule));};
/** The number names a line shows: Workload's Applies keeps people off duty. */
export const numberLabel=(e:RuleCatalogueEntry,k:string,long=false)=>k==='apply'&&e.modes?.includes('no_duties')?'No duties':(long&&LONG_LABELS[k])||NUMBER_LABELS[k]||k;
const PERIOD_DAYS=['rolling','fixed'],REPEATS=['calendar_week','calendar_month','rolling','fixed'];
/** "Counting ends 30 Sep 2026, before the line stops applying (no end)": the server's counting_ends facts in words
 *  (rule_lines.counting_attention), for Needs attention, a line's Counts and a person's targets. Ends are exclusive. */
export const countingEndText=(f:{counting_end:string;validity_end?:string|null})=>
  `Counting ends ${formatDate(dayBefore(f.counting_end))}, before the line stops applying (${f.validity_end?formatDate(dayBefore(f.validity_end)):'no end'})`;
/** Counts: what a Workload line counts and over which period (the server's measures and periods), in the shared
 *  dropdown. Hidden while the line keeps people off duty (No duties). The counting dates are the period's own, apart
 *  from the line's Valid dates. `counting` asks the server's rule check for this line's counting-end warning
 *  (`counting_attention`), so the panel says what Needs attention will say. */
export type CountingEnd={counting_end:string;validity_end:string|null};
/** A quota's counting window (its counting dates, the blocks' anchor and Repeats): one editor for a line's Counts and
 *  the bulk Change form's Counting window. Ends are exclusive (the field shows the last day). `keep` (several lines at
 *  once): the fields start empty and Repeats mixed, and what is left so keeps each line's own; `read` returns only what
 *  was given, in the quota's own keys. shortcut: in `keep` mode an end cannot be cleared (empty keeps it); add a
 *  "No end" choice if lines need one in bulk. */
export type CountingWindow={starts_on?:string|null;ends_on?:string|null;anchor_on?:string|null;repeat?:boolean};
export function countingWindowEditor(now:CountingWindow,keep=false){
  const block=validEditor({starts_on:now.starts_on,ends_on:now.ends_on},'Counting dates');
  const anchor=field(settingWords('block_quota','window_anchor_on'),'date',now.anchor_on||'');
  anchor.wrap.append(el('small',{class:'hint'},SETTING_HELP.window_anchor_on));
  const repeat=check(settingWords('block_quota','repeat'),!!now.repeat,SETTING_HELP.repeat+(keep?' Mixed (–): each line keeps its own.':''));
  if(keep)repeat.input.indeterminate=true;
  const read=()=>{const b=block.read(),out:Record<string,unknown>={};
    if(b.starts_on)out.starts_on=b.starts_on;if(b.ends_on)out.ends_on=b.ends_on;if(anchor.input.value)out.window_anchor_on=anchor.input.value;
    if(!repeat.input.indeterminate)out.repeat=repeat.input.checked;return out;};
  return {block,anchor,repeat,read,node:el('div',{class:'rc-lp-grid rc-counts'},block.node,anchor.wrap,repeat.wrap,
    ...(keep?[el('small',{class:'hint'},'Empty fields keep each line’s own.')]:[]))};
}
export function countFields(e:RuleCatalogueEntry,w:RuleLine,editable:boolean,read:(()=>void)[],counting?:()=>Promise<CountingEnd|null>,known?:CountingEnd|null):HTMLElement|null{
  if(!e.measures?.length||!e.periods?.length)return null;
  const was=w.period||{} as Partial<WorkloadPeriod>;
  const measure=select('Measure',e.measures.map(v=>({value:v,label:words(v)})),w.measure||'',true);
  const period=select('Period',e.periods.map(v=>({value:v,label:words(v)})),w.period?.kind||'',true);
  const days=field('Period days','number',w.period?.days??'',{min:1,max:366,step:1});
  const {block,anchor,repeat}=countingWindowEditor({starts_on:w.period?.starts_on,ends_on:w.period?.ends_on,anchor_on:was.anchor_on,repeat:was.repeat});
  const warn=el('p',{class:'warn-text rc-count-end',role:'status'});
  warn.hidden=true;
  // Asked 300 ms after the last change; an older answer never paints over a newer one.
  let timer=0,asked=0;
  const show=(a:CountingEnd|null)=>{warn.textContent=a?'⚠ '+countingEndText(a)+'. Move or clear the counting end.':'';warn.hidden=!a;};
  const paintWarn=()=>{if(!counting)return;clearTimeout(timer);timer=window.setTimeout(()=>{const n=++asked;
    counting().then(a=>{if(n===asked)show(a);}).catch(()=>undefined);},300);};
  const sync=()=>{const k=period.input.value;days.wrap.hidden=!PERIOD_DAYS.includes(k);days.input.required=!days.wrap.hidden;block.node.hidden=k==='day'||!k;
    anchor.wrap.hidden=k!=='fixed';repeat.wrap.hidden=!REPEATS.includes(k);};
  period.input.addEventListener('change',()=>{sync();paintWarn();});sync();
  // Opened unchanged: the check the page already has (the rule detail's); asked afresh only after an edit.
  if(known!==undefined)show(known);else paintWarn();
  const grid=el('div',{class:'rc-lp-grid rc-counts'},measure.wrap,period.wrap,days.wrap,block.node,anchor.wrap,repeat.wrap,warn);
  for(const x of grid.querySelectorAll<HTMLInputElement|HTMLSelectElement>('input,select'))x.disabled=!editable;
  // After the line's fields are read (its Valid dates decide the warning too).
  read.push(()=>{queueMicrotask(paintWarn);if(!editable||'apply' in (w.numbers||{})||!measure.input.value||!period.input.value)return;
    const kind=period.input.value as WorkloadPeriod['kind'],b=block.read(),n=Number(days.input.value),fixed=kind==='fixed';
    // In the server's key order (packages/contracts/rule_catalogue.py workload_fields), so an untouched period compares
    // equal. A value is sent when set and allowed for the kind; an empty one only when the period had it (cleared).
    const has=(k:keyof WorkloadPeriod,on:boolean,v:unknown)=>on&&v||k in was&&(on||!v)?{[k]:v}:{};
    w.measure=measure.input.value as RuleLine['measure'];
    w.period={kind,...has('repeat',REPEATS.includes(kind),repeat.input.checked),...(PERIOD_DAYS.includes(kind)&&n>0?{days:n}:{}),
      ...has('anchor_on',fixed,anchor.input.value||null),
      // Part blocks are prorated by working days automatically (quotas.py); a stored legacy switch is kept as it was
      // (still read by the server) and cleared only when the period stops being fixed, which the server requires.
      ...has('prorate_partial_windows',fixed,fixed&&!!was.prorate_partial_windows),
      ...has('starts_on',true,b.starts_on),...has('ends_on',true,b.ends_on)} as WorkloadPeriod;});
  return grid;
}
// ---- + New line, its duplicate check and the bulk bar ----
/** Per rule, the new line being written in its panel: not in the draft (nor in the rule's check) until it has a
 *  number; closing the panel before that drops it. */
const pendingLines=new Map<string,RuleLine>();
/** The line the rule line panel shows now, across redraws of the page (a redraw replaces the panel with itself). */
let lineShown:string|null=null;
export function newLinePicker(ctx:Context,e:RuleCatalogueEntry,anchor:HTMLElement,draft:{lines:RuleLine[]},changed:()=>void,open?:(id:string)=>void){
  // "Can work at" names people by the clinics they may work at: said so, so it is never taken for the clinic itself.
  // A new line names what is in use: inactive programmes, ranks, people… are not offered (lines already naming them stay).
  const inactive=(field:string,id:string)=>{const kind=RULE_FIELDS.find(x=>x.key===field)?.kind;return !!kind&&(ctx.catalogues[kind]||[]).some(r=>r.id===id&&r.active===false);};
  const choices=ruleConditionFields(ctx).flatMap(f=>f.choices.filter(c=>!inactive(f.key,c.value)).map(c=>({...c,value:f.key+'|'+c.value,group:c.group==='Tags'?'Tags':f.label,
    label:f.key==='eligible_service_ids'?'Can work at '+c.label:c.label})));
  openPicker(anchor,anchor.parentElement!,{label:'New line',choices,multiple:false,selected:new Set(),onPick:value=>{
    const [key,id]=value.split('|'),tree:Conditions={match:'all',conditions:[{field:key as Condition['field'],op:'any',values:[id]}]};
    const line:RuleLine={...newLine(e),numbers:{},...(SESSION_FIELDS.includes(key)?{where:tree}:{who:tree})};
    if(open){pendingLines.set(e.id,line);open(line.id);return;}
    draft.lines.push(line);changed();}});
}
function bulkActions(e:Entry,ids:string[],draft:Draft,changed:()=>void,redraw:()=>Promise<void>):BulkAction[]{
  const sel=()=>[{e,draft,ids:new Set(ids)}];
  // The universal Change… for lines that are saved: it proposes one new version per rule (approved by someone else).
  const saved=ids.filter(id=>(e.rule?.definition?.rule?.lines||[]).some((l:RuleLine)=>l.id===id));
  // It proposes from the saved rule: offered only while the rule has no unsaved edits (they would be lost, not proposed).
  const proposes=saved.length===ids.length&&changesIn(e,draft)===0;
  return [
    ...(proposes?[{label:'Propose a change…',icon:'edit' as const,primary:true,tip:'Sent for approval now, on its own; Numbers…, Valid dates… and the rest wait for this page’s Propose',run:(chosen:string[],done:()=>void)=>changesIn(e,draft)?notify('Propose or discard the unsaved edits of this rule first.','warning'):openChange({collection:'policy_lines',noun:['line','lines'],
      selection:lineSelection([e.rule!.id],chosen),governed:true,title:`Change ${plural(chosen.length,'line')} of ${e.name}`,onDone:async()=>{done();await redraw();}})}]:[]),
    {label:'Numbers…',icon:'edit',primary:!proposes,run:(_,done)=>numbersPopover(barButton('Numbers…'),sel(),()=>{changed();done();})},
    {label:'Valid dates…',icon:'calendar',run:(_,done)=>datesDialog(sel(),()=>{changed();done();})},
    {label:'Duplicate…',icon:'copy',more:true,run:(_,done)=>datesDialog(sel(),()=>{changed();done();},true)},
    {label:'Remove lines',icon:'trash',danger:true,armed:true,run:(_,done)=>{draft.lines=draft.lines.filter(l=>!ids.includes(l.id));changed();done();}},
  ];
}
/** Selected lines of one or several rules: each rule's draft and the ids chosen in it. */
export type Chosen={e:Entry;draft:Draft;ids:Set<string>}[];
/** The selection bar's button with this label (an action's popover is tied to it). */
export const barButton=(label:string)=>document.querySelector<HTMLElement>(`.selection-bar:not([hidden]) button[aria-label="${CSS.escape(label)}"]`)||document.body;
const chosenWords=(groups:Chosen)=>{const n=groups.reduce((a,g)=>a+g.ids.size,0);
  return groups.length===1?`Line${n===1?'':'s'} ${groups[0].draft.lines.flatMap((l,i)=>groups[0].ids.has(l.id)?[i+1]:[]).join(', ')}`:`${plural(n,'line')} in ${plural(groups.length,'rule')}`;};
/** Lines changed in every chosen rule, checked by the server first (the same line twice, a number a rule refuses):
 *  nothing changes unless every rule passes. `change` returns the new lines of one rule. */
async function checkedChange(groups:Chosen,change:(g:Chosen[number])=>RuleLine[]):Promise<string>{
  const next=groups.map(g=>({g,lines:change(g)}));
  const found=await Promise.all(next.map(x=>checkLines(x.g.e,{on:x.g.draft.on,lines:x.lines,settings:x.g.draft.settings})));
  const words=found.flatMap((r,i)=>[...r.duplicates.map(()=>`${next[i].g.e.name}: two lines would be the same line`),...r.issues.map(x=>`${next[i].g.e.name}: ${x.message}`)]);
  if(words.length)return words.join(' · ');
  for(const x of next){x.g.draft.lines=x.lines;keepDraft(x.g.e,x.g.draft);}
  return '';
}
/** Numbers… for the selected lines: Min, Max, Target (blank keeps each line's own; None removes it; Inherit leaves it
 *  blank, taken from a broader line), Keep / Mandatory / Advisory / Inherit, checked before it is applied. */
export function numbersPopover(anchor:HTMLElement,groups:Chosen,done:()=>void){
  const keys=['min','max','target'].filter(k=>groups.some(g=>k in g.e.numbers));
  popover(anchor,(panel,close)=>{
    panel.classList.add('dt-cell-pop','rc-numbers-pop');
    const inputs=keys.map(k=>{const f=field(NUMBER_LABELS[k],'number','',{step:'any',placeholder:'keep'});let mode='';
      const chips=['None','Inherit'].map(label=>{const b=el('button',{type:'button',class:'chip-toggle','aria-pressed':'false','aria-label':`${NUMBER_LABELS[k]}: ${label}`},label);
        b.addEventListener('click',()=>{mode=mode===label?'':label;for(const c of chips)c.setAttribute('aria-pressed',String(c.textContent===mode));f.input.disabled=!!mode;if(mode)f.input.value='';});return b;});
      f.wrap.append(el('span',{class:'rc-chips'},...chips));
      return {k,f,mode:()=>mode};});
    let level='';
    const levels=segmented('Mandatory, advisory or inherited',[['','Keep'],['mandatory','Mandatory'],['medium','Advisory'],['inherit','Inherit']],level,v=>{level=v;});
    const said=el('p',{class:'muted rc-numbers-said',role:'status'},`${chosenWords(groups)} · checked before applying`);
    const apply=button('Apply',async()=>{
      apply.disabled=true;said.textContent='Checking…';
      try{
        const why=await checkedChange(groups,g=>g.draft.lines.map(l=>{if(!g.ids.has(l.id))return l;const c=clone(l);
          for(const x of inputs){if(!(x.k in g.e.numbers))continue;const was=c.numbers[x.k];
            if(x.mode()==='None')c.numbers[x.k]={value:null,level:(was?was.level:defaultNumberLevel(g.e,x.k)) as 'mandatory'};
            else if(x.mode()==='Inherit')delete c.numbers[x.k];
            else if(x.f.input.value!=='')c.numbers[x.k]={value:Number(x.f.input.value),level:(was?was.level:defaultNumberLevel(g.e,x.k)) as 'mandatory'};}
          if(level)for(const [k,n] of Object.entries(c.numbers))n.level=(level==='inherit'?null:level==='mandatory'&&!mandatoryNumber(g.e,k)?(n.level&&n.level!=='mandatory'?n.level:'medium'):level) as 'mandatory';
          return c;}));
        if(why){said.textContent='⚠ '+why;said.classList.add('warn-text');return;}
        close();done();
      }catch(err){said.textContent='⚠ '+(err as Error).message;}finally{apply.disabled=false;}
    },'primary small');
    panel.append(el('div',{class:'dt-pop-label'},`Numbers · ${plural(groups.reduce((a,g)=>a+g.ids.size,0),'line')}`),el('p',{class:'muted'},'Blank keeps each line’s own · Inherit takes it from the broader line'),
      el('div',{class:'rc-lp-grid'},...inputs.map(x=>x.f.wrap)),levels,said,el('div',{class:'actions'},button('Cancel',close,'ghost small'),apply));
    inputs[0]?.f.input.focus();
  },{label:'Numbers',placement:'above-start'});
}
/** Valid dates… for the selected lines (checked first), or Duplicate…: copies of them, with these dates when given (a
 *  copy is a line to change next: the check marks it until it differs). */
export function datesDialog(groups:Chosen,done:()=>void,copy=false){
  const n=groups.reduce((a,g)=>a+g.ids.size,0);
  formDialog(copy?'Duplicate lines':'Valid dates',copy?`Copies of ${plural(n,'line')}; dates left empty keep each line’s own.`:`The same dates for ${plural(n,'line')}.`,form=>{
    const v=validEditor({});form.append(v.node);
    return async()=>{const d=v.read();
      // A copy has its line's who, so its specificity until the server checks it.
      if(copy){for(const g of groups){g.draft.lines.push(...g.draft.lines.filter(l=>g.ids.has(l.id)).map(l=>{const {source_bindings,source_default,...copy}=clone(l),id=crypto.randomUUID();
        return {...copy,id,starts_on:d.starts_on??l.starts_on,ends_on:d.ends_on??l.ends_on};}));keepDraft(g.e,g.draft);}done();return;}
      const why=await checkedChange(groups,g=>g.draft.lines.map(l=>g.ids.has(l.id)?{...clone(l),starts_on:d.starts_on,ends_on:d.ends_on}:l));
      if(why)throw new Error(why);
      done();};},copy?'Duplicate':'Apply to all');
}
/** Every version of this rule, newest first, with what each changed (line by line). It opens at once, so Escape gives
 *  the focus back to History, and fills in a page at a time from this rule's own history (CA-08: never the department's
 *  retired rules); Show more reads the next page. */
function historyDialog(ctx:Context,e:Entry,names:Namer){
  const {body}=showDialog(`History · ${e.name}`,'Every version, newest first. Retiring a version brings back the one before.',body=>body.append(skeleton('lines','Loading history…')));
  const versions:Row[]=[];let page=0;
  const read=()=>get<{policies:Row[];has_more:boolean}>(`/api/rules/catalogue/${encodeURIComponent(e.id)}/history?version=${e.catalogue_version||1}&page=${page+1}&limit=50`)
    .then(r=>{if(!body.isConnected)return;const first=versions.length;page++;versions.push(...r.policies);draw(r.has_more);
      // Show more keeps the reader's place: focus moves to the first version it added.
      if(first)body.querySelectorAll<HTMLElement>('.rc-history>li')[first]?.focus();},
      (error:Error)=>{if(!body.isConnected)return;const failed=readFailed('the history',error,()=>void read());
        if(versions.length)body.querySelector('.rc-history-more')?.replaceWith(failed);else body.replaceChildren(failed);});
  const draw=(more:boolean)=>{
  body.replaceChildren(versions.length?el('ol',{class:'rc-history'},...versions.map((v,i)=>{
    // The oldest version on a page compares with the next page's first once it arrives; until then nothing is claimed.
    const older=versions[i+1],rule=v.definition.rule;
    // Who decided: a name when the server sends one (approved_by_name), "you" for your own; activated by its proposer = self-approved.
    const by=(id:unknown,name:unknown)=>name?String(name):id&&id===ctx.me.identity_id?'you':'',approver=by(v.approved_by,v.approved_by_name);
    const self=!!v.approved_by&&v.approved_by===v.created_by;
    const source=String(v.source||''),brief=source.split('\n')[0].slice(0,120);
    return el('li',{tabindex:-1},el('div',{},statusBadge(v.status),' ',el('strong',{},formatDate(String(v.created_at||v.starts_on))),
      v.approved_by?el('span',{class:'muted'},` · approved${approver?' by '+approver:''}`):'',self?[' ',selfApprovedBadge()]:''),
      // The reason as given; a long one (an import's log, file references) opens on demand under its first line.
      source.length>brief.length?el('details',{class:'rc-source'},el('summary',{},brief+'…'),el('p',{class:'muted'},source)):source?el('p',{class:'muted'},source):'',
      older&&(older.definition.rule.on!==false)!==(rule.on!==false)?el('p',{},rule.on===false?'Turned off':'Turned on'):'',
      older||!more?changeList(e,(older?.definition.rule.lines||[]) as RuleLine[],(rule.lines||[]) as RuleLine[],names,(rule.settings||{}) as Settings):'');})):empty('No versions yet','The first proposal starts its history.',null,'rules'),
    more?el('div',{class:'rc-history-more'},button('Show more',read,'secondary small')):'');
  };
  void read();
}
/** Rules' secondary actions in one ⋯ menu beside the primary ones (RUL-02): each keeps its own button (its state,
 *  relabels and hiding stay its own) out of sight, and the menu lists the ones shown now under their current names, so
 *  a pending count ("Exceptions (2 pending)") reads in the menu. `pending` is also said on the menu button. */
export function moreActions(label:string,items:Child[],pending=0):HTMLElement{
  const held=el('span',{hidden:true},...items);
  const visible=(b:HTMLButtonElement)=>{for(let n:HTMLElement|null=b;n&&n!==held;n=n.parentElement)if(n.hidden)return false;return !b.disabled;};
  let close=()=>{};
  // Keep each entry in place when a read relabels it. A hidden slot holds the order for actions that appear later.
  const entries=[...held.querySelectorAll<HTMLButtonElement>('button')].map(b=>{
    const caption=el('span',{}),slot=el('span',{hidden:true});
    const item=el('button',{type:'button',role:'menuitem',class:'menu-item',tabindex:-1},b.querySelector('svg')?.cloneNode(true)||'',caption);
    item.addEventListener('click',()=>{close();b.click();});
    return {b,caption,slot,item};
  });
  const sync=()=>{
    for(const {b,caption,slot,item} of entries){
      const text=b.getAttribute('aria-label')||b.textContent?.trim()||'';
      if(caption.textContent!==text)caption.textContent=text;
      if(visible(b))slot.replaceWith(item);
      else if(item.parentElement){
        const panel=item.parentElement,focused=document.activeElement===item;
        item.replaceWith(slot);
        item.tabIndex=-1;
        if(focused)refocusItem(panel,text);
      }
    }
  };
  new MutationObserver(sync).observe(held,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['aria-label','hidden','disabled']});
  const more:HTMLButtonElement=opens(iconButton(pending?`${label}, ${plural(pending,'item')} waiting`:label,'dots',()=>{
    close=menu(more,label,()=>{sync();return entries.map(({b,item,slot})=>visible(b)?item:slot);},{placement:'below-end'});}),'menu');
  if(pending)more.append(el('span',{class:'nav-count','aria-hidden':'true'},String(pending)));
  return el('span',{class:'rc-more'},more,held);
}
/** Rules in force that are not in the catalogue yet: they keep applying as they are (read-only here). */
export const keptRules=(ctx:Context)=>(ctx.catalogues.policies||[]).filter(p=>p.status==='active'&&p.kind!=='catalogue'&&!p.definition?.workspace_id);
/** Proposals for an older rule kind (not in the catalogue): one list for the ⋯ menu's count and Waiting for approval. */
export const olderPending=(ctx:Context)=>(ctx.catalogues.policies||[]).filter(p=>p.status==='proposed'&&p.kind!=='catalogue');
const recordName=(ctx:Context)=>(kind:string,id:string)=>labelOf((ctx.catalogues[kind]||[]).find(r=>r.id===id));
/** What an older rule does, in words (its Effect in Other pending rules). */
export const olderEffect=(ctx:Context,p:Row)=>ruleSentence(p.definition.rule,p.definition,recordName(ctx));
/** May check an older-kind proposal against current rotas (its Review). */
export const mayReviewOlder=(ctx:Context)=>ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER);
/** An older-kind proposal's decision panel (check, Activate, Decline): Activate for another approver, or the proposer
 *  under a self-approval permission (mayDecide). */
export const reviewOlder=(ctx:Context,p:Row,refresh:()=>Promise<void>)=>ruleReview(p,mayReviewOlder(ctx),ctx.permits(CAPABILITY.APPROVER)&&mayDecide(ctx.me,'rule.activate',p.created_by===ctx.me.identity_id),
  refresh,recordName(ctx),ctx.me.organisation.timezone,false,[],labelOf(p),'',ctx);
function otherDepartmentRules(rows:Row[],effect:(rule:Row)=>string,levels:(rule:Row)=>(string|null)[],open?:(rule:Row)=>void,
    effectCell:(rule:Row)=>Child=effect){
  if(!rows.length)return null;
  return compact(button(`Other department rules (${rows.length})`,()=>dockPanel({key:'other-department-rules',title:'Other department rules',width:640,
    build:({body})=>body.append(...(open?[el('p',{class:'muted'},'These approved department rules also apply during their valid dates.')]:[]),
      dataTable<Row>({id:'other-department-rules',noun:['rule','rules'],rows,rowId:p=>p.id,pageSize:25,
        searchLabel:'Search other department rules',searchPlaceholder:'Name or effect',onOpen:open,
        defaults:{wrap:['name','effect']},
        columns:[{key:'name',label:'Rule',text:labelOf,cell:p=>open?button(labelOf(p),()=>open(p),'link-button'):
          p.definition.rule.on===false?el('span',{},labelOf(p),' ',badge('Off','neutral')):labelOf(p)},
          {key:'effect',label:'Effect',text:effect,cell:effectCell},
          {key:'level',label:'Level',text:p=>levels(p).map(levelWords).join(', '),
            cell:p=>el('span',{},...levels(p).map(level=>levelBadge(level,p.severity_reason)))},
          {key:'dates',label:'Valid dates',text:p=>`${p.starts_on||''} ${p.ends_on?dayBefore(p.ends_on):''}`,
            cell:p=>dateUnits(p.starts_on?[formatDate(p.starts_on),'– '+(p.ends_on?formatDate(dayBefore(p.ends_on)):'Ongoing')]:
              [p.ends_on?'Until '+formatDate(dayBefore(p.ends_on)):'Ongoing'])}]}).root)}),
    'secondary'),'layers');
}
export function capturedRulesContext(ctx:Context,data:Catalogue){
  const name=recordName(ctx),names=namer(ctx);
  const effect=(p:Row)=>ruleSentence(p.definition.rule,p.definition,name);
  const entryOf=(rule:Row)=>(rule.catalogue_version===2?RULE_CATALOGUE_V2:RULE_CATALOGUE).find(e=>e.id===rule.id);
  const levels=(p:Row):(string|null)[]=>{
    const rule=p.definition.rule;
    if(rule.kind!=='catalogue')return [p.effective_severity??p.definition.severity];
    return capturedRuleLevels(rule.lines);
  };
  const lines=(p:Row):string[]=>{
    const definition=p.definition,rule=definition.rule;
    if(rule.kind==='catalogue'){
      const entry=entryOf(rule);
      return entry?(rule.lines as RuleLine[]).map(l=>lineWords(entry,l,names,Infinity,rule.settings)):[];
    }
    return (definition.per_person||[]).map((l:Row)=>[
      l.person_id?name('people',l.person_id):'',l.programme_id?name('programmes',l.programme_id):'',
      ...(l.category_ids||[]).map((id:string)=>name('categories',id)),
      lineNumbers(rule,l),lineSessions(l,name),
      daysText({weekdays:l.weekdays,start_time:l.start_time,end_time:l.end_time}),
      validText({starts_on:l.starts_on,ends_on:l.ends_on}),
    ].filter(Boolean).join(' · '));
  };
  return otherDepartmentRules(data.captured_other_rules||[],effect,levels,undefined,p=>{
    const rows=lines(p),summary=effect(p);
    return rows.length?el('details',{},el('summary',{},summary||plural(rows.length,'line')),el('ol',{},...rows.map(text=>el('li',{},text)))):summary;
  });
}
/** Pending proposals stay actionable; additional approved rules stay in their on-demand context. */
export function otherRulesContext(ctx:Context,kept:Row[],o:Options,refresh:()=>Promise<void>):HTMLElement|null{
  const pending=olderPending(ctx);
  if(!kept.length&&!pending.length)return null;
  const name=recordName(ctx);
  // A labelled button, not a second "?" beside the page's own: these rules apply too (an old department's are all here).
  const context=otherDepartmentRules(kept,p=>ruleSentence(p.definition.rule,p.definition,name),
    p=>[p.effective_severity??p.definition.severity],o.openRule);
  if(!pending.length)return context;
  const canCheck=mayReviewOlder(ctx),canDecide=ctx.permits(CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.ADMIN);
  const changed=async()=>{closeDock();await refresh();};
  // Folds to its icon beside a side panel, like the other header buttons.
  const waiting=compact(button(`Other pending rules (${pending.length})`,()=>dockPanel({key:'pending-rules',title:'Other pending rules',width:640,
    build:({body})=>body.append(dataTable<Row>({id:'pending-rules',noun:['rule','rules'],rows:pending,rowId:p=>p.id,pageSize:25,
      searchLabel:'Search pending rules',searchPlaceholder:'Name or effect',onOpen:p=>o.openRule?.(p),
      // A 640 px panel: the decisions are in each row's ⋯ (no Actions column cut off the edge) and long text wraps.
      defaults:{wrap:['name','effect']},
      columns:[{key:'name',label:'Rule',text:labelOf,cell:p=>button(labelOf(p),()=>o.openRule?.(p),'link-button')},
        {key:'effect',label:'Effect',text:p=>ruleSentence(p.definition.rule,p.definition,name),cell:p=>ruleSentence(p.definition.rule,p.definition,name)}],
      rowActions:p=>{const mine=p.created_by===ctx.me.identity_id;return [
        ...(canCheck?[{label:'Review',icon:'shield' as const,run:()=>reviewOlder(ctx,p,changed)}]:[]),
        // Withdrawing your own proposal requires a recorded reason; Undo puts it back waiting.
        ...(canDecide&&mine?[{label:'Withdraw',icon:'close' as const,danger:true,run:()=>confirmAction(`Withdraw ${labelOf(p)}`,'The proposal stays in History; Undo puts it back waiting.',reason=>declineProposal(p,reason,'Proposal withdrawn.',changed),'Withdraw')}]:[]),
        ...(canDecide&&!mine?[{label:'Decline',icon:'close' as const,danger:true,run:()=>confirmAction(`Decline ${labelOf(p)}`,'The proposal is kept in History.',reason=>declineProposal(p,reason,'Proposal declined.',changed),'Decline')}]:[])];},
      // Many proposals (a Simplify blocker lists each): decline those others proposed together, with one reason and one Undo.
      bulk:canDecide?ids=>{const others=pending.filter(p=>ids.includes(p.id)&&p.created_by!==ctx.me.identity_id);if(!others.length)return [];
        return [{label:`Decline ${others.length}`,icon:'close',danger:true,run:(_:string[],done:()=>void)=>confirmAction(`Decline ${plural(others.length,'proposal')}`,'They are kept in History.',
          // One bulk change (one command, one Undo), as the legacy Rules page declines several.
          async reason=>{await runBulk({collection:'policies',noun:['proposal','proposals'],destructive:true,verb:'Decline',message:'{n} proposals declined.',onDone:async()=>{done();await changed();},
            body:{item_kind:'policy.decline',items:others.map(p=>({target_id:p.id,expected_revision:p.revision})),reason}});},'Decline')}];}:undefined}).root)}),'secondary'),'clock');
  return el('span',{},context,waiting);
}
