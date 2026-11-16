"""Bulk dispatch preserves current programme receipt access and mailbox revocation hooks."""
from copy import deepcopy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.commands import COMMAND_PERMISSIONS, command_roles
from apps.api.db import transaction
from apps.api.services import mailboxes
from packages.contracts import schemas as s
from .governance_support import identity
from .test_bulk import REASON, preview
from .test_overtime_volunteer import offer, offer_rule


@pytest.mark.parametrize("kind", [
    "email.settings", "event_types.import_defaults", "email.microsoft.connect", "email.microsoft.disconnect",
    "event.occurrence.update", "department_content.remind", "event.rsvp", "department_content.seen",
    "notification.acknowledge", "rotation.plan", "rotation.copy", "rotation.undo_plan", "rotation.remove",
    "rotation.request_change", "trainee_note.create", "trainee_note.update", "trainee_note.delete", "trainee_note.restore",
])
def test_merged_commands_read_their_exact_canonical_permission_entry(kind, monkeypatch):
    assert COMMAND_PERMISSIONS[kind] == kind
    monkeypatch.setitem(s.PERMISSIONS, kind, ("auditor",))
    assert command_roles(s.Command(kind=kind)) == ["auditor"]


@pytest.mark.parametrize("change", ["owner_removed", "legacy_context_missing"])
def test_bulk_catalogue_receipt_rechecks_programme_ownership_on_get_and_retry(h, change):
    owner = identity(h, "bulk_director", ["programme_lead"])
    programme = h.configure("programmes", {"name": "Synthetic owned programme", "starts_on": "2026-01-01"})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme["id"]).owner_identity_id = owner
    payload = {"item_kind": "catalogue.create", "common": {"catalogue": "cohorts"}, "items": [
        {"payload": {"value": {"name": f"Synthetic owned cohort {number}", "programme_id": programme["id"],
                               "starts_on": "2026-07-01", "ends_on": "2027-07-01"}}} for number in range(2)],
        "reason": REASON}
    key = str(uuid.uuid4())
    result = h.command("bulk_director", "bulk.apply", payload, key=key)
    assert h.get("bulk_director", "/api/commands/" + key)["result"] == result
    assert h.command("bulk_director", "bulk.apply", payload, key=key) == result
    for item in result["items"]:
        assert item["results"][0]["authorization_command"] == {
            "kind": "catalogue.create", "payload": {"catalogue": "cohorts"}}
    with transaction(h.app.state.engine, write=True) as db:
        if change == "owner_removed":
            db.get(m.Programme, programme["id"]).owner_identity_id = None
        else:
            original = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
            stored = deepcopy(original.result)
            for item in stored["items"]:
                for reply in item["results"]:
                    reply.pop("authorization_command")
            key, receipt_id = str(uuid.uuid4()), m.uid()
            db.add(m.CommandReceipt(id=receipt_id, department_id=original.department_id, actor_id=owner,
                command_kind="bulk.apply", required_roles=original.required_roles, key=key,
                payload_hash=original.payload_hash, result={**stored, "receipt_id": receipt_id}))
    for reply in (h.get("bulk_director", "/api/commands/" + key, status=403),
                  h.command("bulk_director", "bulk.apply", payload, key=key, status=403)):
        assert reply["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("kind", ["identity.disable", "role.revoke", "person.anonymise"])
def test_bulk_access_changes_prune_mailbox_consent_once(h, monkeypatch, kind):
    person = h.configure("people", {"display_name": "Synthetic former mailbox owner", "active": False})["record"]
    target = identity(h, "bulk_mailbox_owner", ["coordinator"], person=person["id"])
    with transaction(h.app.state.engine, write=True) as db:
        connection = m.MailboxConnection(department_id=h.seed["department_id"], identity_id=target,
            encrypted_refresh_token="Synthetic opaque ciphertext", configuration_hash="synthetic")
        db.add(connection)
        if kind == "role.revoke":
            row = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == target))
        else:
            row = db.get(m.Person, person["id"]) if kind == "person.anonymise" else db.get(m.Identity, target)
        target_id, revision = row.id, row.revision
        db.flush()
        connection_id = connection.id
    calls = []
    original = mailboxes.prune
    def prune(db):
        calls.append(1)
        return original(db)
    monkeypatch.setattr(mailboxes, "prune", prune)
    h.command("admin", "bulk.apply", {"item_kind": kind, "items": [
        {"target_id": target_id, "expected_revision": revision, "payload": {}}], "reason": REASON})
    assert calls == [1]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.MailboxConnection, connection_id) is None


def test_bulk_without_access_changes_does_not_prune_mailboxes(h, monkeypatch):
    monkeypatch.setattr(mailboxes, "prune", lambda db: pytest.fail("Unrelated bulk edits do not prune mailboxes"))
    h.command("coordinator", "bulk.apply", {"item_kind": "catalogue.create", "common": {"catalogue": "people"},
        "items": [{"payload": {"value": {"display_name": "Synthetic ordinary bulk person"}}}], "reason": REASON})


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_bulk_overtime_approval_then_undo_retires_new_offer_facts(request, database):
    h = request.getfixturevalue(database)
    offer_rule(h)
    row = offer(h)["record"]
    payload = {"items": [
        {"item_kind": "request.decide", "target_id": row["id"], "expected_revision": row["revision"],
         "payload": {"decision": "approve"}},
        {"item_kind": "request.undo", "target_id": row["id"], "expected_revision": row["revision"],
         "payload": {}}], "reason": REASON}
    assert preview(h, "coordinator", payload)["counts"]["problem"] == 0
    h.command("coordinator", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, row["id"]).status == "submitted"
        facts = list(db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.request_id == row["id"])))
        assert facts and all(fact.status == "retired" for fact in facts)


@pytest.fixture
def protected_time_case(h):
    owner = identity(h, "bulk_protection_lead", ["programme_lead"])
    programme = h.configure("programmes", {"name": "Synthetic protected-time programme", "starts_on": "2026-01-01"})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme["id"]).owner_identity_id = owner
    h.configure("programme_memberships", {"person_id": h.seed["people"][0], "programme_id": programme["id"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01"})
    value = {"person_id": h.seed["people"][0], "kind": "protected_time", "start": "2026-10-05T08:00:00+03:00",
             "end": "2026-10-05T12:00:00+03:00", "private_reason": "Synthetic academic protected time"}
    row = h.command("bulk_protection_lead", "availability.record", value)["record"]
    assert h.get("bulk_protection_lead", "/api/me")["roles"] == ["programme_lead"]
    return h, programme, value, row


@pytest.mark.parametrize("loss", ["before_undo", "after_undo"])
def test_pure_programme_lead_bulk_protected_time_retract_and_undo_keep_current_authority(protected_time_case, loss):
    h, programme, value, row = protected_time_case
    payload = {"item_kind": "availability.retract", "items": [
        {"target_id": row["id"], "expected_revision": row["revision"], "payload": {}}], "reason": REASON}
    retracted = h.command("bulk_protection_lead", "bulk.apply", payload)
    undo_key = str(uuid.uuid4())
    if loss == "after_undo":
        restored = h.command("bulk_protection_lead", **retracted["undo"], key=undo_key)
        assert restored["items"][0]["record"]["retracted"] is False
        assert h.get("bulk_protection_lead", "/api/commands/" + undo_key)["result"] == restored
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, programme["id"]).owner_identity_id = None
    assert h.command("bulk_protection_lead", **retracted["undo"], key=undo_key, status=403)["error"]["code"] == "FORBIDDEN"
    if loss == "after_undo":
        assert h.get("bulk_protection_lead", "/api/commands/" + undo_key, status=403)["error"]["code"] == "FORBIDDEN"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, row["id"]).retracted is (loss == "before_undo")


@pytest.mark.parametrize("outside", ["person", "kind"])
def test_programme_lead_cannot_restore_time_outside_protected_trainee_scope(protected_time_case, outside):
    h, programme, value, _ = protected_time_case
    value = {**value, **({"person_id": h.seed["people"][1]} if outside == "person" else {"kind": "external_commitment"})}
    row = h.command("coordinator", "availability.record", value)["record"]
    row = h.command("coordinator", "availability.retract", {"reason": REASON}, row["id"], row["revision"])["record"]
    payload = {"item_kind": "availability.restore", "items": [
        {"target_id": row["id"], "expected_revision": row["revision"], "payload": {}}], "reason": REASON}
    issue = preview(h, "bulk_protection_lead", payload)["items"][0]["issues"][0]
    assert issue["code"] == "FORBIDDEN" and "handoff" not in issue
    assert h.command("bulk_protection_lead", "availability.restore", {"reason": REASON},
                     row["id"], row["revision"], status=403)["error"]["code"] == "FORBIDDEN"
    assert h.command("bulk_protection_lead", "bulk.apply", payload, status=403)["error"]["code"] == "FORBIDDEN"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, row["id"]).retracted
