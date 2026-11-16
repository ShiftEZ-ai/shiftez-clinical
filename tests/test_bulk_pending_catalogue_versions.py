"""Catalogue version admission follows earlier lifecycle transitions in a batch."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_rule_catalogue_conflicts import approve, line, most, propose


def item(kind, row, payload=None):
    return {"item_kind": kind, "target_id": row["id"], "expected_revision": row["revision"],
            "payload": payload or {}}


@pytest.mark.parametrize("following", ["restore_proposal", "first_proposal"])
def test_retire_then_propose_uses_current_catalogue_version(h, following):
    declined = propose(h, "duties_per_week", [line(numbers=most(7))])["record"]
    declined = h.command("coordinator", "policy.decline", {"reason": REASON},
                         declined["id"], declined["revision"])["record"]
    current = approve(h, propose(h, "duties_per_week", [line(numbers=most(5))])["record"])
    second = (item("policy.restore_proposal", declined) if following == "restore_proposal" else
              {"item_kind": "rule.propose", "payload": {
                  "catalogue_id": "duties_per_week", "lines": [line(numbers=most(3))]}})
    value = {"items": [item("policy.retire", current), second], "reason": REASON}
    assert preview(h, "admin", value)["counts"]["problem"] == 0
    reply = h.command("admin", "bulk.apply", value)
    waiting = reply["items"][1]["record"]
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "retired"
        assert db.get(m.Policy, waiting["id"]).status == "proposed"


def test_restored_active_catalogue_version_requires_expected_policy(h):
    current = approve(h, propose(h, "duties_per_week", [line(numbers=most(5))])["record"])
    retired = h.command("approver", "policy.retire", {"reason": REASON},
                        current["id"], current["revision"])["record"]
    value = {"items": [item("policy.restore", retired),
        {"item_kind": "rule.propose", "payload": {
            "catalogue_id": "duties_per_week", "lines": [line(numbers=most(3))]}}], "reason": REASON}
    checked = preview(h, "admin", value)
    assert checked["items"][1]["issues"][0]["code"] == "STALE_REVISION"
    refused = h.command("admin", "bulk.apply", value, status=409)["error"]
    assert refused["code"] == "STALE_REVISION" and refused["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, current["id"]).status == "retired"
