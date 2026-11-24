"""Bulk reverses preserve the shared Developer permission and credential-recovery boundary."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity
from .test_accounts import PASSWORD, accepted, anonymous, invite, token_of, with_users_file


REASON = {"reason": "Synthetic explicit-role restore after the dev merge"}


@pytest.mark.usefixtures('branding_editor')
def test_bulk_installation_admin_cannot_grant_after_revoking_own_access(h):
    role = s.INSTALLATION_ADMIN_ROLE
    admin, staff, coordinator = (h.seed['identities'][r] for r in ('admin', 'staff', 'coordinator'))
    first = next(b for b in h.get('admin', '/api/access')['bindings']
                 if b['identity_id'] == admin and b['role'] == role)

    def grant(identity_id):
        return {'item_kind': 'role.grant', 'payload': {
            'identity_id': identity_id, 'role': role, 'starts_on': '2020-01-01', **REASON}}

    revoke = {'item_kind': 'role.revoke', 'target_id': first['id'], 'expected_revision': first['revision'],
              'payload': REASON}
    payload = {'items': [grant(staff), revoke, grant(coordinator)], **REASON}
    refused = h.command('admin', 'bulk.apply', payload, status=(403, 409))
    assert refused['error']['code'] == 'INSTALLATION_ADMIN_REQUIRED'
    assert refused['error']['details']['failed_index'] == 2
    with transaction(h.app.state.engine) as db:
        bindings = list(db.scalars(select(m.RoleBinding).where(m.RoleBinding.role == role)))
        assert [(b.id, b.identity_id, b.revoked) for b in bindings] == [(first['id'], admin, False)]


def test_bulk_developer_cannot_grant_installation_admin_after_revoking_own_access(h):
    operator, staff, coordinator = (h.seed['identities'][r] for r in ('operator', 'staff', 'coordinator'))
    h.command('operator', 'role.grant', {'identity_id': staff, 'role': 'operator',
        'starts_on': '2020-01-01', **REASON})
    first = next(b for b in h.get('operator', '/api/access')['bindings']
                 if b['identity_id'] == operator and b['role'] == 'operator')
    payload = {'items': [
        {'item_kind': 'role.revoke', 'target_id': first['id'], 'expected_revision': first['revision'],
         'payload': REASON},
        {'item_kind': 'role.grant', 'payload': {'identity_id': coordinator, 'role': s.INSTALLATION_ADMIN_ROLE,
         'starts_on': '2020-01-01', **REASON}},
    ], **REASON}
    refused = h.command('operator', 'bulk.apply', payload, status=(403, 409))
    assert refused['error']['code'] == 'INSTALLATION_ADMIN_REQUIRED'
    assert refused['error']['details']['failed_index'] == 1
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.RoleBinding, first['id']).revoked
        assert db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == coordinator,
            m.RoleBinding.role == s.INSTALLATION_ADMIN_ROLE)) is None


def revoked_binding(h, role, target=None):
    target = target or identity(h, "restore_target", ["staff"])
    row = h.command("operator", "role.grant", {
        "identity_id": target, "role": role, "starts_on": "2020-01-01", **REASON})["record"]
    return h.command("operator", "role.revoke", REASON, row["id"], row["revision"])["record"]


def restore(h, actor, row, bulk, status=200):
    if bulk:
        return h.command(actor, "bulk.apply", {
            "item_kind": "role.restore", "items": [{"target_id": row["id"],
                "expected_revision": row["revision"], "payload": REASON}], **REASON}, status=status)
    return h.command(actor, "role.restore", REASON, row["id"], row["revision"], status=status)


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
@pytest.mark.parametrize("bulk", [False, True])
def test_explicit_role_restore_requires_developer_not_admin(h, role, bulk):
    row = revoked_binding(h, role)
    refused = restore(h, "admin", row, bulk, status=403)
    assert refused["error"]["code"] == "SYSTEM_ROLE_GRANT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RoleBinding, row["id"]).revoked
    restore(h, "operator", row, bulk)
    with transaction(h.app.state.engine) as db:
        restored = db.get(m.RoleBinding, row["id"])
        assert not restored.revoked and restored.revision == row["revision"] + 1


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES - {s.INSTALLATION_ADMIN_ROLE}))
@pytest.mark.parametrize("bulk", [False, True])
def test_explicit_role_restore_uses_current_system_permission_map(h, monkeypatch, role, bulk):
    row = revoked_binding(h, role)
    monkeypatch.setitem(s.PERMISSIONS, "access.system_roles", ("coordinator",))
    refused = restore(h, "operator", row, bulk, status=403)
    assert refused["error"]["code"] in {"FORBIDDEN", "SYSTEM_ROLE_GRANT"}
    restore(h, "coordinator", row, bulk)
    with transaction(h.app.state.engine) as db:
        assert not db.get(m.RoleBinding, row["id"]).revoked


@pytest.mark.parametrize("role", sorted(s.EXPLICIT_ROLES))
@pytest.mark.parametrize("purpose", ["invite", "reset"])
@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.usefixtures('branding_editor')
def test_explicit_role_restore_invalidates_open_recovery_links(h, tmp_path, role, purpose, bulk):
    with_users_file(h, tmp_path)
    target = accepted(h) if purpose == "reset" else invite(h)["record"]["id"]
    row = revoked_binding(h, role, target)
    link = h.command("admin", "account.reset_link" if purpose == "reset" else "account.resend", REASON, target)
    public = anonymous(h)
    path = f"/api/account-links/{token_of(link)}"
    assert public.get(path).status_code == 200
    restore(h, "operator", row, bulk)
    assert public.get(path).status_code == 404
    assert public.post(path, json={"password": PASSWORD + " replacement"}).status_code == 404
    with transaction(h.app.state.engine) as db:
        stored = db.get(m.AccountLink, link["link"]["id"])
        assert stored.revoked_at is not None and stored.used_at is None and stored.revision == 2
