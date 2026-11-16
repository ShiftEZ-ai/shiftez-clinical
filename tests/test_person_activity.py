"""The existing swap and wish reads scope to a person without broadening who may read them."""
from datetime import date, timedelta
from time import process_time

import pytest
from sqlalchemy import event

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import swaps
from packages.contracts.schemas import ROLE_NAMES
from .governance_support import identity


@pytest.mark.parametrize("area", ("swaps", "wishes"))
@pytest.mark.parametrize("role", ROLE_NAMES)
def test_person_activity_retains_area_permissions(h, area, role):
    if role == "programme_lead":
        identity(h, role, [role])
    readers = {"admin", "coordinator", "approver", "staff"}
    if area == "wishes":
        readers |= {"programme_lead", "auditor"}
    data = h.get(role, f"/api/{area}?person_id={h.seed['people'][0]}", status=200 if role in readers else 403)
    if role in readers:
        assert data[area] == []


@pytest.mark.parametrize("area", ("swaps", "wishes"))
def test_person_activity_rejects_peer_unlinked_and_unaffiliated_filters(h, area):
    identity(h, "unlinked_staff", ["staff"])
    h.get("staff", f"/api/{area}?person_id={h.seed['people'][1]}", status=403)
    h.get("unlinked_staff", f"/api/{area}?person_id={h.seed['people'][0]}", status=403)
    with transaction(h.app.state.engine, write=True) as db:
        person = m.Person(organisation_id=h.seed["organisation_id"], display_name="Synthetic unrelated person")
        db.add(person)
        db.flush()
        person_id = person.id
    h.get("coordinator", f"/api/{area}?person_id={person_id}", status=404)


def test_person_wishes_can_include_withdrawn_history_and_keep_publication_outcomes(h):
    main, other = h.seed["people"][:2]
    with transaction(h.app.state.engine, write=True) as db:
        for person_id, status, outcome in ((main, "open", "declined"), (main, "withdrawn", None), (other, "open", None)):
            db.add(m.StaffWish(department_id=h.seed["department_id"], person_id=person_id,
                first_day=date(2026, 10, 4), last_day=date(2026, 10, 4), kind="prefer_off", points=1,
                status=status, outcome=outcome, created_by=h.seed["identities"]["coordinator"]))
    default = h.get("staff", f"/api/wishes?person_id={main}")
    assert len(default["wishes"]) == 1 and default["wishes"][0]["outcome"] == "declined"
    history = h.get("staff", f"/api/wishes?person_id={main}&include_history=true")
    assert {w["status"] for w in history["wishes"]} == {"open", "withdrawn"}
    assert {w["person_id"] for w in history["wishes"]} == {main}
    assert history["partial"] is False
    assert h.get("coordinator", f"/api/wishes?person_id={main}&start=2026-11-01&end=2026-12-01&include_history=true")["wishes"] == []


def test_person_wish_history_advertises_truncation_and_can_be_read_by_period(h):
    main = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        for n in range(2001):
            day = date(2020, 1, 1) + timedelta(days=n)
            db.add(m.StaffWish(department_id=h.seed["department_id"], person_id=main, first_day=day, last_day=day,
                kind="prefer_off", points=1, status="withdrawn", created_by=h.seed["identities"]["staff"]))
    history = h.get("staff", f"/api/wishes?person_id={main}&include_history=true")
    assert len(history["wishes"]) == 2000 and history["partial"] is True
    month = h.get("staff", f"/api/wishes?person_id={main}&include_history=true&start=2020-01-01&end=2020-02-01")
    assert len(month["wishes"]) == 31 and month["partial"] is False


def published_assignments(h):
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00", people=[h.seed["people"][1]])["workspace"]
    publication_id = h.publish(w)["publication_id"]
    shifts = h.get("coordinator", "/api/publications/" + publication_id)["snapshot"]["shifts"]
    return publication_id, {p["person_id"]: p["id"] for shift in shifts for p in shift["participants"]}


def test_person_swaps_include_offered_received_and_filter_before_paging(h):
    main, other, third = h.seed["people"]
    other_identity = identity(h, "other_staff", ["staff"], other)
    publication_id, assignments = published_assignments(h)
    with transaction(h.app.state.engine, write=True) as db:
        for proposer, recipient in ((main, other), (other, main), (other, third)):
            db.add(m.SwapRequest(department_id=h.seed["department_id"], publication_id=publication_id,
                proposer_id=h.seed["identities"]["staff"] if proposer == main else other_identity,
                proposer_person_id=proposer, recipient_person_id=recipient,
                first_assignment_id=assignments[proposer], reason="Synthetic exchange", expires_at=m.now() + timedelta(hours=2)))
    data = h.get("coordinator", f"/api/swaps?person_id={main}")
    assert data["total"] == 2 and {s["direction"] for s in data["swaps"]} == {"offered", "received"}
    assert {s["effective_status"] for s in data["swaps"]} == {"offered"}
    page = h.get("staff", f"/api/swaps?person_id={main}&limit=1&offset=1")
    assert page["total"] == 2 and len(page["swaps"]) == 1 and page["has_more"] is False


@pytest.mark.parametrize("state", ("offered", "expired", "stale", "access_revoked", "partly_replaced", "withdrawn"))
def test_swap_batch_projection_matches_shared_status_and_assignment_evaluation(h, state):
    other_identity = identity(h, "other_staff", ["staff"], h.seed["people"][1])
    publication_id, assignments = published_assignments(h)
    with transaction(h.app.state.engine, write=True) as db:
        row = m.SwapRequest(department_id=h.seed["department_id"], publication_id=publication_id,
            proposer_id=h.seed["identities"]["staff"], proposer_person_id=h.seed["people"][0],
            recipient_id=other_identity, recipient_person_id=h.seed["people"][1],
            first_assignment_id=assignments[h.seed["people"][0]], second_assignment_id=assignments[h.seed["people"][1]],
            status="withdrawn" if state == "withdrawn" else "offered", reason="Synthetic batch projection",
            expires_at=m.now() + timedelta(hours=-2 if state == "expired" else 2))
        db.add(row)
        if state == "access_revoked":
            db.get(m.Identity, other_identity).disabled = True
        pub = db.get(m.Publication, publication_id)
        if state == "stale":
            pub.current = False
        if state == "partly_replaced":
            pub.effective_until = date(2026, 10, 5)
        db.flush()
        actor = Actor(h.seed["identities"]["coordinator"], h.seed["department_id"], h.seed["organisation_id"],
                      "Synthetic coordinator", frozenset({"coordinator"}), None, "test")
        expected = swaps.projection(db, row)
        actual = swaps.projection(db, row, swaps.list_cache(db, actor, [row]))
        assert actual == expected
        assert actual["effective_status"] == ("stale" if state == "partly_replaced" else state)


@pytest.fixture(scope='module')
def person_swaps_scale(scale_harness):
    h = scale_harness('h')
    main = h.seed["people"][0]
    publication_id, assignments = published_assignments(h)
    with transaction(h.app.state.engine, write=True) as db:
        for n in range(150):
            person = m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic swap peer {n}")
            db.add(person)
            db.flush()
            account = m.Identity(issuer="synthetic-test", subject=f"swap-peer-{n}", display_name=person.display_name, person_id=person.id)
            db.add(account)
            db.flush()
            db.add(m.RoleBinding(identity_id=account.id, department_id=h.seed["department_id"], role="staff", starts_on=date(2020, 1, 1)))
            db.add(m.SwapRequest(department_id=h.seed["department_id"], publication_id=publication_id,
                proposer_id=h.seed["identities"]["staff"], proposer_person_id=main, recipient_id=account.id, recipient_person_id=person.id,
                first_assignment_id=assignments[main], reason="Synthetic scale swap", expires_at=m.now() + timedelta(hours=2)))
    h.client("coordinator")
    queries = []
    def query(*args):
        queries.append(args[2])
    event.listen(h.app.state.engine, "before_cursor_execute", query)
    began = process_time()
    try:
        data = h.get("coordinator", f"/api/swaps?person_id={main}&limit=100")
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", query)
    elapsed = process_time() - began
    return data, queries, elapsed


def test_person_swaps_department_scale_has_bounded_queries(person_swaps_scale):
    data, queries, _ = person_swaps_scale
    assert len(data["swaps"]) == 100 and data["total"] == 150
    assert all(s["effective_status"] == "offered" for s in data["swaps"])
    assert len(queries) <= 22, len(queries)


@pytest.mark.cpu_budget
def test_person_swaps_department_scale_has_bounded_queries_cpu_budget(person_swaps_scale):
    _, _, elapsed = person_swaps_scale
    assert elapsed < 5


@pytest.fixture(scope='module')
def person_wishes_scale(scale_harness):
    h = scale_harness('h')
    main = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        people = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic wish peer {n}") for n in range(149)]
        db.add_all(people)
        db.flush()
        for person_id in (main, *(p.id for p in people)):
            for day in range(1, 21):
                db.add(m.StaffWish(department_id=h.seed["department_id"], person_id=person_id,
                    first_day=date(2026, 10, day), last_day=date(2026, 10, day), kind="prefer_off", points=1,
                    created_by=h.seed["identities"]["coordinator"]))
    h.client("coordinator")
    queries = []
    def query(*args):
        queries.append(args[2])
    event.listen(h.app.state.engine, "before_cursor_execute", query)
    began = process_time()
    try:
        data = h.get("coordinator", f"/api/wishes?person_id={main}&start=2026-10-01&end=2026-11-01&include_history=true")
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", query)
    elapsed = process_time() - began
    return data, queries, elapsed


def test_person_wishes_department_scale_has_bounded_queries(person_wishes_scale):
    data, queries, _ = person_wishes_scale
    assert len(data["wishes"]) == 20
    assert len(queries) <= 20, len(queries)


@pytest.mark.cpu_budget
def test_person_wishes_department_scale_has_bounded_queries_cpu_budget(person_wishes_scale):
    _, _, elapsed = person_wishes_scale
    assert elapsed < 5
