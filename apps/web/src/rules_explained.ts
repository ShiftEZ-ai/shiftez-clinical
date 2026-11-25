/* The explained-rules projection (UX-WHY-PANEL CONTRACT §10; the server's rule explainer): a rota's sentences and
 * conflicts for the rules panel, and the department's rules by question for the Rules page. The server owns every
 * word, the A–F groups and their order; this file only reads them, in the generated contract's types. */
import {get,keptRead,forgetRead,type KeptReadOptions} from './api.js';
import {el,icon} from './dom.js';
import {TERMS} from './terms.js';
import type {ExplainedConflict,ExplainedRule,RulesExplainedResponse,DepartmentRulesExplainedResponse,DepartmentExplainedRule,ExplainedPeopleTable} from './generated.js';

export type {ExplainedRule,ExplainedPeopleTable};
/** Two rules that can't both hold (CONTRACT §6): the server's whole sentence, the two lines and rules, their
 *  department-wide line numbers (`line_refs`) and the sessions it reaches (`reach.sessions_count`). */
export type Conflict=ExplainedConflict;
export type DepartmentRules=DepartmentRulesExplainedResponse;
export type RuleSummary=DepartmentExplainedRule;

// Kept across rebuilds of the page (api.ts keptRead; maintenance). The department's explainer reads the rules, the lists they
// name and who may read them: only those live scopes drop its answers. A rota's also says whether each rule holds in it
// ("Not met twice"): a change to that rota (its id) or a publication drops it too, and the person's own save drops
// every answer (api.ts). A failed read is not kept: the next draw asks again.
const RULE_SCOPES=['rules','catalogue','people','settings','access'] as const;
const EXPLAINED:KeptReadOptions={scopes:RULE_SCOPES,cap:4};
const ROTA_EXPLAINED:KeptReadOptions={scopes:[...RULE_SCOPES,'publication'],byId:['workspace'],cap:4};
/** A whole rota's sentences and conflicts, one read per rota version (`key`: its id, revision, check and fingerprint,
 *  planner_rules.ts keyOf), for the panel's rows. Exported for tests/test_client_kept_reads.py. */
export function rotaSentences(workspace:string,key:string):Promise<RulesExplainedResponse>{
  const url=`/api/workspaces/${encodeURIComponent(workspace)}/rules-explained`;
  return keptRead(`${workspace}|${key}`,()=>get<RulesExplainedResponse>(url),ROTA_EXPLAINED).p;
}
/** The department's rules by question for a day (no rota, so no statuses), with its conflicts and people table. */
export function departmentRules(at:string,read:typeof get=get,workspace?:string):Promise<DepartmentRules>{
  const url=`/api/rules-explained?group_by=question&at=${encodeURIComponent(at)}`;
  if(workspace)return read<DepartmentRules>(`${url}&workspace_id=${encodeURIComponent(workspace)}`);
  return keptRead(url,()=>read<DepartmentRules>(url),EXPLAINED).p;
}
/** Rules page Retry drops only this day's held question read; unrelated kept answers continue to share their requests. */
export function forgetDepartmentRules(at:string){
  forgetRead(`/api/rules-explained?group_by=question&at=${encodeURIComponent(at)}`,EXPLAINED);
}
/** DL-037: a rule the explainer could not word (`unexplained`) says so in its row, in the warning tone with the warning
 *  icon, whatever its level; its status, safety mark and Open rule stay as they are. Both surfaces draw this one line, and
 *  their counts (`words`) in the same tone. */
export const unexplainedLine=(words:string=TERMS.ruleUnexplained)=>el('span',{class:'iss-line inline rule-unexplained'},el('span',{class:'iss-mark warning','aria-hidden':'true'},icon('alert')),
  el('span',{class:'iss-words warning'},words));
