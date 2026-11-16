/** K1g: who and where a rule line reaches. "15 people" and "9 clinics" are links (accent, dotted underline) in the line
 *  panel's Who / Where headings and in "This line says"; each opens its list in the shared picker: people grouped by the
 *  filter row that matched them, with their load against the limit in words ("3 of 2, over the limit"), a way to the
 *  rules that apply to one of them and to the Schedule; clinics with how they are counted (Each). Every answer is the server's reach
 *  details (rule_map.reach_details), never worked out here: a line as it is in force from GET /api/policies/reach (this
 *  week of the rota that holds today, with counts), a line being changed from the check of the draft (today, no counts).
 *  Excluded records never appear (the server leaves them out). Roles that cannot read details see the plain count. */
import {el,labelOf,plural,weekOrder,formatDate,formatDay,CAPABILITY} from './dom.js';
import {fullNameOf} from './person_label.js';
import {openPicker} from './picker.js';
import {get} from './api.js';
import {lineReachPath} from './rule_check_api.js';
import {conditionsText} from './conditions.js';
import type {Context} from './forms.js';
import type {Condition,Conditions,RuleLine} from './generated.js';
import type {Row} from './types.js';

/** A person's load against a line's number: a maximum (how full), or a minimum (`min`: how short). */
type Load={actual:number;limit:number;min?:boolean}|null;
/** How close to breaking the line: full against a maximum, short against a minimum. */
const risk=(l:NonNullable<Load>)=>l.min?(l.limit-l.actual)/(l.limit||1):l.actual/(l.limit||1);
export type Reach={people:Map<string,Load>;clinics:Map<string,{kind:'service'|'location';each:boolean}>;counted:boolean;
  /** Person or clinic id → the line's filter rows that matched it ("who.0", "where.1.2": the server's matched_paths). */paths:Map<string,Set<string>>;
  /** What the counts are of: "Rota name · 4 – 10 Oct", or "today" without counts. */period:string};
type Side='who'|'where';
type Group={label:string;ids:string[]};

export const readsReach=(ctx:Context)=>ctx.permits(CAPABILITY.ADMIN,CAPABILITY.COORDINATOR,CAPABILITY.APPROVER,CAPABILITY.PROGRAMME_LEAD,CAPABILITY.AUDITOR);
/** Everything the reach and its limits depend on: who, where, days, hours, dates, numbers and the line's settings. */
const reachKey=(l:RuleLine)=>JSON.stringify([l.who||null,l.where||null,l.weekdays||null,l.start_time||null,l.end_time||null,l.starts_on||null,l.ends_on||null,l.numbers||{},l.measure||null,l.period||null,l.settings||null]);

/** The server's rows for one line (one per counting bucket and number) as one reach: each person once with their
 *  fullest load against a maximum, each clinic or place once (Each when any bucket counts it on its own). */
function reachOf(rows:Row[],counted:boolean,period='today'):Reach{
  const people=new Map<string,Load>(),clinics:Reach['clinics']=new Map(),paths:Reach['paths']=new Map();
  const matched=(id:string,list:string[]|undefined)=>{let set=paths.get(id);if(!set)paths.set(id,set=new Set());for(const x of list||[])set.add(x);};
  for(const row of rows){
    for(const p of row.people||[]){matched(p.person_id,p.matched_paths);
      let best=people.get(p.person_id)??null;
      // A maximum's load wins the bar; a line with only a minimum shows how short each person is (a minimum prorated to 0 asks nothing).
      for(const c of p.counts||[]){const limit=c.limit??c.amount,min=c.comparison==='minimum';
        if(c.actual==null||limit==null||!min&&c.comparison!=='maximum'||!limit&&(min||!c.actual)||min&&best&&!best.min)continue;
        const l={actual:c.actual,limit,...(min?{min:true}:{})};if(!best||best.min&&!min||risk(l)>risk(best))best=l;}
      people.set(p.person_id,best);
    }
    for(const [list,kind] of [[row.clinics,'service'],[row.places,'location']] as const)for(const c of list||[]){
      const id=c.service_id||c.location_id,was=clinics.get(id);clinics.set(id,{kind,each:!!was?.each||c.count==='each'});matched(id,c.matched_paths);}
  }
  return {people,clinics,paths,counted,period};
}

/** This week (the department's week start) of the rota that holds today, clipped to it; none: no counts. */
export async function thisWeek(day:string):Promise<{id:string;from:string;to:string;name:string}|null>{
  const iso=(t:number)=>new Date(t).toISOString().slice(0,10),at=Date.parse(day+'T12:00:00Z'),dow=(new Date(at).getUTCDay()+6)%7;
  const start=at-((dow-weekOrder()[0]+7)%7)*86400000;
  const rotas=await get<{workspaces:Row[]}>('/api/workspaces?limit=50&offset=0').then(r=>r.workspaces).catch(()=>[] as Row[]);
  const w=rotas.find(r=>r.status!=='archived'&&r.starts_on<=day&&day<r.ends_on);if(!w)return null;
  const from=[iso(start),w.starts_on].sort()[1],to=[iso(start+7*86400000),w.ends_on].sort()[0];
  const last=new Date(Date.parse(to+'T12:00:00Z')-86400000).toISOString().slice(0,10);
  return from<to?{id:w.id,from,to,name:`${labelOf(w)} · ${formatDate(from)} – ${formatDate(last)}`}:null;
}
/** A dated line's own dates in words ("Sun 29 Nov – Sat 5 Dec 2026"; ends are exclusive), or '' for an undated line. */
export const lineDates=(l:Pick<RuleLine,'starts_on'|'ends_on'>)=>l.starts_on&&l.ends_on?
  `${formatDay(l.starts_on)} – ${formatDay(new Date(Date.parse(l.ends_on+'T12:00:00Z')-86400000).toISOString().slice(0,10),'UTC',true)}`:'';
/** The rota a dated line falls in, named after its dates: "Sun 29 Nov – Sat 5 Dec 2026 · Rota 1–31 Dec". */
async function datedPeriod(l:RuleLine){
  const rotas=await get<{workspaces:Row[]}>('/api/workspaces?limit=50&offset=0').then(r=>r.workspaces).catch(()=>[] as Row[]);
  const w=rotas.find(r=>r.status!=='archived'&&r.starts_on<l.ends_on!&&l.starts_on!<r.ends_on);
  return [lineDates(l),w?labelOf(w):''].filter(Boolean).join(' · ');
}
/** Reach of the rule as in force (this rule only: policy_id), read once per rule page: this week's counts when a rota
 *  holds today. A dated line (a person's week) is counted over its own dates instead (B3), read on its own. */
export function savedReach(ctx:Context,ruleId:string,day:string,primed?:{reach:Row|null;period:string}){
  let p:Promise<{lines:Row[];counted:boolean;period:string}|null>|null=null;
  // A deep link's read holds its own line (this week's counts when a rota holds today); any other line reads the rule's reach.
  const given=(primed?.reach?.details?.[ruleId]?.lines||[]) as Row[],counted=!!primed?.reach?.workspace_id;
  return (lineId?:string,line?:RuleLine)=>line?.starts_on&&line.ends_on&&readsReach(ctx)?Promise.all([get<Row>(lineReachPath(ruleId,line.id,day)),datedPeriod(line)])
      .then(([r,period])=>({lines:(r.details?.[ruleId]?.lines||[]) as Row[],counted:true,period})).catch(()=>null)
    :lineId&&given.some(x=>x.line_id===lineId)?Promise.resolve({lines:given,counted,period:counted?primed!.period:'today'}):p??=(async()=>{
    if(!readsReach(ctx))return null;
    const week=await thisWeek(day),q=week?`&workspace_id=${week.id}&starts_on=${week.from}&ends_on=${week.to}`:'';
    const one=`/api/policies/reach?details=true&policy_id=${encodeURIComponent(ruleId)}&day=${day}`;
    const r=await get<Row>(one+q).catch(()=>q?get<Row>(one):null);
    return r?{lines:(r.details?.[ruleId]?.lines||[]) as Row[],counted:!!week&&!!r.workspace_id,period:week&&r.workspace_id?week.name:'today'}:null;
  })().catch(()=>null);
}

/** The rows a side splits into for grouping, each with its path in the line (as the server's matched_paths name it):
 *  the "or" filters at the top ("who.1"), or those of the one "or" group among "and" filters ("who.0.1": Group(a or b
 *  or c) and not d). */
function splitOf(side:Side,t:Conditions|null|undefined):{row:Condition|Conditions;path:string}[]|null{
  if(!t||t.conditions.length<1)return null;
  if(t.match==='any'&&t.conditions.length>1)return t.conditions.map((row,i)=>({row,path:`${side}.${i}`}));
  const ors=t.conditions.filter(c=>'conditions' in c&&c.match==='any'&&c.conditions.length>1) as Conditions[];
  if(ors.length!==1)return null;
  const k=t.conditions.indexOf(ors[0]);
  return ors[0].conditions.map((row,i)=>({row,path:`${side}.${k}.${i}`}));
}

export type LineReach=ReturnType<typeof lineReach>;
export function lineReach(ctx:Context,o:{day:string;line:()=>RuleLine;
  /** The draft's lines with this one as it is being edited (its reach is with the more specific lines taking theirs). */lines:()=>RuleLine[];saved?:RuleLine;plain?:number;fromSaved:(lineId?:string,line?:RuleLine)=>Promise<{lines:Row[];counted:boolean;period:string}|null>;
  /** The server's check of exactly these lines (its reach_details); absent: this role cannot check. */check?:(lines:RuleLine[])=>Promise<{issues?:Row[];duplicates?:Row[];reach_details?:{lines:Row[]}}>;
  label:(field:string,value:string)=>string;whichRule?:(personId:string)=>void;onSchedule?:()=>void}){
  let now:Reach|null=null,timer=0,version=0;const listeners:(()=>void)[]=[];
  const asSaved=()=>!!o.saved&&reachKey(o.saved)===reachKey(o.line());
  async function load(){
    const v=++version;let r:Reach|null=null;
    if(asSaved()){const s=await o.fromSaved(o.line().id,o.line());if(s)r=reachOf(s.lines.filter(x=>x.line_id===o.line().id),s.counted,s.period);}
    else if(o.check){const c=await o.check(o.lines()).catch(()=>null);
      // The check counts a dated line over its own dates (maintenance), so the list says them.
      if(c&&!c.issues?.length&&!c.duplicates?.length)r=reachOf((c.reach_details?.lines||[]).filter((x:Row)=>x.line_id===o.line().id),false,lineDates(o.line())||'today');}
    if(v!==version)return;now=r;listeners.forEach(f=>f());
  }
  void load();

  const people=(ids:string[])=>new Map((ctx.catalogues.people||[]).filter(p=>ids.includes(p.id)).map(p=>[p.id,p]));
  const day=o.day,ranks=new Map<string,string>();
  for(const m of ctx.catalogues.memberships||[])if((!m.starts_on||m.starts_on<=day)&&(!m.ends_on||m.ends_on>day))ranks.set(m.person_id,labelOf((ctx.catalogues.categories||[]).find(c=>c.id===m.category_id)));
  const clinicName=(id:string,kind:string)=>labelOf((ctx.catalogues[kind==='service'?'services':'locations']||[]).find(r=>r.id===id));
  /** Where reaches clinics; places only when it names no clinic (a session's room is not a second clinic). */
  const where=(r:Reach)=>{const all=[...r.clinics.keys()],clinics=all.filter(id=>r.clinics.get(id)!.kind==='service');return clinics.length?clinics:all;};
  const noun=(side:Side)=>{const r=now!;if(side==='who')return plural(r.people.size,'person','people');
    const ids=where(r);return plural(ids.length,ids.some(id=>r.clinics.get(id)!.kind==='service')?'clinic':'place');};

  /** People (or clinics) by the row that matched them, from the server's matched_paths; one row: one list. */
  function groupsOf(side:Side,ids:string[]):Group[]{
    const split=splitOf(side,o.line()[side]);
    if(!split)return [{label:'',ids}];
    const seen=new Set<string>(),hit=(id:string,path:string)=>[...now!.paths.get(id)||[]].some(x=>x===path||x.startsWith(path+'.'));
    // Each person once, under the row that names them when one does (Alex under "Person is Alex", not "Rank is …").
    const named=(r:Condition|Conditions)=>!('conditions' in r)&&r.field==='person_ids';
    const mine=new Map<string,string[]>();
    for(const {row,path} of [...split.filter(x=>named(x.row)),...split.filter(x=>!named(x.row))]){
      const got=ids.filter(id=>!seen.has(id)&&hit(id,path));got.forEach(id=>seen.add(id));mine.set(path,got);}
    const out=split.map(({row,path})=>({label:conditionsText({match:'all',conditions:[row]},o.label),ids:mine.get(path)!}));
    const rest=ids.filter(id=>!seen.has(id));
    return [...out,...(rest.length?[{label:'Other',ids:rest}]:[])].filter(g=>g.ids.length);
  }
  /** The list in the shared picker (picker.ts): grouped by the filter row that matched, fullest load first (no
   *  Recent section to break that order), each person's rank and load as a bar with words ("1 of 1, at the limit"); picking a person shows the rules that apply to them. */
  function list(anchor:HTMLElement,side:Side){
    const r=now!,title=noun(side),ids=side==='who'?[...r.people.keys()]:where(r);
    const named=people(ids),load=(id:string)=>r.people.get(id)??null;
    const ratio=(id:string)=>{const l=load(id);return l?risk(l):-1;};
    const name=(id:string)=>side==='who'?fullNameOf(named.get(id)):clinicName(id,r.clinics.get(id)!.kind);
    const loadText=(l:Load)=>l?`${l.actual} of ${l.limit}${l.min?(l.actual<l.limit?', short':''):l.actual>l.limit?', over the limit':l.actual===l.limit?', at the limit':''}`:'';
    const hint=(id:string)=>side==='who'?ranks.get(id)||'':r.clinics.get(id)!.each?'Counted for each':'';
    // The load as a bar (red over the limit, gold at it) beside the same in words: never colour alone.
    const bar=(id:string)=>{const l=side==='who'?load(id):null;if(!l)return null;
      const fill=el('span',{class:'rc-load-fill'});fill.style.setProperty('--fill',`${Math.min(100,l.actual/(l.limit||1)*100)}%`);
      return el('span',{class:'rc-load'+(l.min?(l.actual<l.limit?' short':''):l.actual>l.limit?' over':l.actual===l.limit?' at':'')},el('span',{class:'rc-load-bar','aria-hidden':'true'},fill),el('span',{class:'rc-load-n'},loadText(l)));};
    const choices=groupsOf(side,ids).flatMap(g=>[...g.ids].sort((a,b)=>ratio(b)-ratio(a)||name(a).localeCompare(name(b)))
      .map(id=>({value:id,label:name(id),...(g.label?{group:g.label}:{}),hint:hint(id)})));
    const count=(test:(l:NonNullable<Load>)=>boolean)=>side==='who'?ids.filter(id=>{const l=load(id);return !!l&&test(l);}).length:0;
    const over=count(l=>!l.min&&l.actual>l.limit),at=count(l=>!l.min&&l.actual===l.limit),short=count(l=>!!l.min&&l.actual<l.limit);
    const go=o.onSchedule?el('button',{type:'button',class:'link-button'},'Show on Schedule →'):null;
    const close=openPicker(anchor,anchor.parentElement!,{label:`${title} this line applies to`,choices,multiple:false,selected:new Set(),recent:false,meta:c=>bar(c.value),
      footer:[r.period,over?`${over} over`:'',at?`${at} at the limit`:'',short?`${short} short`:''].filter(Boolean).join(' · '),
      ...(go?{extra:el('div',{class:'rc-reach-foot'},go)}:{}),
      onPick:id=>{if(side==='who')o.whichRule?.(id);}});
    go?.addEventListener('click',()=>{close();o.onSchedule!();});
  }
  /** " · 15 people" for a heading or "15 people" in a sentence: a link to the list, or the plain count. */
  const link=(side:Side):HTMLElement|string|null=>{
    if(now&&(side==='who'||now.clinics.size)){const b=el('button',{type:'button',class:'rc-reach-link','aria-haspopup':'dialog','aria-expanded':'false'},noun(side));
      b.addEventListener('click',()=>list(b,side));return b;}
    return side==='who'&&asSaved()&&o.plain!=null?plural(o.plain,'person','people'):null;
  };
  return {now:()=>now,link,noun,onChange:(f:()=>void)=>listeners.push(f),changed:()=>{window.clearTimeout(timer);timer=window.setTimeout(()=>void load(),300);}};
}
