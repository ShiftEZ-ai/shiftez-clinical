"""GET /api/requests/all: every kind of request in one paged list, scoped per role like each kind's own list."""
import json
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity, swap_automation, transition
from .test_staff_swaps import ordered_clock
from .test_backend_scale import measure

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    with ordered_clock(monkeypatch):
        yield


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def both(request):
    """The list's ordering, filters and JSON reads run on both databases."""
    return request.getfixturevalue("h" if request.param == "sqlite" else "pg")


def leave(h, role="staff", person=None, day="2026-10-04", kind="leave"):
    return h.command(role, "request.create", {"person_id": person or h.seed["people"][0], "start": f"{day}T00:00:00+03:00",
                                              "end": f"{day}T23:59:00+03:00", "private_reason": "Family matter", "kind": kind})["record"]


def everything(h, role, **query):
    q = "&".join(f"{k}={v}" for k, v in query.items())
    return h.get(role, "/api/requests/all" + ("?" + q if q else ""))


def test_staff_see_their_own_requests_of_every_kind(h):
    me, other = h.seed["people"][0], h.seed["people"][1]
    leave(h)
    h.command("staff", "availability_pattern.propose", {"person_id": me, "weekdays": [1], "start_time": "13:00", "end_time": "17:00", "starts_on": "2026-10-01"})
    h.command("staff", "wish.create", {"person_id": me, "first_day": "2026-11-10", "last_day": "2026-11-10", "kind": "prefer_off", "points": 2})
    for person in (me, other):
        h.command("coordinator", "availability.record", {"person_id": person, "start": "2026-11-01T00:00:00+03:00", "end": "2026-11-02T00:00:00+03:00",
                                                         "private_reason": "Recorded by the coordinator", "kind": "off_duty"})
    leave(h, "coordinator", other)
    mine = everything(h, "staff")
    assert {i["person_id"] for i in mine["items"]} == {me}
    assert sorted((i["type"], i["source"], i["stage"]) for i in mine["items"]) == [
        ("leave", "request", "waiting"), ("weekly", "availability", "approved"), ("weekly", "pattern", "waiting"), ("wish", "wish", "open")]
    assert all("private_reason" not in i for i in mine["items"] if i["source"] == "availability")
    assert mine["wishes"]["used"] == 0 and mine["wishes"]["budget_points"] > 0  # today's period; the wish is in November
    team = everything(h, "coordinator")
    assert {i["person_id"] for i in team["items"]} == {me, other} and team["total"] == 6
    assert everything(h, "approver")["total"] == 6


@pytest.mark.parametrize("role", ["credentialer", "operator"])
def test_other_roles_have_no_requests_list(h, role):
    h.get(role, "/api/requests/all", status=403)


@pytest.mark.parametrize("role", ["auditor", "programme_lead"])
def test_fact_readers_do_not_gain_access_to_private_requests(h, role):
    if role == "programme_lead":
        identity(h, role, [role])
    row = leave(h)
    assert everything(h, role)["items"] == []
    h.get(role, "/api/requests/all?id=" + row["id"], status=404)


def test_waiting_first_filters_and_paging(both):
    h = both
    people = h.seed["people"]
    made = [leave(h, "coordinator", people[i % 3], f"2026-10-{10 + i:02d}") for i in range(9)]
    for r in made[:4]:
        h.command("approver", "request.decide", {"decision": "approve", "reason": "Cover arranged"}, r["id"], r["revision"])
    leave(h, "coordinator", people[1], "2026-10-30", "meeting")
    full = everything(h, "approver", limit=200)["items"]
    stages = [i["stage"] for i in full]
    assert stages == sorted(stages, key=lambda s: s != "waiting")  # waiting first
    waiting = [i["start"] for i in full if i["stage"] == "waiting"]
    assert waiting == sorted(waiting)  # soonest first
    rest = [i["start"] for i in full if i["stage"] != "waiting"]
    assert rest == sorted(rest, reverse=True)  # newest first
    # Pages add up to the whole list, without gaps or repeats.
    pages = [everything(h, "approver", limit=3, offset=o) for o in range(0, 12, 3)]
    assert [i["id"] for p in pages for i in p["items"]] == [i["id"] for i in full]
    assert pages[0]["total"] == 10 and pages[0]["has_more"] and not pages[3]["has_more"]
    assert {i["stage"] for i in everything(h, "approver", status="approved")["items"]} == {"approved"}
    assert len(everything(h, "approver", status="waiting")["items"]) == 6
    assert [i["kind"] for i in everything(h, "approver", type="other")["items"]] == ["meeting"]
    assert len(everything(h, "approver", type="leave,other", status="waiting,approved")["items"]) == 10
    assert everything(h, "approver", type="swap")["items"] == []
    assert {i["person_id"] for i in everything(h, "coordinator", person_id=people[1])["items"]} == {people[1]}
    # Staff cannot widen their list with a person filter.
    assert {i["person_id"] for i in everything(h, "staff", person_id=people[1])["items"]} <= {people[0]}
    h.get("approver", "/api/requests/all?status=submitted", status=422)
    h.get("approver", "/api/requests/all?limit=500", status=422)
    # What waits on the approver: the waiting requests (the dot on Requests).
    assert everything(h, "approver")["counts"]["to_decide"] == 6
    # Each stage's count (the views picker), whatever stage is listed; a type filter narrows them.
    stages = everything(h, "approver", status="approved")["counts"]["stages"]
    assert stages["waiting"] == 6 and stages["approved"] == 4 and sum(stages.values()) == 10
    assert everything(h, "approver", type="other")["counts"]["stages"]["waiting"] == 1


def test_impact_names_the_published_duties_a_request_opens(both):
    h = both
    me = h.seed["people"][0]
    w = h.shift(h.workspace())["workspace"]  # 4 Oct 08:00-12:00, staff's person
    h.publish(w)
    r = leave(h, day="2026-10-04")
    item = next(i for i in everything(h, "approver")["items"] if i["id"] == r["id"])
    assert item["impact_count"] == 1 and item["impact"][0]["day"] == "2026-10-04" and item["impact"][0]["publication_id"]
    elsewhere = leave(h, day="2026-10-08")
    assert next(i for i in everything(h, "staff")["items"] if i["id"] == elsewhere["id"])["impact_count"] == 0
    # A weekly pattern on Sundays (weekday 6) overlaps the same duty.
    p = h.command("staff", "availability_pattern.propose", {"person_id": me, "weekdays": [6], "start_time": "07:00", "end_time": "13:00", "starts_on": "2026-09-01"})["pattern"]
    assert next(i for i in everything(h, "coordinator", type="weekly")["items"] if i["id"] == p["id"])["impact_count"] == 1


def test_one_request_with_its_history(h):
    r = leave(h)
    h.command("approver", "request.decide", {"decision": "approve", "reason": "Cover arranged with the team"}, r["id"], r["revision"])
    detail = everything(h, "staff", id=r["id"])
    assert detail["item"]["stage"] == "approved"
    assert [e["action"] for e in detail["history"]] == ["created", "request.decide"]
    assert detail["history"][1]["reason"] == "Cover arranged with the team"
    record = everything(h, "coordinator", id=r["id"])["item"]
    assert record["record_id"] and record["record_retracted"] is False  # the approved leave, which a coordinator may retract
    other = leave(h, "coordinator", h.seed["people"][1])
    h.get("staff", "/api/requests/all?id=" + other["id"], status=404)


def test_staff_follow_their_linked_rota_without_its_identifier(h):
    r = leave(h, kind="meeting")
    r = h.command("approver", "request.decide", {"decision": "approve", "reason": "Meeting reviewed"}, r["id"], r["revision"])["record"]
    w = h.workspace()
    h.command("coordinator", "request.link_rota", {"workspace_id": w["id"], "reason": "Meeting in this rota"}, r["id"], r["revision"])
    for role in ("staff", "coordinator"):
        listed = next(i for i in everything(h, role)["items"] if i["id"] == r["id"])
        detail = everything(h, role, id=r["id"])["item"]
        assert listed["rota_status"] == detail["rota_status"] == "draft"
        assert ("workspace_id" in listed) == ("workspace_id" in detail) == (role == "coordinator")


def test_swaps_are_listed_with_their_stage_and_history(both):
    h = both
    swap_automation(h, prepare=False, publish=False)
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    w = h.shift(h.workspace())["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    pid = h.publish(w)["publication_id"]
    first, second = [s["participants"][0]["id"] for s in h.get("coordinator", "/api/publications/" + pid)["snapshot"]["shifts"]]
    row = h.command("staff", "swap.offer", {"publication_id": pid, "assignment_id": first, "recipient_person_id": h.seed["people"][1],
                                            "expires_at": (NOW + timedelta(days=1)).isoformat(), "reason": "Exchange request"})["swap"]
    assert everything(h, "staff_two")["counts"]["to_decide"] == 1  # their reply is awaited
    item = everything(h, "staff", type="swap")["items"][0]
    assert item["stage"] == "reply" and item["effective_status"] == "offered" and item["offered_assignment"]["start"] and item["recipient_name"]
    assert everything(h, "staff", status="reply")["items"][0]["id"] == row["id"]
    assert everything(h, "staff", status="waiting")["items"] == []
    assert everything(h, "coordinator")["counts"]["stages"]["reply"] == 1
    assert everything(h, "coordinator")["counts"]["to_decide"] == 0
    row = transition(h, row, "counteroffer", "staff_two", {"assignment_id": second, "reason": "This one in return"})["swap"]
    returned = everything(h, "staff", id=row["id"])["item"]
    assert returned["stage"] == "reply" and returned["effective_status"] == "counteroffered" and returned["proposer_name"]
    assert everything(h, "staff")["counts"]["to_decide"] == 1
    row = transition(h, row, "confirm")["swap"]
    listed = everything(h, "coordinator", status="accepted")["items"]
    assert [i["id"] for i in listed] == [row["id"]] and everything(h, "coordinator")["counts"]["to_decide"] == 1
    assert [e["action"] for e in everything(h, "staff", id=row["id"])["history"]] == ["swap.offered", "swap.counteroffer", "swap.confirm"]


@pytest.fixture(scope="module", params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def department_requests_page(request, scale_harness):
    """Years of history for ~150 people: the first page reads only what it shows."""
    h = scale_harness(request.param)
    with pytest.MonkeyPatch.context() as monkeypatch, ordered_clock(monkeypatch):
        people = h.seed["people"]
        with transaction(h.app.state.engine, write=True) as db:
            org = h.seed["organisation_id"]
            made = [m.Person(organisation_id=org, display_name=f"Scale person {n}") for n in range(150 - len(people))]
            db.add_all(made); db.flush()
            everyone = people + [p.id for p in made]
            creator = h.seed["identities"]["coordinator"]
            start = datetime(2023, 1, 1, tzinfo=timezone.utc)
            for n, pid in enumerate(everyone):
                for k in range(30):  # ~10 requests a year for three years
                    at = start + timedelta(days=37 * k + n % 30)
                    db.add(m.RequestRecord(department_id=h.seed["department_id"], person_id=pid, kind="leave", start=at, end=at + timedelta(days=1),
                                           private_reason="Scale fixture", status="submitted" if k == 29 and n % 5 == 0 else "approved", created_by=creator))
                for k in range(40):
                    day = date(2024, 1, 1) + timedelta(days=27 * k + n % 20)
                    db.add(m.StaffWish(department_id=h.seed["department_id"], person_id=pid, first_day=day, last_day=day, kind="prefer_off",
                                       points=1, created_by=creator, outcome="granted" if day < date(2026, 9, 1) else None))
        began, wall_started = time.process_time(), time.perf_counter()
        page, metrics = measure(h, "requests-page", lambda: h.client("approver").get("/api/requests/all"))
        elapsed = time.process_time() - began
        wall_elapsed = time.perf_counter() - wall_started
        body = page.json()
        size = len(page.content)
        # The old page read every request (capped at 200, so history was cut off) plus every list beside it.
        before = [h.client("approver").get(p) for p in ("/api/requests", "/api/availability", "/api/availability-patterns", "/api/swaps?limit=100&offset=0",
                                                         "/api/wishes")]
        return page, body, size, before, elapsed, wall_elapsed, metrics


def test_department_scale_page_is_fast_and_small(department_requests_page, capsys):
    page, body, size, before, elapsed, _, metrics = department_requests_page
    assert page.status_code == 200
    assert body["total"] == 150 * 70 and len(body["items"]) == 100 and body["items"][0]["stage"] == "waiting"
    with capsys.disabled():
        print(f"\n/api/requests/all first page: {elapsed * 1000:.0f} ms, {size / 1024:.0f} KB, {body['total']} rows in all;"
              f" before: {len(before)} requests, {sum(len(r.content) for r in before) / 1024:.0f} KB, /api/requests showed {len(before[0].json()['requests'])} of 4500")
    assert size < 200_000, size
    assert len(json.dumps(body["items"][0])) < 2000
    assert metrics["sql"].get("SELECT", 0) <= 60, metrics


@pytest.mark.cpu_budget
def test_department_scale_page_is_fast_and_small_cpu_budget(department_requests_page):
    _, _, _, _, elapsed, _, _ = department_requests_page
    import coverage
    budget = 10 if coverage.Coverage.current() else 2
    assert elapsed < budget, (elapsed, budget)


@pytest.mark.wall_benchmark
def test_department_scale_request_page_wall_benchmark(department_requests_page):
    _, _, _, _, elapsed, _, _ = department_requests_page
    import coverage
    budget = 10 if coverage.Coverage.current() else 2
    assert elapsed < budget, (elapsed, budget)


UNDO = {"reason": "Undo"}


def refused(h, role, kind, row):
    reply = h.client(role).post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())},
                                json={"kind": kind, "payload": UNDO, "target_id": row["id"], "expected_revision": row["revision"]})
    assert reply.status_code == 409, reply.text
    return reply.json()["error"]["code"]


def test_request_decisions_can_be_undone_while_nothing_acts_on_them(both):
    h = both
    identity(h, "approver_two", ["approver"])
    r = leave(h)
    r = h.command("approver", "request.decide", {"decision": "approve", "reason": "Cover arranged"}, r["id"], r["revision"])["record"]
    assert everything(h, "coordinator", id=r["id"])["item"]["record_retracted"] is False
    # Only whoever decided may undo, at the current revision.
    h.command("approver_two", "request.undo", UNDO, r["id"], r["revision"], status=403)
    h.command("approver", "request.undo", UNDO, r["id"], r["revision"] - 1, status=409)
    r = h.command("approver", "request.undo", UNDO, r["id"], r["revision"])["record"]
    assert r["status"] == "submitted" and r["decided_by"] is None
    # The approved leave's absence is retracted with it: rota checks no longer count it.
    assert all(a["retracted"] for a in h.get("coordinator", "/api/availability")["availability"] if a["request_id"] == r["id"])
    assert refused(h, "approver", "request.undo", r) == "WORKFLOW_STATE"  # already waiting
    assert [e["action"] for e in everything(h, "staff", id=r["id"])["history"]] == ["created", "request.decide", "request.undo"]
    # The refused attempts changed nothing, so they are not in the request's history; the audit trail keeps them.
    with transaction(h.app.state.engine) as db:
        assert db.scalars(select(m.AuditEvent.id).where(m.AuditEvent.target_id == r["id"], m.AuditEvent.action == "command.refused")).first()
    # Decided again; once linked to a rota the decision is no longer undone (a rota may act on it).
    r = h.command("approver", "request.decide", {"decision": "approve", "reason": "Cover arranged again"}, r["id"], r["revision"])["record"]
    assert everything(h, "coordinator", id=r["id"])["item"]["record_retracted"] is False  # the current record, not the retracted one
    r = h.command("coordinator", "request.link_rota", {"workspace_id": h.workspace()["id"], "reason": "Handled in this rota"}, r["id"], r["revision"])["record"]
    assert refused(h, "approver", "request.undo", r) == "REQUEST_UNDO_LINKED"
    # A withdrawal is undone by whoever withdrew.
    mine = leave(h, day="2026-10-09")
    mine = h.command("staff", "request.decide", {"decision": "withdraw", "reason": "Plans changed"}, mine["id"], mine["revision"])["record"]
    assert h.command("staff", "request.undo", UNDO, mine["id"], mine["revision"])["record"]["status"] == "submitted"


def test_weekly_availability_decisions_can_be_undone(h):
    p = h.command("staff", "availability_pattern.propose", {"person_id": h.seed["people"][0], "weekdays": [1], "start_time": "13:00",
                                                             "end_time": "17:00", "starts_on": "2026-10-01"})["pattern"]
    p = h.command("coordinator", "availability_pattern.decide", {"decision": "approve", "reason": "Fine"}, p["id"], p["revision"])["pattern"]
    p = h.command("coordinator", "availability_pattern.undo", UNDO, p["id"], p["revision"])["pattern"]
    assert p["status"] == "proposed"
    assert refused(h, "coordinator", "availability_pattern.undo", p) == "WORKFLOW_STATE"


def swap_fixture(h):
    swap_automation(h, prepare=False, publish=False)
    identity(h, "staff_two", ["staff"], h.seed["people"][1])
    w = h.shift(h.workspace())["workspace"]
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    pid = h.publish(w)["publication_id"]
    first, second = [s["participants"][0]["id"] for s in h.get("coordinator", "/api/publications/" + pid)["snapshot"]["shifts"]]
    row = h.command("staff", "swap.offer", {"publication_id": pid, "assignment_id": first, "recipient_person_id": h.seed["people"][1],
                                            "expires_at": (NOW + timedelta(days=1)).isoformat(), "reason": "Exchange request"})["swap"]
    return pid, second, row


def swap_step(h, role, kind, row, payload=None):
    return h.command(role, "swap." + kind, payload or {"reason": "Explicit decision"}, row["id"], row["revision"])["swap"]


def test_swap_steps_are_undone_forward_as_a_reopened_swap(both):
    h = both
    pid, second, row = swap_fixture(h)
    # Offer in return, undone by the recipient: that swap closes and a new one is open at the offer, linked to it.
    countered = swap_step(h, "staff_two", "counteroffer", row, {"assignment_id": second, "reason": "This one"})
    assert refused(h, "staff", "swap.undo", countered) == "SWAP_UNDO_UNAVAILABLE"  # not the proposer's step
    h.command("staff_two", "swap.undo", UNDO, countered["id"], countered["revision"] - 1, status=409)  # stale revision
    again = swap_step(h, "staff_two", "undo", countered, UNDO)
    assert again["id"] != row["id"] and again["status"] == "offered" and again["second_assignment_id"] is None
    assert again["reopened_from_id"] == row["id"]
    assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["status"] == "withdrawn"
    listed = everything(h, "staff", type="swap")["items"]
    assert [i["id"] for i in listed] == [again["id"]]  # one thread, its newest row
    history = [e["action"] for e in everything(h, "staff", id=again["id"])["history"]]
    assert history == ["swap.offered", "swap.counteroffer", "swap.undo", "swap.reopened"]
    # An immediate Undo retracts the pending step; its forward-only consent history remains above.
    with transaction(h.app.state.engine) as db:
        notices = [o.state for o in db.query(m.Outbox).filter(m.Outbox.event_key.like(f"swap:{again['id']}:%"))]
        original = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:{countered['revision']}:counteroffer"))
    assert notices == ['superseded'] and original.state == 'superseded'
    # Confirm undone: open again at the offer in return. A decline undone: open again where it was.
    row = swap_step(h, "staff_two", "counteroffer", again, {"assignment_id": second, "reason": "This one again"})
    row = swap_step(h, "staff", "confirm", row)
    row = swap_step(h, "staff", "undo", row, UNDO)
    assert row["status"] == "counteroffered" and row["confirmed_at"] is None and row["second_assignment_id"] == second
    row = swap_step(h, "staff", "confirm", row)
    declined = swap_step(h, "coordinator", "reject", row)
    row = swap_step(h, "coordinator", "undo", declined, UNDO)
    assert row["status"] == "accepted" and row["confirmed_at"]
    assert refused(h, "coordinator", "swap.undo", h.get("staff", "/api/swaps/" + declined["id"])["swap"]) == "SWAP_UNDO_UNAVAILABLE"  # once
    # Prepared into a rota change: no undo (withdraw or decline it instead).
    row = swap_step(h, "coordinator", "prepare", row, {"urgency": "routine", "reason": "Both agreed"})
    row = swap_step(h, "staff", "withdraw", row)
    assert refused(h, "staff", "swap.undo", row) == "SWAP_UNDO_UNAVAILABLE"
    # The consent guard still refuses moving any swap backwards directly.
    from sqlalchemy import text
    with pytest.raises(Exception):
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(text("UPDATE swap_requests SET status='offered' WHERE id=:id"), {"id": row["id"]})


def test_a_swap_undo_needs_the_duty_still_on_the_rota(h):
    pid, _, row = swap_fixture(h)
    declined = swap_step(h, "coordinator", "reject", row)
    # A later version of the rota replaces the one the offer was made on: the offer cannot reopen.
    h.publish(h.workspace("Replacement", base=pid))
    assert refused(h, "coordinator", "swap.undo", declined).startswith("SWAP_")


def test_wishes_and_weekly_availability_move_the_live_pulse(h):
    before = h.get("coordinator", "/api/pulse")["activity"]
    h.command("staff", "wish.create", {"person_id": h.seed["people"][0], "first_day": "2026-11-10", "last_day": "2026-11-10", "kind": "prefer_off", "points": 1})
    after_wish = h.get("coordinator", "/api/pulse")["activity"]
    h.command("staff", "availability_pattern.propose", {"person_id": h.seed["people"][0], "weekdays": [2], "start_time": "08:00",
                                                         "end_time": "10:00", "starts_on": "2026-10-01"})
    assert before != after_wish != h.get("coordinator", "/api/pulse")["activity"]


def test_requests_revalidation_is_revision_backed(h):
    client = h.client("approver")
    first = client.get("/api/requests/all")
    assert first.status_code == 200 and first.headers.get("etag")
    repeat = client.get("/api/requests/all", headers={"if-none-match": first.headers["etag"]})
    assert repeat.status_code == 304 and repeat.content == b""
    leave(h, "staff")
    changed = client.get("/api/requests/all", headers={"if-none-match": first.headers["etag"]})
    assert changed.status_code == 200 and changed.headers["etag"] != first.headers["etag"]
