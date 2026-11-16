import type {Me} from './types.js';
import type {SelfApprovalInput} from './generated.js';

/** Decisions that normally need a second person (packages/contracts/schemas.py INDEPENDENT_ACTIONS). */
export type IndependentAction=SelfApprovalInput['actions'][number];

/** May this person decide this work? Always when someone else made it; their own work only under an active
 *  self-approval permission for `action` (/api/me `self_approval`). The one place screens ask before offering a decision;
 *  the server decides (apps/api/services/independence.py) and still requires a real reason. */
export const mayDecide=(me:Pick<Me,'self_approval'>,action:IndependentAction,own:boolean)=>!own||!!me.self_approval?.includes(action);
