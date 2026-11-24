"""Opaque database sessions, explicit identity bindings and per-command authority."""
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import ipaddress
import logging
import secrets
from threading import BoundedSemaphore
from zoneinfo import ZoneInfo
from argon2.exceptions import VerificationError, InvalidHashError
from sqlalchemy import and_, delete, inspect, or_, select, text, update
from .models import (Account, AuditEvent, AuthLoginEvent, Department, Identity, Organisation, RateLimit, RoleBinding,
                     Session, now)
from .errors import Problem, require
from .settings import password_login_enabled
from packages.domain.time import aware
from packages.contracts.schemas import CATALOGUE_READS, EXPLICIT_ROLES, INSTALLATION_ADMIN_ROLE, PERMISSIONS, ROLE_NAMES

from .identity_store import HASHER as PASSWORDS, _DUMMY as _DUMMY_HASH
from .db import current_request_parent, liveness_write, log_row_lock_diagnostic
# Bound concurrent 64 MiB verification/rehash work within the API's 1 GiB memory budget.
PASSWORD_VERIFICATIONS = BoundedSemaphore(4)
LOGIN_ACCOUNT_BUCKET_COUNT = 1 << 16
CATALOGUE_READ_ROLES = PERMISSIONS["catalogue.read"]
log = logging.getLogger("rota.auth")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Actor:
    identity_id: str
    department_id: str
    organisation_id: str
    display_name: str
    roles: frozenset[str]
    person_id: str | None
    session_id: str

    def permits(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))


def require_role(actor: Actor, *roles: str) -> None:
    require(actor.permits(*roles), "FORBIDDEN", "Your current role does not permit this action", 403)


def require_catalogue_read(actor: Actor, kind: str | None = None) -> None:
    if kind == "rotations" and actor.permits("staff"):
        return
    if kind is None:
        require_permission(actor, 'catalogue.read')
    else:
        require(can_read_catalogue(actor, kind), "FORBIDDEN", "Your current role does not permit this action", 403)


def require_permission(actor: Actor, name: str) -> None:
    require_role(actor, *PERMISSIONS[name])


def can_read_rules(actor: Actor) -> bool:
    return actor.permits(*PERMISSIONS['rules.read'])


def can_read_catalogue(actor: Actor, kind: str) -> bool:
    permission = CATALOGUE_READS[kind]['permission']
    return can_read_rules(actor) if permission == 'rules.read' else actor.permits(*PERMISSIONS[permission])


def _local_today(db, department_id):
    department = db.get(Department, department_id)
    if department is None:
        return None
    organisation = current_request_parent(db, Organisation, department.organisation_id)
    return now().astimezone(ZoneInfo(organisation.timezone)).date()


def _effective(roles):
    # Admin inherits department powers; installation and audit roles require explicit bindings.
    roles = frozenset(roles)
    return roles | (set(ROLE_NAMES) - EXPLICIT_ROLES) if "admin" in roles else roles


effective_roles = _effective  # the same inheritance for a role set that is not one person's bindings


def _live(binding, today):
    return not binding.revoked and binding.starts_on <= today and (binding.ends_on is None or today < binding.ends_on)


def has_installation_admin(db, identity_id=None, *, excluding_binding_id=None, excluding_identities=(), permanent=False):
    """Check installation-wide grants on the local day of each binding's department."""
    query = select(RoleBinding.starts_on, RoleBinding.ends_on, RoleBinding.revoked, Organisation.timezone).join_from(RoleBinding,
        Department, Department.id == RoleBinding.department_id).join(
        Organisation, Organisation.id == Department.organisation_id).join(
        Identity, Identity.id == RoleBinding.identity_id).outerjoin(
        Account, Account.identity_id == Identity.id).where(
        RoleBinding.role == INSTALLATION_ADMIN_ROLE, RoleBinding.revoked.is_(False), Identity.disabled.is_(False),
        or_(Account.id.is_(None), Account.accepted_at.is_not(None)))
    if identity_id is not None:
        query = query.where(RoleBinding.identity_id == identity_id)
    if excluding_binding_id is not None:
        query = query.where(RoleBinding.id != excluding_binding_id)
    if excluding_identities:
        query = query.where(RoleBinding.identity_id.not_in(excluding_identities))
    if permanent:
        query = query.where(RoleBinding.ends_on.is_(None))
    moment = now()
    with db.execute(query.execution_options(stream_results=True)) as rows:
        return any(_live(row, moment.astimezone(ZoneInfo(row.timezone)).date()) for row in rows)


def role_bindings(db, department_id, identity_id=None):
    """Load dated grants once; a window can evaluate them on several dates."""
    # READ COMMITTED readers must still see a revocation committed after authentication.
    memo = db.info.get('_request_authority')
    key = (RoleBinding, department_id, identity_id)
    query = select(RoleBinding).where(RoleBinding.department_id == department_id)
    if identity_id is not None:
        query = query.where(RoleBinding.identity_id == identity_id)
    if memo is None or not db.autoflush or db.new or db.dirty or db.deleted:
        return list(db.scalars(query))
    if key not in memo:
        memo[key] = tuple(db.scalars(query))
    return list(memo[key])


def held_roles(db, identity_id: str, department_id: str, *, day=None, bindings=None) -> frozenset[str]:
    """The roles granted on a date, defaulting to today (without administrator inheritance)."""
    day = _local_today(db, department_id) if day is None else day
    if day is None:
        return frozenset()
    bindings = role_bindings(db, department_id, identity_id) if bindings is None else bindings
    return frozenset(b.role for b in bindings if b.department_id == department_id and b.identity_id == identity_id and _live(b, day))


def current_roles(db, identity_id: str, department_id: str) -> frozenset[str]:
    """Granted roles plus an administrator's department powers."""
    return _effective(held_roles(db, identity_id, department_id))


def department_roles(db, department_id: str, effective: bool = True, *, day=None, bindings=None) -> dict[str, frozenset[str]]:
    """Everyone's roles on a date in one query, defaulting to current department access."""
    day = _local_today(db, department_id) if day is None else day
    if day is None:
        return {}
    held: dict[str, set[str]] = {}
    bindings = role_bindings(db, department_id) if bindings is None else bindings
    for b in bindings:
        if b.department_id == department_id and _live(b, day):
            held.setdefault(b.identity_id, set()).add(b.role)
    return {identity: _effective(roles) if effective else frozenset(roles) for identity, roles in held.items()}


def _session_lookup(token_hash):
    return select(Session).where(Session.token_hash == token_hash)


def authenticate(db, token: str | None, csrf: str | None = None, write: bool = False, *, settings=None) -> Actor:
    require(bool(token), "AUTH_REQUIRED", "Sign in to continue", 401)
    session = db.scalar(_session_lookup(digest(token)))
    moment = now()
    require(session is not None and session_active(session, moment),
            "SESSION_EXPIRED", "Your session has expired or was revoked; sign in again", 401)
    department = db.get(Department, session.department_id)
    if settings is None:
        from .settings import Settings
        settings = Settings.from_env()
    require(session_active(session, moment, session_limits(department, settings)),
            "SESSION_EXPIRED", "Your session has expired or was revoked; sign in again", 401)
    identity = db.get(Identity, session.identity_id)
    require(identity is not None and not identity.disabled, "ACCESS_REVOKED", "Access has been revoked", 401)
    if write:
        require(bool(csrf) and secrets.compare_digest(session.csrf_hash, digest(csrf)),
                "CSRF_FAILED", "Refresh your session before saving", 403)
    roles = current_roles(db, identity.id, session.department_id)
    if roles:
        roles -= {INSTALLATION_ADMIN_ROLE}
        if has_installation_admin(db, identity.id):
            roles |= {INSTALLATION_ADMIN_ROLE}
    require(bool(roles), "FORBIDDEN", "No active access binding for this department", 403)
    actor = Actor(identity.id, department.id, department.organisation_id, identity.display_name,
                  roles, identity.person_id, session.id)
    from .services import workspace_access  # registers the shared graph visibility policy
    db.info['workspace_actor'] = actor
    return actor


def device_label(user_agent: str | None) -> str | None:
    """Keep only fixed family labels: no browser versions, raw UA or device fingerprint."""
    if not user_agent:
        return None
    agent = user_agent[:1024].casefold()
    browser = next((name for tokens, name in (
        (("edg/", "edga/", "edgios/"), "Edge"), (("opr/", "opios/"), "Opera"),
        (("firefox/", "fxios/"), "Firefox"), (("chrome/", "crios/"), "Chrome"),
        (("safari/",), "Safari")) if any(token in agent for token in tokens)), "Unknown browser")
    os = next((name for tokens, name in (
        (("iphone", "ipad", "ipod"), "iOS"), (("android",), "Android"),
        (("windows",), "Windows"), (("macintosh", "mac os"), "macOS"),
        (("linux",), "Linux")) if any(token in agent for token in tokens)), "Unknown OS")
    form = "Tablet" if "ipad" in agent or ("android" in agent and "mobile" not in agent) else \
        "Phone" if any(token in agent for token in ("mobile", "iphone", "ipod")) else "Desktop"
    return f"{browser} / {os} / {form}"


def network_label(peer: str | None) -> str | None:
    """Describe the connection peer, without storing an address or trusting forwarded headers."""
    try:
        address = ipaddress.ip_address(peer or "")
    except ValueError:
        return None
    if address.is_loopback:
        return "Local network"
    if address.is_private:
        return "Private network"
    return "Public network" if address.is_global else "Other network"


SESSION_EVENT_REASONS = {"auth.sign_in": "Signed in", "auth.sign_out": "Signed out",
                         "auth.session_revoke": "Session revoked"}
SESSION_COOKIE_MAX_AGE_SECONDS = 400 * 24 * 3600  # Browser transport ceiling; server idle/absolute limits remain authoritative.


def session_cookie_max_age(deadline):
    """Sign-in/refresh cookies follow the absolute cap, independently of the server's sliding idle deadline."""
    return SESSION_COOKIE_MAX_AGE_SECONDS if deadline is None else max(
        0, min(SESSION_COOKIE_MAX_AGE_SECONDS, int((aware(deadline) - now()).total_seconds())))


def session_event(db, session, person_id, action, *, actor_id=None, correlation_id=None):
    """Persist only a database session reference and fixed labels, independently of session pruning."""
    db.add(AuditEvent(department_id=session.department_id, actor_id=actor_id or session.identity_id,
                      action=action, target_id=session.id,
                      reason=SESSION_EVENT_REASONS[action],
                      detail={"person_id": person_id, "identity_id": session.identity_id, "session_id": session.id,
                              "device_label": session.device_label, "network_label": session.network_label,
                              **({"method": session.auth_method} if action == "auth.sign_in" and session.auth_method else {})},
                      correlation_id=correlation_id or db.info.get("correlation_id") or "session"))


def allowed_departments(db, identity: Identity):
    require(not identity.disabled, "INVALID_LOGIN", "Unable to sign in with these credentials", 401)
    # Resolve all dated grants in one joined read. The previous loop called
    # current_roles for every department, which re-read the department,
    # organisation and bindings on every successful sign-in.
    moment = now()
    grants = db.execute(select(RoleBinding.department_id, RoleBinding.role, RoleBinding.starts_on,
                               RoleBinding.ends_on, RoleBinding.revoked, Organisation.timezone)
                        .join(Department, Department.id == RoleBinding.department_id)
                        .join(Organisation, Organisation.id == Department.organisation_id)
                        .where(RoleBinding.identity_id == identity.id)).all()
    held: dict[str, set[str]] = {}
    for department_id, role, starts_on, ends_on, revoked, timezone in grants:
        day = moment.astimezone(ZoneInfo(timezone)).date()
        if not revoked and starts_on <= day and (ends_on is None or day < ends_on):
            held.setdefault(department_id, set()).add(role)
    allowed = sorted(department_id for department_id, roles in held.items() if _effective(roles))
    require(bool(allowed), "UNPROVISIONED_IDENTITY", "Your identity needs an active departmental role binding", 403)
    return allowed


def issue_session(db, identity: Identity, department_id: str | None, settings, *, user_agent=None, peer=None,
                  correlation_id=None, id_token=None, method="oidc", remember_me=False):
    allowed = allowed_departments(db, identity)
    if department_id is None:
        names = dict(db.execute(select(Department.id, Department.name).where(Department.id.in_(allowed))).all())
        require(len(allowed) == 1, "DEPARTMENT_REQUIRED", "Select a department for this session", 409,
                {"departments": [{"id": d, "name": names[d]} for d in allowed]})
        department_id = allowed[0]
    require(department_id in allowed, "FORBIDDEN", "No access to this department", 403)
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    idle, hours = session_limits(db.get(Department, department_id), settings)
    if remember_me:
        from .services.auth_configuration import current
        idle, hours = None, current(db, settings).remember_me_days * 24
    moment = now()
    deadline = session_expiry(moment, moment, idle, hours)
    cookie_deadline = session_expiry(moment, moment, None, hours)
    session = Session(identity_id=identity.id, department_id=department_id, created_at=moment,
                      token_hash=digest(token), csrf_hash=digest(csrf),
                      expires_at=deadline or moment, expiry_unlimited=deadline is None,
                      last_active_at=moment, device_label=device_label(user_agent), network_label=network_label(peer),
                      auth_method=method, remember_me=remember_me)
    identity.last_sign_in_at = moment
    db.add(session)
    db.flush()
    session_event(db, session, identity.person_id, "auth.sign_in", correlation_id=correlation_id)
    return {"token": token, "csrf": csrf, "expires_at": deadline.isoformat() if deadline else None, "session_id": session.id,
            "cookie_expires_at": cookie_deadline.isoformat() if cookie_deadline else None}


def session_expiry(created_at, moment, idle, hours):
    """Use only finite limits. None means genuinely no deadline, never a distant date sentinel."""
    latest = aware(datetime.max)
    deadlines = []
    if hours is not None:
        deadlines.append(created_at + min(timedelta(hours=hours), latest - created_at))
    if idle is not None:
        deadlines.append(moment + min(timedelta(minutes=idle), latest - moment))
    return min(deadlines) if deadlines else None


def session_limits(department, settings) -> tuple[int | None, int | None]:
    """Effective finite limits; NULL duration fields still inherit legacy deployment values."""
    idle = None if department.session_idle_unlimited else (
        department.session_idle_minutes if department.session_idle_minutes is not None else settings.session_idle_minutes)
    hours = None if department.session_max_unlimited else (
        department.session_max_hours if department.session_max_hours is not None else settings.session_hours)
    return idle, hours


def session_deadline(session, limits=None):
    """Pure read: combine stored state and current policy without recording activity."""
    deadlines = [] if session.expiry_unlimited else [aware(session.expires_at)]
    if limits is not None and not session.remember_me:
        current = session_expiry(aware(session.created_at), aware(session.last_active_at or session.created_at), *limits)
        if current is not None:
            deadlines.append(current)
    return min(deadlines) if deadlines else None


def session_active(session, moment=None, limits=None):
    deadline = session_deadline(session, limits)
    return not session.revoked and (deadline is None or deadline > (moment or now()))


def live_session_clause(moment=None):
    return and_(Session.revoked.is_(False), or_(Session.expiry_unlimited.is_(True), Session.expires_at > (moment or now())))


def revoke_session_installations(db, session_id, reason='authorizing_session_revoked'):
    from . import models as m
    from .services.pwa import revoke_installation
    for installation in db.scalars(select(m.PushInstallation).where(
            m.PushInstallation.session_id == session_id, m.PushInstallation.state == 'enabled')):
        revoke_installation(db, installation, reason)


def revoke_session(session, *, cleanup=True):
    session.revoked, session.revision = True, session.revision + 1
    session.oidc_id_token = None
    if session.expiry_unlimited:
        session.expiry_unlimited, session.expires_at = False, now()
    state = inspect(session, raiseerr=False)
    db = state.session if state is not None else None
    if cleanup and db is not None:
        revoke_session_installations(db, session.id)


def reconcile_session_limits(db, department_id, previous, current):
    """Apply a policy transition atomically, anchored to actual activity, preserving ended sessions."""
    moment = now()
    rows = db.scalars(select(Session).where(Session.department_id == department_id, live_session_clause(moment))
                      .with_for_update())
    for session in rows:
        prior = session_deadline(session, previous)
        # A session already ended under the previous policy cannot gain access through a later toggle or Undo.
        deadline = prior if session.remember_me or (prior is not None and prior <= moment) else session_expiry(
            aware(session.created_at), aware(session.last_active_at or session.created_at), *current)
        session.expiry_unlimited = deadline is None
        if deadline is not None:
            session.expires_at = deadline
        # Also invalidates a refresh read before this transition, even if the resulting deadline is identical.
        session.revision += 1


def _session_activity_lookup(token_hash):
    return (select(Session.id, Session.created_at, Session.expires_at, Session.expiry_unlimited, Session.remember_me,
                   Session.last_active_at, Session.revoked, Session.revision, Session.department_id,
                   Department.revision.label("department_revision"), Department.session_idle_minutes,
                   Department.session_max_hours, Department.session_idle_unlimited, Department.session_max_unlimited)
            .join(Department, Department.id == Session.department_id)
            .where(Session.token_hash == token_hash, Session.revoked.is_(False)))


def prepare_session_reads(db):
    # Empty cannot be a SHA-256 token digest. These reads prime SQL shapes without resolving an identity.
    db.execute(_session_lookup("")).close()
    db.connection().execute(_session_activity_lookup("")).close()


def keep_alive(engine, token: str | None, settings) -> None:
    """Record foreground activity at most once a minute. Never revive ended sessions or overwrite a policy change."""
    if not token:
        return
    try:
        moment = now()
        with engine.begin() as conn:
            row = conn.execute(_session_activity_lookup(digest(token))).first()
            if row is None:
                return
            idle, hours = session_limits(row, settings)
            if not session_active(row, moment, (idle, hours)):
                return
            slid = aware(row.expires_at) if row.remember_me else session_expiry(aware(row.created_at), moment, idle, hours)
            changed = (slid is None) != row.expiry_unlimited or (slid is not None and
                      abs(slid - aware(row.expires_at)) >= timedelta(minutes=1))
            if changed or row.last_active_at is None \
                    or moment - aware(row.last_active_at) >= timedelta(minutes=1):
                same_policy = select(Department.id).where(Department.id == row.department_id,
                                                         Department.revision == row.department_revision).exists()
                conn.execute(update(Session).where(
                    Session.id == row.id, live_session_clause(moment), same_policy, Session.revision == row.revision,
                    Session.expiry_unlimited == row.expiry_unlimited, Session.expires_at == row.expires_at,
                    Session.last_active_at.is_(None) if row.last_active_at is None else Session.last_active_at == row.last_active_at
                ).values(expires_at=slid if slid is not None else row.expires_at,
                         expiry_unlimited=slid is None, last_active_at=moment))
    except Exception as exc:
        log.warning('session_liveness_write_skipped', extra={'error_code': type(exc).__name__})


def _signin_key(settings, purpose: bytes) -> bytes:
    from .data_crypto import data_key_from_secret
    key = data_key_from_secret(settings.fmuc_data_key)
    if key is None:
        require(settings.environment in {"development", "test"}, "LOGIN_UNCONFIGURED",
                "Sign-in is unavailable; contact the administrator", 503)
        # Development/test keys stay stable across workers/restarts without a deployment secret.
        key = b"fmuc:signin-account-buckets:public:v1"
    return hmac.digest(key, purpose, "sha256")


def login_digest(value: str, settings) -> str:
    """Keyed hash of a sign-in account name or client address: both are low in entropy, so a plain hash reverses."""
    return hmac.new(_signin_key(settings, b"signin-event-and-limit-keys:v1"), value.encode(), "sha256").hexdigest()


def login_keys(username, peer, settings):
    account = username.strip().casefold()
    return (login_digest("account:" + account, settings), login_digest("peer:" + peer, settings),
            login_digest(f"pair:{len(account)}:{account}{peer}", settings))


def login_account_bucket_keys(identifier, settings):
    """Fixed storage and shared admission for colliding known or unknown identifiers."""
    key = _signin_key(settings, b"fmuc:signin-account-buckets")
    bucket = int.from_bytes(hmac.digest(key, identifier.casefold().encode(), "sha256"), "big") % LOGIN_ACCOUNT_BUCKET_COUNT
    return digest(f"login-account-consecutive-bucket:{bucket}"), digest(f"login-account-delay-bucket:{bucket}")


def _login_deadline(moment, duration):
    return moment + min(duration, aware(datetime.max) - moment)


def login_event(db, keys, outcome, correlation_id, session_id=None, *, window, known_identity=False,
                record_each=False):
    if outcome != "success":
        from .rate_limit import hit
        # Untrusted input shares outcome counters; callers can request one retained row per attempt.
        subject = keys[0] if known_identity and outcome in {"INVALID_LOGIN", "DEPARTMENT_REQUIRED", "SIGN_IN_BUSY"} else "rejected"
        hits, _ = hit(db.connection(), "login-audit:" + outcome, subject, window, now())
        if hits != 1 and not record_each:
            return
    db.add(AuthLoginEvent(account_hash=keys[0], peer_hash=keys[1], outcome=outcome,
                          correlation_id=correlation_id or "login", session_id=session_id))


def resolve_identifier(db, identifier, settings, *, institutional_issuer=None):
    """Indexed aliases, with provider subjects considered only for explicit institutional routing."""
    normalized = identifier.strip().casefold()
    matches = db.execute(select(Account, Identity).outerjoin(Identity, Identity.id == Account.identity_id)
                         .where(or_(Account.username_normalized == normalized, Account.email_normalized == normalized))
                         .limit(2)).all()
    issuers = ["local-development"] if settings.environment != "production" else []
    if institutional_issuer:
        issuers.append(institutional_issuer)
    identities = list(db.scalars(select(Identity).where(Identity.issuer.in_(issuers),
                                                       Identity.subject_normalized == normalized).limit(2)))
    if matches:
        return matches[0] if len(matches) == 1 else (None, None)
    return None, identities[0] if len(identities) == 1 else None


def _login_identity(db, data, settings):
    return resolve_identifier(db, data.username, settings)


def identifier_method(engine, identifier, peer, settings):
    from .db import transaction
    from .rate_limit import enforce
    from .services import auth_configuration
    enforce(engine, "login-identify", [login_digest("peer:" + peer, settings)], settings.password_login_address_limit,
            settings.password_login_window_seconds, "Too many attempts; try again later")
    with transaction(engine) as db:
        config = auth_configuration.current(db, settings)
        _, identity = resolve_identifier(db, identifier, settings,
            institutional_issuer=config.issuer if config.institutional_enabled and config.mode != "password_only" else None)
        # Unknown names do the same queries/file read; only a configured binding selects the provider.
        local_admin = auth_configuration.local_password_admin(db, identity, settings)
        grants = db.execute(select(RoleBinding, Organisation.timezone)
            .join(Department, Department.id == RoleBinding.department_id)
            .join(Organisation, Organisation.id == Department.organisation_id)
            .where(RoleBinding.identity_id == (identity.id if identity else ""))).all()
        active = any(_live(binding, now().astimezone(ZoneInfo(zone)).date()) for binding, zone in grants)
        bound = any(item.identity_id == (identity.id if identity else None) and item.issuer == config.issuer
                    for item in config.institutional_bindings)
        institutional = (identity is not None and not identity.disabled and active and not local_admin and bound
                         and config.institutional_enabled and config.mode != "password_only")
        return {"method": "institutional" if institutional else "password"}


def _credential_state(account, identity):
    return (None if account is None else (account.id, account.identity_id, account.username, account.email, account.accepted_at,
                                          account.password_changed_at, account.revision),
            None if identity is None else (identity.id, identity.issuer, identity.subject, identity.password_hash,
                                           identity.disabled, identity.person_id, identity.revision))


def _lock_login(db, keys):
    """Serialize overlapping admission scopes across workers, independently of rota writes."""
    if db.get_bind().dialect.name == 'postgresql':
        for key in sorted({int.from_bytes(hashlib.sha256(('password-login:' + key).encode()).digest()[:8],
                                         'big', signed=True) for key in keys}):
            db.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})


def password_login(engine, data, peer: str, settings, *, user_agent=None, correlation_id=None, previous_token=None):
    from .identity_store import IdentityStoreError, store_for
    from .rate_limit import counter_key, hit
    from .services import auth_configuration
    window, moment = settings.password_login_window_seconds, now()
    progressive = settings.password_login_progressive_delay
    keys, limits, delay_key, account_delay_key, consecutive_key = None, None, None, None, None
    reserved_pair_deadline = reserved_account_deadline = reserved_consecutive_stamp = None
    verification_reserved = False
    refusal = "Too many attempts from this connection; try again shortly" if progressive else "Too many attempts; try again later"

    def reserve(db):
        nonlocal keys, limits, delay_key, account_delay_key, consecutive_key, moment
        nonlocal reserved_pair_deadline, reserved_account_deadline, reserved_consecutive_stamp, verification_reserved
        account, identity = _login_identity(db, data, settings)
        username = account.username if account else identity.subject if identity else data.username
        keys = login_keys(username, peer, settings)
        bucket_keys = login_account_bucket_keys(username, settings) if progressive and password_login_enabled(settings) else ()
        _lock_login(db, (*keys, *bucket_keys))
        limits = (("login-account", keys[0], settings.password_login_account_limit),
                  ("login-pair", keys[2], settings.password_login_pair_limit),
                  ("login-address", keys[1], settings.password_login_address_limit))
        delay_key = digest("login-delay:" + keys[2])
        if not password_login_enabled(settings):
            login_event(db, keys, "NOT_FOUND", correlation_id, window=window)
            return Problem(404, "NOT_FOUND", "Not found")
        created_delays = []
        if progressive:
            consecutive_key, account_delay_key = bucket_keys
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            # Create absent rows before locking all account/pair scopes in one consistent order.
            for key in sorted((delay_key, account_delay_key, consecutive_key)):
                expiry = _login_deadline(moment, timedelta(days=30)) if key == consecutive_key else moment
                created = db.execute(insert(RateLimit).values(key_hash=key, hits=0, expires_at=expiry)
                                     .on_conflict_do_nothing(index_elements=["key_hash"])
                                     .returning(RateLimit.key_hash)).scalar_one_or_none()
                if created:
                    created_delays.append(created)
            states = {row.key_hash: row for row in db.execute(select(RateLimit.key_hash, RateLimit.hits, RateLimit.expires_at)
                      .where(RateLimit.key_hash.in_((delay_key, account_delay_key, consecutive_key)))
                      .order_by(RateLimit.key_hash).with_for_update())}
            delays = {key: row.expires_at for key, row in states.items() if key != consecutive_key}
            moment = now()  # A lock wait must not leave the admission check using an old timestamp.
            # Non-command request counters may prune expired rows before this lock query.
            deadline = max((aware(value) for value in delays.values()), default=moment)
            if deadline > moment:
                db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(created_delays)))
                wait = max(1, int((deadline - moment).total_seconds() + 0.999))
                login_event(db, keys, "LOGIN_RATE_LIMIT", correlation_id, window=window)
                scope = "for this account from this connection" if aware(delays.get(delay_key, moment)) > moment else "for this account"
                message = f"Too many sign-in attempts {scope}; try again in {wait} second{'s' if wait != 1 else ''}"
                return Problem(429, "LOGIN_RATE_LIMIT", message, {"retry_after": wait})
            prior = states.get(consecutive_key)
            if prior and aware(prior.expires_at) == aware(datetime.max) and consecutive_key not in created_delays:
                # No distinct reservation stamp remains: refusing preserves the success-reset race guard.
                db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(created_delays)))
                login_event(db, keys, "LOGIN_RATE_LIMIT", correlation_id, window=window)
                wait = min(86400, max(1, int((aware(datetime.max) - moment).total_seconds() + 0.999)))
                return Problem(429, "LOGIN_RATE_LIMIT",
                    f"Too many sign-in attempts for this account; try again in {wait} second{'s' if wait != 1 else ''}",
                    {"retry_after": wait})
        else:
            moment = now()
        for scope, subject, limit in limits:
            if progressive and scope in {"login-account", "login-pair"}:
                continue
            key, ends = counter_key(scope, subject, window, moment)
            count = db.scalar(select(RateLimit.hits).where(RateLimit.key_hash == key)) or 0
            if count >= limit:
                db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(created_delays)))
                login_event(db, keys, "LOGIN_RATE_LIMIT", correlation_id, window=window)
                wait = max(1, int((ends - moment).total_seconds() + 0.999))
                return Problem(429, "LOGIN_RATE_LIMIT", refusal, {"retry_after": wait})
        if not verification_reserved:
            verification_reserved = PASSWORD_VERIFICATIONS.acquire(blocking=False)
            if not verification_reserved:
                db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(created_delays)))
                login_event(db, keys, "LOGIN_RATE_LIMIT", correlation_id, window=window)
                return Problem(429, "LOGIN_RATE_LIMIT", "Too many attempts; try again later", {"retry_after": 1})
        if progressive:
            failures = (prior.hits if prior and aware(prior.expires_at) > moment else 0) + 1
            # Keep a monotonic stamp through resets so an older success cannot erase a new failure.
            reserved_consecutive_stamp = max(_login_deadline(moment, timedelta(days=30)),
                _login_deadline(aware(prior.expires_at), timedelta(microseconds=1)) if prior else moment)
            db.execute(insert(RateLimit).values(key_hash=consecutive_key, hits=failures, expires_at=reserved_consecutive_stamp)
                       .on_conflict_do_update(index_elements=["key_hash"],
                           set_={"hits": failures, "expires_at": reserved_consecutive_stamp}))
        # Reserve in-flight failure capacity; a crash retains it until its counter retention expires.
        for scope, subject, _ in limits:
            count, _ = hit(db.connection(), scope, subject, window, moment)
            if progressive and scope in {"login-account", "login-pair"}:
                if scope == "login-pair":
                    reserved_pair_deadline = _login_deadline(moment, timedelta(seconds=min(60, 2 ** min(count - 1, 6))))
                    key, deadline = delay_key, reserved_pair_deadline
                else:
                    key = account_delay_key
                    wait = min(86400, 60 * 2 ** min(failures - 100, 11)) if failures >= 100 else 0
                    wait = max(wait, 1 if count >= settings.password_login_account_limit else 0)
                    reserved_account_deadline = deadline = _login_deadline(moment, timedelta(seconds=wait))
                db.execute(insert(RateLimit).values(key_hash=key, hits=0, expires_at=deadline)
                           .on_conflict_do_update(index_elements=["key_hash"], set_={"expires_at": deadline}))
        return _login_identity(db, data, settings)

    try:
        try:
            reserved = liveness_write(engine, reserve, context={"route": "auth.login", "phase": "reserve"})
            if isinstance(reserved, Problem):
                return reserved
            account, identity = reserved
            username = account.username if account else identity.subject if identity else data.username.strip().casefold()
            state = _credential_state(account, identity)
            valid = False
            users, credential = None, None
            if account or identity is None:
                users = store_for(settings)
                try:
                    # Verify before eligibility checks so unknown, pending and disabled accounts all pay the hash cost.
                    if users:
                        credential = users.verify_credential(username, data.password)
                        valid = credential is not None
                    else:
                        PASSWORDS.verify(_DUMMY_HASH, data.password)
                except IdentityStoreError:
                    try:
                        PASSWORDS.verify(_DUMMY_HASH, data.password)
                    except (VerificationError, InvalidHashError):
                        pass
                except (VerificationError, InvalidHashError):
                    pass
                valid = bool(valid and account and account.accepted_at)
            else:
                try:
                    valid = PASSWORDS.verify(identity.password_hash if identity and identity.password_hash else _DUMMY_HASH, data.password)
                except (VerificationError, InvalidHashError):
                    pass
        except Problem as exc:
            if exc.status >= 500:
                log.error("password_login_failed", extra={"correlation_id": correlation_id, "phase": "reserve", "error_code": exc.code})
            raise
        except Exception:
            log.exception("password_login_failed", extra={"correlation_id": correlation_id, "phase": "reserve"})
            raise
    finally:
        if verification_reserved:
            PASSWORD_VERIFICATIONS.release()
    valid = bool(valid and identity and not identity.disabled)
    write_context = {"route": "auth.login", "phase": "finish",
                     "lock_diagnostic": "password_login", "lock_relations": ("accounts", "identities")}

    def finish(db):
        _lock_login(db, (*keys, *((consecutive_key, account_delay_key) if progressive else ())))
        current_account, current_identity = _login_identity(db, data, settings)
        locked_rows = {}
        sign_in_busy = False
        if valid:
            # Credential edits use the same rows. Never queue sign-in behind one: a locked
            # credential row fails closed and the caller can retry after the edit commits.
            for model, row in ((Account, current_account), (Identity, current_identity)):
                if row is not None:
                    locked_rows[model] = db.scalar(select(model).where(model.id == row.id)
                        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
                    sign_in_busy |= locked_rows[model] is None
        current_account, current_identity = _login_identity(db, data, settings)
        if sign_in_busy:
            log_row_lock_diagnostic(db, settings, write_context)
            problem = Problem(409, "SIGN_IN_BUSY",
                              "Sign-in is busy while your credentials are being updated; try again shortly",
                              {"retry_after": 1})
            _release_failure_reservation(db)
            login_event(db, keys, problem.code, correlation_id, window=window,
                        known_identity=bool(current_account or current_identity), record_each=True)
            return problem
        current = valid and all(locked_rows.get(model) is not None for model, row in
                                ((Account, current_account), (Identity, current_identity)) if row is not None)
        current = bool(current and state == _credential_state(current_account, current_identity))
        current = bool(current and auth_configuration.password_eligible(
            db, current_identity, settings, password_verified=True))
        if current and users:
            try:
                current = users.credential_matches(username, credential)
            except IdentityStoreError:
                current = False
        values, problem = None, Problem(401, "INVALID_LOGIN", "Unable to sign in with these credentials")
        if current:
            try:
                values = issue_session(db, current_identity, data.department_id, settings, user_agent=user_agent, peer=peer,
                                       correlation_id=correlation_id, method="password", remember_me=data.remember_me)
                if previous_token:
                    previous = db.scalar(select(Session).where(Session.token_hash == digest(previous_token)).with_for_update())
                    if previous and not previous.revoked:
                        revoke_session(previous)
                        prior_identity = db.get(Identity, previous.identity_id)
                        session_event(db, previous, prior_identity.person_id, "auth.session_revoke", correlation_id=correlation_id)
            except Problem as exc:
                if exc.code == "DEPARTMENT_REQUIRED":
                    problem = exc
        if values is not None or problem.code == "DEPARTMENT_REQUIRED":
            _release_failure_reservation(db)
        login_event(db, keys, "success" if values else problem.code, correlation_id, values["session_id"] if values else None,
                    window=window, known_identity=bool(current_account or current_identity))
        return {**values, "display_name": current_identity.display_name} if values else problem

    def _release_failure_reservation(db):
        if progressive:
            pair_key, _ = counter_key("login-pair", keys[2], window, moment)
            # An older success cannot erase a newer in-flight reservation for the same pair.
            cleared = db.execute(delete(RateLimit).where(RateLimit.key_hash == delay_key,
                                 RateLimit.expires_at == reserved_pair_deadline).returning(RateLimit.key_hash)).scalar_one_or_none()
            if cleared:
                db.execute(delete(RateLimit).where(RateLimit.key_hash == pair_key))
            cleared = db.execute(update(RateLimit).where(RateLimit.key_hash == consecutive_key,
                RateLimit.expires_at == reserved_consecutive_stamp).values(hits=0)
                .returning(RateLimit.key_hash)).scalar_one_or_none()
            if cleared:
                db.execute(delete(RateLimit).where(RateLimit.key_hash == account_delay_key,
                                                  RateLimit.expires_at == reserved_account_deadline))
        for scope, subject, _ in limits:
            if progressive and scope == "login-pair":
                continue  # Only the matching reservation may clear this resettable counter.
            key, _ = counter_key(scope, subject, window, moment)
            db.execute(update(RateLimit).where(RateLimit.key_hash == key, RateLimit.hits > 0).values(hits=RateLimit.hits - 1))

    try:
        return liveness_write(engine, finish, context=write_context)
    except Problem as exc:
        if exc.status >= 500:
            log.error("password_login_failed", extra={"correlation_id": correlation_id, "phase": "finish", "error_code": exc.code})
        raise
    except Exception:
        log.exception("password_login_failed", extra={"correlation_id": correlation_id, "phase": "finish"})
        raise


def rotate_session(db, actor, settings):
    """Replace both bearer secrets without extending the original authentication lifetime."""
    session = db.scalar(select(Session).where(Session.id == actor.session_id).with_for_update())
    require(session is not None and session.identity_id == actor.identity_id and
            session_active(session, now(), session_limits(db.get(Department, session.department_id), settings)),
            "SESSION_EXPIRED", "Your session has expired or was revoked; sign in again", 401)
    limits = session_limits(db.get(Department, session.department_id), settings)
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    session.token_hash, session.csrf_hash = digest(token), digest(csrf)
    session.revision += 1
    deadline = session_deadline(session, limits)
    cookie_deadline = deadline if session.remember_me else session_expiry(aware(session.created_at), now(), None, limits[1])
    return {"token": token, "csrf": csrf, "expires_at": deadline.isoformat() if deadline else None,
            "session_id": session.id, "display_name": actor.display_name,
            "cookie_expires_at": cookie_deadline.isoformat() if cookie_deadline else None}


def person_access(db, actor: Actor, person_id: str):
    from .models import Person
    person = db.info.get('bulk_relationship_references', {}).get((Person, person_id))
    person = person or db.info.get('bulk', {}).get('relationship_references', {}).get((Person, person_id))
    if person is not None:
        state = inspect(person)
        if state.session is not db or state.deleted:
            person = None
    person = person or db.get(Person, person_id)
    require(person is not None and person not in db.deleted and person.organisation_id == actor.organisation_id,
            "NOT_FOUND", "Person not found", 404)
    return person
