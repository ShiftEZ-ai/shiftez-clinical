import {el,select,area,field,plural,button,notify,formDialog,latestReading,type Choice} from './dom.js';
import {parseDay} from './date_picker.js';
import {sessionChanged} from './api.js';
import type {SheetField,SheetRow} from './sheet.js';

import {parseTable,splitValues} from './table_text.js';
/** How names are compared when matching: case, accents, a leading # and repeated spaces do not count. */
export const fold=(s:string)=>s.normalize('NFD').replace(/[̀-ͯ]/g,'').trim().toLowerCase().replace(/^#/,'').replace(/\s+/g,' ');
// Tiers: an exact label, across every choice (a tag's label #name is unique even when another tag is named "#name");
// an exact value; then a folded match (case, accents, #) (Cafe vs Café). Each tier counts only when it names ONE record:
// two clinics called "Clinic" (or two mentors of one name) are 'ambiguous', never the first of them.
type Chosen=Choice|'ambiguous'|undefined;
function choose(choices:Choice[],s:string):Chosen{
  const k=fold(s),tiers:((c:Choice)=>boolean)[]=[c=>c.label===s,c=>c.value===s,c=>c.value===s.replace(/^#/,''),c=>fold(c.label)===k||fold(c.value)===k];
  for(const fits of tiers){const hits=choices.filter(fits);if(!hits.length)continue;return new Set(hits.map(c=>c.value)).size===1?hits[0]:'ambiguous';}
  return undefined;
}
const found=(c:Chosen):c is Choice=>!!c&&c!=='ambiguous';
/** Why a pasted value is not read (the dialog's line note): it names several records, or none. */
export const unreadWhy=(f:SheetField,raw:string)=>(f.kind==='select'?[raw.trim()]:f.kind==='multi'?splitValues(raw):[]).some(p=>choose(f.choices||[],p)==='ambiguous')
  ?'names more than one record (choose it in the cell)':'not recognised';

/** A typed or pasted text as the field's value (null: not recognised). Dates are read day first (31/12/2026), as
 *  the shared date field reads them; choices by their name or code. */
export function readValue(f:SheetField,raw:string):{value:unknown}|null{
  const s=raw.trim();
  if(!s)return f.required?null:{value:f.kind==='multi'?[]:f.kind==='toggle'?false:null};
  // A last day included is stored as the day after (ends_on), as the shared date field's lastDayIncluded does.
  if(f.kind==='date'){const d=parseDay(s);if(!d)return null;if(!f.lastDay)return {value:d};const x=new Date(d+'T12:00:00Z');x.setUTCDate(x.getUTCDate()+1);return {value:x.toISOString().slice(0,10)};}
  if(f.kind==='number'){const n=Number(s.replace(/,/g,''));return Number.isFinite(n)?{value:n}:null;}
  if(f.kind==='toggle'){const yes=/^(yes|y|true|1|on)$/i.test(s);return yes||/^(no|n|false|0|off)$/i.test(s)?{value:yes}:null;}
  if(f.kind==='select'){const c=choose(f.choices||[],s);return found(c)?{value:c.value}:null;}
  // Each value as the Sheet copies it (table_text joinValues): a known one by its exact name first ("#student" is the tag
  // student, kept), a new one (tags) by its own name without the "#".
  if(f.kind==='multi'){const parts=splitValues(s),hits=parts.map(p=>choose(f.choices||[],p));
    if(hits.includes('ambiguous'))return null;
    return hits.every(found)?{value:hits.map(c=>(c as Choice).value)}:f.creatable?{value:parts.map((p,i)=>found(hits[i])?(hits[i] as Choice).value:p.replace(/^#/,''))}:null;}
  return f.maxLength&&s.length>f.maxLength?null:{value:s};
}

type Use='match'|'value'|'';
type Edit={row:SheetRow;key:string;value:unknown};
/** One pasted line: its record (matched), its candidates (several match), and values that were not recognised; a line
 *  added as a new record also holds the values it is created with. */
type Line={cells:string[];text:string;found:SheetRow[];bad:string[];values?:Record<string,unknown>};
/** Add rows: each pasted line is a new record of the type, opened in the bulk-change review before anything is saved.
 *  `named`: the field the server's name matcher reads (a person, by name or staff number); `resolve` matches it. */
export type AddRows={fields:SheetField[];named?:string;resolve?:(names:string[])=>Promise<{line:string;ids:string[]}[]>;
  create:(records:Record<string,unknown>[])=>Promise<void>};

/**
 * Paste from Excel (artboard B3): a header row and data rows are read, each pasted column goes to a field (the shared
 * picker) and is either a key that finds the row ("Match rows", e.g. staff number + clinic) or the new value. Lines that
 * find no record, or two, are listed (two: choose which, with the shared picker); dates are read day first. "Stage N
 * changes" stages the values like typed edits: nothing is saved here.
 */
export function pasteDialog(o:{noun:[string,string];fields:SheetField[];candidates:()=>Promise<SheetRow[]>;
  /** Columns that name the record or its person (name, staff number, short code): matched by the server's name
   *  matcher (`resolve`: each pasted line → the ids it names), never compared here. */
  identity?:(key:string)=>boolean;resolve?:(names:string[])=>Promise<{line:string;ids:string[]}[]>;
  text:(row:SheetRow,key:string)=>string;title:(row:SheetRow)=>string;differs:(row:SheetRow,key:string,value:unknown)=>boolean;stage:(edits:Edit[])=>void;
  /** A type whose pasted lines may also be added as new records (no fields to change: only that). */add?:AddRows}){
  formDialog('Paste from Excel','Copy columns from Excel or a CSV, including the header row. Nothing is saved yet.',form=>{
    const pasted=area('Pasted','',5);pasted.input.placeholder='Staff number\tClinic\tLast day\n10423\tWalk-in North\t31/12/2026';
    const mapping=el('div',{class:'span-2 sheet-paste-map'}),summary=el('p',{class:'matrix-summary span-2',role:'status','aria-live':'polite'}),problems=el('div',{class:'span-2 table-scroll'});
    // Change rows (match each line to a record and stage its new values) or Add rows (each line a new record).
    let adding=!o.fields.length&&!!o.add;
    const mode=o.add&&o.fields.length?select('Mode',[{value:'change',label:'Change rows'},{value:'add',label:'Add rows'}],'change'):null;
    form.append(...(mode?[mode.wrap]:[]),pasted.wrap,mapping,summary,problems);
    const fieldsNow=()=>adding?o.add!.fields:o.fields,named=()=>adding?o.add!.named:undefined;
    let by=new Map(fieldsNow().map(f=>[f.key,f]));
    // Add rows: a required field no column gives takes one value for every line (Type, Private note).
    const every=new Map<string,string>();
    let rows:SheetRow[]|null=null,header:string[]=[],data:string[][]=[],goes:string[]=[],uses:Use[]=[],lines:Line[]=[],showMatched=false;
    mode?.input.addEventListener('change',()=>{adding=mode.input.value==='add';by=new Map(fieldsNow().map(f=>[f.key,f]));header=[];every.clear();forget();void analyse();});
    // A record chosen for an ambiguous line holds only while that line reads the same and the record is still one of its
    // candidates; a new mapping clears every choice.
    const picked=new Map<number,{cells:string;id:string}>();
    const choice=(l:Line,i:number)=>{const p=picked.get(i);return p&&p.cells===JSON.stringify(l.cells)?l.found.find(r=>r.id===p.id):undefined;};
    const guessField=(h:string)=>{const k=fold(h),hit=(f:SheetField)=>[f.label,f.key.replace(/^\w+\.|_/g,' '),...(f.lastDay?['last day','until']:[]),
      ...(f.key===named()?['name','display name','staff number','short code']:[])].map(fold);
      return (fieldsNow().find(f=>hit(f).includes(k))||fieldsNow().find(f=>k.length>2&&hit(f).some(x=>x.startsWith(k))))?.key||'';};
    // Keys (what finds the row) by default: related and read-only fields and choices (a clinic); other fields are values.
    // A line added as a new record has no keys: every column is a value.
    const guessUse=(key:string):Use=>{const f=by.get(key);return !f?'':adding?'value':!f.editable||f.related||f.kind==='select'?'match':'value';};
    const submit=()=>form.querySelector<HTMLButtonElement>('button[type=submit]');
    const edits=():Edit[]=>{
      const out:Edit[]=[];
      lines.forEach((l,i)=>{const row=l.found.length===1?l.found[0]:choice(l,i);if(!row||l.bad.length)return;
        goes.forEach((key,c)=>{if(uses[c]!=='value')return;const v=readValue(by.get(key)!,l.cells[c]||'');if(v&&o.differs(row,key,v.value))out.push({row,key,value:v.value});});});
      return out;};
    // The new records of the lines read in full, each with its person when one is named (chosen, for a name of several).
    const records=():Record<string,unknown>[]=>{const n=named(),c=n?goes.indexOf(n):-1;if(n&&c<0)return [];
      return lines.flatMap((l,i)=>{if(l.bad.length||!l.values)return [];if(!n)return [l.values];
        const r=l.found.length===1?l.found[0]:choice(l,i);return r?[{...l.values,[n]:r.id}]:[];});};
    // Stage waits for the paste to be read: while lines are being matched (or typing has not settled) they are stale.
    const reads=latestReading(pasted.input);let reading=false;
    const hold=()=>{reading=true;const b=submit();if(b)b.disabled=true;};
    // Results of an earlier text (its lines and their choice lists) are gone once the text changes or its reading fails:
    // Stage can only ever take the lines of the text now pasted (a choice still holds for a line that reads the same).
    const forget=()=>{lines=[];problems.replaceChildren();const b=submit();if(b){b.disabled=true;b.textContent=adding?'Review 0 new rows':'Stage 0 changes';}};
    async function analyse(){
      const mine=reads.start(),t=parseTable(pasted.input.value),fresh=JSON.stringify(t[0]||[])!==JSON.stringify(header);
      header=t[0]||[];data=t.slice(1);
      if(fresh){goes=header.map(guessField);uses=goes.map(guessUse);picked.clear();drawMapping();}
      if(adding)return addLines(mine);
      const keys=goes.map((k,c)=>uses[c]==='match'?c:-1).filter(c=>c>=0);
      // Names, staff numbers and codes go to the server's one name matcher (one call per such column: each pasted
      // line → the records it names); other keys (a clinic) are read as the field's own values.
      const named=o.resolve?keys.filter(c=>o.identity?.(goes[c])):[],other=keys.filter(c=>!named.includes(c));
      const said:Map<string,string[]>[]=[];
      if(rows&&named.length){summary.replaceChildren('Matching names…');hold();
        try{for(const c of named){const vals=[...new Set(data.map(r=>(r[c]||'').trim()).filter(Boolean))];
          said.push(new Map(vals.length?(await o.resolve!(vals)).map(m=>[m.line,m.ids] as [string,string[]]):[]));}}
        catch(e){if(sessionChanged(e))return;if(reads.current(mine)){reading=false;forget();summary.replaceChildren(el('span',{class:'field-error'},(e as Error).message));}return;}}
      // Only the latest reading of the text now pasted may enable Stage.
      if(!reads.current(mine))return;
      reading=false;
      const index=new Map<string,SheetRow[]>(),byId=new Map((rows||[]).map(r=>[r.id,r]));
      const keyOf=(cols:number[],cell:(c:number)=>string)=>cols.map(c=>fold(cell(c))).join('\u0001');
      if(rows&&other.length&&!named.length)for(const r of rows){const k=keyOf(other,c=>o.text(r,goes[c]));index.set(k,[...(index.get(k)||[]),r]);}
      lines=data.map(cells=>{const bad=goes.flatMap((key,c)=>uses[c]==='value'&&!readValue(by.get(key)!,cells[c]||'')?[`${by.get(key)!.label}: “${cells[c]||''}” ${unreadWhy(by.get(key)!,cells[c]||'')}`]:[]);
        let found:SheetRow[]=[];
        if(named.length){const sets=named.map((c,n)=>new Set(said[n]?.get((cells[c]||'').trim())||[]));
          found=[...sets[0]].filter(id=>sets.every(x=>x.has(id))).map(id=>byId.get(id)).filter((r):r is SheetRow=>!!r)
            .filter(r=>keyOf(other,c=>o.text(r,goes[c]))===keyOf(other,c=>cells[c]||''));}
        else if(other.length)found=index.get(keyOf(other,c=>cells[c]||''))||[];
        return {cells,text:keys.map(c=>cells[c]||'').join(' · '),found,bad};});
      // A line that now reads differently, or no longer offers the chosen record, drops its choice.
      for(const i of [...picked.keys()])if(!lines[i]||!choice(lines[i],i))picked.delete(i);
      drawResult();}
    /** Add rows: each line's values read as the fields read them, its person by the server's name matcher. */
    async function addLines(mine:number){
      const n=named(),c=n?goes.indexOf(n):-1;let said=new Map<string,string[]>();
      if(c>=0&&o.add!.resolve){const vals=[...new Set(data.map(r=>(r[c]||'').trim()).filter(Boolean))];
        if(vals.length){summary.replaceChildren('Matching names…');hold();
          try{said=new Map((await o.add!.resolve(vals)).map(m=>[m.line,m.ids] as [string,string[]]));}
          catch(e){if(sessionChanged(e))return;if(reads.current(mine)){reading=false;forget();summary.replaceChildren(el('span',{class:'field-error'},(e as Error).message));}return;}}}
      if(!reads.current(mine))return;
      reading=false;
      const label=(id:string)=>by.get(n||'')?.choices?.find(x=>x.value===id)?.label||id;
      lines=data.map(cells=>{const values:Record<string,unknown>={},bad:string[]=[];
        for(const f of fieldsNow()){if(f.key===n)continue;const col=goes.indexOf(f.key),raw=col>=0?cells[col]||'':every.get(f.key)||'';
          const v=readValue(f,raw);
          if(!v)bad.push(`${f.label}: ${raw.trim()?`“${raw}” ${unreadWhy(f,raw)}`:'required'}`);
          // A blank optional value is left to the record's own default.
          else if(v.value!==null)values[f.key]=v.value;}
        const found=c<0?[]:(said.get((cells[c]||'').trim())||[]).map(id=>({id,revision:0,title:label(id)}));
        return {cells,text:c>=0?cells[c]||'':cells.join(' · '),found,bad,values};});
      for(const i of [...picked.keys()])if(!lines[i]||!choice(lines[i],i))picked.delete(i);
      drawResult();}
    function drawMapping(){
      if(!header.length)return mapping.replaceChildren();
      const fieldChoices:Choice[]=[{value:'',label:'Not used'},...fieldsNow().map(f=>({value:f.key,label:f.label,group:f.related?'Related (read-only)':'Fields',hint:f.editable?'':'read-only'}))];
      // Add rows: one value for every line for each required field no column gives.
      const unmapped=adding?fieldsNow().filter(f=>f.required&&f.key!==named()&&!goes.includes(f.key)):[];
      const forEvery=unmapped.map(f=>{const label=`${f.label} for every row`;
        const x=f.choices?.length?select(label,f.choices,every.get(f.key)||''):field(label,'text',every.get(f.key)||'');
        // A single possible choice is chosen by the select itself.
        if(x.input.value&&!every.has(f.key))every.set(f.key,x.input.value);
        // Typed text is read once typing settles, as the pasted text is; a choice at once.
        if(f.choices?.length)x.input.addEventListener('change',()=>{every.set(f.key,x.input.value);void analyse();});
        else x.input.addEventListener('input',()=>{every.set(f.key,x.input.value);hold();window.clearTimeout(timer);timer=window.setTimeout(analyse,150);});
        return x.wrap;});
      mapping.replaceChildren(el('div',{class:'dt-pop-label'},'Columns'),el('div',{class:'table-scroll'},el('table',{class:'plain-table'},
        el('thead',{},el('tr',{},el('th',{scope:'col'},'Pasted column'),el('th',{scope:'col'},'Goes to'),...(adding?[]:[el('th',{scope:'col'},'Used for')]))),
        el('tbody',{},...header.map((h,c)=>{
          const to=select(`${h} goes to`,fieldChoices,goes[c],false,'Not used');to.wrap.querySelector('label')!.classList.add('sr-only');
          const f=by.get(goes[c]);
          const use=select(`${h} used for`,[{value:'match',label:'Match rows'},{value:'value',label:'New value',disabled:!f?.editable}],uses[c]||'match');use.wrap.querySelector('label')!.classList.add('sr-only');
          use.wrap.hidden=!f;
          to.input.addEventListener('change',()=>{goes[c]=to.input.value;picked.clear();uses[c]=guessUse(goes[c]);drawMapping();void analyse();});
          use.input.addEventListener('change',()=>{uses[c]=use.input.value as Use;picked.clear();void analyse();});
          return el('tr',{},el('td',{class:'mono'},h),el('td',{},to.wrap),...(adding?[]:[el('td',{},use.wrap)]));})))),...forEvery);}
    function drawResult(){
      if(adding)return drawAdded();
      const n=reading?0:edits().length,b=submit();if(b){b.textContent=`Stage ${plural(n,'change')}`;b.disabled=!n;}
      if(!data.length){summary.replaceChildren(header.length?'Add the rows under the header row.':'Paste the cells here, with the header row first.');return problems.replaceChildren();}
      if(!goes.some((_,c)=>uses[c]==='match')){summary.replaceChildren('Choose at least one column that matches rows (e.g. staff number).');return problems.replaceChildren();}
      if(!rows){summary.replaceChildren('Reading the records…');return problems.replaceChildren();}
      const matched=lines.filter((l,i)=>!l.bad.length&&(l.found.length===1||!!choice(l,i))),missing=lines.filter(l=>!l.found.length),many=lines.filter(l=>l.found.length>1);
      const copy=button('Copy not found',async()=>{try{await navigator.clipboard.writeText(missing.map(l=>l.cells.join('\t')).join('\n'));notify(`${plural(missing.length,'line')} copied.`);}catch{notify('Copying is blocked here; select the lines below and copy them.','error');}},'',{class:'link-button'});
      const show=button(showMatched?'Hide matched':'Show matched',()=>{showMatched=!showMatched;drawResult();},'',{class:'link-button','aria-pressed':String(showMatched)});
      summary.replaceChildren(el('b',{},String(matched.length)),` ${matched.length===1?'row':'rows'} matched · `,el('b',{},String(missing.length)),' not found · ',el('b',{},String(many.length)),` ${many.length===1?'matches':'match'} more than one record · dates read as day/month/year`,
        ...(matched.length?[' · ',show]:[]),...(missing.length?[' · ',copy]:[]));
      const listed=lines.map((l,i)=>({l,i})).filter(({l,i})=>l.found.length!==1&&!choice(l,i)||l.bad.length||showMatched);
      problems.replaceChildren(...(listed.length?[el('table',{class:'plain-table sheet-paste-lines','aria-label':'Pasted lines to check'},el('tbody',{},...listed.map(({l,i})=>{
        const row=l.found.length===1?l.found[0]:choice(l,i);
        let state:HTMLElement,why:Node|string;
        if(l.bad.length){state=el('span',{class:'matrix-cell own'},'Not read');why=l.bad.join(' · ');}
        else if(!l.found.length){state=el('span',{class:'matrix-cell own'},'Not found');why=`No ${o.noun[0]} with these values`;}
        else if(l.found.length>1&&!row){state=el('span',{class:'matrix-cell overlap'},`${l.found.length} records`);
          const which=select(`Which record for ${l.text}`,l.found.map(r=>({value:r.id,label:o.title(r)+' · '+goes.map(k=>o.text(r,k)).filter(Boolean).join(' · ')})),'',false,'Skip this line');which.wrap.querySelector('label')!.classList.add('sr-only');
          which.input.addEventListener('change',()=>{const r=l.found.find(x=>x.id===which.input.value);if(r){picked.set(i,{cells:JSON.stringify(l.cells),id:r.id});drawResult();}});why=which.wrap;}
        else{state=el('span',{class:'matrix-cell has'},'Matched');why=o.title(row!);}
        return el('tr',{},el('td',{class:'mono'},l.text||l.cells.join(' · ')),el('td',{},state),el('td',{class:'muted'},why));})))]:[]));}
    /** Add rows: how many lines are new records, which are not read, and which name no person or several (choose one). */
    function drawAdded(){
      const n=reading?0:records().length,b=submit();if(b){b.textContent=`Review ${plural(n,'new row')}`;b.disabled=!n;}
      if(!data.length){summary.replaceChildren(header.length?'Add the rows under the header row.':'Paste the cells here, with the header row first.');return problems.replaceChildren();}
      const who=named()?by.get(named()!):undefined;
      if(who&&!goes.includes(who.key)){summary.replaceChildren(`Choose the column that names the ${who.label.toLowerCase()}.`);return problems.replaceChildren();}
      const ready=lines.filter((l,i)=>!l.bad.length&&(!who||l.found.length===1||!!choice(l,i))),bad=lines.filter(l=>l.bad.length);
      const missing=who?lines.filter(l=>!l.bad.length&&!l.found.length):[],many=who?lines.filter((l,i)=>!l.bad.length&&l.found.length>1&&!choice(l,i)):[];
      summary.replaceChildren(el('b',{},String(ready.length)),' new · ',el('b',{},String(bad.length)),' not read',
        ...(who?[' · ',el('b',{},String(missing.length)),' not found · ',el('b',{},String(many.length)),` ${many.length===1?'names':'name'} more than one ${who.label.toLowerCase()}`]:[]),' · dates read as day/month/year');
      problems.replaceChildren(...(lines.length?[el('table',{class:'plain-table sheet-paste-lines','aria-label':'Pasted lines to check'},el('tbody',{},...lines.map((l,i)=>{
        const row=l.found.length===1?l.found[0]:choice(l,i);let state:HTMLElement,why:Node|string;
        if(l.bad.length){state=el('span',{class:'matrix-cell own'},'Not read');why=l.bad.join(' · ');}
        else if(who&&!l.found.length){state=el('span',{class:'matrix-cell own'},'Not found');why=`No ${who.label.toLowerCase()} with this name or number`;}
        else if(who&&!row){state=el('span',{class:'matrix-cell overlap'},`${l.found.length} matches`);
          const which=select(`Which ${who.label.toLowerCase()} for ${l.text}`,l.found.map(r=>({value:r.id,label:String(r.title)})),'',false,'Skip this line');which.wrap.querySelector('label')!.classList.add('sr-only');
          which.input.addEventListener('change',()=>{const r=l.found.find(x=>x.id===which.input.value);if(r){picked.set(i,{cells:JSON.stringify(l.cells),id:r.id});drawResult();}});why=which.wrap;}
        else{state=el('span',{class:'matrix-cell has'},'New');why=row?String(row.title):'';}
        return el('tr',{},el('td',{class:'mono'},l.text),el('td',{},state),el('td',{class:'muted'},why));})))]:[]));}
    let timer=0;
    // A new paste invalidates any reading still in flight at once (its reply is for the old text and is dropped).
    pasted.input.addEventListener('input',()=>{reads.start();hold();forget();window.clearTimeout(timer);timer=window.setTimeout(analyse,150);});
    // The records of the current filter are read once, for matching (the paste may name any of them, not only those shown).
    o.candidates().then(r=>{rows=r;void analyse();},e=>{summary.replaceChildren(el('span',{class:'field-error'},(e as Error).message));});
    queueMicrotask(drawResult);
    return async()=>{if(reading)throw new Error('Still reading the pasted lines; try again in a moment.');
      if(adding){const r=records();if(!r.length)throw new Error('Nothing to add: no line is ready.');await o.add!.create(r);return;}
      const e=edits();if(!e.length)throw new Error('Nothing to stage: no matched row has a new value.');o.stage(e);notify(`${plural(e.length,'change')} staged · not saved yet.`);};
  },!o.fields.length&&o.add?'Review 0 new rows':'Stage 0 changes');
}
