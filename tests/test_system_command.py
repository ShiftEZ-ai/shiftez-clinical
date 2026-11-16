"""Maintenance commands use the same account permissions, receipts and audit as the API."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from scripts.system_command import apply, maintenance_identity
from .test_accounts import PASSWORD, anonymous, token_of, with_users_file


@pytest.mark.parametrize("kind", ["account.resend", "account.revoke_invite", "account.end_sessions"])
def test_maintenance_does_not_add_system_role_to_other_account_commands(h, monkeypatch, kind):
    from apps.api import commands

    actors = []

    def capture(db, actor, command, settings):
        actors.append(actor)
        return {}

    monkeypatch.setattr(commands, "dispatch", capture)
    with transaction(h.app.state.engine, write=True) as db:
        identity = maintenance_identity(db, m, select)
        department = db.get(m.Department, h.seed["department_id"])
        command = {"kind": kind, "payload": {"reason": "Synthetic maintenance authority check"}}
        result = apply(db, identity, department, [command], "synthetic-maintenance-authority", h.settings)[0]
        assert actors[0].roles == frozenset(commands.command_roles(s.Command.model_validate(command)))
        assert not actors[0].permits(*s.PERMISSIONS["access.system_roles"])
        assert db.get(m.CommandReceipt, result["receipt_id"]).required_roles == ["admin"]


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES))
@pytest.mark.parametrize("permission_roles", [s.PERMISSIONS["access.system_roles"], ("auditor",)])
def test_maintenance_invites_and_resets_explicit_roles_from_permission_map(h, tmp_path, monkeypatch, role, permission_roles):
    with_users_file(h, tmp_path)
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", permission_roles)

    def maintenance(kind, payload, target=None):
        with transaction(h.app.state.engine, write=True) as db:
            identity = maintenance_identity(db, m, select)
            department = db.get(m.Department, h.seed["department_id"])
            return apply(db, identity, department, [{"kind": kind, "payload": payload, "target_id": target}],
                         "synthetic-maintenance-accounts", h.settings)[0]

    invited = maintenance("account.invite", {"display_name": "Maintenance invite (test)",
        "email": "maintenance@example.test", "username": "maintenance.invite", "roles": [role],
        "reason": "Synthetic maintenance invitation"})
    target = invited["record"]["id"]
    with anonymous(h) as client:
        accepted = client.post(f"/api/account-links/{token_of(invited)}", json={"password": PASSWORD})
        assert accepted.status_code == 200, accepted.text
        reset = maintenance("account.reset_link", {"reason": "Synthetic maintenance recovery"}, target)
        accepted = client.post(f"/api/account-links/{token_of(reset)}", json={"password": PASSWORD + " replacement"})
        assert accepted.status_code == 200, accepted.text
    with transaction(h.app.state.engine) as db:
        identity = maintenance_identity(db, m, select)
        assert identity.disabled and identity.password_hash is None
        assert not list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == identity.id)))
        assert db.scalar(select(m.RoleBinding.role).where(m.RoleBinding.identity_id == target)) == role
        for result, action in ((invited, "account.invite"), (reset, "account.reset_link")):
            receipt = db.get(m.CommandReceipt, result["receipt_id"])
            assert receipt.actor_id == identity.id and receipt.command_kind == action
            assert "one_time_link" not in receipt.result
            audit = db.scalars(select(m.AuditEvent).where(m.AuditEvent.action == action)).one()
            assert audit.actor_id == identity.id and audit.detail["maintenance"] is True
            assert audit.detail["receipt_id"] == receipt.id
