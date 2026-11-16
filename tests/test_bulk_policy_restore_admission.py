"""Restoring historical rules cannot bypass the department's catalogue conversion."""
import pytest
from sqlalchemy import func, select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_bulk_inverse_gates import generations
from .test_bulk_reverse_transitions import proposal
from .test_rule_catalogue_conflicts import approve, line, most, propose
from .test_rule_catalogue_v2_api import proposed


DATABASES = ["h", pytest.param("pg", marks=pytest.mark.postgres)]


def historical_proposal(h, kind):
    return (proposal(h) if kind == "min_rest" else
            propose(h, "duties_per_week", [line(numbers=most(5))])["record"])


def convert(h):
    h.policy({"kind": "max_duties", "maximum": 9, "window": "week"})
    change = proposed(h)
    h.command("approver", "rule.catalogue.activate", {"reason": REASON},
              change["id"], change["revision"])


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("kind", ["min_rest", "catalogue_v1"])
@pytest.mark.parametrize("vehicle", ["single", "bulk", "undo"])
def test_declined_policy_restore_reuses_conversion_admission(request, database, kind, vehicle):
    h = request.getfixturevalue(database)
    original = historical_proposal(h, kind)
    decline = h.command("coordinator", "bulk.apply", {"item_kind": "policy.decline", "items": [
        {"target_id": original["id"], "expected_revision": original["revision"], "payload": {}}], "reason": REASON})
    declined = decline["items"][0]["record"]
    convert(h)
    payload = {"item_kind": "policy.restore_proposal", "items": [
        {"target_id": declined["id"], "expected_revision": declined["revision"], "payload": {}}], "reason": REASON}
    if vehicle == "undo":
        payload = decline["undo"]["payload"]
    with transaction(h.app.state.engine) as db:
        history_count = db.scalar(select(func.count()).select_from(m.DefinitionHistory))
    if vehicle == "single":
        error = h.command("coordinator", "policy.restore_proposal", {"reason": REASON},
                          declined["id"], declined["revision"], status=409)["error"]
    else:
        checked = preview(h, "coordinator", payload)
        assert checked["items"][0]["issues"][0]["code"] == "USE_CATALOGUE_V2"
        error = h.command("coordinator", "bulk.apply", payload, status=409)["error"]
        assert error["details"]["failed_index"] == 0
    assert error["code"] == "USE_CATALOGUE_V2"
    assert h.command("approver", "policy.activate", {"reason": REASON}, declined["id"],
                     declined["revision"], status=409)["error"]["code"] == "WORKFLOW_STATE"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, declined["id"])
        assert row.status == "retired" and row.revision == declined["revision"] and row.approved_by is None
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory)) == history_count


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("kind", ["min_rest", "catalogue_v1"])
def test_retired_policy_restore_reuses_conversion_admission(request, database, kind):
    h = request.getfixturevalue(database)
    original = approve(h, historical_proposal(h, kind))
    retired = h.command("approver", "policy.retire", {"reason": REASON},
                        original["id"], original["revision"])["record"]
    convert(h)
    error = h.command("approver", "policy.restore", {"reason": REASON},
                      retired["id"], retired["revision"], status=409)["error"]
    assert error["code"] == "USE_CATALOGUE_V2"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, retired["id"])
        assert row.status == "retired" and row.revision == retired["revision"]


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("kind", ["min_rest", "catalogue_v1"])
def test_previously_restored_legacy_proposal_cannot_activate_after_conversion(request, database, kind):
    h = request.getfixturevalue(database)
    original = historical_proposal(h, kind)
    declined = h.command("coordinator", "policy.decline", {"reason": REASON},
                         original["id"], original["revision"])["record"]
    convert(h)
    # Simulate a proposal already restored by the old bypass before this fix was deployed.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, declined["id"])
        row.status, row.active = "proposed", original["active"]
    error = h.command("approver", "policy.activate", {"reason": REASON},
                      declined["id"], declined["revision"], status=409)["error"]
    assert error["code"] == "USE_CATALOGUE_V2"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, declined["id"])
        assert row.status == "proposed" and row.revision == declined["revision"] and row.approved_by is None


@pytest.mark.parametrize("database", DATABASES)
def test_retiring_previously_restored_group_cannot_revive_legacy_predecessors(request, database):
    h = request.getfixturevalue(database)
    predecessors, group = generations(h, "legacy_group")
    convert(h)
    # The pre-fix restore path could also have left an approved legacy group in force.
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Policy, group["id"])
        row.status, row.active = "active", True
        revision = row.revision
        original_revisions = {row["id"]: db.get(m.Policy, row["id"]).revision for row in predecessors}
    error = h.command("approver", "policy.retire", {"reason": REASON},
                      group["id"], revision, status=409)["error"]
    assert error["code"] == "USE_CATALOGUE_V2"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, group["id"])
        assert row.status == "active" and row.revision == revision
        for predecessor in predecessors:
            row = db.get(m.Policy, predecessor["id"])
            assert row.status == "retired" and row.revision == original_revisions[row.id]


@pytest.mark.parametrize("database", DATABASES)
def test_converted_department_still_restores_and_activates_v2_proposals(request, database):
    h = request.getfixturevalue(database)
    convert(h)
    current = next(entry["rule"] for entry in h.get("coordinator", "/api/rules/catalogue?version=2")["entries"]
                   if entry["id"] == "workload_limit")
    waiting = h.command("coordinator", "rule.propose", {
        "catalogue_id": "workload_limit", "catalogue_version": 2, "expected_policy_id": current["id"],
        "expected_revision": current["revision"], "lines": current["definition"]["rule"]["lines"], "reason": REASON})["record"]
    declined = h.command("coordinator", "policy.decline", {"reason": REASON},
                         waiting["id"], waiting["revision"])["record"]
    restored = h.command("coordinator", "policy.restore_proposal", {"reason": REASON},
                         declined["id"], declined["revision"])["record"]
    assert restored["status"] == "proposed" and restored["approved_by"] is None
    assert approve(h, restored)["status"] == "active"


@pytest.mark.parametrize("database", DATABASES)
def test_group_retirement_restores_duplicate_legacy_predecessor_only_once(request, database):
    h = request.getfixturevalue(database)
    original = h.policy({"kind": "min_rest", "hours": 10})
    group = h.command("coordinator", "policy.create", {
        "name": "Synthetic legacy group with repeated predecessor", "owner": "Test", "source": REASON,
        "starts_on": "2020-01-01", "definition": {"rule": {"kind": "min_rest", "hours": 12},
                                                   "merged_ids": [original["id"], original["id"]]}})["record"]
    current = approve(h, group)
    retired = h.command("approver", "policy.retire", {"reason": REASON}, current["id"], current["revision"])
    assert retired["restored_ids"] == [original["id"]]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Policy, original["id"])
        assert row.status == "active" and row.revision == original["revision"] + 2
        revisions = list(db.scalars(select(m.DefinitionHistory.object_revision).where(
            m.DefinitionHistory.kind == "policy", m.DefinitionHistory.object_id == original["id"]).order_by(
                m.DefinitionHistory.object_revision)))
        assert revisions == list(range(1, row.revision + 1))
