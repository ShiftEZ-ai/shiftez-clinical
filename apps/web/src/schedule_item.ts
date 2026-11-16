/** One item renderer for every Schedule layout. An item is a session, or one person's duty on it. What it says is the view's
 *  Properties, in the view's order and form, from the one registry (contracts VIEW_PROPERTIES /
 *  VIEW_PROPERTY_DEFINITIONS); each kind of item starts from the contract's VIEW_ITEM_SHOWN_BY_DEFAULT until the view
 *  chooses. Week/Month cards, People × Days, the Timeline's codes grid, Day's bars and the phone agenda all draw their
 *  items' words through itemParts and itemNodes; print and export copy what they draw. */
import {el,icon,labelOf,formatTime,colourOf,MARKS,type Legend} from './dom.js';
import {dutyTime,personTags} from './schedule_dimensions.js';
import {VIEW_PROPERTIES,VIEW_PROPERTY_DEFINITIONS,VIEW_ITEM_SHOWN_BY_DEFAULT,type ViewProperty} from './generated.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';

export type PropKey=ViewProperty['key'];
export type PropForm=NonNullable<ViewProperty['form']>;
/** Card: the Week's session cards; pill: a person in a Month day cell; chip: one grid cell (People × Days, the Timeline);
 *  bar: Day; agenda: the phone list. */
export type ItemSurface=keyof typeof VIEW_ITEM_SHOWN_BY_DEFAULT;

// Names and forms are the contract's; the examples are the renderer's own words for View settings.
export const PROPERTY_NAMES=Object.fromEntries(Object.entries(VIEW_PROPERTY_DEFINITIONS).map(([k,d])=>[k,d.label])) as Record<PropKey,string>;
export const PROPERTY_FORMS=Object.fromEntries(Object.entries(VIEW_PROPERTY_DEFINITIONS).map(([k,d])=>[k,[...d.forms]])) as unknown as Record<PropKey,PropForm[]>;
export const PROPERTY_EXAMPLES:Record<PropKey,string>={label:'',time:'08:00–12:00',lead:'TL',overtime:'OT',supervisor:'Supervisor first, trainees beneath',open:'+ 1 open place',rule:'Could-improve marks (must fix always shows)',code:'The duty type’s short code, e.g. M',location:'The session’s place',duty:'The duty type’s name',service:'The session’s service',hours:'4 h',places:'2 / 3 filled',tags:'#Tag on each person',people:'Names, by the view’s Person label',leave:'Leave shaded, a wish in the corner'};
export const FORM_NAMES:Record<PropForm,string>={chip:'Chip',icon:'Icon',text:'Text',count:'Count',tree:'Tree',box:'Box'};

/** What each kind of item can draw (View settings offers only these; a layout narrows further where a fact is the
 *  row's own, e.g. People only where a cell is a session). The Week's card says time, place, service and hours on its
 *  row (rowFacts) and the rest on each person's pill; a Month pill says them all. */
export const ITEM_DRAWS:Record<ItemSurface,ReadonlySet<PropKey>>={
  card:new Set<PropKey>(['label','time','lead','overtime','supervisor','open','rule','code','location','service','hours','tags']),
  pill:new Set<PropKey>(['label','time','lead','overtime','supervisor','open','rule','code','location','service','duty','hours','tags']),
  chip:new Set<PropKey>(['label','code','time','location','service','duty','hours','lead','overtime','tags','people','leave']),
  bar:new Set<PropKey>(['label','supervisor','code','time','duty','location','service','hours','people','lead']),
  agenda:new Set<PropKey>(['label','supervisor','rule','code','time','duty','location','service','hours','people','lead']),
};

/** The department's default card properties (display settings, with person labels on; D75): a view without its own uses them. */
let departmentProperties:ViewProperty[]|null=null;
export const setDepartmentProperties=(list:ViewProperty[]|null)=>{departmentProperties=list;};
/** A view's properties in its order, completed with the ones it does not name (each item kind's own defaults). The
 *  person's label always shows. */
export function propertiesOf(saved:ViewProperty[]|null|undefined,surface:ItemSurface='card'):ViewProperty[]{
  const shown=new Set<string>(VIEW_ITEM_SHOWN_BY_DEFAULT[surface]);
  const list=(saved?.length?saved:surface==='card'?departmentProperties||[]:[]).filter(p=>p.key in PROPERTY_FORMS),have=new Set(list.map(p=>p.key));
  return [...list,...VIEW_PROPERTIES.filter(k=>!have.has(k)).map((k):ViewProperty=>({key:k,on:shown.has(k)}))]
    .map(p=>({key:p.key,on:p.key==='label'||p.on!==false,form:PROPERTY_FORMS[p.key].includes(p.form as PropForm)?p.form!:PROPERTY_FORMS[p.key][0]}));
}

/** A duty type's short code: the one configured (Configuration › Duty types, 0139_duty_short_code), else the derived
 *  initials of a record that has none. */
export const dutyCode=(type:string|{name?:string;short_code?:string})=>{
  if(typeof type!=='string'&&type.short_code)return String(type.short_code);
  const name=typeof type==='string'?type:String(type.name||'');
  return name.split(/\s+/).filter(Boolean).map(w=>w[0]).join('').slice(0,3).toUpperCase();
};
/** A session's code: its snapshot's, else its duty type's configured code now (sessions saved before codes existed). */
export const sessionCode=(ctx:{catalogues:Record<string,Row[]>},s:Row)=>
  dutyCode(s.type_snapshot?.short_code?s.type_snapshot:{name:s.type_snapshot?.name,short_code:byId(ctx.catalogues.assignment_types,String(s.assignment_type_id))?.short_code});

/** Records by id, indexed once per catalogue list (a grid names thousands of items from the same few lists). */
const indexes=new WeakMap<Row[],Map<string,Row>>();
const byId=(list:Row[]|undefined,id:string)=>{if(!list)return undefined;let m=indexes.get(list);if(!m){m=new Map(list.map(r=>[String(r.id),r]));indexes.set(list,m);}return m.get(id);};
const tagIndex=new WeakMap<object,Map<string,Row[]>>();
const tagsOf=(ctx:Context)=>{const k=(ctx.catalogues.tag_members||ctx.catalogues) as object;let m=tagIndex.get(k);if(!m){m=personTags(ctx.catalogues);tagIndex.set(k,m);}return m;};

/** One thing an item says: its property, words and form (a tag also carries its record, for its colour). */
export type ItemPart={key:PropKey;text:string;form:PropForm;record?:Row};
/** What an item says, in the view's order: each shown property that has words for it. `pid`: one person's duty (their
 *  times, TL, OT and tags); empty: the session (its people). `only`: the properties this item draws here (the rest are
 *  its row's, or another layout's). An item with nothing shown is its colour alone (the key names it). */
export function itemParts(ctx:Context,s:Row,pid:string,zone:string,props:ViewProperty[],o:{only?:ReadonlySet<PropKey>;name?:(id:string)=>string}={}):ItemPart[]{
  const p=pid?((s.participants||[]) as Row[]).find(x=>x.person_id===pid):undefined,parts:ItemPart[]=[];
  for(const x of props){
    if(!x.on||o.only&&!o.only.has(x.key))continue;
    const add=(text:string,record?:Row)=>{if(text)parts.push({key:x.key,text,form:x.form as PropForm,...(record?{record}:{})});};
    switch(x.key){
      case 'code':add(sessionCode(ctx,s));break;
      case 'time':add(p?`${formatTime(p.start,zone)}–${formatTime(p.end,zone)}`:dutyTime(s,zone));break;
      case 'location':add(labelOf(byId(ctx.catalogues.locations,String(s.location_id))));break;
      case 'service':add(((s.service_ids||[]) as string[]).map(id=>labelOf(byId(ctx.catalogues.services,id))).join(' + '));break;
      case 'duty':add(String(s.type_snapshot?.name||''));break;
      case 'hours':{const h=(Date.parse(p?.end||s.end)-Date.parse(p?.start||s.start))/36e5;add(`${Number.isInteger(h)?h:h.toFixed(1)} h`);break;}
      case 'lead':if(p?.team_lead)add(MARKS.lead[0]);break;
      case 'overtime':if(p?.overtime)add(MARKS.ot[0]);break;
      case 'tags':if(pid)for(const t of tagsOf(ctx).get(pid)||[])add('#'+t.name,t);break;
      // A session's people, by the view's Person label (an item that is a session, not one person's duty).
      case 'people':if(!pid&&o.name)add(((s.participants||[]) as Row[]).map(x=>o.name!(String(x.person_id))).join(', '));break;
    }
  }
  return parts;
}
/** Whether a grid's chips say more than a code and marks (their day columns then widen to fit; app.css .it-words). */
export const itemSaysWords=(props:ViewProperty[])=>props.some(p=>p.on&&ITEM_DRAWS.chip.has(p.key)&&!['label','code','lead','overtime','leave'].includes(p.key));
/** The words an item's accessible name adds (TL and OT by their full names). */
export const itemWords=(parts:ItemPart[])=>parts.map(x=>x.key==='lead'?MARKS.lead[1]:x.key==='overtime'?MARKS.ot[1]:x.text);

/** Marks (TL, OT, tags) sit beside an item's words; the first of its words is its head. */
const MARK_PARTS=new Set<PropKey>(['lead','overtime','tags']);
export const itemHead=(parts:ItemPart[])=>parts.find(x=>!MARK_PARTS.has(x.key));
/** Each part drawn in its form, with its key entry: TL and OT as the key's tags (chip), an icon or words; tags as their
 *  coloured chips or words; the rest as words or a chip. `cls`: the item kind's class for words (a grid cell's time is
 *  mono). `head`: the first words in bold (a grid chip's). `codeKey`: the key explains a code beside a name (a card's
 *  pill; a grid's key already pairs each duty type with its code). Every node carries `data-part`, so a layout's CSS
 *  (and its tests) reach a property by name. */
export function itemNodes(parts:ItemPart[],key:Legend,cls:(x:ItemPart)=>string,o:{head?:boolean;codeKey?:boolean}={}):HTMLElement[]{
  const out:HTMLElement[]=[],tags=parts.filter(x=>x.key==='tags'),head=o.head?itemHead(parts):undefined;
  for(const x of parts){
    let n:HTMLElement;
    if(x.key==='lead'||x.key==='overtime'){
      const k=x.key==='lead'?'lead':'ot',glyph=x.key==='lead'?'star':'clock';
      // Each mark is drawn through the key, which stamps it with its entry (dom.ts legend `mark`: every mark has one).
      if(x.form==='icon')n=key.mark(el('span',{class:'wk-tag icon '+k,'aria-hidden':'true'},icon(glyph)),'i:'+k,el('span',{class:'wk-tag '+k,'aria-hidden':'true'},icon(glyph)),MARKS[k][1]);
      else if(x.form==='text')n=key.mark(el('span',{class:cls(x)},MARKS[k][0]),'x:'+k,el('span',{class:cls(x)+' lg-mark','aria-hidden':'true'},MARKS[k][0]),MARKS[k][1]);
      else n=key.tag(k);
    }else if(x.key==='tags'){
      // The person's tags once, together (each part is one tag).
      if(x!==tags[0])continue;
      n=x.form==='text'?el('span',{class:cls(x)+' pchip-tags'},tags.map(t=>t.text).join(' '))
        :el('span',{class:'pchip-tags'},...tags.map(t=>el('span',{class:`tag-chip duty-${colourOf(t.record)}`,'data-lg-self':''},t.text)));  // a tag names itself
    }else{
      n=el(x===head?'b':'span',{class:cls(x)+(x.form==='chip'&&x!==head?' it-chip':'')},x.text);  // a grid chip's head is the chip
      if(x.key==='code'&&o.codeKey)key.mark(n,'code',el('span',{class:cls(x)+' lg-mark','aria-hidden':'true'},'M'),PROPERTY_NAMES.code);
    }
    n.dataset.part=x.key;out.push(n);
  }
  return out;
}
