/** The Rules page as one database (mockups R9, R9b, R10; docs/design/rule-catalogue.md §6, §8): families as group rows,
 *  each catalogue rule a row that opens to its numbered lines, tri-state boxes rolling up line › rule › family ›
 *  select-all, one selection bar, and the Needs attention view. A rule row is its name and one count line. Every change goes into
 *  the rules' drafts and is proposed together ("Propose N changes"): one version per rule, each approved by another
 *  person. */
import {el,badge,helpButton,statusTone,disabledWhy,type BadgeTone,button,segmented,check,compact,confirmAction,empty,footerStatus,formatDate,icon,labelOf,notify,plural,relabel,routineReason,sectionHead,armed,type Child,popup} from './dom.js';
import {TABLE_CONDITION_DEFINITIONS,type Conditions} from './generated.js';
import {command,sessionKey,type Undone} from './api.js';
import {savingMark,failedMark,conflict} from './loading.js';
import {collectionToolbar,conditionFilter,holds,joinFilter,mentions,savedViewItems,fetchViews,openingView,type SavedView,type SortKey} from './table_view.js';
import {selectionBar,type SelectionAction} from './selection_bar.js';
import {setTriState,triStateOf} from './check_tree.js';
import {columnHeader,wrapAllColumns,columnGrip,sized,sizeColumn} from './table_menu.js';
import {ruleMark,proposalPanel,movedNotes,groupings,specificFirst,exceptionTree,ruleCounts,ruleLevels,whoWins,reachNode,levelOf,validFacets,effectiveOf,proposeTip,proposeButton,replacedWords,whenWords,unitWords,setsSafety,safetyLock,tickGates,unitText,appliesWord,measureText,periodText,countingEndText} from './rule_catalogue.js';
import {openPicker} from './picker.js';
import {unexplainedLine,type DepartmentRules,type RuleSummary} from './rules_explained.js';
import {levelBadge} from './rule_view.js';
import {issueGlyph} from './issue_marker.js';
import {tagsFor} from './tags.js';
import {ruleProposing,ruleKey,redrawWhenSettled} from './bulk.js';
import {simplifyButton} from './rule_simplify.js';
import {RULE_ENDING_DAYS,TERMS} from './terms.js';
import {normalSearch,recordMatcher,searchWords,serverCapability,type SearchOptions,type SearchRecord} from './search.js';
import type {Context} from './forms.js';
import type {RuleLine,RulesViewSettings} from './generated.js';
import type {Row} from './types.js';
import {WAITING,linesOf,isOn,numberText,draftOf,draftView,keepDraft,discardDraft,rememberProposed,restoreProposedDrafts,changesOf,changesIn,changedRules,namer,treeText,pills,numbersText,numbersNode,sourceWords,daysText,validText,lineWords,
  newLine,newLinePicker,propose,moreActions,checkBeforeProposing,checkWords,changeList,missingNeeds,settingsOf,numbersPopover,datesDialog,barButton,keptRules,otherRulesContext,capturedRulesContext,ruleViewKey,validNode,olderPending,olderEffect,mayReviewOlder,reviewOlder,
  type Catalogue,type Entry,type Issue,type Fix,type Options,type Chosen,type Namer,type Draft} from './rule_catalogue.js';

type Mode=NonNullable<RulesViewSettings['mode']>;
/** `order`: the columns after Rule / who, as moved (Move left / right). `filters` + `conditions`: the shared Filter's
 *  tree (table_view.ts conditionFilter); `then_sort`: sort keys after `sort`. */
/** `widths`: columns the person resized (px); a named view keeps the contract's columns (TABLE_CONDITION_DEFINITIONS
 *  `rule-tree`), the others (Reach, the issue columns) stay on this device. */
type State={search_options?:SearchOptions;
  /** Set once a kept state has moved to the Question default (Why panel v2); a grouping chosen after that stays. */question_default?:boolean;mode:Mode;q:string;filters:Record<string,string[]>;conditions:Conditions|null;group:string;sort:SortKey|null;then_sort:SortKey[];open:string[];wrap:string[];hidden:string[];order:string[];widths:Record<string,number>};
const DEFAULT:State={mode:'rules',q:'',search_options:undefined,filters:{},conditions:null,group:'question',sort:null,then_sort:[],open:[],wrap:[],hidden:[],order:[],widths:{}};
type Saved=Omit<State,'mode'|'open'|'conditions'|'then_sort'|'widths'>&{conditions?:Conditions;then_sort?:SortKey[];widths?:Record<string,number>|null;rules:RulesViewSettings};
const savedWidths=(w:Record<string,number>)=>{const kept=Object.entries(w).filter(([k,n])=>n>0&&Object.hasOwn(TABLE_CONDITION_DEFINITIONS['rule-tree'],k));return kept.length?Object.fromEntries(kept):null;};
const definitionOf=(s:State):Saved=>({q:s.q,...(normalSearch(s.search_options)?{search_options:normalSearch(s.search_options)}:{}),filters:s.filters,...(s.conditions?{conditions:s.conditions}:{}),group:s.group,sort:s.sort,...(s.then_sort.length?{then_sort:s.then_sort}:{}),
  wrap:s.wrap,hidden:s.hidden,order:s.order,...(savedWidths(s.widths)?{widths:savedWidths(s.widths)}:{}),rules:{mode:s.mode,open:s.open}});
const stateOf=(d:Partial<Saved>):State=>structuredClone({mode:d.rules?.mode??DEFAULT.mode,q:d.q??'',search_options:normalSearch(d.search_options),filters:d.filters||{},conditions:d.conditions||null,group:d.group??DEFAULT.group,sort:d.sort??null,
  then_sort:d.then_sort||[],open:d.rules?.open||[],wrap:d.wrap||[],hidden:d.hidden||[],order:d.order||[],widths:{...(d.widths||{})}});
const STORE='rota.rules.tree';
/** The Rules collection's properties as the contract lists them (TABLE_CONDITION_DEFINITIONS['rule-tree']). */
const RULE_TREE=TABLE_CONDITION_DEFINITIONS['rule-tree'];
/** What Rules search can tell apart: the rule lines' own text and the records their who and where name (the server's
 *  policy_lines registry; the table key rule-tree is a view, not a collection). */
const LINE_SEARCH=serverCapability('policy_lines')!;
const fieldsOf=(t:Conditions):string[]=>t.conditions.flatMap(n=>'conditions' in n?fieldsOf(n):[n.field]);
const read=<T>(key:string,fallback:T):T=>{try{return {...fallback,...JSON.parse(localStorage.getItem(key)||'null')};}catch{return fallback;}};
const write=(key:string,value:unknown)=>{try{localStorage.setItem(key,JSON.stringify(value));}catch{/* this browser keeps nothing: the defaults apply */}};
let state:State|null=null,viewId='',treeSession=-1,treeStore='';
let treePosition:{scope:string;layout:'rules'|'issues';left:number;top:number}|null=null;
/** Page renderers prepare detached content; native coordinates are restored only after the page is mounted. */
export function restoreRulesScroll(root:HTMLElement){
  if(treeSession!==sessionKey()||!treePosition)return;
  // A newly drawn scroller is already at the origin. Touching its native coordinates here would force a layout of
  // the entire table after every search, even when there is no position to restore.
  if(!treePosition.left&&!treePosition.top)return;
  const scroll=root.querySelector<HTMLElement>('.rt-body>.rt-scroll');
  if(!scroll?.isConnected)return;
  scroll.scrollLeft=treePosition.left;scroll.scrollTop=treePosition.top;
  treePosition={...treePosition,left:scroll.scrollLeft,top:scroll.scrollTop};
}
/** Undo restores the Rules collection's own state before the page is drawn again. */
export function restoreRulesViews(done:Undone){
  restoreProposedDrafts(done);
  if(treeSession!==sessionKey()||!treeStore)return;
  for(const item of done){
    const restored=item.kind.startsWith('table_view.')&&item.result?.view;
    if(!restored||restored.table_key!=='rule-tree')continue;
    state=stateOf(restored.definition);viewId=restored.id;
    write(treeStore,{...state,viewId});
  }
}
const ISSUE_COLS=[{key:'issue-rule',label:'Rule'},{key:'issue-lines',label:'Lines'},{key:'issue-what',label:'What happens'},{key:'issue-recommendation',label:'Recommendation'}];
/** Issue kinds, in the order the server sorts them, with their words. */
export const KINDS:Record<string,{label:string;tone:BadgeTone;about:string}>={
  inherits:{label:'Inherits a conflict',tone:'err',about:'a blank number taken from a broader line contradicts the line’s own'},
  shadowed:{label:'Applies to no one',tone:'warn',about:'more specific lines replace every number it sets, for all its people today'},
  both:{label:'Both apply',tone:'warn',about:'equally specific lines both count; the stricter number binds'},
  duplicate:{label:'Duplicate',tone:'err',about:'the same who, where and days twice'},
  ending:{label:'Ending soon',tone:'info',about:`valid dates end within ${RULE_ENDING_DAYS} days`},
  counting_ends:{label:'Counting ends early',tone:'warn',about:'a counting period ends before the line stops applying'},
  nobody:{label:'Reaches nobody',tone:'',about:'no active person matches today'},
  blocks:{label:'Blocks a draft',tone:'err',about:'a mandatory line causes schedule issues'}};
/** A kind's words; one the server adds before this client knows it still shows, under its own name (never dropped). */
const kindOf=(k:string)=>KINDS[k]||{label:k.charAt(0).toUpperCase()+k.slice(1).replace(/_/g,' '),tone:'' as BadgeTone,about:''};
/** Older-kind proposals are rows of Waiting for approval: their one property is that they wait, so they show when the
 *  Filter asks for waiting rules and nothing else in it excludes them; a search matches their name. */
export const olderShown=(older:Row[],t:Conditions|null,name?:(text:string)=>boolean):Row[]=>
  t&&mentions(t,'state',[WAITING])&&holds(t,k=>k==='state'?[WAITING]:[],()=>'multiple_choice')?older.filter(p=>!name||name(labelOf(p))):[];
/** Waiting for approval's count: catalogue rules with a proposal and older-kind proposals, each once. */
export const waitingCount=(entries:Entry[],older:Row[])=>entries.filter(e=>e.proposed).length+older.length;
// Acknowledged issues are shared by the department (rule.acknowledge): they leave Needs attention for everyone, show under
// Acknowledged with who and when, and come back when their lines change. Fixes applied to a draft stay hidden until it
// is proposed or discarded.
const applied=new Set<string>();

/** The Rules list's saved views (one shared read per session, table_view.ts fetchViews). */
export const ruleTreeViews=()=>fetchViews<Saved>('rule-tree');
/** `byQuestion`: the server's department projection (GET /api/rules-explained?group_by=question): the questions A–F in
 *  its order, one sentence per rule and the conflicts; null while that route is not served. */
export function rulesDatabase(ctx:Context,target:HTMLElement,data:Catalogue,o:Options,canEdit:boolean,refresh:()=>Promise<void>,byQuestion:DepartmentRules|null=null,
    sourceControl?:()=>HTMLElement|'',sourceLabel?:string){
  const captured=data.source?.kind==='snapshot';
  const openTag=captured?undefined:o.openTag;
  const ruleView=(e:Entry)=>captured?{on:isOn(e),lines:linesOf(e)}:draftView(e);
  const ruleChanges=(e:Entry)=>captured?0:changesOf(e);
  const store=`${STORE}.${ctx.me.identity_id}.${ctx.me.department.id}`;
  if(treeSession!==sessionKey()||treeStore!==store){state=null;viewId='';treePosition=null;applied.clear();treeSession=sessionKey();treeStore=store;}
  if(!state){const {viewId:id='',...kept}=read<State&{viewId?:string}>(store,{...DEFAULT,filters:{}});
    // Question became the default grouping (Why panel v2): a device that kept the old default (Family) moves to it once
    // (a missing grouping already reads the new default); widths, wraps and the rest stay, and a grouping chosen since is kept.
    if(!kept.question_default){if(kept.group==='family')kept.group='question';kept.question_default=true;write(store,{...kept,viewId:id});}state={...kept,conditions:kept.conditions||null,then_sort:kept.then_sort||[],widths:kept.widths||{}};viewId=id;}
  const s=state;
  // By question needs the projection; when its read failed (rule_catalogue.ts says so) the list groups by family.
  const grouping=()=>s.group==='question'&&!byQuestion?'family':s.group;
  // A rule's summary by its catalogue entry: the first the server lists (its order), never a wording of our own.
  const summaries=new Map<string,RuleSummary>();for(const r of byQuestion?.rules??[])if(r.catalogue_id&&(!summaries.has(r.catalogue_id)||r.unexplained))summaries.set(r.catalogue_id,r);  // an unexplained row is never hidden by an explained twin (DL-037)
  const questionOf=(e:Entry)=>grouping()==='question'?summaries.get(e.id):undefined;
  // The catalogue entry says it; only a rule mixing safety and other kinds needs the server's line-aware answer.
  const safe=(e:Entry)=>e.mixed_safety?!!summaries.get(e.id)?.safety:!!e.safety;
  // DL-037: a fact about the rules, said in every grouping.
  const unexplainedCount=()=>[...summaries.values()].filter(r=>r.unexplained).length;
  const names=namer(ctx),all=data.entries,byId=new Map(all.map(e=>[e.id,e])),families=new Map(data.families.map(f=>[f.id,f.name]));
  // Catalogue v2 defaults to current lines; retained empty/off/ended data is available in the collection filter.
  const validity=new Map(captured?all.flatMap(e=>Object.entries(e.line_validity||{})):[]);
  const effective=(l:RuleLine)=>validity.get(l.id)?.effective??effectiveOf(l,data.day);
  const v2=data.catalogue_version===2,ended=(l:RuleLine)=>v2&&effective(l)==='ended';
  // Read through draftView (no copy of every line per question): the list asks these several times per rule per draw.
  const availability=(e:Entry)=>!ruleView(e).lines.length?'Empty':!isOn(e)?'Off':ruleView(e).lines.every(ended)?'Ended only':'In use';
  const current=(e:Entry)=>!v2||!!e.proposed||ruleChanges(e)>0||isOn(e)&&ruleView(e).lines.some(l=>!ended(l));
  // the list has no columns; a saved view's column settings (wrap, hidden, order, widths) load and are ignored.
  // Line facets stay in Filter and line sorts in Sort.
  const everyDay=daysText({numbers:{}} as unknown as RuleLine);  // a line on every day, all day: no When words
  const g=groupings(ctx,names,data.day);
  const LINE_FACETS:Record<string,[string,(l:RuleLine)=>string|string[]]>={who:['Who',l=>g.kind(l)],
    where:['Where',l=>g.of(l.where,['service_ids','location_ids','assignment_type_ids'],'All clinics')],numbers:['Numbers',l=>levelOf(l)],
    days:['Days',l=>daysText(l)],valid:['Valid',l=>validFacets(l,data.day,effective(l))],
    ...(v2?{measure:['Measure',l=>measureText(l)||'No measure'],period:['Period',l=>periodText(l)||'No period']} as Record<string,[string,(l:RuleLine)=>string]>:{})};
  const LINE_SORT_LABELS:[string,string][]=[['where','Where'],...(v2?[['measure','Measure'],['period','Period']] as [string,string][]:[]),['numbers','Numbers'],['days','Days'],['valid','Valid']];
  const LINE_SORT:Record<string,(e:Entry,l:RuleLine)=>string>={name:(_,l)=>treeText(l.who,names,'Everyone'),where:(_,l)=>treeText(l.where,names,'All clinics'),
    numbers:(e,l)=>numbersText(l,e.inherited?.[l.id]),days:(_,l)=>(l.weekdays||[0,1,2,3,4,5,6]).join(),valid:(_,l)=>l.ends_on||'9999',measure:(_,l)=>measureText(l),period:(_,l)=>periodText(l)};
  const facetValues=(k:string)=>[...new Set(all.flatMap(e=>lines(e).flatMap(l=>[LINE_FACETS[k][1](l)].flat())))].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
  /** The Rules collection's properties, defined once (keys: the contract's `rule-tree` table): [name, values, group].
   *  The Filter offers them, and `shown()` evaluates the tree over them: a rule's
   *  own values, and each line's for the Lines ones (a rule shows with the lines that match). */
  const properties=(attention:boolean):Record<string,[string,string[],string]>=>{
    const all:Record<string,[string,string[],string]>=attention?{kind:['Issue',Object.keys(KINDS).map(k=>KINDS[k].label),'Issue'],rule:['Rule',[...new Set(data.attention.map(x=>byId.get(x.catalogue_id)?.name||''))],'Issue']}
      :{state:['Rule state',['On','Off',WAITING,'Changed'],'Rule'],family:['Family',data.families.map(f=>f.name),'Rule'],issue:['Checks',['Overlaps',`Ends within ${RULE_ENDING_DAYS} days`,'Needs attention'],'Rule'],
        ...(v2?{availability:['Availability',['In use','Empty','Off','Ended only'],'Rule'] as [string,string[],string]}:{}),
        ...Object.fromEntries(Object.entries(LINE_FACETS).map(([k,[label]])=>[k,[`Lines: ${label}`,facetValues(k),'Lines']]))};
    return Object.fromEntries(Object.entries(all).filter(([k])=>k in RULE_TREE));};
  const tree=()=>joinFilter(s.filters,s.conditions);
  const choice=()=>'multiple_choice' as const;
  const save=()=>write(store,{...s,viewId});
  if(!captured)for(const id of [...applied]){const e=all.find(x=>x.rule?.id===id.split(':')[1]);if(!e||!changesOf(e))applied.delete(id);}
  const issues=()=>data.attention.filter(x=>!x.acknowledged&&(captured||!applied.has(x.id))),acked=data.attention.filter(x=>x.acknowledged);
  const attentionCount=(list:Issue[])=>new Set(list.flatMap(x=>x.line_ids||[])).size+list.filter(x=>!(x.line_ids||[]).length).length;
  /** SW3-D-20: one number for what needs attention, in the badge's unit (lines), wherever it is said. */
  const attentionTotal=()=>{const c=data.attention_counts;return applied.size||!c?attentionCount(issues()):c.lines||c.rules||0;};
  /** Acknowledge issues for everyone (or take that back), with Undo; the page reads them again from the server. */
  async function ack(list:Issue[],on:boolean,note=''){
    if(!list.length)return false;
    // Optimistic: the rows read as saving at once; a refusal puts them back with "Couldn't save · Retry" (a changed
    // issue, 409: the page reads what stands now, no Retry).
    const cells=()=>list.map(x=>document.querySelector(`tr[data-issue="${x.id}"] td:last-child`)).filter((c):c is Element=>!!c);
    const send=async(acknowledging:boolean,text:string,restoreNotes=false)=>{
      cells().forEach(c=>c.replaceChildren(savingMark(acknowledging?'Acknowledging':'Taking back')));
      let failed:unknown=null,done=0;
      const groups=new Map<string,Issue[]>();
      for(const x of list){const note=restoreNotes?x.acknowledged?.note||'':text;groups.set(note,[...(groups.get(note)||[]),x]);}
      try{for(const [note,items] of groups){await command(acknowledging?'rule.acknowledge':'rule.unacknowledge',{fingerprints:items.map(x=>x.fingerprint),note});done+=items.length;}return true;}
      catch(err){failed=err;notify(`${done?`${done} of ${list.length} restored. `:''}${(err as Error).message}`,'error',undefined,err);return false;}
      finally{picked.clear();await refresh();
        if(failed)cells().forEach(c=>c.replaceChildren(failedMark(conflict(failed)?undefined:()=>void send(acknowledging,text,restoreNotes))));}};
    if(!await send(on,note))return false;
    notify(on?`${plural(list.length,'issue')} acknowledged for everyone.`:`${plural(list.length,'acknowledgement')} taken back.`,'success',{label:'Undo',run:()=>void send(!on,'',!on)});
    return true;
  }
  /** Ask for an optional note, then acknowledge. */
  const acknowledge=(list:Issue[])=>confirmAction(`Acknowledge ${plural(list.length,'issue')}`,'Everyone who opens Rules sees them as acknowledged, with your name and note, until their lines change. The rules and rota checks do not change.',
    async reason=>{await ack(list,true,reason===routineReason('')?'':reason);},'Acknowledge',undefined,true);
  const selected=new Set<string>(),picked=new Set<string>();  // rules mode: 'l:'+line / 'r:'+rule; attention mode: issue ids
  // A rule with a bulk proposal on its way reads only until it is in (rule_catalogue.ts rulePage does the same).
  const editable=(e:Entry)=>canEdit&&!e.proposed&&!ruleProposing(ruleKey(e));
  if(!captured&&all.some(e=>ruleProposing(ruleKey(e))))redrawWhenSettled(target,refresh);
  const lines=(e:Entry)=>ruleView(e).lines;
  const leaves=(e:Entry)=>{const l=lines(e);return l.length?l.map(x=>'l:'+x.id):['r:'+e.id];};
  const totalChanges=()=>all.reduce((n,e)=>n+ruleChanges(e),0);
  if(!captured)o.setLeave?.(()=>{const n=totalChanges();return n>0&&`${plural(n,'change')} not yet proposed. Propose ${n===1?'it':'them'}, or leave again (they are kept here until you discard them).`;});
  const go=(view:string)=>{o.setLeave?.(null);o.navigate(view);};

  // ---- Header: Which rule applies?, Discard and Propose N changes ----
  // The button waits for the work (busy while the check runs) and reports what stops it: never a silent press.
  const proposeBtn=proposeButton(()=>proposeAll());
  const discardBtn=armed(button('Discard changes',()=>{for(const e of changedRules(all))discardDraft(e.id);applied.clear();redraw();},'secondary'),'Discard all?');
  const context=captured?capturedRulesContext(ctx,data):otherRulesContext(ctx,keptRules(ctx),o,refresh);
  const pendingExceptions=(ctx.catalogues.exceptions||[]).filter(r=>r.status==='proposed').length;
  // How many are shown (E13: one place, the footer, as on every list); the totals follow it.
  const status=el('span',{class:'footer-total'}),count=el('span',{class:'dt-count muted',role:'status'});
  const body=el('div',{class:'rt-body'}),bar=selectionBar({label:'Selected rules and lines'});
  const holder=el('div',{class:'rt'}),marks=ruleViewKey(),keyRoot=marks.watch(body);  // the key after the list it explains
  const wrapKeys=()=>ISSUE_COLS.map(c=>c.key);  // only Needs attention is a table
  const allWrapped=()=>wrapKeys().every(key=>s.wrap.includes(key));
  const setWrapped=(key:string,on:boolean)=>{s.wrap=s.wrap.filter(k=>k!==key);if(on)s.wrap.push(key);changed();};
  const setWrapAll=(on:boolean)=>{const keys=wrapKeys();s.wrap=s.wrap.filter(k=>!keys.includes(k));if(on)s.wrap.push(...keys);changed();};
  const simplify=captured?null:simplifyButton(ctx,refresh,o);
  // Add, Discard and Propose stay buttons; the rest is one ⋯ menu that says what waits (RUL-02). Older department rules
  // and their pending proposals keep their own panels there (legacy compatibility until a governed conversion); the
  // proposals are rows of Waiting for approval too, counted from the same list.
  const older=captured?[]:olderPending(ctx),otherPending=older.length;
  target.append(sectionHead('Rules','',
    moreActions('More rule actions',[...(!captured&&o.whichRule?[compact(button('Which rule applies?',o.whichRule,'secondary'),'directory')]:[]),
      // Print: the page as shown (its sentences under the questions when grouped so), through the app's print styles.
      compact(button('Print',()=>window.print(),'secondary'),'printer'),context,
      ...(!captured&&o.exceptions?[compact(button(pendingExceptions?`Exceptions (${pendingExceptions} pending)`:'Exceptions',o.exceptions,'secondary'),'shield')]:[]),
      simplify||''],(!captured&&o.exceptions?pendingExceptions:0)+(context?otherPending:0)),
    ...(canEdit&&v2?[addRule()]:[]),...(canEdit?[discardBtn,proposeBtn]:[])),
    ...(captured?[]:movedNotes(all,names,()=>void refresh())),holder,footerStatus(count,status));
  /** + Add rule: a type not in use opens on its own page, starting from its first line. Names only (a description cut to
   *  "How many people …" in the narrow picker said nothing); the description still finds it in the search. */
  function addRule(){const b:HTMLButtonElement=button('Add rule',()=>openPicker(b,b.parentElement!,{label:'Add rule',multiple:false,selected:new Set(),
    choices:all.filter(e=>!current(e)).map(e=>({value:e.id,label:e.name,group:COMMON.includes(e.id)?'Common first rules':families.get(e.family)||e.family,keywords:e.description}))
      .sort((a,b)=>Number(b.group==='Common first rules')-Number(a.group==='Common first rules')),onPick:id=>go('rules/'+id)}),'secondary');return b;}
  let saved:SavedView<Saved>[]=[];
  void ruleTreeViews().then(v=>{saved=v;const open=openingView('rule-tree',saved);
    if(open!==undefined&&(open?.id||'')!==viewId){viewId=open?.id||'';Object.assign(s,open?stateOf(open.definition):{...DEFAULT,filters:{},then_sort:[]});save();rebuild();return;}
    if(viewId&&!saved.some(x=>x.id===viewId)){viewId='';save();}toolbar.sync();}).catch(()=>undefined);
  // Built once every helper below it is defined (its warn reads attentionNow at once): see the end of this function.
  let toolbar:ReturnType<typeof build>;
  function redraw(){draw();paint();}
  /** The toolbar for the view shown (Rules or Needs attention have their own filters, groups and sorts). */
  function build(){
    const attention=s.mode!=='rules';
    const builtins=()=>[{label:'All rules',count:all.filter(current).length,on:!viewId&&!attention&&!s.filters.state?.length,apply:()=>open('rules',{})},
      {label:'Needs attention',count:attentionTotal(),on:!viewId&&s.mode==='attention',apply:()=>open('attention',{})},
      {label:'Acknowledged',count:acked.length,on:!viewId&&s.mode==='acknowledged',apply:()=>open('acknowledged',{})},
      {label:WAITING,count:waitingCount(all,older),on:!viewId&&!attention&&s.filters.state?.join()===WAITING,apply:()=>open('rules',{state:[WAITING]})}];
    const on=()=>builtins().find(b=>b.on);
    const facets=properties(attention);
    const t=collectionToolbar({label:'Rules',
      saved:{name:()=>saved.find(x=>x.id===viewId)?.name||(on()&&on()!.label!=='All rules'?on()!.label:''),defaultName:()=>on()?.label||'Custom view',
        // What needs attention stays on the button in any view, one click from its list (DESIGN: built-in views).
        // In lines, as the rule's own badge and the footer say (attentionWords): one unit for one list.
        // DL-037: rules the explainer could not word are said once here too, beside what needs attention.
        warn:()=>[!attention&&issues().length?attentionNow():'',unexplainedCount()?TERMS.rulesUnexplained(unexplainedCount()):''].filter(Boolean).join(' · '),dirty:()=>{const a=saved.find(x=>x.id===viewId);return !!a&&dirty(a.definition);},
        items:close=>savedViewItems<Saved>({key:'rule-tree',noun:'rules',saved,activeId:viewId,current:definition,dirty,
          builtins:builtins(),close,apply:v=>{viewId=v?.id||'';Object.assign(s,v?stateOf(v.definition):{...DEFAULT,filters:{},then_sort:[]});rebuild();},done:(next,id)=>{saved=next;viewId=id;save();t.sync();},reset:()=>{Object.assign(s,{...DEFAULT,filters:{},then_sort:[],mode:s.mode});rebuild();}})},
      // Drawn once typing pauses (one letter used to draw nearly every line); Enter searches at once.
      search:{value:s.q,label:attention?'Search issues':'Search rules',placeholder:'Rule, clinic, person or #tag',wait:200,onInput:q=>{s.q=q;},apply:()=>{draw();toolbar.sync();},
        // Own text: a rule's name and its lines' numbers, days and notes; related: the people, programmes, tags and
        // clinics its lines name (a picked one by id). Never who a rule binds: Which rule applies answers that.
        options:{value:()=>s.search_options,set:o=>{s.search_options=o;save();},can:LINE_SEARCH}},
      // The shared Filter (+ Add filter rule, And / Or, groups) over the Rules collection's properties.
      filter:conditionFilter({label:k=>facets[k]?.[0]||k,
        fields:()=>Object.entries(facets).map(([key,[label,values,group]])=>({key,label,group,kind:'multiple_choice' as const,choices:values.filter(Boolean).map(x=>({value:x,label:x}))})),
        get:()=>({filters:s.filters,conditions:s.conditions}),set:n=>{s.filters=n.filters;s.conditions=n.conditions;changed();},
        // The view's own filter (a saved view's, or Waiting for approval's): not counted, and Show all keeps it.
        baseline:()=>{const a=saved.find(x=>x.id===viewId);return a?{filters:a.definition.filters||{},conditions:a.definition.conditions||null}:{filters:on()?.label===WAITING?{state:[WAITING]}:{},conditions:null};}}),
      group:{options:attention?[{key:'kind',label:'Issue'}]:[...(byQuestion?[{key:'question',label:'Question'}]:[]),{key:'family',label:'Family'}],value:()=>attention?(s.group===''?'':'kind'):grouping(),set:k=>{s.group=k||'';changed();}},  // in use = a grouping applied (the shared default), so the phone's Group dot shows it
      sort:attention?undefined:{options:[{key:'name',label:'Name'},{key:'reach',label:'Reach'},{key:'lines',label:'Lines'},
        ...LINE_SORT_LABELS.map(([key,label])=>({key,label:`Lines by ${label.toLowerCase()}`}))],value:()=>s.sort,set:v=>{s.sort=v;s.then_sort=[];changed();},defaultLabel:'Catalogue order',active:()=>!!s.sort,
        then:{value:()=>s.then_sort,set:k=>{s.sort=k[0]||null;s.then_sort=k.slice(1);changed();}}},
      view:{active:()=>attention&&(wrapKeys().some(key=>s.wrap.includes(key))||Object.keys(s.widths).length>0),build:(panel,done)=>{
        const source=sourceControl?.();
        if(source)panel.append(el('div',{class:'dt-pop-label'},'Rules'),source);
        // Rules: one setting, every rule shown open or collapsed, kept as the view's open rules.
        if(!attention){const ids=shown().map(x=>x.e.id),openAll=ids.length>0&&ids.every(id=>s.open.includes(id));
          panel.append(el('div',{class:'dt-pop-label'},'Layout'),segmented<'open'|'collapsed'>('Layout',[['open','Open all rules'],['collapsed','Collapsed']],openAll?'open':'collapsed',v=>{setOpen(ids,v==='open');done();}));return;}
        panel.append(wrapAllColumns(allWrapped(),on=>{setWrapAll(on);done();}));
        if(Object.keys(s.widths).length)panel.append(button('Reset column widths',()=>{s.widths={};save();redraw();toolbar.sync();done();},'ghost small',{class:'dt-reset-widths'}));}}});
    holder.replaceChildren(t.root,body,keyRoot,bar.root);
    return t;
  }
  function definition():Saved{return definitionOf(s);}
  function dirty(d:Saved){return JSON.stringify(definitionOf(stateOf(d)))!==JSON.stringify(definition());}
  function open(mode:Mode,filters:Record<string,string[]>){viewId='';s.mode=mode;s.filters=filters;s.conditions=null;s.group=mode==='rules'?DEFAULT.group:'kind';selected.clear();picked.clear();rebuild();}
  function rebuild(){toolbar=build();redraw();}
  function changed(){save();draw();toolbar.sync();}

  // ---- Rules: families › rules › lines ----
  /** A line's own words (numbers, days, dates, note) and, apart, each record its who and where name (a person,
   *  programme, #tag, clinic, by name and by id; "except" ones too): never the joined sentence, never who it binds. */
  const ownText=(e:Entry,l:RuleLine)=>[numbersText(l,e.inherited?.[l.id],appliesWord(e)),unitText(e,l,settingsOf(e)),daysText(l),validText(l),l.note||''];
  const tagIds=new Map((ctx.catalogues.tags||[]).map(t=>[String(t.name).toLowerCase(),String(t.id)]));
  const operands=(t:Conditions|null|undefined):{field:string;value:string}[]=>(t?.conditions||[]).flatMap(c=>'conditions' in c?operands(c):(c.values||[]).map(v=>({field:c.field,value:String(v)})));
  // The rule's own tags (Configuration › Tags, kind policies) relate each of its lines, as the server's policy_lines search
  // does (search.projected_matches _tagged_policies): the version shown (e.rule), never a proposal's or an older one's.
  // The Rules route holds tag_members (app.ts awaits ensureCatalogues, complete or an error), so none is no membership.
  const ruleTags=(e:Entry):Row[]=>e.rule?tagsFor(ctx,'policies',String(e.rule.id)):[];
  const lineRecord=(e:Entry,l:RuleLine,tagged:()=>Row[]):SearchRecord=>{const named=()=>[...operands(l.who),...operands(l.where)];
    return {own:()=>ownText(e,l),id:l.id,related:()=>[...named().map(x=>names.tag(x.value)?.name??(x.field==='tags'?x.value.replace(/^#/,''):names.label(x.field,x.value))),...tagged().map(t=>t.name)],
      // A tag is named by its id or (older lines) its name: either finds a picked tag.
      refs:()=>[...named().flatMap(x=>x.field==='tags'?[x.value,tagIds.get(x.value.replace(/^#/,'').toLowerCase())||'']:[x.value]),...tagged().map(t=>t.id)]};};
  // Without options the older search: the line's words and who/where as one text, as before.
  const lower=(x:string)=>x.toLocaleLowerCase();
  const lineText=(e:Entry,l:RuleLine)=>lower([treeText(l.who,names,'Everyone'),treeText(l.where,names,'All clinics'),...ownText(e,l)].join(' '));
  /** One test of a rule (by its name) or its lines against the search and its options; null when nothing is searched. */
  const searching=()=>{const o=normalSearch(s.search_options);
    if(!o){const q=lower(s.q.trim());return q?{name:(text:string|undefined)=>!!text&&lower(text).includes(q),line:(e:Entry,l:RuleLine)=>lineText(e,l).includes(q)}:null;}
    // Each rule's tags are read once per search pass, not once per line.
    const m=recordMatcher(s.q,o),tags=new Map<string,Row[]>(),tagged=(e:Entry)=>()=>{let t=tags.get(e.id);if(!t)tags.set(e.id,t=ruleTags(e));return t;};
    return m?{name:(text:string|undefined)=>!!text&&m({own:()=>[text]}),line:(e:Entry,l:RuleLine)=>m(lineRecord(e,l,tagged(e)))}:null;};
  const endsIn=(l:RuleLine)=>l.ends_on?Math.round((Date.parse(l.ends_on+'T12:00:00Z')-Date.parse(data.day+'T12:00:00Z'))/86400000)-1:null;
  const issuesOf=(e:Entry)=>issues().filter(x=>x.catalogue_id===e.id);
  // What needs attention, counted in lines (RU-02): an overlap is one issue per pair of lines, so 150 person lines read
  // as "146"; the lines involved are what someone acts on. An issue with no line counts as one.
  const attentionWords=(list:Issue[])=>{if(!list.length)return '';const n=attentionCount(list);return `${plural(n,'line')} need${n===1?'s':''} attention`;};
  // The whole catalogue's count: the server's (unique rules and lines, attention_counts) until a fix is applied in this
  // draft; then the issues still open here.
  const attentionNow=()=>{const c=data.attention_counts;if(applied.size||!c)return attentionWords(issues());
    return c.lines?`${plural(c.lines,'line')} need${c.lines===1?'s':''} attention`:c.rules?`${plural(c.rules,'rule')} need${c.rules===1?'s':''} attention`:'';};
  const facetsOf=(e:Entry)=>({state:[isOn(e)||ruleView(e).on&&lines(e).length?'On':'Off',...(e.proposed?[WAITING]:[]),...(ruleChanges(e)?['Changed']:[])],family:[families.get(e.family)||e.family],availability:[availability(e)],
    issue:[...(e.overlaps?['Overlaps']:[]),...(lines(e).some(l=>{const d=endsIn(l);return d!==null&&d>=0&&d<=RULE_ENDING_DAYS;})?[`Ends within ${RULE_ENDING_DAYS} days`]:[]),...(issuesOf(e).length?['Needs attention']:[])]});
  type Shown={e:Entry;lines:{line:RuleLine;n:number}[];hit:boolean};
  const shownLeaves=(x:Shown)=>!editable(x.e)?[]:x.lines.length?x.lines.map(({line})=>'l:'+line.id):leaves(x.e);
  function shown():Shown[]{
    const q=searching(),out:Shown[]=[],t=tree(),keys=t?[...new Set(fieldsOf(t))].filter(k=>k in LINE_FACETS):[];
    // Retired data (empty, off, ended) shows once the Filter asks for it.
    const widen=mentions(t,'availability')||mentions(t,'state',['Off'])||mentions(t,'valid',['Ended']),endedToo=mentions(t,'valid',['Ended'])||mentions(t,'availability',['Ended only']);
    // Each sort key in turn: the rule keys order the rules, the line keys the lines within their rule.
    const sorts=s.sort?[s.sort,...s.then_sort]:[],lineSorts=sorts.filter(([k])=>k in LINE_SORT),ruleSorts=sorts.filter(([k])=>['name','reach','lines'].includes(k));
    for(const e of all){
      if(!current(e)&&!widen)continue;
      const f=facetsOf(e) as Record<string,string[]>,have=(l?:RuleLine)=>(k:string)=>k in LINE_FACETS?l?[LINE_FACETS[k][1](l)].flat():[]:f[k]||[];
      // Filtered by a line's values: the rule shows (open) with its matching lines.
      let ls=lines(e).map((line,n)=>({line,n})).filter(x=>!ended(x.line)||endedToo);
      if(t&&keys.length){ls=ls.filter(x=>holds(t,have(x.line),choice));if(!ls.length&&(lines(e).length||!holds(t,have(),choice)))continue;}
      else if(t&&!holds(t,have(),choice))continue;
      // Unsorted, a rule's lines read most specific first (as on its page): the line that wins comes first.
      if(!lineSorts.length)ls.sort((a,b)=>specificFirst(a.line)-specificFirst(b.line));
      else ls.sort((a,b)=>{for(const [k,dir] of lineSorts){const n=LINE_SORT[k](e,a.line).localeCompare(LINE_SORT[k](e,b.line),undefined,{numeric:true})*(dir==='desc'?-1:1);if(n)return n;}return 0;});
      if(!q){out.push({e,lines:ls,hit:keys.length>0});continue;}
      // A rule's name or, grouped by question, the server's sentence for it (LEAD-UIUX: search matches both).
      const byName=q.name(e.name)||q.name(questionOf(e)?.summary_sentence),hits=ls.filter(x=>q.line(e,x.line));
      if(byName||hits.length)out.push({e,lines:byName?ls:hits,hit:!byName});
    }
    if(ruleSorts.length)out.sort((a,b)=>{for(const [by,dir] of ruleSorts){
      const n=(by==='name'?a.e.name.localeCompare(b.e.name):by==='reach'?a.e.people-b.e.people:lines(a.e).length-lines(b.e).length)*(dir==='desc'?-1:1);if(n)return n;}return 0;});
    return out;
  }
  const isOpen=(x:Shown)=>s.open.includes(x.e.id)||x.hit;
  const wrapped=(key:string)=>s.wrap.includes(key)?' dt-wrap':'';
  /** What select-all and a group's box pick: the lines shown (an empty rule is picked by its own box, never by
   *  select-all, so "Rule on" cannot switch on rules with no lines). */
  const lineLeaves=(rows:Shown[])=>rows.flatMap(shownLeaves).filter(k=>k.startsWith('l:'));
  /** The list's head: select every line shown, and open or close every rule (no column headings). */
  function header(rows:Shown[]){
    const keys=lineLeaves(rows),allOpen=rows.length>0&&rows.every(isOpen);
    const toggleAll=el('button',{type:'button',class:'icon-button bare rt-toggle','data-key':'toggle-all','aria-label':'All rules','aria-expanded':String(allOpen),'data-tip':allOpen?'Collapse all':'Expand all'},icon(allOpen?'chevronDown':'chevronRight'));
    toggleAll.addEventListener('click',()=>setOpen(rows.map(x=>x.e.id),!allOpen));
    return el('div',{class:'rt-lhead'},canEdit?box('Select all lines shown',keys,!keys.length):el('span',{}),toggleAll,el('span',{},'Rule'));
  }
  /** A column's width (Needs attention), kept with the view; undefined: back to its content's. */
  function setWidth(key:string,w:number|undefined){if(w)s.widths={...s.widths,[key]:w};else{const {[key]:_,...rest}=s.widths;s.widths=rest;}save();toolbar.sync();}
  function grip(key:string,label:string){return columnGrip({label:`${label} column`,width:()=>s.widths[key],keep:w=>setWidth(key,w),size:(w,g)=>sizeColumn(g.closest('table')!,key,w)});}
  /** A tri-state box over some leaves (lines, or a rule without lines). */
  // Every box drawn, with the leaves it stands for: a tick updates them in place (a redraw of the whole table per tick
  // cost a second at department scale).
  let boxes:[HTMLInputElement,string[]][]=[],picks:[HTMLElement,Entry][]=[];
  function box(label:string,keys:string[],disabled=false){
    const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':label,disabled}) as HTMLInputElement;
    setTriState(b,triStateOf(keys.filter(k=>selected.has(k)).length,keys.length));
    b.addEventListener('change',()=>{for(const k of keys)b.checked?selected.add(k):selected.delete(k);syncSelection();});
    boxes.push([b,keys]);
    return b;
  }
  /** The selection changed: each box's tri-state, each rule's "N of M selected" and the bar, without a redraw. */
  function syncSelection(){
    boxes=boxes.filter(([b])=>b.isConnected);picks=picks.filter(([n])=>n.isConnected);
    for(const [b,keys] of boxes)setTriState(b,triStateOf(keys.filter(k=>selected.has(k)).length,keys.length));
    for(const [n,e] of picks){const all=lines(e).length,mine=leaves(e).filter(k=>selected.has(k)).length;n.textContent=mine&&all?`${mine} of ${all} selected`:'';}
    selectionShown();
  }
  function setOpen(ids:string[],on:boolean){s.open=on?[...new Set([...s.open,...ids])]:s.open.filter(id=>!ids.includes(id));
    if(ids.length===1){save();patchRule(ids[0]);toolbar.sync();}else changed();}
  /** One rule drawn again in place (opened, closed, switched): the rest of the list is left as it is. */
  function patchRule(id:string){
    const old=body.querySelector<HTMLElement>(`li.rt-item[data-rule="${CSS.escape(id)}"]`);
    const rows=shown(),x=rows.find(r=>r.e.id===id);
    if(!old||!x||s.mode!=='rules'){draw();return;}
    const focused=document.activeElement as HTMLElement|null,key=focused&&old.contains(focused)?focused.dataset.key||focused.getAttribute('aria-label'):null;
    const next=ruleItem(x,rows);old.replaceWith(next);
    body.querySelector('.rt-lhead')?.replaceWith(header(rows));
    syncSelection();
    if(key)next.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"],[aria-label="${CSS.escape(key)}"]`)?.focus({preventScroll:true});
  }
  /** Two rules that can't both hold: the server's sentence, its two line numbers, and Open both (their lines open). */
  function conflictItem(c:NonNullable<DepartmentRules['conflicts']>[number],rows:Shown[]):HTMLElement{
    const ids=rows.filter(r=>r.e.rule?.id&&c.rule_ids.includes(String(r.e.rule.id))).map(r=>r.e.id);
    const refs=c.line_refs;
    return el('li',{class:'rt-conflict','data-conflict':c.conflict_id},
      el('span',{class:'rt-conflict-in'},issueGlyph('error'),el('span',{},c.sentence,refs.length===2?` Lines #${refs[0]} and #${refs[1]}.`:''),
        ids.length?button('Open both',()=>setOpen(ids,true),'secondary small'):''));
  }
  /** One rule: its catalogue name with what changes what happens (safety, strength, Off, waiting, changes,
   *  attention), over one count line; its lines only when it is opened. Never a sentence about one of its lines. */
  function ruleItem(x:Shown,rows:Shown[]):HTMLElement{
    const e=x.e,d=ruleView(e),ls=d.lines,openNow=isOpen(x),region=`rt-lines-${e.id}`;
    const mine=leaves(e).filter(k=>selected.has(k)).length;
    const toggle=el('button',{type:'button',class:'icon-button bare rt-toggle','data-key':'toggle:'+e.id,'aria-expanded':String(openNow),'aria-controls':region,'aria-label':`${e.name} lines`,'data-tip':'Alt+click: all rules'},icon(openNow?'chevronDown':'chevronRight'));
    toggle.addEventListener('click',ev=>setOpen(ev.altKey?rows.map(r=>r.e.id):[e.id],!openNow));
    const on=d.on&&(!!e.rule||ls.length>0);
    // A reader who cannot change rules reads the fact (On / Off), not a switch they cannot use.
    // A switch that cannot turn says why on hover (a rule waiting for approval, or one with nothing to switch on yet).
    const why=!editable(e)?WAITING:on?'':cannotStart(e);
    const sw=!canEdit?(()=>{const b=badge(on?'On':'Off',on?'ok':'neutral');b.classList.add('rt-state');return b;})():el('input',{type:'checkbox',role:'switch',class:'switch','aria-label':`${e.name} on or off`,checked:on,disabled:!!why,'data-tip':why||'No reason'}) as HTMLInputElement;
    if(!on)ruleMark(sw,'off');
    if(sw instanceof HTMLInputElement)sw.addEventListener('change',()=>{if(!turn(e,sw.checked))sw.checked=!sw.checked;});
    const picked=el('span',{class:'rt-picked'},mine&&ls.length?`${mine} of ${ls.length} selected`:'');picks.push([picked,e]);
    const changes=ruleChanges(e),att=attentionWords(issuesOf(e)),shownLines=x.lines.map(i=>i.line);
    const title=el('div',{class:'rt-title'},
      // The safety lock in every grouping: drawn here, once per rule row, never per grouping path.
      safe(e)?ruleMark(el('span',{class:'rk-lockmark',role:'img','aria-label':'Safety rule: can only be made stricter','data-tip':'Safety rule: can only be made stricter'},icon('lock')),'safety'):'',
      captured?el('strong',{},e.name):button(e.name,()=>go('rules/'+e.id),'',{class:'link-button rt-rule-link'}),
      // Its strength once (one badge when every number agrees, else Mandatory and Advisory): the shared level badges.
      marks.numbers(el('span',{class:'rt-levels'},...ruleLevels(e,shownLines).map(v=>levelBadge(v))),false),
      // a rule the explainer can't word says so beside its name, in every grouping; by question in full (its check still applies).
      summaries.get(e.id)?.unexplained?unexplainedLine(questionOf(e)?TERMS.ruleUnexplained:TERMS.ruleUnexplainedShort):'',
      // Off in words too (the switch's colour is never the only signal); a reader's On / Off badge already says it.
      canEdit&&!on?badge('Off','neutral','rt-off'):'',
      // Waiting on someone else is information, not a call to act: info with a clock.
      e.proposed?badge(WAITING,'info','rt-waiting-chip',{icon:'clock','data-rule-mark':'waiting'}):'',changes?badge(plural(changes,'change'),'info'):'',
      att?badge(`⚠ ${att}`,statusTone('attention'),'rt-att',{as:'button',onclick:()=>{open('attention',{rule:[e.name]});},'data-rule-mark':'attention'}):'',picked);
    const meta=el('div',{class:'rt-meta'},ruleCounts(shownLines,names,data.day,on,effective));
    const item=el('li',{class:'rt-item'+(d.on?'':' is-off'),'data-rule':e.id},
      el('div',{class:'rt-row'},canEdit?box(`Select ${e.name}`,shownLeaves(x),!editable(e)):el('span',{}),toggle,sw,el('div',{class:'rt-main'},title,meta)));
    if(openNow)item.append(linesRegion(x,region));
    return marks.stamp(item);
  }
  /** An opened rule's lines: grouped by where (lines with a different where all apply), all clinics first; in each
   *  group the most specific who first (`shown` orders them, or a chosen line sort). Each line: who › when › numbers. */
  function linesRegion(x:Shown,id:string):HTMLElement{
    const e=x.e,ls=ruleView(e).lines,live=new Map(linesOf(e).map(l=>[l.id,JSON.stringify(l)]));
    // Inherited labels read this render's snapshot, not a fresh clone of the whole rule for every number.
    const from=sourceWords(()=>ls as RuleLine[],names);  // read only: the label reader never edits
    const one=ruleLevels(e,x.lines.map(i=>i.line)).length<=1,sorted=[s.sort,...s.then_sort].some(k=>!!k&&k[0] in LINE_SORT);
    const region=el('div',{class:'rt-lines',id,role:'region','aria-label':`${e.name} lines`});
    const set=(l:RuleLine)=>!!l.where?.conditions?.length,groups=new Map<string,Shown['lines']>();
    for(const it of x.lines){const k=set(it.line)?JSON.stringify(it.line.where):'';groups.set(k,[...(groups.get(k)||[]),it]);}
    [...groups.values()].sort((a,b)=>Number(set(a[0].line))-Number(set(b[0].line))).forEach((g,i)=>{
      const where=g[0].line.where;
      region.append(el('div',{class:'rt-wh'},el('span',{class:'rt-wh-label'},'Where'),pills(ctx,where,names,'All clinics',openTag,'Where',3,marks),el('span',{class:'dt-group-count'},String(g.length)),
        // How lines combine is a rule of reading: contextual help on the first where, never a line of text.
        i===0?helpButton(`How ${e.name}'s lines combine`,whoWins(e),'rt-help'):''),
        el('ol',{class:'rt-lns','aria-label':`${plural(g.length,'line')} at ${treeText(where,names,'all clinics')}`},...shaped(g)));
      // an anchor line with its exceptions nested under it (exceptionTree, as on the rule's page); a chosen line sort
      // keeps its own flat order.
      function shaped(g:Shown['lines']):HTMLElement[]{
        const shape=sorted?g.map(item=>({item,under:false,always:false})):exceptionTree(e,g,it=>it.line),subs=shape.filter(x=>x.under),always=shape.filter(x=>x.always);
        const item=(x:typeof shape[number])=>lineItem(e,x.item.line,x.item.n,live,from,one,live.get(x.item.line.id)===JSON.stringify(x.item.line)?replacedWords(e,x.item.line,linesOf(e),e.line_replaces,settingsOf(e)):'');
        // C1: a mixed rule's safety lines after the anchor and its exceptions, under their own word, so they never read as
        // more exceptions (every matching one applies).
        return [...shape.filter(x=>!x.under&&!x.always).map(item),...(subs.length?[el('li',{class:'rt-sub'},el('ol',{class:'rt-lns rt-exc','aria-label':plural(subs.length,'exception')},...subs.map(item)))]:[]),
          ...(always.length?[el('li',{class:'rt-sub'},el('span',{class:'rt-always-word','aria-hidden':'true'},'Always applies'),el('ol',{class:'rt-lns rt-always','aria-label':`Always applies: ${plural(always.length,'line')}`},...always.map(item)))]:[])];
      }
    });
    if(editable(e)&&!s.q){const add:HTMLButtonElement=button(`+ New line in ${e.name}`,()=>{const d=draftOf(e);newLinePicker(ctx,e,add,d,()=>{keepDraft(e,d);if(!d.on&&!e.rule)d.on=true;s.open=[...new Set([...s.open,e.id])];redraw();},lid=>o.navigate(`rules/${e.id}/${lid}`));},'',{class:'link-button rt-add'});
      region.append(el('div',{class:'rt-new'},add));}
    else if(canEdit&&e.proposed&&!s.q)region.append(el('div',{class:'rt-new'},disabledWhy(button(`+ New line in ${e.name}`,()=>undefined,'',{class:'link-button rt-add'}),`${e.name} already has a change waiting for approval`,
      [' · ',button('Open it',()=>proposalPanel(ctx,e,names,()=>refresh()),'',{class:'link-button',...popup('dialog')})])));
    return region;
  }
  /** `one`: the rule says its one strength on its row, so the line does not repeat it. `instead`: what the server says this line replaces (replacedWords), in words; the anchor's numbers
   *  this exception replaces, only when certain. */
  function lineItem(e:Entry,l:RuleLine,n:number,live:Map<string,string>,from:(id:string)=>string,one:boolean,instead=''){
    // Overlaps only where two lines both apply (equal tiers, no winner); a winning exception and its anchor are not a warning.
    const ov=e.line_overlaps?.[l.id]||[],days=endsIn(l),state=!live.has(l.id)?'New':live.get(l.id)!==JSON.stringify(l)?'Changed':'';
    // An Applies number reads as its mode alone ("next day combination", "no duties"), never "applies · …".
    const mode=l.numbers.apply&&e.modes&&!e.measures?unitText(e,l,settingsOf(e)):'';
    const noDuties='apply' in (l.numbers||{})&&appliesWord(e)==='no duties';
    const num=numbersNode(l,e.inherited?.[l.id],from,mode||(noDuties?'No duties':appliesWord(e)));
    // 11: the number and its measure are one unbroken phrase; a strength follows the measure words, and only when the
    // rule's lines differ in strength (an inherited strength says nothing).
    const levels=[...num.querySelectorAll<HTMLElement>('.rc-level')];for(const b of levels){if(b.previousSibling?.textContent===' ')b.previousSibling.remove();b.remove();}
    // Measure and period read in the unit words ("duties per calendar week"), not columns of their own.
    // shortcut: the unit shown is the in-force rule's, so a draft that changes the unit shows the old unit here (its page shows the new); upgrade when the catalogue sends each line's effective unit (a server field).
    const unit=e.measures&&l.numbers.apply||mode?'':unitText(e,l,settingsOf(e))||unitWords(e,l);if(unit)num.append(el('small',{class:'muted'},' '+unit));
    // 12 + C1: a safety line (no duties, …) carries its lock.
    const lock=noDuties||e.mixed_safety&&setsSafety(e,l)?[safetyLock(),' ']:[];
    marks.numbers(num);
    const after=[...(one?[]:levels.filter(b=>b.dataset.level!=='inherit')),...(instead?[el('small',{class:'muted rt-instead'},`(instead of ${instead})`)]:[])];
    const tail=after.length?marks.numbers(el('span',{class:'rt-ln-after'},...after.flatMap(x=>[' ',x])),false):'';
    // When: its days (only when not every day) and valid dates (only when set); blank otherwise.
    // every condition the resolver applies (a restriction's days, hours, window around leave) reads here.
    const dayWords=whenWords(e,l,settingsOf(e)),when:Child[]=[dayWords!==everyDay?dayWords:'',l.starts_on||l.ends_on?validNode(l):''].filter(Boolean);
    // Reach only when it is not a plain count: No one, Starts later, Ended.
    const reached=e.reach[l.id];
    return el('li',{class:'rt-ln'+(state?' rc-changed':''),'data-line':l.id},
      canEdit?box(`Select line ${n+1} of ${e.name}`,['l:'+l.id],!editable(e)):el('span',{}),
      el('span',{class:'rt-n','aria-label':`Line ${n+1}`},String(n+1)),
      el('span',{class:'rt-ln-who'},pills(ctx,l.who,names,'Everyone',openTag,'Who',2,marks)),
      el('span',{class:'rt-ln-what'},el('span',{class:'rt-ln-when'},...when.flatMap((w,i)=>i?[' · ',w]:[w])),el('span',{class:'rt-ln-num'},...lock,num,tail),
        el('span',{class:'rt-ln-flags'},state?badge(state,'info'):'',ov.some(o=>o.winner==null)?badge('⚠ Overlaps','warn'):'',days!==null&&days>=0&&days<=RULE_ENDING_DAYS?badge(`⚠ Ends in ${plural(days,'day')}`,'warn'):'',
          reached===0?reachNode(reached,l,data.day):'')),
      el('span',{class:'rt-ln-open'},captured?'':button('↗',()=>go(`rules/${e.id}/${l.id}`),'',{class:'icon-button bare','aria-label':`Open line ${n+1} of ${e.name}`,'data-tip':'Open in the rule'})));
  }
  /** On or off from the switch or the selection bar: a draft change, proposed with the rest. A rule not in force gets
   *  its first line; one whose settings only the department can choose opens its page. */
  function turn(e:Entry,on:boolean):boolean{
    if(!editable(e)){if(e.proposed)notify(`A change to ${e.name} is already waiting for approval; approve or decline it first.`,'warning');return false;}
    const d=draftOf(e),lineCount=d.lines.length;
    if(on&&!e.rule){
      // Its switch is disabled with the same words; here only from the selection bar's Rule on.
      const why=cannotStart(e);if(why){notify(`${e.name}: ${why.toLowerCase()}.`,'info');return false;}
      if(!d.lines.length)d.lines=[newLine(e)];
    }
    if(!on&&!e.rule){discardDraft(e.id);redraw();return true;}
    d.on=on;keepDraft(e,d);
    // Only this rule's row changes, unless a search or filter may now hide or show it, a sort may move it, or its lines
    // changed (its first line: the family's box must pick it too).
    if(!s.q.trim()&&!tree()&&!s.sort&&d.lines.length===lineCount){patchRule(e.id);paint();}else redraw();return true;
  }
  function drawRules(){
    boxes=[];picks=[];
    const rows=shown();
    // By question: the server's groups A–F in its order, with its letters and labels as sent; a rule it does not explain
    // (not in force) after them. By family: the catalogue's families.
    const asked=grouping()==='question'?[...byQuestion?.groups??[]].sort((a,b)=>a.order-b.order):[];
    const groups:{id:string;name:string;letter?:string;rows:Shown[]}[]=grouping()==='question'
      ?[...asked.map(g=>({id:g.key,name:g.label,letter:g.letter,rows:rows.filter(x=>summaries.get(x.e.id)?.question_group===g.key)})),
        {id:'not-in-force',name:'Not in force',rows:rows.filter(x=>!asked.some(g=>g.key===summaries.get(x.e.id)?.question_group))}].filter(g=>g.rows.length)
      :grouping()==='family'?data.families.map(f=>({id:f.id,name:f.name,rows:rows.filter(x=>x.e.family===f.id)})).filter(g=>g.rows.length):[{id:'',name:'',rows}];
    // A conflict shows once, under the first of its two rules drawn (CONTRACT §6): the server's sentence and line numbers.
    const told=new Set<string>();
    const sections=groups.map(g=>{
      const list=el('ul',{class:'rt-list'});
      for(const x of g.rows){list.append(ruleItem(x,rows));
        for(const c of byQuestion?.conflicts||[])if(!told.has(c.conflict_id)&&x.e.rule?.id&&c.rule_ids.includes(String(x.e.rule.id))){told.add(c.conflict_id);list.append(conflictItem(c,rows));}}
      if(!g.id)return el('section',{class:'rt-q'},list);
      // The group's tri-state box says how much of it is selected.
      const keys=lineLeaves(g.rows),head=`rt-q-${g.id}`;
      return el('section',{class:'rt-q','aria-labelledby':head},el('div',{class:'rt-qhead'},canEdit?box(`Select ${g.name}`,keys,!keys.length):el('span',{}),
        el('h3',{id:head},...(g.letter?[el('span',{class:'rt-letter'},g.letter),' ']:[]),g.name),el('span',{class:'dt-group-count'},String(g.rows.length))),list);
    });
    // Older-kind proposals after the catalogue's groups, in a section of their own with no heading: each row names its kind.
    const olders=olderShown(older,tree(),searching()?.name);
    if(olders.length)sections.push(el('section',{class:'rt-q'},el('ul',{class:'rt-list','aria-label':plural(olders.length,'older rule')+' waiting for approval'},...olders.map(olderItem))));
    const n=rows.reduce((a,x)=>a+x.lines.length,0);
    const listed=all.filter(current).length+olderShown(older,tree()).length,shownCount=rows.length+olders.length;
    count.textContent=(shownCount===listed?plural(listed,'rule'):`${shownCount} of ${listed} rules`)+(s.q?` · ${plural(n,'line')}`:'')
      +(sourceLabel?` · ${sourceLabel}`:'');toolbar.shown(shownCount,listed,['rule','rules']);
    const filtered=!!s.q.trim()||!!normalSearch(s.search_options)?.reference||Object.values(s.filters).some(v=>v.length)||!!s.conditions;
    // One list, no table and no columns: nothing is empty and nothing scrolls sideways.
    body.replaceChildren(shownCount?el('div',{class:'table-scroll rt-scroll',tabindex:0,'aria-label':'Rules and their lines'},header(rows),...sections)
      :v2&&!filtered?empty('No rules in use',canEdit?'Add rule starts one, from its first line. Filter shows empty types and ended lines.':'No rule applies yet. Filter shows empty types and ended lines.',null,'rules')
      :empty(all.length?'No matching rules':'No rules',all.length?searchWords(s.search_options)||'Clear the search or a filter.':'',null,'search'));
    selectionShown();
  }
  /** An older-kind proposal: its name opens the decision panel Other pending rules' Review opens (check, Activate,
   *  Decline), with the same permissions; one who cannot check reads its rule sheet, as that panel's name link does. */
  function olderItem(p:Row):HTMLElement{
    const run=()=>mayReviewOlder(ctx)?void reviewOlder(ctx,p,refresh).catch(err=>notify((err as Error).message,'error',undefined,err)):o.openRule?.(p);
    const title=el('div',{class:'rt-title'},button(labelOf(p),run,'',{class:'link-button rt-rule-link',...(mayReviewOlder(ctx)?popup('dialog'):{})}),
      badge('Older rule','neutral','rt-older'),badge(WAITING,'info','rt-waiting-chip',{icon:'clock','data-rule-mark':'waiting'}));
    // The row's columns (box, toggle, switch) stay empty: it has no lines and changes only through its decision.
    return marks.stamp(el('li',{class:'rt-item','data-older-rule':p.id},el('div',{class:'rt-row'},el('span',{}),el('span',{}),el('span',{}),
      el('div',{class:'rt-main'},title,el('div',{class:'rt-meta'},olderEffect(ctx,p))))));
  }
  /** The rules and lines picked, per rule (only rules that can change). */
  function chosen():Chosen{
    return all.filter(e=>editable(e)).flatMap(e=>{const ids=new Set(lines(e).filter(l=>selected.has('l:'+l.id)).map(l=>l.id));return ids.size?[{e,draft:draftOf(e),ids}]:[];});
  }
  const pickedRules=()=>all.filter(e=>editable(e)&&leaves(e).some(k=>selected.has(k)));
  function selectionShown(){
    if(s.mode!=='rules')return;
    const rules=pickedRules(),lineCount=chosen().reduce((a,g)=>a+g.ids.size,0),count=selected.size;
    const after=()=>{for(const g of chosen())keepDraft(g.e,g.draft);redraw();};
    const actions:SelectionAction[]=[
      {label:'Rule on',icon:'check',tip:'Turn the selected rules on',run:()=>{for(const e of rules)turn(e,true);}},
      {label:'Rule off',icon:'close',tip:'Turn the selected rules off (their lines are kept)',run:()=>{for(const e of rules)turn(e,false);}},
      ...(lineCount?[{label:'Numbers…',icon:'edit' as const,primary:true,run:()=>numbersPopover(barButton('Numbers…'),chosen(),after)},
        {label:'Valid dates…',icon:'calendar' as const,run:()=>datesDialog(chosen(),after)},
        {label:'Duplicate…',icon:'copy' as const,more:true,run:()=>datesDialog(chosen(),after,true)},
        {label:'Remove lines',icon:'trash' as const,danger:true,armed:true,run:()=>{for(const g of chosen()){g.draft.lines=g.draft.lines.filter(l=>!g.ids.has(l.id));keepDraft(g.e,g.draft);}selected.clear();redraw();}}]:[])];
    bar.show(count?{count,noun:['item','items'],detail:[plural(rules.length,'rule'),lineCount?plural(lineCount,'line'):''].filter(Boolean).join(' · '),actions,clear:()=>{selected.clear();syncSelection();}}:null);
  }

  // ---- Needs attention ----
  function drawAttention(){
    const shown=s.mode==='acknowledged'?acked:issues();
    const q=searching(),list=shown.filter(x=>{const e=byId.get(x.catalogue_id);
      const t=tree(),f:Record<string,string[]>={kind:[KINDS[x.kind]?.label||x.kind],rule:[e?.name||'']};
      const hit=!q||(normalSearch(s.search_options)?[e?.name,KINDS[x.kind]?.label,what(x)].some(q.name):q.name([e?.name,KINDS[x.kind]?.label,what(x)].join(' ')))||!!e&&linesOf(e).some(l=>x.line_ids.includes(l.id)&&q.line(e,l));
      return hit&&(!t||holds(t,k=>f[k]||[],choice));});
    const tbody=el('tbody',{});
    const kinds=s.group===''?[{kind:'',items:list}]:[...Object.keys(KINDS),...new Set(list.map(x=>x.kind).filter(k=>!KINDS[k]))].map(kind=>({kind,items:list.filter(x=>x.kind===kind)})).filter(g=>g.items.length);
    const pick=(label:string,ids:string[])=>{const b=el('input',{type:'checkbox',class:'bulk-check','aria-label':label}) as HTMLInputElement;
      setTriState(b,triStateOf(ids.filter(i=>picked.has(i)).length,ids.length));b.addEventListener('change',()=>{for(const i of ids)b.checked?picked.add(i):picked.delete(i);draw();});return b;};
    for(const g of kinds){
      if(g.kind)tbody.append(el('tr',{class:'dt-group rt-kind'},el('td',{class:'dt-check'},canEdit?pick(`Select every ${kindOf(g.kind).label} issue`,g.items.map(x=>x.id)):''),
        el('td',{colspan:'4'},badge(`${kindOf(g.kind).label} ${g.items.length}`,kindOf(g.kind).tone,'',{tip:cap(kindOf(g.kind).about)}))));  // each row says what happens
      for(const x of g.items)tbody.append(issueRow(x,pick));
    }
    count.textContent=plural(list.length,'issue')+(sourceLabel?` · ${sourceLabel}`:'');toolbar.shown(list.length,shown.length,['issue','issues']);
    body.replaceChildren(list.length?el('div',{class:'table-scroll rt-scroll',tabindex:0,'aria-label':'Issues'},el('table',{class:'dt-table rt-table rt-issues'},
      el('thead',{},el('tr',{},el('th',{class:'dt-check'},canEdit?pick('Select every issue shown',list.map(x=>x.id)):''),...ISSUE_COLS.map(c=>{
        const button=columnHeader({label:c.label,wrap:{on:s.wrap.includes(c.key),set:on=>setWrapped(c.key,on)},wrapAll:{on:allWrapped(),set:setWrapAll},
          get resetWidth(){return s.widths[c.key]?()=>{setWidth(c.key,undefined);draw();}:undefined;},
          keep:`.rt-body thead [data-key="column:${CSS.escape(c.key)}"]`});
        button.dataset.key='column:'+c.key;
        const h=el('th',{scope:'col','aria-label':c.label,'data-col':c.key,class:wrapped(c.key).trim()},button,grip(c.key,c.label));sized(h,s.widths[c.key]);return h;
      }))),tbody))
      :shown.length?empty('No matching issues','Clear the search or a filter.',null,'check')
      :s.mode==='acknowledged'?empty('Nothing acknowledged','Issues someone acknowledged show here for everyone, with who and when, until their lines change.',null,'check')
      :empty('Nothing needs attention',applied.size?'The remaining fixes are staged locally. Propose them for approval; the rules in force have not changed.':'No outstanding issues. Acknowledged issues remain in the Acknowledged view.',null,'check'));
    // Every issue picked, also one a search or filter hides now: the bar's count and its actions agree.
    const chosenIssues=()=>shown.filter(x=>picked.has(x.id));
    const detail=[...new Set(chosenIssues().map(x=>KINDS[x.kind]?.label))].join(' · ');
    if(s.mode==='acknowledged'){bar.show(picked.size&&canEdit?{count:picked.size,noun:['issue','issues'],detail,clear:()=>{picked.clear();draw();},actions:[
      {label:'Take back',icon:'reset',run:()=>void ack(chosenIssues(),false)}]}:null);return;}
    bar.show(picked.size&&canEdit?{count:picked.size,noun:['issue','issues'],detail,clear:()=>{picked.clear();draw();},actions:[
      {label:'Apply recommended',icon:'check',primary:true,run:()=>applyRecommended(chosenIssues())},
      {label:'Acknowledge',icon:'shield',run:()=>acknowledge(chosenIssues())}]}:null);
  }
  /** Draft fixes and shared acknowledgements have different effects; confirm their actual union before changing either. */
  function applyRecommended(chosen:Issue[]){
    const ready=chosen.filter(x=>{const e=byId.get(x.catalogue_id);return e&&editable(e)&&x.fixes[0].kind!=='resolve';});
    const keep=ready.filter(x=>!x.fixes[0].ops.length),edits=ready.filter(x=>x.fixes[0].ops.length),skipped=chosen.length-ready.length;
    const left=skipped?`${plural(skipped,'issue')} must be opened individually (a draft rota or a rule awaiting approval).`:'';
    if(!ready.length){notify(left||'No recommended fixes selected.','info');return;}
    const run=async(note='')=>{
      let staged=0;for(const x of edits)if(fix(x,x.fixes[0],false))staged++;
      picked.clear();redraw();
      if(keep.length)await ack(keep,true,note);
      const unchanged=edits.length-staged;
      if(staged||unchanged||left)notify([staged?`${plural(staged,'fix')} staged locally; propose for approval.`:'',unchanged?`${plural(unchanged,'fix')} could not be staged; review the unsent edits.`:'',left].filter(Boolean).join(' '),unchanged?'warning':'info');
    };
    if(keep.length)confirmAction('Apply recommended fixes',[
      `${plural(keep.length,'issue')} will be acknowledged for everyone now, with your name and optional note.`,
      edits.length?`${plural(edits.length,'fix')} will be staged locally and must be proposed for independent approval.`:'No rule change will be proposed.',left].filter(Boolean).join(' '),
      reason=>run(reason===routineReason('')?'':reason),'Apply',undefined,true);
    else void run();
  }
  /** "Line 1 gives way to line 3 for 12 people: they lose its min 2 → target 4" — an issue in words (one place). */
  function what(x:Issue):string{
    const L=(id:string)=>`line ${(byId.get(x.catalogue_id)?linesOf(byId.get(x.catalogue_id)!):[]).findIndex(l=>l.id===id)+1}`,f=x.facts,p=plural(x.people||0,'person','people');
    if(x.kind==='inherits')return `${cap(L(f.line))} inherits ${Object.entries(f.inherited as Record<string,{value:number;from:string}>).map(([k,v])=>`${k} ${numberText(v)} from ${L(v.from)}`).join(' and ')}, which contradicts its own numbers`;
    if(x.kind==='both')return `${cap(L(x.line_ids[0]))} and ${L(x.line_ids[1])} both apply to ${p}: the stricter number binds`;
    if(x.kind==='duplicate')return `${cap(L(x.line_ids[0]))} and ${L(x.line_ids[1])} are the same line (who, where, days and what they count)`;
    if(x.kind==='ending')return `Ends ${formatDate(f.last_day)} (${plural(f.days,'day')})`;
    if(x.kind==='counting_ends')return countingEndText(f as unknown as {counting_end:string;validity_end:string|null});
    if(x.kind==='nobody')return 'No active person matches this scope today';
    // Every number it sets is replaced, for all its people today, by more specific lines: it holds for no one.
    if(x.kind==='shadowed'){const w=(f.winners as string[]|undefined)||[],ks=(f.numbers as string[]|undefined)||[];
      return `${cap(L(x.line_ids[0]))} applies to no one today: ${w.length?w.map(L).join(' and '):'more specific lines'} replace${w.length===1?'s':''} every number it sets${ks.length?` (${ks.join(' and ')})`:''}, for all ${p}`;}
    const drafts=Array.isArray(f.drafts)?f.drafts as {workspace:string;count:number;message:string}[]:[];
    if(x.kind==='blocks'&&drafts.length)return `${plural(f.count,'issue')} in ${drafts.map(d=>d.workspace).join(', ')}: ${drafts[0].message}`;
    // A kind without its own words here says what its kind means, never nothing and never an error.
    return cap(kindOf(x.kind).about||kindOf(x.kind).label);
  }
  /** A fix in words: "Give line 3 min 2 → 4 too", "Remove line 2", "Let it end". */
  function fixWords(x:Issue,f:Fix,action=false):string{
    const e=byId.get(x.catalogue_id),L=(id:string)=>`line ${(e?linesOf(e):[]).findIndex(l=>l.id===id)+1}`;
    if(f.kind==='inherit')return `Let ${L(f.line_id!)} inherit its ${(f.numbers||[]).join(' and ')}`;
    if(f.kind==='none')return `Give ${L(f.ops[0].line_id)} no ${(f.numbers||[]).join(' and ')} (None)`;
    // The line a removal targets: its own id, else its one op's (a fix may carry only the op).
    if(f.kind==='remove')return `Remove ${L(f.line_id??f.ops[0]?.line_id??x.line_ids[0])}`+(f.recommended&&x.kind==='both'?` (${L(x.line_ids.find(i=>i!==f.line_id)!)} already covers it)`:'');
    if(f.kind==='extend')return `Extend to ${formatDate(new Date(Date.parse(f.ops[0].ends_on+'T12:00:00Z')-86400000).toISOString().slice(0,10))}`;
    if(f.kind==='advisory')return `Make ${f.ops.map(op=>L(op.line_id)).join(', ')} advisory`;
    if(f.kind==='resolve')return `Open Fix list (${f.workspace})`;
    const outcome={both:'Keep both',ending:'Let it end',nobody:'Keep',duplicate:'Keep both'}[x.kind]||'Keep';
    return action?`${outcome} · acknowledge…`:outcome;
  }
  function issueRow(x:Issue,pick:(label:string,ids:string[])=>HTMLInputElement){
    const e=byId.get(x.catalogue_id),live=e?linesOf(e):[],[best,...rest]=x.fixes;
    const lineCell=el('div',{class:'rt-ilines'},...x.lines.map(n=>live[n]?el('div',{class:'rt-iline'},el('span',{class:'rt-n'},String(n+1)),pills(ctx,live[n].who,names,'Everyone',openTag,'Who',1,marks),pills(ctx,live[n].where,names,'All clinics',openTag,'Where',1,marks),
      numbersNode(live[n],e?.inherited?.[live[n].id],sourceWords(()=>live,names),undefined,marks)):''));
    const can=canEdit&&!!e&&(best.kind==='resolve'||editable(e)),a=x.acknowledged,description=what(x);
    return el('tr',{class:'rt-issue','data-issue':x.id},el('td',{class:'dt-check'},canEdit?pick(`Select ${KINDS[x.kind]?.label} in ${e?.name}`,[x.id]):''),
      el('td',{'data-col':'issue-rule',class:wrapped('issue-rule').trim()},e?
        captured?el('strong',{},e.name):button(e.name,()=>go('rules/'+e.id),'',{class:'link-button rt-rule-link'}):''),
      el('td',{'data-col':'issue-lines',class:wrapped('issue-lines').trim()},x.lines.length?lineCell:el('span',{class:'muted'},'The rule')),
      el('td',{'data-col':'issue-what',class:wrapped('issue-what').trim()},el('span',{class:'dt-clip rt-what-text','data-tip':description},description)),
      el('td',{'data-col':'issue-recommendation',class:(a?'rt-acked':'rt-recommendation')+wrapped('issue-recommendation')},el('div',{class:'rt-rec'},
        el('div',{class:'rt-best'},el('small',{class:'rt-star'},'★ Recommended'),
          !a&&can?button(fixWords(x,best,true),()=>fix(x,best),'secondary small rt-recommended-action'):el('span',{class:'rt-choice'},fixWords(x,best))),
        a?el('div',{class:'rt-ack-detail'},el('div',{},`Acknowledged · ${a.by||'Someone'} · ${formatDate(a.at)}`),a.note?el('small',{class:'muted'},a.note):'',
          canEdit?button('Take back',()=>void ack([x],false),'',{class:'link-button','aria-label':`Take back the acknowledgement of ${KINDS[x.kind]?.label} in ${e?.name}`}):'')
        :can&&rest.length?el('div',{class:'rt-fixes'},...rest.map(f=>button(fixWords(x,f,true),()=>fix(x,f),'',{class:'link-button'}))):'')));
  }
  /** A fix goes into the rule's draft (the same proposal as every other change); keeping is an acknowledgement. */
  function fix(x:Issue,f:Fix,say=true){
    if(f.kind==='resolve'){go('schedule/issues?draft='+encodeURIComponent(f.workspace_id||''));return false;}  // Schedule › Fix list
    if(!f.ops.length){acknowledge([x]);return false;}
    const e=byId.get(x.catalogue_id);if(!e||!editable(e)){notify(`A change to ${e?.name||'this rule'} is already waiting for approval; approve or decline it first.`,'warning');return false;}
    const before=draftOf(e),d:Draft=JSON.parse(JSON.stringify(before)),same=(a:unknown,b:unknown)=>JSON.stringify(a)===JSON.stringify(b);
    const blocked=()=>{notify(`${e.name}: this fix conflicts with unsent edits. Review the line before applying it.`,'warning');return false;};
    // A recommendation was computed from the stored line. Apply only its changed fields to the local draft.
    // Conflicting local edits require review; unrelated numbers and notes must survive.
    for(const op of f.ops){const i=d.lines.findIndex(l=>l.id===op.line_id),stored=linesOf(e).find(l=>l.id===op.line_id);
      if(i<0){if(op.remove)continue;return blocked();}if(!stored)return blocked();
      const line=d.lines[i];
      if(op.remove){if(!same(line,stored))return blocked();d.lines.splice(i,1);continue;}
      if(op.numbers)for(const key of new Set([...Object.keys(stored.numbers),...Object.keys(op.numbers)])){
        const was=stored.numbers[key],want=op.numbers[key];if(same(was,want))continue;
        if(!same(line.numbers[key],was)&&!same(line.numbers[key],want))return blocked();
        if(want===undefined)delete line.numbers[key];else line.numbers[key]=JSON.parse(JSON.stringify(want));}
      if(op.ends_on!==undefined){if(!same(line.ends_on,stored.ends_on)&&!same(line.ends_on,op.ends_on))return blocked();line.ends_on=op.ends_on;}}
    keepDraft(e,d);applied.add(x.id);
    if(say){redraw();notify(`${fixWords(x,f)}: added to the proposal.`,'success',{label:'Undo',run:()=>{keepDraft(e,before);applied.delete(x.id);redraw();}});}
    return true;
  }

  // ---- Proposing every changed rule together ----
  async function proposeAll(){
    const rules=changedRules(all);if(!rules.length)return;
    for(const e of rules){const d=draftOf(e);
      if(d.on&&!d.lines.length){notify(`${e.name}: a rule that is on needs at least one line.`,'warning');return;}
      if(d.lines.some(l=>!Object.keys(l.numbers||{}).length)){notify(`${e.name}: every line needs a number (or Applies).`,'warning');return;}
      const missing=missingNeeds(e,d.settings,d.lines);if(d.on&&missing.length){notify(`${e.name}: choose ${missing.join(', ')} in its Settings first.`,'warning');return;}}
    const checks=await checkBeforeProposing(rules.map(e=>({e,body:draftOf(e)})),proposeAll);if(!checks)return;
    const said=(e:Entry)=>checkWords(e,()=>draftOf(e).lines,names);
    const stop=checks.flatMap((c,i)=>[...c.duplicates.map(d=>`${rules[i].name}: ${said(rules[i]).same(d)}.`),...c.issues.map(x=>`${rules[i].name}: ${x.message}`)]);
    if(stop.length){notify(stop.join(' '),'error');return;}
    const total=totalChanges();
    const intro=el('div',{class:'span-2 rt-propose'},...rules.flatMap(e=>{const d=draftOf(e);return [el('h4',{},`${e.name} · ${plural(changesIn(e,d),'change')}`),
      (e.rule?d.on!==isOn(e):d.on)?el('p',{},d.on?'Turns the rule on.':'Turns the rule off; its lines are kept.'):'',changeList(e,linesOf(e),d.lines,names,d.settings)];}));
    confirmAction(`Propose ${plural(total,'change')}`,`One new version of ${rules.length===1?rules[0].name:plural(rules.length,'rule')}, each applied once another authorised person approves it. Retiring a version later brings back the one before.`,async reason=>{
      let n=0;
      for(const [i,e] of rules.entries()){
        try{const d=draftOf(e),record=await propose(e,d,reason);rememberProposed(String(record.id),e,d);discardDraft(e.id);n++;}
        catch(err){await refresh();throw new Error(`${n} of ${rules.length} proposed. Stopped at ${e.name}: ${(err as Error).message}`);}
      }
      applied.clear();o.setLeave?.(null);notify(`Proposed ${plural(n,'rule')}; each applies once another authorised person approves it.`);await refresh();},'Propose',intro);
  }

  // ---- Drawing ----
  /** Redraw the table; a box, toggle or button that had keyboard focus hands it to the same control in the new table
   *  (by its key, else its name), so ticking with Space or opening a rule with Enter does not drop focus to the page. */
  function draw(){
    save();
    // Selection, expansion and wrapping keep the place being read; a different result set starts at its first row.
    const scope=JSON.stringify([s.mode,s.q,s.filters,s.conditions,s.group,s.sort,s.then_sort]),keepPosition=scope===treePosition?.scope,layout=s.mode==='rules'?'rules':'issues';
    // Native scroll events already keep the horizontal position for a new result set. Reading the old viewport here
    // would lay out a preceding search's whole table just before replacing it. Same-scope actions still capture
    // native coordinates directly, including a programmatic scroll immediately followed by a row action.
    // Rules and issues have different columns: entering the other layout starts with its identifying facts visible.
    const previous=keepPosition?body.querySelector<HTMLElement>('.rt-scroll'):null,left=treePosition?.layout===layout?(previous?.isConnected?previous.scrollLeft:treePosition.left):0,top=previous?.isConnected?previous.scrollTop:treePosition?.top??0;
    const a=document.activeElement as HTMLElement|null,key=a&&body.contains(a)?a.dataset.key||a.getAttribute('aria-label'):null;
    if(s.mode==='rules')drawRules();else drawAttention();
    const scroll=body.querySelector<HTMLElement>('.rt-scroll');
    if(scroll){
      treePosition={scope,layout,left,top:keepPosition?top:0};
      if(scroll.isConnected)restoreRulesScroll(target);
      // Native coordinates also survive a server refresh (acknowledge/Undo), scoped to this account and result set.
      scroll.addEventListener('scroll',()=>{if(scroll.isConnected&&treeStore===store&&treeSession===sessionKey()&&treePosition?.scope===scope)
        treePosition={scope,layout,left:scroll.scrollLeft,top:scroll.scrollTop};});
    }else treePosition={scope,layout,left,top:0};
    // On the next frame, when the table is laid out anyway (focusing now would force a layout of a 1,000-line table).
    if(key&&!a!.isConnected)requestAnimationFrame(()=>{if(document.activeElement&&document.activeElement!==document.body)return;
      body.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"],[aria-label="${CSS.escape(key)}"]`)?.focus({preventScroll:true});});
  }
  function paint(){
    const n=totalChanges(),on=all.filter(e=>isOn(e)).length,activeLine=(l:RuleLine)=>!v2||effective(l)==='active',inUse=all.filter(e=>isOn(e)&&linesOf(e).some(activeLine)),lineCount=all.filter(isOn).reduce((a,e)=>a+linesOf(e).filter(activeLine).length,0);
    relabel(proposeBtn,n?`Propose ${plural(n,'change')}`:'Propose');disabledWhy(proposeBtn,n?'':TERMS.nothingToPropose);proposeTip(proposeBtn,n);discardBtn.hidden=!n;simplify?.drafting(n>0);
    // The same count as the Rules view button (one predicate); how many of them are in force only when that differs.
    // What needs attention is said once, on the saved-view button (F-R1), not again here.
    const listed=all.filter(current).length;
    status.textContent=[...(v2?[inUse.length!==listed?`${inUse.length} in force`:'']:[`${on} on`]),plural(lineCount,'line'),n?`${plural(n,'change')} not yet proposed`:''].filter(Boolean).join(' · ');
    toolbar.sync();
  }
  toolbar=build();
  redraw();
}
const cap=(t:string)=>t.charAt(0).toUpperCase()+t.slice(1);

/** Why a rule not in force cannot be switched on from the list yet ('' = it can): it needs a line, or a setting only the
 *  department can choose (on its page). */
const cannotStart=(e:Entry)=>{if(e.rule)return '';const missing=missingNeeds(e,settingsOf(e));
  return missing.length?`Choose ${missing.join(', ')} in Settings first`:draftView(e).lines.length||Object.keys(e.default_line?.numbers||{}).length?'':'Add a line first';};
/** Add rule's first group, before the families: what a department starting out sets first (rest, workload, people per
 *  session, days in a row). */
const COMMON=['duty_spacing','workload_limit','people_per_session','days_in_a_row'];
