/** The person card (D67/D68, contact-card DESIGN-NOTE §0 v2): the person hub's Overview. Today's duty with its team,
 *  every visible contact as a row (tap to copy, Call / Message where configured), and More with what this reader may see. Everything comes from
 *  GET /api/people/{id}/card (sections and contacts the server permits) and the contact-type rows; nothing about a
 *  type, label or visibility is decided here. */
import {el,button,icon,iconButton,note,notify,formatDate,formatTime,toLocal,field,select,formDialog,routineReason,grants,legend,type Child,type Legend} from './dom.js';
import {readFailed} from './dom.js';  // a failed read: refused (403) or an error with Retry (maintenance)
import {get,command,sessionChanged,sessionKey} from './api.js';
import {skeleton} from './loading.js';
import {teamGroups} from './planner_teams.js';
import {teamView,rankOn} from './team_view.js';
import {personPill} from './person_pill.js';
import {personLink,personName} from './person_label.js';
import {activeOn,type Context} from './forms.js';
import {contactVisibleTo} from './contacts.js';
import {CONTACT_KIND_ACTIONS,type PersonCard,type PersonCardDuty,type VisibleContact,type ContactTypeInput} from './generated.js';
import type {Row} from './types.js';

export type CardHooks={
  /** The panel head's second line: rank and programme, once the card says them. */sub?:(text:string)=>void;
  /** Opens another tab of the hub (Rules). */tab?:(key:string)=>void;
  /** Records leave for this person (the one availability form). */leave?:()=>void};
type ContactType=ContactTypeInput&{id:string;revision:number};
type Member={person_id:string;display_name?:string;rank?:string;supervisor_person_id?:string|null};

/** Each contact kind's action beside Copy comes from the contract's one mapping (CONTACT_KIND_ACTIONS); its words,
 *  icon and link scheme are keyed by that action, so a new action fails the build here. The link itself is the
 *  server's dial target, which the type's one-tap setting can switch off. */
const ACTIONS:Record<NonNullable<typeof CONTACT_KIND_ACTIONS[keyof typeof CONTACT_KIND_ACTIONS]>,{verb:string;icon:'phone'|'message';scheme:string}>=
  {call:{verb:'Call',icon:'phone',scheme:'tel:'},email:{verb:'Email',icon:'message',scheme:'mailto:'}};
const actionOf=(c:VisibleContact)=>{const a=CONTACT_KIND_ACTIONS[c.kind as keyof typeof CONTACT_KIND_ACTIONS],x=a?ACTIONS[a]:null;
  return x&&c.dial_target?.startsWith(x.scheme)?x:null;};
const num=(n:number)=>String(Math.round(n*10)/10);

export function personCard(ctx:Context,p:Row,hooks:CardHooks={}):HTMLElement{
  const box=el('div',{class:'pc'}),generation=sessionKey();let serial=0;
  const load=async()=>{
    const mine=++serial;box.replaceChildren(skeleton('lines','Loading…'));
    try{
      const own=ctx.me.person_id===p.id,admin=grants(ctx.me.roles,'catalogue.department');
      const [card,types]=await Promise.all([get<PersonCard>(`/api/people/${encodeURIComponent(p.id)}/card?upcoming=1`),
        admin||own?get<{contact_types:ContactType[]}>('/api/contact-types').then(r=>r.contact_types):Promise.resolve([] as ContactType[])]);
      if(mine!==serial||generation!==sessionKey()||!box.isConnected)return;
      box.replaceChildren(...draw(ctx,p,card,types,hooks));
    }catch(e){
      if(mine!==serial||generation!==sessionKey()||!box.isConnected||sessionChanged(e))return;
      box.replaceChildren(readFailed('this person’s card',e,()=>void load()));
    }
  };
  // A contact saved or undone anywhere (this card's form, Undo in a toast) draws the card again.
  const saved=(e:Event)=>{if(!box.isConnected){window.removeEventListener('rota-saved',saved);return;}
    if((e as CustomEvent).detail?.kind==='person.contact.set')void load();};
  window.addEventListener('rota-saved',saved);
  void load();return box;
}

function draw(ctx:Context,p:Row,card:PersonCard,types:ContactType[],hooks:CardHooks):(Node|string)[]{
  const zone=ctx.me.organisation.timezone,person=card.person as Row,name=String(person.display_name||p.display_name||'');
  const names=(key:string)=>((person[key]||[]) as Row[]).map(r=>String(r.name));
  const sub=[...names('categorys'),...names('programmes')].join(' · ');
  if(sub)hooks.sub?.(sub);
  const time=(d:PersonCardDuty)=>`${formatTime(d.start,zone)}–${formatTime(d.end,zone)}`;
  const what=(d:PersonCardDuty)=>[d.duty_type,d.location&&d.location!==d.duty_type?d.location:'',d.status!=='published'?'draft':''].filter(Boolean).join(' · ');
  // The card's own key (the shared legend, as the session panel's): today's team boxes, as drawn (every mark has one).
  const key=legend('Key'),today=card.today.map(d=>el('div',{class:'pc-duty'},el('p',{class:'pc-when'},el('span',{class:'mono'},time(d)),' · '+what(d)),team(ctx,p,d,key)));
  return [...today,key.root,contactRows(ctx,p,name,card.contacts,types),more(ctx,p,card,hooks)].filter((n):n is HTMLElement=>!!n);
}

/** The person's own supervision team on a duty, drawn by the one team box (team_view.ts); others' names open their card. */
function team(ctx:Context,p:Row,d:PersonCardDuty,key:Legend):HTMLElement|null{
  const people=d.team as unknown as Member[];
  const group=teamGroups(people).find(g=>g.some(e=>e.p.person_id===p.id));
  if(!group||group.length<2)return null;
  // The card's heading keeps the full name (D75); its team shows the label, full names in the tips and the group's name.
  const fullName=(id:string)=>id===ctx.me.person_id?'You':String(people.find(x=>x.person_id===id)?.display_name||'Named supervisor');
  const nameOf=(id:string)=>{const known=people.find(x=>x.person_id===id)?.display_name;return id!==ctx.me.person_id&&known?personName(id,known):fullName(id);};
  // The rank's own short code where this reader holds the rank list (as the Schedule's boxes show it), else its name.
  const day=toLocal(d.start,ctx.me.organisation.timezone).slice(0,10);
  return teamView(group,{density:'card',nameOf,fullName,rankOf:id=>rankOn(ctx.catalogues,id,day)||String(people.find(x=>x.person_id===id)?.rank||''),
    member:e=>{const id=e.p.person_id;return id===p.id||id===ctx.me.person_id||!people.some(x=>x.person_id===id&&x.display_name)
      ?personPill({label:nameOf(id),full:fullName(id),key}):personPill({base:personLink(id,nameOf(id),ctx.navigate,fullName(id)),label:nameOf(id),key});},words:true,key});
}

/** More: next duty, hours, fairness, skills, leave and rules, each only when the server sent it. */
function more(ctx:Context,p:Row,card:PersonCard,hooks:CardHooks):HTMLElement|null{
  const zone=ctx.me.organisation.timezone,today=toLocal(new Date().toISOString(),zone).slice(0,10),facts:[string,Child][]=[];
  const next=card.coming_up[0];
  if(next)facts.push(['Next',`${formatDate(next.start,zone)} · ${formatTime(next.start,zone)} ${next.duty_type}`]);
  const period=card.workload?.periods.find(x=>x.key==='this_rota')||card.workload?.periods.find(x=>x.key==='this_month');
  if(period){const cap=period.limits.filter(l=>l.kind==='max_hours').sort((a,b)=>b.actual-b.maximum-(a.actual-a.maximum))[0];
    facts.push(['Hours',cap?`${num(period.totals.hours)} of ${num(cap.maximum)}`:`${num(period.totals.hours)} h`]);}
  const standing=(card.fairness?.goals||[]).flatMap(g=>g.people.filter(r=>r.person_id===p.id)).sort((a,b)=>b.cost-a.cost)[0];
  if(standing)facts.push(['Fairness',standing.status==='within'?'Within range':standing.status==='above'?'Above range':'Below range']);
  if(card.can_work){
    const named=(kind:string,id:unknown)=>String(ctx.catalogues[kind]?.find(r=>r.id===id)?.name||'');
    const now=(list:unknown)=>((list||[]) as Row[]).filter(r=>activeOn(r,today));
    const works=[...now(card.can_work.eligibilities).map(r=>named('services',r.service_id)),...now(card.can_work.skills).map(r=>named('capabilities',r.capability_id))].filter(Boolean);
    if(works.length)facts.push(['Can work',[...new Set(works)].join(', ')]);
  }
  const away=((card.availability?.availability||[]) as Row[]).filter(r=>Date.parse(String(r.end))>Date.now()).slice(0,2);
  if(away.length)facts.push(['Leave',el('span',{class:'pc-lines'},...away.map(r=>{const last=new Date(Date.parse(String(r.end))-1).toISOString();
    return el('span',{},`${formatDate(String(r.start),zone)} – ${formatDate(last,zone)}${r.label?' · '+r.label:''}`);}))]);
  if(card.rules){const n=card.rules.length,words=`${n} appl${n===1?'ies':'y'}`;
    facts.push(['Rules',n&&hooks.tab?button(words,()=>hooks.tab!('rules'),'',{class:'link-button'}):words]);}
  const links=[card.actions.includes('schedule.open')?button('Open in Schedule',()=>ctx.navigate(`schedule/week/${next?toLocal(next.start,zone).slice(0,10):today}?person=${encodeURIComponent(p.id)}`),'',{class:'link-button'}):null,
    card.actions.includes('availability.record')&&hooks.leave?button('Add leave',hooks.leave,'',{class:'link-button'}):null].filter(Boolean) as HTMLElement[];
  if(!facts.length&&!links.length)return null;
  // A plain text button, never a disclosure arrow (owner): More / Show less.
  const id=`pc-more-${++moreSequence}`,body=el('div',{class:'pc-more-body',id,hidden:true},
    facts.length?el('dl',{class:'pc-facts'},...facts.flatMap(([k,v])=>[el('dt',{},k),el('dd',{},v)])):'',
    links.length?el('div',{class:'pc-links'},...links):'');
  const toggle=button('More',()=>{body.hidden=!body.hidden;toggle.textContent=body.hidden?'More':'Show less';toggle.setAttribute('aria-expanded',String(!body.hidden));},
    '',{class:'link-button pc-more','aria-expanded':'false','aria-controls':id});
  return el('div',{},toggle,body);
}
let moreSequence=0;

/** Every contact this reader may see, always shown (owner): its type's label, then the value in full. Tapping the value
 *  copies it; Message and Call sit on the row only where the type's configuration gives a dial target; Edit where the
 *  server says this reader may change it; Add for an empty type they may fill. Order and labels are the server's. */
function contactRows(ctx:Context,p:Row,name:string,contacts:VisibleContact[],types:ContactType[]):HTMLElement|null{
  const shown=contacts.filter(c=>c.value),today=toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
  const own=ctx.me.person_id===p.id,admin=grants(ctx.me.roles,'catalogue.department');
  const empty=types.filter(t=>activeOn(t as unknown as Row,today)&&(admin||own&&t.edited_by==='person_and_admins')&&contactVisibleTo(t.visibility,ctx.me,p.id)
    &&!shown.some(c=>c.contact_type_id===t.id));
  if(!shown.length&&!empty.length)return null;
  const copy=async(c:VisibleContact)=>{try{await navigator.clipboard.writeText(c.value);notify('Copied.');}catch{notify(`Copying is blocked here. ${c.name} ${c.value}`,'info');}};
  // Like a native contact card: label over the value (tap it to copy), then Copy and the kind's action on the right.
  const row=(c:VisibleContact)=>{const t=types.find(x=>x.id===c.contact_type_id),act=actionOf(c);
    const value=el('button',{type:'button',class:'pc-value','aria-label':`Copy ${c.name} ${c.value}`,'data-tip':'Copy'},el('small',{},c.name),el('span',{class:'mono'},c.value));
    value.addEventListener('click',()=>void copy(c));
    return el('li',{class:'pc-contact'},value,el('span',{class:'pc-contact-actions'},
      c.can_edit&&t?iconButton('Edit '+c.name,'edit',()=>contactForm(p,[t],c),{class:'pc-act pc-quiet'}):'',
      iconButton('Copy '+c.name,'copy',()=>void copy(c),{class:'pc-act'}),
      act?el('a',{class:'icon-button pc-act pc-dial',href:c.dial_target!,'aria-label':`${act.verb} ${name}, ${c.name} ${c.value}`,'data-tip':`${act.verb} ${c.name}`},icon(act.icon)):''));};
  return el('div',{class:'pc-contacts-box'},shown.length?el('ul',{class:'pc-contacts','aria-label':'Contact'},...shown.map(row)):'',
    empty.length?button('Add contact',()=>contactForm(p,empty),'',{class:'link-button'}):'');
}

/** One contact value through person.contact.set (revision-checked; the reply's Undo is played as returned). */
function contactForm(p:Row,choices:ContactType[],current?:VisibleContact){
  const first=choices[0];
  formDialog(`${current?'Edit':'Add'} ${current?current.name:'contact'}`,'',form=>{
    const kind=choices.length>1?select('Type',choices.map(t=>({value:t.id,label:t.name})),first.id):null;
    const value=field(current?.name||(choices.length>1?'Value':first.name),'text',current?.value||'',{maxlength:254,autocomplete:'off'});
    const reason=field('Reason','text','',{maxlength:500,placeholder:'Optional note'});
    // The chosen type's own format: its input kind, required, length and pattern (from its configuration row).
    const fit=()=>{const t=chosen(),i=value.input;
      i.type=t.kind==='email'?'email':t.kind==='phone'?'tel':'text';i.inputMode=t.kind==='pager'||t.kind==='extension'?'numeric':'';
      i.required=!!t.required;i.maxLength=Math.min(254,t.format?.max_length||254);
      if(t.format?.pattern)i.pattern=t.format.pattern.replace(/^\^/,'').replace(/\$$/,'');else i.removeAttribute('pattern');
      if(kind)value.wrap.querySelector('label')!.textContent=t.name;};
    const chosen=()=>choices.find(t=>t.id===kind?.input.value)||first;
    kind?.input.addEventListener('change',fit);fit();
    form.append(...(kind?[kind.wrap]:[]),value.wrap,reason.wrap);
    return async()=>{const t=chosen(),c=current?.contact_type_id===t.id?current:undefined;
      const result=await command<{message?:string}>('person.contact.set',{person_id:p.id,contact_type_id:t.id,value:value.input.value.trim(),
        reason:routineReason(reason.input.value)},c?.id||undefined,c?.revision??undefined);
      notify(result.message||'Contact saved.');};
  },current?'Save':'Add');
}
