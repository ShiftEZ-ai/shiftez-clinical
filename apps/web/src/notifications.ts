/** My notifications (Account › Notifications, mockup P4) and Configuration › Notifications (P5, admins).
 *  The inbox keeps everything; alerts only tell people about it, and never acknowledge, accept or approve. The events
 *  and the department's defaults come from GET /api/me/push (build/contracts/PWA-BE.md); two events cannot be turned
 *  off (D28); email shows only where the department has email set up (D35); the lock-screen detail is the department's
 *  maximum and a person may choose less (D27). Shapes: build/contracts/PWA-BE.md and generated.ts. */
import {el,select,button,iconButton,icon,badge,note,notify,armed,sectionHead,saveBar,confirmAction,historyDialog,table,formatDate,withIcon,brandEmblem,type Child,CAPABILITY,roleForCapability} from './dom.js';
import {TERMS} from './terms.js';
import {get,command,ApiError,sessionChanged} from './api.js';
import {dockPanel} from './side_panel.js';
import {brand} from './brand.js';
import {kindLabel} from './config_kinds.js';
import {pushStatus,forgetPushStatus,deviceState,turnOn,turnOff,turnedOn,sendTest,removeDevice,mine,thisDevice,install,installWay,
  tabAlertsWanted,toggleTabAlerts,ios,android,standalone,type PushStatus,type Installation,type DeviceState,type Detail,type Switch,
  type Channels,type Prefs,type PrefEvent} from './pwa.js';
import {segmented} from './dom.js';  // the one segmented choice (UX › controls S6)

const DETAILS:{value:Detail;label:string}[]=[{value:'generic',label:'Generic'},{value:'event',label:'Event'},{value:'event_date',label:'Event and date'}];
/** How a person hears of one event: alert and/or email. Email choices exist only where the department has email
 *  set up (D35, `email_configured`); without it an event is Alert or Inbox only. */
const CHOICES:{value:string;label:string;push:Switch;email:Switch}[]=[
  {value:'alert_email',label:'Alert and email',push:'on',email:'on'},{value:'alert',label:'Alert',push:'on',email:'off'},
  {value:'email',label:'Email',push:'off',email:'on'},{value:'inbox',label:'Inbox only',push:'off',email:'off'}];
/** Department alert levels: On, Urgent (high urgency; it never breaks through Focus) or Off. */
const ALERTS:{value:string;label:string}[]=[{value:'on',label:'On'},{value:'urgent',label:'Urgent'},{value:'off',label:'Off'}];
const lock=(text:string)=>el('span',{class:'np-lock'},icon('lock'),text);
const help=(text:string)=>iconButton(text,'help',()=>{},{class:'bare help-tip'});
const on=(v:Switch|undefined)=>v!=='off';
const level=(v:Detail|undefined|null)=>Math.max(0,DETAILS.findIndex(d=>d.value===v));
const choiceOf=(c:Channels,email:boolean)=>CHOICES.find(x=>x.push===(on(c.push)?'on':'off')&&(!email||x.email===(c.email==='on'?'on':'off'))&&(email||x.email==='off'))!;

/** The inbox's unread count in words, the one source for the bell (its number, name and tooltip) and the inbox's own
 *  counts: the server's unread total (the pulse's `unread`, the list's `unread_total`), never the rows one page holds
 *  (maintenance: the bell said 525 while the first page of 100 said 100). */
export const unreadCount=(n:number)=>({text:n>99?'99+':String(n),label:n?`${n.toLocaleString('en-GB')} unread`:'All caught up'});

// ---------------------------------------------------------------- P4 · My notifications
/** The server lists only the events this person's roles receive (build/contracts/PWA-BE.md). */
export function notificationsPanel(departmentId:string){
  dockPanel({key:'notifications',title:'Notifications',width:460,className:'np-panel',build:d=>{
    const device=el('section',{'aria-labelledby':'np-device'}),others=el('section',{'aria-labelledby':'np-others'}),prefs=el('div',{class:'np'});
    d.body.append(el('div',{class:'np'},device,others,prefs));
    const draw=async(fresh=false)=>{
      let s:PushStatus|null;
      try{s=await pushStatus(fresh);}catch(e){if(sessionChanged(e))return;device.replaceChildren(note((e as Error).message,'error',e));return;}
      const state=await deviceState(s);
      device.replaceChildren(el('h3',{id:'np-device'},'This device'),thisDeviceRow(s,state,()=>void draw(true)));
      // shortcut: the first 100 of this person's devices (has_more); add paging if anyone ever has more.
      const rest=(s?.installations||[]).filter(i=>i!==mine(s!)&&i.state!=='revoked');
      others.replaceChildren(...(rest.length?[el('h3',{id:'np-others'},'Other devices'),el('ul',{class:'np-list'},...rest.map(i=>otherRow(i,()=>void draw(true))))]:[]));
      if(s?.enabled&&s.preferences)drawPrefs(prefs,s.preferences,departmentId,()=>void draw(true));else prefs.replaceChildren();
    };
    void draw();
  }});
}

function thisDeviceRow(s:PushStatus|null,state:DeviceState,redraw:()=>void):HTMLElement{
  const me=thisDevice(),lead=icon(me.mobile?'phone':'monitor');
  const row=(sub:Child,...actions:Child[])=>el('ul',{class:'np-list'},el('li',{},lead,el('span',{class:'np-t'},el('span',{},me.title),el('small',{},sub)),...actions));
  // SX-09: Add to Home Screen is the way to alerts only where the department has them on.
  if(state==='needs-install'&&s?.readiness!=='disabled_by_department')return row('Add to Home Screen to get alerts',installWay()?button('How to add',()=>install(),'secondary small'):null);
  // SX-09/SW3-C-08: alerts off for the department is said plainly before any browser state: no browser steps for alerts
  // that cannot arrive. A computer may still opt into its own tab alerts, offered only where the browser allows them.
  const off=s?.readiness==='disabled_by_department';
  if(off&&(me.mobile||state==='needs-install'||state==='blocked'||state==='unsupported'))return row('Your department hasn’t turned alerts on · updates appear in your Inbox');
  if(state==='needs-install')return row('Your department hasn’t turned alerts on');
  if(state==='unsupported')return row('This browser can’t show alerts');
  if(state==='blocked'){
    const b=(t:string)=>el('b',{},t);
    const steps:Child[][]=ios?[['Open ',b('Settings'),' › ',b('Notifications')],['Choose ',b(brand().short_name)],['Turn on ',b('Allow Notifications')]]
      :android&&standalone()?[['Open ',b('Settings'),' › ',b('Apps')],['Choose ',b(brand().short_name),' › ',b('Notifications')],['Turn on ',b('All notifications')]]
      :[['Select the icon beside the address'],['Open ',b('Site settings'),' › ',b('Notifications')],['Choose ',b('Allow'),', then reload']];
    const list=row(`Turned off in ${ios?'iPhone Settings':'the browser'}`,badge('Off','warn'));
    list.append(el('li',{},el('ol',{class:'np-steps'},...steps.map(parts=>el('li',{},...parts)))));
    return list;}
  // The department has not turned alerts on (or this server has none yet): a computer keeps its tab alerts.
  if(!s||off){
    const wanted=tabAlertsWanted();
    return row(wanted?'Alerts while open in a tab':'Alerts off',button(wanted?'Turn off':'Turn on',async()=>{await toggleTabAlerts();redraw();},wanted?'ghost small':'primary small'));}
  if(s.readiness!=='ready')return row('Alerts aren’t ready on this server yet');
  if(state==='on')return row(standalone()?`${me.label.split(' · ')[1]} · alerts on`:'Alerts on',button('Send a test',()=>sendTest(s),'secondary small'),
    button('Turn off',async()=>{await turnOff(s);notify('Alerts are off on this device.');redraw();},'ghost small'));
  return row(state==='repair'?'Alerts stopped on this device':'Alerts off',
    button(state==='repair'?'Turn on again':'Turn on',async()=>{if(await turnOn(s)){turnedOn();redraw();}},'primary small'));
}

function otherRow(i:Installation,redraw:()=>void){
  const phone=/iPhone|iPad|Android|phone/i.test(i.label+' '+i.platform);
  return el('li',{},icon(phone?'phone':'monitor'),
    el('span',{class:'np-t'},el('span',{},i.label||'Device'),el('small',{},i.state==='repair_required'?'Alerts stopped':i.last_accepted_at?`Last alert ${formatDate(i.last_accepted_at)}`:'No alerts yet')),
    armed(button('Remove',async()=>{await removeDevice(i);notify(`${i.label||'Device'} removed. It gets no more alerts.`);redraw();},'ghost small'),'Remove?'));
}

function drawPrefs(host:HTMLElement,p:Prefs,departmentId:string,reread:()=>void){
  const email=p.email_configured,cap=level(p.detail_cap),shown=p.detail&&level(p.detail)<=cap?p.detail:DETAILS[cap].value;
  // Each change is a new version of this person's choices; Undo restores the version before it.
  const save=async(change:{event_channels?:Record<string,Channels>;detail?:Detail|null},said:string)=>{
    let reply:{revision:number;previous_version_id:string|null};const before={event_channels:p.event_channels,detail:p.detail};
    try{reply=await command('notification.preferences.update',{department_id:departmentId,event_overrides:change.event_channels||p.event_channels,
      detail:change.detail===undefined?p.detail:change.detail,reason:'Changed in My notifications'},undefined,p.revision);
      // The reread redraws this panel a moment later: a second change made before it lands saves on this new version.
      p.revision=reply.revision;if(change.event_channels)p.event_channels=change.event_channels;if(change.detail!==undefined)p.detail=change.detail;}
    finally{forgetPushStatus();reread();}
    notify(said,'success',{label:'Undo',run:async()=>{
      await (reply.previous_version_id?command('notification.preferences.undo',{department_id:departmentId,version_id:reply.previous_version_id,reason:'Undo in My notifications'},undefined,reply.revision)
        :command('notification.preferences.update',{department_id:departmentId,event_overrides:before.event_channels,detail:before.detail,reason:'Undo in My notifications'},undefined,reply.revision));
      forgetPushStatus();reread();notify('Undone.');}});
  };
  const fail=(e:unknown)=>notify((e as Error).message,'error',undefined,e);
  const row=(ev:PrefEvent)=>{
    const text=el('span',{class:'np-t'},el('span',{},ev.label));
    const now=choiceOf(ev.channels,email);
    // D28: a required alert stays on; where email exists, email can still be added to it.
    const offered=CHOICES.filter(c=>(email||c.email==='off')&&(!ev.required||c.push==='on'));
    if(offered.length<2)return el('li',{},text,el('span',{'data-tip':'Your department requires this alert'},lock(now.label)));
    const pick=select(ev.label,offered,(offered.includes(now)?now:offered[0]).value).input;pick.setAttribute('aria-label',ev.label);
    pick.addEventListener('change',()=>{const c=CHOICES.find(x=>x.value===pick.value)!;
      void save({event_channels:{...p.event_channels,[ev.id]:{...p.event_channels[ev.id],push:c.push,...(email?{email:c.email}:{})}}},`${ev.label}: ${c.label}.`).catch(fail);});
    return el('li',{},text,pick);};
  // My duties: events that reach staff; Needs my decision: the rest (they ask someone to act).
  const groups:[string,PrefEvent[]][]=[['My duties',p.events.filter(e=>e.roles.includes(roleForCapability(CAPABILITY.STAFF)))],['Needs my decision',p.events.filter(e=>!e.roles.includes(roleForCapability(CAPABILITY.STAFF)))]];
  // Each choice saves at once, so arrows only move (manual): Space or a click chooses.
  const levels=segmented('On the lock screen',DETAILS.map((d,i)=>[d.value,d.label,i>cap?{disabled:true,tip:'More than your department allows'}:{}]),shown,
    v=>{void save({detail:v},`On the lock screen: ${DETAILS.find(d=>d.value===v)!.label}.`).catch(fail);},{labelledBy:'np-lock',activation:'manual'});
  host.replaceChildren(...groups.filter(([,list])=>list.length).map(([label,list],n)=>el('section',{'aria-labelledby':`np-g${n}`},
      el('div',{class:'np-head'},el('h3',{id:`np-g${n}`},label),n===0?help(email?'Alert: on this device’s lock screen and in its notification list. Email: to your work address. The inbox keeps everything.'
        :'Alert: on this device’s lock screen and in its notification list. The inbox keeps everything.'):null),
      el('ul',{class:'np-list'},...list.map(row)))),
    el('section',{'aria-labelledby':'np-lock'},el('div',{class:'np-head'},el('h3',{id:'np-lock'},'On the lock screen'),
      help(`What an alert shows before the device is unlocked. Your department allows up to ${DETAILS[cap].label}; never names, clinics or reasons.`)),levels));
}

// ---------------------------------------------------------------- P5 · Configuration › Notifications
/** A sample alert at each level, as the lock screen shows it (illustrative: tomorrow 13:00–16:00). */
function sample(value:Detail){
  const day=new Intl.DateTimeFormat('en-GB',{weekday:'short',day:'numeric',month:'short'}).format(new Date(Date.now()+864e5));
  const [title,line]=value==='generic'?[brand().short_name,'You have a rota update']:value==='event'?['Duty changed',`Open ${brand().short_name} to see it`]:['Duty changed',`${day} · 13:00–16:00`];
  return el('div',{class:'mini-note','aria-hidden':'true'},brandEmblem(),el('b',{},title,el('small',{},'now')),el('span',{},line));
}

export async function notificationPolicyPage(target:HTMLElement,departmentId:string,setLeave:(dirty:(()=>boolean)|null)=>void){
  let s=await pushStatus(true);
  if(!s){target.append(sectionHead(kindLabel('notifications'),''),note('Alerts on phones and computers need a newer server.','info'));return;}
  const conflict=el('div',{});
  const draw=(st:PushStatus)=>{
    setLeave(null);
    const pol=st.policy,email=st.email_configured,revision=st.policy_revision,ids=Object.keys(pol.events);
    const alertOf=(id:string)=>!on(pol.events[id].push)?'off':pol.event_defaults[id]?.urgency==='high'?'urgent':'on';
    const draft={push_enabled:pol.push_enabled,detail:pol.lock_screen_detail,
      events:Object.fromEntries(ids.map(id=>[id,{alert:alertOf(id),email:pol.events[id].email==='on'}]))};
    const save=button('Save…',()=>review(),'primary',{disabled:true,why:TERMS.nothingChanged});
    const changed=()=>{save.disabled=!changes().length;};
    const onOff=el('input',{type:'checkbox',class:'switch',role:'switch',checked:draft.push_enabled,'aria-labelledby':'dn-switch'});
    onOff.addEventListener('change',()=>{draft.push_enabled=onOff.checked;if(!missing)ready.lastChild!.textContent=readyWord();changed();});
    // What is missing stays visible: it changes whether anything is sent.
    const missing=!st.public_key?'the server has no sender key yet':st.readiness==='disabled_by_system'?'alerts are off for this server':st.readiness==='not_configured'?'the server is not set up to send':'';
    // SX-E2: ready beside a switch that is off reads as "ready to turn on", never as on.
    const readyWord=()=>draft.push_enabled?'Ready':'Ready to turn on';
    const ready=missing?badge(`Not ready · ${missing}`,'warn','',{icon:'alert'}):badge(readyWord(),'ok');
    const head=el('section',{class:'panel dn-switch'},el('label',{id:'dn-switch',class:'dn-switch-label'},'Alerts on phones and computers'),onOff,el('span',{class:'grow'}),ready);
    const rows=ids.map(id=>{
      const ev=pol.events[id],d=draft.events[id];
      // D28: a required event's alert cannot be turned off; it may still be urgent.
      const alerts=ALERTS.filter(a=>!ev.mandatory||a.value!=='off'),alert=select(`Alert for ${ev.label}`,alerts,(alerts.find(a=>a.value===d.alert)||alerts[0]).value).input;
      alert.setAttribute('aria-label',`Alert for ${ev.label}`);
      alert.addEventListener('change',()=>{d.alert=alert.value;changed();});
      const mail=el('input',{type:'checkbox',class:'switch',role:'switch',checked:d.email,'aria-label':`Email for ${ev.label}`});
      mail.addEventListener('change',()=>{d.email=mail.checked;changed();});
      return [ev.label,el('span',{class:'who'},ev.audience),ev.mandatory?el('span',{class:'dn-alert'},alert,lock('Required')):alert,...(email?[mail]:[])];});
    const cards=el('div',{class:'lvl-cards',role:'radiogroup','aria-labelledby':'dn-lock'},...DETAILS.map(x=>{
      const radio=el('input',{type:'radio',name:'dn-detail',value:x.value,checked:draft.detail===x.value});
      const card=el('label',{class:'lvl-card'+(draft.detail===x.value?' on':'')},el('span',{class:'lvl-head'},radio,x.label),sample(x.value));
      radio.addEventListener('change',()=>{draft.detail=x.value;cards.querySelectorAll('.lvl-card').forEach(c=>c.classList.toggle('on',c===card));changed();});
      return card;}));
    const word=(v:boolean)=>v?'On':'Off',alertWord=(v:string)=>ALERTS.find(a=>a.value===v)!.label;
    function changes():[string,string,string][]{
      const out:[string,string,string][]=[];
      if(draft.push_enabled!==pol.push_enabled)out.push(['Alerts on phones and computers',word(pol.push_enabled),word(draft.push_enabled)]);
      for(const id of ids){const label=pol.events[id].label,d=draft.events[id];
        if(d.alert!==alertOf(id))out.push([`${label} · Alert`,alertWord(alertOf(id)),alertWord(d.alert)]);
        if(d.email!==(pol.events[id].email==='on'))out.push([`${label} · Email`,word(!d.email),word(d.email)]);}
      if(draft.detail!==pol.lock_screen_detail)out.push(['On the lock screen',DETAILS[level(pol.lock_screen_detail)].label,DETAILS[level(draft.detail)].label]);
      return out;}
    function review(){
      const list=el('ul',{class:'dn-diff'},...changes().map(([what,was,now])=>el('li',{},`${what}: `,el('del',{},was),' → ',el('ins',{},now))));
      const defaults=Object.fromEntries(ids.map(id=>{const d=draft.events[id];
        return [id,{...pol.event_defaults[id],push:d.alert==='off'?'off':'on',email:email?(d.email?'on':'off'):(pol.event_defaults[id]?.email||'inherit'),
          urgency:d.alert==='urgent'?'high':'normal'}];}));
      confirmAction('Save notifications','Applies to the next alert sent.',async reason=>{
        try{await command('notification.policy.update',{department_id:departmentId,push_enabled:draft.push_enabled,lock_screen_detail:draft.detail,
          event_defaults:defaults,...(pol.retry?{retry:pol.retry}:{}),...(pol.delivery?{delivery:pol.delivery}:{}),reason},undefined,revision);}
        catch(e){if(stale(e))return;throw e;}
        notify('Notifications saved.');s=(await pushStatus(true))||st;draw(s);},'Save',list);}  // said first: its message carries the Undo
    // SW3-D-07: the shared history, with Restore (the server's undo to any version, saved as a new one).
    const history=withIcon(button('History',()=>policyHistory(revision,(v,close)=>confirmAction(`Restore version ${v.revision}`,'Its settings come back as a new version.',async reason=>{
      try{await command('notification.policy.undo',{department_id:departmentId,version_id:v.id,reason},undefined,revision);}catch(e){if(stale(e))return;throw e;}
      close();s=(await pushStatus(true))||st;draw(s);notify('Earlier notifications restored.');},'Restore')),'ghost'),'history');
    const undo=revision>1?withIcon(button('Undo last change',()=>confirmAction('Undo the last notifications change','The settings before it come back.',async reason=>{
      const {versions}=await get<{versions:{id:string;previous_id:string|null}[]}>('/api/admin/notifications/policy/history?limit=1&offset=0');
      const before=versions[0]?.previous_id;
      if(!before){notify('There is no earlier version to go back to.','info');return;}
      try{await command('notification.policy.undo',{department_id:departmentId,version_id:before,reason},undefined,revision);}catch(e){if(stale(e))return;throw e;}
      s=(await pushStatus(true))||st;draw(s);notify('Last notifications change undone.');},'Undo'),'ghost'),'undo'):null;
    target.replaceChildren(sectionHead(kindLabel('notifications'),'',history,undo),
      revision>1?el('p',{class:'dn-ver'},`Version ${revision}`):'',conflict,head,
      el('section',{class:'panel dn-panel'},el('div',{class:'np-head'},el('h3',{},'Events'),help('Who hears it follows who an event involves and who may act on it; this page can narrow it, never widen it. Urgent sends at once with a short lifetime; it does not break through Focus or Do Not Disturb.')),
        table(['Event','Who hears it','Alert',...(email?['Email']:[])],rows,'No events.')),
      el('section',{class:'panel dn-panel'},el('div',{class:'np-head'},el('h3',{id:'dn-lock'},'On the lock screen'),help('The most an alert may show before the device is unlocked. People may choose less. No level shows a colleague’s name, a clinic note, a reason or patient data.')),cards),
      saveBar(save,()=>draw(st)));
    target.querySelector('table')?.classList.add('dn-table');
    setLeave(()=>!save.disabled);
  };
  /** A change saved elsewhere since this page was read: say so, with Reload (what was chosen here is not saved). */
  function stale(e:unknown){
    if(!(e instanceof ApiError&&e.code==='STALE_REVISION'))return false;
    const n=note('Someone else changed the notification settings. Reload to see them; your changes here are not saved.','warning');
    n.append(' ',button('Reload',async()=>{conflict.replaceChildren();s=(await pushStatus(true))||s;draw(s!);},'secondary small'));
    conflict.replaceChildren(n);conflict.scrollIntoView({block:'nearest'});return true;}
  draw(s);
}

type PolicyVersion={id:string;revision:number;created_at:string;reason:string;actor_id:string;definition:{push_enabled?:boolean;lock_screen_detail?:string}};
function policyHistory(current:number,restore:(v:PolicyVersion,close:()=>void)=>void){
  historyDialog('Notifications history','Settings',async offset=>{
    const {versions}=await get<{versions:PolicyVersion[]}>(`/api/admin/notifications/policy/history?limit=100&offset=${offset}`);
    return {more:versions.length===100,versions:versions.map(v=>({version:v.revision,saved:formatDate(v.created_at),actor_id:v.actor_id,reason:v.reason,
      value:`${v.definition.push_enabled===false?'Alerts off':'Alerts on'}${v.definition.lock_screen_detail?' · lock screen: '+v.definition.lock_screen_detail.replaceAll('_',' '):''}`,
      restore:v.revision!==current?(close:()=>void)=>restore(v,close):undefined}))};});
}
