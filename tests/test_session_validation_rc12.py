"""Legacy session snapshots use the contract's duration default on read."""
from datetime import timedelta

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import TypeDefinition
from packages.domain.time import aware
from packages.domain.validation import evaluate


@pytest.mark.parametrize("maximum,minutes,limited", [
    (None, 720, False), (None, 721, True), (180, 240, True),
])
def test_session_duration_uses_default_only_when_missing(h, maximum, minutes, limited):
    workspace = h.shift(h.workspace())["workspace"]
    data = h.snapshot(workspace)
    shift = next(s for s in data["shifts"] if s["id"] in data["focus_ids"])
    definition = shift["type_snapshot"]["definition"]
    if maximum is None:
        definition.pop("maximum_duration_minutes")
    else:
        definition["maximum_duration_minutes"] = maximum
    shift["end"] = (aware(shift["start"]) + timedelta(minutes=minutes)).isoformat()
    for participant in shift["participants"]:
        participant["end"] = shift["end"]
    findings = evaluate(data)["findings"]
    assert any(f["code"] == "DURATION_LIMIT" for f in findings) is limited


def test_policy_activation_tolerates_missing_session_duration_field(h):
    workspace = h.shift(h.workspace())["workspace"]
    shift_id = h.detail(workspace)["shifts"][0]["id"]
    with transaction(h.app.state.engine, write=True) as db:
        shift = db.get(m.Shift, shift_id)
        snapshot = dict(shift.type_snapshot)
        snapshot["definition"] = {k: v for k, v in snapshot["definition"].items()
                                  if k != "maximum_duration_minutes"}
        shift.type_snapshot = snapshot
    policy = h.policy({"kind": "max_hours", "hours": 8, "window": "day"})
    assert policy["status"] == "active"
    assert policy["approved_by"] != policy["created_by"]
    assert h.detail(workspace)["shifts"][0]["type_snapshot"]["definition"].get("maximum_duration_minutes") is None


@pytest.mark.parametrize("maximum", [14, 2161, None])
def test_assignment_type_write_still_validates_duration(h, maximum):
    result = h.configure("assignment_types", {
        "name": "Invalid duration", "starts_on": "2020-01-01",
        "definition": {"maximum_duration_minutes": maximum},
    }, status=422)
    assert "maximum_duration_minutes" in str(result)


def test_assignment_type_write_supplies_contract_duration_default(h):
    result = h.configure("assignment_types", {
        "name": "Default duration", "starts_on": "2020-01-01", "definition": {},
    })
    assert result["record"]["definition"]["maximum_duration_minutes"] == TypeDefinition.model_fields[
        "maximum_duration_minutes"].default
