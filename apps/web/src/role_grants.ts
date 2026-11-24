import {ROLE_GRANT_PERMISSIONS,PERMISSIONS,ROLE_LABELS} from './generated.js';

/** Why these roles may not give (or take back) a role, in words, or '' when they may: the server's rule
 *  (apps/api/services/access.py require_role_change) reads from the contract. A role that cannot be given is
 *  shown with this reason, never left out. */
export function grantRefusal(role:string,roles:readonly string[]):string{
  const permission=ROLE_GRANT_PERMISSIONS[role as keyof typeof ROLE_GRANT_PERMISSIONS];
  if(!permission)return "Unknown role";
  const holders:readonly string[]=PERMISSIONS[permission];
  if(holders.some(r=>roles.includes(r)))return '';
  const who=holders.map(r=>ROLE_LABELS[r as keyof typeof ROLE_LABELS]??r).join(' or ');
  return `Only ${/^[AEIOU]/.test(who)?'an':'a'} ${who} can grant this`;
}
