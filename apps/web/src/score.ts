import {adoptPopover,footerAct,el,icon,popover,type Tone,emptyLine} from './dom.js';
import {mustWords} from './fix_list.js';import type {Row} from './types.js';

/* The schedule score (packages/domain/score.py): a compact pill for the planner bar, the "Why this score" popover,
 * and before/after deltas for Compare rotas and auto-schedule results. The server computes it; nothing is scored here. */
export const COMPONENT_LABELS:Record<string,string>={coverage:'Coverage',rules:'Rules & targets',fairness:'Fairness',preferences:'Preferences',stability:'Stability',gate:'Not publishable (capped)',other:'Other'};
const signed=(n:number)=>(n>0?'+':n<0?'−':'±')+String(Math.round(Math.abs(n)*10)/10);  // "+2.5", "−3" (no ".0")
/** Tone of a score (one status palette): 85+ green, 60–84 plain (not "running" blue), below 60 gold, not publishable red. */
export const scoreTone=(s:Row):Tone=>shownScore(s)==null?'neutral':!s.publishable?'err':shownScore(s)!>=85?'ok':shownScore(s)!>=60?'neutral':'warn';
/** The number shown: the score before the blocking-issue cap (uncapped_score), so every change still moves it; the
 *  must-fix issues are said beside it ("3 must fix"), never folded into the number. Older stored scores have only `score`.
 *  A rota with no active session has no score (null, gate EMPTY_ROTA): shown as "n/a". */
export const shownScore=(s:Row):number|null=>s.uncapped_score??s.score??null;
export function scoreDelta(before:Row|null|undefined,after:Row|null|undefined):number|null{
  const a=after?shownScore(after):null,b=before?shownScore(before):null;
  return a==null||b==null?null:Math.round((a-b)*10)/10;
}
const shown=(s:Row)=>shownScore(s)??'n/a';
const blockingOf=(s:Row):number=>s.blocking_count??(s.publishable===false?1:0);
/** "3 must fix": the badge beside a score that is not publishable yet. */
export const blockingBadge=(s:Row)=>blockingOf(s)?el('span',{class:'pill-extra score-blocking'},` · ${mustWords(blockingOf(s))}`):'';
const scoreWords=(s:Row)=>shownScore(s)==null?`Schedule score n/a${s.gate?.message?': '+s.gate.message:''}`:`Schedule score ${shownScore(s)} of 100${blockingOf(s)?`, ${mustWords(blockingOf(s))}: not publishable until fixed`:''}`;

const lastScore=new Map<string,number>();
/** Planner pill: the score, a short delta after an edit, and a popover explaining the lost points. */
export function scorePill(key:string,score:Row,nameOf:(personId:string)=>string,openShift:(shiftId:string)=>void,review?:()=>void):HTMLElement{
  const value=shownScore(score),before=lastScore.get(key),delta=before===undefined||value==null?0:Math.round((value-before)*10)/10;if(value!=null)lastScore.set(key,value);
  const label=scoreWords(score);
  const b=el('button',{type:'button',class:`health-pill score-pill ${scoreTone(score)}`,'aria-haspopup':'dialog','aria-expanded':'false','aria-label':label,'data-ctip':label},
    icon('chart'),el('span',{class:'num'+(delta?' num-moved':'')},String(value??'n/a')),el('span',{class:'label'},' score'),
    delta?el('span',{class:'pill-extra score-delta '+(delta>0?'up':'down')},' '+signed(delta)):'');
  // The footer's one delegated handler (dom.ts footerAct, maintenance); a redrawn footer hands the open "Why" to the new pill.
  footerAct(b,'score-pill',()=>popover(b,(panel,close)=>panel.append(...scoreWhy(score,nameOf,shiftId=>{close();openShift(shiftId);},review&&(()=>{close();review();}))),{label:'Why this score',placement:'below-end',key:'score-pill'}));
  queueMicrotask(()=>adoptPopover('score-pill',b));
  return b;
}

/** "Why this score": component bars, the lost points (they add up to 100 − score), and the people most affected. */
/** `review`: the draft's Score review (Configuration › Auto-schedule & scoring), every soft rule line and its points. */
export function scoreWhy(score:Row,nameOf:(personId:string)=>string,openShift?:(shiftId:string)=>void,review?:()=>void):HTMLElement[]{
  const parts=Object.entries(score.components as Record<string,Row>).filter(([,c])=>c.value!==null&&c.weight);
  const value=shownScore(score),n=blockingOf(score);
  if(value==null)return [el('div',{class:'popover-head score-head'},el('div',{},el('strong',{},'Schedule score n/a'),el('small',{},score.gate?.message||'Nothing to score yet')))];
  const head=el('div',{class:'popover-head score-head'},el('div',{},el('strong',{},`Schedule score ${value} / 100`),
    el('small',{},[n?`${mustWords(n)}: not publishable until fixed`:'',value>=100?'Nothing lost: every scored part is at its best':'Lost points below add up to '+(100-value).toFixed(1)].filter(Boolean).join(' · '))));
  const bars=el('div',{class:'popover-section score-bars'},...parts.map(([name,c])=>el('div',{class:'score-bar'},
    el('span',{class:'score-bar-label'},COMPONENT_LABELS[name]||name),
    el('meter',{min:'0',max:'1',low:'0.6',high:'0.85',optimum:'1',value:String(c.value),'aria-label':`${COMPONENT_LABELS[name]} ${Math.round(c.value*100)}%`}),
    el('small',{class:'muted'},c.lost?'−'+c.lost.toFixed(1):'—'))));
  // The cap is not a lost point of the number shown (blocking issues are said in the head), so its line is left out.
  const lines:Row[]=(score.why||[]).filter((w:Row)=>w.component!=='gate'||score.uncapped_score==null);
  const why=el('div',{class:'popover-section'},el('span',{class:'popover-label'},'Why this score'),lines.length?el('ul',{class:'menu-list score-why'},...lines.map(w=>{
    const text=el('span',{class:'issue-text'},el('strong',{},'−'+w.points.toFixed(1)+' · '+(COMPONENT_LABELS[w.component]||w.component)),el('small',{},w.text));
    if(!w.shift_id||!openShift)return el('li',{class:'score-line'},text);
    const row=el('button',{type:'button',class:'menu-item issue-item'},text,el('span',{class:'hint'},'Open'));row.addEventListener('click',()=>openShift(w.shift_id));
    return el('li',{},row);
  })):emptyLine('Nothing lost. Every scored part is at its best.'));
  const people=Object.entries((score.people||{}) as Record<string,Row>).filter(([,p])=>p.issues?.length).sort((a,b)=>a[1].score-b[1].score).slice(0,5);
  const who=people.length?el('div',{class:'popover-section'},el('span',{class:'popover-label'},'People most affected'),
    el('ul',{class:'menu-list'},...people.map(([pid,p])=>el('li',{class:'score-line'},el('span',{class:'issue-text'},el('strong',{},`${nameOf(pid)} · ${p.score}`),el('small',{},p.issues.map((i:Row)=>i.text.split(' · ').slice(1).join(' · ')||i.text).join('; '))))))):null;
  // At 100 the head already says nothing was lost: no empty "Why this score" list under it.
  const more=review?el('div',{class:'popover-section'},el('button',{type:'button',class:'link-button score-review-link',onclick:review},'Review rule scores')):null;
  return [head,bars,...(lines.length||value<100?[why]:[]),...(who?[who]:[]),...(more?[more]:[])];
}

/** One line comparing two scores, with the component changes that moved it most. */
export function scoreChange(before:Row|null|undefined,after:Row|null|undefined):HTMLElement|null{
  if(!after)return null;
  if(!before||shownScore(before)==null||shownScore(after)==null)return el('p',{class:'score-change'},el('strong',{},`Schedule score ${shown(after)}`),blockingBadge(after));
  const delta=scoreDelta(before,after)!;
  const moves=Object.keys(COMPONENT_LABELS).filter(c=>before.components?.[c]!=null&&after.components?.[c]!=null)
    .map(c=>{const a=after.components[c],b=before.components[c];const d=Math.round(((a?.value??a)-(b?.value??b))*1000)/10;return [c,d] as [string,number];})
    .filter(([,d])=>d).sort((x,y)=>Math.abs(y[1])-Math.abs(x[1]));
  return el('p',{class:'score-change'},el('strong',{},`Schedule score ${shownScore(before)} → ${shownScore(after)}`),` (${signed(delta)})`,blockingBadge(after),
    moves.length?el('small',{class:'muted'},' · '+moves.map(([c,d])=>`${COMPONENT_LABELS[c]} ${signed(d)}%`).join(', ')):'');
}
