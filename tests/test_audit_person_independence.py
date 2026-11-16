"""Self-approval grants follow people across their current department logins."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.worker.main import process_one
from .governance_support import identity


def linked_logins(h, alias_admin=False):
    person = h.seed["people"][2]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["admin"]).person_id = person
    return identity(h, "admin_alias", ["approver", *(["admin"] if alias_admin else [])], person)


def grant_payload(alias, subject, reason="Only one administrator this quarter"):
    return {subject: alias if subject == "identity_id" else "approver",
            "actions": ["rule.activate"], "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": reason}


def assert_no_self_grant(h):
    assert not h.get("admin", "/api/self-approval")["permissions"]
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "self_approval")))


@pytest.mark.parametrize("subject", ["identity_id", "role"])
def test_an_alias_cannot_bypass_an_independent_admin(h, subject):
    alias = linked_logins(h)
    identity(h, "independent_admin", ["admin"])
    refused = h.command("admin", "self_approval.grant", grant_payload(alias, subject), status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    assert refused["error"]["message"] == "Ask another admin to grant this"
    assert_no_self_grant(h)


@pytest.mark.parametrize("subject", ["identity_id", "role"])
def test_restoring_an_alias_cannot_bypass_an_independent_admin(h, subject):
    alias = linked_logins(h)
    permission = h.command("admin", "self_approval.grant", grant_payload(alias, subject))["permission"]
    revoked = h.command("admin", "self_approval.revoke", {"reason": "Ended temporary cover"},
                        permission["id"], permission["revision"])["permission"]
    identity(h, "independent_admin", ["admin"])
    before = h.get("admin", "/api/self-approval")["permissions"]
    events = h.get("auditor", "/api/audit?self_approved=true")["events"]
    refused = h.command("admin", "self_approval.restore", {"reason": "Restore temporary cover"},
                        revoked["id"], revoked["revision"], status=403)
    assert refused["error"]["code"] == "SELF_APPROVAL"
    assert refused["error"]["message"] == "Ask another admin to restore this"
    assert h.get("admin", "/api/self-approval")["permissions"] == before
    assert h.get("auditor", "/api/audit?self_approved=true")["events"] == events
    with transaction(h.app.state.engine) as db:
        row = db.get(m.SelfApprovalPermission, revoked["id"])
        assert row.revoked and row.revision == revoked["revision"]
        assert len(list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "self_approval")))) == 1


@pytest.mark.parametrize("subject", ["identity_id", "role"])
def test_own_alias_restore_bootstrap_needs_a_reason_and_is_audited(h, subject):
    alias = linked_logins(h)
    permission = h.command("admin", "self_approval.grant", grant_payload(alias, subject))["permission"]
    revoked = h.command("admin", "self_approval.revoke", {"reason": "Ended temporary cover"},
                        permission["id"], permission["revision"])["permission"]
    refused = h.command("admin", "self_approval.restore", {"reason": "Routine update"},
                        revoked["id"], revoked["revision"], status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_REASON"
    restored = h.command("admin", "self_approval.restore", {"reason": "Restoring sole administrator cover"},
                         revoked["id"], revoked["revision"])["permission"]
    assert restored[subject] == permission[subject]
    assert h.get("admin_alias", "/api/me")["self_approval"] == ["rule.activate"]
    event, = [e for e in h.get("auditor", "/api/audit?self_approved=true")["events"]
              if e["action"] == "self_approval.restore"]
    assert event["detail"]["self_approved"] == [{"action": "self_approval.grant", "permission_id": None}]
    with transaction(h.app.state.engine) as db:
        assert len(list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "self_approval")))) == 2


@pytest.mark.parametrize("subject", ["identity_id", "role"])
@pytest.mark.parametrize("alias_admin", [False, True])
def test_own_alias_bootstrap_is_marked_audited_and_announced(h, subject, alias_admin):
    alias = linked_logins(h, alias_admin)
    payload = grant_payload(alias, subject)
    permission = h.command("admin", "self_approval.grant", payload)["permission"]
    assert permission[subject] == payload[subject]
    assert permission["reason"] == payload["reason"]
    assert h.get("admin_alias", "/api/me")["self_approval"] == ["rule.activate"]
    event, = h.get("auditor", "/api/audit?self_approved=true")["events"]
    assert event["action"] == "self_approval.grant"
    assert event["detail"]["self_approved"] == [{"action": "self_approval.grant", "permission_id": None}]
    with transaction(h.app.state.engine) as db:
        version, = db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "self_approval", m.DefinitionHistory.object_id == permission["id"]))
        assert version.snapshot[subject] == payload[subject]
        assert version.reason == payload["reason"]
        note, = db.scalars(select(m.Outbox).where(m.Outbox.kind == "self_approval"))
        assert note.payload == {"action": "self_approval.grant", "target_id": permission["id"],
                                "actor_id": h.seed["identities"]["admin"]}
    process_one(h.app.state.engine)
    for recipient in ("approver", "auditor"):
        note, = h.get(recipient, "/api/notifications")["notifications"]
        assert "Grant self-approval permissions covering themselves" in note["message"]
        assert "allowed because no other admin is available" in note["message"]
        assert payload["reason"] not in note["message"]


@pytest.mark.parametrize("subject", ["identity_id", "role"])
@pytest.mark.parametrize("reason", ["Routine update", "Routine rota check"])
def test_own_alias_bootstrap_needs_a_real_reason(h, subject, reason):
    alias = linked_logins(h)
    refused = h.command("admin", "self_approval.grant", grant_payload(alias, subject, reason), status=422)
    assert refused["error"]["code"] == "SELF_APPROVAL_REASON"
    assert_no_self_grant(h)


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("subject", ["identity_id", "role"])
def test_grants_and_restores_for_other_people_remain_independent(h, subject, linked):
    recipient = identity(h, "other_person", ["approver"], h.seed["people"][1] if linked else None)
    identity(h, "independent_admin", ["admin"])
    payload = grant_payload(recipient, subject, "Routine update")
    payload["actions"] = ["request.decide"]
    permission = h.command("admin", "self_approval.grant", payload)["permission"]
    assert permission[subject] == payload[subject]
    revoked = h.command("admin", "self_approval.revoke", {"reason": "Mistaken revocation"},
                        permission["id"], permission["revision"])["permission"]
    restored = h.command("admin", "self_approval.restore", {"reason": "Routine update"},
                         revoked["id"], revoked["revision"])["permission"]
    assert restored[subject] == payload[subject]
    assert h.get("other_person", "/api/me")["self_approval"] == ["request.decide"]
    assert not h.get("auditor", "/api/audit?self_approved=true")["events"]
    with transaction(h.app.state.engine) as db:
        assert not list(db.scalars(select(m.Outbox).where(m.Outbox.kind == "self_approval")))
