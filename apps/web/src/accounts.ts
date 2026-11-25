/** Sign-in accounts: the public invitation / password-reset page, changing your own password, and the admin's
 *  invitation, one-time link and session-limit dialogs (Access & roles). Passwords only ever go to the API. */
import {brandRow,el,field,area,check,select,multi,button,note,formDialog,showDialog,notify,formatDate,sectionHead,withIcon,type Choice,CAPABILITY,roleForCapability,armed,badge,plural,type Tone} from './dom.js';
import {request,command,ApiError} from './api.js';
import {dataTable} from './table_view.js';
import {dockPanel} from './side_panel.js';
import {personName,nameText} from './person_label.js';
import {csvText,download as save} from './schedule_export.js';
import {ACCOUNT_ONBOARDING_LIMITS,type AccountOnboarding,type AccountOnboardingApply} from './generated.js';
import {accountSetUp} from './account_memory.js';
import type {Row} from './types.js';

/** A rough, local guide to how guessable a password is (the server enforces the actual rules). */
export function strength(password:string):{label:string;tone:string}{
  const classes=[/[a-z]/,/[A-Z]/,/[0-9]/,/[^A-Za-z0-9]/].filter(r=>r.test(password)).length;
  const score=(password.length>=12?1:0)+(password.length>=16?1:0)+(password.length>=20?1:0)+(classes>=3?1:0)+(new Set(password).size>=8?1:0);
  return password.length<12?{label:'Too short: use at least 12 characters',tone:'error'}:score<=2?{label:'Fair: a longer phrase is harder to guess',tone:'warning'}:{label:'Strong',tone:'success'};
}

/** New password + confirmation with a live strength hint; `problems()` is what still blocks saving. */
function newPassword(username:string,email=''){
  const first=field('New password','password','',{required:true,minLength:12,maxLength:256,autocomplete:'new-password'});
  const second=field('Repeat new password','password','',{required:true,autocomplete:'new-password'});
  const hint=el('small',{class:'hint','aria-live':'polite'},'At least 12 characters. A few unrelated words make a strong password.');
  first.wrap.append(hint);
  const problems=()=>{const p=first.input.value,low=p.toLowerCase();
    if(p.length<12)return 'Use at least 12 characters.';
    if([username,email,email.split('@')[0]].filter(Boolean).some(x=>x.toLowerCase()===low))return 'Do not use your sign-in name or email address as the password.';
    if(p!==second.input.value)return 'The two passwords differ.';
    return '';};
  const sync=()=>{const s=strength(first.input.value);hint.textContent=first.input.value?s.label:'At least 12 characters. A few unrelated words make a strong password.';hint.className='hint '+(first.input.value?s.tone:'');
    second.input.setCustomValidity(second.input.value&&second.input.value!==first.input.value?'The two passwords differ.':'');};
  first.input.addEventListener('input',sync);second.input.addEventListener('input',sync);
  return {first,second,problems};
}

/** The page behind /invite/<token> and /reset/<token>: no session needed; the link itself is the credential. */
export async function accountLinkPage(root:Element,token:string){
  const box=el('div',{class:'login-form'},brandRow());
  root.replaceChildren(el('main',{id:'main',class:'login-layout'},el('section',{class:'login-form-wrap'},box)));
  const toSignIn=el('a',{class:'button secondary',href:'/'},'Go to sign-in');
  let info:{purpose:string;display_name:string;username:string;expires_at:string};
  try{info=await request(`/api/account-links/${encodeURIComponent(token)}`);}
  catch(e){box.append(el('h1',{},'This link cannot be used'),note((e as Error).message,'error',e),toSignIn);return;}
  const invite=info.purpose==='invite';
  box.append(el('h1',{},invite?`Welcome, ${info.display_name}`:'Choose a new password'),
    el('p',{},invite?'Set a password to finish setting up your account.':`For ${info.display_name}.`),
    el('p',{class:'muted'},'Your sign-in name is ',el('strong',{},info.username),`. This link works once and expires ${formatDate(info.expires_at)}.`));
  const form=el('form',{}),error=el('div',{'aria-live':'assertive'}),submit=el('button',{type:'submit',class:'button primary'},invite?'Set password':'Save new password');
  // A hidden username lets password managers store the new password under the right name.
  const user=el('input',{type:'text',name:'username',autocomplete:'username',value:info.username,hidden:true,readonly:true});
  const pw=newPassword(info.username);
  form.append(user,pw.first.wrap,pw.second.wrap,error,submit);box.append(form);
  form.addEventListener('submit',async e=>{e.preventDefault();error.replaceChildren();
    const problem=pw.problems();if(problem){error.append(note(problem,'error'));return;}
    submit.disabled=true;submit.textContent='Saving…';
    try{const result=await request<{sign_in:string}>(`/api/account-links/${encodeURIComponent(token)}`,{method:'POST',body:JSON.stringify({password:pw.first.input.value})});
      accountSetUp(info.display_name,info.username);  // sign-in offers this account, not the one used here before (maintenance)
      form.replaceWith(note(invite?'Your password is set. Taking you to sign-in…':'Your password is changed and your other sessions were signed out. Taking you to sign-in…','success'));
      window.setTimeout(()=>location.replace(result.sign_in||'/'),1200);}
    catch(err){error.append(note((err as Error).message,'error',err));submit.disabled=false;submit.textContent=invite?'Set password':'Save new password';}});
  pw.first.input.focus();
}

export function changePasswordDialog(username:string){
  formDialog('Change password','Your other sessions are signed out. This session stays signed in.',form=>{
    const current=field('Current password','password','',{required:true,autocomplete:'current-password'});
    const user=el('input',{type:'text',name:'username',autocomplete:'username',value:username,hidden:true,readonly:true});
    const pw=newPassword(username);form.append(user,current.wrap,pw.first.wrap,pw.second.wrap);
    return async()=>{const problem=pw.problems();if(problem)throw new Error(problem);
      const r=await request<{sessions_ended:number}>('/api/account/password',{method:'POST',body:JSON.stringify({current_password:current.input.value,new_password:pw.first.input.value})});
      notify('Password changed.'+(r.sessions_ended?` ${r.sessions_ended} other session${r.sessions_ended===1?' was':'s were'} signed out.`:''));};
  },'Change password');
}

/** Show a one-time link once, ready to copy. The app never shows it again (only its expiry). */
export function linkDialog(title:string,who:string,result:{one_time_link?:string;link?:{expires_at:string}}){
  const link=result.one_time_link;
  showDialog(title,link?`Send this link to ${who} yourself (for example by hospital email or in person). It works once and expires ${formatDate(result.link?.expires_at||'')}. Anyone with the link can set the password, so do not post it in shared channels.`:'The link was created, but this repeated request cannot show it again. Use Resend to create a new one.',(body,close)=>{
    if(!link)return;
    const input=el('input',{type:'text',readonly:true,value:link,'aria-label':'One-time link',class:'mono'});
    const copy=button('Copy link',async()=>{try{await navigator.clipboard.writeText(link);notify('Link copied.');}catch{input.select();notify('Press Ctrl+C to copy the selected link.','info');}},'primary');
    // The link is the credential: never in a feedback screenshot.
    body.append(el('div',{class:'field span-2','data-private':''},input),el('div',{class:'form-footer'},button('Done',close,'secondary'),copy));
    input.addEventListener('focus',()=>input.select());
  });
}

/** `roles`: every role, those this person may not give disabled with why (role_grants.ts, maintenance). The invitation
 *  grants roles, so it takes a reason like every other grant (maintenance): the access history records it. */
export function inviteDialog(people:Choice[],refresh:()=>Promise<void>,roles:Choice[]){
  formDialog('Invite a person','They get a sign-in to this app. You give them a one-time link; they choose their own password. You never see or set it.',form=>{
    const name=field('Name shown in the app','text','',{required:true,maxLength:160});
    const email=field('Email','email','',{required:true,maxLength:254,autocomplete:'off'});
    const username=field('Sign-in name','text','',{required:true,pattern:'[a-z0-9][a-z0-9._\\-]{1,62}',title:'2–63 lowercase letters, digits, dot, dash or underscore',autocomplete:'off'});
    username.wrap.append(el('small',{class:'hint'},'What they type to sign in. Lowercase letters, digits, “.”, “-” or “_”.'));
    let edited=false;username.input.addEventListener('input',()=>{edited=true;});
    email.input.addEventListener('input',()=>{if(!edited)username.input.value=usernameFrom(email.input.value);});
    const person=select('Their staff profile',people,'',false,'None (not on rotas)');
    const picked=multi('Access roles',roles,[roleForCapability(CAPABILITY.STAFF)]),reason=area('Reason');reason.input.required=true;
    form.append(name.wrap,email.wrap,username.wrap,person.wrap,picked.wrap,reason.wrap);
    return async()=>{
      const result=await command('account.invite',{display_name:name.input.value,email:email.input.value.trim(),username:username.input.value,person_id:person.input.value||null,roles:picked.values(),reason:reason.input.value.trim()});
      await refresh();linkDialog('Invitation link',name.input.value,result);
    };
  },'Create invitation');
}

/** A sign-in name from an email's local part, within the Username contract; '' when too little is left. */
export const usernameFrom=(email:string)=>{
  const name=email.split('@')[0].toLowerCase().replace(/[^a-z0-9._-]/g,'').replace(/^[._-]+/,'').slice(0,63);
  return name.length>=2?name:'';};

/** The private list an onboarding apply returns, as a spreadsheet: each person and their one-time link. */
export const invitationsCsv=(rows:{person_label:string;one_time_link:string}[])=>
  '\uFEFF'+csvText([['Person','One-time link'],...rows.map(r=>[r.person_label,r.one_time_link])]);

const WAVE=Math.min(ACCOUNT_ONBOARDING_LIMITS.rows,ACCOUNT_ONBOARDING_LIMITS.wave);
const RESULT:Record<string,[string,Tone]>={ready:['Gets an account','info'],already_linked:['Already has an account','neutral'],
  created:['Account created','ok'],conflict:['Conflict','err'],stopped:['Not created','warn']};
const CONFLICT:Record<string,string>={PERSON_NOT_IN_DEPARTMENT:'Not in this department',
  DUPLICATE_SELECTION:'Same sign-in name or email as another selected person',PERSON_IDENTITY_CONFLICT:'Already has a different sign-in',
  SIGN_IN_TAKEN:'Sign-in name or email already in use',INVITATION_RECEIPT_STALE:'Invitation not confirmed'};
const COUNTED:Record<keyof Counts,string>={ready:'to create',already_linked:'already set up',created:'created',conflict:'with a conflict',stopped:'not created'};
type Ledger=Record<string,{status:string;code?:string}>;
type Counts={ready:number;already_linked:number;created:number;conflict:number;stopped:number};
type Preview={expected_fingerprint:string;people:Record<string,string>;ledger:Ledger;counts:Counts};
type Applied={ledger:Ledger;counts:Counts;invitations:{person_id:string;person_label:string;one_time_link:string}[]};
const post=<T>(path:string,body:AccountOnboarding|AccountOnboardingApply)=>request<T>(path,{method:'POST',body:JSON.stringify(body)});

/** Onboard staff (Access & roles): pick people without a sign-in, preview who gets an account, create them with a
 *  same-button confirm, and save their one-time links once as a file. The links are never drawn, stored or logged. */
export function onboardingPanel(people:Row[],linked:Set<string>,refresh:(quiet?:boolean)=>Promise<void>){
  const open=people.filter(p=>p.active!==false&&!linked.has(p.id));
  const email=(p:Row)=>String(p.work_email||'');
  let changed=false;
  dockPanel({key:'staff-onboarding',title:'Onboard staff',width:720,onClose:()=>{if(changed)void refresh(true);},build:({body,close})=>{
    const pick=()=>{const msg=el('div',{'aria-live':'polite'});
      body.replaceChildren(msg,dataTable<Row>({id:'staff-onboarding',noun:['person','people'],rows:open,rowId:p=>p.id,
        searchLabel:'Search people',searchPlaceholder:'Name or email',empty:['Everyone has an account',''],
        selectable:p=>!!usernameFrom(email(p)),rowLabel:p=>personName(p.id),
        columns:[{key:'person',label:'Person',text:p=>personName(p.id),cell:p=>nameText(p.id)},
          {key:'email',label:'Work email',text:email,facet:p=>usernameFrom(email(p))?'Has a work email':'No work email',
            cell:p=>email(p)||el('span',{class:'muted'},'No work email')}],
        bulk:()=>[{label:'Preview',icon:'eye',primary:true,run:(ids:string[])=>{
          if(ids.length>WAVE){msg.replaceChildren(note(`Select up to ${WAVE} people per batch.`,'error'));return;}
          const rows=open.filter(p=>ids.includes(p.id)).map(p=>({person_id:p.id,username:usernameFrom(email(p)),email:email(p)}));
          return review({rows,reason:'Staff onboarding'});}}]}).root);};
    // Who the batch reaches, in the server's words for each person.
    const outcome=(labels:Record<string,string>,ledger:Ledger)=>dataTable<[string,{status:string;code?:string}]>({id:'staff-onboarding-result',
      noun:['person','people'],rows:Object.entries(ledger),rowId:([id])=>id,searchLabel:'Search people',searchPlaceholder:'Name',
      columns:[{key:'person',label:'Person',text:([id])=>personName(id,labels[id]),cell:([id])=>nameText(id,labels[id])},
        {key:'result',label:'Result',facet:([,r])=>(RESULT[r.status]||[r.status])[0],cell:([,r])=>el('div',{},badge(...(RESULT[r.status]||[r.status,'neutral'] as [string,Tone])),
          r.code?el('div',{class:'muted cell-note'},CONFLICT[r.code]||r.code.replaceAll('_',' ').toLowerCase()):'')}]}).root;
    const summary=(c:Counts)=>el('div',{class:'import-summary',role:'status'},...(Object.keys(COUNTED) as (keyof Counts)[]).filter(k=>c[k])
      .map(k=>el('span',{class:'import-stat'+(k==='conflict'?' error':'')},el('b',{},String(c[k])),' '+COUNTED[k])));
    const review=async(selection:AccountOnboarding,said:Node|string='')=>{
      const view=await post<Preview>('/api/account-onboarding/preview',selection),c=view.counts;
      const create=c.ready&&!c.conflict?armed(button(`Create ${plural(c.ready,'account')}`,async()=>{
        const done=await post<Applied>('/api/account-onboarding/apply',{...selection,expected_fingerprint:view.expected_fingerprint})
          .catch((e:unknown)=>{if(e instanceof ApiError&&e.code==='STALE_PREVIEW')return null;throw e;});
        if(!done){await review(selection,note('Accounts or people changed since this preview, so nothing was created. Check it again.','warning'));return;}
        changed=true;
        // The links leave only as this file, once; nothing here keeps them.
        if(done.invitations.length)save(new Blob([invitationsCsv(done.invitations)],{type:'text/csv'}),'staff-invitations.csv');
        const doneButton=button('Done',async()=>{
          changed=false;
          // Keep the result open until Access can offer actions for the new accounts.
          await refresh();
          if(body.isConnected)close();},'secondary');
        body.replaceChildren(el('div',{class:'import-head'},summary(done.counts),doneButton),
          done.invitations.length?note(`Links downloaded for ${plural(done.invitations.length,'person','people')}.`,'success'):'',
          outcome(view.people,done.ledger));},'primary')):null;
      body.replaceChildren(el('div',{class:'import-head'},summary(c),el('div',{class:'actions'},button('Back',pick,'secondary'),...(create?[create]:[]))),said,
        c.conflict?note('Clear the people with a conflict, then preview again.','error'):'',outcome(view.people,view.ledger));};
    pick();
  }});
}

/** Session limits for everyone in the department (Configuration › Department settings). */
export function sessionsPanel(department:Row,sessions:{idle_minutes:number;max_hours:number;idle_unlimited:boolean;max_unlimited:boolean;inherited:{idle_minutes:boolean;max_hours:boolean};defaults:{idle_minutes:number;max_hours:number};limits:{idle_minutes:{min:number;max:number};max_hours:{min:number;max:number}}},saved:(result:Row)=>Promise<void>){
  const hours=(n:number)=>`${n} hour${n===1?'':'s'}`,minutes=(n:number)=>n%60===0?hours(n/60):`${n} minute${n===1?'':'s'}`;
  const unlimitedWarning='Unlimited sessions increase the risk of access from an unattended device or a stolen session token. Sign out on shared devices; administrators can still revoke sessions.';
  const edit=button('Edit session settings',()=>formDialog('Session settings','Applies to everyone in the department from their next action. Shorter limits also shorten sessions that are open now.',form=>{
    const {idle_minutes:idleLimit,max_hours:maxLimit}=sessions.limits;
    const legacyIdle=sessions.inherited.idle_minutes&&sessions.idle_minutes<idleLimit.min;
    const idle=field('Sign out after inactivity (minutes)','number',sessions.idle_minutes,{required:true,min:legacyIdle?sessions.idle_minutes:idleLimit.min,max:idleLimit.max,step:1});
    const max=field('Longest session (hours)','number',sessions.max_hours,{required:true,min:maxLimit.min,max:maxLimit.max,step:1});
    idle.wrap.append(el('small',{class:'hint'},legacyIdle?`Inherited deployment setting: ${minutes(sessions.idle_minutes)}. Keep it unchanged or choose at least ${idleLimit.min} minutes.`
      :`At least ${idleLimit.min} minutes. Default ${sessions.defaults.idle_minutes}.`));
    const newIdleError=()=>Number(idle.input.value)<idleLimit.min&&!(legacyIdle&&Number(idle.input.value)===sessions.idle_minutes)
      ?`New inactivity limits must be at least ${idleLimit.min} minutes.`:'';
    idle.input.addEventListener('input',()=>idle.input.setCustomValidity(newIdleError()));
    max.wrap.append(el('small',{class:'hint'},`At least ${hours(maxLimit.min)}, counted from sign-in. Default ${sessions.defaults.max_hours}.`));
    const idleUnlimited=check('No inactivity limit',sessions.idle_unlimited);
    const maxUnlimited=check('No maximum session length',sessions.max_unlimited);
    const warning=note(unlimitedWarning,'warning');warning.classList.add('span-2');
    const sync=()=>{idle.input.disabled=idleUnlimited.input.checked;max.input.disabled=maxUnlimited.input.checked;
      warning.hidden=!idleUnlimited.input.checked&&!maxUnlimited.input.checked;};
    idleUnlimited.input.addEventListener('change',sync);maxUnlimited.input.addEventListener('change',sync);
    form.append(idle.wrap,max.wrap,idleUnlimited.wrap,maxUnlimited.wrap,warning);sync();
    return async()=>{const iu=idleUnlimited.input.checked,mu=maxUnlimited.input.checked;
      const i=iu?sessions.idle_minutes:Number(idle.input.value),m=mu?sessions.max_hours:Number(max.input.value);
      if(!iu&&newIdleError())throw new Error(newIdleError());
      if(!iu&&!mu&&i>m*60)throw new Error('The inactivity limit cannot be longer than the longest session.');
      const result=await command('department.sessions',{idle_minutes:sessions.inherited.idle_minutes&&i===sessions.idle_minutes?null:i,
        max_hours:sessions.inherited.max_hours&&m===sessions.max_hours?null:m,idle_unlimited:iu,max_unlimited:mu,reason:'Session settings'},department.id,department.revision);notify('Session settings saved.');await saved(result);};
  }),'secondary');
  const description=sessions.idle_unlimited?(sessions.max_unlimited?'No automatic sign-out for inactivity or session length.':`No inactivity limit; signed out ${hours(sessions.max_hours)} after signing in at the latest.`)
    :`Signed out after ${minutes(sessions.idle_minutes)} without activity${sessions.max_unlimited?'; no maximum session length.':`, and ${hours(sessions.max_hours)} after signing in at the latest.`}`;
  return el('section',{class:'panel'},sectionHead('Sign-in sessions',description,withIcon(edit,'clock')),
    sessions.idle_unlimited||sessions.max_unlimited?note(unlimitedWarning,'warning'):null);
}
