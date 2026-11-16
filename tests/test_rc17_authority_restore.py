"""Undo restores recorded authority, never the administrator who presses the button."""
from .clock_support import freeze_clock
from datetime import timedelta
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .governance_support import identity, route
from .test_review_routes import delegation, submitted


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def store(request):
    return request.getfixturevalue(request.param)


def revoked_delegation(h):
    identity(h, "programme", ["programme_lead"])
    review_route = route(h, [{"key": "programme", "label": "Programme review", "role": "programme_lead"}])
    row = delegation(h, review_route)
    return h.command("admin", "review_delegation.revoke", {"reason": "Mistaken revocation"},
                     row["id"], row["revision"])["delegation"], review_route


def restore(h, kind, row, who="admin", status=200, payload=None, key=None):
    return h.command(who, kind + ".restore", payload or {"reason": "Undo"}, row["id"], row["revision"], status=status, key=key)


@pytest.mark.parametrize("who", ["admin", "programme"])
def test_restore_keeps_original_delegator_and_audits_successor(store, who):
    h = store
    old, _ = revoked_delegation(h)
    key = str(uuid.uuid4())
    result = restore(h, "review_delegation", old, who=who, key=key)
    new = result["delegation"]
    assert new["id"] != old["id"] and not new["revoked"]
    for field in ("delegator_id", "delegate_id", "route_id", "route_revision", "step_key", "starts_at", "expires_at", "reason"):
        assert new[field] == old[field]
    assert new["delegator_id"] == h.seed["identities"]["programme"]
    assert restore(h, "review_delegation", old, who=who, key=key) == result
    assert restore(h, "review_delegation", old, status=409)["error"]["code"] == "STALE_REVISION"
    assert restore(h, "review_delegation", {**old, "revision": old["revision"] + 1}, status=409)["error"]["code"] == "DELEGATION_STATE"
    with transaction(h.app.state.engine) as db:
        source = db.get(m.ReviewDelegation, old["id"])
        assert source.revoked and source.revision == old["revision"] + 1
        entry = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == new["id"]))
        assert entry.actor_id == h.seed["identities"][who]
        assert entry.reason == "Undo" and entry.snapshot["restored_from_id"] == old["id"]
        assert entry.snapshot["restored_from_revision"] == old["revision"]
    event, = (e for e in h.get("auditor", "/api/audit")["events"] if e["action"] == "review_delegation.restore")
    assert event["target_id"] == old["id"]
    # Restored authority still disappears when its original principal loses the role.
    w = submitted(h)
    w = h.command("approver", "workspace.approve", {"reason": "Delegated review", "delegation_id": new["id"]},
                  w["id"], w["revision"])["workspace"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, old["delegator_id"]).disabled = True
    assert h.lifecycle(w, "publish", status=409)["error"]["code"] == "APPROVER_REVOKED"


@pytest.mark.parametrize("change,code,status", [
    ("role", "DELEGATION_AUTHORITY", 403), ("disabled", "DELEGATION_AUTHORITY", 403),
    ("delegate", "DELEGATE_INELIGIBLE", 403), ("expired", "DELEGATION_EXPIRED", 409),
    ("route", "STALE_REVIEW_ROUTE", 409), ("same_person", "SELF_DELEGATION", 403),
])
def test_restore_revalidates_original_authority_and_expiry(store, monkeypatch, change, code, status):
    h = store
    row, review_route = revoked_delegation(h)
    if change == "role":
        with transaction(h.app.state.engine) as db:
            binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == row["delegator_id"]))
            target, revision = binding.id, binding.revision
        h.command("admin", "role.revoke", {"reason": "Lead responsibility ended"}, target, revision)
    elif change == "expired":
        expiry = m.now() + timedelta(days=1)
        freeze_clock(monkeypatch, lambda: expiry)
        h.clients.pop('admin').close()
    elif change == "route":
        h.command("approver", "review_route.retire", {"reason": "Route retired"}, review_route["id"], review_route["revision"])
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if change == "same_person":
                for identity_id in (row["delegator_id"], row["delegate_id"]):
                    db.get(m.Identity, identity_id).person_id = h.seed["people"][2]
            else:
                db.get(m.Identity, row["delegate_id"] if change == "delegate" else row["delegator_id"]).disabled = True
    refused = restore(h, "review_delegation", row, status=status)["error"]
    assert refused["code"] == code
    if change == "role":
        assert "restore their departmental role" in refused["message"]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ReviewDelegation, row["id"]).revision == row["revision"]
        assert len(list(db.scalars(select(m.ReviewDelegation)))) == 1


@pytest.mark.parametrize("kind", ["review_delegation", "self_approval"])
def test_restore_requires_bound_revoked_source_and_rejects_payload_authority(store, kind):
    h = store
    row = revoked_delegation(h)[0] if kind == "review_delegation" else revoked_permission(h)
    h.command("admin", kind + ".restore", {"reason": "Undo"}, revision=row["revision"], status=422)
    h.command("admin", kind + ".restore", {"reason": "Undo"}, row["id"], status=428)
    assert restore(h, kind, {**row, "revision": row["revision"] - 1}, status=409)["error"]["code"] == "STALE_REVISION"
    restore(h, kind, row, payload={"reason": "Undo", "created_by": h.seed["identities"]["admin"]}, status=422)
    new = restore(h, kind, row)["delegation" if kind == "review_delegation" else "permission"]
    restore(h, kind, new, status=409)
    restore(h, kind, row, who="staff", status=403)
    if kind == "review_delegation":
        restore(h, kind, {**row, "revision": row["revision"] + 1}, who="approver", status=403)


def revoked_permission(h, **subject):
    identity(h, "grantor", ["admin"])
    permission = h.command("grantor", "self_approval.grant", {
        "identity_id": h.seed["identities"]["approver"], "actions": ["rota.approve"],
        "starts_on": "2026-01-01", "ends_on": "2026-10-04", "reason": "Original bounded permission", **subject})["permission"]
    return h.command("admin", "self_approval.revoke", {"reason": "Mistaken revocation"},
                     permission["id"], permission["revision"])["permission"]


@pytest.mark.parametrize("subject", ["identity", "role"])
@pytest.mark.parametrize("dates", [{}, {"starts_on": "2099-01-01", "ends_on": "2100-01-01"}])
def test_permission_restore_preserves_original_grantor_and_scope(store, subject, dates):
    h = store
    old = revoked_permission(h, **({"identity_id": None, "role": "approver"} if subject == "role" else {}), **dates)
    new = restore(h, "self_approval", old)["permission"]
    assert new["id"] != old["id"] and not new["revoked"]
    for field in ("created_by", "identity_id", "role", "actions", "starts_on", "ends_on", "reason"):
        assert new[field] == old[field]
    assert new["created_by"] == h.seed["identities"]["grantor"]
    assert h.get("approver", "/api/me")["self_approval"] == ([] if dates else ["rota.approve"])
    assert restore(h, "self_approval", old, status=409)["error"]["code"] == "STALE_REVISION"
    assert restore(h, "self_approval", {**old, "revision": old["revision"] + 1}, status=409)["error"]["code"] == "WORKFLOW_STATE"
    with transaction(h.app.state.engine) as db:
        source = db.get(m.SelfApprovalPermission, old["id"])
        assert source.revoked and source.revision == old["revision"] + 1
        entry = db.scalar(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == new["id"]))
        assert entry.actor_id == h.seed["identities"]["admin"] and entry.reason == "Undo"
        assert entry.snapshot["restored_from_id"] == old["id"]
    assert any(e["action"] == "self_approval.restore" for e in h.get("auditor", "/api/audit")["events"])


@pytest.mark.parametrize("change,code,status", [("expired", "PERMISSION_EXPIRED", 409), ("subject", "NOT_FOUND", 404)])
def test_permission_restore_revalidates_dates_and_subject(store, monkeypatch, change, code, status):
    h = store
    row = revoked_permission(h)
    if change == "expired":
        expiry = m.now() + timedelta(days=1)
        freeze_clock(monkeypatch, lambda: expiry)
        h.clients.pop('admin').close()
    else:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Identity, row["identity_id"]).disabled = True
    assert restore(h, "self_approval", row, status=status)["error"]["code"] == code
    with transaction(h.app.state.engine) as db:
        assert db.get(m.SelfApprovalPermission, row["id"]).revision == row["revision"]


def test_permission_restore_cannot_bypass_independent_admin(store):
    h = store
    row = revoked_permission(h, identity_id=h.seed["identities"]["admin"])
    refused = restore(h, "self_approval", row, status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    # With no other admin, the existing bootstrap exception still requires a reason and announcement.
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["grantor"]).disabled = True
    restore(h, "self_approval", row, payload={"reason": "Routine update"}, status=422)
    restore(h, "self_approval", row, payload={"reason": "Restoring sole administrator cover"})
    event, = h.get("auditor", "/api/audit?self_approved=true")["events"]
    assert event["action"] == "self_approval.restore"
    assert event["detail"]["self_approved"] == [{"action": "self_approval.grant", "permission_id": None}]
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Outbox).where(m.Outbox.kind == "self_approval")) is not None


def test_restore_contract_only_accepts_reason():
    assert s.AuthorityRestoreInput.model_validate({"reason": "Undo"}).model_dump() == {"reason": "Undo"}
    with pytest.raises(ValueError):
        s.AuthorityRestoreInput.model_validate({"reason": "Undo", "delegator_id": str(uuid.uuid4())})
