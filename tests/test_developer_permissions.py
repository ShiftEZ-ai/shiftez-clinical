"""Developer is an explicit binding; capability decisions share the contract map."""
from datetime import timedelta
import json
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.auth import Actor, _effective, require_permission
from apps.api.db import transaction
from apps.api.errors import Problem
from apps.api.services import feedback
from apps.api.services.independence import today
from packages.contracts import schemas as s
from scripts.generate_contracts import render
from .governance_support import developer, identity
from .test_accounts import PASSWORD, accepted, anonymous, invite, token_of, with_users_file
from .test_feedback_api import screenshot, submit


def test_permission_contract_and_generated_values_are_identical():
    assert s.SYSTEM_ROLES == ("operator",)
    assert set(s.ROLE_LABELS) == set(s.ROLE_NAMES)
    assert s.ROLE_LABELS["operator"] == "Developer"
    system_permissions = {
        "schedule.read_team": ("admin", "coordinator", "approver", "programme_lead", "auditor"),
        "catalogue.read": ("admin", "coordinator", "approver", "credentialer", "auditor", "programme_lead"),
        "publications.read_team": ("coordinator", "approver", "auditor"),
        "access.system_roles": ("operator",),
        "feedback.read_all": ("operator", "auditor"),
        "feedback.triage": ("operator",),
        "feedback.export_github": ("operator",),
        "integrations.github": ("operator",),
        "audit.read": ("operator", "auditor"),
        "system.operations": ("operator", "auditor"),
    }
    assert {name: s.PERMISSIONS[name] for name in system_permissions} == system_permissions
    assert {*s.BULK_ITEM_KINDS, "bulk.apply", "bulk.stage", "bulk.staged.drop", "department.bulk_settings"} <= s.PERMISSIONS.keys()
    source, _ = render()
    assert Path("apps/web/src/generated.ts").read_text() == source
    for name in ("PERMISSIONS", "ROLE_LABELS", "SYSTEM_ROLES", "EXPLICIT_ROLES"):
        value = json.loads(re.search(rf"export const {name} = (.+) as const;", source)[1])
        expected = getattr(s, name)
        assert value == json.loads(json.dumps(sorted(expected) if isinstance(expected, frozenset) else expected))
    assert all(set(roles) <= set(s.ROLE_NAMES) for roles in s.PERMISSIONS.values())


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_admin_expansion_preserves_department_roles_and_explicit_system_bindings(role):
    expected = frozenset(s.ROLE_NAMES) - s.EXPLICIT_ROLES
    assert _effective(["admin"]) == expected
    assert _effective(["admin", role]) == expected | {role}
    assert _effective([role]) == (expected if role == "admin" else frozenset({role}))
    actor = Actor("identity", "department", "organisation", "Fictional", _effective([role]), None, "session")
    for permission, roles in s.PERMISSIONS.items():
        if actor.permits(*roles):
            require_permission(actor, permission)
        else:
            with pytest.raises(Problem, match="current role") as caught:
                require_permission(actor, permission)
            assert caught.value.status == 403


@pytest.mark.parametrize("actor", ["admin", "operator"])
@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
def test_explicit_role_grants_require_system_role_permission(h, actor, role):
    target = h.seed["identities"]["admin" if actor == "admin" else "staff"]
    result = h.command(actor, "role.grant", {"identity_id": target, "role": role, "starts_on": "2020-01-01",
                       "reason": "Synthetic system access grant"}, status=200 if actor == "operator" else 403)
    if actor == "admin":
        assert result["error"]["code"] == "SYSTEM_ROLE_GRANT"
        assert "Only a Developer" in result["error"]["message"]
    with transaction(h.app.state.engine) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target, m.RoleBinding.role == role))
        assert (binding is not None) == (actor == "operator")


@pytest.mark.parametrize("actor", ["admin", "operator"])
@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
def test_explicit_role_revocations_require_system_role_permission(h, actor, role):
    identity(h, "replacement", ["operator"])
    with transaction(h.app.state.engine) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"][role],
                                                       m.RoleBinding.role == role))
        binding_id, revision = binding.id, binding.revision
    result = h.command(actor, "role.revoke", {"reason": "Synthetic system access revocation"}, binding_id, revision,
                       status=200 if actor == "operator" else 403)
    if actor == "admin":
        assert result["error"]["code"] == "SYSTEM_ROLE_REVOKE"
        assert "Only a Developer" in result["error"]["message"]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RoleBinding, binding_id).revoked == (actor == "operator")


@pytest.mark.parametrize("state", ["live", "pending", "accepted", "temporary", "future", "expired", "revoked", "disabled", "other_department"])
@pytest.mark.parametrize("system_role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
def test_admin_cannot_bootstrap_explicit_roles_regardless_of_developer_cover(h, state, system_role):
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"][system_role]))
        date = today(db, binding.department_id)
        if state in {"pending", "accepted"}:
            db.add(m.Account(identity_id=binding.identity_id, username="managed.developer", email="developer@example.test",
                             accepted_at=m.now() if state == "accepted" else None))
        elif state == "temporary":
            binding.ends_on = date + timedelta(days=1)
        elif state == "future":
            binding.starts_on = date + timedelta(days=1)
        elif state == "expired":
            binding.ends_on = date
        elif state == "revoked":
            binding.revoked = True
        elif state == "disabled":
            db.get(m.Identity, binding.identity_id).disabled = True
        elif state == "other_department":
            department = m.Department(organisation_id=db.get(m.Department, binding.department_id).organisation_id,
                                      name="Other test department")
            db.add(department); db.flush()
            binding.department_id = department.id
    result = h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["admin"], "role": system_role,
                       "starts_on": date.isoformat(), "reason": "Bootstrap the department Developer"},
                       status=403)
    assert result["error"]["code"] == "SYSTEM_ROLE_GRANT"


def test_bootstrap_never_allows_admin_to_grant_auditor(h):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["operator"]).disabled = True
    result = h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["admin"], "role": "auditor",
                       "starts_on": "2020-01-01", "reason": "Attempt to bootstrap audit access"}, status=403)
    assert result["error"]["code"] == "SYSTEM_ROLE_GRANT"


def test_system_role_changes_use_current_permission_map(h, monkeypatch):
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ("coordinator",))
    payload = {"identity_id": h.seed["identities"]["staff"], "role": "auditor", "starts_on": "2020-01-01",
               "reason": "Exercise the shared permission contract"}
    row = h.command("coordinator", "role.grant", payload)["record"]
    h.command("operator", "role.grant", payload, status=403)
    h.command("operator", "role.revoke", {"reason": "Permission removed"}, row["id"], row["revision"], status=403)
    assert h.command("coordinator", "role.revoke", {"reason": "Permission granted"}, row["id"], row["revision"])["record"]["revoked"]


def test_developer_cannot_manage_ordinary_department_roles(h):
    access = h.get("operator", "/api/access")
    assert h.seed["identities"]["staff"] in {row["id"] for row in access["identities"]}
    assert any(row["role"] == "operator" for row in access["bindings"])
    payload = {"identity_id": h.seed["identities"]["staff"], "role": "admin", "starts_on": "2020-01-01",
               "reason": "Attempt to gain department administration"}
    h.command("operator", "role.grant", payload, status=403)
    row = h.command("admin", "role.grant", payload)["record"]
    h.command("operator", "role.revoke", {"reason": "Attempt to remove an admin"}, row["id"], row["revision"], status=403)


@pytest.mark.parametrize("role", ["staff", *sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE})])
@pytest.mark.parametrize("developer", [False, True])
def test_account_invitation_cannot_bypass_system_role_permission(h, tmp_path, role, developer):
    with_users_file(h, tmp_path)
    if developer:
        identity(h, "admin_developer", ["admin", "operator"])
    allowed = developer or role not in s.EXPLICIT_ROLES
    result = h.command("admin_developer" if developer else "admin", "account.invite",
                       {"display_name": "New person (test)", "email": "new.person@example.test",
                        "username": "new.person", "roles": ["staff", role], "reason": "Synthetic role invitation"}, status=200 if allowed else 403)
    with transaction(h.app.state.engine) as db:
        account = db.scalar(select(m.Account).where(m.Account.username == "new.person"))
        assert (account is not None) == allowed
        if allowed:
            assert {b.role for b in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == account.identity_id))} == {"staff", role}
        else:
            assert result["error"]["code"] == "SYSTEM_ROLE_GRANT"
            assert json.loads(h.users.read_text()) == {"users": {}}


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
def test_account_invitation_bootstrap_requires_system_role_permission(h, tmp_path, role):
    with_users_file(h, tmp_path)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["operator"]).disabled = True
    result = h.command("admin", "account.invite", {"display_name": "Bootstrap person (test)",
                       "email": "bootstrap@example.test", "username": "bootstrap", "roles": [role],
                       "reason": "Synthetic bootstrap role invitation"},
                       status=403)
    assert result["error"]["code"] == "SYSTEM_ROLE_GRANT"
    assert json.loads(h.users.read_text()) == {"users": {}}
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Account).where(m.Account.username == "bootstrap")) is None


def test_department_admin_cannot_invite_an_installation_admin(h, tmp_path):
    with_users_file(h, tmp_path)
    before = h.get("admin", "/api/access")
    refused = invite(h, roles=(s.INSTALLATION_ADMIN_ROLE,), status=403)
    assert refused["error"]["code"] == "FORBIDDEN"
    assert h.get("admin", "/api/access") == before
    assert json.loads(h.users.read_text()) == {"users": {}}


def test_refused_system_role_invite_keeps_target_role_and_error_in_audit(h, tmp_path):
    with_users_file(h, tmp_path)
    refused = h.command("admin", "account.invite", {
        "display_name": "Auditor target (test)", "email": "auditor.target@example.test",
        "username": "auditor.target", "roles": ["auditor"],
        "reason": "Synthetic refused auditor grant",
    }, status=403)
    assert refused["error"]["code"] == "SYSTEM_ROLE_GRANT"
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.action == "command.refused"
            ).order_by(m.AuditEvent.created_at.desc(), m.AuditEvent.id.desc()))
        assert event.detail["command_kind"] == "account.invite"
        assert event.detail["roles"] == ["auditor"]
        assert event.detail["error_code"] == "SYSTEM_ROLE_GRANT"
        assert event.detail["target_label"] == "Auditor target (test)"


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES))
@pytest.mark.parametrize("purpose", ["invite", "reset"])
@pytest.mark.parametrize("state", ["live", "temporary", "future", "expired", "revoked"])
@pytest.mark.usefixtures('branding_editor')
def test_account_recovery_protects_live_and_scheduled_system_roles(h, tmp_path, role, purpose, state):
    with_users_file(h, tmp_path)
    target = accepted(h) if purpose == "reset" else invite(h)["record"]["id"]
    with transaction(h.app.state.engine) as db:
        date = today(db, h.seed["department_id"])
    binding = h.command("operator", "role.grant", {"identity_id": target, "role": role,
        "starts_on": (date + timedelta(days=1) if state == "future" else date - timedelta(days=2)).isoformat(),
        "ends_on": (date if state == "expired" else date + timedelta(days=1)).isoformat()
                   if state in {"expired", "temporary"} else None, "reason": "Synthetic recovery protection"})["record"]
    if state == "revoked":
        h.command("operator", "role.revoke", {"reason": "End synthetic access"}, binding["id"], binding["revision"])
    command = "account.reset_link" if purpose == "reset" else "account.resend"
    protected = state in {"live", "temporary", "future"}
    result = h.command("admin", command, {"reason": "Recover synthetic account"}, target, status=403 if protected else 200)
    if protected:
        assert result["error"]["code"] == "FORBIDDEN" and "one_time_link" not in result
        with transaction(h.app.state.engine) as db:
            assert not list(db.scalars(select(m.AccountLink).where(m.AccountLink.identity_id == target,
                m.AccountLink.used_at.is_(None), m.AccountLink.revoked_at.is_(None))))
        result = h.command(developer(h), command, {"reason": "Authorized synthetic recovery"}, target)
    assert anonymous(h).get(f"/api/account-links/{token_of(result)}").status_code == 200


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES))
@pytest.mark.parametrize("purpose", ["invite", "reset"])
@pytest.mark.parametrize("scheduled", [False, True])
def test_system_role_elevation_invalidates_outstanding_account_links(h, tmp_path, role, purpose, scheduled):
    with_users_file(h, tmp_path)
    if purpose == "reset":
        target = accepted(h)
        link = h.command("admin", "account.reset_link", {"reason": "Synthetic recovery before elevation"}, target)
    else:
        link = invite(h)
        target = link["record"]["id"]
    public = anonymous(h)
    path = f"/api/account-links/{token_of(link)}"
    assert public.get(path).status_code == 200
    with transaction(h.app.state.engine) as db:
        date = today(db, h.seed["department_id"])
    h.command("operator", "role.grant", {"identity_id": target, "role": role,
        "starts_on": (date + timedelta(days=1) if scheduled else date).isoformat(), "reason": "Synthetic elevation"})
    assert public.get(path).status_code == 404
    assert public.post(path, json={"password": PASSWORD + " replacement"}).status_code == 404
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.AccountLink, link["link"]["id"])
        assert stored.revoked_at is not None and stored.used_at is None and stored.revision == 2
    renewed = h.command(developer(h), "account.reset_link" if purpose == "reset" else "account.resend",
                        {"reason": "Authorized recovery after elevation"}, target)
    assert public.post(f"/api/account-links/{token_of(renewed)}", json={"password": PASSWORD}).status_code == 200


@pytest.mark.parametrize("purpose", ["invite", "reset"])
def test_account_recovery_uses_current_system_role_permission_map(h, tmp_path, monkeypatch, purpose):
    with_users_file(h, tmp_path)
    target = accepted(h) if purpose == "reset" else invite(h)["record"]["id"]
    h.command("operator", "role.grant", {"identity_id": target, "role": "operator", "starts_on": "2020-01-01",
                                       "reason": "Synthetic protected account"})
    identity(h, "admin_auditor", ["admin", "auditor"])
    admin_developer = developer(h)
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ("auditor",))
    command = "account.reset_link" if purpose == "reset" else "account.resend"
    h.command(admin_developer, command, {"reason": "Permission removed"}, target, status=403)
    result = h.command("admin_auditor", command, {"reason": "Permission granted"}, target)
    assert anonymous(h).get(f"/api/account-links/{token_of(result)}").status_code == 200


@pytest.mark.parametrize("roles", [(role,) for role in s.ROLE_NAMES] + [("admin", "operator"), ("admin", "auditor")])
def test_api_capabilities_for_each_role_and_explicit_admin_bindings(h, roles):
    reader = "_".join(roles)
    if reader not in h.seed["identities"]:
        identity(h, reader, roles)
    row = submit(h, "staff", screenshot=screenshot())
    own = submit(h, reader, route="#own")
    effective = _effective(roles)
    can = lambda name: bool(effective.intersection(s.PERMISSIONS[name]))
    h.get(reader, "/api/access", status=200 if "admin" in effective or can("access.installation_admin") else 403)
    reports = h.get(reader, "/api/feedback")["rows"]
    assert {r["id"] for r in reports} == ({row["id"], own["id"]} if can("feedback.read_all") or reader == "staff" else {own["id"]})
    h.get(reader, "/api/feedback/" + row["id"], status=200 if can("feedback.read_all") or reader == "staff" else 404)
    image = h.client(reader).get("/api/feedback/" + row["id"] + "/screenshot")
    assert image.status_code == (200 if can("feedback.read_all") or reader == "staff" else 404)
    for path, permission in (("/api/audit", "audit.read"), ("/api/audit.csv", "audit.read"),
                             ("/api/operations", "system.operations")):
        assert h.client(reader).get(path).status_code == (200 if can(permission) else 403)
    changed = h.command(reader, "feedback.update", {"id": row["id"], "revision": 1, "status": "triaged"},
                        status=200 if can("feedback.triage") else 403)
    if can("feedback.triage"):
        h.command(reader, "feedback.undo", {"id": row["id"], "revision": changed["report"]["revision"]})
    else:
        h.command(reader, "feedback.undo", {"id": row["id"], "revision": 1}, status=403)
    h.command(reader, "feedback.export_github", {"id": row["id"]},
              status=200 if can("feedback.export_github") else 403)


def test_changed_permission_map_drives_reads_commands_services_receipts_and_home(h, monkeypatch):
    row = submit(h, "coordinator", screenshot=screenshot())
    for name in s.PERMISSIONS:
        monkeypatch.setitem(s.PERMISSIONS, name, ("staff",))
    h.get("staff", "/api/access")
    h.get("operator", "/api/access", status=403)
    assert h.get("staff", "/api/feedback")["total"] == 1
    assert h.get("operator", "/api/feedback")["total"] == 0
    h.get("operator", "/api/feedback/" + row["id"], status=404)
    assert h.client("staff").get("/api/feedback/" + row["id"] + "/screenshot").status_code == 200
    for path in ("/api/audit", "/api/audit.csv", "/api/operations"):
        assert h.client("staff").get(path).status_code == 200
        assert h.client("operator").get(path).status_code == 403
    assert any(item["key"] == "feedback" for item in h.get("staff", "/api/home")["attention"])
    assert not h.get("operator", "/api/home")["recent_changes"]
    key = str(uuid.uuid4())
    changed = h.command("staff", "feedback.update", {"id": row["id"], "revision": 1, "status": "triaged"}, key=key)
    assert h.get("staff", "/api/commands/" + key)["result"]["report"] == changed["report"]
    h.command("staff", "feedback.undo", {"id": row["id"], "revision": changed["report"]["revision"]})
    h.command("staff", "feedback.export_github", {"id": row["id"]})
    with transaction(h.app.state.engine) as db:
        actor = Actor(h.seed["identities"]["operator"], h.seed["department_id"], h.get("operator", "/api/me")["organisation"]["id"],
                      "Fictional Developer", frozenset({"operator"}), None, "session")
        for action, args in ((feedback.update, (SimpleNamespace(),)), (feedback.undo, (SimpleNamespace(),)),
                             (feedback.export_github, (SimpleNamespace(), h.settings))):
            with pytest.raises(Problem) as caught:
                action(db, actor, *args)
            assert caught.value.status == 403


def test_legacy_feedback_receipt_uses_current_permission_without_rewriting_history(h, monkeypatch):
    from apps.api import commands
    identity(h, "legacy_developer", ["admin", "operator"])
    row = submit(h)
    key = str(uuid.uuid4())
    payload = {"id": row["id"], "revision": 1, "status": "triaged"}
    with monkeypatch.context() as old_contract:
        old_contract.setattr(commands, "command_roles", lambda command: ["admin"])
        result = h.command("legacy_developer", "feedback.update", payload, key=key)
    with transaction(h.app.state.engine, write=True) as db:
        bindings = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"]["legacy_developer"])))
        next(b for b in bindings if b.role == "admin").revoked = True
    assert h.command("legacy_developer", "feedback.update", payload, key=key) == result
    assert h.get("legacy_developer", "/api/commands/" + key)["result"] == result
    with transaction(h.app.state.engine, write=True) as db:
        for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"]["legacy_developer"])):
            binding.revoked = binding.role == "operator"
    h.get("legacy_developer", "/api/commands/" + key, status=403)
    h.command("legacy_developer", "feedback.update", payload, key=key, status=403)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.CommandReceipt, result["receipt_id"]).required_roles == ["admin"]


@pytest.mark.parametrize("action", ["role.revoke", "identity.disable"])
@pytest.mark.parametrize("replacement", [None, "live", "pending", "accepted", "disabled", "future", "expired", "temporary", "revoked", "same_identity", "other_department"])
@pytest.mark.parametrize("system_role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
def test_last_developer_requires_enabled_current_permanent_cover(h, action, replacement, system_role, monkeypatch):
    monkeypatch.setattr(s, "SYSTEM_ROLES", (system_role,))
    target = h.seed["identities"][system_role]
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        binding_id, revision = binding.id, binding.revision
        if replacement:
            date = today(db, h.seed["department_id"])
            holder = target if replacement == "same_identity" else h.seed["identities"]["staff"]
            department_id = h.seed["department_id"]
            if replacement == "other_department":
                department = m.Department(organisation_id=db.get(m.Department, department_id).organisation_id, name="Other test department")
                db.add(department); db.flush()
                department_id = department.id
            db.add(m.RoleBinding(identity_id=holder, department_id=department_id, role=system_role,
                                starts_on=date + timedelta(days=1) if replacement == "future" else date - timedelta(days=2),
                                ends_on=date - timedelta(days=1) if replacement == "expired" else
                                        date + timedelta(days=1) if replacement == "temporary" else None,
                                revoked=replacement == "revoked"))
            if replacement == "disabled":
                db.get(m.Identity, holder).disabled = True
            if replacement in {"pending", "accepted"}:
                db.add(m.Account(identity_id=holder, username="replacement.developer", email="replacement@example.test",
                                 accepted_at=m.now() if replacement == "accepted" else None))
    permitted = replacement in {"live", "accepted"}
    result = h.command("operator" if action == "role.revoke" else "admin", action, {"reason": "Synthetic Developer handover"},
                       binding_id if action == "role.revoke" else target,
                       revision if action == "role.revoke" else 1, status=200 if permitted else 409)
    if not permitted:
        assert result["error"]["code"] == "LAST_DEVELOPER"
        with transaction(h.app.state.engine) as db:
            assert not db.get(m.Identity, target).disabled and not db.get(m.RoleBinding, binding_id).revoked


@pytest.mark.parametrize("action", ["role.revoke", "identity.disable"])
@pytest.mark.parametrize("account_accepted", [False, True])
def test_last_admin_requires_accepted_managed_cover(h, action, account_accepted):
    target = h.seed["identities"]["admin"]
    holder = identity(h, "replacement_admin", ["admin"])
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        binding_id, revision = binding.id, binding.revision
        db.add(m.Account(identity_id=holder, username="replacement.admin", email="replacement@example.test",
                         accepted_at=m.now() if account_accepted else None))
    result = h.command("admin", action, {"reason": "Synthetic Admin handover"},
                       binding_id if action == "role.revoke" else target,
                       revision if action == "role.revoke" else 1, status=200 if account_accepted else 409)
    if not account_accepted:
        assert result["error"]["code"] == "LAST_ADMIN"


@pytest.mark.parametrize("state", ["future", "expired", "revoked", "disabled", "pending"])
def test_non_live_developer_binding_revocation_preserves_its_existing_state(h, state):
    identity(h, "developer", ["operator"])
    with transaction(h.app.state.engine, write=True) as db:
        row = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == h.seed["identities"]["operator"]))
        date = today(db, row.department_id)
        if state == "future":
            row.starts_on = date + timedelta(days=1)
        elif state == "expired":
            row.ends_on = date
        elif state == "revoked":
            row.revoked = True
        elif state == "disabled":
            db.get(m.Identity, row.identity_id).disabled = True
        else:
            db.add(m.Account(identity_id=row.identity_id, username="pending.developer", email="pending@example.test"))
        row_id, revision = row.id, row.revision
    result = h.command("developer", "role.revoke", {"reason": "Remove historical binding"}, row_id, revision,
                       status=409 if state == "revoked" else 200)
    if state == "revoked":
        assert result["error"]["code"] == "WORKFLOW_STATE"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.RoleBinding, row_id)
        assert row.revoked and row.revision == revision + (state != "revoked")


def test_revoking_one_department_binding_keeps_other_departments_unchanged(h):
    h.client("operator")
    with transaction(h.app.state.engine, write=True) as db:
        target = h.seed["identities"]["operator"]
        row = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        row_id, revision = row.id, row.revision
        other = m.Department(organisation_id=db.get(m.Department, row.department_id).organisation_id, name="Other test department")
        db.add(other); db.flush()
        db.add_all([m.RoleBinding(identity_id=target, department_id=other.id, role="operator", starts_on=row.starts_on),
                    m.RoleBinding(identity_id=h.seed["identities"]["staff"], department_id=row.department_id,
                                  role="operator", starts_on=row.starts_on)])
        other_id = other.id
    h.command("operator", "role.revoke", {"reason": "Department-only handover"}, row_id, revision)
    with transaction(h.app.state.engine) as db:
        assert not db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target,
                                                       m.RoleBinding.department_id == other_id)).revoked


@pytest.mark.parametrize("harness", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("pending_cover", [False, True])
def test_anonymising_multiple_sign_ins_cannot_remove_all_developers(request, harness, pending_cover):
    h = request.getfixturevalue(harness)
    person_id = h.seed["people"][0]
    identity(h, "second_developer", ["operator"], person=person_id)
    if pending_cover:
        holder = identity(h, "pending_developer", ["operator"])
    with transaction(h.app.state.engine, write=True) as db:
        if pending_cover:
            db.add(m.Account(identity_id=holder, username="pending.developer", email="pending@example.test"))
        person = db.get(m.Person, person_id)
        person.active = False
        revision, name = person.revision, person.display_name
        db.get(m.Identity, h.seed["identities"]["operator"]).person_id = person_id
    result = h.command("admin", "person.anonymise", {"reason": "Synthetic leaver test"}, person_id, revision, status=409)
    assert result["error"]["code"] == "LAST_DEVELOPER"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Person, person_id).display_name == name
        assert not db.get(m.Identity, h.seed["identities"]["operator"]).disabled
        assert not db.get(m.Identity, h.seed["identities"]["second_developer"]).disabled
