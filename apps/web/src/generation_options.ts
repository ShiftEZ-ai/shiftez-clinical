import {el,field,select,check,button,armed,tabs,icon,plural,sparkline,duration,durationField,type DurationField} from './dom.js';
import {GENERATION_TIME_FIELDS,AUTOSCHEDULE_ANIMATION_DEFAULTS,AUTOSCHEDULE_ANIMATION_LIMITS,AUTOSCHEDULE_ANIMATION_CHOICES,type AutoscheduleAnimationSettings,type AutoscheduleBigMoveSettings,type AutoscheduleLiveEvent,type AutoscheduleLivePair,type AutoscheduleProgress,AUTO_SCHEDULE_LIMITS,GENERATION_PROGRESS_STAGES,GENERATION_SEARCH_FINISHED_STAGES,GENERATION_WEIGHT_PRESETS,SCORE_WEIGHT_LABELS,SCORE_WEIGHT_HELP,STRENGTH_LABELS,GENERATION_STOP_FIELDS,GENERATION_CONFLICT_CHOICES,MAX_SCORE_INPUT,MAX_WISH_POINTS,WISH_PRIORITY_FIELDS,type GenerationOptions,type ScoringSettings} from './generated.js';
import type {Row} from './types.js';
import {GENERATION_FILL_FIELDS} from './generated.js';

// The one auto-schedule configuration: department defaults in Configuration › Auto-schedule & scoring. A run picks
// minutes, a preset and Keep existing assignments in the planner's Auto-schedule panel. Factory defaults come from the server.
const WEIGHTS=Object.entries(SCORE_WEIGHT_LABELS) as [string,string][];
const WEIGHT_HELP:Readonly<Record<string,string>>=SCORE_WEIGHT_HELP;
const STRENGTHS=Object.entries(STRENGTH_LABELS) as [string,string][];
// A field's explanation is its "?" beside the label, not helper text under it (it would push the row out of line).
// The input keeps the label's own words as its name (the "?" inside the label would otherwise join it).
const hint=<T extends {wrap:HTMLElement,input:HTMLElement}>(f:T,text:string)=>{const label=f.wrap.querySelector('label'),name=label?.textContent||'';
  f.input.setAttribute('aria-label',name);
  label?.append(el('button',{type:'button',class:'icon-button bare score-help','aria-label':`About ${name}`,'data-help':'','data-tip':text},icon('help')));return f;};
// The "?" is a 24 px target (WCAG 2.5.8), the size of every icon button.
const group=(legend:string,help:string,...children:Node[])=>el('fieldset',{class:'score-grid'},el('legend',{},legend,help?el('button',{type:'button',class:'icon-button bare score-help','aria-label':`About ${legend}`,'data-help':'','data-tip':help},icon('help')):null),...children);

/** Run presets come from the contracts: each sets the score weights one run optimises (null = the department's own).
 *  The first is the department default. Coverage and must rules always come first, whatever the preset. */
type Weights=Record<string,number>;
export const PRESETS=Object.entries(GENERATION_WEIGHT_PRESETS).map(([key,p])=>({key,label:p.label,weights:p.weights as Weights|null}));
export const DEFAULT_PRESET=PRESETS[0].key;
/** A preset this browser remembered that the contracts no longer offer falls back to the department default. */
export const knownPreset=(key:string)=>PRESETS.some(p=>p.key===key)?key:DEFAULT_PRESET;
/** The options a run sends: the department's, plus the preset by name (the server resolves its weights). */
export function presetOptions(defaults:GenerationOptions,key:string):GenerationOptions{
  const o=structuredClone(defaults);
  if(knownPreset(key)!==DEFAULT_PRESET)o.weight_preset=key as NonNullable<GenerationOptions['weight_preset']>;
  return o;
}
/** "Coverage 35% · Rules & targets 25% · …" for a preset (the department's weights for the default one). */
export function weightSummary(defaults:GenerationOptions,key:string){
  const w=PRESETS.find(p=>p.key===knownPreset(key))?.weights||(defaults.scoring?.weights||{}) as Weights;
  const total=WEIGHTS.reduce((n,[k])=>n+(Number(w[k])||0),0)||1;
  return WEIGHTS.filter(([k])=>Number(w[k])).map(([k,label])=>`${label} ${Math.round(Number(w[k])/total*100)}%`).join(' · ');
}

/** Keep improving fairness (fairness_plateau_seconds): one field for the department default (Timing) and one run
 *  (the planner's Priorities). The contract gives no words for it: keep searching while fairness still improves, up to
 *  this long without progress. */
/** A run's time as the one duration input (dom.ts durationField): its words, help, default, bounds and unit are the
 *  contract's (GENERATION_TIME_FIELDS), the same on Configuration and on the run card (LEAD-UIUX L10). */
export type TimeKey=keyof typeof GENERATION_TIME_FIELDS;
export function timeField(key:TimeKey,value:number|null|undefined,o:{label?:string;max?:number}={}):DurationField{
  const d=GENERATION_TIME_FIELDS[key];
  return hint(durationField(o.label||d.title,d.unit,value??d.default,{min:d.min,max:o.max??d.max}),d.help);
}
export const fairnessPlateauField=(value:number|undefined)=>timeField('fairness_plateau_seconds',value);
/** Stop when: the run's stop conditions beside its search time, one component for the department default (Timing) and
 *  one run (Setup › Change). Words, defaults and bounds are the contract's (GENERATION_STOP_FIELDS); an empty optional
 *  number is off. `now`: the department's clock ("HH:MM") for the per-run Stop by (a time already past is refused); without
 *  it Stop by is not offered (a department default would be a time on no particular day). */
type StopOptions=Pick<GenerationOptions,'stop_without_improvement_seconds'|'target_gap_percent'|'target_score'|'stop_by'|'on_conflict'>;
const STOP_KEYS=['stop_without_improvement_seconds','target_gap_percent','target_score','stop_by','on_conflict'] as const;
export function stopFields(initial:Partial<StopOptions>,now?:()=>string){
  const F=GENERATION_STOP_FIELDS,num=(k:'target_gap_percent'|'target_score')=>{const f=F[k];
    return hint(field(f.title,'number',initial[k]??'',{required:false,min:f.min,max:f.max,step:f.integer?1:'any'}),f.help);};
  const stall=timeField('stop_without_improvement_seconds',initial.stop_without_improvement_seconds),gap=num('target_gap_percent'),score=num('target_score');
  const clock=now?hint(field(F.stop_by.title,'time',initial.stop_by??''),F.stop_by.help):null;
  const conflict=hint(select(F.on_conflict.title,Object.entries(GENERATION_CONFLICT_CHOICES).map(([value,label])=>({value,label})),initial.on_conflict||F.on_conflict.default),F.on_conflict.help);
  conflict.wrap.classList.add('auto-wide');
  const opt=(f:{input:HTMLInputElement})=>f.input.value===''?null:Number(f.input.value);
  const read=():StopOptions=>({stop_without_improvement_seconds:stall.value(),target_gap_percent:opt(gap),target_score:opt(score),
    stop_by:clock?.input.value||null,on_conflict:conflict.input.value as StopOptions['on_conflict']});
  // The shared time input checks the format (and clears its message) first on each input and change; a whole time
  // already past today is refused here, after it.
  const sync=()=>{const v=clock?.input.value||'';if(clock&&now&&/^\d\d:\d\d$/.test(v)&&v<=now())clock.input.setCustomValidity('Choose a time later today.');};
  const set=(d:Partial<StopOptions>)=>{stall.set(d.stop_without_improvement_seconds??F.stop_without_improvement_seconds.default??0);for(const [f,v] of [[gap,d.target_gap_percent],[score,d.target_score],[clock,d.stop_by],[conflict,d.on_conflict||F.on_conflict.default]] as const)
    if(f)f.input.value=v==null?'':String(v);clock?.input.setCustomValidity('');sync();};
  const inputs=[stall,gap,score,...(clock?[clock]:[]),conflict];
  clock?.input.addEventListener('input',sync);clock?.input.addEventListener('change',sync);sync();
  return {stall,gap,score,clock,conflict,inputs,read,set,valid:()=>inputs.every(f=>f.input.checkValidity())};
}
/** Whether a run's stop conditions all equal the department's (unset optional ones are equal). */
export const stopsDefault=(o:Partial<StopOptions>,d:Partial<StopOptions>)=>{const v=(x:Partial<StopOptions>,k:typeof STOP_KEYS[number])=>x[k]??GENERATION_STOP_FIELDS[k].default??null;
  return STOP_KEYS.every(k=>v(o,k)===v(d,k));};
/** "After 5 min, or 10 s without improvement": the run's stop conditions as one line (Stop when, closed). */
export function stopWords(minutes:number,o:Partial<StopOptions>){
  const or=[o.stop_without_improvement_seconds?`${duration(o.stop_without_improvement_seconds)} without improvement`:'',o.target_gap_percent!=null?`within ${o.target_gap_percent}% of the best possible`:'',
    o.target_score!=null?`at score ${o.target_score}`:'',o.stop_by?`at ${o.stop_by}`:''].filter(Boolean);
  return [`After ${duration(minutes*60)}`,...or].join(', or ')+(o.on_conflict==='continue_flagged'?` · rules conflict: ${GENERATION_CONFLICT_CHOICES.continue_flagged.toLowerCase()}`:'');
}
let shareIds=0;
/** The Configuration tab last shown: a save or Discard draws the page again on the same tab. */
let shownTab='decides';
/** `defaults`: what Reset puts back (the factory defaults in Configuration, the department's own in a run). */
export function generationOptionsFields(initial:GenerationOptions,onChange:(value:GenerationOptions)=>void,defaults?:GenerationOptions,resetLabel='Reset to recommended'){
  const s=(initial.scoring||defaults?.scoring||{}) as ScoringSettings;
  const nums=(pairs:[string,string][],values:unknown,min:number,max:number,step:number,suffix:string)=>
    Object.fromEntries(pairs.map(([key,label])=>[key,field(label+suffix,'number',(values as Record<string,number>|undefined)?.[key]??0,{required:true,min,max,step})]));
  // How a run decides: fixed steps first, then everything else weighed together by the score weights (the weights' "?").
  const decides='Must rules and safety are never broken, and required places are filled before anything else. Everything else is weighed together by these weights, the same weights that grade the schedule score (0–100); anything that must be fixed caps it. Run presets in the planner change them for one run.';
  const balance=select('Balance workload by',[{value:'reserved_hours',label:'Reserved hours'},{value:'duties',label:'Number of duties'}],initial.balance_measure);
  const fillOrder=hint(select('Fill priorities',[{value:'weighted',label:'Weighed with everything else'},{value:'tiered',label:'Higher priorities filled first'}],initial.allocation_order||'weighted'),
    'How the priority numbers on Allocation order lines are used.');
  fillOrder.wrap.classList.add('auto-wide');  // its choices are long (C-09)
  const overtime=hint(field('Avoid overtime weight','number',initial.overtime_penalty,{required:true,min:1,max:MAX_SCORE_INPUT,step:1}),'Higher makes permitted overtime less attractive.');
  const weights=nums(WEIGHTS,s.weights,0,MAX_SCORE_INPUT,1,' weight');
  // Each weight shows its share of the score beside it (C-08); what each part measures is the group's "?".
  const shares=Object.fromEntries(Object.entries(weights).map(([key,f])=>{const share=el('span',{class:'weight-share',id:`weight-share-${++shareIds}`});
    f.input.setAttribute('aria-describedby',share.id);f.wrap.append(share);return [key,share];}));
  const strengths=nums(STRENGTHS,s.strengths,1,MAX_SCORE_INPUT,1,' strength (×)');
  // How three score parts are measured (the solver uses the same settings).
  const worstOff=hint(field('Worst-off fairness emphasis (0–1)','number',s.fairness_worst_off_emphasis??0.5,{required:true,min:0,max:1,step:'any'}),
    'How much the person furthest from a fair share counts, beside the average. 0 looks only at the average.');
  const desired=hint(field('Desired-cover factor (0–1)','number',s.desired_cover_factor??0.25,{required:true,min:0,max:1,step:'any'}),
    'How much a missing desired place lowers coverage, compared with a required one.');
  const prefMean=field('Mean preference weight','number',s.preferences_mean_weight??7,{required:true,min:0,max:MAX_SCORE_INPUT,step:1});
  const prefWorst=hint(field('Worst preference weight','number',s.preferences_worst_weight??3,{required:true,min:0,max:MAX_SCORE_INPUT,step:1}),
    'Blends everyone’s average preference result with the least satisfied person’s.');
  const mode=select('Default for each run',[{value:'rebalance',label:'Rebalance unlocked assignments'},{value:'fill_gaps',label:'Keep assignments and fill gaps'}],initial.mode);
  mode.wrap.classList.add('auto-wide');  // its choices are long: two columns wide where there is room
  const maximum=timeField('max_search_minutes',initial.max_search_minutes);
  const budget=timeField('search_minutes',initial.search_minutes,{max:initial.max_search_minutes});
  // Stop when: the department's default stop conditions (Stop by is per run only).
  const stops=stopFields(initial),stall=stops.stall;
  const fairStall=fairnessPlateauField(initial.fairness_plateau_seconds);
  const shortlist=hint(field('People tried first for each place','number',initial.shortlist_width??3,{required:true,min:0,max:20,step:1}),'Faster for large departments; 0 tries everyone.');
  const fillMeta=GENERATION_FILL_FIELDS.fill_tier_time_share;
  const fillShare=hint(field(fillMeta.title,'number',initial.fill_tier_time_share??fillMeta.default,{required:true,min:fillMeta.min,max:fillMeta.max,step:'any'}),fillMeta.help);
  const pairBudget=hint(field('Most pairs a pair limit may weigh','number',initial.pair_limit_max_opportunities??50000,{required:true,min:1,max:1000000,step:1}),
    'A larger department may need more; a run stops and says so when a pair limit needs more.');
  const diagnostics=timeField('diagnostic_seconds',initial.diagnostic_seconds);
  const points=hint(field('Wish points per person (0 = wishes off)','number',s.wish_budget_points??10,{required:true,min:0,max:MAX_WISH_POINTS,step:1}),'Everyone gets the same points to spend.');
  const period=select('Wish points renew each',[{value:'week',label:'Week'},{value:'month',label:'Month'},{value:'quarter',label:'Quarter'}],s.wish_period||'month');
  const cap=hint(field('Catch-up for declined wishes (up to ×)','number',s.wish_debt_cap??3,{required:true,min:1,max:MAX_SCORE_INPUT,step:'any'}),'Declined wishes weigh more next time.');
  // F-C1 (maintenance): how wish priority builds up; words, defaults and bounds are the contract's (WISH_PRIORITY_FIELDS).
  const wishField=(k:keyof typeof WISH_PRIORITY_FIELDS)=>{const f=WISH_PRIORITY_FIELDS[k] as {title:string;help:string;default:number;min:number|null;max:number|null;integer:boolean};
    return hint(field(f.title,'number',s[k]??f.default,{required:true,...(f.min!=null?{min:f.min}:{}),...(f.max!=null?{max:f.max}:{}),step:f.integer?1:'any'}),f.help);};
  const decay=wishField('wish_priority_decay'),credit=wishField('wish_granted_credit'),history=wishField('wish_history_periods');
  const numbers=[decay,credit,history,overtime,...Object.values(weights),...Object.values(strengths),worstOff,desired,prefMean,prefWorst,maximum,budget,stall,stops.gap,stops.score,fairStall,shortlist,fillShare,pairBudget,diagnostics,points,cap];
  const num=(f:{input:HTMLInputElement})=>Number(f.input.value)||0;
  const explain=el('p',{class:'muted score-explain','aria-live':'polite'}),totalLine=el('p',{class:'hint weights-total warn-text','aria-live':'polite'}),
    order=el('p',{class:'muted score-order','aria-live':'polite'});
  const sync=()=>{
    budget.bound({max:maximum.value()});
    const total=Object.values(weights).reduce((n,f)=>n+num(f),0);
    Object.values(weights)[0].input.setCustomValidity(total?'':'Give at least one part of the score a weight.');
    // F-C5: shares are said only while every weight is valid (a negative one makes them meaningless).
    const valid=Object.values(weights).every(({input:{validity:v}})=>!v.rangeUnderflow&&!v.rangeOverflow&&!v.badInput&&!v.stepMismatch);
    // Each part shows the share people read ("35%"): the weights are relative, any total is fine (L10). The warning line
    // speaks only when the weights cannot be used, and says why.
    for(const [key,share] of Object.entries(shares)){const pct=total?Math.round(num(weights[key])/total*100):0;share.textContent=valid&&total?pct+'%':'';}
    totalLine.textContent=!valid?`Each weight is a whole number from 0 to ${MAX_SCORE_INPUT}.`:total?'':'Give at least one part a weight.';
    totalLine.hidden=!totalLine.textContent;
    // The weights in plain words: what auto-schedule cares about most, in order (equal weights share a place).
    const ranked=WEIGHTS.map(([k,label])=>({label,n:num(weights[k])})).filter(x=>x.n>0).sort((a,b)=>b.n-a.n);
    order.textContent=ranked.length?'Matters most → least: '+ranked.map((x,i)=>(i&&ranked[i-1].n===x.n?'= ':'')+x.label).join(' › ').replace(/ › = /g,' = '):'';
    const v=Object.values(strengths).map(num),ordered=v.every((x,i)=>!i||x>=v[i-1]);
    strengths.critical.input.setCustomValidity(ordered?'':'Strengths must not decrease from low to critical.');
    prefMean.input.setCustomValidity(num(prefMean)+num(prefWorst)?'':'Give the mean or the worst preference a weight.');
    explain.textContent=`A critical advisory rule counts ${Math.round(num(strengths.critical)/Math.max(1,num(strengths.low)))}× a low one.`;
  };
  const read=():GenerationOptions=>({search_minutes:budget.value(),max_search_minutes:maximum.value(),...stops.read(),stop_by:null,fairness_plateau_seconds:fairStall.value(),shortlist_width:num(shortlist),diagnostic_seconds:diagnostics.value(),
    mode:mode.input.value,balance_measure:balance.input.value,overtime_penalty:num(overtime),
    allocation_order:fillOrder.input.value,fill_tier_time_share:num(fillShare),pair_limit_max_opportunities:num(pairBudget),
    // The loaded scoring first: settings this form does not show are kept as they are (F-C1).
    scoring:{...(initial.scoring||{}),weights:Object.fromEntries(WEIGHTS.map(([k])=>[k,num(weights[k])])),wish_budget_points:num(points),wish_period:period.input.value,strengths:Object.fromEntries(STRENGTHS.map(([k])=>[k,num(strengths[k])])),wish_debt_cap:num(cap),
      fairness_worst_off_emphasis:num(worstOff),desired_cover_factor:num(desired),preferences_mean_weight:num(prefMean),preferences_worst_weight:num(prefWorst),
      wish_priority_decay:num(decay),wish_granted_credit:num(credit),wish_history_periods:num(history)}} as GenerationOptions);
  const changed=()=>{sync();onChange(read());};
  for(const f of numbers)f.input.addEventListener('input',changed);
  for(const f of [mode,balance,period,fillOrder,stops.conflict])f.input.addEventListener('change',changed);
  // Puts a whole set of options into the fields (Reset, a preset).
  const set=(d:GenerationOptions)=>{
    const ds=(d.scoring||{}) as ScoringSettings,put=(f:{input:HTMLInputElement|HTMLSelectElement},v:unknown)=>{if(v!=null)f.input.value=String(v);};
    put(mode,d.mode);if(d.max_search_minutes!=null)maximum.set(d.max_search_minutes);if(d.search_minutes!=null)budget.set(d.search_minutes);put(balance,d.balance_measure);put(overtime,d.overtime_penalty);
    stops.set(d);fairStall.set(d.fairness_plateau_seconds??GENERATION_TIME_FIELDS.fairness_plateau_seconds.default);put(shortlist,d.shortlist_width??3);if(d.diagnostic_seconds!=null)diagnostics.set(d.diagnostic_seconds);
    put(fillOrder,d.allocation_order||'weighted');put(fillShare,d.fill_tier_time_share??fillMeta.default);put(pairBudget,d.pair_limit_max_opportunities??50000);
    for(const [key,f] of Object.entries(weights))put(f,(ds.weights as Record<string,number>|undefined)?.[key]);
    for(const [key,f] of Object.entries(strengths))put(f,(ds.strengths as Record<string,number>|undefined)?.[key]);
    put(points,ds.wish_budget_points);put(period,ds.wish_period||'month');put(cap,ds.wish_debt_cap??3);
    put(decay,ds.wish_priority_decay??WISH_PRIORITY_FIELDS.wish_priority_decay.default);put(credit,ds.wish_granted_credit??WISH_PRIORITY_FIELDS.wish_granted_credit.default);
    put(history,ds.wish_history_periods??WISH_PRIORITY_FIELDS.wish_history_periods.default);
    put(worstOff,ds.fairness_worst_off_emphasis??0.5);put(desired,ds.desired_cover_factor??0.25);put(prefMean,ds.preferences_mean_weight??7);put(prefWorst,ds.preferences_worst_weight??3);
    changed();
  };
  // Same-button confirm: the first click arms, the second puts every option back.
  const reset=defaults?armed(button(resetLabel,()=>set(defaults),'secondary small'),'Reset all?'):null;
  sync();
  // How it decides and the weights are the everyday settings; advisory strengths and search tuning are Advanced.
  const weightGroup=group('Score weights',[decides,...WEIGHTS.map(([k,label])=>`${label}: ${WEIGHT_HELP[k]}`)].join(' '),...Object.values(weights).map(f=>f.wrap),totalLine,order);
  weightGroup.classList.add('score-weights');
  const main=el('div',{},weightGroup,
    group('Workload','How workload is balanced and how much permitted overtime is avoided.',fillOrder.wrap,balance.wrap,overtime.wrap),
    group('Existing assignments','A run can still change it with Keep existing assignments. People marked Keep assigned, locked sessions and published work stay as they are.',mode.wrap));
  const wishes=group('Staff wishes','Points each person spends on wishes, and how much more a declined wish weighs next time.',points.wrap,period.wrap,cap.wrap,decay.wrap,credit.wrap,history.wrap);
  wishes.classList.add('score-aligned');  // SW-27: inputs on one line however their labels wrap
  const preferences=el('div',{},wishes);
  const advanced=el('div',{},
    group('Advisory rule strength','How much an advisory rule of each strength weighs. Mandatory rules always apply.',...Object.values(strengths).map(f=>f.wrap),explain),
    group('How score parts are measured','The schedule score and auto-schedule measure fairness, desired cover and preferences the same way.',worstOff.wrap,desired.wrap,prefMean.wrap,prefWorst.wrap),
    group('Search','How widely each run searches.',shortlist.wrap,fillShare.wrap,pairBudget.wrap,diagnostics.wrap));
  // How long runs search, together (`timing` takes the department's other run controls: Configuration's Add time step).
  const timing=el('div',{},group('Timing','How long each run searches, its ceiling, and when it stops early.',maximum.wrap,budget.wrap,stall.wrap,stops.gap.wrap,stops.score.wrap,fairStall.wrap,stops.conflict.wrap));
  const head=reset?el('div',{class:'auto-options-head'},reset):null,root=el('div',{class:'auto-options'},head,main,timing,preferences,advanced);
  // Configuration shows them as tabs: How it decides (with the weights), Timing, Staff preferences (with `extra`, the
  // kinds of wish), and Advanced for strengths and search tuning.
  let sections:HTMLElement|null=null;
  // `more`: further tabs after these (Animation).
  // `lead`: tabs before these (Score review); `open`: the tab the address asks for (?tab=score).
  const tabbed=(extra:Node[]=[],more:[string,string,()=>Node][]=[],lead:[string,string,()=>Node][]=[],open?:string)=>{preferences.append(...extra);
    if(open)shownTab=open;
    sections=tabs('Auto-schedule settings',[...lead,['decides','How it decides',()=>main],['timing','Timing',()=>timing],['preferences','Staff preferences',()=>preferences],['advanced','Advanced',()=>advanced],...more],shownTab,k=>{shownTab=k;});
    // The tabs and Reset share one row (Reset applies to both tabs).
    sections.prepend(el('div',{class:'auto-options-head'},sections.querySelector('[role=tablist]')!,reset||''));return el('div',{class:'auto-options'},sections);};
  // A wrong number on a tab not shown: open its tab so the browser can point at it.
  const valid=()=>{const bad=numbers.find(f=>!f.input.checkValidity())?.input||preferences.querySelector<HTMLInputElement>('input:invalid');
    if(bad&&!bad.isConnected)sections?.querySelector<HTMLElement>(`[role=tab][id$="-${preferences.contains(bad)?'preferences':advanced.contains(bad)?'advanced':timing.contains(bad)?'timing':'decides'}-tab"]`)?.click();
    return !bad||bad.reportValidity();};
  return {root,tabbed,read,set,valid,timing};
}

/* ---- Auto-schedule live animation: the one adapter between the contract and the client ----
 * Settings: department `autoschedule_animation` (Configuration › Auto-schedule › Animation); defaults, bounds and
 * choices come from the contract (generated.ts). Events: the solver's better schedules in `job.progress.events`.
 * Nothing else in the client reads either shape. */
export type AnimationSettings=Required<Omit<AutoscheduleAnimationSettings,'big_move'>>&{big_move:Required<AutoscheduleBigMoveSettings>};
export type AnimationMode=AnimationSettings['mode'];
const ANIMATION_DEFAULTS=AUTOSCHEDULE_ANIMATION_DEFAULTS as AnimationSettings;
/** The department's animation settings (the server sends them complete; the contract's defaults fill an older reply). */
export function animationSettings(department:Row|undefined):AnimationSettings{
  const saved=(department?.autoschedule_animation||{}) as Partial<AnimationSettings>;
  return {...ANIMATION_DEFAULTS,...saved,big_move:{...ANIMATION_DEFAULTS.big_move,...saved.big_move}};
}
export type LivePair={shift:string;person:string};
export type LiveEvent={seq:number;adds:LivePair[];removes:LivePair[];truncated:boolean;open_places:number|null;warnings:number|null};
/** A running job as the live view reads it: the step (1–5), the search clock and the solver's events. */
export type LiveProgress={step:number;limit:number;searched:number;searching:boolean;improvements:number|null;events:LiveEvent[]};
const pairs=(list:AutoscheduleLivePair[]|undefined):LivePair[]=>(list||[]).map(x=>({shift:x.shift_id,person:x.person_id}));
/** Search budget and time used: `job.search_control` (Add 5 min) wins over the run's options and the worker's limit. */
export function searchClock(job:Row){
  const p:Row=job.progress||{};
  const limit=Number(job.search_control?.minutes||job.effective_options?.search_minutes)*60||Number(p.time_limit_seconds)||0;
  const searched=Math.max(0,Number(p.search_elapsed_seconds??(p.search_started_seconds==null?0:Number(p.elapsed_seconds||0)-Number(p.search_started_seconds))));
  return {limit,searched};
}
/** The latest complete solution a job's progress carries (`progress.solution`), for catching up after missed events. */
export function liveSolution(job:Row):{seq:number;pairs:LivePair[]}|null{
  const s=(job.progress as AutoscheduleProgress|undefined)?.solution;return s?{seq:s.seq,pairs:pairs(s.pairs)}:null;
}
/** A step awaited for a view that may have gone meanwhile (another rota, a redraw): its value while `fresh()` still
 *  holds; otherwise it never settles, so nothing after it runs for the view that is gone. */
export const whileFresh=(fresh:()=>boolean)=><T>(step:Promise<T>):Promise<T>=>step.then(v=>fresh()?v:new Promise<T>(()=>{}));
/** A run's stage (job.progress.stage) in the contract's words (GENERATION_PROGRESS_STAGES, the one source of labels). */
export const stageLabel=(stage:string)=>(GENERATION_PROGRESS_STAGES as Record<string,string>)[stage]||stage;
/** The run's steps in order, each with the stages it covers; the footer reads "n/5 <label>" (Done is the fifth). */
const RUN_STEPS:[string,string[]][]=[['checking',['queued','checking']],['preparing',['preparing']],['searching',['finding','improving','searching','explaining']],
  ['scoring',['scoring','validating','summarizing','ready']]];
export const LIVE_STEPS=[...RUN_STEPS.map(([key])=>stageLabel(key)),'Done'];
/** 1–4 for a stage (an unknown or absent stage is the first step, as before stages were reported). */
const stepOf=(stage:string)=>1+Math.max(0,RUN_STEPS.findIndex(([,stages])=>stages.includes(stage)));
export function liveProgress(job:Row):LiveProgress{
  const p:Row=job.progress||{},stage=String(p.stage||'checking'),done=!['queued','running'].includes(job.status);
  const step=done?5:stepOf(stage);
  const events=((p.events||[]) as AutoscheduleLiveEvent[]).filter(e=>(e.kind??'improvement')==='improvement').map(e=>({seq:e.seq,adds:pairs(e.adds),removes:pairs(e.removes),
    truncated:e.truncated,open_places:e.open_places,warnings:e.warnings}));
  return {...searchClock(job),step,searching:step===3,improvements:p.improvements_found==null?null:Number(p.improvements_found),events};
}

/** Configuration › Auto-schedule › Animation: how a run shows its changes, with a preview that plays the values typed
 *  (before Save). Labels only (owner rule); limits from the contract; the preview loads the live view module. */
export function animationFields(initial:AnimationSettings,onChange:(value:AnimationSettings)=>void){
  // The contract's choices, each named by its value ("hybrid" → "Hybrid").
  const L=AUTOSCHEDULE_ANIMATION_LIMITS,choices=(list:readonly string[])=>list.map(value=>({value,label:value[0].toUpperCase()+value.slice(1)})),C=AUTOSCHEDULE_ANIMATION_CHOICES;
  const num=(label:string,value:number,[min,max]:readonly [number,number],step:number|string=1)=>field(label,'number',value,{required:true,min,max,step});
  const mode=select('Style',choices(C.mode),initial.mode);
  const big=select('Big move style',choices(C.big_style),initial.big_style);
  const small=select('Small change style',choices(C.small_style),initial.small_style);
  const rows=num('Rows for a big move',initial.big_move.min_row_distance,L.min_row_distance);
  const swaps=check('Swaps',initial.big_move.swaps),fills=check('Fills from Open places',initial.big_move.fills_from_open),cross=check('Moves between sessions',initial.big_move.cross_session);
  const flights=num('Flights at once',initial.max_concurrent_flights,L.max_concurrent_flights),rate=num('Updates per second',initial.max_updates_per_second,L.max_updates_per_second);
  const speed=num('Duration (×)',initial.speed,L.speed,0.1);
  const step=select('Footer step change',choices(C.step_change),initial.step_change);
  const value=():AnimationSettings=>({mode:mode.input.value as AnimationMode,big_move:{min_row_distance:Number(rows.input.value),swaps:swaps.input.checked,fills_from_open:fills.input.checked,cross_session:cross.input.checked},
    big_style:big.input.value as AnimationSettings['big_style'],small_style:small.input.value as AnimationSettings['small_style'],max_concurrent_flights:Number(flights.input.value),
    max_updates_per_second:Number(rate.input.value),step_change:step.input.value as AnimationSettings['step_change'],speed:Number(speed.input.value)});
  const hybrid=group('Big moves','',rows.wrap,swaps.wrap,fills.wrap,cross.wrap,big.wrap,small.wrap);
  const numbers=[rows,flights,rate,speed];
  // The preview plays what is typed while it is valid; the saved settings otherwise.
  const shown=()=>numbers.every(f=>f.input.checkValidity())?value():initial;
  const stage=el('div',{class:'asl-preview-stage'});let replay:(()=>void)|null=null,wait=0;
  const play=()=>{if(replay)replay();else void import('./autoschedule_live.js').then(m=>{replay=m.preview(stage,shown);});};
  const sync=()=>{hybrid.disabled=mode.input.value!=='hybrid';};
  const changed=()=>{sync();onChange(value());window.clearTimeout(wait);wait=window.setTimeout(play,300);};
  for(const f of [mode,big,small,step])f.input.addEventListener('change',changed);
  for(const f of [swaps,fills,cross])f.input.addEventListener('change',changed);
  for(const f of numbers)f.input.addEventListener('input',changed);
  sync();
  const root=el('div',{class:'auto-options'},group('Motion','',mode.wrap,step.wrap),hybrid,group('Pace','',flights.wrap,rate.wrap,speed.wrap),
    el('section',{class:'asl-preview','aria-label':'Preview'},el('div',{class:'asl-preview-head'},el('strong',{},'Preview'),button('Play preview',play,'secondary small')),stage));
  // It plays once when first shown (its tab is drawn when opened), then after each change and on Play preview.
  const seen=new IntersectionObserver(e=>{if(e.some(x=>x.isIntersecting)){seen.disconnect();play();}});seen.observe(stage);
  const valid=()=>{const bad=numbers.find(f=>!f.input.checkValidity());return !bad||bad.input.reportValidity();};
  return {root,value,valid,play};
}

// Auto-schedule progress: the worker reports its stage every few seconds (job.progress). One changing line ("3/5 Searching ·
// 40% · 2 min 14 s used") over one determinate bar reviewed. Only the step is a live region, so
// screen readers hear a stage change, not a ticking clock.
const AIMS:Record<string,string>={targets:'staffing targets',coverage:'staffing cover',rules:'advisory rules and targets',preferences:'preferences',soft:'preferences and a fair spread',changes:'fewer changes',balance:'a fair spread of work',feasibility:'a valid schedule'};
export function generationPhaseTimings(timings:Row,format=duration):string[]{
  const phases:Record<string,string>={preparation_seconds:'Preparing assignments',search_seconds:'Searching',diagnostic_seconds:'Explaining conflicts',validation_seconds:'Final checks',summary_seconds:'Preparing the result'};
  return Object.entries(phases).filter(([key])=>timings[key]!=null).map(([key,label])=>`${label} ${format(Number(timings[key]))}`);
}

/** What this page has seen of a run's search (job.progress is a snapshot, not a history): the best score at each read
 *  where it changed, and the search clock when the count of better schedules last rose. One run at a time.
 *  shortcut: only observations made by this page are shown; add server-side history to show a complete run. */
let trail={id:'',scores:[] as number[],count:0,at:null as number|null};
const TRAIL_POINTS=120;
function searchTrail(job:Row,searched:number){
  const p:Row=job.progress||{},id=String(job.id||''),count=Number(p.improvements_found||0),best=Number(p.best_score?.total);
  if(trail.id!==id)trail={id,scores:[],count:0,at:null};
  if(count>trail.count){trail.count=count;trail.at=searched;}
  const before=trail.scores[trail.scores.length-1];
  if(Number.isFinite(best)&&p.best_score?.total!=null&&before!==best){trail.scores.push(best);
    if(trail.scores.length>TRAIL_POINTS)trail.scores=trail.scores.filter((_,i)=>i%2===0||i===trail.scores.length-1);}  // bounded: halve, keep the latest
  return {...trail,best:Number.isFinite(best)&&p.best_score?.total!=null?best:null,rose:before!=null&&best>before};
}
/** The search as it goes (stage 3, D76 run card): the best score so far, big, with its line; when the last better
 *  schedule came, or that it is levelling off once nothing better came within the run's own stop window; then three
 *  numbers: open places in the latest schedule, better schedules found, and the stop window. Only facts the job reports
 *  (`defaults`: the department's options for a run that has none of its own); nothing moves on its own, so reduced motion
 *  needs nothing more. */
function searchLine(job:Row,searched:number,searching:boolean,defaults?:GenerationOptions):HTMLElement|string{
  const t=searchTrail(job,searched);if(!t.count)return '';
  const stall=Number((job.effective_options||defaults)?.stop_without_improvement_seconds)||0,quiet=searching&&t.at!=null&&stall>0&&searched-t.at>=stall;
  const round=(n:number)=>Math.round(n*10)/10,lo=Math.floor(Math.min(...t.scores))-1,hi=Math.ceil(Math.max(...t.scores))+1;
  const events=(job.progress?.events||[]) as Row[],open=events.length?events[events.length-1].open_places:null;
  const first=job.progress?.first_solution_seconds!=null?Number(job.progress.first_solution_seconds):null;
  const fact=(label:string,value:string)=>el('div',{},el('dt',{},label),el('dd',{},value));
  return el('div',{class:'auto-search'},
    // A better score rises in once (CSS, off with reduced motion); the number is the job's, never animated between.
    el('div',{class:'auto-best-row'},t.best!=null?el('div',{class:'auto-best'},el('b',{class:'auto-best-num'+(t.rose?' num-moved':'')},String(round(t.best))),el('small',{},'best score so far')):'',
      t.scores.length>1?sparkline(t.scores,`Best score so far: from ${round(t.scores[0])} to ${round(t.scores[t.scores.length-1])}`,lo,hi):''),
    t.at!=null||first!=null?el('p',{class:'muted auto-last'},[first!=null?`First schedule after ${duration(first)}`:'',
      t.at==null?'':quiet?`levelling off, none better since ${duration(t.at)}`:`last better at ${duration(t.at)}`].filter(Boolean).join(' · ').replace(/^./,c=>c.toUpperCase())):'',
    el('dl',{class:'auto-facts'},...(open!=null?[fact('Open places',String(open))]:[]),fact('Better schedules',String(t.count)),...(stall?[fact('Stops after no gain',duration(stall))]:[])));
}
/** `announce`: the stage line is the live region; off where the footer's run strip already announces each step. */
/** `defaults`: the department's run options, for a run that used them (its job carries no effective_options). */
export function generationProgress(job:Row,elapsed:number,diagnostic=false,announce=true,defaults?:GenerationOptions):HTMLElement{
  const p:Row=job.progress||{},queued=job.status==='queued';
  const stage=queued?'queued':String(p.stage||'checking'),step=stepOf(stage)-1,search=RUN_STEPS[2][1].includes(stage)&&!GENERATION_SEARCH_FINISHED_STAGES.includes(stage);
  const {limit,searched}=searchClock(job);
  const share=limit?Math.min(1,searched/limit):0;
  const value=queued?0:stage==='checking'?2:stage==='preparing'?2+13*(p.total_sessions?Number(p.prepared_sessions)/Number(p.total_sessions):0)
    :search?15+75*share:stage==='scoring'||stage==='explaining'?92:stage==='validating'?95:100;
  const names:string[]=p.objective_names||[],aim=AIMS[names[Number(p.tier)]]||'';
  const label=queued?'Waiting for the scheduler to start':stage==='finding'&&diagnostic?'Filling what can be filled':stage==='improving'&&aim?`Improving ${aim}`
    :stage in GENERATION_PROGRESS_STAGES?stageLabel(stage):LIVE_STEPS[step];
  // While it searches: the search time used and what is left of the run's allowance, said as a ceiling ("up to"): a
  // plateau may end it sooner (A4: never a forecast). Before the search: the time the run has been going; after it, the
  // search time it used.
  // The search clock is the one the footer's run strip shows (ME-02: one measure, one number).
  const going=Number(p.elapsed_seconds)||elapsed;
  const before=!searched&&['checking','preparing'].includes(stage);
  const pct=Math.round(value),when=queued?'':before?duration(going):`${duration(searched)} used${search&&limit?` · up to ${duration(Math.max(0,limit-searched))} left`:''}`;
  const line=queued?label:`${step+1}/${LIVE_STEPS.length} ${label}`;
  return el('div',{class:'auto-progress'},
    el('p',{class:'auto-stage',tabindex:'-1'},el('strong',announce?{role:'status'}:{},line),queued?'':el('span',{class:'muted auto-time'},` · ${pct}%${when?' · '+when:''}`)),
    el('progress',{max:'100',value:String(pct),'aria-label':'Auto-schedule progress','aria-valuetext':`${line}, ${pct}%${when?', '+when:''}`}),
    queued?'':searchLine(job,searched,search,defaults));
}
/** One window listener per kind for the scheduling panel: each redrawn panel's listener replaces the previous one's, so
 *  a panel taken off the page is not kept alive by an idle stream, like the visibility listener. */
export function ownedListener(type:string){let held:EventListener|null=null;
  return (f:EventListener)=>{if(held)window.removeEventListener(type,held);held=f;window.addEventListener(type,f);};}
/** Finishes the run shown live once it has ended. Its id stays in `state.live` until the finish succeeds, so a panel
 *  redrawn while the finishing read is pending (Week ↔ People) finishes it instead. */
export function liveFinisher(state:{live:string},finish:(id:string)=>Promise<unknown>){let busy='';
  return ()=>{const id=state.live;if(!id||busy===id)return;busy=id;
    void finish(id).then(()=>{if(state.live===id)state.live='';},()=>{/* kept: the next read tries again */}).finally(()=>{busy='';});};}
