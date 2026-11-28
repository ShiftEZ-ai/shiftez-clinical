/** Print presets: built-in ones defined here, and saved ones (personal or shared with the department) kept as saved views
 *  of the collection "schedule_print" with `definition.print`. The one
 *  place that reads, saves and resolves them; schedule_export.ts draws the dialog and the pages. */
import {PRINT_IMAGE_DPI,PRINT_IMAGE_DPI_DEFAULT,type PrintSettings,type TableViewDefaults} from './generated.js';
import {get} from './api.js';

/** `wrap` (the retired Long names choice, DL-014) may still be stored: it is read and ignored, and never written. */
export type Print=Required<Omit<PrintSettings,'wrap'>>;
export type Include=Print['include'][number];
export const PRINT_KEY='schedule_print';
/** A saved preset as the saved-views API returns it. */
export type Preset={id:string;revision:number;name:string;definition:{print?:PrintSettings|null};scope?:'personal'|'department';can_edit?:boolean;
  is_personal_default?:boolean;is_department_default?:boolean};
export const INCLUDES:[Include,string][]=[['title','Title'],['period','Period'],['status','Status'],['key','Key'],['filters','Active filters'],
  ['page_numbers','Page numbers'],['printed_by','Printed by and date'],['signature','Signature line']];

export const STANDARD:Print={schema_version:1,paper:'a4',orientation:'landscape',scale:'fit_width',scale_percent:null,margins:'normal',colour:'colour',
  text_size:'normal',include:['title','period','status','key','page_numbers','printed_by'],repeat_headers:true,page_break:'none',range:'view',view_id:null,format:'print',
  image_dpi:null};
/** The image quality a setting means: absent or null is the generated default. */
export const dpiOf=(s:Pick<PrintSettings,'image_dpi'>):number=>s.image_dpi??PRINT_IMAGE_DPI_DEFAULT;
/** Built-in presets. The first is the built-in default (the one a `null` default chooses). */
export const BUILT_INS:{id:string;name:string;print:Print}[]=[
  {id:'builtin:standard',name:'Standard · A4 landscape',print:STANDARD},
  {id:'builtin:notice',name:'Notice board · A3 landscape, colour',print:{...STANDARD,paper:'a3',text_size:'large',include:['title','period','status','key','page_numbers'],page_break:'week'}},
  {id:'builtin:handout',name:'Handout · A4 landscape, greyscale',print:{...STANDARD,colour:'greyscale',margins:'narrow',text_size:'small'}},
  {id:'builtin:signoff',name:'Sign-off copy · A4 portrait',print:{...STANDARD,orientation:'portrait',colour:'greyscale',include:['title','period','status','key','filters','page_numbers','printed_by','signature']}},
];

const one=<T extends string>(value:unknown,allowed:readonly T[],fallback:T):T=>allowed.includes(value as T)?value as T:fallback;
/** Any stored or partial settings as complete, valid ones (unknown values fall back to Standard's). */
export function normalise(p?:PrintSettings|null):Print{
  const s=p||{},scale=one(s.scale,['fit_width','fit_page','actual','custom'],STANDARD.scale);
  const pct=Math.round(Number(s.scale_percent));
  const known=INCLUDES.map(i=>i[0]),dpi=Number(s.image_dpi);
  return {schema_version:1,paper:one(s.paper,['a4','a3','letter','legal'],STANDARD.paper),orientation:one(s.orientation,['landscape','portrait'],STANDARD.orientation),
    scale,scale_percent:scale==='custom'?(Number.isFinite(pct)?Math.min(200,Math.max(25,pct)):100):null,margins:one(s.margins,['narrow','normal','wide'],STANDARD.margins),
    colour:one(s.colour,['colour','greyscale'],STANDARD.colour),text_size:one(s.text_size,['small','normal','large'],STANDARD.text_size),
    include:Array.isArray(s.include)?known.filter(k=>s.include!.includes(k)):[...STANDARD.include],
    repeat_headers:s.repeat_headers??STANDARD.repeat_headers,page_break:one(s.page_break,['none','group','week','month'],STANDARD.page_break),
    range:one(s.range,['view','week','2weeks','month','rota'],STANDARD.range),view_id:typeof s.view_id==='string'&&s.view_id?s.view_id:null,
    format:one(s.format,['print','png','csv'],STANDARD.format),
    // The default is stored as absent (null), so older presets and an explicit default read and compare the same.
    image_dpi:Object.hasOwn(PRINT_IMAGE_DPI,String(dpi))&&dpi!==PRINT_IMAGE_DPI_DEFAULT?dpi as Print['image_dpi']:null};
}
/** Two settings print the same (key order and include order aside). */
export const samePrint=(a:PrintSettings,b:PrintSettings)=>{const x=normalise(a),y=normalise(b);
  return (Object.keys(x) as (keyof Print)[]).every(k=>k==='include'?x.include.join()===y.include.join():x[k]===y[k]);};

/** The preset a dialog opens with: this session's last settings, else the personal default, else the department's,
 *  else the built-in Standard. A default set to `null` (`*_set` true) explicitly chooses Standard. */
export function startingPreset(views:Preset[],defaults:TableViewDefaults|undefined):{id:string;print:Print}{
  const pick=(set:boolean|undefined,id:string|null|undefined)=>{if(!set)return null;const v=id&&views.find(x=>x.id===id);
    return v?{id:v.id,print:normalise(v.definition.print)}:id?null:{id:BUILT_INS[0].id,print:BUILT_INS[0].print};};
  return pick(defaults?.personal_set,defaults?.personal_view_id)||pick(defaults?.department_set,defaults?.department_view_id)||{id:BUILT_INS[0].id,print:BUILT_INS[0].print};
}

// ---- Persistence: the saved-views API (create, update, rename, delete and share go through table_view.ts's menu) ----
export type PresetList={views:Preset[];defaults?:TableViewDefaults};
export const listPresets=()=>get<PresetList>('/api/table-views?table='+PRINT_KEY);

/** Settings chosen in this session (any preset, changed or not): the next dialog opens with them. */
export const session:{last:{id:string;print:Print}|null}={last:null};

/** What a dialog opens with before the saved presets are read: the print settings last used from the saved view on
 *  screen, else this session's; `null` leaves it to the defaults (startingPreset). The preset named is a built-in with
 *  the same settings, else the session's, so the button says Changed only when they differ from it. */
export function openingPrint(kept:PrintSettings|null|undefined,last:typeof session.last):{id:string;print:Print}|null{
  if(!kept)return last;
  const print=normalise(kept);
  return {id:BUILT_INS.find(b=>samePrint(b.print,print))?.id||last?.id||BUILT_INS[0].id,print};
}
