/** The Schedule's View settings popover (Notion-style): the view's name, Layout and Range, then one row per setting that
 *  opens its own page (Properties › Person label, Days, Colour by, Layers), Density, and Duplicate /
 *  Reset at the foot. Group, Sort and Filter live on the toolbar only (S-10: never twice). The page owns the state; this only draws it and reports choices. */
import {el,icon,button,withIcon,iconButton,select,field,check,colourOf,WEEKDAYS,type IconName,type Choice} from './dom.js';
import {LABEL_PARTS,LABEL_PART_DEFINITIONS,LABEL_SEPARATORS,LABEL_HONORIFIC_MODES,PLANNER_TEAM_ROWS_LIMIT,type PersonLabel,type ViewProperty} from './generated.js';
import {ROW_FACTS} from './planner_week.js';
import {PROPERTY_NAMES,PROPERTY_FORMS,PROPERTY_EXAMPLES,FORM_NAMES,ITEM_DRAWS} from './schedule_item.js';
import {LABEL_NAMES,type LabelPart} from './person_label.js';

type Options=[string,string][];
export interface ViewSettingsSpec{
  /** The active saved view's name (renamed in place), or null for the default view. */
  name:string|null;rename:(name:string)=>void;
  /** Read when drawn: a choice redraws the page while this popover stays open. */
  layout:()=>string;layouts:Options;layoutHint:(layout:string)=>string;setLayout:(key:string)=>void;
  range:()=>string;ranges:Options;setRange:(key:string)=>void;openYear?:()=>void;
  /** The Calendar layout's axes: what each row is (duties, people or any dimension), the columns (days, weeks,
   *  weekdays, months or a dimension) and what a cell shows. Choices that do not apply are listed disabled, with why. */
  rows?:{value:()=>string;options:()=>Choice[];set:(rows:string)=>void;cells:()=>string;cellOptions:()=>Options;setCells:(cells:string)=>void;
    columns:()=>string;columnOptions:()=>Choice[];setColumns:(columns:string)=>void};
  /** Coverage's Rows and Columns (places filled / needed by any session dimension and date axis). */
  coverage?:{rows:()=>string;rowOptions:()=>Choice[];setRows:(rows:string)=>void;columns:()=>string;columnOptions:()=>Choice[];setColumns:(columns:string)=>void};
  /** A Custom range's length in days. */
  custom:{value:()=>number;set:(days:number)=>void};
  /** Days & time: weekdays shown (Monday 0 … Sunday 6, null = all), the view's week start (null = the department's,
   *  `departmentStart`), and the hours Day shows (null = the whole day). */
  days:{weekdays:()=>number[]|null;setWeekdays:(days:number[]|null)=>void;weekStart:()=>number|null;departmentStart:number;setWeekStart:(day:number|null)=>void;
    window:()=>{start:string;end:string}|null;setWindow:(window:{start:string;end:string}|null)=>void};
  /** `tags`: Colour by tag's ordered choice (stable tag ids; the first one a person holds colours them, none is neutral). */
  colour?:{options:Options;value:()=>string;set:(key:string)=>void;
    tags?:{options:()=>{id:string;name?:string;colour?:string;active?:boolean}[];value:()=>string[];set:(ids:string[])=>void;limit:number}};
  density?:{value:()=>string;set:(key:string)=>void};
  /** Trainee rows a supervision team shows before "+N more" (PlannerViewSettings.team_rows). */
  teamRows?:{value:()=>number;set:(rows:number)=>void};
  /** Staffing numbers (places filled / needed under each day, group totals, a grid's Staffed / needed row): shown or
   *  hidden for this view (PlannerViewSettings.staffing) and, in display mode, for this export. */
  staffing?:{value:()=>boolean;set:(on:boolean)=>void};
  /** `preview`: a real person named with this label; `part`: what one part shows for them. */
  label:{value:()=>PersonLabel;set:(label:PersonLabel)=>void;preview:(label:PersonLabel)=>string;part:(part:LabelPart,label:PersonLabel)=>string};
  /** What each session card shows, in order, and how (the calendar honours it). */
  properties:{value:()=>ViewProperty[];set:(props:ViewProperty[])=>void;
    /** Whether this layout and account can draw a property (person tags need the tag lists); others keep their saved place. */supports?:(key:ViewProperty['key'])=>boolean;
    /** What the layout's items draw (schedule_item.ts ITEM_DRAWS): offered only where drawn. */drawn?:(key:ViewProperty['key'])=>boolean};
  /** Your own duties, read-only (S-12): each chip is labelled by its time and place, so Time, Location, Service and Hours
   *  only add row details there, and the copy says so. */
  personal?:boolean;
  layers:{hint:()=>string;build:(body:HTMLElement)=>void};
  /** List: its table's own column settings (shown, order, wrap), saved with the view (app.ts plannerDefinition). */
  columns?:{build:(body:HTMLElement,changed:()=>void)=>void};
  /** 'none': no saved view is active; 'dirty': it has unsaved changes. */
  saved:()=>'none'|'clean'|'dirty';saveAs:()=>void;update:()=>Promise<void>;duplicate?:()=>void;reset:()=>void;
  /** Refresh the toolbar buttons after a choice. */
  sync:()=>void;
  /** Print or export: only what a page shows (Rows and columns, Properties, Colour by, List columns, Density), for that export alone;
   *  `reset` returns to the chosen view's own settings. Nothing here is saved. */
  display?:boolean;
}
/** A labelled single-choice row of buttons that marks the choice in place (the popover stays open across the redraw).
 *  Each choice redraws the schedule, so arrow keys only move focus; Enter or Space chooses. */
export function segment(label:string,options:Options,value:string,pick:(key:string)=>void,after?:()=>void):HTMLElement[]{
  const g=el('div',{class:'segmented dt-layouts',role:'radiogroup',activation:'manual','aria-label':label},...options.map(([k,t])=>{
    const b=el('button',{type:'button',role:'radio','aria-checked':String(k===value)},t);
    b.addEventListener('click',()=>{if(b.getAttribute('aria-checked')==='true')return;g.querySelectorAll('[role=radio]').forEach(n=>n.setAttribute('aria-checked',String(n===b)));pick(k);after?.();});return b;}));
  return [el('div',{class:'dt-pop-label'},label),g];
}
const summary=(l:PersonLabel)=>l.parts.map(p=>LABEL_NAMES[p]).join(', ');
const nameOf=(options:Options|{key:string;label:string}[],key:string)=>(options as {key?:string;label?:string}[]).map(o=>Array.isArray(o)?{key:o[0],label:o[1]}:o).find(o=>o.key===key)?.label||'';

/** Rows that can be shown or hidden and put in order: a drag handle (Alt+↑/↓ on it), the name with an example, an
 *  optional control (a property's form) and the eye. Person label parts and card properties use it. */
export function orderRows<K extends string>(b:HTMLElement,order:K[],o:{on:(k:K)=>boolean;name:(k:K)=>string;example:(k:K)=>string;locked:(k:K)=>boolean;
  move:(k:K,to:number)=>void;toggle:(k:K)=>void;control?:(k:K)=>HTMLElement|null;dot?:(k:K)=>string|null}){
  for(const [i,k] of order.entries()){
    const on=o.on(k),label=o.name(k);
    const grip=el('button',{type:'button',class:'icon-button bare grip','aria-label':`Move ${label}`,'data-tip':'Drag, or Alt+↑/↓',draggable:'true','aria-keyshortcuts':'Alt+ArrowUp Alt+ArrowDown'},icon('menu'));
    grip.addEventListener('keydown',e=>{if(e.altKey&&(e.key==='ArrowUp'||e.key==='ArrowDown')){e.preventDefault();o.move(k,i+(e.key==='ArrowUp'?-1:1));}});
    grip.addEventListener('dragstart',e=>{e.dataTransfer?.setData('text/plain',k);});
    const eye=el('button',{type:'button',class:'icon-button bare vs-eye','aria-pressed':String(on),'aria-label':(on?'Hide ':'Show ')+label},icon(on?'eye':'eyeOff')) as HTMLButtonElement;
    eye.disabled=o.locked(k);
    eye.addEventListener('click',()=>o.toggle(k));
    const hue=o.dot?.(k),item=el('div',{class:'vs-prop'+(on?'':' off'),'data-part':k},grip,el('span',{},hue?el('span',{},el('span',{class:`person-dot duty-${hue}`,'aria-hidden':'true'}),label):label,el('small',{},o.example(k)||'—')),o.control?.(k)||'',eye);
    item.addEventListener('dragover',e=>e.preventDefault());
    item.addEventListener('drop',e=>{e.preventDefault();const from=e.dataTransfer?.getData('text/plain') as K;if(from&&from!==k&&order.includes(from))o.move(from,i);});
    b.append(item);
  }
}
const moved=<K,>(order:K[],k:K,to:number)=>{const list=order.filter(x=>x!==k);list.splice(Math.max(0,Math.min(to,list.length)),0,k);return list;};
/** The Person label editor reviewed, one component for a view's Person label page and the department's
 *  default (Configuration › Names & codes): a preview, the name format (the contract's name parts), what is added before
 *  or after the name (its tokens and the title, in order), and the separator. `set` redraws; `focus` names what to focus. */
export function labelBody(b:HTMLElement,o:{value:()=>PersonLabel;set:(label:PersonLabel,focus:string)=>void;part:(part:LabelPart,label:PersonLabel)=>string;preview:(label:PersonLabel)=>string}){
  const now=o.value(),defs=LABEL_PART_DEFINITIONS as Record<LabelPart,{label:string;role:string}>,isName=(p:LabelPart)=>defs[p].role==='name';
  const name=now.parts.find(isName),at=name?now.parts.indexOf(name):-1;
  const before=now.parts.filter((p,i)=>!isName(p)&&i<at),after=now.parts.filter((p,i)=>!isName(p)&&i>at);
  const compose=(bef:LabelPart[],nm:LabelPart|undefined,aft:LabelPart[])=>[...bef,...(nm?[nm]:[]),...aft];
  const set=(next:Partial<PersonLabel>,focus='')=>o.set({...now,...next},focus);
  b.append(el('div',{class:'vs-preview','aria-label':'Preview'},el('small',{},'Preview'),el('span',{class:'pchip duty-teal'},el('span',{class:'pchip-name'},o.preview(now)))));
  // A label shows at least one part: "No name" only while something else is added.
  const names:[string,string][]=[...LABEL_PARTS.filter(isName).map(p=>[p,defs[p].label] as [string,string]),...(before.length+after.length?[['','No name'] as [string,string]]:[])];
  b.append(...segment('Name',names,name||'',k=>set({parts:compose(before,(k||undefined) as LabelPart|undefined,after)},'[role=radiogroup][aria-label="Name"] [aria-checked=true]')));
  const tokens=[...before,...after],rows:string[]=[...(now.honorific==='none'?[]:['title']),...tokens];
  if(rows.length)b.append(el('div',{class:'dt-pop-label'},'Added to the name'));
  orderRows(b,rows,{on:()=>true,name:k=>k==='title'?'Title':defs[k as LabelPart].label,
    example:k=>k==='title'?LABEL_HONORIFIC_MODES.dr:o.part(k as LabelPart,now),locked:k=>k!=='title'&&now.parts.length===1,
    move:(k,to)=>{if(k==='title')return;const list=moved(tokens,k as LabelPart,to-(rows[0]==='title'?1:0));
      set({parts:compose(list.filter(x=>before.includes(x)),name,list.filter(x=>after.includes(x)))},`[data-part="${k}"] .grip`);},
    toggle:k=>k==='title'?set({honorific:'none'}):set({parts:now.parts.filter(x=>x!==k)}),
    control:k=>{if(k==='title'||!name)return null;const part=k as LabelPart,where=before.includes(part)?'before':'after';
      const f=select(`Place ${defs[part].label}`,[{value:'before',label:'Before'},{value:'after',label:'After'}],where);f.input.classList.add('vs-as');f.wrap.classList.add('vs-as-field');
      f.input.addEventListener('change',()=>{
        set({parts:f.input.value==='before'?compose([...before.filter(x=>x!==part),part],name,after.filter(x=>x!==part)):compose(before.filter(x=>x!==part),name,[...after.filter(x=>x!==part),part])},`[data-part="${k}"] select`);});return f.wrap;}});
  const hidden=[...(now.honorific==='none'?[{value:'title',label:'Title'}]:[]),...LABEL_PARTS.filter(p=>!isName(p)&&!now.parts.includes(p)).map(p=>({value:p,label:defs[p].label}))];
  if(hidden.length){const add=select('Add to the name',[{value:'',label:'Add to the name'},...hidden],'');
    add.input.addEventListener('change',()=>{const k=add.input.value;if(!k)return;
      if(k==='title')set({honorific:'dr'},'[data-part="title"] .vs-eye');else set({parts:compose(before,name,[...after,k as LabelPart])},`[data-part="${k}"] .vs-eye`);});
    b.append(el('div',{class:'vs-fields'},add.wrap));}
  const sep=select('Separator',Object.entries(LABEL_SEPARATORS).map(([value,d])=>({value,label:d.label})),now.separator||'dot');
  sep.input.addEventListener('change',()=>set({separator:sep.input.value as PersonLabel['separator']},'select[name=Separator]'));
  b.append(el('div',{class:'vs-fields'},sep.wrap));
}
export function viewSettings(spec:ViewSettingsSpec,panel:HTMLElement,close:()=>void){
  panel.classList.add('vs-pop');
  const row=(label:string,glyph:IconName,hint:string,open:()=>void)=>{
    const b=el('button',{type:'button',class:'vs-row'},icon(glyph),el('span',{class:'vs-row-name'},label),el('span',{class:'hint truncate'},hint),icon('chevronRight'));
    b.addEventListener('click',()=>open());return b;};
  const foot=el('div',{class:'vs-foot'});
  const drawFoot=()=>{const state=spec.saved();
    if(spec.display){foot.replaceChildren(withIcon(button('Reset to the view',()=>{spec.reset();changed();main();},'ghost small'),'reset'),el('span',{class:'grow'}),el('span',{class:'hint'},'This export only'));return;}
    foot.replaceChildren(
    ...(spec.duplicate?[withIcon(button('Duplicate view',()=>{close();spec.duplicate!();},'ghost small'),'copy')]:[]),
    withIcon(button('Reset to default',()=>{spec.reset();changed();main();},'ghost small'),'reset'),el('span',{class:'grow'}),
    state==='none'?button('Save as view…',()=>{close();spec.saveAs();},'ghost small'):state==='dirty'?button('Update view',async()=>{await spec.update();drawFoot();},'secondary small'):el('span',{class:'hint'},'Saved to this view'));};
  const changed=()=>{spec.sync();drawFoot();};
  /** Redraws the settings after a choice that adds or removes rows, keeping focus on the chosen option. */
  const again=(group:string)=>{main();changed();panel.querySelector<HTMLElement>(`[role=radiogroup][aria-label="${group}"] [aria-checked=true],select[name="${group}"]`)?.focus();};
  /** A choice from a longer list: the shared dropdown (dom.ts select, opened by the picker). */
  const choice=(label:string,choices:Choice[],value:string,pick:(key:string)=>void)=>{const f=select(label,choices,value);
    f.input.addEventListener('change',()=>{pick(f.input.value);again(label);});return f.wrap;};
  /** A setting's own page; Back returns to where it was opened from, focus starts on `focus` (or Back). */
  function page(title:string,build:(body:HTMLElement)=>void,back:()=>void=main,focus=''){
    const body=el('div',{class:'vs-sec'});build(body);
    const b=iconButton(back===main?'Back to view settings':'Back to properties','chevronLeft',()=>back(),{class:'bare'});
    panel.replaceChildren(el('div',{class:'vs-back'},b,el('strong',{},title)),body,foot);drawFoot();
    ((focus&&body.querySelector<HTMLElement>(focus))||b).focus();
  }
  /** The Calendar layout's Rows, Columns and Cells show (also in Print or export, for that export alone). */
  const axes=()=>spec.coverage&&spec.layout()==='coverage'?[el('div',{class:'vs-fields vs-axes'},choice('Rows',spec.coverage.rowOptions(),spec.coverage.rows(),k=>spec.coverage!.setRows(k)),
      choice('Columns',spec.coverage.columnOptions(),spec.coverage.columns(),k=>spec.coverage!.setColumns(k)))]
    :spec.rows&&spec.layout()==='calendar'?[el('div',{class:'vs-fields vs-axes'},choice('Rows',spec.rows.options(),spec.rows.value(),k=>spec.rows!.set(k)),
      choice('Columns',spec.rows.columnOptions(),spec.rows.columns(),k=>spec.rows!.setColumns(k))),
    // A single possibility (duty rows over weeks show counts) is not offered as a choice.
    ...(spec.rows.cellOptions().length>1?segment('Cells show',spec.rows.cellOptions(),spec.rows.cells(),k=>{spec.rows!.setCells(k);again('Cells show');}):[])]:[];
  /** The view's team rows, kept within the contract's bound (an invalid entry goes back to the saved value). */
  function teamRowsField(){
    if(!spec.teamRows)return null;
    const n=field('Trainees per team','number',spec.teamRows.value(),{min:1,max:PLANNER_TEAM_ROWS_LIMIT,required:true});n.wrap.classList.add('vs-custom');
    n.input.addEventListener('change',()=>{const v=Math.round(Number(n.input.value));if(v>=1&&v<=PLANNER_TEAM_ROWS_LIMIT){spec.teamRows!.set(v);changed();}else n.input.value=String(spec.teamRows!.value());});
    return n.wrap;
  }
  function staffingField(){
    if(!spec.staffing)return null;
    const c=check('Staffing numbers',spec.staffing.value());c.input.addEventListener('change',()=>{spec.staffing!.set(c.input.checked);changed();});
    return el('div',{class:'vs-inline'},c.wrap);
  }
  /** What this layout's items draw and this reader may see: the one set the Properties page lists and its summary names
   *  (a property the layout does not draw is never named, e.g. Rule marks on Coverage). */
  const offered=(k:ViewProperty['key'])=>(spec.properties.drawn?.(k)??ITEM_DRAWS.card.has(k))&&(spec.properties.supports?.(k)??true);
  function main(){
    if(spec.display){
      panel.replaceChildren(el('div',{class:'vs-sec'},...axes(),
        row('Properties','eye',[summary(spec.label.value()),...spec.properties.value().filter(p=>p.on&&p.key!=='label'&&offered(p.key)).map(p=>PROPERTY_NAMES[p.key].replace(/ \(.*\)$/,''))].join(', '),properties),
        spec.colour?row('Colour by','palette',colourHint(),()=>colourPage()):null,
        spec.columns?row('Columns','eye','Shown, order and wrap',()=>page('Columns',b=>spec.columns!.build(b,changed))):null,
        ...(spec.density?segment('Density',[['compact','Compact'],['comfortable','Comfortable']],spec.density.value(),k=>spec.density!.set(k),changed):[]),teamRowsField(),staffingField()),foot);
      drawFoot();return;
    }
    const name=spec.name===null?el('strong',{class:'vs-title'},'Default view'):(()=>{
      const input=el('input',{type:'text',value:spec.name,'aria-label':'View name',maxlength:160,required:true}) as HTMLInputElement;
      input.addEventListener('change',()=>{if(input.value.trim())spec.rename(input.value);});return input;})();
    const range=spec.range(),words=spec.layoutHint(spec.layout()),hint=words?el('p',{class:'hint vs-hint'},words):'';
    panel.replaceChildren(el('div',{class:'vs-name'},icon('grid'),name),
      el('div',{class:'vs-sec'},...segment('Layout',spec.layouts,spec.layout(),k=>{spec.setLayout(k);again('Layout');}),hint,
        ...axes(),
        ...segment('Range',spec.ranges,range,k=>{spec.setRange(k);if([k,range].includes('custom'))again('Range');else changed();}),  // Custom adds its length
        ...(range==='custom'?[(()=>{const n=field('Days in the range','number',spec.custom.value(),{min:1,max:62,required:true});n.wrap.classList.add('vs-custom');
          n.input.addEventListener('change',()=>{const v=Math.round(Number(n.input.value));if(v>=1&&v<=62){spec.custom.set(v);changed();}else n.input.value=String(spec.custom.value());});return n.wrap;})()]:[]),
        ...(spec.openYear?[(()=>{const y=el('button',{type:'button',class:'link-button vs-link'},'Open the year overview');y.addEventListener('click',()=>{close();spec.openYear!();});return y;})()]:[])),
      el('div',{class:'vs-sec'},
        row('Properties','eye',[summary(spec.label.value()),...spec.properties.value().filter(p=>p.on&&p.key!=='label'&&offered(p.key)).map(p=>PROPERTY_NAMES[p.key].replace(/ \(.*\)$/,''))].join(', '),properties),
        row('Days','calendar',daysHint(),daysPage),
        spec.colour?row('Colour by','palette',colourHint(),()=>colourPage()):null,
        row('Layers','layers',spec.layers.hint(),()=>page('Layers',b=>spec.layers.build(b))),
        spec.columns?row('Columns','eye','Shown, order and wrap',()=>page('Columns',b=>spec.columns!.build(b,changed))):null,
        ...(spec.density?segment('Density',[['compact','Compact'],['comfortable','Comfortable']],spec.density.value(),k=>spec.density!.set(k),changed):[]),teamRowsField(),staffingField()),
      foot);
    drawFoot();
  }
  const start=()=>spec.days.weekStart()??spec.days.departmentStart,order=()=>Array.from({length:7},(_,i)=>(start()+i)%7);
  function daysHint(){const w=spec.days.weekdays(),win=spec.days.window();
    return [w?order().filter(d=>w.includes(d)).map(d=>WEEKDAYS[d].slice(0,3)).join(', '):'Every day',`week starts ${WEEKDAYS[start()].slice(0,3)}`,win?`${win.start}–${win.end}`:''].filter(Boolean).join(' · ');}
  /** Days & time: which weekdays show (at least one), the view's own week start, and the hours of the Day layout. */
  function daysPage(focus=''){
    page('Days & time',b=>{
      const shown=spec.days.weekdays()||order();
      const days=el('div',{class:'vs-days',role:'group','aria-label':'Weekdays shown'},...order().map(d=>{
        const on=shown.includes(d),t=el('button',{type:'button','aria-pressed':String(on),'aria-label':WEEKDAYS[d],'data-day':String(d)},WEEKDAYS[d].slice(0,3)) as HTMLButtonElement;
        if(on&&shown.length===1)t.disabled=true;  // at least one day stays
        t.addEventListener('click',()=>{const next=on?shown.filter(x=>x!==d):[...shown,d];spec.days.setWeekdays(next.length===7?null:next);changed();daysPage(`[data-day="${d}"]`);});return t;}));
      const first=select('Week starts on',[{value:'',label:`Department default (${WEEKDAYS[spec.days.departmentStart]})`},...WEEKDAYS.map((n,i)=>({value:String(i),label:n}))],String(spec.days.weekStart()??''),false);
      first.input.addEventListener('change',()=>{spec.days.setWeekStart(first.input.value===''?null:Number(first.input.value));changed();daysPage('select[name="Week starts on"]');});
      const win=spec.days.window(),from=field('From','time',win?.start||'07:00'),to=field('To','time',win?.end||'19:00');
      const apply=()=>{if(from.input.value&&to.input.value&&to.input.value>from.input.value){spec.days.setWindow({start:from.input.value,end:to.input.value});changed();}};
      from.input.addEventListener('change',apply);to.input.addEventListener('change',apply);
      const whole=button('Whole day',()=>{spec.days.setWindow(null);changed();daysPage();},'ghost small');whole.disabled=!win;
      b.append(el('div',{class:'dt-pop-label'},'Weekdays'),days,el('div',{class:'vs-fields'},first.wrap),
        el('div',{class:'dt-pop-label'},'Time window · Day'),el('div',{class:'vs-fields'},from.wrap,to.wrap),el('div',{class:'vs-inline'},el('span',{class:'hint'},win?'Day shows these hours.':'Day shows the whole day.'),whole));
    },main,focus);
  }
  const colourHint=()=>{const c=spec.colour!,name=nameOf(c.options,c.value());if(c.value()!=='tag'||!c.tags)return name;
    const by=new Map(c.tags.options().map(t=>[t.id,t.name||''])),ids=c.tags.value();
    return ids.length?`${name}: ${ids.slice(0,3).map(id=>'#'+(by.get(id)??'removed')).join(', ')}${ids.length>3?` +${ids.length-3}`:''}`:`${name}: none chosen`;};
  /** Colour by, and for Tag the tags in priority order: shown ones colour people, the first one a person holds wins. */
  function colourPage(focus=''){
    const c=spec.colour!;
    page('Colour by',b=>{
      b.append(...segment('Colour by',c.options,c.value(),k=>{c.set(k);changed();colourPage(`[role=radiogroup] [aria-checked=true]`);}));
      if(c.value()!=='tag'||!c.tags)return;
      const t=c.tags,all=t.options(),by=new Map(all.map(x=>[x.id,x])),chosen=t.value();
      // Chosen first, in their order (a removed or archived one stays until taken off); then the active rest by name.
      const order=[...chosen,...all.filter(x=>x.active!==false&&!chosen.includes(x.id)).sort((a,b)=>String(a.name).localeCompare(String(b.name))).map(x=>x.id)];
      const set=(ids:string[],again:string)=>{t.set(ids);changed();colourPage(again);};
      b.append(el('div',{class:'dt-pop-label'},'Tags · the first one a person has gives the colour'));
      if(!order.length){b.append(el('p',{class:'hint'},'No person tags yet. Add them in Configuration › Tags.'));return;}
      orderRows(b,order,{on:k=>chosen.includes(k),dot:k=>by.has(k)&&by.get(k)!.active!==false?colourOf(by.get(k)!):null,
        name:k=>{const x=by.get(k);return x?'#'+x.name+(x.active===false?' · archived, not drawn':''):'Removed tag, not drawn';},
        example:k=>chosen.includes(k)?`Priority ${chosen.indexOf(k)+1}`:'',locked:k=>!chosen.includes(k)&&chosen.length>=t.limit,
        move:(k,to)=>set(moved(order,k,to).filter(x=>chosen.includes(x)),`[data-part="${k}"] .grip`),
        toggle:k=>set(chosen.includes(k)?chosen.filter(x=>x!==k):[...chosen,k],`[data-part="${k}"] .vs-eye`)});
      b.append(el('div',{class:'vs-inline'},el('span',{class:'hint'},chosen.length?'People with none of these stay grey.':'Show a tag to colour people by it.')));
    },main,focus);
  }
  /** Properties: what each session card shows, in order, and in which form; the Person label has its own page. */
  function properties(focus=''){
    // Properties no renderer draws yet stay out of the list but keep their saved place and choice.
    const all=spec.properties.value(),now=all.filter(p=>offered(p.key)),undrawn=all.filter(p=>!offered(p.key)),keys=now.map(p=>p.key),of=(k:ViewProperty['key'])=>now.find(p=>p.key===k)!;
    const set=(next:ViewProperty[],again='')=>{spec.properties.set([...next,...undrawn]);changed();properties(again);};
    page('Properties',b=>{
      const hide=el('button',{type:'button',class:'link-button'},'Hide all');
      hide.addEventListener('click',()=>set(now.map(p=>({...p,on:p.key==='label'}))));
      const rowDetail=(k:ViewProperty['key'])=>!!spec.personal&&(ROW_FACTS as readonly string[]).includes(k);
      b.append(el('div',{class:'vs-inline'},el('span',{class:'dt-pop-label'},spec.personal?'On each card or row · drag to reorder':'On each card · drag to reorder'),hide),row('Person label','people',summary(spec.label.value()),()=>personLabelPage()));
      orderRows(b,keys.filter(k=>k!=='label'),{on:k=>!!of(k).on,name:k=>PROPERTY_NAMES[k]+(rowDetail(k)?' · row detail':''),example:k=>PROPERTY_EXAMPLES[k],locked:()=>false,
        move:(k,to)=>set(['label' as const,...moved(keys.filter(x=>x!=='label'),k,to)].map(of),`[data-part="${k}"] .grip`),
        toggle:k=>set(now.map(p=>p.key===k?{...p,on:!p.on}:p),`[data-part="${k}"] .vs-eye`),
        control:k=>{const forms=PROPERTY_FORMS[k];if(forms.length<2)return null;
          // The shared dropdown (dom.ts select, opened by the picker); its label is read by screen readers only.
          const f=select(`Show ${PROPERTY_NAMES[k]} as`,forms.map(x=>({value:x,label:FORM_NAMES[x]})),of(k).form||forms[0]);f.input.classList.add('vs-as');f.wrap.classList.add('vs-as-field');
          f.input.addEventListener('change',()=>set(now.map(p=>p.key===k?{...p,form:f.input.value as ViewProperty['form']}:p),`[data-part="${k}"] select`));return f.wrap;}});
    },main,focus);
  }
  /** Person label: the shared editor (labelBody), for this view. */
  function personLabelPage(focus=''){
    page('Person label',b=>labelBody(b,{value:spec.label.value,part:spec.label.part,preview:spec.label.preview,
      set:(l,f)=>{spec.label.set(l);changed();personLabelPage(f);}}),()=>properties(),focus);
  }
  main();
}
