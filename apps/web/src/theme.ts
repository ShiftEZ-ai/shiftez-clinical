// Per-viewer display preferences. Stored only in this browser and cleared on sign-out.
export type Theme='light'|'dark'|'system';
const KEY='rota.prefs';
export type NameMode='full'|'short'|'code'|'number';
type Prefs={theme:Theme;navPinned:boolean;names?:NameMode;/** Auto-schedule shows its result still, without motion. */still?:boolean};
function read():Prefs{
  try{const v=JSON.parse(localStorage.getItem(KEY)||'{}');return {theme:['light','dark'].includes(v.theme)?v.theme:'system',navPinned:!!v.navPinned,names:['full','short','code','number'].includes(v.names)?v.names:undefined,still:v.still===true||undefined};}
  catch{return {theme:'system',navPinned:false};}
}
let prefs=read();
// The old form layout preference ("forms") is dropped once, so it is never read again.
try{if('forms' in JSON.parse(localStorage.getItem(KEY)||'{}'))save();}catch{/* nothing stored */}
function save(){try{localStorage.setItem(KEY,JSON.stringify(prefs));}catch{/* storage unavailable: preference lasts for this page only */}}
export function applyTheme(){
  const root=document.documentElement;
  if(prefs.theme==='system')root.removeAttribute('data-theme');else root.setAttribute('data-theme',prefs.theme);
  // The browser and installed-app chrome follow the page (maintenance): the system choice keeps index.html's per-scheme
  // colours; a chosen theme uses that theme's page background.
  const bg=prefs.theme==='system'?'':getComputedStyle(root).getPropertyValue('--bg').trim();
  document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]').forEach(m=>{m.dataset.system??=m.content;m.content=bg||m.dataset.system;});
}
export const theme=()=>prefs.theme;
export const isDark=()=>prefs.theme==='dark'||(prefs.theme==='system'&&matchMedia('(prefers-color-scheme: dark)').matches);
export function setTheme(value:Theme){prefs={...prefs,theme:value};save();applyTheme();}
export const navPinned=()=>prefs.navPinned;
export function setNavPinned(value:boolean){prefs={...prefs,navPinned:value};save();}
// The department default applies until this viewer picks their own.
let departmentNames:NameMode='full';
export function setDepartmentNames(value:NameMode|undefined){departmentNames=value||'full';}
export const nameMode=()=>prefs.names||departmentNames;
export const ownNameMode=()=>prefs.names;
export function setNameMode(value:NameMode){prefs={...prefs,names:value};save();}
/** This viewer's motion choice (REQ-MOTION): Still shows auto-schedule changes without animation, whatever the
 *  department's style; the device's reduced-motion setting settles every change at once without it. */
export const motionStill=()=>!!prefs.still;
export function setMotionStill(value:boolean){prefs={...prefs,still:value||undefined};save();}
export function clearPrefs(){prefs={theme:'system',navPinned:false};try{localStorage.removeItem(KEY);}catch{/* ignore */}applyTheme();}
