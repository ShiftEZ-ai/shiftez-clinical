/** Configuration › AI provider (Developers, CAPABILITIES['ai.providers']; PROPOSAL.md §2 and §4): whether rota files may be
 *  read with AI, and the provider profiles that read them. The API key is write-only: the server keeps it encrypted and
 *  answers only whether one is saved. Connection tests are queued, audited jobs. Every call goes through scoreCheckApi.ts. */
import {el,button,duration,field,select,check,sectionHead,notify,badge,formDialog,table,formatDate,formatTime,empty,withIcon} from './dom.js';
import * as api from './scoreCheckApi.js';

export async function aiProviderPage(target:HTMLElement,zone:string){
  let [s,calls]=await Promise.all([api.readAiSettings(),api.readAiCalls()]);
  const body=el('div',{class:'ai-provider'});
  const add=withIcon(button('Add provider',()=>edit(null),'primary'),'plus');
  target.append(sectionHead('AI provider','',add),body);
  // Recent calls read again after a change or a finished Test (its call is recorded); only the newest read draws.
  let callsRead=0;
  const reloadCalls=()=>{const mine=++callsRead;void api.readAiCalls().then(c=>{if(mine!==callsRead)return;calls=c;if(body.isConnected)draw();}).catch(()=>{});};
  const apply=(next:api.AiSettings)=>{s=next;if(body.isConnected)draw();reloadCalls();};
  function draw(){
    const onOff=check('Read rota files with AI',s.enabled),toggle=onOff.input;toggle.setAttribute('role','switch');
    toggle.addEventListener('change',async()=>{toggle.disabled=true;try{apply(await api.setAiEnabled(s,toggle.checked));notify(toggle.checked?'Reading with AI is on.':'Reading with AI is off.');}
      catch(e){toggle.checked=!toggle.checked;notify((e as Error).message,'error',undefined,e);}finally{toggle.disabled=false;}});
    const rows=s.profiles.map(p=>[
      el('span',{class:'ai-name'},el('strong',{},p.name),p.is_default?badge('Default','draft'):null,p.enabled?null:badge('Off','neutral')),
      api.PROVIDER_KINDS.find(k=>k.value===p.kind)?.label||p.kind,
      el('span',{class:'mono'},p.model),
      el('span',{class:'muted'},api.sendsTo(p)),
      p.key_set?`Saved ${p.key_saved_at?formatDate(p.key_saved_at,zone):''}`:badge('No key','warn'),
      el('div',{class:'actions'},
        button('Test',async()=>{const r=await api.testProfile(p,()=>body.isConnected);if(!r)return;reloadCalls();notify(r.ok?`${p.name} answered.`:`${p.name} did not answer${r.message?': '+r.message:'.'}`,r.ok?'success':'warning');},'small'),
        button('Edit',()=>edit(p),'small'),
        button('Delete',async()=>{apply(await api.deleteProfile(p));notify(p.name+' deleted.');},'ghost small'))]);
    body.replaceChildren(
      el('div',{class:'switch-group ai-switch'},onOff.wrap),
      s.profiles.length?table(['Name','Kind','Model','Data','API key',''],rows)
        :empty('No AI provider yet','Without one, CSV and Excel files in the standard layout are read without AI.',null,'sparkle'),
      ...(calls.length?[el('h3',{class:'ai-calls-title'},'Recent AI calls'),table(['When','Provider','Model','Tokens in / out','Time','Result'],calls.map(c=>[
        `${formatDate(c.created_at,zone)} ${formatTime(c.created_at,zone)}`,s.profiles.find(p=>p.id===c.profile_id)?.name||'—',el('span',{class:'mono'},c.model),
        c.input_tokens===null||c.input_tokens===undefined?'—':`${c.input_tokens} / ${c.output_tokens??'—'}`,
        c.duration_ms===null||c.duration_ms===undefined?'—':duration(c.duration_ms/1000),
        badge({valid:'Valid',invalid:'Invalid reply',error:'Error',pending:'Waiting'}[c.result],c.result==='valid'?'ok':c.result==='pending'?'neutral':'err')]))]:[]));
  }
  function edit(p:api.AiProviderProfileRecord|null){
    formDialog(p?'Edit '+p.name:'Add provider','',form=>{
      const name=field('Name','text',p?.name||'',{required:true,maxLength:160});
      const kind=select('Kind',api.PROVIDER_KINDS,p?.kind||'anthropic');
      const url=field('Base URL','url',p?.base_url||'https://api.anthropic.com',{required:true,maxLength:2000});
      const model=field('Model','text',p?.model||'',{required:true,maxLength:200,spellcheck:'false'});
      const style=select('API style',[{value:'chat',label:'Chat completions'},{value:'responses',label:'Responses'}],p?.api_style||'chat');
      // A token budget is not accepted by any adapter: an older profile that has one is said once and saved with a level.
      const reasoning=select('Thinking',api.REASONING,p?.reasoning&&p.reasoning!=='budget'?p.reasoning:'none');
      const oldBudget=p?.reasoning==='budget'?el('p',{class:'muted span-2'},'This provider used a token budget, which is no longer supported. Choose a thinking level.'):null;
      const tokens=field('Most output tokens','number',p?.max_tokens??4096,{required:true,min:1,max:1000000,step:1});
      const timeout=field('Time limit (seconds)','number',p?.timeout_s??120,{required:true,min:1,max:600});
      const key=field(p?.key_set?'Replace API key':'API key','password','',{autocomplete:'new-password',spellcheck:'false',maxLength:10000});
      const def=check('Default for new imports',p?.is_default??!s.profiles.length),on=check('On',p?.enabled??true);
      // A provider on the organisation network is an explicit choice (the server checks the address); never guessed from the URL.
      const onSite=check('On the organisation network',p?.allow_private_endpoint??false);
      // Only what the chosen kind uses is shown: the API style for OpenAI-compatible and Gemini.
      const sync=()=>{style.wrap.hidden=!['openai_compatible','gemini'].includes(kind.input.value);
        notice.textContent=api.sendsTo({kind:kind.input.value as api.ProviderKind,base_url:url.input.value});};
      const notice=el('p',{class:'muted span-2'});
      kind.input.addEventListener('change',sync);url.input.addEventListener('input',sync);sync();
      form.append(name.wrap,kind.wrap,url.wrap,model.wrap,style.wrap,reasoning.wrap,...(oldBudget?[oldBudget]:[]),tokens.wrap,timeout.wrap,key.wrap,el('div',{},def.wrap,on.wrap,onSite.wrap),notice);
      return async()=>{const reasoningValue=reasoning.input.value as api.Reasoning;
        apply(await api.saveProfile(p,{name:name.input.value.trim(),kind:kind.input.value as api.ProviderKind,base_url:url.input.value.trim(),
          model:model.input.value.trim(),reasoning:reasoningValue,thinking_budget:null,structured_output:p?.structured_output??'json_schema',
          api_style:style.input.value as 'chat'|'responses',max_tokens:Number(tokens.input.value),timeout_s:Number(timeout.input.value),
          allow_private_endpoint:onSite.input.checked,is_default:def.input.checked,enabled:on.input.checked,
          ...(key.input.value?{api_key:key.input.value}:{})}));
        notify((p?'Saved ':'Added ')+name.input.value.trim()+'.');};
    },p?'Save':'Add provider');
  }
  draw();
}
