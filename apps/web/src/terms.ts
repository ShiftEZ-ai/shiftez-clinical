import {grants,CAPABILITY,plural} from './dom.js';
/** A long wait's words (DL-045): kept in loading.ts, which imports nothing (api.ts loads it before any page module). */
export {WAIT_WORDS,timedOutWords} from './loading.js';

/** One name per concept. Navigation, page titles, the command menu and forms use these words, and so do the
 *  user guides. API and field names are unchanged (a rota is still a `workspace` there). */
/** How a person takes part in a session (participation), in words. */
export const PARTICIPATION: Record<string, string> = {staff: 'Staff member', trainee: 'Trainee', supervisor: 'Supervisor'};

/** SW3-A-10: what a coordinator who is not the period rota's Rota Coordinator cannot do to its sessions, one phrase per
 *  control the server refuses (planning_groups.guard_changes), each with its own preposition. A session's type includes
 *  the type's own fields. Shown where forms.ts sessionsHeld says so. */
const ROTA_COORDINATOR_ACTIONS = {
  add: "add sessions to the period's rota",
  remove: "remove sessions from the period's rota",
  close: "close or reopen sessions on the period's rota",
  lock: "lock or unlock sessions on the period's rota",
  define: "change a session's type, time, place or clinics on the period's rota",
} as const;
export type RotaCoordinatorAction = keyof typeof ROTA_COORDINATOR_ACTIONS;
/** Who may (DL-040's names drop in here).
 *  shortcut: no names: a coordinator who is not an admin cannot read who the Rota Coordinators are
 *  (planning_groups.read_settings gives ids only). Name them once the reply carries their names. */
const ROTA_COORDINATOR = 'the Rota Coordinator';
/** The one sentence: "Only the Rota Coordinator can <action>." */
export const onlyRotaCoordinator = (action: RotaCoordinatorAction) => `Only ${ROTA_COORDINATOR} can ${ROTA_COORDINATOR_ACTIONS[action]}.`;

export const TERMS = {
  rota: 'Rota', rotas: 'Rotas',
  checkRota: 'Check rota', rotaCheck: 'Rota check', checked: 'Checked', needsCheck: 'Needs a check', notChecked: 'Not checked',
  backgroundCheck: 'Full rota check', runBackgroundCheck: 'Check the whole rota',
  ruleCheck: 'Rule check', checkRuleAgainstRotas: 'Check rule against rotas',
  // A disabled Propose says why (Rules review maintenance).
  nothingToPropose: 'Nothing to propose yet: change a line first.', nothingChanged: 'Nothing changed yet.',
  keepBestStopped: 'Stopping: the best result so far is kept.',
  cannotChangeRota: 'You can’t change this rota.', runGoing: 'Auto-schedule is running.', needsDraft: 'Needs an open draft rota.',
  // A rule replaced or proposed while someone had an unsent change to it (Design Lead rules the words).
  changedWhileEditing: (what:string)=>`${what} changed while you were editing`,
  unsentChangeKept: 'Your unsent change is kept to compare; it is not in the rule.',
  ruleNow: 'Rule now', yourChange: 'your change', discardUnsentChange: 'Discard my unsent change',
  autoSchedule: 'Auto-schedule', runAutoSchedule: 'Run auto-schedule',
  announcements: 'Announcements', newAnnouncement: 'New announcement', addAnnouncement: 'Add an announcement',
  rank: 'Rank', ranks: 'Ranks', accessRole: 'Access role',
  inbox: 'Inbox', auditLog: 'Audit log',
  offerInReturn: 'Offer a duty in return',
  acceptDuty: 'Accept duty',
  closeSession: 'Close session', reopenSession: 'Reopen session',
  returnToCoordinator: 'Return to coordinator',
  disableSignIn: 'Disable sign-in', enableSignIn: 'Enable sign-in',
  newRequest: 'New request',
  // A session is a time and place in a rota that needs people; a duty is one person's place in a session. "Assignment"
  // is the API's word only (I-04): counts of people's work read "duties", counts of rota slots "sessions".
  session: 'Session', sessions: 'Sessions', duty: 'Duty', duties: 'Duties',
  // A person link this reader cannot open: not shown to them, or since removed (the page cannot tell, so neither is claimed).
  personUnavailable: "That person can't be shown.",
  // DL-037 (fail-visible): the explainer could not word a rule; its check is unchanged. The row line, and the panel's count.
  ruleUnexplained: 'Can’t explain this rule right now. Its check still applies.',
  rulesUnexplained: (n:number)=>`${plural(n,'rule')} can’t be explained right now`,
  // The short form beside a rule's name where no sentence is drawn (the Rules list grouped by family).
  ruleUnexplainedShort: 'Can’t explain right now',
  // DL-009 (D38): a stored series may still hold the retired "close when every usual assignee is away"; it changes nothing.
  retiredCloseSetting: 'No longer closes sessions: they stay open when every usual assignee is away.',
} as const;

/** A staff sign-in not linked to a staff profile (SCA-02): Home, Schedule and Requests say why personal work is missing
 *  and who fixes it. Nothing is inferred from names; only an administrator's explicit link binds the account. */
export const UNLINKED_STAFF = {
  title: 'Your sign-in isn’t linked to a staff profile',
  text: 'Your duties and requests appear here once an administrator links your account to your staff profile in Access & approvals.',
} as const;
/** Only an account whose own work is personal (duties, requests) needs a profile: an admin holds every role but the explicit
 *  ones (EXPLICIT_ROLES; apps/api/auth.py), staff included, without being a staff member, so it never sees this (maintenance). */
export const unlinkedStaff = (me: {roles: readonly string[]; person_id?: string | null}) => grants(me.roles,CAPABILITY.STAFF) && !grants(me.roles,CAPABILITY.ADMIN) && !me.person_id;

/** Requests: every kind (leave, unavailability, weekly patterns, wishes, swaps, other requests) shares one status flow.
 *  The server maps each record's own state to a stage (apps/api/services/availability.py REQUEST_STAGE and its
 *  siblings); these are the stage's words, and its badge tone is statusTone(stage). "waiting" reads statusLabel('submitted')
 *  (dom.ts), the one term for every record waiting on a decision. */
export const REQUEST_STAGES = {
  reply: 'Waiting for colleague', accepted: 'Accepted by colleague', open: 'Open', approved: 'Approved', declined: 'Declined', withdrawn: 'Withdrawn', expired: 'Expired',
} as const;
export type RequestStage = keyof typeof REQUEST_STAGES | 'waiting';
export const REQUEST_TYPES = {leave: 'Leave', weekly: 'Unavailable', wish: 'Wish', swap: 'Swap', other: 'Other request'} as const;
export type RequestType = keyof typeof REQUEST_TYPES;

/** Two different "ending" windows on Rules, on purpose (RUL-03): a rule's list flag warns 30 days ahead (time to renew
 *  it before rotas are planned past it); a line's Valid filter groups lines ending within 60 days (two planning months). */
export const RULE_ENDING_DAYS = 30, LINE_ENDING_DAYS = 60;
