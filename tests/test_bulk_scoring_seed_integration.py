"""Bulk rule edits use the editor's effective seeded lines and independent review."""
from copy import deepcopy
from uuid import uuid4

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services.scoring_defaults import line_id, seed
from packages.contracts.schemas import TypeDefinition
from .test_bulk import REASON, preview
from .test_rule_catalogue_api import approve, line


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("path", ["line", "policy", "add_lines"])
def test_bulk_proposals_preserve_effective_scoring_seed_lines(request, database, path):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        duty = db.get(m.AssignmentType, h.seed["type"])
        duty.definition = TypeDefinition.model_validate({**duty.definition,
            "lead": {"minimum": 1, "maximum": 1}}).model_dump(mode="json")
        seed(db, h.seed["department_id"], types=[duty], workload=False)
    current = next(p for p in h.get("coordinator", "/api/policies?status=active")["policies"]
                   if p["definition"]["rule"].get("id") == "fair_share")
    seeded_id = line_id(h.seed["department_id"], "lead:" + h.seed["type"])
    original = deepcopy(current["definition"]["rule"]["lines"])
    assert seeded_id in {row["id"] for row in original}
    with transaction(h.app.state.engine) as db:
        assert seeded_id not in {row["id"] for row in db.get(m.Policy, current["id"]).definition["rule"]["lines"]}
    selection = {"collection": "policy_lines", "where": {
        "filters": {"policy_id": [current["id"]], "id": [seeded_id]}}}
    resolved = h.client("coordinator").post("/api/bulk/resolve", json=selection)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["count"] == 1
    rows = h.client("coordinator").post("/api/bulk/rows", json={
        "selection": selection, "columns": ["note", "numbers"], "limit": 100}).json()
    assert [row["id"] for row in rows["rows"]] == [seeded_id]
    payload = {"item_kind": "rule.propose", "reason": REASON}
    expected = deepcopy(original)
    if path == "line":
        payload.update(selection=selection, changes=[{"field": "note", "value": "Reviewed seeded goal"}])
        next(row for row in expected if row["id"] == seeded_id)["note"] = "Reviewed seeded goal"
    elif path == "policy":
        payload["items"] = [{"target_id": current["id"], "expected_revision": current["revision"],
                             "changes": [{"field": "on", "value": False}]}]
    else:
        added = line(numbers={"apply": {"level": "medium"}}, settings={"balance_goal": {
            "metric": "hours", "aim": "equal_share", "period": "rota"}})
        payload["items"] = [{"target_id": current["id"], "expected_revision": current["revision"],
                             "payload": {"add_lines": [added]}}]
    checked = preview(h, "coordinator", payload)
    assert checked["counts"]["waiting"] == 1, checked
    response = h.client("coordinator").post("/api/commands", headers={"idempotency-key": str(uuid4())},
        json={"kind": "bulk.apply", "payload": payload, "expected_fingerprint": checked["source_fingerprint"]})
    assert response.status_code == 200, response.text
    result = response.json()
    proposed = result["items"][0]["results"][0]["record"]
    assert proposed["status"] == "proposed"
    assert proposed["definition"]["rule"]["on"] is (path != "policy")
    actual = {row["id"]: row for row in proposed["definition"]["rule"]["lines"]}
    assert all(actual[row["id"]] == row for row in expected)
    assert len(actual) == len(expected) + (path == "add_lines")
    active = approve(h, proposed)
    assert active["definition"]["rule"]["lines"] == proposed["definition"]["rule"]["lines"]
