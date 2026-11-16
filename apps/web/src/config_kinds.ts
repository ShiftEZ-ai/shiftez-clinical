import {TERMS} from './terms.js';

/** What one kind of configuration record is, in one place (C-35): its section name, one item in words, its address,
 *  its Configuration group, the one sentence saying what it is for (empty states, the Setup guide and the command
 *  menu), the create button's words and its colour (`type`: duty-type colours, `own`: a colour the record stores,
 *  `auto`: a stable colour from its id) and the kinds a record cannot be saved without (`needs`). Settings pages and the
 *  Setup guide have no records, so no purpose. */
export interface ConfigKind {label:string;singular?:string;address?:string;group?:string;purpose?:string;
  /** What the kind changes in scheduling: a behaviour fact, always shown with the list (never behind ⓘ). */effect?:string;create?:string;colour?:'type'|'own'|'auto';settings?:boolean;needs?:string[]}

export const CONFIG_KINDS:Record<string,ConfigKind>={
  setup:{label:'Setup guide',group:'Start'},
  assignment_types:{label:'Duty types',singular:'duty type',group:'Scheduling',colour:'type',purpose:'Each session you schedule, with its hours, staffing and colour.'},
  recurring_series:{label:'Recurring sessions',singular:'recurring session',group:'Scheduling',needs:['assignment_types','locations','services'],purpose:'Weekly clinics that repeat, so new rotas start filled in.'},
  operating_calendars:{label:'Opening hours',singular:'opening hours',address:'opening-hours',group:'Scheduling',create:'Set opening hours',purpose:'When clinics and services open, so sessions fit real hours.'},
  // Its own page (scheduling_groups.ts) reads its versioned rows, so no record count here (settings).
  scheduling_groups:{label:'Scheduling groups',singular:'scheduling group',address:'scheduling-groups',group:'Scheduling',settings:true,
    purpose:'Each group schedules its own people in the shared rota; people in no group are scheduled by the Rota Coordinators.'},
  resource_pools:{label:'Resource pools',singular:'resource pool',group:'Scheduling',purpose:'Shared rooms or equipment that sessions draw on; without one nothing is capped.'},
  locations:{label:'Locations',singular:'location',group:'Places & services',purpose:'Where sessions happen: UCC, clinic rooms, remote.'},
  location_types:{label:'Location types',singular:'location type',group:'Places & services',colour:'auto',purpose:'Kinds of place (clinic room, ward) so duty types and rules can name them.'},
  services:{label:'Services',singular:'service',group:'Places & services',colour:'own',purpose:'The care you provide; eligibility says who may work each one.'},
  categories:{label:'Ranks',singular:'rank',group:'People',colour:'own',purpose:'Ranks such as consultant or resident; rules and staffing refer to them.'},
  capabilities:{label:'Skills & capabilities',singular:'skill',group:'People',purpose:'Verified skills such as supervision or procedures that some duties require.'},
  leave_types:{label:'Leave types',singular:'leave type',group:'People',colour:'own',purpose:'The kinds of leave people choose when they ask for or record leave.'},
  contact_types:{label:'Contact types',singular:'contact type',group:'People',purpose:'The numbers and addresses a person card shows: who sees each, who edits it and whether it dials in one tap.'},
  names:{label:'Names & codes',group:'People',settings:true},
  tags:{label:'Tags',singular:'tag',group:'Department',purpose:'Labels such as VIP_Clinics for people, clinics, places and rules; rules, filters and saved views use them.'},
  date_sets:{label:'Holidays & dates off',singular:'date set',address:'holidays',group:'Department',purpose:'Public holidays and other named days off; rules and opening hours can follow them.'},
  department:{label:'Department settings',group:'Department',settings:true},
  notifications:{label:'Notifications',group:'Department',settings:true},
  auto_schedule:{label:'Auto-schedule & scoring',group:'Department',settings:true},
  integrations:{label:'Integrations',group:'Department',settings:true},
  sign_in:{label:'Sign-in',address:'sign-in',group:'Department',settings:true},
  ai_provider:{label:'AI provider',address:'ai-provider',group:'Department',settings:true},
  department_content:{label:TERMS.announcements,singular:'announcement',address:'announcements',group:'Department',purpose:'What staff see first on Home.'},
  event_types:{label:'Event types',singular:'event type',address:'event-types',group:'Department',colour:'own',purpose:'Kinds of department event, such as teaching or meetings: their usual time, who they are for and whether they take people off duties.'},
  // Programmes (their own page) and people share the same words.
  programmes:{label:'Programmes',singular:'programme',purpose:'Training programmes (e.g. Family Medicine residency). Members, cohorts and rotations hang off a programme.'},
  programme_memberships:{label:'Programme memberships',singular:'programme membership',purpose:'Who is in which programme and when. Programme rules and the academic calendar use these dates.'},
  mentorships:{label:'Attachments',singular:'attachment',purpose:'A trainee attached to a consultant for a period: when both are on a session (of the chosen duty types and clinics) the consultant supervises them.',effect:'Required: auto-schedule always pairs them and the rota check flags another pairing; preferred: auto-schedule prefers it. It grants no supervision authority.'},
  cohorts:{label:'Cohorts',singular:'cohort',purpose:'Groups of trainees who rotate together, such as a year group.'},
  rotation_types:{label:'Rotation types',singular:'rotation type',purpose:'Kinds of rotation, such as Emergency or Research. Rules can apply to people while they are on a rotation of a type; whether a rotation keeps someone away (external) is set on each rotation.'},
  rotations:{label:'Rotations',singular:'rotation',purpose:'When a person is away on rotation.',effect:'External rotations block overlapping duties.'},
  programme_calendar_entries:{label:'Academic calendar',singular:'academic calendar entry',purpose:'Teaching days, exams and holidays for a programme.',effect:'They inform planning but never create leave.'},
  rotation_templates:{label:'Rotation templates',singular:'rotation template',purpose:'Prebuilt rotation plans: blocks in order, each with its rotation type or service and length. Plan rotations on the board starts from one.'},
  board:{label:'Rotation board'},
  people:{label:'People',singular:'person'},
};

const kindOf=(kind:string):ConfigKind|undefined=>CONFIG_KINDS[kind];
/** A kind's section name ("Duty types"). */
export const kindLabel=(kind:string)=>kindOf(kind)?.label||kind;
/** One item of a kind, in words ("duty type"); "record" for a kind with none. */
export const singularOf=(kind:string)=>kindOf(kind)?.singular||'record';
/** What a kind is for and what it changes, in full (empty states, the Setup guide, the command menu; '' for settings pages). */
export const purposeOf=(kind:string)=>[kindOf(kind)?.purpose,kindOf(kind)?.effect].filter(Boolean).join(' ');
/** The plain explanation only (contextual help beside a list's name). */
export const aboutOf=(kind:string)=>kindOf(kind)?.purpose||'';
/** The behaviour fact only, shown visibly under a list's name. */
export const effectOf=(kind:string)=>kindOf(kind)?.effect||'';
/** The create button's words ("New duty type", "Set opening hours"): "New <thing>" starts a record everywhere (maintenance). */
export const createLabel=(kind:string)=>kindOf(kind)?.create||'New '+singularOf(kind);
/** A section's address word (#configuration/opening-hours); the list's own name still opens it. */
export const addressOf=(kind:string)=>kindOf(kind)?.address||kind;
export const kindAt=(part:string)=>Object.keys(CONFIG_KINDS).find(k=>CONFIG_KINDS[k].address===part)||part;
/** Settings pages: no record list, so no count. */
export const isSettings=(kind:string)=>!!kindOf(kind)?.settings;
/** Configuration's nav groups, in order, with their kinds in order. */
export const CONFIG_GROUPS:[string,string[]][]=[...new Set(Object.values(CONFIG_KINDS).map(k=>k.group).filter(Boolean) as string[])]
  .map(group=>[group,Object.keys(CONFIG_KINDS).filter(k=>CONFIG_KINDS[k].group===group)]);
