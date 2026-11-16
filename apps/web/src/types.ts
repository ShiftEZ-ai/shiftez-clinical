import type {QuotaReport} from './quotas.js';
import type { TypeDefinition, SeriesDefinition, DateSetMarker, PublishedScheduleSummary } from './generated.js';
/** A named date on a day (date_set_markers of the schedule and calendar reads). */
export type {DateSetMarker};

// Catalogue rows have heterogeneous, schema-validated attributes. Privileged writes
// are validated again by the server, not trusted from this read-view adapter.
export interface Row { id: string; revision: number; name?: string; display_name?: string; [key: string]: any }
/** A person's row, branded by type only (nothing at run time): `labelOf` refuses it, so a person is named through
 *  person_label.ts (their label, or `fullNameOf` for the full name), never as a record (audit C19). */
declare const personRow: unique symbol;
export type PersonRow = Row & { readonly [personRow]: true };
/** Any row that is not a person's (what `labelOf` takes). */
export type NotPerson = { readonly [personRow]?: never };
export interface Catalogues { [kind: string]: Row[]; people: PersonRow[] }
export interface Me {
  identity_id: string; display_name: string; roles: string[]; person_id: string | null;
  /** Current active staff binding for personal swap consent, checked by the server. */
  staff_swap_access: boolean;
  organisation: Row & {name: string; timezone: string; week_start: number};
  department: Row & {name: string}; environment: string; deployment: string; production_qualified: boolean;
  /** The running server version, and release (version+commit: apps/api/version.py build()); the bundle's own is WEB_RELEASE. */
  version: string; release: string;
  /** The department's rule catalogue: 1 until its rules are converted to one rule per idea, then 2 (server-resolved). */
  catalogue_version: 1 | 2;
  /** Actions this person may currently decide for themselves (a self-approval permission covers them). */
  self_approval?: string[];
  /** In a training programme of this department: Programmes shows them My training. */
  trainee?: boolean;
  /** Set when this app manages the person's sign-in (they can change their password here). */
  account?: {username: string} | null;
  /** The department's duty types, light (id, created, explicit colour): their colours for roles without catalogues. */
  duty_types?: {id: string; created_at: string; colour: string}[];
}
export interface Finding {code: string; message: string; severity: string; shift_id?: string; person_id?: string; start?: string; end?: string; rule_id?: string; detail?: Record<string, unknown>;
  /** The rule's window reaches outside the rota (look-back/look-ahead); `reach` is set by plainFindings from the rota's start. */
  outside_rota?: boolean; window?: {start: string; end: string}; reach?: 'before' | 'after'}
export interface Evaluation {valid: boolean; error_count: number; warning_count: number; findings: Finding[]; coverage: Row[]; fingerprint?: string; quota_report?: QuotaReport}
export interface WorkspaceData {/** Only the days in view (app.ts readWindow): never used for whole-rota work. */ partial?: boolean; workspace: Row; shifts: Row[]; current_fingerprint: string; current_input_revision: number; current_availability_revision: number; approvals: Row[]; wishes?: Row[]; review_progress: {route: {name:string;mode:string;route_id:string|null}|null;steps:Row[];pending_step:Row|null;complete:boolean;stale_inputs?:boolean};
  /** Schedule: a draft drawn inside the published layer. Days outside the draft are read-only context; sessions with
   *  `context: true` are published ones (grey, read-only). `published`: the days [from, until) a publication is in force. */
  context?: boolean; published?: [string,string][];
  /** The server's contributor test for the viewer (any edit, swap party or alias): approving is self-approval. */
  reviewer_involved?: boolean;
  /** The swap this amendment carries out; its check's scope is `published_changes`. */
  swap_id?: string | null;
  /** The department's named dates in view, by day (date_sets.ts dateMarks): from the published window's read. */
  date_set_markers?: Record<string,DateSetMarker[]>}
export interface PublicData {summary?: PublishedScheduleSummary; publication: Row; snapshot: {workspace: Row; shifts: Row[]; timezone: string; people: Record<string, Row>; locations: Record<string, Row>; services: Record<string, Row>}; original_validation: unknown; /** Current absences on its issued duties (published_operational.py), apart from the issued check. */ operational?: Row}
