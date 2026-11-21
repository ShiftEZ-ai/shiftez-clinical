"""Constrained canonical records. JSON is reserved for versioned definitions/evidence.

No browser-owned operational state; no name-derived identifiers. Published snapshots
are immutable projections, not write authorities. Every mutable command is audited.
"""
from packages.domain.time import now as clock_now
from datetime import date, datetime, timezone
import uuid
from typing import get_args
from sqlalchemy import (Boolean, CheckConstraint, Date, DateTime, Enum, Float, ForeignKey, Index,
                        Integer, JSON, LargeBinary, String, Text, ForeignKeyConstraint, UniqueConstraint, event, false, func, inspect, select, text, true, update)
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, Session as OrmSession, mapped_column, object_session, relationship


def now() -> datetime:
    return clock_now()


def uid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


SUPERSESSION_CURSOR_KEY = 'generation_supersessions'
SUPERSESSION_DONE = 'done'


class RecordInsertion(Base):
    """Append-only insertion ordinals; source evidence and UUIDs remain untouched."""
    __tablename__ = "record_insertions"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(80))
    record_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("table_name", "record_id"),)


class InsertionOrdered:
    """Insertion order is assigned by the insert trigger, not at commit.

    Non-serialized writers can make rows visible out of ``seq`` order. Do not build an exactly-once
    "after seq" consumer without a safe horizon; current consumers use ``seq`` only as a hint, a
    keyset UI cursor, or a tiebreak.
    """

    @hybrid_property
    def seq(self):
        db = object_session(self)
        return db.scalar(select(type(self).seq).where(type(self).id == self.id)) if db is not None else None

    @seq.inplace.expression
    @classmethod
    def _seq(cls):
        return (select(RecordInsertion.seq).where(
            RecordInsertion.table_name == cls.__tablename__, RecordInsertion.record_id == cls.id)
            .correlate_except(RecordInsertion).scalar_subquery().label("seq"))


from packages.contracts.schemas import (EVENT_HORIZON_MAX_DAYS, FEEDBACK_KINDS, FEEDBACK_LIMITS, FEEDBACK_STATUSES,
                                        DRAFT_CONFIG_DEFAULTS, DraftSafetyChange, DraftSafetyUseDefault,
                                        DraftConfigEditors, DraftConfigApproval, DraftConfigSource,
                                        DraftDefaultChange, DraftConfigReason, DraftSettingsStatus,
                                        MAX_APPROVALS_REQUIRED, MAX_WISH_POINTS, ROLE_NAMES, SWAP_KINDS, TAG_KINDS, WISH_KINDS,
                                        SESSION_IDLE_MINUTES_MIN, SESSION_MAX_HOURS_MIN,
                                        SESSION_IDLE_MINUTES_DEFAULT, SESSION_MAX_HOURS_DEFAULT)

class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class Withdrawable:
    withdrawn: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class ApplicationBranding(Record, Base):
    __tablename__ = 'application_branding'
    data: Mapped[dict] = mapped_column(JSON)
    previous: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("id = 'application'"),)


class BrandingImage(Record, Base):
    __tablename__ = 'branding_images'
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)


class BrandingIconArtifact(Record, Base):
    """Immutable, branding-derived PWA icon bytes."""
    __tablename__ = 'branding_icon_artifacts'
    source_image_id: Mapped[str | None] = mapped_column(ForeignKey('branding_images.id'))
    source_sha256: Mapped[str] = mapped_column(String(64))
    profile: Mapped[str] = mapped_column(String(40))
    purpose: Mapped[str] = mapped_column(String(24))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    __table_args__ = (UniqueConstraint('source_sha256', 'profile', 'purpose', 'width', 'height'),)


class Organisation(Record, Base):
    __tablename__ = "organisations"
    name: Mapped[str] = mapped_column(String(160))
    timezone: Mapped[str] = mapped_column(String(80), default="Asia/Riyadh")
    week_start: Mapped[int] = mapped_column(Integer, default=6)  # Python: Monday=0
    input_revision: Mapped[int] = mapped_column(Integer, default=1)
    availability_revision: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (CheckConstraint("week_start BETWEEN 0 AND 6"),)


class Department(Record, Base):
    __tablename__ = "departments"
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    catalogue_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    generation_defaults: Mapped[dict] = mapped_column(JSON, default=dict)
    wish_settings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    display_settings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    score_check_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    bulk_settings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    # Weekend days (Monday 0 … Sunday 6): the one department setting calendars shade (0056).
    weekend_days: Mapped[list] = mapped_column(JSON, default=lambda: [4, 5], server_default="[4, 5]")
    operating_calendar_id: Mapped[str | None] = mapped_column(ForeignKey("operating_calendars.id"))
    # How leave shows on planning findings: 'none', 'type' (type and dates) or 'details' (also the note) (0057).
    leave_reason_display: Mapped[str] = mapped_column(String(16), default="type", server_default="type")
    swap_offer_window_days: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    swap_open_offer_cap: Mapped[int] = mapped_column(Integer, default=25, server_default="25")
    swap_kinds_allowed: Mapped[list] = mapped_column(JSON, default=lambda: list(SWAP_KINDS), server_default='["exchange", "give"]')
    swap_open_offers: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    swap_review_route_id: Mapped[str | None] = mapped_column(ForeignKey("review_routes.id"))
    swap_auto_prepare: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    swap_auto_publish: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    policy_retire_approvals_required: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Existing NULLs retain legacy deployment defaults; new departments start with explicit finite settings.
    # Explicit None means inheritance; only omitted values receive the new-department defaults.
    session_idle_minutes: Mapped[int | None] = mapped_column(Integer().evaluates_none(), default=SESSION_IDLE_MINUTES_DEFAULT)
    session_max_hours: Mapped[int | None] = mapped_column(Integer().evaluates_none(), default=SESSION_MAX_HOURS_DEFAULT)
    session_idle_unlimited: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    session_max_unlimited: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    announcement_send_time: Mapped[str] = mapped_column(String(5), default="07:00", server_default="07:00")
    announcement_auto_reminder_days: Mapped[int | None] = mapped_column(Integer)
    announcement_reminder_interval_hours: Mapped[int] = mapped_column(Integer, default=24, server_default="24")
    event_horizon_days: Mapped[int] = mapped_column(Integer, default=365, server_default="365")
    event_receipt_window_days: Mapped[int] = mapped_column(Integer, default=14, server_default="14")
    events_checked_on: Mapped[date | None] = mapped_column(Date)
    email_settings: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    __table_args__ = (CheckConstraint("leave_reason_display IN ('none','type','details')"),
                      CheckConstraint(f"session_idle_minutes IS NULL OR session_idle_minutes >= {SESSION_IDLE_MINUTES_MIN}",
                                      name="ck_departments_session_idle"),
                      CheckConstraint(f"session_max_hours IS NULL OR session_max_hours >= {SESSION_MAX_HOURS_MIN}",
                                      name="ck_departments_session_max"),
                      CheckConstraint("swap_offer_window_days BETWEEN 1 AND 2147483647", name="ck_departments_swap_offer_window_days"),
                      CheckConstraint("swap_open_offer_cap BETWEEN 1 AND 2147483647", name="ck_departments_swap_open_offer_cap"),
                      CheckConstraint(f"policy_retire_approvals_required BETWEEN 1 AND {MAX_APPROVALS_REQUIRED}",
                                      name="ck_departments_policy_retire_approvals"),
                      CheckConstraint("announcement_auto_reminder_days IS NULL OR announcement_auto_reminder_days BETWEEN 1 AND 3650"),
                      CheckConstraint("announcement_reminder_interval_hours BETWEEN 1 AND 8760"),
                      CheckConstraint(f"event_horizon_days BETWEEN 7 AND {EVENT_HORIZON_MAX_DAYS}"),
                      CheckConstraint(f"event_receipt_window_days BETWEEN 1 AND {EVENT_HORIZON_MAX_DAYS}"))


class Identity(Record, Base):
    __tablename__ = "identities"
    issuer: Mapped[str] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(255))
    subject_normalized: Mapped[str | None] = mapped_column(
        String(765), default=lambda context: context.get_current_parameters()["subject"].casefold())
    display_name: Mapped[str] = mapped_column(String(160))
    # Password hashes exist only for explicitly bootstrapped non-production accounts.
    password_hash: Mapped[str | None] = mapped_column(Text)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), index=True)
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("issuer", "subject"),
                      Index("ix_identities_issuer_subject_normalized", "issuer", "subject_normalized"))


class Account(Record, Base):
    """A sign-in this application manages in the environment's sign-in service (apps/api/identity_store.py).
    Until the invitation is accepted the identity's subject is 'pending:<username>'; the first sign-in through the
    provider binds the provider's real subject (apps/api/oidc.py)."""
    __tablename__ = "accounts"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), unique=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(254))
    username_normalized: Mapped[str | None] = mapped_column(
        String(192), index=True, default=lambda context: context.get_current_parameters()["username"].casefold())
    email_normalized: Mapped[str | None] = mapped_column(
        String(762), index=True, default=lambda context: context.get_current_parameters()["email"].casefold())
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(Account, "before_insert")
@event.listens_for(Account, "before_update")
def normalize_account_identifiers(_mapper, _connection, account):
    account.username_normalized, account.email_normalized = account.username.casefold(), account.email.casefold()


@event.listens_for(Identity, "before_insert")
@event.listens_for(Identity, "before_update")
def normalize_identity_subject(_mapper, _connection, identity):
    identity.subject_normalized = identity.subject.casefold()


class RoleBinding(Record, Base):
    __tablename__ = "role_bindings"
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),
                     CheckConstraint("role IN (" + ",".join(f"'{r}'" for r in ROLE_NAMES) + ")"))


class Session(InsertionOrdered, Record, Base):
    __tablename__ = "sessions"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Explicitly no deadline: expires_at retains a finite value, never an infinity sentinel.
    expiry_unlimited: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_label: Mapped[str | None] = mapped_column(String(100))
    network_label: Mapped[str | None] = mapped_column(String(32))
    oidc_id_token: Mapped[str | None] = mapped_column(Text, deferred=True)
    auth_method: Mapped[str | None] = mapped_column(String(16))  # null preserves pre-migration OIDC logout behaviour
    remember_me: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)


class AuthConfiguration(Record, Base):
    """One deployment-local configuration; secrets and Undo snapshots never enter public projections."""
    __tablename__ = "auth_configuration"
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text, deferred=True)
    previous: Mapped[dict | None] = mapped_column(JSON, deferred=True)
    __table_args__ = (CheckConstraint("id = 'environment'", name="ck_auth_configuration_singleton"),)


class LoginAttempt(Record, Base):
    __tablename__ = "login_attempts"
    key_hash: Mapped[str] = mapped_column(String(64), index=True)
    succeeded: Mapped[bool] = mapped_column(Boolean)


class AuthLoginEvent(Record, Base):
    """Retained authentication audit, including failures without any known identity or department."""
    __tablename__ = "auth_login_events"
    account_hash: Mapped[str] = mapped_column(String(64))
    peer_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    correlation_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(36))  # No FK: pruning a session keeps its audit.
    __table_args__ = (Index("ix_auth_login_events_created_at", "created_at"),)


class RateLimit(Base):
    """Hashed request counters and sign-in delay/consecutive state; expired rows are disposable."""
    __tablename__ = "rate_limits"
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)


class OIDCFlow(Record, Base):
    __tablename__ = "oidc_flows"
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    force_login: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_session_id: Mapped[str | None] = mapped_column(String(36))  # flows and expired sessions are pruned independently
    verified_identity_id: Mapped[str | None] = mapped_column(String(36))  # server-held result, never client claims
    nonce: Mapped[str] = mapped_column(String(128))
    verifier: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    remember_me: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    config_revision: Mapped[int | None] = mapped_column(Integer)


class Effective:
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DepartmentRecord:
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"), index=True)


class IntegrationCredential(DepartmentRecord, Base):
    __tablename__ = "integration_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32))
    repo: Mapped[str] = mapped_column(String(300))
    token_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    token_hint: Mapped[str] = mapped_column(String(4))
    connected_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (UniqueConstraint("department_id", "kind"),
                      CheckConstraint("kind = 'github_issues'"), CheckConstraint("revision >= 1"))


class FeedbackReport(InsertionOrdered, Record, DepartmentRecord, Base):
    """Private report context. The screenshot is retained only for 180 days, never in JSON evidence."""
    __tablename__ = "feedback_reports"
    reporter_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"))
    reporter_identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    kind: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    expected: Mapped[str | None] = mapped_column(Text)
    route: Mapped[str] = mapped_column(String(FEEDBACK_LIMITS["route"]))
    recent_routes: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    view_state: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    page_title: Mapped[str] = mapped_column(String(160), default="", server_default="")
    app_version: Mapped[str] = mapped_column(String(80), default="", server_default="")
    build_commit: Mapped[str] = mapped_column(String(80), default="", server_default="")
    environment: Mapped[str] = mapped_column(String(16))
    role_names: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    viewport: Mapped[dict] = mapped_column(JSON)
    theme: Mapped[str] = mapped_column(String(16), default="system", server_default="system")
    user_agent: Mapped[str] = mapped_column(String(FEEDBACK_LIMITS["user_agent"]), default="", server_default="")
    recent_errors: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    request_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    mark: Mapped[dict | None] = mapped_column(JSON)
    # shortcut: private DB bytes up to 2 MB per report; use upload storage if measured database growth needs it.
    screenshot: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    screenshot_media_type: Mapped[str | None] = mapped_column(String(16))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="new", server_default="new")
    duplicate_of: Mapped[str | None] = mapped_column(ForeignKey("feedback_reports.id"))
    resolved_in: Mapped[str | None] = mapped_column(String(80))
    triage_note: Mapped[str | None] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    github_issue_url: Mapped[str | None] = mapped_column(String(2000))
    github_export_repo: Mapped[str | None] = mapped_column(String(300))
    __table_args__ = (
        CheckConstraint("kind IN (" + ",".join(f"'{kind}'" for kind in FEEDBACK_KINDS) + ")"),
        CheckConstraint("status IN (" + ",".join(f"'{status}'" for status in FEEDBACK_STATUSES) + ")"),
        CheckConstraint("environment IN ('dev','production')"),
        CheckConstraint(f"length(message) BETWEEN 1 AND {FEEDBACK_LIMITS['message']}"),
        CheckConstraint(f"expected IS NULL OR length(expected) <= {FEEDBACK_LIMITS['expected']}"),
        CheckConstraint(f"length(user_agent) <= {FEEDBACK_LIMITS['user_agent']}"),
        CheckConstraint("occurrences >= 1"),
        CheckConstraint("duplicate_of IS NULL OR duplicate_of <> id"),
        CheckConstraint(f"screenshot IS NULL OR length(screenshot) <= {FEEDBACK_LIMITS['screenshot_bytes']}"),
        CheckConstraint("(screenshot IS NULL AND screenshot_media_type IS NULL) OR "
                        "(screenshot IS NOT NULL AND screenshot_media_type IS NOT NULL "
                        "AND screenshot_media_type IN ('image/png','image/webp'))"),
        Index("ix_feedback_reports_department_status_created", "department_id", "status", "created_at"),
        Index("ix_feedback_reports_department_fingerprint_created", "department_id", "fingerprint", "created_at"),
        Index("ix_feedback_reports_department_reporter_created", "department_id", "reporter_identity_id", "created_at"),
        Index("ix_feedback_reports_person_created", "reporter_person_id", "created_at"),
        Index("ix_feedback_reports_identity_created", "reporter_identity_id", "created_at"),
        Index("ix_feedback_reports_created_at", "created_at"),
    )


class LeaveType(Record, DepartmentRecord, Base):
    __tablename__ = "leave_types"
    short_name: Mapped[str] = mapped_column(String(40), default="", server_default="")
    name: Mapped[str] = mapped_column(String(160))
    short_code: Mapped[str] = mapped_column(String(6))
    colour: Mapped[str] = mapped_column(String(16), default="", server_default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    counts_as_unavailable: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    __table_args__ = (UniqueConstraint("department_id", "name"), UniqueConstraint("department_id", "short_code"),
                     CheckConstraint("sort_order BETWEEN 0 AND 100000"))


class AccountLink(InsertionOrdered, Record, DepartmentRecord, Base):
    """A one-time invitation or password-reset link. Only the SHA-256 of the random token is stored."""
    __tablename__ = "account_links"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("purpose IN ('invite','reset')"),)


class Category(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = "categories"
    sort_order: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    colour: Mapped[str] = mapped_column(String(16), default="", server_default="")
    short_code: Mapped[str] = mapped_column(String(6), server_default="")


class Capability(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = "capabilities"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")


class LocationType(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "location_types"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")


class Location(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "locations"
    name: Mapped[str] = mapped_column(String(160))
    location_type_id: Mapped[str] = mapped_column(ForeignKey("location_types.id"))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"))
    calendar_id: Mapped[str | None] = mapped_column(ForeignKey("operating_calendars.id"))
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (CheckConstraint("capacity > 0 AND capacity <= 1000"),)


class OperatingCalendar(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "operating_calendars"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    definition: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),)


class DateSet(Record, DepartmentRecord, Base):
    __tablename__ = "date_sets"
    name: Mapped[str] = mapped_column(String(160))
    entries: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class ResourcePool(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "resource_pools"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    capacity: Mapped[int] = mapped_column(Integer)
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"))
    calendar_id: Mapped[str | None] = mapped_column(ForeignKey("operating_calendars.id"))
    __table_args__ = (CheckConstraint("capacity > 0 AND capacity <= 1000"),
                     CheckConstraint("ends_on IS NULL OR ends_on > starts_on"))


class LocationClosure(Record, DepartmentRecord, Base):
    __tablename__ = "location_closures"
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (CheckConstraint('"end" > start'),)


class Service(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = "services"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(16), default="unspecified", server_default="unspecified")
    calendar_id: Mapped[str | None] = mapped_column(ForeignKey("operating_calendars.id"))
    colour: Mapped[str] = mapped_column(String(16), default="", server_default="")
    __table_args__ = (CheckConstraint("mode IN ('unspecified','physical','virtual')"),)


class ContactType(Record, Effective, DepartmentRecord, Base):
    __tablename__ = 'contact_types'
    department_id: Mapped[str] = mapped_column(ForeignKey('departments.id'), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(16))
    icon: Mapped[str] = mapped_column(String(40), default='')
    sort_order: Mapped[int] = mapped_column(Integer, default=1000)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    one_tap: Mapped[bool] = mapped_column(Boolean, default=False)
    action_off: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    dial: Mapped[dict] = mapped_column(JSON, default=dict)
    visibility: Mapped[dict] = mapped_column(JSON)
    format: Mapped[dict] = mapped_column('validation_format', JSON, key='format', default=dict)
    person_may_change: Mapped[bool] = mapped_column(Boolean, default=False)
    edited_by: Mapped[str] = mapped_column(String(24), default='admins')
    legacy_field: Mapped[str | None] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint('department_id', 'id'), UniqueConstraint('department_id', 'legacy_field'))


class PersonContact(Record, DepartmentRecord, Base):
    __tablename__ = 'person_contacts'
    person_id: Mapped[str] = mapped_column(ForeignKey('people.id'), index=True)
    contact_type_id: Mapped[str] = mapped_column(String(36))
    value: Mapped[str] = mapped_column(String(254))
    visibility_override: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint('department_id', 'person_id', 'contact_type_id'),
        ForeignKeyConstraint(['department_id', 'contact_type_id'], ['contact_types.department_id', 'contact_types.id']))


class Person(Record, Base):
    __tablename__ = "people"
    organisation_id: Mapped[str] = mapped_column(ForeignKey("organisations.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    given_name: Mapped[str] = mapped_column(String(160), server_default="")
    family_name: Mapped[str] = mapped_column(String(160), server_default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    inactive_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    title: Mapped[str] = mapped_column(String(160), default="", server_default="")
    work_email: Mapped[str] = mapped_column(String(254), default="", server_default="")
    work_phone: Mapped[str] = mapped_column(String(80), default="", server_default="")
    work_extension: Mapped[str] = mapped_column(String(80), default="", server_default="")
    staff_number: Mapped[str] = mapped_column(String(80), default="", server_default="")
    # Server default only: the ORM leaves it out of inserts, so code seeding an older schema (migration tests) still works.
    pager: Mapped[str] = mapped_column(String(40), default="", server_default="")
    directory_section: Mapped[str] = mapped_column(String(160), default="", server_default="")
    directory_order: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    short_code: Mapped[str] = mapped_column(String(6), default="", server_default="")
    rota_name: Mapped[str] = mapped_column(String(160), default="", server_default="")
    rota_aliases: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    tags: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    colour: Mapped[str] = mapped_column(String(16), default="", server_default="")


class Tag(Record, DepartmentRecord, Base):
    """A department label (e.g. VIP_Clinics) for the kinds in `labels`. Rules, filters and views name the tag, so
    whoever is a member at that moment is included (packages/domain/conditions.with_tags)."""
    __tablename__ = "tags"
    short_name: Mapped[str] = mapped_column(String(40), default="", server_default="")
    name: Mapped[str] = mapped_column(String(40))
    colour: Mapped[str] = mapped_column(String(16), default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    labels: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    __table_args__ = (UniqueConstraint("department_id", "name"),)


class TagMember(Record, DepartmentRecord, Base):
    """One record (person, clinic, place, duty type, programme or rule) labelled by one tag: a reference, not a copy."""
    __tablename__ = "tag_members"
    tag_id: Mapped[str] = mapped_column(ForeignKey("tags.id"), index=True)
    tag: Mapped["Tag"] = relationship()
    kind: Mapped[str] = mapped_column(String(24))
    member_id: Mapped[str] = mapped_column(String(36), index=True)
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("tag_id", "kind", "member_id"),
                      CheckConstraint("kind IN (" + ",".join(f"'{k}'" for k in TAG_KINDS) + ")"),
                      CheckConstraint("(ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on)"
                                      " AND (kind = 'people' OR (starts_on IS NULL AND ends_on IS NULL))",
                                      name="ck_tag_members_dates"))


class RuleAcknowledgement(Record, DepartmentRecord, Base):
    """A Rules › Needs attention issue someone acknowledged for the whole department (created_at = when). It holds while
    the issue's fingerprint (packages/domain/rule_lines.fingerprint) is the same: a change to its lines expires it."""
    __tablename__ = "rule_acknowledgements"
    fingerprint: Mapped[str] = mapped_column(String(64))
    acknowledged_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    note: Mapped[str] = mapped_column(Text, default="", server_default="")
    __table_args__ = (UniqueConstraint("department_id", "fingerprint"),)


class DepartmentContent(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "department_content"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(24), default="announcement")
    url: Mapped[str] = mapped_column(String(2000), default="")
    # Who sees it (and is told, when it is sent to their inbox); see services/audiences.py.
    audience: Mapped[dict] = mapped_column(JSON, default=lambda: {"everyone": True}, server_default='{"everyone": true}')
    importance: Mapped[str] = mapped_column(String(16), default="normal", server_default="normal")
    sent_outbox_id: Mapped[str | None] = mapped_column(ForeignKey("outbox.id"))
    receipts_checked_on: Mapped[date | None] = mapped_column(Date)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_empty_checked_on: Mapped[date | None] = mapped_column(Date)
    also_email: Mapped[bool | None] = mapped_column(Boolean)
    email_sender: Mapped[str | None] = mapped_column(String(16))
    email_sender_identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("kind IN ('announcement','link')"),
                      CheckConstraint("importance IN ('normal','important','must_acknowledge')"),
                      Index("ix_department_content_active_dates", "department_id", "active", "starts_on"))


class EventType(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "event_types"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    colour: Mapped[str] = mapped_column(String(16), default="", server_default="")
    default_start_time: Mapped[str] = mapped_column(String(5), default="09:00", server_default="09:00")
    default_length_minutes: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    default_audience: Mapped[dict] = mapped_column(JSON, default=lambda: {"everyone": True}, server_default='{"everyone": true}')
    attendance: Mapped[str] = mapped_column(String(20), default="expected", server_default="expected")
    blocks_duties: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    calendar: Mapped[str] = mapped_column(String(16), default="everyone", server_default="everyone")
    notify_inbox: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    also_email: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    rsvp: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    default_tag_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    __table_args__ = (CheckConstraint("default_length_minutes BETWEEN 1 AND 10080"),
                      CheckConstraint("attendance IN ('optional','expected','must_acknowledge')"),
                      CheckConstraint("calendar IN ('everyone','going_only','none')"))


class Event(Record, DepartmentRecord, Base):
    __tablename__ = "events"
    event_type_id: Mapped[str] = mapped_column(ForeignKey("event_types.id"), index=True)
    heading: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    format: Mapped[str] = mapped_column(String(16), default="in_person", server_default="in_person")
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"))
    place: Mapped[str] = mapped_column(String(500), default="", server_default="")
    url: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    organiser_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    presenter_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    audience: Mapped[dict] = mapped_column(JSON, default=lambda: {"everyone": True}, server_default='{"everyone": true}')
    repeat: Mapped[dict | None] = mapped_column(JSON)
    repeat_until: Mapped[date | None] = mapped_column(Date)
    skip_date_set_id: Mapped[str | None] = mapped_column(String(36))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    max_attendees: Mapped[int | None] = mapped_column(Integer)
    also_email: Mapped[bool | None] = mapped_column(Boolean)
    email_sender: Mapped[str | None] = mapped_column(String(16))
    email_sender_identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    occurrence_data: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    __table_args__ = (CheckConstraint('"end" > start'),
                      CheckConstraint("format IN ('in_person','online','hybrid')"),
                      CheckConstraint("max_attendees IS NULL OR max_attendees > 0"),
                      Index("ix_events_department_start", "department_id", "start"))


class EventOccurrence(Record, DepartmentRecord, Base):
    __tablename__ = "event_occurrences"
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), index=True)
    original_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)
    overridden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    __table_args__ = (UniqueConstraint("event_id", "original_start"), CheckConstraint('"end" > start'),
                      Index("ix_event_occurrences_department_start", "department_id", "start"))


class AnnouncementReceipt(InsertionOrdered, Record, Base):
    __tablename__ = "announcement_receipts"
    content_id: Mapped[str | None] = mapped_column(ForeignKey("department_content.id"))
    event_occurrence_id: Mapped[str | None] = mapped_column(ForeignKey("event_occurrences.id"), index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response: Mapped[str | None] = mapped_column(String(16))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("content_id", "identity_id"),
                      UniqueConstraint("event_occurrence_id", "identity_id"),
                      CheckConstraint("(content_id IS NOT NULL AND event_occurrence_id IS NULL) OR "
                                      "(content_id IS NULL AND event_occurrence_id IS NOT NULL)"),
                      CheckConstraint("response IS NULL OR response IN ('going','not_going')"))


class ReferenceImport(InsertionOrdered, Record, DepartmentRecord, Base):
    """Immutable source evidence, never a parallel operational schedule."""
    __tablename__ = "reference_imports"
    archive_sha256: Mapped[str] = mapped_column(String(64))
    source_name: Mapped[str] = mapped_column(String(160))
    manifest: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("department_id", "archive_sha256"),)


class SourceMapping(Record, DepartmentRecord, Base):
    __tablename__ = "source_mappings"
    source_name: Mapped[str] = mapped_column(String(160))
    source_key: Mapped[str] = mapped_column(String(300))
    target_kind: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("department_id", "source_name", "source_key"),)


class Membership(Withdrawable, InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "memberships"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),)


class Eligibility(Withdrawable, InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "eligibilities"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),)


class CapabilityGrant(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "capability_grants"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("capabilities.id"))
    service_id: Mapped[str | None] = mapped_column(ForeignKey("services.id"))
    granted_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    evidence: Mapped[str] = mapped_column(String(500))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),)


class AssignmentType(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = "assignment_types"
    name: Mapped[str] = mapped_column(String(160))
    short_code: Mapped[str] = mapped_column(String(6), default="", server_default="")
    description: Mapped[str] = mapped_column(Text, default="")
    definition: Mapped[dict] = mapped_column(JSON)


class DefinitionHistory(Record, DepartmentRecord, Base):
    __tablename__ = "definition_history"
    batch_id: Mapped[str | None] = mapped_column(String(36), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    object_id: Mapped[str] = mapped_column(String(36), index=True)
    object_revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("kind", "object_id", "object_revision"),)


class Policy(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = "policies"
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(40))
    definition: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    owner: Mapped[str] = mapped_column(String(160))
    source: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("status IN ('proposed','active','retired')"),)


Index('ix_policy_catalogue_history', Policy.department_id,
      Policy.definition['rule']['id'].as_string(),
      func.coalesce(Policy.definition['rule']['catalogue_version'].as_integer(), 1),
      Policy.created_at.desc(), sqlite_where=Policy.kind == 'catalogue', postgresql_where=Policy.kind == 'catalogue')


class PolicySeedLine(Record, DepartmentRecord, Base):
    """Immutable default lines appended without rewriting existing approved definitions or their references."""
    __tablename__ = 'policy_seed_lines'
    policy_id: Mapped[str] = mapped_column(ForeignKey('policies.id'), index=True)
    policy_revision: Mapped[int] = mapped_column(Integer)
    line: Mapped[dict] = mapped_column(JSON)


class TypeLeadOwnership(Record, DepartmentRecord, Base):
    """Append-only ownership receipt; retiring Rules never resurrects a legacy type setting."""
    __tablename__ = 'type_lead_ownership'
    assignment_type_id: Mapped[str] = mapped_column(ForeignKey('assignment_types.id'))
    source_type_revision: Mapped[int] = mapped_column(Integer)
    source_history_id: Mapped[str] = mapped_column(ForeignKey('definition_history.id'))
    source_lead_hash: Mapped[str] = mapped_column(String(64))
    initial_policy_id: Mapped[str] = mapped_column(ForeignKey('policies.id'))
    initial_line_id: Mapped[str] = mapped_column(String(36))
    effective_on: Mapped[date] = mapped_column(Date)
    __table_args__ = (UniqueConstraint('department_id', 'assignment_type_id'),
                      CheckConstraint('source_type_revision >= 1 AND revision = 1'))


class PolicyException(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "policy_exceptions"
    policy_id: Mapped[str] = mapped_column(ForeignKey("policies.id"))
    policy_revision: Mapped[int] = mapped_column(Integer)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    __table_args__ = (CheckConstraint("ends_on > starts_on"),)


class CatalogueConversion(InsertionOrdered, Record, DepartmentRecord, Base):
    """One reviewed department conversion; its plan and proof never overwrite source evidence."""
    __tablename__ = "catalogue_conversions"
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    proposed_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500))
    input_revision: Mapped[int] = mapped_column(Integer)
    plan_hash: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    plan: Mapped[dict] = mapped_column(JSON)
    proof: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("status IN ('proposed','active','declined','retired')"),
                     Index("uq_catalogue_conversion_pending", "department_id", unique=True,
                           sqlite_where=(status == "proposed"), postgresql_where=(status == "proposed")))


class SchedulingFact(InsertionOrdered, Record, DepartmentRecord, Base):
    """Department-owned immutable definition versions; approvals change only their lifecycle."""
    __tablename__ = "scheduling_facts"
    kind: Mapped[str] = mapped_column(String(24))
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), index=True)
    calendar_entry_id: Mapped[str | None] = mapped_column(ForeignKey("programme_calendar_entries.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    on: Mapped[bool] = mapped_column(Boolean, default=True)
    replaces_id: Mapped[str | None] = mapped_column(ForeignKey("scheduling_facts.id"), index=True)
    replaces_revision: Mapped[int | None] = mapped_column(Integer)
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    proposed_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500))
    migration_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("catalogue_conversions.id"), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(ForeignKey("requests.id"), index=True)
    __table_args__ = (CheckConstraint("kind IN ('workload','target','overtime_offer','protected_time')"),
                     CheckConstraint("status IN ('proposed','active','retired','declined')"),
                     CheckConstraint("kind = 'protected_time' OR person_id IS NOT NULL"),
                     CheckConstraint("kind <> 'protected_time' OR person_id IS NOT NULL OR calendar_entry_id IS NOT NULL"),
                     CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"),
                     Index("ix_scheduling_facts_department_status", "department_id", "status"))


class RuleSourceBinding(Record, DepartmentRecord, Base):
    """Frozen source identity/precedence, never a second scheduling definition."""
    __tablename__ = "rule_source_bindings"
    migration_proposal_id: Mapped[str | None] = mapped_column(ForeignKey("catalogue_conversions.id"), index=True)
    source_policy_id: Mapped[str] = mapped_column(ForeignKey("policies.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("migration_proposal_id", "source_policy_id", "source_revision"),)


class Programme(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = "programmes"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"), index=True)


class ProgrammeCalendarEntry(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = 'programme_calendar_entries'
    programme_id: Mapped[str] = mapped_column(ForeignKey('programmes.id'), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default='')
    kind: Mapped[str] = mapped_column(String(24))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    __table_args__ = (CheckConstraint('ends_on > starts_on'),
                     CheckConstraint("kind IN ('academic_block','break','notice')"))


class Mentorship(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = 'mentorships'
    person_id: Mapped[str] = mapped_column(ForeignKey('people.id'), index=True)
    mentor_id: Mapped[str] = mapped_column(ForeignKey('people.id'))
    programme_id: Mapped[str | None] = mapped_column(ForeignKey('programmes.id'))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    # An attachment: for these duty types and clinics (empty: all); required pairs the two, preferred is scored.
    assignment_type_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    service_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    strength: Mapped[str] = mapped_column(String(12), default='preferred', server_default='preferred')
    __table_args__ = (CheckConstraint('ends_on > starts_on'), CheckConstraint('person_id <> mentor_id'))


class ProgrammeMembership(Withdrawable, InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "programme_memberships"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    programme_id: Mapped[str] = mapped_column(ForeignKey("programmes.id"))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    # An external rotator: a trainee of another programme placed here and managed by this department's programme lead.
    external: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    home_programme: Mapped[str] = mapped_column(String(160), default="", server_default="")
    __table_args__ = (CheckConstraint("ends_on > starts_on"),)


class Cohort(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "cohorts"
    name: Mapped[str] = mapped_column(String(160))
    programme_id: Mapped[str] = mapped_column(ForeignKey("programmes.id"))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    __table_args__ = (CheckConstraint("ends_on > starts_on"),)


class RotationType(InsertionOrdered, Record, Effective, DepartmentRecord, Base):
    __tablename__ = 'rotation_types'
    short_name: Mapped[str] = mapped_column(String(40), default="", server_default="")
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default='')
    leave_policy: Mapped[str] = mapped_column(String(24), default='not_allowed', server_default='not_allowed')
    __table_args__ = (CheckConstraint("leave_policy IN ('not_allowed','needs_approval','allowed')"),)


class Rotation(Withdrawable, InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "rotations"
    rotation_type_id: Mapped[str | None] = mapped_column(ForeignKey("rotation_types.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    cohort_id: Mapped[str] = mapped_column(ForeignKey("cohorts.id"))
    service_id: Mapped[str | None] = mapped_column(ForeignKey("services.id"))
    # Retained for old database/snapshot readers; live mentors come from the attachment.
    mentor_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), deferred=True)
    attachment_link: Mapped["RotationAttachment | None"] = relationship(lazy="selectin", viewonly=True)

    @hybrid_property
    def attachment_mentor_id(self):
        attachment = self.attachment_link.mentorship if self.attachment_link else None
        if attachment is None and self.attachment_link and self.withdrawn and (db := object_session(self)):
            attachment = db.get(Mentorship, self.attachment_link.mentorship_id,
                                execution_options={'include_withdrawn': True})
        return attachment.mentor_id if attachment and attachment.person_id == self.person_id and attachment.department_id == self.department_id else None

    @attachment_mentor_id.expression
    def attachment_mentor_id(cls):
        return select(Mentorship.mentor_id).join(RotationAttachment, RotationAttachment.mentorship_id == Mentorship.id).where(
            RotationAttachment.rotation_id == cls.id, Mentorship.person_id == cls.person_id,
            Mentorship.department_id == cls.department_id).correlate(cls).scalar_subquery()

    department_available: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    __table_args__ = (CheckConstraint("ends_on > starts_on"),
                      CheckConstraint("service_id IS NOT NULL OR rotation_type_id IS NOT NULL", name="rotation_has_definition"),
                      Index("ix_rotations_department_person_window", "department_id", "person_id", "starts_on", "ends_on"))


class RotationAttachment(Base):
    __tablename__ = "rotation_attachments"
    rotation_id: Mapped[str] = mapped_column(ForeignKey("rotations.id"), primary_key=True)
    mentorship_id: Mapped[str] = mapped_column(ForeignKey("mentorships.id"), index=True)
    synced_revision: Mapped[int | None] = mapped_column(Integer)
    mentorship: Mapped["Mentorship"] = relationship(lazy="joined")


class RotationTemplate(Record, DepartmentRecord, Base):
    """A prebuilt rotation plan: blocks in order ([{rotation_type_id, service_id, length, unit, department_available}]),
    applied to trainees from a start date (apps/api/services/training.plan)."""
    __tablename__ = "rotation_templates"
    name: Mapped[str] = mapped_column(String(160))
    programme_id: Mapped[str | None] = mapped_column(ForeignKey("programmes.id"), index=True)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    blocks: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class TraineeNote(InsertionOrdered, Record, DepartmentRecord, Base):
    """A programme lead's note on a trainee. Read and written only by programme leads; never shown to the trainee."""
    __tablename__ = "trainee_notes"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    author_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retracted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Workspace(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "workspaces"
    access_version_id: Mapped[str | None] = mapped_column(ForeignKey("workspace_access_versions.id", use_alter=True))
    access_version: Mapped['WorkspaceAccessVersion | None'] = relationship(
        'WorkspaceAccessVersion', foreign_keys=[access_version_id], lazy='joined')
    name: Mapped[str] = mapped_column(String(160))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    origin: Mapped[str] = mapped_column(String(16), default="planner", server_default="planner")
    content_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    last_edited_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    base_publication_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id", use_alter=True))
    validation: Mapped[dict | None] = mapped_column(JSON)
    submitted_fingerprint: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_snapshot: Mapped[dict | None] = mapped_column(JSON)
    urgency: Mapped[str] = mapped_column(String(16), default="routine", server_default="routine")
    urgency_reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    target_overrides: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    # This rota's changes to department rules: switch a rule off, or use another number (reviewed with the rota).
    rule_adjustments: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    # Advisory issues accepted as they are (Schedule › Resolve issues › Accept): [{key, by, at}], issue_key of each finding.
    accepted_issues: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    # Set when a draft is discarded: it leaves lists, counts and checks, and can be restored.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Archive provenance is retained while hidden; archive_state is the exact
    # pre-archive lifecycle/review snapshot used by Restore.
    archived_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    archive_reason: Mapped[str | None] = mapped_column(String(500))
    archive_state: Mapped[dict | None] = mapped_column(JSON)
    # An approver's return: {by, by_name, at, reason}; shown on the draft until it is submitted again.
    return_note: Mapped[dict | None] = mapped_column(JSON)
    # "Replace from a date": this draft replaces that publication from its own first day; on publish the old one stays
    # in force only until then (Publication.effective_until).
    replaces_publication_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id", use_alter=True))
    __table_args__ = (Index("ix_workspaces_archived_id", "archived_at", "id"),
                     CheckConstraint("ends_on > starts_on"),
                     CheckConstraint("origin IN ('planner','auto','import')", name="ck_workspaces_origin"),
                     CheckConstraint("status IN ('draft','submitted','approved','published','superseded')"))


class WorkspaceForkBase(Record, DepartmentRecord, Base):
    """Immutable source/content pair captured when a private sandbox is copied."""
    __tablename__ = "workspace_fork_bases"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    source_workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    source_content_revision: Mapped[int] = mapped_column(Integer)
    base_content: Mapped[dict] = mapped_column(JSON)
    fork_content: Mapped[dict] = mapped_column(JSON)
    session_map: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str] = mapped_column(String(500))


class WorkspaceBaseline(Record, DepartmentRecord, Base):
    """Immutable department-input baseline captured for a workspace draft."""
    __tablename__ = "workspace_baselines"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    workspace_revision: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    scope_mode: Mapped[str] = mapped_column(String(16), default="department", server_default="department")
    requested_sources: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    included_sources: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    omitted_sources: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    dependency_closure: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    source_revisions: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    followed: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    safety_receipts: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    canonical_payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (UniqueConstraint("workspace_id", "workspace_revision"),
                      CheckConstraint("workspace_revision >= 1"),
                      CheckConstraint("schema_version >= 1"),
                      CheckConstraint("scope_mode IN ('department','custom')"),
                      Index("ix_workspace_baselines_workspace_created", "workspace_id", "created_at"))


class WorkspaceConfigKeep(Record, DepartmentRecord, Base):
    """Append-only acknowledgement of one department item hash for a draft."""
    __tablename__ = "workspace_config_keeps"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    item_key: Mapped[str] = mapped_column(String(160))
    department_item_hash: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (CheckConstraint("revision >= 1"),
                     Index("ix_workspace_config_keeps_item_created", "workspace_id", "item_key", "created_at"))


class DepartmentDraftSettings(Record, DepartmentRecord, Base):
    """Immutable department settings versions, including their governance evidence."""
    __tablename__ = "department_draft_settings"
    safety_change: Mapped[DraftSafetyChange] = mapped_column(
        Enum(*get_args(DraftSafetyChange), native_enum=False, create_constraint=True, length=32, name="ck_draft_safety_change"),
        default=DRAFT_CONFIG_DEFAULTS["safety_change"], server_default=DRAFT_CONFIG_DEFAULTS["safety_change"])
    safety_use_default: Mapped[DraftSafetyUseDefault] = mapped_column(
        Enum(*get_args(DraftSafetyUseDefault), native_enum=False, create_constraint=True, length=32, name="ck_draft_safety_use_default"),
        default=DRAFT_CONFIG_DEFAULTS["safety_use_default"], server_default=DRAFT_CONFIG_DEFAULTS["safety_use_default"])
    editors: Mapped[DraftConfigEditors] = mapped_column(
        Enum(*get_args(DraftConfigEditors), native_enum=False, create_constraint=True, length=32, name="ck_draft_config_editors"),
        default=DRAFT_CONFIG_DEFAULTS["editors"], server_default=DRAFT_CONFIG_DEFAULTS["editors"])
    non_safety_approval: Mapped[DraftConfigApproval] = mapped_column(
        Enum(*get_args(DraftConfigApproval), native_enum=False, create_constraint=True, length=32, name="ck_draft_config_approval"),
        default=DRAFT_CONFIG_DEFAULTS["non_safety_approval"], server_default=DRAFT_CONFIG_DEFAULTS["non_safety_approval"])
    rules_source: Mapped[DraftConfigSource] = mapped_column(
        Enum(*get_args(DraftConfigSource), native_enum=False, create_constraint=True, length=32, name="ck_draft_rules_source"),
        default=DRAFT_CONFIG_DEFAULTS["rules_source"], server_default=DRAFT_CONFIG_DEFAULTS["rules_source"])
    duties_source: Mapped[DraftConfigSource] = mapped_column(
        Enum(*get_args(DraftConfigSource), native_enum=False, create_constraint=True, length=32, name="ck_draft_duties_source"),
        default=DRAFT_CONFIG_DEFAULTS["duties_source"], server_default=DRAFT_CONFIG_DEFAULTS["duties_source"])
    score_source: Mapped[DraftConfigSource] = mapped_column(
        Enum(*get_args(DraftConfigSource), native_enum=False, create_constraint=True, length=32, name="ck_draft_score_source"),
        default=DRAFT_CONFIG_DEFAULTS["score_source"], server_default=DRAFT_CONFIG_DEFAULTS["score_source"])
    auto_source: Mapped[DraftConfigSource] = mapped_column(
        Enum(*get_args(DraftConfigSource), native_enum=False, create_constraint=True, length=32, name="ck_draft_auto_source"),
        default=DRAFT_CONFIG_DEFAULTS["auto_source"], server_default=DRAFT_CONFIG_DEFAULTS["auto_source"])
    display_source: Mapped[DraftConfigSource] = mapped_column(
        Enum(*get_args(DraftConfigSource), native_enum=False, create_constraint=True, length=32, name="ck_draft_display_source"),
        default=DRAFT_CONFIG_DEFAULTS["display_source"], server_default=DRAFT_CONFIG_DEFAULTS["display_source"])
    default_change: Mapped[DraftDefaultChange] = mapped_column(
        Enum(*get_args(DraftDefaultChange), native_enum=False, create_constraint=True, length=32, name="ck_draft_default_change"),
        default=DRAFT_CONFIG_DEFAULTS["default_change"], server_default=DRAFT_CONFIG_DEFAULTS["default_change"])
    reason_required: Mapped[DraftConfigReason] = mapped_column(
        Enum(*get_args(DraftConfigReason), native_enum=False, create_constraint=True, length=32, name="ck_draft_config_reason"),
        default=DRAFT_CONFIG_DEFAULTS["reason_required"], server_default=DRAFT_CONFIG_DEFAULTS["reason_required"])
    status: Mapped[DraftSettingsStatus] = mapped_column(
        Enum(*get_args(DraftSettingsStatus), native_enum=False, create_constraint=True, length=16, name="ck_draft_settings_status"),
        default="proposed", server_default="proposed")
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    proposed_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey("department_draft_settings.id"))
    __table_args__ = (UniqueConstraint("department_id", "revision"),
                     CheckConstraint("revision >= 1"),
                     CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"),
                     CheckConstraint("status <> 'active' OR approved_by IS NOT NULL", name="ck_draft_settings_active_approval"),
                     Index("ix_department_draft_settings_status", "department_id", "status", "revision"))


class WorkspaceMergeProposal(Record, DepartmentRecord, Base):
    """A frozen, single-use three-way merge proposal."""
    __tablename__ = "workspace_merge_proposals"
    source_workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    target_workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    source_content_revision: Mapped[int] = mapped_column(Integer)
    target_revision: Mapped[int] = mapped_column(Integer)
    target_content_revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="preview", server_default="preview")
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (CheckConstraint("status IN ('preview','applied','stale','cancelled')"),
                      Index("ix_workspace_merge_proposals_source_status", "source_workspace_id", "status"),
                      Index("ix_workspace_merge_proposals_target_status", "target_workspace_id", "status"))


class WorkspaceReadProjection(Base):
    """Disposable read work, never a clinical check or approval authority."""
    __tablename__ = 'workspace_read_projections'
    workspace_id: Mapped[str] = mapped_column(ForeignKey('workspaces.id'), primary_key=True)
    audience: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    stamp: Mapped[str | None] = mapped_column(String(64))
    value: Mapped[dict | str | None] = mapped_column(JSON)


@event.listens_for(Workspace, 'after_insert')
@event.listens_for(Workspace, 'after_update')
def store_workspace_read_check(mapper, connection, workspace):
    # Every ORM writer, including a standalone worker, maintains the same compact check.
    from .services.workspace_reads import store_check
    store_check(mapper, connection, workspace)


class ReadProjectionWarmCursor(Base):
    """Durable keyset position; workers inspect one workspace per pass without rescanning history."""
    __tablename__ = 'read_projection_warm_cursors'
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    after_id: Mapped[str | None] = mapped_column(String(36))


class WorkspaceAccessVersion(Record, Base):
    __tablename__ = 'workspace_access_versions'
    workspace_id: Mapped[str] = mapped_column(ForeignKey('workspaces.id'), index=True)
    visibility: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('workspace_access_versions.id'))
    __table_args__ = (UniqueConstraint('workspace_id', 'revision'),
        CheckConstraint("visibility IN ('private','shared')"),
        CheckConstraint("purpose IN ('sandbox','alternative','integrated')"),
        CheckConstraint("visibility <> 'private' OR purpose = 'sandbox'"))


class PlanningGroupVersion(Record, DepartmentRecord, Base):
    __tablename__ = 'planning_group_versions'
    group_id: Mapped[str] = mapped_column(String(36))
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('planning_group_versions.id'))
    __table_args__ = (UniqueConstraint('group_id', 'revision'),
        Index('ix_planning_group_department_version', 'department_id', 'group_id', 'revision'))


class PlanningSettingsVersion(Record, DepartmentRecord, Base):
    __tablename__ = 'planning_settings_versions'
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('planning_settings_versions.id'))
    __table_args__ = (UniqueConstraint('department_id', 'revision'),)


class ScoreSettingsVersion(Record, DepartmentRecord, Base):
    """Immutable department scoring weights and advisory tier multipliers."""
    __tablename__ = 'score_settings_versions'
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('score_settings_versions.id'))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (UniqueConstraint('department_id', 'revision'),
                      Index('ix_score_settings_department_revision', 'department_id', 'revision'))


class Shift(Record, Base):
    __tablename__ = "shifts"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    lineage_id: Mapped[str | None] = mapped_column(String(36))
    assignment_type_id: Mapped[str] = mapped_column(ForeignKey("assignment_types.id"))
    type_revision: Mapped[int] = mapped_column(Integer)
    type_snapshot: Mapped[dict] = mapped_column(JSON)
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id"))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    cancellation_reason: Mapped[str | None] = mapped_column(String(500))
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    series_id: Mapped[str | None] = mapped_column(ForeignKey("recurring_series.id"))
    series_revision: Mapped[int | None] = mapped_column(Integer)
    series_snapshot: Mapped[dict | None] = mapped_column(JSON)
    occurrence_on: Mapped[date | None] = mapped_column(Date, index=True)
    series_modified: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    template_skipped: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    __table_args__ = (CheckConstraint('"end" > start'),
                     Index("uq_series_occurrence", "workspace_id", "series_id", "occurrence_on", unique=True),
                     Index("ix_shifts_series_occurrence", "series_id", "occurrence_on"),
                     Index("ix_shifts_occurrence_series", "occurrence_on", "series_id"))


class ShiftService(Base):
    __tablename__ = "shift_services"
    shift_id: Mapped[str] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"), primary_key=True)
    service_id: Mapped[str] = mapped_column(ForeignKey("services.id"), primary_key=True)


class Assignment(Record, Base):
    __tablename__ = "assignments"
    shift_id: Mapped[str] = mapped_column(ForeignKey("shifts.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    participation: Mapped[str] = mapped_column(String(20), default="staff")
    overtime: Mapped[bool] = mapped_column(Boolean, default=False)
    fixed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    source: Mapped[str | None] = mapped_column(String(20))
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    source_actor_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    override_reason: Mapped[str | None] = mapped_column(String(500))
    supervisor_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"))
    team_lead: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    segments: Mapped[list] = mapped_column(JSON, nullable=False, default=list, server_default="[]")
    __table_args__ = (CheckConstraint('"end" > start'), UniqueConstraint("shift_id", "person_id"),
                     CheckConstraint("participation IN ('staff','trainee','supervisor')"),
                     Index("ix_assignments_shift_end", "shift_id", "end"))


class RecurringSeries(Record, Effective, DepartmentRecord, Base):
    __tablename__ = "recurring_series"
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    assignment_type_id: Mapped[str] = mapped_column(ForeignKey("assignment_types.id"))
    location_id: Mapped[str] = mapped_column(ForeignKey("locations.id"))
    definition: Mapped[dict] = mapped_column(JSON)


class Approval(InsertionOrdered, Record, Base):
    __tablename__ = "approvals"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    content_revision: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str] = mapped_column(String(500))


class Publication(Record, DepartmentRecord, Base):
    __tablename__ = "publications"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    sequence: Mapped[int] = mapped_column(Integer)
    starts_on: Mapped[date] = mapped_column(Date, index=True)
    ends_on: Mapped[date] = mapped_column(Date)
    current: Mapped[bool] = mapped_column(Boolean, default=True)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id"))
    published_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    snapshot: Mapped[dict] = mapped_column(JSON)
    validation: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    # First day this publication is no longer in force because a later rota replaced it from that day (None: in force
    # through ends_on). The only field besides `current` that may change; it only ever moves earlier (database guard).
    effective_until: Mapped[date | None] = mapped_column(Date)
    __table_args__ = (UniqueConstraint("department_id", "sequence"),
                     Index("ix_publication_current_period", "department_id", "current", "starts_on", "ends_on"))


class Availability(InsertionOrdered, Record, Base):
    __tablename__ = "availability"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    leave_type: Mapped[str] = mapped_column(String(80), default="", server_default="")
    leave_type_id: Mapped[str | None] = mapped_column(ForeignKey("leave_types.id"), index=True)
    manual_type_override: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Capture the type's default when recorded; later catalogue edits never unblock old absences.
    counts_as_unavailable: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    private_reason: Mapped[str] = mapped_column(String(1000))
    recorded_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    request_id: Mapped[str | None] = mapped_column(ForeignKey("requests.id"))
    retracted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint('"end" > start'),
                     CheckConstraint("kind IN ('recorded_leave','approved_leave','actual_absence','external_commitment','protected_time','off_duty')"),
                     Index("ix_availability_department_person_window", "department_id", "person_id", "start", "end", "retracted"))


class RequestRecord(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "requests"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    kind: Mapped[str] = mapped_column(String(24), default="leave")
    leave_type_id: Mapped[str | None] = mapped_column(ForeignKey("leave_types.id"), index=True)
    counts_as_unavailable: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    overtime: Mapped[dict | None] = mapped_column(JSON)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    private_reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(24), default="submitted")
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    # A rotation change: the rotation it changes and {proposed: {...}, before: {...}} (apps/api/services/training.py).
    rotation_id: Mapped[str | None] = mapped_column(ForeignKey("rotations.id"), index=True)
    details: Mapped[dict | None] = mapped_column(JSON)
    __table_args__ = (CheckConstraint('"end" > start'),
                     CheckConstraint("status IN ('submitted','approved','rejected','withdrawn')"),
                     Index("ix_requests_person_status_end", "person_id", "status", "end"))


class PublicationChangeList(Record, DepartmentRecord, Base):
    __tablename__ = 'publication_change_lists'
    publication_id: Mapped[str] = mapped_column(ForeignKey('publications.id'), unique=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey('workspaces.id'), unique=True)
    changes: Mapped[list] = mapped_column(JSON, default=list)


class AbsenceReportSettings(Record, DepartmentRecord, Base):
    __tablename__ = 'absence_report_settings'
    document_required: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint('department_id'),)


class ChangeSet(InsertionOrdered, Record, Base):
    __tablename__ = "change_sets"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    before_snapshot: Mapped[dict] = mapped_column(JSON)
    resulting_content_revision: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str] = mapped_column(String(500))
    undone: Mapped[bool] = mapped_column(Boolean, default=False)


class CommandReceipt(Record, DepartmentRecord, Base):
    __tablename__ = "command_receipts"
    private_owner_id: Mapped[str | None] = mapped_column(ForeignKey('identities.id'))
    command_kind: Mapped[str] = mapped_column(String(80))
    required_roles: Mapped[list] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    key: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("actor_id", "key"),)


class BulkStaged(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "bulk_staged"
    owner_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    review_role: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    __table_args__ = (CheckConstraint("status IN ('active','waiting','dropped','applied','expired')"),
                     Index("ix_bulk_staged_owner_status", "department_id", "owner_id", "status", "expires_at"))


class AuditEvent(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "audit_events"
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    action: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str] = mapped_column(String(500))
    # Deliberately minimised: revision links and field names, never private leave text.
    detail: Mapped[dict] = mapped_column(JSON)
    correlation_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (Index("ix_audit_events_target_action_at", "department_id", "target_id", "action", "created_at"),
                     Index("ix_audit_events_department_at", "department_id", text("created_at DESC"), text("id DESC")),
                     Index("ix_audit_events_department_actor", "department_id", "actor_id"))


class Outbox(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "outbox"
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(24), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_error: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (Index("ix_outbox_state_attempt", "state", "next_attempt_at"),)


@event.listens_for(OrmSession, "before_flush")
def bump_outbox_payload_revision(db, _context, _instances):
    """Payload assignments invalidate revision-based projections exactly once.

    Outbox.payload is a plain JSON column, so callers must assign a new mapping
    rather than mutate the loaded dictionary in place.
    """
    for row in db.dirty:
        if not isinstance(row, Outbox):
            continue
        state = inspect(row)
        if state.attrs.payload.history.has_changes() and not state.attrs.revision.history.has_changes():
            row.revision += 1


class EmailDelivery(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "email_deliveries"
    outbox_id: Mapped[str] = mapped_column(ForeignKey("outbox.id"), unique=True)
    source_outbox_id: Mapped[str] = mapped_column(ForeignKey("outbox.id"), index=True)
    receipt_id: Mapped[str | None] = mapped_column(ForeignKey("announcement_receipts.id"), index=True)
    identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"), index=True)
    event_occurrence_id: Mapped[str | None] = mapped_column(ForeignKey("event_occurrences.id"), index=True)
    sender_identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    sender_mode: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(254), default="")
    state: Mapped[str] = mapped_column(String(16), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(80))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("source_outbox_id", "receipt_id"),
                      CheckConstraint("identity_id IS NOT NULL OR person_id IS NOT NULL"),
                      CheckConstraint("sender_mode IN ('smtp','microsoft')"),
                      CheckConstraint("state IN ('queued','sent','failed','draft')"),
                      Index("ix_email_deliveries_sender_sent", "department_id", "sender_mode", "sent_at",
                            sqlite_where=text("sent_at IS NOT NULL"), postgresql_where=text("sent_at IS NOT NULL")),
                      Index("ix_email_deliveries_identity_sent", "department_id", "sender_mode", "sender_identity_id", "sent_at",
                            sqlite_where=text("sent_at IS NOT NULL"), postgresql_where=text("sent_at IS NOT NULL")))


class Notification(InsertionOrdered, Record, Base):
    __tablename__ = "notifications"
    outbox_id: Mapped[str] = mapped_column(ForeignKey("outbox.id"))
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(500))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_kind: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(36))
    __table_args__ = (UniqueConstraint("outbox_id", "identity_id"),
                      Index("ix_notifications_source_identity", "source_kind", "source_id", "identity_id"))


class PushInstallation(Record, DepartmentRecord, Base):
    """A user-authorised browser profile/Home Screen installation."""
    __tablename__ = 'push_installations'
    identity_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    # Retain provenance after disposable expired sessions are pruned.
    session_id: Mapped[str | None] = mapped_column(String(36))
    installation_hint: Mapped[str] = mapped_column(String(128))
    label: Mapped[str] = mapped_column(String(80), default='')
    platform: Mapped[str] = mapped_column(String(40), default='')
    state: Mapped[str] = mapped_column(String(16), default='enabled', server_default='enabled')
    state_reason: Mapped[str] = mapped_column(String(120), default='', server_default='')
    last_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('identity_id', 'installation_hint'),
                      Index('ix_push_installations_identity_state', 'identity_id', 'state'))


class PushSubscriptionVersion(Record, Base):
    """Encrypted endpoint and browser keys for one installation revision."""
    __tablename__ = 'push_subscription_versions'
    installation_id: Mapped[str] = mapped_column(ForeignKey('push_installations.id'))
    endpoint_ciphertext: Mapped[str] = mapped_column(Text)
    endpoint_digest: Mapped[str] = mapped_column(String(64))
    p256dh_ciphertext: Mapped[str] = mapped_column(Text)
    auth_ciphertext: Mapped[str] = mapped_column(Text)
    content_encoding: Mapped[str] = mapped_column(String(16), default='aes128gcm', server_default='aes128gcm')
    vapid_key_id: Mapped[str] = mapped_column(String(80), default='', server_default='')
    browser_expiration_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16), default='active', server_default='active')
    revoked_reason: Mapped[str] = mapped_column(String(120), default='', server_default='')
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_code: Mapped[str | None] = mapped_column(String(80))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    __table_args__ = (UniqueConstraint('installation_id', 'revision'),
                      Index('ix_push_subscription_endpoint_active', 'endpoint_digest', unique=True,
                            sqlite_where=text("state = 'active'"), postgresql_where=text("state = 'active'")),
                      Index('ix_push_subscription_installation_state', 'installation_id', 'state'),
                      Index('ix_push_subscription_installation_active', 'installation_id', unique=True,
                            sqlite_where=text("state = 'active'"), postgresql_where=text("state = 'active'")))


class DisplaySettingsVersion(Record, DepartmentRecord, Base):
    __tablename__ = 'display_settings_versions'
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('display_settings_versions.id'))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('department_id', 'revision'),)


class NotificationPolicyVersion(Record, DepartmentRecord, Base):
    __tablename__ = 'notification_policy_versions'
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('notification_policy_versions.id'))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('department_id', 'revision'), Index('ix_notification_policy_active', 'department_id', 'revision'))


class NotificationPreferenceVersion(Record, DepartmentRecord, Base):
    __tablename__ = 'notification_preference_versions'
    identity_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('notification_preference_versions.id'))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('department_id', 'identity_id', 'revision'), Index('ix_notification_preference_identity', 'department_id', 'identity_id', 'revision'))


class PushDelivery(Record, DepartmentRecord, Base):
    __tablename__ = 'push_deliveries'
    outbox_id: Mapped[str] = mapped_column(ForeignKey('outbox.id'), unique=True)
    source_outbox_id: Mapped[str] = mapped_column(ForeignKey('outbox.id'), index=True)
    notification_id: Mapped[str | None] = mapped_column(ForeignKey('notifications.id'), index=True)
    identity_id: Mapped[str] = mapped_column(ForeignKey('identities.id'), index=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey('push_subscription_versions.id'), index=True)
    policy_revision: Mapped[int] = mapped_column(Integer, default=1)
    preference_revision: Mapped[int | None] = mapped_column(Integer)
    event_id: Mapped[str] = mapped_column(String(40))
    detail_cap: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    service_status: Mapped[int | None] = mapped_column(Integer)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (UniqueConstraint('notification_id', 'subscription_id'), Index('ix_push_deliveries_due', 'department_id', 'expires_at'))


class PushEnrollment(Record, DepartmentRecord, Base):
    __tablename__ = 'push_enrollments'
    installation_id: Mapped[str] = mapped_column(ForeignKey('push_installations.id'))
    __table_args__ = (UniqueConstraint('installation_id', 'department_id'),)


class PwaSystemVersion(Record, Base):
    __tablename__ = 'pwa_system_versions'
    definition: Mapped[dict] = mapped_column(JSON)
    actor_id: Mapped[str] = mapped_column(ForeignKey('identities.id'))
    reason: Mapped[str] = mapped_column(String(500))
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('pwa_system_versions.id'))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint('revision'),)


class PushTransportLease(Base):
    __tablename__ = 'push_transport_leases'
    outbox_id: Mapped[str] = mapped_column(ForeignKey('outbox.id'), primary_key=True)
    owner: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PushAttempt(Record, Base):
    __tablename__ = 'push_attempts'
    delivery_id: Mapped[str] = mapped_column(ForeignKey('push_deliveries.id'), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint('delivery_id', 'attempt'),)


class PushProviderState(Base):
    __tablename__ = 'push_provider_states'
    origin: Mapped[str] = mapped_column(String(255), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    authorization_ciphertext: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused: Mapped[bool] = mapped_column(Boolean, default=False)


class ImportBatch(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "import_batches"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    source_name: Mapped[str] = mapped_column(String(160))
    source_hash: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="preview")
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    workspace_revision: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("workspace_id", "source_hash"),)


class AiProviderProfile(Record, DepartmentRecord, Base):
    __tablename__ = "ai_provider_profiles"
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    provider_kind: Mapped[str | None] = mapped_column(String(32))
    api_style: Mapped[str] = mapped_column(String(16), default="chat", server_default="chat")
    base_url: Mapped[str] = mapped_column(String(2000))
    model: Mapped[str] = mapped_column(String(200))
    reasoning: Mapped[str | int] = mapped_column(JSON, default='none', server_default='"none"')
    max_tokens: Mapped[int] = mapped_column(Integer, default=4096, server_default="4096")
    timeout_s: Mapped[int] = mapped_column(Integer, default=120, server_default="120")
    structured_output: Mapped[str] = mapped_column(String(16), default="json_schema", server_default="json_schema")
    allow_private_endpoint: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    key_ciphertext: Mapped[str | None] = mapped_column(Text)
    key_saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("kind IN ('openai_chat','openai_responses','anthropic','azure')"),
                     CheckConstraint("structured_output IN ('json_schema','json_object')"),
                     CheckConstraint("max_tokens BETWEEN 1 AND 1000000"),
                     CheckConstraint("timeout_s BETWEEN 1 AND 600"),
                     Index("uq_ai_provider_default", "department_id", unique=True,
                           sqlite_where=text("is_default = true AND archived_at IS NULL"),
                           postgresql_where=text("is_default = true AND archived_at IS NULL")))


class ScoreCheckImportBatch(Record, DepartmentRecord, Base):
    __tablename__ = "score_check_import_batches"
    name: Mapped[str] = mapped_column(String(160), default="", server_default="")
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    period_inferred: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    status: Mapped[str] = mapped_column(String(16), default="uploaded", server_default="uploaded")
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.9, server_default="0.9")
    manual_time_hours: Mapped[float | None] = mapped_column(Float)
    manual_time_people: Mapped[int | None] = mapped_column(Integer)
    gaps: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    use_ai: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("ai_provider_profiles.id"))
    profile_revision: Mapped[int | None] = mapped_column(Integer)
    provider_snapshot: Mapped[dict | None] = mapped_column(JSON)
    privacy_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    privacy_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    privacy_acknowledged_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("status IN ('uploaded','reading','check','saved','failed','discarded')"),
                     CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"),
                     CheckConstraint("confidence_threshold BETWEEN 0 AND 1"),
                     CheckConstraint("manual_time_hours IS NULL OR manual_time_hours >= 0"),
                     CheckConstraint("manual_time_people IS NULL OR manual_time_people >= 1"),
                     CheckConstraint("NOT use_ai OR privacy_acknowledged"),
                     Index("ix_score_check_batches_department_created", "department_id", "created_at"))


class ImportSource(Record, DepartmentRecord, Base):
    __tablename__ = "import_sources"
    batch_id: Mapped[str] = mapped_column(ForeignKey("score_check_import_batches.id"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(300))
    file_ref: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64))
    format: Mapped[str] = mapped_column(String(16))
    group: Mapped[str] = mapped_column(String(160), default="", server_default="")
    starts_on: Mapped[date | None] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    period_inferred: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    people_count_override: Mapped[int | None] = mapped_column(Integer)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    work_hours_each: Mapped[float | None] = mapped_column(Float)
    made_by: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    status: Mapped[str] = mapped_column(String(16), default="uploaded", server_default="uploaded")
    content: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    extraction: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    extraction_job_id: Mapped[str | None] = mapped_column(String(36))
    error_code: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (UniqueConstraint("batch_id", "file_ref"),
                     CheckConstraint("status IN ('uploaded','reading','ready','needs_review','failed')"),
                     CheckConstraint("work_hours_each IS NULL OR work_hours_each >= 0"),
                     CheckConstraint("ends_on IS NULL OR starts_on IS NULL OR ends_on > starts_on"))


class ScoreCheckGapDecision(Record, DepartmentRecord, Base):
    __tablename__ = "score_check_gap_decisions"
    batch_id: Mapped[str] = mapped_column(ForeignKey("score_check_import_batches.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id"))
    gap_id: Mapped[str] = mapped_column(String(300))
    action: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str | None] = mapped_column(String(36))
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"))
    replacement: Mapped[dict | None] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("action IN ('acknowledge','add','match','exclude','confirm','resolve')"),
                     Index("ix_score_check_decisions_batch_gap", "batch_id", "gap_id"))


class BenchmarkResult(Record, DepartmentRecord, Base):
    __tablename__ = "benchmark_results"
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date] = mapped_column(Date)
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    baseline_publication_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id"))
    workspace_ids: Mapped[list] = mapped_column(JSON)
    report: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (CheckConstraint("ends_on > starts_on"),
                     Index("ix_benchmark_results_period", "department_id", "starts_on", "ends_on", "created_at"))


class ScoreCheckExtractionJob(Record, DepartmentRecord, Base):
    __tablename__ = "score_check_extraction_jobs"
    source_id: Mapped[str | None] = mapped_column(ForeignKey("import_sources.id"))
    profile_id: Mapped[str] = mapped_column(ForeignKey("ai_provider_profiles.id"))
    profile_revision: Mapped[int | None] = mapped_column(Integer)
    privacy_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    result: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    __table_args__ = (CheckConstraint("kind IN ('extraction','test')"),
                     CheckConstraint("state IN ('queued','running','completed','failed','cancelled')"),
                     Index("ix_score_check_jobs_queue", "state", "created_at"),
                     Index("ix_score_check_jobs_lease", "state", "lease_expires_at"))


class AiCall(Record, DepartmentRecord, Base):
    """Call metadata only: uploaded content, prompts, response text and staff names stay out of the audit."""
    __tablename__ = "ai_calls"
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("ai_provider_profiles.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("score_check_extraction_jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    kind: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    error_code: Mapped[str | None] = mapped_column(String(80))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("job_id", "attempt"),
                     CheckConstraint("attempt >= 1"),
                     CheckConstraint("result IN ('pending','valid','invalid','error')"),
                     CheckConstraint("input_tokens IS NULL OR input_tokens >= 0"),
                     CheckConstraint("output_tokens IS NULL OR output_tokens >= 0"),
                     CheckConstraint("duration_ms IS NULL OR duration_ms >= 0"),
                     Index("ix_ai_calls_department_created", "department_id", "created_at"))


class StaffImportBatch(InsertionOrdered,Record, DepartmentRecord, Base):
    """A staff spreadsheet: parsed rows with per-row matches and errors, and what each apply created (for Undo).
    People and ranks are applied by a coordinator, clinics and skills by a credentialer, from the same batch."""
    __tablename__ = "staff_import_batches"
    source_name: Mapped[str] = mapped_column(String(160))
    source_hash: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    applied: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="preview")
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))


class Job(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "jobs"

    def _read_metadata(context):
        from .serialization import job_read_metadata
        values = context.get_current_parameters()
        return job_read_metadata(values.get('input_snapshot') or {}, values.get('result'))

    def _result_summary(context):
        from .serialization import job_result_projection
        values = context.get_current_parameters()
        return job_result_projection(values.get('kind'), values.get('result'))

    kind: Mapped[str] = mapped_column(String(32))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    requested_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    input_snapshot: Mapped[dict] = mapped_column(JSON)
    resume_state: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), deferred=True)
    resume_checkpoint: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), deferred=True)
    search_control: Mapped[dict | None] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON)
    read_metadata: Mapped[dict | None] = mapped_column(JSON, default=_read_metadata)
    result_summary: Mapped[dict | None] = mapped_column(JSON, default=_result_summary, deferred=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(120))
    progress: Mapped[dict | None] = mapped_column(JSON)
    # Generation runs record the older live runs they supersede; NULL means the worker has not backfilled the row yet.
    supersedes: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    __table_args__ = (Index("ix_jobs_workspace_kind_status", "workspace_id", "kind", "status"),
                     Index("ix_jobs_status_created", "status", "created_at"),
                     Index("ix_jobs_worker_lease", "worker_id", "status"),
                     Index("ix_jobs_status_lease", "status", text("coalesce(heartbeat_at, started_at)")))

    @classmethod
    def __declare_last__(cls):
        from sqlalchemy import event, inspect

        def clear_supersession_done_marker(connection):
            cursor = ReadProjectionWarmCursor.__table__
            connection.execute(update(cursor).where(
                cursor.c.key == SUPERSESSION_CURSOR_KEY, cursor.c.after_id == SUPERSESSION_DONE
            ).values(after_id=None))

        @event.listens_for(OrmSession, 'do_orm_execute')
        def clear_bulk_supersession_done_marker(execute_state):
            statement = execute_state.statement
            if not execute_state.is_insert or getattr(getattr(statement, 'table', None), 'name', None) != cls.__table__.name:
                return
            parameters = execute_state.parameters
            rows = parameters if isinstance(parameters, list) else [parameters or {}]
            if any(hasattr(row, 'get') and row.get('kind') == 'generation'
                   and ('supersedes' not in row or row.get('supersedes') is None) for row in rows):
                # A legacy/null generation insert makes the worker's durable done marker stale in this transaction.
                clear_supersession_done_marker(execute_state.session.connection())

        @event.listens_for(cls, 'after_insert')
        def clear_orm_supersession_done_marker(_mapper, connection, job):
            if job.kind == 'generation' and job.supersedes is None:
                clear_supersession_done_marker(connection)

        @event.listens_for(cls, 'before_update')
        def update_read_projections(_mapper, _connection, job):
            from .serialization import (job_read_metadata, job_result_metadata, job_result_projection,
                                        job_result_with_reconciliation)
            if job.kind == 'generation' and job.supersedes is None:
                clear_supersession_done_marker(_connection)
            state = inspect(job)
            metadata = job.read_metadata
            frozen = None
            if metadata is None:
                frozen = job.input_snapshot or {}
                metadata = job_read_metadata(frozen, job.result)
                job.result_summary = job_result_projection(job.kind, job.result)
            if state.attrs.input_snapshot.history.has_changes():
                frozen = job.input_snapshot
                refreshed = job_read_metadata(frozen, job_result_with_reconciliation(job, job.result))
                metadata = {**metadata, 'input_snapshot': refreshed['input_snapshot'],
                            **({'result': refreshed['result']} if frozen.get('generation_manifest') else {})}
            if state.attrs.input_fingerprint.history.has_changes():
                metadata = {**metadata, 'input_fingerprint': job.input_fingerprint}
            if state.attrs.result.history.has_changes() or state.attrs.kind.history.has_changes():
                result_metadata = job_result_metadata(job.result, include_error_detail=True)
                stored_result = metadata.get('result') or {}
                if 'generation_manifest' in stored_result:
                    manifest = stored_result['generation_manifest']
                elif frozen is not None:
                    manifest = frozen.get('generation_manifest')
                elif 'run_context' in (metadata.get('input_snapshot') or {}):
                    # Modern metadata records missing manifests; reuse loaded input without a deferred read.
                    manifest = (state.dict.get('input_snapshot') or {}).get('generation_manifest')
                else:
                    # Legacy metadata lacks the input marker; recover the manifest once from the frozen input.
                    manifest = (job.input_snapshot or {}).get('generation_manifest')
                if manifest:
                    result_metadata = {**(result_metadata or {}),
                                       'generation_manifest': manifest}
                metadata = {**metadata, 'result': result_metadata}
                job.result_summary = job_result_projection(job.kind, job.result)
            if metadata != job.read_metadata:
                job.read_metadata = metadata


class ReviewRoute(Record, DepartmentRecord, Base):
    __tablename__ = "review_routes"
    name: Mapped[str] = mapped_column(String(160))
    purpose: Mapped[str] = mapped_column(String(16), default="rota", server_default="rota")
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    definition: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    activated_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("ends_on IS NULL OR ends_on > starts_on"),
                     CheckConstraint("purpose IN ('rota','swap')"),
                     CheckConstraint("status IN ('proposed','active','retired')"))


class ReviewDelegation(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "review_delegations"
    route_id: Mapped[str] = mapped_column(ForeignKey("review_routes.id"))
    route_revision: Mapped[int] = mapped_column(Integer)
    step_key: Mapped[str] = mapped_column(String(40))
    delegator_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    delegate_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(500))
    __table_args__ = (CheckConstraint("expires_at > starts_at"),
                     CheckConstraint("delegate_id != delegator_id"))


class ReviewDecision(InsertionOrdered, Record, Base):
    __tablename__ = "review_decisions"
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    content_revision: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    route_hash: Mapped[str] = mapped_column(String(64))
    step_key: Mapped[str] = mapped_column(String(40))
    approved_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    authority_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    authority_role: Mapped[str] = mapped_column(String(32))
    delegation_id: Mapped[str | None] = mapped_column(ForeignKey("review_delegations.id"))
    reason: Mapped[str] = mapped_column(String(500))
    self_approval_id: Mapped[str | None] = mapped_column(ForeignKey("self_approval_permissions.id"))
    __table_args__ = (UniqueConstraint("workspace_id", "content_revision", "approved_by"),
                     UniqueConstraint("workspace_id", "content_revision", "authority_id"),
                     CheckConstraint("authority_role IN ('approver','programme_lead')"))


class SwapRequest(InsertionOrdered, Record, DepartmentRecord, Base):
    __tablename__ = "swap_requests"
    kind: Mapped[str] = mapped_column(String(16), default="exchange", server_default="exchange")
    open_offer: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    proposer_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    proposer_person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    recipient_person_id: Mapped[str | None] = mapped_column(ForeignKey("people.id"))
    recipient_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    first_assignment_id: Mapped[str] = mapped_column(ForeignKey("assignments.id"))
    second_assignment_id: Mapped[str | None] = mapped_column(ForeignKey("assignments.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="offered")
    reason: Mapped[str] = mapped_column(String(500))
    counteroffered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), unique=True)
    expected_content_hash: Mapped[str | None] = mapped_column(String(64))
    result_publication_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id"))
    # Undo closes a swap forward and reopens it as a new row at the earlier stage (consent is never rewritten).
    reopened_from_id: Mapped[str | None] = mapped_column(ForeignKey("swap_requests.id"))
    __table_args__ = (CheckConstraint("proposer_person_id != recipient_person_id"),
                     CheckConstraint("recipient_person_id IS NOT NULL OR (open_offer AND status IN ('offered','withdrawn','rejected'))"),
                     CheckConstraint("kind IN (" + ",".join(f"'{kind}'" for kind in SWAP_KINDS) + ")"),
                     CheckConstraint("kind != 'give' OR second_assignment_id IS NULL"),
                     CheckConstraint("status IN ('offered','counteroffered','accepted','reviewing','published','withdrawn','rejected')"))


class SwapOfferAudience(Base):
    """The worker's frozen invitation audience; consent still rechecks current eligibility."""
    __tablename__ = "swap_offer_audience"
    swap_id: Mapped[str] = mapped_column(ForeignKey("swap_requests.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (Index("ix_swap_offer_audience_person_swap", "person_id", "swap_id"),)


class SwapEvent(InsertionOrdered, Record, Base):
    __tablename__ = "swap_events"
    swap_id: Mapped[str] = mapped_column(ForeignKey("swap_requests.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    action: Mapped[str] = mapped_column(String(24))
    pair_hash: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict | None] = mapped_column(JSON)


class AvailabilityPattern(InsertionOrdered, Record, DepartmentRecord, Base):
    """A recurring weekly time someone cannot work. Counts as unavailability only while active."""
    __tablename__ = "availability_patterns"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    weekdays: Mapped[list] = mapped_column(JSON)
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    decided_by: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_availability_patterns_department_person_window",
                             "department_id", "person_id", "starts_on", "ends_on", "status"),)


class CalendarFeed(Record, DepartmentRecord, Base):
    """A person's private calendar subscription link (only the secret's hash is kept; revoking ends it)."""
    __tablename__ = "calendar_feeds"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hidden_event_type_ids: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")


class TableView(Record, DepartmentRecord, Base):
    """Personal or department-shared collection display state, never scheduling input."""
    __tablename__ = "table_views"
    owner_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    table_key: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[dict] = mapped_column(JSON)
    scope: Mapped[str] = mapped_column(String(16), default="personal", server_default="personal")
    __table_args__ = (Index("ix_table_views_department_scope", "department_id", "scope"),)


class TableViewDefault(Record, DepartmentRecord, Base):
    __tablename__ = "table_view_defaults"
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"))
    table_key: Mapped[str] = mapped_column(String(60))
    view_id: Mapped[str | None] = mapped_column(ForeignKey("table_views.id", ondelete="SET NULL"))
    # SET NULL after deletion must follow the next default, rather than select the built-in view.
    built_in: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    __table_args__ = (
        Index("uq_table_view_defaults_personal", "department_id", "owner_id", "table_key", unique=True,
              postgresql_where=text("owner_id IS NOT NULL"), sqlite_where=text("owner_id IS NOT NULL")),
        Index("uq_table_view_defaults_department", "department_id", "table_key", unique=True,
              postgresql_where=text("owner_id IS NULL"), sqlite_where=text("owner_id IS NULL")),
        CheckConstraint("NOT built_in OR view_id IS NULL"),
    )


class PlannerView(Record, DepartmentRecord, Base):
    __tablename__ = "planner_views"
    owner_id: Mapped[str] = mapped_column(ForeignKey("identities.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    definition: Mapped[dict] = mapped_column(JSON)
    scope: Mapped[str] = mapped_column(String(16), default="personal", server_default="personal")
    __table_args__ = (Index("ix_planner_views_department_scope", "department_id", "scope"),)


class SelfApprovalPermission(InsertionOrdered, Record, DepartmentRecord, Base):
    """Lets a person (or everyone holding a role) decide their own work for the listed actions, between two dates.
    Every use still needs a reason and is marked in the decision history."""
    __tablename__ = "self_approval_permissions"
    identity_id: Mapped[str | None] = mapped_column(ForeignKey("identities.id"), index=True)
    role: Mapped[str | None] = mapped_column(String(32))
    actions: Mapped[list] = mapped_column(JSON)
    starts_on: Mapped[date] = mapped_column(Date)
    ends_on: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(500))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("(identity_id IS NULL) <> (role IS NULL)"), CheckConstraint("ends_on IS NULL OR ends_on > starts_on"))


class PublicationPerson(Base):
    """Who is on a publication: lets staff find their own rotas without reading every snapshot."""
    __tablename__ = "publication_people"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(36), primary_key=True, index=True)


class MonitorIntegration(Base):
    __tablename__ = "monitor_integrations"
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"), primary_key=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), unique=True)


class MonitorIncident(InsertionOrdered, Base):
    __tablename__ = "monitor_incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    source: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(4))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    event_key: Mapped[str] = mapped_column(String(64))
    __table_args__ = (CheckConstraint("state IN ('up','down')"),
                      UniqueConstraint("department_id", "event_key"),
                      Index("ix_monitor_incidents_department_received", "department_id", "received_at", "id"))


class WorkerHeartbeat(Base):
    """Each background worker records that it is alive; Operations and the container healthcheck read it."""
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    passes: Mapped[int] = mapped_column(Integer, default=0)
    release: Mapped[str | None] = mapped_column(String(80))  # apps/api/version.build()["release"]; NULL before 0064


class StaffWish(InsertionOrdered, Record, DepartmentRecord, Base):
    """A soft date or working-pattern wish (last_day included), paid from the person's points for the period.
    Editable until a rota covering those days is submitted; publication records how it went."""
    __tablename__ = "staff_wishes"
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    first_day: Mapped[date] = mapped_column(Date)
    last_day: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(24))
    assignment_type_id: Mapped[str | None] = mapped_column(ForeignKey("assignment_types.id"))
    limit: Mapped[int | None] = mapped_column(Integer)
    weekend_start: Mapped[int | None] = mapped_column(Integer)
    weekend_days: Mapped[int | None] = mapped_column(Integer)
    points: Mapped[int] = mapped_column(Integer)
    repeat: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
    note: Mapped[str] = mapped_column(String(200), default="", server_default="")
    status: Mapped[str] = mapped_column(String(16), default="open", server_default="open")
    # Set on publication: granted, partly or declined, and the publication that decided it.
    outcome: Mapped[str | None] = mapped_column(String(16))
    outcome_reasons: Mapped[list | None] = mapped_column(JSON)
    publication_id: Mapped[str | None] = mapped_column(ForeignKey("publications.id"))
    created_by: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    __table_args__ = (CheckConstraint("last_day >= first_day"),
                      CheckConstraint(f"points BETWEEN 1 AND {MAX_WISH_POINTS}", name="staff_wishes_points"),
                      CheckConstraint("status IN ('open','withdrawn')"),
                      CheckConstraint("kind IN (" + ','.join(repr(kind) for kind in WISH_KINDS) + ")"),
                      Index("ix_staff_wishes_department_days", "department_id", "first_day"))


class WishOverride(Record, DepartmentRecord, Base):
    """Audited standing defaults for a budget period or a particular rota."""
    __tablename__ = "wish_overrides"
    wish_id: Mapped[str] = mapped_column(ForeignKey("staff_wishes.id"))
    period_start: Mapped[date | None] = mapped_column(Date)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"))
    skip: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    points: Mapped[int | None] = mapped_column(Integer)
    number: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("wish_id", "period_start"), UniqueConstraint("wish_id", "workspace_id"),
                      CheckConstraint("(period_start IS NULL) <> (workspace_id IS NULL)"),
                      CheckConstraint("points IS NULL OR points >= 1"),
                      CheckConstraint("number IS NULL OR number >= 1"),
                      Index("ix_wish_overrides_department", "department_id", "wish_id"))


class WishLedger(Base):
    """Wish points per publication, person and metric (submitted, granted, declined): the fairness-ledger key design.
    An amendment's publication replaces the rows of the one it supersedes; declined points raise later wishes."""
    __tablename__ = "wish_ledger"
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), primary_key=True)
    metric: Mapped[str] = mapped_column(String(40), primary_key=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    __table_args__ = (Index("ix_wish_ledger_person", "department_id", "person_id", "period_start"),)


class FairnessLedger(Base):
    """What each current publication gave each person, per local day (packages/domain/fairness.py).

    Keys: duties, hours, session_units, overtime_*, lead_turns, days_worked, available_days; pair / supervised with
    the other person; each optionally '|type:<id>', '|service:<id>' or '|location:<id>' (the session's own place). goal:<policy> rows hold a Balance goal's
    value and expected share at publication (person NULL: the goal's score). Rebuilt per publication (idempotent);
    a superseded publication's rows are removed. The wish columns are reserved for staff wishes (declined-wish debt).
    """
    __tablename__ = "fairness_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36))
    other_person_id: Mapped[str | None] = mapped_column(String(36))
    metric_key: Mapped[str] = mapped_column(String(96))
    bucket: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    expected: Mapped[float | None] = mapped_column(Float)
    wish_points_submitted: Mapped[float | None] = mapped_column(Float)
    wish_points_granted: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (Index("ix_fairness_metric_period", "department_id", "metric_key", "bucket"),
                      Index("ix_fairness_person_period", "department_id", "person_id", "bucket"))


class MailboxConnection(Record, DepartmentRecord, Base):
    """Delegated sending consent belongs to one Rota identity in one department."""
    __tablename__ = "mailbox_connections"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    encrypted_refresh_token: Mapped[str] = mapped_column(Text, deferred=True)
    configuration_hash: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("identity_id", "department_id"),)


class MailboxOAuthFlow(Record, DepartmentRecord, Base):
    __tablename__ = "mailbox_oauth_flows"
    identity_id: Mapped[str] = mapped_column(ForeignKey("identities.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    encrypted_verifier: Mapped[str] = mapped_column(Text, deferred=True)
    configuration_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
