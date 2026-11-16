/** Tags (Configuration › Tags): one flat list of department tags. Members are references, so a
 *  rule, filter or saved view naming a tag includes whoever is a member at that moment. */
import {bidi,el,button,iconButton,field,area,check,multi,multiPick,popover,hoverCard,notify,sectionHead,footerStatus,plural,badge,colourOf,labelOf,formInto,note,empty,withIcon,type Choice,emptyLine} from './dom.js';
import {working} from './loading.js';
import {kindLabel,createLabel,purposeOf} from './config_kinds.js';
import {dockPanel,rowMark,type PanelPosition} from './side_panel.js';
import {command,request} from './api.js';
import {dataTable} from './table_view.js';
import {pillList} from './table_menu.js';
import {colourSwatches,type Context} from './forms.js';
import {TAG_KINDS,TAG_FIELDS} from './generated.js';
import type {Row} from './types.js';

/** Each kind a tag can label, in words: [one, many]. */
/** Keyed by the contract's TAG_KINDS, so a new kind fails the build here instead of breaking the tag form. */
export const TAG_KIND_WORDS:Record<typeof TAG_KINDS[number],[string,string]>={people:['person','people'],services:['clinic','clinics'],locations:['place','places'],
  assignment_types:['duty type','duty types'],programmes:['programme','programmes'],policies:['rule','rules'],
  events:['event','events'],department_content:['announcement','announcements']};
const kindWords=(k:string):[string,string]=>TAG_KIND_WORDS[k as keyof typeof TAG_KIND_WORDS]||[k,k];
type Ref={kind:string;id:string};
type Preview={rules:{policy_id:string;name:string;lines:number;people_before:number;people_after:number}[];rule_lines:number;saved_views:{name:string}[];draft_sessions:number;drafts:number};

const all=(ctx:Context,kind:string)=>ctx.catalogues[kind]||[];
const kindsText=(kinds:string[])=>kinds.map(k=>kindWords(k)[1]).join(', ').replace(/^./,c=>c.toUpperCase());
export const membersOf=(ctx:Context,tagId:string):Ref[]=>all(ctx,'tag_members').filter(m=>m.tag_id===tagId).map(m=>({kind:m.kind,id:m.member_id}));
const memberName=(ctx:Context,ref:Ref)=>labelOf(all(ctx,ref.kind).find(r=>r.id===ref.id));
/** The tags a record is a member of. */
export const tagsFor=(ctx:Context,kind:string,id:string):Row[]=>{const ids=new Set(all(ctx,'tag_members').filter(m=>m.kind===kind&&m.member_id===id).map(m=>m.tag_id));
  return all(ctx,'tags').filter(t=>ids.has(t.id));};
/** "#Name" for a tag id (a rule line may name a tag where it names records), else undefined. */
export const tagLabel=(ctx:Context,id:string)=>{const t=all(ctx,'tags').find(r=>r.id===id);return t?'#'+t.name:undefined;};
/** Tags a picker for this condition field may offer: those labelling the field's kind (a tags line: those labelling people). */
export function tagChoices(ctx:Context,fieldKey:string):Choice[]{
  const kind=fieldKey==='tags'?'people':TAG_FIELDS[fieldKey];
  return kind?all(ctx,'tags').filter(t=>t.active!==false&&(t.labels||[]).includes(kind)).map(t=>({value:t.id,label:'#'+t.name,group:'Tags',tone:'duty-'+colourOf(t),hint:plural(membersOf(ctx,t.id).filter(m=>m.kind===kind).length,...kindWords(kind)),keywords:t.description||''})):[];
}
/** Where rules use a tag, as the server counts it with the tag list (apps/api/services/tags.usage). */
export function tagUse(_ctx:Context,tag:Row){
  const {lines=0,rules=0}=(tag.used_by||{}) as {lines?:number;rules?:number};
  return {lines,rules,text:lines?`${plural(lines,'line')} in ${plural(rules,'rule')}`:'Not used by rules'};
}
/** A tag as a coloured chip with its member count; hovering lists the members, clicking opens them with "Open tag". */
export function tagRef(ctx:Context,tag:Row,open?:(tag:Row)=>void):HTMLElement{
  const members=membersOf(ctx,tag.id);
  // A tag chip names itself ("#Name"): its hue only repeats its words, so it needs no key entry (dom.ts tagChip).
  const chip=el('button',{type:'button',class:`tag-chip tag-ref duty-${colourOf(tag)}`,'data-lg-self':'','aria-label':`Tag ${tag.name}, ${plural(members.length,'member')}`},
    el('span',{class:'chip-text'},'#',bidi(String(tag.name))),el('span',{class:'tag-count'},String(members.length)));
  const summary=()=>[el('strong',{},'#'+tag.name),el('p',{class:'muted'},[kindsText(tag.labels||[])+' tag',plural(members.length,'member'),tagUse(ctx,tag).text].join(' · '))];
  const list=()=>members.length?el('ul',{class:'tag-member-list'},...members.slice(0,12).map(m=>el('li',{},memberName(ctx,m))),members.length>12?el('li',{class:'muted'},`and ${members.length-12} more`):null):emptyLine('No members yet.');
  hoverCard(chip,()=>[el('div',{class:'hovercard-body'},...summary(),list())]);
  chip.addEventListener('click',e=>{e.stopPropagation();popover(chip,(panel,close)=>panel.append(el('div',{class:'tag-pop'},...summary(),list(),
    open?button('Open tag',()=>{close();open(tag);},'secondary small'):null)),{label:'Tag '+tag.name});});
  return chip;
}
/** The chips of a record's tags (People, clinic lists, rule screens). */
export const tagRefs=(ctx:Context,kind:string,id:string,open?:(tag:Row)=>void)=>pillList(tagsFor(ctx,kind,id).map(t=>()=>tagRef(ctx,t,open)),{label:['tag','tags']});

/** Configuration › Tags: the list (tag, what it can label, members, use by rules, description) and its editor. */
export function tagsPage(target:HTMLElement,ctx:Context,canEdit:boolean){
  const tags=all(ctx,'tags'),open=(t?:Row)=>tagEditor(ctx,t,canEdit);
  target.append(sectionHead(kindLabel('tags'),'',canEdit&&tags.length?button(createLabel('tags'),()=>open(),'primary'):null));
  const tagged=new Set(all(ctx,'tag_members').map(m=>m.kind+'|'+m.member_id)).size;
  target.append(footerStatus(el('span',{class:'muted'},plural(tagged,'tagged item'))));
  // The same empty state as every other list (C-03): what tags are for and the one action that adds one.
  if(!tags.length){target.append(empty('No tags yet',purposeOf('tags'),canEdit?withIcon(button(createLabel('tags'),()=>open(),'primary'),'plus'):null,'grid'));return;}
  const status=(t:Row)=>t.active===false?'Archived':'Active';
  target.append(dataTable<Row>({id:'catalogue-tags',noun:['tag','tags'],rows:tags,rowId:t=>t.id,searchLabel:'Search tags',searchPlaceholder:'Search tags',defaults:{filters:{status:['Active']}},
    columns:[
      {key:'name',label:'Tag',text:t=>`${t.name} ${t.description||''}`,sort:t=>String(t.name).toLowerCase(),cell:t=>tagRef(ctx,t,open)},
      {key:'labels',label:'Can label',facet:t=>(t.labels||[]).map((k:string)=>kindWords(k)[1]),text:t=>kindsText(t.labels||[]),cell:t=>pillList((t.labels||[]).map((k:string)=>badge(kindWords(k)[1])),{label:['kind','kinds']})},
      {key:'members',label:'Members',num:true,sort:t=>membersOf(ctx,t.id).length,text:t=>String(membersOf(ctx,t.id).length),cell:t=>String(membersOf(ctx,t.id).length)},
      {key:'used',label:'Used by',text:t=>tagUse(ctx,t).text,sort:t=>tagUse(ctx,t).lines,cell:t=>el('span',{class:tagUse(ctx,t).lines?'':'muted'},tagUse(ctx,t).text)},
      {key:'description',label:'Description',text:t=>t.description||'',cell:t=>t.description||el('span',{class:'muted'},'—')},
      {key:'status',label:'Status',text:status,facet:status,cell:t=>el('span',{class:'ct-status '+(t.active===false?'off':'on')},status(t))},
    ],
    onOpen:(t,position)=>tagEditor(ctx,t,canEdit,position),rowButtons:t=>[button(canEdit?'Edit':'Open',()=>open(t),'secondary small')]}).root);
}

/** The tag editor (side panel): name, colour, description, what it can label, its members, and what saving would change. */
export function tagEditor(ctx:Context,tag:Row|undefined,canEdit=true,position?:PanelPosition,
  source?:{opened:(panel:HTMLElement)=>void;closed:(leaving:boolean)=>void}){
  const current=tag?membersOf(ctx,tag.id):[];
  dockPanel({key:'tag',title:tag?'#'+tag.name:'New tag',sub:tag?[kindsText(tag.labels||[]),plural(current.length,'member'),tagUse(ctx,tag).text].join(' · '):'',  // SW3-D-12: no tagline on a new tag (the form says what to fill)
    position,mark:tag?rowMark(tag.id):undefined,width:620,onClose:source?.closed,build:d=>{
      source?.opened(d.panel);formInto(d.body,d.close,d.tryClose,tagForm(ctx,tag,canEdit,current),canEdit?'Save tag':'Close');}});
}
function tagForm(ctx:Context,tag:Row|undefined,canEdit:boolean,current:Ref[]){
  const key=(r:Ref)=>r.kind+'|'+r.id,had=new Set(current.map(key));
  const adding=new Map<string,Ref>(),removing=new Map<string,Ref>();
  return (form:HTMLFormElement)=>{
    const nameField=field('Name','text',tag?.name||'',{required:true,maxLength:24,pattern:'\\S(.*\\S)?',placeholder:'VIP_Clinics'});
    const colour=colourSwatches(tag?.colour||'',true,'Shown on the tag everywhere it appears.');
    const description=area('Description',tag?.description||'',2);
    const labels=multi('Can label',TAG_KINDS.map(k=>({value:k,label:kindWords(k)[1].replace(/^./,c=>c.toUpperCase())})),tag?.labels||['people']);
    const active=tag?check('Active',tag.active!==false,'Archived tags still work in the rules that use them but are not offered in pickers.'):null;
    const membersBox=el('section',{class:'span-2 tag-members','aria-label':'Members'}),effect=el('div',{class:'span-2 tag-effect','aria-live':'polite'});
    form.append(nameField.wrap,colour.wrap,el('div',{class:'span-2'},description.wrap),el('div',{class:'span-2'},labels.wrap),active?el('div',{class:'span-2'},active.wrap):'',membersBox,effect);
    const value=()=>({name:nameField.input.value.trim(),colour:colour.input.value,description:description.input.value.trim(),labels:labels.values(),active:active?active.input.checked:true});
    // Preview and save use the same delta, including members removed by dropping a labelled kind.
    const delta=()=>{const kinds=labels.values();return {add:[...adding.values()].filter(r=>kinds.includes(r.kind)),
      remove:[...removing.values(),...current.filter(r=>!kinds.includes(r.kind)&&!removing.has(key(r)))]};};
    let timer=0,previewVersion=0;
    const changed=()=>{previewVersion++;window.clearTimeout(timer);timer=window.setTimeout(previewEffect,250);};
    /** One kind's members: the list (new ones marked, each removable) and a picker of the records still outside. */
    function kindBlock(kind:string):HTMLElement{
      const words=kindWords(kind),Kind=words[1].replace(/^./,c=>c.toUpperCase()),list=el('div',{}),count=el('span',{class:'muted'});
      const shown=()=>[...current.filter(r=>r.kind===kind&&!removing.has(key(r))),...[...adding.values()].filter(r=>r.kind===kind)];
      const block=el('div',{class:'tag-member-kind'});
      const paint=()=>{const items=shown();count.textContent=String(items.length);
        list.replaceChildren(items.length?el('ul',{class:'tag-member-list'},...items.map(r=>el('li',{},memberName(ctx,r),adding.has(key(r))?badge('New','info'):null,
          canEdit?iconButton('Remove '+memberName(ctx,r),'close',()=>{if(adding.has(key(r)))adding.delete(key(r));else removing.set(key(r),r);block.replaceWith(kindBlock(kind));changed();},{class:'bare small'}):null))):emptyLine(`No ${words[1]} yet.`));};
      const outside=all(ctx,kind).filter(r=>(!had.has(kind+'|'+r.id)||removing.has(kind+'|'+r.id))&&r.active!==false&&r.status!=='retired');
      const add=canEdit&&outside.length?multiPick('Add '+words[1],outside.map(r=>({value:r.id,label:labelOf(r)})),[...adding.values()].filter(r=>r.kind===kind).map(r=>r.id),ids=>{
        for(const r of [...adding.values()])if(r.kind===kind&&!ids.includes(r.id))adding.delete(key(r));
        for(const id of ids){const r={kind,id};if(removing.has(key(r)))removing.delete(key(r));else adding.set(key(r),r);}
        paint();changed();},'+ Add '+words[1]):null;
      paint();
      block.append(el('div',{class:'tag-member-head'},el('strong',{},Kind),count,add?add.wrap:''),list);
      return block;
    }
    const paintMembers=()=>{const kinds=labels.values();
      membersBox.replaceChildren(el('h3',{},'Members'),...kinds.map(kindBlock),kinds.length?'':el('p',{class:'muted'},'Choose what this tag can label to add members.'));};
    async function previewEffect(){
      const members=delta(),version=previewVersion;
      if(!members.add.length&&!members.remove.length){effect.replaceChildren();return;}
      if(!tag){effect.replaceChildren(note('A new tag is not used by any rule or view yet: saving it changes nothing else.'));return;}
      effect.replaceChildren(working('Working out what this changes…'));
      try{
        const out=await request<Preview>(`/api/tags/${tag.id}/preview`,{method:'POST',body:JSON.stringify({...members,value:value()})});
        if(version!==previewVersion||!form.isConnected)return;
        const moved=out.rules.filter(r=>r.people_before!==r.people_after),n=members.add.length+members.remove.length;
        effect.replaceChildren(el('h3',{},'Saving this changes'),el('ul',{class:'tag-effect-list'},
          el('li',{},out.rule_lines?`${plural(out.rule_lines,'line')} in ${plural(out.rules.length,'rule')} ${out.rule_lines===1?'follows':'follow'} the change: ${out.rules.map(r=>r.name).join(', ')}`:'No rule uses this tag.'),
          ...moved.map(r=>el('li',{},`${r.name}: ${r.people_before} → ${r.people_after} people today`)),
          el('li',{},out.saved_views.length?`${plural(out.saved_views.length,'saved view')} ${out.saved_views.length===1?'follows':'follow'} it: ${out.saved_views.map(v=>v.name).join(', ')}`:'No saved view filters on this tag.'),
          el('li',{},out.draft_sessions?`${plural(out.draft_sessions,'session')} in ${plural(out.drafts,'rota')} still being planned ${out.draft_sessions===1?'involves':'involve'} ${n===1?'this record':'these records'}.`:'No session in a rota being planned involves them.')));
      }catch(e){if(version===previewVersion&&form.isConnected)effect.replaceChildren(note((e as Error).message,'error',e));}
    }
    labels.wrap.addEventListener('change',()=>{paintMembers();changed();});
    nameField.input.addEventListener('input',changed);
    paintMembers();
    if(!canEdit)for(const c of form.querySelectorAll<HTMLInputElement|HTMLTextAreaElement|HTMLButtonElement>('input,textarea,.swatch-row button'))c.disabled=true;
    return async()=>{
      if(!canEdit)return;
      const next=value(),members=delta();
      if(!next.labels.length)throw new Error('Choose at least one kind of record this tag can label.');
      const same=tag&&next.name===tag.name&&next.colour===(tag.colour||'')&&next.description===(tag.description||'')&&next.active===(tag.active!==false)
        &&next.labels.length===(tag.labels||[]).length&&next.labels.every(k=>(tag.labels||[]).includes(k));
      if(same&&!members.add.length&&!members.remove.length){notify('No changes to save.','info');return;}
      await command(tag?'catalogue.update':'catalogue.create',{catalogue:'tags',value:next,...(members.add.length||members.remove.length?{members}:{}),reason:tag?'Tag updated':'Tag created'},tag?.id,tag?.revision);
      notify(tag?'Tag saved.':`Tag #${next.name} created.`);await ctx.refresh();
    };
  };
}
