/** Auto-schedule "People to schedule": a run may fill places for some people only (by person, rank, tag, programme,
 *  skill… whatever the server accepts), so a later group joins the same period without moving the people already
 *  placed. The server owns which fields and protections a run accepts (generation_capability.generation_scope) and who
 *  matches at each duty; this field only builds the choice with the shared condition builder. */
import {el,icon,button,popover,popup} from './dom.js';
import {conditionBuilder,type ConditionField} from './conditions.js';
import type {Conditions} from './generated.js';

/** A redraw can hand the open builder only to the same rota. */
export const peoplePopoverKey=(rota:string)=>'people-to-schedule-'+rota;
type SetGroup=(value:Conditions|null,required:boolean)=>void;
const groupChanges=new WeakMap<HTMLElement,SetGroup>();
/** A picker-styled button showing who, opening the shared builder over the person fields the server accepts.
 *  `required`: a group was asked for and is not chosen yet. An empty Apply leaves it so (Run waits): only the Everyone
 *  button chooses everyone, never an empty or refused group. */
export function peopleToSchedule(o:{key:string;fields:ConditionField[];value:Conditions|null;required:boolean;words:(p:Conditions)=>string;
  onChange:SetGroup}){
  const text=el('span',{class:'pick-text'}),trigger=el('button',{id:o.key,type:'button',class:'pick-button',...popup('dialog')},
    el('span',{class:'pick-values'},text),icon('chevronDown')) as HTMLButtonElement;
  const paint=()=>{const words=o.value?o.words(o.value):o.required?'Choose people…':'Everyone';
    text.textContent=words;text.classList.toggle('muted',!o.value);trigger.setAttribute('aria-label',`People to schedule: ${words}`);trigger.title=o.value?words:'';};
  /** Shown as given, without telling onChange (the caller already holds it). */
  const reset=(value:Conditions|null,required:boolean)=>{o.value=value;o.required=required;paint();};
  groupChanges.set(trigger,(value,required)=>{reset(value,required);o.onChange(value,required);});
  // Resolve the mounted button when the page swaps or the person applies the open builder.
  const current=()=>document.getElementById(o.key);
  const set:SetGroup=(value,required)=>groupChanges.get(current()??trigger)?.(value,required);
  // Opened, it starts with one ready line (a field and its values, from the server's fields): the plain way to choose
  // some people; more lines and groups stay in the same shared builder.
  const open=()=>popover(trigger,(panel,close)=>{const b=conditionBuilder(o.fields,o.value,{allOf:true,context:'rule'});
    panel.classList.add('fx-filter-pop');
    panel.append(el('div',{class:'popover-section'},el('span',{class:'popover-label'},'People to schedule'),b.root),
      el('div',{class:'popover-section fx-pop-actions'},button('Everyone',()=>{close();set(null,false);},'ghost small'),
        button('Apply',()=>{close();const v=b.read();set(v,!v);},'primary small')));},
    {label:'People to schedule',placement:'below',key:o.key,keep:current});
  trigger.addEventListener('click',open);paint();
  const wrap=el('div',{class:'field'},el('label',{'aria-hidden':'true'},'People to schedule'),el('div',{class:'pick'},trigger));
  return {wrap,open,reset};
}
