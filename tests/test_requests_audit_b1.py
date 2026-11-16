"""Operator audit: requests are staged before paging, disclosed by policy, and show scoped rota impact."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_leave_disclosure import setting
from .test_requests_unified import both, everything, fixed_clock, leave  # noqa: F401


def test_default_stage_order_applies_before_paging_across_every_source(both):
    h = both
    me, other = h.seed["people"][:2]
    identity(h, "staff_two", ["staff"], other)
    workspace = h.shift(h.workspace())["workspace"]
    publication = h.publish(workspace)["publication_id"]
    assignment = h.get("coordinator", "/api/publications/" + publication)["snapshot"]["shifts"][0]["participants"][0]["id"]
    with transaction(h.app.state.engine, write=True) as db:
        base = {"department_id": h.seed["department_id"], "person_id": me}
        creator = h.seed["identities"]["coordinator"]
        for n in range(3):
            start = datetime(2026, 10, 4 + n, tzinfo=timezone.utc)
            for status in ("submitted", "approved", "rejected", "withdrawn"):
                db.add(m.RequestRecord(**base, start=start, end=start + timedelta(days=1), kind="leave",
                                       private_reason="Stage fixture", created_by=creator, status=status))
            for retracted in (False, True):
                db.add(m.Availability(**base, start=start, end=start + timedelta(days=1), kind="recorded_leave",
                                      private_reason="Stage fixture", recorded_by=creator, retracted=retracted))
            for status in ("proposed", "active", "declined", "ended"):
                db.add(m.AvailabilityPattern(**base, starts_on=date(2026, 10, 4 + n), weekdays=[0],
                                            start_time="13:00", end_time="17:00", created_by=creator, status=status))
            for status, outcome in (("open", None), ("open", "granted"), ("open", "partly"), ("open", "declined"), ("withdrawn", None)):
                db.add(m.StaffWish(**base, first_day=start.date(), last_day=start.date(), kind="prefer_off", points=1,
                                  created_by=creator, status=status, outcome=outcome))
        for status in ("offered", "counteroffered", "accepted", "reviewing", "published", "rejected", "withdrawn"):
            db.add(m.SwapRequest(department_id=h.seed["department_id"], publication_id=publication,
                                proposer_id=h.seed["identities"]["staff"], proposer_person_id=me,
                                recipient_id=h.seed["identities"]["staff_two"], recipient_person_id=other,
                                first_assignment_id=assignment, expires_at=m.now() + timedelta(days=1),
                                status=status, reason="Stage fixture"))
        db.add(m.SwapRequest(department_id=h.seed["department_id"], publication_id=publication,
                            proposer_id=h.seed["identities"]["staff"], proposer_person_id=me,
                            recipient_id=h.seed["identities"]["staff_two"], recipient_person_id=other,
                            first_assignment_id=assignment, expires_at=m.now() - timedelta(days=1),
                            status="offered", reason="Expired fixture"))
    items = everything(h, "approver", limit=200)["items"]
    stages = [item["stage"] for item in items]
    assert list(dict.fromkeys(stages)) == ["waiting", "reply", "accepted", "open", "approved", "declined", "withdrawn", "expired"]
    assert {item["source"] for item in items} == {"request", "availability", "pattern", "wish", "swap"}
    for stage in ("waiting", "approved", "withdrawn"):
        dates = [item["start"] for item in items if item["source"] == "request" and item["stage"] == stage]
        assert dates == sorted(dates, reverse=stage != "waiting")
    pages = [everything(h, "approver", limit=7, offset=offset) for offset in range(0, len(items), 7)]
    assert [item["id"] for page in pages for item in page["items"]] == [item["id"] for item in items]
    assert sum(pages[0]["counts"]["stages"].values()) == len(items)
    legacy = h.get("approver", "/api/requests")["requests"]
    assert list(dict.fromkeys(item["status"] for item in legacy)) == ["submitted", "approved", "rejected", "withdrawn"]


@pytest.mark.parametrize("mode", ["none", "type", "details"])
def test_recorded_details_and_search_honor_department_policy_and_staff_privacy(both, mode):
    h = both
    setting(h, mode)
    row = h.command("coordinator", "availability.record", {"person_id": h.seed["people"][0], "kind": "recorded_leave",
        "leave_type": "Annual leave", "start": "2026-10-04T00:00:00+03:00", "end": "2026-10-05T00:00:00+03:00",
        "private_reason": "Recorded confidential needle"})["record"]
    request = leave(h)
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=h.seed["organisation_id"], name="Other department", leave_reason_display="details")
        db.add(foreign)
        db.flush()
        db.add(m.Availability(department_id=foreign.id, person_id=h.seed["people"][0], kind="recorded_leave",
                              start=datetime(2026, 10, 4, tzinfo=timezone.utc), end=datetime(2026, 10, 5, tzinfo=timezone.utc),
                              private_reason="Foreign confidential needle", recorded_by=h.seed["identities"]["coordinator"]))
    for role in ("admin", "coordinator", "approver", "staff"):
        visible = mode == "details" and role != "staff"
        listed = next(item for item in everything(h, role)["items"] if item["id"] == row["id"])
        detail = everything(h, role, id=row["id"])["item"]
        assert ("private_reason" in listed) == ("private_reason" in detail) == visible
        assert detail["leave_type"] == "Annual leave"
        if visible:
            assert listed["private_reason"] == detail["private_reason"] == "Recorded confidential needle"
        searched = h.client(role).get("/api/requests/all", params={"q": "Recorded confidential needle"}).json()
        assert searched["total"] == len(searched["items"]) == sum(searched["counts"]["stages"].values()) == 0
        assert "Foreign confidential needle" not in json.dumps(everything(h, role))
        # Explicit requests keep their existing reviewer/own-request reason access in every display mode.
        assert everything(h, role, id=request["id"])["item"]["private_reason"] == "Family matter"


@pytest.mark.parametrize("state", ["draft", "submitted", "approved"])
def test_request_detail_includes_review_rota_impact_only_for_team(both, state):
    h = both
    published = h.shift(h.workspace())["workspace"]
    publication = h.publish(published)["publication_id"]
    draft = h.shift(h.workspace("Next rota"), "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00")["workspace"]
    if state in ("submitted", "approved"):
        draft = h.lifecycle(draft, "submit")["workspace"]
    if state == "approved":
        draft = h.lifecycle(draft, "approve", "approver")["workspace"]
    request = h.command("staff", "request.create", {"person_id": h.seed["people"][0], "kind": "leave",
        "start": "2026-10-04T00:00:00+03:00", "end": "2026-10-06T00:00:00+03:00", "private_reason": "Planned leave"})["record"]
    for role in ("admin", "coordinator", "approver", "staff"):
        item = everything(h, role, id=request["id"])["item"]
        assert item["impact_count"] == 1 and item["impact"][0]["publication_id"] == publication
        impact = item["schedule_impact"]
        assert impact["count"] == (1 if role == "staff" else 2)
        assert impact["published_count"] == 1 and impact["draft_count"] == (0 if role == "staff" else 1)
        assert {duty["status"] for duty in impact["duties"]} == ({"published"} if role == "staff" else {"published", state})
        if role == "staff":
            assert draft["id"] not in json.dumps(item) and all("workspace_id" not in duty for duty in impact["duties"])
        else:
            assert {duty["workspace_id"] for duty in impact["duties"]} == {published["id"], draft["id"]}
    other = leave(h, "coordinator", h.seed["people"][1])
    h.get("staff", "/api/requests/all?id=" + other["id"], status=404)


def test_request_detail_has_explicit_zero_impact_without_a_rota(h):
    request = leave(h)
    for role in ("coordinator", "staff"):
        item = everything(h, role, id=request["id"])["item"]
        assert item["impact"] == [] and item["impact_count"] == 0
        assert item["schedule_impact"] == {"count": 0, "published_count": 0, "draft_count": 0, "duties": []}
