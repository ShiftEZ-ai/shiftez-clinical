"""Job receipts retain their authorization context and recheck current access."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.commands import idempotency_hash_input
from apps.api.db import transaction
from apps.api.snapshots import snapshot_hash
from packages.contracts import schemas as s


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize("replay", ["lookup", "resend"])
def test_approver_can_replay_a_feasibility_job_receipt(h, replay):
    workspace = h.workspace()
    payload = {"workspace_id": workspace["id"], "kind": "feasibility"}
    key = str(uuid.uuid4())
    result = h.command("approver", "job.create", payload, revision=workspace["revision"], key=key)
    if replay == "lookup":
        assert h.get("approver", "/api/commands/" + key)["result"] == result
    else:
        assert h.command("approver", "job.create", payload, revision=workspace["revision"], key=key) == result
    assert result["authorization_commands"] == [{"kind": "job.create", "payload": {"kind": "feasibility"}}]


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize("replay", ["lookup", "resend"])
@pytest.mark.parametrize("kind,current_role", [("generation", "approver"), ("feasibility", "staff")])
def test_job_receipt_replay_refuses_roles_without_current_authority(h, replay, kind, current_role):
    workspace = h.workspace()
    payload = {"workspace_id": workspace["id"], "kind": kind}
    key = str(uuid.uuid4())
    h.command("coordinator", "job.create", payload, revision=workspace["revision"], key=key)
    h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["coordinator"], "role": current_role,
              "starts_on": "2020-01-01", "reason": "Synthetic role change"})
    binding = next(row for row in h.get("admin", "/api/access")["bindings"]
                   if row["identity_id"] == h.seed["identities"]["coordinator"] and row["role"] == "coordinator")
    h.command("admin", "role.revoke", {"reason": "Synthetic role change"}, binding["id"], binding["revision"])
    if replay == "lookup":
        h.get("coordinator", "/api/commands/" + key, status=403)
    else:
        h.command("coordinator", "job.create", payload, revision=workspace["revision"], key=key, status=403)


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize("stored_kind", [True, False])
def test_legacy_job_receipt_replay_uses_only_the_available_kind(h, stored_kind):
    workspace = h.workspace()
    payload = {"workspace_id": workspace["id"], "kind": "feasibility"}
    original = h.command("approver", "job.create", payload, revision=workspace["revision"])
    legacy = {key: value for key, value in original.items() if key != "authorization_commands"}
    if not stored_kind:
        legacy["job"] = {key: value for key, value in legacy["job"].items() if key != "kind"}
    legacy["receipt_id"] = str(uuid.uuid4())
    key = str(uuid.uuid4())
    command = s.Command(kind="job.create", payload=payload, expected_revision=workspace["revision"])
    with transaction(h.app.state.engine, write=True) as db:
        db.add(m.CommandReceipt(id=legacy["receipt_id"], department_id=h.seed["department_id"],
            actor_id=h.seed["identities"]["approver"], command_kind=command.kind,
            required_roles=["coordinator", "approver"], key=key,
            payload_hash=snapshot_hash(idempotency_hash_input(command)), result=legacy))
    if not stored_kind:
        refused = h.get("approver", "/api/commands/" + key, status=403)
        assert refused["error"] == {"code": "FORBIDDEN", "message": "Your current role does not permit this action",
                                    "details": None}
        assert set(refused) == {"error", "correlation_id"}
        assert original["job"]["id"] not in str(refused) and workspace["id"] not in str(refused)
        assert h.command("approver", "job.create", payload, revision=workspace["revision"], key=key,
                         status=403)["error"] == refused["error"]
        h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["approver"], "role": "coordinator",
                  "starts_on": "2020-01-01", "reason": "Synthetic legacy receipt authority"})
    assert h.get("approver", "/api/commands/" + key)["result"] == legacy
    assert h.command("approver", "job.create", payload, revision=workspace["revision"], key=key) == legacy
    with transaction(h.app.state.engine) as db:
        assert db.get(m.CommandReceipt, legacy["receipt_id"]).result == legacy


@pytest.mark.parametrize("h", ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize("isolation", ["identity", "department"])
def test_job_receipt_lookup_does_not_reveal_another_scope(h, isolation):
    workspace = h.workspace()
    key = str(uuid.uuid4())
    original = h.command("approver", "job.create", {"workspace_id": workspace["id"], "kind": "feasibility"},
                         revision=workspace["revision"], key=key)
    reader = "coordinator" if isolation == "identity" else "approver"
    if isolation == "department":
        with transaction(h.app.state.engine, write=True) as db:
            other = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic independent department")
            db.add(other)
            db.flush()
            identity_id = h.seed["identities"]["approver"]
            db.add(m.RoleBinding(identity_id=identity_id, department_id=other.id, role="approver",
                                 starts_on=m.now().date()))
            for session in db.scalars(select(m.Session).where(m.Session.identity_id == identity_id)):
                session.department_id = other.id
    refused = h.get(reader, "/api/commands/" + key, status=404)
    unknown = h.get(reader, "/api/commands/" + str(uuid.uuid4()), status=404)
    assert refused["error"] == unknown["error"]
    assert refused["error"]["code"] == "RECEIPT_NOT_FOUND" and refused["error"]["details"] is None
    assert set(refused) == {"error", "correlation_id"}
    assert original["job"]["id"] not in str(refused) and workspace["id"] not in str(refused)
