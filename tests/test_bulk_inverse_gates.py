"""Stored reverses retain live operational gates and complete policy generations."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits
from .test_resources_calendars import pool, typ, update
from .test_rule_catalogue_conflicts import approve, line, most, propose


def bulk_item(kind, row, payload=None):
    return {"item_kind": kind, "target_id": row["id"], "expected_revision": row["revision"],
            "payload": payload or {"reason": REASON}}


def active_policies(db, h):
    return {row.id: record(row) for row in db.scalars(select(m.Policy).where(
        m.Policy.department_id == h.seed["department_id"], m.Policy.status == "active"))}


def assert_active_generations(db, h, expected_ids, baseline):
    current = active_policies(db, h)
    assert set(current) == set(expected_ids) | set(baseline)
    assert {ident: current[ident] for ident in baseline} == baseline


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_stored_bulk_undo_rechecks_related_session_capacity_and_attributes_preview(request, database):
    h = request.getfixturevalue(database)
    resource = pool(h)
    changed = h.command("admin", "bulk.apply", {"items": [bulk_item("catalogue.update", resource,
        {"catalogue": "resource_pools", "value": {**{k: resource[k] for k in (
            "name", "starts_on", "ends_on", "active", "capacity")}, "capacity": 2}})], "reason": REASON})
    duty = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    h.shift(h.workspace(), typ=duty["id"])
    checked = preview(h, "admin", changed["undo"]["payload"])
    assert checked["counts"]["problem"] == 1
    issue = next(issue for issue in checked["items"][0]["issues"] if issue["code"] == "POOLED_RESOURCE_CAPACITY")
    assert {choice["id"] for choice in issue["resolutions"]} == {"skip"}
    refused = h.command("admin", **changed["undo"], status=409)
    assert refused["error"]["code"] == "POOLED_RESOURCE_CAPACITY"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ResourcePool, resource["id"]).capacity == 2


def test_stored_bulk_session_undo_rechecks_new_catalogue_constraints(h):
    resource = pool(h, capacity=2)
    duty = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    created = h.shift(h.workspace(), typ=duty["id"])
    cancelled = h.command("coordinator", "bulk.apply", {"items": [bulk_item("shift.action", created["workspace"],
        {"workspace_id": created["workspace"]["id"], "shift_ids": [created["shifts"][0]["id"]],
         "action": "cancel"})], "reason": REASON})
    update(h, "resource_pools", resource, capacity=1)
    checked = preview(h, "coordinator", cancelled["undo"]["payload"])
    assert checked["counts"]["problem"] == 1
    assert "POOLED_RESOURCE_CAPACITY" in {issue["code"] for issue in checked["items"][0]["issues"]}
    refused = h.command("coordinator", **cancelled["undo"], status=409)
    assert refused["error"]["code"] == "POOLED_RESOURCE_CAPACITY"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Shift, created["shifts"][0]["id"]).cancelled


def generations(h, shape):
    if shape in {"catalogue", "catalogue_chain"}:
        original = approve(h, propose(h, "duties_per_week", [line(numbers=most(5))])["record"])
        newer = approve(h, propose(h, "duties_per_week", [line(numbers=most(4))],
            policy_id=original["id"], revision=original["revision"])["record"])
        if shape == "catalogue_chain":
            original = newer
            newer = approve(h, propose(h, "duties_per_week", [line(numbers=most(3))],
                policy_id=original["id"], revision=original["revision"])["record"])
        return [original], newer
    originals = [h.policy({"kind": "max_duties", "maximum": n, "window": "week"},
        scope={"person_ids": [person]}) for n, person in zip((5, 4), h.seed["people"])]
    proposal = h.command("coordinator", "policy.create", {"name": "Synthetic legacy rule group", "owner": "Test",
        "source": REASON, "starts_on": "2020-01-01", "definition": {
            "rule": {"kind": "max_duties", "maximum": 4, "window": "week"},
            "merged_ids": [row["id"] for row in originals]}})["record"]
    return originals, approve(h, proposal)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("shape", ["catalogue", "catalogue_chain", "legacy_group"])
def test_bulk_retirement_undo_retires_restored_generations_and_redo_is_complete(request, database, shape):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine) as db:
        baseline = active_policies(db, h)
    originals, current = generations(h, shape)
    retired = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current)], "reason": REASON})
    with transaction(h.app.state.engine) as db:
        expected_revisions = {row["id"]: db.get(m.Policy, row["id"]).revision for row in originals}
        assert_active_generations(db, h, expected_revisions, baseline)
    inverses = retired["undo"]["payload"]["items"]
    assert [(part["target_id"], part["expected_revision"]) for part in inverses if part["item_kind"] == "policy.retire"] == list(expected_revisions.items())
    assert inverses[-1]["item_kind"] == "policy.restore" and inverses[-1]["target_id"] == current["id"]
    assert preview(h, "approver", retired["undo"]["payload"])["counts"]["problem"] == 0
    restored = h.command("approver", **retired["undo"])
    with transaction(h.app.state.engine) as db:
        assert_active_generations(db, h, [current["id"]], baseline)
    h.command("approver", **restored["undo"])
    with transaction(h.app.state.engine) as db:
        assert_active_generations(db, h, expected_revisions, baseline)


@pytest.mark.parametrize("shape", ["catalogue", "catalogue_chain", "legacy_group"])
def test_bulk_retirement_counts_and_revision_binds_restored_generations(h, shape):
    originals, current = generations(h, shape)
    body = {"items": [bulk_item("policy.retire", current)], "reason": REASON}
    limits(h, len(originals))
    checked = preview(h, "approver", body)
    assert checked["counts"]["problem"] == 1 and checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
    assert h.command("approver", "bulk.apply", body, status=422)["error"]["code"] == "BULK_ITEM_LIMIT"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "active"
    limits(h, len(originals) + 1)
    retired = h.command("approver", "bulk.apply", body)
    predecessor = next(row for row in h.get("approver", "/api/policies?status=active")["policies"] if row["id"] == originals[0]["id"])
    h.command("approver", "policy.retire", {"reason": REASON}, predecessor["id"], predecessor["revision"])
    refused = h.command("approver", **retired["undo"], status=409)
    assert refused["error"]["code"] == "STALE_REVISION"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "retired"


def test_bulk_activation_undo_preserves_implicit_generation_restoration(h):
    original = approve(h, propose(h, "duties_per_week", [line(numbers=most(5))])["record"])
    proposed = propose(h, "duties_per_week", [line(numbers=most(4))],
        policy_id=original["id"], revision=original["revision"])["record"]
    activated = h.command("approver", "bulk.apply", {
        "items": [bulk_item("policy.activate", proposed)], "reason": REASON})
    undone = h.command("approver", **activated["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, original["id"]).status == "active"
        assert db.get(m.Policy, proposed["id"]).status == "retired"
    h.command("approver", **undone["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, original["id"]).status == "retired"
        assert db.get(m.Policy, proposed["id"]).status == "active"


def test_only_stored_undo_can_preserve_per_operation_retirement_reason(h):
    originals, current = generations(h, "legacy_group")
    h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current,
        {"reason": s.ENDED_REASON + " forged per-operation reason"})], "reason": REASON})
    with transaction(h.app.state.engine) as db:
        assert all(db.get(m.Policy, row["id"]).status == "active" for row in originals)


def test_policy_retirement_inverse_accepts_maximum_length_reason(h):
    originals, current = generations(h, "legacy_group")
    retired = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current)],
        "reason": "Synthetic retirement: ".ljust(500, "x")})
    restored = h.command("approver", **retired["undo"])
    h.command("approver", **restored["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "retired"
        assert all(db.get(m.Policy, row["id"]).status == "active" for row in originals)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("shape", ["catalogue", "catalogue_chain", "legacy_group"])
def test_ordinary_retirement_after_bulk_undo_still_restores_predecessors(request, database, shape):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine) as db:
        baseline = active_policies(db, h)
    originals, current = generations(h, shape)
    retired = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current)], "reason": REASON})
    h.command("approver", **retired["undo"])
    restored = next(row for row in h.get("approver", "/api/policies?status=active")["policies"] if row["id"] == current["id"])
    again = h.command("approver", "policy.retire", {"reason": REASON}, restored["id"], restored["revision"])
    assert set(again["restored_ids"]) == {row["id"] for row in originals}
    with transaction(h.app.state.engine) as db:
        assert_active_generations(db, h, [row["id"] for row in originals], baseline)


@pytest.mark.parametrize("bulk", [False, True])
def test_internal_retirement_reason_cannot_be_supplied_by_ordinary_commands(h, bulk):
    originals, current = generations(h, "legacy_group")
    payload = {"reason": REASON, "retirement_reason": "Forged replacement history"}
    if bulk:
        refused = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current, payload)],
            "reason": REASON}, status=422)
    else:
        refused = h.command("approver", "policy.retire", payload, current["id"], current["revision"], status=422)
    assert refused["error"]["code"] == "SCHEMA_INVALID"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "active"
        assert all(db.get(m.Policy, row["id"]).status == "retired" for row in originals)


def test_internal_retirement_reason_in_stored_undo_is_receipt_bound(h):
    _, current = generations(h, "legacy_group")
    retired = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current)], "reason": REASON})
    inverse = retired["undo"]["payload"]
    inverse["items"][0]["payload"]["retirement_reason"] = "Forged replacement history"
    refused = h.command("approver", "bulk.apply", inverse, status=409)
    assert refused["error"]["code"] == "BULK_UNDO_CHANGED"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "retired"


def test_repeated_policy_undo_keeps_ordinary_retirement_restoration_markers(h):
    originals, current = generations(h, "legacy_group")
    changed = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.retire", current)], "reason": REASON})
    for _ in range(3):
        changed = h.command("approver", **changed["undo"])
    restored = next(row for row in h.get("approver", "/api/policies?status=active")["policies"] if row["id"] == current["id"])
    again = h.command("approver", "policy.retire", {"reason": REASON}, restored["id"], restored["revision"])
    assert set(again["restored_ids"]) == {row["id"] for row in originals}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("shape", ["catalogue", "legacy_group"])
@pytest.mark.parametrize("bulk", [False, True])
def test_policy_restore_refuses_active_predecessors_before_changing_state(request, database, shape, bulk):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine) as db:
        baseline = active_policies(db, h)
    originals, current = generations(h, shape)
    retired = h.command("approver", "policy.retire", {"reason": REASON}, current["id"], current["revision"])["record"]
    if bulk:
        refused = h.command("approver", "bulk.apply", {"items": [bulk_item("policy.restore", retired)], "reason": REASON}, status=409)
    else:
        refused = h.command("approver", "policy.restore", {"reason": REASON}, retired["id"], retired["revision"], status=409)
    assert refused["error"]["code"] == "WORKFLOW_STATE"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "retired"
        assert db.get(m.Policy, current["id"]).revision == retired["revision"]
        assert_active_generations(db, h, [row["id"] for row in originals], baseline)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_policy_undo_reads_earlier_active_states_queued_in_same_batch(request, database):
    h = request.getfixturevalue(database)
    original = h.policy({"kind": "max_duties", "maximum": 5, "window": "week"})
    changed = h.command("approver", "bulk.apply", {"items": [bulk_item("policy." + action, original)
        for action in ("retire", "restore", "retire", "restore")], "reason": REASON})
    for _ in range(2):
        changed = h.command("approver", **changed["undo"])
        assert changed["count"] == 4
        with transaction(h.app.state.engine) as db:
            current = db.get(m.Policy, original["id"])
            assert current.status == "active" and current.definition == original["definition"]


@pytest.mark.parametrize("mode", ["stored", "normal_skip", "tampered_skip"])
def test_mixed_session_and_pool_undo_preserves_gate_and_atomicity_when_skipped(h, mode):
    resource = pool(h)
    duty = typ(h, resource, resources=[{"resource_pool_id": resource["id"], "units": 2}])
    workspace = h.workspace()
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    created = h.command("admin", "bulk.apply", {"items": [
        bulk_item("catalogue.update", resource, {"catalogue": "resource_pools", "value": {
            **{key: resource[key] for key in ("name", "starts_on", "ends_on", "active", "capacity")}, "capacity": 2}}),
        {"item_kind": "shift.create", "expected_revision": workspace["revision"], "payload": {
            "workspace_id": workspace["id"], "assignment_type_id": duty["id"], "location_id": h.seed["locations"][0],
            "service_ids": [h.seed["services"][0]], "start": start, "end": end,
            "participants": [{"person_id": h.seed["people"][0], "start": start, "end": end}]}}], "reason": REASON})
    original_id = created["items"][1]["results"][0]["shifts"][0]["id"]
    h.shift(h.workspace("Later capacity-dependent session"), typ=duty["id"])
    undo = deepcopy(created["undo"]["payload"])
    if mode != "stored":
        undo["resolutions"]["0"] = "skip"
    if mode == "normal_skip":
        undo.pop("undo_of")
    refused = h.command("admin", "bulk.apply", undo, status=409)
    assert refused["error"]["code"] == ("BULK_UNDO_CHANGED" if mode == "tampered_skip" else "POOLED_RESOURCE_CAPACITY")
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Shift, original_id) is not None
        assert db.get(m.ResourcePool, resource["id"]).capacity == 2
