"""Retirement Undo restores the approved rule switch, never a new rule decision."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import policies
from packages.domain.conditions import active_rules
from packages.domain.validation import evaluate
from .test_bulk_reverse_transitions import REASON, proposal, transition


def retire(h, enabled, vehicle):
    approved = transition(h, "approver", "policy.activate", proposal(h, active=enabled))["record"]
    if vehicle == "undo":
        applied = h.command("approver", "bulk.apply", {"item_kind": "policy.retire", "items": [
            {"target_id": approved["id"], "expected_revision": approved["revision"], "payload": REASON}], **REASON})
        return approved, applied["items"][0]["record"], applied["undo"]
    return approved, transition(h, "approver", "policy.retire", approved)["record"], None


def restore(h, vehicle, retired, undo, status=200):
    if vehicle == "undo":
        return h.command("approver", **undo, status=status)
    if vehicle == "bulk":
        return h.command("approver", "bulk.apply", {"item_kind": "policy.restore", "items": [
            {"target_id": retired["id"], "expected_revision": retired["revision"], "payload": REASON}], **REASON}, status=status)
    return transition(h, "approver", "policy.restore", retired, status=status)


@pytest.mark.parametrize("vehicle", ["individual", "bulk", "undo"])
@pytest.mark.parametrize("enabled", [False, True])
def test_restore_preserves_approved_switch_history_and_effective_rules(h, vehicle, enabled):
    approved, retired, undo = retire(h, enabled, vehicle)
    assert retired["status"] == "retired" and retired["active"] is False
    reply = restore(h, vehicle, retired, undo)
    restored = reply["record"] if vehicle == "individual" else reply["items"][0]["record"]
    assert restored["status"] == "active" and restored["active"] is enabled
    assert restored["revision"] == retired["revision"] + 1 == approved["revision"] + 2
    assert all(restored[key] == approved[key] for key in (
        "id", "name", "owner", "source", "starts_on", "ends_on", "definition", "created_by", "approved_by"))
    reloaded = next(row for row in h.get("approver", "/api/policies?status=active")["policies"]
                    if row["id"] == approved["id"])
    assert reloaded["active"] is enabled and reloaded["revision"] == restored["revision"]
    assert bool(active_rules([reloaded])) is enabled
    with transaction(h.app.state.engine) as db:
        versions = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "policy", m.DefinitionHistory.object_id == approved["id"])
            .order_by(m.DefinitionHistory.object_revision)))
        assert [row.object_revision for row in versions] == [1, 2, 3, 4]
        assert [(row.snapshot["status"], row.snapshot["active"]) for row in versions] == [
            ("proposed", enabled), ("active", enabled), ("retired", False), ("active", enabled)]
        assert all(row.snapshot["definition"] == approved["definition"] for row in versions)
        assert versions[-1].reason == "Restored: " + (undo["payload"]["reason"] if undo else REASON["reason"])
    assert transition(h, "approver", "policy.restore", retired, status=409)["error"]["code"] == "STALE_REVISION"
    workspace = h.shift(h.workspace())["workspace"]
    workspace = h.shift(workspace, start="2026-10-04T13:00:00+03:00", end="2026-10-04T17:00:00+03:00")["workspace"]
    findings = evaluate(h.snapshot(workspace))["findings"]
    assert any(row["code"] == "MIN_REST" and row.get("rule_id") == approved["id"] for row in findings) is enabled


@pytest.mark.parametrize("vehicle", ["individual", "bulk", "undo"])
@pytest.mark.parametrize("replacement_status", ["proposed", "active"])
def test_restore_keeps_replacement_conflicts_atomic(h, vehicle, replacement_status):
    approved, retired, undo = retire(h, False, vehicle)
    replacement = proposal(h, definition={**approved["definition"], "replaces_id": approved["id"]})
    if replacement_status == "active":
        # Simulate a retained replacement already in force; activation itself requires the predecessor active.
        with transaction(h.app.state.engine, write=True) as db:
            row = db.get(m.Policy, replacement["id"])
            row.status, row.approved_by = "active", approved["approved_by"]
    error = restore(h, vehicle, retired, undo, status=409)["error"]
    assert error["code"] == "WORKFLOW_STATE" and "replaces this rule" in error["message"]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, approved["id"])
        assert row.status == "retired" and row.active is False and row.revision == retired["revision"]
        assert list(db.scalars(select(m.DefinitionHistory.object_revision).where(
            m.DefinitionHistory.kind == "policy", m.DefinitionHistory.object_id == approved["id"])
            .order_by(m.DefinitionHistory.object_revision))) == [1, 2, 3]


@pytest.mark.parametrize("vehicle", ["individual", "bulk", "undo"])
@pytest.mark.parametrize("prior_state", [None, {"status": "proposed", "active": False}, {"status": "active", "active": None}])
def test_restore_refuses_unavailable_or_invalid_approved_history(h, monkeypatch, vehicle, prior_state):
    approved, retired, undo = retire(h, False, vehicle)
    original = policies.history_version

    def retained(db, actor, kind, ident, revision=None):
        if kind == "policy" and ident == approved["id"] and revision == approved["revision"]:
            return SimpleNamespace(snapshot=prior_state) if prior_state is not None else None
        return original(db, actor, kind, ident, revision)

    monkeypatch.setattr(policies, "history_version", retained)
    error = restore(h, vehicle, retired, undo, status=409)["error"]
    assert error["code"] == ("BULK_UNDO_UNAVAILABLE" if vehicle == "undo" else "WORKFLOW_STATE")
    assert "previous approved rule state is unavailable" in error["message"]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, approved["id"])
        assert row.status == "retired" and row.active is False and row.revision == retired["revision"]


def test_restore_still_rejects_a_caller_selected_switch(h):
    _, retired, _ = retire(h, False, "individual")
    transition(h, "approver", "policy.restore", retired, status=422, payload={**REASON, "active": True})


@pytest.mark.parametrize("vehicle", ["individual", "bulk"])
@pytest.mark.parametrize("enabled", [False, True])
def test_group_retirement_restores_predecessor_switch_and_history(h, vehicle, enabled):
    approved = transition(h, "approver", "policy.activate", proposal(h, active=enabled))["record"]
    group = proposal(h, definition={**approved["definition"], "merged_ids": [approved["id"], approved["id"]]})
    group = transition(h, "approver", "policy.activate", group)["record"]
    if vehicle == "bulk":
        result = h.command("approver", "bulk.apply", {"item_kind": "policy.retire", "items": [
            {"target_id": group["id"], "expected_revision": group["revision"], "payload": REASON}], **REASON})
        result = result["items"][0]
    else:
        result = transition(h, "approver", "policy.retire", group)
    if vehicle == "individual":
        assert result["restored_ids"] == [approved["id"]]
    reloaded = next(row for row in h.get("approver", "/api/policies?status=active")["policies"]
                    if row["id"] == approved["id"])
    assert reloaded["active"] is enabled
    assert reloaded["revision"] == approved["revision"] + 2
    assert reloaded["definition"] == approved["definition"]
    assert bool(active_rules([reloaded])) is enabled
    with transaction(h.app.state.engine) as db:
        versions = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.kind == "policy", m.DefinitionHistory.object_id == approved["id"])
            .order_by(m.DefinitionHistory.object_revision)))
        assert [(row.snapshot["status"], row.snapshot["active"]) for row in versions] == [
            ("proposed", enabled), ("active", enabled), ("retired", False), ("active", enabled)]


@pytest.mark.parametrize("vehicle", ["individual", "bulk"])
def test_group_retirement_without_approved_history_is_atomic(h, monkeypatch, vehicle):
    approved = transition(h, "approver", "policy.activate", proposal(h, name="Legacy predecessor", active=False))["record"]
    group = proposal(h, name="Grouped rule", definition={**approved["definition"], "merged_ids": [approved["id"]]})
    group = transition(h, "approver", "policy.activate", group)["record"]
    original = policies.history_version

    def retained(db, actor, kind, ident, revision=None):
        if kind == "policy" and ident == approved["id"] and revision == approved["revision"]:
            return None
        return original(db, actor, kind, ident, revision)

    monkeypatch.setattr(policies, "history_version", retained)
    if vehicle == "bulk":
        reply = h.command("approver", "bulk.apply", {"item_kind": "policy.retire", "items": [
            {"target_id": group["id"], "expected_revision": group["revision"], "payload": REASON}], **REASON}, status=409)
    else:
        reply = transition(h, "approver", "policy.retire", group, status=409)
    assert reply["error"]["code"] == "WORKFLOW_STATE"
    assert "previous approved rule state is unavailable" in reply["error"]["message"]
    assert approved["name"] in reply["error"]["message"]
    assert "propose" in reply["error"]["message"]
    assert reply["error"]["details"]["policy_id"] == approved["id"]
    assert reply["error"]["details"]["policy_name"] == approved["name"]
    with transaction(h.app.state.engine) as db:
        current, predecessor = db.get(m.Policy, group["id"]), db.get(m.Policy, approved["id"])
        assert current.status == "active" and current.revision == group["revision"]
        assert predecessor.status == "retired" and predecessor.revision == approved["revision"] + 1
