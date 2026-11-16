"""Partial rule-number changes preserve each untouched authored number and strength."""
from copy import deepcopy
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize("patch,max_level", [
    ({"max": {"value": 6}}, "mandatory"),
    ({"max": {"value": 6}}, "high"),
    ({"max": {"value": 6}}, None),
    ({"max": {"level": "high"}}, "mandatory"),
    ({"max": {"value": 6, "level": None}}, "mandatory"),
    ({"max": {"value": None}}, "mandatory"),
])
def test_bulk_rule_number_change_merges_slots_and_preserves_untouched_levels(h, patch, max_level):
    original = {"min": {"value": 2, "level": "medium"},
                "max": {"value": 5, "level": max_level},
                "target": {"value": 3, "level": "low"}}
    authored = line(numbers=original)
    active = approve(h, propose(h, "duties_per_month", [authored])["record"])
    body = {"item_kind": "rule.propose", "selection": {
        "collection": "policy_lines", "include_ids": [authored["id"]]},
        "changes": [{"field": "numbers", "how": "set", "value": patch}], "reason": REASON}
    expected = deepcopy(original)
    expected["max"].update(patch["max"])

    checked = preview(h, "coordinator", body)
    assert checked["counts"]["waiting"] == 1, checked
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Policy.id).where(m.Policy.status == "proposed")) is None
    response = h.client("coordinator").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
        "kind": "bulk.apply", "payload": body, "expected_fingerprint": checked["source_fingerprint"]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["items"][0]["result"] == "waiting"
    pending = result["items"][0]["results"][0]["record"]
    assert pending["status"] == "proposed"
    assert pending["definition"]["rule"]["lines"][0]["numbers"] == expected
    with transaction(h.app.state.engine) as db:
        current = db.get(m.Policy, active["id"])
        assert current.status == "active" and current.revision == active["revision"]
        assert current.definition["rule"]["lines"][0]["numbers"] == original
    h.command("coordinator", "bulk.apply", result["undo"]["payload"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active["id"]).status == "active"
        assert db.get(m.Policy, pending["id"]).status == "retired"
