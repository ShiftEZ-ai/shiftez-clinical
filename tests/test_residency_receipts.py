"""Skipped rotation outcomes retain the same programme boundary as created snapshots."""
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_residency import REASON, other_programme, rotation_value, setup_residency


@pytest.fixture
def receipt_case(h, command_kind, mixed):
    h, programme, cohort, types, people, blocks = setup_residency(h)
    other, other_cohort, _ = other_programme(h, people[1])
    owner = identity(h, "receipt_director", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        for p in (programme, other):
            db.get(m.Programme, p["id"]).owner_identity_id = owner
    if command_kind == "rotation.copy":
        source = h.configure("rotations", rotation_value(people[0], cohort, types[0]))["record"]
        if mixed:
            h.configure("rotations", rotation_value(people[1], other_cohort, types[0]))
        payload = {"starts_on": "2026-10-01", "ends_on": "2026-11-01", "months": 12, "reason": REASON}
    else:
        source = None
        payload = {"cohort_id": cohort["id"], "person_ids": people[:1], "blocks": blocks,
                   "starts_on": "2027-06-01" if mixed else "2027-07-01", "reason": REASON}
    key = str(uuid.uuid4())
    result = h.command("receipt_director", command_kind, payload, key=key)
    assert len(result["created"]) == int(mixed)
    assert result["skipped"]
    if source:
        assert [row["source_id"] for row in result["skipped"]] == [source["id"]]
    assert h.get("receipt_director", "/api/commands/" + key)["result"] == result
    assert h.command("receipt_director", command_kind, payload, key=key) == result
    return h, programme, cohort, other, owner, key, payload, result


@pytest.mark.parametrize("command_kind", ["rotation.copy", "rotation.plan"])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("change", ["owner_removed", "original_programme", "current_cohort"])
def test_skipped_rotation_receipts_reauthorize_original_and_current_scope(receipt_case, command_kind, mixed, change):
    h, programme, cohort, other, owner, key, payload, result = receipt_case
    if change == "current_cohort":
        destination = h.configure("programmes", {"name": "Unowned destination", "starts_on": "2026-01-01"})["record"]
    else:
        destination = other
    if change != "owner_removed":
        h.configure("cohorts", {"name": cohort["name"], "programme_id": destination["id"],
            "starts_on": cohort["starts_on"], "ends_on": cohort["ends_on"]}, cohort["id"], cohort["revision"])
    if change != "current_cohort":
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Programme, programme["id"]).owner_identity_id = None
    assert h.get("receipt_director", "/api/me")["roles"] == ["programme_lead"]
    if command_kind == "rotation.copy" and mixed:
        assert {row["id"] for row in result["created"]} <= {
            row["id"] for row in h.get("receipt_director", "/api/catalogues")["rotations"]}
    for reply in (h.get("receipt_director", "/api/commands/" + key, status=403),
                  h.command("receipt_director", command_kind, payload, key=key, status=403)):
        assert reply["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("command_kind", ["rotation.copy", "rotation.plan"])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("provenance", ["missing", "empty", "null", "missing_cohort", "missing_programme", "unknown_cohort"])
def test_rotation_receipts_fail_closed_without_resolvable_scope(receipt_case, command_kind, mixed, provenance):
    h, programme, cohort, other, owner, key, payload, result = receipt_case
    with transaction(h.app.state.engine, write=True) as db:
        original = db.scalar(select(m.CommandReceipt).where(m.CommandReceipt.key == key))
        stored = dict(original.result)
        if provenance == "missing":
            stored.pop("rotation_scope")
        elif provenance in {"empty", "null"}:
            stored["rotation_scope"] = [] if provenance == "empty" else None
        else:
            scope = [dict(c) for c in stored["rotation_scope"]]
            if provenance == "unknown_cohort":
                scope[0]["cohort_id"] = m.uid()
            else:
                scope[0].pop("cohort_id" if provenance == "missing_cohort" else "programme_id")
            stored["rotation_scope"] = scope
        key, receipt_id = str(uuid.uuid4()), m.uid()
        db.add(m.CommandReceipt(id=receipt_id, department_id=original.department_id, actor_id=owner,
            command_kind=command_kind, required_roles=original.required_roles, key=key,
            payload_hash=original.payload_hash, result={**stored, "receipt_id": receipt_id}))
    for reply in (h.get("receipt_director", "/api/commands/" + key, status=403),
                  h.command("receipt_director", command_kind, payload, key=key, status=403)):
        assert reply["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("change", ["source_owner", "target_owner", "source_transfer", "target_transfer"])
@pytest.mark.parametrize("mixed", [False, True])
def test_skipped_copy_receipt_keeps_explicit_source_and_target_context(h, change, mixed):
    h, programme, cohort, types, people, blocks = setup_residency(h)
    other, target, _ = other_programme(h, people[0])
    owner = identity(h, "receipt_director", ["programme_lead"])
    with transaction(h.app.state.engine, write=True) as db:
        for p in (programme, other):
            db.get(m.Programme, p["id"]).owner_identity_id = owner
    h.configure("rotations", rotation_value(people[0], cohort, types[0], "2027-01-30", "2027-01-31"))
    if mixed:
        h.configure("rotations", rotation_value(people[0], cohort, types[0], "2027-01-01", "2027-01-15"))
    key = str(uuid.uuid4())
    payload = {"programme_id": programme["id"], "cohort_id": target["id"], "person_ids": people[:1],
               "starts_on": "2027-01-01", "ends_on": "2027-02-01", "months": 1, "reason": REASON}
    result = h.command("receipt_director", "rotation.copy", payload, key=key)
    assert len(result["created"]) == int(mixed) and len(result["skipped"]) == 1
    assert h.get("receipt_director", "/api/commands/" + key)["result"] == result
    assert h.command("receipt_director", "rotation.copy", payload, key=key) == result
    if change.endswith("transfer"):
        moving, destination = (cohort, other) if change == "source_transfer" else (target, programme)
        h.configure("cohorts", {"name": moving["name"], "programme_id": destination["id"],
            "starts_on": moving["starts_on"], "ends_on": moving["ends_on"]}, moving["id"], moving["revision"])
    lost = programme if change.startswith("source") else other
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Programme, lost["id"]).owner_identity_id = None
    for reply in (h.get("receipt_director", "/api/commands/" + key, status=403),
                  h.command("receipt_director", "rotation.copy", payload, key=key, status=403)):
        assert reply["error"]["code"] == "FORBIDDEN"
