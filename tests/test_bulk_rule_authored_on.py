"""Governed bulk rule edits keep an authored off switch unless it is explicitly changed."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.bulk_rule_lines import proposal_payload
from .test_bulk import REASON, preview
from .test_rule_catalogue_api import approve, line, propose


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("change", ["note", "numbers", "add_lines", "turn_on"])
def test_bulk_rule_edits_and_additions_preserve_authored_off_unless_overridden(h, version, change):
    entry = "duties_per_week" if version == 1 else "workload_limit"
    extra = {} if version == 1 else {"measure": "duties", "period": {"kind": "calendar_week"}}
    authored = line(weekdays=[0], numbers={"max": {"value": 5}}, note="Original note", **extra)
    active = approve(h, propose(h, entry, [authored], catalogue_version=version, on=False)["record"])
    assert active["active"] and active["definition"]["rule"]["on"] is False
    original = deepcopy(active["definition"])
    body = {"item_kind": "rule.propose", "reason": REASON}
    if change == "add_lines":
        added = line(weekdays=[1], numbers={"max": {"value": 3}}, **extra)
        body["items"] = [{"target_id": active["id"], "expected_revision": active["revision"],
                          "payload": {"add_lines": [added]}}]
    else:
        body["selection"] = {"collection": "policy_lines", "where": {
            "filters": {"policy_id": [active["id"]], "id": [authored["id"]]}}}
        body["changes"] = [{"field": "numbers", "how": "set", "value": {"max": {"value": 3}}}
                           if change == "numbers" else {"field": "note", "how": "set", "value": "Edited note"}]
        if change == "turn_on":
            body["common"] = {"on": True}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["waiting"] == 1, checked
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(m.Policy.id).where(m.Policy.status == "proposed")) is None
        assert db.get(m.Policy, active["id"]).definition == original
    response = h.client("coordinator").post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
        json={"kind": "bulk.apply", "payload": body, "expected_fingerprint": checked["source_fingerprint"]})
    assert response.status_code == 200, response.text
    result = response.json()
    proposed = result["items"][0]["results"][0]["record"]
    assert proposed["status"] == "proposed"
    assert proposed["definition"]["rule"]["on"] is (change == "turn_on")
    lines = proposed["definition"]["rule"]["lines"]
    if change == "add_lines":
        assert {row["id"] for row in lines} == {authored["id"], added["id"]}
    elif change == "numbers":
        assert lines[0]["numbers"]["max"]["value"] == 3
        assert lines[0]["note"] == "Original note"
    else:
        assert lines[0]["note"] == "Edited note"
        assert lines[0]["numbers"]["max"]["value"] == 5
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active["id"]).definition == original


@pytest.mark.parametrize("active,authored,override,expected", [
    (True, {}, None, True),
    (True, {"on": False}, None, False),
    (True, {"on": True}, None, True),
    (False, {"on": True}, None, False),
    (True, {"on": False}, {"on": True}, True),
    (True, {"on": True}, {"on": False}, False),
])
def test_rule_proposal_builder_keeps_effective_on_and_explicit_overrides(active, authored, override, expected):
    policy = SimpleNamespace(id="policy", revision=2, active=active,
                             definition={"rule": {"id": "duties_per_week", **authored}})
    assert proposal_payload(policy, [], REASON, override)["on"] is expected


@pytest.mark.parametrize("version", [1, 2])
def test_policies_sheet_toggle_builds_full_authored_catalogue_proposal(h, version):
    entry = "duties_per_week" if version == 1 else "workload_limit"
    extra = {} if version == 1 else {"measure": "duties", "period": {"kind": "calendar_week"}}
    authored = line(numbers={"max": {"value": 5, "level": "high"}}, note="Keep this line", **extra)
    active = approve(h, propose(h, entry, [authored], catalogue_version=version,
                               balance_between_people=False)["record"])
    body = {"item_kind": "rule.propose", "common": {}, "items": [{
        "target_id": active["id"], "expected_revision": active["revision"],
        "changes": [{"field": "on", "how": "set", "value": False}]}], "reason": "Edited in the Bulk editor"}
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["waiting"] == 1, checked
    result = h.command("coordinator", "bulk.apply", body)
    proposal = result["items"][0]["results"][0]["record"]
    expected = {**active["definition"]["rule"], "on": False}
    assert proposal["status"] == "proposed"
    assert proposal["definition"]["rule"] == expected
    assert proposal["definition"]["balance_between_people"] is False
    with transaction(h.app.state.engine) as db:
        original = db.get(m.Policy, active["id"])
        assert original.status == "active" and original.definition["rule"]["on"] is True
        assert original.revision == active["revision"] and original.definition == active["definition"]
        stored = db.get(m.Policy, proposal["id"])
        assert stored.status == "proposed" and stored.definition == proposal["definition"]
    repeated = preview(h, "coordinator", body)
    assert repeated["counts"]["problem"] == 1, repeated
    assert repeated["items"][0]["issues"][0]["code"] == "PROPOSAL_WAITING"
    refused = h.command("coordinator", "bulk.apply", body, status=409)
    assert refused["error"]["code"] == "PROPOSAL_WAITING"
    h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["coordinator"],
        "role": "approver", "starts_on": "2020-01-01", "reason": REASON})
    own = h.command("coordinator", "policy.activate", {"reason": REASON},
                    proposal["id"], proposal["revision"], status=403)
    assert own["error"]["code"] == "SELF_APPROVAL"
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.Policy.id).where(m.Policy.status == "proposed"))) == [proposal["id"]]
        stored = db.get(m.Policy, proposal["id"])
        assert stored.status == "proposed" and stored.revision == proposal["revision"]
        original = db.get(m.Policy, active["id"])
        assert original.status == "active" and original.revision == active["revision"]
        assert original.definition == active["definition"]
    activated = approve(h, proposal)
    assert activated["status"] == "active" and activated["definition"]["rule"] == expected
    assert activated["definition"]["balance_between_people"] is False
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, active["id"]).status == "retired"
