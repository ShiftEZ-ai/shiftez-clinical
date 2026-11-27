/** Configuration › Sign-in: how people sign in on this server — Rota passwords, the
 *  organisation's institutional provider, or both. One environment-wide setting on /api/admin/auth-config: saving takes a
 *  reason, a same-button confirm and the revision it was read at (a change made elsewhere meanwhile is refused, never
 *  overwritten); one step can be undone; the server audits every change. The client secret is write-only. */
import {el,button,iconButton,field,check,select,sectionHead,note,notify,badge,ruleFacts,saveBar,confirmAction,byLabel,type Child} from './dom.js';
import {TERMS} from './terms.js';
import {get,request,ApiError,sessionChanged} from './api.js';
import {kindLabel} from './config_kinds.js';
import type {AuthConfigStatus,AuthConnectionResult,AuthInstitutionalBinding} from './generated.js';

type Mode=NonNullable<AuthConfigStatus['mode']>;
const MODES:{value:Mode;label:string}[]=[
  {value:'password_only',label:'Password only'},{value:'institutional_only',label:'Institutional only'},{value:'both',label:'Password and institutional'}];
const list=(text:string)=>text.split(/[\s,]+/).filter(Boolean);
const put=(path:string,body:unknown,method='PUT')=>request<AuthConfigStatus>(path,{method,body:JSON.stringify(body)});

/** `setLeave` is the page's dirty-close guard, scoped by the caller to this render. */
export async function signInConfigPage(target:HTMLElement,setLeave:(dirty:(()=>boolean)|null)=>void){
  let s=await get<AuthConfigStatus>('/api/admin/auth-config');
  // People with a sign-in here, for linking them to their institutional subject. Unread: links are shown by ID only.
  const identities=await get<{identities?:{id:string;display_name?:string}[]}>('/api/access').then(a=>a.identities||[]).catch(e=>{if(sessionChanged(e))throw e;return [];});
  // The page draws straight into its Configuration pane, like every section: the shared head is the pane's own.
  const host=target;

  function draw(){
    setLeave(null);
    const conflict=el('div',{});
    const enabled=check('Institutional sign-in',!!s.institutional_enabled);
    const mode=select('Sign-in method',MODES,s.mode||'password_only');
    const issuer=field('Issuer','url',s.issuer||'',{spellcheck:false,autocomplete:'off'});
    const discovery=field('Discovery URL','url',s.discovery_url||'',{spellcheck:false,autocomplete:'off'});
    const client=field('Client ID','text',s.client_id||'',{maxLength:255,spellcheck:false,autocomplete:'off'});
    // Write-only: never filled in. Typed replaces; left blank keeps; "Remove saved secret" clears.
    const secret=field('Client secret','password','',{autocomplete:'new-password',disabled:!s.key_configured});
    const clearSecret=s.client_secret_set?check('Remove saved secret'):null;
    const scopes=field('Scopes','text',(s.scopes||['openid','profile','email']).join(' '),{spellcheck:false,autocomplete:'off'});
    const userClaim=field('Username claim','text',s.username_claim||'preferred_username',{spellcheck:false,autocomplete:'off'});
    const emailClaim=field('Email claim','text',s.email_claim||'email',{spellcheck:false,autocomplete:'off'});
    const groupsClaim=field('Groups claim','text',s.groups_claim||'groups',{spellcheck:false,autocomplete:'off'});
    const domains=field('Allowed email domains','text',(s.allowed_domains||[]).join(', '),{spellcheck:false,autocomplete:'off'});
    const hosts=field('Allowed provider hosts','text',(s.allowed_hosts||[]).join(', '),{spellcheck:false,autocomplete:'off'});

    // Linked accounts: an explicit person ↔ institutional subject pair; nothing is matched by email or name.
    const people=identities.map(i=>({value:i.id,label:i.display_name||i.id})).sort(byLabel);
    for(const b of s.institutional_bindings||[])if(!people.some(p=>p.value===b.identity_id))people.push({value:b.identity_id,label:b.identity_id});
    // Links are bound to an issuer: a saved link whose issuer is not the one above is confirmed for it explicitly
    // (a new link takes the issuer above).
    const rows=el('div',{class:'sign-in-links'}),links=new Map<HTMLElement,{saved:string|null;confirm:ReturnType<typeof check>}>();
    const addRow=(b?:AuthInstitutionalBinding)=>{
      const who=select('Person',people,b?.identity_id||''),subject=field('Institutional subject','text',b?.subject||'',{required:true,maxLength:255,spellcheck:false,autocomplete:'off'});
      const saved=b?b.issuer||'':null,confirm=check('Confirm for the new issuer');
      const row=el('div',{class:'sign-in-link'},who.wrap,subject.wrap);
      row.append(iconButton('Remove link','trash',()=>{row.remove();links.delete(row);changed();}));
      if(saved!==null)row.append(el('div',{class:'sign-in-link-issuer'},el('small',{class:'muted'},'Issuer: ',el('code',{class:'mono'},saved||'none')),confirm.wrap));
      links.set(row,{saved,confirm});rows.append(row);return row;};
    (s.institutional_bindings||[]).forEach(b=>addRow(b));
    const addLink=button('Add link',()=>{addRow().querySelector('select')?.focus();changed();},'secondary small');

    const copy=iconButton('Copy redirect URI','copy',async()=>{try{await navigator.clipboard.writeText(s.redirect_uri);notify('Redirect URI copied.');}catch{notify('Copying is blocked here. The redirect URI is '+s.redirect_uri,'info');}});
    const facts:[string,Child][]=[
      ['Redirect URI',el('span',{class:'sign-in-uri'},el('code',{class:'mono'},s.redirect_uri),copy)],
      ['Client secret',s.client_secret_set?badge('Set','ok'):badge('Not set','neutral')],
      ['Encryption key',s.key_configured?badge('Configured','ok'):badge('Not configured','warn')]];
    const result=el('div',{'aria-live':'polite'});
    const test=button('Test connection',async()=>{result.replaceChildren();
      try{const {ok,message}=await request<AuthConnectionResult>('/api/admin/auth-config/test-connection',{method:'POST',body:JSON.stringify({expected_revision:s.revision})});
        result.replaceChildren(note(message,ok?'success':'error'));}
      catch(e){if(sessionChanged(e)||stale(e))return;result.replaceChildren(note((e as Error).message,'error',e));}},'secondary',
      {disabled:!s.issuer||!s.client_id,why:'Save an issuer and client ID first.'});
    const institutional=el('section',{class:'panel','aria-labelledby':'sign-in-institutional'},
      el('h3',{id:'sign-in-institutional'},'Institutional provider'),
      ...(!s.key_configured?[note('The client secret cannot be saved until whoever runs the server sets FMUC_DATA_KEY.','warning')]:[]),
      ruleFacts(facts),
      el('div',{class:'form-grid'},issuer.wrap,discovery.wrap,client.wrap,secret.wrap,...(clearSecret?[clearSecret.wrap]:[]),scopes.wrap,userClaim.wrap,emailClaim.wrap,groupsClaim.wrap,domains.wrap,hosts.wrap),
      el('h4',{},'Linked accounts'),rows,el('div',{class:'actions'},addLink),
      el('div',{class:'actions'},test),result);

    const rememberDays=field('Remember me for (days)','number',s.remember_me_days??30,{required:true,min:1,max:90,step:1});
    const form=el('form',{class:'settings-form',novalidate:true},el('section',{class:'panel'},el('div',{class:'form-grid'},enabled.wrap,mode.wrap,rememberDays.wrap)),institutional);
    const save=button('Save',()=>{if(!form.reportValidity())return;
      confirmAction('Save sign-in settings','Takes effect for the next sign-in.',async reason=>{
        try{apply(await put('/api/admin/auth-config',{expected_revision:s.revision,confirmed:true,reason,...values()}),'Sign-in settings saved.');}
        catch(e){if(!stale(e))throw e;}},'Save',undefined,false,true);},'primary',{disabled:true,why:TERMS.nothingChanged});
    const undo=s.can_undo?button('Undo last change',()=>confirmAction('Undo the last sign-in change','The settings before it come back, with the client secret they had.',async reason=>{
      try{apply(await put('/api/admin/auth-config/undo',{expected_revision:s.revision,confirmed:true,reason},'POST'),'Last sign-in change undone.');}
      catch(e){if(!stale(e))throw e;}},'Undo',undefined,false,true),'secondary'):null;

    function values(){
      const on=enabled.input.checked;
      return {institutional_enabled:on,mode:on?mode.input.value as Mode:'password_only' as Mode,remember_me_days:Number(rememberDays.input.value),
        issuer:issuer.input.value.trim(),discovery_url:discovery.input.value.trim(),client_id:client.input.value.trim(),
        scopes:list(scopes.input.value),username_claim:userClaim.input.value.trim(),email_claim:emailClaim.input.value.trim(),groups_claim:groupsClaim.input.value.trim(),
        allowed_domains:list(domains.input.value),allowed_hosts:list(hosts.input.value),
        institutional_bindings:[...links].map(([r,{saved,confirm}])=>({identity_id:r.querySelector('select')!.value,subject:r.querySelector<HTMLInputElement>('input[type=text]')!.value.trim(),
          issuer:saved===null||confirm.input.checked?issuer.input.value.trim():saved})),
        // Omitted keeps the saved secret; an empty string clears it.
        ...(clearSecret?.input.checked?{client_secret:''}:secret.input.value?{client_secret:secret.input.value}:{})};
    }
    /** What shows: the provider only once institutional sign-in is on; the method only then (else password only). */
    function sync(){const on=enabled.input.checked;institutional.hidden=!on;mode.wrap.hidden=!on;
      for(const f of [issuer,client])f.input.required=on;
      for(const {saved,confirm} of links.values()){const differs=saved!==null&&saved!==issuer.input.value.trim();confirm.wrap.hidden=!differs;confirm.input.required=on&&differs;}secret.input.disabled=!s.key_configured||!!clearSecret?.input.checked;}
    function changed(){save.disabled=false;test.disabled=true;test.dataset.tip='Save first: the test uses the saved settings.';sync();}
    form.addEventListener('input',changed);form.addEventListener('change',changed);
    form.addEventListener('submit',e=>{e.preventDefault();save.click();});
    setLeave(()=>!save.disabled);sync();
    host.replaceChildren(sectionHead(kindLabel('sign_in'),'',undo),conflict,form,saveBar(save,()=>draw()));

    /** A change saved elsewhere since this page was read: say so, with Reload (which drops what was typed here). */
    function stale(e:unknown){
      if(!(e instanceof ApiError&&e.code==='STALE_REVISION'))return false;
      const n=note('Someone else changed the sign-in settings. Reload to see them; what you typed here is not saved.','warning');
      n.append(' ',button('Reload',async()=>{s=await get<AuthConfigStatus>('/api/admin/auth-config');draw();},'secondary small'));
      conflict.replaceChildren(n);conflict.scrollIntoView({block:'nearest'});return true;}
  }
  function apply(next:AuthConfigStatus,message:string){s=next;if(host.isConnected)draw();notify(message);}
  draw();
}
