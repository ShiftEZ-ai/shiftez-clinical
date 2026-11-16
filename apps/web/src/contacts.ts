/** Contact types' words and who sees one, in one place (D68). Labels are keyed by the contract's values, so a new kind
 *  or visibility mode fails the build here; the rows themselves (names, order, visibility) are configuration. */
import {roleLabel,grants,CAPABILITY} from './dom.js';
import type {ContactTypeInput,ContactVisibility} from './generated.js';

export const CONTACT_KINDS:Record<ContactTypeInput['kind'],string>={phone:'Phone',pager:'Pager',extension:'Extension',email:'Email'};
export const CONTACT_VISIBILITY:Record<NonNullable<ContactVisibility['mode']>,string>={everyone:'Everyone',roles:'Chosen roles',self_and_admins:'Only the person and admins'};
/** "Everyone", "Coordinator, Admin" or "Only the person and admins": who sees a contact type. */
export const contactAudience=(v:ContactVisibility|undefined)=>v?.mode==='roles'?(v.roles||[]).map(roleLabel).join(', ')||'Nobody':CONTACT_VISIBILITY[v?.mode||'self_and_admins'];
/** Whether a reader sees a type, as the server's disclosure filter reads its visibility (services/contacts.py
 *  `permits`; tests/test_client_contacts.py keeps them equal). Only offers an empty contact for editing: the server
 *  checks every read and save itself. */
export function contactVisibleTo(v:ContactVisibility|undefined,reader:{roles:readonly string[];person_id?:string|null},personId:string){
  // A linked person always sees their own value (6ab444a2e); the mode only narrows what others see.
  if(reader.person_id&&reader.person_id===personId)return true;
  const mode=v?.mode||'self_and_admins';
  return mode==='everyone'||mode==='roles'&&reader.roles.some(r=>(v?.roles||[]).includes(r as never))
    ||mode==='self_and_admins'&&grants(reader.roles,CAPABILITY.ADMIN);
}
