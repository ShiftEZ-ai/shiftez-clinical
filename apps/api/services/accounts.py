"""Managed sign-in accounts: invitations, one-time password links, password changes, sessions and session limits.

The sign-in itself lives in the environment's sign-in service (apps/api/identity_store.py); the application keeps the
identity, the account (username, email, whether the invitation was accepted) and one-time links, of which only the
SHA-256 of the random token is stored. Admins never set or see a password: they hand the person a link.
Every change is audited (command bus, or an audit event written here for the public link and password endpoints).
"""
import secrets
from datetime import timedelta
from sqlalchemy import case, or_, select, update
from packages.contracts import schemas as s
from packages.domain.time import aware
from apps.api import models as m
from apps.api.auth import (digest, live_session_clause, person_access, reconcile_session_limits, require_permission,
                          require_role, session_event, session_limits)
from apps.api.errors import Problem, require
from apps.api.identity_store import IdentityStoreError, store_for
from apps.api.serialization import record
from .access import organisation_identities, require_role_change, require_sign_in
from .common import expected, history
from .events import invalidate_audience_inputs

LINK_HOURS = {"invite": 72, "reset": 24}
LOCAL_ISSUER = "local-development"
PENDING = "pending:"  # subject prefix until the first sign-in through the provider binds the real subject


def store(settings):
    found = store_for(settings)
    require(found is not None, "ACCOUNTS_UNAVAILABLE",
            "Account management is not set up in this environment: the sign-in users file (ROTA_IDENTITY_USERS_FILE) "
            "is not configured. Ask the operator.", 409)
    return found


def written(action):
    """Run a users-file change; its errors become a clear, secret-free problem (and roll the transaction back)."""
    try:
        return action()
    except IdentityStoreError as exc:
        raise Problem(503, "SIGN_IN_SERVICE_UNAVAILABLE", str(exc)) from None


def link_url(settings, purpose, token):
    return f"{settings.public_origin}/{'invite' if purpose == 'invite' else 'reset'}/{token}"


def open_links(db, identity_id, purpose=None):
    query = select(m.AccountLink).where(m.AccountLink.identity_id == identity_id, m.AccountLink.used_at.is_(None),
                                        m.AccountLink.revoked_at.is_(None))
    if purpose:
        query = query.where(m.AccountLink.purpose == purpose)
    return [link for link in db.scalars(query) if aware(link.expires_at) > m.now()]


def lock_link_account(db, identity_id):
    # Link writers and acceptance share this row lock, including liveness writes.
    return db.scalar(select(m.Account).where(m.Account.identity_id == identity_id).with_for_update()
                     .execution_options(populate_existing=True))


def issue_link(db, actor, identity_id, purpose, settings):
    """A new one-time link (older open links of the same purpose stop working). The raw token leaves only through
    db.info, so the command bus returns it to the admin once without storing it in the receipt."""
    from .independence import today
    # Future grants also protect credentials: a recovered account could wait for its access to start.
    protected = db.scalar(select(m.RoleBinding.id).where(
        m.RoleBinding.identity_id == identity_id, m.RoleBinding.role.in_(s.EXPLICIT_ROLES),
        m.RoleBinding.revoked.is_(False),
        or_(m.RoleBinding.ends_on.is_(None), m.RoleBinding.ends_on > today(db, actor.department_id))).limit(1))
    if protected:
        require_permission(actor, "access.system_roles")
    account = lock_link_account(db, identity_id)
    require(account is not None, "ACCOUNT_NOT_MANAGED", "This sign-in is not managed by this app", 409)
    if purpose == "invite":
        require(account.accepted_at is None, "ALREADY_ACCEPTED",
                "This invitation was already accepted; send a password reset link instead", 409)
    moment = m.now()
    for old in open_links(db, identity_id, purpose):
        old.revoked_at, old.revision = moment, old.revision + 1
    token = secrets.token_urlsafe(32)
    link = m.AccountLink(department_id=actor.department_id, identity_id=identity_id, purpose=purpose,
                         token_hash=digest(token), expires_at=moment + timedelta(hours=LINK_HOURS[purpose]),
                         created_by=actor.identity_id)
    db.add(link)
    db.flush()
    db.info["one_time_link"] = link_url(settings, purpose, token)
    return {"id": link.id, "purpose": purpose, "expires_at": aware(link.expires_at).isoformat()}


def end_sessions(db, identity_id, keep=None, *, actor=None, correlation_id=None):
    moment = m.now()
    query = update(m.Session).where(m.Session.identity_id == identity_id, live_session_clause(moment))
    if keep:
        query = query.where(m.Session.id != keep)
    ended = list(db.scalars(query.values(revoked=True, revision=m.Session.revision + 1, expiry_unlimited=False, oidc_id_token=None,
                                        expires_at=case((m.Session.expiry_unlimited.is_(True), moment),
                                                        else_=m.Session.expires_at)).returning(m.Session)))
    person_id = db.get(m.Identity, identity_id).person_id
    for session in ended:
        session_event(db, session, person_id, "auth.session_revoke",
                      actor_id=actor.identity_id if actor else identity_id, correlation_id=correlation_id)
    return len(ended)


def managed(db, actor, identity_id, need_account=True):
    """The identity (and account) an admin of this department may manage."""
    identity = db.scalar(select(m.Identity).where(m.Identity.id == identity_id, organisation_identities(actor)))
    require(identity is not None, "NOT_FOUND", "Sign-in not found", 404)
    require_sign_in(identity)
    bindings = list(db.scalars(select(m.RoleBinding.department_id).where(m.RoleBinding.identity_id == identity_id)))
    require(all(d == actor.department_id for d in bindings), "OPERATOR_REQUIRED",
            "Cross-department sign-in changes require the institutional operator", 403)
    account = db.scalar(select(m.Account).where(m.Account.identity_id == identity_id))
    if need_account:
        require(account is not None, "ACCOUNT_NOT_MANAGED",
                "This sign-in is managed by the identity provider, not by this app", 409)
    return identity, account


def account_command(db, actor, command, settings):
    kind = command.kind
    if kind == "account.end_other_sessions":
        s.Decision.model_validate(command.payload)
        require(command.target_id in (None, actor.identity_id), "FORBIDDEN", "Only your own sessions can be ended", 403)
        return {"identity_id": actor.identity_id,
                "ended": end_sessions(db, actor.identity_id, keep=actor.session_id, actor=actor)}
    require_role(actor, "admin")
    if kind == "account.invite":
        return invite(db, actor, s.AccountInvite.model_validate(command.payload), settings)
    s.Decision.model_validate(command.payload)
    identity, account = managed(db, actor, command.target_id, need_account=kind != "account.end_sessions")
    if kind == "account.end_sessions":
        require(identity.id != actor.identity_id, "OWN_SESSIONS", "Use Sign out to end your own session", 409)
        return {"record": record(identity, exclude=("password_hash",)),
                "ended": end_sessions(db, identity.id, actor=actor)}
    if kind == "account.revoke_invite":
        lock_link_account(db, identity.id)
        links = open_links(db, identity.id, "invite")
        require(bool(links), "NO_OPEN_INVITE", "There is no open invitation to revoke", 409)
        for link in links:
            link.revoked_at, link.revision = m.now(), link.revision + 1
        return {"record": record(identity, exclude=("password_hash",)), "revoked": len(links)}
    require(not identity.disabled, "IDENTITY_DISABLED", "Enable sign-in first", 409)
    if kind == "account.resend":
        require(account.accepted_at is None, "ALREADY_ACCEPTED", "This invitation was already accepted; send a password reset link instead", 409)
        return {"record": record(identity, exclude=("password_hash",)), "link": issue_link(db, actor, identity.id, "invite", settings)}
    if kind == "account.reset_link":
        require(account.accepted_at is not None, "NOT_ACCEPTED", "The invitation has not been accepted yet; resend it instead", 409)
        store(settings)
        return {"record": record(identity, exclude=("password_hash",)), "link": issue_link(db, actor, identity.id, "reset", settings)}
    require(False, "UNKNOWN_COMMAND", "Unknown account command")


def invite(db, actor, value, settings):
    for role in set(value.roles):
        require_role_change(db, actor, role, grant=True)
    users = store(settings)
    if value.person_id:
        person_access(db, actor, value.person_id)
    # Local passwords are independent of whichever institutional provider is configured later.
    issuer, subject = LOCAL_ISSUER, value.username
    taken = db.scalar(select(m.Account).where(m.Account.username == value.username)) is not None or \
        db.scalar(select(m.Identity).where(m.Identity.issuer == issuer, m.Identity.subject == subject)) is not None
    require(not taken, "USERNAME_TAKEN", "This sign-in name is already taken", 409)
    identity = m.Identity(issuer=issuer, subject=subject, display_name=value.display_name, person_id=value.person_id)
    db.add(identity)
    db.flush()
    account = m.Account(identity_id=identity.id, username=value.username, email=value.email)
    db.add(account)
    from .independence import today
    for role in sorted(set(value.roles)):
        binding = m.RoleBinding(department_id=actor.department_id, identity_id=identity.id, role=role, starts_on=today(db, actor.department_id))
        db.add(binding)
        history(db, actor, "access", binding, value.reason)
    link = issue_link(db, actor, identity.id, "invite", settings)
    invalidate_audience_inputs(db, actor)
    pending_options = {"pending_for": owner} if (owner := db.info.get("staff_onboarding")) else {}
    written(lambda: users.create(value.username, value.display_name, value.email, **pending_options))
    return {"record": record(identity, exclude=("password_hash",)), "account": record(account), "link": link}


def session_command(db, actor, command, settings):
    require_role(actor, "admin")
    require(command.target_id == actor.department_id, "NOT_FOUND", "Department not found", 404)
    value = s.SessionSettingsInput.model_validate(command.payload)
    department = db.get(m.Department, actor.department_id)
    expected(department, command.expected_revision)
    # Validate the effective pair too: inherited values must still fit when both timeouts are finite.
    s.DeploymentSessionSettingsInput(
        idle_minutes=value.idle_minutes if value.idle_minutes is not None else settings.session_idle_minutes,
        max_hours=value.max_hours if value.max_hours is not None else settings.session_hours,
        idle_unlimited=value.idle_unlimited, max_unlimited=value.max_unlimited)
    previous = {"idle_minutes": department.session_idle_minutes, "max_hours": department.session_max_hours,
                "idle_unlimited": department.session_idle_unlimited, "max_unlimited": department.session_max_unlimited}
    old_limits = session_limits(department, settings)
    if (department.session_idle_minutes, department.session_max_hours,
        department.session_idle_unlimited, department.session_max_unlimited) != (
            value.idle_minutes, value.max_hours, value.idle_unlimited, value.max_unlimited):
        department.session_idle_minutes, department.session_max_hours = value.idle_minutes, value.max_hours
        department.session_idle_unlimited, department.session_max_unlimited = value.idle_unlimited, value.max_unlimited
        department.revision += 1
        reconcile_session_limits(db, department.id, old_limits, session_limits(department, settings))
        history(db, actor, "department_sessions", department, value.reason)
    return {"department": record(department), "sessions": session_view(department, settings),
            "previous": previous}


def session_view(department, settings):
    from apps.api.settings import new_department_session_limits
    defaults = new_department_session_limits()
    return {"idle_minutes": department.session_idle_minutes if department.session_idle_minutes is not None else settings.session_idle_minutes,
            "max_hours": department.session_max_hours if department.session_max_hours is not None else settings.session_hours,
            "idle_unlimited": department.session_idle_unlimited, "max_unlimited": department.session_max_unlimited,
            "inherited": {"idle_minutes": department.session_idle_minutes is None,
                          "max_hours": department.session_max_hours is None},
            "defaults": {"idle_minutes": defaults["session_idle_minutes"] if defaults["session_idle_minutes"] is not None
                                          else settings.session_idle_minutes, "max_hours": defaults["session_max_hours"]},
            "limits": s.SESSION_LIMITS}


def account_rows(db, identity_ids):
    """Account details for the Access page, by identity id."""
    accounts = {a.identity_id: a for a in db.scalars(select(m.Account).where(m.Account.identity_id.in_(identity_ids)))}
    invites = {}  # latest unused, unrevoked invitation per identity (it may have expired)
    for link in db.scalars(select(m.AccountLink).where(m.AccountLink.identity_id.in_(list(accounts)), m.AccountLink.purpose == "invite",
                                                        m.AccountLink.used_at.is_(None), m.AccountLink.revoked_at.is_(None))):
        invites[link.identity_id] = max(invites.get(link.identity_id, aware(link.expires_at)), aware(link.expires_at))
    moment = m.now()
    return {identity_id: {**record(a, exclude=("id", "revision", "created_at", "identity_id")),
                          "invite_expires_at": invites[identity_id].isoformat() if identity_id in invites else None,
                          "invite_open": identity_id in invites and invites[identity_id] > moment}
            for identity_id, a in accounts.items()}


# Public one-time links -------------------------------------------------------------------------------------------

INVALID = "This link is not valid: it may have expired, been replaced or been used already. Ask your admin for a new one."


def find_link(db, token, *, for_update=False):
    require(isinstance(token, str) and 20 <= len(token) <= 100, "LINK_INVALID", INVALID, 404)
    hashed = digest(token)
    query = select(m.AccountLink).where(m.AccountLink.token_hash == hashed)
    account = None
    if for_update:
        identity_id = db.scalar(query.with_only_columns(m.AccountLink.identity_id).where(
            m.AccountLink.used_at.is_(None), m.AccountLink.revoked_at.is_(None), m.AccountLink.expires_at > m.now()))
        require(identity_id is not None, "LINK_INVALID", INVALID, 404)
        account = lock_link_account(db, identity_id)
    link = db.scalar(query)
    valid = link is not None and secrets.compare_digest(link.token_hash, hashed) and link.used_at is None \
        and link.revoked_at is None and aware(link.expires_at) > m.now()
    require(valid, "LINK_INVALID", INVALID, 404)
    identity = db.get(m.Identity, link.identity_id)
    if not for_update:
        account = db.scalar(select(m.Account).where(m.Account.identity_id == link.identity_id))
    if identity is not None:
        require_sign_in(identity)
    require(identity is not None and account is not None and not identity.disabled, "LINK_INVALID", INVALID, 404)
    require(link.purpose != "invite" or account.accepted_at is None, "LINK_INVALID", INVALID, 404)
    return link, identity, account


def link_info(db, token):
    link, identity, account = find_link(db, token)
    return {"purpose": link.purpose, "display_name": identity.display_name, "username": account.username,
            "expires_at": aware(link.expires_at).isoformat()}


def password_problems(password, account):
    lowered = password.casefold()
    problems = []
    if len(password) < 12:
        problems.append("Use at least 12 characters")
    if lowered in {account.username.casefold(), account.email.casefold(), account.email.split("@")[0].casefold()}:
        problems.append("Do not use your sign-in name or email address as the password")
    if len(set(password)) < 4:
        problems.append("Use more varied characters")
    return problems


def audit(db, department_id, identity_id, action, reason, correlation_id, **detail):
    db.add(m.AuditEvent(department_id=department_id, actor_id=identity_id, action=action, target_id=identity_id,
                        reason=reason, detail=detail, correlation_id=correlation_id))


def accept_link(db, token, password, settings, correlation_id):
    link, identity, account = find_link(db, token, for_update=True)
    problems = password_problems(password, account)
    require(not problems, "PASSWORD_WEAK", "; ".join(problems), 422)
    users = store(settings)
    moment = m.now()
    link.used_at, link.revision = moment, link.revision + 1
    for other in open_links(db, identity.id):
        if other.id != link.id:
            other.revoked_at, other.revision = moment, other.revision + 1
    first = account.accepted_at is None
    account.accepted_at = account.accepted_at or moment
    account.password_changed_at, account.revision = moment, account.revision + 1
    ended = end_sessions(db, identity.id, correlation_id=correlation_id)
    audit(db, link.department_id, identity.id, "account.invite_accepted" if first else "account.password_reset",
          "Invitation accepted; password set" if first else "Password reset with a one-time link",
          correlation_id, link_id=link.id, sessions_ended=ended)
    db.flush()
    written(lambda: users.set_password(account.username, password))  # last: a failure rolls the link back
    return {"accepted": True, "sign_in": "/"}


def change_password(db, actor, value, settings, correlation_id, *, account):
    require_sign_in(db.get(m.Identity, actor.identity_id))
    require(account is not None and account.identity_id == actor.identity_id, "ACCOUNT_NOT_MANAGED",
            "Your sign-in is managed by your organisation's identity service; change your password there.", 409)
    users = store(settings)
    require(written(lambda: users.verify(account.username, value.current_password)), "PASSWORD_INCORRECT",
            "The current password is not correct", 403)
    problems = password_problems(value.new_password, account)
    if value.new_password == value.current_password:
        problems.append("Choose a password different from the current one")
    require(not problems, "PASSWORD_WEAK", "; ".join(problems), 422)
    account.password_changed_at, account.revision = m.now(), account.revision + 1
    ended = end_sessions(db, actor.identity_id, keep=actor.session_id, actor=actor, correlation_id=correlation_id)
    audit(db, actor.department_id, actor.identity_id, "account.password_change", "Changed own password",
          correlation_id, sessions_ended=ended)
    db.flush()
    written(lambda: users.set_password(account.username, value.new_password))
    return {"changed": True, "sessions_ended": ended}


# Sign-in through the provider ------------------------------------------------------------------------------------

def bind_first_sign_in(db, settings, subject, username):
    """An accepted invitation signs in for the first time: bind the provider's real subject to the pending identity.
    The username comes from the configured provider (the one this app manages the users of), never from the browser."""
    identity = db.scalar(select(m.Identity).where(m.Identity.issuer == settings.oidc_issuer,
                                                  m.Identity.subject == PENDING + username))
    account = identity and db.scalar(select(m.Account).where(m.Account.identity_id == identity.id))
    if not account or account.accepted_at is None or account.username != username:
        return None
    identity.subject, identity.revision = subject, identity.revision + 1
    link = db.scalar(select(m.AccountLink).where(m.AccountLink.identity_id == identity.id).order_by(m.AccountLink.created_at.desc(), m.AccountLink.seq.desc()))
    if link:
        audit(db, link.department_id, identity.id, "account.first_sign_in", "First sign-in bound the provider's user ID", "oidc")
    db.flush()
    return identity


def adopt(db, settings, identity, username):
    """A sign-in made before accounts were managed here (for example by the old bootstrap script) becomes manageable
    the next time it signs in, when the sign-in service knows the same username."""
    users = store_for(settings)
    if not users or not username or db.scalar(select(m.Account).where(
            (m.Account.identity_id == identity.id) | (m.Account.username == username))) is not None:
        return
    try:
        entry = users.get(username)
    except IdentityStoreError:
        return
    if entry and not entry.get("disabled"):
        db.add(m.Account(identity_id=identity.id, username=username, email=str(entry.get("email", ""))[:254], accepted_at=m.now()))
