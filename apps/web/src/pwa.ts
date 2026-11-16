/** The installable app and its alerts.
 *  Installing and allowing alerts are always the person's own taps: nothing here installs, subscribes or asks the
 *  system question on page load. What this device can do is measured here (capability detection, never the user agent
 *  sent to the server); whether the department sends alerts comes from the server. Every push call lives in this file.
 *  Request and command shapes come from the generated contracts (generated.ts). */
import {el,button,iconButton,icon,notify,showDialog,brandEmblem,type Child} from './dom.js';
import {get,command,ApiError} from './api.js';
import {brand} from './brand.js';
import {PWA_DETAIL_LEVELS,type PwaEventChannels,type PwaInstallationSummary,type PwaStatus} from './generated.js';

// ---- Device-local memory: offers dismissed with Not now (D31: never repeated) and this device's installation hint.
const DISMISSED='rota.offer.dismissed',HINT='rota.push.hint',BOUND='rota.push.bound',KEY='rota.push.key';
const stored=(key:string)=>{try{return localStorage.getItem(key)||'';}catch{return '';}};
const store=(key:string,value:string|null)=>{try{if(value===null)localStorage.removeItem(key);else localStorage.setItem(key,value);}catch{/* not kept: asked again */}};
const dismissed=(offer:'install'|'alerts')=>stored(DISMISSED).split(' ').includes(offer);
const dismiss=(offer:'install'|'alerts')=>store(DISMISSED,[...new Set([...stored(DISMISSED).split(' ').filter(Boolean),offer])].join(' '));
/** A random name for this browser profile or Home Screen copy (BACKEND-DESIGN §5: not a device fingerprint). */
const hint=()=>{let h=stored(HINT);if(!h){h=crypto.randomUUID();store(HINT,h);}return h;};

// ---- Platform: what this browser is and can do.
const hasNavigator=typeof navigator!=='undefined';
const ua=hasNavigator?navigator.userAgent:'';
export const ios=/iPhone|iPad|iPod/.test(ua)||(hasNavigator&&navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
export const android=/Android/.test(ua);
export const standalone=()=>typeof window!=='undefined'&&window.matchMedia('(display-mode: standalone)').matches||
  hasNavigator&&(navigator as {standalone?:boolean}).standalone===true;
/** iOS browsers all carry Safari's token; other apps' web views (Outlook, Teams, WhatsApp) do not. */
const inAppBrowser=()=>ios&&!/Safari\//.test(ua);
const iosChromeOrEdge=()=>/CriOS|EdgiOS/.test(ua);
const browserName=()=>/EdgA?\//.test(ua)||/EdgiOS/.test(ua)?'Edge':/SamsungBrowser/.test(ua)?'Samsung Internet':/Firefox|FxiOS/.test(ua)?'Firefox':/Chrome|CriOS/.test(ua)?'Chrome':/Safari/.test(ua)?'Safari':'';
const deviceName=()=>/iPad/.test(ua)||ios&&!/iPhone|iPod/.test(ua)?'iPad':ios?'iPhone':android?'Android phone':/Mac/.test(ua)?'Mac':/Windows/.test(ua)?'Windows computer':'computer';
/** "This iPhone" · "Home Screen app" / "This computer · Chrome". */
export const thisDevice=()=>({title:`This ${ios||android?deviceName():'computer'}`+(standalone()?'':browserName()?` · ${browserName()}`:''),
  label:`${deviceName()} · ${standalone()?(ios?'Home Screen app':'Installed app'):browserName()||'Browser'}`,mobile:ios||android});
/** Web Push can work here. A normal iPhone browser tab never counts, whatever its version (BACKEND-DESIGN §2). */
export const pushCapable=()=>hasNavigator&&typeof window!=='undefined'&&'serviceWorker' in navigator&&'PushManager' in window&&
  'Notification' in window&&!(ios&&!standalone());

// ---- Install (P1, P2). The browser's own install dialog is kept for the person's tap.
interface InstallPrompt extends Event {prompt():Promise<void>;userChoice:Promise<{outcome:'accepted'|'dismissed'}>}
let deferred:InstallPrompt|null=null;
const listeners=new Set<()=>void>();
const changed=()=>listeners.forEach(f=>f());
/** Redraw when install becomes possible or done (the browser says so at any time). */
export const onInstallChange=(run:()=>void)=>{listeners.add(run);return ()=>listeners.delete(run);};
if(typeof window!=='undefined'){
  window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferred=e as InstallPrompt;changed();});
  window.addEventListener('appinstalled',()=>{deferred=null;changed();});
}
/** How this browser installs: its own dialog, the iPhone guide, or "open in Safari" from another app's browser. */
export const installWay=():'prompt'|'ios'|'in-app'|null=>standalone()?null:deferred?'prompt':inAppBrowser()?'in-app':ios?'ios':null;
export async function install(){
  const way=installWay();
  if(way==='prompt'&&deferred){const prompt=deferred;await prompt.prompt();const {outcome}=await prompt.userChoice;deferred=null;if(outcome==='accepted')dismiss('install');changed();return;}
  if(way==='ios'||way==='in-app')installGuide(way);
}
/** P2: points at the browser's own controls (Apple offers websites no install button). The Share sheet's words were
 *  checked for iOS 26 (pwa/RESEARCH.md); recheck on a real iPhone before release. */
function installGuide(way:'ios'|'in-app'){
  const name=brand().short_name;
  const chip=(text:string,glyph?:Child)=>el('span',{class:'glyph'},glyph||null,text);
  const steps=(chrome:boolean)=>el('ol',{class:'install-steps'},...(chrome
    ?[['Tap ',chip('Share',icon('upload')),' in the address bar'],['Tap ',chip('Add to Home Screen',icon('plus'))],['Tap ',chip('Add')]]
    :[['Tap ',chip('•••'),' below'],['Tap ',chip('Share',icon('upload'))],['Tap View More, then ',chip('Add to Home Screen',icon('plus'))],['Tap ',chip('Add')]])
    .map((parts,i)=>el('li',{},el('span',{class:'n','aria-hidden':'true'},String(i+1)),el('span',{class:'g-t'},...parts))));
  showDialog(way==='in-app'?'Open in Safari to add the app':`Add ${name} to your Home Screen`,'',(body,close)=>{
    body.closest('dialog')!.classList.add('install-guide');
    body.closest('dialog')!.querySelector('.dialog-head')!.prepend(brandEmblem());
    if(way==='in-app'){
      body.append(el('p',{},'This browser can’t add apps to the Home Screen.'),el('div',{class:'actions'},
        button('Copy link',async()=>{try{await navigator.clipboard.writeText(location.origin+'/');notify('Link copied. Paste it in Safari.');}catch{notify('Copying is blocked here. Open '+location.origin+' in Safari.','info');}},'primary small')));
      return;}
    let chrome=iosChromeOrEdge();
    const list=el('div',{},steps(chrome));
    const other=button(chrome?'Using Safari?':'Using Chrome or Edge?',()=>{chrome=!chrome;list.replaceChildren(steps(chrome));other.textContent=chrome?'Using Safari?':'Using Chrome or Edge?';},'',{class:'link-button'});
    body.append(list,el('div',{class:'install-foot'},other,button('Done',close,'secondary small')));
  });
}

// ---- Push status from the server and this device's subscription.
export type Detail=typeof PWA_DETAIL_LEVELS[number];
export type Switch=NonNullable<PwaEventChannels['push']>;
export type Channels=PwaEventChannels;
export type Installation=PwaInstallationSummary;
export interface PrefEvent {id:string;label:string;audience:string;roles:string[];required:boolean;channels:Channels}
export interface Prefs {revision:number;events:PrefEvent[];event_channels:Record<string,Channels>;detail:Detail|null;
  detail_cap:Detail;email_configured:boolean}
export interface PolicyEvent {id:string;label:string;audience:string;mandatory:boolean;push:Switch;email:Switch}
export interface Policy {push_enabled:boolean;lock_screen_detail:Detail;event_defaults:Record<string,Channels>;
  retry?:Record<string,number>;delivery?:Record<string,number>;revision:number;events:Record<string,PolicyEvent>}
/** GET /api/me/push (PwaStatus): readiness, the public subscribing key, own installations, the department policy and
 *  this person's effective preferences (the two maps the generated type leaves open, typed here as the contract says). */
export type PushStatus=Omit<PwaStatus,'policy'|'preferences'|'installations'>&{policy:Policy;preferences:Prefs;installations:Installation[]};
let status:Promise<PushStatus|null>|null=null;
/** null: this server has no push yet (an older backend): the browser-tab alerts stay as they were. */
export const pushStatus=(fresh=false)=>{if(fresh||!status)status=get<PushStatus>('/api/me/push').catch(e=>{status=null;if(e instanceof ApiError&&e.status===404)return null;throw e;});return status;};
export const forgetPushStatus=()=>{status=null;};
const registration=()=>navigator.serviceWorker.ready;
const subscription=async()=>pushCapable()?(await registration()).pushManager.getSubscription():null;
/** This device's own installation: the one the server gave this device when alerts were turned on here. */
export const mine=(s:PushStatus)=>s.installations.find(i=>i.id===stored(BOUND));
const keyBytes=(base64url:string)=>Uint8Array.from(atob(base64url.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
/** Alerts are on for this device: the browser allows them and the server holds this device's subscription. */
export const pushOnHere=()=>!!stored(BOUND)&&'Notification' in window&&Notification.permission==='granted';

/** What "This device" shows (P3, P4). */
export type DeviceState='unsupported'|'needs-install'|'blocked'|'off'|'on'|'repair';
export async function deviceState(s:PushStatus|null):Promise<DeviceState>{
  if(ios&&!standalone())return 'needs-install';
  if(!('Notification' in window))return 'unsupported';
  if(Notification.permission==='denied')return 'blocked';
  if(!s||!pushCapable())return 'off';
  const own=mine(s);
  if(own?.state==='enabled')return await subscription()?'on':'repair';
  return own?.state==='repair_required'?'repair':'off';
}
/** Turn on (P3): called straight from the tap, so the system question is asked inside it (Apple requires that). The
 *  status, with its public key, is already loaded when the button is drawn. */
export async function turnOn(s:PushStatus){
  if(!s.public_key||s.readiness!=='ready')throw new Error('Alerts aren’t ready on this server yet.');
  const permission=Notification.permission==='granted'?'granted':await Notification.requestPermission();
  if(permission!=='granted'){notify('Alerts are blocked on this device. My notifications shows how to allow them.','warning');return false;}
  const reg=await registration();
  let sub=await reg.pushManager.getSubscription();
  // A subscription made for another sender key cannot be relabelled (RFC 8292): replace it.
  if(sub&&s.key_id&&stored(KEY)&&stored(KEY)!==s.key_id){await sub.unsubscribe().catch(()=>false);sub=null;}
  sub||=await reg.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:keyBytes(s.public_key)});
  const {endpoint,keys,expirationTime}=sub.toJSON();
  const result=await command<{installation_id:string}>('push.subscribe',{endpoint,keys,
    expiration_time:expirationTime?new Date(expirationTime).toISOString():null,installation_hint:hint(),
    label:thisDevice().label,platform:deviceName(),explicit_consent:true,key_id:s.key_id});
  store(BOUND,result.installation_id);store(KEY,s.key_id||'');forgetPushStatus();
  return true;
}
/** Turn off on this device: the server stops sending, then the browser forgets the subscription. */
export async function turnOff(s:PushStatus){
  const own=mine(s);
  if(own)await command('push.unsubscribe',{installation_id:own.id,expected_revision:own.revision,reason:'Turned off on this device'});
  await (await subscription())?.unsubscribe().catch(()=>false);
  store(BOUND,null);forgetPushStatus();
}
/** Another of my devices (P4 Other devices › Remove). */
export async function removeDevice(i:Installation){
  await command('push.unsubscribe',{installation_id:i.id,expected_revision:i.revision,reason:'Removed from another device'});forgetPushStatus();}
/** Send a test (P4): says what the server knows (queued, or accepted by the push service), never "delivered". */
export async function sendTest(s:PushStatus){
  const own=mine(s);if(!own)return;
  const r=await command<{status?:string}>('push.test',{installation_id:own.id,reason:'Test from My notifications'});
  notify(r?.status==='accepted'?'The push service accepted the test. Check this device.':'Test sent. It should show on this device shortly.');
}
/** After signing in: a subscription this person turned on before is registered again if the server lost it (a
 *  replaced browser subscription, a new sender key). Nothing is asked and nothing new is turned on. */
export async function reconcile(){
  if(!stored(BOUND)||!pushCapable()||Notification.permission!=='granted')return;
  const s=await pushStatus().catch(()=>null);if(s?.readiness!=='ready'||!s.public_key)return;
  const own=mine(s),sub=await subscription();
  if(own?.state==='enabled'&&sub&&stored(KEY)===s.key_id)return;
  // Turned off or removed (here or from another device): stays off until turned on again.
  if(!own||own.state==='revoked'){store(BOUND,null);return;}
  await turnOn(s).catch(()=>{});
}
/** Signing out (BACKEND-DESIGN §5): the server stops this device's alerts with the session; here the icon number and
 *  this app's shown alerts go, and the binding is forgotten (the next account turns alerts on itself). */
export async function signedOut(){
  store(BOUND,null);forgetPushStatus();setBadge(0);
  const reg=await navigator.serviceWorker?.getRegistration().catch(()=>undefined);
  (await reg?.getNotifications().catch(()=>[]))?.forEach(n=>n.close());
}

// ---- Alerts in a browser tab (before push, or where the department has not turned it on): shown by the open page
// when the tab is in the background. Never on top of a push alert for the same item (this device's push is on).
const TAB_ALERTS='rota.alerts';
export const tabAlertsWanted=()=>stored(TAB_ALERTS)==='on';
export const tabAlertsOn=()=>tabAlertsWanted()&&!pushOnHere()&&'Notification' in window&&Notification.permission==='granted';
export async function toggleTabAlerts(){
  if(tabAlertsWanted()){store(TAB_ALERTS,'off');notify('Desktop alerts are off.');return;}
  if(!('Notification' in window)){notify('This browser cannot show desktop alerts.','warning');return;}
  const permission=Notification.permission==='default'?await Notification.requestPermission():Notification.permission;
  if(permission!=='granted'){notify('Alerts are blocked for this site in the browser settings.','warning');return;}
  store(TAB_ALERTS,'on');
  notify('Desktop alerts are on: new inbox items show even when this tab is in the background.');
}

// ---- The icon number (D30): the unread inbox count, the bell's number.
export function setBadge(unread:number){
  const n=navigator as Navigator&{setAppBadge?:(n:number)=>Promise<void>;clearAppBadge?:()=>Promise<void>};
  (unread>0?n.setAppBadge?.(unread):n.clearAppBadge?.())?.catch(()=>{});
}

/** An alert's address (/n/{id}) as the notification it opens, after sign-in and access checks on the server; null when
 *  it is not one (or is gone: the caller opens the inbox). */
export async function alertNotification(url:URL):Promise<Record<string,unknown>|null|undefined>{
  const id=/^\/n\/([0-9a-f-]{36})$/i.exec(url.pathname)?.[1];
  if(!id)return undefined;
  return get<{notification:Record<string,unknown>}>(`/api/notifications/${id}/destination`).then(d=>d.notification,()=>null);
}

// ---- Worker: registration, a safe update after a deploy, and an alert's tap handed to this page.
/** Opens what an alert's tap asked for (app.ts: through the page's own unsaved-changes guard). */
export function startWorker(open:(url:string)=>void){
  if(!('serviceWorker' in navigator))return;
  // The worker script always revalidates (updateViaCache none; /sw.js is no-cache too).
  window.addEventListener('load',()=>{navigator.serviceWorker.register('/sw.js',{updateViaCache:'none'}).catch(()=>{});});
  navigator.serviceWorker.addEventListener('message',e=>{
    if(e.data?.type==='fmuc-open'&&typeof e.data.url==='string')open(e.data.url);
    else if(e.data?.type==='fmuc-resubscribe')void reconcile();});
}
/** A new build is deployed: fetch its worker now, so Reload starts the new shell. */
export const updateWorker=()=>{void navigator.serviceWorker?.getRegistration().then(r=>r?.update()).catch(()=>{});};

// ---- The Home row (P1–P3): install first, then alerts; each offered until Not now, on this device only.
/** P3's toast: alerts are on here, with a test one tap away. */
export const turnedOn=()=>notify(`Alerts are on for ${thisDevice().title.replace(/^This /,'this ')}.`,'success',
  {label:'Send a test',run:async()=>{const s=await pushStatus(true);if(s)await sendTest(s);}});
/** Asked again only in context (mockup P3 after-cant-make-it): once someone has reported they can't make a duty, a
 *  second message offers alerts for when it is covered, if this device could turn them on now. */
export async function offerAlertsAfterReport(){
  if(!pushCapable()||Notification.permission!=='default')return;
  const s=await pushStatus().catch(()=>null);
  if(s?.readiness!=='ready'||await deviceState(s)!=='off')return;
  notify('Alert when it’s covered?','info',{label:'Turn on',run:async()=>{if(await turnOn(s))turnedOn();}});
}
/** Drawn on a phone's Home only (D31: nothing on a computer's Home or on sign-in). */
export function offerRow():HTMLElement{
  const slot=el('div',{class:'offer-slot'});
  const row=(name:string,title:string,line:string,lead:Node,action:HTMLButtonElement,offer:'install'|'alerts')=>el('section',{class:'panel app-offer','aria-label':title,'data-offer':name},
    lead,el('span',{class:'txt'},el('strong',{},title),el('small',{},line)),
    el('span',{class:'actions'},action,iconButton('Not now','close',()=>{dismiss(offer);slot.replaceChildren();},{class:'bare'})));
  const draw=async()=>{
    const way=installWay();
    if(way&&!dismissed('install')){
      const paint=(alerts:boolean)=>slot.replaceChildren(row('install',`Get the ${brand().short_name} app`,alerts?'Icon and duty alerts':'An icon on your Home Screen',brandEmblem(),
        button(way==='prompt'?'Install':'How to add',()=>install(),'primary small'),'install'));
      // SX-09: alerts are promised only where the department has them ready.
      paint(false);void pushStatus().then(s=>{if(s?.readiness==='ready'&&slot.querySelector('[data-offer=install]'))paint(true);},()=>{});return;}
    slot.replaceChildren();
    if(dismissed('alerts')||!pushCapable()||Notification.permission!=='default')return;
    const s=await pushStatus().catch(()=>null);
    if(s?.readiness!=='ready'||await deviceState(s)!=='off')return;
    slot.replaceChildren(row('alerts','Turn on alerts','Duty changes, swaps and requests',el('span',{class:'offer-icon','aria-hidden':'true'},icon('bell')),
      button('Turn on',async()=>{if(await turnOn(s)){slot.replaceChildren();turnedOn();}},'primary small'),'alerts'));
  };
  const stop=onInstallChange(()=>{if(!slot.isConnected){stop();return;}void draw();});
  void draw();
  return slot;
}
