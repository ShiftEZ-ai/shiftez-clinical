"""Department-scoped linked sign-ins and their sessions, for administrators and the person."""
from sqlalchemy import func, or_, select
from apps.api import models as m
from apps.api.auth import (SESSION_EVENT_REASONS, department_roles, revoke_session, session_active,
                           session_deadline, session_event, session_limits)
from apps.api.errors import require
from apps.api.serialization import json_value, record
from packages.contracts import schemas as s
from packages.domain.time import aware
from .access import SWAP_AUTOMATION_ISSUER
from .common import expected
from .person_record import require_person


def linked_identities(actor, person_id):
    bindings = select(m.RoleBinding.id).where(m.RoleBinding.identity_id == m.Identity.id,
                                             m.RoleBinding.department_id == actor.department_id).exists()
    sessions = select(m.Session.id).where(m.Session.identity_id == m.Identity.id,
                                         m.Session.department_id == actor.department_id).exists()
    return (m.Identity.person_id == person_id, m.Identity.issuer != SWAP_AUTOMATION_ISSUER, or_(bindings, sessions))


def accounts(db, actor, person_id):
    require_person(db, actor, person_id, readers=("admin",))
    last_sign_in = select(m.Session.identity_id, func.max(m.Session.created_at).label("last_sign_in_at")).join(m.Identity).where(
        m.Session.department_id == actor.department_id, m.Identity.person_id == person_id).group_by(m.Session.identity_id).subquery()
    rows = db.execute(select(m.Identity, m.Account.username, m.Account.email, m.Account.accepted_at,
                             m.Account.password_changed_at, last_sign_in.c.last_sign_in_at)
                      .outerjoin(m.Account, m.Account.identity_id == m.Identity.id)
                      .outerjoin(last_sign_in, last_sign_in.c.identity_id == m.Identity.id)
                      .where(*linked_identities(actor, person_id))
                      .order_by(m.Identity.display_name, m.Identity.id))
    roles = department_roles(db, actor.department_id, effective=False)
    return json_value({"person_id": person_id, "accounts": [
        {"identity_id": identity.id, "display_name": identity.display_name, "username": username,
         "email": email, "disabled": identity.disabled, "roles": sorted(roles.get(identity.id, ())),
         "accepted_at": accepted, "password_changed_at": password_changed, "last_sign_in_at": signed_in}
        for identity, username, email, accepted, password_changed, signed_in in rows]})


def session_view(session, actor, limits):
    active = session_active(session, m.now(), limits)
    return json_value({"id": session.id, "identity_id": session.identity_id, "revision": session.revision,
                       "created_at": session.created_at, "last_active_at": session.last_active_at,
                       "expires_at": session_deadline(session, limits),
                       "revoked": session.revoked, "active": active,
                       "current": session.id == actor.session_id, "device_label": session.device_label,
                       "network_label": session.network_label, "can_revoke": active})


def sessions(db, actor, person_id, limit=100, offset=0, *, settings):
    require_person(db, actor, person_id, readers=("admin",))
    limits = session_limits(db.get(m.Department, actor.department_id), settings)
    where = (m.Session.department_id == actor.department_id, m.Identity.person_id == person_id)
    total = db.scalar(select(func.count()).select_from(m.Session).join(m.Identity).where(*where))
    rows = db.scalars(select(m.Session).join(m.Identity).where(*where)
                      .order_by(m.Session.created_at.desc(), m.Session.seq.desc()).offset(offset).limit(limit))
    return {"person_id": person_id, "sessions": [session_view(row, actor, limits) for row in rows],
            "total": total, "limit": limit, "offset": offset}


def activity_view(event):
    """The person's auth feed never returns arbitrary command details or private reasons."""
    return {**record(event),
            "reason": SESSION_EVENT_REASONS[event.action],
            "detail": {key: event.detail.get(key) for key in
                       ("person_id", "identity_id", "session_id", "device_label", "network_label")}
                      | ({"method": event.detail["method"]} if event.detail.get("method") in {"password", "oidc"} else {})}


def auth_activity(db, actor, person_id, limit=100, offset=0):
    require_person(db, actor, person_id, readers=("admin",))
    where = (m.AuditEvent.department_id == actor.department_id,
             m.AuditEvent.action.in_(SESSION_EVENT_REASONS),
             m.AuditEvent.detail["person_id"].as_string() == person_id)
    total = db.scalar(select(func.count()).select_from(m.AuditEvent).where(*where))
    events = [activity_view(row) for row in db.scalars(select(m.AuditEvent).where(*where)
              .order_by(m.AuditEvent.created_at.desc(), m.AuditEvent.seq.desc()).offset(offset).limit(limit))]
    return {"person_id": person_id, "events": events, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(events) < total}


def session_access(db, actor, session_id, *, lock=False):
    query = select(m.Session, m.Identity.person_id).join(m.Identity).where(
        m.Session.id == session_id, m.Session.department_id == actor.department_id)
    if lock:
        query = query.with_for_update(of=m.Session)
    row = db.execute(query).first()
    require(row is not None and row.person_id is not None, "NOT_FOUND", "Session not found", 404)
    require_person(db, actor, row.person_id, readers=("admin",))
    return row.Session, row.person_id


def revoke(db, actor, command, settings):
    value = s.Decision.model_validate(command.payload)
    session, person_id = session_access(db, actor, command.target_id, lock=True)
    expected(session, command.expected_revision)
    limits = session_limits(db.get(m.Department, actor.department_id), settings)
    require(session_active(session, m.now(), limits),
            "SESSION_INACTIVE", "This session has already ended", 409)
    revoke_session(session)
    session_event(db, session, person_id, "auth.session_revoke", actor_id=actor.identity_id)
    # Keep the existing safe revision history; never serialize a Session row.
    db.add(m.DefinitionHistory(department_id=actor.department_id, kind='session', object_id=session.id,
                              object_revision=session.revision, actor_id=actor.identity_id, reason=value.reason,
                              snapshot={'id': session.id, 'person_id': person_id, 'revoked': True}))
    return {"person_id": person_id, "session": session_view(session, actor, limits)}
