"""Bulk preferences retain dev's standing/pattern data and canonical evidence privacy."""
from copy import deepcopy
import json
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import StaffWishInput
from .governance_support import identity
from .test_bulk import REASON, preview


def apply_checked(h, role, body, check, *, key=None):
    response = h.client(role).post("/api/commands", headers={"idempotency-key": key or str(uuid.uuid4())},
        json={"kind": "bulk.apply", "payload": body, "expected_fingerprint": check["source_fingerprint"]})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("address", ["items", "selection"])
@pytest.mark.parametrize("fields", [
    pytest.param({"kind": "prefer_off", "repeat": {"weekdays": [0], "starts_on": "2026-10-01",
        "ends_on": "2026-10-31", "number": 2}}, id="standing"),
    pytest.param({"kind": "max_weekends_in_row", "first_day": "2026-10-01", "last_day": "2026-10-31",
        "limit": 2, "weekend_start": 6, "weekend_days": 3}, id="weekend-pattern"),
])
def test_bulk_points_only_edit_and_undo_preserve_standing_and_pattern_fields(request, database, address, fields):
    h = request.getfixturevalue(database)
    saved = h.command("staff", "wish.create", {"person_id": h.seed["people"][0], "points": 1,
        "note": "Synthetic existing preference", **deepcopy(fields)})["wish"]
    body = {"item_kind": "wish.update", "changes": [{"field": "points", "how": "set", "value": 2}],
            "reason": REASON}
    if address == "selection":
        body["selection"] = {"collection": "wishes", "include_ids": [saved["id"]]}
    else:
        body["items"] = [{"target_id": saved["id"], "expected_revision": saved["revision"], "payload": {}}]
    checked = preview(h, "staff", body)
    assert checked["counts"]["ready"] == 1, checked
    assert checked["items"][0]["after"] == {"points": 2}
    assert h.get("staff", "/api/wishes")["wishes"][0]["points"] == 1
    applied = apply_checked(h, "staff", body, checked)
    changed = applied["items"][0]["record"]
    assert changed["points"] == 2
    for field in StaffWishInput.model_fields.keys() - {"points"}:
        assert changed[field] == saved[field]
    inverse = applied["undo"]["payload"]["items"][0]
    assert inverse["payload"] == {field: saved[field] for field in StaffWishInput.model_fields}
    h.command("staff", **applied["undo"])
    restored = h.get("staff", "/api/wishes")["wishes"][0]
    assert {field: restored[field] for field in StaffWishInput.model_fields} == {
        field: saved[field] for field in StaffWishInput.model_fields}
    assert restored["id"] == saved["id"]


def wish_with_evidence(h, role="staff"):
    saved = h.command(role, "wish.create", {"person_id": h.seed["people"][0], "kind": "prefer_on",
        "first_day": "2026-10-05", "last_day": "2026-10-05", "points": 1})["wish"]
    reasons = [
        {"day": "2026-10-05", "kind": "rule", "code": "SYNTHETIC_RULE", "message": "Sensitive rule details",
         "rule_id": str(uuid.uuid4()), "rule_name": "Sensitive rule name"},
        {"day": "2026-10-05", "kind": "competing_wishes", "message": "Sensitive colleague details",
         "competing_wishes": [{"person_id": h.seed["people"][1], "person_name": "Sensitive colleague name"}]},
        {"day": "2026-10-05", "kind": "cover", "message": "Sensitive cover analysis",
         "shift_id": str(uuid.uuid4()), "session_label": "Synthetic duty", "start": "2026-10-05T08:00:00+03:00",
         "end": "2026-10-05T12:00:00+03:00"},
    ]
    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.StaffWish, saved["id"])
        row.outcome, row.outcome_reasons = "declined", reasons
    return saved, reasons


@pytest.mark.parametrize("role", ["staff", "coordinator", "approver", "programme_lead", "auditor"])
@pytest.mark.parametrize("filtered", [False, True])
def test_bulk_resolve_and_rows_match_canonical_preference_evidence_visibility(h, role, filtered):
    if role == "programme_lead":
        identity(h, role, [role])
    saved, reasons = wish_with_evidence(h)
    expected = next(row for row in h.get(role, "/api/wishes")["wishes"] if row["id"] == saved["id"])["outcome_reasons"]
    selection = {"collection": "wishes", "include_ids": [saved["id"]]}
    if filtered:
        selection = {"collection": "wishes", "where": {"filters": {"points": [1]}}}
    response = h.client(role).post("/api/bulk/resolve", json=selection)
    assert response.status_code == 200, response.text
    [resolved] = response.json()["sample"]
    assert resolved["outcome_reasons"] == expected
    for _ in range(2):
        response = h.client(role).post("/api/bulk/rows", json={"selection": selection,
            "columns": ["outcome_reasons"], "offset": 0, "limit": 1})
        assert response.status_code == 200, response.text
        assert response.json()["rows"][0]["outcome_reasons"] == expected
    assert (expected == reasons) is (role != "staff")


@pytest.mark.parametrize("role", ["staff", "coordinator"])
def test_bulk_preview_apply_and_undo_use_canonical_preference_evidence_visibility(h, role):
    saved, _ = wish_with_evidence(h)
    expected = h.get(role, "/api/wishes")["wishes"][0]["outcome_reasons"]
    body = {"item_kind": "wish.update", "items": [{"target_id": saved["id"],
        "expected_revision": saved["revision"], "changes": [{"field": "points", "value": 2}]}], "reason": REASON}
    checked = preview(h, role, body)
    assert checked["counts"]["ready"] == 1, checked
    applied = apply_checked(h, role, body, checked)
    undone = h.command(role, **applied["undo"])
    for result in (checked, applied, undone):
        assert result["items"][0]["record"]["outcome_reasons"] == expected
        assert result["items"][0]["results"][0]["wish"]["outcome_reasons"] == expected
        if role == "staff":
            assert "Sensitive" not in json.dumps(result)


def test_bulk_receipt_reprojects_preference_evidence_after_coordinator_role_is_revoked(h):
    owner = identity(h, "preference_owner", ["staff", "coordinator"], person=h.seed["people"][0])
    saved, reasons = wish_with_evidence(h, "preference_owner")
    body = {"item_kind": "wish.update", "items": [{"target_id": saved["id"],
        "expected_revision": saved["revision"], "changes": [{"field": "points", "value": 2}]}], "reason": REASON}
    checked = preview(h, "preference_owner", body)
    key = str(uuid.uuid4())
    applied = apply_checked(h, "preference_owner", body, checked, key=key)
    assert applied["items"][0]["record"]["outcome_reasons"] == reasons
    with transaction(h.app.state.engine) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == owner,
            m.RoleBinding.department_id == h.seed["department_id"], m.RoleBinding.role == "coordinator"))
        binding_id, revision = binding.id, binding.revision
    h.command("admin", "role.revoke", {"reason": REASON}, binding_id, revision)
    expected = h.get("preference_owner", "/api/wishes")["wishes"][0]["outcome_reasons"]
    replay = apply_checked(h, "preference_owner", body, checked, key=key)
    recovered = h.get("preference_owner", "/api/commands/" + key)["result"]
    for result in (replay, recovered):
        assert result["items"][0]["record"]["outcome_reasons"] == expected
        assert result["items"][0]["results"][0]["wish"]["outcome_reasons"] == expected
        assert "Sensitive" not in json.dumps(result)


def test_standing_override_uses_the_shared_command_permission_map(monkeypatch):
    from apps.api.commands import command_roles
    from packages.contracts import schemas as s
    monkeypatch.setitem(s.PERMISSIONS, "wish.override", ("auditor",))
    assert command_roles(s.Command(kind="wish.override", payload={})) == ["auditor"]
