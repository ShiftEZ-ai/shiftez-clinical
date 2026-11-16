"""Microsoft delegated Mail.Send consent, bound to an existing Rota session and editor."""
import base64
from datetime import timedelta
import hashlib
import json
import secrets
from urllib.parse import quote, urlencode, urlsplit

import httpx
from sqlalchemy import delete, select

from apps.api import models as m
from apps.api.auth import current_roles, digest, session_active
from apps.api.data_crypto import decrypt_secret, encrypt_secret
from apps.api.errors import Problem, require
from apps.api.settings import secret_from_env
from packages.contracts import schemas as s
from packages.domain.time import aware


def _eligible(db, identity_id, department_id):
    identity = db.get(m.Identity, identity_id)
    return bool(identity and not identity.disabled and
                current_roles(db, identity_id, department_id).intersection({"admin", "coordinator"}))


def _actor(db, actor):
    require(_eligible(db, actor.identity_id, actor.department_id), "FORBIDDEN",
            "An active editor role is required to connect a sending mailbox", 403)
    session = db.get(m.Session, actor.session_id)
    require(session is not None and session_active(session)
            and session.identity_id == actor.identity_id and session.department_id == actor.department_id,
            "SESSION_EXPIRED", "Sign in again before connecting your mailbox", 401)


def _configuration(db, department_id, settings):
    department = db.get(m.Department, department_id)
    config = s.EmailSettings.model_validate(department.email_settings)
    require(config.m365_enabled and config.m365_tenant_id and config.m365_client_id,
            "MAILBOX_UNCONFIGURED", "Microsoft sending is disabled or is not configured", 422)
    require(config.m365_tenant_id.lower() not in {"common", "organizations", "consumers"},
            "MAILBOX_UNCONFIGURED", "Configure the department's Microsoft tenant", 422)
    redirect = settings.public_origin.rstrip("/") + "/api/email/microsoft/callback"
    signature = digest(json.dumps([config.m365_tenant_id, config.m365_client_id,
        config.m365_authority_url, config.m365_graph_url, redirect]))
    return config, redirect, signature


def _context(kind, identity_id, department_id):
    return f"fmuc:{kind}:{department_id}:{identity_id}"


def _unavailable():
    return Problem(422, "MAILBOX_UNAVAILABLE",
        "Microsoft mailbox sending is unavailable. Reconnect your mailbox or download the email draft.",
        {"draft_available": True})


def _client_secret():
    try:
        value = secret_from_env("FMUC_M365_CLIENT_SECRET")
    except RuntimeError:
        raise _unavailable() from None
    if not value:
        raise _unavailable()
    return value


def _scopes(config):
    endpoint = urlsplit(config.m365_graph_url)
    return f"offline_access {endpoint.scheme}://{endpoint.netloc}/Mail.Send"


def _endpoint(config, action):
    return config.m365_authority_url.rstrip("/") + "/" + quote(config.m365_tenant_id, safe="") + "/oauth2/v2.0/" + action


def _tokens(config, values):
    # Provider bodies can contain tokens and identifying information; none reach errors or logs.
    try:
        response = httpx.post(_endpoint(config, "token"), data={"client_id": config.m365_client_id,
            "client_secret": _client_secret(), "scope": _scopes(config), **values},
            timeout=config.timeout_seconds, follow_redirects=False, trust_env=False)
        if response.status_code != 200:
            raise _unavailable()
        tokens = response.json()
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise _unavailable()
        return tokens
    except (httpx.HTTPError, ValueError, TypeError):
        raise _unavailable() from None


def start(db, actor, settings):
    _actor(db, actor)
    config, redirect, signature = _configuration(db, actor.department_id, settings)
    _client_secret()
    prune(db)
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    context = _context("oauth-verifier", actor.identity_id, actor.department_id)
    try:
        encrypted = encrypt_secret(verifier, context, settings)
    except RuntimeError:
        raise _unavailable() from None
    db.execute(delete(m.MailboxOAuthFlow).where(m.MailboxOAuthFlow.identity_id == actor.identity_id,
        m.MailboxOAuthFlow.department_id == actor.department_id))
    db.add(m.MailboxOAuthFlow(identity_id=actor.identity_id, department_id=actor.department_id,
        session_id=actor.session_id, state_hash=digest(state), encrypted_verifier=encrypted,
        configuration_hash=signature, expires_at=m.now() + timedelta(minutes=config.oauth_flow_minutes)))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "response_mode": "query", "client_id": config.m365_client_id,
        "redirect_uri": redirect, "scope": _scopes(config), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
    account = db.scalar(select(m.Account).where(m.Account.identity_id == actor.identity_id))
    if account is not None:
        params["login_hint"] = account.email
    return {"authorization_url": _endpoint(config, "authorize") + "?" + urlencode(params)}


def callback(db, actor, settings, state, code):
    _actor(db, actor)
    config, redirect, signature = _configuration(db, actor.department_id, settings)
    flow = db.scalar(select(m.MailboxOAuthFlow).where(m.MailboxOAuthFlow.state_hash == digest(state)).with_for_update())
    require(flow is not None and not flow.used and aware(flow.expires_at) > m.now()
        and flow.identity_id == actor.identity_id and flow.department_id == actor.department_id
        and flow.session_id == actor.session_id and flow.configuration_hash == signature,
        "MAILBOX_STATE", "Mailbox sign-in expired or belongs to a different session; connect again", 401)
    flow.used = True
    encrypted_verifier, flow.encrypted_verifier = flow.encrypted_verifier, ""
    if not isinstance(code, str) or not 0 < len(code) <= 16384:
        raise _unavailable()
    try:
        verifier = decrypt_secret(encrypted_verifier,
            _context("oauth-verifier", actor.identity_id, actor.department_id), settings)
    except RuntimeError:
        raise _unavailable() from None
    tokens = _tokens(config, {"grant_type": "authorization_code", "code": code,
        "code_verifier": verifier, "redirect_uri": redirect})
    refresh = tokens.get("refresh_token")
    require(isinstance(refresh, str) and bool(refresh), "MAILBOX_UNAVAILABLE",
        "Microsoft did not grant offline sending. Connect again with offline access or download the email draft.",
        details={"draft_available": True})
    try:
        encrypted = encrypt_secret(refresh, _context("mailbox", actor.identity_id, actor.department_id), settings)
    except RuntimeError:
        raise _unavailable() from None
    connection = _connection(db, actor.identity_id, actor.department_id)
    if connection is None:
        connection = m.MailboxConnection(identity_id=actor.identity_id, department_id=actor.department_id)
        db.add(connection)
    connection.encrypted_refresh_token, connection.configuration_hash = encrypted, signature
    connection.updated_at = m.now()
    return {"connected": True}


def _connection(db, identity_id, department_id):
    return db.scalar(select(m.MailboxConnection).where(m.MailboxConnection.identity_id == identity_id,
        m.MailboxConnection.department_id == department_id).with_for_update())


def connected(db, identity_id, department_id, settings=None):
    if not _eligible(db, identity_id, department_id):
        return False
    connection = _connection(db, identity_id, department_id)
    if connection is None:
        return False
    if settings is not None:
        try:
            _, _, signature = _configuration(db, department_id, settings)
        except Problem:
            return False
        return connection.configuration_hash == signature
    return True


def status(db, actor, settings=None):
    return {"connected": connected(db, actor.identity_id, actor.department_id, settings)}


def disconnect(db, actor):
    db.execute(delete(m.MailboxConnection).where(m.MailboxConnection.identity_id == actor.identity_id,
        m.MailboxConnection.department_id == actor.department_id))
    db.execute(delete(m.MailboxOAuthFlow).where(m.MailboxOAuthFlow.identity_id == actor.identity_id,
        m.MailboxOAuthFlow.department_id == actor.department_id))
    return {"connected": False}


def access_token(db, identity_id, department_id, settings):
    if not _eligible(db, identity_id, department_id):
        prune(db)
        raise _unavailable()
    config, _, signature = _configuration(db, department_id, settings)
    connection = _connection(db, identity_id, department_id)
    if connection is None or connection.configuration_hash != signature:
        raise _unavailable()
    try:
        refresh = decrypt_secret(connection.encrypted_refresh_token, _context("mailbox", identity_id, department_id), settings)
    except RuntimeError:
        raise _unavailable() from None
    tokens = _tokens(config, {"grant_type": "refresh_token", "refresh_token": refresh})
    rotated = tokens.get("refresh_token")
    if rotated is not None:
        if not isinstance(rotated, str) or not rotated:
            raise _unavailable()
        try:
            connection.encrypted_refresh_token = encrypt_secret(rotated, _context("mailbox", identity_id, department_id), settings)
        except RuntimeError:
            raise _unavailable() from None
    connection.updated_at = m.now()
    return tokens["access_token"]


def prune(db):
    """End local consent as soon as an identity loses its department editing access."""
    removed = 0
    for connection in db.scalars(select(m.MailboxConnection)):
        if not _eligible(db, connection.identity_id, connection.department_id):
            db.delete(connection)
            removed += 1
    for flow in db.scalars(select(m.MailboxOAuthFlow)):
        session = db.get(m.Session, flow.session_id)
        if (flow.used or aware(flow.expires_at) <= m.now() or session is None or not session_active(session)
            or not _eligible(db, flow.identity_id, flow.department_id)):
            db.delete(flow)
    return removed
