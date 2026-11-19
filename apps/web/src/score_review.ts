/** Score review reviewed: every soft rule line with its tier, weight and the
 *  points it lost on one draft, in the shared collection table, sorted by points lost. A tier or a Fair share weight is
 *  changed in place through the governed rule proposal (rule.propose: another person approves it; its Undo withdraws
 *  it); Must lines are listed, not editable here. The numbers are the server's (GET …/score-review): the same score,
 *  lines and contributions as the footer. */
import {el,select,field,badge,plural,notify,confirmAction,helpButton,type Child} from './dom.js';
import {get,ApiError} from './api.js';
import {dataTable} from './table_view.js';
import {conditionsSummary} from './conditions.js';
import {COMPONENT_LABELS} from './score.js';
import {propose,linesOf,isOn,settingsOf,type Entry} from './rule_catalogue.js';
import {STRENGTH_LABELS,type RuleLine,type ScoreReview,type ScoreReviewLine} from './generated.js';

export type ScoreReviewHooks={
  /** The department's draft rotas, newest first, and the one to show first (the open draft). */
  drafts:{id:string;name:string}[];draft:string;
  label:(field:string,value:string)=>string;
  /** May propose rule changes (coordinator, admin). */canPropose:boolean;
  /** The draft shown changed (the page keeps it in its address). */chosen:(id:string)=>void;
};
type Row={key:string;line:ScoreReviewLine|null;entry:Entry|null;rule:RuleLine|null;name:string;scope:string;must:boolean};
const TIERS=Object.keys(STRENGTH_LABELS) as (keyof typeof STRENGTH_LABELS)[];
const round=(n:number)=>Math.round(n*10)/10;

/** The rule line a review line is: its catalogue entry (by rule id) and the line (by id, else by number). */
function find(entries:Entry[],l:ScoreReviewLine):{entry:Entry|null;rule:RuleLine|null}{
  const entry=entries.find(e=>e.rule?.id===l.rule_id)||null,lines=entry?linesOf(entry):[];
  return {entry,rule:lines.find(x=>x.id===l.line_id)||(l.line!=null?lines[l.line]:undefined)||null};
}
/** The number a tier is set on: the review's slot, else the line's first number with a level. */
const slotOf=(l:ScoreReviewLine,r:RuleLine)=>l.slot&&r.numbers[l.slot]?l.slot:Object.keys(r.numbers).find(k=>r.numbers[k]?.level!=null)||Object.keys(r.numbers)[0];
const words=(r:RuleLine|null,label:ScoreReviewHooks['label'])=>r?[conditionsSummary(r.who,label)||'Everyone',conditionsSummary(r.where,label)].filter(Boolean).join(' · '):'';

/** One governed proposal changing one line (its tier, or its Fair share weight), with a reason. */
async function proposeLine(e:Entry,lineId:string,change:(r:RuleLine)=>void,reason:string){
  const lines=JSON.parse(JSON.stringify(linesOf(e))) as RuleLine[],target=lines.find(x=>x.id===lineId);if(!target)throw new Error('This line changed meanwhile. Reload the page.');
  change(target);
  const body={on:isOn(e),lines,settings:settingsOf(e),seen:linesOf(e).map(x=>x.id)};
  return propose(e,body,reason);
}

export async function scoreReview(hooks:ScoreReviewHooks,reload:()=>void):Promise<HTMLElement>{
  const root=el('div',{class:'score-review'});
  if(!hooks.drafts.length){root.append(el('p',{class:'muted'},'No draft rota to review. A draft’s score shows here once one exists.'));return root;}
  const draft=hooks.drafts.some(d=>d.id===hooks.draft)?hooks.draft:hooks.drafts[0].id;
  const [review,catalogue]=await Promise.all([get<ScoreReview>(`/api/workspaces/${encodeURIComponent(draft)}/score-review`),
    get<{entries:Entry[]}>('/api/rules/catalogue?version=2')]);
  const entries=catalogue.entries;
  // The advisory lines (the server's), then the Must lines of rules in force (an explicitly mandatory number).
  // shortcut: a number that inherits Must from a broader line is not listed as Must here; the Rules page shows it.
  const listed=new Set(review.lines.map(l=>`${l.rule_id}|${l.line_id}`));
  const rows:Row[]=[...review.lines.map((l,i)=>{const {entry,rule}=find(entries,l);
      return {key:`a${i}`,line:l,entry,rule,name:l.name,scope:words(rule,hooks.label),must:false};}),
    ...entries.filter(e=>isOn(e)).flatMap(e=>linesOf(e).filter(r=>!listed.has(`${e.rule!.id}|${r.id}`)&&Object.values(r.numbers).some(n=>n?.level==='mandatory'))
      .map(r=>({key:`m${e.rule!.id}${r.id}`,line:null,entry:e,rule:r,name:e.name,scope:words(r,hooks.label),must:true})))];
  // The score as the footer says it (capped while anything must be fixed); the uncapped one is said beside it.
  const s=review.score,value=s.score??null,lost=Object.entries(s.components as Record<string,{lost:number}>).filter(([,c])=>c.lost>0);
  const picker=select('Draft',hooks.drafts.map(d=>({value:d.id,label:d.name})),draft);
  picker.input.addEventListener('change',()=>hooks.chosen(picker.input.value));
  const head=el('div',{class:'sr-head'},
    el('div',{class:'sr-score'},el('b',{},String(value??'n/a')),el('small',{class:'muted'},s.uncapped_score!=null&&s.uncapped_score!==value?`of 100 · ${round(s.uncapped_score)} before the cap`:'of 100')),
    // No must-fix count here: the footer's Fix list count is the one (a count of checks here would disagree with it).
    lost.length?el('ul',{class:'sr-parts','aria-label':'Points lost by part'},...lost.map(([k,c])=>el('li',{},`${COMPONENT_LABELS[k]||k} `,el('b',{},String(round(c.lost)))))):'',
    helpButton('About the score','Auto-schedule fills required places first, then weighs rules, fairness, preferences and stability together. The score is that weighted grade out of 100, capped while anything must be fixed. Each line’s points lost are its share of 100 minus the score.'));

  const tierCell=(r:Row):Child=>{
    if(r.must)return badge('Must','neutral');
    const l=r.line!,current=l.level==='off'?'':l.level;
    if(!hooks.canPropose||!r.entry||!r.rule||r.entry.proposed||l.level==='off')return el('span',{},STRENGTH_LABELS[current as keyof typeof STRENGTH_LABELS]||'Off',r.entry?.proposed?badge('Proposed','info'):'');
    // The shared select, its name in its aria-label (a label per row would be clutter in a table).
    const pick=select('Tier',TIERS.map(t=>({value:t,label:STRENGTH_LABELS[t]})),current).input;
    pick.classList.add('sr-tier');pick.setAttribute('aria-label',`Tier of ${r.name}${r.scope?' · '+r.scope:''}`);
    pick.addEventListener('change',()=>{const to=pick.value as typeof TIERS[number];pick.value=current;
      confirmAction(`Propose ${STRENGTH_LABELS[to]} for ${r.name}`,'One new version of the rule, applied once another authorised person approves it.',async reason=>{
        const slot=slotOf(l,r.rule!);await proposeLine(r.entry!,r.rule!.id,x=>{x.numbers[slot]={...x.numbers[slot],level:to};},reason);
        notify('Proposed; it applies once another authorised person approves it.');reload();},'Propose');});
    return pick;};
  const weightCell=(r:Row):Child=>{
    if(r.must)return '—';
    const l=r.line!,own=r.rule?.settings?.balance_goal?.weight,was=String(own??(r.entry?settingsOf(r.entry).balance_goal?.weight:undefined)??1);
    if(l.kind!=='balance_goal'||!hooks.canPropose||!r.entry||!r.rule||r.entry.proposed)return el('span',{'data-tip':'The tier’s value times the line’s own weight'},`×${round(l.weight)}`);
    const f=field('Weight','number',was,{min:0,step:'any'});
    f.input.classList.add('sr-weight');f.input.setAttribute('aria-label',`Weight of ${r.name}${r.scope?' · '+r.scope:''}`);
    f.input.addEventListener('change',()=>{const n=Number(f.input.value);if(!f.input.checkValidity()||!Number.isFinite(n)){f.input.value=was;return;}f.input.value=was;
      confirmAction(`Propose weight ${n} for ${r.name}`,'One new version of the rule, applied once another authorised person approves it.',async reason=>{
        await proposeLine(r.entry!,r.rule!.id,x=>{x.settings={...(x.settings||{}),balance_goal:{...(x.settings?.balance_goal||{}),weight:n}};},reason);
        notify('Proposed; it applies once another authorised person approves it.');reload();},'Propose');});
    return f.input;};
  const table=dataTable<Row>({id:'score-review',noun:['rule line','rule lines'],rows,rowId:r=>r.key,pageSize:50,
    searchLabel:'Search rules',searchPlaceholder:'Rule, who or where',controls:[picker.wrap],
    defaults:{sort:['lost','desc']},empty:['No rule lines','No advisory or Must rule line is in force for this draft.'],
    columns:[
      {key:'rule',label:'Rule',text:r=>`${r.name} ${r.scope}`,cell:r=>el('span',{class:'sr-rule'},el('strong',{},r.name),r.scope?el('small',{class:'muted'},r.scope):'')},
      {key:'tier',label:'Tier',facet:r=>r.must?'Must':STRENGTH_LABELS[r.line!.level as keyof typeof STRENGTH_LABELS]||'Off',sort:r=>r.must?99:TIERS.indexOf(r.line!.level as typeof TIERS[number]),cell:tierCell},
      {key:'weight',label:'Weight',num:true,sort:r=>r.line?.weight??-1,cell:weightCell},
      {key:'lost',label:'Points lost',num:true,sort:r=>r.line?.lost_points??-1,
        cell:r=>r.line?el('span',{class:'sr-lost'},el('b',{},String(round(r.line.lost_points))),r.line.lost_points?el('small',{class:'muted'},` · ${Math.round(r.line.lost_share*100)}%`):''):'—'},
      {key:'reach',label:'Reach',num:true,sort:r=>r.line?.session_count??-1,
        cell:r=>r.line?`${plural(r.line.session_count,'session')} · ${plural(r.line.people_count,'person','people')}`:'—'},
      {key:'part',label:'Part',hidden:true,cell:()=>'',facet:r=>r.line?.component?COMPONENT_LABELS[r.line.component]:'',text:r=>r.line?.component?COMPONENT_LABELS[r.line.component]:''},
    ]}).root;
  root.append(head,table);
  return root;
}
