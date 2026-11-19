import {bidi,el,button,note,badge,legend,type Legend,field,sectionHead,formDialog,notify,labelOf,colourOf,rankColour,dateUnits,formatDate,plural,armed,toLocal,type Child,statusTone,type Tone,CAPABILITY,roleForCapability} from './dom.js';
import {get,command,download,ApiError} from './api.js';
import {dataTable} from './table_view.js';
import {pillList} from './table_menu.js';
import {dockPanel} from './side_panel.js';
import type {Context} from './forms.js';
import type {Row} from './types.js';

/* Staff spreadsheet import (Imports page) and the person card's
 * chips. The server plans and checks everything; these screens only show the plan and send the reviewed step. */

const has=(ctx:Context,...keys:string[])=>ctx.permits(...keys);
const canPeople=(ctx:Context)=>has(ctx,CAPABILITY.ADMIN,CAPABILITY.COORDINATOR),canCredentials=(ctx:Context)=>has(ctx,CAPABILITY.CREDENTIALER);
const nameOf=(ctx:Context,kind:string,id:string)=>labelOf((ctx.catalogues[kind]||[]).find(r=>r.id===id));
const today=(ctx:Context)=>toLocal(new Date().toISOString(),ctx.me.organisation.timezone).slice(0,10);
/** A planned clinic, skill, rank or programme. Its state is in words for everyone. */
const chip=(ctx:Context,kind:string,id:string,add:boolean,key:Legend)=>{
  const words=add?'Will be added':'Already current';
  const c=el('span',{class:`role-chip duty-${kind==='categories'?rankColour(id):colourOf((ctx.catalogues[kind]||[]).find(r=>r.id===id),id)} ${add?'import-add':'import-same'}`,'data-lg-self':''},
    add?el('span',{'aria-hidden':'true'},'+ '):'',bidi(nameOf(ctx,kind,id)),el('span',{class:'sr-only'},', '+words.toLowerCase()));
  return key.mark(c,add?'add':'same',add?el('span',{class:'role-chip duty-slate import-add','aria-hidden':'true'},'+'):'lg-same',words,add?0:1);};
/** A refused apply that the review on screen answers by reading again (an older review; the import changed or was applied). */
const REVIEW_AGAIN=new Set(['STALE_PREVIEW','STALE_REVISION','IMPORT_ALREADY_APPLIED']);
const STATUS:Record<string,[string,Tone|'']>=Object.fromEntries(Object.entries({preview:'Preview',partly_applied:'Partly applied',applied:'Applied',undone:'Undone'}).map(([k,v])=>[k,[v,statusTone(k)]]));

/** Returns the staff imports it lists (the Imports footer counts those waiting for review). */
export async function staffImportSection(target:HTMLElement,ctx:Context):Promise<Row[]>{
  const data=await get<{batches:Row[]}>('/api/staff-imports');
  const upload=()=>formDialog('Upload staff spreadsheet','A CSV with one person per row. You see every match and change before anything is saved.',form=>{
    const file=field('CSV file','file','',{required:true,accept:'.csv,text/csv'}),source=field('Name for this import','text','',{required:true,maxlength:160});
    file.input.addEventListener('change',()=>{const f=(file.input as HTMLInputElement).files?.[0];if(f&&!source.input.value)source.input.value=f.name.replace(/\.csv$/i,'');});
    form.append(file.wrap,source.wrap,note('Save an Excel sheet as "CSV UTF-8". Separate several clinics, skills or tags with a semicolon (;). Dates look like 2026-10-01. Blank cells leave what is already recorded.'));
    return async()=>{const f=(file.input as HTMLInputElement).files?.[0];if(!f)throw new Error('Choose a CSV file.');if(f.size>1_000_000)throw new Error('The file is over the 1 MB limit.');
      const result=await command('staff_import.preview',{source_name:source.input.value,csv_text:await f.text()});
      await ctx.refresh();staffImportReview(ctx,result.batch.id);};
  },'Preview');
  // Credentialers apply only the clinics and skills part (services/staff_import.apply); a coordinator applies the rest.
  target.append(sectionHead('Staff spreadsheet',canPeople(ctx)?'':'Add clinics and skills from one sheet. People, ranks and programmes are applied by a coordinator.',
    button('Download template',()=>download('/api/staff-imports/template.csv'),'secondary'),button('Upload staff CSV',upload,'primary')));
  target.append(el('section',{class:'panel'},dataTable<Row>({id:'staff-imports',noun:['staff import','staff imports'],rows:data.batches,rowId:r=>r.id,
    searchLabel:'Search staff imports',searchPlaceholder:'Import name',empty:['No staff imports yet','Fill in the CSV template with one row per person, then upload it.'],defaults:{sort:['created','desc']},
    columns:[
      {key:'source',label:'Import',text:r=>String(r.source_name),cell:r=>el('strong',{},r.source_name)},
      {key:'rows',label:'Rows',num:true,sort:r=>Number(r.rows),cell:r=>String(r.rows)},
      {key:'state',label:'State',facet:r=>STATUS[r.status]?.[0]||r.status,cell:r=>badge(...(STATUS[r.status]||[r.status,'']))},
      {key:'created',label:'Uploaded',sort:r=>String(r.created_at),cell:r=>dateUnits([formatDate(r.created_at,ctx.me.organisation.timezone)])},
    ],onOpen:r=>staffImportReview(ctx,r.id),rowButtons:r=>[button('Review',()=>staffImportReview(ctx,r.id),'secondary small')]}).root));
  return data.batches;
}

const PERSON_COLS=['display_name','short_code','staff_number','pager','title','tags','email'];
const GROUPS:Record<string,string[]>={person:PERSON_COLS,rank:['rank','rank_from'],clinics:['clinics','clinics_from'],skills:['skills','skill_evidence','skills_from'],programme:['programme','programme_from','programme_last_day']};
const problems=(r:Row,group:string)=>GROUPS[group].filter(c=>r.errors[c]).map(c=>el('div',{class:'cell-error'},`${c.replaceAll('_',' ')}: ${r.errors[c]}`));

/** The reviewed preview of one staff import: what applying would change now, split by who may apply it. */
export async function staffImportReview(ctx:Context,batchId:string){
  // 820px leaves the import list its room beside the panel (the grip widens it); each row says how it was matched.
  dockPanel({key:'staff-import',className:'staff-import',title:'Staff import',width:820,build:({body,close})=>{
    // Apply sends the fingerprint of the review on screen (Command.expected_fingerprint): the server applies only that
    // plan. While a review reloads, the one on screen cannot be applied; a refused stale review is read again, never re-sent.
    let applying:HTMLButtonElement|null=null,stale=false;
    const draw=async()=>{
      if(applying)applying.disabled=true;
      const view=await get<{batch:Row;rows:Row[];summary:Record<string,number>;source_fingerprint:string}>('/api/staff-imports/'+batchId);
      const {batch,rows,summary:s}=view,errors=s.error_rows,key=legend('Key');
      const stats:[number,string][]=[[s.new_people,s.new_people===1?'new person':'new people'],[s.updated_people,s.updated_people===1?'person updated':'people updated'],[s.rank_changes,s.rank_changes===1?'rank change':'rank changes'],
        [s.programme_memberships,s.programme_memberships===1?'programme membership':'programme memberships'],[s.eligibilities,s.eligibilities===1?'clinic permission':'clinic permissions'],[s.skills,s.skills===1?'skill':'skills']];
      // A finished part (the server's finished_parts) is done like an applied import: its live plan would re-write over later
      // edits. Clinics & skills still waiting on new people stay open.
      const finished=new Set<string>(batch.finished_parts||[]);
      const part=(title:string,key:string,pending:number,mine:boolean,who:string)=>finished.has(key)?el('div',{class:'import-part'},el('strong',{},title),el('span',{class:'muted'},'Applied')):
        el('div',{class:'import-part'+(pending&&!mine?' waiting':'')},el('strong',{},title),
        el('span',{class:'muted'},!pending?'Nothing pending':mine?`${plural(pending,'change')} you can apply`:`${plural(pending,'change')} · needs a ${who}`));
      const mayPeople=canPeople(ctx),mayCreds=canCredentials(ctx);
      const people=mayPeople&&!finished.has('people'),creds=mayCreds&&!finished.has('credentials'),mine=(people?s.people_pending:0)+(creds?s.credentials_pending:0);
      const open=(batch.applied?.applications||[]).filter((a:Row)=>!a.undone&&(a.part==='people'?mayPeople:mayCreds));
      // Applied is terminal: it shows when, not the live plan (applying the sheet again would re-write people over later
      // edits; uploading it again re-reviews).
      const done=batch.status==='applied',last=[...(batch.applied?.applications||[])].sort((x:Row,y:Row)=>String(x.at).localeCompare(String(y.at))).pop();
      // The name only when the server resolved it (by_name); never the identity id.
      const on=done&&last?el('span',{class:'muted'},' · '+formatDate(String(last.at),ctx.me.organisation.timezone)+(last.by_name?' · by '+last.by_name:'')):'';
      const apply=done||!(people||creds)?null:button(people&&creds?'Apply all':people?'Apply people & ranks':'Apply clinics & skills',async()=>{
        const result=await command('staff_import.apply',{reason:'Staff import: '+batch.source_name},batch.id,batch.revision,view.source_fingerprint)
          .catch(async(e:unknown)=>{if(!(e instanceof ApiError&&REVIEW_AGAIN.has(e.code)))throw e;stale=true;await draw();return null;});
        if(!result)return;
        notify(String(result.message));await ctx.refresh();await draw();},'primary',{disabled:Boolean(errors)||!mine,why:errors?'Fix the rows with errors first.':'Nothing here you can apply.'});
      applying=apply;
      // A tab opened before someone else applied it reads again and shows the import as applied: never applied twice.
      const changed=!stale?'':done?note('This import was applied since this review opened, so nothing was applied again.','info')
        :note('People, clinics or dates changed since this review, so nothing was applied. This is the new review: check it, then apply again.','warning');stale=false;
      const undo=open.length?armed(button('Undo this import',async()=>{
        const result=await command('staff_import.undo',{reason:'Undo staff import: '+batch.source_name},batch.id,batch.revision);
        notify(String(result.message));await ctx.refresh();await draw();},'secondary')):null;
      body.replaceChildren(
        el('div',{class:'import-head'},el('div',{},el('strong',{},batch.source_name),' ',badge(...(STATUS[batch.status]||[batch.status,''])),on),el('div',{class:'actions'},...[undo,apply].filter(Boolean) as Child[])),
        done?'':el('div',{class:'import-summary',role:'status'},...stats.filter(([n])=>n).map(([n,label])=>el('span',{class:'import-stat'},el('b',{},String(n)),' '+label)),
          ...(stats.every(([n])=>!n)?[el('span',{class:'muted'},'Nothing to change: everything in this sheet is already recorded.')]:[]),
          errors?el('span',{class:'import-stat error'},el('b',{},String(errors)),' '+(errors===1?'row has':'rows have')+' problems'):''),
        changed,
        done?'':el('div',{class:'import-parts'},part('People, ranks & programmes','people',s.people_pending,mayPeople,roleForCapability(CAPABILITY.COORDINATOR)),part('Clinics & skills','credentials',s.credentials_pending,mayCreds,roleForCapability(CAPABILITY.CREDENTIALER))),
        errors&&!done?note(`Fix the ${plural(errors,'row')} marked below in the spreadsheet and upload it again. Nothing is applied while a row has a problem.`,'error'):'',
        dataTable<Row>({id:'staff-import-rows',noun:['row','rows'],rows,rowId:r=>String(r.row_number),searchLabel:'Search rows',searchPlaceholder:'Name, rank, clinic or skill',pageSize:50,
          columns:[
            {key:'row',label:'Row',num:true,sort:r=>r.row_number,cell:r=>String(r.row_number)},
            {key:'person',label:'Person',text:r=>`${r.data.display_name} ${r.data.staff_number||''} ${r.data.short_code||''}`,sort:r=>String(r.data.display_name),
              facet:r=>Object.keys(r.errors).length?'Has problems':r.person.left?'Left · unchanged':r.person.action==='new'?'New person':r.person.action==='update'?'Person updated':'No person change',
              cell:r=>el('div',{},el('strong',{},r.data.display_name||nameOf(ctx,'people',r.person.id)),' ',
                r.person.left?badge('Left · unchanged','info'):r.person.action==='new'?badge('New','info'):r.person.action==='update'?badge('Updated','warn','',{tip:Object.entries(r.person.changes).map(([k,v])=>`${k.replaceAll('_',' ')}: ${JSON.stringify((v as unknown[])[0])} → ${JSON.stringify((v as unknown[])[1])}`).join('\n')}):'',
                r.person.by&&r.person.by!=='import'?el('div',{class:'muted cell-note'},'Matched by '+r.person.by.replaceAll('_',' ').replace('display name','name')):'',...problems(r,'person'))},
            {key:'rank',label:'Rank',search:'related',text:r=>r.rank?nameOf(ctx,'categories',r.rank.category_id):'',facet:r=>r.rank?.action==='add'?'Rank change':'No rank change',
              cell:r=>el('div',{},r.rank?chip(ctx,'categories',r.rank.category_id,r.rank.action==='add',key):'',r.rank?.action==='add'?el('div',{class:'muted cell-note'},'From '+formatDate(r.rank.starts_on)+(r.rank.ends.length?' · ends '+r.rank.ends.map((id:string)=>nameOf(ctx,'categories',id)).join(', '):'')):'',...problems(r,'rank'))},
            {key:'clinics',label:'Clinics',text:r=>r.clinics.items.map((i:Row)=>nameOf(ctx,'services',i.id)).join(' '),facet:r=>r.clinics.items.map((i:Row)=>nameOf(ctx,'services',i.id)),
              cell:r=>el('div',{},pillList(r.clinics.items.map((i:Row)=>chip(ctx,'services',i.id,i.action==='add',key)),{label:['clinic','clinics']}),...problems(r,'clinics'))},
            {key:'skills',label:'Skills',text:r=>r.skills.items.map((i:Row)=>nameOf(ctx,'capabilities',i.id)).join(' '),facet:r=>r.skills.items.map((i:Row)=>nameOf(ctx,'capabilities',i.id)),
              cell:r=>el('div',{},pillList(r.skills.items.map((i:Row)=>chip(ctx,'capabilities',i.id,i.action==='add',key)),{label:['skill','skills']}),...problems(r,'skills'))},
            {key:'programme',label:'Programme',search:'related',text:r=>r.programme?nameOf(ctx,'programmes',r.programme.programme_id):'',
              cell:r=>el('div',{},r.programme?chip(ctx,'programmes',r.programme.programme_id,r.programme.action==='add',key):'',...problems(r,'programme'))},
          ]}).root);
      body.append(key.follow([body.lastElementChild!]));  // below the rows: what "+" and a neutral chip mean, for the chips on this page
    };
    draw().catch(e=>{body.replaceChildren(note((e as Error).message,'error',e),button('Close',close,'secondary'));});
  }});
}

