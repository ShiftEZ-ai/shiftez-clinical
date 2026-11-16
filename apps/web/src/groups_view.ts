/** Scheduling groups on the rota reviewed. The server owns
 *  every fact here (P2-BE `planning_context` on the workspace detail and summary, `attribution` on each Fix-list cause,
 *  per-group readiness and `review_items[].group_id` on submit/approve); nothing is recounted or classified here.
 *
 *  - Fix list › Group: Scheduling group: Mine to fix · Linked to <group> · Other groups (quiet, no actions) · No group.
 *  - A group row: initials · group · owner · state ("3 must fix" / "Ready"), the same in the Fix list, Submit and Approve. */
import {el,badge,plural,icon,type Child,needText,legend,avatar} from './dom.js';
import {fixRow,mustWords,checkedAgo,type FixCause,type FixHooks} from './fix_list.js';

export type Classification='mine'|'coupling'|'other_pending'|'unassigned';
export type Attribution={classification:Classification;group_ids?:string[];responsible_group_ids?:string[];required_group_ids?:string[];complete?:boolean};
/** One group's state on this rota (P2-BE planning_context.groups[]). `filled`/`total` are the group's cover places
 *  (not additive across groups); `last_change` is its latest attributed edit and `active_run` a queued or running group
 *  auto-schedule. */
export type GroupState={group_id:string;group_version_id?:string;name:string;must:number;could:number;ready:boolean;is_mine?:boolean;acknowledged?:number;owner_name?:string|null;
  last_change?:{by_name?:string|null;at:string}|null;active_run?:boolean;
  filled?:number;total?:number;adjustments?:number};
export type PlanningContext={group_version_id?:string|null;group_id?:string|null;/** The department's default (planning settings). */group_lookahead?:'coupled'|'none';capabilities?:{editable_group_ids?:string[]};
  counts?:Record<Classification,number>;held?:{count:number;session_ids:string[]};groups?:GroupState[];
};

export const planningOf=(...from:unknown[]):PlanningContext|null=>{for(const x of from){const p=(x as {planning_context?:PlanningContext}|null|undefined)?.planning_context;if(p?.groups?.length)return p;}return null;};
export const attributionOf=(c:FixCause)=>(c as FixCause&{attribution?:Attribution}).attribution||null;
export const attributed=(causes:FixCause[])=>causes.some(c=>!!attributionOf(c));

/** A group row: initials, the group and its owner, presence in words, and its state. `extra` goes before the state. */
export function groupRow(g:GroupState,o:{mine?:boolean;extra?:Child;state?:Child}={}):HTMLElement{
  o={mine:g.is_mine,...o};
  return el('div',{class:'group-row','data-group':g.group_id},avatar(g.owner_name||g.name,o.mine?'me':''),
    needText(g.name,g.owner_name||o.mine?(o.mine?'You':String(g.owner_name)):''),
    activity(g),o.extra||'',
    o.state??(g.must?badge(mustWords(g.must),'err'):badge('Ready','ok')));
}

/** "190 of 190 places filled · 2 adjustments" (SW3-A-25: the number says what it counts): a group's fill and its adjustments for this rota, when the server sends them. */
export const groupFill=(g:GroupState)=>el('span',{class:'muted'},[g.total?`${g.filled??0} of ${g.total} places filled`:'',g.adjustments?plural(g.adjustments,'adjustment'):''].filter(Boolean).join(' · '));

/** What is happening in a group, quietly: a running auto-schedule (a dot and words), else its last change. */
export const activity=(g:GroupState):Child=>g.active_run?el('span',{class:'g-live'},'Auto-scheduling…')
  :g.last_change?.at?el('span',{class:'muted g-change'},checkedAgo(g.last_change.at,'Changed')+(g.last_change.by_name?` by ${g.last_change.by_name}`:'')):'';

const section=(title:string,n:number,tone:'err'|'neutral',...body:Child[])=>el('section',{class:'panel fix-panel g-section','aria-label':title},
  el('div',{class:'g-sect'},el('h3',{},title),n?badge(String(n),tone):''),...body);

/** Fix list grouped by scheduling group (maintenance/R2): what is mine to fix, what is linked to another group (both groups see
 *  it; whoever fixes it clears it), the other groups as rows with their causes read only, and people in no group.
 *  `mine`: the group shown (the viewer's own, or the one chosen in the planner bar). */
export function groupSections(causes:FixCause[],p:PlanningContext,h:FixHooks,o:{mine?:string|null}={}):HTMLElement[]{
  const of=(k:Classification)=>causes.filter(c=>attributionOf(c)?.classification===k);
  const name=(id:string)=>p.groups?.find(g=>g.group_id===id)?.name||'another group';
  // One key for every section's rows (every mark has one): the caller's (the Fix list's), else this view's own.
  const own=h.key?null:legend('Key'),out:HTMLElement[]=[];if(own)h={...h,key:own};
  const mine=of('mine'),linked=of('coupling');
  if(mine.length)out.push(section('Mine to fix',mine.filter(c=>c.level==='must').length,'err',el('ul',{class:'fix-list'},...mine.map(c=>fixRow(c,h)))));
  // One section per other group a linked cause needs ("Linked to Residents"), each cause once.
  const by=new Map<string,FixCause[]>();
  for(const c of linked){const other=(attributionOf(c)?.required_group_ids||attributionOf(c)?.group_ids||[]).find(id=>id!==o.mine)||'';by.set(other,[...(by.get(other)||[]),c]);}
  // The held line (sessions another group relies on keep their people) closes the linked sections, or stands alone.
  const held=p.held?.count?el('p',{class:'g-held',role:'note'},icon('lock'),`${plural(p.held.count,'session')} another group relies on keep their people`):'';
  [...by].forEach(([id,list],i)=>out.push(section(`Linked to ${id?name(id):'another group'}`,list.filter(c=>c.level==='must').length,'err',el('ul',{class:'fix-list'},...list.map(c=>fixRow(c,h))),i===by.size-1?held:'')));
  if(held&&!by.size)out.push(held);
  // Other groups: their state and their causes, quiet (no actions): they are someone else's to fix.
  const pending=of('other_pending'),quiet={...h,act:undefined,fixes:undefined};
  const others=(p.groups||[]).filter(g=>g.group_id!==o.mine);
  if(others.length)out.push(section('Other groups',0,'neutral',el('ul',{class:'group-rows'},...others.map(g=>{
    const theirs=pending.filter(c=>(attributionOf(c)?.responsible_group_ids||attributionOf(c)?.group_ids||[]).includes(g.group_id));
    return el('li',{},groupRow(g),theirs.length?el('ul',{class:'fix-list g-quiet'},...theirs.map(c=>fixRow(c,quiet))):'');}))));
  const none=of('unassigned');
  if(none.length)out.push(section('No group',none.filter(c=>c.level==='must').length,'err',el('ul',{class:'fix-list'},...none.map(c=>fixRow(c,h)))));
  if(own)out.push(own.root);
  return out;
}
