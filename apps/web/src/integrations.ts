/** Configuration › Integrations: the GitHub connection that lets an admin turn a feedback
 *  report into a GitHub issue. An admin pastes a token once; the server checks it with GitHub, keeps it encrypted and
 *  never sends it back (only its last four characters). Test, Replace and Disconnect are audited on the server. */
import {brand} from './brand.js';
import {el,button,field,sectionHead,note,notify,armed,badge,ruleFacts,formatDate,formatTime,type Child} from './dom.js';
import {get,command} from './api.js';
import type {GithubIntegrationStatus} from './generated.js';

const NEW_TOKEN='https://github.com/settings/personal-access-tokens/new',TOKENS='https://github.com/settings/personal-access-tokens';
/** A token this close to its expiry date gets a warning to replace it. */
const WARN_DAYS=14;
const DAY=86_400_000;

/** `setLeave` is the page's dirty-close guard: a typed repository or token asks before leaving. The caller scopes it to
 *  this render, so a status that arrives after the admin moved on cannot replace the next page's guard. */
export async function integrationsPage(target:HTMLElement,zone:string,setLeave:(dirty:(()=>boolean)|null)=>void){
  let s=await get<GithubIntegrationStatus>('/api/integrations/github');
  const panel=el('section',{class:'panel integration','aria-labelledby':'integration-github'});
  target.append(sectionHead('Integrations',''),panel);
  const when=(v:string|null)=>v?`${formatDate(v,zone)} ${formatTime(v,zone)}`:'—';
  /** A late answer (Test, Connect, Disconnect) after leaving the page must not redraw it or clear the next page's guard. */
  const apply=(next:GithubIntegrationStatus)=>{s=next;if(panel.isConnected)draw();};
  let replacing=false;

  function draw(){
    setLeave(null);
    const app=s.source==='app',left=s.token_expires_at?Date.parse(s.token_expires_at)-Date.now():null;
    const state=!s.connected?badge('Not connected','neutral'):s.last_test_ok===false?badge('Last check failed','err'):badge(app?'Connected':'Connected from server settings','ok');
    const notices:HTMLElement[]=[];
    // Plain words for admins; the setting's name only for whoever runs the server.
    if(!s.key_configured){const n=note('A GitHub token cannot be saved on this server yet. Ask whoever runs the server to turn on saved secrets.','warning');
      n.append(el('details',{class:'hint'},el('summary',{},'For whoever runs the server'),el('p',{},'Set the encryption key FMUC_DATA_KEY. Until then only server settings (FEEDBACK_GITHUB_TOKEN) can connect GitHub.')));notices.push(n);}
    if(left!==null&&left<=0)notices.push(note(`The token expired on ${formatDate(s.token_expires_at!,zone)}. Replace it to create issues again.`,'error'));
    else if(left!==null&&left<WARN_DAYS*DAY)notices.push(note(`The token expires on ${formatDate(s.token_expires_at!,zone)}. Replace it before then.`,'warning'));
    if(s.last_test_ok===false)notices.push(note('GitHub refused the token or the repository at the last check. Replace the token, or check that Issues is enabled on the repository.','error'));
    const facts:[string,Child][]=s.connected?[
      ['Repository',el('a',{href:`https://github.com/${s.repo}`,target:'_blank',rel:'noopener noreferrer'},s.repo||'')],
      ['Token',el('span',{class:'mono'},s.token_hint?`••••${s.token_hint}`:'••••')],
      ...(app?[['Connected by',`${s.connected_by_name||'—'} · ${when(s.connected_at)}`] as [string,Child],
        ['Last check',s.last_tested_at?`${when(s.last_tested_at)} · ${s.last_test_ok?'worked':'failed'}`:'—'] as [string,Child],
        ['Last issue created',when(s.last_used_at)] as [string,Child],
        ['Expires',s.token_expires_at?formatDate(s.token_expires_at,zone):'Not reported by GitHub'] as [string,Child]]
        :[['Set in','Server settings (FEEDBACK_GITHUB_TOKEN). Connect here to manage it in the app instead.'] as [string,Child]]),
    ]:[];
    const actions=el('div',{class:'form-footer integration-actions'});
    if(app&&!replacing){
      /** One action at a time: while Test or Disconnect waits for GitHub, the other actions (Replace too) stay disabled. */
      const one=(work:()=>Promise<void>)=>async()=>{
        const others=[...actions.querySelectorAll('button')].map(b=>[b,b.disabled] as const);others.forEach(([b])=>b.disabled=true);
        try{await work();}finally{others.forEach(([b,was])=>{if(b.isConnected)b.disabled=was;});}};
      const test=button('Test',one(async()=>{const r=await command<GithubIntegrationStatus>('integration.github.test',{});apply(r);
        notify(r.last_test_ok?'GitHub accepted the token.':'GitHub refused the token. Replace it.',r.last_test_ok?'success':'warning');}),'secondary');
      const replace=button('Replace token…',()=>{replacing=true;draw();panel.querySelector<HTMLInputElement>('input[name=github-token]')?.focus();},'secondary',{disabled:!s.key_configured,why:'No token saved yet.'});
      const disconnect=armed(button('Disconnect',one(async()=>{
        const r=await command<GithubIntegrationStatus>('integration.github.disconnect',{revision:s.revision});apply(r);
        notify(`GitHub disconnected. ${brand().short_name} no longer holds the token; delete it on GitHub too if it is not used elsewhere.`,'success',{label:'Open GitHub tokens',run:()=>window.open(TOKENS,'_blank','noopener')});}),'ghost'),'Disconnect?');
      actions.append(test,replace,disconnect);
    }
    panel.replaceChildren(
      el('div',{class:'integration-head'},el('h3',{id:'integration-github'},'GitHub issues'),state),
      el('p',{class:'muted'},'Developers create a GitHub issue from a feedback report; its text is shown first, without screenshots or names.'),
      ...notices,...(facts.length?[ruleFacts(facts)]:[]),
      ...(app&&!replacing?[actions]:s.key_configured?[connectForm()]:[]));
  }

  /** Connect (or Replace): the repository and a token. The server checks both with GitHub before saving. */
  function connectForm(){
    const app=s.source==='app';
    const repo=field('Repository','text',s.repo||'',{required:true,maxLength:300,placeholder:'owner/repository',autocomplete:'off',spellcheck:'false'});
    const token=field('Token','password','',{required:true,minLength:8,maxLength:16384,autocomplete:'off',name:'github-token',spellcheck:'false'});
    // The steps are needed exactly here, so they are shown (not folded away).
    const help=el('div',{class:'integration-help'},el('h4',{id:'integration-token-steps'},'How to get a token'),
      el('ol',{'aria-labelledby':'integration-token-steps'},
        el('li',{},el('a',{href:NEW_TOKEN,target:'_blank',rel:'noopener noreferrer'},'Generate a fine-grained token'),' on GitHub for the repository’s owner, with access to ',el('strong',{},'only this repository'),'.'),
        el('li',{},'Permissions › Repositories: ',el('strong',{},'Issues'),' — ',el('strong',{},'Read and write'),'; everything else No access.'),
        el('li',{},`Choose an expiry (at most a year); ${brand().short_name} warns two weeks before.`),
        el('li',{},'Paste it here. An organisation owner may need to approve it on GitHub.')));
    const form=el('form',{class:'form-grid integration-form','aria-label':app?'Replace GitHub token':'Connect GitHub'},repo.wrap,token.wrap,el('div',{class:'span-2'},help));
    const cancel=replacing?button('Cancel',()=>{replacing=false;draw();},'ghost'):null;
    const send=button(app?'Replace token':'Connect',async()=>{
      if(!form.reportValidity())return;
      // Cancel and the fields wait for GitHub's answer: that answer redraws the panel, which would drop a newer
      // Replace form or anything typed meanwhile. On a failure they work again with what was sent.
      if(cancel)cancel.disabled=true;
      repo.input.readOnly=token.input.readOnly=true;
      try{
        const r=await command<GithubIntegrationStatus>('integration.github.connect',{repo:repo.input.value.trim(),token:token.input.value},undefined,app?s.revision??undefined:undefined);
        replacing=false;apply(r);notify('GitHub connected. The token was checked with GitHub and saved encrypted.');
      }finally{if(cancel?.isConnected)cancel.disabled=false;repo.input.readOnly=token.input.readOnly=false;}},'primary');
    form.addEventListener('submit',e=>{e.preventDefault();send.click();});
    setLeave(()=>token.input.value!==''||repo.input.value.trim()!==(s.repo||''));
    const footer=el('div',{class:'form-footer'},...(cancel?[cancel]:[]),send);
    return el('div',{},form,footer);
  }
  draw();
}
