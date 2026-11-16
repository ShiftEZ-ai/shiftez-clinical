"""The Requests ``for_me`` view is the same queue as the waiting-for-you count."""
from datetime import datetime, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction

from .test_scheduling_facts_api import workload
from .test_staff_swaps import accepted, ordered_clock


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    with ordered_clock(monkeypatch):
        yield


def read(h, role, **params):
    response = h.client(role).get("/api/requests/all", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def submit(h, role, person, day, kind="leave"):
    return h.command(role, "request.create", {
        "person_id": person,
        "start": f"2026-10-{day:02d}T00:00:00+03:00",
        "end": f"2026-10-{day:02d}T23:59:00+03:00",
        "private_reason": "Synthetic request for QA-039",
        "kind": kind,
    })["record"]


def test_for_me_is_the_count_queue_with_paging_stage_counts_and_exclusions(h):
    me, other = h.seed["people"][:2]
    submitted = [submit(h, "staff", me, 10), submit(h, "coordinator", other, 11)]
    pattern = h.command("staff", "availability_pattern.propose", {
        "person_id": me, "weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01",
    })["pattern"]
    own_pattern = h.command("coordinator", "availability_pattern.propose", {
        "person_id": other, "weekdays": [2], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01",
    })["pattern"]
    fact = h.command("coordinator", "person.workload.propose", workload(h))["fact"]
    with transaction(h.app.state.engine, write=True) as db:
        excluded = m.RequestRecord(department_id=h.seed["department_id"], person_id=me, kind="leave",
            start=datetime(2026, 10, 12, tzinfo=timezone.utc), end=datetime(2026, 10, 13, tzinfo=timezone.utc),
            private_reason="Synthetic self-authored request", status="submitted",
            created_by=h.seed["identities"]["approver"])
        db.add(excluded)

    approver = read(h, "approver", for_me=1, limit=1)
    expected_approver = {submitted[0]["id"], submitted[1]["id"], fact["id"]}
    assert {item["id"] for item in approver["items"]} <= expected_approver
    assert approver["total"] == approver["counts"]["to_decide"] == 3
    assert approver["counts"]["stages"]["waiting"] == 3
    assert sum(approver["counts"]["stages"].values()) == 3
    pages = [read(h, "approver", for_me=1, limit=1, offset=offset) for offset in range(3)]
    assert {item["id"] for page in pages for item in page["items"]} == expected_approver
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert all(page["counts"]["stages"]["waiting"] == 3 for page in pages)
    assert excluded.id not in {item["id"] for item in read(h, "approver", for_me=1, limit=100)["items"]}

    accepted_swap, _ = accepted(h)
    coordinator = read(h, "coordinator", for_me=1, limit=1)
    expected_coordinator = {pattern["id"], accepted_swap["id"]}
    assert coordinator["total"] == coordinator["counts"]["to_decide"] == 2
    assert {item["id"] for item in coordinator["items"]} <= expected_coordinator
    assert coordinator["counts"]["stages"]["waiting"] == 1
    assert coordinator["counts"]["stages"]["accepted"] == 1
    assert own_pattern["id"] not in {item["id"] for item in read(h, "coordinator", for_me=1, limit=100)["items"]}

    all_requests = read(h, "approver")
    assert all_requests["total"] > all_requests["counts"]["to_decide"]
    assert read(h, "approver", for_me=0)["items"] == all_requests["items"]
    h.get("operator", "/api/requests/all?for_me=1", status=403)
