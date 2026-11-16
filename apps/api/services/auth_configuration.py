"""Deployment-local authentication settings with write-only encrypted credentials and one-step Undo."""
from types import SimpleNamespace

from sqlalchemy import select
from zoneinfo import ZoneInfo

from packages.contracts import schemas as s
from .. import models as m
from ..auth import current_roles, require_permission, require_role
from ..data_crypto import decrypt_secret, encrypt_secret
from ..errors import Problem, require
from ..identity_store import IdentityStoreError, store_for

SINGLETON = "environment"
SECRET_CONTEXT = "auth-configuration:oidc-client-secret:v1"


def current(db, settings):
    row = db.get(m.AuthConfiguration, SINGLETON)
    return s.AuthSettings.model_validate(row.data) if row else s.AuthSettings()


def local_password_admin(db, identity, settings=None, *, password_verified=False):
    identity_id = identity.id if identity else ""
    # Before a password is verified (e.g. /auth/identify), known and unknown names do the same queries and the same
    # users-file read, so neither the answer time nor the query trail reveals whether an account exists. Only after a
    # verified password may the file read be skipped (it then reveals nothing new).
    grants = db.execute(select(m.RoleBinding.starts_on, m.RoleBinding.ends_on, m.Organisation.timezone)
                        .join(m.Department, m.Department.id == m.RoleBinding.department_id)
                        .join(m.Organisation, m.Organisation.id == m.Department.organisation_id)
                        .where(m.RoleBinding.identity_id == identity_id, m.RoleBinding.role == "admin",
                               m.RoleBinding.revoked.is_(False))).all()
    account = db.scalar(select(m.Account).where(m.Account.identity_id == identity_id))
    usable = False
    if settings is not None and not password_verified:
        try:
            users = store_for(settings)
            usable = bool(users and users.has_credential(account.username if account else ""))
        except IdentityStoreError:
            pass
    active_admin = any(start <= m.now().astimezone(ZoneInfo(zone)).date()
                       and (end is None or m.now().astimezone(ZoneInfo(zone)).date() < end)
                       for start, end, zone in grants)
    if identity is None or identity.disabled or not active_admin:
        return False
    if settings is None:
        return bool(account and account.accepted_at or identity.password_hash)
    if account:
        return bool(account.accepted_at and (password_verified or usable))
    return bool(settings.environment != "production" and identity.issuer == "local-development" and identity.password_hash)


def password_eligible(db, identity, settings, *, password_verified=False):
    config = current(db, settings)
    if identity is None:
        return False
    if config.mode == "password_only" or local_password_admin(db, identity, settings,
                                                              password_verified=password_verified):
        return True
    return config.mode == "both" and not any(v.identity_id == identity.id for v in config.institutional_bindings)


def require_break_glass(db, settings):
    candidates = db.execute(select(m.Identity, m.RoleBinding.starts_on, m.Organisation.timezone)
        .join(m.RoleBinding, m.RoleBinding.identity_id == m.Identity.id)
        .join(m.Department, m.Department.id == m.RoleBinding.department_id)
        .join(m.Organisation, m.Organisation.id == m.Department.organisation_id).where(
        m.RoleBinding.role == "admin", m.RoleBinding.revoked.is_(False), m.RoleBinding.ends_on.is_(None),
        m.Identity.disabled.is_(False)))
    require(any(start <= m.now().astimezone(ZoneInfo(zone)).date() and local_password_admin(db, identity, settings)
                for identity, start, zone in candidates), "BREAK_GLASS_REQUIRED",
            "Keep at least one active administrator with an accepted Rota password account", 409)


def protect_break_glass(db, settings):
    """Call before an irreversible users-file change, after staging the access change in this transaction."""
    if db.get(m.AuthConfiguration, SINGLETON) is not None:
        db.flush()
        require_break_glass(db, settings)


def status(db, actor, settings):
    require_role(actor, "admin")
    row = db.get(m.AuthConfiguration, SINGLETON)
    return s.AuthConfigStatus(**current(db, settings).model_dump(), revision=row.revision if row else 0,
                              redirect_uri=settings.public_origin + "/auth/callback",
                              client_secret_set=bool(row and row.secret_ciphertext),
                              key_configured=bool(settings.fmuc_data_key), can_undo=bool(row and row.previous)).model_dump(mode="json")


def remove_identity_bindings(db, actor, identity_ids, reason):
    """Anonymisation must also remove identifying subjects from the mutable Undo snapshot."""
    row = db.scalar(select(m.AuthConfiguration).where(m.AuthConfiguration.id == SINGLETON).with_for_update())
    if row is None:
        return
    changed = False
    for container in ("data", "previous"):
        value = getattr(row, container)
        if value is None:
            continue
        data = value if container == "data" else value["data"]
        bindings = data.get("institutional_bindings", [])
        retained = [binding for binding in bindings if binding["identity_id"] not in identity_ids]
        if retained != bindings:
            data = {**data, "institutional_bindings": retained}
            setattr(row, container, data if container == "data" else {**value, "data": data})
            changed = True
    if changed:
        row.revision += 1
        db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                           action="auth.config.anonymise", target_id=SINGLETON, reason=reason,
                           detail={"revision": row.revision, "changed_fields": ["institutional_bindings"],
                                   "client_secret_changed": False},
                           correlation_id=db.info.get("correlation_id") or "auth-configuration"))


def effective(db, settings, *, secret=False):
    row = db.get(m.AuthConfiguration, SINGLETON)
    config = current(db, settings)
    client_secret = ""
    if secret and row and row.secret_ciphertext:
        try:
            client_secret = decrypt_secret(row.secret_ciphertext, SECRET_CONTEXT, settings)
        except RuntimeError:
            raise Problem(409, "AUTH_KEY_UNAVAILABLE", "Restore the authentication data key or replace the client secret") from None
    values = dict(vars(settings)) | {
        "oidc_issuer": config.issuer, "oidc_discovery_url": config.discovery_url,
        "oidc_client_id": config.client_id, "oidc_client_secret": client_secret,
        "oidc_redirect_uri": settings.public_origin + "/auth/callback", "oidc_scopes": config.scopes,
        "oidc_username_claim": config.username_claim, "oidc_email_claim": config.email_claim,
        "oidc_groups_claim": config.groups_claim, "oidc_allowed_domains": config.allowed_domains,
        "oidc_allowed_hosts": ",".join(config.allowed_hosts),
        "institutional_bindings": [v.model_dump() for v in config.institutional_bindings],
        "auth_configuration_revision": row.revision if row else 0,
        "auth_mode": config.mode, "institutional_enabled": config.institutional_enabled,
    }
    return SimpleNamespace(**values)


def require_institutional(settings):
    require(settings.institutional_enabled and settings.auth_mode != "password_only", "OIDC_UNCONFIGURED",
            "Institutional sign-in is not available", 404)


def _revision(row, revision):
    require(revision == (row.revision if row else 0), "STALE_REVISION",
            "Authentication settings changed; reload before continuing", 409)


def change(db, actor, value, settings, *, action="update"):
    # Any department admin may change shared settings; binding edits still require identity authority.
    require_role(actor, "admin")
    row = db.scalar(select(m.AuthConfiguration).where(m.AuthConfiguration.id == SINGLETON).with_for_update())
    _revision(row, value.expected_revision)
    previous = {"data": row.data if row else s.AuthSettings().model_dump(mode="json"),
                "secret_ciphertext": row.secret_ciphertext if row else None}
    if action == "undo":
        require(row is not None and row.previous is not None, "NOTHING_TO_UNDO", "There is no authentication change to undo", 409)
        data, encrypted = s.AuthSettings.model_validate(row.previous["data"]), row.previous["secret_ciphertext"]
    elif action == "reset":
        data, encrypted = s.AuthSettings(), None
    else:
        data = s.AuthSettings.model_validate(value.model_dump(include=set(s.AuthSettings.model_fields)))
        encrypted = previous["secret_ciphertext"]
        if value.client_secret is not None:
            try:
                encrypted = encrypt_secret(value.client_secret, SECRET_CONTEXT, settings) if value.client_secret else None
            except RuntimeError:
                raise Problem(409, "AUTH_KEY_UNAVAILABLE", "Configure the deployment FMUC_DATA_KEY before storing a client secret") from None
    require_break_glass(db, settings)
    if data.institutional_enabled:
        if encrypted:
            try:
                decrypt_secret(encrypted, SECRET_CONTEXT, settings)
            except RuntimeError:
                raise Problem(409, "AUTH_KEY_UNAVAILABLE", "Restore the authentication data key or replace the client secret") from None
    # Explicit, existing sign-ins only; claims never create accounts or assign roles.
    from .access import require_sign_in
    from .accounts import managed
    from .independence import today
    prior_bindings = {item["identity_id"]: s.AuthInstitutionalBinding.model_validate(item)
                      for item in previous["data"].get("institutional_bindings", [])}
    bindings = {binding.identity_id: binding for binding in data.institutional_bindings}
    bound_subjects = set()
    for binding in data.institutional_bindings:
        if not binding.issuer and action == "update":
            prior = prior_bindings.get(binding.identity_id)
            binding.issuer = prior.issuer if prior is not None and prior.subject == binding.subject else data.issuer
        require(action == "undo" and not binding.issuer or bool(binding.issuer) and binding.issuer == data.issuer,
                "AUTH_BINDING_ISSUER",
                "Confirm institutional links for this issuer; remove old links before switching providers", 422)
        require(binding.subject not in bound_subjects, "AUTH_BINDING_INVALID",
                "Each institutional subject may be bound only once", 422)
        bound_subjects.add(binding.subject)
        identity = db.get(m.Identity, binding.identity_id)
        require(identity is not None, "AUTH_BINDING_INVALID", "Institutional bindings must name an existing sign-in", 422)
        require_sign_in(identity)
    # Provider trust affects every retained link; only remember-me is independent of identity trust.
    trust_fields = set(s.AuthSettings.model_fields) - {"remember_me_days", "institutional_bindings"}
    trust_changed = (data.model_dump(include=trust_fields)
                     != s.AuthSettings.model_validate(previous["data"]).model_dump(include=trust_fields)
                     or encrypted != previous["secret_ciphertext"])
    for identity_id in sorted(prior_bindings.keys() | bindings.keys()):
        if prior_bindings.get(identity_id) == bindings.get(identity_id) and not trust_changed:
            continue
        identity = db.get(m.Identity, identity_id)
        require(identity is not None, "AUTH_BINDING_INVALID",
                "Institutional bindings must name an existing sign-in in this department", 422)
        managed(db, actor, identity.id, need_account=False)
        # Undo restores the exact snapshot; restoring a link never restores its owner's access.
        if action != "undo" and identity_id in bindings:
            require(current_roles(db, identity_id, actor.department_id), "AUTH_BINDING_INVALID",
                    "Institutional bindings must name an existing sign-in in this department", 422)
        protected = db.scalar(select(m.RoleBinding.id).where(m.RoleBinding.identity_id == identity.id,
                              m.RoleBinding.role.in_(s.EXPLICIT_ROLES), m.RoleBinding.revoked.is_(False),
                              (m.RoleBinding.ends_on.is_(None) | (m.RoleBinding.ends_on > today(db, actor.department_id)))).limit(1))
        if protected:
            require_permission(actor, "access.system_roles")
    if row is None:
        row = m.AuthConfiguration(id=SINGLETON, revision=1)
        db.add(row)
    else:
        row.revision += 1
    row.data, row.secret_ciphertext, row.previous = data.model_dump(mode="json"), encrypted, previous
    db.add(m.AuditEvent(department_id=actor.department_id, actor_id=actor.identity_id,
                       action="auth.config." + action, target_id=SINGLETON, reason=value.reason,
                       detail={"revision": row.revision, "mode": data.mode,
                               "changed_fields": sorted(k for k in row.data if previous["data"].get(k) != row.data[k]),
                               "client_secret_changed": previous["secret_ciphertext"] != encrypted},
                       correlation_id=db.info.get("correlation_id") or "auth-configuration"))
    db.flush()
    return status(db, actor, settings)


def prepare_test(db, actor, value, settings):
    require_role(actor, "admin")
    row = db.get(m.AuthConfiguration, SINGLETON)
    _revision(row, value.expected_revision)
    configured = effective(db, settings, secret=True)
    require(bool(configured.oidc_issuer and configured.oidc_client_id), "OIDC_UNCONFIGURED",
            "Save an issuer and client ID before testing the connection", 409)
    return configured


def test_connection(settings):
    """Validate discovery/keys without creating an authorization flow or using a user's account."""
    from .. import oidc
    try:
        document = oidc.document(settings)
        keys = oidc.signing_keys(settings, document["jwks_uri"])
        require(bool(keys.get("keys")), "OIDC_SIGNING_KEY", "Provider has no signing keys", 503)
        ok, message = True, "Discovery, S256 PKCE and signing keys verified; complete a test sign-in to verify client registration"
    except (Problem, ValueError, TypeError, RuntimeError):
        ok, message = False, "Unable to verify provider discovery or signing keys"
    except Exception:
        # HTTP/library failures may include URLs or tokens; return only a fixed message.
        ok, message = False, "Unable to verify provider discovery or signing keys"
    return {"ok": ok, "revision": settings.auth_configuration_revision, "message": message}
