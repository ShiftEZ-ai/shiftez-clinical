"""Open offers and Requests reads stay interactive at department scale."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.services import swaps
from .test_backend_scale import measure
from .test_performance_budget import python_budget


def scale_publication(h):
    """150 current staff, each with ten future duties; fixture construction is outside the budget."""
    workspace = h.workspace("Open swap scale fixture", end="2026-10-14")
    with transaction(h.app.state.engine, write=True) as db:
        made = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Scale clinician {n:03d}")
                for n in range(150 - len(h.seed["people"]))]
        db.add_all(made)
        db.flush()
        people = [*h.seed["people"], *(person.id for person in made)]
        password_hash = db.get(m.Identity, h.seed["identities"]["staff"]).password_hash
        for n, person_id in enumerate(people[1:], 1):
            username = "staff_two" if n == 1 else "staff_three" if n == 2 else f"scale_staff_{n:03d}"
            identity = m.Identity(id=m.uid(), issuer="local-development", subject=username,
                display_name=f"Synthetic scale staff {n}", person_id=person_id, password_hash=password_hash)
            db.add(identity)
            h.seed["identities"][username] = identity.id
            db.add(m.RoleBinding(identity_id=identity.id, department_id=h.seed["department_id"],
                role="staff", starts_on=date(2020, 1, 1)))
        for person in made:
            db.add(m.Membership(person_id=person.id, category_id=h.seed["category"],
                department_id=h.seed["department_id"], starts_on=date(2020, 1, 1)))
            for service_id in h.seed["services"]:
                db.add(m.Eligibility(person_id=person.id, service_id=service_id,
                    department_id=h.seed["department_id"], starts_on=date(2020, 1, 1)))
        db.get(m.Location, h.seed["locations"][0]).capacity = 1000
        typ = record(db.get(m.AssignmentType, h.seed["type"]))
        db.flush()
        duties, shifts, links = [], [], []
        for day in range(10):
            for n, person_id in enumerate(people):
                start = datetime(2026, 10, 4, 5 if n < 2 else 10, tzinfo=timezone.utc) + timedelta(days=day)
                end = start + timedelta(hours=4)
                shift = m.Shift(id=m.uid(), workspace_id=workspace["id"], assignment_type_id=typ["id"],
                    type_revision=typ["revision"], type_snapshot=typ, location_id=h.seed["locations"][0],
                    start=start, end=end)
                participant = m.Assignment(id=m.uid(), shift_id=shift.id, person_id=person_id, start=start, end=end)
                shifts.append(shift)
                links.extend((participant, m.ShiftService(shift_id=shift.id, service_id=h.seed["services"][0])))
                if n < 2:
                    duties.append(("staff" if n == 0 else "staff_two", participant.id,
                                   "exchange" if day % 2 else "give"))
        db.add_all(shifts)
        db.flush()
        db.add_all(links)
    publication = h.publish(workspace)["publication_id"]
    return publication, people, duties


@pytest.fixture(scope="module", params=["h", pytest.param("pg", marks=pytest.mark.postgres)])
def department_open_swap_reads(scale_harness, request):
    h = scale_harness(request.param)
    with pytest.MonkeyPatch.context() as monkeypatch:
        publication, people, duties = scale_publication(h)
        for role in ("staff", "staff_two", "staff_three"):
            h.client(role)

        def no_eligibility(*args, **kwargs):
            pytest.fail("Open offer list/detail reads must reuse the frozen audience")

        rows, command_metrics = measure(h, "open_swap_20_offers", lambda: [h.command(role, "swap.offer", {
            "kind": kind, "publication_id": publication, "assignment_id": assignment_id,
            "expires_at": (m.now() + timedelta(hours=12)).isoformat(), "reason": "Synthetic scale offer"})["swap"]
            for role, assignment_id, kind in duties])
        monkeypatch.setattr(swaps, "Eligibility", no_eligibility)
        pending_total = h.get("staff_three", "/api/requests/all")["total"]

        # Simulate the worker's completed result; audience computation has its own correctness tests and no UI budget.
        with transaction(h.app.state.engine, write=True) as db:
            for row in rows:
                audience = people[2:] if row["kind"] == "give" else people
                db.add_all(m.SwapOfferAudience(swap_id=row["id"], person_id=person_id, computed_at=m.now())
                           for person_id in audience if person_id != row["proposer_person_id"])
            keys = [f"swap:{row['id']}:1:offered" for row in rows]
            for event in db.scalars(select(m.Outbox).where(m.Outbox.event_key.in_(keys))):
                event.payload = {**event.payload, "audience_pending": False}

        listed, list_metrics = measure(h, "open_swap_requests_20_offers",
            lambda: h.get("staff_three", "/api/requests/all?type=swap"))
        detail, detail_metrics = measure(h, "open_swap_request_detail",
            lambda: h.get("staff_three", "/api/requests/all?id=" + rows[0]["id"]))
        metrics = {"people": len(people), "duties_per_person": 10, "open_offers": len(rows),
                   "offer_commands": command_metrics, "requests_list": list_metrics, "request_detail": detail_metrics}
        return rows, pending_total, listed, detail, metrics


def test_open_offer_and_requests_use_frozen_audience_at_department_scale(department_open_swap_reads, request):
    rows, pending_total, listed, detail, metrics = department_open_swap_reads
    assert len(rows) == 20 and all(row["audience_pending"] for row in rows)
    assert pending_total == 0
    assert {item["id"] for item in listed["items"]} == {row["id"] for row in rows}
    assert listed["total"] == listed["counts"]["to_decide"] == listed["counts"]["stages"]["reply"] == 20
    assert detail["item"]["id"] == rows[0]["id"] and detail["history"] == []
    request.node.user_properties.append(("open_swap_performance", json.dumps(metrics)))
    print("open swap department performance", metrics)
    # SW-03 adds one canonical non-empty check per offer. Measured SQLite/PG:
    # 79/81 SELECTs per command (formerly no eligibility work); retain linear work.
    assert metrics["offer_commands"]["sql"].get("SELECT", 0) <= 100 * len(rows), metrics
    for measured in (metrics["requests_list"], metrics["request_detail"]):
        assert measured["sql"].get("SELECT", 0) < 80, metrics


@pytest.mark.cpu_budget
def test_open_offer_and_requests_use_frozen_audience_at_department_scale_cpu_budget(department_open_swap_reads):
    *_, metrics = department_open_swap_reads
    command_metrics = metrics["offer_commands"]
    # Twenty canonical checks measured 7.65/12.36 CPU seconds; include headroom.
    assert command_metrics["cpu_seconds"] < python_budget(20), metrics
    for measured in (metrics["requests_list"], metrics["request_detail"]):
        assert measured["cpu_seconds"] < python_budget(1), metrics


@pytest.mark.wall_benchmark
def test_open_offer_and_requests_use_frozen_audience_at_department_scale_wall_benchmark(department_open_swap_reads):
    *_, metrics = department_open_swap_reads
    # The same sequential commands measured 7.73 s locally / 40.22 s over the PG tunnel.
    assert metrics["offer_commands"]["wall_seconds"] < python_budget(90), metrics
    for measured in (metrics["requests_list"], metrics["request_detail"]):
        assert measured["wall_seconds"] < python_budget(5), metrics
