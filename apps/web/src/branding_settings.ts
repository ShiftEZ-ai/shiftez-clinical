/** Application-wide name and logos, including sign-in. The server decides who can edit.
 *  Each change is a version with a reason, Undo and history (branding.* commands). */
import {el,field,area,button,note,readFailed,sectionHead,formDialog,historyDialog,confirmAction,notify,withIcon,formatDate} from './dom.js';
import {get,request,command,sessionChanged,keptRead,type KeptReadOptions} from './api.js';
import {skeleton} from './loading.js';
import {BRANDING_DEFAULTS} from './brand.js';
import {BRANDING_IMAGE_FORMATS,BRANDING_IMAGE_LIMITS,type BrandingImage,type BrandingSettings,type BrandingStatus} from './generated.js';

// The image types and limits POST /api/admin/branding/images accepts (its contract): checked here first, so a file it
// would refuse is never read into a preview or sent.
const IMAGE_TYPES=Object.keys(BRANDING_IMAGE_FORMATS),MB=1024*1024;
const imageProblem=`Use a ${new Intl.ListFormat('en',{type:'disjunction'}).format(Object.values(BRANDING_IMAGE_FORMATS))} image of at most ${BRANDING_IMAGE_LIMITS.file_bytes/MB} MB and ${BRANDING_IMAGE_LIMITS.image_pixels/1e6} million pixels.`;
type Logo='emblem'|'wordmark';
const LOGOS:[Logo,string][]=[['emblem','Emblem'],['wordmark','Wordmark']];

/** A logo as it will look: natural proportions, on the light and the dark theme (the wordmark keeps its white plate). */
const preview=(kind:Logo,url:string,alt:string)=>el('div',{class:`brand-preview brand-preview-${kind}`,'data-private':''},
  ...['light','dark'].map(theme=>el('span',{class:`brand-preview-tile ${theme}`},el('img',{src:url,alt:theme==='light'?alt:'',class:kind==='wordmark'?'brand-plate':''}))));

/** The installed app's look (BrandingInput: colours, icon background and fills): sent with every save, so a change to
 *  the name or a logo never puts it back to its defaults. */
const installLook=(st:BrandingSettings)=>Object.fromEntries((['theme_color','background_color','icon_background','icon_fill','maskable_fill'] as const)
  .filter(k=>st[k]!==undefined).map(k=>[k,st[k]]));
/** The icon backgrounds offered (mockup P7): white, the accent and dark. */
const ICON_BACKGROUNDS:{value:string;label:string}[]=[{value:'#ffffff',label:'White'},{value:'#0f7b6c',label:'Accent'},{value:'#16191b',label:'Dark'}];

// maintenance: the linked manifest, read once and kept across rebuilds of Configuration (api.ts keptRead). It is generated
// from the branding: a branding change (live scope settings) or the person's own save reads it again.
type Manifest={icons?:{src:string;purpose?:string;sizes?:string}[]};
const MANIFEST:KeptReadOptions={scopes:['settings'],cap:1};
const manifest=(href:string)=>keptRead<Manifest>(href,signal=>fetch(href,{signal,credentials:'same-origin'}).then(r=>{
  if(!r.ok)throw new Error(`The app manifest answered ${r.status}.`);return r.json() as Promise<Manifest>;}),MANIFEST).p;
/** On phones and computers (mockup P7): the installed app's icon as each platform shapes it, the name it shows, the
 *  opening screen and the icon background. The icons are the linked manifest's own (generated from this branding), so
 *  this shows whatever a device will install. */
function installed(status:BrandingStatus,change:(fields:Record<string,unknown>,what:string)=>void){
  const b=status.branding,short=b.short_name,app=b.app_name;
  const box=el('div',{class:'brand-installed'});
  const link=document.querySelector<HTMLLinkElement>('link[rel=manifest]');
  if(!link)return box;
  void manifest(link.href).then(m=>{
    const icon=(purpose:string)=>{const i=(m.icons||[]).filter(x=>(x.purpose||'any')===purpose).sort((x,y)=>parseInt(y.sizes||'0')-parseInt(x.sizes||'0'))[0];return i?new URL(i.src,link.href).href:'';};
    const any=icon('any'),mask=icon('maskable')||any;if(!any)return;
    const tile=(cls:string,src:string,name:string,where:string)=>el('figure',{},el('span',{class:`ic ${cls}`},el('img',{src,alt:''})),el('b',{},name),el('figcaption',{},where));
    const current=(status.settings.icon_background||'#ffffff').toLowerCase();
    const swatches=status.can_edit?el('div',{class:'brand-icon-bg'},el('span',{id:'icon-bg-label'},'Icon background'),
      el('div',{role:'radiogroup','aria-labelledby':'icon-bg-label',class:'swatch-row',activation:'manual'},...ICON_BACKGROUNDS.map(c=>{
        const r=el('button',{type:'button',role:'radio',class:'icon-swatch','aria-checked':String(c.value===current),'aria-label':c.label,'data-tip':c.label});
        r.style.setProperty('--swatch',c.value);
        r.addEventListener('click',()=>{if(c.value!==current)change({icon_background:c.value},`Icon background: ${c.label}`);});
        return r;}))):'';
    box.append(el('h3',{},'On phones and computers'),
      el('div',{class:'brand-installed-row'},
        el('div',{class:'icon-previews'},tile('ios',any,short,'iPhone'),tile('and',mask,short,'Android'),tile('desk',any,app,'Computer')),
        el('div',{class:'splash-pair','aria-label':'Opening screen'},...['light','dark'].map(t=>el('span',{class:`splash-mini ${t}`},el('span',{class:'ic ios'},el('img',{src:any,alt:''})),app)))),
      swatches);
  }).catch(()=>{});
  return box;
}

export function brandingPanel():HTMLElement{
  const box=el('section',{class:'panel brand-settings'});
  const load=async()=>{
    let status:BrandingStatus;
    try{status=await get<BrandingStatus>('/api/admin/branding');}
    catch(e){if(sessionChanged(e))throw e;
      box.replaceChildren(sectionHead('Name and logo',''),readFailed('the name and logo',e,()=>void load()));return;}
    draw(status);};
  const draw=(status:BrandingStatus)=>{
    const b=status.branding;
    const actions=status.can_edit?[withIcon(button('Edit',()=>edit(status),'secondary'),'upload'),withIcon(button('History',history,'ghost'),'history'),
      status.can_undo?withIcon(button('Undo last change',()=>undo(status),'ghost'),'undo'):'']:[];
    box.replaceChildren(sectionHead('Name and logo','',...actions),
      el('dl',{class:'brand-facts'},el('dt',{},'App name'),el('dd',{},b.app_name),el('dt',{},'Short name'),el('dd',{},b.short_name),el('dt',{},'Institution'),el('dd',{},b.institution_name)),
      el('div',{class:'brand-logos'},...LOGOS.map(([k,label])=>el('figure',{},preview(k,b[`${k}_url` as const],b[`${k}_alt` as const]),el('figcaption',{},label)))),
      installed(status,(fields,what)=>look(status,fields,what)),
      status.can_edit?'':note('The name and logo apply to every department. Ask an installation operator to change them.'));};
  const saved=(result:BrandingStatus)=>{if(box.isConnected)draw(result);};
  /** One change to how the installed app looks, as its own branding version with a reason (Undo and History as any). */
  const look=(status:BrandingStatus,fields:Record<string,unknown>,what:string)=>{const st=status.settings,b=status.branding;
    confirmAction(what,'Installed apps pick up the new icon when they next update it; some phones need the app added again.',async reason=>{
      saved(await command<BrandingStatus>('branding.update',{app_name:st.app_name??b.app_name,short_name:st.short_name??b.short_name,
        institution_name:st.institution_name??b.institution_name,emblem_alt:st.emblem_alt??b.emblem_alt,wordmark_alt:st.wordmark_alt??b.wordmark_alt,
        ...installLook(st),...fields,reason},undefined,b.revision));
      notify('Name and logo saved.');},'Save');};
  const undo=(status:BrandingStatus)=>confirmAction('Undo the last name and logo change','Every department shows the name and logos from before that change.',async reason=>{
    saved(await command<BrandingStatus>('branding.undo',{reason},undefined,status.branding.revision));notify('Name and logo restored.');},'Undo change');
  // SW3-D-07: the shared history (newest first, who). Restore of an older version needs a server command (branding.undo
  // only reverses the last change, offered as Undo last change beside History).
  const history=()=>historyDialog('Name and logo history','App name',async offset=>{
    const {versions}=await get<{versions:{object_revision:number;created_at:string;reason:string;actor_id:string;snapshot:{data?:{app_name?:string}}}[]}>(`/api/admin/branding/history?limit=100&offset=${offset}`);
    // shortcut: the server lists oldest first (pinned by tests/test_branding_configuration.py); each page is turned
    // newest first here, which is the whole history below 100 versions. A desc order on the server lifts the limit.
    return {more:versions.length===100,versions:[...versions].reverse().map(v=>({version:v.object_revision,saved:formatDate(v.created_at),actor_id:v.actor_id,reason:v.reason,
      value:v.snapshot?.data?.app_name||BRANDING_DEFAULTS.app_name}))};});
  const edit=(status:BrandingStatus)=>{
    const s=status.settings,b=status.branding;
    const app=field('App name','text',s.app_name??b.app_name,{required:true,maxLength:160});
    const short=field('Short name','text',s.short_name??b.short_name,{required:true,maxLength:160});
    const institution=field('Institution name','text',s.institution_name??b.institution_name,{required:true,maxLength:160});
    // Each logo: unchanged (undefined), the bundled default (null) or a new file shown from this device until saved
    // (a data: URL: the page's CSP allows no blob: images, and nothing is sent before Save).
    const picks=LOGOS.map(([kind,label])=>{
      let choice:File|null|undefined;
      const current=b[`${kind}_url` as const],alt=field(`${label} description`,'text',s[`${kind}_alt` as const]??b[`${kind}_alt` as const],{maxLength:160,placeholder:'None (decorative)'});
      const file=field(`${label} image`,'file','',{accept:IMAGE_TYPES.join(',')}),shown=el('div',{});
      // A validity set outside the field's own input/change (a read that lands later, Use default) is announced as one,
      // so the shared field-error UI (dom.ts watchErrors) clears or updates its message, aria-invalid and description.
      const settled=()=>file.input.dispatchEvent(new Event('input',{bubbles:true}));
      const reset=button('Use default',()=>{choice=null;file.input.value='';file.input.setCustomValidity('');show(BRANDING_DEFAULTS[`${kind}_url` as const]);settled();},'ghost small');
      const show=(url:string)=>{shown.replaceChildren(preview(kind,url,alt.input.value));reset.hidden=url===BRANDING_DEFAULTS[`${kind}_url` as const];};
      // A chosen file is the field's value only once it is read and checked: until then (and when refused) the field is
      // invalid, so Save cannot go ahead without the logo asked for. A file chosen later supersedes an earlier one.
      file.input.addEventListener('change',()=>{const f=file.input.files?.[0];choice=undefined;file.input.setCustomValidity('');
        if(!f){show(current);return;}
        const refuse=()=>{if(file.input.files?.[0]!==f)return;file.input.setCustomValidity(imageProblem);file.input.reportValidity();};
        if(!IMAGE_TYPES.includes(f.type)||f.size>BRANDING_IMAGE_LIMITS.file_bytes){refuse();return;}
        file.input.setCustomValidity('Checking the image… Save once its preview shows.');
        const reader=new FileReader();reader.addEventListener('error',refuse);reader.addEventListener('load',()=>{const url=String(reader.result),img=new Image();img.src=url;
          img.decode().then(()=>img.naturalWidth*img.naturalHeight<=BRANDING_IMAGE_LIMITS.image_pixels,()=>false).then(ok=>{
            if(file.input.files?.[0]!==f)return;if(!ok){refuse();return;}choice=f;file.input.setCustomValidity('');show(url);settled();});});reader.readAsDataURL(f);});
      show(current);
      return {kind,choice:()=>choice,alt,file,wrap:el('fieldset',{class:'span-2 brand-pick'},el('legend',{},label),shown,el('div',{class:'actions'},reset),file.wrap,alt.wrap)};});
    const reason=area('Reason for the change','',2);reason.input.name='reason';reason.input.required=true;reason.input.minLength=3;reason.input.maxLength=500;
    formDialog('Name and logo','Shown on sign-in, in the navigation and in page titles, for every department.',form=>{
      form.append(app.wrap,short.wrap,institution.wrap,...picks.map(p=>p.wrap),reason.wrap);
      return async()=>{
        const images:Record<string,string|null>={};
        // A pending or refused file never reaches here (its field is invalid); this keeps it from being dropped silently.
        for(const p of picks)if(p.file.input.files?.length&&p.choice()===undefined)throw new Error(imageProblem);
        for(const p of picks){const c=p.choice();if(c===undefined)continue;
          images[`${p.kind}_image_id`]=c&&(await request<BrandingImage>('/api/admin/branding/images',{method:'POST',body:c,headers:{'Content-Type':c.type}})).image_id;}
        saved(await command<BrandingStatus>('branding.update',{app_name:app.input.value.trim(),short_name:short.input.value.trim(),institution_name:institution.input.value.trim(),
          ...Object.fromEntries(picks.map(p=>[`${p.kind}_alt`,p.alt.input.value.trim()])),...installLook(s),...images,reason:reason.input.value.trim()},undefined,status.branding.revision));
        notify('Name and logo saved.');};});};
  box.append(sectionHead('Name and logo',''),skeleton('lines','Loading the name and logo…'));
  void load().catch(()=>{});
  return box;
}
