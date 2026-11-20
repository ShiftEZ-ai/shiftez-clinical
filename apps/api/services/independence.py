"""Independent decisions: which actions normally need a second person, and the permissions that waive that.

Every "you cannot decide your own work" check in the service goes through `ensure`, so the rule, the
exceptions to it and the trail they leave live here and nowhere else.
"""
from zoneinfo import ZoneInfo
from sqlalchemy import select
from packages.contracts import schemas as s
from apps.api import models as m
from apps.api.auth import current_roles, department_roles, effective_roles, held_roles, role_bindings
from apps.api.db import current_request_parent
from apps.api.errors import require
from apps.api.serialization import record
from .common import scoped, expected, history, history_version, outbox

LABELS = {
    "rota.approve": "Approve a rota they built or changed",
    "rule.activate": "Activate a rule they proposed",
    "exception.approve": "Approve a rule exception they asked for",
    "request.decide": "Decide their own leave or availability request",
    "route.decide": "Activate or retire a review route they proposed",
    "skills.self": "Record their own clinical skills",
}
assert set(LABELS) == set(s.INDEPENDENT_ACTIONS), "Every grantable action needs a label"
SELF_GRANT_ACTION = "self_approval.grant"
SELF_GRANT_LABEL = "Grant self-approval permissions covering themselves"
# Reasons the screens send when a person leaves the note empty; deciding your own work needs a real one.
ROUTINE_REASONS = {"Routine update", "Routine rota check"}


def person_key(db, identity_id):
    """Two sign-ins for the same person count as one person."""
    identity = db.get(m.Identity, identity_id)
    return "person:" + identity.person_id if identity and identity.person_id else "identity:" + identity_id


def today(db, department_id):
    department = db.get(m.Department, department_id)
    organisation = current_request_parent(db, m.Organisation, department.organisation_id)
    return m.now().astimezone(ZoneInfo(organisation.timezone)).date()


def valid_period(permission):
    return permission.ends_on is None or 0 < (permission.ends_on - permission.starts_on).days <= s.SELF_APPROVAL_MAX_DAYS


def permission_state(permission, day):
    if permission.revoked:
        return "ended"
    if not valid_period(permission):
        return "needs_renewal"
    if permission.starts_on > day:
        return "upcoming"
    return "active" if permission.ends_on is None or day < permission.ends_on else "ended"


def active_permissions(db, department_id, day=None):
    day = day or today(db, department_id)
    query = select(m.SelfApprovalPermission).where(
        m.SelfApprovalPermission.department_id == department_id, m.SelfApprovalPermission.revoked.is_(False),
        m.SelfApprovalPermission.starts_on <= day)
    memo = db.info.get('_request_authority')
    key = (m.SelfApprovalPermission, department_id, day)
    if memo is None or not db.autoflush or db.new or db.dirty or db.deleted:
        rows = db.scalars(query)
    else:
        if key not in memo:
            memo[key] = tuple(db.scalars(query))
        rows = memo[key]
    return [p for p in rows if permission_state(p, day) == "active"]


def covers(permission, identity_id, held_roles):
    return permission.identity_id == identity_id or permission.role in held_roles


def performable(roles):
    """The actions whose own work someone with these effective roles can do (s.INDEPENDENT_ACTION_ROLES)."""
    return {a for a, steps in s.INDEPENDENT_ACTION_ROLES.items() if all(set(roles) & set(step) for step in steps)}


def department_holders(db, department_id):
    """Everyone with current access: identity -> (held roles, the effective roles of every login of that person,
    since one person may propose with one sign-in and decide with another); one bindings and one identity read."""
    held = department_roles(db, department_id, effective=False, bindings=role_bindings(db, department_id))
    rows = db.execute(select(m.Identity.id, m.Identity.person_id).where(
        m.Identity.id.in_(held or {""}), m.Identity.disabled.is_(False))).all()
    key = {i: "person:" + person if person else "identity:" + i for i, person in rows}
    person_roles = {}
    for i, k in key.items():
        person_roles.setdefault(k, set()).update(held[i])
    return {i: (held[i], effective_roles(person_roles[k])) for i, k in key.items()}


def usable_actions(permission, holders):
    """SW3-B-18: the permission's actions someone it covers can actually take (a role grant also counts the role
    itself, so a role nobody holds yet can be granted what that role does)."""
    usable = set().union(*(performable(effective) for i, (held, effective) in holders.items()
                           if covers(permission, i, held)))
    if permission.role:
        usable |= performable(effective_roles({permission.role}))
    return sorted(usable & set(permission.actions))


def require_usable(db, actor, permission):
    unusable = sorted(set(permission.actions) - set(usable_actions(permission, department_holders(db, actor.department_id))))
    require(not unusable, "SELF_APPROVAL_ACTION_UNUSABLE",
            "Their roles cannot do this work themselves, so the permission would never be used: "
            + "; ".join(LABELS[a].lower() for a in unusable), 422, {"actions": unusable})


def covers_actor_person(db, actor, permission):
    person = person_key(db, actor.identity_id)
    return any(covers(permission, identity_id, roles)
               for identity_id, roles in department_roles(db, actor.department_id, effective=False).items()
               if not db.get(m.Identity, identity_id).disabled and person_key(db, identity_id) == person)


def permission_for(db, actor, action, *, context=None):
    held = held_roles(db, actor.identity_id, actor.department_id) if context is None else context['held']
    permissions = active_permissions(db, actor.department_id) if context is None else context['permissions']
    for p in permissions:
        if action in p.actions and covers(p, actor.identity_id, held):
            return p
    return None


def my_actions(db, actor, *, context=None):
    """The actions this person may currently decide for themselves (the screens use it to offer the button)."""
    held = held_roles(db, actor.identity_id, actor.department_id) if context is None else context['held']
    permissions = active_permissions(db, actor.department_id) if context is None else context['permissions']
    mine = {a for p in permissions if covers(p, actor.identity_id, held) for a in p.actions}
    if not mine:
        return []
    # A saved grant for work this person can never do is not offered as theirs (SW3-B-18); their other logins count.
    logins = [actor.identity_id] if not actor.person_id else db.scalars(select(m.Identity.id).where(
        m.Identity.person_id == actor.person_id, m.Identity.disabled.is_(False)))
    roles = set(actor.roles).union(*(current_roles(db, i, actor.department_id) for i in logins))
    return sorted(mine & performable(roles))


def can_decide(db, actor, action, independent, *, context=None):
    """Read-side independence only; callers still enforce roles, route seats and current inputs."""
    return independent or permission_for(db, actor, action, context=context) is not None


def ensure(db, actor, action, independent, message, reason=None, target_id=None, *, context=None,
           allow_self_approval=True):
    """Pass when someone else decides. Own decisions need permission (self-grants need no other admin),
    a real reason, a mark in the decision history and an announcement to approvers and auditors."""
    if independent:
        return None
    require(allow_self_approval, "SELF_APPROVAL", message, 403)
    if action == SELF_GRANT_ACTION:
        permission = None  # Bootstrap has no authorizing permission; the new grant cannot authorize itself.
        require(not any(person_key(db, identity_id) != person_key(db, actor.identity_id) and "admin" in roles
                        and not db.get(m.Identity, identity_id).disabled
                        for identity_id, roles in department_roles(db, actor.department_id, effective=False).items()),
                "SELF_APPROVAL", message, 403)
    else:
        permission = permission_for(db, actor, action, context=context)
        require(permission is not None, "SELF_APPROVAL", message, 403)
    text = (reason or "").strip()
    require(len(text) >= 3 and text not in ROUTINE_REASONS, "SELF_APPROVAL_REASON",
            "You are deciding your own work: add a reason", 422)
    db.info.setdefault("self_approved", []).append({"action": action, "permission_id": permission.id if permission else None})
    # The reason stays in the decision history only: it can be private (a leave request, for example).
    outbox(db, actor, f"self_approval:{action}:{target_id or ''}:{m.uid()}", "self_approval",
           {"action": action, "target_id": target_id})
    return permission


def permission_command(db, actor, command):
    if command.kind == "self_approval.grant":
        value = s.SelfApprovalInput.model_validate(command.payload)
        if value.identity_id:
            identity = db.get(m.Identity, value.identity_id)
            require(identity is not None and not identity.disabled and bool(current_roles(db, value.identity_id, actor.department_id)),
                    "NOT_FOUND", "That person has no current access to this department", 404)
        row = m.SelfApprovalPermission(department_id=actor.department_id, created_by=actor.identity_id,
                                       **value.model_dump(exclude={"reason", "no_end_date"}), reason=value.reason)
        require_usable(db, actor, row)
        db.add(row)
        db.flush()
        ensure(db, actor, SELF_GRANT_ACTION,
               not covers_actor_person(db, actor, value),
               "Ask another admin to grant this", value.reason, row.id)
        history(db, actor, "self_approval", row, value.reason)
        return {"permission": record(row)}
    restoring = command.kind == "self_approval.restore"
    value = (s.AuthorityRestoreInput if restoring else s.Decision).model_validate(command.payload)
    row = scoped(db, m.SelfApprovalPermission, command.target_id, actor)
    expected(row, command.expected_revision)
    if restoring:
        require(row.revoked, "WORKFLOW_STATE", "Only an ended permission can be restored", 409)
        previous = history_version(db, actor, "self_approval", row.id)
        require(previous is None or not previous.snapshot.get("restored_to_id"),
                "WORKFLOW_STATE", "This permission was already restored; reload to use its replacement", 409)
        require(valid_period(row),
                "SELF_APPROVAL_PERIOD", "Grant a new permission with an end date within one year", 422)
        require(row.ends_on is None or today(db, actor.department_id) < row.ends_on,
                "PERMISSION_EXPIRED", "This permission has expired; ask an admin to grant a new permission", 409)
        if row.identity_id:
            identity = db.get(m.Identity, row.identity_id)
            require(identity is not None and not identity.disabled and bool(current_roles(db, row.identity_id, actor.department_id)),
                    "NOT_FOUND", "Restore this person's departmental access before retrying", 404)
        require_usable(db, actor, row)
        ensure(db, actor, SELF_GRANT_ACTION,
               not covers_actor_person(db, actor, row),
               "Ask another admin to restore this", value.reason, row.id)
        source = row
        source.revision += 1
        row = m.SelfApprovalPermission(**{key: getattr(source, key) for key in (
            "department_id", "created_by", "identity_id", "role", "actions", "starts_on", "ends_on", "reason")})
        db.add(row)
        db.flush()
        history(db, actor, "self_approval", source, value.reason, snapshot_extra={"restored_to_id": row.id})
        history(db, actor, "self_approval", row, value.reason,
                snapshot_extra={"restored_from_id": source.id, "restored_from_revision": command.expected_revision})
        return {"permission": record(row)}
    require(not row.revoked, "WORKFLOW_STATE", "This permission has already ended", 409)
    row.revoked = True
    row.revision += 1
    history(db, actor, "self_approval", row, value.reason)
    return {"permission": record(row)}
