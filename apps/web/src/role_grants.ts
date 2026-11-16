import {EXPLICIT_ROLES,PERMISSIONS,ROLE_LABELS} from './generated.js';

/** Why these roles may not give (or take back) a role, in words, or '' when they may: the server's rule
 *  (apps/api/services/access.py require_role_change) read from the contract. An explicit role (EXPLICIT_ROLES: Auditor,
 *  Developer) needs `access.system_roles`; any other role needs `role.grant`. maintenance: a role that can't be given is
 *  shown with this reason, never left out. */
export function grantRefusal(role:string,roles:readonly string[]):string{
  const holders:readonly string[]=PERMISSIONS[(EXPLICIT_ROLES as readonly string[]).includes(role)?'access.system_roles':'role.grant'];
  if(holders.some(r=>roles.includes(r)))return '';
  const who=holders.map(r=>ROLE_LABELS[r as keyof typeof ROLE_LABELS]??r).join(' or ');
  return `Only ${/^[AEIOU]/.test(who)?'an':'a'} ${who} can grant this`;
}
