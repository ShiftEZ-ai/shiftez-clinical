"""Bounded schema contract: reject unknown fields and unsupported semantics."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import re
from typing import Annotated, Any, ClassVar, Literal, Union, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError
from .condition_limits import MAX_COUNT_BUCKETS, check_count_bucket_budget
from .media_limits import MEDIA_LIMITS
from .audit import AUDIT_ACTION_CATALOGUE, AUDIT_GROUPS
from .notification_audiences import audience_definition
from packages.domain.worker_contract import OUTBOX_STATES

ID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=160)]
Email = Annotated[str, StringConstraints(pattern=r"^[^@\s\"]+@[^@\s\"]+\.[^@\s\"]+$", max_length=254)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
# A tag's length, and how many one person carries: the person form checks them under its field (generated.ts).
TAG_LIMITS = {"length": 24, "per_person": 30}
Tag = Annotated[str, Field(min_length=1, max_length=TAG_LIMITS["length"])]
Clock = Annotated[str, StringConstraints(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$|^24:00$")]
# The one list of roles: the server, the database check and the generated frontend types all read it.
RoleName = Literal["staff", "coordinator", "approver", "credentialer", "admin", "auditor", "programme_lead", "operator",
                   "installation_admin"]
ROLE_NAMES: tuple[str, ...] = get_args(RoleName)
TEAM_READER_ROLES: tuple[RoleName, ...] = ("admin", "coordinator", "approver", "programme_lead", "auditor")
SYSTEM_ROLES: tuple[RoleName, ...] = ("operator",)
INSTALLATION_ADMIN_ROLE: RoleName = "installation_admin"
EXPLICIT_ROLES: frozenset[RoleName] = frozenset((*SYSTEM_ROLES, "auditor", INSTALLATION_ADMIN_ROLE))
ROLE_LABELS: dict[RoleName, str] = {role: role.replace("_", " ").capitalize() for role in ROLE_NAMES}
ROLE_LABELS["operator"] = "Developer"
# A 503 is definite only when raised before any request write can commit; otherwise the client must confirm.
UNAVAILABLE_OUTCOME: dict[str, Literal['definite', 'unknown']] = {
    'CATALOGUE_BUSY': 'definite',
    'COMMAND_WRITE_TIMEOUT': 'unknown',
    'DATABASE_NOT_READY': 'definite',
    'DATABASE_UNAVAILABLE': 'unknown',
    'IDENTITY_PROVIDER_UNAVAILABLE': 'unknown',
    'LOGIN_UNCONFIGURED': 'definite',
    'MIGRATION_REQUIRED': 'definite',
    'OIDC_ALGORITHM': 'definite',
    'OIDC_CALLBACK': 'definite',
    'OIDC_CLIENT_AUTH_METHOD': 'unknown',
    'OIDC_CODE_REQUIRED': 'definite',
    'OIDC_DISCOVERY_INVALID': 'definite',
    'OIDC_ENDPOINT_REJECTED': 'definite',
    'OIDC_ISSUER_MISMATCH': 'definite',
    'OIDC_PKCE_REQUIRED': 'definite',
    'OIDC_SIGNING_KEY': 'definite',
    'OIDC_UNCONFIGURED': 'definite',
    'PUSH_CONTACT_REQUIRED': 'definite',
    'PUSH_KEY_UNAVAILABLE': 'definite',
    'PUSH_STORAGE_NOT_CONFIGURED': 'definite',
    'RULE_CATALOGUE_BUSY': 'definite',
    'SCORE_ISOLATION_UNAVAILABLE': 'definite',
    'SIGN_IN_SERVICE_UNAVAILABLE': 'unknown',
    'STATUS_UNAVAILABLE': 'definite',
    'WEB_NOT_BUILT': 'definite',
}
# Live invalidation is intentionally a small reader-facing vocabulary. Writers map their domain change to one scope;
# an unknown mapping stays silent and the periodic pulse remains the safe full-refresh fallback.
LiveScope = Literal["workspace", "publication", "rules", "catalogue", "people", "availability", "requests", "swaps", "inbox", "jobs", "settings", "access", "feedback"]
WorkerLiveness = Literal['running', 'not_responding', 'never_seen']
JobStatus = Literal['queued', 'running', 'succeeded', 'failed', 'cancelled']
WorkspaceDetailView = Literal['full', 'compact', 'schedule']

INTERNAL_CHECK_KEYS = frozenset({'_session_parts', '_person_parts'})
JOB_PRIVATE_CHECK_FIELDS = (*sorted(INTERNAL_CHECK_KEYS), 'fix_list_context', 'fix_list_state', 'fix_list_delta',
                            'input_manifest', 'score_dependencies', 'diagnostic_version')
# What the Schedule's whole-rota read leaves out: fields no client path reads (Design ruling a′, 20 Nov 2026).
SCHEDULE_DETAIL_FIELDS = {
    'session': ('series_snapshot', 'series_revision', 'series_modified', 'series_id', 'occurrence_on', 'lineage_id',
                'template_skipped', 'closure_reason', 'created_at', 'workspace_id'),
    'participant': ('created_at', 'source', 'source_actor_id', 'source_run_id', 'source_at', 'shift_id', 'revision'),
    'coverage': ('availability_basis',),
    'check': (*JOB_PRIVATE_CHECK_FIELDS, 'goals'),
}
WorkspacePart = Literal['shifts', 'validation']

# Draft edit replies are deliberately bounded.  A command that touches more than
# these amounts returns ``delta: null`` and the reader performs its normal full
# workspace read.  The limits are protocol limits, not department assumptions.
EDIT_DELTA_LIMITS = {'shifts': 100, 'findings': 200, 'coverage': 200}


# Every catalogue reader must declare its audience; unknown kinds never default open.
CATALOGUE_READS: dict[str, dict[str, str]] = {
    **{kind: {'permission': 'catalogue.read'} for kind in (
        'contact_types', 'date_sets', 'leave_types', 'department_content', 'event_types', 'events',
        'operating_calendars', 'resource_pools', 'categories', 'capabilities', 'location_types',
        'locations', 'services', 'people', 'tags', 'assignment_types', 'programmes', 'rotation_types',
        'cohorts', 'rotations', 'recurring_series', 'programme_memberships', 'mentorships',
        'programme_calendar_entries', 'rotation_templates', 'memberships', 'eligibilities', 'grants',
        'closures', 'tag_members',
    )},
    **{kind: {'permission': 'rules.read'} for kind in ('policies', 'policy_lines', 'exceptions')},
}


PERMISSIONS: dict[str, tuple[RoleName, ...]] = {
    'notification.policy.update': ('admin',),
    'planning.configure': ('admin',),
    'planning.read': ('coordinator', 'approver', 'programme_lead', 'auditor'),
    'workspace.publish': ('coordinator', 'approver'),
    'planning.draft': ('coordinator',),
    'score_check.add_missing': ('coordinator',),
    'catalogue.read': ('admin', 'coordinator', 'approver', 'credentialer', 'auditor', 'programme_lead'),
    # Rules readers may list, inspect capabilities, and explain department rules.
    'rules.read': ('admin', 'coordinator', 'approver', 'auditor', 'programme_lead'),
    "schedule.read_team": ("admin", "coordinator", "approver", "programme_lead", "auditor"),
    "fairness.read_team": TEAM_READER_ROLES,
    "publications.read_team": ("coordinator", "approver", "auditor"),
    "date_sets.read_all": ("admin", "coordinator"),
    "access.system_roles": ("operator",),
    "access.installation_admin": (INSTALLATION_ADMIN_ROLE, *SYSTEM_ROLES),
    "department.create": SYSTEM_ROLES,
    "branding.configure": (INSTALLATION_ADMIN_ROLE, *SYSTEM_ROLES),
    "feedback.read_all": ("operator", "auditor"),
    "feedback.triage": ("operator",),
    "feedback.export_github": ("operator",),
    "integrations.github": ("operator",),
    "integrations.monitor": ("operator",),
    "ai.providers": ("operator",),
    "audit.read": ("operator", "auditor"),
    "system.operations": ("operator", "auditor"),
    "job.cancel": ("coordinator", "operator"),
    **dict.fromkeys(('workspace.archive', 'workspace.restore', 'workspace.delete_draft'), ('admin', 'coordinator')),
    # Bulk item authority shares this map with the command bus, receipts and generated registry.
    **dict.fromkeys((
        'person.anonymise', 'catalogue.create', 'catalogue.undo_create', 'catalogue.update',
        'catalogue.bulk_update', 'role.grant', 'role.revoke', 'role.restore', 'identity.disable',
        'identity.enable', 'department.bulk_settings',
    ), ('admin',)),
    **dict.fromkeys((
        'tag.members', 'membership.create', 'membership.bulk_create', 'membership.end', 'membership.update_dates',
        'membership.undo_create', 'membership.withdraw', 'rule.propose',
    ), ('admin', 'coordinator')),
    **dict.fromkeys((
        'eligibility.create', 'eligibility.bulk_create', 'eligibility.programme', 'eligibility.extend',
        'eligibility.end', 'eligibility.update_dates', 'eligibility.undo_create', 'eligibility.withdraw', 'grant.create',
        'grant.bulk_create', 'grant.revoke', 'grant.restore', 'grant.update_dates', 'grant.undo_create',
    ), ('credentialer',)),
    **dict.fromkeys((
        'availability.update', 'availability.undo_update', 'availability_pattern.decide', 'availability_pattern.undo', 'exception.create',
        'exception.create_many', 'shift.create', 'shift.update', 'shift.action', 'shift.copy', 'shift.undo_batch',
        'issues.resolve',
    ), ('coordinator',)),
    **dict.fromkeys((
        'policy.activate', 'policy.activate_many', 'policy.retire', 'policy.restore',
        'exception.approve', 'person.workload.activate', 'person.workload.retire', 'person.overtime.activate',
        'person.overtime.retire', 'calendar.protection.activate', 'calendar.protection.retire',
    ), ('approver',)),
    **dict.fromkeys((
        'request.decide', 'request.decide_many',
    ), ('approver', 'coordinator', 'programme_lead')),
    **dict.fromkeys(('availability.record', 'availability.retract', 'availability.restore', 'availability.undo_create'), ('coordinator', 'programme_lead')),
    **dict.fromkeys((
        'request.undo', 'bulk.apply', 'bulk.staged.drop',
    ), ROLE_NAMES),
    # Staging a broad change is itself a write; read-only staff and auditors may not create one.
    'bulk.stage': ('coordinator', 'approver', 'credentialer', 'admin', 'programme_lead', 'operator'),
    **dict.fromkeys((
        'availability_pattern.propose', 'availability_pattern.end', 'availability_pattern.restore', 'wish.create',
        'wish.update', 'wish.withdraw', 'wish.restore', 'wish.override',
    ), ('staff', 'coordinator')),
    **dict.fromkeys((
        'policy.decline', 'policy.restore_proposal', 'person.workload.decline', 'person.workload.restore_proposal',
        'person.workload.undo', 'person.overtime.decline', 'person.overtime.restore_proposal',
        'person.overtime.undo', 'calendar.protection.decline', 'calendar.protection.restore_proposal',
        'calendar.protection.undo',
    ), ('approver', 'coordinator', 'admin')),
    **dict.fromkeys((
        'exception.end', 'exception.undo',
    ), ('coordinator', 'approver')),
    **dict.fromkeys((
        'person.workload.propose', 'person.overtime.propose', 'calendar.protection.propose',
    ), ('coordinator', 'admin')),
    **dict.fromkeys(('email.settings', 'event_types.import_defaults'), ('admin',)),
    **dict.fromkeys((
        'email.microsoft.connect', 'email.microsoft.disconnect', 'event.occurrence.update',
        'department_content.remind',
    ), ('admin', 'coordinator')),
    **dict.fromkeys(('event.rsvp', 'department_content.seen', 'notification.acknowledge'), ROLE_NAMES),
    **dict.fromkeys((
        'rotation.plan', 'rotation.copy', 'rotation.undo_plan', 'rotation.remove',
    ), ('admin', 'coordinator', 'programme_lead')),
    'rotation.request_change': ('staff', 'coordinator', 'programme_lead'),
    'rotation.swap': ('staff', 'coordinator', 'programme_lead'),
    **dict.fromkeys((
        'trainee_note.create', 'trainee_note.update', 'trainee_note.delete', 'trainee_note.restore',
    ), ('programme_lead',)),
    'catalogue.department': ('admin', 'coordinator'),
    'catalogue.programmes': ('admin', 'coordinator', 'programme_lead'),
    'request.withdraw': ROLE_NAMES,
}
PERMISSIONS['programme_membership.withdraw'] = PERMISSIONS['catalogue.programmes']
PERMISSIONS['rotation.restore'] = PERMISSIONS['rotation.remove']
for _family in ('membership', 'eligibility', 'programme_membership'):
    PERMISSIONS[_family + '.restore'] = PERMISSIONS[_family + '.withdraw']
# UI access gates use these generated capability keys instead of copying role literals.  The aliases are
# intentionally backed by the same map the server uses for command enforcement. Keep the command permission
# registry unchanged: `/api/me.permissions` is an action list, while this UI map also carries role-shaped gates.
PERMISSION_NAMES: tuple[str, ...] = tuple(PERMISSIONS)
ROLE_GRANT_PERMISSIONS = {
    role: ("access.installation_admin" if role == INSTALLATION_ADMIN_ROLE else
           "access.system_roles" if role in EXPLICIT_ROLES else "role.grant")
    for role in ROLE_NAMES
}

CAPABILITIES: dict[str, tuple[RoleName, ...]] = {
    **PERMISSIONS,
    **{f'ui.{role}': (role,) for role in ROLE_NAMES},
    # Only these roles open the Rules page, and each must also have rules.read.
    'ui.rules': ('admin', 'coordinator', 'approver'),
}
# Known pages, tabs, legacy page aliases and literal subroutes; never record IDs or query values.
APP_ROUTE_PARTS: tuple[str, ...] = (
    "access", "audit", "availability", "benchmark", "bulk", "calendar", "configuration", "data", "directory", "exports",
    "fairness", "feedback", "governance", "history", "home", "imports", "inbox", "insights", "issues",
    "operations", "overview", "people", "planner", "programmes", "published", "related", "reports", "requests",
    "routes", "rules", "schedule", "setup", "swaps", "system", "timeline", "today", "workload",
)
Strength = Literal['low', 'medium', 'high', 'critical']
# Numeric score inputs are bounded for solver arithmetic, rather than an administrative 100-point scale.
MAX_SCORE_INPUT = 1_000_000
MAX_DB_INT = 2_147_483_647  # PostgreSQL Integer ceiling, not a policy limit.
BalanceShareBy = Literal['available_days', 'equal', 'offered', 'supervisable_sessions']
DEFAULT_FAIR_SHARE_WINDOW = {'period': 'rolling', 'period_months': 12, 'period_days': None}
SchedulingFactKind = Literal['workload', 'target', 'overtime_offer', 'protected_time']
RequestKind = Literal['leave', 'clinic_change', 'meeting', 'administrative', 'overtime_availability', 'rotation_change', 'absence_report']
REQUEST_KINDS: tuple[str, ...] = get_args(RequestKind)
REQUEST_STAGE_LABELS = {'waiting': 'Waiting for approval', 'reply': 'Waiting for colleague',
    'accepted': 'Accepted by colleague', 'open': 'Open', 'approved': 'Approved', 'declined': 'Declined',
    'withdrawn': 'Withdrawn', 'expired': 'Expired'}
EventAttendance = Literal['optional', 'expected', 'must_acknowledge']
# Sortable Requests facts: shared by API query validation and the generated collection controls.
RequestSortField = Literal["type", "person", "when", "proposed_at", "decided"]
REQUEST_SORT_FIELDS: tuple[str, ...] = get_args(RequestSortField)
MAX_REQUEST_DECISIONS = 500
EVENT_HORIZON_MAX_DAYS = 3660
EVENT_OCCURRENCE_RANGE_MAX_DAYS = 366
WISH_RANGE_MAX_DAYS = 366  # Bound one admission/usage expansion; counts and department budgets remain configurable.
# The one list of decisions a self-approval permission may cover (see apps/api/services/independence.py).
IndependentAction = Literal["rota.approve", "rule.activate", "exception.approve", "request.decide", "route.decide", "skills.self"]
INDEPENDENT_ACTIONS: tuple[str, ...] = get_args(IndependentAction)
# SW3-B-18: the roles each action's own work needs, one tuple per step (author, then decider): a grantee must hold a
# role in every step, or the permission can never be used. Steps are the commands' own authority (PERMISSIONS);
# review routes are created by an admin and decided by an approver (apps/api/services/reviews.py). rota.approve has no
# fixed roles: its contributors include swap parties and its approval step role comes from the review route.
INDEPENDENT_ACTION_ROLES: dict[str, tuple[tuple[str, ...], ...]] = {
    'rota.approve': (),
    'rule.activate': (PERMISSIONS['rule.propose'], PERMISSIONS['policy.activate']),
    'exception.approve': (PERMISSIONS['exception.create'], PERMISSIONS['exception.approve']),
    'request.decide': (PERMISSIONS['request.decide'],),
    'route.decide': (('admin',), ('approver',)),
    'skills.self': (PERMISSIONS['eligibility.create'],),
}
assert set(INDEPENDENT_ACTION_ROLES) == set(INDEPENDENT_ACTIONS)
# The reason an ended rule is retired with (its dates are past, no rota covers them): retiring it this way brings back
# none of the rules it replaced, and an undo finds it by this reason.
ENDED_REASON = "Ended: its dates are past and no rota covers them"


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class JobErrorDetailEntry(Schema):
    rule_id: str | None = None
    rule_revision: int | None = Field(default=None, ge=1)
    field: str | None = None
    code: str | None = None
    message: str | None = None


class JobErrorDetail(Schema):
    code: str
    message: str
    details: list[JobErrorDetailEntry] = Field(max_length=50)
    total: int = Field(ge=0)


class LiveChange(Schema):
    scope: LiveScope
    id: str | None
    # A completed validation wakes the owning workspace and names the job whose
    # compact status/delta can settle it without rereading the whole rota.
    job_id: str | None = None
    # A committed planner edit may carry the same bounded reply fields.  These
    # stay optional so an omitted, stale, partial or oversized patch retains
    # the existing full-read invalidation path.
    delta: dict[str, Any] | None = None
    workspace: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    fix_list_delta: dict[str, Any] | None = None


class LivePulse(Schema):
    input_revision: int
    availability_revision: int
    activity: int
    roles: list[RoleName]
    unread: int
    as_of: datetime
    build: str | None
    department_id: ID
    changes: list[LiveChange] | None
    generation_progress: int | None = None


class WorkspaceCompactDetail(Schema):
    view: Literal['compact'] = 'compact'
    workspace: dict[str, Any]
    current_input_revision: int
    current_availability_revision: int
    current_fingerprint: str
    health: dict[str, Any]
    summary: dict[str, Any] | None
    review_progress: dict[str, Any]
    review_items: list[dict[str, Any]]
    reviewer_involved: bool
    can_change_fixed: bool
    can_redo: bool
    swap_id: ID | None
    approvals: list[dict[str, Any]]


class WorkspacePartQuery(Schema):
    part: WorkspacePart
    start: date
    end: date
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0)


class CoverageInterval(BaseModel):
    """Basic headcount remains evidence; places_* includes scoped/qualified-cover shortages."""
    model_config = ConfigDict(extra='allow')
    shift_id: str
    status: str
    places_needed: int | None = Field(default=None, ge=0)
    places_filled: int | None = Field(default=None, ge=0)
    places_open: int | None = Field(default=None, ge=0)


class WorkspacePartPage(Schema):
    workspace_id: ID
    revision: int
    part: WorkspacePart
    start: date
    end: date
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    has_more: bool
    coverage: list[dict[str, Any]]
    coverage_total: int
    coverage_has_more: bool


class DraftEditScope(Schema):
    """Rows replaced by an edit delta; omitted dimensions are unchanged."""
    shift_ids: list[ID] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['coverage'])
    person_ids: list[ID] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['coverage'])


class DraftEditFindings(DraftEditScope):
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['findings'])


class DraftEditCoverage(DraftEditScope):
    items: list[dict[str, Any]] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['coverage'])


class DraftEditDelta(Schema):
    """A bounded, revision-bound patch returned by a draft edit command.

    The client applies this only when ``base_revision`` is the revision it
    holds and ``revision`` is the revision in the command reply.  A command
    touching more than the protocol limits returns ``delta: null`` so the
    authoritative workspace read remains the fallback.
    """
    base_revision: int = Field(ge=0)
    revision: int = Field(ge=1)
    stale: bool = False
    shifts: list[dict[str, Any]] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['shifts'])
    removed_shift_ids: list[ID] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['shifts'])
    assignments: list[dict[str, Any]] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['shifts'])
    removed_assignment_ids: list[ID] = Field(default_factory=list, max_length=EDIT_DELTA_LIMITS['shifts'])
    findings: DraftEditFindings | None = None
    coverage: DraftEditCoverage | None = None
    summary_counts: dict[str, int] = Field(default_factory=dict)
    current_fingerprint: str = Field(min_length=1)
    current_input_revision: int | None = Field(default=None, ge=0)
    current_availability_revision: int | None = Field(default=None, ge=0)


class TimeResolveBatchInput(Schema):
    locals: list[Annotated[str, StringConstraints(min_length=16, max_length=16)]] = Field(min_length=1, max_length=1000)


class TimeResolveBatchResult(Schema):
    instants: list[str]
    timezone: str


class FairnessExposurePerson(Schema):
    person_id: ID
    name: str


class FairnessExposure(Schema):
    starts_on: date
    ends_on: date
    pairing: Literal['present', 'supervised']
    rows: list[FairnessExposurePerson]
    cols: list[FairnessExposurePerson]
    cells: list[list[float]]
    total_rows: int
    total_cols: int
    row_totals: list[float]


class MeAccount(Schema):
    username: str


class MeDutyType(Schema):
    id: ID
    created_at: datetime
    colour: str


class Me(Schema):
    """The signed-in identity and current department returned by GET /api/me."""

    identity_id: ID
    display_name: str
    department: dict[str, Any]
    organisation: dict[str, Any]
    catalogue_version: Literal[1, 2]
    roles: list[RoleName]
    permissions: list[str]
    person_id: ID | None
    staff_swap_access: bool
    trainee: bool = False
    self_approval: list[IndependentAction]
    account: MeAccount | None
    duty_types: list[MeDutyType]
    environment: str
    deployment: str
    production_qualified: bool
    version: str
    release: str
    bulk_staged_count: int = Field(ge=0, strict=True,
        description="Active staged changes owned by this identity in the current department that have not expired.")
    bulk_review_count: int = Field(ge=0, strict=True,
        description="Unexpired bulk changes in the current department waiting for this identity's independent review.")


# Department swap limits: the floor, the INTEGER storage ceiling (not policy) and new-department defaults.
SWAP_LIMITS = {"minimum": 1, "maximum": 2_147_483_647, "offer_window_days": 7, "open_offer_cap": 25}


WishKind = Literal['prefer_off', 'prefer_on', 'prefer_shift_type', 'avoid_shift_type',
                   'max_days_in_row', 'min_days_off_together', 'max_weekends_in_row', 'whole_weekends']
PATTERN_WISH_KINDS = frozenset({'max_days_in_row', 'min_days_off_together', 'max_weekends_in_row', 'whole_weekends'})

WISH_KINDS: tuple[str, ...] = get_args(WishKind)
MAX_WISH_POINTS = MAX_DB_INT
WishPoints = Annotated[int, Field(ge=1, le=MAX_WISH_POINTS, strict=True)]


WishOccurrenceID = ID | Annotated[str, StringConstraints(
    pattern=r"^standing:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[0-9]{4}-[0-9]{2}-[0-9]{2}$")]


class WishEffect(Schema):
    """One directional marker in a planner participant's wish_effects; unrelated wishes are omitted."""
    wish_id: WishOccurrenceID
    effect: Literal['grants', 'against']


class DraftWishOutcome(Schema):
    """A planner wish's draft_outcome, separate from its recorded publication outcome."""
    outcome: Literal['granted', 'partly', 'declined']
    honoured_days: int = Field(ge=0)
    total_days: int = Field(ge=1)


MONITOR_LIMITS = {"body_bytes": 64 * 1024, "page_size": 50, "status_bytes": 1024 * 1024,
                  "alert_lines": 200}


class MonitorTokenInput(Schema):
    rotate: Literal[True] | None = None
    token: None = None
    reason: Reason

    @model_validator(mode="after")
    def one_action(self):
        if (self.rotate is True) == ("token" in self.model_fields_set):
            raise ValueError("Choose either rotating or disabling the monitor token")
        return self


class MonitorIntegrationStatus(Schema):
    configured: bool


class MonitorTokenResult(MonitorIntegrationStatus):
    token: str | None = None


class MonitorEvent(Schema):
    # Provider-specific facts remain evidence; only these routing fields are interpreted.
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True, allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    state: Literal["up", "down"]
    event_id: str | None = Field(default=None, min_length=1, max_length=160)


class MonitorIncident(Schema):
    id: ID
    source: str
    state: Literal["up", "down"]
    received_at: datetime
    raw_payload: dict[str, Any]


class MonitorEventList(Schema):
    incidents: list[MonitorIncident]
    next_offset: int | None


class MonitorEventReceipt(Schema):
    accepted: bool
    duplicate: bool


class SystemStatusFiles(Schema):
    model_config = ConfigDict(str_strip_whitespace=False)
    health: str | None
    alerts: list[str]


FeedbackKind = Literal["problem", "suggestion", "confusing", "question"]
FeedbackStatus = Literal["new", "triaged", "in_progress", "fixed", "wont_fix", "duplicate"]
FEEDBACK_KINDS = get_args(FeedbackKind)
FEEDBACK_STATUSES = get_args(FeedbackStatus)
FEEDBACK_LIMITS = {"message": 4000, "expected": 2000, "view_state_bytes": 16 * 1024,
                   "screenshot_bytes": 2 * 1024 * 1024, "body_bytes": 3 * 1024 * 1024,
                   "recent_errors": 20, "recent_routes": 10, "route": 2000, "request_ids": 20, "user_agent": 300,
                   "reports_per_hour": 10, "screenshot_days": 180, "duplicate_days": 30,
                   "page_size": 50}
FeedbackRoute = Annotated[str, StringConstraints(
    strict=True, min_length=1, max_length=FEEDBACK_LIMITS["route"], pattern=r"^(?:#[A-Za-z0-9_-]+|#?/)[^\x00-\x1f\\]*$")]


class FeedbackViewport(Schema):
    w: int = Field(ge=1, le=32768)
    h: int = Field(ge=1, le=32768)


class FeedbackMark(Schema):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def inside_image(self):
        if self.x + self.w > 1 or self.y + self.h > 1:
            raise ValueError("The marked area must fit inside the screenshot")
        return self


class FeedbackError(Schema):
    at: datetime
    message: str = Field(max_length=300)
    source: str = Field(max_length=100)


class FeedbackScreenshot(Schema):
    media_type: Literal["image/png", "image/webp"]
    data: str = Field(min_length=1, max_length=4 * ((FEEDBACK_LIMITS["screenshot_bytes"] + 2) // 3))


class FeedbackContext(Schema):
    route: FeedbackRoute
    recent_routes: list[FeedbackRoute] = Field(default_factory=list, max_length=FEEDBACK_LIMITS["recent_routes"])
    view_state: dict[str, Any] = Field(default_factory=dict)
    page_title: str = Field(default="", max_length=160)
    app_version: str = Field(default="", max_length=80)
    build_commit: str = Field(default="", max_length=80)
    environment: Literal["dev", "production"] = "dev"
    role_names: list[RoleName] = Field(default_factory=list, max_length=len(ROLE_NAMES))
    viewport: FeedbackViewport = Field(default_factory=lambda: FeedbackViewport(w=1, h=1))
    theme: Literal["light", "dark", "system"] = "system"
    user_agent: str = Field(default="", max_length=FEEDBACK_LIMITS["user_agent"])
    recent_errors: list[FeedbackError] = Field(default_factory=list, max_length=FEEDBACK_LIMITS["recent_errors"])
    request_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")]] = Field(default_factory=list, max_length=FEEDBACK_LIMITS["request_ids"])
    mark: FeedbackMark | None = None

    @field_validator("route", "recent_routes")
    @classmethod
    def local_route(cls, value):
        for route in [value] if isinstance(value, str) else value:
            if route.removeprefix("#").startswith("//"):
                raise ValueError("Feedback must refer to a local app route")
        return value

    @field_validator("view_state")
    @classmethod
    def bounded_navigation_state(cls, value):
        import json
        # Only navigation metadata belongs here; form values, rows and record snapshots never do.
        allowed = {"active_view", "view_id", "filters", "grouping", "group_by", "sort", "dates",
                   "open_panel", "selection_count", "layout", "range", "search"}
        if set(value) - allowed:
            raise ValueError("View state may contain only navigation metadata")
        try:
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, RecursionError):
            raise ValueError("View state must be JSON navigation metadata") from None
        if len(raw) > FEEDBACK_LIMITS["view_state_bytes"]:
            raise ValueError("View state exceeds the supported size")
        for key, item in value.items():
            if item is None:
                continue
            if key == "filters":
                if isinstance(item, dict):
                    TableViewDefinition(filters=item)  # The shared collection filter contract.
                elif isinstance(item, list):
                    # Flat clauses are also accepted by the initially published feedback contract.
                    for clause in item:
                        if not isinstance(clause, dict) or set(clause) - {"field", "operator", "op", "value", "values"}:
                            raise ValueError("Feedback filters must contain only navigation choices")
                        for choice in clause.values():
                            choices = choice if isinstance(choice, list) else [choice]
                            if any(type(v) not in {str, int, float, bool, type(None)} for v in choices):
                                raise ValueError("Feedback filters must not contain record objects")
                else:
                    raise ValueError("Feedback filters must contain navigation choices")
            elif key == "sort":
                TableViewDefinition(sort=item)
            elif key == "dates":
                if not isinstance(item, dict) or set(item) - {"start", "end", "date", "anchor", "from", "to", "starts_on", "ends_on"} \
                        or any(not isinstance(v, str) for v in item.values()):
                    raise ValueError("Feedback dates must contain only date navigation values")
            elif key == "selection_count":
                if type(item) is not int or item < 0:
                    raise ValueError("Feedback selection count must be a nonnegative integer")
            elif not isinstance(item, str):
                raise ValueError("Feedback view and panel choices must be text, not record objects")
        return value


class FeedbackCreate(FeedbackContext):
    kind: FeedbackKind
    message: str = Field(min_length=1, max_length=FEEDBACK_LIMITS["message"])
    expected: str | None = Field(default=None, max_length=FEEDBACK_LIMITS["expected"])
    screenshot: FeedbackScreenshot | None = None


class FeedbackCreated(Schema):
    id: ID
    matched_report_id: ID | None = None


class FeedbackRow(FeedbackContext):
    id: ID
    created_at: datetime
    reporter_person_id: ID | None
    reporter_identity_id: ID
    reporter_name: str
    kind: FeedbackKind
    message: str
    expected: str | None
    fingerprint: str
    status: FeedbackStatus
    duplicate_of: ID | None
    resolved_in: str | None
    triage_note: str | None
    occurrences: int
    duplicates_count: int = Field(ge=0)
    updated_at: datetime
    revision: int
    has_screenshot: bool
    github_issue_url: str | None = None


class FeedbackList(Schema):
    rows: list[FeedbackRow]
    total: int
    page: int
    page_size: int


class FeedbackUpdate(Schema):
    id: ID
    revision: int = Field(ge=1)
    status: FeedbackStatus | None = None
    duplicate_of: ID | None = None
    resolved_in: str | None = Field(default=None, max_length=80)
    triage_note: str | None = Field(default=None, max_length=2000)


class FeedbackUndo(Schema):
    id: ID
    revision: int = Field(ge=1)


class FeedbackExport(Schema):
    id: ID
    include_message: bool = Field(default=False, strict=True)
    create_issue: bool = Field(default=False, strict=True)


class FeedbackExportResult(Schema):
    title: str
    body: str
    github_issue_url: str | None = None
    configured: bool = False


def valid_feedback_github_repo(value):
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)) and all(
        part not in {".", ".."} for part in value.split("/"))


class GithubIntegrationStatus(Schema):
    connected: bool
    source: Literal["app", "env"] | None
    repo: str | None
    token_hint: str | None
    connected_by_name: str | None
    connected_at: datetime | None
    last_tested_at: datetime | None
    last_test_ok: bool | None
    last_used_at: datetime | None
    token_expires_at: datetime | None
    key_configured: bool
    revision: int | None


class GithubConnect(Schema):
    repo: str = Field(min_length=3, max_length=300)
    token: str = Field(min_length=8, max_length=16384, pattern=r"^[!-~]+$", repr=False)

    @field_validator("repo")
    @classmethod
    def repository(cls, value):
        if not valid_feedback_github_repo(value):
            raise ValueError("Use a GitHub owner/repository")
        return value


class GithubDisconnect(Schema):
    revision: int = Field(ge=1, strict=True)


class GithubTest(Schema):
    pass


class PersonRecordWorkspace(Schema):
    id: ID
    name: str
    status: str
    starts_on: date
    ends_on: date


class PersonFairnessSource(Schema):
    goal_id: str
    line_id: str | None
    line: int | None
    slot: str | None
    severity: Literal['mandatory', 'advisory']
    strength: Strength
    weight: float = 1
    count_bucket: list[tuple[str, str]] = Field(default_factory=list)


class PersonFairnessBucket(Schema):
    bucket: str
    partner_id: ID | None
    actual: float
    expected: float
    cost: float


class PersonFairnessValue(Schema):
    person_id: ID
    prior: float
    planned: float
    actual: float
    expected: float
    low: float
    high: float | None
    cost: float
    status: Literal['below', 'within', 'above']
    rank: int | None = None
    peers: int | None = None
    peer_count: int | None = None
    available_days: float | None = None
    supervisable_sessions: float | None = None
    overtime_offered_hours: float | None = None
    rota_days: int | None = None
    strengths: list[Strength] = Field(default_factory=list)
    sources: list[PersonFairnessSource] = Field(default_factory=list)
    buckets: list[PersonFairnessBucket] = Field(default_factory=list)


class PersonFairnessPair(Schema):
    person_id: ID
    partner_id: ID
    partner_name: str
    prior: float
    planned: float
    actual: float
    expected: float
    low: float
    high: float | None
    cost: float
    status: Literal['below', 'within', 'above']
    rank: int | None = None
    peers: int | None = None
    peer_count: int | None = None


class PersonFairnessTrend(Schema):
    publication_id: ID
    workspace_id: ID
    starts_on: date
    ends_on: date
    effective_until: date | None
    actual: float
    expected: float | None
    partner_id: ID | None


class PersonFairnessGoal(Schema):
    policy_id: str
    goal_id: str
    audience_label: str = ''
    count_bucket: list[tuple[str, str]] = Field(default_factory=list)
    line_id: str | None
    line: int | None
    slot: str | None
    name: str
    metric: str
    aim: str
    share_by: BalanceShareBy | None = None
    comparison: Literal['within_group', 'pooled'] = 'within_group'
    period_start: date | None = None
    period_end: date | None = None
    applies_to_nobody: bool = False
    unit: str
    period: str
    strength: str
    distribution: Literal['people', 'share', 'period_spread', 'pair_limit'] = 'people'
    distribution_period: Literal['week', 'rota'] = 'week'
    weight: float = 1
    balance_between_people: bool = True
    source_goal_ids: list[str] = Field(default_factory=list)
    quota_target: bool = False
    history: bool
    score: float | None
    starts_on: date
    ends_on: date
    history_starts_on: date | None
    history_ends_on: date | None
    rank_basis: Literal['cost'] | None
    people: list[PersonFairnessValue]
    pairs: list[PersonFairnessPair]
    trend: list[PersonFairnessTrend]
    trend_complete: bool


class PersonFairness(Schema):
    person_id: ID
    peers_visible: bool
    workspace: PersonRecordWorkspace | None
    goals: list[PersonFairnessGoal]


class PersonScoreContribution(Schema):
    component: str
    points: float
    text: str
    person_id: ID | None
    shift_id: ID | None
    rule_id: str | None
    score_delta: float
    component_weight: float | None
    line: int | None = None
    slot: str | None = None
    rule_severity: str | None = None
    rule_strength: Strength | None = None
    rule_weight: float | None = None
    line_id: ID | None = None
    rule_source_id: ID | None = None


class PersonOpenPlace(Schema):
    supervision: 'StaffingSupervisionPlan | None' = None
    shift_id: ID
    start: datetime
    end: datetime
    session_label: str
    service_names: list[str]
    location_name: str
    status: Literal['ok', 'warn']
    team_lead: bool
    score_delta: float


class PersonScoreRota(Schema):
    workspace_id: ID
    publication_id: ID | None
    name: str
    starts_on: date
    ends_on: date
    basis: Literal['issued_publication', 'stored_draft_check']
    fresh: bool
    score: float | None
    person_score: dict | None
    contributions: list[PersonScoreContribution]
    findings: list[dict]
    rule_weights: list[dict]
    contributions_complete: bool
    open_places: list[PersonOpenPlace]
    open_places_total: int | None
    opportunity_score: float | None
    opportunity_basis: Literal['current_recorded_inputs']
    open_limit: int
    open_offset: int
    open_has_more: bool | None


class PersonScore(Schema):
    person_id: ID
    draft: PersonScoreRota | None
    published: PersonScoreRota | None


class PersonWorkloadTotals(Schema):
    duties: int
    hours: float
    reserved_hours: float
    session_units: float
    overtime_duties: int
    overtime_hours: float
    overtime_offered_hours: float
    lead_turns: int
    nights: None


class PersonWorkloadWeekend(Schema):
    weekend_start: int
    weekend_days: int
    value: int


class PersonWorkloadLimit(Schema):
    policy_id: str
    policy_revision: int
    policy_name: str
    kind: Literal['max_hours', 'max_duties']
    severity: Literal['mandatory', 'advisory']
    strength: str
    start: datetime
    end: datetime
    actual: float
    maximum: float
    group_by: Literal['all', 'weekday', 'supervisor']
    group: str | int | None
    line_id: str | None


class PersonWorkloadPeriod(Schema):
    key: Literal['this_week', 'this_month', 'this_rota', 'rolling_12_weeks']
    starts_on: date
    ends_on: date
    totals: PersonWorkloadTotals
    weekends: list[PersonWorkloadWeekend]
    quotas: list[dict]
    limits: list[PersonWorkloadLimit]
    complete: bool


class PersonWorkload(Schema):
    person_id: ID
    on: date
    timezone: str
    basis: str
    rule_basis: str
    workspace: PersonRecordWorkspace | None
    periods: list[PersonWorkloadPeriod]
    unsupported_metrics: list[str]


class RotationRecord(Schema):
    id: ID
    revision: int
    withdrawn: bool = False
    created_at: str
    department_id: ID
    rotation_type_id: ID | None
    person_id: ID
    cohort_id: ID
    service_id: ID | None
    mentor_id: ID | None
    department_available: bool
    starts_on: date
    ends_on: date


class PersonRotations(Schema):
    rotations: list[RotationRecord]


class PersonAvailability(Schema):
    person_id: ID
    start: date
    end: date
    leave_reason_display: Literal['none', 'type', 'details']
    availability: list[dict]
    requests: list[dict]
    patterns: list[dict]
    balances_supported: bool
    availability_partial: bool
    requests_partial: bool
    patterns_partial: bool


class PersonAccount(Schema):
    identity_id: ID
    display_name: str
    username: str | None
    email: str | None
    disabled: bool
    roles: list[RoleName]
    last_sign_in_at: datetime | None
    accepted_at: datetime | None
    password_changed_at: datetime | None


class PersonAccounts(Schema):
    person_id: ID
    accounts: list[PersonAccount]


class PersonSession(Schema):
    id: ID
    identity_id: ID
    revision: int
    created_at: datetime
    last_active_at: datetime | None
    expires_at: datetime | None
    revoked: bool
    active: bool
    current: bool
    device_label: str | None
    network_label: str | None
    can_revoke: bool


class PersonSessions(Schema):
    person_id: ID
    sessions: list[PersonSession]
    total: int
    limit: int
    offset: int


class PersonAuthDetail(Schema):
    person_id: ID
    identity_id: ID
    session_id: ID
    device_label: str | None
    network_label: str | None
    method: Literal["password", "oidc"] | None = None


class PersonAuthEvent(Schema):
    id: ID
    revision: int
    created_at: datetime
    department_id: ID
    actor_id: ID
    action: Literal['auth.sign_in', 'auth.sign_out', 'auth.session_revoke']
    target_id: ID
    reason: str
    detail: PersonAuthDetail
    correlation_id: str


class PersonAuthActivity(Schema):
    person_id: ID
    events: list[PersonAuthEvent]
    offset: int
    limit: int
    total: int
    has_more: bool


class EffectiveInput(Schema):
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})
    active: bool = Field(default=True, json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on is not None and self.ends_on <= self.starts_on:
            raise ValueError("End date is exclusive and must follow start date")
        return self


class RevisionRef(Schema):
    id: ID
    revision: int = Field(ge=1)


class NamedInput(EffectiveInput):
    name: Name = Field(json_schema_extra={"bulk": True})
    description: str = Field(default="", max_length=2000, json_schema_extra={"bulk": True})


# Named colours shared with duty types; '' means an automatic colour chosen from the record id.
Colour = Literal['', 'teal', 'blue', 'violet', 'amber', 'slate', 'coral', 'indigo', 'pink']


class RotationTypeInput(NamedInput):
    short_name: str = Field(default="", max_length=40, json_schema_extra={"bulk": True})
    leave_policy: Literal['not_allowed', 'needs_approval', 'allowed'] = Field(
        default='not_allowed', json_schema_extra={"bulk": True})


class LeaveTypeInput(Schema):
    short_name: str = Field(default="", max_length=40, json_schema_extra={"bulk": True})
    name: Name = Field(json_schema_extra={"bulk": True})
    short_code: str = Field(min_length=1, max_length=6, pattern=r'^[A-Z0-9]{1,6}$', json_schema_extra={"bulk": True})
    colour: Colour = Field(default='', json_schema_extra={"bulk": True})
    active: bool = Field(default=True, json_schema_extra={"bulk": True})
    sort_order: int = Field(default=1000, ge=0, le=100000, strict=True, json_schema_extra={"bulk": True})
    counts_as_unavailable: bool = Field(default=True, json_schema_extra={"bulk": True})


class ServiceInput(NamedInput):
    mode: Literal['unspecified', 'physical', 'virtual'] = Field(default='unspecified', json_schema_extra={"bulk": True})
    calendar_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    colour: Colour = Field(default='', json_schema_extra={"bulk": True})


class CategoryInput(NamedInput):
    sort_order: int = Field(default=1000, ge=0, le=100000, strict=True, json_schema_extra={"bulk": True})
    colour: Colour = Field(default='', json_schema_extra={"bulk": True})
    short_code: str = Field(default='', max_length=6, pattern=r'^[A-Z0-9]{0,6}$', json_schema_extra={"bulk": True})


# What a tag can label (a catalogue name each).
TagKind = Literal['people', 'services', 'locations', 'assignment_types', 'programmes', 'policies', 'events', 'department_content']
TAG_KINDS: tuple[str, ...] = get_args(TagKind)


class TagInput(Schema):
    """A department tag (Configuration › Tags). Its members are kept as references (command tag.members)."""
    short_name: str = Field(default="", max_length=40, json_schema_extra={"bulk": True})
    name: Tag = Field(json_schema_extra={"bulk": True})
    colour: Colour = Field(default='', json_schema_extra={"bulk": True})
    description: str = Field(default='', max_length=2000, json_schema_extra={"bulk": True})
    labels: list[TagKind] = Field(min_length=1, max_length=len(TAG_KINDS), json_schema_extra={"bulk": True})
    active: bool = Field(default=True, json_schema_extra={"bulk": True})

    @field_validator('labels')
    @classmethod
    def unique_labels(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Choose each kind once')
        return value


class TagMemberRef(Schema):
    kind: TagKind
    id: ID


class TagMemberChange(Schema):
    """Records to add to and remove from one tag (POST /api/tags/{id}/preview previews it unsaved)."""
    add: list[TagMemberRef] = Field(default_factory=list, max_length=500)
    remove: list[TagMemberRef] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def something(self):
        if not self.add and not self.remove:
            raise ValueError('Choose records to add or remove')
        return self


class TagMembersInput(TagMemberChange):
    """Command tag.members (target: the tag, at the revision seen). Undo is the same command with add and remove swapped."""
    reason: Reason


class TagMemberPreviewInput(TagMemberChange):
    """Preview the same member delta as Save, including newly selected label kinds."""
    value: TagInput | None = None


# The one condition tree (announcement audiences and rule scopes). Person fields: roles (audiences only), person_ids,
# category_ids (rank), capability_ids (skill), programme_ids, rotation_type_ids, cohort_ids, tags, eligible_service_ids
# (clinics the person may work at that moment: their dated eligibility). Session fields (rules only): service_ids,
# location_ids (a place and everything inside it), assignment_type_ids (duty type). `plural` counts several values in a
# one-line scope summary ("9 ranks · 80 people").
# In an audience, service_ids means clinics the person may work.
EXCLUSIVE_LAST_DAY_FIELDS = frozenset({'ends_on', 'end'})
CONDITION_FIELD_DEFINITIONS = (
    {'key': 'roles', 'label': 'Role', 'plural': 'roles', 'kind': 'roles', 'session': False},
    {'key': 'category_ids', 'label': 'Rank', 'plural': 'ranks', 'kind': 'categories', 'session': False},
    {'key': 'capability_ids', 'label': 'Skill', 'plural': 'skills', 'kind': 'capabilities', 'session': False},
    {'key': 'programme_ids', 'label': 'Programme', 'plural': 'programmes', 'kind': 'programmes', 'session': False},
    {'key': 'rotation_type_ids', 'label': 'Rotation type', 'plural': 'rotation types', 'kind': 'rotation_types', 'session': False},
    {'key': 'cohort_ids', 'label': 'Cohort', 'plural': 'cohorts', 'kind': 'cohorts', 'session': False},
    {'key': 'tags', 'label': 'Tag', 'plural': 'tags', 'kind': '', 'session': False},
    {'key': 'person_ids', 'label': 'Person', 'plural': 'people', 'kind': 'people', 'session': False},
    {'key': 'eligible_service_ids', 'label': 'Can work at', 'plural': 'clinics they can work at', 'kind': 'services', 'session': False},
    {'key': 'service_ids', 'label': 'Clinic', 'plural': 'clinics', 'kind': 'services', 'session': True},
    {'key': 'location_ids', 'label': 'Location', 'plural': 'locations', 'kind': 'locations', 'session': True},
    {'key': 'assignment_type_ids', 'label': 'Duty type', 'plural': 'duty types', 'kind': 'assignment_types', 'session': True},
    {'key': 'date', 'label': 'Duty date', 'kind': '', 'value_kind': 'date', 'session': True},
    *({'key': key, 'label': label, 'kind': '', 'value_kind': 'date', 'session': False}
      for key, label in (('starts_on', 'From'), ('ends_on', 'Last day'), ('start', 'Start'), ('end', 'End'),
                         ('first_day', 'First day'), ('last_day', 'Last day'))),
    *({'key': key, 'label': label, 'kind': '', 'value_kind': 'text', 'session': False}
      for key, label in (('name', 'Name'), ('display_name', 'Person name'), ('note', 'Note'), ('short_code', 'Short code'))),
    *({'key': key, 'label': label, 'kind': '', 'value_kind': 'number', 'session': False}
      for key, label in (('number', 'Number'), ('target', 'Target'), ('capacity', 'Capacity'),
                         ('points', 'Points'), ('min', 'Minimum'), ('max', 'Maximum'))),
    {'key': 'rota_ids', 'label': 'Rota', 'plural': 'rotas', 'kind': '', 'session': False},
    {'key': 'workspace_id', 'label': 'Draft rota', 'plural': 'draft rotas', 'kind': '', 'session': False},
    {'key': 'department_ids', 'label': 'Department', 'plural': 'departments', 'kind': '', 'session': False},
    {'key': 'usual_assignee_ids', 'label': 'Usual assignee', 'plural': 'usual assignees', 'kind': 'people', 'session': True},
    {'key': 'date_set_ids', 'label': 'On a date set', 'plural': 'date sets', 'kind': 'date_sets', 'session': True},
)
ConditionFieldKind = Literal['date', 'text', 'number', 'people', 'single_choice', 'multiple_choice', 'tags']
CONDITION_OPERATORS = {
    'date': ('is', 'before', 'after', 'on_or_before', 'on_or_after', 'between', 'within', 'empty', 'not_empty'),
    'text': ('is', 'is_not', 'contains', 'not_contains', 'starts_with', 'ends_with', 'empty', 'not_empty'),
    'number': ('is', 'is_not', 'gt', 'gte', 'lt', 'lte', 'between', 'empty', 'not_empty'),
    'people': ('any', 'all', 'none', 'is', 'is_not', 'empty', 'not_empty'),
    'single_choice': ('any', 'none', 'is', 'is_not', 'empty', 'not_empty'),
    'multiple_choice': ('any', 'all', 'none', 'is', 'is_not', 'empty', 'not_empty'),
    'tags': ('any', 'all', 'none', 'is', 'is_not', 'empty', 'not_empty'),
}
CONDITION_FIELD_DEFINITIONS = tuple({**field, 'value_kind': kind, 'operators': CONDITION_OPERATORS[kind],
                                   'exclusive_last_day': kind == 'date' and field['key'] in EXCLUSIVE_LAST_DAY_FIELDS}
    for field in CONDITION_FIELD_DEFINITIONS
    for kind in (field.get('value_kind', 'tags' if field['key'] == 'tags' else
                          'people' if field['kind'] == 'people' else
                          'single_choice' if field['key'] in {'service_ids', 'location_ids', 'assignment_type_ids',
                                                               'workspace_id', 'department_ids'} else 'multiple_choice'),))
ConditionField = Literal[tuple(field['key'] for field in CONDITION_FIELD_DEFINITIONS)]
CONDITION_FIELDS: tuple[str, ...] = get_args(ConditionField)
CollectionConditionField = Annotated[str, StringConstraints(pattern=r'^[a-zA-Z0-9_.-]{1,100}$')]
ViewTableKey = Annotated[str, StringConstraints(pattern=r'^[a-z0-9_-]{1,60}$')]
SESSION_SCOPE_FIELDS = frozenset(field['key'] for field in CONDITION_FIELD_DEFINITIONS if field['session'])
AUDIENCE_FIELDS = frozenset({'roles', 'category_ids', 'service_ids', 'programme_ids', 'capability_ids', 'tags', 'person_ids', 'date'})
SCOPE_FIELDS = frozenset({'category_ids', 'capability_ids', 'programme_ids', 'rotation_type_ids', 'cohort_ids', 'tags',
                         'person_ids', 'eligible_service_ids'}) | SESSION_SCOPE_FIELDS
LEAD_FIELDS = SCOPE_FIELDS - SESSION_SCOPE_FIELDS  # who may lead: person fields
MULTI_PERSON_FIELDS = LEAD_FIELDS - {'person_ids'} | {'roles'}
ConditionContext = Literal['filter', 'selection', 'bulk', 'rule', 'audience']
RelativeUnit = Literal['days', 'weeks', 'months']
_VIEW_CONTEXTS = ('filter', 'selection', 'bulk')


def _relative_token(token, label, field_kinds, *, fields=(), requirements=(), roles=(), duty=False,
                    needs_n=False, needs_unit=False, description=''):
    return {'token': token, 'label': label, 'description': description, 'field_kinds': field_kinds, 'fields': fields,
            'contexts': (*_VIEW_CONTEXTS, 'rule', 'audience') if duty else _VIEW_CONTEXTS,
            'requirements': requirements, 'roles': roles, 'needs_n': needs_n, 'needs_unit': needs_unit,
            'units': get_args(RelativeUnit) if needs_unit else ()}


RELATIVE_TOKEN_CATALOGUE = (
    *(_relative_token(token, label, ('date',)) for token, label in
      (('today', 'Today'), ('yesterday', 'Yesterday'), ('tomorrow', 'Tomorrow'))),
    *(_relative_token(f'{which}_{unit}', f'{which.capitalize()} {unit}', ('date',))
      for unit in ('week', 'month', 'quarter', 'year') for which in ('this', 'last', 'next')),
    _relative_token('last_n', 'Last N days / weeks / months', ('date',), needs_n=True, needs_unit=True),
    _relative_token('next_n', 'Next N days / weeks / months', ('date',), needs_n=True, needs_unit=True),
    *(_relative_token(token, label, ('date',), duty=True, requirements=('duty_day',), needs_n=needs_n)
      for token, label, needs_n in (('duty_day', "The duty's day", False),
        ('days_before_duty', 'Within N days before the duty', True),
        ('days_after_duty', 'Within N days after the duty', True),
        ('duty_week', "The duty's week", False), ('duty_month', "The duty's month", False))),
    _relative_token('me', 'Me', ('people',), requirements=('person',)),
    _relative_token('people_i_approve', 'People I approve', ('people',), requirements=('approvals',), roles=('approver',)),
    _relative_token('my_team', 'My team', ('people',), requirements=('review_routes',)),
    _relative_token('people_i_supervise', 'People I supervise', ('people',), requirements=('supervisees',)),
    _relative_token('my_programme', 'My programme', ('multiple_choice',), fields=('programme_ids',),
                    requirements=('programme',)),
    _relative_token('my_cohort', 'My cohort', ('multiple_choice',), fields=('cohort_ids',), requirements=('cohort',)),
    _relative_token('my_rank', 'My rank', ('multiple_choice',), fields=('category_ids',), requirements=('rank',)),
    _relative_token('my_clinics', 'Clinics I can work at', ('single_choice', 'multiple_choice'),
                    fields=('service_ids', 'eligible_service_ids', 'location_ids'), requirements=('clinic_permission',),
                    description="Clinics where you hold a clinic permission on the As of day (or duty day when supplied), "
                                "including their descendant locations. Viewer-relative; unavailable in rules and audiences."),
    _relative_token('my_department', 'My department', ('single_choice', 'multiple_choice'),
                    fields=('service_ids', 'eligible_service_ids', 'location_ids', 'department_ids'), requirements=('department',)),
    *(_relative_token(token, label, ('single_choice', 'multiple_choice'), fields=('rota_ids', 'workspace_id'),
                      requirements=('viewed_rota',) if token == 'viewed_rota' else ('rota',))
      for token, label in (('current_rota', 'Current rota'), ('next_rota', 'Next rota'), ('viewed_rota', 'The rota being viewed'))),
    _relative_token('its_target', 'Its target', ('number',), requirements=('target',)),
    _relative_token('department_average', 'The department average', ('number',), requirements=('department_average',)),
)
RelativeToken = Literal[tuple(row['token'] for row in RELATIVE_TOKEN_CATALOGUE)]
DUTY_RELATIVE_TOKENS = frozenset(row['token'] for row in RELATIVE_TOKEN_CATALOGUE if 'rule' in row['contexts'])
ConditionScalar = (Annotated[str, Field(strict=True)] | Annotated[int, Field(strict=True)] |
                   Annotated[float, Field(strict=True)] | Annotated[bool, Field(strict=True)])


class AbsoluteOperand(Schema):
    kind: Literal['absolute']
    value: ConditionScalar | list[ConditionScalar] | None


class RelativeOperand(Schema):
    kind: Literal['relative']
    token: RelativeToken
    n: int | None = Field(default=None, ge=1, le=10000, strict=True, exclude_if=lambda value: value is None)
    unit: RelativeUnit | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def parameters(self):
        token = next(row for row in RELATIVE_TOKEN_CATALOGUE if row['token'] == self.token)
        if token['needs_n'] != (self.n is not None) or token['needs_unit'] != (self.unit is not None):
            raise ValueError(f"{token['label']}: supply N and unit only when required by this token")
        return self


ConditionOperand = ConditionScalar | Annotated[AbsoluteOperand | RelativeOperand, Field(discriminator='kind')]


class ConditionResolveContext(Schema):
    collection: 'BulkCollection | None' = None
    table_key: ViewTableKey | None = None
    target_id: ID | None = None
    as_of: date | None = None
    workspace_id: ID | None = None
    duty_day: date | None = None
    context: ConditionContext = 'filter'


class ConditionResolveInput(ConditionResolveContext):
    operand: ConditionOperand
    field: CollectionConditionField | None = None

    @model_validator(mode='after')
    def known_field(self):
        if self.field and condition_field_definition(self.field, self.table_key) is None:
            raise ValueError(f'Unknown condition field for this list: {self.field}')
        return self


class ConditionResolveItem(Schema):
    field: CollectionConditionField | None = None
    operand: ConditionOperand


class ConditionResolveBatchInput(ConditionResolveContext):
    operands: list[ConditionResolveItem] = Field(min_length=1, max_length=1000)

    @model_validator(mode='after')
    def known_fields(self):
        if any(item.field and condition_field_definition(item.field, self.table_key) is None for item in self.operands):
            raise ValueError('Unknown condition field for this list')
        return self


class ConditionResolvedOperand(Schema):
    label: str
    display: str
    from_: date | None = Field(default=None, alias='from')
    to: date | None = None
    ids: list[ConditionScalar] | None = None
    value: ConditionScalar | list[ConditionScalar] | None = None


class ConditionResolveBatchResult(Schema):
    results: list[ConditionResolvedOperand]


class ConditionDateParseInput(Schema):
    text: str = Field(min_length=1, max_length=120)
    as_of: date | None = None
    workspace_id: ID | None = None
    duty_day: date | None = None
    context: ConditionContext = 'filter'


class ConditionResolution(Schema):
    label: str
    from_: date | None = Field(alias='from')
    to: date | None
    display: str
    value: ConditionScalar | list[ConditionScalar] | None


class ConditionDateMatch(ConditionResolution):
    operand: ConditionOperand
    rank: int = Field(ge=0)


class ConditionDateParseResult(Schema):
    matches: list[ConditionDateMatch]


class Condition(Schema):
    """One line of a condition: "rank is any of Consultant, Resident" or "tag is none of locum"."""
    field: CollectionConditionField
    op: Literal['any', 'all', 'none', 'is', 'is_not', 'before', 'after', 'on_or_before', 'on_or_after', 'between',
                'within', 'empty', 'not_empty', 'contains', 'not_contains', 'starts_with', 'ends_with',
                'gt', 'gte', 'lt', 'lte'] = 'any'
    values: list[str] = Field(default_factory=list, max_length=500)
    value: ConditionOperand | tuple[ConditionOperand, ConditionOperand] | None = Field(default=None,
        exclude_if=lambda value: value is None)
    entry_names: list[Name] = Field(default_factory=list, max_length=500, exclude_if=lambda value: not value)
    count: Literal['together', 'each'] = Field(default='together', exclude_if=lambda value: value == 'together')
    _collection_values: ClassVar[bool] = False

    @model_validator(mode='after')
    def meaningful(self, info: ValidationInfo):
        table_key = (info.context or {}).get('table_key')
        metadata = condition_line_definition(self.field, table_key, self.op, self.value)
        if metadata is None:
            raise ValueError(f'Unknown condition field: {self.field}')
        legacy = self.op in {'any', 'all', 'none'} and self.value is None
        legacy_collection = self._collection_values and legacy
        if not legacy_collection and metadata['value_kind'] in {'date', 'text', 'number'} and self.op not in metadata['operators']:
            raise ValueError(f"{metadata['label']} does not support {self.op}; choose a typed operand")
        if self.op in {'empty', 'not_empty'}:
            if self.values or self.value is not None:
                raise ValueError('Is empty / Is not empty takes no operand')
        elif legacy:
            if not self.values:
                raise ValueError('Choose at least one condition value')
        else:
            if self.op not in metadata['operators']:
                raise ValueError(f"{metadata['label']} does not support {self.op}")
            if self.values or self.value is None:
                raise ValueError('Supply one operand in value, or two operands for between')
            if (self.op == 'between') != isinstance(self.value, tuple):
                raise ValueError('Between needs exactly two operands; other operators need one operand')
            operands = self.value if isinstance(self.value, tuple) else (self.value,)
            for operand in operands:
                if isinstance(operand, RelativeOperand):
                    token = next(row for row in RELATIVE_TOKEN_CATALOGUE if row['token'] == operand.token)
                    dimension = metadata.get('operand_field', self.field)
                    if metadata['value_kind'] not in token['field_kinds'] or token['fields'] and dimension not in token['fields']:
                        raise ValueError(f"{token['label']} does not apply to {metadata['label']}")
                else:
                    absolute = operand.value if isinstance(operand, AbsoluteOperand) else operand
                    if metadata['value_kind'] == 'date':
                        if isinstance(absolute, list):
                            if len(absolute) != 2 or not all(isinstance(value, str) for value in absolute):
                                raise ValueError('An absolute date range needs exactly two ISO dates')
                            for value in absolute:
                                date.fromisoformat(value)
                        elif isinstance(absolute, str):
                            date.fromisoformat(absolute)
                        else:
                            raise ValueError('A date operand must be an ISO date or a relative date')
                    if metadata['value_kind'] == 'number' and (isinstance(absolute, bool) or not isinstance(absolute, (int, float))):
                        raise ValueError('A number operand must be a number or a relative number')
                    if metadata['value_kind'] == 'text' and not isinstance(absolute, str):
                        raise ValueError('A text operand must be text')
                    if metadata['value_kind'] in {'people', 'single_choice', 'multiple_choice', 'tags'}:
                        choices = absolute if isinstance(absolute, list) else [absolute]
                        if not choices or not all(isinstance(value, str) for value in choices):
                            raise ValueError('Choice operands must contain text values')
            if self.op == 'within' and not isinstance(self.value, RelativeOperand):
                raise ValueError('Within needs a relative date range')
        if self.count == 'each' and not legacy:
            raise ValueError('Count each applies only to listed absolute choices')
        if self.entry_names and self.field != 'date_set_ids':
            raise ValueError('Entry names apply only to on a date set conditions')
        if self.field == 'date_set_ids':
            from packages.domain.conditions import absolute_values
            values = absolute_values(self) or []
            TypeAdapter(list[ID]).validate_python(values)
            if len(set(values)) != len(values):
                raise ValueError('Choose distinct date set IDs')
        if self.op == 'all' and (self.field in CONDITION_FIELDS and self.field not in MULTI_PERSON_FIELDS | {'date_set_ids', 'usual_assignee_ids'}
                                and not (legacy_collection and metadata['value_kind'] in {'date', 'text', 'number'})
                                or self.field not in CONDITION_FIELDS and not legacy_collection and metadata['value_kind'] not in {'multiple_choice', 'tags'}):
            why = ('a person has one identity' if self.field == 'person_ids' else
                   'a session is at exactly one place/duty type')
            raise ValueError(f'Has all of cannot match {self.field}: {why}; choose is any of instead')
        if self.op == 'none' and 'count' in self.model_fields_set:
            raise ValueError('Is not conditions cannot set Count; Count applies only to is any of or has all of')
        return self


class Conditions(Schema):
    """Conditions joined by and (all) or or (any); a group may hold one level of nested groups."""
    match: Literal['all', 'any'] = 'all'
    conditions: list['Condition | Conditions'] = Field(min_length=1, max_length=20)
    count: Literal['together', 'each'] = Field(default='together', exclude_if=lambda value: value == 'together')

    @model_validator(mode="after")
    def shallow(self):
        for c in self.conditions:
            if isinstance(c, Conditions) and any(isinstance(x, Conditions) for x in c.conditions):
                raise ValueError("Groups can be nested one level deep")
        if self.count == 'each' and any(c.value is not None or c.op in {'empty', 'not_empty'}
            for child in self.conditions for c in (child.conditions if isinstance(child, Conditions) else [child])):
            raise ValueError('Count each applies only to listed absolute choices')
        return self

    def fields(self) -> set[str]:
        return {f for c in self.conditions for f in (c.fields() if isinstance(c, Conditions) else {c.field})}


class CollectionCondition(Condition):
    """A collection facet, using its API field key and canonical values (not translated display labels)."""
    _collection_values: ClassVar[bool] = True


class CollectionConditions(Conditions):
    """The shared And/Or condition tree for collection facets; endpoints allow-list their readable fields."""
    conditions: list['CollectionCondition | CollectionConditions'] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def shallow(self):
        pending, lines, nodes = [(self, 0)], [], 1
        while pending:
            group, depth = pending.pop()
            if depth > 5:
                raise ValueError('Collection groups can be nested at most five levels deep')
            for child in group.conditions:
                if isinstance(child, CollectionConditions):
                    pending.append((child, depth + 1))
                else:
                    lines.append(child)
            nodes += len(group.conditions)
            if nodes > 200:
                raise ValueError('Collection conditions can contain at most 200 lines and groups')
        if self.count == 'each' and any(c.value is not None or c.op in {'empty', 'not_empty'} for c in lines):
            raise ValueError('Count each applies only to listed absolute choices')
        return self


SearchMatch = Literal['partial', 'exact']
SearchScope = Literal['all', 'self', 'related']
SEARCH_MODES = ({'value': 'partial', 'label': 'Contains'}, {'value': 'exact', 'label': 'Exact text'})
SEARCH_SCOPES = ({'value': 'all', 'label': 'Names and references'},
                 {'value': 'self', 'label': 'Own text'}, {'value': 'related', 'label': 'Related records'})
SEARCH_ENTITY_KINDS = tuple(dict.fromkeys(
    field['kind'] for field in CONDITION_FIELD_DEFINITIONS if field['kind'] not in {'', 'roles'})) + ('tags',)
SearchEntityKind = Literal[SEARCH_ENTITY_KINDS]


class SearchReference(Schema):
    kind: SearchEntityKind
    id: ID


class SearchOptions(Schema):
    match: SearchMatch = 'partial'
    scope: SearchScope = 'all'
    case_sensitive: bool = Field(default=False, strict=True)
    reference: SearchReference | None = None


class CollectionFilter(Schema):
    """GET operations, requests/all, audit, audit.csv and Schedule accept this JSON in the `filter` query parameter.

    Conditions use canonical API facet values, apply with existing query filters before count/paging, and never
    widen authorization. Bulk rows/resolve accept the same fields under selection.where. as_of is the department
    day for dated facts; immutable event states remain their recorded state.

    Operations: id/state/kind/error. Requests: id/type/status/stage/source/person_id/person (display name).
    Audit: id/action/kind (action namespace)/actor_id/person (actor display name)/target_id/reason.
    Schedule: session, source/rota, participant, clinic, location (including descendants), duty type and date fields.
    Canonical state/type/stage codes must be sent rather than translated labels; existing flat query filters also apply
    by And. Primary sort and then_sort reuse saved-view order and apply before paging.
    """
    search_options: SearchOptions | None = Field(default=None, exclude_if=lambda value: value is None)
    conditions: CollectionConditions | None = None
    as_of: date | None = None
    sort: tuple[CollectionConditionField, Literal['asc', 'desc']] | None = None
    then_sort: list[tuple[CollectionConditionField, Literal['asc', 'desc']]] = Field(default_factory=list, max_length=30,
        exclude_if=lambda value: not value)


def only_fields(tree: 'Conditions | None', allowed: frozenset, where: str):
    if tree is not None and tree.fields() - allowed:
        raise ValueError(f"{where} cannot filter on: {', '.join(sorted(tree.fields() - allowed))}")
    if tree is not None:
        for child in tree.conditions:
            if isinstance(child, Conditions):
                only_fields(child, allowed, where)
                continue
            operands = child.value if isinstance(child.value, tuple) else (child.value,)
            if any(isinstance(value, RelativeOperand) and value.token not in DUTY_RELATIVE_TOKENS for value in operands):
                raise ValueError(f'{where}: rules and audiences accept only duty-relative date tokens or absolute values; '
                                 'today-relative and viewer-relative tokens are not allowed')


WORKLOAD_COUNT_KINDS = frozenset({'max_hours', 'max_duties', 'block_quota', 'max_consecutive_days',
                                 'max_consecutive_weekends', 'run_length', 'days_off_run', 'weekend_shape',
                                 'shift_succession', 'balance_goal', 'min_rest', 'travel_buffer'})


def workload_counts(tree: 'Conditions | None'):
    """Workload is already counted per person: only session filters can split its buckets."""
    if tree is None:
        return
    if tree.count == 'each' and tree.fields() & (LEAD_FIELDS | {'roles'}):
        raise ValueError('Workload rules always count per person; Who cannot use Count each')
    for child in tree.conditions:
        if isinstance(child, Conditions):
            workload_counts(child)
        elif child.count == 'each' and child.field in (LEAD_FIELDS | {'roles'}):
            raise ValueError('Workload rules always count per person; Who cannot use Count each')


# Older names, kept so saved announcements and callers read unchanged.
AudienceCondition, AudienceGroup = Condition, Conditions


class Audience(Schema):
    """Who a message is for: everyone, or the people matching `where` (a condition tree). The older flat form
    (groups narrow each other; several values in one group widen it; roles and named people always count) still reads."""
    everyone: bool = False
    where: Conditions | None = None
    roles: list[RoleName] = Field(default_factory=list, max_length=len(ROLE_NAMES))
    person_ids: list[ID] = Field(default_factory=list, max_length=500)
    category_ids: list[ID] = Field(default_factory=list, max_length=100)
    service_ids: list[ID] = Field(default_factory=list, max_length=64)
    programme_ids: list[ID] = Field(default_factory=list, max_length=32)
    capability_ids: list[ID] = Field(default_factory=list, max_length=32)
    tags: list[Tag] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def someone(self):
        only_fields(self.where, AUDIENCE_FIELDS, "An audience")
        if not self.everyone and not self.where and not any((self.roles, self.person_ids, self.category_ids, self.service_ids,
                                          self.programme_ids, self.capability_ids, self.tags)):
            raise ValueError("Choose who it is for")
        return self


def safe_http_url(value: Any) -> Any:
    """Department links and event join links share the same trust boundary."""
    from urllib.parse import urlsplit
    if not isinstance(value, str):
        return value  # Pydantic's string validator reports invalid input types.
    if value:
        if any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in value):
            raise ValueError('URLs must not contain whitespace or control characters')
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Use an HTTP or HTTPS URL without embedded credentials')
        _ = parsed.port  # Access rejects malformed and out-of-range port numbers.
    return value


SafeHttpURL = Annotated[str, BeforeValidator(safe_http_url)]


M365_CLOUDS = {
    'global': ('https://login.microsoftonline.com', 'https://graph.microsoft.com/v1.0'),
    'us_gov': ('https://login.microsoftonline.us', 'https://graph.microsoft.us/v1.0'),
    'china': ('https://login.chinacloudapi.cn', 'https://microsoftgraph.chinacloudapi.cn/v1.0'),
}


class EmailSettings(Schema):
    smtp_enabled: bool = False
    smtp_host: str = Field(default='', max_length=253)
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_starttls: bool = True
    smtp_username: str = Field(default='', max_length=254)
    sender_name: str = Field(default='', max_length=160)
    sender_address: Email | Literal[''] = ''
    reply_to: Email | Literal[''] = ''
    m365_enabled: bool = False
    m365_tenant_id: Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9.-]*$')] = ''
    m365_client_id: Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9-]*$')] = ''
    m365_cloud: Literal['global', 'us_gov', 'china'] = 'global'
    default_sender: Literal['smtp', 'microsoft'] = 'smtp'
    announcement_default: bool = False
    recipient_header: Literal['Bcc', 'To'] = 'Bcc'
    event_invites: Literal['everyone', 'without_calendar_link'] = 'without_calendar_link'
    max_attempts: int = Field(default=5, ge=1)
    retry_base_seconds: int = Field(default=30, ge=1)
    retry_max_seconds: int = Field(default=3600, ge=1)
    timeout_seconds: int = Field(default=30, ge=1, le=60)
    send_interval_seconds: int = Field(default=0, ge=0)
    batch_size: int = Field(default=50, ge=1)
    oauth_flow_minutes: int = Field(default=10, ge=1)

    @property
    def m365_authority_url(self):
        return M365_CLOUDS[self.m365_cloud][0]

    @property
    def m365_graph_url(self):
        return M365_CLOUDS[self.m365_cloud][1]

    @model_validator(mode='after')
    def configured_channels(self):
        if any(ord(c) < 32 or ord(c) == 127 for c in self.sender_name + self.smtp_username + self.smtp_host):
            raise ValueError('Email settings cannot contain control characters')
        if self.smtp_enabled and not (self.sender_address and (self.smtp_username or self.smtp_host)):
            raise ValueError('SMTP needs a sender address and either a relay host or authenticated relay username')
        if self.smtp_host and any(c.isspace() or c in '/\\@:' for c in self.smtp_host):
            raise ValueError('SMTP host must be a hostname or IPv4 address')
        if self.m365_enabled and not (self.m365_tenant_id and self.m365_client_id):
            raise ValueError('Microsoft email needs a tenant and client id')
        if self.m365_tenant_id.lower() in {'common', 'organizations', 'consumers', '.', '..'}:
            raise ValueError('Use the department Microsoft tenant id or verified tenant domain')
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError('Maximum retry delay must cover the initial delay')
        return self


class EmailSettingsInput(Schema):
    value: EmailSettings
    reason: Reason


class DepartmentContentInput(NamedInput):
    kind: Literal['announcement', 'link'] = 'announcement'
    url: SafeHttpURL = Field(default='', max_length=2000)
    audience: Audience = Field(default_factory=lambda: Audience(everyone=True))
    importance: Literal['normal', 'important', 'must_acknowledge'] = 'normal'
    # Send it to the audience's inbox on saving (or on its start date, when that is later). Not stored.
    notify: bool = Field(default=False, json_schema_extra={"writeOnly": True})
    also_email: bool | None = None
    email_sender: Literal['smtp', 'microsoft'] | None = None
    email_sender_identity_id: ID | None = None

    @model_validator(mode='after')
    def safe_link(self):
        if self.kind == 'link' and self.importance == 'must_acknowledge':
            raise ValueError('Only announcements can require acknowledgement')
        if self.kind == 'link' and not self.url:
            raise ValueError('A departmental link needs a URL')
        return self


class EventTypeInput(NamedInput):
    colour: Colour = ''
    default_start_time: Clock = '09:00'
    default_length_minutes: int = Field(default=60, ge=1, le=10080)
    default_audience: Audience = Field(default_factory=lambda: Audience(everyone=True))
    attendance: EventAttendance = 'expected'
    blocks_duties: bool = False
    calendar: Literal['everyone', 'going_only', 'none'] = 'everyone'
    notify_inbox: bool = True
    also_email: bool = False
    rsvp: bool = False
    default_tag_ids: list[ID] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def optional_duty_protection(self):
        if self.blocks_duties and self.attendance == 'optional' and not self.rsvp:
            raise ValueError('An optional event can take people off duties only when they answer Going')
        return self

    @field_validator('default_tag_ids')
    @classmethod
    def unique_default_tags(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Choose each record only once')
        return value

    @field_validator('default_start_time')
    @classmethod
    def valid_start(cls, value):
        if value == '24:00':
            raise ValueError('Use 00:00 for a start time')
        return value


# Editable initial catalogue data, never names used to select behaviour.
EVENT_TYPE_DEFAULTS = (
    {'name': 'Morning meeting', 'attendance': 'expected', 'blocks_duties': False},
    {'name': 'M&M', 'attendance': 'expected'},
    {'name': 'Conference', 'attendance': 'expected', 'blocks_duties': True},
    {'name': 'Activity', 'attendance': 'optional', 'calendar': 'everyone', 'notify_inbox': False, 'rsvp': True},
)


class CalendarFeedSettingsInput(Schema):
    hidden_event_type_ids: list[ID] = Field(default_factory=list, max_length=500)


CONTENT_PAGE_SIZE = 200


class DepartmentContentSeen(Schema):
    ids: list[ID] = Field(min_length=1, max_length=CONTENT_PAGE_SIZE)


class AnnouncementCounts(Schema):
    audience: int
    delivered: int
    read: int
    acknowledged: int


class AnnouncementReceiptRow(Schema):
    id: str
    revision: int
    created_at: datetime
    content_id: str
    identity_id: str
    person_id: str | None
    person: dict[str, str] | None
    identity: dict[str, str]
    delivered_at: datetime | None
    read_at: datetime | None
    acknowledged_at: datetime | None
    in_audience: bool
    email_deliveries: list[dict[str, Any]] = Field(default_factory=list)


class AnnouncementReceiptList(Schema):
    receipts: list[AnnouncementReceiptRow]
    counts: AnnouncementCounts
    page: int
    page_size: int
    total: int


class NotificationRow(Schema):
    id: str
    revision: int
    created_at: datetime
    outbox_id: str
    identity_id: str
    title: str
    message: str
    read_at: datetime | None
    acknowledged_at: datetime | None
    source_kind: str | None
    source_id: str | None
    kind: str
    link: dict[str, str]
    acknowledgement_required: bool
    replace_action: dict[str, Any] | None = None
    duty_changes: dict[str, list[dict[str, Any]]] | None = None
    published_impact: dict[str, Any] | None = None


class NotificationList(Schema):
    notifications: list[NotificationRow]
    total: int = Field(default=0, ge=0)
    unread_total: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    has_more: bool


NOTIFICATION_UNDO_LIMIT = 5000


class NotificationReadBefore(Schema):
    id: ID
    revision: int = Field(ge=1)
    read_at: datetime | None
    acknowledged_at: datetime | None


class NotificationReceiptReadBefore(Schema):
    id: ID
    revision: int = Field(ge=1)


class NotificationReadResult(Schema):
    notifications: list[NotificationRow] = Field(default_factory=list)
    unread_total: int = Field(ge=0)
    receipt_id: ID | None = None
    activity: int | None = None
    notification: dict[str, Any] | None = None
    receipt: dict[str, Any] | None = None
    acknowledged: int | None = None
    read: int | None = None
    notification_ids: list[ID] = Field(default_factory=list, max_length=NOTIFICATION_UNDO_LIMIT)
    previous_notifications: list[NotificationReadBefore] = Field(default_factory=list, max_length=NOTIFICATION_UNDO_LIMIT)
    previous_receipts: list[NotificationReceiptReadBefore] = Field(default_factory=list, max_length=NOTIFICATION_UNDO_LIMIT)
    unread: int | None = None
    undo_of: ID | None = None
    undo: 'Command | None' = None


class AuditRow(Schema):
    id: ID
    revision: int
    created_at: datetime
    department_id: ID
    actor_id: ID
    action: str
    target_id: ID | None
    reason: str
    detail: dict[str, Any]
    correlation_id: str
    kind: str
    actor_display_name: str | None
    target_label: str | None = None
    # The sentence after the actor (packages/contracts/audit.py audit_summary): search and CSV use the same text.
    summary: str | None = None
    count: int | None = None
    first_at: datetime | None = None
    last_at: datetime | None = None
    first_id: ID | None = None
    last_id: ID | None = None
    run_id: str | None = None


class AuditActor(Schema):
    id: ID
    display_name: str | None


class AuditCursor(Schema):
    before_at: datetime
    before_id: ID


class AuditPage(Schema):
    events: list[AuditRow]
    offset: int
    limit: int
    people: list[AuditActor]
    people_has_more: bool = False
    target_id: ID | None = None
    person_id: ID | None
    has_more: bool
    next_cursor: str | AuditCursor | None = None
    changes: int | None = None


class PersonInput(Schema):
    """On update, send the required display name and any fields to change. Omitted fields keep their stored
    values. Explicit empty or null values clear fields that allow them. A new deactivation with no date starts today."""

    display_name: Name = Field(json_schema_extra={"bulk": True})
    given_name: str = Field(default="", max_length=160, json_schema_extra={"bulk": True, "property": True})
    family_name: str = Field(default="", max_length=160, json_schema_extra={"bulk": True, "property": True})
    active: bool = Field(default=True, json_schema_extra={"bulk": True})
    inactive_from: date | None = Field(default=None, json_schema_extra={"bulk": True})
    title: str = Field(default="", max_length=160, json_schema_extra={"bulk": True, "property": True})
    work_email: Email | Literal[""] = ""
    work_phone: str = Field(default="", max_length=80, json_schema_extra={"bulk": True})
    work_extension: str = Field(default="", max_length=80, json_schema_extra={"bulk": True})
    staff_number: str = Field(default="", max_length=80, json_schema_extra={"bulk": True, "property": True})
    pager: str = Field(default="", max_length=40, json_schema_extra={"bulk": True})
    directory_section: str = Field(default="", max_length=160, json_schema_extra={"bulk": True, "property": True})
    directory_order: int = Field(default=1000, ge=0, le=100000, json_schema_extra={"bulk": True})
    short_code: str = Field(default="", max_length=6, pattern=r"^[A-Z0-9]{0,6}$", json_schema_extra={"bulk": True, "property": True})
    rota_name: str = Field(default='', max_length=160)
    rota_aliases: list[Name] = Field(default_factory=list, max_length=30)
    tags: list[Tag] = Field(default_factory=list, max_length=TAG_LIMITS["per_person"], json_schema_extra={"bulk": True})
    colour: Colour = Field(default='', json_schema_extra={"bulk": True})

    @field_validator('tags')
    @classmethod
    def unique_tags(cls, value):
        cleaned = [v.strip() for v in value]
        if any(not v for v in cleaned) or len({v.lower() for v in cleaned}) != len(cleaned):
            raise ValueError('Tags must be non-empty and unique')
        return cleaned

class ScoreWeights(Schema):
    """Schedule score components."""
    coverage: int = Field(default=35, ge=0, le=MAX_SCORE_INPUT, strict=True)
    rules: int = Field(default=25, ge=0, le=MAX_SCORE_INPUT, strict=True)
    fairness: int = Field(default=20, ge=0, le=MAX_SCORE_INPUT, strict=True)
    preferences: int = Field(default=15, ge=0, le=MAX_SCORE_INPUT, strict=True)
    stability: int = Field(default=5, ge=0, le=MAX_SCORE_INPUT, strict=True)


GenerationWeightPreset = Literal['department_defaults', 'fairness_first', 'preferences_first', 'fewest_changes']

# Run presets change only search weights; None uses the department's configured weights.
GENERATION_WEIGHT_PRESETS: dict[GenerationWeightPreset, dict] = {
    'department_defaults': {'label': 'Department defaults', 'weights': None},
    'fairness_first': {'label': 'Fairness first', 'weights': ScoreWeights(fairness=40, preferences=10).model_dump()},
    'preferences_first': {'label': 'Preferences first', 'weights': ScoreWeights(fairness=10, preferences=40).model_dump()},
    'fewest_changes': {'label': 'Fewest changes', 'weights': ScoreWeights(fairness=10, preferences=10, stability=40).model_dump()},
}


class StrengthMultipliers(Schema):
    low: int = Field(default=1, ge=1, le=MAX_SCORE_INPUT, strict=True)
    medium: int = Field(default=3, ge=1, le=MAX_SCORE_INPUT, strict=True)
    high: int = Field(default=10, ge=1, le=MAX_SCORE_INPUT, strict=True)
    critical: int = Field(default=30, ge=1, le=MAX_SCORE_INPUT, strict=True)

    @model_validator(mode='after')
    def ordered(self):
        if not self.low <= self.medium <= self.high <= self.critical:
            raise ValueError('Strengths must not decrease from low to critical')
        return self


class TierTolerance(Schema):
    """Legacy priority tolerances: accepted for stored options, ignored by the weighted objective."""
    targets: float = Field(default=0, ge=0, le=50)
    preferences: float = Field(default=0, ge=0, le=50)
    changes: float = Field(default=0, ge=0, le=50)
    balance: float = Field(default=0, ge=0, le=50)


class ScoringSettings(Schema):
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    strengths: StrengthMultipliers = Field(default_factory=StrengthMultipliers)
    tolerance: TierTolerance = Field(default_factory=TierTolerance, deprecated=True, exclude=True,
                                     description='Legacy option; ignored by the weighted objective')
    # Staff wishes: every person has the same points to spend per period (0 switches wishes off). Declined wishes
    # raise that person's later wishes, up to wish_debt_cap times their normal weight (1 = no catch-up).
    wish_budget_points: int = Field(default=10, ge=0, le=MAX_WISH_POINTS, strict=True, title='Preference Budget Points')
    wish_period: Literal['week', 'month', 'quarter'] = Field(default='month', title='Preference Period')
    wish_debt_cap: float = Field(default=3, ge=1, le=MAX_SCORE_INPUT, title='Preference Debt Cap')
    wish_priority_decay: float = Field(default=0.5, ge=0, le=1, title='Wish priority retained per period',
        description='Fraction of earlier unmet wish points retained each renewal period (0 forgets earlier periods; 1 retains them).')
    wish_granted_credit: float = Field(default=0.5, ge=0, le=1, title='Granted wish credit',
        description='Fraction of granted wish points deducted from accumulated unmet points before applying the catch-up cap.')
    wish_history_periods: int = Field(default=6, ge=1, strict=True, title='Wish history periods',
        description='Number of completed renewal periods used to calculate wish priority.')
    # Retained for historical configuration; visible Fair share lines now control lead balance.
    lead_balance: Literal['off', 'low', 'medium', 'high', 'critical'] = Field(default='medium', deprecated=True)
    fairness_worst_off_emphasis: float = Field(default=0.5, ge=0, le=1)
    desired_cover_factor: float = Field(default=0.25, ge=0, le=1)
    preferences_mean_weight: int = Field(default=7, ge=0, le=MAX_SCORE_INPUT, strict=True)
    preferences_worst_weight: int = Field(default=3, ge=0, le=MAX_SCORE_INPUT, strict=True)

    @model_validator(mode='after')
    def some_weight(self):
        if not sum(self.weights.model_dump().values()):
            raise ValueError('Give at least one score component a weight')
        if not self.preferences_mean_weight + self.preferences_worst_weight:
            raise ValueError('Give the preference mean or worst-off blend a weight')
        return self


AUTO_SCHEDULE_LIMITS = {'minimum_minutes': 1, 'default_minutes': 5, 'default_max_minutes': 60,
                       'maximum_minutes': 1440, 'maximum_stall_seconds': 600}


class GenerationControls(Schema):
    extension_minutes: int = Field(default=AUTO_SCHEDULE_LIMITS['default_minutes'],
        ge=AUTO_SCHEDULE_LIMITS['minimum_minutes'], le=AUTO_SCHEDULE_LIMITS['maximum_minutes'], strict=True,
        title='Add time step', description='Suggested by Add time on a running search. Each press can still choose up to the run’s maximum.')


GENERATION_CONTROL_DEFAULTS = GenerationControls().model_dump()
GENERATION_RUNTIME_ALIASES = {'fair_share_stop_without_improvement_seconds': 'fairness_plateau_seconds'}
GENERATION_RUNTIME_OPTIONS = frozenset({'search_minutes', 'max_search_minutes', 'stop_without_improvement_seconds',
                                      'fairness_plateau_seconds', 'target_gap_percent', 'target_score', 'stop_by',
                                      'on_conflict', 'generation_stop_grace_seconds'})
GENERATION_SEARCH_FINISHED_STAGES = ('validating', 'summarizing', 'ready', 'explaining')
GENERATION_PROGRESS_STAGES = {'queued': 'Queued', 'checking': 'Checking rules', 'preparing': 'Preparing',
    'finding': 'Searching', 'improving': 'Improving', 'searching': 'Searching', 'scoring': 'Scoring',
    'validating': 'Checking result', 'summarizing': 'Scoring', 'ready': 'Ready', 'explaining': 'Explaining conflicts'}
AUTOSCHEDULE_PRECHECK_LIMITS = {'issues': 50, 'samples': 3}


class GenerationOptions(Schema):
    # A strict infeasibility proof is the default.  `continue_flagged` is an explicit
    # request for the least-breaks proposal, whose mandatory findings remain
    # visible and block publication.
    on_conflict: Literal['report', 'continue_flagged'] = Field(default='report', title='If rules conflict',
        description='Stop and show the rules that cannot all be met, or continue with the fewest broken rules. Every break is a must-fix and blocks publishing until fixed.')
    # Titles and descriptions are the words every screen uses for these times (GENERATION_TIME_FIELDS, one source).
    search_minutes: int = Field(default=AUTO_SCHEDULE_LIMITS['default_minutes'],
        ge=AUTO_SCHEDULE_LIMITS['minimum_minutes'], le=AUTO_SCHEDULE_LIMITS['maximum_minutes'], strict=True,
        title='Search time', description='Preparation and final checks do not use the search time.')
    max_search_minutes: int = Field(default=AUTO_SCHEDULE_LIMITS['default_max_minutes'],
        ge=AUTO_SCHEDULE_LIMITS['minimum_minutes'], le=AUTO_SCHEDULE_LIMITS['maximum_minutes'], strict=True,
        title='Maximum search time', description='The longest a run may search, Add time included.')
    # Each objective step stops once no better schedule has turned up for this long (0 = use the whole budget).
    # A proven optimum stops at once either way.
    stop_without_improvement_seconds: int = Field(default=10, ge=0,
        le=AUTO_SCHEDULE_LIMITS['maximum_stall_seconds'], strict=True,
        title='Stop after no improvement', description='0 uses the whole search time.')
    # Optional run stops. A gap is the relative distance between the incumbent and the best native bound;
    # target_score is the canonical 0..100 schedule score. None leaves that condition disabled.
    target_gap_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False,
        title='Stop when within (%)', description='Stops once the schedule is proven this close to the best possible one. Empty: off.')
    target_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False,
        title='Stop at score', description='Stops once the schedule score (0–100) reaches this. Empty: off.')
    stop_by: Clock | None = Field(default=None, title='Stop by',
        description='Stops searching at this time today, in department time. For one run only.')
    # Extends the ordinary plateau while unmet Fair share improves within this window; 0 permits the full allowance.
    fairness_plateau_seconds: int = Field(default=15, ge=0,
        le=AUTO_SCHEDULE_LIMITS['maximum_stall_seconds'], strict=True, title='Keep improving fairness',
        description='While fair shares still improve, the search goes on until this long passes without progress. 0 uses the whole search time.')
    # The first pass searches this many candidates per place of each duty (0 = everyone from the start); a second
    # pass then lets everyone in to improve that schedule.
    shortlist_width: int = Field(default=3, ge=0, le=20, strict=True)
    # The 150-person / 532-session named proof took 45.35 s under contention; retain about 15 s margin.
    diagnostic_seconds: int = Field(default=60, ge=1, le=60, strict=True, title='Time to explain unfilled places',
        description='Used to say why a place cannot be filled.')
    generation_stop_grace_seconds: int = Field(default=10, ge=1, le=15, strict=True,
        title='Time to finish after Stop',
        description='Shared allowance for final checks and scoring after Stop. An unchecked incumbent stays resumable; an unfinished score is unavailable.')
    mode: Literal['rebalance', 'fill_gaps'] = 'fill_gaps'
    weight_preset: GenerationWeightPreset = 'department_defaults'
    objective_order: list[Literal['targets', 'preferences', 'changes', 'balance']] = Field(
        default_factory=lambda: ['targets', 'preferences', 'changes', 'balance'], min_length=4, max_length=4,
        deprecated=True, exclude=True, description='Legacy option; coverage always precedes the weighted objective')
    overtime_penalty: int = Field(default=1, ge=1, le=MAX_SCORE_INPUT, strict=True)
    balance_measure: Literal['duties', 'reserved_hours'] = 'reserved_hours'
    allocation_order: Literal['weighted', 'tiered'] = 'weighted'
    fill_tier_time_share: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False,
        title='Fill-priority time share (0–1)',
        description='Maximum share of time remaining after coverage and mandatory minima for fill-priority steps; the rest improves schedule quality. 0 skips fill-priority search.')
    pair_limit_max_opportunities: int = Field(default=50000, ge=1, le=1000000, strict=True)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)

    @model_validator(mode='before')
    @classmethod
    def legacy_fairness_window(cls, value):
        if isinstance(value, dict) and any(name in value for name in GENERATION_RUNTIME_ALIASES):
            value = dict(value)
            for old, current in GENERATION_RUNTIME_ALIASES.items():
                if old not in value:
                    continue
                seconds = value.pop(old)
                if current in value and value[current] != seconds:
                    raise ValueError('Fairness plateau settings disagree')
                value[current] = seconds
        return value

    @model_validator(mode='before')
    @classmethod
    def legacy_conflict_policy(cls, value):
        # Accept names from the first draft while serialising the public
        # report/continue_flagged vocabulary.
        if isinstance(value, dict) and value.get('on_conflict') in {'stop', 'continue'}:
            value = dict(value)
            value['on_conflict'] = {'stop': 'report', 'continue': 'continue_flagged'}[value['on_conflict']]
        return value

    @model_validator(mode='before')
    @classmethod
    def legacy_search_seconds(cls, value):
        if isinstance(value, dict) and 'search_seconds' in value:
            value = dict(value)
            seconds = value.pop('search_seconds')
            if type(seconds) is not int or seconds not in (300, 600):
                raise ValueError('Legacy search time must be 300 or 600 seconds; use search_minutes')
            if 'search_minutes' in value and value['search_minutes'] != seconds // 60:
                raise ValueError('Search minutes and legacy search seconds disagree')
            value['search_minutes'] = seconds // 60
        return value

    @model_validator(mode='after')
    def search_within_maximum(self):
        # Run overrides are checked against department limits after merging with its saved defaults.
        if 'max_search_minutes' in self.model_fields_set and self.search_minutes > self.max_search_minutes:
            raise ValueError('Search minutes cannot exceed Maximum auto-schedule minutes')
        return self

    @field_validator('objective_order')
    @classmethod
    def every_objective_once(cls, value):
        if len(set(value)) != 4:
            raise ValueError('Include each scheduling priority exactly once')
        return value


class BulkSettings(Schema):
    bulk_max_items: int = Field(default=2000, ge=1, le=5000, strict=True, title='Most items per change',
        description='Maximum affected records in one bulk change.')
    bulk_direct_max: int = Field(default=50, ge=0, le=2000, strict=True, title='Most applied directly',
        description='Maximum ready items applied without opening the review panel; 0 always opens it.')
    bulk_staged_days: int = Field(default=14, ge=1, le=365, strict=True, title='Days staged changes are kept',
        description='Days to keep unapplied staged changes before they expire.')
    rate_bulk_preview_per_minute: int = Field(default=30, ge=0, le=10000, strict=True, title='Previews per minute',
        description='Previews allowed per person per minute in each session; 0 means no limit.')

    @model_validator(mode='after')
    def direct_limit(self):
        if self.bulk_direct_max > self.bulk_max_items:
            raise ValueError('The direct-change limit cannot exceed the bulk item limit')
        return self


class BulkSettingsInput(BulkSettings):
    reason: Reason

    @model_validator(mode='after')
    def direct_limit(self):
        # Partial updates are checked against the saved settings after the service merges them.
        if {'bulk_direct_max', 'bulk_max_items'} <= self.model_fields_set:
            return super().direct_limit()
        return self


SWAP_KINDS = ("exchange", "give")
SwapKind = Literal[SWAP_KINDS]


class WishTypeSettings(Schema):
    allowed: bool = True
    may_repeat: bool = True
    default_points: WishPoints = 1
    # Historical values remain readable; department.settings refuses unsupported routing.
    approval: Literal['none', 'when_soft_clash', 'always'] = 'none'
    approver_roles: list[RoleName] = Field(default_factory=list, max_length=len(ROLE_NAMES))

    @field_validator('approver_roles')
    @classmethod
    def distinct_roles(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Each approver role once')
        return value


class WishSettings(Schema):
    types: dict[WishKind, WishTypeSettings] = Field(default_factory=dict)

    @model_validator(mode='after')
    def all_kinds(self):
        self.types = {kind: self.types.get(kind, WishTypeSettings()) for kind in WISH_KINDS}
        return self


MAX_APPROVALS_REQUIRED = 3


class AutoscheduleBigMoveSettings(Schema):
    min_row_distance: int = Field(default=4, ge=1, le=40, strict=True)
    swaps: bool = Field(default=True, strict=True)
    fills_from_open: bool = Field(default=True, strict=True)
    cross_session: bool = Field(default=True, strict=True)


class AutoscheduleAnimationSettings(Schema):
    mode: Literal['hybrid', 'fly', 'roll', 'fade', 'retype', 'off'] = 'hybrid'
    big_move: AutoscheduleBigMoveSettings = Field(default_factory=AutoscheduleBigMoveSettings)
    big_style: Literal['fly', 'roll', 'fade'] = 'fly'
    small_style: Literal['roll', 'fade', 'retype'] = 'roll'
    max_concurrent_flights: int = Field(default=16, ge=4, le=32, strict=True)
    max_updates_per_second: int = Field(default=3, ge=1, le=8, strict=True)
    step_change: Literal['roll', 'retype'] = 'roll'
    speed: float = Field(default=1.0, ge=0.5, le=2.0, strict=True)


class DepartmentCreateInput(Schema):
    name: Annotated[Name, StringConstraints(strip_whitespace=False)]
    first_admin_identity_id: ID
    reason: Reason

    @field_validator('name')
    @classmethod
    def test_department_name(cls, value):
        name = value.strip()
        if not name.endswith('(test)') or not name[:-6].strip():
            raise ValueError('A test department name must end with (test) and include a department name')
        return value


class OrganisationSettingsHolder(Schema):
    identity_id: ID
    display_name: Name


class DepartmentCreatePreview(Schema):
    preview: Literal[True]
    kind: Literal['department.create']
    would_create: bool
    name: Annotated[Name, StringConstraints(strip_whitespace=False)]
    department_id: ID | None
    first_admin_identity_id: ID
    org_settings_holders_lost: list[OrganisationSettingsHolder]
    expected_fingerprint: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]


class DepartmentSettingsInput(Schema):
    generation_controls: GenerationControls | None = None
    score_check_enabled: bool | None = Field(default=None, strict=True)
    autoschedule_animation: AutoscheduleAnimationSettings | None = None
    bulk_settings: BulkSettings | None = None
    generation_defaults: GenerationOptions | None = Field(default=None,
        description='Recursively merges explicitly supplied fields into versioned department defaults; omitted groups and fields are preserved.')
    inherit_generation_mode: bool = Field(default=False, strict=True)
    wish_settings: WishSettings | None = None
    swap_review_route_id: ID | None = None
    swap_auto_prepare: bool = Field(default=True, strict=True)
    swap_auto_publish: bool = Field(default=True, strict=True)
    policy_retire_approvals_required: int | None = Field(default=None, ge=1, le=MAX_APPROVALS_REQUIRED, strict=True)
    department_name: Name
    department_revision: int = Field(ge=1, strict=True)
    organisation_name: Name
    timezone: str = Field(min_length=1, max_length=80)
    week_start: int = Field(ge=0, le=6, strict=True)
    # Weekend days, Monday 0 … Sunday 6 (unset keeps the department's); calendars shade them.
    weekend_days: list[Annotated[int, Field(ge=0, le=6, strict=True)]] | None = Field(default=None, max_length=6)
    # Omission preserves the selection; explicit null clears it.
    operating_calendar_id: ID | None = None
    # How leave shows on planning findings (unset keeps the department's): none, its type and dates, or also its note.
    leave_reason_display: Literal['none', 'type', 'details'] | None = None
    # Unset keeps the saved value; the ceiling is PostgreSQL INTEGER storage, not department policy.
    swap_offer_window_days: int | None = Field(default=None, ge=SWAP_LIMITS["minimum"], le=SWAP_LIMITS["maximum"], strict=True)
    swap_open_offer_cap: int | None = Field(default=None, ge=SWAP_LIMITS["minimum"], le=SWAP_LIMITS["maximum"], strict=True)
    swap_kinds_allowed: list[SwapKind] | None = Field(default=None, max_length=len(SWAP_KINDS))
    swap_open_offers: bool | None = Field(default=None, strict=True)
    # Omitted fields preserve saved settings for older clients; explicit null disables automatic reminders.
    announcement_send_time: Annotated[str, StringConstraints(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')] | None = None
    announcement_auto_reminder_days: int | None = Field(default=None, ge=1, le=3650, strict=True)
    announcement_reminder_interval_hours: int | None = Field(default=None, ge=1, le=8760, strict=True)
    event_horizon_days: int | None = Field(default=None, ge=7, le=EVENT_HORIZON_MAX_DAYS, strict=True)
    event_receipt_window_days: int | None = Field(default=None, ge=1, le=EVENT_HORIZON_MAX_DAYS, strict=True)
    reason: Reason

    @model_validator(mode='after')
    def unambiguous_generation_mode(self):
        if (self.inherit_generation_mode and self.generation_defaults is not None
                and 'mode' in self.generation_defaults.model_fields_set):
            raise ValueError('Choose either an inherited or explicit assignment mode')
        return self

    @field_validator('swap_kinds_allowed')
    @classmethod
    def distinct_swap_kinds(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('Each swap kind once')
        return [kind for kind in SWAP_KINDS if kind in value] if value is not None else None

    @field_validator('weekend_days')
    @classmethod
    def distinct_weekend(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError('Each weekend day once')
        return sorted(value) if value is not None else None

    @field_validator('timezone')
    @classmethod
    def known_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Unknown IANA timezone')
        return value


class OpeningWindow(Schema):
    start_time: Clock
    end_time: Clock

    @model_validator(mode="after")
    def ordered(self):
        if self.start_time == "24:00" or self.end_time <= self.start_time:
            raise ValueError("Opening windows must be ordered within one day; split overnight windows at midnight")
        return self


def opening_windows_are_disjoint(windows):
    ordered = sorted(windows, key=lambda w: w.start_time)
    if any(a.end_time > b.start_time for a, b in zip(ordered, ordered[1:])):
        raise ValueError("Opening windows must not overlap")
    return windows


class OpeningDay(Schema):
    weekday: int = Field(ge=0, le=6)
    windows: list[OpeningWindow] = Field(default_factory=list, max_length=8)

    @field_validator("windows")
    @classmethod
    def disjoint(cls, value):
        return opening_windows_are_disjoint(value)


class CalendarOverride(Schema):
    on: date
    label: Name
    windows: list[OpeningWindow] = Field(default_factory=list, max_length=8)

    @field_validator("windows")
    @classmethod
    def disjoint(cls, value):
        return opening_windows_are_disjoint(value)


class DateSetOverride(Schema):
    """The recurrence's native year: Hijri for a Hijri repeat, Gregorian otherwise."""
    year: int = Field(ge=1, le=9999, strict=True)
    first_day: date
    last_day: date

    @model_validator(mode='after')
    def ordered(self):
        if self.last_day < self.first_day:
            raise ValueError('The last day must be on or after the first day')
        return self


class DateSetEntry(Schema):
    id: ID = Field(default_factory=lambda: str(uuid4()))
    name: Name
    repeat: Literal['once', 'yearly_gregorian', 'yearly_hijri'] = 'once'
    first_day: Annotated[str, StringConstraints(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    last_day: Annotated[str, StringConstraints(pattern=r'^\d{4}-\d{2}-\d{2}$')]
    last_day_of_month: bool = False
    start_year: int | None = Field(default=None, ge=1, le=9999, strict=True)
    fixed_duration: bool = False
    overrides: list[DateSetOverride] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def valid_dates(self):
        from packages.domain.date_sets import validate_entry
        validate_entry(self.model_dump(mode='json'))
        return self


class DateSetInput(Schema):
    name: Name = Field(json_schema_extra={"bulk": True})
    active: bool = Field(default=True, json_schema_extra={"bulk": True})
    entries: list[DateSetEntry] = Field(default_factory=list, max_length=500, json_schema_extra={"bulk": True})

    @model_validator(mode='after')
    def distinct_entries(self):
        if len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError('Entry IDs must be unique within the set')
        return self


class DateSetDeleteInput(Schema):
    reason: Reason


class DateSetRecord(DateSetInput):
    id: ID
    department_id: ID
    revision: int = Field(ge=1)
    created_at: datetime


class DateSetEntryWriteInput(DateSetDeleteInput):
    entry: DateSetEntry


class DateSetEntryDeleteInput(DateSetDeleteInput):
    entry_id: ID


class DateSetAdjustmentInput(DateSetEntryDeleteInput):
    year: int = Field(ge=1, le=9999, strict=True)
    override: DateSetOverride | None

    @model_validator(mode='after')
    def same_year(self):
        if self.override is not None and self.override.year != self.year:
            raise ValueError('The adjustment must name the same recurrence year')
        return self


class DateSetImportInput(DateSetDeleteInput):
    format: Literal['csv', 'ical']
    content: str = Field(min_length=1, max_length=1024 * 1024)


class DateSetCopyInput(DateSetDeleteInput):
    year: int = Field(ge=1, le=9998, strict=True)
    name: Name | None = None


class DateSetMarker(Schema):
    set_id: ID
    set_name: Name
    entry_id: ID
    name: Name


class ResolvedDateSetEntry(DateSetMarker):
    first_day: date
    last_day: date
    year: int
    adjusted: bool = False


class DateSetCalendarRule(Schema):
    date_set_id: ID
    entry_names: list[Name] = Field(default_factory=list, max_length=500)
    windows: list[OpeningWindow] = Field(default_factory=list, max_length=8)

    @field_validator('windows')
    @classmethod
    def disjoint(cls, value):
        return opening_windows_are_disjoint(value)


class OperatingCalendarDefinition(Schema):
    schema_version: Literal[1] = 1
    timezone: str
    week: list[OpeningDay] = Field(default_factory=list, max_length=7)
    overrides: list[CalendarOverride] = Field(default_factory=list, max_length=366)
    date_set_rules: list[DateSetCalendarRule] = Field(default_factory=list, max_length=500, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def unambiguous(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown IANA timezone")
        if len({d.weekday for d in self.week}) != len(self.week):
            raise ValueError("Only one opening-day definition per weekday")
        if len({d.on for d in self.overrides}) != len(self.overrides):
            raise ValueError("Only one replacement override per date")
        return self


class OperatingCalendarInput(NamedInput):
    definition: OperatingCalendarDefinition = Field(json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def effective_overrides(self):
        if any(d.on < self.starts_on or (self.ends_on and d.on >= self.ends_on) for d in self.definition.overrides):
            raise ValueError("Calendar overrides must lie within the effective interval")
        return self


class ResourcePoolInput(NamedInput):
    capacity: int = Field(ge=1, le=1000, strict=True, json_schema_extra={"bulk": True})
    location_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    calendar_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})


class LocationInput(EffectiveInput):
    name: Name = Field(json_schema_extra={"bulk": True})
    location_type_id: ID = Field(json_schema_extra={"bulk": True})
    parent_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    calendar_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    capacity: int = Field(default=1, ge=1, le=1000, json_schema_extra={"bulk": True})


class MembershipBulkInput(Schema):
    """Give many people the same rank from a date; optionally end their other ranks from that date."""
    person_ids: list[ID] = Field(min_length=1, max_length=500)
    category_id: ID
    starts_on: date
    ends_on: date | None = None
    end_other_ranks: bool = False
    reason: Reason

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.person_ids)) != len(self.person_ids):
            raise ValueError("Each person once")
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("End date is exclusive and must follow start date")
        return self


class MembershipInput(Schema):
    person_id: ID
    category_id: ID
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("End date must follow start date")
        return self


class RelationshipUpdateDates(Schema):
    starts_on: date | None = Field(default=None, json_schema_extra={"bulk": True, "bulk_clear": False})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})
    reason: Reason

    @model_validator(mode="after")
    def dates_to_change(self):
        if not self.model_fields_set & {"starts_on", "ends_on"}:
            raise ValueError("Choose a start or end date to change")
        if "starts_on" in self.model_fields_set and self.starts_on is None:
            raise ValueError("A start date is required")
        if self.starts_on is not None and self.ends_on is not None and self.ends_on <= self.starts_on:
            raise ValueError("End date must follow start date")
        return self


class EligibilityInput(Schema):
    person_id: ID
    service_id: ID
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("End date must follow start date")
        return self


class GrantInput(Schema):
    person_id: ID
    capability_id: ID
    service_id: ID | None = None
    evidence: Reason
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("End date must follow start date")
        return self


class RequiredWhen(Schema):
    field: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
    equals: str | bool | float


class CustomField(Schema):
    key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
    label: Name
    kind: Literal["text", "long_text", "number", "boolean", "date", "choice", "multi_choice"]
    required: bool = False
    required_when: RequiredWhen | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: list[Name] = Field(default_factory=list, max_length=30)
    help_text: str = Field(default="", max_length=400)
    visibility: Literal["staff", "planners"] = "staff"
    export: bool = False

    @model_validator(mode="after")
    def compatible(self):
        if self.kind in {"choice", "multi_choice"} and not self.options:
            raise ValueError("Choice fields require options")
        if self.kind not in {"choice", "multi_choice"} and self.options:
            raise ValueError("Options only apply to choice fields")
        if len(set(self.options)) != len(self.options):
            raise ValueError("Options must be unique")
        if self.kind != "number" and (self.minimum is not None or self.maximum is not None):
            raise ValueError("Numeric bounds only apply to numbers")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Minimum exceeds maximum")
        return self


class CapabilityClause(Schema):
    capability_id: ID
    minimum: int = Field(ge=1, le=50)


MAX_DUTY_DURATION_MINUTES = 2160


class RequirementSegment(Schema):
    from_minute: int = Field(default=0, ge=0, le=MAX_DUTY_DURATION_MINUTES)
    to_minute: int | None = Field(default=None, ge=1, le=MAX_DUTY_DURATION_MINUTES)
    minimum: int = Field(default=1, ge=0, le=100)
    target: int | None = Field(default=None, ge=0, le=100)
    maximum: int | None = Field(default=None, ge=0, le=100)
    capabilities: list[CapabilityClause] = Field(default_factory=list, max_length=12)
    distinct_capability_seats: bool = False

    @model_validator(mode="after")
    def bounds(self):
        if self.to_minute is not None and self.to_minute <= self.from_minute:
            raise ValueError("Requirement end must follow start")
        if self.target is not None and self.target < self.minimum:
            raise ValueError("Target cannot be below minimum")
        if self.maximum is not None and (self.maximum < self.minimum or (self.target is not None and self.maximum < self.target)):
            raise ValueError("Maximum cannot be below minimum or target")
        if self.distinct_capability_seats and len(self.capabilities) < 2:
            raise ValueError("Distinct seats require at least two capability clauses")
        return self


def requirement_key(row: dict) -> tuple:
    """Canonical identity for a requirement segment's safety scope."""
    capabilities = tuple(sorted(str(clause.get('capability_id')) for clause in row.get('capabilities') or []))
    return (row.get('from_minute', 0), row.get('to_minute'), capabilities,
            bool(row.get('distinct_capability_seats', False)))


class Supervision(Schema):
    trainee_category_ids: list[ID] = Field(min_length=1, max_length=100)
    capability_id: ID
    trainees_per_supervisor: int = Field(ge=1, le=20)
    require_pairing: bool = False


class TeamLead(Schema):
    """Who may lead a session of this duty type, and how many leads it needs at every moment."""
    minimum: int = Field(default=1, ge=0, le=20)
    maximum: int | None = Field(default=1, ge=1, le=20)  # None = no upper limit
    capability_ids: list[ID] = Field(default_factory=list, max_length=100)  # any one of these skills; empty = any
    category_ids: list[ID] = Field(default_factory=list, max_length=100)  # any one of these ranks; empty = any
    conditions: Conditions | None = None  # and these person conditions (the shared condition model)
    # Historical input, retained for additive conversion into visible Workload limit lines.
    most_turns: int | None = Field(default=None, ge=1, le=100, strict=True, deprecated=True)
    most_turns_per: Literal["week", "month"] = "month"

    @model_validator(mode="after")
    def ordered(self):
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("The most team leads cannot be fewer than the fewest")
        only_fields(self.conditions, LEAD_FIELDS, "Who may lead")
        for values in (self.capability_ids, self.category_ids):
            if len(values) != len(set(values)):
                raise ValueError("Team lead skills and ranks must be unique")
        return self


class TeamLeadRequirement(Schema):
    """Named leadership: Who qualifies occupants; Where determines the duty obligation.

    ``count_scope='concurrent_duties'`` counts distinct named leaders across all
    concurrent duties matched by the same catalogue line, interval by interval.
    """
    kind: Literal['team_lead_requirement']
    minimum: int = Field(default=0, ge=0, le=20)
    maximum: int | None = Field(default=None, ge=1, le=20)
    continuity: Literal['whole_duty', 'continuous'] = 'whole_duty'
    count_scope: Literal['duty', 'concurrent_duties'] = 'duty'

    @model_validator(mode='after')
    def ordered(self):
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError('The most team leads cannot be fewer than the fewest')
        return self


class ResourceRequirement(Schema):
    resource_pool_id: ID
    units: int = Field(ge=1, le=1000, strict=True)
    from_minute: int = Field(default=0, ge=0, le=MAX_DUTY_DURATION_MINUTES)
    to_minute: int | None = Field(default=None, ge=1, le=MAX_DUTY_DURATION_MINUTES)

    @model_validator(mode="after")
    def ordered(self):
        if self.to_minute is not None and self.to_minute <= self.from_minute:
            raise ValueError("Resource demand end must follow start")
        return self


class DutyTiming(Schema):
    start_time: Clock
    end_time: Clock

    @model_validator(mode="after")
    def ordered(self):
        if self.start_time == "24:00" or self.start_time == self.end_time:
            raise ValueError("Choose a valid start and a different end; earlier ends mean next day")
        return self


class TypeDefinition(Schema):
    schema_version: Literal[1] = 1
    purpose: Literal["staffed", "group", "protected"] = "staffed"
    extra_cover: bool = False
    extra_cover_unfilled: Literal['open', 'gap'] = 'open'
    colour: Colour = ''  # '' = automatic (stable colour from the type id)
    maximum_duration_minutes: int = Field(default=720, ge=15, le=MAX_DUTY_DURATION_MINUTES)
    default_timing: DutyTiming | None = None
    allowed_category_ids: list[ID] = Field(default_factory=list, max_length=100)
    allowed_location_type_ids: list[ID] = Field(default_factory=list, max_length=100)
    allow_shared_services: bool = False
    shared_duty_reason: str = Field(default="", max_length=500)
    requirements: list[RequirementSegment] = Field(default_factory=lambda: [RequirementSegment()], max_length=24)
    resources: list[ResourceRequirement] = Field(default_factory=list, max_length=24)
    supervision: Supervision | None = None
    lead: TeamLead | None = None  # Frozen compatibility input; new leadership is governed in Rules.
    workload_credit_per_hour: float = Field(default=1, ge=0, le=10)
    session_units: float = Field(default=1, ge=0, le=100)
    session_unit_basis: Literal['fixed', 'proportional'] = 'fixed'
    training_credit_per_hour: float = Field(default=0, ge=0, le=10)
    fields: list[CustomField] = Field(default_factory=list, max_length=15)

    @model_validator(mode="after")
    def combinations(self):
        if self.default_timing:
            start, end = (int(t[:2]) * 60 + int(t[3:]) for t in
                          (self.default_timing.start_time, self.default_timing.end_time))
            duration = (end - start) % 1440 or 1440
            if duration > self.maximum_duration_minutes:
                raise ValueError("Default timing exceeds the type's maximum duration")
        if self.allow_shared_services and len(self.shared_duty_reason.strip()) < 3:
            raise ValueError("Shared services require a reviewed common-duty explanation")
        if not self.allow_shared_services and self.shared_duty_reason:
            raise ValueError("A shared-duty reason requires shared services to be enabled")
        if self.purpose == "protected" and (self.requirements or self.supervision or self.lead):
            raise ValueError("Protected time reserves attendance; it does not supply staffed cover")
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("Custom field keys must be unique")
        by_key = {f.key: f for f in self.fields}
        for f in self.fields:
            if f.required_when:
                dep = by_key.get(f.required_when.field)
                if dep is None or dep is f:
                    raise ValueError("Required conditions must reference another declared field")
                # One-level conditions only: a bounded language with no cycles/chains.
                if dep.required_when:
                    raise ValueError("Nested conditional requirements are unsupported")
                if dep.kind not in {"text", "number", "boolean", "choice"}:
                    raise ValueError("Conditional source must be a supported scalar field")
                validate_field_value(dep, f.required_when.equals)
        for demand in self.resources:
            if demand.from_minute >= self.maximum_duration_minutes or (demand.to_minute and demand.to_minute > self.maximum_duration_minutes):
                raise ValueError("Resource interval exceeds the type's maximum duration")
        for i, demand in enumerate(self.resources):
            for other in self.resources[i + 1:]:
                if (demand.resource_pool_id == other.resource_pool_id
                        and demand.from_minute < (other.to_minute or self.maximum_duration_minutes)
                        and other.from_minute < (demand.to_minute or self.maximum_duration_minutes)):
                    raise ValueError("Intervals for the same pool must not overlap; specify total required units")
        for segment in self.requirements:
            if segment.from_minute >= self.maximum_duration_minutes or (segment.to_minute and segment.to_minute > self.maximum_duration_minutes):
                raise ValueError("Requirement interval exceeds the type's maximum duration")
        return self


class AssignmentTypeInput(NamedInput):
    short_code: str = Field(default='', max_length=6, pattern=r'^[A-Z0-9]{0,6}$', json_schema_extra={"bulk": True})
    definition: TypeDefinition = Field(json_schema_extra={"bulk": True})


def validate_field_value(field: CustomField, value: Any) -> None:
    if field.kind in {"text", "long_text"}:
        if not isinstance(value, str) or len(value) > (4000 if field.kind == "long_text" else 300):
            raise ValueError(f"{field.label}: invalid text")
    elif field.kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field.label}: number required")
        import math
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or (field.minimum is not None and value < field.minimum) or (field.maximum is not None and value > field.maximum):
            raise ValueError(f"{field.label}: number outside allowed range")
    elif field.kind == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{field.label}: boolean required")
    elif field.kind == "date":
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError(f"{field.label}: ISO date required")
        date.fromisoformat(value)
    elif field.kind == "choice":
        if value not in field.options:
            raise ValueError(f"{field.label}: select a declared option")
    elif field.kind == "multi_choice":
        if not isinstance(value, list) or any(not isinstance(x, str) or x not in field.options for x in value) or len(set(value)) != len(value):
            raise ValueError(f"{field.label}: select unique declared options")


def validate_attributes(definition: TypeDefinition, values: dict) -> dict:
    by_key = {field.key: field for field in definition.fields}
    if set(values) - set(by_key):
        raise ValueError("Unknown custom field")
    for field in definition.fields:
        required = field.required or (field.required_when is not None and values.get(field.required_when.field) == field.required_when.equals)
        value = values.get(field.key)
        if value is None or value == "" or value == []:
            if required:
                raise ValueError(f"{field.label}: value required")
        else:
            validate_field_value(field, value)
    return values


class Scope(Schema):
    rotation_type_id: ID | None = None
    person_id: ID | None = None
    person_ids: list[ID] = Field(default_factory=list, max_length=500)
    category_id: ID | None = None
    category_ids: list[ID] = Field(default_factory=list, max_length=100)
    service_id: ID | None = None
    service_ids: list[ID] = Field(default_factory=list, max_length=32)
    location_id: ID | None = None
    programme_id: ID | None = None
    assignment_type_id: ID | None = None
    # Groups of people: anyone with one of these tags, or holding one of these capabilities (both, when both are set).
    tags: list[Tag] = Field(default_factory=list, max_length=8)
    capability_ids: list[ID] = Field(default_factory=list, max_length=32)
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    start_time: Clock = "00:00"
    end_time: Clock = "24:00"
    # Who and what, as one condition tree (the same as announcement audiences). Matching needs the fields above AND these.
    conditions: Conditions | None = None

    @model_validator(mode='after')
    def service_selection(self):
        only_fields(self.conditions, SCOPE_FIELDS, "A rule")
        if len({t.lower() for t in self.tags}) != len(self.tags) or len(set(self.capability_ids)) != len(self.capability_ids):
            raise ValueError('Select each tag and capability once')
        if self.person_id and self.person_ids:
            raise ValueError('Use either one person or a list of people')
        if len(self.person_ids) != len(set(self.person_ids)):
            raise ValueError('Select each person once')
        if self.category_id and self.category_ids:
            raise ValueError('Use either a single rank or a rank group')
        if len(self.category_ids) != len(set(self.category_ids)):
            raise ValueError('Select each rank once')
        if self.service_id and self.service_ids:
            raise ValueError('Use either a single service or a service group')
        if len(self.service_ids) != len(set(self.service_ids)):
            raise ValueError('Select each service once')
        return self

    def chosen_services(self) -> list[str]:
        """Clinics named by a positive top-level condition (pooled cover needs an explicit set)."""
        from packages.domain.conditions import absolute_values
        c = self.conditions
        return [v for line in (c.conditions if c and c.match == 'all' else [])
                if isinstance(line, Condition) and line.field == 'service_ids' and line.op in {'any', 'is'}
                for v in absolute_values(line) or []]

    @field_validator("weekdays")
    @classmethod
    def valid_weekdays(cls, value):
        if len(set(value)) != len(value) or any(day not in range(7) for day in value):
            raise ValueError("Use unique weekdays Monday=0 through Sunday=6")
        return value


class Restriction(Schema):
    leave_buffer_days: int | None = Field(default=None, ge=1, le=31, strict=True)
    kind: Literal["restriction"]
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    start_time: Clock = "00:00"
    end_time: Clock = "24:00"

    @field_validator("weekdays")
    @classmethod
    def weekdays_valid(cls, value):
        if len(value) != len(set(value)) or any(x not in range(7) for x in value):
            raise ValueError("Weekdays must be unique Monday=0 through Sunday=6")
        return value


class MaxHours(Schema):
    kind: Literal["max_hours"]
    hours: float = Field(ge=0, le=744)
    window: Literal["day", "week", "rolling"] = "week"
    window_days: int = Field(default=7, ge=1, le=31)
    include_overtime: bool = True

    @model_validator(mode="after")
    def no_unused_window(self):
        if self.window != "rolling" and self.window_days != 7:
            raise ValueError("window_days is configurable only for rolling windows")
        return self


class MaxDuties(Schema):
    kind: Literal["max_duties"]
    maximum: int = Field(ge=0, le=500)
    window: Literal["day", "week", "rolling"] = "week"
    window_days: int = Field(default=7, ge=1, le=31, strict=True)
    include_overtime: bool = True
    group_by: Literal['all', 'supervisor', 'weekday'] = 'all'

    @model_validator(mode='after')
    def configured_window(self):
        if self.window != 'rolling' and self.window_days != 7:
            raise ValueError('window_days is configurable only for rolling windows')
        return self


class MinRest(Schema):
    kind: Literal["min_rest"]
    hours: float = Field(gt=0, le=168)


class ForbiddenTransition(Schema):
    kind: Literal['forbidden_transition']
    overtime_only: bool = False
    from_type_ids: list[ID] = Field(min_length=1, max_length=32)
    to_type_ids: list[ID] = Field(min_length=1, max_length=32)

    @field_validator('from_type_ids', 'to_type_ids')
    @classmethod
    def unique_types(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Select each duty type once')
        return value


class ConsecutiveDays(Schema):
    kind: Literal["max_consecutive_days"]
    maximum: int = Field(ge=1, le=31)
    include_overtime: bool = True
    overtime_extension_days: int = Field(default=0, ge=0, le=31, strict=True)

    @model_validator(mode='after')
    def one_overtime_mode(self):
        if not self.include_overtime and self.overtime_extension_days:
            raise ValueError('Exclude overtime or extend the run, not both')
        return self


WEEKEND_START_DEFAULT = 4
WEEKEND_DAYS_DEFAULT = 2


class ConsecutiveWeekends(Schema):
    kind: Literal['max_consecutive_weekends']
    maximum: int = Field(default=1, ge=1, le=4)
    weekend_start: int = Field(default=WEEKEND_START_DEFAULT, ge=0, le=6)
    weekend_days: int = Field(default=WEEKEND_DAYS_DEFAULT, ge=1, le=3)
    overtime_only: bool = False


class TravelBuffer(Schema):
    kind: Literal["travel_buffer"]
    from_location_id: ID
    to_location_id: ID
    minutes: int = Field(ge=1, le=1440)

    @model_validator(mode="after")
    def different_sites(self):
        if self.from_location_id == self.to_location_id:
            raise ValueError("Travel requires different locations")
        return self


def quota_target_amount(metric, amount):
    """Count targets use two decimal places, rounding a halfway value up; other quota units retain their precision."""
    if amount is not None and metric in ('duties', 'lead_turns'):
        return float(Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    return amount


class BlockQuota(Schema):
    """A department-owned block commitment or recurring calendar limit, never a payroll entitlement."""
    kind: Literal["block_quota"]
    starts_on: date
    ends_on: date | None = None
    repeat: bool = False
    metric: Literal["duties", "session_units", "scheduled_hours", "reserved_hours", "workload_credit", "training_credit", "lead_turns"]
    # range: at least `amount` and at most `maximum_amount` (one rule; checked as a minimum and a maximum).
    comparison: Literal["maximum", "minimum", "target", "range"]
    amount: float = Field(ge=0, le=100000)
    maximum_amount: float | None = Field(default=None, ge=0, le=100000)
    # An optional advisory aim within the limits ("at least 6, at most 8, aim for 7"): scored, never a constraint.
    target_amount: float | None = Field(default=None, ge=0, le=100000)
    target_strength: Strength | None = None  # empty: the rule's strength
    assignment_type_ids: list[ID] = Field(default_factory=list, max_length=32)
    participation: list[Literal["staff", "trainee", "supervisor"]] = Field(default_factory=list, max_length=3)
    overtime: Literal["include", "exclude", "only"] = "include"
    window_days: int | None = Field(default=None, ge=1, le=366, strict=True)
    # rolling: every run of window_days in a row; fixed: back-to-back blocks of window_days from starts_on;
    # calendar_week / calendar_month: each week (from the department's first weekday) or month, cut at the rule's dates.
    window_alignment: Literal["rolling", "fixed", "calendar_week", "calendar_month"] = "rolling"
    window_anchor_on: date | None = None
    prorate_partial_windows: bool = False
    prorate_department_days: bool = False
    reference_days: int = Field(default=28, ge=1, le=366, strict=True)
    rounding: Literal['down', 'nearest', 'up'] = 'nearest'

    @model_validator(mode="after")
    def bounded_meaning(self):
        length = (self.ends_on - self.starts_on).days if self.ends_on else None
        if not self.repeat and (length is None or not 0 < length <= 366):
            raise ValueError("A quota block must be an explicit 1–366-day interval")
        if self.repeat and (length is not None and length <= 0 or self.prorate_department_days or not (
                self.window_alignment in ('fixed', 'calendar_week', 'calendar_month') or self.window_days)):
            raise ValueError('Repeating quotas need a counting window and valid dates, without department-day proration')
        if self.window_days and length is not None and self.window_days > length and self.window_alignment == 'rolling':
            raise ValueError('The effective quota period must contain a complete rolling window')
        if (self.window_anchor_on is not None or self.prorate_partial_windows) and self.window_alignment != 'fixed':
            raise ValueError('An anchor and partial-window proration belong to fixed counting periods')
        if self.prorate_partial_windows and self.prorate_department_days:
            raise ValueError('Choose partial-window or department-day proration')
        if self.window_alignment == "fixed" and not self.window_days:
            raise ValueError("Repeating blocks need a block length in days")
        if self.window_alignment in ("calendar_week", "calendar_month") and self.window_days:
            raise ValueError("Calendar weeks and months have their own length; leave the day count empty")
        if self.prorate_department_days and (self.comparison != 'target' or self.window_days):
            raise ValueError('Department-day proration is a rota target, separate from rolling or mandatory limits')
        if (self.comparison == "range") != (self.maximum_amount is not None):
            raise ValueError("A range needs both numbers (at least and at most); other quotas take one")
        if self.maximum_amount is not None and self.maximum_amount < self.amount:
            raise ValueError("The most cannot be below the least")
        if self.target_amount is not None:
            if self.comparison == 'target':
                raise ValueError('A target rule is already a target; add a target to a minimum, maximum or range')
            low = self.amount if self.comparison in ('minimum', 'range') else 0
            high = self.maximum_amount if self.comparison == 'range' else self.amount if self.comparison == 'maximum' else None
            if self.target_amount < low or high is not None and self.target_amount > high:
                raise ValueError('The target must lie within the minimum and maximum')
        elif self.target_strength is not None:
            raise ValueError('A target strength needs a target')
        if self.metric in ("duties", "lead_turns"):
            if self.comparison != 'target' and not all(float(x or 0).is_integer() for x in (self.amount, self.maximum_amount)):
                raise ValueError("Duty quota bounds require a whole-number amount")
            if self.comparison == 'target':
                self.amount = quota_target_amount(self.metric, self.amount)
            self.target_amount = quota_target_amount(self.metric, self.target_amount)
        for values in (self.assignment_type_ids, self.participation):
            if len(values) != len(set(values)):
                raise ValueError("Quota filter values must be unique")
        return self


def _setting_errors(errors, prefix=()):
    if errors:
        raise ValidationError.from_exception_data('Rule settings', [
            {'type': PydanticCustomError(code, message), 'loc': (*prefix, *location), 'input': None}
            for location, code, message in errors])


def _prefix_setting_error(error, prefix):
    _setting_errors([(e['loc'], e['type'], e['msg']) for e in error.errors(include_input=False)], prefix)


def _credit_pair_errors(conditions, others):
    if (conditions is not None) != bool(others):
        field = 'credit_conditions' if conditions is None else 'credit_minimum_others'
        return [((field,), 'value_error', 'Conditional credit needs both who counts conditionally and the number of others')]
    return []


def _share_pair_errors(distribution, within, target_share):
    errors = []
    if (distribution == 'share') != (within is not None):
        errors.append((('within',), 'value_error', 'A share goal needs the conditions for its share; other goals do not'))
    if distribution != 'share' and target_share != 0.5:
        errors.append((('target_share',), 'value_error', 'A target share belongs to a share goal'))
    return errors


class MinimumCover(Schema):
    kind: Literal["minimum_cover"]
    aggregation: Literal['assignment', 'service_group'] = 'assignment'
    minimum: int = Field(ge=1, le=100)
    when_person_ids: list[ID] = Field(default_factory=list, max_length=100)
    credit_conditions: Conditions | None = None
    credit_minimum_others: int = Field(default=0, ge=0, le=100, strict=True)
    selection_order: list[Conditions] = Field(default_factory=list, max_length=20)
    selection_period: Literal['duty', 'week'] = 'duty'

    @model_validator(mode='after')
    def conditional_credit(self):
        _setting_errors(_credit_pair_errors(self.credit_conditions, self.credit_minimum_others))
        only_fields(self.credit_conditions, LEAD_FIELDS, 'Conditional credit')
        if self.credit_conditions is not None:
            check_count_bucket_budget(self.credit_conditions.model_dump(), 'Conditional credit')
        if not self.selection_order and self.selection_period != 'duty':
            raise ValueError('A selection period needs ordered person conditions')
        for conditions in self.selection_order:
            only_fields(conditions, LEAD_FIELDS, 'Staffing selection')
            check_count_bucket_budget(conditions.model_dump(), 'Staffing selection')
        return self

    @field_validator('when_person_ids')
    @classmethod
    def unique_people(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Select each triggering employee once')
        return values


class AllocationPreference(Schema):
    kind: Literal["allocation_preference"]
    priority: int = Field(ge=-100, le=MAX_SCORE_INPUT, strict=True)
    session_priority: int = Field(default=0, ge=-100, le=MAX_SCORE_INPUT, strict=True)
    overtime_priority: int = Field(default=0, ge=-100, le=MAX_SCORE_INPUT, strict=True)
    supervisor_relationship: Literal['any', 'mentor'] = 'any'
    supervisor_ids: list[ID] = Field(default_factory=list, max_length=100)
    balance_by: Literal['none', 'duties', 'supervisor', 'supervisor_group', 'service_mode'] = 'none'
    balance_weight: float = Field(default=1, gt=0, le=MAX_SCORE_INPUT, allow_inf_nan=False)
    balance_window: Literal['week', 'rolling', 'rota'] = 'week'
    balance_window_days: int = Field(default=28, ge=1, le=31, strict=True)
    balance_target_mode: Literal['physical', 'virtual'] = 'virtual'
    balance_target_share: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode='after')
    def selected_supervisor_group(self):
        if self.balance_by == 'supervisor_group' and not self.supervisor_ids:
            raise ValueError('Select supervisors for grouped exposure balancing')
        return self

    @field_validator('supervisor_ids')
    @classmethod
    def unique_supervisors(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Select each supervisor once')
        return values


class MaximumParticipants(Schema):
    kind: Literal["maximum_participants"]
    aggregation: Literal["assignment", "service"] = "assignment"
    maximum: int = Field(ge=0, le=100, strict=True)
    overflow_maximum: int = Field(default=0, ge=0, le=100, strict=True)
    overflow_category_ids: list[ID] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def explicit_overflow(self):
        if bool(self.overflow_maximum) != bool(self.overflow_category_ids):
            raise ValueError('Permitted overflow requires both extra places and selected ranks')
        if self.maximum + self.overflow_maximum > 100:
            raise ValueError('Normal and extra capacity together cannot exceed 100')
        if len(self.overflow_category_ids) != len(set(self.overflow_category_ids)):
            raise ValueError('Select each overflow rank once')
        return self


class SupervisionLimit(Schema):
    kind: Literal['supervision_limit']
    mode: Literal['allowed_ranks', 'capacity']
    trainee_category_ids: list[ID] = Field(default_factory=list, max_length=100)
    maximum: int | None = Field(default=None, ge=0, le=100, strict=True)
    overflow_maximum: int = Field(default=0, ge=0, le=100, strict=True)
    overflow_category_ids: list[ID] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def defined_limit(self):
        if len(self.trainee_category_ids) != len(set(self.trainee_category_ids)):
            raise ValueError('Select each trainee rank once')
        if self.mode == 'allowed_ranks' and (not self.trainee_category_ids or self.maximum is not None):
            raise ValueError('Allowed ranks require at least one rank and no capacity value')
        if self.mode == 'capacity' and self.maximum is None:
            raise ValueError('A supervision capacity needs a maximum')
        if self.mode == 'allowed_ranks' and (self.overflow_maximum or self.overflow_category_ids):
            raise ValueError('Extra places apply only to supervision capacity')
        if self.mode == 'capacity':
            MaximumParticipants(kind='maximum_participants', maximum=self.maximum,
                overflow_maximum=self.overflow_maximum, overflow_category_ids=self.overflow_category_ids)
        return self


class SpreadEvenly(Schema):
    """A bounded distribution rule; supervision is the first supported holder kind."""
    kind: Literal['spread_evenly']
    holder: Literal['supervisors'] = 'supervisors'
    within: Literal['session'] = 'session'
    mode: Literal['levelled', 'maximum_gap'] = 'levelled'
    maximum_gap: int = Field(default=1, ge=0, le=100, strict=True)

    @model_validator(mode='after')
    def supported_scope(self):
        if self.mode == 'levelled' and self.maximum_gap > 1:
            raise ValueError('Levelled spread uses a maximum gap of 1 or less')
        return self


class AllocationOvertime(Schema):
    kind: Literal["allocation_overtime"]
    required_policy_ids: list[ID] = Field(default_factory=list, max_length=100)
    volunteer_required: bool = Field(default=False, strict=True)

    @model_validator(mode='before')
    @classmethod
    def existing_offers(cls, data, info: ValidationInfo):
        if isinstance(data, dict) and 'volunteer_required' not in data and (info.context or {}).get('historical'):
            return {**data, 'volunteer_required': False}
        return data

    @field_validator('required_policy_ids')
    @classmethod
    def unique_dependencies(cls, values):
        if len(values) != len(set(values)):
            raise ValueError('Select each required overtime cap once')
        return values


class ShareAdjustment(Schema):
    """Relative available-day share, or a signed adjustment in the goal's own unit."""
    mode: Literal['percent', 'amount'] = 'percent'
    value: float = Field(default=100.0, ge=-MAX_SCORE_INPUT, le=MAX_SCORE_INPUT)

    @model_validator(mode='before')
    @classmethod
    def default_amount(cls, data):
        if isinstance(data, dict) and data.get('mode') == 'amount' and 'value' not in data:
            return {**data, 'value': 0}
        return data

    @model_validator(mode='after')
    def nonnegative_percent(self):
        if self.mode == 'percent' and self.value < 0:
            raise ValueError('A share percentage cannot be negative')
        return self


BALANCE_SHARE_OFFERED_METRIC = 'BALANCE_SHARE_OFFERED_METRIC'


def _balance_share_metric(metric, share_by):
    if share_by == 'offered' and metric not in ('overtime_duties', 'overtime_hours'):
        raise PydanticCustomError(BALANCE_SHARE_OFFERED_METRIC,
                                  'Share by offered is only available for overtime duties and overtime hours')
    if share_by == 'supervisable_sessions' and metric != 'supervision':
        raise ValueError('Supervisable sessions are a share basis for supervision only')


class BalanceGoal(Schema):
    """Count WHAT for WHO (the scope's person conditions) over PERIOD aiming for AIM (advisory, history-aware).
    The fairness ledger supplies the history."""
    kind: Literal['balance_goal']
    comparison: Literal['within_group', 'pooled'] = Field(default='within_group',
        description='Compare within each Who/Where group; explicitly choose pooled to combine compatible groups.')
    distribution: Literal['people', 'share', 'period_spread', 'pair_limit'] = 'people'
    weight: float = Field(default=1, gt=0, le=100, allow_inf_nan=False)
    optimize_greedy: bool = False
    within: Conditions | None = None
    target_share: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    distribution_period: Literal['week', 'rota'] = 'week'
    metric: Literal['duties', 'hours', 'session_units', 'weekends', 'overtime_duties', 'overtime_hours', 'lead_turns', 'supervision', 'pair_exposure', 'workload']
    # Only duties of these types, or at these clinics (one or the other; empty: every duty).
    assignment_type_ids: list[ID] = Field(default_factory=list, max_length=32)
    service_ids: list[ID] = Field(default_factory=list, max_length=32)
    # Pair exposure: who the partners are (e.g. consultants), and whether sharing a session or a named supervision counts.
    partners: Conditions | None = Field(default=None, json_schema_extra={'x-applies-to': {'metric': ['pair_exposure']}})
    pairing: Literal['present', 'supervised'] = Field(default='present', json_schema_extra={'x-applies-to': {'metric': ['pair_exposure']}})
    weekend_start: int = Field(default=4, ge=0, le=6, json_schema_extra={'x-applies-to': {'metric': ['weekends']}})  # weekends: first day (Monday=0) and length, as consecutive weekends
    weekend_days: int = Field(default=2, ge=1, le=3, json_schema_extra={'x-applies-to': {'metric': ['weekends']}})
    # Calendar periods are evaluated separately; rolling windows end at the exclusive rota end.
    period: Literal['rota', 'rolling', 'month', 'quarter', 'year', 'block', 'custom', 'since', 'all_history'] = Field(
        default=DEFAULT_FAIR_SHARE_WINDOW['period'], json_schema_extra={'x-default-window': DEFAULT_FAIR_SHARE_WINDOW})
    period_days: int | None = Field(default=None, ge=1, le=366, strict=True)
    period_months: int | None = Field(default=None, ge=1, le=12, strict=True, title='Calendar months back',
        description='Subtract calendar months from the exclusive rota end, clamping month ends; choose days or months.',
        json_schema_extra={'x-applies-to': {'period': ['rolling']}})
    starts_on: date | None = None
    ends_on: date | None = None
    # equal_share: pro rata to days available in the department; target: an amount each (or per person);
    # min_per_pair: at least `amount` with each partner; max_min: lift whoever is furthest below their share.
    aim: Literal['equal_share', 'target', 'min_per_pair', 'max_min', 'workload_spread'] = 'equal_share'
    amount: float | None = Field(default=None, ge=0, le=100000)
    person_amounts: dict[ID, float] = Field(default_factory=dict, max_length=500)
    share_by: BalanceShareBy = Field(default='available_days',
        json_schema_extra={'x-default-by-metric': {'supervision': 'supervisable_sessions'},
            'x-choice-metrics': {'offered': ['overtime_duties', 'overtime_hours'], 'supervisable_sessions': ['supervision']}})
    share_adjustment: ShareAdjustment = Field(default_factory=ShareAdjustment)
    tolerance_percent: float = Field(default=10, ge=0, le=MAX_SCORE_INPUT)
    # None preserves the metric's default: four hours for hours, one unit otherwise.
    tolerance_units: float | None = Field(default=None, ge=0, le=MAX_SCORE_INPUT)

    @model_validator(mode='before')
    @classmethod
    def measure_defaults(cls, value, *, window=None):
        if isinstance(value, dict):
            value = dict(value)
            if 'period' not in value:
                default = {'period': 'rota'} if value.get('metric') == 'workload' else (window or DEFAULT_FAIR_SHARE_WINDOW)
                value['period'] = default['period']
                if value.get('period_days') is None and value.get('period_months') is None:
                    value.update({key: amount for key, amount in default.items() if key != 'period'})
            value.setdefault('share_by', 'supervisable_sessions' if value.get('metric') == 'supervision' else 'available_days')
        return value

    @model_validator(mode='after')
    def coherent(self, info: ValidationInfo):
        _balance_share_metric(self.metric, self.share_by)
        if (self.metric == 'workload') != (self.aim == 'workload_spread'):
            raise ValueError('Workload uses the workload spread aim, and only workload does')
        if self.metric == 'workload' and self.period != 'rota':
            raise ValueError('Workload spread compares this rota with its cover budget')
        if self.aim in ('target', 'min_per_pair') and self.share_adjustment != ShareAdjustment():
            raise ValueError('Share adjustments apply to shared totals; edit the target or pair minimum directly')
        if self.aim == 'min_per_pair' and (self.tolerance_percent != 10 or self.tolerance_units is not None):
            raise ValueError('A pair minimum counts its full deficit; tolerance applies to per-person goals')
        pair = self.metric == 'pair_exposure'
        if pair != (self.aim == 'min_per_pair') and self.distribution != 'pair_limit':
            raise ValueError('Pair exposure aims for a minimum per pair, and only pair exposure does')
        if self.distribution == 'pair_limit' and (not pair or self.amount is None or self.aim != 'target'):
            raise ValueError('Pair limits need pair exposure, a target aim and an amount')
        if self.distribution in ('share', 'period_spread') and self.metric != 'duties':
            raise ValueError('Share and period spread count duties')
        _setting_errors(_share_pair_errors(self.distribution, self.within, self.target_share))
        if self.distribution not in ('period_spread', 'pair_limit') and self.distribution_period != 'week':
            raise ValueError('A distribution period belongs to period spread or pair limits')
        if self.distribution == 'period_spread' and self.distribution_period != 'week':
            raise ValueError('Period spread compares calendar weeks')
        if self.distribution in ('share', 'period_spread') and self.aim != 'equal_share':
            raise ValueError('Share and period spread use an equal-share aim')
        only_fields(self.within, SCOPE_FIELDS, 'Share conditions')
        if self.within is not None:
            check_count_bucket_budget(self.within.model_dump(), 'Share conditions')
        if not (info.context or {}).get('stored_goal') and any(
                Decimal(str(value)).as_tuple().exponent < -4 for value in (self.weight, self.target_share)):
            raise ValueError('Use at most four decimal places for fairness weights and shares')
        if pair and not self.partners:
            raise ValueError('Choose who the partners are')
        only_fields(self.partners, LEAD_FIELDS, 'Partners')
        if self.assignment_type_ids and self.service_ids:
            raise ValueError('Count duty types or clinics, not both')
        for values in (self.assignment_type_ids, self.service_ids):
            if len(values) != len(set(values)):
                raise ValueError('Select each duty type and clinic once')
        if self.aim in ('target', 'min_per_pair') and self.amount is None and not self.person_amounts:
            raise ValueError('Give the amount to aim for')
        windows = int(self.period_days is not None) + int(self.period_months is not None)
        if windows != int(self.period == 'rolling'):
            raise ValueError('A rolling period needs either days or calendar months, and only a rolling period')
        if self.period == 'since':
            if self.starts_on is None or self.ends_on is not None:
                raise ValueError('Since a date needs a start date and no end date')
        elif (self.period == 'custom') != (self.starts_on is not None and self.ends_on is not None) or (
                self.period != 'custom' and (self.starts_on is not None or self.ends_on is not None)):
            raise ValueError('Custom periods need both dates; since needs only a start; other periods need neither')
        if self.period == 'custom' and not 0 < (self.ends_on - self.starts_on).days <= 732:
            raise ValueError('A custom period is 1 day to 2 years')
        if any(v < 0 or v > 100000 for v in self.person_amounts.values()):
            raise ValueError('Per-person amounts are 0 to 100000')
        return self


BalanceGoalMeasure = BalanceGoal.model_fields['metric'].annotation
BalanceGoalPeriodKind = BalanceGoal.model_fields['period'].annotation


class BalanceGoalPeriod(Schema):
    """Resolved BalanceGoal window, retaining its own period units and dates."""
    kind: BalanceGoalPeriodKind
    days: int | None = Field(default=None, ge=1, le=366, strict=True)
    months: int | None = Field(default=None, ge=1, le=12, strict=True)
    starts_on: date | None = None
    ends_on: date | None = None


# Shape rules (advisory only): how work is laid out over days. Days are local calendar days a duty starts on.
class RunLength(Schema):
    """Consecutive working days: at least `minimum` together (no isolated days), at most `maximum`."""
    kind: Literal['run_length']
    minimum: int | None = Field(default=None, ge=2, le=7, strict=True)
    maximum: int | None = Field(default=None, ge=1, le=31, strict=True)

    @model_validator(mode='after')
    def some_bound(self):
        if self.minimum is None and self.maximum is None or self.minimum and self.maximum and self.maximum < self.minimum:
            raise ValueError('Set a shortest and/or longest run, with the longest at least the shortest')
        return self


class DaysOffRun(Schema):
    """Days off come together: a break between working days lasts at least `minimum` days."""
    kind: Literal['days_off_run']
    minimum: int = Field(default=2, ge=2, le=7, strict=True)


class WeekendShape(Schema):
    """complete: work every day of a weekend or none of it. identical_types: the same duty types on each weekend day worked."""
    kind: Literal['weekend_shape']
    shape: Literal['complete', 'identical_types'] = 'complete'
    weekend_start: int = Field(default=4, ge=0, le=6, strict=True)
    weekend_days: int = Field(default=2, ge=2, le=3, strict=True)


class ShiftSuccession(Schema):
    """forward: the next day's duty is not earlier in `type_order` (early → late → night).
    avoid: a `from` duty is not followed by a `to` duty within `within_days` days.
    rest_after_series: after `series_length`+ days in a row of `from` duties, `rest_days` days off."""
    kind: Literal['shift_succession']
    mode: Literal['forward', 'avoid', 'rest_after_series']
    type_order: list[ID] = Field(default_factory=list, max_length=8)
    from_type_ids: list[ID] = Field(default_factory=list, max_length=32)
    to_type_ids: list[ID] = Field(default_factory=list, max_length=32)
    within_days: int = Field(default=1, ge=1, le=3, strict=True)
    series_length: int = Field(default=1, ge=1, le=7, strict=True)
    rest_days: int = Field(default=2, ge=1, le=3, strict=True)

    @model_validator(mode='after')
    def configured(self):
        lists = (self.type_order, self.from_type_ids, self.to_type_ids)
        if any(len(v) != len(set(v)) for v in lists):
            raise ValueError('Select each duty type once')
        if self.mode == 'forward' and len(self.type_order) < 2:
            raise ValueError('Order at least two duty types, earliest first')
        if self.mode == 'avoid' and not (self.from_type_ids and self.to_type_ids):
            raise ValueError('Choose the first duty types and the ones to avoid after them')
        if self.mode == 'rest_after_series' and not self.from_type_ids:
            raise ValueError('Choose the duty types of the series')
        return self


class TogetherApart(Schema):
    """Named people prefer to work the same days (or sessions), or not to. Set by a coordinator, activated by an approver."""
    kind: Literal['together_apart']
    mode: Literal['together', 'apart']
    person_ids: list[ID] = Field(min_length=2, max_length=10)
    level: Literal['day', 'session'] = 'day'

    @field_validator('person_ids')
    @classmethod
    def unique_people(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Select each person once')
        return value


SHAPE_RULES = (RunLength, DaysOffRun, WeekendShape, ShiftSuccession, TogetherApart)
SHAPE_KINDS = frozenset({'run_length', 'days_off_run', 'weekend_shape', 'shift_succession', 'together_apart'})

# The rule catalogue: one rule per catalogue entry (packages/contracts/rule_catalogue.py),
# its lines saying who, where, when and the numbers. packages/domain/rule_lines.py reads it as the old kinds above.
Level = Literal['mandatory', 'critical', 'high', 'medium', 'low', 'off']


class LineNumber(Schema):
    """One number of a line and how much it counts: mandatory (a constraint) or a preference's strength.
    `apply` (a rule without a number) has no value. Blank inherits: a number a line
    leaves out, or a level left null, comes from the broader line that sets it; any other number without a value is an
    explicit None (it removes the broader line's number for this line's people)."""
    value: float | None = Field(default=None, ge=-100000, le=MAX_SCORE_INPUT, allow_inf_nan=False)
    level: Level | None = 'mandatory'


WorkloadMeasure = BlockQuota.model_fields['metric'].annotation
PRIMITIVE_MODELS = (Restriction, MaxHours, MaxDuties, MinRest, ForbiddenTransition, ConsecutiveDays,
                    ConsecutiveWeekends, TravelBuffer, BlockQuota, MinimumCover, MaximumParticipants,
                    SupervisionLimit, SpreadEvenly, TeamLeadRequirement, AllocationPreference, AllocationOvertime, RunLength, DaysOffRun,
                    WeekendShape, ShiftSuccession, TogetherApart, BalanceGoal)
PrimitiveKind = Literal[tuple(get_args(model.model_fields['kind'].annotation)[0] for model in PRIMITIVE_MODELS)]


class WorkloadPeriod(Schema):
    kind: Literal['day', 'calendar_week', 'calendar_month', 'rolling', 'fixed', 'block']
    days: int | None = Field(default=None, ge=1, le=366, strict=True)
    starts_on: date | None = None
    ends_on: date | None = None
    anchor_on: date | None = None
    prorate_partial_windows: bool = False
    repeat: bool = False

    @model_validator(mode='after')
    def configured(self):
        if (self.kind in ('rolling', 'fixed')) != (self.days is not None):
            raise ValueError('Only rolling/fixed periods take a required number of days')
        if self.starts_on and self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError('The quota period must end after it starts')
        if (self.anchor_on is not None or self.prorate_partial_windows) and self.kind != 'fixed':
            raise ValueError('An anchor and partial-window proration belong to a fixed period')
        if self.repeat and self.kind in ('day', 'block'):
            raise ValueError('Choose a repeating counting period')
        return self


class RuleSourceReference(Schema):
    policy_id: ID
    revision: int = Field(ge=1, strict=True)
    line_id: ID | None = None
    slot: str | None = Field(default=None, max_length=100)


class RuleSourceBinding(RuleSourceReference):
    """Immutable identity/slot metadata; clinical values stay in the destination line."""
    number_key: Literal['min', 'max', 'target', 'priority', 'session_priority', 'apply']
    primitive_kind: PrimitiveKind
    precedence_partition: str = Field(min_length=1, max_length=200)
    source_order: int = Field(default=0, ge=0, strict=True)
    exception_allowed: bool = False
    catalogue_id: Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')] | None = None
    catalogue_version: Literal[1, 2] = 1
    line_index: int | None = Field(default=None, ge=0, strict=True)


class RuleLine(Schema):
    """A line of a catalogue rule: who (person fields; empty: everyone), where (clinics, places, duty types; empty:
    everywhere), days and hours, its numbers, its own settings (by old rule kind, over the rule's), dates (end not
    included), the academic calendar entry it protects, and a note."""
    id: ID
    who: Conditions | None = Field(default=None, json_schema_extra={"bulk": True})
    where: Conditions | None = Field(default=None, json_schema_extra={"bulk": True})
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7, json_schema_extra={"bulk": True})
    start_time: Clock = Field(default='00:00', json_schema_extra={"bulk": True})
    end_time: Clock = Field(default='24:00', json_schema_extra={"bulk": True})
    numbers: dict[str, LineNumber] = Field(min_length=1, max_length=6, json_schema_extra={"bulk": True})
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=8, json_schema_extra={"bulk": True})
    starts_on: date | None = Field(default=None, json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})
    calendar_entry_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    note: str = Field(default='', max_length=500, json_schema_extra={"bulk": True})
    source_bindings: dict[str, RuleSourceBinding] = Field(default_factory=dict, max_length=6)
    source_default: 'RuleLine | None' = None
    # Editor inputs translate once; persisted snapshots contain only canonical primitive settings.
    mode: Literal['count', 'no_duties'] | None = Field(default=None, exclude=True)
    measure: WorkloadMeasure | None = Field(default=None, exclude=True)
    period: WorkloadPeriod | None = Field(default=None, exclude=True)

    @model_validator(mode='before')
    @classmethod
    def typed_workload(cls, data):
        if isinstance(data, dict) and data.get('mode') is not None:
            restriction = set(data.get('numbers') or {}) == {'apply'}
            if (data['mode'] == 'no_duties') != restriction:
                raise ValueError('The workload mode must match its count or restriction numbers')
        if isinstance(data, dict) and (data.get('measure') is not None or data.get('period') is not None):
            if data.get('measure') is None or data.get('period') is None:
                raise ValueError('A workload count needs both measure and period')
            from .rule_catalogue import workload_settings
            period = WorkloadPeriod.model_validate(data['period']).model_dump(mode='json', exclude_unset=True)
            bindings = {k: v.model_dump() if isinstance(v, RuleSourceBinding) else v
                        for k, v in (data.get('source_bindings') or {}).items()}
            return {**data, 'settings': workload_settings(data['measure'], period, data.get('settings'),
                                                        data.get('numbers') or {}, bindings)}
        return data

    @model_validator(mode='after')
    def coherent(self):
        only_fields(self.who, LEAD_FIELDS, 'Who')
        only_fields(self.where, SESSION_SCOPE_FIELDS, 'Where')
        check_count_bucket_budget({'match': 'all', 'conditions': [t.model_dump() for t in (self.who, self.where) if t]},
                                  f'Line {self.id}')
        if len(set(self.weekdays)) != len(self.weekdays) or any(d not in range(7) for d in self.weekdays):
            raise ValueError('Use unique weekdays Monday=0 through Sunday=6')
        if self.starts_on and self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError('A line must end after it starts')
        if set(self.source_bindings) - set(self.numbers):
            raise ValueError('A source binding needs its destination number')
        if any(key != binding.number_key for key, binding in self.source_bindings.items()):
            raise ValueError('A source binding must name its destination number')
        if self.source_default is not None and self.source_default.source_default is not None:
            raise ValueError('A source default cannot contain another source default')
        return self


def _canonical_tree(tree):
    from packages.domain.conditions import absolute_values
    if tree is None:
        return None
    tree = tree if isinstance(tree, dict) else tree.model_dump(mode='json')
    lines = []
    for c in tree['conditions']:
        if 'conditions' in c:
            lines.append(_canonical_tree(c))
            continue
        values, op = absolute_values(c), c.get('op', 'any')
        choices = c.get('value') is None or (values is not None and
            (condition_field_definition(c['field']) or {}).get('value_kind') in
            {'people', 'single_choice', 'multiple_choice', 'tags'})
        if choices:
            op = {'is': 'any', 'is_not': 'none'}.get(op, op)
        lines.append([c['field'], op, sorted({v.lower() for v in values} if c['field'] == 'tags' else set(values)) if choices else []]
                     + ([['value', _canonical_operand(c['value'])]] if not choices else [])
                     + ([['entry_names', sorted(set(c['entry_names']))]] if c.get('entry_names') else [])
                     + (['each'] if c.get('count') == 'each' else []))
    # No condition is no tree (everyone, everywhere); one condition reads the same under "all" and "any".
    each = tree.get('count') == 'each'
    return (['all' if len(lines) == 1 else tree.get('match', 'all'), lines if each else sorted(lines, key=str)]
            + (['each'] if each else [])) if lines else None


def _canonical_operand(value):
    if isinstance(value, dict):
        return _canonical_operand(value['value']) if value.get('kind') == 'absolute' else sorted(value.items())
    if isinstance(value, (list, tuple)):
        return [_canonical_operand(item) for item in value]
    return value


def line_key(entry_id, line):
    """"The same line" of a catalogue rule: the entry, who, where, days and hours (dates and numbers aside)."""
    import json
    d = line if isinstance(line, dict) else line.model_dump(mode='json')
    return json.dumps([entry_id, _canonical_tree(d.get('who')), _canonical_tree(d.get('where')), sorted(d.get('weekdays') or range(7)),
                       d.get('start_time') or '00:00', d.get('end_time') or '24:00'])


@lru_cache(maxsize=None)
def _primitive_adapter(kind, name):
    from .rule_catalogue import _MODELS
    field = _MODELS[kind].model_fields[name]
    annotation = Annotated[field.annotation, *field.metadata] if field.metadata else field.annotation
    if isinstance(field.annotation, type) and issubclass(field.annotation, BaseModel):
        return TypeAdapter(annotation)
    return TypeAdapter(annotation, config=ConfigDict(allow_inf_nan=False))


def _validate_primitive_value(kind, name, value):
    return _primitive_adapter(kind, name).validate_python(value)


def validate_catalogue_settings(entry, settings, prefix=()):
    """Primitive schemas are the authority for editable field names, types and limits."""
    for kind, values in settings.items():
        if kind not in entry['line_settings']:
            raise ValueError(f"{entry['name']} has no settings for {kind}")
        fixed = {}
        for slots in entry['numbers'].values():
            for slot in slots:
                if slot['kind'] == kind:
                    for name, value in slot['fixed'].items():
                        fixed.setdefault(name, []).append(value)
        allowed = set(entry['line_settings'][kind]) | set(fixed)
        if set(values) - allowed:
            raise ValueError(f"{entry['name']} has no line settings for {', '.join(sorted(set(values) - allowed))}")
        for name, value in values.items():
            if name in fixed and value not in fixed[name]:
                raise ValueError(f"{entry['name']} cannot change the {name} of its {kind} slot")
            try:
                values[name] = _validate_primitive_value(kind, name, value)
            except ValidationError as error:
                _prefix_setting_error(error, (*prefix, kind, name))


def _catalogue_pair_errors(rule, entry):
    """Check resolved pairs, allowing a line to complete or clear inherited settings."""
    from .rule_catalogue import slot_for_number
    guards = {
        'minimum_cover': lambda values: _credit_pair_errors(values.get('credit_conditions'), values.get('credit_minimum_others', 0)),
        'balance_goal': lambda values: _share_pair_errors(values.get('distribution', 'people'), values.get('within'), values.get('target_share', 0.5)),
    }
    errors = []
    for kind, guard in guards.items():
        if kind not in entry['line_settings']:
            continue
        base = {**entry['default_settings'].get(kind, {}), **rule.settings.get(kind, {})}
        relevant = [(n, line) for n, line in enumerate(rule.lines)
                    if kind in line.settings or any(slot_for_number(entry, key, {'settings': rule.settings},
                        {'settings': line.settings, 'source_bindings': {k: v.model_dump() for k, v in line.source_bindings.items()}},
                        line.numbers)['kind'] == kind for key in line.numbers)]
        for n, line in relevant or [(None, None)]:
            own = line.settings.get(kind, {}) if line else {}
            prefix = ('lines', n, 'settings', kind) if own else ('settings', kind)
            errors.extend(((*prefix, *location), code, message) for location, code, message in guard({**base, **own}))
    _setting_errors(errors)


class CatalogueRule(Schema):
    """The rule of one catalogue entry: on or off (off keeps its lines), its settings by old kind, and its lines."""
    kind: Literal['catalogue']
    id: Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')]
    catalogue_version: Literal[1, 2] = 1
    on: bool = True
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=8)
    lines: list[RuleLine] = Field(default_factory=list, max_length=2000)

    @model_validator(mode='after')
    def known(self, info: ValidationInfo):
        from .rule_catalogue import catalogue_entry, entry_kinds, slot_for_number, number_range_errors
        entry = catalogue_entry(self.id, self.catalogue_version)
        if entry is None:
            raise ValueError('Not a rule in the catalogue')
        kinds = set(entry_kinds(entry))
        if set(self.settings) - kinds:
            raise ValueError(f"{entry['name']} has no settings for {', '.join(sorted(set(self.settings) - kinds))}")
        if len({line.id for line in self.lines}) != len(self.lines):
            raise ValueError('Each line needs its own id')
        for n, line in enumerate(self.lines, 1):
            if self.id == 'overtime_offers':
                settings = line.settings.setdefault('allocation_overtime', {})
                settings.setdefault('volunteer_required', self.settings.get('allocation_overtime', {}).get(
                    'volunteer_required', not (info.context or {}).get('historical', False)))
            if line.source_default is not None:
                try:
                    line.source_default = CatalogueRule.model_validate({
                        'kind': 'catalogue', 'id': self.id, 'catalogue_version': self.catalogue_version,
                        'settings': self.settings,
                        'lines': [line.source_default.model_dump(mode='json')],
                    }, context=info.context).lines[0]
                except ValidationError as error:
                    _setting_errors([((e['loc'][2:] if e['loc'][:2] == ('lines', 0) else e['loc']), e['type'], e['msg'])
                                     for e in error.errors(include_input=False)], ('lines', n - 1, 'source_default'))
            if (line.mode is not None or line.measure is not None or line.period is not None) and (self.catalogue_version != 2 or self.id != 'workload_limit'):
                raise ValueError('Measure and period belong to catalogue v2 Workload limit lines')
            if set(line.settings) - kinds:
                raise ValueError(f"Line {n}: {entry['name']} has no settings for {', '.join(sorted(set(line.settings) - kinds))}")
            for key, number in line.numbers.items():
                slots = entry['numbers'].get(key)
                if slots is None:
                    raise ValueError(f"Line {n}: {entry['name']} has no {key.replace('_', ' ')} number")
                if key == 'apply' and number.value is not None:
                    raise ValueError(f'Line {n}: this rule has no number')
                chosen = slot_for_number(entry, key, {'settings': self.settings},
                                         {'settings': line.settings, 'source_bindings': {k: v.model_dump() for k, v in line.source_bindings.items()}},
                                         line.numbers)
                if (number.level not in (None, 'mandatory') and chosen['kind'] in ALWAYS_MANDATORY_KINDS
                        and not chosen['advisory'] and not (info.context or {}).get('historical')):
                    raise ValueError(f'Line {n}: {SAFETY_MANDATORY_REASON}')
                if number.level is not None and (number.level not in entry['levels'] or number.level == 'mandatory' and all(s['advisory'] for s in slots)):
                    raise ValueError(f"Line {n}: the {key.replace('_', ' ')} cannot be {number.level}")
                if self.catalogue_version == 2 and number.level == 'mandatory' and chosen['advisory']:
                    raise ValueError(f"Line {n}: the {key.replace('_', ' ')} here is a preference, not mandatory")
                if self.catalogue_version == 2 and chosen['field'] and number.value is not None:
                    value = int(number.value) if float(number.value).is_integer() else number.value
                    _validate_primitive_value(chosen['kind'], chosen['field'], value)
                if chosen['kind'] == 'block_quota' and chosen['fixed'].get('comparison') == 'target' and number.value is not None:
                    settings = {**entry['default_settings'].get('block_quota', {}), **self.settings.get('block_quota', {}),
                                **line.settings.get('block_quota', {}), **chosen['fixed']}
                    number.value = quota_target_amount(settings.get('metric'), _validate_primitive_value('block_quota', 'amount', number.value))
                if self.catalogue_version == 2 and chosen['kind'] == 'allocation_preference':
                    settings = line.settings.get(chosen['kind'], {})
                    if chosen['field'] in settings and settings[chosen['field']] != number.value:
                        raise ValueError(f"Line {n}: allocation priority settings conflict with the selected number")
                if self.catalogue_version == 2 and line.source_bindings.get(key) and line.source_bindings[key].exception_allowed and chosen['kind'] in NEVER_OVERRIDDEN:
                    raise ValueError('Safety source bindings cannot grant person exceptions')
            if not (info.context or {}).get('historical'):
                _setting_errors([(('numbers', key), 'invalid_number_range', message)
                    for key, message in number_range_errors({key: number.value for key, number in line.numbers.items()})],
                    ('lines', n - 1))
            if line.who is not None and any(slot_for_number(entry, key, {'settings': self.settings},
                                                           {'settings': line.settings, 'source_bindings': {k: v.model_dump() for k, v in line.source_bindings.items()}}, line.numbers)['kind']
                                            in WORKLOAD_COUNT_KINDS for key in line.numbers):
                workload_counts(line.who)
        if self.catalogue_version == 2:
            validate_catalogue_settings(entry, self.settings, ('settings',))
            for n, line in enumerate(self.lines):
                validate_catalogue_settings(entry, line.settings, ('lines', n, 'settings'))
        _catalogue_pair_errors(self, entry)
        if self.id == 'team_leads':
            validate_catalogue_settings(entry, self.settings, ('settings',))
            for n, line in enumerate(self.lines):
                validate_catalogue_settings(entry, line.settings, ('lines', n, 'settings'))
                nodes = [tree.model_dump(mode='json') for tree in (line.who, line.where) if tree]
                while nodes:
                    node = nodes.pop()
                    if node.get('count') == 'each':
                        raise ValueError('Team leads has one role per duty; Count each is not supported')
                    nodes.extend(node.get('conditions') or [])
                bounds = {key: num.value for key, num in line.numbers.items() if num.value is not None}
                TeamLeadRequirement.model_validate({'kind': 'team_lead_requirement',
                    **{'minimum' if key == 'min' else 'maximum': value for key, value in bounds.items()}})
        if self.id == 'fair_share':
            base = {**entry['default_settings'].get('balance_goal', {}), **self.settings.get('balance_goal', {})}
            for line in self.lines or [None]:
                settings = {**base, **(line.settings.get('balance_goal', {}) if line else {})}
                _balance_share_metric(settings.get('metric'), settings.get('share_by'))
        return self


RulePrimitive = Annotated[Union[PRIMITIVE_MODELS + (CatalogueRule,)], Field(discriminator="kind")]
# The number a line of a rule can change (the rule field each kind's line sets).
PER_PERSON_FIELD = {'block_quota': 'amount', 'max_hours': 'hours', 'max_duties': 'maximum', 'max_consecutive_days': 'maximum',
                    'max_consecutive_weekends': 'maximum', 'allocation_preference': 'priority', 'min_rest': 'hours',
                    'minimum_cover': 'minimum', 'maximum_participants': 'maximum', 'supervision_limit': 'maximum',
                    'spread_evenly': 'maximum_gap'}
# Numbers a line may set instead (a rule chooses one with `line_number`): an allocation order ranks sessions, so its
# lines set the session priority. The first is the usual one.
LINE_NUMBER_FIELDS = {kind: [field] for kind, field in PER_PERSON_FIELD.items()} | {'allocation_preference': ['priority', 'session_priority']}


def line_field(definition):
    """The rule setting a line's number changes (None: the rule has no number a line may change)."""
    kind = definition['rule']['kind']
    return definition.get('line_number') or PER_PERSON_FIELD.get(kind)


# Rules checked person by person, so a line can narrow them to one person (with their own number when the rule has one).
PER_PERSON_KINDS = frozenset({'block_quota', 'max_hours', 'max_duties', 'max_consecutive_days', 'max_consecutive_weekends',
                              'allocation_preference', 'restriction', 'allocation_overtime', 'forbidden_transition', 'min_rest'})
# Rules whose lines may also pick a programme, ranks or sessions (clinics, duty types, days, hours) instead of a person:
# a rule for its lines only, each line one narrower copy of the rule. On staffing levels a line's person or ranks are
# who counts (a placement: this fellow attends this clinic); on supervision limits, the supervisors it limits.
LINE_KINDS = PER_PERSON_KINDS | {'minimum_cover', 'maximum_participants', 'supervision_limit', 'spread_evenly'}
# Settings a line may set for itself (a rule for its lines only): a staffing level's line may apply only when named
# people are on duty; an allocation order's line may prefer sessions those supervisors lead; a supervision limit's line
# says which trainee ranks, and how.
LINE_SETTINGS = {'minimum_cover': ['when_person_ids'], 'allocation_preference': ['supervisor_ids'],
                 'supervision_limit': ['mode', 'trainee_category_ids', 'overflow_maximum', 'overflow_category_ids'],
                 'spread_evenly': ['holder', 'within', 'mode'],
                 'block_quota': ['maximum_amount', 'target_amount']}
# Kinds whose every line may set its own settings (a quota line's own at-most and target), not only a rule for its lines.
ANY_LINE_SETTINGS = frozenset({'block_quota'})


def line_rule(rule, row, field):
    """The rule one line checks: the rule's settings, then the line's own settings and number, and a quota line's
    block. The one reading for the rule's validation and for every check (`active_rules`)."""
    out = {**rule, **{k: v for k, v in (row.get('settings') or {}).items() if v is not None}}
    amount = row.get('amount')
    if field and amount is not None:  # whole-number settings stay whole numbers
        out[field] = int(amount) if float(amount).is_integer() and not isinstance(rule.get(field), float) else amount
    if out['kind'] == 'supervision_limit' and out['mode'] == 'allowed_ranks':
        out.update(maximum=None, overflow_maximum=0, overflow_category_ids=[])  # a list of allowed ranks has no capacity
    if out['kind'] == 'block_quota':
        out.update(starts_on=row.get('starts_on') or rule['starts_on'], ends_on=row.get('ends_on') or rule['ends_on'])
    return out


# Hard safety slots always bind. Advisory-only slots are scored separately by the catalogue.
ALWAYS_MANDATORY_KINDS = frozenset({
    'restriction', 'min_rest', 'forbidden_transition', 'supervision_limit',
    'minimum_cover', 'maximum_participants', 'team_lead_requirement',
})
SAFETY_MANDATORY_REASON = 'Safety rules are always mandatory'

# Override protection extends the mandatory set to spread-evenly, which may stay advisory.
NEVER_OVERRIDDEN = ALWAYS_MANDATORY_KINDS | {'spread_evenly'}

# Numeric safety limits are combined when a frozen and live revision describe
# the same obligation.  The direction belongs to the primitive kind, not to
# the field name: ``hours`` is a minimum for rest and a maximum for workload.
SAFETY_STRICTNESS = {
    'min_rest': {'hours': 'max'},
    'minimum_cover': {'minimum': 'max'},
    'maximum_participants': {'maximum': 'min'},
    'supervision_limit': {'maximum': 'min'},
    'team_lead_requirement': {'minimum': 'max', 'maximum': 'min'},
}
SAFETY_KEEP_BOTH = frozenset({'restriction', 'forbidden_transition', 'spread_evenly'})


def safety_direction(kind, field):
    """Return the strictness direction for one numeric safety slot."""
    return SAFETY_STRICTNESS.get(kind, {}).get(field)


def safety_merge_policy(kind, field):
    """Return ``min``/``max`` or the explicit non-numeric ``keep_both`` policy."""
    if kind in SAFETY_KEEP_BOTH or field is None and kind in NEVER_OVERRIDDEN:
        return 'keep_both'
    direction = safety_direction(kind, field)
    if direction is None and kind in NEVER_OVERRIDDEN:
        raise ValueError(f'No strictness direction declared for safety slot {kind}.{field}')
    return direction


def safety_rule(definition):
    """One safety classification for admission, historical projections and every override evaluator."""
    rule = definition.get('rule') or {}
    if rule.get('kind') == 'catalogue':
        from .rule_catalogue import catalogue_entry, slot_for_number
        entry = catalogue_entry(rule.get('id'), rule.get('catalogue_version', 1)) or {}
        if rule.get('catalogue_version', 1) == 2 and entry.get('mixed_safety'):
            return any(slot_for_number(entry, key, rule, line, line.get('numbers') or {})['kind'] in NEVER_OVERRIDDEN
                       for line in rule.get('lines') or [] for key in line.get('numbers') or {})
        return bool(entry.get('safety'))
    return rule.get('kind') in NEVER_OVERRIDDEN


class LineSettings(Schema):
    """A line's own settings (LINE_SETTINGS says which a rule's lines may set); empty: the rule's."""
    when_person_ids: list[ID] | None = Field(default=None, max_length=100)
    supervisor_ids: list[ID] | None = Field(default=None, max_length=100)
    mode: Literal['allowed_ranks', 'capacity'] | None = None
    trainee_category_ids: list[ID] | None = Field(default=None, max_length=100)
    overflow_maximum: int | None = Field(default=None, ge=0, le=100, strict=True)
    overflow_category_ids: list[ID] | None = Field(default=None, max_length=100)
    # A quota line's own at-most (a range) and target; its `amount` is the at-least (or the one number).
    maximum_amount: float | None = Field(default=None, ge=0, le=100000)
    target_amount: float | None = Field(default=None, ge=0, le=100000)

    def chosen(self):
        return {k: v for k, v in self.model_dump().items() if v is not None}


class PersonAmount(Schema):
    """One line of a rule: who (a person, or a programme's members), its own number (empty: the rule's number, or a rule
    without one), and optionally only some dates (end date not included), weekdays, hours, clinics or duty types.
    A block quota's line dates are that line's block. Lines without a person belong to rules for their lines only."""
    person_id: ID | None = None
    programme_id: ID | None = None
    # Ranks (a rule for its lines only): who the line is for, or on a staffing level, who counts.
    category_ids: list[ID] = Field(default_factory=list, max_length=100)
    service_ids: list[ID] = Field(default_factory=list, max_length=32)
    start_time: Clock | None = None
    end_time: Clock | None = None
    amount: float | None = Field(default=None, ge=-100000, le=100000)  # the rule's own bounds apply
    starts_on: date | None = None
    ends_on: date | None = None
    weekdays: list[int] | None = Field(default=None, min_length=1, max_length=7)
    assignment_type_ids: list[ID] = Field(default_factory=list, max_length=32)
    settings: LineSettings | None = None

    @model_validator(mode='after')
    def ordered(self):
        if self.starts_on and self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError('A per-person line must end after it starts')
        if self.weekdays is not None and (len(set(self.weekdays)) != len(self.weekdays) or any(d not in range(7) for d in self.weekdays)):
            raise ValueError('Use unique weekdays Monday=0 through Sunday=6')
        if len(set(self.assignment_type_ids)) != len(self.assignment_type_ids) or len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError('Select each duty type and clinic once')
        if len(set(self.category_ids)) != len(self.category_ids):
            raise ValueError('Select each rank once')
        if (self.start_time is None) != (self.end_time is None):
            raise ValueError('Give a line both a start and an end time, or neither')
        return self

    def who(self):
        return self.person_id, self.programme_id


class PolicyLine(Schema):
    policy_id: ID
    expected_revision: int = Field(ge=1)


class LegacyLeadCheckInput(Schema):
    legacy_effective_on: date | None = None


class LegacyLeadSource(Schema):
    type_id: ID
    type_revision: int = Field(ge=1)
    lead_hash: str
    source_history_id: ID | None = None
    destination_line_id: ID


class LegacyLeadConversion(Schema):
    effective_on: date
    source_manifest_hash: str
    sources: list[LegacyLeadSource] = Field(min_length=1, max_length=2000)


class PolicyDefinition(Schema):
    legacy_lead_conversion: LegacyLeadConversion | None = None
    schema_version: Literal[1] = 1
    scope: Scope = Field(default_factory=Scope)
    severity: Literal["mandatory", "advisory"] = "mandatory"
    # How much an advisory rule weighs; only a Fair share goal may exclude its audience with off.
    strength: Strength | Literal['off'] = 'medium'
    # Compare people across the rule's lines; off keeps each line's fairness goal separate.
    balance_between_people: bool = Field(default=True, strict=True)
    exception_allowed: bool = False
    rule: RulePrimitive
    # Set for a rule that belongs to one rota only (added from that rota); unset for department rules.
    workspace_id: ID | None = None
    # The rule's lines: a listed person gets their own number on their line's dates (a quota line is their block, and
    # replaces the rule's block for them); everyone else the rule matches gets the rule's number. With listed_only the
    # lines are the whole rule, and a line may also be a programme or sessions.
    per_person: list[PersonAmount] = Field(default_factory=list, max_length=2000)
    # Only the listed people, on their lines: the rule's conditions add no one else (a grouped rule's lines are all of it).
    listed_only: bool = False
    # Rules this one replaces for the people it matches (checked on proposal: advisory rules and numeric limits only).
    overrides: list[ID] = Field(default_factory=list, max_length=20)
    # Near-duplicate rules this template was grouped from; they are retired when it is activated. As many as a rule has
    # lines (each grouped rule gives at least one line).
    merged_ids: list[ID] = Field(default_factory=list, max_length=2000)
    # Server-captured immutable defaults on the proposal's source versions; None is a historical proposal.
    seeded_line_ids: list[ID] | None = Field(default=None, max_length=2000)
    # The academic calendar entry this protected-time rule was made from.
    calendar_entry_id: ID | None = None
    # The rule this is a new version of; it is retired when this one is activated.
    replaces_id: ID | None = None
    # Which of the rule's numbers its lines set, when it has several (LINE_NUMBER_FIELDS; empty: the usual one).
    line_number: str | None = None

    @model_validator(mode='before')
    @classmethod
    def no_quota_sets(cls, data):
        # Linked quota sets (bounds and a target as two rules) were replaced by one rule with a target.
        if isinstance(data, dict) and (data.get('authoring_set_id') or data.get('authoring_replaces')):
            raise ValueError('This rule belongs to a linked quota set, which is no longer used: propose one rule with its '
                             'minimum, maximum and target instead, then decline or retire this one')
        return data

    @model_validator(mode='after')
    def template_lines(self):
        check_count_bucket_budget(self.scope.conditions.model_dump() if self.scope.conditions else None, 'Scope line')
        if len(set(self.overrides)) != len(self.overrides):
            raise ValueError('Select each replaced rule once')
        if self.line_number is not None and self.line_number not in LINE_NUMBER_FIELDS.get(self.rule.kind, ()):
            raise ValueError('Lines of this rule cannot set that number')
        if isinstance(self.rule, CatalogueRule) and (self.per_person or self.listed_only or self.overrides or self.workspace_id
                                                     or self.scope != Scope() or self.severity != 'mandatory' or self.calendar_entry_id):
            raise ValueError('A catalogue rule says who, where, when and how much on its lines')
        if not self.per_person:
            if self.listed_only:
                raise ValueError('Add at least one line, or let the rule apply to everyone its conditions match')
            return self
        field = self.line_number or PER_PERSON_FIELD.get(self.rule.kind)
        if self.rule.kind not in (LINE_KINDS if self.listed_only else PER_PERSON_KINDS):
            raise ValueError('Lines apply to limits, quotas, restrictions, overtime, preferences and staffing levels')
        if self.scope.person_id or self.scope.person_ids:
            raise ValueError('A rule with per-person numbers chooses who with conditions, not a list of people')
        base = self.rule.model_dump()
        blocks = set()
        for n, row in enumerate(self.per_person, 1):
            if row.amount is not None and not field:
                raise ValueError(f'Line {n}: this rule has no number to change; leave the number empty')
            if not self.listed_only and not (row.person_id and not row.programme_id and not row.service_ids and not row.category_ids and row.start_time is None):
                raise ValueError(f'Line {n}: a rule for everyone its conditions match takes lines for people only')
            if row.category_ids and (self.scope.category_id or self.scope.category_ids):
                raise ValueError(f'Line {n}: the rule already names its ranks')
            if row.programme_id and self.scope.programme_id:
                raise ValueError(f'Line {n}: the rule already names a programme')
            if row.service_ids and (self.scope.service_id or self.scope.service_ids):
                raise ValueError(f'Line {n}: the rule already names its clinics')
            dates = {'starts_on': row.starts_on or self.rule.starts_on, 'ends_on': row.ends_on or self.rule.ends_on} if self.rule.kind == 'block_quota' else {}
            if dates and (dates['starts_on'] < self.rule.starts_on or self.rule.ends_on is not None
                          and (dates['ends_on'] is None or self.rule.ends_on < dates['ends_on'])):
                raise ValueError(f'Line {n}: its dates must lie within the quota block')
            if dates:
                # A quota line is a block of its own, known by who and dates.
                key = (row.who(), tuple(row.category_ids), tuple(row.service_ids), tuple(row.assignment_type_ids), tuple(row.weekdays or ()),
                       row.start_time, row.end_time, dates['starts_on'], dates['ends_on'])
                if key in blocks:
                    raise ValueError(f'Line {n}: another line is for the same people and dates')
                blocks.add(key)
            own = row.settings.chosen() if row.settings else {}
            allowed = set(LINE_SETTINGS.get(self.rule.kind, ())) if self.listed_only or self.rule.kind in ANY_LINE_SETTINGS else set()
            if not set(own) <= allowed:
                raise ValueError(f"Line {n}: a line of this rule cannot set its own {', '.join(sorted(set(own) - allowed)).replace('_', ' ')}")
            if row.amount is not None and self.rule.kind == 'supervision_limit' and {**base, **own}['mode'] == 'allowed_ranks':
                raise ValueError(f'Line {n}: a list of allowed ranks has no number; leave it empty')
            try:
                validated = type(self.rule).model_validate(line_rule(base, {**row.model_dump(), 'settings': own}, field))
            except ValidationError as error:
                raise ValueError(f"Line {n}: {error.errors()[0]['msg'].removeprefix('Value error, ')}") from None
            if isinstance(validated, BlockQuota):
                if row.amount is not None:
                    row.amount = getattr(validated, field)
                if row.settings and row.settings.target_amount is not None:
                    row.settings.target_amount = validated.target_amount
        return self

    @model_validator(mode="after")
    def target_is_not_a_constraint(self, info: ValidationInfo):
        if self.strength == 'off' and not isinstance(self.rule, BalanceGoal):
            raise ValueError('Only Fair share goals have an off strength')
        # Old snapshots remain readable; the evaluator never honours their safety exception flag. Admission never
        # supplies this context, and all other schema checks still apply to historical definitions.
        if self.exception_allowed and safety_rule({'rule': self.rule.model_dump()}) and not (info.context or {}).get('historical'):
            raise ValueError('Safety rules cannot grant person exceptions')
        if self.rule.kind in WORKLOAD_COUNT_KINDS:
            workload_counts(self.scope.conditions)
        if isinstance(self.rule, MinimumCover) and self.rule.aggregation == 'service_group' and not (self.scope.service_id or self.scope.service_ids or self.scope.chosen_services()):
            raise ValueError('Pooled coverage requires an explicit clinic/service scope')
        context = info.context or {}
        if (self.rule.kind in ALWAYS_MANDATORY_KINDS and self.severity != 'mandatory'
                and not (context.get('historical') or context.get('advisory_catalogue_slot'))):
            raise ValueError(SAFETY_MANDATORY_REASON)
        if isinstance(self.rule, SHAPE_RULES) and (self.severity != 'advisory' or self.exception_allowed):
            raise ValueError('Run, days-off, weekend, succession and together/apart rules are advisory preferences')
        if isinstance(self.rule, (AllocationPreference, AllocationOvertime)) and (self.severity != "advisory" or self.exception_allowed):
            raise ValueError("Allocation settings are advisory and never grant safety exceptions")
        if isinstance(self.rule, BalanceGoal):
            if self.severity != 'advisory' or self.exception_allowed:
                raise ValueError('A balance goal is advisory, with no exceptions')
            nodes, counted = [self.scope.conditions] if self.scope.conditions else [], False
            while nodes:
                node = nodes.pop()
                counted |= node.count == 'each'
                if isinstance(node, Conditions):
                    nodes.extend(node.conditions)
            # Only Count trees need session filters here; legacy goals keep their separate What settings.
            resolved_line = (info.context or {}).get('resolved_catalogue_line', False)
            only_fields(self.scope.conditions, SCOPE_FIELDS if counted or resolved_line or self.rule.distribution != 'people' else SCOPE_FIELDS - SESSION_SCOPE_FIELDS, 'A balance goal')
            if not resolved_line and self.rule.distribution == 'people' and (self.scope.service_id or self.scope.service_ids or self.scope.location_id or self.scope.assignment_type_id
                    or len(self.scope.weekdays) != 7 or self.scope.start_time != '00:00' or self.scope.end_time != '24:00'):
                raise ValueError('Choose the duties a balance goal counts on the goal itself, not in its scope')
        if isinstance(self.rule, BlockQuota) and self.rule.comparison == "target":
            if self.severity != "advisory" or self.exception_allowed:
                raise ValueError("A quota target is advisory, with no exception mechanism; use an explicit minimum or maximum for a constraint")
        if isinstance(self.rule, BlockQuota) and (self.rule.window_days or self.rule.repeat) and self.exception_allowed:
            raise ValueError('Rolling quotas do not support fixed-block exceptions')
        return self


class PolicyInput(EffectiveInput):
    name: Name
    owner: Name
    source: Reason
    definition: PolicyDefinition

    @model_validator(mode="after")
    def quota_within_effective_period(self):
        rule = self.definition.rule
        if isinstance(rule, BlockQuota):
            if self.starts_on > rule.starts_on or (self.ends_on and (
                    rule.ends_on is not None and self.ends_on < rule.ends_on
                    or rule.ends_on is None and not rule.repeat)):
                raise ValueError("The full quota block must lie within the policy effective period")
            if not self.active:
                raise ValueError("A proposed quota must have an active effective definition; its workflow still requires independent activation")
        return self


class ExceptionInput(Schema):
    policy_id: ID
    person_id: ID
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date = Field(json_schema_extra={"bulk": True})
    reason: Reason

    @model_validator(mode="after")
    def dates(self):
        if not 0 < (self.ends_on - self.starts_on).days <= 366:
            raise ValueError("Exception must be bounded to 1–366 days")
        return self


def _utc_instant(value):
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise ValueError("Dated instants must be representable in UTC") from None


class InstantInterval(Schema):
    start: datetime = Field(json_schema_extra={"bulk": True})
    end: datetime = Field(json_schema_extra={"bulk": True})

    @field_validator("start", "end")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Dated instants require an explicit UTC offset")
        return _utc_instant(value)

    @model_validator(mode="after")
    def interval(self):
        if self.end <= self.start:
            raise ValueError("End must follow start; intervals are start-inclusive/end-exclusive")
        return self


class PolicyVersionApproval(Schema):
    approved_by_name: str | None
    approved_at: datetime | None
    reason: str | None


class ExceptionApprovalContext(Schema):
    workspace_id: ID
    workspace_name: str
    publication_id: ID | None
    status: str
    starts_on: date
    ends_on: date
    lines: list[dict[str, Any]]
    complete: bool


class ExceptionApproval(Schema):
    exception: dict[str, Any]
    policy: dict[str, Any]
    person: dict[str, str]
    starts_on: date
    ends_on: date
    timezone: str
    current_load: list[ExceptionApprovalContext]
    complete: bool
    basis: str


class AvailabilityInput(InstantInterval):
    person_id: ID
    kind: Literal["recorded_leave", "actual_absence", "external_commitment", "protected_time", "off_duty"] = Field(json_schema_extra={"bulk": True})
    leave_type: str = Field(default='', max_length=80)
    leave_type_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    private_reason: str = Field(min_length=3, max_length=1000)


class AvailabilityUpdate(Schema):
    kind: Literal["recorded_leave", "approved_leave", "actual_absence", "external_commitment", "protected_time", "off_duty"] | None = Field(default=None, json_schema_extra={"bulk": True, "bulk_clear": False})
    start: datetime | None = Field(default=None, json_schema_extra={"bulk": True, "bulk_clear": False})
    end: datetime | None = Field(default=None, json_schema_extra={"bulk": True, "bulk_clear": False})
    leave_type_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    reason: Reason

    @field_validator("start", "end")
    @classmethod
    def timezone_required(cls, value):
        return InstantInterval.timezone_required(value) if value is not None else None

    @model_validator(mode="after")
    def fields_to_change(self):
        fields = self.model_fields_set & {"kind", "start", "end", "leave_type_id"}
        if not fields:
            raise ValueError("Choose an availability field to change")
        if any(getattr(self, key) is None for key in fields - {"leave_type_id"}):
            raise ValueError("Kind, start and end cannot be cleared")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("End must follow start; intervals are start-inclusive/end-exclusive")
        return self


class AvailabilityUndoUpdate(Schema):
    receipt_id: ID
    operation_id: ID
    reason: Reason


class AvailabilityUndoCreate(Schema):
    receipt_id: ID
    reason: Reason


class LeaveInput(InstantInterval):
    person_id: ID
    leave_type_id: ID | None = None
    private_reason: str = Field(min_length=3, max_length=1000)
    kind: Literal['leave', 'clinic_change', 'meeting', 'administrative'] = 'leave'


class RequestEditUndo(Schema):
    receipt_id: ID


class OvertimeAvailabilityInput(Schema):
    person_id: ID
    kind: Literal['overtime_availability'] = 'overtime_availability'
    starts_on: date
    ends_on: date
    dates: list[date] = Field(default_factory=list, max_length=366)
    weekdays: list[Annotated[int, Field(ge=0, le=6, strict=True)]] = Field(default_factory=list, max_length=7)
    start_time: Clock = '00:00'
    end_time: Clock = '24:00'
    service_id: ID | None = None
    assignment_type_id: ID | None = None
    note: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def bounded_dates(self):
        if not 0 < (self.ends_on - self.starts_on).days <= 366:
            raise ValueError('Overtime availability must cover 1–366 days; end date is exclusive')
        if bool(self.dates) == bool(self.weekdays):
            raise ValueError('Choose one-off dates or a weekly pattern')
        if len(set(self.dates)) != len(self.dates) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('Each date or weekday once')
        if any(day < self.starts_on or day >= self.ends_on for day in self.dates):
            raise ValueError('Offered dates must fall within the availability period')
        if self.weekdays and not any((self.starts_on.weekday() + n) % 7 in self.weekdays
                                     for n in range((self.ends_on - self.starts_on).days)):
            raise ValueError('The weekly pattern must include a date in the availability period')
        if self.start_time == '24:00' or self.end_time <= self.start_time:
            raise ValueError('The offered time must end after it starts on the same day')
        return self


class ClosureInput(InstantInterval):
    location_id: ID
    reason: Reason


class RuleAdjustmentInput(Schema):
    policy_id: ID
    mode: Literal["off", "set", "clear"]
    amount: float | None = Field(default=None, ge=0, le=100000)
    line_id: ID | None = None
    number: Literal['min', 'max', 'target', 'priority', 'session_priority', 'apply'] | None = None
    person_id: ID | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    reason: Reason

    @model_validator(mode="after")
    def amount_for_set(self):
        if (self.mode == "set") != (self.amount is not None):
            raise ValueError("A number is needed only when changing the rule's number")
        if (self.number or self.person_id or self.starts_on or self.ends_on) and not self.line_id:
            raise ValueError('Choose the catalogue line for a scoped adjustment')
        if self.line_id and not self.number:
            raise ValueError('Choose the number on this line')
        if (self.starts_on is None) != (self.ends_on is None) or (
                self.starts_on and self.starts_on >= self.ends_on):
            raise ValueError('Choose a valid start and end date together')
        return self


class AdjustmentEffectInput(Schema):
    expected_revision: int = Field(ge=1)
    adjustment: RuleAdjustmentInput
    fit_person_ids: list[ID] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def unique_people(self):
        if len(set(self.fit_person_ids)) != len(self.fit_person_ids):
            raise ValueError('Choose each fit candidate once')
        return self


class TargetOverrideInput(Schema):
    policy_id: ID
    person_id: ID
    amount: float | None = Field(default=None, ge=0, le=100000)
    reason: Reason


class PeopleCounts(Schema):
    active_people: int = Field(ge=0)
    people_without_current_rank: int = Field(ge=0)


class IssueCounts(Schema):
    blocking: int = Field(ge=0)
    advisory: int = Field(ge=0)
    blocking_without_open_places: int = Field(default=0, ge=0)


class PublishedScheduleSummary(Schema):
    sessions_with_gaps: int = Field(ge=0)
    blocking: int = Field(ge=0)
    advisory: int = Field(ge=0)
    extra_cover_sessions: int = Field(ge=0)
    # The Fix list's causes over these checks (what the rota's footer counts), beside the checks above.
    must: int = Field(default=0, ge=0)
    could: int = Field(default=0, ge=0)


class SessionSummary(Schema):
    shift_id: str
    places_needed: int = Field(ge=0)
    places_filled: int = Field(ge=0)
    places_open: int = Field(ge=0)
    extra_cover_places_open: int = Field(default=0, ge=0)
    has_gap: bool
    other_gaps: bool = False
    start: datetime | None = None
    end: datetime | None = None
    locked: bool = False
    person_ids: list[str] = Field(default_factory=list)


FindingCauseCode = Literal['no_candidate_sessions', 'no_eligible_people', 'capacity_short',
                           'person_unavailable', 'empty_population', 'partial_window', 'unfilled', 'above_limit']


class QuotaProration(Schema):
    configured_amount: float = Field(ge=0)
    amount: float = Field(ge=0)
    working_days: int = Field(ge=0)
    full_working_days: int = Field(gt=0)
    starts_on: date
    ends_on: date
    calendar_id: ID | None
    rounding: Literal['up'] = 'up'


class FindingCause(Schema):
    code: FindingCauseCode
    eligible_people_basis: Literal['potential_cover_known_records'] | None = None
    sessions_found: int = Field(ge=0)
    eligible_people: int = Field(ge=0)
    places_available: int | None = Field(default=None, ge=0)
    days_in_window: int = Field(ge=0)
    days_available: int | None = Field(default=None, ge=0)
    required: float = Field(ge=0)
    possible: float | None = Field(default=None, ge=0)
    actual: float | None = Field(default=None, ge=0)
    proration: QuotaProration | None = None
    population_people: int | None = Field(default=None, ge=0)
    qualified_people: int | None = Field(default=None, ge=0)
    basis: Literal['known_records_not_solver_proof'] = 'known_records_not_solver_proof'


class FindingFixPrefill(Schema):
    starts_on: date
    ends_on: date
    assignment_type_ids: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    location_id: str | None = None
    weekdays: list[int] | None = None
    start_time: str | None = None
    end_time: str | None = None
    person_id: str | None = None
    shift_ids: list[str] = Field(default_factory=list)
    rule_id: str | None = None
    scope: dict[str, Any] = Field(default_factory=dict)


class FindingSeriesMatch(Schema):
    id: str
    revision: int
    name: str
    active: bool
    starts_on: date
    ends_on: date | None = None
    action: Literal['expand', 'extend', 'reactivate']


class FindingFixHint(Schema):
    action: Literal['add_sessions', 'open_eligibility', 'open_leave', 'open_rotation',
                    'open_rota', 'raise_capacity', 'run_auto_schedule', 'review_assignments']
    prefill: FindingFixPrefill
    matching_series: list[FindingSeriesMatch] = Field(default_factory=list)


class FindingSummary(Schema):
    key: str
    code: str
    severity: str
    shift_id: str | None
    person_id: str | None
    rule_id: str | None
    accepted: bool
    cause: FindingCause | None = None
    fix_hints: list[FindingFixHint] = Field(default_factory=list)


class FindingCauseGroup(Schema):
    key: str
    code: FindingCauseCode
    severity: str
    count: int = Field(ge=1)
    finding_keys: list[str]
    person_ids: list[str]
    rule_ids: list[str]
    fix_hints: list[FindingFixHint]


class RotaCoverageGap(Schema):
    starts_on: date
    ends_on: date
    days: int = Field(ge=1)
    before_workspace_id: str
    after_workspace_id: str
    suggested_name: str


class ScheduleScore(Schema):
    version: str
    score: float | None
    raw: float | None
    uncapped_score: float | None
    blocking_count: int = Field(ge=0)
    capped: bool
    capped_points: float
    publishable: bool
    gate: dict[str, Any] | None
    settings: dict[str, Any]
    components: dict[str, Any]
    why: list[dict[str, Any]]
    people: dict[str, Any]
    contributions: list[dict[str, Any]]
    coverage_fraction: float | None
    rule_weights: list[dict[str, Any]]


class ScoreReviewLine(Schema):
    """One advisory line, including zero-loss lines so the review can explain the whole catalogue."""
    rule_id: ID
    line_id: ID | None = None
    line: int | None = None
    slot: str | None = None
    name: str
    kind: PrimitiveKind
    level: Strength | Literal['off']
    weight: float = Field(ge=0)
    lost_points: float = Field(ge=0)
    lost_share: float = Field(ge=0, le=1)
    session_count: int = Field(ge=0)
    people_count: int = Field(ge=0)
    component: Literal['rules', 'fairness', 'preferences'] | None = None


class ScoreReview(Schema):
    workspace_id: ID
    workspace_revision: int = Field(ge=1)
    fingerprint: str
    score: ScheduleScore
    total_lost_points: float = Field(ge=0)
    lines: list[ScoreReviewLine]


class FindingObservationSummary(Schema):
    id: str
    finding_key: str
    code: str
    severity: str
    shift_id: str | None = None
    person_id: str | None = None
    rule_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    line: int | str | None = None
    slot: str | None = None


class RotaRepairTask(Schema):
    severity: Literal['error', 'warning'] | None = None
    key: str
    shift_ids: list[str]
    start: datetime | None = None
    end: datetime | None = None
    finding_keys: list[str]
    observation_ids: list[str]


class RotaRepairCounts(Schema):
    total_tasks: int = Field(default=0, ge=0)
    blocking_tasks: int = Field(default=0, ge=0)
    advisory_tasks: int = Field(default=0, ge=0)
    observations: int = Field(default=0, ge=0)


FIX_ACTION_LABELS = {
    'assign': 'Assign someone', 'assign_supervisor': 'Assign a supervisor',
    'autoschedule_scoped': 'Auto-schedule these', 'clash': 'See the clash',
    'open_line': 'Change the rule…', 'review_targets': 'Review targets', 'rebalance': 'Rebalance',
    'choose_cover': 'Choose cover', 'adjust_rota': 'Allow for this rota…',
    'add_sessions': 'Add sessions', 'open_eligibility': 'Who can work it', 'open_leave': 'See the leave',
    'open_rotation': 'See the rotation', 'refresh_sessions': 'Refresh sessions',
}
FIX_GROUP_LABELS = {'plan_duties': 'Plan more duties', 'share_work': 'Share work more evenly',
                    'who_can_work': 'Check who can work', 'review_rule': 'Review a rule'}
FIX_ACTION_GROUPS = {
    'assign': 'plan_duties', 'assign_supervisor': 'plan_duties', 'autoschedule_scoped': 'plan_duties',
    'add_sessions': 'plan_duties', 'rebalance': 'share_work', 'choose_cover': 'who_can_work',
    'open_eligibility': 'who_can_work', 'clash': 'review_rule', 'open_line': 'review_rule',
    'adjust_rota': 'review_rule', 'review_targets': 'review_rule', 'refresh_sessions': 'review_rule',
}


class FixListAction(Schema):
    kind: Literal[tuple(FIX_ACTION_LABELS)]
    scope: dict[str, Any]
    available: bool = True
    unavailable_reason: str | None = None


class FixListWhere(Schema):
    label: str


class FixListMissing(Schema):
    who_label: str
    have: int = Field(ge=0)
    need: int = Field(ge=1)


class FixListGroup(Schema):
    key: Literal[tuple(FIX_GROUP_LABELS)]
    label: str


class FixListCause(Schema):
    id: str
    level: Literal['must', 'could']
    checks: int = Field(ge=1)
    who: list[dict[str, Any]]
    person_ids: list[str]
    shift_ids: list[str]
    safety: bool
    new: bool
    sentence: str
    meta: str
    where: FixListWhere | None = None
    missing: list[FixListMissing] = Field(default_factory=list)
    detail_line: str = ''
    session_findings: dict[str, str] = Field(default_factory=dict)
    action: FixListAction
    fixes: list[FixListAction] = Field(default_factory=list, max_length=4)
    eligible_people: int | None = Field(default=None, ge=0)
    group: FixListGroup | None = None
    evidence: list[dict[str, Any]]
    score_gain: float | None = None
    score_gain_estimated: bool = False
    rule_lines: list[dict[str, Any]]
    finding_keys: list[str]
    evidence_hash: str
    acknowledgeable: bool
    acknowledgement: dict[str, Any] | None = None
    attribution: dict[str, Any] | None = None
    workflow_effect: dict[str, Any] | None = None


class FixListLeanActionScope(Schema):
    shift_ids: list[str] | None = None
    person_ids: list[str]
    dates: dict[str, Any]
    classification: str | None
    prefills: list[dict[str, Any]]
    shift_id: str | None = None
    keep_existing: bool | None = None


class FixListLeanAction(Schema):
    kind: Literal[tuple(FIX_ACTION_LABELS)]
    label: str
    scope: FixListLeanActionScope
    available: bool = True
    unavailable_reason: str | None = None


class FixListLeanAttribution(Schema):
    kind: str | None = None
    classification: str | None = None
    group_ids: list[str] = Field(default_factory=list)
    responsible_group_ids: list[str] = Field(default_factory=list)
    required_group_ids: list[str] = Field(default_factory=list)
    complete: bool | None = None


class RotaSubmissionIssue(Schema):
    code: Literal['EMPTY_WORKSPACE']
    message: str
    action: Literal['add_session']


class FixListLeanCause(Schema):
    id: str
    level: Literal['must', 'could']
    sentence: str
    shift_ids: list[str]
    group: FixListGroup | None = None
    action: FixListLeanAction
    fixes: list[FixListLeanAction] = Field(default_factory=list, max_length=4)
    safety: bool
    acknowledgeable: bool
    new: bool
    detail_line: str = ''
    where: FixListWhere | None = None
    session_findings: dict[str, str] = Field(default_factory=dict)
    finding_keys: list[str]
    person_ids: list[str]
    rule_id: str | None = None
    line_id: str | None = None
    slot: str | None = None
    acknowledgement: dict[str, Any] | None = None
    attribution: FixListLeanAttribution | None = None


class FixListLeanSummary(Schema):
    must: int = Field(ge=0)
    could: int = Field(ge=0)
    checking: bool = False
    check_status: JobStatus | None = None
    check_queued_at: datetime | None = None
    population_note: str | None = None
    since: dict[str, Any] | None = None
    checked_at: datetime | None = None
    places_filled: int = Field(ge=0)
    places_total: int = Field(ge=0)
    places_open: int = Field(ge=0)
    checks_must: int = Field(ge=0)
    checks_could: int = Field(ge=0)
    fairness_checks: int = Field(ge=0)
    acknowledged: int = Field(ge=0)
    submit_blocked: bool
    submission_issue: RotaSubmissionIssue | None = None
    score: float | None = None
    uncapped_score: float | None = None


class RotaReviewItem(Schema):
    group_ids: list[str] = Field(default_factory=list)
    author_identity_id: str | None = None
    author_name: str | None = None
    at: datetime | None = None
    sentence: str | None = None
    rule_id: str | None = None
    id: str
    kind: Literal['rule_adjustment', 'target_override', 'acknowledged_gap']
    reason: str | None
    name: str | None = None
    source: dict[str, Any]


class FixListLean(Schema):
    workspace_id: str | None = None
    revision: int | None = None
    fingerprint: str | None = None
    summary: FixListLeanSummary
    causes: list[FixListLeanCause]
    review_items: list[RotaReviewItem] = Field(default_factory=list)
    planning_context: dict[str, Any] | None = None


class FixListDetail(Schema):
    workspace_id: str | None = None
    revision: int | None = None
    fingerprint: str | None = None
    cause: FixListCause


class FixListSummary(Schema):
    submission_issue: RotaSubmissionIssue | None = None
    population_note: str | None = None
    must: int = Field(ge=0)
    could: int = Field(ge=0)
    checking: bool = False
    check_status: JobStatus | None = None
    check_queued_at: datetime | None = None
    since: dict[str, Any] | None = None
    checked_at: datetime | None = None
    places_filled: int = Field(ge=0)
    places_total: int = Field(ge=0)
    places_open: int = Field(ge=0)
    checks_must: int = Field(ge=0)
    checks_could: int = Field(ge=0)
    fairness_checks: int = Field(ge=0)
    acknowledged: int = Field(ge=0)
    submit_blocked: bool
    score: float | None = None
    uncapped_score: float | None = None
    could_score_gain: float | None = None


class FixList(Schema):
    workspace_id: str | None = None
    revision: int | None = None
    fingerprint: str | None = None
    summary: FixListSummary
    causes: list[FixListCause]
    review_items: list[RotaReviewItem] = Field(default_factory=list)
    planning_context: dict[str, Any] | None = None


class FixListDelta(Schema):
    cleared: list[str]
    new: list[str]
    changed: list[str]
    causes: list[FixListCause]
    summary: FixListSummary | None = None


class RotaCauseSummary(Schema):
    workspace_id: ID
    revision: int
    fingerprint: str
    observations: list[FindingObservationSummary] = Field(default_factory=list)
    repair_tasks: list[RotaRepairTask] = Field(default_factory=list)
    cause_groups: list[FindingCauseGroup] = Field(default_factory=list)


class RotaSummary(Schema):
    population_note: str | None = None
    scope: str | None = None
    issued_counts: PublishedScheduleSummary
    fix_list: FixList
    repair_counts: RotaRepairCounts = Field(default_factory=RotaRepairCounts)
    checked_at: datetime | None = None
    session_count: int = Field(default=0, ge=0)
    observations: list[FindingObservationSummary] = Field(default_factory=list)
    repair_tasks: list[RotaRepairTask] = Field(default_factory=list)
    workspace_id: ID
    revision: int
    fingerprint: str
    as_of: datetime
    checked: bool
    valid: bool | None
    worker_liveness: WorkerLiveness
    worker_heartbeat_max_age_seconds: int = Field(ge=1, le=86400, strict=True)
    places_needed: int = Field(ge=0)
    places_filled: int = Field(ge=0)
    places_open: int = Field(ge=0)
    extra_cover_places_open: int = Field(default=0, ge=0)
    sessions_with_open_extra_cover: int = Field(default=0, ge=0)
    sessions_with_gaps: int = Field(ge=0)
    other_gaps: int = Field(default=0, ge=0)
    issues: IssueCounts
    score: ScheduleScore | None
    people: PeopleCounts
    sessions: list[SessionSummary]
    findings: list[FindingSummary]
    cause_groups: list[FindingCauseGroup] = Field(default_factory=list)
    coverage_gaps: list[RotaCoverageGap]


class JobScopeWindow(Schema):
    start: date
    end: date


class JobSelectionWindow(Schema):
    start: datetime
    end: datetime


class JobRunContext(Schema):
    scope_window: JobScopeWindow
    selection_window: JobSelectionWindow | None
    service_ids: list[str]
    basis: Literal['frozen_workspace_and_selected_duties']


class InputChangeActor(Schema):
    id: ID
    display_name: str | None = None


class ChangedInputFact(Schema):
    kind: str
    id: str
    source_id: str | None = None
    source_kind: str | None = None
    change: Literal['added', 'changed', 'removed']
    person_id: ID | None = None
    person_name: str | None = None
    actor: InputChangeActor | None = None
    attribution_complete: bool = False
    first_day: str | None = None
    last_day: str | None = None
    starts_on: str | None = None
    ends_on: str | None = None
    start: str | None = None
    end: str | None = None
    wish_kind: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class ConflictingPlacement(Schema):
    bundle_id: str
    launch: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    shift_id: ID
    person_ids: list[ID] = Field(default_factory=list)
    addition_indexes: list[int]
    removal_indexes: list[int]
    code: str
    findings: list[dict[str, Any]] = Field(default_factory=list)


class ProposalReconciliation(Schema):
    id: str
    status: Literal['ready', 'partial', 'content_conflict', 'unresolved', 'applied', 'revalidation_required', 'undone']
    format: int = 1
    job_id: ID | None = None
    source_fingerprint: str | None = None
    current_fingerprint: str | None = None
    prepared_fingerprint: str | None = None
    validation_current: bool = False
    content_revision: int | None = None
    current_revision: int | None = None
    addition_indexes: list[int] = Field(default_factory=list)
    removal_indexes: list[int] = Field(default_factory=list)
    changed_facts: list[ChangedInputFact] = Field(default_factory=list)
    changed_fact_count: int = 0
    conflicting_placements: list[ConflictingPlacement] = Field(default_factory=list)
    conflicting_placement_count: int = 0
    retention_optimal: bool = False
    reconciliation_allowance_exhausted: bool = False
    solver_status: str | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    applied_at: str | None = None
    applied_content_revision: int | None = None
    # D54: the applied pass's population (None = everyone); the workspace's checked audience is the union of these.
    population_recorded: bool = False
    population_people: list[ID] | None = None


JobCurrency = Literal['current', 'revalidation_required', 'unknown', 'preparing', 'superseded']
RUN_SUPERSEDED = 'RUN_SUPERSEDED'
LIVE_RECHECK_BASIS = "today's availability and safety rules"


class JobPlanningContext(Schema):
    currency: JobCurrency = 'unknown'
    reconciliation: ProposalReconciliation | None = None
    result_stale: bool | None = None
    run_context: JobRunContext | None = None


class HomeProposal(Schema):
    job_id: ID
    outcome: Literal['complete', 'partial']
    created_at: datetime
    summary: dict[str, Any]
    currency: JobCurrency = 'unknown'
    reconciliation: ProposalReconciliation | None = None


class RuleLineValidity(Schema):
    effective: Literal['active', 'ended', 'future']
    starts_on: date | None
    ends_on: date | None


class RuleAttentionCounts(Schema):
    rules: int = Field(ge=0)
    lines: int = Field(ge=0)


class RuleInForce(Schema):
    """A binding policy retained alongside the catalogue's editable rule."""
    id: ID
    name: str
    kind: str
    lines: int = Field(ge=0)
    reach: int = Field(ge=0)
    starts_on: date | None
    ends_on: date | None
    safety: bool


class RuleLineReplacement(Schema):
    replaced_line_id: ID
    numbers: list[str]


class RuleCatalogueInForceEntry(Schema):
    also_in_force: list[RuleInForce]
    line_replaces: dict[ID, list[RuleLineReplacement]] = Field(default_factory=dict)


class RuleCatalogueCounts(Schema):
    historical: int = Field(ge=0)
    v2: int = Field(ge=0)
    authorable: int = Field(ge=0)
    in_force: int = Field(ge=0)
    lines: int = Field(ge=0)


class RuleCatalogueInForce(Schema):
    not_on_catalogue: list[RuleInForce]
    catalogue_counts: RuleCatalogueCounts


class CatalogueCounts(Schema):
    """Compact catalogue counts used by setup badges, including the checker-owned rules count."""
    counts: dict[str, int]
    rules_in_force: int = Field(ge=0)


class PublishedOperationalAction(Schema):
    key: Literal['amend_publication'] = 'amend_publication'
    publication_id: ID
    workspace_id: ID
    amendment_workspace_id: ID | None = None
    amendment_shift_id: ID | None = None
    amendment_shift_state: Literal['not_created', 'matched', 'ambiguous', 'changed_or_missing']
    shift_id: ID
    date: date


class PublishedOperationalWarning(Schema):
    key: str
    publication_id: ID
    workspace_id: ID
    shift_id: ID
    assignment_id: ID
    person_id: ID
    start: datetime
    end: datetime
    action: PublishedOperationalAction | None = None


class PublishedOperationalSummary(Schema):
    basis: Literal['current_absence_overlaps'] = 'current_absence_overlaps'
    as_of: datetime
    fingerprint: str
    starts_on: date
    ends_on: date
    conflicting_assignments: int = Field(ge=0)
    affected_sessions: int = Field(ge=0)
    warnings: list[PublishedOperationalWarning]


class PublishedOperationalAttention(Schema):
    key: Literal['published_absence_conflicts'] = 'published_absence_conflicts'
    basis: Literal['current_absence_overlaps'] = 'current_absence_overlaps'
    count: int = Field(ge=0)
    sample: list[PublishedOperationalWarning] = Field(max_length=2)
    action: PublishedOperationalAction | None = None


class FairnessMetricTotal(Schema):
    """Published-plan quantity across the filtered result, independent of the session display cap."""
    metric_value: float
    basis: Literal['published_plan'] = 'published_plan'


class HomeRotaRow(Schema):
    proposal: HomeProposal | None = Field(default=None, exclude_if=lambda value: value is None)
    id: ID
    name: str
    status: str
    starts_on: date
    ends_on: date
    base_publication_id: ID | None = None
    health: dict[str, Any]


class HomeRotaList(Schema):
    workspaces: list[HomeRotaRow]
    limit: int
    offset: int
    has_more: bool


class WorkspaceForkInput(Schema):
    workspace_id: ID
    revision: int = Field(ge=1)


BASELINE_SOURCE_NAMES = ("people", "duties", "calendars", "rules", "score", "auto", "receipts", "display")
BaselineSourceName = Literal["people", "duties", "calendars", "rules", "score", "auto", "receipts", "display"]

DraftSafetyChange = Literal["stricter_only", "approved_loosening", "any_with_reason"]
DraftSafetyUseDefault = Literal["allowed", "needs_approval"]
DraftConfigEditors = Literal["department_roles", "draft_editors", "administrators"]
DraftConfigApproval = Literal["free", "needs_approval"]
DraftConfigSource = Literal["own_snapshot", "follow_department"]
DraftDefaultChange = Literal["notify", "use_unchanged"]
DraftConfigReason = Literal["safety_only", "all", "optional"]
DraftSettingsStatus = Literal["proposed", "active", "declined", "retired"]
DEFAULT_DRAFT_SAFETY_CHANGE: DraftSafetyChange = "stricter_only"
DRAFT_CONFIG_DEFAULTS = {
    "safety_change": DEFAULT_DRAFT_SAFETY_CHANGE,
    "safety_use_default": "allowed",
    "editors": "department_roles",
    "non_safety_approval": "free",
    "rules_source": "own_snapshot",
    "duties_source": "own_snapshot",
    "score_source": "own_snapshot",
    "auto_source": "own_snapshot",
    "display_source": "own_snapshot",
    "default_change": "notify",
    "reason_required": "safety_only",
}


class WorkspaceBaselineScope(Schema):
    """Validated source selection for the immutable draft baseline."""
    mode: Literal["department", "custom"] = "department"
    sources: list[BaselineSourceName] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def distinct_custom_sources(self):
        if len(self.sources) != len(set(self.sources)):
            raise ValueError("Choose each baseline source once")
        if self.mode == "custom" and not self.sources:
            raise ValueError("Custom baseline scope needs at least one source")
        return self


class WorkspaceBaselineRefreshInput(Schema):
    """Three-way refresh decisions; unresolved conflicts stay blocked."""
    expected_baseline_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None
    decisions: dict[BaselineSourceName, Literal["use_department", "keep_draft", "accept", "keep"]] = Field(default_factory=dict)
    reason: Reason | None = None


class WorkspaceInput(Schema):
    visibility: Literal['private', 'shared'] = 'shared'
    purpose: Literal['sandbox', 'alternative', 'integrated'] = 'alternative'
    name: Name
    starts_on: date
    ends_on: date
    base_publication_id: ID | None = None
    # Replace from a date: this rota replaces that published one from its own first day.
    replaces_publication_id: ID | None = None
    urgency: Literal["routine", "urgent"] = "routine"
    urgency_reason: str = Field(default="", max_length=500)
    source_workspace_id: ID | None = None
    source_revision: int | None = Field(default=None, ge=1)
    baseline: WorkspaceBaselineScope | None = None
    fork_from: WorkspaceForkInput | None = None
    copy_people: bool = False
    copy_alignment: Literal['date', 'weekday'] = Field(default='weekday', description='Deprecated: copies use offsets from the period start')
    series_ids: list[ID] = Field(default_factory=list, max_length=500)
    generate_assignments: bool = False

    @model_validator(mode="after")
    def bounded(self):
        if self.fork_from is not None:
            if self.source_workspace_id or self.source_revision is not None:
                raise ValueError('Choose either source_workspace_id or fork_from')
            self.source_workspace_id = self.fork_from.workspace_id
            self.source_revision = self.fork_from.revision
            self.copy_people = True
        if self.visibility == 'private' and self.purpose != 'sandbox':
            raise ValueError('Private workspaces must be sandboxes')
        if self.purpose == 'sandbox' and self.base_publication_id:
            raise ValueError('Create a publication amendment separately from a sandbox')
        if self.generate_assignments and (self.base_publication_id or (self.ends_on-self.starts_on).days > 31):
            raise ValueError('Automatic setup supports new rotas of at most 31 days')
        if self.base_publication_id and self.replaces_publication_id:
            raise ValueError('A rota either changes a published one or replaces it from a date, not both')
        if self.base_publication_id and (self.source_workspace_id or self.series_ids):
            raise ValueError('Amendments cannot copy another rota or expand templates during creation')
        if self.source_workspace_id and (self.source_revision is None or self.series_ids):
            raise ValueError('Choose either a previous rota with its current revision or recurring templates')
        if not 0 < (self.ends_on - self.starts_on).days <= 93:
            raise ValueError("Workspace must span 1–93 days, with exclusive end")
        if self.urgency == "urgent" and (not self.base_publication_id or len(self.urgency_reason) < 3):
            raise ValueError("Urgency requires an amendment and an explicit reason")
        if self.urgency == "routine" and self.urgency_reason:
            raise ValueError("An urgency reason applies only to urgent amendments")
        return self


class ParticipationInterval(InstantInterval):
    fixed: bool = False
    participation: Literal["staff", "trainee", "supervisor"] = "staff"
    overtime: bool = False
    supervisor_person_id: ID | None = None
    team_lead: bool = False
    segments: list[InstantInterval] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def bounded_segments(self):
        # [] is the retained contiguous-duty contract, not zero attendance.
        if self.segments:
            ordered = sorted(self.segments, key=lambda row: row.start)
            if ordered != self.segments:
                raise ValueError("Participation segments must be in chronological order")
            if ordered[0].start != self.start or ordered[-1].end != self.end:
                raise ValueError("First and last segment boundaries must match the reserved duty interval")
            if any(a.end >= b.start for a, b in zip(ordered, ordered[1:])):
                raise ValueError("Segments must be disjoint with an explicit gap; combine adjacent segments")
        return self


class ParticipantInput(ParticipationInterval):
    person_id: ID


AssignmentSourceKind = Literal['auto_schedule', 'manual', 'swap', 'import', 'recurring']


class AssignmentSource(Schema):
    """Server-owned provenance; null means unknown, including pre-migration assignments."""
    source: AssignmentSourceKind | None = None
    source_run_id: ID | None = None
    source_actor_id: ID | None = None
    source_at: datetime | None = None
    override_reason: Reason | None = None


class ParticipantRecord(ParticipantInput, AssignmentSource):
    # Dated rank is a read-only schedule label; assignments never store it.
    rank: str = ''
    id: ID
    shift_id: ID
    revision: int
    created_at: datetime


class StaffingSuggestionsInput(Schema):
    search_options: SearchOptions | None = Field(default=None, exclude_if=lambda value: value is None)
    workspace_id: ID
    shift_id: ID
    expected_revision: int = Field(ge=1)
    replace_assignment_id: ID | None = None
    slot: ParticipationInterval | None = None
    search: str = Field(default="", max_length=80)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def exact_replacement(self):
        if self.replace_assignment_id and self.slot is not None:
            raise ValueError("Replacement suggestions retain the exact existing duty; do not supply a different slot")
        return self


class FillOptionsInput(Schema):
    workspace_id: ID
    shift_id: ID
    expected_revision: int = Field(ge=1)
    request_id: ID | None = None
    replace_assignment_id: ID | None = None
    finding_key: 'IssueKey | None' = None
    supervisor_person_id: ID | None = None


ASSIGNMENT_STAMP_PREFIX = 'assign:'
AssignmentStamp = Annotated[str, StringConstraints(pattern=rf'^{ASSIGNMENT_STAMP_PREFIX}[a-f0-9]{{64}}$')]


class FillOptionsResult(Schema):
    """A checked fill-options page or the final complete candidate result."""
    provisional_page: bool
    workspace_id: ID
    workspace_revision: int = Field(ge=1)
    shift_id: ID
    replace_assignment_id: ID | None = None
    replace_refusal: str | None = None
    slot: dict[str, Any]
    source_fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    assignment_stamp: AssignmentStamp | None = None
    evaluated_at: datetime
    candidates: list[dict[str, Any]]
    basis: str


class WhyInput(Schema):
    """The planner's Why tab: one session (everyone), or one person across up to 200 sessions."""
    workspace_id: ID
    expected_revision: int = Field(ge=1)
    shift_ids: list[ID] = Field(min_length=1, max_length=200)
    person_id: ID | None = None
    runner_up: bool = False

    @model_validator(mode="after")
    def one_session_without_a_person(self):
        if self.person_id is None and len(self.shift_ids) != 1:
            raise ValueError("Without a person, explain one session at a time")
        return self


class WhyWishEffect(Schema):
    text: str
    granted: bool


class WhyScoreEffect(Schema):
    """With minus without; components, empty-rota adjustment and gate sum to displayed points."""
    points: float
    coverage: float
    rules: float
    preferences: float
    fairness: float
    stability: float
    gate: float
    other: float
    score_with: float
    score_without: float
    uncapped_score_with: float
    uncapped_score_without: float
    wishes: list[WhyWishEffect]


class WhyLeave(Schema):
    type: str
    starts_on: date
    ends_on: date
    note: str | None = None


class WhyFinding(Schema):
    person_id: str | None = None
    shift_id: str | None = None
    code: str
    kind: str
    severity: Literal['error', 'warning']
    rule_id: str | None
    message: str
    detail: dict | None = None
    start: datetime | None = None
    end: datetime | None = None
    leave: WhyLeave | None = None


class StaffingSupervisorChoice(Schema):
    supervisor_person_id: ID
    display_name: str
    proposal: ParticipantInput


class StaffingSupervisionPlan(Schema):
    """An exact validation trial, never a saved assignment or permission to bypass checks."""
    choices: list[StaffingSupervisorChoice]
    proposal: ParticipantInput | None
    choice_required: bool


class WhyRow(Schema):
    supervision: StaffingSupervisionPlan | None = None
    supervisor_person_id: ID | None = None
    person_id: ID
    display_name: str
    group: Literal['assigned', 'free', 'rule', 'unavailable', 'not_eligible']
    findings: list[WhyFinding]
    assignment_id: ID | None = None
    team_lead: bool | None = None
    participation: str | None = None
    status: Literal['ok', 'warn', 'blocked'] | None = None
    fills: bool | None = None
    can_lead: bool | None = None
    rota_hours: float | None = None
    week_hours: float | None = None
    rest_before_hours: float | None = None
    rest_after_hours: float | None = None
    effect: WhyScoreEffect | None = None


class WhyRunnerUp(Schema):
    person_id: ID
    display_name: str
    # Selected person's score minus the best alternative's; negative means the alternative scores better.
    point_difference: float
    uncapped_point_difference: float


class WhySession(Schema):
    shift_id: ID
    cancelled: bool
    closed: bool = False
    rows: list[WhyRow]
    score_status: Literal['complete', 'work_limit', 'not_applicable']
    runner_up: WhyRunnerUp | None


class WhyResult(Schema):
    workspace_id: ID
    workspace_revision: int
    source_fingerprint: str
    person_id: ID | None
    evaluated_at: datetime
    sessions: list[WhySession]
    scored_limit: int
    basis: str


class ShiftInput(InstantInterval):
    assignment_type_id: ID = Field(json_schema_extra={"bulk": True})
    location_id: ID = Field(json_schema_extra={"bulk": True})
    service_ids: list[ID] = Field(min_length=0, max_length=12, json_schema_extra={"bulk": True})
    attributes: dict[str, Any] = Field(default_factory=dict, json_schema_extra={"bulk": True})
    participants: list[ParticipantInput] = Field(default_factory=list, max_length=100, json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def no_duplicate_participants(self):
        if len(set(p.person_id for p in self.participants)) != len(self.participants):
            raise ValueError("One person may have one participation interval per duty")
        if len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("Service references must be unique")
        for p in self.participants:
            if p.start < self.start or p.end > self.end:
                raise ValueError("Participation must be contained in the session")
        return self


class ShiftCreate(ShiftInput):
    workspace_id: ID
    override_reason: Reason | None = None


class ShiftEdit(ShiftInput):
    workspace_id: ID
    shift_id: ID
    override_reason: Reason | None = None


class ShiftAction(Schema):
    workspace_id: ID
    shift_ids: list[ID] = Field(min_length=1, max_length=500)
    action: Literal["cancel", "reopen", "lock", "unlock", "remove", "refresh_types", "release_unavailable", "unassign"]
    reason: Reason


IssueKey = Annotated[str, StringConstraints(min_length=3, max_length=400)]


class IssueBatch(Schema):
    """Schedule › Resolve issues: one action on many findings of the rota's check, named by issue key
    (apps/api/services/issues.py `issue_key`). One command: one change set (one Undo), one audit event, one re-check.
    Replace writes the plan its preview showed (`plan_hash`); the others need no preview."""
    workspace_id: ID
    action: Literal["replace", "unassign", "remove", "accept"]
    keys: list[IssueKey] = Field(min_length=1, max_length=2000)
    plan_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None
    reason: Reason

    @model_validator(mode="after")
    def replace_needs_its_preview(self):
        if self.action == "replace" and not self.plan_hash:
            raise ValueError("Preview the replacements first")
        return self


class IssuePreviewInput(Schema):
    """Who Replace would put in each issue's place (read-only)."""
    workspace_id: ID
    expected_revision: int = Field(ge=1)
    keys: list[IssueKey] = Field(min_length=1, max_length=2000)


class ExceptionSpan(Schema):
    policy_id: ID
    person_id: ID
    starts_on: date
    ends_on: date


class ExceptionBatch(Schema):
    """Exception requests for many issues at once (each checked like `exception.create`, all or none)."""
    items: list[ExceptionSpan] = Field(min_length=1, max_length=500)
    reason: Reason


class ShiftCopy(Schema):
    workspace_id: ID
    shift_ids: list[ID] = Field(min_length=1, max_length=100)
    offset_days: int = Field(ge=-92, le=92)
    reason: Reason


class DraftSwap(Schema):
    workspace_id: ID
    first_assignment_id: ID
    second_assignment_id: ID
    reason: Reason


class DraftMove(Schema):
    workspace_id: ID
    assignment_id: ID
    target_shift_id: ID
    reason: Reason


class Decision(Schema):
    reason: Reason


class JobCancelInput(Decision):
    only_if_queued: bool = False


class WorkspaceValidation(Decision):
    automatic: bool = False


class WorkspaceUndoInput(Decision):
    undo_of: ID | None = None


class ApprovalNote(Schema):
    """An independent approval needs no invented reason; retain the legacy reason input."""
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ''

    @model_validator(mode='after')
    def approval_note(self):
        if self.note is not None:
            if self.reason and self.reason != self.note:
                raise ValueError('Supply one approval note')
            self.reason = self.note
        self.note = self.reason or None
        return self


class PublicationChangeInput(Decision):
    kind: Literal['replace', 'absence', 'move', 'cancel', 'add', 'swap']
    shift_id: ID
    assignment_id: ID | None = None
    person_id: ID | None = None
    target_shift_id: ID | None = None
    target_assignment_id: ID | None = None
    absence_reason: str = Field(default='', max_length=500)


class PublicationChangeUndo(Decision):
    change_id: ID


class PublicationChangesSubmit(Decision):
    change_ids: list[ID] | None = Field(default=None, min_length=1, max_length=500)
    urgency: Literal['routine', 'urgent'] = 'routine'


# One vocabulary for reading every absence; who may CHOOSE what differs (DL-035): the coordinator's "Mark absent"
# offers all of them, a staff "Can’t make it" report never offers "leave" (planned leave goes through a Leave request,
# with its approval, balance and overlap checks).
AbsenceCategory = Literal['sick', 'leave', 'emergency', 'other']
ABSENCE_CATEGORIES: tuple[str, ...] = get_args(AbsenceCategory)
ReporterAbsenceCategory = Literal['sick', 'emergency', 'other']
ABSENCE_REPORTER_CATEGORIES: tuple[str, ...] = get_args(ReporterAbsenceCategory)
assert set(ABSENCE_REPORTER_CATEGORIES) <= set(ABSENCE_CATEGORIES)
ABSENCE_REPORT_LABEL = "Can’t make it"
ABSENCE_CATEGORY_LABELS: dict[AbsenceCategory, str] = {
    'sick': 'Sick', 'leave': 'Leave', 'emergency': 'Emergency', 'other': 'Other'}
assert tuple(ABSENCE_CATEGORY_LABELS) == ABSENCE_CATEGORIES


class OwnAbsenceReportSummary(Schema):
    assignment_id: ID
    absence_category: AbsenceCategory | None = None
    note: str | None = None
    created_at: datetime


class AbsenceReportInput(Decision):
    kind: Literal['absence_report'] = 'absence_report'
    # Optional, explicitly submitted label for the reporter's own summary, never inferred from reason.
    category: ReporterAbsenceCategory | None = None
    publication_id: ID
    assignment_id: ID
    note: str = Field(default='', max_length=1000)
    document_reference: str | None = Field(default=None, min_length=1, max_length=500)


class AbsenceReportSettingsInput(Decision):
    document_required: bool


class GapAcknowledgement(Schema):
    cause_id: str
    reason: Reason


class WorkspaceSubmitInput(Decision):
    acknowledgements: list[GapAcknowledgement] = Field(default_factory=list)


class WorkspaceMergePreviewInput(Decision):
    source_workspace_id: ID
    background: bool = True


class WorkspaceMergeApplyInput(Decision):
    merge_id: ID
    background: bool = True
    bundle_ids: list[str] | None = None
    choices: dict[str, Literal['rota', 'yours', 'left_out', 'not_merged']] = Field(default_factory=dict)


class PlanningGroupDefinition(EffectiveInput):
    name: Name
    owner_identity_ids: list[ID] = Field(min_length=1)
    co_editor_identity_ids: list[ID] = Field(default_factory=list)
    membership: Conditions
    primary_priority: int = Field(ge=0, strict=True)

    @model_validator(mode='after')
    def absolute_membership(self):
        only_fields(self.membership, GENERATION_SCOPE_FIELDS, 'Scheduling group membership')
        if len(set(self.owner_identity_ids + self.co_editor_identity_ids)) != len(self.owner_identity_ids + self.co_editor_identity_ids):
            raise ValueError('Choose each owner or co-editor once')
        return self


class PlanningGroupInput(Decision):
    definition: PlanningGroupDefinition


class PlanningSettingsDefinition(Schema):
    coordinator_identity_ids: list[ID] = Field(default_factory=list)
    active_private_drafts_per_owner: int | None = Field(default=None, ge=1, strict=True)
    archive_after_days: int | None = Field(default=None, ge=1, strict=True)
    group_lookahead: Literal['coupled', 'none'] = 'coupled'


class PlanningSettingsInput(Decision):
    definition: PlanningSettingsDefinition


class PlanningRollback(Decision):
    version_id: ID


class ScoreSettingsDefinition(Schema):
    """The department-owned, versioned part of scoring that changes output weights."""
    weights: ScoreWeights = Field(default_factory=ScoreWeights)
    strengths: StrengthMultipliers = Field(default_factory=StrengthMultipliers)


class ScoreSettingsInput(Decision):
    definition: ScoreSettingsDefinition


class ScoreSettingsRollback(Decision):
    version_id: ID


class AuthorityRestoreInput(Decision):
    """review_delegation.restore / self_approval.restore payload; Command target/revision bind the revoked source.

    The server copies original authority into a successor; callers cannot replace its provenance, scope or dates.
    """


class NotificationRead(Schema):
    reason: Reason = "Read notification"


class NotificationUnack(NotificationRead):
    notification_ids: list[ID] = Field(max_length=NOTIFICATION_UNDO_LIMIT)
    undo_of: ID

    @field_validator("notification_ids")
    @classmethod
    def distinct_notifications(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Each notification once")
        return value


class OverlapChoice(Decision):
    """How a draft sits beside the published rota it overlaps: start after it, or replace it from a date
    (`starts_on`, defaulting to the suggested day). Changing the published rota itself is a new amendment."""
    choice: Literal["start_after", "replace_from"]
    starts_on: date | None = None


class PolicyActivationDecision(ApprovalNote):
    check_job_ids: list[ID] = Field(default_factory=list, max_length=100)
    acknowledge_unresolved: bool = Field(default=False,
        description='Explicitly accept unresolved effects with a recorded reason; does not permit proven new contradictions or invalid rule settings.')
    # Older active copies this rule replaces; they are retired in the same decision.
    replaces_ids: list[ID] = Field(default_factory=list, max_length=500)


class PolicyManyActivation(PolicyActivationDecision):
    """Several lines of one rule approved in one decision (checked together, retired copies included)."""
    items: list[PolicyLine] = Field(min_length=1, max_length=1000)


class PolicyGroupInput(Decision):
    """Similar rules (each list, as the preview proposed it) become one proposed rule each; a list of one rule links a
    protected academic day to the academic calendar. `changes` are the previewed changes that are not identical (an
    allocation balance as a Balance goal, overtime switched back on), each chosen on its own. `names` renames a group,
    keyed by any one of its rules."""
    groups: list[list[ID]] = Field(default_factory=list, max_length=200)
    changes: list[list[ID]] = Field(default_factory=list, max_length=200)
    names: dict[ID, Name] = Field(default_factory=dict, max_length=200)
    # Link protected academic days to the programme's academic calendar (adding the calendar entry when missing).
    link_calendar: bool = True

    @model_validator(mode='after')
    def distinct_rules(self):
        ids = [i for g in self.groups + self.changes for i in g]
        if not ids or any(not g for g in self.groups + self.changes) or len(ids) != len(set(ids)) or len(ids) > 5000:
            raise ValueError('Each rule belongs to one group')
        return self


class RuleMigrationBlocker(Schema):
    """A conversion blocker with exact records the operator can open and resolve."""
    rule_id: str = ''
    name: str = ''
    kind: str
    reason: str
    proposal_ids: list[ID] = Field(default_factory=list, max_length=2000)
    person_ids: list[ID] = Field(default_factory=list, max_length=2000)
    line_ids: list[ID] = Field(default_factory=list, max_length=2000)


class RuleMigrationInput(Decision):
    """Move the rules in force into the rule catalogue as the preview planned it: every entry, or the chosen ones."""
    catalogue_ids: list[Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')]] = Field(default_factory=list, max_length=40)
    version: Literal[1, 2] = 1
    plan_hash: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')] | None = None

    @model_validator(mode='after')
    def department_conversion(self):
        if self.version == 2 and self.catalogue_ids:
            raise ValueError('Catalogue v2 conversion is one complete department proposal')
        if self.version == 2 and self.plan_hash is None:
            raise ValueError('Catalogue v2 conversion requires the preview plan hash')
        return self


class CatalogueProofJob(Schema):
    id: ID
    status: JobStatus
    progress: dict[str, Any] | None = None
    error_code: str | None = None


class CatalogueProofPreview(Schema):
    proof_state: Literal['blocked', 'not_checked', 'queued', 'running', 'passed', 'different', 'failed', 'cancelled']
    proof_job: CatalogueProofJob | None = None
    proof: dict[str, Any] | None = None


class CatalogueUpdating(Schema):
    """A catalogue projection is being refreshed and can be retried later."""
    state: Literal['updating']
    projection: Literal['rule_catalogue']
    stamp: str
    job_id: ID | None
    retry_after_seconds: int = Field(gt=0, strict=True)


class RuleProposalInput(Decision):
    """Propose the first version of a catalogue rule or amend the active policy ID and revision the editor read.
    An amendment keeps omitted rule fields. The catalogue version also follows department authoring rules.
    Sending settings or lines replaces the whole field, including with an empty object or list.
    Ordered overrides need no acknowledgement."""
    legacy_lead_conversion: LegacyLeadConversion | None = None
    catalogue_id: Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')]
    catalogue_version: Literal[1, 2] = 1
    expected_policy_id: ID | None = Field(default=None, json_schema_extra={"writeOnly": True})
    expected_revision: int | None = Field(default=None, ge=1, json_schema_extra={"writeOnly": True})
    expected_line_ids: list[ID] | None = Field(default=None, max_length=2000, json_schema_extra={"writeOnly": True})
    on: bool = Field(default=True, json_schema_extra={"bulk": True})
    balance_between_people: bool = Field(default=True, strict=True, json_schema_extra={"bulk": True})
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict, max_length=8, json_schema_extra={"bulk": True})
    lines: list[RuleLine] = Field(default_factory=list, max_length=2000, json_schema_extra={"bulk": True})
    acknowledged_overlaps: list[ID] = Field(default_factory=list, max_length=2000, description='Ignored legacy input; ordered overrides need no acknowledgement.', json_schema_extra={"writeOnly": True})


class RuleMapCatalogueLine(Schema):
    model_config = ConfigDict(extra='allow')
    binding_value: float | None


class RuleMapEntry(Schema):
    model_config = ConfigDict(extra='allow')
    binding_value: float | None
    lines: list[RuleMapCatalogueLine | PersonAmount]


class RuleMapResponse(Schema):
    model_config = ConfigDict(extra='allow')
    rules: list[RuleMapEntry]


class RuleMapProposal(Schema):
    """The newest department rule proposal waiting behind one rule-map entry."""
    policy_id: ID
    by_name: str | None
    at: datetime
    self_approval_allowed: bool


class RuleExplainedSentence(Schema):
    """One server-owned plain-language sentence for an effective rule line."""
    text: str
    question_group: str
    who: str
    where: str
    when: str
    number: str
    cell: str
    short: str | None = None
    qualified: bool = False
    level: Literal['mandatory', 'advisory']
    weight: Literal['critical', 'high', 'medium', 'low'] | None = None
    safety: bool
    source_line_ids: list[ID] = Field(default_factory=list, max_length=20)


class ExplainedRule(Schema):
    """A rule grouped in the Rules explained view."""
    rule_id: ID
    title: str
    question_group: str
    level: Literal['mandatory', 'advisory']
    safety: bool
    sentences: list[RuleExplainedSentence] = Field(max_length=2000)
    unexplained: bool = False
    catalogue_id: str | None = None
    summary_sentence: str | None = None
    summary_short: str | None = None
    line_ids: list[ID] = Field(default_factory=list, max_length=2000)


class ExplainedConflictReach(Schema):
    sessions_count: int = Field(ge=0, strict=True)


class ExplainedConflict(Schema):
    """Two authored lines that the server found applying together."""
    conflict_id: str = Field(min_length=1, max_length=200)
    sentence: str
    line_ids: list[ID] = Field(min_length=2, max_length=2)
    rule_ids: list[ID] = Field(min_length=2, max_length=2)
    line_refs: list[Annotated[int, Field(ge=1, strict=True)]] = Field(min_length=2, max_length=2)
    reach: ExplainedConflictReach


class ExplainedQuestionGroup(Schema):
    key: str
    letter: str
    label: str
    order: int = Field(ge=0, strict=True)


class ExplainedPeopleAction(Schema):
    action: str
    label: str
    enabled: bool
    disabled_reason: str | None = None
    fix_list_item_id: ID | None = None
    line_id: ID | None = None


class ExplainedPeopleCell(Schema):
    display: str
    cell: str
    short: str | None = None
    qualified: bool = False
    rule_id: ID
    line_id: ID | None = None
    exception_id: ID | None = None
    kind: Literal['line', 'exception'] = 'line'
    actions: list[ExplainedPeopleAction] = Field(default_factory=list, max_length=12)


class ExplainedPeopleColumn(Schema):
    key: str
    label: str
    rule_ids: list[ID] = Field(min_length=1, max_length=200)


class ExplainedPeopleRow(Schema):
    kind: Literal['everyone', 'rank', 'person']
    id: str
    display: str
    rank_id: ID | None = None
    parent: str | None = None
    cells: dict[str, ExplainedPeopleCell] = Field(default_factory=dict)


class ExplainedPeopleTable(Schema):
    columns: list[ExplainedPeopleColumn] = Field(default_factory=list, max_length=200)
    rows: list[ExplainedPeopleRow] = Field(default_factory=list, max_length=10000)


class DepartmentExplainedRule(Schema):
    rule_id: ID
    catalogue_id: str | None = None
    question_group: str
    safety: bool
    level: Literal['mandatory', 'advisory']
    summary_sentence: str
    summary_short: str = ''
    unexplained: bool = False
    line_ids: list[ID] = Field(default_factory=list, max_length=2000)


class DepartmentRulesExplainedResponse(Schema):
    """Department rules at a date, without a rota-specific status."""
    at: date
    status: None = None
    groups: list[ExplainedQuestionGroup] = Field(default_factory=list, max_length=20)
    rules: list[DepartmentExplainedRule] = Field(default_factory=list, max_length=2000)
    conflicts: list[ExplainedConflict] = Field(default_factory=list, max_length=2000)
    people_table: ExplainedPeopleTable | None = None


class RulesExplainedResponse(Schema):
    """Rules that bind a rota, resolved through the checker's active-rule resolver."""
    workspace_id: ID
    who: str | None = None
    rules: list[ExplainedRule] = Field(default_factory=list, max_length=2000)
    conflicts: list[ExplainedConflict] = Field(default_factory=list, max_length=2000)


class RuleLineEffectInput(Schema):
    expected_policy_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    catalogue_version: Literal[1, 2] = 1
    line: RuleLine


class RuleRelation(Schema):
    """How a catalogue line relates to the broader line it may replace."""
    line_id: ID
    line_no: int = Field(ge=1, strict=True)
    overrides: bool
    both_apply: bool | None = None
    reason: Literal['strength_differs', 'settings_differ', 'where_differs', 'safety'] | None = None
    fix: Literal['match_strength', 'match_settings'] | None = None
    default_severity: dict[str, str | None] | None = None
    effective_numbers: dict[str, float | None] | None = None
    default_settings: dict[str, Any] | None = None


class PromoteAdjustmentInput(Schema):
    workspace_id: ID
    adjustment_id: ID


class PromoteAdjustmentSeed(Schema):
    catalogue_id: Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')]
    expected_policy_id: ID
    expected_revision: int = Field(ge=1, strict=True)
    line: RuleLine
    lines: list[RuleLine] = Field(max_length=2000)
    relation: RuleRelation | None = None


class SchedulingFactInput(Decision):
    """A reviewed department-owned child record; ordinary Person updates cannot write it."""
    kind: SchedulingFactKind
    person_id: ID | None = None
    calendar_entry_id: ID | None = None
    catalogue_id: Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')]
    catalogue_version: Literal[1, 2] = 2
    line: RuleLine = Field(json_schema_extra={"bulk": True})
    goal_ref: RuleSourceReference | None = None
    on: bool = Field(default=True, json_schema_extra={"bulk": True})
    replaces_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True, json_schema_extra={"writeOnly": True})

    @model_validator(mode='after')
    def person_data(self, info: ValidationInfo):
        if self.kind != 'protected_time' and self.person_id is None:
            raise ValueError('Personal workload and overtime records need a person')
        if self.kind == 'protected_time' and not (self.person_id or self.calendar_entry_id):
            raise ValueError('Protected time needs a person or calendar entry')
        if (self.replaces_id is None) != (self.expected_revision is None):
            raise ValueError('A replacement needs the active fact id and revision')
        expected = {'workload': {'workload_limit'}, 'target': {'workload_limit', 'fair_share'},
                    'overtime_offer': {'overtime_offers'}, 'protected_time': {'protected_time', 'workload_limit'}}
        if self.catalogue_id not in expected[self.kind]:
            raise ValueError('The scheduling fact kind does not match its catalogue source')
        version = 1 if self.catalogue_id in ('overtime_offers', 'protected_time') else self.catalogue_version
        checked = CatalogueRule.model_validate({'kind': 'catalogue', 'id': self.catalogue_id, 'catalogue_version': version,
                                               'lines': [self.line.model_dump(mode='json')]}, context=info.context)
        self.line = checked.lines[0]
        from .rule_catalogue import catalogue_entry
        validate_catalogue_settings(catalogue_entry(self.catalogue_id, version), self.line.settings)
        if self.catalogue_id == 'fair_share' and self.goal_ref is None:
            raise ValueError('A Fair-share personal target needs its exact source goal')
        if self.kind == 'overtime_offer' and any(n.level == 'mandatory' for n in self.line.numbers.values()):
            raise ValueError('Overtime offers are advisory and grant no safety exemption')
        if self.kind == 'protected_time' and set(self.line.numbers) != {'apply'}:
            raise ValueError('Protected time retains its restriction switch')
        if self.kind == 'workload' and 'apply' in self.line.numbers:
            raise ValueError('A workload fact contains quotas/targets, not a no-duties restriction')
        return self


class FactDecisionInput(PolicyActivationDecision):
    fact_id: ID
    expected_revision: int = Field(ge=1, strict=True)
    predecessor_revision: int | None = Field(default=None, ge=1, strict=True,
        title='Previous approved fact revision',
        description='Exact active predecessor revision when restoring an approved replacement.')


class SchedulingFactApproval(Schema):
    """Pending scheduling facts in the shared Requests list and Home attention sample."""
    id: ID
    revision: int
    source: Literal['scheduling_fact'] = 'scheduling_fact'
    type: Literal['other'] = 'other'
    stage: Literal['waiting', 'approved', 'declined', 'withdrawn'] = 'waiting'
    status: Literal['proposed', 'active', 'declined', 'retired'] = 'proposed'
    kind: SchedulingFactKind
    title: str
    catalogue_id: Annotated[str, StringConstraints(pattern=r'^[a-z_]{1,40}$')]
    person_id: ID | None
    person_name: str | None
    calendar_entry_id: ID | None
    calendar_entry_name: str | None
    proposed_by: ID
    proposer_name: str
    proposed_at: datetime
    decided_at: datetime | None = None
    starts_on: date | None = Field(description='Fact start date, inclusive.')
    ends_on: date | None = Field(description='Fact end date, exclusive; the summary says "before" this date.')
    start_time: Clock | None = Field(description='Fact start time; RuleLine defaults to 00:00.')
    end_time: Clock | None = Field(description='Fact end time; RuleLine defaults to 24:00.')
    weekdays: list[int] = Field(description='Fact weekdays using Monday=0 through Sunday=6.')
    numbers: dict[str, LineNumber] = Field(description='Fact line numbers keyed by catalogue number.')
    measure: WorkloadMeasure | None = Field(default=None, description='Resolved workload measure from the RuleLine.')
    period: WorkloadPeriod | None = Field(default=None, description='Resolved workload counting period from the RuleLine.')
    goal_measure: BalanceGoalMeasure | None = Field(default=None,
        description='Resolved BalanceGoal metric for personal target facts.')
    goal_period: BalanceGoalPeriod | None = Field(default=None,
        description='Resolved BalanceGoal period for personal target facts; dates retain the goal values.')
    link_target: str
    can_decide: bool


class CatalogueConversionDecision(Decision):
    proposal_id: ID
    expected_revision: int = Field(ge=1, strict=True)


class RuleAcknowledgementInput(Schema):
    """Rules › Needs attention: acknowledge issues for the whole department (rule.acknowledge), or take that back
    (rule.unacknowledge), by fingerprint. An acknowledgement holds until the issue's lines change."""
    fingerprints: list[Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]] = Field(min_length=1, max_length=2000)
    note: Annotated[str, StringConstraints(max_length=500)] = ""


class OptionalApprovalDecision(ApprovalNote):
    @model_validator(mode='after')
    def refusal_reason(self):
        if self.decision != 'approve' and len(self.reason) < 3:
            raise ValueError('Enter a meaningful decision reason')
        return self


class LeaveAllowanceLine(Schema):
    leave_type_id: ID
    days: int = Field(ge=0, le=366, strict=True)
    person_id: ID | None = None
    starts_on: date | None = None

    @model_validator(mode='after')
    def dated_person_line(self):
        if self.person_id is not None and self.starts_on is None:
            raise ValueError('Supply a start date for an individual allowance')
        return self


class LeaveBalanceConfiguration(Schema):
    year_start_month: int = Field(default=1, ge=1, le=12, strict=True)
    year_start_day: int = Field(default=1, ge=1, le=31, strict=True)
    carry_over_days: int = Field(default=0, ge=0, le=366, strict=True)
    allowances: list[LeaveAllowanceLine] = Field(default_factory=list, max_length=2000)

    @model_validator(mode='after')
    def valid_calendar_and_lines(self):
        try:
            date(2001, self.year_start_month, self.year_start_day)
        except ValueError:
            raise ValueError('Choose a leave year start that occurs every year') from None
        keys = {(line.leave_type_id, line.person_id, line.starts_on) for line in self.allowances}
        if len(keys) != len(self.allowances):
            raise ValueError('Use one allowance per leave type, person and start date')
        return self


class LeaveBalanceSettingsChange(Decision):
    configuration: LeaveBalanceConfiguration | None


class LeaveBalanceSettings(Schema):
    revision: int | None = None
    configuration: LeaveBalanceConfiguration
    default_starts_on_min: date | None = None


class LeaveBalance(Schema):
    leave_type_id: ID | None
    leave_type_name: str
    starts_on: date
    ends_on: date
    allowance_days: int
    carried_days: int
    used_days: int
    remaining_days: int
    projected_days: int | None = None


class RequestDecision(OptionalApprovalDecision):
    decision: Literal["approve", "reject", "withdraw"]
    balance_confirmation: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None


class RequestDecisionItem(Schema):
    id: ID
    expected_revision: int = Field(ge=1)
    source: Literal["request", "pattern"] = "request"
    balance_confirmation: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None


class RequestDecisionAccess(Schema):
    """Shared decision fields of request and pattern rows in the Requests list and detail."""
    id: ID
    revision: int
    source: Literal["request", "pattern"]
    can_decide: bool


class RequestManyDecision(OptionalApprovalDecision):
    decision: Literal["approve", "reject"]
    items: list[RequestDecisionItem] = Field(min_length=1, max_length=MAX_REQUEST_DECISIONS)

    @field_validator("items")
    @classmethod
    def distinct(cls, value):
        if len({(item.source, item.id) for item in value}) != len(value):
            raise ValueError("Decide each request only once")
        return value


class RequestUndo(Decision):
    batch_id: ID | None = None


class RequestRotaLink(Decision):
    workspace_id: ID | None


class SeriesDay(Schema):
    weekday: int = Field(ge=0, le=6)
    start_time: Clock
    end_time: Clock


class SeriesDefinition(Schema):
    schema_version: Literal[1] = Field(default=1, json_schema_extra={"bulk": True})
    attributes: dict[str, Any] = Field(default_factory=dict, json_schema_extra={"bulk": True})
    timezone: str = Field(default="Asia/Riyadh", json_schema_extra={"bulk": True})
    anchor: date = Field(json_schema_extra={"bulk": True})
    interval_weeks: int = Field(default=1, ge=1, le=12, json_schema_extra={"bulk": True})
    month_occurrence: Literal[-1, 1, 2, 3, 4, 5] | None = Field(default=None, json_schema_extra={"bulk": True})
    days: list[SeriesDay] = Field(min_length=1, max_length=7, json_schema_extra={"bulk": True})
    excluded_dates: list[date] = Field(default_factory=list, max_length=366, json_schema_extra={"bulk": True})
    service_ids: list[ID] = Field(default_factory=list, max_length=12, json_schema_extra={"bulk": True})
    default_person_ids: list[ID] = Field(default_factory=list, max_length=100, json_schema_extra={"bulk": True})
    usual_assignees_fixed: bool = Field(default=True, json_schema_extra={"bulk": True})
    # Retired by D38/DL-009: kept readable for stored series, no longer bulk-editable; sessions stay open.
    close_when_all_usual_unavailable: bool = Field(default=False)

    @model_validator(mode="before")
    @classmethod
    def default_fixed_assignees(cls, value):
        if isinstance(value, dict) and not value.get('default_person_ids') and 'usual_assignees_fixed' not in value:
            return {**value, 'usual_assignees_fixed': False}
        return value

    @model_validator(mode="after")
    def sane(self):
        if self.month_occurrence is not None and self.interval_weeks != 1:
            raise ValueError("Choose either monthly weekday recurrence or alternating weeks")
        if len(set(self.default_person_ids)) != len(self.default_person_ids):
            raise ValueError("Usual assignees must be unique")
        if (self.usual_assignees_fixed or self.close_when_all_usual_unavailable) and not self.default_person_ids:
            raise ValueError('Choose usual assignees before fixing them or closing when all are unavailable')
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown IANA timezone")
        if len(set(d.weekday for d in self.days)) != len(self.days):
            raise ValueError("Use one timing per weekday in this series; compose series for split days")
        if any(d.start_time == "24:00" or d.start_time == d.end_time for d in self.days):
            raise ValueError("Use 00:00 for a start; equal start and end is ambiguous")
        return self


class EventInput(Schema):
    event_type_id: ID
    heading: Name
    description: str = Field(default='', max_length=10000)
    start: datetime
    end: datetime
    format: Literal['in_person', 'online', 'hybrid'] = 'in_person'
    location_id: ID | None = None
    place: str = Field(default='', max_length=500)
    url: SafeHttpURL = Field(default='', max_length=2000)
    organiser_ids: list[ID] = Field(default_factory=list, max_length=500)
    presenter_ids: list[ID] = Field(default_factory=list, max_length=500)
    audience: Audience | None = None
    tag_ids: list[ID] | None = Field(default=None, max_length=500)
    repeat: SeriesDefinition | None = None
    repeat_until: date | None = None
    skip_date_set_id: ID | None = None
    cancelled: bool = False
    max_attendees: int | None = Field(default=None, ge=1)
    also_email: bool | None = None
    email_sender: Literal['smtp', 'microsoft'] | None = None
    email_sender_identity_id: ID | None = None

    @field_validator('start', 'end')
    @classmethod
    def aware_time(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('Event times require an explicit UTC offset')
        return _utc_instant(value)

    @field_validator('organiser_ids', 'presenter_ids', 'tag_ids')
    @classmethod
    def unique_ids(cls, value):
        if value is not None and len(value) != len(set(value)):
            raise ValueError('Choose each record only once')
        return value

    @model_validator(mode='after')
    def valid_event(self):
        if self.end <= self.start:
            raise ValueError('An event must end after it starts')
        if self.format in {'in_person', 'hybrid'} and not (self.location_id or self.place):
            raise ValueError('An in-person or hybrid event needs a location or place')
        if self.format in {'online', 'hybrid'} and not self.url:
            raise ValueError('An online or hybrid event needs a join link')
        if self.repeat is None and self.repeat_until is not None:
            raise ValueError('A recurrence end date needs a repeat definition')
        if self.repeat is not None and self.repeat_until is not None:
            first_day = self.start.astimezone(ZoneInfo(self.repeat.timezone)).date()
            if self.repeat_until <= first_day:
                raise ValueError('The exclusive end date must follow the first event')
        if self.skip_date_set_id is not None:
            raise ValueError('Skipping a date set is unavailable until date sets are enabled')
        return self


class EventOccurrenceEdit(Schema):
    value: EventInput
    reason: Reason


class EventRSVP(Schema):
    response: Literal['going', 'not_going'] | None
    reason: Reason


class EventCounts(AnnouncementCounts):
    going: int
    not_going: int
    responded: int


class EventReceiptRow(AnnouncementReceiptRow):
    content_id: str | None
    event_occurrence_id: str
    response: Literal['going', 'not_going'] | None
    responded_at: datetime | None


class EventReceiptList(Schema):
    receipts: list[EventReceiptRow]
    counts: EventCounts
    page: int
    page_size: int
    total: int


class EventOccurrenceRow(Schema):
    id: str
    revision: int
    created_at: datetime
    department_id: str
    event_id: str
    original_start: datetime
    start: datetime
    end: datetime
    data: dict[str, Any]
    overridden: bool
    cancelled: bool
    event_type: dict[str, Any] | None = None
    audience_summary: str = ''
    counts: EventCounts | None = None
    receipt: dict[str, Any] | None = None


class EventOccurrenceList(Schema):
    occurrences: list[EventOccurrenceRow]


class ScheduleEventRow(Schema):
    """Schedule event layer with viewer audience membership; never contributes duties, cover or workload."""
    id: str
    revision: int
    event_id: str
    event_type_id: str
    heading: str
    description: str
    start: datetime
    end: datetime
    format: Literal['in_person', 'online', 'hybrid']
    location_id: str | None
    place: str
    url: str
    attendance: EventAttendance
    cancelled: bool
    in_audience: bool


class SeriesInput(NamedInput):
    assignment_type_id: ID = Field(json_schema_extra={"bulk": True})
    location_id: ID = Field(json_schema_extra={"bulk": True})
    definition: SeriesDefinition = Field(json_schema_extra={"bulk": True})


class SeriesOverlapAcknowledgement(Schema):
    overlap_acknowledgement: Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')] | None = None


class CatalogueBulk(SeriesOverlapAcknowledgement):
    catalogue: str
    items: list[RevisionRef] = Field(min_length=1, max_length=500)
    active: bool | None = None
    colour: Colour | None = None
    add_tags: list[Tag] = Field(default_factory=list, max_length=8)
    remove_tags: list[Tag] = Field(default_factory=list, max_length=8)
    set: dict[str, Any] = Field(default_factory=dict, max_length=8)
    reason: Reason


class SeriesPreviewItem(Schema):
    value: SeriesInput
    series_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class SeriesPreviewInput(SeriesPreviewItem):
    value: SeriesInput | None = None
    items: list[SeriesPreviewItem] | None = Field(default=None, min_length=1, max_length=5000)

    @model_validator(mode='after')
    def one_pending_state(self):
        if (self.value is None) == (self.items is None):
            raise ValueError('Send a value or pending items')
        if self.items is not None:
            if self.series_id is not None or self.expected_revision is not None:
                raise ValueError('Put each saved series identity and revision on its item')
            ids = [item.series_id for item in self.items if item.series_id is not None]
            if len(ids) != len(set(ids)):
                raise ValueError('Send each saved series once')
        return self


class OverlapCancelAction(Schema):
    label: Literal['Cancel this occurrence', 'Cancel the other occurrence']
    side: Literal['this', 'other']
    kind: Literal['shift.action', 'series.occurrence.cancel']
    target_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    payload: dict[str, Any]


class SeriesOverlapConflict(Schema):
    code: Literal['TIME_OVERLAP', 'UNAVAILABLE', 'EXTERNAL_ROTATION']
    date: date
    start: datetime
    end: datetime
    person_id: ID
    person_name: str
    shift_id: str
    workspace_id: ID | None = None
    series_id: ID | None = None
    occurrence_on: date | None = None
    session_name: str
    clinic_ids: list[ID]
    clinic_names: list[str]
    other_shift_id: str | None = None
    other_workspace_id: ID | None = None
    other_series_id: ID | None = None
    other_occurrence_on: date | None = None
    other_session_name: str | None = None
    other_clinic_ids: list[ID] = Field(default_factory=list)
    other_clinic_names: list[str] = Field(default_factory=list)
    availability_id: str | None = None
    label: str | None = None
    leave: WhyLeave | None = None
    cancel_actions: list[OverlapCancelAction] = Field(default_factory=list)


class SeriesOverlapPreview(Schema):
    occurrences: list[InstantInterval]
    timezone: str
    conflicts: list[SeriesOverlapConflict]
    conflict_token: str


class PublishedAbsenceReport(Schema):
    """A team-safe absence marker; the private report reason never crosses this boundary."""
    request_id: ID
    person_id: ID
    assignment_id: ID
    status: Literal['submitted', 'approved']


class SeriesOverlapPreviewItem(Schema):
    index: int = Field(ge=0)
    series_id: ID | None = None
    occurrences: list[InstantInterval]
    timezone: str
    conflicts: list[SeriesOverlapConflict]


class SeriesOverlapBatchPreview(Schema):
    items: list[SeriesOverlapPreviewItem]
    conflict_token: str


class SessionOverlapFlags(Schema):
    double_booking_conflicts: list[SeriesOverlapConflict] = Field(default_factory=list)
    absence_reports: list[PublishedAbsenceReport] = Field(default_factory=list)


class ScheduleParticipant(ParticipantInput):
    id: ID
    override_reason: Reason | None
    rank: str = ''
    wish_effects: list[dict[str, Any]] = Field(default_factory=list)


class ScheduleSession(InstantInterval):
    id: ID
    revision: int
    assignment_type_id: ID
    type_revision: int
    type_snapshot: dict[str, Any]
    location_id: ID
    service_ids: list[ID]
    participants: list[ScheduleParticipant]
    attributes: dict[str, Any]
    cancelled: bool
    cancellation_reason: Reason | None
    locked: bool
    closed: bool = False
    series_state_frozen: bool = False
    usual_assignee_unavailability: list[dict[str, Any]] = Field(default_factory=list)
    double_booking_conflicts: list[SeriesOverlapConflict] = Field(default_factory=list)
    absence_reports: list[PublishedAbsenceReport] = Field(default_factory=list)


class SeriesOccurrenceCancel(Schema):
    occurrence_on: date
    reason: Reason


class SeriesExpand(Schema):
    workspace_id: ID
    series_id: ID | None = None
    series_ids: list[ID] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def selected_series(self):
        if bool(self.series_id) == bool(self.series_ids):
            raise ValueError("Choose one series or a non-empty list of series")
        return self


class ProgrammeEligibilityInput(Schema):
    programme_id: ID
    person_ids: list[ID] = Field(min_length=1, max_length=100)
    service_ids: list[ID] = Field(min_length=1, max_length=26)
    starts_on: date
    ends_on: date
    reason: Reason

    @model_validator(mode="after")
    def valid_batch(self):
        if self.ends_on <= self.starts_on:
            raise ValueError("End date must follow start date")
        if len(set(self.person_ids)) != len(self.person_ids) or len(set(self.service_ids)) != len(self.service_ids):
            raise ValueError("People and services must be unique")
        return self


class SeriesReconcile(Schema):
    workspace_id: ID
    series_id: ID
    expected_series_revision: int = Field(ge=1)
    from_on: date
    replace_modified: bool = False
    participant_policy: Literal["preserve_offsets", "clear"]
    reason: Reason


class ProgrammeMembershipInput(Schema):
    person_id: ID
    programme_id: ID = Field(json_schema_extra={"bulk": True})
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date = Field(json_schema_extra={"bulk": True})
    # An external rotator: a trainee from another programme (named in home_programme), managed here by the programme lead.
    external: bool = False
    home_programme: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on <= self.starts_on:
            raise ValueError("End must follow start")
        if self.external != bool(self.home_programme):
            raise ValueError("External rotators need a home programme; internal memberships have none")
        return self


class ProgrammeCalendarInput(Schema):
    programme_id: ID = Field(json_schema_extra={"bulk": True})
    name: Name = Field(json_schema_extra={"bulk": True})
    description: str = Field(default='', max_length=2000, json_schema_extra={"bulk": True})
    kind: Literal['academic_block', 'break', 'notice'] = Field(json_schema_extra={"bulk": True})
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date = Field(json_schema_extra={"bulk": True})

    @model_validator(mode='after')
    def bounded_dates(self):
        if not 0 < (self.ends_on - self.starts_on).days <= 366:
            raise ValueError('Use a calendar interval of 1–366 days')
        return self


class MentorshipInput(Schema):
    """A trainee attached to a consultant for a period (shown as an attachment): the consultant supervises the trainee
    whenever both are on a session of these duty types and clinics (empty: all). Required: auto-schedule always pairs
    them and the rota check flags another pairing; preferred: auto-schedule prefers it."""
    person_id: ID
    mentor_id: ID = Field(json_schema_extra={"bulk": True})
    programme_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})
    assignment_type_ids: list[ID] = Field(default_factory=list, max_length=50, json_schema_extra={"bulk": True})
    service_ids: list[ID] = Field(default_factory=list, max_length=100, json_schema_extra={"bulk": True})
    strength: Literal['required', 'preferred'] = Field(default='preferred', json_schema_extra={"bulk": True})

    @model_validator(mode='after')
    def valid_relationship(self):
        if (self.ends_on is not None and self.ends_on <= self.starts_on) or self.person_id == self.mentor_id:
            raise ValueError('Use ordered dates and a different person as consultant')
        for values in (self.assignment_type_ids, self.service_ids):
            if len(values) != len(set(values)):
                raise ValueError('Duty types and clinics must be unique')
        return self


class CohortInput(Schema):
    name: Name = Field(json_schema_extra={"bulk": True})
    programme_id: ID = Field(json_schema_extra={"bulk": True})
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date = Field(json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on <= self.starts_on:
            raise ValueError("End must follow start")
        return self


class RotationInput(Schema):
    """A mentor writes a preferred attachment for the rotation dates and clinic; the retired mentor column is unused."""
    rotation_type_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    person_id: ID
    cohort_id: ID = Field(json_schema_extra={"bulk": True})
    service_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    mentor_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    department_available: bool = Field(default=True, json_schema_extra={"bulk": True})
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date = Field(json_schema_extra={"bulk": True})

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on <= self.starts_on or self.person_id == self.mentor_id:
            raise ValueError("Invalid rotation dates or self-mentorship")
        if not self.service_id and not self.rotation_type_id:
            raise ValueError("Select a rotation type or a service")
        return self


class RoleInput(Schema):
    identity_id: ID
    role: RoleName = Field(json_schema_extra={"bulk": True})
    starts_on: date = Field(json_schema_extra={"bulk": True})
    ends_on: date | None = Field(default=None, json_schema_extra={"bulk": True})
    reason: Reason

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("End must follow start")
        return self


class IdentityInput(Schema):
    issuer: str = Field(min_length=3, max_length=500)
    subject: str = Field(min_length=1, max_length=255)
    display_name: Name
    person_id: ID | None = None
    reason: Reason


Username = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{1,62}$")]
Password = Annotated[str, StringConstraints(min_length=12, max_length=256, strip_whitespace=False)]


class AccountInvite(Schema):
    """Invite someone: the app creates their sign-in (disabled until they accept) and a one-time link."""
    display_name: Name
    email: Email
    username: Username
    person_id: ID | None = None
    roles: list[RoleName] = Field(default_factory=list, max_length=len(ROLE_NAMES))
    reason: Reason


AccountOnboardingFingerprint = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


# rows: one request's ceiling; wave: the most people the admin screen sends in one batch.
ACCOUNT_ONBOARDING_LIMITS = {"rows": 200, "wave": 25}


class AccountOnboardingRow(Schema):
    person_id: ID
    username: Username
    email: Email


class AccountOnboarding(Schema):
    rows: list[AccountOnboardingRow] = Field(min_length=1, max_length=ACCOUNT_ONBOARDING_LIMITS["rows"])
    reason: Reason


class AccountOnboardingApply(AccountOnboarding):
    expected_fingerprint: AccountOnboardingFingerprint


SESSION_IDLE_MINUTES_MIN = 5
SESSION_MAX_HOURS_MIN = 1
SESSION_IDLE_MINUTES_DEFAULT = 480
SESSION_MAX_HOURS_DEFAULT = 24
# One duration ceiling, bounded by datetime's range and PostgreSQL INTEGER minute storage.
SESSION_DURATION_MAX_HOURS = min((datetime.max - datetime.min) // timedelta(hours=1), (2**31 - 1) // 60)
SESSION_LIMITS = {"idle_minutes": {"min": SESSION_IDLE_MINUTES_MIN, "max": SESSION_DURATION_MAX_HOURS * 60},
                  "max_hours": {"min": SESSION_MAX_HOURS_MIN, "max": SESSION_DURATION_MAX_HOURS}}


class SessionSettingsInput(Schema):
    """Numeric overrides use admin minimums; null retains deployment inheritance."""
    idle_minutes: int | None = Field(ge=SESSION_IDLE_MINUTES_MIN, le=SESSION_LIMITS["idle_minutes"]["max"], strict=True)
    max_hours: int | None = Field(ge=SESSION_MAX_HOURS_MIN, le=SESSION_DURATION_MAX_HOURS, strict=True)
    idle_unlimited: bool = Field(default=False, strict=True)
    max_unlimited: bool = Field(default=False, strict=True)
    reason: Reason = "Session settings"

    @model_validator(mode="after")
    def idle_within_max(self):
        if not self.idle_unlimited and not self.max_unlimited and self.idle_minutes is not None \
                and self.max_hours is not None and self.idle_minutes > self.max_hours * 60:
            raise ValueError("The idle timeout cannot be longer than the maximum session length")
        return self


class DeploymentSessionSettingsInput(SessionSettingsInput):
    """Existing operator configuration permits 1–4 minute idle limits; admin overrides still start at five."""
    idle_minutes: int = Field(ge=1, le=SESSION_LIMITS["idle_minutes"]["max"], strict=True)
    max_hours: int = Field(ge=SESSION_MAX_HOURS_MIN, le=SESSION_DURATION_MAX_HOURS, strict=True)


class PasswordSet(Schema):
    password: Password


class PasswordChange(Schema):
    current_password: Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=False)]
    new_password: Password


WorkspaceOrigin = Literal['planner', 'auto', 'import']
RotaExtractionLayout = Literal['people_by_days', 'days_by_people', 'sessions_list', 'other']
RotaExtractionDutyKind = Literal['duty', 'leave', 'off', 'other']
ScoreCheckGapKind = Literal['missing', 'unknown', 'uncertain', 'unreadable']
ScoreCheckEntityKind = Literal['person', 'duty_code', 'clinic', 'coverage', 'cell']
ScoreCheckDecisionAction = Literal['acknowledge', 'add', 'match', 'exclude', 'confirm', 'resolve']
AiProviderKind = Literal['openai_compatible', 'gemini', 'anthropic', 'azure']
AiProviderInputKind = Literal['openai_compatible', 'gemini', 'anthropic', 'azure', 'openai_chat', 'openai_responses']
AI_PROVIDER_KINDS = get_args(AiProviderKind)
AI_REASONING_VALUES = ('none', 'low', 'medium', 'high')
AiReasoning = Literal['none', 'low', 'medium', 'high'] | Annotated[int, Field(ge=1, le=1_000_000, strict=True)]
FileReference = Annotated[str, StringConstraints(min_length=1, max_length=300)]
Sha256 = Annotated[str, StringConstraints(pattern=r'^[a-f0-9]{64}$')]
# Raw uploads: 10 MiB/file, 20 MiB/batch; the JSON upload endpoint allows 28 MiB including base64 and metadata.
BRANDING_IMAGE_FORMATS = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}
BRANDING_IMAGE_LIMITS = MEDIA_LIMITS
SCORE_CHECK_LIMITS = {'files': 20, 'file_bytes': MEDIA_LIMITS['file_bytes'], 'total_file_bytes': 20 * 1024 * 1024,
                      'upload_body_bytes': 28 * 1024 * 1024, 'assignments': 50_000, 'cells': 250_000}
# One browser tab's kept replies (apps/web/src/api.ts keptReads): least recently read dropped
# past max_chars of JSON text or max_paths replies; a reply checked within fresh_ms is shown without asking again.
# Measured 6 Nov 2026 on the VPS: a December rota's whole detail is ~5 M characters; a coordinator's visit of every
# main screen keeps ~12 M.
WEB_KEPT_READS = {'max_chars': 24_000_000, 'max_paths': 160, 'fresh_ms': 5000}


class RotaExtractionFile(Schema):
    file_ref: FileReference
    group: str = Field(default='', max_length=160)
    layout: RotaExtractionLayout
    rows_read: int = Field(ge=0, strict=True)
    confidence: float = Field(ge=0, le=1)
    starts_on: date | None = None
    ends_on: date | None = None


class RotaExtractionPerson(Schema):
    as_written: Name
    file_ref: FileReference
    cell_refs: list[FileReference] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['cells'])
    suggested_person_id: ID | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2000)


class RotaExtractionDutyCode(Schema):
    code: Name
    meaning_as_written: str = Field(default='', max_length=500)
    file_ref: FileReference
    cell_refs: list[FileReference] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['cells'])
    suggested_type_id: ID | None = None
    kind: RotaExtractionDutyKind
    start: Clock | None = None
    end: Clock | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode='after')
    def paired_times(self):
        if (self.start is None) != (self.end is None):
            raise ValueError('Supply both duty-code times or neither')
        return self


class RotaExtractionClinic(Schema):
    as_written: Name
    file_ref: FileReference
    cell_refs: list[FileReference] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['cells'])
    suggested_service_id: ID | None = None
    suggested_location_id: ID | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2000)


class RotaExtractionAssignment(Schema):
    person: Name
    date: date
    file_ref: FileReference
    cell_ref: FileReference
    duty_code: Name
    clinic: str = Field(default='', max_length=160)
    start: Clock | None = None
    end: Clock | None = None
    session: Name | None = None
    team_lead: bool = False
    overtime: bool = False

    @model_validator(mode='after')
    def paired_times(self):
        if (self.start is None) != (self.end is None):
            raise ValueError('Supply both assignment times or neither')
        return self


class RotaExtractionUnparsed(Schema):
    file_ref: FileReference
    cell_ref: FileReference
    value: str = Field(max_length=10_000)
    reason: str = Field(min_length=1, max_length=2000)


class RotaExtractionWarning(Schema):
    file_ref: FileReference
    message: str = Field(min_length=1, max_length=2000)


class RotaExtraction(Schema):
    """Untrusted extraction evidence; catalogue ids, periods and actual file cells are checked by the import service."""
    files: list[RotaExtractionFile] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['files'])
    people: list[RotaExtractionPerson] = Field(default_factory=list, max_length=10_000)
    duty_codes: list[RotaExtractionDutyCode] = Field(default_factory=list, max_length=10_000)
    clinics: list[RotaExtractionClinic] = Field(default_factory=list, max_length=10_000)
    assignments: list[RotaExtractionAssignment] = Field(default_factory=list, max_length=SCORE_CHECK_LIMITS['assignments'])
    unparsed: list[RotaExtractionUnparsed] = Field(default_factory=list, max_length=SCORE_CHECK_LIMITS['cells'])
    warnings: list[RotaExtractionWarning] = Field(default_factory=list, max_length=10_000)


class ScoreCheckUploadFile(Schema):
    file_name: FileReference
    content_base64: str = Field(min_length=4, max_length=4 * ((SCORE_CHECK_LIMITS['file_bytes'] + 2) // 3), repr=False)


class ScoreCheckImportInput(Schema):
    files: list[ScoreCheckUploadFile] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['files'])
    name: Name | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    group: str = Field(default='', max_length=160)
    use_ai: bool = False
    profile_id: ID | None = None
    privacy_acknowledged: bool = False
    confidence_threshold: float = Field(default=0.9, ge=0, le=1)
    manual_time_hours: float | None = Field(default=None, ge=0, le=100_000)
    manual_time_people: int | None = Field(default=None, ge=1, le=10_000, strict=True)

    @model_validator(mode='after')
    def optional_metadata(self):
        if (self.starts_on is None) != (self.ends_on is None):
            raise ValueError('Supply both period dates or neither')
        if self.starts_on and not 0 < (self.ends_on - self.starts_on).days <= 93:
            raise ValueError('Import period must span 1–93 days, with exclusive end')
        if self.use_ai and not self.privacy_acknowledged:
            raise ValueError('Acknowledge where the files will be sent before using AI')
        return self


class ScoreCheckSourceUpdate(Schema):
    source_id: ID
    group: str | None = Field(default=None, max_length=160)
    starts_on: date | None = None
    ends_on: date | None = None
    work_hours_each: float | None = Field(default=None, ge=0, le=100_000)
    made_by: list[ID] | None = Field(default=None, max_length=500)
    people_count_override: int | None = Field(default=None, ge=0, le=10_000, strict=True)
    period_inferred: bool | None = None

    @field_validator('group', 'made_by')
    @classmethod
    def supplied_values_are_not_null(cls, value):
        if value is None:
            raise ValueError('Use an empty group or empty people list to clear the value')
        return value

    @field_validator('period_inferred')
    @classmethod
    def supplied_inference_is_not_null(cls, value):
        if value is None:
            raise ValueError('Choose whether the period is inferred')
        return value

    @model_validator(mode='after')
    def paired_period(self):
        if ('starts_on' in self.model_fields_set) != ('ends_on' in self.model_fields_set) or (
                (self.starts_on is None) != (self.ends_on is None)):
            raise ValueError('Supply both file-period dates or neither')
        if self.starts_on and not 0 < (self.ends_on - self.starts_on).days <= 93:
            raise ValueError('File period must span 1–93 days, with exclusive end')
        return self


class ScoreCheckUpdateInput(Schema):
    batch_id: ID
    name: Name | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    period_inferred: bool | None = None
    confidence_threshold: float | None = Field(default=None, ge=0, le=1)
    manual_time_hours: float | None = Field(default=None, ge=0, le=100_000)
    manual_time_people: int | None = Field(default=None, ge=1, le=10_000, strict=True)
    files: list[ScoreCheckSourceUpdate] = Field(default_factory=list, max_length=SCORE_CHECK_LIMITS['files'])
    reason: str = Field(default='', max_length=500)


class ScoreCheckFileResult(Schema):
    rows_read: int = Field(ge=0)
    assignment_count: int = Field(ge=0)
    leave_count: int = Field(default=0, ge=0)
    off_count: int = Field(default=0, ge=0)
    people_count: int = Field(default=0, ge=0)
    confidence: float = Field(ge=0, le=1)
    layout: RotaExtractionLayout
    warnings: list[str] = Field(default_factory=list)


class ScoreCheckSource(Schema):
    id: ID
    revision: int = Field(ge=1)
    file_name: str
    file_ref: str
    sha256: Sha256
    format: str
    group: str
    starts_on: date | None = None
    ends_on: date | None = None
    work_hours_each: float | None = Field(default=None, ge=0)
    made_by: list[ID] = Field(default_factory=list)
    status: Literal['uploaded', 'reading', 'ready', 'needs_review', 'failed']
    result: ScoreCheckFileResult | None = None
    extraction_job_id: ID | None = None
    error_code: str | None = None


class ScoreCheckGapAddPerson(Schema):
    display_name: Name


class ScoreCheckGapAddService(Schema):
    name: Name
    starts_on: date


ScoreCheckGapReplacement = ScoreCheckGapAddPerson | ScoreCheckGapAddService | RotaExtractionAssignment


class ScoreCheckGapDecisionInfo(Schema):
    id: ID
    action: ScoreCheckDecisionAction
    target_id: ID | None = None
    location_id: ID | None = None
    # Historical decisions retain their original replacement payload; new input is narrowed below.
    replacement: dict[str, Any] | None = None
    by: ID
    at: datetime
    can_undo: bool = True


class ScoreCheckGap(Schema):
    id: str
    kind: ScoreCheckGapKind
    entity_kind: ScoreCheckEntityKind
    source_id: ID | None = None
    file_ref: str | None = None
    cell_refs: list[str] = Field(default_factory=list)
    as_written: str
    suggested_target_id: ID | None = None
    suggested_location_id: ID | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    reason: str
    decision: ScoreCheckGapDecisionInfo | None = None


class ScoreCheckGapDecisionInput(Schema):
    batch_id: ID
    gap_id: str = Field(min_length=1, max_length=300)
    action: ScoreCheckDecisionAction
    target_id: ID | None = None
    location_id: ID | None = None
    replacement: ScoreCheckGapReplacement | None = None
    reason: str = Field(default='', max_length=500)

    @model_validator(mode='after')
    def decision_payload(self):
        if self.action == 'match' and self.target_id is None:
            raise ValueError('Choose an existing match')
        if self.action in {'add', 'resolve'} and not self.replacement:
            raise ValueError('Supply the item to add or the resolved cell')
        if self.action not in {'match', 'confirm'} and self.target_id is not None:
            raise ValueError('A target applies only to a match or confirmation')
        if self.action not in {'add', 'match', 'confirm', 'resolve'} and self.location_id is not None:
            raise ValueError('A location applies only to an item being added, matched or resolved')
        if self.action not in {'add', 'resolve'} and self.replacement is not None:
            raise ValueError('Replacement data applies only to add or resolve')
        return self


class ScoreCheckUndoInput(Schema):
    batch_id: ID
    decision_id: ID
    reason: str = Field(default='', max_length=500)


class ScoreCheckSaveInput(Schema):
    batch_id: ID
    name: Name | None = None
    reason: str = Field(default='', max_length=500)


class ScoreCheckGapCounts(Schema):
    missing: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)
    uncertain: int = Field(default=0, ge=0)
    unreadable: int = Field(default=0, ge=0)
    added: int = Field(default=0, ge=0)
    excluded: int = Field(default=0, ge=0)
    acknowledged: int = Field(default=0, ge=0)
    unresolved: int = Field(default=0, ge=0)


class ScoreCheckBatch(Schema):
    id: ID
    revision: int = Field(ge=1)
    name: str
    status: Literal['uploaded', 'reading', 'check', 'saved', 'failed', 'discarded']
    starts_on: date | None = None
    ends_on: date | None = None
    period_inferred: bool = True
    workspace_id: ID | None = None
    files: list[ScoreCheckSource]
    gaps: list[ScoreCheckGap]
    counts: ScoreCheckGapCounts
    confidence_threshold: float = Field(default=0.9, ge=0, le=1)
    confident_matches: int = Field(default=0, ge=0)
    can_save: bool
    use_ai: bool = False
    profile_id: ID | None = None
    manual_time_hours: float | None = Field(default=None, ge=0)
    manual_time_people: int | None = Field(default=None, ge=1)
    created_at: datetime


class ScoreCheckAutoInput(Schema):
    workspace_id: ID
    name: Name | None = None
    reason: str = Field(default='', max_length=500)


class ScoreCheckBenchmarkInput(Schema):
    workspace_ids: list[ID] = Field(min_length=1, max_length=20)
    reason: str = Field(default='', max_length=500)

    @field_validator('workspace_ids')
    @classmethod
    def distinct_workspaces(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('Compare each draft once')
        return value


class ScoreCheckTime(Schema):
    work_hours: float | None = Field(default=None, ge=0)
    people_count: int | None = Field(default=None, ge=1)
    solver_seconds: float | None = Field(default=None, ge=0)
    review_hours: float | None = Field(default=None, ge=0)
    missing_files: int = Field(default=0, ge=0)
    source: Literal['entered', 'measured', 'estimated', 'mixed', 'not_entered']


class ScoreCheckBenchmarkRota(Schema):
    workspace_id: ID
    revision: int = Field(ge=1)
    name: str
    origin: WorkspaceOrigin
    status: str
    fingerprint: str
    score: ScheduleScore | None
    blocking_count: int = Field(ge=0)
    warnings_count: int = Field(ge=0)
    places_open: int = Field(ge=0)
    fairness: dict[str, Any]
    preferences: dict[str, Any]
    stability: dict[str, Any] | None = None
    findings: list[dict[str, Any]]
    time: ScoreCheckTime | None = None
    import_gaps: ScoreCheckGapCounts | None = None


class ScoreImportTime(Schema):
    work_hours: float = Field(ge=0, le=100_000)
    people: int = Field(ge=1, le=10_000, strict=True)


class ImportSource(Schema):
    id: ID
    file_name: str
    format: Literal['xlsx', 'csv', 'pdf', 'image']
    group: str
    period_start: date | None
    period_end: date | None
    period_inferred: bool = True
    people_count: int = Field(ge=0)
    status: Literal['reading', 'ready', 'needs_check', 'failed']
    unclear_cells: int = Field(ge=0)
    error: str | None


class ScoreImport(Schema):
    id: ID
    revision: int = Field(ge=1)
    saved_workspace_id: ID | None
    sources: list[ImportSource]
    time: ScoreImportTime | None
    profile_id: ID | None
    groups: list[str]


class CheckSuggested(Schema):
    id: ID
    label: str


ImportDecisionKind = Literal['acknowledge', 'add', 'match', 'leave_out', 'confirm', 'skip']


class CheckItemDecision(Schema):
    kind: ImportDecisionKind
    label: str


class ReadingContext(Schema):
    file_ref: FileReference
    cell_ref: FileReference


class CheckItem(Schema):
    id: str
    section: Literal['missing', 'unknown', 'unsure', 'cell']
    kind: Literal['person', 'duty_code', 'clinic', 'session', 'cell']
    as_written: str
    detail: str
    suggested: CheckSuggested | None
    confidence: float | None = Field(ge=0, le=1)
    decision: CheckItemDecision | None
    reading_context: ReadingContext | None = None


class ImportCheck(Schema):
    import_id: ID
    revision: int = Field(ge=1)
    items: list[CheckItem]
    matched: int = Field(ge=0)
    threshold: float = Field(ge=0, le=1)


class ImportMatch(Schema):
    id: str
    kind: Literal['person', 'duty_code', 'clinic']
    as_written: str
    matched: CheckSuggested
    confidence: float = Field(ge=0, le=1)
    source_id: ID
    file_name: str
    file_ref: FileReference
    cell_refs: list[FileReference]
    location_id: ID | None = None


class ImportMatches(Schema):
    import_id: ID
    revision: int = Field(ge=1)
    items: list[ImportMatch]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    has_more: bool


class InternalScoreImportUploadCommand(Schema):
    files: list[ScoreCheckUploadFile] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['files'])
    profile_id: ID | None = None
    profile_revision: int | None = Field(default=None, ge=1, strict=True)
    privacy_acknowledged: bool = False

    @model_validator(mode='after')
    def consented_profile(self):
        if self.profile_id and (self.profile_revision is None or not self.privacy_acknowledged):
            raise ValueError('Choose the current provider revision and acknowledge where files will be sent')
        return self


class InternalScoreImportAddFilesCommand(Schema):
    import_id: ID
    files: list[ScoreCheckUploadFile] = Field(min_length=1, max_length=SCORE_CHECK_LIMITS['files'])
    profile_revision: int | None = Field(default=None, ge=1, strict=True)
    privacy_acknowledged: bool = False


class ScoreImportUpload(InternalScoreImportUploadCommand):
    files: list[Annotated[bytes, Field(min_length=1, max_length=SCORE_CHECK_LIMITS['file_bytes'])]] = Field(
        min_length=1, max_length=SCORE_CHECK_LIMITS['files'])


class ScoreImportAddFiles(Schema):
    files: list[Annotated[bytes, Field(min_length=1, max_length=SCORE_CHECK_LIMITS['file_bytes'])]] = Field(
        min_length=1, max_length=SCORE_CHECK_LIMITS['files'])
    revision: int = Field(ge=1, strict=True)
    profile_revision: int | None = Field(default=None, ge=1, strict=True)
    privacy_acknowledged: bool = False


class ScoreImportUpdateSource(Schema):
    import_id: ID
    source_id: ID
    group: str | None = Field(default=None, max_length=160)
    period_start: date | None = None
    period_end: date | None = None
    period_inferred: bool | None = None
    people_count: int | None = Field(default=None, ge=0, le=10_000, strict=True)

    @field_validator('group')
    @classmethod
    def supplied_group_is_not_null(cls, value):
        if value is None:
            raise ValueError('Use an empty group to clear the value')
        return value

    @model_validator(mode='after')
    def paired_period(self):
        if {'period_start', 'period_end'} <= self.model_fields_set and (
                (self.period_start is None) != (self.period_end is None)):
            raise ValueError('Supply both file-period dates or neither')
        if self.period_start and self.period_end and not 0 < (self.period_end - self.period_start).days <= 93:
            raise ValueError('File period must span 1–93 days, with exclusive end')
        return self


class ScoreImportSourceAction(Schema):
    import_id: ID
    source_id: ID
    reason: str = Field(default='', max_length=500)


class ScoreImportSetTime(Schema):
    import_id: ID
    time: ScoreImportTime | None


class ScoreImportProfile(Schema):
    import_id: ID
    profile_id: ID | None
    profile_revision: int | None = Field(default=None, ge=1, strict=True)
    privacy_acknowledged: bool = False

    @model_validator(mode='after')
    def consented_profile(self):
        if self.profile_id and (self.profile_revision is None or not self.privacy_acknowledged):
            raise ValueError('Choose the current provider revision and acknowledge where files will be sent')
        return self


class ScoreImportAction(Schema):
    import_id: ID
    reason: str = Field(default='', max_length=500)


class ImportDecisionInput(Schema):
    kind: ImportDecisionKind
    target_id: ID | None = None
    location_id: ID | None = None
    value: dict[str, Any] | None = None
    reading: RotaExtractionAssignment | None = None

    @model_validator(mode='after')
    def required_decision_value(self):
        if self.kind == 'add' and not self.value:
            raise ValueError('Supply the new record value to add')
        if self.kind == 'match' and self.target_id is None:
            raise ValueError('Choose an existing match')
        return self


class ScoreImportDecide(Schema):
    import_id: ID
    item_id: str = Field(min_length=1, max_length=300)
    decision: ImportDecisionInput | None
    reason: str = Field(default='', max_length=500)


class ScoreImportSave(Schema):
    import_id: ID
    name: Name | None = None
    reason: str = Field(default='', max_length=500)


class ScoreImportSaved(Schema):
    workspace_id: ID


class BenchmarkPart(Schema):
    label: str
    value: float | None = Field(description='Negative score points lost; null when this part has no data')


class BenchmarkSide(Schema):
    workspace_id: ID
    name: str
    origin: WorkspaceOrigin
    detail: str
    score: float | None = Field(ge=0, le=100)
    publishable: bool
    before_cap: float | None = Field(ge=0, le=100)
    blocking: int = Field(ge=0)
    warnings: int = Field(ge=0)
    not_covered: int = Field(ge=0)
    fairness_gap: float | None = Field(ge=0, description='Gap in the shared fairness measure units')
    within_fair: int | None = Field(ge=0)
    people: int = Field(ge=0)
    wishes_met: float | None = Field(ge=0, le=100, description='Percentage of preference points honoured')
    time_hours: float | None = Field(ge=0)
    time_basis: Literal['entered', 'estimated'] | None
    time_detail: str
    parts: list[BenchmarkPart]
    stability_detail: str = ''


class BenchmarkRule(Schema):
    rule_id: ID
    number: str | None
    name: str
    mandatory: bool
    counts: list[int] = Field(description='Finding counts in the same order as sides')


class BenchmarkFairness(Schema):
    person_id: ID
    name: str
    fair_share: float | None
    counts: list[float | None]
    within: list[bool | None] = Field(default_factory=list)


class BenchmarkGap(Schema):
    shift_id: ID
    name: str
    start: datetime
    open: list[int]


class BenchmarkImportSummary(Schema):
    added: int = Field(ge=0)
    left_out: int = Field(ge=0)
    acknowledged: int = Field(ge=0)


class Benchmark(Schema):
    period_start: date
    period_end: date
    sessions: int = Field(ge=0)
    sides: list[BenchmarkSide]
    rules: list[BenchmarkRule]
    fairness: list[BenchmarkFairness]
    gaps: list[BenchmarkGap]
    gaps_total: int = Field(ge=0)
    import_summary: BenchmarkImportSummary | None
    fairness_unit: str | None = None


class ScoreCheckBenchmarkResult(Schema):
    id: ID
    starts_on: date
    ends_on: date
    created_at: datetime
    baseline_publication_id: ID | None = None
    common_inputs_fingerprint: str
    rotas: list[ScoreCheckBenchmarkRota] = Field(min_length=1, max_length=20)
    frontend: Benchmark | None = None


class AiProviderProfileInput(Schema):
    name: Name
    kind: AiProviderInputKind
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=200)
    reasoning: AiReasoning | Literal['budget'] = 'none'
    thinking_budget: int | None = Field(default=None, ge=1, le=1_000_000, strict=True)
    api_style: Literal['chat', 'responses'] = 'chat'
    api_key: Annotated[str, StringConstraints(min_length=1, max_length=10_000, strip_whitespace=False)] | None = Field(
        default=None, repr=False, exclude=True, json_schema_extra={'writeOnly': True})
    max_tokens: int = Field(default=4096, ge=1, le=1_000_000, strict=True)
    timeout_s: int = Field(default=120, ge=1, le=600, strict=True)
    structured_output: Literal['json_schema', 'json_object'] = 'json_schema'
    allow_private_endpoint: bool = False
    enabled: bool = False
    is_default: bool = False
    reason: str = Field(default='', max_length=500)


class AiProviderKeyInput(Schema):
    profile_id: ID
    api_key: Annotated[str, StringConstraints(min_length=1, max_length=10_000, strip_whitespace=False)] = Field(
        repr=False, json_schema_extra={'writeOnly': True})
    reason: str = Field(default='', max_length=500)


class AiProviderProfileRecord(Schema):
    id: ID
    revision: int = Field(ge=1)
    name: str
    kind: AiProviderKind
    base_url: str
    model: str
    reasoning: Literal['none', 'low', 'medium', 'high', 'budget']
    thinking_budget: int | None = None
    api_style: Literal['chat', 'responses'] = 'chat'
    max_tokens: int
    timeout_s: int
    structured_output: Literal['json_schema', 'json_object']
    allow_private_endpoint: bool
    enabled: bool
    is_default: bool
    key_set: bool
    key_saved_at: datetime | None = None
    created_at: datetime


class ScoreProviderChoice(Schema):
    id: ID
    revision: int = Field(ge=1)
    name: str
    kind: AiProviderKind
    base_url: str = Field(description='Provider host and optional port only, without URL path or settings')
    model: str
    is_default: bool


class ScoreProviderChoices(Schema):
    profiles: list[ScoreProviderChoice]


class AiSettings(Schema):
    enabled: bool
    revision: int = Field(ge=1)
    profiles: list[AiProviderProfileRecord]


class AiEnabledInput(Schema):
    enabled: bool
    reason: str = Field(default='', max_length=500)


class AiCallRecord(Schema):
    id: ID
    profile_id: ID | None = None
    actor_id: ID
    job_id: ID | None = None
    attempt: int = Field(ge=1)
    kind: str
    model: str
    file_sha256: Sha256 | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    result: Literal['pending', 'valid', 'invalid', 'error']
    error_code: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ImportPreview(Schema):
    workspace_id: ID
    source_name: Name
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class ReferenceRotaPreview(Schema):
    workspace_id: ID
    reference_import_id: ID


class ImportRefresh(Schema):
    batch_id: ID
    workspace_id: ID
    reason: Reason


class ImportResolve(Schema):
    row_number: int = Field(ge=2, le=1001)
    assignment_type_id: ID
    location_id: ID
    service_id: ID
    additional_service_ids: list[ID] = Field(default_factory=list, max_length=11)
    person_id: ID

    @model_validator(mode='after')
    def distinct_services(self):
        services = [self.service_id, *self.additional_service_ids]
        if len(services) != len(set(services)):
            raise ValueError('Select each covered clinic only once')
        return self


class ImportApply(Schema):
    batch_id: ID
    workspace_id: ID
    mappings: list[ImportResolve] = Field(min_length=1, max_length=1000)
    reason: Reason


class StaffImportPreview(Schema):
    """A staff spreadsheet (CSV): people, rank, clinics, skills and programme, one person per row."""
    source_name: Name
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class StaffImportReview(Schema):
    batch: dict[str, Any]
    rows: list[dict[str, Any]]
    summary: dict[str, int]
    columns: list[str]
    source_fingerprint: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    message: str | None = None


class StaffAccessBulkInput(Schema):
    """Credentialer: the same dated clinics (service_ids) or skills (capability_ids + evidence) for many people."""
    person_ids: list[ID] = Field(min_length=1, max_length=500)
    service_ids: list[ID] = Field(default_factory=list, max_length=100)
    capability_ids: list[ID] = Field(default_factory=list, max_length=100)
    evidence: str = Field(default="", max_length=500)
    starts_on: date
    ends_on: date | None = None
    reason: Reason

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.person_ids)) != len(self.person_ids) or len(set(self.service_ids)) != len(self.service_ids) \
                or len(set(self.capability_ids)) != len(self.capability_ids):
            raise ValueError("Each person, clinic and skill once")
        if bool(self.service_ids) == bool(self.capability_ids):
            raise ValueError("Choose clinics or skills")
        if self.capability_ids and len(self.evidence.strip()) < 3:
            raise ValueError("Skills need verification evidence")
        if len(self.person_ids) * len(self.service_ids or self.capability_ids) > 5000:
            raise ValueError("Choose fewer people or items per change")
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("End date must follow start date")
        return self


class CreatedUndo(Schema):
    """Remove records this person just created and nobody has changed since (Undo of a bulk grant)."""
    items: list[RevisionRef] = Field(min_length=1, max_length=5000)
    reason: Reason


class LeadContextInput(Schema):
    shift: ShiftInput
    shift_id: ID | None = None
    person_ids: list[ID] = Field(default_factory=list, max_length=5000)


class LeadSource(Schema):
    policy_id: ID | None = None
    revision: int | None = None
    line_id: ID | None = None
    line_index: int | None = None
    slot: str | None = None
    legacy_type_id: ID | None = None
    legacy_type_revision: int | None = None
    ownership_id: ID | None = None


class LeadContextSegment(InstantInterval):
    enabled: bool
    minimum: int = Field(ge=0)
    maximum: int | None = None
    continuity: Literal['whole_duty', 'continuous']
    sources: list[LeadSource]
    contradictory_bounds: bool = False
    empty_pool: bool = False


class LeadContextFailure(InstantInterval):
    code: Literal['LEAD_NOT_QUALIFIED', 'LEAD_NOT_ENABLED']
    sources: list[LeadSource]


class LeadContextPerson(Schema):
    person_id: ID
    can_lead: bool
    failures: list[LeadContextFailure]


class LeadContext(Schema):
    workspace_revision: int
    input_revision: int
    context_hash: str
    segments: list[LeadContextSegment]
    people: list[LeadContextPerson]


class FindingRecitation(Schema):
    """The same obligation outcome now cites another rule/line; line numbers are one-based.

    Null line fields identify a whole-rule citation. finding_key is the substance
    digest, independent of either citation; recitations never add or clear checks.
    """
    finding_key: str
    code: str
    person_id: str | None
    shift_id: str | None
    start: str | None
    end: str | None
    rule_id_before: str | None
    rule_id_after: str | None
    line_id_before: str | None
    line_id_after: str | None
    line_number_before: int | None = Field(ge=1)
    line_number_after: int | None = Field(ge=1)


class ObservedRuleEffect(Schema):
    """Saved-assignment effect, separate from solver feasibility; citations remain auditable."""
    status: Literal['ready', 'unresolved']
    clears: int = Field(ge=0)
    adds: int = Field(ge=0)
    mandatory: dict[str, int]
    advisory: dict[str, int]
    effect: Literal['clears', 'adds', 'no_change']
    summary: str
    recited: list[FindingRecitation] = Field(default_factory=list)


class CatalogueWorkspaceImpact(Schema):
    """Exact before/after substance comparison for catalogue and bulk previews/applies."""
    workspace_id: str
    name: str
    status: str
    before_error_count: int = Field(ge=0)
    after_error_count: int = Field(ge=0)
    before_warning_count: int = Field(ge=0)
    after_warning_count: int = Field(ge=0)
    introduced: list[dict[str, Any]]
    resolved: list[dict[str, Any]]
    recited: list[FindingRecitation] = Field(default_factory=list)
    saved_draft_validation: dict[str, Any] | None = None


class RuleCheckComparison(Schema):
    """Observed finding comparison, separate from a full rota feasibility proof."""
    new_or_worse_findings: list[dict[str, Any]]
    unchanged_existing_count: int = Field(ge=0)
    comparison_complete: bool
    no_new_problems: bool
    recited: list[FindingRecitation] = Field(default_factory=list)


class RuleCheckChange(Schema):
    policy_id: ID
    expected_revision: int = Field(ge=1, strict=True)
    definition: PolicyDefinition | None = None


class FactCheckChange(Schema):
    fact_id: ID
    expected_revision: int = Field(ge=1, strict=True)


class GenerationNotApplied(Schema):
    """An engine limitation, including the precise kinds and partial-support caveats."""
    input: Literal['wishes', 'work_patterns', 'fairness_targets', 'advisory_limits']
    kinds: list[str]
    message: str


class CompetingWish(Schema):
    wish_id: WishOccurrenceID
    person_id: ID
    person_name: str
    kind: WishKind
    weight: float = Field(ge=0)
    daily_weight: float = Field(ge=0)


class WishReason(Schema):
    """Evidence in the final proposal, not a counterfactual optimality proof. Private fields are omitted for staff."""
    day: date
    kind: Literal['rule', 'cover', 'competing_wishes', 'tradeoff']
    message: str
    code: str | None = None
    rule_id: ID | None = None
    rule_name: str | None = None
    shift_id: ID | None = None
    session_label: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    competing_wishes: list[CompetingWish] = Field(default_factory=list)


class DeclinedWish(Schema):
    wish_id: WishOccurrenceID
    person_id: ID
    kind: WishKind
    weight: float = Field(ge=0)
    honoured: float = Field(ge=0, le=1)
    reasons: list[WishReason]


class PublishedWishOutcome(Schema):
    outcome: Literal['granted', 'partly', 'declined'] | None = None
    outcome_reasons: list[WishReason] | None = None


class GenerationApplied(Schema):
    """The suggestions actually applied, indexed against the original stored proposal."""
    addition_indexes: list[int]
    removal_indexes: list[int]
    added_count: int = Field(ge=0)
    removed_count: int = Field(ge=0)


class JobRecovery(Schema):
    """Ephemeral next action projected using the viewer's current permissions."""
    cause_code: str
    action: Literal['review_rules', 'report_problem']
    owner_roles: list[RoleName]
    link_target: str | None = None


class SolverConflictSource(Schema):
    rule_id: str
    line_id: str | None = None
    line: int | None = None


class SolverConflictRequirement(Schema):
    kind: Literal['policy', 'duty']
    name: str
    rule_id: str | None = None
    revision: int | None = None
    line_id: str | None = None
    line: int | None = None
    slot: str | None = None
    sources: list[SolverConflictSource] = Field(default_factory=list)
    rule_kind: str | None = None
    safety: bool
    person_ids: list[str]
    session_ids: list[str]
    starts_on: str | None = None
    ends_on: str | None = None
    start: str | None = None
    end: str | None = None
    comparison: str | None = None
    amount: float | None = None
    metric: str | None = None
    definition_hash: str | None = None
    count_bucket: list[list[str]] | None = None
    shift_id: str | None = None
    service_ids: list[str] = Field(default_factory=list)
    assignment_type_id: str | None = None
    type_revision: int | None = None


class SolverConflictEvidence(Schema):
    required_places: int | None
    maximum_possible: int | None
    complete: bool
    basis: Literal['confirmed_capacity_upper_bound', 'not_established']
    person_ids: list[str]
    session_ids: list[str]
    starts_on: str | None = None
    ends_on: str | None = None
    message: str


class SolverConflictDecision(Schema):
    kind: Literal['open_rule', 'review_eligibility', 'propose_cap_change', 'person_exception', 'leave_open']
    rule_id: str | None = None
    person_id: str | None = None
    line_id: str | None = None
    line: int | None = None
    slot: str | None = None
    starts_on: str | None = None
    ends_on: str | None = None
    service_ids: list[str] = Field(default_factory=list)
    person_ids: list[str]
    session_ids: list[str]
    places: int | None = Field(default=None, ge=1, strict=True)
    requires_approval: bool
    effect_status: Literal['not_checked'] = 'not_checked'


class SolverConflictCause(Schema):
    key: str
    message: str
    requirements: list[SolverConflictRequirement]
    evidence: SolverConflictEvidence
    decisions: list[SolverConflictDecision]
    session_ids: list[str] = Field(default_factory=list)
    starts_on: str | None = None
    ends_on: str | None = None
    service_ids: list[str] = Field(default_factory=list)


class SolverConflictBreak(Schema):
    """One mandatory requirement left broken by an explicitly flagged continuation."""
    rule_id: str | None = None
    session_id: str | None = None
    person_id: str | None = None


class SolverConflictTiming(Schema):
    budget_seconds: float
    elapsed_seconds: float
    stop_reason: Literal['completed', 'allowance_exhausted', 'no_named_set', 'model_error']


class SolverConflictPerson(Schema):
    id: str
    name: str


class SolverConflictSession(SolverConflictPerson):
    start: str
    end: str


class SolverConflictExplanation(Schema):
    code: Literal['CONFLICTING_REQUIREMENTS', 'CONFLICT_EXPLANATION_UNRESOLVED', 'DIAGNOSTIC_MODEL_ERROR']
    strict_status: Literal['INFEASIBLE']
    explanation_status: Literal['INFEASIBLE', 'UNKNOWN', 'MODEL_INVALID']
    implementation_error: bool
    proof_kind: Literal['sufficient_set_with_background', 'unresolved']
    basis: Literal['full_model', 'capacity_relaxation']
    proven: bool
    minimal: Literal[False] = False
    exhaustive: Literal[False] = False
    requirements: list[SolverConflictRequirement]
    people: list[SolverConflictPerson]
    sessions: list[SolverConflictSession]
    input_fingerprint: str
    checked_at: datetime
    elapsed_seconds: float
    solver_status: str
    message: str
    causes: list[SolverConflictCause]
    timing: SolverConflictTiming
    boundary: str


class GenerationOutcome(Schema):
    status: Literal['OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'UNKNOWN']
    candidate: Literal['valid', 'with_gaps', 'none']
    strict_status: Literal['INFEASIBLE'] | None = None
    optimality_proven: bool = False


class GenerationContinuation(Schema):
    """Search-time accounting for a run continued from a saved checkpoint."""
    from_job_id: ID
    previous_search_seconds: float = Field(ge=0, allow_inf_nan=False)
    search_seconds: float = Field(ge=0, allow_inf_nan=False)
    total_search_seconds: float = Field(ge=0, allow_inf_nan=False)


# The words for each choice and each stop reason (GenerationOptions.on_conflict, GenerationSearchStop.reason): the
# client says them as they are written here (tests/test_contracts_generation_stop.py keeps the keys complete).
GENERATION_CONFLICT_CHOICES = {'report': 'Stop and show me', 'continue_flagged': 'Continue with fewest breaks'}
GENERATION_STOP_REASONS = {'optimal': 'proven best for these priorities', 'time_limit': 'search time used up',
    'plateau': 'stopped when it stopped improving', 'gap': 'close enough to the best possible',
    'score': 'target score reached', 'clock': 'stop time reached', 'operator_stop': 'stopped with Stop and keep best',
    'model_proven': 'search finished; checks changed the proposal', 'cancelled': 'cancelled',
    'failed': 'stopped by an error', 'no_search': 'nothing to search',
    'conflict': 'rules conflict'}


class GenerationSearchStop(Schema):
    """The typed reason a finished generation run stopped searching."""
    reason: Literal['optimal', 'model_proven', 'time_limit', 'plateau', 'gap', 'score', 'clock',
                    'operator_stop', 'cancelled', 'failed', 'no_search', 'conflict']
    search_seconds: float = Field(ge=0, allow_inf_nan=False)
    detail: str = ''


class JobResult(BaseModel):
    """Shared job outcome fields; engine-specific proposal and evidence fields are retained."""
    model_config = ConfigDict(extra='allow')
    status: str
    solver_status: Literal['OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'UNKNOWN'] = 'UNKNOWN'
    score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    generation_outcome: GenerationOutcome | None = Field(default=None, exclude_if=lambda value: value is None)
    change_count: int | None = Field(default=None, ge=0, strict=True, exclude_if=lambda value: value is None)
    not_applied: list[GenerationNotApplied] = Field(default_factory=list)
    declined_wishes: list[DeclinedWish] = Field(default_factory=list)
    applied: GenerationApplied | None = None
    reconciliation: ProposalReconciliation | None = None
    conflict: SolverConflictExplanation | None = Field(default=None, exclude_if=lambda value: value is None)
    on_conflict: Literal['report', 'continue_flagged'] = 'report'
    stop_reason: Literal['optimal', 'time_limit', 'plateau', 'target_gap', 'target_score', 'stop_by',
                         'slice_limit', 'operator_stop', 'conflict'] | None = Field(default=None,
                         exclude_if=lambda value: value is None)
    search_stop: GenerationSearchStop | None = Field(default=None, exclude_if=lambda value: value is None)
    breaks: list[SolverConflictBreak] = Field(default_factory=list, max_length=10000)
    continuation: GenerationContinuation | None = Field(default=None, exclude_if=lambda value: value is None)
    resume_available: bool = False
    fix_list_before: FixList | None = None
    fix_list_after: FixList | None = None
    open_places_before: int | None = Field(default=None, ge=0)
    open_places_after: int | None = Field(default=None, ge=0)
    score_delta: float | None = None
    changes_by_cause: list[dict[str, Any]] = Field(default_factory=list)


class AutoscheduleLivePair(Schema):
    shift_id: ID
    person_id: ID


class AutoscheduleLiveEvent(Schema):
    seq: int = Field(ge=1, strict=True)
    at: datetime
    kind: Literal['improvement'] = 'improvement'
    objective: float
    bound: float
    adds: list[AutoscheduleLivePair] = Field(max_length=200)
    removes: list[AutoscheduleLivePair] = Field(max_length=200)
    open_places: int | None = Field(ge=0)
    # Incumbents have not passed shared validation; unknown is never shown as zero warnings.
    warnings: int | None = Field(ge=0)
    truncated: bool

    @model_validator(mode='after')
    def bounded_diff(self):
        if len(self.adds) + len(self.removes) > 200 or self.truncated and (self.adds or self.removes):
            raise ValueError('A truncated solution diff must have empty adds and removes')
        return self


class AutoscheduleLiveSolution(Schema):
    seq: int = Field(ge=1, strict=True)
    pairs: list[AutoscheduleLivePair]


class AutoschedulePrecheckReference(Schema):
    shift_id: ID | None = None
    person_id: ID | None = None
    rule_id: ID | None = None


class AutoschedulePrecheckIssue(Schema):
    classification: Literal['blocking', 'protected', 'outside_scope', 'unresolved']
    code: str = Field(min_length=1, max_length=160)
    count: int = Field(ge=1, strict=True)
    samples: list[AutoschedulePrecheckReference] = Field(default_factory=list,
        max_length=AUTOSCHEDULE_PRECHECK_LIMITS['samples'])


ScoreComponentValue = Annotated[float, Field(ge=0, le=1, strict=True)]


class AutoscheduleBestScoreComponents(Schema):
    """Canonical component values; null means the component does not apply."""
    coverage: ScoreComponentValue | None
    rules: ScoreComponentValue | None
    fairness: ScoreComponentValue | None
    preferences: ScoreComponentValue | None
    stability: ScoreComponentValue | None


class AutoscheduleBestScore(Schema):
    total: float = Field(ge=0, le=100, strict=True,
        description='Canonical uncapped saved-rota score shown in the footer')
    components: AutoscheduleBestScoreComponents


class AutoscheduleProgress(BaseModel):
    """Existing progress remains extensible as solver stages add their own evidence."""
    model_config = ConfigDict(extra='allow', allow_inf_nan=False)
    stage: str
    elapsed_seconds: float = 0
    time_limit_seconds: float | None = None
    first_solution_seconds: float | None = None
    search_elapsed_seconds: float = 0
    best_score: AutoscheduleBestScore | None = None
    precheck_issues: list[AutoschedulePrecheckIssue] = Field(default_factory=list,
        max_length=AUTOSCHEDULE_PRECHECK_LIMITS['issues'])
    events: list[AutoscheduleLiveEvent] = Field(default_factory=list, max_length=50)
    solution: AutoscheduleLiveSolution | None = None
    improvements_found: int = Field(default=0, ge=0, strict=True)


GENERATION_SCOPE_FIELDS = LEAD_FIELDS | {'roles'}
GENERATION_SCOPE_PROTECTIONS = ('keep_existing', 'rebalance_selected')
GENERATION_SCOPE_CAPABILITY = {'fields': sorted(GENERATION_SCOPE_FIELDS),
    'protections': list(GENERATION_SCOPE_PROTECTIONS), 'default_protection': 'keep_existing'}


class GenerationScope(Schema):
    """Job-local population selection; never an eligibility grant or a rule waiver."""
    conditions: Conditions
    protection: Literal[GENERATION_SCOPE_PROTECTIONS] = 'keep_existing'

    @model_validator(mode='after')
    def population(self):
        only_fields(self.conditions, GENERATION_SCOPE_FIELDS, 'Scheduling population')
        return self


PlanningExclusionCode = Literal['PERSON_INACTIVE', 'MEMBERSHIP_MISSING', 'RANK_NOT_ALLOWED',
    'ELIGIBILITY_MISSING', 'ELIGIBILITY_ENDED', 'UNAVAILABLE', 'EXTERNAL_ROTATION', 'RESTRICTION',
    'USUAL_ASSIGNEES_FIXED', 'OVERTIME_NOT_ALLOWED', 'OUTSIDE_GENERATION_POPULATION',
    'CAPABILITY_NOT_GRANTED', 'DUTY_NOT_EDITABLE', 'SUPERVISION_UNAVAILABLE']
GENERATION_SESSION_LIMIT = 1500


class SolverExclusionCount(Schema):
    code: PlanningExclusionCode
    service_id: str | None = None
    rule_id: str | None = None
    people: int = Field(ge=0)


class PlanningContextInput(Schema):
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    session_ids: list[ID] = Field(default_factory=list, max_length=GENERATION_SESSION_LIMIT)
    generation_scope: GenerationScope | None = None

    @field_validator('generation_scope', mode='before')
    @classmethod
    def supported_population(cls, value):
        if value is None:
            return None
        try:
            parsed = GenerationScope.model_validate(value)
            from packages.domain.conditions import absolute_values
            pending = [parsed.conditions]
            while pending:
                for line in pending.pop().conditions:
                    if isinstance(line, Conditions):
                        pending.append(line)
                    elif line.op in {'any', 'all', 'none'} and not absolute_values(line):
                        raise ValueError('Choose at least one population value')
            return parsed
        except (ValidationError, ValueError) as error:
            raise PydanticCustomError('INVALID_SCOPE', 'Choose a supported, nonempty population from this department') from error


class RequirementEvidenceInput(PlanningContextInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class PlanningRecordReference(Schema):
    collection: Literal['memberships', 'eligibilities', 'grants', 'rotations', 'availability']
    id: str
    starts_on: date | None = None
    ends_on: date | None = None
    start: datetime | None = None
    end: datetime | None = None
    service_id: str | None = None
    category_id: str | None = None
    capability_id: str | None = None


class PlanningExclusion(Schema):
    code: PlanningExclusionCode
    service_id: str | None = None
    rule_id: str | None = None
    capability_id: str | None = None
    allowed_category_ids: list[str] = Field(default_factory=list)
    related_records: list[PlanningRecordReference] = Field(default_factory=list)
    owner_roles: list[RoleName] = Field(default_factory=list)
    fix_hints: list[FindingFixHint] = Field(default_factory=list)


class PlanningCandidate(Schema):
    person_id: ID
    start: datetime
    end: datetime
    retained: bool
    exclusions: list[PlanningExclusion]


class ReadinessGroup(SolverExclusionCount):
    assignment_type_id: str
    start: datetime
    end: datetime
    sample_person_ids: list[str]


class PeriodReadiness(Schema):
    workspace_id: ID
    fingerprint: str
    context_fingerprint: str
    scope_window: JobScopeWindow
    generation_scope: GenerationScope | None
    session_ids: list[ID]
    sessions: int = Field(ge=0)
    checked_at: datetime | None
    checked: bool
    complete: Literal[False] = False
    basis: Literal['known_records_not_solver_proof'] = 'known_records_not_solver_proof'
    records_coverage: Literal['assigned_people_only', 'paged_duty_candidates'] = 'assigned_people_only'
    groups: list[ReadinessGroup]
    check_action: Literal['workspace.validate'] = 'workspace.validate'


class RequirementEvidence(PeriodReadiness):
    observation: FindingObservationSummary
    candidates: list[PlanningCandidate]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    has_more: bool
    requirement_scope_complete: Literal[False] = False
    fix_hints: list[FindingFixHint]


class JobInput(Schema):
    automatic: bool = False
    proposed_rule: PolicyInput | None = None
    diagnostic_proposal: bool = False
    rule_changes: list[RuleCheckChange] = Field(default_factory=list, max_length=100)
    fact_changes: list[FactCheckChange] = Field(default_factory=list, max_length=100)
    check_seconds: Literal[10, 60] | None = None
    generation_options: GenerationOptions | None = None
    generation_scope: GenerationScope | None = Field(default=None, exclude_if=lambda value: value is None)
    workspace_id: ID
    kind: Literal["validation", "generation", "feasibility", "fill_options"]
    session_ids: list[ID] = Field(default_factory=list, max_length=GENERATION_SESSION_LIMIT)
    repair_unavailable: bool = False
    repair_conflicts: bool = False
    planning_group_version_id: ID | None = None
    lookahead: Literal['coupled', 'none'] | None = None

    @model_validator(mode='after')
    def contextual_check_options(self):
        if self.kind != 'generation' and (self.planning_group_version_id is not None or 'lookahead' in self.model_fields_set):
            raise ValueError('Group scheduling options require a generation job')
        if self.generation_scope is not None and self.kind != 'generation':
            raise ValueError('Population selection requires a generation job')
        if self.diagnostic_proposal and self.kind != 'generation':
            raise ValueError('Diagnostic proposals require a generation job')
        if self.kind != 'feasibility' and (self.rule_changes or self.fact_changes or self.proposed_rule is not None or self.check_seconds is not None):
            raise ValueError('Rule checks require a feasibility job')
        if self.kind == 'feasibility' and (self.generation_options or self.session_ids or self.repair_unavailable or self.repair_conflicts):
            raise ValueError('A rule check evaluates the full rota without applying repairs')
        if self.proposed_rule is not None and (self.rule_changes or self.fact_changes):
            raise ValueError('Check an unsaved rule separately from saved rule changes')
        if len({c.policy_id for c in self.rule_changes}) != len(self.rule_changes):
            raise ValueError('Check each rule once')
        if len({c.fact_id for c in self.fact_changes}) != len(self.fact_changes):
            raise ValueError('Check each scheduling fact once')
        return self


class GenerationApply(Schema):
    workspace_id: ID
    job_id: ID
    reason: Reason
    # Apply only some suggestions (positions in the proposal's additions / removals); unset applies all.
    addition_indexes: list[int] | None = Field(default=None, max_length=20000)
    removal_indexes: list[int] | None = Field(default=None, max_length=20000)


class TagDefinition(Schema):
    label: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9+!]{1,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=40)]


LABEL_PART_DEFINITIONS = {
    'short': {'label': 'F. Lastname', 'role': 'name'},
    'level': {'label': 'Rank', 'role': 'token', 'source': 'category_ids'},
    'pager': {'label': 'Pager', 'role': 'token', 'source': 'pager', 'requires': 'contact_visibility'},
    'full': {'label': 'Full name', 'role': 'name'},
    'code': {'label': 'Short code', 'role': 'name'},
    'number': {'label': 'Staff number', 'role': 'token', 'source': 'staff_number', 'requires': 'catalogue.department'},
    'first': {'label': 'First name', 'role': 'name'},
    'programme': {'label': 'Programme', 'role': 'token', 'source': 'programme_ids'},
}
LABEL_PARTS = tuple(LABEL_PART_DEFINITIONS)
LABEL_SEPARATORS = {'dot': {'text': ' · ', 'label': '· middle dot'},
                    'comma': {'text': ', ', 'label': ', comma'}, 'space': {'text': ' ', 'label': 'Space'}}
LABEL_HONORIFIC_MODES = {'dr': 'As recorded', 'none': 'No title'}
HONORIFIC_DEFAULTS = ('Dr', 'Prof', 'Mr', 'Mrs', 'Ms', 'د.')
NAME_PARTICLE_DEFAULTS = ('Al', 'ال')
NAME_LABEL_DEFAULTS = {key: {'parts': [key], 'separator': 'dot', 'honorific': 'dr'}
                       for key in ('full', 'short', 'code', 'number')}
PersonPropertyKey = Annotated[str, StringConstraints(pattern=r"^person\.[a-z_]+(:[A-Za-z0-9_-]{1,40})?$")]


class PersonLabel(Schema):
    """How a person is named on the schedule: the parts in order, between them a separator, with or without a title."""
    parts: list[Literal[LABEL_PARTS]] = Field(min_length=1, max_length=len(LABEL_PARTS))
    separator: Literal[tuple(LABEL_SEPARATORS)] = "dot"
    honorific: Literal[tuple(LABEL_HONORIFIC_MODES)] = "dr"

    @field_validator("parts")
    @classmethod
    def parts_once(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Each part once")
        return value


    @model_validator(mode="after")
    def one_name(self, info: ValidationInfo):
        if sum(LABEL_PART_DEFINITIONS[part]['role'] == 'name' for part in self.parts) > 1:
            if (info.context or {}).get('label_write'):
                raise ValueError('Choose at most one name format')
            import warnings
            warnings.warn('Legacy label contains multiple name formats; retained unchanged', UserWarning)
        return self


WHEREABOUTS_LIMITS = {'area_share': [1, 100], 'max_areas': [1, 5]}


class WhereaboutsSettings(Schema):
    """People › Where is everyone (packages/domain/whereabouts.py): a period's "worked in" names the areas with at
    least `area_share` percent of its duties, at most `max_areas` of them, the largest first."""
    area_share: int = Field(default=25, ge=WHEREABOUTS_LIMITS['area_share'][0], le=WHEREABOUTS_LIMITS['area_share'][1])
    max_areas: int = Field(default=2, ge=WHEREABOUTS_LIMITS['max_areas'][0], le=WHEREABOUTS_LIMITS['max_areas'][1])


WHEREABOUTS_DEFAULTS = WhereaboutsSettings().model_dump()


class DisplaySettingsInput(Schema):
    names_default: Literal[tuple(NAME_LABEL_DEFAULTS)] = 'full'
    person_label: PersonLabel | None = None
    properties: list['ViewProperty'] = Field(default_factory=list, max_length=30)
    person_labels_enabled: bool = False
    honorifics: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12)]] = Field(
        default_factory=lambda: list(HONORIFIC_DEFAULTS), max_length=20)
    name_particles: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=12)]] = Field(
        default_factory=lambda: list(NAME_PARTICLE_DEFAULTS), max_length=20)
    ignore_al_prefix: bool = True
    avoid_duplicate_codes: bool = True
    tags: list[TagDefinition] = Field(default_factory=list, max_length=30)
    # The Rules page shows the rule catalogue (owner decision, 19 Nov 2026: the default for new departments).
    rule_catalogue: bool = True
    whereabouts: WhereaboutsSettings = Field(default_factory=WhereaboutsSettings)
    reason: Reason

    @field_validator('tags')
    @classmethod
    def unique_labels(cls, value):
        if len({t.label for t in value}) != len(value):
            raise ValueError('Each tag needs its own short label')
        return value


class GenerationExtension(Decision):
    minutes: int = Field(ge=AUTO_SCHEDULE_LIMITS['minimum_minutes'],
                         le=AUTO_SCHEDULE_LIMITS['maximum_minutes'], strict=True)


class JobTimeInput(GenerationExtension):
    """Backward-compatible command name; GenerationExtension is the shared contract."""


class GenerationContinue(Decision):
    """Continue a saved checkpoint; minutes is optional for legacy callers that choose a new total limit."""
    minutes: int | None = Field(default=None, ge=AUTO_SCHEDULE_LIMITS['minimum_minutes'],
                                le=AUTO_SCHEDULE_LIMITS['maximum_minutes'], strict=True)


class Command(Schema):
    expected_fingerprint: Annotated[str, StringConstraints(pattern=rf'^(?:{ASSIGNMENT_STAMP_PREFIX})?[a-f0-9]{{64}}$')] | None = None
    # Async planner previews bind the eventual write to the durable preview receipt.
    preview_job_id: ID | None = None
    kind: Annotated[str, StringConstraints(min_length=3, max_length=80)]
    target_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict, repr=False)


BRANDING_DEFAULTS = {
    'short_name': 'Rota',
    'app_name': 'ShiftEZ Clinical',
    'institution_name': 'Organisation',
    # Bundle-relative filenames: the client resolves against import.meta.url, HTTP against the built index.
    'emblem_url': 'brand/emblem.png', 'wordmark_url': '',
    'emblem_alt': '',
}
BRANDING_DEFAULTS['wordmark_alt'] = BRANDING_DEFAULTS['app_name']


class BrandingSettings(Schema):
    short_name: Name = BRANDING_DEFAULTS['short_name']
    app_name: Name = BRANDING_DEFAULTS['app_name']
    institution_name: Name = BRANDING_DEFAULTS['institution_name']
    emblem_image_id: ID | None = None
    wordmark_image_id: ID | None = None
    emblem_alt: str = Field(default=BRANDING_DEFAULTS['emblem_alt'], max_length=160)
    wordmark_alt: str = Field(default=BRANDING_DEFAULTS['wordmark_alt'], max_length=160)
    theme_color: str = Field(default='#0f766e', pattern=r'^#[0-9a-fA-F]{6}$')
    background_color: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    icon_background: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    icon_fill: float = Field(default=.94, ge=.1, le=1)
    maskable_fill: float = Field(default=.78, ge=.1, le=.8)


class BrandingPublic(Schema):
    short_name: Name
    app_name: Name
    institution_name: Name
    emblem_url: str
    wordmark_url: str
    emblem_alt: str
    wordmark_alt: str
    revision: int


class BrandingInput(Schema):
    short_name: Name = BRANDING_DEFAULTS['short_name']
    app_name: Name
    institution_name: Name
    emblem_image_id: ID | None = None
    wordmark_image_id: ID | None = None
    emblem_alt: str = Field(default=BRANDING_DEFAULTS['emblem_alt'], max_length=160)
    wordmark_alt: str = Field(default=BRANDING_DEFAULTS['wordmark_alt'], max_length=160)
    theme_color: str = Field(default='#0f766e', pattern=r'^#[0-9a-fA-F]{6}$')
    background_color: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    icon_background: str = Field(default='#ffffff', pattern=r'^#[0-9a-fA-F]{6}$')
    icon_fill: float = Field(default=.94, ge=.1, le=1)
    maskable_fill: float = Field(default=.78, ge=.1, le=.8)
    reason: Reason


class BrandingStatus(Schema):
    branding: BrandingPublic
    settings: BrandingSettings
    can_edit: bool
    can_undo: bool


class BrandingImage(Schema):
    image_id: ID
    url: str
    sha256: str


# PWA transport bounds are protocol/security constraints; operator settings are versioned rows.
PWA_DETAIL_LEVELS = ('generic', 'event', 'event_date')
PwaDetail = Annotated[Literal['generic', 'event', 'event_date'], BeforeValidator(
    lambda value: 'event_date' if value == 'event_and_date' else value)]
PWA_ICON_PROFILE = {'version': 1, 'variants': [('any', 192), ('any', 512), ('maskable', 192), ('maskable', 512), ('apple', 180)]}
PWA_NOTIFICATION_EVENTS = [
    {'id': 'duty_changed', 'label': 'A duty of mine changed', 'audience': audience_definition('duty_changed').description, 'roles': ['staff'], 'required': True},
    {'id': 'absence_reported', 'label': ABSENCE_REPORT_LABEL + " reported", 'audience': audience_definition('absence_reported').description, 'roles': ['coordinator'], 'required': True},
    {'id': 'publication', 'label': 'Rota published', 'audience': audience_definition('publication').description, 'roles': list(ROLE_NAMES), 'required': False},
    {'id': 'review', 'label': 'Rota review', 'audience': audience_definition('review').description, 'roles': ['coordinator', 'approver', 'programme_lead'], 'required': False},
    {'id': 'swap', 'label': 'Swap or give-away', 'audience': audience_definition('swap').description, 'roles': ['staff', 'coordinator', 'approver'], 'required': False},
    {'id': 'request', 'label': 'Request decision', 'audience': audience_definition('request').description, 'roles': ['staff', 'coordinator', 'approver', 'programme_lead'], 'required': False},
    {'id': 'availability', 'label': 'Availability changed', 'audience': audience_definition('availability').description, 'roles': ['staff', 'coordinator', 'approver'], 'required': False},
    {'id': 'rule', 'label': 'Rule proposal or decision', 'audience': audience_definition('rule').description, 'roles': ['coordinator', 'approver'], 'required': False},
    {'id': 'announcement', 'label': 'Announcement', 'audience': audience_definition('announcement').description, 'roles': list(ROLE_NAMES), 'required': False},
    {'id': 'event', 'label': 'Calendar event', 'audience': audience_definition('event').description, 'roles': list(ROLE_NAMES), 'required': False},
    {'id': 'feedback', 'label': 'Feedback updated', 'audience': audience_definition('feedback').description, 'roles': list(ROLE_NAMES), 'required': False},
    {'id': 'generation_finished', 'label': 'Auto-schedule finished', 'audience': audience_definition('generation_finished').description, 'roles': ['coordinator'], 'required': False, 'default_push': 'off'},
    {'id': 'credential_impact', 'label': 'Eligibility review needed', 'audience': audience_definition('credential_impact').description, 'roles': ['staff', 'coordinator', 'approver', 'credentialer'], 'required': False},
    {'id': 'bulk_review', 'label': 'Bulk review needed', 'audience': audience_definition('bulk_review').description, 'roles': list(ROLE_NAMES), 'required': False},
    {'id': 'self_approval', 'label': 'Self-approval used', 'audience': audience_definition('self_approval').description, 'roles': ['coordinator', 'approver', 'auditor'], 'required': False},
]
PWA_EVENT_IDS = tuple(event['id'] for event in PWA_NOTIFICATION_EVENTS)


PublicationAudienceReason = Literal['on_rota', 'coordinator', 'approver', 'has_wish']
# One label table for the Publish dialog's groups, in display order (Design Lead DL-036); clients never type these.
PUBLICATION_AUDIENCE_REASON_LABELS: dict[PublicationAudienceReason, str] = {
    'on_rota': 'On the rota', 'coordinator': 'Coordinators', 'approver': 'Approvers', 'has_wish': 'Has a wish'}
assert tuple(PUBLICATION_AUDIENCE_REASON_LABELS) == get_args(PublicationAudienceReason)


class NotificationAudienceRecipient(Schema):
    """One visible in-app recipient; hidden recipients are represented by counts."""
    identity_id: ID
    display_name: str | None = None
    reasons: list[PublicationAudienceReason] = Field(min_length=1, max_length=len(PUBLICATION_AUDIENCE_REASON_LABELS))
    channels: list[Literal['in_app']] = Field(min_length=1, max_length=1)


class NotificationAudienceGroup(Schema):
    reason: PublicationAudienceReason
    count: int = Field(ge=0)
    visible_count: int = Field(ge=0)
    hidden_count: int = Field(ge=0)


class PublicationAudiencePreview(Schema):
    workspace_id: ID
    kind: Literal['publication'] = 'publication'
    description: str = Field(max_length=400)
    delivery: Literal['in_app'] = 'in_app'
    count: int = Field(ge=0)
    visible_count: int = Field(ge=0)
    hidden_count: int = Field(ge=0)
    recipients: list[NotificationAudienceRecipient]
    groups: list[NotificationAudienceGroup] = Field(max_length=len(PUBLICATION_AUDIENCE_REASON_LABELS))


class PwaIcon(Schema):
    src: str
    sizes: str
    type: Literal['image/png'] = 'image/png'
    purpose: Literal['any', 'maskable'] = 'any'


class PwaManifest(Schema):
    id: str = '/'
    name: Name
    short_name: Name
    start_url: str = '/'
    scope: str = '/'
    display: Literal['standalone'] = 'standalone'
    theme_color: str = Field(pattern=r'^#[0-9a-fA-F]{6}$')
    background_color: str = Field(pattern=r'^#[0-9a-fA-F]{6}$')
    icons: list[PwaIcon] = Field(min_length=1, max_length=8)


class PwaUnreadSnapshot(Schema):
    count: int = Field(ge=0)
    revision: int = Field(ge=0)
    scope: Literal['department'] = 'department'
    department_id: ID


class PwaInstallationSummary(Schema):
    id: ID
    revision: int = Field(ge=1)
    label: str
    platform: str
    state: Literal['enabled', 'revoked', 'repair_required']
    state_reason: str
    last_reconciled_at: datetime | None = None
    last_accepted_at: datetime | None = None
    subscription_id: ID | None = None
    key_id: str | None = None


class PwaEventChannels(Schema):
    push: Literal['inherit', 'on', 'off'] = 'inherit'
    email: Literal['inherit', 'on', 'off'] = 'inherit'
    urgency: Literal['very-low', 'low', 'normal', 'high'] = 'normal'


class PwaRetrySettings(Schema):
    max_attempts: int = Field(default=5, ge=1, le=100)
    base_seconds: int = Field(default=30, ge=1, le=86400)
    max_seconds: int = Field(default=3600, ge=1, le=2592000)

    @model_validator(mode='after')
    def increasing_backoff(self):
        if self.max_seconds < self.base_seconds:
            raise ValueError('Maximum retry delay must be at least the initial delay')
        return self


class PwaDeliverySettings(Schema):
    ttl_seconds: int = Field(default=86400, ge=0, le=2592000)
    batch_size: int = Field(default=50, ge=1, le=1000)
    concurrency: int = Field(default=4, ge=1, le=16)


class NotificationPolicy(Schema):
    push_enabled: bool = False
    lock_screen_detail: PwaDetail = 'generic'
    event_defaults: dict[str, PwaEventChannels] = Field(default_factory=dict, max_length=len(PWA_EVENT_IDS), validate_default=True)
    retry: PwaRetrySettings = Field(default_factory=PwaRetrySettings)
    delivery: PwaDeliverySettings = Field(default_factory=PwaDeliverySettings)
    generic_message: Literal['You have a rota update. Open the app to see it.'] = 'You have a rota update. Open the app to see it.'
    test_message: Literal['This is your requested test alert.'] = 'This is your requested test alert.'

    @field_validator('event_defaults')
    @classmethod
    def known_policy_events(cls, value):
        if set(value) - set(PWA_EVENT_IDS):
            raise ValueError('Unknown notification event')
        for event in PWA_NOTIFICATION_EVENTS:
            if event['required'] and event['id'] in value and value[event['id']].push == 'off':
                raise ValueError('Required alerts cannot be disabled; device consent remains necessary')
            if 'default_push' in event:
                channels = value.get(event['id'], PwaEventChannels())
                if channels.push == 'inherit':
                    value[event['id']] = channels.model_copy(update={'push': event['default_push']})
        return value


class NotificationPolicyInput(NotificationPolicy):
    department_id: ID
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Reason


class NotificationPreferences(Schema):
    event_overrides: dict[str, PwaEventChannels] = Field(default_factory=dict, max_length=len(PWA_EVENT_IDS))
    detail: PwaDetail | None = None

    @field_validator('event_overrides', mode='before')
    @classmethod
    def push_only_compatibility(cls, value):
        if isinstance(value, dict):
            return {key: {'push': channel} if isinstance(channel, str) else channel for key, channel in value.items()}
        return value

    @field_validator('event_overrides')
    @classmethod
    def known_events(cls, value):
        return NotificationPolicy.known_policy_events(value)


class NotificationPreferencesInput(NotificationPreferences):
    department_id: ID
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Reason


class PwaRollbackInput(Schema):
    department_id: ID
    version_id: ID
    reason: Reason


class PwaSystemSettings(Schema):
    enabled: bool = False
    provider_hosts: list[str] = Field(default_factory=lambda: ['fcm.googleapis.com', 'updates.push.services.mozilla.com'], max_length=100)
    provider_suffixes: list[str] = Field(default_factory=lambda: ['push.apple.com'], max_length=100)
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    lease_seconds: int = Field(default=120, ge=1, le=3600)
    max_inflight: int = Field(default=8, ge=1, le=32)
    batch_size: int = Field(default=50, ge=1, le=1000)
    jwt_lifetime_seconds: int = Field(default=43200, ge=7200, le=86400)
    jwt_refresh_margin_seconds: int = Field(default=3600, ge=0, le=82800)
    test_interval_seconds: int = Field(default=60, ge=1, le=86400)

    @model_validator(mode='after')
    def safe_transport(self):
        if self.lease_seconds <= self.timeout_seconds * 2:
            raise ValueError('The lease must exceed twice the network timeout')
        if self.jwt_lifetime_seconds - self.jwt_refresh_margin_seconds < 3600:
            raise ValueError('VAPID authorisations must be reused for at least one hour')
        for host in [*self.provider_hosts, *self.provider_suffixes]:
            if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '.' not in host or '..' in host:
                raise ValueError('Use fully qualified provider DNS hostnames')
        return self


class PwaSystemInput(PwaSystemSettings):
    reason: Reason


class PwaProviderResumeInput(Schema):
    origin: str = Field(max_length=255)
    key_id: str = Field(min_length=1, max_length=80)
    reason: Reason


class PushKeysInput(Schema):
    p256dh: str = Field(min_length=87, max_length=100, repr=False)
    auth: str = Field(min_length=22, max_length=48, repr=False)


class PushSubscribeInput(Schema):
    endpoint: str = Field(min_length=10, max_length=8192, repr=False)
    keys: PushKeysInput = Field(repr=False)
    expiration_time: datetime | None = None
    content_encoding: Literal['aes128gcm'] = 'aes128gcm'
    installation_hint: str = Field(min_length=8, max_length=128)
    label: str = Field(default='', max_length=80)
    platform: str = Field(default='', max_length=40)
    explicit_consent: Literal[True]
    key_id: str = Field(min_length=1, max_length=80)
    department_id: ID | None = None

    @field_validator('expiration_time')
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('Use a timezone-aware expiration time')
        return value


class PushRotateInput(PushSubscribeInput):
    installation_id: ID
    expected_revision: int = Field(ge=1)


class PushUnsubscribeInput(Schema):
    installation_id: ID
    expected_revision: int | None = Field(default=None, ge=1)
    reason: Reason


class PushTestInput(Schema):
    installation_id: ID
    reason: Reason


class PwaStatus(Schema):
    enabled: bool
    policy_revision: int = Field(ge=1)
    email_configured: bool
    email_available: bool
    readiness: Literal['disabled_by_department', 'disabled_by_system', 'not_configured', 'ready']
    public_key: str | None = None
    key_id: str | None = None
    policy: dict[str, Any]
    installations: list[PwaInstallationSummary] = Field(default_factory=list)
    has_more: bool = False
    preferences: dict[str, Any]
    unread: PwaUnreadSnapshot


class AuthConfig(Schema):
    environment: Literal["development", "test", "production"]
    deployment: str
    password_login: bool
    development_login: bool = Field(description="Deprecated alias of password_login for older sign-in clients")
    institutional_login: bool
    mode: Literal["password_only", "institutional_only", "both"] = "password_only"


class IdentifierInput(Schema):
    identifier: str = Field(min_length=1, max_length=255)


class IdentifierResponse(Schema):
    method: Literal["password", "institutional"]


def valid_oidc_host(value):
    labels = value.split(".")
    if (len(value) > 253 or len(labels) < 2 or labels[-1] == "localhost"
            or not re.match(r"[a-z]", labels[-1])
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        return False
    return True


class AuthInstitutionalBinding(Schema):
    identity_id: ID
    issuer: str = Field(default="", max_length=500)
    subject: str = Field(min_length=1, max_length=255)


class AuthSettings(Schema):
    mode: Literal["password_only", "institutional_only", "both"] = "password_only"
    remember_me_days: int = Field(default=30, ge=1, le=90)
    institutional_enabled: bool = False
    issuer: str = Field(default="", max_length=500)
    discovery_url: str = Field(default="", max_length=1000)
    client_id: str = Field(default="", max_length=255)
    scopes: list[Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:._/-]{1,100}$")]] = Field(
        default_factory=lambda: ["openid", "profile", "email"], min_length=1, max_length=30)
    username_claim: str = Field(default="preferred_username", pattern=r"^[A-Za-z0-9_:.\-]{1,100}$")
    email_claim: str = Field(default="email", pattern=r"^[A-Za-z0-9_:.\-]{1,100}$")
    groups_claim: str = Field(default="groups", pattern=r"^[A-Za-z0-9_:.\-]{1,100}$")
    allowed_domains: list[str] = Field(default_factory=list, max_length=100)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=30)
    institutional_bindings: list[AuthInstitutionalBinding] = Field(default_factory=list, max_length=10000)

    @field_validator("allowed_domains", "allowed_hosts")
    @classmethod
    def host_names(cls, values, info: ValidationInfo):
        normalized = [v.strip().lower() for v in values]
        if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", v)
               or ".." in v for v in normalized):
            raise ValueError("Use exact DNS names without a scheme, path or wildcard")
        if info.field_name == "allowed_hosts" and any(not valid_oidc_host(value) for value in normalized):
            raise ValueError("Identity provider hosts must use fully qualified public DNS names, without IP literals")
        return sorted(set(normalized))

    @model_validator(mode="after")
    def provider_configuration(self):
        from urllib.parse import urlparse
        for value in (self.issuer, self.discovery_url):
            if value:
                parsed = urlparse(value)
                try:
                    port = parsed.port
                except ValueError:
                    raise ValueError("Identity provider URL has an invalid port") from None
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                        or parsed.password is not None or parsed.fragment or parsed.query
                        or re.search(r"[\s\\\x00-\x1f]", value) or port == 0):
                    raise ValueError("Identity provider URLs must use HTTPS without credentials, query or fragment")
        if self.issuer.endswith("/"):
            raise ValueError("Issuer must match discovery exactly and have no trailing slash")
        if self.mode != "password_only" and not self.institutional_enabled:
            raise ValueError("Enable institutional sign-in before selecting this mode")
        if self.institutional_enabled:
            if not self.issuer or not self.client_id or "openid" not in self.scopes:
                raise ValueError("Institutional sign-in requires an issuer, client ID and openid scope")
            if any(urlparse(v).hostname not in self.allowed_hosts for v in (self.issuer, self.discovery_url) if v):
                raise ValueError("Allow the issuer and discovery HTTPS hosts explicitly")
        bindings = self.institutional_bindings
        if (len({v.identity_id for v in bindings}) != len(bindings)
                or len({(v.issuer, v.subject) for v in bindings}) != len(bindings)):
            raise ValueError("Each identity and institutional subject may be bound only once")
        return self


class AuthConfigChange(Schema):
    expected_revision: int = Field(ge=0)
    confirmed: Literal[True]
    reason: Reason


class AuthConfigInput(AuthSettings, AuthConfigChange):
    client_secret: Annotated[str, StringConstraints(max_length=4096, strip_whitespace=False)] | None = Field(
        default=None, repr=False, description="Write-only: null preserves the secret; empty clears it")


class AuthConfigStatus(AuthSettings):
    revision: int = Field(ge=0)
    redirect_uri: str
    client_secret_set: bool
    key_configured: bool
    can_undo: bool


class AuthConnectionTest(Schema):
    expected_revision: int = Field(ge=0)


class AuthConnectionResult(Schema):
    ok: bool
    revision: int = Field(ge=0)
    message: str


class LoginResponse(Schema):
    expires_at: datetime | None
    display_name: str


class OIDCContinuation(Schema):
    continuation: str = Field(min_length=1, max_length=500)


class OIDCDepartmentChoice(OIDCContinuation):
    department_id: ID


class OIDCDepartment(Schema):
    id: ID
    name: str


class OIDCDepartmentChoices(Schema):
    expires_at: datetime
    departments: list[OIDCDepartment]


class OIDCDepartmentSession(LoginResponse):
    return_to: str


class LoginInput(Schema):
    username: str = Field(min_length=1, max_length=255)
    password: Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=False)]
    department_id: ID | None = None
    remember_me: bool = False


RotationBlockUnit = Literal["weeks", "months"]
ROTATION_BLOCK_UNITS = get_args(RotationBlockUnit)
MAX_ROTATIONS_PER_PLAN = 200 * 52
# shortcut: 10,400 rotations in the recent/future window; paginate when it exceeds one maximum plan.
MAX_CATALOGUE_ROTATIONS = MAX_ROTATIONS_PER_PLAN


class RotationBoardChoices(Schema):
    rotation_types: dict[ID, str]
    services: dict[ID, str]


class RotationBoard(Schema):
    start: date
    end: date
    names: dict[Literal["programmes", "cohorts", "rotation_types", "services"], dict[ID, str]]
    choices: RotationBoardChoices
    people: list[dict[str, Any]]
    memberships: list[dict[str, Any]]
    rotations: list[dict[str, Any]]
    leave: list[dict[str, Any]]
    changes: list[dict[str, Any]]
    can_plan: bool
    can_note: bool
    note_person_ids: list[ID]


class RotationBlock(Schema):
    """One block of a rotation plan: what the trainee rotates through and for how long."""
    rotation_type_id: ID | None = None
    service_id: ID | None = None
    length: int = Field(default=4, ge=1, le=52)
    unit: RotationBlockUnit = "weeks"
    department_available: bool = True

    @model_validator(mode="after")
    def defined(self):
        if not self.rotation_type_id and not self.service_id:
            raise ValueError("Choose a rotation type or a service for each block")
        return self


class RotationTemplateInput(Schema):
    name: Name
    programme_id: ID | None = None
    description: str = Field(default="", max_length=2000)
    blocks: list[RotationBlock] = Field(min_length=1, max_length=52)
    active: bool = True


class RotationPlanInput(Schema):
    """Create rotations for trainees from a start date, block after block. With `stagger`, trainee n starts at block n
    (round-robin), so the same blocks are not all taken at once. People who already have a rotation overlapping a new
    block keep it; that block is skipped and reported."""
    cohort_id: ID
    person_ids: list[ID] = Field(min_length=1, max_length=200)
    starts_on: date
    blocks: list[RotationBlock] = Field(min_length=1, max_length=52)
    template_id: ID | None = None
    stagger: bool = False
    reason: Reason


class RotationCopyInput(Schema):
    """Recreate: copy the rotations that start in [starts_on, ends_on) forward by `months` (for the same people)."""
    starts_on: date
    ends_on: date
    months: int = Field(default=12, ge=1, le=60)
    cohort_id: ID | None = None
    person_ids: list[ID] = Field(default_factory=list, max_length=200)
    programme_id: ID | None = None
    reason: Reason

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on <= self.starts_on or (self.ends_on - self.starts_on).days > 731:
            raise ValueError("Choose a period of up to two years")
        return self


class RotationRemoveInput(Schema):
    items: list[RevisionRef] = Field(min_length=1, max_length=MAX_ROTATIONS_PER_PLAN)
    reason: Reason


class RotationChangeInput(Schema):
    """A trainee asks to change one of their rotations (type, service, dates); a programme lead or approver decides."""
    rotation_id: ID
    rotation_type_id: ID | None = None
    service_id: ID | None = None
    starts_on: date
    ends_on: date
    private_reason: str = Field(min_length=3, max_length=1000)

    @model_validator(mode="after")
    def valid_dates(self):
        if self.ends_on <= self.starts_on:
            raise ValueError("End must follow start")
        return self


class RotationSwapInput(Schema):
    rotation_id: ID
    expected_revision: int = Field(ge=1)
    other_rotation_id: ID
    other_expected_revision: int = Field(ge=1)
    reason: Reason

    @model_validator(mode="after")
    def distinct_rotations(self):
        if self.rotation_id == self.other_rotation_id:
            raise ValueError("Choose two different rotations")
        return self


class TraineeNoteInput(Schema):
    person_id: ID
    body: str = Field(min_length=1, max_length=4000)


class TraineeNoteEdit(Schema):
    body: str = Field(min_length=1, max_length=4000)


class TimelineConflict(Schema):
    """A finding from the shared validator; message is its user-facing reason."""
    code: str
    message: str
    severity: Literal['error', 'warning']
    person_id: ID | None = None
    shift_id: ID | None = None
    start: datetime | None = None
    end: datetime | None = None
    rule_id: ID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    outside_rota: bool = False
    window: dict[Literal['start', 'end'], datetime] | None = None
    cause: FindingCause | None = None
    fix_hints: list[FindingFixHint] = Field(default_factory=list)


class CatalogueConflictDetails(Schema):
    """Catalogue saves expose distinct conflict kinds; conflicts remains the legacy alias."""
    overlap_conflicts: list[SeriesOverlapConflict] = Field(default_factory=list)
    rotation_leave_conflicts: list[TimelineConflict] = Field(default_factory=list)
    conflicts: list[SeriesOverlapConflict] | list[TimelineConflict] = Field(default_factory=list)
    conflict_token: str | None = None
    warnings: list[str] = Field(default_factory=list)


class TimelineLayer(Schema):
    kind: str
    label: str
    style_key: str
    count: int = Field(ge=0)
    source: str
    requested: bool = True


class TimelineItem(Schema):
    id: str
    kind: str
    source: str
    label: str
    starts_on: date
    ends_on: date | None
    start: datetime | None = None
    end: datetime | None = None
    status: str
    revision: int | None = Field(default=None, ge=1)
    details: dict[str, Any] = Field(default_factory=dict)


class TimelineDutyDay(Schema):
    day: date
    count: int = Field(ge=0)


class TimelineArea(Schema):
    id: ID
    label: str
    short_name: str
    colour: str | None = None


class TimelineAreaTotal(Schema):
    id: ID
    label: str
    short_name: str
    count: int = Field(ge=0)


class TimelineClinic(Schema):
    id: ID
    label: str
    count: int = Field(ge=0)


class TimelineSegment(Schema):
    id: str
    starts_on: date
    ends_on: date
    kind: Literal['outside', 'leave', 'academic', 'protected', 'away', 'work', 'home']
    label: str
    short_label: str
    source: Literal['published', 'draft', 'usual']
    reason: Literal['not_started', 'left', 'gap'] | None = None
    rota: str | None = None
    # Every rota the stretch spans, in order (`rota` is the first): one stretch crosses rotas that read the same.
    rotas: list[str] = Field(default_factory=list, max_length=800)  # at most one rota a day over a two-year window
    areas: list[TimelineArea] = Field(default_factory=list)
    clinics: list[TimelineClinic] = Field(default_factory=list)
    leave_type_id: ID | None = None
    leave_type: str | None = None
    leave_short_name: str | None = None
    leave_colour: str | None = None
    status: str | None = None
    rotation_type_id: ID | None = None


class TimelinePerson(Schema):
    id: ID
    display_name: str
    short_code: str
    group: str | None = None
    rota_eligible: bool | None = None
    items: list[TimelineItem]
    duty_density: list[TimelineDutyDay]
    areas: list[TimelineAreaTotal] | None = None
    segments: list[TimelineSegment] | None = None
    conflicts: list[TimelineConflict]
    notes: int = Field(default=0, ge=0)


class RotaEligibility(Schema):
    workspace_id: ID
    sessions: int = Field(ge=0)
    basis: Literal['qualified'] = 'qualified'


class TimelineResponse(Schema):
    start: date
    end: date
    timezone: str
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    has_more: bool
    group_by: Literal['none', 'programme', 'rank', 'cohort']
    sort_by: Literal['name', 'code']
    sort_direction: Literal['asc', 'desc']
    layers: list[TimelineLayer]
    people: list[TimelinePerson]
    rota_eligibility: RotaEligibility | None = None
    names: dict[str, dict[str, str]]
    can_plan: bool
    choices: RotationBoardChoices = Field(default_factory=lambda: RotationBoardChoices(rotation_types={}, services={}))
    can_note: bool = False
    note_person_ids: list[ID] = Field(default_factory=list)


class TimelineRotationInput(Schema):
    """The identical payload for preview and timeline.rotation. All date ends are exclusive."""
    action: Literal['create', 'move', 'resize', 'copy', 'template']
    person_id: ID
    rotation_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    starts_on: date
    ends_on: date | None = None
    rotation_type_id: ID | None = None
    service_id: ID | None = None
    cohort_id: ID | None = None
    mentor_id: ID | None = None
    department_available: bool = True
    template_id: ID | None = None
    reason: Reason

    @model_validator(mode='after')
    def valid_action(self):
        if self.ends_on is not None and self.ends_on <= self.starts_on:
            raise ValueError('End must follow start (end is exclusive)')
        if self.action in {'move', 'resize', 'copy'} and (not self.rotation_id or self.expected_revision is None):
            raise ValueError('Choose a source rotation and its current revision')
        if self.action in {'create', 'resize'} and self.ends_on is None:
            raise ValueError('Choose an end date')
        if self.action == 'create' and (not self.cohort_id or not (self.rotation_type_id or self.service_id)):
            raise ValueError('Choose a cohort and a rotation type or service')
        if self.action == 'template' and (not self.template_id or not self.cohort_id):
            raise ValueError('Choose a template and cohort')
        if self.action == 'template' and self.model_fields_set & {
                'ends_on', 'rotation_type_id', 'service_id', 'mentor_id', 'department_available'}:
            raise ValueError('A template uses its saved block definitions and durations')
        if self.action in {'create', 'template'} and (self.rotation_id or self.expected_revision is not None):
            raise ValueError('New rotations do not take a source rotation or revision')
        if self.action != 'template' and self.template_id is not None:
            raise ValueError('Only a template action takes a template')
        return self


class TimelinePreview(Schema):
    action: Literal['create', 'move', 'resize', 'copy', 'template']
    rotations: list[RotationInput]
    conflicts: list[TimelineConflict]
    requires_approval: bool
    can_apply: bool


class TimelineRotationResult(Schema):
    action: Literal['create', 'move', 'resize', 'copy', 'template']
    status: Literal['applied', 'submitted']
    created: list[RotationRecord]
    records: list[RotationRecord]
    previous_records: list[RotationRecord] = Field(default_factory=list)
    request: dict[str, Any] | None = None
    undo: dict[str, Any]
    conflicts: list[TimelineConflict]
    message: str


class ContactVisibility(Schema):
    mode: Literal['everyone', 'roles', 'self_and_admins'] = 'self_and_admins'
    roles: list[RoleName] = Field(default_factory=list, max_length=len(ROLE_NAMES))


class ContactFormat(Schema):
    pattern: str | None = Field(default=None, max_length=500)
    max_length: int = Field(default=254, ge=1, le=254)

    @field_validator('pattern')
    @classmethod
    def safe_pattern(cls, value):
        if value is not None:
            try:
                TypeAdapter(Annotated[str, StringConstraints(pattern=value)])
            except Exception as exc:
                raise ValueError('Use a supported regular expression (no look-around or backreferences)') from exc
        return value


class ContactDial(Schema):
    line: str = Field(default='', max_length=40, pattern=r'^$|^\+[1-9][0-9]{1,14}$')
    join: Literal['wait', 'pause'] = 'wait'


class ContactTypeInput(EffectiveInput):
    name: Name
    kind: Literal['phone', 'pager', 'extension', 'email']
    icon: str = Field(default='', max_length=40)
    sort_order: int = Field(default=1000, ge=0, le=100000)
    required: bool = False
    one_tap: bool = Field(default=False,
                          description='Accepted for compatibility; CONTACT_KIND_ACTIONS and action_off control card actions.')
    action_off: bool = False
    dial: ContactDial = Field(default_factory=ContactDial)
    visibility: ContactVisibility = Field(default_factory=ContactVisibility)
    format: ContactFormat = Field(default_factory=ContactFormat)
    person_may_change: bool = False
    edited_by: Literal['admins', 'person_and_admins'] = 'admins'


class PersonContactInput(Schema):
    person_id: ID
    contact_type_id: ID
    value: str = Field(max_length=254)
    visibility_override: ContactVisibility | None = None
    reason: Reason


class VisibleContact(Schema):
    id: ID | None = None
    revision: int | None = None
    contact_type_id: ID
    name: str
    kind: str
    icon: str
    sort_order: int
    value: str
    dial_target: str | None = None
    can_edit: bool
    legacy_field: str | None = None


class PersonCardDuty(Schema):
    id: ID
    start: datetime
    end: datetime
    publication_id: ID | None = None
    workspace_id: ID
    status: str
    duty_type: str
    location: str
    services: list[str]
    team: list[dict]


class PersonCard(Schema):
    person: dict
    contacts: list[VisibleContact]
    today: list[PersonCardDuty]
    coming_up: list[PersonCardDuty]
    permitted_sections: list[str]
    actions: list[str]
    workload: PersonWorkload | None = None
    fairness: PersonFairness | None = None
    availability: PersonAvailability | None = None
    can_work: dict | None = None
    rules: list[dict] | None = None


# What a contact kind does beside Copy reviewed: one mapping for the card's buttons and the server's dial
# targets. A type's own setting can only switch the action off; None means Copy only. Keys are ContactTypeInput.kind.
CONTACT_KIND_ACTIONS: dict[str, Literal['call', 'email'] | None] = {'phone': 'call', 'pager': 'call', 'extension': 'call', 'email': 'email'}


# Initial rows only; saved department configuration owns all subsequent behavior (D68).
DEFAULT_CONTACT_TYPES = (
    {'id': 'f0136000-0000-4000-8000-000000000001', 'name': 'Mobile', 'kind': 'phone', 'legacy_field': 'work_phone', 'sort_order': 20,
     'visibility': {'mode': 'everyone', 'roles': []}, 'one_tap': True, 'action_off': False},
    {'id': 'f0136000-0000-4000-8000-000000000002', 'name': 'Extension', 'kind': 'extension', 'legacy_field': 'work_extension', 'sort_order': 30,
     'visibility': {'mode': 'roles', 'roles': ['admin', 'coordinator', 'approver']}, 'one_tap': False, 'action_off': False},
    {'id': 'f0136000-0000-4000-8000-000000000003', 'name': 'MCD pager', 'kind': 'pager', 'legacy_field': 'pager', 'sort_order': 10,
     'visibility': {'mode': 'everyone', 'roles': []}, 'one_tap': False, 'action_off': False},
    {'id': 'f0136000-0000-4000-8000-000000000004', 'name': 'Work email', 'kind': 'email', 'legacy_field': 'work_email', 'sort_order': 40,
     'visibility': {'mode': 'roles', 'roles': ['admin', 'coordinator', 'approver']}, 'one_tap': True, 'action_off': False},
)


CATALOGUE_SCHEMAS = {
    "contact_types": ContactTypeInput,
    "date_sets": DateSetInput,
    "leave_types": LeaveTypeInput,
    "department_content": DepartmentContentInput,
    "event_types": EventTypeInput, "events": EventInput,
    "operating_calendars": OperatingCalendarInput, "resource_pools": ResourcePoolInput,
    "categories": CategoryInput, "capabilities": NamedInput, "location_types": NamedInput,
    "locations": LocationInput, "services": ServiceInput, "people": PersonInput, "tags": TagInput,
    "assignment_types": AssignmentTypeInput, "programmes": NamedInput, "rotation_types": RotationTypeInput,
    "programme_memberships": ProgrammeMembershipInput,
    "mentorships": MentorshipInput,
    "programme_calendar_entries": ProgrammeCalendarInput,
    "cohorts": CohortInput, "rotations": RotationInput, "recurring_series": SeriesInput,
    "rotation_templates": RotationTemplateInput,
}


# Review roles are controlled application capabilities, never clinical categories.
ReviewerRole = Literal["approver", "programme_lead"]
StepKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]


class ReviewStep(Schema):
    key: StepKey
    label: Name
    role: ReviewerRole = "approver"
    approvals_required: int = Field(default=1, ge=1, le=MAX_APPROVALS_REQUIRED)
    deadline_hours: int = Field(default=48, ge=1, le=168)
    escalation_role: ReviewerRole | None = None

    @model_validator(mode="after")
    def meaningful_escalation(self):
        if self.escalation_role == self.role:
            raise ValueError("Escalation must designate a different review role")
        return self


class ReviewRouteDefinition(Schema):
    schema_version: Literal[1] = 1
    steps: list[ReviewStep] = Field(min_length=1, max_length=5)
    urgent_steps: list[ReviewStep] | None = Field(default=None, min_length=1, max_length=5)

    @model_validator(mode="after")
    def unique_keys(self):
        keys = [step.key for step in self.steps + (self.urgent_steps or [])]
        if len(keys) != len(set(keys)):
            raise ValueError("Each routine and urgent step needs its own unique key")
        return self


class ReviewRouteInput(Schema):
    name: Name
    purpose: Literal["rota", "swap"] = "rota"
    starts_on: date
    ends_on: date | None = None
    definition: ReviewRouteDefinition
    reason: Reason

    @model_validator(mode="after")
    def dates(self):
        if self.ends_on and self.ends_on <= self.starts_on:
            raise ValueError("Route end is exclusive and must follow start")
        return self


class ReviewDelegationInput(Schema):
    route_id: ID
    step_key: StepKey
    delegate_id: ID
    starts_at: datetime
    expires_at: datetime
    reason: Reason

    @field_validator("starts_at", "expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Delegation instants require an explicit UTC offset")
        return _utc_instant(value)

    @model_validator(mode="after")
    def bounded(self):
        duration = (self.expires_at - self.starts_at).total_seconds()
        if not 0 < duration <= 14 * 86400:
            raise ValueError("Delegation must be bounded to at most fourteen days")
        return self


class RotaItemDecision(Schema):
    item_id: str
    kind: Literal['adjustment', 'gap']
    accept: bool
    reason: Reason | None = None

    @model_validator(mode='after')
    def rejection_reason(self):
        if not self.accept and not self.reason:
            raise ValueError('Give a reason for rejecting this item')
        return self


class ReviewDecisionInput(ApprovalNote):
    delegation_id: ID | None = None
    decisions: list[RotaItemDecision] = Field(default_factory=list)
    rejected_review_item_ids: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def one_decision_form(self):
        if self.decisions and 'rejected_review_item_ids' in self.model_fields_set:
            raise ValueError('Choose complete decisions or rejected item IDs')
        if len(set(self.rejected_review_item_ids)) != len(self.rejected_review_item_ids):
            raise ValueError('Choose each rejected item once')
        return self


class SwapBlockingFinding(Schema):
    """A mandatory validator finding safe for the two staff members to read."""
    code: str
    message: str


class SwapEligibility(Schema):
    eligible: bool
    message: str | None = None
    findings: list[SwapBlockingFinding] = Field(default_factory=list)


class SwapNote(Schema):
    # A step both people agree to needs no typed reason: the message is optional and stored empty when left out.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ''


class SwapOfferInput(Schema):
    kind: SwapKind = "exchange"
    publication_id: ID
    assignment_id: ID
    recipient_person_id: ID | None = None
    expires_at: datetime
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ''

    @field_validator("expires_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Consent expiry requires an explicit UTC offset")
        return _utc_instant(value)


class SwapCounterofferInput(SwapNote):
    assignment_id: ID


class SwapPrepareInput(Decision):
    urgency: Literal["routine", "urgent"] = "routine"


class PlannerViewDefinition(Schema):
    schema_version: Literal[1] = 1
    mode: Literal["week", "month", "person", "list"] = "week"
    search: str = Field(default="", max_length=120)
    location_id: ID | None = None
    assignment_type_id: ID | None = None
    person_id: ID | None = None


VIEW_KEY_MAX_LENGTH = 40
VIEW_KEY_MEMBER_MAX_LENGTH = 40
PERSON_VIEW_KEY_MAX_LENGTH = VIEW_KEY_MAX_LENGTH + 1 + VIEW_KEY_MEMBER_MAX_LENGTH
_PERSON_VIEW_KEY_RE = re.compile(rf'person\.([a-z_]+)(?::[A-Za-z0-9_-]{{1,{VIEW_KEY_MEMBER_MAX_LENGTH}}})?')


def person_view_key_field(value):
    match = _PERSON_VIEW_KEY_RE.fullmatch(value)
    return match.group(1) if match else None


def is_current_person_view_key(value):
    field = person_view_key_field(value)
    return field is not None and len(value) <= PERSON_VIEW_KEY_MAX_LENGTH and field in PERSON_PROPERTY_DEFINITIONS


def known_view_key(value):
    field = person_view_key_field(value)
    if field is not None:
        if not is_current_person_view_key(value):
            raise ValueError("Unknown person property" if field not in PERSON_PROPERTY_DEFINITIONS
                             else "Person view key is too long")
        return value  # Retain unknown registry members in saved views for forward compatibility.
    if ('.' in value and not any(value in collection['related_columns'] for collection in BULK_COLLECTIONS.values())
            and condition_field_definition(value) is None):
        raise ValueError("Unknown related column")
    return value


ViewKey = Annotated[str, StringConstraints(
    pattern=rf"^(?:[a-z0-9_.-]{{1,{VIEW_KEY_MAX_LENGTH}}}|person\.[a-z_]{{1,{VIEW_KEY_MAX_LENGTH - len('person.')}}}(?::[A-Za-z0-9_-]{{1,{VIEW_KEY_MEMBER_MAX_LENGTH}}})?)$"),
    AfterValidator(known_view_key)]
ViewValue = Annotated[str, StringConstraints(max_length=160)]
VIEW_CHOICE_LIMIT = 50  # Existing saved-view choice bound, shared by display selections.
CollectionFilters = Annotated[dict[ViewKey, Annotated[list[ViewValue], Field(max_length=VIEW_CHOICE_LIMIT)]], Field(max_length=20)]
ViewScope = Literal["personal", "department"]
PLANNER_DIMENSIONS = (
    {"key": "type", "label": "Duty type", "one": "duty type", "many": "duty types", "person": False, "empty": "No duty type"},
    {"key": "service", "label": "Service", "one": "service", "many": "services", "person": False, "empty": "No service"},
    {"key": "location", "label": "Location", "one": "location", "many": "locations", "person": False, "empty": "No location"},
    {"key": "time", "label": "Duty time", "one": "duty time", "many": "duty times", "person": False, "empty": "No time"},
    {"key": "person", "label": "Person", "one": "person", "many": "people", "person": True, "empty": "No one"},
    {"key": "rank", "label": "Rank", "one": "rank", "many": "ranks", "person": True, "empty": "No rank"},
    {"key": "programme", "label": "Programme", "one": "programme", "many": "programmes", "person": True, "empty": "No programme"},
    {"key": "tag", "label": "Tag", "one": "tag", "many": "tags", "person": True, "empty": "No tag"},
)
PLANNER_TIME_AXES = (("day", "Days"), ("week", "Weeks"), ("weekday", "Weekdays"), ("month", "Months"))
PlannerDimension = Literal[tuple(item["key"] for item in PLANNER_DIMENSIONS)]
PlannerRow = Literal[tuple(item["key"] for item in PLANNER_DIMENSIONS) + ("none",)]
ViewSortDirection = Literal["asc", "desc"]
VIEW_SORT_DIRECTIONS = {"asc": "Ascending", "desc": "Descending"}
PLANNER_SORT_LABELS = {"time": "Time", "type": "Duty type", "place": "Place", "hours": "Hours, most first", "code": "Short code"}
PLANNER_PEOPLE_SORT_LABELS = {"hours_desc": "Most hours first", "hours_asc": "Fewest hours first", "name": "Name A–Z", "name_desc": "Name Z–A"}
PLANNER_COLOUR_LABELS = {"type": "Duty type", "service": "Service", "role": "Rank", "person": "Person", "tag": "Tag"}
PLANNER_COLOUR_TAG_LIMIT = VIEW_CHOICE_LIMIT
PLANNER_PERSON_DISPLAYS = {"person": "Matched person", "team": "Recorded supervision team", "duty": "Everyone on matching duties"}
PLANNER_PERSON_DISPLAY_DEFAULT = "duty"
# Week/Month: a supervision team shows this many trainee rows, then "+N more" reviewed.
PLANNER_TEAM_ROWS_DEFAULT = 3
PLANNER_TEAM_ROWS_LIMIT = VIEW_CHOICE_LIMIT
PLANNER_WIDTH_KEYS = ("row_heading", "date", "cell")
# Reuse the collection column-count bound; widths have no department-specific pixel ceiling.
ViewWidths = Annotated[dict[ViewKey, Annotated[float, Field(gt=0, strict=True)]], Field(max_length=30)]


VIEW_PROPERTIES = ("label", "time", "lead", "overtime", "supervisor", "open", "rule", "code", "location",
                   "duty", "service", "hours", "places", "tags", "people", "leave")
VIEW_PROPERTIES_HIDDEN_BY_DEFAULT = ("code", "duty", "service", "hours", "places", "tags", "people", "leave")
VIEW_PROPERTY_DEFINITIONS = {
    "label": {"label": "Person label", "forms": ("text",)},
    "time": {"label": "Time", "forms": ("text", "chip")},
    "lead": {"label": "Team lead (TL)", "forms": ("chip", "icon", "text")},
    "overtime": {"label": "Overtime (OT)", "forms": ("chip", "icon", "text")},
    # Supervision teams (D78): a tree (one guide line) or a box; the first form is the built-in default. Older views saved
    # "text"/"icon" open as the first form (the web normaliser), so no migration.
    "supervisor": {"label": "Supervision teams", "forms": ("tree", "box")},
    "open": {"label": "Open places", "forms": ("chip", "count")},
    # One issue marker for every problem (D78): on/off for could-improve marks only; must-fix marks always show (AGENTS §2).
    "rule": {"label": "Rule marks", "forms": ("icon",)},
    "code": {"label": "Duty code", "forms": ("chip", "text")},
    "location": {"label": "Location", "forms": ("text", "chip")},
    "duty": {"label": "Duty name", "forms": ("text", "chip")},
    "service": {"label": "Service", "forms": ("text", "chip")},
    "hours": {"label": "Hours", "forms": ("text", "chip")},
    "places": {"label": "Places filled", "forms": ("text", "chip")},
    "tags": {"label": "Person tags", "forms": ("chip", "text")},
    # The people on a session, named by the view's Person label, where a cell is a session (grid rows that are not people).
    "people": {"label": "People", "forms": ("text",)},
    # Leave and wishes on a person's day cells where a grid has them (people rows by day); People × Days keeps its Layers switch.
    "leave": {"label": "Leave and wishes", "forms": ("text",)},
}
# One item renderer draws a session or a person's duty in every Schedule layout (apps/web/src/schedule_item.ts); each kind
# of item shows these properties until the view chooses its own (a view's own list then applies in every layout).
# card: the Week's session cards (time, place, service and hours on their row); pill: a person in a Month day cell (no row
# says the session's facts, so they start hidden); chip: one grid cell (People × Days, the Timeline's codes); bar: the
# Day timeline; agenda: the phone list. The card's are the registry's defaults (all but VIEW_PROPERTIES_HIDDEN_BY_DEFAULT).
VIEW_ITEM_SHOWN_BY_DEFAULT = {
    "card": tuple(k for k in VIEW_PROPERTIES if k not in VIEW_PROPERTIES_HIDDEN_BY_DEFAULT),
    "pill": ("label", "lead", "overtime", "supervisor", "open", "rule"),
    "chip": ("label", "code", "lead", "overtime", "supervisor", "open", "rule"),
    "bar": ("label", "time", "duty", "people", "lead", "supervisor", "open", "rule"),
    "agenda": ("label", "time", "duty", "location", "people", "lead", "supervisor", "open", "rule"),
}


class ViewProperty(Schema):
    """One thing drawn on each session card, in the view's order; `form` is how it is drawn (chip, icon, text, count)."""
    key: Literal[VIEW_PROPERTIES] | PersonPropertyKey
    on: bool = True
    form: Literal["chip", "icon", "text", "count", "tree", "box"] = "chip"

    @model_validator(mode="after")
    def default_visibility(self):
        if "on" not in self.model_fields_set and (self.key in VIEW_PROPERTIES_HIDDEN_BY_DEFAULT or self.key.startswith("person.")):
            self.on = False
        return self




class ViewTimeWindow(Schema):
    start: Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
    end: Annotated[str, StringConstraints(pattern=r"^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")]

    @model_validator(mode="after")
    def ordered(self):
        if self.end <= self.start:
            raise ValueError("The time window must end after it starts")
        return self


class PlannerLayers(Schema):
    draft: bool = Field(default=True, strict=True)
    leave: bool = Field(default=True, strict=True)
    mine: bool = Field(default=False, strict=True)
    mode: Literal['edit', 'side', 'overlay'] = 'edit'


PLANNER_LAYER_DEFAULTS = PlannerLayers().model_dump()


class CoverageAxes(Schema):
    """Coverage's own Rows and Columns (D80): a layout never inherits another layout's axes. Absent is Coverage's default
    (duty types over days)."""
    rows: PlannerRow | None = Field(default=None, exclude_if=lambda value: value is None)
    columns: Literal["day", "week", "weekday", "month", "type", "service", "location", "time", "rank", "programme", "tag", "person"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def distinct(self):
        if self.rows is not None and self.rows == self.columns:
            raise ValueError("Coverage rows and columns must be different dimensions")
        return self


class PlannerViewSettings(PlannerViewDefinition):
    """What a saved planner view keeps: its layout (mode), how many days it shows (range), its filters and display.

    "week" is the calendar layout (duty rows × day columns) for any range; older views saved as "month"
    open as the calendar layout over a month. A view without a range uses its layout's default. Every display
    field is optional: a view saved before it existed opens with the default."""
    search_options: SearchOptions | None = Field(default=None, exclude_if=lambda value: value is None)
    mode: Literal["week", "month", "person", "list", "coverage", "year", "timeline"] = "week"
    range: Literal["day", "3days", "week", "2weeks", "month", "custom", "rota"] | None = None
    days: int | None = Field(default=None, ge=1, le=62)  # the length of a custom range
    group_by: Literal["service", "type", "location", "time", "none", "rank", "programme", "person", "tag", "team"] = "service"
    rows: PlannerRow | None = Field(default=None, exclude_if=lambda value: value is None)
    columns: Literal["day", "week", "weekday", "month", "type", "service", "location", "time", "rank", "programme", "tag", "person"] | None = Field(default=None, exclude_if=lambda value: value is None)
    subgroup: PlannerRow | None = Field(default=None, exclude_if=lambda value: value is None)
    cells: Literal["cards", "codes", "counts"] | None = Field(default=None, exclude_if=lambda value: value is None)
    collapsed: list[ViewValue] | None = Field(default=None, max_length=200, exclude_if=lambda value: value is None)
    filters: CollectionFilters | None = Field(default=None, exclude_if=lambda value: value is None)
    # Display projection only; omission keeps every member of a matching duty visible.
    person_display: Literal[tuple(PLANNER_PERSON_DISPLAYS)] | None = Field(default=None, exclude_if=lambda value: value is None)
    group_sort: list[tuple[PlannerDimension, ViewSortDirection]] | None = Field(
        default=None, max_length=len(PLANNER_DIMENSIONS), exclude_if=lambda value: not value)
    widths: ViewWidths | None = Field(default=None, exclude_if=lambda value: not value)
    sort_by: Literal["time", "type", "place", "hours", "code"] = "time"  # hours, code: Timeline person order (default: name)
    # People ordering is independent of the calendar/Timeline sort; absence keeps older views unchanged.
    people_sort: Literal["hours_desc", "hours_asc", "name", "name_desc"] | None = Field(default=None, exclude_if=lambda value: value is None)
    colour_by: Literal[tuple(PLANNER_COLOUR_LABELS)] = "type"
    # Ordered tag IDs explicitly chosen by the viewer; first matching tag wins, no match is neutral.
    # Names/colours/membership remain catalogue-owned, so a rename never changes this selection.
    colour_tags: list[ID] | None = Field(default=None, max_length=PLANNER_COLOUR_TAG_LIMIT,
                                        exclude_if=lambda value: not value)
    compact: bool = False  # older views; `density` wins when both are saved
    density: Literal["compact", "comfortable"] | None = None
    # Trainee rows a supervision team shows in Week and Month before "+N more"; none: PLANNER_TEAM_ROWS_DEFAULT.
    team_rows: int | None = Field(default=None, ge=1, le=PLANNER_TEAM_ROWS_LIMIT, strict=True, exclude_if=lambda value: value is None)
    # Staffing numbers (places filled / needed under each day, group totals, a grid's Staffed / needed row): absent shows
    # them (older views unchanged); false hides them for this view and its exports.
    staffing: bool | None = Field(default=None, strict=True, exclude_if=lambda value: value is None)
    # Coverage's own axes; `rows`/`columns` above are the calendar's. A view saved before this field read its Coverage
    # axes from rows/columns when its mode was coverage, and still does (the web normaliser).
    coverage: CoverageAxes | None = Field(default=None, exclude_if=lambda value: value is None)
    only_open: bool = False
    only_issues: bool = False
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(default=None, min_length=1, max_length=7)  # Monday 0 … Sunday 6
    week_start: int | None = Field(default=None, ge=0, le=6)  # none: the department's week start
    time_window: ViewTimeWindow | None = None
    properties: list[ViewProperty] | None = Field(default=None, max_length=len(VIEW_PROPERTIES))
    label: PersonLabel | None = None
    layers: PlannerLayers | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def distinct_groups(self):
        dimensions = [value for value in (self.rows, self.columns, self.group_by, self.subgroup)
                      if value not in (None, "none", "day", "week", "weekday", "month")]
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("Rows, categorical columns, group and subgroup must be different dimensions")
        ordered = [key for key, direction in self.group_sort or []]
        if len(ordered) != len(set(ordered)) or any(key not in (self.group_by, self.subgroup) for key in ordered):
            raise ValueError("Group sort must name each active grouping dimension at most once")
        if self.widths and self.widths.keys() - set(PLANNER_WIDTH_KEYS):
            raise ValueError("Unknown schedule width key")
        return self

    @field_validator("weekdays", "properties", "colour_tags")
    @classmethod
    def each_once(cls, value):
        keys = [v.key if isinstance(v, ViewProperty) else v for v in value or []]
        if len(set(keys)) != len(keys):
            raise ValueError("Each entry once")
        return value
    show_pairs: bool | None = None  # older views only; the Supervisor card property replaces it


Clock = Annotated[str, StringConstraints(pattern=r"^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$")]


class AvailabilityPatternInput(Schema):
    person_id: ID
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(min_length=1, max_length=7)
    start_time: Clock
    end_time: Clock
    starts_on: date
    ends_on: date | None = None
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def valid_times(self):
        if self.start_time == "24:00" or self.end_time <= self.start_time:
            raise ValueError("The time must end after it starts on the same day")
        if self.ends_on is not None and self.ends_on <= self.starts_on:
            raise ValueError("End date is exclusive and must follow the start date")
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("Each weekday once")
        return self


class PatternDecision(OptionalApprovalDecision):
    decision: Literal["approve", "decline"]


class FairnessViewSettings(Schema):
    """What a saved fairness dashboard view keeps: period, who, measure, filters, grouping, exposure axes and display."""
    period: Literal["last_3_months", "last_6_months", "last_12_months", "this_year", "next_3_months", "custom"] = "last_12_months"
    starts_on: date | None = None
    ends_on: date | None = None
    who: Conditions | None = None
    metric: Literal["duties", "hours", "session_units", "weekends", "overtime_duties", "overtime_hours", "lead_turns", "supervision",
                    "wishes_submitted", "wishes_granted", "wishes_declined"] = "duties"
    assignment_type_ids: list[ID] = Field(default_factory=list, max_length=50)
    service_ids: list[ID] = Field(default_factory=list, max_length=50)
    location_ids: list[ID] = Field(default_factory=list, max_length=50)
    within: bool = True  # a location counts the locations inside it
    group_by: Literal["", "category_ids", "capability_ids", "programme_ids", "rotation_type_ids", "cohort_ids", "tags",
                      "service_ids", "assignment_type_ids", "location_ids"] = ""
    rows: Conditions | None = None
    cols: Conditions | None = None
    trend_who: Conditions | None = None  # the people drawn as trend lines (none: the furthest from their share)
    pairing: Literal["present", "supervised"] = "present"
    sort: Literal["name", "value", "gap"] = "gap"
    hidden: list[Literal["score", "goals", "distribution", "people", "exposure", "trends"]] = Field(default_factory=list, max_length=6)
    density: Literal["comfortable", "compact"] = "comfortable"

    @model_validator(mode="after")
    def custom_dates(self):
        if self.period == "custom" and not (self.starts_on and self.ends_on and self.starts_on < self.ends_on):
            raise ValueError("A custom period needs a start before its end")
        return self


class RulesViewSettings(Schema):
    """Rules-only view state; search, filters and columns use the shared definition."""
    mode: Literal["rules", "attention", "acknowledged"] = "rules"
    open: list[ViewKey] = Field(default_factory=list, max_length=1000)


class TimelineViewSettings(Schema):
    """A Timeline layout's own display (apps/web/src/timeline.ts): zoom, the layers hidden, the properties the name
    column shows, row density and weekend shading. `one_line`: one line per person, each stretch one segment (People ›
    Where is everyone, D48); its labels are `short` or `name` (full)."""
    zoom: Literal["week", "month", "quarter", "year"] = "year"
    hidden: list[ViewKey] = Field(default_factory=list, max_length=30)
    properties: list[ViewKey] | None = Field(default=None, max_length=30)
    density: Literal["comfortable", "compact"] = "comfortable"
    weekends: bool = True
    bar_label: Literal["name_length", "name", "dates", "short"] = "name_length"
    one_line: bool = False
    colour_by: Literal["rotation_type", "service", "cohort"] = "rotation_type"


PRINT_IMAGE_DPI = {96: "Screen", 150: "Medium", 200: "High", 300: "Print"}
PRINT_IMAGE_DPI_DEFAULT = 200
PRINT_WRAP_DEFAULT = True


class PrintSettings(Schema):
    schema_version: Literal[1] = 1
    paper: Literal["a4", "a3", "letter", "legal"] = "a4"
    orientation: Literal["landscape", "portrait"] = "landscape"
    scale: Literal["fit_width", "fit_page", "actual", "custom"] = "fit_width"
    scale_percent: int | None = Field(default=None, ge=25, le=200)
    margins: Literal["narrow", "normal", "wide"] = "normal"
    colour: Literal["colour", "greyscale"] = "colour"
    text_size: Literal["small", "normal", "large"] = "normal"
    include: list[Literal["title", "period", "status", "key", "filters", "page_numbers", "printed_by", "signature"]] = Field(
        default_factory=lambda: ["title", "period", "status", "key", "page_numbers", "printed_by"], max_length=8)
    repeat_headers: bool = True
    page_break: Literal["none", "group", "week", "month"] = "none"
    range: Literal["view", "week", "2weeks", "month", "rota"] = "view"
    view_id: ID | None = None
    format: Literal["print", "png", "csv"] = "print"
    # Optional additions keep legacy preset JSON unchanged; renderers apply the generated defaults.
    image_dpi: Literal[tuple(PRINT_IMAGE_DPI)] | None = Field(default=None, exclude_if=lambda value: value is None)
    wrap: bool | None = Field(default=None, strict=True, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def valid_print_settings(self):
        if (self.scale == "custom") != (self.scale_percent is not None):
            raise ValueError("A scale percentage is required only for custom scale")
        if len(self.include) != len(set(self.include)):
            raise ValueError("Each print inclusion once")
        return self


TABLE_GROUP_FIELDS = ('group', 'then')


class TableViewDefinition(Schema):
    label: PersonLabel | None = Field(default=None, exclude_if=lambda value: value is None)
    search_options: SearchOptions | None = Field(default=None, exclude_if=lambda value: value is None)
    schema_version: Literal[1] = 1
    layout: Literal["table", "cards", "board", "calendar", "sheet", "timeline"] = "table"
    hidden: list[ViewKey] = Field(default_factory=list, max_length=30)
    filters: CollectionFilters = Field(default_factory=dict)
    conditions: CollectionConditions | None = None
    as_of: date | None = None
    group: ViewKey | Literal[""] = ""
    then: ViewKey | Literal[""] = ""  # a second grouping level ("Then by")
    group_sort: list[tuple[ViewKey, ViewSortDirection]] | None = Field(
        default=None, max_length=len(TABLE_GROUP_FIELDS), exclude_if=lambda value: not value)
    sort: tuple[ViewKey, Literal["asc", "desc"]] | None = None
    # Further sort keys after `sort` ("then by"), in order; one per property at most, so bounded by the columns.
    then_sort: list[tuple[ViewKey, Literal["asc", "desc"]]] = Field(default_factory=list, max_length=30,
                                                                    exclude_if=lambda value: not value)
    q: str = Field(default="", max_length=120)
    wrap: list[ViewKey] = Field(default_factory=list, max_length=30)  # columns that wrap (default: one line, "+N")
    order: list[ViewKey] = Field(default_factory=list, max_length=30)  # column order chosen in the column menu
    widths: ViewWidths | None = Field(default=None, exclude_if=lambda value: not value)
    planner: PlannerViewSettings | None = None
    fairness: FairnessViewSettings | None = None
    rules: RulesViewSettings | None = None
    timeline: TimelineViewSettings | None = None
    print: PrintSettings | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def active_group_order(self):
        ordered = [key for key, direction in self.group_sort or []]
        active = {getattr(self, field) for field in TABLE_GROUP_FIELDS} - {""}
        if len(ordered) != len(set(ordered)) or set(ordered) - active:
            raise ValueError("Group sort must name each active grouping property at most once")
        return self


def department_view_filters(planner, filters=None, conditions=None):
    # Collection keys are extensible; also catch aliases and qualified person identifiers.
    person_fields = {"person", "people", "staff", "identity", "identities", "user", "users", "account", "accounts",
                     "assignee", "assignees", "participant", "participants", "supervisor", "supervisors",
                     "owner", "owners", "name", "email", "pager", "employee"}
    if planner and planner.search_options and planner.search_options.reference and planner.search_options.reference.kind == 'people':
        raise ValueError("A view shared with the department cannot filter by person")
    groups = [filters or {}, (planner.filters if planner else None) or {}]
    groups.append({key: True for key in conditions.fields()} if conditions else {})
    if (planner and planner.person_id) or any(values and person_fields.intersection(key.split('.')[0].replace("-", "_").split("_"))
                                              for group in groups for key, values in group.items()):
        raise ValueError("A view shared with the department cannot filter by person")


class TableViewInput(Schema):
    table_key: ViewTableKey
    name: Name
    definition: TableViewDefinition
    scope: ViewScope = "personal"
    workspace_id: ID | None = Field(default=None, exclude=True,
        description='Page context for validating viewed_rota; never stored in the view definition.')

    @model_validator(mode='before')
    @classmethod
    def collection_conditions(cls, data):
        if isinstance(data, dict):
            source = data.get('definition')
            source = source.model_dump(mode='json') if isinstance(source, TableViewDefinition) else source
        else:
            source = None
        if isinstance(source, dict) and source.get('conditions'):
            conditions = source['conditions']
            conditions = conditions.model_dump(mode='json') if isinstance(conditions, Conditions) else conditions
            definition = {**source, 'conditions': CollectionConditions.model_validate(conditions,
                context={'table_key': data.get('table_key')})}
            return {**data, 'definition': definition}
        return data

    @model_validator(mode="after")
    def valid_definition(self):
        if self.definition.widths:
            allowed_widths = {field['key'] for field in table_condition_definitions(self.table_key)}
            if self.definition.widths.keys() - allowed_widths:
                raise ValueError("Widths must name known columns in this list")
        if self.definition.conditions:
            allowed = set(CONDITION_FIELDS) | {field['key'] for field in table_condition_definitions(self.table_key)}
            unknown = self.definition.conditions.fields() - allowed
            if unknown:
                raise ValueError(f"This list cannot filter on: {', '.join(sorted(unknown))}")
        if (self.table_key == "schedule_print") != (self.definition.print is not None):
            raise ValueError("Print settings belong to a schedule_print view and are required there")
        if self.scope == "department":
            if self.definition.search_options and self.definition.search_options.reference and self.definition.search_options.reference.kind == 'people':
                raise ValueError("A view shared with the department cannot filter by person")
            department_view_filters(self.definition.planner, self.definition.filters, self.definition.conditions)
            if self.definition.fairness:
                for conditions in (self.definition.fairness.who, self.definition.fairness.rows,
                                   self.definition.fairness.cols, self.definition.fairness.trend_who):
                    department_view_filters(None, conditions=conditions)
        return self


class TableViewDefaultInput(Schema):
    table_key: ViewTableKey
    scope: ViewScope = "personal"
    view_id: ID | None = None
    clear: bool = False

    @model_validator(mode="after")
    def valid_choice(self):
        if self.clear and self.view_id is not None:
            raise ValueError("Clearing a default cannot select a view")
        if not self.clear and "view_id" not in self.model_fields_set:
            raise ValueError("Select a saved view or null for the built-in default")
        return self


class TableViewDefaults(Schema):
    department_view_id: ID | None = None
    department_set: bool = False
    personal_view_id: ID | None = None
    personal_set: bool = False


class PlannerViewInput(Schema):
    name: Name
    definition: PlannerViewSettings
    scope: ViewScope = "personal"

    @model_validator(mode="after")
    def shared_filters(self):
        if self.scope == "department":
            department_view_filters(self.definition)
        return self


SELF_APPROVAL_MAX_DAYS = 365


class SelfApprovalInput(Schema):
    """Who may decide their own work, for which actions and when. Exactly one of a person's sign-in or a role."""
    identity_id: ID | None = None
    role: RoleName | None = None
    actions: list[IndependentAction] = Field(min_length=1, max_length=len(INDEPENDENT_ACTIONS))
    starts_on: date
    ends_on: date | None = None
    no_end_date: bool = Field(default=False, strict=True)
    reason: Reason

    @model_validator(mode="after")
    def one_subject(self):
        if (self.identity_id is None) == (self.role is None):
            raise ValueError("Choose either a person or a role")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("Each action once")
        if self.no_end_date and self.ends_on is not None:
            raise ValueError('Choose an end date or No end date, not both')
        if self.ends_on is None and not self.no_end_date:
            self.ends_on = self.starts_on + timedelta(days=SELF_APPROVAL_MAX_DAYS)
        if self.ends_on is not None and self.ends_on <= self.starts_on:
            raise ValueError("The end date must be after the start date")
        if self.ends_on is not None and (self.ends_on - self.starts_on).days > SELF_APPROVAL_MAX_DAYS:
            raise ValueError(f"Self-approval permissions may last at most {SELF_APPROVAL_MAX_DAYS} days")
        return self


class RuleRelaxInput(Schema):
    workspace_id: ID
    person_id: ID
    binding_line_id: ID
    number: Literal['min', 'max', 'target', 'priority', 'session_priority', 'apply'] | None = None
    starts_on: date
    ends_on: date

    @model_validator(mode='after')
    def period(self):
        if self.ends_on <= self.starts_on:
            raise ValueError('The end date must be after the start date')
        return self


class WishRepeat(Schema):
    """Standing weekdays (Monday=0); ends_on is inclusive, None means open-ended."""
    weekdays: list[Annotated[int, Field(ge=0, le=6, strict=True)]] = Field(min_length=1)
    starts_on: date
    ends_on: date | None = None
    number: int | None = Field(default=None, ge=1, le=MAX_DB_INT, strict=True)

    @model_validator(mode='after')
    def valid_repeat(self):
        if len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError('Each weekday once')
        if self.ends_on is not None and self.ends_on < self.starts_on:
            raise ValueError('The end date must be on or after the start date')
        return self


class WishOverrideInput(Schema):
    """Replace standing defaults for one budget period or one existing rota; empty values reset it."""
    period_start: date | None = None
    workspace_id: ID | None = None
    skip: bool = False
    points: int | None = Field(default=None, ge=1, le=MAX_WISH_POINTS, strict=True)
    number: int | None = Field(default=None, ge=1, le=MAX_DB_INT, strict=True)

    @model_validator(mode='after')
    def one_period(self):
        if (self.period_start is None) == (self.workspace_id is None):
            raise ValueError('Choose one budget period or rota')
        return self


class StaffWishInput(Schema):
    """A soft preference for a day or days (last_day included). Points come from the person's budget for the period."""
    person_id: ID
    first_day: date | None = Field(default=None, json_schema_extra={"bulk": True})
    last_day: date | None = Field(default=None, json_schema_extra={"bulk": True})
    kind: WishKind = Field(json_schema_extra={"bulk": True})
    assignment_type_id: ID | None = Field(default=None, json_schema_extra={"bulk": True})
    limit: int | None = Field(default=None, ge=1, le=MAX_DB_INT, strict=True)
    weekend_start: int | None = Field(default=None, ge=0, le=6, strict=True)
    weekend_days: int | None = Field(default=None, ge=1, le=7, strict=True)
    points: WishPoints = Field(default=1, json_schema_extra={"bulk": True})
    note: str = Field(default='', max_length=200, json_schema_extra={"bulk": True})
    repeat: WishRepeat | None = None

    @model_validator(mode='after')
    def valid_wish(self):
        if self.repeat is not None:
            if self.kind in PATTERN_WISH_KINDS:
                raise ValueError('Pattern preferences use a date range, not weekday repeats')
            self.first_day = self.last_day = self.repeat.starts_on
        elif self.first_day is None or self.last_day is None or self.last_day < self.first_day:
            raise ValueError('Choose a valid first and last preference day')
        elif (self.last_day - self.first_day).days >= WISH_RANGE_MAX_DAYS:
            raise ValueError(f'A preference date range spans at most {WISH_RANGE_MAX_DAYS} days')
        elif self.kind not in PATTERN_WISH_KINDS and (self.last_day - self.first_day).days >= 14:
            raise ValueError('A preference covers one day or up to 14 days in a row')
        if (self.kind in {'prefer_shift_type', 'avoid_shift_type'}) != (self.assignment_type_id is not None):
            raise ValueError('Choose a duty type for a duty-type preference (and only then)')
        if (self.kind in PATTERN_WISH_KINDS - {'whole_weekends'}) != (self.limit is not None):
            raise ValueError('Choose a limit for days in a row, days off together or weekends in a row (and only then)')
        if self.kind not in {'max_weekends_in_row', 'whole_weekends'} and any(
                value is not None for value in (self.weekend_start, self.weekend_days)):
            raise ValueError('Weekend settings are only for a weekend pattern wish')
        if self.kind == 'whole_weekends' and self.weekend_days == 1:
            raise ValueError('A whole weekend has at least two days')
        return self


# The command bus owns execution; these are the only commands a bulk payload may contain.
# Reverse names describe existing handlers; "result" uses their structured server-built Undo.
class BulkRuleLine(RuleLine):
    id: ID | None = None


class BulkRuleLinesInput(Schema):
    add_lines: list[BulkRuleLine] = Field(min_length=1, max_length=2000)


class BulkTagActionInput(Schema):
    tag_ids: list[ID] = Field(max_length=500)


BULK_REVERSE_KINDS = {
    **{f'{family}.{action}': 'result' for family in ('membership', 'eligibility', 'programme_membership')
       for action in ('withdraw', 'restore')},
    'rotation.remove': 'result', 'rotation.restore': 'result',
    'person.anonymise': None,
    'job.cancel': None,
    'catalogue.create': 'catalogue.undo_create', 'catalogue.undo_create': 'catalogue.create',
    'catalogue.update': 'catalogue.update',
    'catalogue.bulk_update': 'catalogue.update', 'tag.members': 'tag.members',
    'membership.create': 'membership.undo_create', 'membership.bulk_create': 'result',
    'membership.end': 'membership.update_dates', 'membership.update_dates': 'membership.update_dates',
    'membership.undo_create': 'membership.create',
    'eligibility.create': 'eligibility.undo_create', 'eligibility.bulk_create': 'eligibility.undo_create',
    'eligibility.programme': 'eligibility.undo_create', 'eligibility.extend': 'eligibility.update_dates',
    'eligibility.end': 'eligibility.update_dates', 'eligibility.update_dates': 'eligibility.update_dates',
    'eligibility.undo_create': 'eligibility.create',
    'grant.create': 'grant.undo_create', 'grant.bulk_create': 'grant.undo_create',
    'grant.revoke': 'grant.restore', 'grant.restore': 'grant.revoke',
    'grant.update_dates': 'grant.update_dates', 'grant.undo_create': 'grant.create',
    'availability.record': 'availability.undo_create', 'availability.undo_create': None,
    'availability.update': 'availability.undo_update',
    'availability.undo_update': 'availability.update',
    'availability.retract': 'availability.restore', 'availability.restore': 'availability.retract',
    'request.decide': 'request.undo', 'request.decide_many': 'request.undo', 'request.undo': 'result',
    'availability_pattern.propose': 'availability_pattern.undo',
    'availability_pattern.decide': 'availability_pattern.undo',
    'availability_pattern.end': 'availability_pattern.restore', 'availability_pattern.undo': 'result',
    'availability_pattern.restore': 'availability_pattern.end',
    'rule.propose': 'policy.decline', 'policy.decline': 'policy.restore_proposal',
    'policy.restore_proposal': 'policy.decline',
    'policy.activate': 'policy.retire', 'policy.activate_many': 'policy.retire',
    'policy.retire': 'policy.restore', 'policy.restore': 'policy.retire',
    'exception.create': 'exception.end', 'exception.create_many': 'exception.end',
    'exception.approve': 'exception.end', 'exception.end': 'exception.undo', 'exception.undo': 'exception.end',
    'role.grant': 'role.revoke', 'role.revoke': 'role.restore', 'role.restore': 'role.revoke',
    'identity.disable': 'identity.enable', 'identity.enable': 'identity.disable',
    'wish.create': 'wish.withdraw', 'wish.update': 'wish.update', 'wish.withdraw': 'wish.restore',
    'wish.restore': 'wish.withdraw',
    'shift.create': 'shift.action', 'shift.update': 'shift.update', 'shift.action': 'result',
    'shift.copy': 'shift.action', 'shift.undo_batch': 'result', 'issues.resolve': 'result',
    **{f'{prefix}.{action}': (f'{prefix}.restore_proposal' if action == 'decline' else
                              f'{prefix}.decline' if action == 'restore_proposal' else f'{prefix}.undo')
       for prefix in ('person.workload', 'person.overtime', 'calendar.protection')
       for action in ('propose', 'activate', 'decline', 'restore_proposal', 'retire', 'undo')},
}
BULK_ITEM_KINDS: tuple[str, ...] = tuple(BULK_REVERSE_KINDS)
BulkItemKind = Literal[tuple(BULK_ITEM_KINDS)]
BULK_LIMITS = {'payload_bytes': 1024 * 1024, 'resolve_candidates': 5000, 'sample_rows': 100}


_BULK_FIELD_LABELS = {
    'id': 'Record', 'display_name': 'Name', 'active': 'In use', 'on': 'Enabled',
    'starts_on': 'From', 'ends_on': 'Last day', 'start': 'From', 'end': 'Until',
    'first_day': 'First day', 'last_day': 'Last day', 'start_time': 'Start time', 'end_time': 'Finish time',
    'person_id': 'Person', 'person_ids': 'People', 'category_id': 'Rank', 'category_ids': 'Ranks',
    'service_id': 'Clinic', 'service_ids': 'Clinics', 'capability_id': 'Skill', 'capability_ids': 'Skills',
    'assignment_type_id': 'Duty type', 'assignment_type_ids': 'Duty types', 'leave_type_id': 'Leave type',
    'calendar_id': 'Opening hours', 'calendar_entry_id': 'Academic calendar entry',
    'programme_id': 'Programme', 'programme_ids': 'Programmes', 'cohort_id': 'Cohort', 'cohort_ids': 'Cohorts',
    'location_id': 'Location', 'location_ids': 'Locations', 'location_type_id': 'Location type',
    'rotation_type_id': 'Rotation type', 'rotation_type_ids': 'Rotation types', 'rotation_id': 'Rotation',
    'policy_id': 'Rule', 'catalogue_id': 'Rule type', 'catalogue_version': 'Rule catalogue version',
    'parent_id': 'Parent location', 'identity_id': 'Account', 'workspace_id': 'Draft rota',
    'mentor_id': 'Mentor', 'mentee_id': 'Person', 'resource_pool_id': 'Resource pool',
    'tag_id': 'Tag', 'tag_ids': 'Tags', 'date_set_id': 'Date set', 'date_set_ids': 'Date sets',
    'short_code': 'Short code', 'private_reason': 'Private note', 'work_phone': 'Work phone',
    'work_extension': 'Extension', 'staff_number': 'Staff number', 'directory_section': 'Directory section',
    'directory_order': 'Directory order', 'who': 'Who', 'where': 'Where', 'numbers': 'Numbers',
    'add_lines': 'New rule lines', 'lines': 'Rule lines', 'kind': 'Type', 'labels': 'Applies to',
    'definition': 'Details', 'acknowledged_overlaps': 'Accepted overlaps', 'replaces_ids': 'Rules replaced',
    'check_job_ids': 'Rota checks', 'expected_policy_id': 'Current rule', 'replaces_id': 'Record replaced',
    'expected_revision': 'Current revision', 'source_bindings': 'Source rules', 'source_default': 'Inherited rule',
    'default_person_ids': 'Usual assignees', 'balance_between_people': 'Balance between people',
    'usual_assignees_fixed': 'Keep the usual assignees on every session',
    'close_when_all_usual_unavailable': 'Close a session when every usual assignee is away',
    'minimum_rest_hours': 'Minimum rest in hours', 'goal_ref': 'Source goal', 'keys': 'Issues',
    'shift_ids': 'Sessions', 'plan_hash': 'Replacement preview', 'acknowledge_unresolved': 'Accept unresolved findings',
    'notify': 'Notify people', 'evidence': 'Evidence', 'add': 'Records to add', 'remove': 'Records to remove',
}
_BULK_FIELD_REFS = {
    **{field['key']: field['kind'] for field in CONDITION_FIELD_DEFINITIONS if field['kind'] not in {'', 'roles'}},
    'person_id': 'people', 'category_id': 'categories', 'service_id': 'services', 'capability_id': 'capabilities',
    'assignment_type_id': 'assignment_types', 'leave_type_id': 'leave_types', 'calendar_id': 'operating_calendars',
    'calendar_entry_id': 'programme_calendar_entries', 'programme_id': 'programmes', 'cohort_id': 'cohorts',
    'location_id': 'locations', 'location_type_id': 'location_types', 'parent_id': 'locations',
    'rotation_type_id': 'rotation_types', 'rotation_id': 'rotations', 'mentor_id': 'people', 'mentee_id': 'people',
    'identity_id': 'identities', 'workspace_id': 'workspaces', 'policy_id': 'policies', 'expected_policy_id': 'policies',
    'resource_pool_id': 'resource_pools', 'tag_id': 'tags', 'tag_ids': 'tags', 'date_set_id': 'date_sets',
    'default_person_ids': 'people', 'shift_ids': 'sessions', 'replaces_ids': 'policies',
}
_BULK_CHOICE_LABELS = {'violet': 'Purple', 'amber': 'Gold', 'recorded_leave': 'Recorded leave', 'actual_absence': 'Actual absence',
    'external_commitment': 'External commitment', 'protected_time': 'Protected time', 'off_duty': 'Off duty',
    'approved_leave': 'Approved leave', 'prefer_shift_type': 'Prefer a duty type', 'avoid_shift_type': 'Avoid a duty type',
    'cancel': 'Close', 'reopen': 'Reopen', 'refresh_types': 'Refresh duty types',
    'release_unavailable': 'Release unavailable people', 'unassign': 'Clear people'}


def _bulk_label(name, field=None):
    return _BULK_FIELD_LABELS.get(name, (field.title if field and field.title else
                                      name.removesuffix('_ids').removesuffix('_id').replace('_', ' ').capitalize()))


def _bulk_fields(cls, *, inputs=False, exclude=()):
    schema = cls.model_json_schema()
    schema = schema.get('$defs', {}).get(cls.__name__, schema)
    result = {}
    for name, field in cls.model_fields.items():
        if name in exclude or not inputs and (not isinstance(field.json_schema_extra, dict) or not field.json_schema_extra.get('bulk')):
            continue
        prop = schema['properties'][name]
        alternatives = prop.get('anyOf', [prop])
        value = next((p for p in alternatives if p.get('type') != 'null'), prop)
        kind = ('date' if value.get('format') in ('date', 'date-time') else
                'multi' if value.get('type') == 'array' else 'scalar')
        extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}
        nullable = any(p.get('type') == 'null' for p in alternatives) and extra.get('bulk_clear', True)
        how = {'date': ['set', 'move_by'], 'multi': ['add', 'remove', 'set'], 'scalar': ['set']}[kind]
        # Required strings can be emptied only when their single-item contract permits it.
        empty_string = kind == 'scalar' and value.get('type') == 'string' and (
            value.get('default') == '' or '' in value.get('enum', []) or
            not value.get('enum') and not value.get('pattern') and value.get('minLength', 0) == 0)
        if extra.get('bulk_clear', nullable or empty_string):
            how = [*how, 'clear']
        result[name] = {'label': _bulk_label(name, field), 'kind': kind,
                        'how': how, 'required': field.is_required(), 'nullable': nullable,
                        'schema': prop}
        if name in _BULK_FIELD_REFS:
            result[name]['ref'] = _BULK_FIELD_REFS[name]
        choices = value.get('items', value).get('enum')
        if choices is not None:
            result[name]['choices'] = [{'value': choice, 'label': _BULK_CHOICE_LABELS.get(choice,
                str(choice).replace('_', ' ').capitalize())} for choice in choices]
    if cls is SeriesInput:
        definition = _bulk_fields(SeriesDefinition)
        result['usual_assignees_fixed'] = {**definition['usual_assignees_fixed'], 'path': 'definition'}
    if cls is RuleLine:
        quota = BlockQuota.model_json_schema()['properties']
        result['counting'] = {'label': 'Counting window', 'kind': 'scalar', 'how': ['set'],
            'required': False, 'nullable': False, 'schema': {'type': 'object', 'additionalProperties': False,
                'minProperties': 1, 'properties': {name: quota[name] for name in
                    ('starts_on', 'ends_on', 'repeat', 'window_anchor_on')}}}
    return result


def _bulk_collection(label, cls=None, *, catalogue=None, actions=None, parent_of=None, children=(),
                     conditions=None, related_columns=None, governed=False):
    actions = actions or ({'edit': 'catalogue.update'} if catalogue else {})
    declared = {}
    for name, kind in actions.items():
        if kind not in BULK_ITEM_KINDS:
            raise ValueError(f'{label}: bulk action {kind} is not allow-listed')
        declared[name] = {'kind': kind, 'destructive': kind.endswith(('.end', '.revoke', '.retire', '.retract',
                                                                     '.disable', '.withdraw', '.remove', '.decline', '.anonymise')),
                          'governed': governed or kind.startswith(('rule.', 'policy.', 'person.workload.',
                                                                   'person.overtime.', 'calendar.protection.'))}
    fields = _bulk_fields(cls) if cls else {}
    columns = {'id': {'label': 'Record', 'read_only': True},
               'revision': {'label': 'Revision', 'read_only': True}}
    if cls:
        schema = cls.model_json_schema()
        schema = schema.get('$defs', {}).get(cls.__name__, schema)
        columns.update({name: {'label': _bulk_label(name, cls.model_fields[name]),
                              'read_only': name not in fields}
                        for name, prop in schema['properties'].items()
                        if name not in {'private_reason', 'reason', 'source_bindings', 'source_default'}
                        and not prop.get('writeOnly') and not cls.model_fields[name].exclude})
    return {'label': label, 'schema': cls.__name__ if cls else None, 'catalogue': catalogue,
            'item_kinds': tuple(dict.fromkeys(a['kind'] for a in declared.values())), 'fields': fields,
            'columns': columns, 'actions': declared, 'dated': bool(cls and {'starts_on', 'start'} & cls.model_fields.keys()),
            'conditions': tuple(d for d in CONDITION_FIELD_DEFINITIONS if conditions is None or d['key'] in conditions),
            'parent_of': parent_of or {}, 'children': tuple(children), 'related_columns': related_columns or {}}


def _bulk_status(cls=None, *, options=None, defaults=None):
    if options is None:
        fields = cls.model_fields if cls else {}
        options = (('current', 'future', 'ended') if {'starts_on', 'start'} & fields.keys() else
                   ('active', 'inactive') if 'active' in fields else ())
        if 'active' in fields and 'current' in options:
            options = (*options, 'inactive')
        if 'cancelled' in fields:
            options = (*options, 'cancelled')
        defaults = [value for value in options if value not in {'ended', 'inactive', 'cancelled'}]
    return {'options': [{'value': value, 'label': value.replace('_', ' ').capitalize()} for value in options],
            'defaults': list(defaults if defaults is not None else options)}


_PERSON_BULK_COLUMNS = {
    f'person.{field}': {'label': label, 'collection': 'people', 'field': field, 'read_only': True}
    for field, label in (('display_name', 'Person'), ('staff_number', 'Staff number'), ('category_ids', 'Person rank'),
                         ('tags', 'Person tags'), ('programme_ids', 'Person programme'))
}
_SERVICE_BULK_COLUMNS = {
    f'service.{field}': {'label': label, 'collection': 'services', 'field': field, 'read_only': True}
    for field, label in (('name', 'Clinic'), ('tags', 'Clinic tags'), ('location_ids', 'Clinic locations'))
}


# Catalogue schema references are the same inputs used by the single-item command.
_BULK_CATALOGUE_LABELS = {
    'contact_types': 'Contact types',
    'people': 'People', 'services': 'Services', 'locations': 'Locations', 'location_types': 'Location types',
    'assignment_types': 'Duty types', 'categories': 'Ranks', 'capabilities': 'Skills', 'leave_types': 'Leave types',
    'tags': 'Tags', 'date_sets': 'Date sets', 'operating_calendars': 'Opening hours', 'resource_pools': 'Resource pools',
    'programmes': 'Programmes', 'cohorts': 'Cohorts', 'rotations': 'Rotations', 'rotation_types': 'Rotation types',
    'programme_memberships': 'Programme memberships', 'mentorships': 'Mentorships / attachments',
    'programme_calendar_entries': 'Academic calendar entries', 'recurring_series': 'Recurring sessions',
    'department_content': 'Department information', 'event_types': 'Event types', 'events': 'Events',
    'rotation_templates': 'Rotation templates',
}
BULK_COLLECTIONS = {key: _bulk_collection(_BULK_CATALOGUE_LABELS[key], cls, catalogue=key,
    actions={'edit': 'catalogue.update', **({'tag': 'tag.members'} if key in TAG_KINDS else {}),
             **({'withdraw': 'programme_membership.withdraw'} if key == 'programme_memberships' else {}),
             **({'remove': 'rotation.remove'} if key == 'rotations' else {})})
    for key, cls in CATALOGUE_SCHEMAS.items()}
# Contact values are exposed only through the role-aware contacts reader, never as bulk person properties.
for _field in {entry['legacy_field'] for entry in DEFAULT_CONTACT_TYPES}:
    BULK_COLLECTIONS['people']['columns'].pop(_field, None)
    BULK_COLLECTIONS['people']['fields'].pop(_field, None)
BULK_COLLECTIONS['people']['children'] = ('eligibilities', 'grants', 'memberships', 'programme_memberships',
    'availability', 'policy_lines', 'scheduling_facts', 'recurring_series', 'mentorships', 'rotations', 'exceptions')
BULK_COLLECTIONS['people']['actions'].update({name: {'kind': kind, 'destructive': False,
    'governed': kind.startswith(('rule.', 'person.'))} for name, kind in {
    'rank': 'membership.create', 'programme': 'catalogue.create', 'authorise': 'eligibility.create',
    'grant': 'grant.create', 'record_leave': 'availability.record', 'rule_number': 'rule.propose'}.items()})
BULK_COLLECTIONS['people']['actions']['programme']['payload'] = {'catalogue': 'programme_memberships'}
BULK_COLLECTIONS['people']['actions']['anonymise'] = {
    'kind': 'person.anonymise', 'destructive': True, 'governed': False}
BULK_COLLECTIONS['services']['children'] = ('eligibilities', 'sessions', 'recurring_series', 'policy_lines')
BULK_COLLECTIONS['programmes']['children'] = ('programme_memberships', 'cohorts', 'programme_calendar_entries', 'mentorships')
BULK_COLLECTIONS['cohorts']['children'] = ('rotations',)
BULK_COLLECTIONS['programme_calendar_entries']['children'] = ('scheduling_facts',)
BULK_COLLECTIONS['tags']['children'] = ('people', 'services', 'locations', 'assignment_types', 'programmes', 'policy_lines')
for _key in ('people', 'services', 'locations', 'assignment_types', 'programmes'):
    BULK_COLLECTIONS[_key]['parent_of']['tags'] = 'tag_id'
for key, parents in {
    'programme_memberships': {'people': 'person_id', 'programmes': 'programme_id'},
    'cohorts': {'programmes': 'programme_id'},
    'programme_calendar_entries': {'programmes': 'programme_id'},
    'mentorships': {'people': 'person_id', 'programmes': 'programme_id'},
    'rotations': {'people': 'person_id', 'cohorts': 'cohort_id', 'services': 'service_id'},
    'recurring_series': {'people': 'person_id', 'services': 'service_id'},
}.items():
    BULK_COLLECTIONS[key]['parent_of'] = parents
    BULK_COLLECTIONS[key]['related_columns'] = {**_PERSON_BULK_COLUMNS, **_SERVICE_BULK_COLUMNS}
BULK_COLLECTIONS.update({
    'eligibilities': _bulk_collection('Clinic permissions', RelationshipUpdateDates,
        actions={'update_dates': 'eligibility.update_dates', 'extend': 'eligibility.extend',
                 'end': 'eligibility.end', 'authorise': 'eligibility.create', 'withdraw': 'eligibility.withdraw'},
        parent_of={'people': 'person_id', 'services': 'service_id'},
        related_columns={**_PERSON_BULK_COLUMNS, **_SERVICE_BULK_COLUMNS}),
    'grants': _bulk_collection('Skill grants', RelationshipUpdateDates,
        actions={'update_dates': 'grant.update_dates', 'revoke': 'grant.revoke', 'grant': 'grant.create'},
        parent_of={'people': 'person_id', 'services': 'service_id', 'capabilities': 'capability_id'},
        related_columns={**_PERSON_BULK_COLUMNS, **_SERVICE_BULK_COLUMNS}),
    'memberships': _bulk_collection('Rank memberships', RelationshipUpdateDates,
        actions={'update_dates': 'membership.update_dates', 'end': 'membership.end', 'rank': 'membership.create',
                 'withdraw': 'membership.withdraw'},
        parent_of={'people': 'person_id', 'categories': 'category_id'}, related_columns=_PERSON_BULK_COLUMNS),
    'availability': _bulk_collection('Leave & availability', AvailabilityUpdate,
        actions={'update': 'availability.update', 'retract': 'availability.retract', 'record': 'availability.record'},
        parent_of={'people': 'person_id'}, related_columns=_PERSON_BULK_COLUMNS),
    'requests': _bulk_collection('Requests', actions={'decide': 'request.decide'}, parent_of={'people': 'person_id'},
        related_columns=_PERSON_BULK_COLUMNS),
    'availability_patterns': _bulk_collection('Weekly availability', AvailabilityPatternInput,
        actions={'propose': 'availability_pattern.propose', 'decide': 'availability_pattern.decide', 'end': 'availability_pattern.end'},
        parent_of={'people': 'person_id'}, related_columns=_PERSON_BULK_COLUMNS),
    'wishes': _bulk_collection('Preferences', StaffWishInput, actions={'update': 'wish.update', 'withdraw': 'wish.withdraw'},
        parent_of={'people': 'person_id'}, related_columns=_PERSON_BULK_COLUMNS),
    'scheduling_facts': _bulk_collection('Scheduling facts', SchedulingFactInput, governed=True,
        actions={f'{family}_{action}': f'{prefix}.{action}'
                 for family, prefix in (('workload', 'person.workload'), ('overtime', 'person.overtime'), ('protection', 'calendar.protection'))
                 for action in ('propose', 'activate', 'retire')},
        parent_of={'people': 'person_id', 'programme_calendar_entries': 'calendar_entry_id'}, related_columns=_PERSON_BULK_COLUMNS),
    'policies': _bulk_collection('Rules', RuleProposalInput, governed=True,
        actions={'propose': 'rule.propose', 'activate': 'policy.activate', 'decline': 'policy.decline',
                 'retire': 'policy.retire'}, children=('policy_lines',)),
    'policy_lines': _bulk_collection('Rule lines', RuleLine, governed=True, actions={'propose': 'rule.propose'},
        parent_of={'people': 'person_id', 'services': 'service_id', 'policies': 'policy_id', 'tags': 'tag_id'},
        related_columns={**_PERSON_BULK_COLUMNS, **_SERVICE_BULK_COLUMNS}),
    'exceptions': _bulk_collection('Person exceptions', ExceptionInput, governed=True,
        actions={'propose': 'exception.create', 'approve': 'exception.approve', 'end': 'exception.end'},
        parent_of={'people': 'person_id', 'policies': 'policy_id'}, related_columns=_PERSON_BULK_COLUMNS),
    'sessions': _bulk_collection('Planner sessions', ShiftInput,
        actions={'update': 'shift.update', 'action': 'shift.action', 'copy': 'shift.copy'},
        parent_of={'services': 'service_id', 'people': 'person_id'}, related_columns={**_PERSON_BULK_COLUMNS, **_SERVICE_BULK_COLUMNS}),
    'roles': _bulk_collection('Account roles', RoleInput, actions={'grant': 'role.grant', 'revoke': 'role.revoke'},
        parent_of={'identities': 'identity_id'}),
    'identities': _bulk_collection('Accounts', actions={'disable': 'identity.disable', 'enable': 'identity.enable'},
        parent_of={'people': 'person_id'}, children=('roles',)),
    'issues': _bulk_collection('Issues', actions={'resolve': 'issues.resolve'}),
    **{key: _bulk_collection(label) for key, label in (
        ('swaps', 'Swaps'), ('feedback', 'Feedback'), ('inbox', 'Inbox'), ('outbox', 'Outbox'),
        ('imports', 'Imports'), ('history', 'History'))},
})
# Workflow states remain workflow states even when their records also have dates.
for _key, _field, _label in (('grants', 'capability_id', 'Skill'), ('memberships', 'category_id', 'Rank')):
    BULK_COLLECTIONS[_key]['columns'][_field] = {'label': _label, 'read_only': True}

_BULK_WORKFLOW_STATUSES = {
    'policies': (('proposed', 'active', 'retired'), ('active', 'proposed')),
    'policy_lines': (('proposed', 'active', 'retired'), ('active', 'proposed')),
    'scheduling_facts': (('proposed', 'active', 'retired', 'declined'), ('active', 'proposed')),
    'exceptions': (('proposed', 'approved', 'ended', 'withdrawn'), ('approved', 'proposed')),
    'requests': (('submitted', 'approved', 'rejected', 'withdrawn'), ('submitted', 'approved')),
    'availability_patterns': (('proposed', 'active', 'declined', 'ended'), ('active', 'proposed')),
    'wishes': (('open', 'withdrawn'), ('open',)),
    'swaps': (('offered', 'counteroffered', 'accepted', 'reviewing', 'published', 'withdrawn', 'rejected'),
              ('offered', 'counteroffered', 'accepted', 'reviewing')),
    'identities': (('enabled', 'disabled'), ('enabled',)),
    'inbox': (('unread', 'read'), ('unread', 'read')),
    'outbox': (OUTBOX_STATES, OUTBOX_STATES),
    'imports': (('preview', 'saved_draft'), ('preview', 'saved_draft')),
    'issues': (('draft', 'submitted', 'approved', 'published', 'superseded'),
               ('draft', 'submitted', 'approved', 'published', 'superseded')),
    'sessions': (('current', 'future', 'ended', 'cancelled'), ('current', 'future')),
    'feedback': (FEEDBACK_STATUSES, tuple(value for value in FEEDBACK_STATUSES if value not in {'fixed', 'wont_fix', 'duplicate'})),
}
for _key, _collection in BULK_COLLECTIONS.items():
    _workflow = _BULK_WORKFLOW_STATUSES.get(_key)
    _collection['status'] = (_bulk_status(options=_workflow[0], defaults=_workflow[1]) if _workflow else
                             _bulk_status(globals().get(_collection['schema'])))
_BULK_ACTION_LABELS = {'edit': 'Change details', 'tag': 'Change tags', 'rank': 'Give rank',
    'programme': 'Add to programme', 'authorise': 'Authorise clinics', 'grant': 'Grant skills',
    'record_leave': 'Record leave', 'record': 'Record leave', 'rule_number': 'Set a rule number',
    'anonymise': 'Remove personal details', 'update_dates': 'Change dates', 'extend': 'Extend permission',
    'end': 'End', 'revoke': 'Revoke skill', 'update': 'Change details', 'retract': 'Retract', 'remove': 'Remove',
    'decide': 'Review', 'propose': 'Propose a change', 'withdraw': 'Withdraw', 'activate': 'Approve',
    'retire': 'Retire', 'decline': 'Decline', 'approve': 'Approve', 'action': 'Change sessions',
    'copy': 'Copy sessions', 'disable': 'Disable account', 'enable': 'Enable account', 'resolve': 'Resolve issues'}
_BULK_ACTION_SCHEMAS = {
    'membership.create': MembershipInput, 'eligibility.create': EligibilityInput, 'grant.create': GrantInput,
    'availability.record': AvailabilityInput, 'availability.update': AvailabilityUpdate,
    'availability.undo_create': AvailabilityUndoCreate,
    'tag.members': TagMembersInput, 'membership.update_dates': RelationshipUpdateDates,
    'eligibility.update_dates': RelationshipUpdateDates, 'grant.update_dates': RelationshipUpdateDates,
    'rule.propose': RuleProposalInput, 'policy.activate': PolicyActivationDecision,
    'request.decide': RequestDecision, 'availability_pattern.propose': AvailabilityPatternInput,
    'availability_pattern.decide': PatternDecision, 'wish.update': StaffWishInput,
    'exception.create': ExceptionInput, 'shift.update': ShiftEdit, 'shift.action': ShiftAction,
    'shift.copy': ShiftCopy, 'role.grant': RoleInput, 'issues.resolve': IssueBatch,
    **{f'{prefix}.{action}': (SchedulingFactInput if action == 'propose' else FactDecisionInput)
       for prefix in ('person.workload', 'person.overtime', 'calendar.protection')
       for action in ('propose', 'activate')},
}


def _bulk_action_fields(key, name, action):
    kind = action['kind']
    if kind == 'catalogue.update':
        return _bulk_fields(CATALOGUE_SCHEMAS[key])
    if kind == 'catalogue.create':
        cls = CATALOGUE_SCHEMAS[action.get('payload', {}).get('catalogue', key)]
    elif key == 'people' and name == 'rule_number':
        cls = BulkRuleLinesInput
    elif kind == 'tag.members' and key in TAG_KINDS:
        cls = BulkTagActionInput
    elif kind in {'membership.end', 'eligibility.end', 'grant.revoke', 'eligibility.extend'}:
        fields = _bulk_fields(RelationshipUpdateDates)
        end = fields['ends_on']
        if kind != 'eligibility.extend':
            end = {**end, 'required': True, 'nullable': False, 'how': ['set'],
                   'schema': {'type': 'string', 'format': 'date', 'title': 'Last day'}}
        return {'ends_on': end}
    else:
        cls = _BULK_ACTION_SCHEMAS.get(kind, Decision)
    # Selection supplies the selected parent; audit reasons and revision guards are outside the action form.
    exclude = {'reason', 'override_reason', 'expected_revision', 'expected_policy_id', 'fact_id', 'predecessor_revision'}
    if key == 'people':
        exclude.add('person_id')
    if key in {'sessions', 'issues'}:
        exclude.update({'workspace_id', 'shift_id', 'shift_ids', 'keys'})
    if kind == 'availability.record':
        exclude.add('leave_type')  # legacy free text; new forms choose the existing leave-type reference
    if kind == 'request.decide':
        exclude.add('balance_confirmation')
    return _bulk_fields(cls, inputs=True, exclude=exclude)


for _key, _collection in BULK_COLLECTIONS.items():
    _collection['group'] = ('Rules' if _key in {'policies', 'policy_lines', 'scheduling_facts', 'exceptions'} else
        'Rota' if _key in {'sessions', 'recurring_series', 'availability', 'requests', 'availability_patterns',
                           'wishes', 'swaps', 'issues'} else
        'People & permissions' if _key in {'people', 'eligibilities', 'grants', 'memberships', 'programme_memberships',
                                          'mentorships', 'programmes', 'cohorts', 'rotations', 'rotation_types',
                                          'programme_calendar_entries'} else 'Configuration')
    for _name, _action in _collection['actions'].items():
        _action['label'] = _BULK_ACTION_LABELS.get(_name, _name.replace('_', ' ').capitalize())
        _action['fields'] = _bulk_action_fields(_key, _name, _action)
        _action['undoable'] = BULK_REVERSE_KINDS[_action['kind']] is not None
    _collection['item_kinds'] = tuple(dict.fromkeys(a['kind'] for a in _collection['actions'].values()))
BulkCollection = Literal[tuple(BULK_COLLECTIONS)]


def _collection_condition_field(key, label, value_kind='multiple_choice', operand_field=None, *, display_values=False,
                                value_kinds=()):
    canonical = next((field for field in CONDITION_FIELD_DEFINITIONS if field['key'] == operand_field), {})
    kind = canonical.get('value_kind', value_kind) if operand_field else value_kind
    return {'key': key, 'label': label, 'kind': canonical.get('kind', ''), 'session': False,
            'value_kind': kind, 'operators': tuple(dict.fromkeys(op for value in (value_kinds or (kind,)) for op in CONDITION_OPERATORS[value])),
            'display_values': display_values,
            'exclusive_last_day': kind == 'date' and (operand_field or key) in EXCLUSIVE_LAST_DAY_FIELDS,
            **({'value_kinds': value_kinds} if value_kinds else {}),
            **({'operand_field': operand_field} if operand_field else {})}


def _collection_condition_fields(collection):
    result = {}
    cls = globals().get(collection['schema']) if collection['schema'] else None
    if cls:
        schema = cls.model_json_schema()
        schema = schema.get('$defs', {}).get(cls.__name__, schema)
        for key, prop in schema['properties'].items():
            if key not in collection['columns']:
                continue
            value = next((item for item in prop.get('anyOf', [prop]) if item.get('type') != 'null'), prop)
            value_kind = ('date' if value.get('format') in {'date', 'date-time'} else
                          'number' if value.get('type') in {'integer', 'number'} else
                          'multiple_choice' if value.get('type') in {'array', 'boolean'} or value.get('enum') or key in _BULK_FIELD_REFS or key.endswith(('_id', '_ids')) else
                          'text' if value.get('type') == 'string' else None)
            if value_kind is None:
                continue
            dimension = key if key in CONDITION_FIELDS else key.removesuffix('_id') + '_ids'
            if dimension not in CONDITION_FIELDS:
                ref = _BULK_FIELD_REFS.get(key)
                dimension = next((field['key'] for field in CONDITION_FIELD_DEFINITIONS if ref and field['kind'] == ref), None)
            result[key] = _collection_condition_field(key, _bulk_label(key, cls.model_fields[key]), value_kind, dimension,
                value_kinds=('text', 'multiple_choice') if value_kind == 'text' and not dimension else ())
    for key, column in collection['columns'].items():
        if key not in result and (cls is None or key not in cls.model_fields):
            kind = ('number' if key == 'revision' else 'multiple_choice' if key in _BULK_FIELD_REFS or key == 'id'
                    or key.endswith(('_id', '_ids')) else 'text')
            result[key] = _collection_condition_field(key, column['label'], kind)
    for key in collection['parent_of'].values():
        ref = _BULK_FIELD_REFS.get(key)
        dimension = key.removesuffix('_id') + '_ids'
        if dimension not in CONDITION_FIELDS:
            dimension = next((field['key'] for field in CONDITION_FIELD_DEFINITIONS if ref and field['kind'] == ref), None)
        result.setdefault(key, _collection_condition_field(key, _bulk_label(key), 'multiple_choice', dimension))
    for key, column in collection['related_columns'].items():
        field = column['field']
        canonical = ('person_ids' if column['collection'] == 'people' and field == 'display_name' else
                     'service_ids' if column['collection'] == 'services' and field == 'name' else
                     field if field in CONDITION_FIELDS else None)
        result[key] = _collection_condition_field(key, column['label'], 'text', canonical, display_values=True,
            value_kinds=('text', 'multiple_choice') if not canonical else ())
    if collection['status']['options']:
        result.setdefault('status', _collection_condition_field('status', 'Status', display_values=True))
    return result


# Virtual columns belong to the same registry as model-derived fields: facets store their displayed labels.
_TABLE_CONDITION_COLUMNS = {
    'names': 'name:text,code:text,staff:text,status',
    'people': 'name:text,code:text,phone:text,ext:text,order:number,works_in_area,rank>category_ids,services>eligible_service_ids,skills>capability_ids,programme>programme_ids,tags>tags,title,section,status,review',
    'requests': 'id,type,person>person_ids,person_id>person_ids,status,stage,source,start:date,end:date,proposed_at:date,dates.start:date>start,dates.end:date>end,detail:text,impact:text',
    'operations': 'id,state,kind,error:text,created_at:date,date:date',
    'audit': 'id,action,kind,actor_id,person:text,target_id,reason:text,created_at:date,date:date',
    'schedule': 'id,source,workspace_id,rota_ids,person_ids>person_ids,category_ids>category_ids,programme_ids>programme_ids,tags>tags,service_ids>service_ids,location_ids>location_ids,assignment_type_ids>assignment_type_ids,consultation_mode,weekday,start:date,end:date,date:date',
    'wishes': 'person>person_ids,wish,outcome,when:text,points:number,note:text',
    'exceptions': 'rule,person>person_ids,dates:text,status,reason:text',
    'rule-lines': 'who,dates:text,days,value,status',
    'rule-line-db': 'who:text,where:text,mode:text,measure:text,period:text,days:text,valid:text,reach:number,note:text,specific:text,kind,rank>category_ids,person>person_ids,programme>programme_ids,tag>tags,clinic>service_ids,clinic_type,building>location_ids,days_group,level,valid_group,overlaps',
    'rule-tree': 'state,family,issue,availability,kind,rule,who,where,numbers,days,valid,measure,period',
    'rotation-board': 'programme,cohort,rotation_type,trainees',
    'workload': 'person>person_ids,from:date,through:date,period,assignments:number,hours:number,credits:number,ot:number,lead:number,training:number,ot_hours:number,off_cover:number,reserved:number,ot_offered:number',
    'source-records': 'record:text,status,details:text,source:text',
    'imports': 'source:text,state,created:date',
    'shortages': 'duty,day,time:text,needs:number,can',
    'schedule-removals': 'person>person_ids,duty,day,time:text,reason:text',
    'schedule-additions': 'day,person>person_ids,duty,clinic>service_ids,location>location_ids,time:text,participation,overtime',
    'inbox': 'note:text,day,kind,read',
    'access': 'name:text,identity.display_name:text,issuer,roles>roles,status,last:date',
    'access-history': 'role>roles,person>person_ids,from:date,until:date,status',
    'outbox': 'kind,state,attempts:number,error:text',
    'bulk-preview': 'status,record:text,change:text,problem,fix:text',
    'bulk-results': 'status,record:text,change:text,why:text',
    'feedback': 'report:text,kind,status,page,occurrences:number,reporter,version,sent:date',
    'self-approval': 'who:text,actions:text,dates:text,reason:text,state',
    'resolve-issues': 'person>person_ids,reason,session:text,rule,fix:text,day,rank>category_ids,clinic>service_ids,type>assignment_type_ids,leave,severity,cause',
    'quotas': 'status,person>person_ids,rule:text,block:text,measure:text,kind:text,calc:text,amount:number,actual:number,difference:number,overtime:text',
    'planner-list': 'when,duty,place>location_ids,service>service_ids,people:text,status',
    'person-sessions': 'device:text,network:text,active:text,status:text',
    'person-work': 'when:text,what:text',
    'other-department-rules': 'name:text,effect:text,level:text,dates:text',
    'pending-rules': 'name:text,effect:text,actions:text',
    'simplify-rule-history': 'created:date,status:text,reason:text',
    'simplify-rules': 'was:text,now:text,order:text',
    'staff-imports': 'source:text,rows:number,state,created:date',
    'staff-import-rows': 'row:number,person,rank,clinics>service_ids,skills>capability_ids,programme:text',
    'planner': 'consultation_mode',
}
_TABLE_CONDITION_ALIASES = {'person-requests': 'requests', 'person-wishes': 'wishes',
    'person-exceptions': 'exceptions', 'feedback-mine': 'feedback', 'quotas-check': 'quotas', 'quotas-rota': 'quotas'}
_CATALOGUE_CONDITION_COLUMNS = {
    'assignment_types': ('Session hours', 'Staff needed', 'Where'),
    'locations': ('Type', 'Capacity'), 'resource_pools': ('Units', 'Where'),
    'services': ('Mode', 'Opening hours'), 'operating_calendars': ('Timezone', 'Date overrides'),
    'date_sets': ('Entries', 'Next'), 'leave_types': (),
}
TABLE_CONDITION_DEFINITIONS = {key: _collection_condition_fields(collection) for key, collection in BULK_COLLECTIONS.items()}
RULE_LINE_COLUMN_LIMIT = 9 + max(map(len, LINE_SETTINGS.values()), default=0)
TABLE_CONDITION_DEFINITIONS['rule-line-rows'] = {
    f'c{index}': _collection_condition_field(f'c{index}', f'Column {index + 1}', display_values=True,
                                            value_kinds=('text', 'number', 'multiple_choice'))
    for index in range(RULE_LINE_COLUMN_LIMIT)}
for _table, _columns in _TABLE_CONDITION_COLUMNS.items():
    _fields = TABLE_CONDITION_DEFINITIONS.setdefault(_table, {})
    for _column in _columns.split(','):
        _key, _, _dimension = _column.strip().partition('>')
        _key, _, _kind = _key.partition(':')
        _fields[_key] = _collection_condition_field(_key, _key.replace('_', ' ').replace('.', ' · ').capitalize(),
            _kind or 'multiple_choice', _dimension or None, display_values=not _kind)
TABLE_CONDITION_DEFINITIONS['planner'].update(TABLE_CONDITION_DEFINITIONS['schedule'])
for _key in CATALOGUE_SCHEMAS:
    _fields = dict(TABLE_CONDITION_DEFINITIONS[_key])
    _fields.update({key: _collection_condition_field(key, label, kind, display_values=kind == 'multiple_choice')
                   for key, label, kind in (('name', 'Name', 'text'), ('status', 'Status', 'multiple_choice'))})
    for _index, _label in enumerate(_CATALOGUE_CONDITION_COLUMNS.get(_key, ('Effective',))):
        _fields[f'c{_index}'] = _collection_condition_field(f'c{_index}', _label, display_values=True)
    if _key in TAG_KINDS:
        _fields['tags'] = _collection_condition_field('tags', 'Tags', 'tags', 'tags', display_values=True)
    if _key == 'leave_types':
        _fields['blocks'] = _collection_condition_field('blocks', 'Blocks duties', display_values=True)
        _fields['order'] = _collection_condition_field('order', 'Order', 'number')
    TABLE_CONDITION_DEFINITIONS[f'catalogue-{_key}'] = _fields
for _key, _columns in {'catalogue-tags': 'labels,status,name:text,description:text,members:number,used:text',
                     'catalogue-recurring_series': 'clinic>service_ids,clinic_type,type>assignment_type_ids,days,staff,people>person_ids,status,times:text,places:text,dates:text'}.items():
    for _column in _columns.split(','):
        _field, _, _dimension = _column.partition('>')
        _field, _, _kind = _field.partition(':')
        TABLE_CONDITION_DEFINITIONS[_key][_field] = _collection_condition_field(_field,
            _field.replace('_', ' ').capitalize(), _kind or 'multiple_choice', _dimension or None, display_values=not _kind)


for _fields in TABLE_CONDITION_DEFINITIONS.values():
    _source = next((field['key'] for field in CONDITION_FIELD_DEFINITIONS
                    if field['exclusive_last_day'] and field['key'] in _fields), None)
    if _source and 'last_day' not in _fields:
        _fields['last_day'] = _collection_condition_field('last_day', 'Last day', 'date', _source)


@lru_cache(maxsize=2048)
def table_condition_definitions(table_key):
    """The table's own fields, including derived raw/related columns and named virtual columns."""
    key = _TABLE_CONDITION_ALIASES.get(table_key, table_key)
    for prefix in ('related-', 'bulk-editor-'):
        if key and key.startswith(prefix) and key[len(prefix):] in BULK_COLLECTIONS:
            key = key[len(prefix):]
            break
    if key and re.fullmatch(r'source-records-[0-9a-f-]{36}', key):
        key = 'source-records'
    fields = tuple(TABLE_CONDITION_DEFINITIONS.get(key, {}).values())
    if key == 'rule-line-db':
        from .rule_catalogue import NUMBER_ORDER
        fields += tuple(_collection_condition_field(f'n_{number}', number.replace('_', ' ').capitalize(),
                        'text' if number == 'apply' else 'number', None if number == 'apply' else number) for number in NUMBER_ORDER)
    return fields


@lru_cache(maxsize=1)
def table_condition_registry():
    """Export the complete registry, including catalogue-derived dynamic columns."""
    return {key: {field['key']: field for field in table_condition_definitions(key)} for key in TABLE_CONDITION_DEFINITIONS}


@lru_cache(maxsize=8192)
def condition_field_definition(field, table_key=None):
    """One metadata lookup for validation, token applicability and resolution; rule admission stays separate."""
    if table_key:
        own = next((item for item in table_condition_definitions(table_key) if item['key'] == field), None)
        if own is not None:
            return own
    legacy = next((item for item in CONDITION_FIELD_DEFINITIONS if item['key'] == field), None)
    if legacy is not None or table_key:
        return legacy
    return next((fields[field] for fields in TABLE_CONDITION_DEFINITIONS.values() if field in fields), None)


def condition_line_definition(field, table_key=None, op=None, value=None):
    """Data-dependent numbered columns share the same operand type inference in admission and evaluation."""
    metadata = condition_field_definition(field, table_key)
    if metadata is None or not metadata.get('value_kinds'):
        return metadata
    operand = value[0] if op == 'between' and isinstance(value, (list, tuple)) and value else value
    absolute = (operand.value if isinstance(operand, AbsoluteOperand) else operand.get('value')
                if isinstance(operand, dict) and operand.get('kind') == 'absolute' else operand)
    kind = ('number' if op in {'gt', 'gte', 'lt', 'lte', 'between'} or isinstance(absolute, (int, float)) and not isinstance(absolute, bool) else
            'text' if op in {'contains', 'not_contains', 'starts_with', 'ends_with'} or isinstance(absolute, str) else 'multiple_choice')
    if kind not in metadata['value_kinds']:
        kind = metadata['value_kind']
    return {**metadata, 'value_kind': kind, 'operators': CONDITION_OPERATORS[kind]}


ConditionResolveContext.model_rebuild()
ConditionResolveInput.model_rebuild()
ConditionResolveBatchInput.model_rebuild()


def bulk_collection_registry(command_roles, collection_readers=None):
    """Expose the same registry and the command bus's authoritative role rules to HTTP and generated clients."""
    return {key: {**collection, **({'readers': list(collection_readers(key))} if collection_readers else {}), 'actions': {
        name: {**action, 'roles': command_roles(Command(kind=action['kind'],
            payload=action.get('payload', {'catalogue': key})))}
        for name, action in collection['actions'].items()}} for key, collection in BULK_COLLECTIONS.items()}


class BulkWhere(Schema):
    search_options: SearchOptions | None = Field(default=None, exclude_if=lambda value: value is None)
    conditions: CollectionConditions | None = None
    as_of: date | None = None
    workspace_id: ID | None = Field(default=None, exclude_if=lambda value: value is None)
    q: str = Field(default='', max_length=500)
    filters: dict[str, Any] = Field(default_factory=dict, max_length=50)


class BulkParent(Schema):
    collection: BulkCollection
    selection: 'BulkSelection'


class BulkSelection(Schema):
    collection: BulkCollection
    parent: BulkParent | None = None
    where: BulkWhere = Field(default_factory=BulkWhere)
    names: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] | None = Field(default=None, max_length=5000)
    include_ids: list[ID] = Field(default_factory=list, max_length=5000)
    exclude_ids: list[ID] = Field(default_factory=list, max_length=5000)

    @model_validator(mode='before')
    @classmethod
    def collection_conditions(cls, data):
        source = data.get('where') if isinstance(data, dict) else None
        source = source.model_dump(mode='json') if isinstance(source, BulkWhere) else source
        if isinstance(source, dict) and source.get('conditions'):
            tree = source['conditions']
            tree = tree.model_dump(mode='json') if isinstance(tree, Conditions) else tree
            return {**data, 'where': {**source, 'conditions': CollectionConditions.model_validate(tree,
                context={'table_key': data.get('collection')})}}
        return data

    @model_validator(mode='after')
    def two_levels(self):
        if self.parent:
            if self.parent.collection != self.parent.selection.collection:
                raise ValueError('The parent collection must match its selection')
            if self.parent.selection.parent is not None:
                raise ValueError('Select parents and their children in at most two levels')
            if self.parent.collection not in BULK_COLLECTIONS[self.collection]['parent_of']:
                raise ValueError('These collections have no parent-child relationship')
        return self


BulkParent.model_rebuild()


class BulkChange(Schema):
    field: str | None = Field(default=None, min_length=1, max_length=160)
    action: str | None = Field(default=None, min_length=1, max_length=160)
    how: Literal['set', 'clear', 'move_by', 'add', 'remove', 'give', 'end', 'extend'] = 'set'
    value: Any = None
    item_kind: BulkItemKind | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def operation(self):
        if not (self.field or self.action or self.item_kind):
            raise ValueError('Choose a field or action to change')
        if self.field and (self.action or self.item_kind):
            raise ValueError('A change edits a field or runs an action')
        return self


class BulkItem(Schema):
    item_kind: BulkItemKind | None = None
    kind: BulkItemKind | None = None
    target_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    payload: dict[str, Any] = Field(default_factory=dict)
    changes: list[BulkChange] = Field(default_factory=list, max_length=100)

    @model_validator(mode='after')
    def one_kind(self):
        if self.item_kind and self.kind and self.item_kind != self.kind:
            raise ValueError('Choose one command kind for each item')
        return self


class BulkApply(SeriesOverlapAcknowledgement):
    item_kind: BulkItemKind | None = None
    common: dict[str, Any] = Field(default_factory=dict)
    items: list[BulkItem] = Field(default_factory=list, max_length=5000)
    changes: list[BulkChange] = Field(default_factory=list, max_length=100)
    selection: BulkSelection | None = None
    source_selection: BulkSelection | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Original unresolved selection retained when staging exact revision-bound items.')
    resolutions: dict[str, str] = Field(default_factory=dict, max_length=5000)
    reason: Reason
    only: list[Annotated[int, Field(ge=0, strict=True)]] | None = Field(default=None, max_length=5000)
    apply_ready_only: bool = False
    staged_id: ID | None = None
    undo_of: ID | None = None

    @model_validator(mode='after')
    def selected_items(self):
        if bool(self.items) == bool(self.selection):
            raise ValueError('Send items or a selection')
        if self.items and any(not (self.item_kind or item.item_kind or item.kind or item.changes or self.changes)
                              for item in self.items):
            raise ValueError('Every item needs a command kind or changes')
        if self.only is not None and len(set(self.only)) != len(self.only):
            raise ValueError('Choose each row index once')
        if self.items and self.only and max(self.only) >= len(self.items):
            raise ValueError('A chosen row index is outside the items')
        return self


class BulkResolutionOperation(Schema):
    item_kind: BulkItemKind | None = None
    target_id: ID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    patch_item: dict[str, Any] | None = None
    skip: bool = False


class BulkResolution(Schema):
    id: str
    label: str
    best: bool = False
    operations: list[BulkResolutionOperation] = Field(default_factory=list)


class BulkHandoff(Schema):
    role: RoleName
    people: list[dict[str, Any]] = Field(default_factory=list)
    route: str


class BulkIssue(Schema):
    code: str
    message: str
    fixable_here: bool
    group_key: str
    handoff: BulkHandoff | None = None
    resolutions: list[BulkResolution] = Field(default_factory=list)


class BulkPreviewItem(Schema):
    """One row's outcome, shared by preview and apply results.

    Preview returns ready (valid and would change), unchanged (no change, including
    a chosen skip), problem (blocked by an issue), or waiting (requires approval).
    Apply returns changed (applied), skipped (deliberately omitted), refused
    (rejected without applying), waiting (proposed or sent for approval), unchanged
    (no change), or staged (not applied, kept as a staged change to resolve later;
    shown to users as "Skipped"). Resolved preview issues use the same states.
    """

    index: int = Field(ge=0)
    target_id: ID | None = None
    item_kind: BulkItemKind | None = None
    result: Literal['ready', 'unchanged', 'problem', 'waiting', 'changed', 'skipped', 'refused', 'staged'] = Field(
        description='Preview: ready, unchanged, problem, waiting. Apply: changed, skipped, refused, waiting, '
                    'unchanged, staged. See the row contract for each state\'s meaning.')
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    issues: list[BulkIssue] = Field(default_factory=list)
    record: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list, description=
        'Session-creation children include their saved session and final draft/check summary. '
        'The first child per draft also includes final health, issue deltas and changed existing peer sessions.')
    undoable: bool = True


class BulkPreview(Schema):
    preview: Literal[True] = True
    kind: Literal['bulk.apply'] = 'bulk.apply'
    undo: None = None
    source_fingerprint: str
    fingerprint: str | None = None
    expected_fingerprint: str | None = None
    count: int | None = Field(default=None, ge=0)
    counts: dict[str, int]
    groups: list[dict[str, Any]] = Field(default_factory=list)
    items: list[BulkPreviewItem] = Field(default_factory=list)
    rows: list[BulkPreviewItem] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    impacts: list[CatalogueWorkspaceImpact] = Field(default_factory=list)
    undoable: bool = True
    undo_partial: bool = False
    reversible_count: int = Field(default=0, ge=0)
    irreversible_count: int = Field(default=0, ge=0)


class BulkAmbiguous(Schema):
    line: str
    candidates: list[dict[str, Any]]


class BulkNameMatch(Schema):
    """One pasted line's matching collection IDs, including ambiguous candidates.

    IDs respect the collection's permissions and selection filters/exclusions.
    Child collections return their own record IDs, not person IDs. Ambiguous
    candidates are reported without being selected automatically.
    """
    line: str
    ids: list[ID]


class BulkResolved(Schema):
    count: int = Field(ge=0)
    token: str
    sample: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    matches: list[BulkNameMatch] = Field(default_factory=list,
        description='One entry per names line, in pasted order including duplicates; empty when names is absent. '
                    'Total matching IDs are bounded by BULK_LIMITS.resolve_candidates; excessive expansion returns 422.')
    unmatched: list[str] = Field(default_factory=list)
    ambiguous: list[BulkAmbiguous] = Field(default_factory=list)
    by_parent: dict[str, int] = Field(default_factory=dict)


class BulkSort(Schema):
    field: str = Field(min_length=1, max_length=160)
    direction: Literal['asc', 'desc'] = 'asc'


class BulkRowsInput(Schema):
    selection: BulkSelection
    columns: list[str] = Field(default_factory=list, max_length=100)
    sort: list[BulkSort] = Field(default_factory=list, max_length=10)
    group: str | None = Field(default=None, max_length=160)
    offset: int = Field(default=0, ge=0, le=5000, strict=True)
    limit: int = Field(default=100, ge=1, le=5000, strict=True)


class BulkRows(Schema):
    count: int = Field(ge=0)
    total: int | None = Field(default=None, ge=0)
    token: str | None = None
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    has_more: bool = False
    rows: list[dict[str, Any]]
    groups: list[dict[str, Any]] = Field(default_factory=list)


class BulkCollectionCount(Schema):
    count: int = Field(ge=0)


class BulkCollections(Schema):
    """Readable record types and counts for one department; unreadable types are omitted."""
    collections: dict[BulkCollection, BulkCollectionCount]


class BulkStageInput(Schema):
    payload: BulkApply
    reason: Reason | None = None


class BulkStagedRow(Schema):
    id: ID
    revision: int = Field(ge=1)
    created_at: datetime
    department_id: ID
    owner_id: ID
    payload: BulkApply | dict[str, Any]
    expires_at: datetime
    status: Literal['active', 'waiting', 'dropped', 'applied', 'expired']
    review_role: RoleName | None = None


class BulkStagedList(Schema):
    items: list[BulkStagedRow]
    count: int = Field(ge=0)


class BulkApplyResult(Schema):
    batch_id: ID
    count: int = Field(ge=0)
    item_kinds: list[BulkItemKind]
    items: list[BulkPreviewItem]
    rows: list[BulkPreviewItem]
    counts: dict[str, int]
    groups: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list,
        description='New validator findings, tagged with workspace_id, from the actual after-write impact check '
                    'for session-creation batches and receipt-bound inverses. Draft creation may succeed with '
                    'error-severity findings; saved does not mean compliant. Empty for other apply paths.')
    impacts: list[CatalogueWorkspaceImpact] = Field(default_factory=list,
        description='Per-workspace before/after counts and introduced/resolved findings from that actual '
                    'after-write check. Session IDs refer to saved sessions, never preview trials. '
                    'Other apply paths retain their per-item command results.')
    undo: Command | None = None
    undoable: bool = True
    undo_partial: bool = False
    reversible_count: int = Field(default=0, ge=0)
    irreversible_count: int = Field(default=0, ge=0)
    staged: BulkStagedRow | None = None
    staged_count: int = Field(default=0, ge=0)
    reviews: list[BulkStagedRow] = Field(default_factory=list)
    authorization_commands: list[Command] = Field(default_factory=list)
    # A small single-workspace batch may be patched in place; broad batches use
    # null and let the client request the authoritative workspace projection.
    delta: DraftEditDelta | None = None
    workspace: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    fix_list: dict[str, Any] | None = None
    fix_list_delta: dict[str, Any] | None = None
    receipt_id: ID | None = None
    activity: int | None = Field(default=None, ge=0)
    message: str


# Public presentation metadata: the generated client reads the same supported kinds and defaults as the server.
USUAL_ASSIGNEES_FIXED_DEFAULT = SeriesDefinition.model_fields['usual_assignees_fixed'].default
WEEKDAY_SHORT_LABELS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
STRENGTH_LABELS = {strength: strength.capitalize() for strength in get_args(Strength)}
SCORE_WEIGHT_LABELS = {'coverage': 'Coverage', 'rules': 'Rules & targets', 'fairness': 'Fairness',
                       'preferences': 'Preferences', 'stability': 'Stability'}
SCORE_WEIGHT_HELP = {
    'coverage': 'Desired places filled. Required places always come first.',
    'rules': 'Advisory rules and quota targets kept.',
    'fairness': 'Work shared evenly.',
    'preferences': 'Allocation preferences and wishes met.',
    'stability': 'Few changes from the rota before a run.',
}
RULE_KINDS = (
    ('team_lead_requirement', 'Team leads', 'Require qualified named leaders for a duty.', 'One qualified leader for the whole duty'),
    ('min_rest', 'Minimum rest', 'Require a break between the end of one duty and the start of the next.', '11 hours after a night shift'),
    ('max_hours', 'Maximum hours', 'Cap reserved duty hours per day, week or rolling period.', '48 hours in any 7 days'),
    ('max_duties', 'Maximum number of duties', 'Cap the number of duties in a day, week or rolling period.', 'No more than 5 duties a week'),
    ('max_consecutive_days', 'Days in a row', 'Limit how many days in a row someone can work.', 'At most 6 days in a row'),
    ('max_consecutive_weekends', 'Consecutive weekends', 'Limit how many weekends in a row someone can work.', 'No more than 2 weekends running'),
    ('forbidden_transition', 'Next-day combination', 'Stop one duty being followed by another on the next day.', 'No clinic the morning after a night'),
    ('travel_buffer', 'Travel time', 'Leave time to move between two locations.', '30 minutes from Clinic A to UCC'),
    ('minimum_cover', 'Minimum cover', 'Require at least a number of eligible people matching a scope.', 'At least 1 consultant in UCC'),
    ('maximum_participants', 'Maximum people', 'Cap how many matching people are scheduled at once.', 'At most 3 residents per clinic'),
    ('supervision_limit', 'Supervision', 'Limit which trainees a supervisor takes, or how many.', 'Up to 2 residents per consultant'),
    ('spread_evenly', 'Spread evenly', 'Distribute selected members evenly across supported session holders.', 'Level residents across the consultants in a session'),
    ('block_quota', 'Quota or commitment', 'A minimum, maximum and target amount over a period, each week or month.', 'Between 6 and 8 session units a week, aim for 7'),
    ('balance_goal', 'Balance goal', 'Count something for a group over a period and aim for a fair share, a target or a minimum per pair.', 'Fair weekends; each resident sees each consultant twice a block'),
    ('allocation_preference', 'Allocation preference', 'Guide auto-schedule towards preferred people or sessions.', 'Prefer residents for continuity clinic'),
    ('allocation_overtime', 'Overtime suggestions', 'Let auto-schedule suggest overtime after regular staff.', 'Offer overtime for UCC nights'),
    ('restriction', 'Time restriction', 'Keep matching people off duty at recurring times.', 'No duties on Friday mornings for R1'),
    ('run_length', 'Run length', 'Prefer working days together, not too few or too many in a row.', 'At least 2 and at most 5 days in a row'),
    ('days_off_run', 'Days off together', 'Prefer days off in blocks rather than single days.', 'At least 2 days off together'),
    ('weekend_shape', 'Weekend shape', 'Prefer whole weekends on or off, or the same duty all weekend.', 'Work both weekend days or neither'),
    ('shift_succession', 'Shift order', 'Prefer forward rotation, avoid quick changes, rest after a run of evenings.', 'No morning within 2 days of an evening'),
    ('together_apart', 'Together or apart', 'Prefer named people on the same days, or on different days.', 'Keep two new residents apart'),
)
RULE_HELP = {
    'team_lead_requirement': 'Sets the minimum and maximum named leaders. Count them per duty or across matching concurrent duties. Whole-duty leaders cover every required interval; continuous cover may pass between qualified leaders.',
    'min_rest': 'After a duty ends, the person is off for at least this many hours before their next duty starts.',
    'max_hours': 'Adds up each person’s duty hours in a day, a week or a rolling window, and flags anything over the limit.',
    'max_duties': 'Counts each person’s duties in a day, a week or a rolling window, and flags anything over the limit.',
    'max_consecutive_days': 'Counts the days a person works in a row. A day without a duty starts the count again.',
    'max_consecutive_weekends': 'Counts the weekends a person works in a row. A weekend off starts the count again.',
    'forbidden_transition': 'Stops one kind of duty being followed by another on the next day, even when the rest hours are met.',
    'travel_buffer': 'Leaves enough minutes between duties at two places for the person to get from one to the other.',
    'minimum_cover': 'Each matching session needs at least this many matching people who are eligible and available.',
    'maximum_participants': 'No more than this many matching people on a session, or in a clinic, at the same time.',
    'supervision_limit': 'Limits which trainee ranks each supervisor in scope takes, or how many trainees at once.',
    'spread_evenly': 'Distributes selected members evenly across the supported holders in each session.',
    'block_quota': 'Counts duties, hours, session units or credits over a block, or each week or month, against a minimum, maximum or target. People with their own amount get a line in the per-person table.',
    'allocation_preference': 'Steers auto-schedule towards preferred people or a fairer spread. It is advice: safety checks always win.',
    'allocation_overtime': 'Lets auto-schedule suggest overtime once regular staff cannot cover. Every limit still applies.',
    'restriction': 'Keeps matching people off duty on the chosen days and times, such as protected teaching time.',
    'run_length': 'A preference for how many days in a row people work: not single isolated days, not long stretches. Auto-schedule follows it when it can; it never blocks a rota.',
    'days_off_run': 'A preference for days off in blocks (for example two together) rather than single days between duties.',
    'weekend_shape': 'A preference for whole weekends on or off, or for the same duty on each weekend day worked.',
    'shift_succession': 'A preference about which duty follows which: forward rotation, a gap after some duties, or rest after a run of evenings or nights.',
    'together_apart': 'A preference that named people work on the same days, or on different days. Only planners see it.',
}
RULE_NUMBER_FIELD = {
    kind: (PER_PERSON_FIELD[kind], label) for kind, label in {
        'block_quota': 'Amount', 'max_hours': 'Hours', 'max_duties': 'Maximum duties',
        'max_consecutive_days': 'Days in a row', 'max_consecutive_weekends': 'Weekends in a row',
        'allocation_preference': 'Priority', 'min_rest': 'Rest hours', 'minimum_cover': 'Minimum people',
        'maximum_participants': 'Maximum people', 'supervision_limit': 'Trainees', 'spread_evenly': 'Maximum gap',
    }.items()
} | {'travel_buffer': ('minutes', 'Minutes'), 'team_lead_requirement': ('minimum', 'Minimum leaders')}
RULE_TOPICS = (
    {'name': 'Staffing & cover', 'kinds': ('team_lead_requirement', 'minimum_cover', 'maximum_participants', 'supervision_limit', 'spread_evenly'),
     'icon': 'people', 'hint': 'How many people each session needs, and how selected members are distributed.', 'hue': 'teal'},
    {'name': 'Limits & rest', 'kinds': ('min_rest', 'max_hours', 'max_duties', 'max_consecutive_days', 'max_consecutive_weekends', 'forbidden_transition', 'travel_buffer'),
     'icon': 'clock', 'hint': 'Ceilings that keep workload safe.', 'hue': 'indigo'},
    {'name': 'Targets & commitments', 'kinds': ('block_quota', 'balance_goal'),
     'icon': 'chart', 'hint': 'What people should reach over a period.', 'hue': 'amber'},
    {'name': 'Restrictions & protected time', 'kinds': ('restriction',),
     'icon': 'lock', 'hint': 'Times people are kept off duty.', 'hue': 'sky'},
    {'name': 'Preferences & fairness', 'kinds': ('allocation_preference', 'allocation_overtime', 'run_length', 'days_off_run', 'weekend_shape', 'shift_succession', 'together_apart'),
     'icon': 'layers', 'hint': 'Advice for auto-schedule; never blocks a rota.', 'hue': 'violet'},
)
# An issue popover (the shared issue marker, D78) lists at most this many issues, then "Show all M in Fix list".
ISSUE_LIST_LIMIT = 5

TEAM_PROBLEM_WORDS = {
    'needs': 'Needs supervisor',
    'elsewhere': 'Not on this session',
    'unpaired': 'Unpaired',
    'over': '{n} · limit {limit}',
}


FINDING_MESSAGES = {
    'COVERAGE_GAP': 'Fewer people than the minimum — this session still needs someone',
    'COVERAGE_TARGET': 'Fewer people than the preferred number (advisory)',
    'TIME_OVERLAP': 'This person is already working at this time',
    'EXTERNAL_ROTATION': 'This person is away on another rotation',
    'NOT_ELIGIBLE': 'This person is not eligible for this clinic on this date — check their rank, membership and clinic eligibility under People',
    'MEMBERSHIP_MISSING': 'This person has no current rank in the department — add one under People',
    'ELIGIBILITY_MISSING': 'This person is not eligible for this clinic — add clinic eligibility under People',
    'PERSON_INACTIVE': 'This person is marked inactive',
    'RANK_NOT_ALLOWED': "This person's rank is not allowed for this duty",
    'TYPE_INACTIVE': 'This duty type is not active on this date — check its dates in Configuration › Duty types',
    'TYPE_VERSION_CHANGED': 'The duty type was edited after this session was added — refresh the session to use the new settings',
    'SERVICE_INACTIVE': 'This clinic is not active on this date — check its dates in Configuration › Services',
    'SERVICE_REQUIRED': 'Choose a clinic for this staffed duty',
    'LOCATION_INACTIVE': 'This place, or the building it belongs to, is not active on this date',
    'LOCATION_TYPE_INACTIVE': 'This kind of place is not active on this date — check Configuration › Location types',
    'LOCATION_TYPE': 'This duty cannot run in this kind of place',
    'LOCATION_CLOSED': 'This place is closed at this time',
    'CALENDAR_CLOSED': 'The session is outside the opening hours',
    'CALENDAR_UNKNOWN': 'The opening hours for this place are missing',
    'CALENDAR_TIME_UNRESOLVED': 'The opening hours are ambiguous at this time (e.g. a clock change) — review the calendar',
    'CAPABILITY_GAP': 'Nobody here has a required skill for the whole session',
    'DISTINCT_PERSON_GAP': 'More different qualified people are needed',
    'OPEN_EXTRA_COVER': 'Extra cover still open — nobody allowed overtime has taken it yet',
    'EXTRA_COVER_NOT_OVERTIME': 'This extra-cover duty is overtime only — mark this person as overtime or pick someone allowed overtime',
    'DURATION_LIMIT': 'The session is longer than this duty type allows',
    'MAXIMUM_PARTICIPANTS': 'More people are scheduled than allowed',
    'SURPLUS_COVER': 'More people are scheduled than the configured cover places need',
    'SUPERVISION_GAP': 'Not enough supervisors for the trainees on this session',
    'SUPERVISION_LEVEL': 'Selected members are not spread evenly across the session supervisors',
    'LEAD_NOT_QUALIFIED': 'This person cannot lead this duty — they do not meet who may lead it, or the duty type has no team lead (Configuration › Duty types › Team lead)',
    'SUPERVISION_UNCONFIGURED': 'Trainees are scheduled but no supervision rule is set up',
    'SHARED_DUTY_UNAPPROVED': 'This duty type cannot be shared between clinics',
    'RESOURCE_CAPACITY': 'Too many sessions at this place at the same time',
    'POOLED_RESOURCE_CAPACITY': 'Not enough of a shared resource (e.g. rooms) at this time',
    'RESOURCE_INACTIVE': 'A required resource is not available on this date',
    'RESOURCE_UNKNOWN': 'A required resource is missing',
    'RESOURCE_LOCATION': 'A required resource belongs to a different place',
    'PARTICIPATION_INVALID': "Someone's planned presence times are invalid",
    'REQUIREMENT_OUTSIDE_SHIFT': 'A requirement is set outside the session times',
    'RESOURCE_INTERVAL_OUTSIDE_SHIFT': 'A resource is booked outside the session times',
    'EXTERNAL_LOCATION_UNKNOWN': 'An outside duty has no known place, so travel time cannot be checked',
    'INSUFFICIENT_WHOLE_DUTY_CANDIDATES': 'Not enough people can work the whole of this duty',
    'INSUFFICIENT_CAPABILITY_CANDIDATES': 'Not enough people with the required skill can work this duty',
}


# Person columns compose the existing filter and person-input metadata; dynamic families resolve at read time.
FAIRNESS_METRIC_LABELS = {
    'duties': 'Duties', 'hours': 'Hours', 'session_units': 'Session units', 'weekends': 'Weekends worked',
    'overtime_duties': 'Overtime duties', 'overtime_hours': 'Overtime hours', 'lead_turns': 'Team lead turns',
    'supervision': 'Trainee sessions supervised', 'pair_exposure': 'Duties shared with a partner',
    'workload': 'Workload', 'wishes_granted': 'Wish points granted', 'wishes_declined': 'Wish points declined',
    'wishes_submitted': 'Wish points asked for',
}
PERSON_PROPERTY_DEFINITIONS = {
    **{field['key']: {'label': field['label'],
                      'value_kind': field.get('value_kind') or ('choice' if field.get('kind') else 'text'),
                      'source': 'condition', 'forms': ('chip', 'text')}
       for field in CONDITION_FIELD_DEFINITIONS
       if not field['session'] and field['key'] != 'person_ids'},
    **{key: {'label': BULK_COLLECTIONS['people']['columns'][key]['label'], 'value_kind': 'text',
             'source': 'person', 'forms': ('text',),
             **({'requires': 'catalogue.department'} if key == 'staff_number' else {})}
       for key, field in PersonInput.model_fields.items() if (field.json_schema_extra or {}).get('property')},
    'contact': {'label': 'Contacts', 'value_kind': 'contact', 'source': 'contact_types', 'family': True,
                'requires': 'contact_visibility', 'forms': ('text',)},
    'workload': {'label': 'Workload', 'value_kind': 'number', 'source': 'fairness', 'family': True,
                 'requires': 'fairness.read_team', 'forms': ('text',),
                 'members': {key: FAIRNESS_METRIC_LABELS[key]
                             for key in get_args(FairnessViewSettings.model_fields['metric'].annotation)}},
    'away': {'label': 'Away now', 'value_kind': 'status', 'source': 'availability',
             'requires': 'availability_visibility', 'forms': ('icon', 'text')},
}
PERSON_PROPERTY_ALIASES = {
    **{alias: 'person.' + key for alias, key in
       (('rank', 'category_ids'), ('services', 'eligible_service_ids'), ('skills', 'capability_ids'),
        ('programme', 'programme_ids'), ('tags', 'tags'), ('code', 'short_code'), ('title', 'title'),
        ('section', 'directory_section'))},
    **{alias: 'person.contact:' + typ['id'] for alias, field in
       (('phone', 'work_phone'), ('ext', 'work_extension'), ('pager', 'pager'))
       for typ in DEFAULT_CONTACT_TYPES if typ['legacy_field'] == field},
}


class DisplaySettingsRestore(Decision):
    version_id: ID


class PersonPropertyPeriod(Schema):
    starts_on: date
    ends_on: date

    @model_validator(mode='after')
    def ordered(self):
        if self.starts_on >= self.ends_on:
            raise ValueError('The period end must follow its start')
        return self


class PersonPropertiesInput(Schema):
    person_ids: list[ID] = Field(max_length=500)
    keys: list[PersonPropertyKey] = Field(max_length=30)
    period: PersonPropertyPeriod | None = None


DisplaySettingsInput.model_rebuild()
