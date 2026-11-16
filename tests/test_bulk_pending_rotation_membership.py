"""Approving a requested rotation observes current membership intervals in the batch."""
import pytest

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview


@pytest.mark.parametrize("extend", [False, True])
def test_rotation_request_approval_uses_pending_programme_membership_dates(h, extend):
    programme = h.configure("programmes", {"name": "Pending membership programme", "starts_on": "2026-01-01"})["record"]
    cohort = h.configure("cohorts", {"name": "Pending membership cohort", "programme_id": programme["id"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01"})["record"]
    membership = h.configure("programme_memberships", {"person_id": h.seed["people"][0], "programme_id": programme["id"],
        "starts_on": "2026-01-01", "ends_on": "2026-12-01"}, role="coordinator")["record"]
    rotation = h.configure("rotations", {"person_id": h.seed["people"][0], "cohort_id": cohort["id"],
        "service_id": h.seed["services"][0], "starts_on": "2026-10-01", "ends_on": "2026-11-01"}, role="coordinator")["record"]
    request = h.command("staff", "rotation.request_change", {"rotation_id": rotation["id"],
        "service_id": h.seed["services"][0], "starts_on": "2026-10-01", "ends_on": "2026-11-02", "private_reason": REASON})["record"]
    fields = {key: membership[key] for key in ("person_id", "programme_id", "starts_on", "ends_on")}
    if extend:
        membership = h.configure("programme_memberships", {**fields, "ends_on": "2026-10-15"},
            membership["id"], membership["revision"], role="coordinator")["record"]
    new_end = "2026-12-01" if extend else "2026-10-15"
    value = {"items": [
        {"item_kind": "catalogue.update", "target_id": membership["id"], "expected_revision": membership["revision"],
         "payload": {"catalogue": "programme_memberships", "value": {**fields, "ends_on": new_end}}},
        {"item_kind": "request.decide", "target_id": request["id"], "expected_revision": request["revision"],
         "payload": {"decision": "approve"}},
    ], "reason": REASON}
    checked = preview(h, "admin", value)
    if extend:
        assert checked["counts"]["problem"] == 0
        h.command("admin", "bulk.apply", value)
    else:
        assert checked["items"][1]["issues"][0]["code"] == "ROTATION_MEMBERSHIP"
        refused = h.command("admin", "bulk.apply", value, status=409)["error"]
        assert refused["code"] == "ROTATION_MEMBERSHIP" and refused["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ProgrammeMembership, membership["id"]).ends_on.isoformat() == "2026-12-01"
        assert db.get(m.RequestRecord, request["id"]).status == ("approved" if extend else "submitted")
        assert db.get(m.Rotation, rotation["id"]).ends_on.isoformat() == ("2026-11-02" if extend else "2026-11-01")
