"""Person absence reads preserve existing role access and server-side leave disclosure."""
import json
from datetime import date, timedelta
from time import process_time

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ROLE_NAMES
from packages.domain.time import aware
from .governance_support import identity


def path(person):
    return f"/api/people/{person}/availability?start=2026-10-01&end=2026-11-01"


def absences(h, mode="type"):
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).leave_reason_display = mode
        common = {"department_id": h.seed["department_id"], "person_id": h.seed["people"][0]}
        for kind in ("recorded_leave", "approved_leave", "actual_absence", "external_commitment", "protected_time", "off_duty"):
            db.add(m.Availability(**common, kind=kind, leave_type="Synthetic private category", private_reason="Synthetic private reason",
                start=aware("2026-10-04T00:00:00+03:00"), end=aware("2026-10-05T00:00:00+03:00"),
                recorded_by=h.seed["identities"]["coordinator"]))
        for status in ("submitted", "approved", "rejected", "withdrawn"):
            db.add(m.RequestRecord(**common, kind="leave", status=status, private_reason="Synthetic request detail",
                start=aware("2026-10-05T00:00:00+03:00"), end=aware("2026-10-06T00:00:00+03:00"),
                created_by=h.seed["identities"]["staff"]))
        for status in ("proposed", "active", "declined", "ended"):
            db.add(m.AvailabilityPattern(**common, weekdays=[6], start_time="08:00", end_time="12:00",
                starts_on=date(2026, 10, 1), status=status, note="Synthetic pattern detail",
                created_by=h.seed["identities"]["staff"]))


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_person_availability_permissions(h, role):
    if role == "programme_lead":
        identity(h, role, [role])
    allowed = role in {"admin", "coordinator", "approver", "staff"}
    reply = h.get(role, path(h.seed["people"][0]), status=200 if allowed else 403)
    if allowed:
        assert reply["availability"] == [] and reply["patterns"] == []
        assert reply["balances_supported"] is False


@pytest.mark.parametrize("mode", ("none", "type", "details"))
def test_person_availability_disclosure_and_own_type(h, mode):
    absences(h, mode)
    data = h.get("coordinator", path(h.seed["people"][0]))
    assert len(data["availability"]) == 6
    assert {r["status"] for r in data["requests"]} == {"submitted", "approved", "rejected", "withdrawn"}
    assert {p["status"] for p in data["patterns"]} == {"proposed", "active", "declined", "ended"}
    assert all("private_reason" not in r and "leave_type" not in r for r in data["availability"])
    encoded = json.dumps(data)
    assert ("Synthetic private category" in encoded) == (mode != "none")
    for text in ("Synthetic private reason", "Synthetic request detail", "Synthetic pattern detail"):
        assert (text in encoded) == (mode == "details")
    own = h.get("staff", path(h.seed["people"][0]))
    assert all(r["label"] == "Synthetic private category" for r in own["availability"])
    assert all("note" not in row for key in ("availability", "requests", "patterns") for row in own[key])
    assert all("workspace_id" not in r for r in own["requests"])


def test_person_availability_is_department_scoped_and_period_bounded(h):
    absences(h)
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic other department")
        db.add(foreign)
        db.flush()
        db.add(m.Availability(department_id=foreign.id, person_id=h.seed["people"][0], kind="recorded_leave",
            leave_type="Foreign private category", private_reason="Foreign private reason",
            start=aware("2026-10-04T00:00:00+03:00"), end=aware("2026-10-05T00:00:00+03:00"),
            recorded_by=h.seed["identities"]["coordinator"]))
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Synthetic unaffiliated person")
        db.add(person)
        db.flush()
        unaffiliated = person.id
    data = h.get("coordinator", path(h.seed["people"][0]))
    assert len(data["availability"]) == 6 and "Foreign private" not in json.dumps(data)
    empty = h.get("coordinator", path(h.seed["people"][0]).replace("2026-10-01&end=2026-11-01", "2026-11-01&end=2026-12-01"))
    assert empty["availability"] == [] and empty["requests"] == []
    h.get("staff", path(h.seed["people"][1]), status=403)
    identity(h, "unlinked_staff", ["staff"])
    h.get("unlinked_staff", path(h.seed["people"][0]), status=403)
    h.get("coordinator", path(unaffiliated), status=404)
    h.get("coordinator", f"/api/people/{h.seed['people'][0]}/availability?start=2026-10-01&end=2026-10-01", status=422)
    h.get("coordinator", f"/api/people/{h.seed['people'][0]}/availability?start=2026-10-01&end=2027-11-01", status=422)


@pytest.fixture(scope='module')
def person_availability_scale(scale_harness):
    h = scale_harness('h')
    main = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic scale {n}") for n in range(149)]
        db.add_all(people)
        db.flush()
        for person_id in (main, *(p.id for p in people)):
            for n in range(20):
                start = aware("2026-10-01T00:00:00+03:00") + timedelta(days=n)
                db.add(m.Availability(department_id=h.seed["department_id"], person_id=person_id, kind="recorded_leave",
                    private_reason="Synthetic scale note", start=start, end=start + timedelta(hours=1),
                    recorded_by=h.seed["identities"]["coordinator"]))
    h.client("coordinator")
    queries = []
    def query(*args):
        queries.append(args[2])
    event.listen(h.app.state.engine, "before_cursor_execute", query)
    began = process_time()
    try:
        data = h.get("coordinator", path(main))
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", query)
    elapsed = process_time() - began
    return data, queries, elapsed


def test_person_availability_department_scale_has_bounded_queries(person_availability_scale):
    data, queries, _ = person_availability_scale
    assert len(data["availability"]) == 20
    assert len(queries) <= 20, len(queries)


@pytest.mark.cpu_budget
def test_person_availability_department_scale_has_bounded_queries_cpu_budget(person_availability_scale):
    _, _, elapsed = person_availability_scale
    assert elapsed < 5


def test_person_availability_uses_local_midnight_as_utc_query_bounds(h):
    main = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        for start, end in (("2026-09-30T23:00:00+03:00", "2026-10-01T00:00:00+03:00"),
                           ("2026-10-01T00:00:00+03:00", "2026-10-01T01:00:00+03:00"),
                           ("2026-10-31T23:00:00+03:00", "2026-11-01T00:00:00+03:00"),
                           ("2026-11-01T00:00:00+03:00", "2026-11-01T01:00:00+03:00")):
            db.add(m.Availability(department_id=h.seed["department_id"], person_id=main, kind="recorded_leave",
                private_reason="Synthetic boundary", start=aware(start), end=aware(end),
                recorded_by=h.seed["identities"]["coordinator"]))
            db.add(m.RequestRecord(department_id=h.seed["department_id"], person_id=main, kind="leave",
                private_reason="Synthetic boundary", start=aware(start), end=aware(end),
                created_by=h.seed["identities"]["staff"]))
    data = h.get("coordinator", path(main))
    assert len(data["availability"]) == len(data["requests"]) == 2
    assert {row["start"] for row in data["availability"]} == {"2026-09-30T21:00:00+00:00", "2026-10-31T20:00:00+00:00"}
