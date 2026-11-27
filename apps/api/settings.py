"""Deployment settings, not coordinator-editable operational policies.

ROTA_WORKER_CLAIM_MAX_DEFER_SECONDS defaults to 20 (range 1..600 seconds).
Once the oldest enabled queued job reaches this age, claiming uses the existing
bounded writer wait before housekeeping; the command write budget still applies.
"""
from dataclasses import dataclass, field
from ipaddress import IPv4Address
import os
from pathlib import Path
import stat
from typing import Literal
from urllib.parse import urlparse
from pydantic import ValidationError
from .data_crypto import data_key_from_secret  # noqa: F401  the one key parser (GitHub and mailbox secrets)
from packages.contracts.schemas import (BulkSettings, DeploymentSessionSettingsInput, DisplaySettingsInput,
                                        SESSION_IDLE_MINUTES_MIN,
                                        SESSION_IDLE_MINUTES_DEFAULT, SESSION_MAX_HOURS_DEFAULT,
                                        valid_feedback_github_repo)

# The compose health check allows a two-minute file age; keep configurable worker waits at half that window.
WORKER_IDLE_MAX_SECONDS = 60


def new_department_display_settings() -> dict[str, bool]:
    """A new department stores the default Rules page; departments created before it store none and keep theirs."""
    return {"rule_catalogue": DisplaySettingsInput.model_fields["rule_catalogue"].default}


def new_department_session_limits() -> dict[str, int | None]:
    """New-department defaults only; existing NULLs retain Settings' legacy fallback.

    Explicit deployment values still take precedence. No migration backfills customer settings.
    """
    try:
        hours = int(os.getenv("ROTA_SESSION_HOURS", str(SESSION_MAX_HOURS_DEFAULT)))
        # A shorter explicit maximum also bounds the implicit idle default; explicit idle values stay exact.
        value = DeploymentSessionSettingsInput(
            idle_minutes=int(os.getenv("ROTA_SESSION_IDLE_MINUTES", str(min(SESSION_IDLE_MINUTES_DEFAULT, hours * 60)))),
            max_hours=hours)
    except (ValueError, ValidationError):
        raise RuntimeError("ROTA_SESSION_IDLE_MINUTES / ROTA_SESSION_HOURS: invalid new-department session defaults") from None
    # Legacy 1–4 minute operator values stay inherited: they cannot be stored as new admin overrides.
    return {"session_idle_minutes": value.idle_minutes if value.idle_minutes >= SESSION_IDLE_MINUTES_MIN else None,
            "session_max_hours": value.max_hours}


MAX_SECRET_BYTES = 16_384


def quota_policy_limit() -> int:
    from packages.domain.quotas import MAX_POLICIES
    raw = os.getenv("ROTA_MAX_QUOTA_POLICIES")
    if raw is None:
        return MAX_POLICIES
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError("ROTA_MAX_QUOTA_POLICIES must be an integer from 1 to 2000") from None
    if not 1 <= value <= 2000:
        raise RuntimeError("ROTA_MAX_QUOTA_POLICIES must be an integer from 1 to 2000")
    return value


def secret_from_env(name: str, default: str = "", *, optional: bool = False) -> str:
    """Read an explicitly selected mounted secret; never silently override a value.

    The supported deployment is Linux. No-follow/nonblocking opens prevent a
    symlink or pipe from masquerading as a secret. The file stays out of logs.
    """
    file_key = name + "_FILE"
    if file_key not in os.environ:
        return os.getenv(name, default)
    if name in os.environ:
        raise RuntimeError(f"Set either {name} or {file_key}, never both")
    path = Path(os.environ[file_key])
    if not path.is_absolute():
        raise RuntimeError(f"{file_key} must name an absolute regular file")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SECRET_BYTES:
            raise ValueError("Invalid secret file")
        if optional and info.st_size == 0:
            return default
        if environment_from_env() == "production" and info.st_mode & 0o007:
            raise ValueError("Secret file is accessible to other users")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_SECRET_BYTES + 1)
        if len(raw) > MAX_SECRET_BYTES:
            raise ValueError("Oversized secret")
        value = raw.decode("utf-8")
        if value.endswith("\n"):
            value = value[:-1].removesuffix("\r")
        if optional and not value:
            return default
        if not value or any(c in value for c in ("\n", "\r", "\x00")):
            raise ValueError("Empty or multiline secret")
        return value
    except (OSError, UnicodeError, ValueError, AttributeError) as error:
        if optional and isinstance(error, FileNotFoundError):
            return default
        raise RuntimeError(f"{file_key} must be a readable, bounded, single-line regular file with protected production permissions") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def environment_from_env() -> str:
    """ROTA_ENV, failing safe: unset means production (managed passwords or institutional sign-in, strict checks).
    Development and tests opt out explicitly with ROTA_ENV=development or ROTA_ENV=test."""
    return os.getenv("ROTA_ENV") or "production"


def deployment_from_env(environment: str) -> str:
    """ROTA_DEPLOYMENT names the deployment for the footer and the test-department provisioning guard.
    Authentication security follows ROTA_ENV. DEV runs ROTA_ENV=production for strict checks and sets
    ROTA_DEPLOYMENT=dev. Unset, it follows ROTA_ENV, so production defaults refuse test-department creation."""
    return os.getenv("ROTA_DEPLOYMENT", "").strip().lower() or environment


TEST_ONLY_FLAGS = ("ROTA_ALLOW_DISPOSABLE_TEST_DB", "ROTA_ALLOW_TEST_ROLES")


def refuse_test_flags(environment: str) -> None:
    """Test fixtures create/drop schemas and cluster roles under these flags; production must never carry them."""
    present = [name for name in TEST_ONLY_FLAGS if os.getenv(name)]
    if environment == "production" and present:
        raise RuntimeError(f"{', '.join(present)} authorise destructive test fixtures and are refused in production; unset them")


def valid_http_url(value: str, *, origin_only: bool = False) -> bool:
    """No credentials, fragments, query strings, whitespace or ambiguous ports."""
    try:
        parsed = urlparse(value)
        return bool(value and not any(c.isspace() or ord(c) < 32 for c in value)
            and "\\" not in value and parsed.scheme in {"http", "https"}
            and parsed.hostname and parsed.username is None and parsed.password is None
            and "?" not in value and "#" not in value and not parsed.params
            and (not origin_only or not parsed.path) and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def password_login_enabled(settings):
    return settings.environment != "production" or bool(settings.identity_users_file)


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    deployment: str = "development"  # Footer identity; test-department creation also checks this deployment.
    publish_test_people_only: bool = False
    database_url: str = field(default="sqlite:///./var/rota.db", repr=False)
    sqlite_synchronous: Literal["FULL", "NORMAL", "OFF"] = "FULL"
    public_origin: str = "http://127.0.0.1:8000"
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = field(default="", repr=False)
    oidc_redirect_uri: str = ""
    oidc_allowed_hosts: str = ""
    forwarded_allow_ips: str = ""  # The API trusts only its resolved edge container's exact peer.
    generation_engine: str = "cp_sat"
    generation_resume_days: int = 7
    # Local 150-person/526-session comparison completed each phase within 54.2s; rounded up with 25% margin.
    # shortcut: synthetic timing is provisional; re-measure through the app once the Golden department exists on DEV.
    feasibility_second_pass_seconds: int = 70
    static_cache_seconds: int = 31_536_000
    static_precompressed: bool = True
    session_hours: int = 8  # legacy fallback for existing NULL departments; new departments use explicit 480/24
    session_idle_minutes: int = 60  # preserve inherited values; see new_department_session_limits
    identity_users_file: str = ""  # Rota-managed password file; compatible with existing Authelia volumes.
    password_login_enabled: bool = False  # Deprecated deployment flag; runtime selection lives in the environment DB.
    password_login_progressive_delay: bool = True
    password_login_account_limit: int = 8  # Progressive account burst; fixed-mode failure cap.
    password_login_pair_limit: int = 40
    password_login_address_limit: int = 3000  # Shared hospital NAT spray backstop; account admission also spans addresses.
    password_login_window_seconds: int = 900
    password_login_audit_retention_days: int = 30
    max_body_bytes: int = 2_000_000
    command_lock_timeout_ms: int = 3_000
    command_statement_timeout_ms: int = 30_000
    command_write_budget_ms: int = 120_000
    command_lock_diagnostic_timeout_ms: int = 500
    # Keep pg_locks scans visible during a storm without running one per timed-out request.
    command_lock_diagnostic_interval_seconds: int = 1
    # Interactive catalogue-impact preview only; a larger forward scope returns exact totals and partial=true, never a refusal.
    catalogue_preview_max_workspaces: int = 40
    catalogue_preview_max_sessions: int = 5000
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 5
    db_idle_tx_timeout_minutes: int = 5  # 0 disables the PostgreSQL idle-transaction default.
    # Catalogue fragments are large JSON strings; keep a measured, configurable per-process bound.
    catalogue_cache_entries: int = 256
    # Request-scoped verified baseline payloads; evicted entries are rehydrated without re-verifying.
    verified_baseline_cache_entries: int = 4
    # A concurrent read may wait briefly for a shared catalogue build, never for the
    # whole build.  A cold caller gets a retryable response instead of occupying a
    # request worker for minutes.
    # shortcut: 60 seconds is a generous default; tighten it after measured build times justify the margin.
    catalogue_build_wait_seconds: int = 60
    # Memory safety bounds; losing history requests a full refresh instead of dropping changes.
    live_history_size: int = 128
    live_history_topics: int = 1024
    workspace_read_max_days: int = 92
    workspace_read_page_size: int = 500
    # Measured 540-session read baseline is <256 MiB; reclaim above twice that.
    memory_reclaim_threshold_mb: int = 512  # Reclaim once RSS grows this much since the last reclaim; 0 disables.
    # Abuse ceilings far above normal use; 0 disables one. Commands/exports per identity per minute, feeds per hour.
    rate_commands_per_minute: int = 120
    bulk_max_items: int = 2000
    bulk_direct_max: int = 50
    bulk_staged_days: int = 14
    rate_bulk_preview_per_minute: int = 30
    rate_exports_per_minute: int = 30
    rate_feed_per_hour: int = 60
    rate_feed_address_per_hour: int = 3000  # calendar services (and a reverse proxy) share addresses across many feeds
    feedback_github_token: str = field(default="", repr=False)
    feedback_github_repo: str = ""
    fmuc_data_key: str = field(default="", repr=False)
    vapid_private_key: str = field(default="", repr=False)
    vapid_draining_keys: str = field(default="", repr=False)
    vapid_key_id: str = ""
    vapid_subject: str = ""
    # Legacy generation-run supersession history is scanned off-writer in bounded passes.
    worker_supersession_interval_seconds: int = 60
    worker_supersession_workspaces: int = 50
    worker_supersession_batch_size: int = 200
    worker_supersession_retry_max_seconds: int = 600
    worker_heartbeat_max_age_seconds: int = 180
    smtp_private_host_allowlist: str = ""
    worker_poll_seconds: int = 2
    worker_claim_max_defer_seconds: int = 20
    # Notification dispatch has a tighter fallback than solver/job polling; PostgreSQL LISTEN wakes it first.
    worker_notification_poll_seconds: int = 1
    worker_idle_max_seconds: int = 30  # Keep below the compose health-check window with margin.
    worker_projection_retry_seconds: int = 300
    worker_projection_retry_max_seconds: int = 3600
    worker_batch_size: int = 200
    worker_email_max_per_pass: int = 10
    worker_email_budget_seconds: int = 5
    monitor_events_per_minute: int = 60
    status_directory: str = "/data/status"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            generation_engine=os.getenv("ROTA_GENERATION_ENGINE", cls.generation_engine),
            static_precompressed=os.getenv('ROTA_STATIC_PRECOMPRESSED', 'true') == 'true',
            environment=environment_from_env(),
            deployment=deployment_from_env(environment_from_env()),
            publish_test_people_only=os.getenv("FMUC_PUBLISH_TEST_PEOPLE_ONLY", "false") == "true",
            database_url=secret_from_env("DATABASE_URL", "sqlite:///./var/rota.db"),
            sqlite_synchronous=os.getenv("ROTA_SQLITE_SYNCHRONOUS", cls.sqlite_synchronous),
            public_origin=os.getenv("PUBLIC_ORIGIN", "http://127.0.0.1:8000").rstrip("/"),
            oidc_issuer=os.getenv("OIDC_ISSUER", "").rstrip("/"),
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
            oidc_client_secret=secret_from_env("OIDC_CLIENT_SECRET"),
            oidc_redirect_uri=os.getenv("OIDC_REDIRECT_URI", ""),
            oidc_allowed_hosts=os.getenv("OIDC_ALLOWED_HOSTS", ""),
            forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", ""),
            identity_users_file=os.getenv("ROTA_IDENTITY_USERS_FILE", ""),
            password_login_enabled=os.getenv("ROTA_PASSWORD_LOGIN_ENABLED", "false") == "true",
            password_login_progressive_delay=os.getenv("ROTA_PASSWORD_LOGIN_PROGRESSIVE_DELAY", "true") == "true",
            smtp_private_host_allowlist=os.getenv("FMUC_SMTP_PRIVATE_HOST_ALLOWLIST", ""),
            feedback_github_token=secret_from_env("FEEDBACK_GITHUB_TOKEN", optional=True),
            feedback_github_repo=os.getenv("FEEDBACK_GITHUB_REPO", "").strip(),
            fmuc_data_key=secret_from_env("FMUC_DATA_KEY", optional=True),
            vapid_private_key=secret_from_env("FMUC_VAPID_PRIVATE_KEY", optional=True),
            vapid_draining_keys=secret_from_env("FMUC_VAPID_DRAINING_KEYS", optional=True),
            vapid_key_id=os.getenv("FMUC_VAPID_KEY_ID", "").strip(),
            vapid_subject=os.getenv("FMUC_VAPID_SUBJECT", "").strip(),
            worker_heartbeat_max_age_seconds=int(os.getenv("FMUC_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "180")),
            worker_email_max_per_pass=int(os.getenv("FMUC_WORKER_EMAIL_MAX_PER_PASS", "10")),
            worker_email_budget_seconds=int(os.getenv("FMUC_WORKER_EMAIL_BUDGET_SECONDS", "5")),
            monitor_events_per_minute=int(os.getenv("FMUC_MONITOR_EVENTS_PER_MINUTE", "60")),
            status_directory=os.getenv("FMUC_STATUS_DIRECTORY", "/data/status"),
            **{name: int(os.getenv("ROTA_" + name.upper(), getattr(cls, name)))
               for name in ("session_idle_minutes", "session_hours", "rate_commands_per_minute", "rate_exports_per_minute", "rate_feed_per_hour", "rate_feed_address_per_hour",
                            "bulk_max_items", "bulk_direct_max", "bulk_staged_days", "rate_bulk_preview_per_minute",
                            "generation_resume_days", "feasibility_second_pass_seconds", "static_cache_seconds", "worker_poll_seconds", "worker_claim_max_defer_seconds", "worker_notification_poll_seconds", "worker_idle_max_seconds", "worker_batch_size",
                            "worker_projection_retry_seconds", "worker_projection_retry_max_seconds",
                            "worker_supersession_interval_seconds", "worker_supersession_workspaces", "worker_supersession_batch_size",
                            "worker_supersession_retry_max_seconds",
                            "password_login_account_limit", "password_login_pair_limit", "password_login_address_limit", "password_login_window_seconds", "password_login_audit_retention_days",
                            "command_lock_timeout_ms", "command_statement_timeout_ms", "command_write_budget_ms", "command_lock_diagnostic_timeout_ms", "command_lock_diagnostic_interval_seconds",
                            "db_pool_timeout_seconds", "db_pool_size", "db_max_overflow",
                            "db_idle_tx_timeout_minutes", "catalogue_cache_entries", "verified_baseline_cache_entries", "catalogue_build_wait_seconds", "memory_reclaim_threshold_mb",
                            "live_history_size", "live_history_topics",
                            "workspace_read_max_days", "workspace_read_page_size",
                            "catalogue_preview_max_workspaces", "catalogue_preview_max_sessions")},
        )

    def check_sqlite_synchronous(self) -> None:
        if self.sqlite_synchronous not in {"FULL", "NORMAL", "OFF"}:
            raise RuntimeError("ROTA_SQLITE_SYNCHRONOUS must be FULL, NORMAL or OFF")
        if self.sqlite_synchronous == "OFF" and os.getenv("ROTA_ALLOW_DISPOSABLE_TEST_DB") != "yes":
            raise RuntimeError("ROTA_SQLITE_SYNCHRONOUS=OFF requires ROTA_ALLOW_DISPOSABLE_TEST_DB=yes for a disposable test database")

    def check(self, *, require_authentication: bool = True, require_proxy: bool = False) -> None:
        if self.database_url.startswith("sqlite"):
            self.check_sqlite_synchronous()
        if not 1 <= self.feasibility_second_pass_seconds <= 600:
            raise RuntimeError('ROTA_FEASIBILITY_SECOND_PASS_SECONDS must be from 1 to 600')
        if not 1 <= self.worker_poll_seconds <= self.worker_idle_max_seconds <= WORKER_IDLE_MAX_SECONDS:
            raise RuntimeError('Worker polling must be positive and leave margin below the container liveness window')
        if not 1 <= self.worker_claim_max_defer_seconds <= 600:
            raise RuntimeError('ROTA_WORKER_CLAIM_MAX_DEFER_SECONDS must be from 1 to 600')
        if not 1 <= self.worker_notification_poll_seconds <= self.worker_idle_max_seconds:
            raise RuntimeError('ROTA_WORKER_NOTIFICATION_POLL_SECONDS must be between one second and the worker idle interval')
        if not self.worker_idle_max_seconds <= self.worker_projection_retry_seconds <= self.worker_projection_retry_max_seconds <= 86400:
            raise RuntimeError('Projection retry delays must be between the worker idle interval and one day')
        if not 1 <= self.worker_batch_size <= 10000:
            raise RuntimeError('ROTA_WORKER_BATCH_SIZE must be from 1 to 10000')
        if not 1 <= self.worker_supersession_interval_seconds <= 86400:
            raise RuntimeError('ROTA_WORKER_SUPERSESSION_INTERVAL_SECONDS must be from 1 to 86400')
        if not 1 <= self.worker_supersession_workspaces <= 10000:
            raise RuntimeError('ROTA_WORKER_SUPERSESSION_WORKSPACES must be from 1 to 10000')
        if not 1 <= self.worker_supersession_batch_size <= 10000:
            raise RuntimeError('ROTA_WORKER_SUPERSESSION_BATCH_SIZE must be from 1 to 10000')
        if not self.worker_supersession_interval_seconds <= self.worker_supersession_retry_max_seconds <= 86400:
            raise RuntimeError('ROTA_WORKER_SUPERSESSION_RETRY_MAX_SECONDS must be at least the interval and at most 86400')
        if self.static_cache_seconds < 0:
            raise RuntimeError('ROTA_STATIC_CACHE_SECONDS must be nonnegative (0 disables immutable caching)')
        try:
            BulkSettings.model_validate({key: getattr(self, key) for key in BulkSettings.model_fields})
        except ValidationError as exc:
            raise RuntimeError("Invalid bulk limits: " + exc.errors()[0]["msg"]) from None
        quota_policy_limit()
        data_key_from_secret(self.fmuc_data_key)
        if self.vapid_private_key and (not self.vapid_key_id or not self.vapid_subject):
            raise RuntimeError("FMUC_VAPID_KEY_ID and FMUC_VAPID_SUBJECT are required with FMUC_VAPID_PRIVATE_KEY")
        if not 1 <= self.worker_heartbeat_max_age_seconds <= 86400:
            raise RuntimeError("FMUC_WORKER_HEARTBEAT_MAX_AGE_SECONDS must be from 1 to 86400")
        if not 1 <= self.worker_email_max_per_pass <= 10000:
            raise RuntimeError("FMUC_WORKER_EMAIL_MAX_PER_PASS must be from 1 to 10000")
        if not 1 <= self.worker_email_budget_seconds <= 60:
            raise RuntimeError("FMUC_WORKER_EMAIL_BUDGET_SECONDS must be from 1 to 60")
        if not 1 <= self.monitor_events_per_minute <= 10000:
            raise RuntimeError("FMUC_MONITOR_EVENTS_PER_MINUTE must be from 1 to 10000")
        if not (Path(self.status_directory).is_absolute() or self.status_directory.startswith("/")):
            raise RuntimeError("FMUC_STATUS_DIRECTORY must be an absolute directory")
        if self.memory_reclaim_threshold_mb < 0:
            raise RuntimeError("ROTA_MEMORY_RECLAIM_THRESHOLD_MB must be non-negative")
        for name, minimum, maximum in (("db_pool_size", 1, 100),
                                        ("db_max_overflow", 0, 100),
                                        ("catalogue_cache_entries", 1, 4096),
                                        ("verified_baseline_cache_entries", 1, 4096),
                                        ("catalogue_build_wait_seconds", 1, 600),
                                        ("live_history_size", 1, 4096),
                                        ("live_history_topics", 1, 16384),
                                        ("workspace_read_max_days", 1, 366),
                                        ("workspace_read_page_size", 1, 10000),
                                        ("catalogue_preview_max_workspaces", 0, 100_000),
                                        ("catalogue_preview_max_sessions", 0, 10_000_000)):
            if not minimum <= getattr(self, name) <= maximum:
                raise RuntimeError(f"ROTA_{name.upper()} must be from {minimum} to {maximum}")
        for name in ("command_lock_timeout_ms", "command_statement_timeout_ms", "command_write_budget_ms", "db_pool_timeout_seconds"):
            ceiling = 2_147_483_647 // 1000 if name == "db_pool_timeout_seconds" else 2_147_483_647
            if not 1 <= getattr(self, name) <= ceiling:
                raise RuntimeError(f"ROTA_{name.upper()} must be from 1 to {ceiling}")
        if not 1 <= self.command_lock_diagnostic_timeout_ms <= 5000:
            raise RuntimeError("ROTA_COMMAND_LOCK_DIAGNOSTIC_TIMEOUT_MS must be from 1 to 5000")
        if not 1 <= self.command_lock_diagnostic_interval_seconds <= 3600:
            raise RuntimeError("ROTA_COMMAND_LOCK_DIAGNOSTIC_INTERVAL_SECONDS must be from 1 to 3600")
        if not 0 <= self.db_idle_tx_timeout_minutes <= 2_147_483_647 // 60_000:
            raise RuntimeError("ROTA_DB_IDLE_TX_TIMEOUT_MINUTES must be from 0 to 35791")
        for name, minimum, maximum in (("password_login_account_limit", 1, 1000), ("password_login_pair_limit", 1, 1000),
                                       ("password_login_address_limit", 1, 100000),
                                       ("password_login_window_seconds", 60, 86400), ("password_login_audit_retention_days", 30, 3650),
                                       ("generation_resume_days", 1, 365)):
            if not minimum <= getattr(self, name) <= maximum:
                raise RuntimeError(f"ROTA_{name.upper()} must be from {minimum} to {maximum}")
        if self.feedback_github_repo and not valid_feedback_github_repo(self.feedback_github_repo):
            raise RuntimeError("FEEDBACK_GITHUB_REPO must name owner/repository")
        if self.feedback_github_token and not self.feedback_github_repo:
            raise RuntimeError("FEEDBACK_GITHUB_TOKEN requires FEEDBACK_GITHUB_REPO")
        if self.generation_engine not in {"legacy", "cp_sat"}:
            raise RuntimeError("ROTA_GENERATION_ENGINE must be legacy or cp_sat")
        if self.environment not in {"development", "test", "production"}:
            raise RuntimeError("ROTA_ENV must be development, test or production")
        if self.deployment == "production" and self.publish_test_people_only:
            raise RuntimeError("FMUC_PUBLISH_TEST_PEOPLE_ONLY must be absent in production")
        refuse_test_flags(self.environment)
        try:
            DeploymentSessionSettingsInput(idle_minutes=self.session_idle_minutes, max_hours=self.session_hours)
        except ValidationError as exc:
            raise RuntimeError("ROTA_SESSION_IDLE_MINUTES / ROTA_SESSION_HOURS: " + exc.errors()[0]["msg"]) from None
        if require_authentication and self.identity_users_file and not (Path(self.identity_users_file).is_absolute() and Path(self.identity_users_file).is_file()):
            raise RuntimeError("ROTA_IDENTITY_USERS_FILE must name an existing users file by its absolute path")
        if not valid_http_url(self.public_origin, origin_only=True):
            raise RuntimeError("PUBLIC_ORIGIN must be a single HTTP(S) origin without a path")
        origin = urlparse(self.public_origin)
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise RuntimeError("Production requires PostgreSQL with the psycopg driver")
            if origin.scheme != "https":
                raise RuntimeError("Production requires an HTTPS origin")
            if not require_authentication:  # The worker serves no sign-in endpoints or users file.
                return
            if require_proxy:
                try:
                    peer = IPv4Address(self.forwarded_allow_ips)
                    if str(peer) != self.forwarded_allow_ips or peer.is_unspecified or peer.is_multicast:
                        raise ValueError()
                except ValueError:
                    raise RuntimeError("FORWARDED_ALLOW_IPS must contain the edge container's exact IPv4 address in production") from None
            if self.identity_users_file and self.oidc_issuer and self.oidc_issuer != self.public_origin + "/authelia":
                raise RuntimeError("ROTA_IDENTITY_USERS_FILE cannot be combined with an external OIDC issuer in production")
            if not self.identity_users_file:
                raise RuntimeError("Production requires a managed identity store for Rota passwords and break-glass access")
        if (require_authentication and self.environment == "production" and password_login_enabled(self)
                and self.password_login_progressive_delay and not self.fmuc_data_key):
            raise RuntimeError("FMUC_DATA_KEY is required for progressive password sign-in")
        if require_authentication and self.environment == "production" and not self.fmuc_data_key:
            # Sign-in event and rate-limit keys are keyed hashes; a keyless production cannot store them safely.
            raise RuntimeError("FMUC_DATA_KEY is required for sign-in in production")
