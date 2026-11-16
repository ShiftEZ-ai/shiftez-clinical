import {el,field,select,formDialog,plural,area} from './dom.js';
import {get} from './api.js';
import {conditionBuilder} from './conditions.js';
import {bulkContext} from './bulk.js';
import {runBulk} from './bulk_change.js';
import {changesOf,type Entry as RuleEntry} from './rule_catalogue.js';
import {CONDITION_FIELD_DEFINITIONS,RULE_CATALOGUE_V2,type Conditions,type RuleCatalogueEntry} from './generated.js';

type Entry=RuleCatalogueEntry&{rule:{id:string;revision:number}|null};
const NUMBER_WORDS:Record<string,string>={min:'Minimum',max:'Maximum',target:'Target'};
const numberKeys=(e?:Entry)=>Object.keys(e?.numbers||{}).filter(k=>k in NUMBER_WORDS);

/**
 * "Set a rule number…" for selected people: a rule, where it applies, its
 * numbers (minimum, maximum and target of the same measure are one rule) and the lines — one for the group (who = the
 * tag they share, or the people) or one each. It is proposed (rule.propose with the new lines), checked like any
 * proposal, and becomes active only when another person approves it.
 */
export async function setRuleNumber(people:{id:string;tags?:string[]}[],onDone:()=>unknown){
  const catalogue=await get<{entries:{id:string;rule:Entry['rule']}[]}>('/api/rules/catalogue?version=2');
  // The rules (contract catalogue) with their version in use, if any: those in use first; one not used yet gets its first version.
  const inUse=new Map(catalogue.entries.map(e=>[e.id,e.rule]));
  const rules:Entry[]=RULE_CATALOGUE_V2.filter(e=>e.authorable!==false&&numberKeys(e as Entry).length).map(e=>({...e,rule:inUse.get(e.id)||null}))
    .sort((a,b)=>Number(!a.rule)-Number(!b.rule));
  const shared=people.map(p=>new Set(p.tags||[])).reduce<Set<string>|null>((a,t)=>a?new Set([...a].filter(x=>t.has(x))):t,null);
  const tag=[...(shared||[])][0];
  formDialog(`Set a rule number for ${plural(people.length,'person','people')}`,'Proposed for approval by another person; nothing applies until then.',form=>{
    const rule=select('Rule',rules.map(e=>({value:e.id,label:e.name,group:e.rule?'In use':'Not used yet'})),rules[0]?.id||'');
    const sessionFields=bulkContext().conditionFields(CONDITION_FIELD_DEFINITIONS.filter(d=>d.session).map(d=>d.key));
    const where=conditionBuilder(sessionFields,null,{startEmpty:true,context:'rule'});
    const numbers=el('div',{class:'bulk-numbers'});let inputs:[string,HTMLInputElement][]=[];
    const drawNumbers=()=>{const e=rules.find(x=>x.id===rule.input.value);inputs=numberKeys(e).map(k=>{const f=field(NUMBER_WORDS[k]||k,'number','',{step:'any'});return [k,f.input] as [string,HTMLInputElement];});
      numbers.replaceChildren(...inputs.map(([,i])=>i.closest('.field')||i));};
    rule.input.addEventListener('change',drawNumbers);drawNumbers();
    const lines=select('Lines',[{value:'group',label:tag?`One line for #${tag}`:`One line for these ${people.length} people`},{value:'each',label:'One line each'}],'group');
    const from=field('Valid from (optional)','date','').input;
    const reason=area('Reason','',2);reason.input.required=true;reason.input.minLength=3;
    rule.wrap.classList.add('span-2');
    // Groups of controls are named by their legend (a bare label named nothing); the date is the shared date field.
    form.append(rule.wrap,el('fieldset',{class:'multi-field span-2'},el('legend',{},'Where'),where.root),el('fieldset',{class:'multi-field span-2'},el('legend',{},'Numbers'),numbers),
      lines.wrap,from.closest('.field')||from,reason.wrap);
    return async()=>{
      const e=rules.find(x=>x.id===rule.input.value);if(!e)throw new Error('Choose a rule.');
      // The lines join the rule as saved: unsaved edits of it on the Rules page would be lost, not proposed.
      const read=catalogue.entries.find(x=>x.id===e.id);if(read&&changesOf(read as unknown as RuleEntry))throw new Error(`${e.name} has unsaved edits on the Rules page: propose or discard them first.`);
      const nums=Object.fromEntries(inputs.filter(([,i])=>i.value!=='').map(([k,i])=>[k,{value:Number(i.value)}]));
      if(!Object.keys(nums).length)throw new Error('Give at least one number.');
      const who=(c:Conditions['conditions'][number]):Conditions=>({match:'all',conditions:[c]});
      // Each line starts from the rule's own default line (its measure and period), then who, where and the numbers.
      const line=(w:Conditions)=>({...e.default_line,id:crypto.randomUUID(),who:w,...(where.read()?{where:where.read()}:{}),numbers:nums,...(from.value?{starts_on:from.value}:{})});
      const add=lines.input.value==='each'?people.map(p=>line(who({field:'person_ids',op:'any',values:[p.id]})))
        :[line(who(tag?{field:'tags',op:'any',values:[tag]}:{field:'person_ids',op:'any',values:people.map(p=>p.id)}))];
      await runBulk({collection:'policy_lines',noun:['line','lines'],governed:true,title:`Propose ${e.name}`,onDone,
        // A rule in use gets the lines added to its current version; a rule not used yet is proposed with them.
        body:{item_kind:'rule.propose',items:[e.rule?{target_id:e.rule.id,expected_revision:e.rule.revision,payload:{add_lines:add}}
          :{payload:{catalogue_id:e.id,catalogue_version:e.catalogue_version,lines:add}}],reason:reason.input.value}});
    };
  },'Preview');
}
