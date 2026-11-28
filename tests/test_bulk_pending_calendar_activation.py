"""Replacement activation reads the lifecycle state changed earlier in the batch."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_bulk import REASON, preview
from .test_rule_templates import propose


def proposals(h, mode):
    extra = {}
    previous = None
    if mode == "calendar":
        programme = h.configure("programmes", {"name": "Synthetic calendar programme",
            "starts_on": "2026-01-01"})["record"]
        entry = h.configure("programme_calendar_entries", {"programme_id": programme["id"],
            "name": "Synthetic teaching day", "kind": "academic_block",
            "starts_on": "2026-10-01", "ends_on": "2026-11-01"})["record"]
        extra["calendar_entry_id"] = entry["id"]
    else:
        previous = propose(h, {"kind": "min_rest", "hours": 8})
        previous = h.command("approver", "policy.activate", {"reason": REASON},
            previous["id"], previous["revision"])["record"]
        extra["replaces_id"] = previous["id"]
    rows = [propose(h, {"kind": "min_rest", "hours": hours}, **extra) for hours in (10, 12)]
    return rows, previous


def activation(row):
    return {"item_kind": "policy.activate", "target_id": row["id"],
        "expected_revision": row["revision"], "payload": {}}


def test_successive_bulk_calendar_activations_retire_pending_predecessor(h):
    rows, _ = proposals(h, "calendar")
    payload = {"items": [activation(row) for row in rows], "reason": REASON}
    checked = preview(h, "approver", payload)
    assert checked["counts"]["problem"] == 0
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Policy, row["id"]).status for row in rows] == ["proposed", "proposed"]
    applied = h.command("approver", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Policy, row["id"]).status for row in rows] == ["retired", "active"]
        history = list(db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id == rows[0]["id"],
            m.DefinitionHistory.batch_id == applied["batch_id"]).order_by(m.DefinitionHistory.object_revision)))
        assert [entry.snapshot["status"] for entry in history] == ["active", "retired"]
    undo = applied["undo"]
    assert preview(h, "approver", undo["payload"])["counts"]["problem"] == 0
    pending = h.command("approver", **undo)
    assert not pending["items"][0]["results"][0]["approval"]["complete"]
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Policy, row["id"]).status for row in rows] == ["retired", "active"]
    identity(h, "second_approver", ["approver"])
    h.command("second_approver", **undo)
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Policy, row["id"]).status for row in rows] == ["retired", "retired"]


def test_bulk_activation_refuses_a_replacement_whose_predecessor_changed_in_batch(h):
    rows, previous = proposals(h, "predecessor")
    payload = {"items": [activation(row) for row in rows], "reason": REASON}
    checked = preview(h, "approver", payload)
    assert checked["items"][1]["issues"][0]["code"] == "WORKFLOW_STATE"
    refused = h.command("approver", "bulk.apply", payload, status=409)["error"]
    assert refused["code"] == "WORKFLOW_STATE" and refused["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Policy, row["id"]).status for row in rows] == ["proposed", "proposed"]
        assert db.get(m.Policy, previous["id"]).status == "active"


def test_definition_replacement_preserves_the_same_kind_gate(h):
    previous = h.policy({"kind": "min_rest", "hours": 8})
    row = propose(h, {"kind": "max_hours", "hours": 40}, replaces_id=previous["id"])
    refused = h.command("approver", "policy.activate", {"reason": REASON},
        row["id"], row["revision"], status=422)["error"]
    assert refused["code"] == "REPLACEMENT_KIND"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, row["id"]).status == "proposed"
        assert db.get(m.Policy, previous["id"]).status == "active"


@pytest.mark.parametrize("mode", ["calendar", "predecessor"])
def test_aggregate_activation_refuses_conflicting_replacement_versions_atomically(h, mode):
    rows, previous = proposals(h, mode)
    payload = {"reason": REASON, "items": [
        {"policy_id": row["id"], "expected_revision": row["revision"]} for row in rows]}
    refused = h.command("approver", "policy.activate_many", payload, status=409)["error"]
    assert refused["code"] == "WORKFLOW_STATE"
    with transaction(h.app.state.engine) as db:
        assert [db.get(m.Policy, row["id"]).status for row in rows] == ["proposed", "proposed"]
        if previous:
            assert db.get(m.Policy, previous["id"]).status == "active"


@pytest.mark.parametrize("pending", [True, False])
def test_goal_retirement_rechecks_current_personal_target_references(h, pending):
    spec = {"kind": "balance_goal", "metric": "hours", "aim": "target", "amount": 10,
        "period": "custom", "starts_on": "2026-01-01", "ends_on": "2027-12-31"}
    goal = h.policy(spec, severity="advisory")
    value = {"kind": "target", "person_id": h.seed["people"][0], "catalogue_id": "fair_share",
        "goal_ref": {"policy_id": goal["id"], "revision": goal["revision"]},
        "line": {"id": str(uuid4()), "numbers": {"target": {"value": 7, "level": "medium"}},
            "settings": {"balance_goal": {key: value for key, value in spec.items() if key not in ("kind", "amount")}}},
        "reason": REASON}
    if pending:
        first = {"item_kind": "person.workload.propose", "payload": value}
    else:
        fact = h.command("coordinator", "person.workload.propose", value)["fact"]
        first = {"item_kind": "person.workload.decline", "target_id": fact["id"],
            "expected_revision": fact["revision"], "payload": {}}
    payload = {"items": [first, {"item_kind": "policy.retire", "target_id": goal["id"],
        "expected_revision": goal["revision"], "payload": {}}], "reason": REASON}
    checked = preview(h, "admin", payload)
    if pending:
        assert checked["items"][1]["issues"][0]["code"] == "GOAL_HAS_PERSONAL_TARGETS"
        refused = h.command("admin", "bulk.apply", payload, status=409)["error"]
        assert refused["code"] == "GOAL_HAS_PERSONAL_TARGETS" and refused["details"]["failed_index"] == 1
    else:
        assert checked["counts"]["problem"] == 0
        h.command("admin", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, goal["id"]).status == ("active" if pending else "retired")
        facts = list(db.scalars(select(m.SchedulingFact)))
        assert len(facts) == (0 if pending else 1)
        if not pending:
            assert facts[0].id == fact["id"] and facts[0].status == "declined"


@pytest.mark.parametrize("pending", [True, False])
def test_calendar_edit_rechecks_current_protection_references(h, pending):
    programme = h.configure("programmes", {"name": "Synthetic protected programme",
        "starts_on": "2026-01-01"})["record"]
    calendar = {"programme_id": programme["id"], "name": "Synthetic teaching day", "kind": "academic_block",
        "starts_on": "2026-10-01", "ends_on": "2026-11-01"}
    entry = h.configure("programme_calendar_entries", calendar)["record"]
    value = {"kind": "protected_time", "calendar_entry_id": entry["id"], "catalogue_id": "protected_time",
        "line": {"id": str(uuid4()), "starts_on": calendar["starts_on"], "ends_on": calendar["ends_on"],
            "weekdays": [1], "start_time": "09:00", "end_time": "11:00",
            "numbers": {"apply": {"value": None, "level": "mandatory"}}}, "reason": REASON}
    if pending:
        first = {"item_kind": "calendar.protection.propose", "payload": value}
    else:
        fact = h.command("coordinator", "calendar.protection.propose", value)["fact"]
        first = {"item_kind": "calendar.protection.decline", "target_id": fact["id"],
            "expected_revision": fact["revision"], "payload": {}}
    payload = {"items": [first, {"item_kind": "catalogue.update", "target_id": entry["id"],
        "expected_revision": entry["revision"], "payload": {"catalogue": "programme_calendar_entries",
            "value": {**calendar, "ends_on": "2026-11-02"}}}], "reason": REASON}
    checked = preview(h, "admin", payload)
    if pending:
        assert checked["items"][1]["issues"][0]["code"] == "CALENDAR_PROTECTION_APPROVAL"
        refused = h.command("admin", "bulk.apply", payload, status=409)["error"]
        assert refused["code"] == "CALENDAR_PROTECTION_APPROVAL" and refused["details"]["failed_index"] == 1
    else:
        assert checked["counts"]["problem"] == 0
        h.command("admin", "bulk.apply", payload)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ProgrammeCalendarEntry, entry["id"]).ends_on.isoformat() == (
            calendar["ends_on"] if pending else "2026-11-02")
