"""Programme actions and director decisions use preceding membership edits."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_residency import setup_residency


@pytest.mark.parametrize("extend", [False, True])
@pytest.mark.parametrize("stale_roster", [False, True])
def test_programme_eligibility_rechecks_pending_membership_roster(h, extend, stale_roster):
    programme = h.configure("programmes", {"name": "Pending permission roster", "starts_on": "2026-01-01"})["record"]
    service = h.configure("services", {"name": "Pending roster clinic", "starts_on": "2026-01-01"})["record"]
    person, stable_person = h.seed["people"][:2]
    old_end, new_end = ("2026-09-01", "2026-12-01") if extend else ("2026-12-01", "2026-09-01")
    membership = h.configure("programme_memberships", {"person_id": person, "programme_id": programme["id"],
        "starts_on": "2026-01-01", "ends_on": old_end}, role="coordinator")["record"]
    h.configure("programme_memberships", {"person_id": stable_person, "programme_id": programme["id"],
        "starts_on": "2026-01-01", "ends_on": "2026-12-01"}, role="coordinator")
    included = not extend if stale_roster else extend
    roster = [stable_person, person] if included else [stable_person]
    value = {"items": [
        {"item_kind": "catalogue.update", "target_id": membership["id"], "expected_revision": membership["revision"],
         "payload": {"catalogue": "programme_memberships", "value": {"person_id": person,
            "programme_id": programme["id"], "starts_on": "2026-01-01", "ends_on": new_end}}},
        {"item_kind": "eligibility.programme", "payload": {"programme_id": programme["id"], "person_ids": roster,
            "service_ids": [service["id"]], "starts_on": "2026-10-01", "ends_on": "2026-11-01"}},
    ], "reason": REASON}
    checked = preview(h, "admin", value)
    if stale_roster:
        assert checked["items"][1]["issues"][0]["code"] == "PROGRAMME_ROSTER_CHANGED"
        refused = h.command("admin", "bulk.apply", value, status=409)["error"]
        assert refused["code"] == "PROGRAMME_ROSTER_CHANGED" and refused["details"]["failed_index"] == 1
    else:
        assert checked["counts"]["problem"] == 0
        h.command("admin", "bulk.apply", value)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ProgrammeMembership, membership["id"]).ends_on.isoformat() == (old_end if stale_roster else new_end)
        actual = set(db.scalars(select(m.Eligibility.person_id).where(m.Eligibility.service_id == service["id"])))
        assert actual == (set() if stale_roster else set(roster))


def test_programme_lead_request_decision_rechecks_pending_trainee_membership(h):
    h, programme, _, _, people, _ = setup_residency(h)
    person = people[0]
    membership = next(row for row in h.get("programme_lead", "/api/catalogues")["programme_memberships"]
        if row["person_id"] == person and row["programme_id"] == programme["id"])
    request = h.command("staff", "request.create", {"person_id": person, "kind": "leave",
        "start": "2026-10-05T08:00:00+03:00", "end": "2026-10-05T12:00:00+03:00",
        "private_reason": "Synthetic roster decision"})["record"]
    value = {"items": [
        {"item_kind": "catalogue.update", "target_id": membership["id"], "expected_revision": membership["revision"],
         "payload": {"catalogue": "programme_memberships", "value": {"person_id": person,
            "programme_id": programme["id"], "starts_on": membership["starts_on"], "ends_on": "2026-10-04"}}},
        {"item_kind": "request.decide", "target_id": request["id"], "expected_revision": request["revision"],
         "payload": {"decision": "approve"}},
    ], "reason": REASON}
    checked = preview(h, "programme_lead", value)
    assert checked["items"][1]["issues"][0]["code"] == "FORBIDDEN"
    refused = h.command("programme_lead", "bulk.apply", value, status=409)["error"]
    assert refused["code"] == "FORBIDDEN" and refused["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.ProgrammeMembership, membership["id"]).ends_on.isoformat() == membership["ends_on"]
        assert db.get(m.RequestRecord, request["id"]).status == "submitted"
        assert db.scalar(select(m.Availability.id).where(m.Availability.request_id == request["id"])) is None
