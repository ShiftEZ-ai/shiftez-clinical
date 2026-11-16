/** The application's names and logos: one applied state for every screen (navigation, sign-in, start-up, account links,
 *  page titles). The app name is its full name (titles, navigation); the short name is how its own words name the
 *  product (About, start-up failure, Compare your rota with …). It is the deployment's public branding (GET
 *  /api/branding, application-wide), never a department's and never kept on this device. Until that read answers, or when it cannot, the generated defaults apply (the bundled
 *  assets and names), so nothing is ever blank or a broken image. */
import {BRANDING_DEFAULTS as defaults,type BrandingPublic} from './generated.js';

export const BRANDING_DEFAULTS={...defaults,emblem_url:new URL(defaults.emblem_url,import.meta.url).pathname,
  wordmark_url:new URL(defaults.wordmark_url,import.meta.url).pathname};

// The bundled assets' pixel sizes (apps/web/public/assets/brand), reserving their space before they load.
const SIZES:Record<string,[number,number]>={[BRANDING_DEFAULTS.emblem_url]:[160,160],[BRANDING_DEFAULTS.wordmark_url]:[342,116]};
let current:BrandingPublic={...BRANDING_DEFAULTS,revision:0};
let titleStem='';

/** The branding now shown. */
export const brand=():Readonly<BrandingPublic>=>current;
/** Width and height for an image's attributes (its aspect ratio before it loads); unknown sizes leave the browser's. */
export const brandSize=(url:string)=>SIZES[url];
/** A logo the server supplied, not the bundled one: drawn exactly as supplied (no circular crop). */
export const suppliedLogo=(url:string)=>url!==BRANDING_DEFAULTS.emblem_url&&url!==BRANDING_DEFAULTS.wordmark_url;

/** Only this site's own paths are used as logos (CSP img-src 'self'): never another origin or a data/script URL. */
const ownPath=(v:unknown):v is string=>typeof v==='string'&&/^\/(?!\/)[^\\\s]*$/.test(v);
const text=(v:unknown,max=160):v is string=>typeof v==='string'&&v.length<=max;
/** The server's answer read field by field: a missing or unusable field keeps its generated default. */
export function parseBranding(v:unknown):BrandingPublic{
  const o=(v&&typeof v==='object'&&!Array.isArray(v)?v:{}) as Record<string,unknown>,d=BRANDING_DEFAULTS;
  const name=(k:'short_name'|'app_name'|'institution_name')=>text(o[k])&&(o[k] as string).trim()?o[k] as string:d[k];
  return {short_name:name('short_name'),app_name:name('app_name'),institution_name:name('institution_name'),
    emblem_url:ownPath(o.emblem_url)?o.emblem_url:d.emblem_url,wordmark_url:ownPath(o.wordmark_url)?o.wordmark_url:d.wordmark_url,
    emblem_alt:text(o.emblem_alt)?o.emblem_alt:d.emblem_alt,wordmark_alt:text(o.wordmark_alt)?o.wordmark_alt:d.wordmark_alt,
    revision:Number.isInteger(o.revision)&&(o.revision as number)>0?o.revision as number:0};
}

/** A page's title: "<page> · <app name>", kept up to date when the branding changes. */
export function documentTitle(stem:string){titleStem=stem;document.title=stem?`${stem} · ${current.app_name}`:current.app_name;}

/** Loads a logo before it is shown; one that fails keeps the logo already shown. */
async function loaded(url:string){
  if(SIZES[url])return true;
  const img=new Image();img.src=url;
  try{await img.decode();SIZES[url]=[img.naturalWidth,img.naturalHeight];return true;}catch{return false;}
}
function paintImage(img:HTMLImageElement,url:string,alt:string){
  img.src=url;img.alt=alt;img.classList.toggle('brand-supplied',suppliedLogo(url));
  const size=SIZES[url];if(size){img.width=size[0];img.height=size[1];}
}
function paint(){
  for(const img of document.querySelectorAll<HTMLImageElement>('img.brand-emblem'))paintImage(img,current.emblem_url,current.emblem_alt);
  // The browser tab's icon is the same emblem (SW3-C-11): a static file, never a server read per page load.
  // The import is also used by the small Node word/branding checks, whose DOM only exposes querySelectorAll.
  document.querySelector?.<HTMLLinkElement>('link[rel=icon]')?.setAttribute('href',current.emblem_url);
  for(const img of document.querySelectorAll<HTMLImageElement>('img.brand-wordmark'))paintImage(img,current.wordmark_url,current.wordmark_alt);
  for(const n of document.querySelectorAll('[data-brand-name]'))n.textContent=current.app_name;
  for(const n of document.querySelectorAll('[data-brand-short]'))n.textContent=current.short_name;
  for(const n of document.querySelectorAll('[data-brand-home]'))n.setAttribute('aria-label','Home · '+current.app_name);
  if(titleStem)documentTitle(titleStem);
}
let asked=0;
/** Shows new branding everywhere it is drawn. The names apply at once; each logo swaps in only once it has loaded, so a
 *  slow or failing logo keeps the one shown and holds up nothing else (no caller waits for it). An older revision than
 *  the one shown is ignored, and a logo that loads after newer branding was asked for is dropped. The promise settles
 *  when both logos have settled (tests). */
export function applyBranding(next:BrandingPublic):Promise<void>{
  if(next.revision&&next.revision<current.revision)return Promise.resolve();
  const mine=++asked;
  current={...next,emblem_url:current.emblem_url,emblem_alt:current.emblem_alt,wordmark_url:current.wordmark_url,wordmark_alt:current.wordmark_alt};
  paint();
  return Promise.all((['emblem','wordmark'] as const).map(async kind=>{
    const url=next[`${kind}_url`];
    if(await loaded(url)&&mine===asked){current={...current,[`${kind}_url`]:url,[`${kind}_alt`]:next[`${kind}_alt`]};paint();}})).then(()=>{});
}
