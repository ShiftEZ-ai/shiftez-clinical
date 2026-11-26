"""Residency: rotation board, plans from blocks, copy forward, rotation change requests decided by a programme lead,
leave decided by a programme lead, notes on trainees and external rotators. Fixtures are fictional."""
from .clock_support import freeze_clock
import pytest
from datetime import date
from packages.contracts import schemas as s

from .governance_support import identity

REASON = "Synthetic residency fixture"


def lead_of(h, p, username="programme_lead"):
    """A programme lead leads the programmes they are a current member of (training.led_programmes)."""
    from apps.api import models as m
    from apps.api.db import transaction
    pd = h.configure("people", {"display_name": "Fictional programme director"})["record"]["id"]
    h.configure("programme_memberships", {"person_id": pd, "programme_id": p["id"], "starts_on": "2026-01-01", "ends_on": "2027-12-31"},
                role="coordinator")
    with transaction(h.app.state.engine, write=True) as db:
        if username in h.seed["identities"]:
            db.get(m.Identity, h.seed["identities"][username]).person_id = pd
    if username not in h.seed["identities"]:
        identity(h, username, ["programme_lead"], person=pd)
    return pd


@pytest.fixture
def r(h):
    return setup_residency(h)


def setup_residency(h):
    p = h.configure("programmes", {"name": "Fictional residency", "starts_on": "2026-01-01"})["record"]
    lead_of(h, p)
    cohort = h.configure("cohorts", {"name": "Fictional year 1", "programme_id": p["id"],
                                     "starts_on": "2026-07-01", "ends_on": "2027-07-01"})["record"]
    types = [h.configure("rotation_types", {"name": name, "starts_on": "2026-01-01"})["record"] for name in ("Emergency", "Paediatrics")]
    people = h.seed["people"][:2]
    for pid in people:
        h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"], "starts_on": "2026-07-01",
                                              "ends_on": "2028-07-01"}, role="programme_lead")
    blocks = [{"rotation_type_id": t["id"], "length": 1, "unit": "months"} for t in types]
    return h, p, cohort, types, people, blocks


def plan(h, cohort, people, blocks, start="2026-10-01", stagger=True, role="programme_lead", status=200):
    return h.command(role, "rotation.plan", {"cohort_id": cohort["id"], "person_ids": people, "starts_on": start,
                                             "blocks": blocks, "stagger": stagger, "reason": REASON}, status=status)


def test_plan_staggers_blocks_skips_overlaps_and_undoes(r):
    h, p, cohort, types, people, blocks = r
    first = plan(h, cohort, people, blocks)
    got = sorted((x["person_id"], x["starts_on"], x["rotation_type_id"]) for x in first["created"])
    assert got == sorted([(people[0], "2026-10-01", types[0]["id"]), (people[0], "2026-11-01", types[1]["id"]),
                          (people[1], "2026-10-01", types[1]["id"]), (people[1], "2026-11-01", types[0]["id"])])
    again = plan(h, cohort, people, blocks)
    assert again["created"] == [] and {x["why"] for x in again["skipped"]} == {"overlap"}
    outside = plan(h, cohort, people[:1], blocks, start="2027-06-15")
    assert [x["why"] for x in outside["skipped"]] == ["outside_cohort", "outside_cohort"]
    plan(h, cohort, people, blocks, role="staff", status=403)
    items = [{"id": x["id"], "revision": x["revision"]} for x in first["created"]]
    undone = h.command("programme_lead", "rotation.undo_plan", {"items": items, "reason": REASON})
    assert undone["removed"] == 4 and len(undone["records"]) == 4
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == []


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_department_scale_plan_is_undone_in_one_command(database, request):
    from time import perf_counter
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, cohort, types, people, blocks = setup_residency(request.getfixturevalue(database))
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic trainee {i}") for i in range(150)]
        db.add_all(rows)
        db.flush()
        ids = [row.id for row in rows]
        db.add_all([m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=pid, programme_id=p["id"],
            starts_on=date(2026, 7, 1), ends_on=date(2027, 7, 1)) for pid in ids])
    created = plan(h, cohort, ids, [{"rotation_type_id": types[0]["id"], "length": 1, "unit": "weeks"}] * 14)["created"]
    assert len(created) == 2100
    client = h.client("programme_lead")
    started = perf_counter()
    response = client.get("/api/catalogues")
    elapsed = perf_counter() - started
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()["rotations"]} == {row["id"] for row in created}
    print(f"Catalogues at 2,100 rotations ({database}): {elapsed:.3f} s, {len(response.content):,} uncompressed bytes")
    undone = h.command("programme_lead", "rotation.undo_plan", {"items": [
        {"id": row["id"], "revision": row["revision"]} for row in created], "reason": REASON})
    assert undone["removed"] == len(created) and undone["kept"] == 0
    assert set(undone["removed_ids"]) == {row["id"] for row in created}
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == []


@pytest.mark.parametrize("moment,cutoff", [("2026-10-16T20:59:00+00:00", date(2025, 10, 16)),
                                          ("2028-02-29T20:59:00+00:00", date(2027, 2, 28))])
def test_rotation_catalogues_keep_recent_window_and_board_keeps_history(h, monkeypatch, moment, cutoff):
    from datetime import datetime, timedelta
    from apps.api import models as m
    from apps.api.db import transaction
    clock = datetime.fromisoformat(moment)
    freeze_clock(monkeypatch, lambda: clock)
    monkeypatch.setattr(s, "MAX_CATALOGUE_ROTATIONS", 3)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Organisation, h.seed["organisation_id"]).timezone = "Asia/Riyadh"
    p = h.configure("programmes", {"name": "Synthetic historical programme", "starts_on": "2020-01-01"})["record"]
    cohort = h.configure("cohorts", {"name": "Synthetic historical cohort", "programme_id": p["id"],
                                    "starts_on": "2020-01-01", "ends_on": "2030-01-01"})["record"]
    pid = h.seed["people"][0]
    h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"],
                                          "starts_on": "2020-01-01", "ends_on": "2030-01-01"})
    value = {"person_id": pid, "cohort_id": cohort["id"], "service_id": h.seed["services"][0]}
    today = clock.date()
    old, boundary, current, future = [h.configure("rotations", {**value, "starts_on": a.isoformat(),
        "ends_on": b.isoformat()})["record"] for a, b in (
            (cutoff - timedelta(days=15), cutoff - timedelta(days=1)),
            (cutoff - timedelta(days=1), cutoff), (today, today + timedelta(days=1)),
            (today + timedelta(days=30), today + timedelta(days=40)))]
    client = h.client("coordinator")
    response = client.get("/api/catalogues")
    assert response.status_code == 200, response.text
    catalogues = response.json()
    assert {row["id"] for row in catalogues["rotations"]} == {boundary["id"], current["id"], future["id"]}
    board = h.get("coordinator", f"/api/rotation-board?start={old['starts_on']}&end={cutoff.isoformat()}")
    assert old["id"] in {row["id"] for row in board["rotations"]}
    # Cross local midnight without changing rows or waiting for the cache's two-minute expiry.
    clock += timedelta(minutes=1)
    after = client.get("/api/catalogues?known=" + ",".join(catalogues["_versions"].values()))
    assert after.status_code == 200, after.text
    assert {row["id"] for row in after.json()["rotations"]} == {current["id"], future["id"]}
    assert after.json()["_versions"]["rotations"] != catalogues["_versions"]["rotations"]
    assert client.get("/api/catalogues", headers={"If-None-Match": response.headers["etag"]}).status_code == 200


def test_rotation_catalogue_refuses_to_truncate_its_recent_window(r, monkeypatch):
    h, p, cohort, types, people, blocks = r
    assert len(plan(h, cohort, people, blocks)["created"]) == 4
    monkeypatch.setattr(s, "MAX_CATALOGUE_ROTATIONS", 3)
    assert h.get("programme_lead", "/api/catalogues", status=409)["error"]["code"] == "CATALOGUE_LIMIT"


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_person_rotations_keep_scoped_trainee_and_mentor_history(request, database, monkeypatch):
    from datetime import datetime
    from apps.api import models as m
    h = request.getfixturevalue(database)
    freeze_clock(monkeypatch, lambda: datetime.fromisoformat("2026-10-16T12:00:00+00:00"))
    p = h.configure("programmes", {"name": "Synthetic historical residency", "starts_on": "2020-01-01"})["record"]
    lead_of(h, p)
    cohort = h.configure("cohorts", {"name": "Synthetic historical cohort", "programme_id": p["id"],
                                    "starts_on": "2020-01-01", "ends_on": "2030-01-01"})["record"]
    trainee, peer, mentor = h.seed["people"][:3]
    for pid in (trainee, peer):
        h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"],
                                              "starts_on": "2020-01-01", "ends_on": "2030-01-01"})
    value = {"person_id": trainee, "mentor_id": mentor, "cohort_id": cohort["id"],
             "service_id": h.seed["services"][0], "department_available": False}
    old = h.configure("rotations", {**value, "starts_on": "2024-09-01", "ends_on": "2024-10-16"})["record"]
    tied = h.configure("rotations", {**value, "person_id": peer,
                                     "starts_on": "2024-09-01", "ends_on": "2024-10-16"})["record"]
    earlier = h.configure("rotations", {**value, "starts_on": "2024-08-01", "ends_on": "2024-09-01"})["record"]
    after = h.configure("rotations", {**value, "starts_on": "2024-10-16", "ends_on": "2024-11-01"})["record"]
    path = "/api/people/{}/rotations?start=2024-09-01&end=2024-10-16"
    expected = sorted([old, tied], key=lambda row: (row["starts_on"], row["id"]))
    for role in s.ROLE_NAMES:
        if role != "staff" and role not in s.PERMISSIONS["catalogue.read"]:
            h.get(role, path.format(trainee), status=403)
            continue
        assert h.get(role, path.format(trainee)) == {"rotations": [old]}
        if role == "staff":
            assert h.get(role, path.format(peer)) == {"rotations": []}
            assert h.get(role, path.format(mentor)) == {"rotations": [old]}
            h.get(role, "/api/catalogues", status=403)
            continue
        assert h.get(role, path.format(mentor)) == {"rotations": expected}
        assert h.get(role, "/api/catalogues")["rotations"] == []
    identity(h, "staff_mentor", ["staff"], person=mentor)
    assert h.get("staff_mentor", path.format(mentor)) == {"rotations": expected}
    identity(h, "staff_coordinator", ["staff", "coordinator"], person=trainee)
    assert h.get("staff_coordinator", path.format(peer)) == {"rotations": [tied]}
    identity(h, "unlinked_staff", ["staff"])
    h.configure("rotations", {**value, "mentor_id": None, "starts_on": "2024-11-01", "ends_on": "2024-12-01"})
    assert h.get("unlinked_staff", path.format(trainee).replace("end=2024-10-16", "end=2024-12-01")) == {
        "rotations": []}
    assert h.get("coordinator", path.format(mentor).replace("start=2024-09-01", "start=2024-08-01")) == {
        "rotations": [earlier, *expected]}
    assert h.get("coordinator", path.format(mentor).replace("start=2024-09-01", "start=2024-10-15")) == {
        "rotations": expected}
    assert h.get("coordinator", path.format(trainee).replace("start=2024-09-01&end=2024-10-16",
                                                           "start=2024-10-16&end=2024-10-17")) == {"rotations": [after]}
    other_programme(h, trainee)
    assert h.get("other_lead", path.format(trainee)) == {"rotations": []}
    assert h.get("other_lead", path.format(mentor)) == {"rotations": []}
    assert h.get("other_lead", "/api/catalogues")["rotations"] == []


@pytest.mark.parametrize("query", [
    "start=2024-01-01&end=2024-01-01", "start=2024-01-02&end=2024-01-01",
    "start=2024-01-01&end=2025-02-05", "start=0001-01-01&end=9999-12-31",
    "start=invalid&end=2024-02-01", "start=2024-01-01&end=2024-02-30",
    "start=2024-01-01", "end=2024-02-01", "",
])
def test_person_rotations_reject_invalid_ranges(h, query):
    h.get("coordinator", f"/api/people/{h.seed['people'][0]}/rotations?{query}", status=422)


def test_person_rotations_allow_exactly_400_days(h):
    assert h.get("coordinator", f"/api/people/{h.seed['people'][0]}/rotations?start=2024-01-01&end=2025-02-04") == {
        "rotations": []}


@pytest.mark.parametrize("foreign", [False, True])
def test_person_rotations_forbid_inaccessible_people(h, foreign):
    from apps.api import models as m
    from apps.api.db import transaction
    pid = m.uid()
    if foreign:
        with transaction(h.app.state.engine, write=True) as db:
            org = m.Organisation(name="Synthetic other organisation")
            db.add(org)
            db.flush()
            db.add(m.Person(id=pid, organisation_id=org.id, display_name="Synthetic inaccessible person"))
    h.get("coordinator", f"/api/people/{pid}/rotations?start=2024-01-01&end=2024-02-01", status=403)


def test_copy_forward_recreates_next_year(r):
    h, p, cohort, types, people, blocks = r
    plan(h, cohort, people, blocks)
    nxt = h.configure("cohorts", {"name": "Fictional year 2", "programme_id": p["id"],
                                  "starts_on": "2027-07-01", "ends_on": "2028-07-01"})["record"]
    copied = h.command("programme_lead", "rotation.copy", {"starts_on": "2026-07-01", "ends_on": "2027-07-01", "months": 12,
                                                           "cohort_id": nxt["id"], "reason": REASON})
    assert sorted(x["starts_on"] for x in copied["created"]) == ["2027-10-01", "2027-10-01", "2027-11-01", "2027-11-01"]
    assert {x["cohort_id"] for x in copied["created"]} == {nxt["id"]}


def test_copy_limit_rejects_before_creating_any_rotations(r, monkeypatch):
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.services import training
    h, p, cohort, types, people, blocks = r
    created = plan(h, cohort, people, blocks)["created"]
    nxt = h.configure("cohorts", {"name": "Synthetic next cohort", "programme_id": p["id"],
        "starts_on": "2027-07-01", "ends_on": "2028-07-01"})["record"]
    with transaction(h.app.state.engine) as db:
        before = db.get(m.Organisation, h.seed["organisation_id"]).input_revision
    monkeypatch.setattr(s, "MAX_ROTATIONS_PER_PLAN", len(created) - 1)
    monkeypatch.setattr(training, "_create_rotation", lambda *args: pytest.fail("Copy wrote before checking its limit"))
    reply = h.command("programme_lead", "rotation.copy", {"starts_on": "2026-10-01", "ends_on": "2026-12-01",
        "cohort_id": nxt["id"], "reason": REASON}, status=422)
    assert reply["error"]["code"] == "ROTATION_COPY_LIMIT"
    assert "choose fewer people or a shorter period" in reply["error"]["message"]
    assert {row["id"] for row in h.get("programme_lead", "/api/catalogues")["rotations"]} == {row["id"] for row in created}
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed["organisation_id"]).input_revision == before


def test_copy_skips_empty_month_end_row_and_copies_other_rows(r, monkeypatch):
    h, p, cohort, types, people, blocks = r
    collapsed = h.configure("rotations", rotation_value(people[0], cohort, types[0], "2027-01-30", "2027-01-31"))["record"]
    normal = h.configure("rotations", rotation_value(people[0], cohort, types[1], "2027-01-01", "2027-01-15"))["record"]
    monkeypatch.setattr(s, "MAX_ROTATIONS_PER_PLAN", 1)
    copied = h.command("programme_lead", "rotation.copy", {"starts_on": "2027-01-01", "ends_on": "2027-02-01",
        "months": 1, "reason": REASON})
    assert [(row["starts_on"], row["ends_on"], row["rotation_type_id"]) for row in copied["created"]] == [
        ("2027-02-01", "2027-02-15", normal["rotation_type_id"])]
    assert copied["skipped"] == [{"person_id": people[0], "starts_on": "2027-02-28", "ends_on": "2027-02-28",
        "why": "month_end", "source_id": collapsed["id"]}]
    assert copied["message"] == "1 rotation copied; 1 skipped (overlap, outside the cohort or shortened to nothing at month end)."


def test_board_scopes_people_and_shows_leave_and_external(r):
    h, p, cohort, types, people, blocks = r
    h.configure('rotation_types', {**{key: types[0][key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': 'needs_approval'}, types[0]['id'], types[0]['revision'])
    plan(h, cohort, people, blocks)
    external = h.seed["people"][2]
    h.configure("programme_memberships", {"person_id": external, "programme_id": p["id"], "starts_on": "2026-10-01",
                                          "ends_on": "2026-12-01", "external": True, "home_programme": "Fictional visiting programme"},
                role="programme_lead")
    h.command("staff", "request.create", {"person_id": people[0], "start": "2026-10-20T00:00:00+03:00",
                                          "end": "2026-10-22T00:00:00+03:00", "private_reason": "Fictional private reason",
                                          "leave_type_id": h.get("staff", "/api/leave-types")["leave_types"][0]["id"]})
    team = h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2027-01-01")
    assert {x["id"] for x in team["people"]} == {*people, external}
    assert next(x for x in team["memberships"] if x["person_id"] == external)["home_programme"] == "Fictional visiting programme"
    assert len(team["rotations"]) == 4 and team["can_plan"] and team["can_note"]
    assert team["names"]["rotation_types"] == {t["id"]: t["name"] for t in types}
    waiting = [x for x in team["leave"] if x["stage"] == "waiting"]
    assert [(x["person_id"], x["starts_on"], x["ends_on"]) for x in waiting] == [(people[0], "2026-10-20", "2026-10-22")]
    assert "Fictional private reason" not in str(team)
    own = h.get("staff", "/api/rotation-board?start=2026-10-01&end=2027-01-01")
    assert [x["id"] for x in own["people"]] == [people[0]]
    assert {x["person_id"] for x in own["rotations"]} == {people[0]} and not own["can_plan"]
    h.get("staff", "/api/rotation-board?start=2026-01-01&end=2029-01-01", status=422)


def test_board_choices_include_unused_active_department_types_and_services(r):
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, cohort, types, people, blocks = r
    rotation = h.configure("rotations", rotation_value(people[0], cohort, types[0]))["record"]
    unused = h.configure("rotation_types", {"name": "Another synthetic rotation", "starts_on": "2026-01-01"})["record"]
    service = h.configure("services", {"name": "Another synthetic service", "starts_on": "2026-01-01"})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=h.seed["organisation_id"], name="Synthetic other department")
        db.add(foreign)
        db.flush()
        for cls in (m.RotationType, m.Service):
            db.add(cls(department_id=h.seed["department_id"], name="Inactive synthetic choice", starts_on=date(2026, 1, 1), active=False))
            db.add(cls(department_id=foreign.id, name="Foreign synthetic choice", starts_on=date(2026, 1, 1)))
    for role in ("programme_lead", "staff"):
        board = h.get(role, "/api/rotation-board?start=2026-10-01&end=2026-11-01")
        assert unused["id"] not in board["names"]["rotation_types"] and service["id"] not in board["names"]["services"]
        assert board["choices"]["rotation_types"] == {t["id"]: t["name"] for t in [*types, unused]}
        assert board["choices"]["services"][service["id"]] == service["name"]
        for choices in board["choices"].values():
            assert list(choices.values()) == sorted(choices.values())
            assert not {"Inactive synthetic choice", "Foreign synthetic choice"} & set(choices.values())
    asked = h.command("staff", "rotation.request_change", {"rotation_id": rotation["id"], "rotation_type_id": unused["id"],
        "service_id": service["id"], "starts_on": rotation["starts_on"], "ends_on": rotation["ends_on"], "private_reason": REASON})["record"]
    assert asked["details"]["proposed"]["rotation_type_id"] == unused["id"]
    assert asked["details"]["proposed"]["service_id"] == service["id"]


def test_rotation_change_request_is_decided_by_programme_lead_and_undone(r):
    h, p, cohort, types, people, blocks = r
    rotation = next(x for x in plan(h, cohort, people, blocks)["created"] if x["person_id"] == people[0] and x["starts_on"] == "2026-10-01")
    payload = {"rotation_id": rotation["id"], "rotation_type_id": types[1]["id"], "starts_on": "2026-10-01", "ends_on": "2026-10-20",
               "private_reason": "Fictional elective swap"}
    other = next(x for x in h.get("programme_lead", "/api/catalogues")["rotations"] if x["person_id"] == people[1])
    h.command("staff", "rotation.request_change", {**payload, "rotation_id": other["id"]}, status=403)
    asked = h.command("staff", "rotation.request_change", payload)["record"]
    h.command("staff", "rotation.request_change", payload, status=409)
    listed = h.get("programme_lead", "/api/requests/all")
    assert [i["id"] for i in listed["items"]] == [asked["id"]] and listed["counts"]["to_decide"] == 1
    decided = h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"])["record"]
    changed = next(x for x in h.get("programme_lead", "/api/catalogues")["rotations"] if x["id"] == rotation["id"])
    assert (changed["rotation_type_id"], changed["ends_on"]) == (types[1]["id"], "2026-10-20")
    h.command("programme_lead", "request.undo", {"reason": REASON}, asked["id"], decided["revision"])
    restored = next(x for x in h.get("programme_lead", "/api/catalogues")["rotations"] if x["id"] == rotation["id"])
    assert (restored["rotation_type_id"], restored["ends_on"]) == (types[0]["id"], "2026-11-01")
    # A rotation a request refers to is kept by a removal.
    kept = h.command("programme_lead", "rotation.remove", {"items": [{"id": rotation["id"], "revision": restored["revision"]}],
                                                           "reason": REASON})
    assert kept["kept"] == 1
    assert kept["kept_records"][0]["code"] == "ROTATION_HAS_REQUEST"
    assert "change request" in kept["kept_records"][0]["message"]


def test_programme_lead_decides_trainee_leave_only(r):
    h, p, cohort, types, people, blocks = r
    leave_type = h.get("staff", "/api/leave-types")["leave_types"][0]["id"]
    ask = lambda pid, role: h.command(role, "request.create", {"person_id": pid, "start": "2026-10-20T00:00:00+03:00",
        "end": "2026-10-22T00:00:00+03:00", "private_reason": "Fictional leave", "leave_type_id": leave_type})["record"]
    trainee = ask(people[0], "staff")
    outsider = ask(h.seed["people"][2], "coordinator")
    h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, outsider["id"], outsider["revision"], status=403)
    h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, trainee["id"], trainee["revision"])
    absences = h.get("coordinator", "/api/availability")
    assert any(a.get("request_id") == trainee["id"] for a in absences["availability"])


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_coordinator_lead_request_eligibility_matches_bulk_decision(database, request):
    from apps.api import models as m
    from apps.api.db import transaction
    from .test_request_decide_many import request_row, decide
    h, p, c, types, people, blocks = setup_residency(request.getfixturevalue(database))
    for typ in types:
        h.configure('rotation_types', {**{key: typ[key] for key in s.RotationTypeInput.model_fields},
            'leave_policy': 'needs_approval'}, typ['id'], typ['revision'])
    with transaction(h.app.state.engine) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
    identity(h, "coordinator_lead", ["coordinator", "programme_lead"], person=director)
    identity(h, "alternate_lead", ["coordinator"], person=director)
    other_programme = h.configure("programmes", {"name": "Other fictional residency", "starts_on": "2026-01-01"})["record"]
    h.configure("programme_memberships", {"person_id": h.seed["people"][2], "programme_id": other_programme["id"],
        "starts_on": "2026-07-01", "ends_on": "2028-07-01"})
    trainee = request_row(h)
    rotation = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    rotation_request = change(h, rotation, types[1])["record"]
    on_behalf = [request_row(h, "coordinator_lead"), request_row(h, "alternate_lead")]
    excluded = [request_row(h, "coordinator", h.seed["people"][2]),
                request_row(h, kind="meeting"), request_row(h, "coordinator", director)]
    listing = h.get("coordinator_lead", "/api/requests/all")["items"]
    assert {row["id"] for row in listing} == {row["id"] for row in [trainee, rotation_request, *on_behalf, *excluded]}
    eligible = [row for row in listing if row["can_decide"]]
    assert {row["id"] for row in eligible} == {row["id"] for row in [trainee, rotation_request, *on_behalf]}
    for row in listing:
        detail = h.get("coordinator_lead", "/api/requests/all?id=" + row["id"])["item"]
        assert detail["can_decide"] is row["can_decide"]
    reply = decide(h, eligible, role="coordinator_lead")
    assert reply["count"] == 4 and {row["stage"] for row in reply["records"]} == {"approved"}
    assert not any(row["can_decide"] for row in h.get("coordinator_lead", "/api/requests/all")["items"])


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_requests_decision_eligibility_uses_bounded_queries(database, request):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import event
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = setup_residency(request.getfixturevalue(database))
    with transaction(h.app.state.engine, write=True) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
        start = datetime(2026, 10, 4, tzinfo=timezone.utc)
        db.add_all([m.RequestRecord(department_id=h.seed["department_id"], person_id=people[0], kind="leave",
            start=start, end=start + timedelta(days=1), created_by=h.seed["identities"]["staff"],
            private_reason="Synthetic query-budget fixture") for _ in range(100)])
    identity(h, "coordinator_lead", ["coordinator", "programme_lead"], person=director)
    h.client("coordinator_lead")
    selects = []

    def count_select(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(h.app.state.engine, "before_cursor_execute", count_select)
    try:
        h.get("coordinator_lead", "/api/requests/all?limit=1")
        small = len(selects)
        selects.clear()
        large = h.get("coordinator_lead", "/api/requests/all?limit=100")
        assert len(selects) <= small + 2, (small, len(selects))
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", count_select)
    assert len(large["items"]) == 100 and all(row["can_decide"] for row in large["items"])


def test_board_change_permissions_match_requests_and_decision_commands(r):
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    with transaction(h.app.state.engine) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
    lead_id = identity(h, "coordinator_lead", ["coordinator", "programme_lead"], person=director)
    identity(h, "alternate_lead", ["coordinator"], person=director)
    other, oc, membership = other_programme(h, h.seed["people"][2])
    specs = [
        (people[0], c, "2026-10-01", "2026-11-01", "staff"),
        (people[1], c, "2026-10-01", "2026-11-01", "coordinator_lead"),
        (people[0], c, "2026-11-01", "2026-12-01", "alternate_lead"),
        (director, c, "2026-10-01", "2026-11-01", "coordinator"),
        (h.seed["people"][2], oc, "2026-10-01", "2026-11-01", "coordinator"),
    ]
    changes = [change(h, h.configure("rotations", rotation_value(pid, cohort, types[0], start, end))["record"],
                      types[1], role=role)["record"] for pid, cohort, start, end, role in specs]
    path = "/api/rotation-board?start=2026-10-01&end=2026-12-01"
    for role in ("admin", "coordinator", "approver", "programme_lead", "coordinator_lead", "staff"):
        board = h.get(role, path)
        requests = {row["id"]: row["can_decide"] for row in h.get(role, "/api/requests/all")["items"]}
        assert all(row["can_decide"] is requests[row["id"]] for row in board["changes"])
    board = h.get("coordinator_lead", path)
    assert {row["id"] for row in board["changes"]} == {row["id"] for row in changes}
    assert {row["id"] for row in board["changes"] if row["can_decide"]} == {row["id"] for row in changes[:3]}
    for row in changes[3:]:
        h.command("coordinator_lead", "request.decide", {"decision": "reject", "reason": REASON},
                  row["id"], row["revision"], status=403)
    for row in changes[1:3]:
        h.command("coordinator_lead", "request.decide", {"decision": "reject", "reason": REASON},
                  row["id"], row["revision"])
    h.command("admin", "self_approval.grant", {"identity_id": lead_id, "actions": ["request.decide"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": "Synthetic time-limited programme cover"})
    assert {row["id"] for row in h.get("coordinator_lead", path)["changes"] if row["can_decide"]} == {
        changes[0]["id"], changes[3]["id"]}
    row = changes[4]
    h.command("coordinator_lead", "request.decide", {"decision": "reject", "reason": REASON},
              row["id"], row["revision"], status=403)
    row = changes[0]
    h.command("coordinator_lead", "request.decide", {"decision": "reject", "reason": REASON}, row["id"], row["revision"])
    assert row["id"] not in {item["id"] for item in h.get("coordinator_lead", path)["changes"]}


def test_board_note_person_ids_match_current_note_access(r):
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    with transaction(h.app.state.engine) as db:
        director = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
    identity(h, "coordinator_lead", ["coordinator", "programme_lead"], person=director)
    identity(h, "trainee_lead", ["coordinator", "programme_lead"], person=people[0])
    outsider = h.seed["people"][2]
    other, oc, membership = other_programme(h, outsider)
    former = h.configure("people", {"display_name": "Synthetic former trainee"})["record"]["id"]
    h.configure("programme_memberships", {"person_id": former, "programme_id": p["id"],
        "starts_on": "2026-01-01", "ends_on": "2026-10-02"})
    h.command("programme_lead", "trainee_note.create", {"person_id": people[0], "body": "Synthetic private note"})
    h.command("other_lead", "trainee_note.create", {"person_id": outsider, "body": "Synthetic other programme note"})
    path = "/api/rotation-board?start=2026-10-01&end=2026-11-01"
    for role in ("admin", "coordinator", "approver", "programme_lead", "coordinator_lead", "trainee_lead", "staff"):
        board = h.get(role, path)
        allowed = set(board["note_person_ids"])
        assert allowed <= {row["id"] for row in board["people"]}
        for person in board["people"]:
            h.get(role, f"/api/people/{person['id']}/trainee-notes", status=200 if person["id"] in allowed else 403)
            if person["id"] not in allowed:
                assert person["notes"] == 0
    assert set(h.get("coordinator_lead", path)["note_person_ids"]) == set(people)
    assert set(h.get("trainee_lead", path)["note_person_ids"]) == {people[1], director}
    assert former in h.get("admin", path)["note_person_ids"]
    assert h.get("coordinator_lead", path + "&programme_id=" + other["id"])["note_person_ids"] == []


def test_programme_lead_records_trainee_protected_time_only(r):
    h, p, cohort, types, people, blocks = r
    entry = {"start": "2026-10-20T08:00:00+03:00", "end": "2026-10-20T12:00:00+03:00", "private_reason": "Fictional teaching"}
    h.command("programme_lead", "availability.record", {**entry, "person_id": people[0], "kind": "protected_time"})
    h.command("programme_lead", "availability.record", {**entry, "person_id": people[0], "kind": "recorded_leave"}, status=403)
    h.command("programme_lead", "availability.record", {**entry, "person_id": h.seed["people"][2], "kind": "protected_time"}, status=403)


def test_trainee_notes_are_for_programme_leads_only(r):
    h, p, cohort, types, people, blocks = r
    note = h.command("programme_lead", "trainee_note.create", {"person_id": people[0], "body": "Fictional progress note"})["record"]
    h.command("coordinator", "trainee_note.create", {"person_id": people[0], "body": "Not allowed"}, status=403)
    h.get("staff", f"/api/people/{people[0]}/trainee-notes", status=403)
    h.get("coordinator", f"/api/people/{people[0]}/trainee-notes", status=403)
    assert [n["body"] for n in h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes")["notes"]] == ["Fictional progress note"]
    edited = h.command("programme_lead", "trainee_note.update", {"body": "Fictional edited note"}, note["id"], note["revision"])["record"]
    gone = h.command("programme_lead", "trainee_note.delete", {}, note["id"], edited["revision"])["record"]
    assert h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes")["notes"] == []
    h.command("programme_lead", "trainee_note.restore", {}, note["id"], gone["revision"])
    board = h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2027-01-01")
    assert {x["id"]: x["notes"] for x in board["people"]} == {people[0]: 1, people[1]: 0}


def test_trainee_leads_cannot_read_or_write_notes_about_their_linked_person(r):
    h, p, cohort, types, people, blocks = r
    for username in ("trainee_lead", "alternate_trainee_lead"):
        identity(h, username, ["staff", "programme_lead"], person=people[0])
    payload = {"person_id": people[0], "body": "Synthetic note by another programme lead"}
    note = h.command("programme_lead", "trainee_note.create", payload)["record"]
    for username in ("trainee_lead", "alternate_trainee_lead"):
        h.get(username, f"/api/people/{people[0]}/trainee-notes", status=403)
        h.command(username, "trainee_note.create", payload, status=403)
    assert h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes")["notes"][0]["id"] == note["id"]
    edited = h.command("programme_lead", "trainee_note.update", {"body": "Synthetic peer edit"}, note["id"], note["revision"])["record"]
    assert edited["body"] == "Synthetic peer edit"


def test_note_author_linked_to_subject_loses_mutation_receipt_and_undo_access(r):
    import uuid
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, cohort, types, people, blocks = r
    receipts = []
    note = None
    for kind, payload in (("create", {"person_id": people[0], "body": "Synthetic private note"}),
                          ("update", {"body": "Synthetic private edit"}), ("delete", {}), ("restore", {})):
        key = str(uuid.uuid4())
        target, revision = (note["id"], note["revision"]) if note else (None, None)
        result = h.command("programme_lead", "trainee_note." + kind, payload, target, revision, key=key)
        note = result["record"]
        assert h.get("programme_lead", "/api/commands/" + key)["result"] == result
        receipts.append((kind, payload, target, revision, key))
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id = people[0]
    h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes", status=403)
    for kind in ("update", "delete", "restore"):
        h.command("programme_lead", "trainee_note." + kind, {"body": "Self edit"} if kind == "update" else {},
                  note["id"], note["revision"], status=403)
    for kind, payload, target, revision, key in receipts:
        h.get("programme_lead", "/api/commands/" + key, status=403)
        h.command("programme_lead", "trainee_note." + kind, payload, target, revision, key=key, status=403)


def test_rotation_template_is_a_programme_catalogue(r):
    h, p, cohort, types, people, blocks = r
    saved = h.configure("rotation_templates", {"name": "Fictional year plan", "programme_id": p["id"], "blocks": blocks},
                        role="programme_lead")["record"]
    assert saved["blocks"][0]["unit"] == "months"
    h.configure("rotation_templates", {"name": "No blocks", "blocks": []}, role="programme_lead", status=422)
    h.configure("rotation_templates", {"name": "Staff plan", "blocks": blocks}, role="staff", status=403)


def other_programme(h, pid):
    p = h.configure("programmes", {"name": "Another fictional programme", "starts_on": "2026-01-01"})["record"]
    lead_of(h, p, "other_lead")
    c = h.configure("cohorts", {"name": "Another fictional cohort", "programme_id": p["id"],
        "starts_on": "2026-07-01", "ends_on": "2028-07-01"})["record"]
    membership = h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"],
        "starts_on": "2026-07-01", "ends_on": "2028-07-01"})["record"]
    return p, c, membership


def rotation_value(pid, cohort, typ, start="2026-10-01", end="2026-11-01"):
    return {"person_id": pid, "cohort_id": cohort["id"], "rotation_type_id": typ["id"],
            "starts_on": start, "ends_on": end}


def change(h, rotation, typ, role="staff", key=None, status=200):
    return h.command(role, "rotation.request_change", {"rotation_id": rotation["id"], "rotation_type_id": typ["id"],
        "starts_on": rotation["starts_on"], "ends_on": rotation["ends_on"], "private_reason": REASON}, key=key, status=status)


def test_director_commands_reject_other_programme_and_own_authority_edits(r):
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, people[0])
    rot = h.configure("rotations", rotation_value(people[0], oc, types[0]))["record"]
    plan(h, oc, people[:1], blocks, status=403)
    change(h, rot, types[1], role="programme_lead", status=403)
    h.command("programme_lead", "rotation.copy", {"starts_on": "2026-10-01", "ends_on": "2026-11-01",
        "programme_id": other["id"], "reason": REASON}, status=403)
    h.command("programme_lead", "rotation.remove", {"items": [{"id": rot["id"], "revision": rot["revision"]}],
        "reason": REASON}, status=403)
    h.configure("rotations", rotation_value(people[0], c, types[0]), rot["id"], rot["revision"], role="programme_lead", status=403)
    h.command("programme_lead", "catalogue.bulk_update", {"catalogue": "cohorts", "items": [{"id": oc["id"], "revision": oc["revision"]}],
        "set": {"name": "Unauthorised rename"}, "reason": REASON}, status=403)
    h.configure("programme_memberships", {"person_id": people[1], "programme_id": other["id"],
        "starts_on": "2026-01-01", "ends_on": "2026-07-01"}, role="programme_lead", status=403)
    own = next(x for x in h.get("programme_lead", "/api/catalogues")["programme_memberships"] if x["person_id"] not in people)
    value = {k: own[k] for k in ("person_id", "programme_id", "starts_on", "ends_on", "external", "home_programme")}
    h.configure("programme_memberships", {**value, "ends_on": "2028-01-01"}, own["id"], own["revision"], role="programme_lead", status=403)
    h.configure("rotation_templates", {"name": "Global template", "blocks": blocks}, role="programme_lead", status=403)
    h.configure("rotation_types", {"name": "Global type", "starts_on": "2026-01-01"}, role="programme_lead", status=403)


def test_director_reads_and_cache_are_scoped_even_for_dual_membership(r):
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, people[0])
    own = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    foreign = h.configure("rotations", rotation_value(people[0], oc, types[0], "2026-11-01", "2026-12-01"))["record"]
    asked = change(h, foreign, types[1])["record"]
    # Warm every cache in the opposite order to its next reader.
    assert len(h.get("coordinator", "/api/catalogues")["rotations"]) == 2
    assert {x["id"] for x in h.get("other_lead", "/api/catalogues")["rotations"]} == {foreign["id"]}
    own_lists = h.get("programme_lead", "/api/catalogues")
    assert {x["id"] for x in own_lists["rotations"]} == {own["id"]}
    assert {x["programme_id"] for x in own_lists["programme_memberships"]} == {p["id"]}
    board = h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2026-12-01")
    assert {x["id"] for x in board["rotations"]} == {own["id"]}
    assert board["changes"] == []
    listing = h.get("programme_lead", "/api/requests/all")
    assert listing["items"] == [] and listing["counts"]["to_decide"] == 0
    h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"], status=403)
    h.get("programme_lead", f"/api/catalogues/rotations/{foreign['id']}/history", status=404)
    calendar = h.get("programme_lead", "/api/calendar?start=2026-11-01&end=2026-12-01")
    assert not any(e["id"] == foreign["id"] for e in calendar["events"])


@pytest.mark.parametrize("role", ["programme_lead", "unlinked_lead"])
def test_director_rota_reads_resolve_directory_people_without_trainee_access(r, role):
    h, p, c, types, people, blocks = r
    h.configure('rotation_types', {**{key: types[0][key] for key in s.RotationTypeInput.model_fields},
        'leave_policy': 'needs_approval'}, types[0]['id'], types[0]['revision'])
    if role == "unlinked_lead":
        identity(h, role, ["programme_lead"])
    outsider = h.seed["people"][2]
    other, oc, membership = other_programme(h, outsider)
    rotation = h.configure("rotations", rotation_value(outsider, oc, types[0]))["record"]
    h.command("other_lead", "trainee_note.create", {"person_id": outsider, "body": "Other programme's private note"})
    request = h.command("coordinator", "request.create", {"person_id": outsider,
        "start": "2026-10-20T08:00:00+03:00", "end": "2026-10-20T12:00:00+03:00",
        "private_reason": REASON, "leave_type_id": h.get("staff", "/api/leave-types")["leave_types"][0]["id"]})["record"]
    rule = h.policy({"kind": "max_hours", "hours": 2, "window": "day"}, {"person_id": outsider})
    start, end = "2026-10-04T08:00:00+03:00", "2026-10-04T12:00:00+03:00"
    w = h.shift(h.workspace(), people=[
        {"person_id": people[0], "start": start, "end": end, "participation": "trainee", "supervisor_person_id": outsider},
        {"person_id": outsider, "start": start, "end": end, "participation": "supervisor"}])["workspace"]
    reference = {x["id"]: x for x in h.get("coordinator", "/api/catalogues")["people"]}
    # Warm another director's cache: shared names must not share their programme facts.
    assert next(x for x in h.get("other_lead", "/api/catalogues")["people"] if x["id"] == outsider)["current_programme_ids"] == [other["id"]]
    full = h.get(role, "/api/catalogues")
    for path in ("/api/catalogues?boot=true", "/api/catalogues?known="):
        directory = {x["id"]: x for x in h.get(role, path)["people"]}
        assert directory == {x["id"]: x for x in full["people"]}
        assert set(directory) == set(reference)
        for pid in (people[0], outsider):
            for field in ("display_name", "display_title", "current_category_ids"):
                assert directory[pid][field] == reference[pid][field]
        assert directory[outsider]["current_category_ids"]
        for field in ("current_programme_ids", "current_cohort_ids", "current_rotation_type_ids"):
            assert directory[outsider][field] == []
    assert other["id"] not in {x["id"] for x in full["programmes"]}
    assert membership["id"] not in {x["id"] for x in full["programme_memberships"]}
    assert rotation["id"] not in {x["id"] for x in full["rotations"]}
    assert h.get(role, "/api/people/summary") == h.get("coordinator", "/api/people/summary")
    assert w["id"] in {x["id"] for x in h.get(role, "/api/workspaces")["workspaces"]}
    detail = h.get(role, f"/api/workspaces/{w['id']}")
    shift = detail["shifts"][0]
    assert {x["person_id"] for x in shift["participants"]} == {people[0], outsider}
    assert next(x for x in shift["participants"] if x["person_id"] == people[0])["supervisor_person_id"] == outsider
    summary = h.get(role, f"/api/workspaces/{w['id']}/summary")
    assert set(summary["sessions"][0]["person_ids"]) == {people[0], outsider}
    mapped = h.get(role, f"/api/workspaces/{w['id']}/rule-map")
    assert outsider in next(x for x in mapped["rules"] if x["id"] == rule["id"])["people"]
    applied = h.get(role, f"/api/workspaces/{w['id']}/shifts/{shift['id']}/rules")
    assert {x["person_id"] for x in applied["people"]} == {people[0], outsider}
    overlaps = h.get(role, "/api/schedule/overlaps?starts_on=2026-10-04&ends_on=2026-10-11")
    assert w["id"] in {x["id"] for x in overlaps["drafts"]}
    h.get(role, f"/api/people/{outsider}/trainee-notes", status=403)
    assert h.get(role, "/api/requests/all")["items"] == []
    assert outsider not in {x["id"] for x in h.get(role, "/api/rotation-board?start=2026-10-01&end=2026-11-01")["people"]}
    h.configure("people", {"display_name": "Forbidden directory edit"}, outsider, reference[outsider]["revision"], role=role, status=403)
    h.configure("rotations", rotation_value(outsider, oc, types[1]), rotation["id"], rotation["revision"], role=role, status=403)
    h.command(role, "request.decide", {"decision": "approve", "reason": REASON}, request["id"], request["revision"], status=403)


def test_catalogue_history_does_not_expose_previous_foreign_programme(r):
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, people[0])
    moved = h.configure("programme_memberships", {"person_id": people[1], "programme_id": other["id"],
        "starts_on": "2026-01-01", "ends_on": "2026-06-01"})["record"]
    h.configure("programme_memberships", {"person_id": people[1], "programme_id": p["id"],
        "starts_on": "2026-01-01", "ends_on": "2026-06-01"}, moved["id"], moved["revision"])
    history = h.get("programme_lead", f"/api/catalogues/programme_memberships/{moved['id']}/history")["versions"]
    assert len(history) == 1 and history[0]["snapshot"]["programme_id"] == p["id"]


def test_notes_scope_author_audit_and_receipt_reauthorization(r):
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select
    import uuid
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, h.seed["people"][2])
    outsider = membership["person_id"]
    h.command("programme_lead", "trainee_note.create", {"person_id": outsider, "body": "Forbidden note"}, status=403)
    h.get("programme_lead", f"/api/people/{outsider}/trainee-notes", status=403)
    key = str(uuid.uuid4())
    note = h.command("programme_lead", "trainee_note.create", {"person_id": people[0], "body": "Private progress"}, key=key)["record"]
    h.command("other_lead", "trainee_note.update", {"body": "Foreign edit"}, note["id"], note["revision"], status=403)
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.target_id == note["id"], m.AuditEvent.action == "trainee_note.create"))
        assert event.detail["person_id"] == people[0] and "Private progress" not in str(event.detail)
        lead = db.get(m.Identity, h.seed["identities"]["programme_lead"])
        director_membership = db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == lead.person_id))
        director_membership.ends_on = director_membership.starts_on.replace(month=2)
        director_membership.revision += 1
    h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes", status=403)
    for kind in ("update", "delete", "restore"):
        h.command("programme_lead", "trainee_note." + kind, {"body": "Former edit"} if kind == "update" else {},
            note["id"], note["revision"], status=403)
    h.get("programme_lead", "/api/commands/" + key, status=403)
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == []


def test_director_extra_auditor_role_does_not_expand_writes_or_notes(r):
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, h.seed["people"][2])
    with transaction(h.app.state.engine) as db:
        lead = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
    identity(h, "lead_auditor", ["programme_lead", "auditor"], person=lead)
    plan(h, oc, [membership["person_id"]], blocks, role="lead_auditor", status=403)
    h.command("lead_auditor", "trainee_note.create", {"person_id": membership["person_id"], "body": "No broadening"}, status=403)


def test_director_without_linked_person_leads_nothing(r):
    h, p, c, types, people, blocks = r
    identity(h, "unlinked_lead", ["programme_lead"])
    assert h.get("unlinked_lead", "/api/rotation-board?start=2026-10-01&end=2026-11-01")["people"] == []
    plan(h, c, people[:1], blocks, role="unlinked_lead", status=403)
    h.get("unlinked_lead", f"/api/people/{people[0]}/trainee-notes", status=403)


def test_rotation_validation_covers_overlap_membership_and_unknown_skipped_blocks(r):
    import uuid
    h, p, c, types, people, blocks = r
    rot = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    h.configure("rotations", rotation_value(people[0], c, types[1], "2026-10-15", "2026-11-15"), status=409)
    h.configure("rotations", rotation_value(h.seed["people"][2], c, types[0]), status=422)
    h.command("staff", "rotation.request_change", {"rotation_id": rot["id"], "rotation_type_id": types[1]["id"],
        "starts_on": "2026-10-15", "ends_on": "2026-11-15", "private_reason": REASON})
    h.configure("rotations", rotation_value(people[0], c, types[0], "2026-11-01", "2026-12-01"))
    plan(h, c, people[:1], [{"rotation_type_id": str(uuid.uuid4()), "length": 52, "unit": "months"}],
        start="2027-06-01", status=404)
    plan(h, c, [str(uuid.uuid4())], blocks, status=404)
    plan(h, c, people[:1], blocks, start="9999-12-01", status=422)


def test_rotation_approval_and_undo_preserve_revision_guards(r):
    h, p, c, types, people, blocks = r
    rot = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    asked = change(h, rot, types[1])["record"]
    edited = h.configure("rotations", rotation_value(people[0], c, types[0], end="2026-10-25"), rot["id"], rot["revision"])["record"]
    h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"], status=409)
    assert next(x for x in h.get("coordinator", "/api/catalogues")["rotations"] if x["id"] == rot["id"])["revision"] == edited["revision"]


def test_rotation_membership_adjacent_spans_are_allowed_but_gaps_are_not(r):
    h, p, c, types, people, blocks = r
    pid = h.seed["people"][2]
    for start, end in (("2026-10-01", "2026-10-15"), ("2026-10-15", "2026-11-01")):
        h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"], "starts_on": start, "ends_on": end})
    h.configure("rotations", rotation_value(pid, c, types[0]))
    h.configure("rotations", rotation_value(pid, c, types[0], "2026-11-01", "2026-12-01"), status=422)


@pytest.mark.parametrize("kind", ["meeting", "administrative", "clinic_change"])
def test_director_cannot_decide_non_residency_requests(r, kind):
    h, p, c, types, people, blocks = r
    asked = h.command("staff", "request.create", {"person_id": people[0], "kind": kind,
        "start": "2026-10-10T08:00:00+03:00", "end": "2026-10-10T10:00:00+03:00", "private_reason": REASON})["record"]
    h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"], status=403)
    assert h.get("programme_lead", "/api/requests/all")["items"] == []


def test_alternate_identity_cannot_approve_own_rotation_proposal(r):
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    pid = people[0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id = pid
    identity(h, "second_lead_login", ["programme_lead"], person=pid)
    rot = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    asked = change(h, rot, types[1], role="programme_lead")["record"]
    h.command("second_lead_login", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"], status=403)
    assert h.get("second_lead_login", "/api/requests/all")["counts"]["to_decide"] == 0


def test_director_batch_receipts_replay_and_current_authority_is_required(r):
    import uuid
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select
    h, p, c, types, people, blocks = r
    rot = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    asked = change(h, rot, types[1])["record"]
    key = str(uuid.uuid4())
    payload = {"items": [{"id": asked["id"], "source": "request", "expected_revision": asked["revision"]}],
               "decision": "approve", "reason": REASON}
    result = h.command("programme_lead", "request.decide_many", payload, key=key)
    assert h.command("programme_lead", "request.decide_many", payload, key=key) == result
    h.get("programme_lead", "/api/commands/" + key)
    with transaction(h.app.state.engine, write=True) as db:
        pd = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
        db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == pd)).ends_on = date(2026, 2, 1)
    h.command("programme_lead", "request.undo", {"batch_id": result["batch_id"], "reason": REASON}, status=403)
    h.command("programme_lead", "request.decide_many", payload, key=key, status=403)
    h.get("programme_lead", "/api/commands/" + key, status=403)


def test_protected_time_uses_local_dates_and_exclusive_end(r):
    h, p, c, types, people, blocks = r
    pid = h.seed["people"][2]
    h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"],
        "starts_on": "2026-10-20", "ends_on": "2026-10-21"})
    entry = {"person_id": pid, "kind": "protected_time", "start": "2026-10-19T21:00:00Z",
        "end": "2026-10-20T21:00:00Z", "private_reason": REASON}
    h.command("programme_lead", "availability.record", entry)
    h.command("programme_lead", "availability.record", {**entry, "start": "2026-10-19T20:59:59Z"}, status=403)
    h.command("programme_lead", "availability.record", {**entry, "end": "2026-10-20T21:00:01Z"}, status=403)
    asked = h.command("coordinator", "request.create", {"person_id": pid, "start": "2026-10-19T21:00:00Z",
        "end": "2026-10-20T21:00:00Z", "private_reason": REASON,
        "leave_type_id": h.get("staff", "/api/leave-types")["leave_types"][0]["id"]})["record"]
    h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"])


def test_external_membership_is_validated_and_never_grants_an_account(r):
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select, func
    h, p, c, types, people, blocks = r
    pid = h.configure("people", {"display_name": "Fictional incoming visitor"})["record"]["id"]
    base = {"person_id": pid, "programme_id": p["id"], "starts_on": "2026-10-01", "ends_on": "2026-11-01"}
    h.configure("programme_memberships", {**base, "external": True}, status=422)
    h.configure("programme_memberships", {**base, "home_programme": "Unmarked visitor"}, status=422)
    h.configure("programme_memberships", {**base, "external": True, "home_programme": "Fictional outside programme"}, role="programme_lead")
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.Identity).where(m.Identity.person_id == pid)) == 0
    assert h.client("staff").post("/auth/login", json={"username": pid, "password": "fictional"}).status_code == 401
    rot = h.configure("rotations", rotation_value(pid, c, types[0]))["record"]
    assert rot["department_available"]


def test_residency_public_contracts_are_generated():
    import json
    from pathlib import Path
    from scripts.generate_contracts import render
    content, schemas = render()
    assert s.MAX_CATALOGUE_ROTATIONS >= s.MAX_ROTATIONS_PER_PLAN
    for name in ("PersonRotations", "RotationBoard", "RotationBlock", "RotationTemplateInput", "RotationPlanInput", "RotationCopyInput", "RotationRemoveInput",
                 "RotationChangeInput", "TraineeNoteInput", "TraineeNoteEdit"):
        assert name in schemas and "export type " + name in content
    assert "choices" in schemas["RotationBoard"]["required"] and "export type RotationBoardChoices" in content
    assert schemas["RotationRemoveInput"]["properties"]["items"]["maxItems"] == s.MAX_ROTATIONS_PER_PLAN == (
        schemas["RotationPlanInput"]["properties"]["person_ids"]["maxItems"] * schemas["RotationPlanInput"]["properties"]["blocks"]["maxItems"])
    assert schemas == json.loads(Path("packages/contracts/public-schemas.json").read_text())


def test_rotation_undo_accepts_the_largest_plan_and_rejects_more():
    import uuid
    from pydantic import ValidationError
    item = {"id": str(uuid.uuid4()), "revision": 1}
    assert len(s.RotationRemoveInput(items=[item] * s.MAX_ROTATIONS_PER_PLAN, reason=REASON).items) == 10400
    with pytest.raises(ValidationError):
        s.RotationRemoveInput(items=[item] * (s.MAX_ROTATIONS_PER_PLAN + 1), reason=REASON)


@pytest.mark.postgres
def test_residency_postgres_permissions_change_and_undo(pg):
    h, p, c, types, people, blocks = setup_residency(pg)
    first = plan(h, c, people, blocks)
    rot = next(x for x in first["created"] if x["person_id"] == people[0] and x["starts_on"] == "2026-10-01")
    asked = change(h, rot, types[1])["record"]
    listed = h.get("programme_lead", "/api/requests/all")
    assert listed["counts"]["to_decide"] == 1 and asked["id"] in {x["id"] for x in listed["items"]}
    decided = h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, asked["id"], asked["revision"])["record"]
    h.command("programme_lead", "request.undo", {"reason": REASON}, asked["id"], decided["revision"])
    note = h.command("programme_lead", "trainee_note.create", {"person_id": people[0], "body": "Synthetic PG note"})["record"]
    other, oc, membership = other_programme(h, h.seed["people"][2])
    h.get("other_lead", f"/api/people/{people[0]}/trainee-notes", status=403)
    plan(h, oc, [membership["person_id"]], blocks, status=403)
    assert h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes")["notes"][0]["id"] == note["id"]


def test_copy_overflow_and_unknown_filters_are_rejected(r):
    from datetime import date
    import uuid
    h, p, c, types, people, blocks = r
    far = h.configure("cohorts", {"name": "Far future synthetic cohort", "programme_id": p["id"],
        "starts_on": "9999-01-01", "ends_on": "9999-12-31"})["record"]
    h.configure("programme_memberships", {"person_id": people[0], "programme_id": p["id"],
        "starts_on": "9999-01-01", "ends_on": "9999-12-31"})
    h.configure("rotations", rotation_value(people[0], far, types[0], "9999-01-01", "9999-02-01"))
    h.command("programme_lead", "rotation.copy", {"starts_on": "9999-01-01", "ends_on": "9999-12-31", "reason": REASON}, status=422)
    h.command("programme_lead", "rotation.copy", {"starts_on": "2026-10-01", "ends_on": "2026-11-01",
        "person_ids": [str(uuid.uuid4())], "reason": REASON}, status=404)
    h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2026-11-01&programme_id=" + str(uuid.uuid4()), status=404)


def test_external_rotators_use_canonical_rank_eligibility_protection_and_fairness(r):
    from datetime import date
    from packages.domain.fairness import member_days
    from packages.domain.validation import evaluate
    h, p, c, types, people, blocks = r
    pid = h.configure("people", {"display_name": "Synthetic external clinician"})["record"]["id"]
    h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"], "starts_on": "2026-10-01",
        "ends_on": "2026-11-01", "external": True, "home_programme": "Other institution"})
    w = h.shift(h.workspace(), people=[pid])["workspace"]
    data = h.snapshot(w)
    assert "NOT_ELIGIBLE" in {f["code"] for f in evaluate(data)["findings"]}
    assert member_days(data, pid, date(2026, 10, 4), date(2026, 10, 5)) == []
    h.command("coordinator", "membership.create", {"person_id": pid, "category_id": h.seed["category"],
        "starts_on": "2026-10-01", "ends_on": "2026-11-01"})
    h.command("credentialer", "eligibility.create", {"person_id": pid, "service_id": h.seed["services"][0],
        "starts_on": "2026-10-01", "ends_on": "2026-11-01"})
    data = h.snapshot(w)
    assert member_days(data, pid, date(2026, 10, 4), date(2026, 10, 5)) == [date(2026, 10, 4)]
    assert "NOT_ELIGIBLE" not in {f["code"] for f in evaluate(data)["findings"]}
    h.command("programme_lead", "availability.record", {"person_id": pid, "kind": "protected_time",
        "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00", "private_reason": REASON})
    assert "UNAVAILABLE" in {f["code"] for f in evaluate(h.snapshot(w))["findings"]}
    h.configure("rotations", {**rotation_value(pid, c, types[0]), "department_available": False})
    data = h.snapshot(w)
    assert "EXTERNAL_ROTATION" in {f["code"] for f in evaluate(data)["findings"]}
    assert member_days(data, pid, date(2026, 10, 4), date(2026, 10, 5)) == []


def test_plan_and_notes_batch_reads_at_department_scale(r):
    check_plan_and_notes_batch_reads_at_department_scale(r)


@pytest.mark.parametrize('h', [pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
def test_plan_and_notes_batch_reads_at_department_scale_on_postgresql(r):
    check_plan_and_notes_batch_reads_at_department_scale(r)


def check_plan_and_notes_batch_reads_at_department_scale(r):
    from datetime import date
    from sqlalchemy import event, select, func
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    with transaction(h.app.state.engine, write=True) as db:
        rows = [m.Person(organisation_id=h.seed["organisation_id"], display_name=f"Synthetic trainee {i}") for i in range(150)]
        db.add_all(rows)
        db.flush()
        ids = [row.id for row in rows]
        db.add_all([m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=pid, programme_id=p["id"],
            starts_on=date(2026, 7, 1), ends_on=date(2028, 7, 1)) for pid in ids])
        authors = [m.Identity(issuer="local-development", subject=f"synthetic-author-{i}", display_name=f"Synthetic author {i}") for i in range(150)]
        db.add_all(authors)
        db.flush()
        db.add_all([m.TraineeNote(department_id=h.seed["department_id"], person_id=people[0], author_id=author.id, body="Synthetic note") for author in authors])
        before = db.get(m.Organisation, h.seed["organisation_id"]).input_revision
    h.client("programme_lead")
    selects = []
    def count_select(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)
    event.listen(h.app.state.engine, "before_cursor_execute", count_select)
    try:
        result = plan(h, c, ids, blocks)
        plan_reads = len(selects)
        selects.clear()
        notes = h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes")["notes"]
        note_reads = len(selects)
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", count_select)
    assert len(result["created"]) == 300 and plan_reads < 40
    assert len(notes) == 150 and note_reads < 20
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Organisation, h.seed["organisation_id"]).input_revision == before + 1
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory).where(m.DefinitionHistory.kind == "rotations")) == 300
    nxt = h.configure("cohorts", {"name": "Synthetic next cohort", "programme_id": p["id"],
        "starts_on": "2027-07-01", "ends_on": "2028-07-01"})["record"]
    selects.clear()
    event.listen(h.app.state.engine, "before_cursor_execute", count_select)
    try:
        copied = h.command("programme_lead", "rotation.copy", {"starts_on": "2026-10-01", "ends_on": "2026-12-01",
            "person_ids": ids, "cohort_id": nxt["id"], "reason": REASON})
        copy_reads = len(selects)
        selects.clear()
        removed = h.command("programme_lead", "rotation.undo_plan", {"items": [{"id": r["id"], "revision": r["revision"]}
            for r in copied["created"]], "reason": REASON})
        remove_reads = len(selects)
    finally:
        event.remove(h.app.state.engine, "before_cursor_execute", count_select)
    assert len(copied["created"]) == 300 and copy_reads < 40, copy_reads
    print('rotation reads', {'plan': plan_reads, 'notes': note_reads, 'copy': copy_reads, 'remove': remove_reads})
    assert removed["removed"] == 300 and remove_reads < 40, remove_reads
    with transaction(h.app.state.engine) as db:
        assert db.scalar(select(func.count()).select_from(m.DefinitionHistory).where(m.DefinitionHistory.kind == "rotations")) == 900


def test_board_and_calendar_keep_leave_inside_director_membership_dates(r):
    h, p, c, types, people, blocks = r
    pid = h.seed["people"][2]
    h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"],
        "starts_on": "2026-10-01", "ends_on": "2026-10-15"})
    h.command("coordinator", "availability.record", {"person_id": pid, "kind": "protected_time",
        "start": "2026-10-20T08:00:00+03:00", "end": "2026-10-20T10:00:00+03:00", "private_reason": REASON})
    h.command("coordinator", "request.create", {"person_id": pid, "start": "2026-10-20T00:00:00+03:00",
        "end": "2026-10-21T00:00:00+03:00", "private_reason": REASON})
    board = h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2026-11-01")
    assert pid in {p["id"] for p in board["people"]} and board["leave"] == []
    cal = h.get("programme_lead", "/api/calendar?start=2026-10-01&end=2026-11-01")
    assert not any(e["kind"] == "availability" for e in cal["events"])


def test_board_programme_filter_applies_to_pending_changes(r):
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, people[0])
    with transaction(h.app.state.engine, write=True) as db:
        pid = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
        db.add(m.ProgrammeMembership(department_id=h.seed["department_id"], person_id=pid, programme_id=other["id"],
            starts_on=date(2026, 1, 1), ends_on=date(2027, 1, 1)))
    rot = h.configure("rotations", rotation_value(people[0], oc, types[0]))["record"]
    change(h, rot, types[1])
    board = h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2026-11-01&programme_id=" + p["id"])
    assert board["changes"] == []


@pytest.mark.parametrize("kind", ["catalogue", "availability", "leave_undo"])
def test_saved_receipts_require_current_director_scope(r, kind):
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select
    from datetime import date
    import uuid
    h, p, c, types, people, blocks = r
    key = str(uuid.uuid4())
    if kind == "catalogue":
        command = "catalogue.create"
        payload = {"catalogue": "rotation_templates", "value": {"name": "Synthetic plan", "programme_id": p["id"], "blocks": blocks}, "reason": REASON}
        target = revision = None
    elif kind == "availability":
        command = "availability.record"
        payload = {"person_id": people[0], "kind": "protected_time", "start": "2026-10-20T08:00:00+03:00",
            "end": "2026-10-20T10:00:00+03:00", "private_reason": REASON}
        target = revision = None
    else:
        request = h.command("staff", "request.create", {"person_id": people[0], "start": "2026-10-20T00:00:00+03:00",
            "end": "2026-10-21T00:00:00+03:00", "private_reason": REASON})["record"]
        decided = h.command("programme_lead", "request.decide", {"decision": "approve", "reason": REASON}, request["id"], request["revision"])["record"]
        command, payload, target, revision = "request.undo", {"reason": REASON}, request["id"], decided["revision"]
    result = h.command("programme_lead", command, payload, target, revision, key=key)
    assert h.command("programme_lead", command, payload, target, revision, key=key) == result
    with transaction(h.app.state.engine, write=True) as db:
        pd = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
        db.scalar(select(m.ProgrammeMembership).where(m.ProgrammeMembership.person_id == pd)).ends_on = date(2026, 2, 1)
    h.get("programme_lead", "/api/commands/" + key, status=403)
    h.command("programme_lead", command, payload, target, revision, key=key, status=403)


def test_cache_identity_relink_and_cohort_reassignment_do_not_leak(r):
    from apps.api import models as m
    from apps.api.db import transaction
    from sqlalchemy import select
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, people[0])
    rot = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    assert len(h.get("programme_lead", "/api/catalogues")["rotations"]) == 1
    h.configure("cohorts", {"name": c["name"], "programme_id": other["id"], "starts_on": c["starts_on"],
        "ends_on": c["ends_on"]}, c["id"], c["revision"])
    assert h.get("programme_lead", "/api/catalogues")["rotations"] == []
    # Old rotation history records its programme as written, even if the cohort subsequently moves.
    assert h.get("other_lead", f"/api/catalogues/rotations/{rot['id']}/history")["versions"] == []
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id = db.get(m.Identity, h.seed["identities"]["other_lead"]).person_id
    assert {x["id"] for x in h.get("programme_lead", "/api/catalogues")["rotations"]} == {rot["id"]}


def test_extreme_protected_time_is_a_validation_error(r):
    h, p, c, types, people, blocks = r
    h.command("programme_lead", "availability.record", {"person_id": people[0], "kind": "protected_time",
        "start": "9999-12-31T08:00:00+03:00", "end": "9999-12-31T12:00:00+03:00", "private_reason": REASON}, status=422)


@pytest.mark.parametrize("role", s.ROLE_NAMES)
def test_note_visibility_uses_authoritative_roles(r, role):
    h, p, c, types, people, blocks = r
    h.command("programme_lead", "trainee_note.create", {"person_id": people[0], "body": "Director-only synthetic note"})
    permitted = role in {"admin", "programme_lead"}
    result = h.get(role, f"/api/people/{people[0]}/trainee-notes", status=200 if permitted else 403)
    if permitted:
        assert result["notes"][0]["body"] == "Director-only synthetic note"


@pytest.mark.parametrize("starts,ends", [("2026-07-01", "2026-10-03"), ("2026-10-04", "2028-07-01")])
def test_notes_require_current_trainee_enrolment_after_transfer(r, starts, ends):
    import uuid
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    key = str(uuid.uuid4())
    payload = {"person_id": people[0], "body": "Synthetic note before transfer"}
    note = h.command("programme_lead", "trainee_note.create", payload, key=key)["record"]
    other_programme(h, people[0])
    with transaction(h.app.state.engine, write=True) as db:
        membership = db.scalar(select(m.ProgrammeMembership).where(
            m.ProgrammeMembership.person_id == people[0], m.ProgrammeMembership.programme_id == p["id"]))
        membership.starts_on, membership.ends_on = date.fromisoformat(starts), date.fromisoformat(ends)
    h.command("other_lead", "trainee_note.create", {"person_id": people[0], "body": "Synthetic current programme note"})
    assert len(h.get("other_lead", f"/api/people/{people[0]}/trainee-notes")["notes"]) == 2
    h.get("programme_lead", f"/api/people/{people[0]}/trainee-notes", status=403)
    h.command("programme_lead", "trainee_note.create", payload, status=403)
    for kind in ("update", "delete", "restore"):
        h.command("programme_lead", "trainee_note." + kind, {"body": "Unauthorised edit"} if kind == "update" else {},
            note["id"], note["revision"], status=403)
    h.get("programme_lead", "/api/commands/" + key, status=403)
    h.command("programme_lead", "trainee_note.create", payload, key=key, status=403)
    board = h.get("programme_lead", "/api/rotation-board?start=2026-10-01&end=2026-11-01")
    assert next(person for person in board["people"] if person["id"] == people[0])["notes"] == 0


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("linked", [False, True])
def test_programme_creator_owns_it_without_becoming_a_trainee(request, database, linked):
    import uuid
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    h = request.getfixturevalue(database)
    person = h.seed["people"][2] if linked else None
    owner = identity(h, "programme_owner", ["programme_lead"], person=person)
    peer = identity(h, "programme_peer", ["programme_lead"], person=person)
    before = h.get("admin", "/api/catalogues")
    assert h.get("programme_owner", "/api/catalogues?boot=true")["programmes"] == []
    key = str(uuid.uuid4())
    payload = {"catalogue": "programmes", "value": {"name": "Owner's fictional programme", "starts_on": "2026-01-01"}, "reason": REASON}
    saved = h.command("programme_owner", "catalogue.create", payload, key=key)["record"]
    assert saved["owner_identity_id"] == owner
    assert h.command("programme_owner", "catalogue.create", payload, key=key)["record"]["id"] == saved["id"]
    for path in ("/api/catalogues", "/api/catalogues?boot=true", "/api/catalogues?known="):
        assert [x["id"] for x in h.get("programme_owner", path)["programmes"]] == [saved["id"]]
        assert h.get("programme_peer", path)["programmes"] == []
    me = h.get("programme_owner", "/api/me")
    assert me["person_id"] == person and me["roles"] == ["programme_lead"] and not me["trainee"]
    after = h.get("admin", "/api/catalogues")
    for kind in ("people", "memberships", "programme_memberships", "eligibilities", "grants", "policies"):
        assert after[kind] == before[kind]
    edited = h.configure("programmes", {**payload["value"], "name": "Owner's revised programme"},
        saved["id"], saved["revision"], role="programme_owner")["record"]
    assert edited["owner_identity_id"] == owner
    h.configure("programmes", payload["value"], saved["id"], edited["revision"], role="programme_peer", status=403)
    h.configure("programmes", {**payload["value"], "owner_identity_id": peer}, role="programme_owner", status=422)
    h.configure("programmes", {**payload["value"], "owner_identity_id": peer}, saved["id"], edited["revision"], role="programme_owner", status=422)
    h.command("programme_owner", "catalogue.bulk_update", {"catalogue": "programmes",
        "items": [{"id": saved["id"], "revision": edited["revision"]}], "set": {"owner_identity_id": peer}, "reason": REASON}, status=422)
    h.configure("people", {"display_name": "Forbidden person authority"}, role="programme_owner", status=403)
    h.command("programme_owner", "role.create", {"identity_id": owner, "role": "admin",
        "starts_on": "2026-01-01", "reason": REASON}, status=403)
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == owner)).revoked = True
    h.configure("programmes", payload["value"], saved["id"], edited["revision"], role="programme_owner", status=403)


@pytest.mark.parametrize("active", [True, False])
def test_owned_programme_clear_revokes_trainee_scope_and_cached_reads(h, active):
    import uuid
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from tests.test_scheduling_fact_approvals import propose
    owner = identity(h, "programme_owner", ["programme_lead"])
    key = str(uuid.uuid4())
    payload = {"catalogue": "programmes", "value": {"name": "Undo owned programme", "starts_on": "2026-01-01", "active": active}, "reason": REASON}
    p = h.command("programme_owner", "catalogue.create", payload, key=key)["record"]
    c = h.configure("cohorts", {"name": "Owned cohort", "programme_id": p["id"],
        "starts_on": "2026-07-01", "ends_on": "2027-07-01"}, role="programme_owner")["record"]
    pid = h.seed["people"][0]
    h.configure("programme_memberships", {"person_id": pid, "programme_id": p["id"],
        "starts_on": "2026-07-01", "ends_on": "2027-07-01"}, role="programme_owner")
    rotation_value = {"person_id": pid, "cohort_id": c["id"], "service_id": h.seed["services"][0],
        "starts_on": "2026-10-01", "ends_on": "2026-11-01"}
    rotation = h.configure("rotations", rotation_value, role="programme_owner")["record"]
    note = h.command("programme_owner", "trainee_note.create", {"person_id": pid, "body": "Owned trainee note"})["record"]
    fact = propose(h, person=pid)
    assert fact["id"] in {x["id"] for x in h.get("programme_owner", "/api/requests/all")["items"]}
    h.command("programme_owner", "person.workload.activate", {"reason": REASON}, fact["id"], fact["revision"], status=403)
    before = h.get("programme_owner", "/api/catalogues")
    assert next(x for x in before["people"] if x["id"] == pid)["current_programme_ids"] == ([p["id"]] if active else [])
    assert h.get("programme_owner", f"/api/people/{pid}/trainee-notes")["notes"][0]["id"] == note["id"]
    # A used programme cannot be erased by creation Undo; privileged owner clearing preserves its data.
    h.command("programme_owner", "catalogue.bulk_update", {"catalogue": "programmes",
        "items": [{"id": p["id"], "revision": p["revision"]}], "active": False, "reason": "Undo"}, status=409)
    cleared = h.command("admin", "catalogue.update", {**payload, "owner_identity_id": None},
        p["id"], p["revision"])["record"]
    assert cleared["active"] == active and cleared["owner_identity_id"] is None
    for path in ("/api/catalogues", "/api/catalogues?known=" + ",".join(before["_versions"].values())):
        after = {**before, **h.get("programme_owner", path)}
        for kind in ("programmes", "cohorts", "programme_memberships", "rotations"):
            assert after[kind] == []
        assert next(x for x in after["people"] if x["id"] == pid)["current_programme_ids"] == []
    h.get("programme_owner", f"/api/people/{pid}/trainee-notes", status=403)
    assert h.get("programme_owner", "/api/requests/all")["items"] == []
    h.configure("programmes", payload["value"], p["id"], cleared["revision"], role="programme_owner", status=403)
    h.configure("rotations", rotation_value, rotation["id"], rotation["revision"], role="programme_owner", status=403)
    h.command("programme_owner", "catalogue.create", payload, key=key, status=403)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Programme, p["id"]).owner_identity_id is None
        assert db.get(m.Rotation, rotation["id"]) is not None and db.get(m.TraineeNote, note["id"]) is not None
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == p["id"]).order_by(m.DefinitionHistory.object_revision)))
        assert [x.snapshot["owner_identity_id"] for x in versions] == [owner, None]


@pytest.mark.parametrize("role", ["admin", "coordinator"])
def test_programme_owner_is_visible_and_privileged_clear_is_null_only(h, role):
    owner = identity(h, "programme_owner", ["programme_lead"])
    value = {"name": "Privileged ownership clearing", "starts_on": "2026-01-01"}
    p = h.configure("programmes", value, role="programme_owner")["record"]
    assert next(x for x in h.get(role, "/api/catalogues")["programmes"] if x["id"] == p["id"])["owner_identity_id"] == owner
    payload = {"catalogue": "programmes", "value": value, "reason": REASON, "owner_identity_id": None}
    h.command("programme_owner", "catalogue.update", payload, p["id"], p["revision"], status=403)
    h.command(role, "catalogue.update", {**payload, "owner_identity_id": owner}, p["id"], p["revision"], status=422)
    h.command(role, "catalogue.update", {**payload, "owner_identity_id": "arbitrary"}, p["id"], p["revision"], status=422)
    h.command(role, "catalogue.update", payload, p["id"], p["revision"] + 1, status=409)
    cleared = h.command(role, "catalogue.update", payload, p["id"], p["revision"])["record"]
    assert cleared["owner_identity_id"] is None and cleared["active"]
    assert h.get("programme_owner", "/api/catalogues")["programmes"] == []
    h.configure("programmes", value, p["id"], cleared["revision"], role="programme_owner", status=403)


def test_programme_owner_loses_reads_writes_and_receipts_when_lead_binding_is_removed(h):
    import uuid
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    from apps.api.auth import authenticate
    from apps.api.services.training import lead_programmes
    from tests.test_scheduling_fact_approvals import propose
    owner = identity(h, "programme_owner", ["staff", "programme_lead"], person=h.seed["people"][2])
    payload = {"catalogue": "programmes", "value": {"name": "Role-bound ownership", "starts_on": "2026-01-01"}, "reason": REASON}
    key = str(uuid.uuid4())
    p = h.command("programme_owner", "catalogue.create", payload, key=key)["record"]
    trainee = h.seed["people"][0]
    h.configure("programme_memberships", {"person_id": trainee, "programme_id": p["id"],
        "starts_on": "2026-01-01", "ends_on": "2027-01-01"}, role="programme_owner")
    note = h.command("programme_owner", "trainee_note.create", {"person_id": trainee, "body": "Owned private note"})["record"]
    fact = propose(h, person=trainee)
    for path in ("/api/catalogues", "/api/catalogues?boot=true", "/api/catalogues?known="):
        assert h.get("programme_owner", path)["programmes"][0]["id"] == p["id"]
    assert fact["id"] in {x["id"] for x in h.get("programme_owner", "/api/requests/all")["items"]}
    with transaction(h.app.state.engine, write=True) as db:
        binding = db.scalar(select(m.RoleBinding).where(m.RoleBinding.identity_id == owner, m.RoleBinding.role == "programme_lead"))
        db.delete(binding)
    assert h.get("programme_owner", "/api/me")["roles"] == ["staff"]
    for path in ("/api/catalogues", "/api/catalogues?boot=true", "/api/catalogues?known=", f"/api/catalogues/programmes/{p['id']}/history"):
        h.get("programme_owner", path, status=403)
    assert h.get("programme_owner", "/api/requests/all")["items"] == []
    h.get("programme_owner", f"/api/requests/all?id={fact['id']}", status=404)
    h.get("programme_owner", f"/api/people/{trainee}/trainee-notes", status=403)
    h.command("programme_owner", "trainee_note.update", {"body": "Forbidden edit"}, note["id"], note["revision"], status=403)
    h.configure("programmes", payload["value"], p["id"], p["revision"], role="programme_owner", status=403)
    h.command("programme_owner", "catalogue.create", payload, key=key, status=403)
    h.command("programme_owner", "catalogue.bulk_update", {"catalogue": "programmes",
        "items": [{"id": p["id"], "revision": p["revision"]}], "active": False, "reason": "Undo"}, status=403)
    with transaction(h.app.state.engine) as db:
        actor = authenticate(db, h.client("programme_owner").cookies["rota_session"])
        assert list(db.scalars(lead_programmes(db, actor))) == []
        assert db.get(m.Programme, p["id"]).owner_identity_id == owner


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_programme_create_inverse_removes_programme_and_owner_atomically(request, database):
    import uuid
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    h = request.getfixturevalue(database)
    owner = identity(h, "programme_owner", ["programme_lead"])
    payload = {"catalogue": "programmes", "value": {"name": "Unused owned programme", "starts_on": "2026-01-01"}, "reason": REASON}
    key = str(uuid.uuid4())
    p = h.command("programme_owner", "catalogue.create", payload, key=key)["record"]
    inverse = {"catalogue": "programmes", "items": [{"id": p["id"], "revision": p["revision"]}], "active": False, "reason": "Undo"}
    h.command("programme_owner", "catalogue.bulk_update", inverse, target=h.seed["people"][0], status=422)
    h.command("programme_owner", "catalogue.bulk_update", inverse, revision=1, status=422)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Programme, p["id"]).owner_identity_id == owner
    undo_key = str(uuid.uuid4())
    result = h.command("programme_owner", "catalogue.bulk_update", inverse, key=undo_key)
    assert result["removed_ids"] == [p["id"]] and result["removed"] == 1 and result["records"] == []
    assert h.command("programme_owner", "catalogue.bulk_update", inverse, key=undo_key) == result
    assert h.get("programme_owner", "/api/catalogues")["programmes"] == []
    h.command("programme_owner", "catalogue.create", payload, key=key, status=403)
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Programme, p["id"]) is None
        event = db.scalar(select(m.AuditEvent).where(m.AuditEvent.detail["receipt_id"].as_string() == result["receipt_id"]))
        assert event.target_id == p["id"] and event.action == "catalogue.bulk_update"
        versions = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == p["id"]).order_by(m.DefinitionHistory.object_revision)))
        assert len(versions) == 2 and all(v.snapshot["owner_identity_id"] == owner for v in versions)


@pytest.mark.parametrize("reference", ["cohort", "tag", "rule", "source", "evidence"])
def test_programme_create_inverse_refuses_dependencies_and_preserves_owner(h, reference):
    from apps.api import models as m
    from apps.api.db import transaction
    owner = identity(h, "programme_owner", ["programme_lead"])
    p = h.configure("programmes", {"name": "Referenced programme", "starts_on": "2026-01-01"}, role="programme_owner")["record"]
    if reference == "cohort":
        h.configure("cohorts", {"name": "Programme cohort", "programme_id": p["id"],
            "starts_on": "2026-01-01", "ends_on": "2027-01-01"}, role="programme_owner")
    elif reference == "rule":
        h.policy({"kind": "max_hours", "hours": 20}, {"programme_id": p["id"]})
    else:
        with transaction(h.app.state.engine, write=True) as db:
            if reference == "tag":
                tag = m.Tag(department_id=h.seed["department_id"], name="Reference tag")
                db.add(tag); db.flush()
                db.add(m.TagMember(department_id=h.seed["department_id"], tag_id=tag.id, kind="programmes", member_id=p["id"]))
            elif reference == "source":
                db.add(m.SourceMapping(department_id=h.seed["department_id"], source_name="Fixture", source_key="programme", target_kind="programmes", target_id=p["id"]))
            else:
                db.add(m.DefinitionHistory(department_id=h.seed["department_id"], kind="fixture", object_id=h.seed["people"][0],
                    object_revision=1, actor_id=h.seed["identities"]["admin"], reason=REASON, snapshot={"programme_id": p["id"]}))
    h.command("programme_owner", "catalogue.bulk_update", {"catalogue": "programmes",
        "items": [{"id": p["id"], "revision": p["revision"]}], "active": False, "reason": "Undo"}, status=409)
    with transaction(h.app.state.engine) as db:
        kept = db.get(m.Programme, p["id"])
        assert kept.active and kept.owner_identity_id == owner and kept.revision == 1


@pytest.mark.parametrize("reason", [REASON, "Undo"])
def test_programme_owner_keeps_authority_for_normal_deactivation_and_undo(h, reason):
    owner = identity(h, "programme_owner", ["programme_lead"])
    value = {"name": "Ordinary programme edit", "starts_on": "2026-01-01"}
    p = h.configure("programmes", value, role="programme_owner")["record"]
    off = h.command("programme_owner", "catalogue.update", {"catalogue": "programmes",
        "value": {**value, "active": False}, "reason": reason}, p["id"], p["revision"])
    assert off["record"]["owner_identity_id"] == owner
    assert h.get("programme_owner", "/api/catalogues")["programmes"][0]["id"] == p["id"]
    restored = h.command("programme_owner", "catalogue.update", {"catalogue": "programmes",
        "value": off["previous_value"], "reason": "Undo"}, p["id"], off["record"]["revision"])["record"]
    assert restored["active"] and restored["owner_identity_id"] == owner
    # Undoing reactivation is an edit reversal, not the original creation's revision-1 inverse.
    off_again = h.command("programme_owner", "catalogue.bulk_update", {"catalogue": "programmes",
        "items": [{"id": p["id"], "revision": restored["revision"]}], "active": False, "reason": "Undo"})["records"][0]
    assert not off_again["active"] and off_again["owner_identity_id"] == owner


def test_programme_ownership_migration_preserves_existing_records(tmp_path):
    from .migration_support import add_historical_admin

    import uuid
    from datetime import date, datetime, timezone
    from alembic import command
    from sqlalchemy import MetaData, text
    from apps.api.db import make_engine
    from scripts.manage import alembic_config
    from apps.api.schema import supported_revision
    url = "sqlite:///" + str(tmp_path / "ownership-upgrade.db")
    config = alembic_config(url)
    command.upgrade(config, "0090_residency")
    engine = make_engine(url)
    metadata = MetaData()
    metadata.reflect(engine, only=("organisations", "departments", "programmes"))
    org, dept, programme = (str(uuid.uuid4()) for _ in range(3))
    base = {"revision": 1, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    with engine.begin() as db:
        db.execute(metadata.tables["organisations"].insert().values(**base, id=org, name="Legacy organisation",
                   timezone="Asia/Riyadh", week_start=6, input_revision=1, availability_revision=1))
        db.execute(metadata.tables["departments"].insert().values(**base, id=dept, organisation_id=org, name="Legacy department"))
        db.execute(metadata.tables["programmes"].insert().values(**base, id=programme, department_id=dept, name="Legacy programme",
                   description="", starts_on=date(2026, 1, 1), active=True))
        add_historical_admin(db, dept)
        before = dict(db.execute(text("SELECT * FROM programmes")).mappings().one())
    engine.dispose()
    command.upgrade(config, "head")
    with engine.connect() as db:
        after = dict(db.execute(text("SELECT * FROM programmes")).mappings().one())
        assert after.pop("owner_identity_id") is None
        assert after == before
        assert db.execute(text("SELECT version_num FROM alembic_version")).scalar() == supported_revision()
    engine.dispose()


def test_unrepresentable_calendar_and_utc_dates_return_validation_errors(r):
    h, p, c, types, people, blocks = r
    for endpoint in ("rotation-board", "calendar"):
        h.get("programme_lead", f"/api/{endpoint}?start=0001-01-01&end=0001-01-02", status=422)
    entry = {"person_id": people[0], "start": "0001-01-01T00:00:00+03:00", "end": "0001-01-01T01:00:00+03:00", "private_reason": REASON}
    h.command("coordinator", "availability.record", {**entry, "kind": "protected_time"}, status=422)
    h.command("staff", "request.create", entry, status=422)
    h.configure("programme_memberships", {"person_id": people[0], "programme_id": p["id"],
        "starts_on": "9999-12-30", "ends_on": "9999-12-31"})
    h.command("coordinator", "availability.record", {"person_id": people[0], "kind": "protected_time",
        "start": "9999-12-30T08:00:00+03:00", "end": "9999-12-31T12:00:00+03:00", "private_reason": REASON})
    h.get("coordinator", "/api/rotation-board?start=9999-12-30&end=9999-12-31", status=422)


def test_extreme_membership_dates_fail_closed_in_request_projection(r):
    h, p, c, types, people, blocks = r
    h.configure("programmes", {"name": p["name"], "starts_on": "0001-01-01"}, p["id"], p["revision"])
    h.configure("programme_memberships", {"person_id": h.seed["people"][2], "programme_id": p["id"],
        "starts_on": "0001-01-01", "ends_on": "2027-01-01"})
    h.get("programme_lead", "/api/requests/all", status=422)


def test_calendar_rejects_unrepresentable_stored_rotation_dates(r):
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    h.configure("programmes", {"name": p["name"], "starts_on": "0001-01-01"}, p["id"], p["revision"])
    cohort = h.configure("cohorts", {"name": "Synthetic earliest dates", "programme_id": p["id"],
        "starts_on": "0001-01-01", "ends_on": "0001-01-04"})["record"]
    h.configure("programme_memberships", {"person_id": people[0], "programme_id": p["id"],
        "starts_on": "0001-01-01", "ends_on": "0001-01-04"})
    with transaction(h.app.state.engine, write=True) as db:
        db.scalar(select(m.Membership).where(m.Membership.person_id == people[0])).starts_on = date.min
    h.command('coordinator', 'availability.record', {'person_id': people[0], 'kind': 'recorded_leave',
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'private_reason': REASON})
    h.command('coordinator', 'availability_pattern.propose', {'person_id': people[0], 'weekdays': [6],
        'start_time': '08:00', 'end_time': '12:00', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01'})
    early_type = h.configure("rotation_types", {"name": "Synthetic earliest type", "starts_on": "0001-01-01"})["record"]
    h.configure("rotations", {"person_id": people[0], "cohort_id": cohort["id"], "rotation_type_id": early_type["id"],
        "starts_on": "0001-01-01", "ends_on": "0001-01-04"})
    blocked = h.command('coordinator', 'availability.record', {'person_id': people[0], 'kind': 'recorded_leave',
        'start': '0001-01-02T08:00:00+03:00', 'end': '0001-01-02T12:00:00+03:00',
        'private_reason': REASON}, status=422)
    assert blocked['error']['code'] == 'INTERVAL_DATES'
    h.get("coordinator", "/api/calendar?start=0001-01-02&end=0001-01-03", status=422)


@pytest.mark.parametrize("command_kind", ["catalogue.create", "rotation.plan"])
def test_rotation_receipts_preserve_original_programme_after_cohort_transfer(r, command_kind):
    import uuid
    from sqlalchemy import select
    from apps.api import models as m
    from apps.api.db import transaction
    h, p, c, types, people, blocks = r
    other, oc, membership = other_programme(h, people[0])
    key = str(uuid.uuid4())
    if command_kind == "catalogue.create":
        payload = {"catalogue": "rotations", "value": rotation_value(people[0], oc, types[0]), "reason": REASON}
    else:
        payload = {"cohort_id": oc["id"], "person_ids": people[:1], "starts_on": "2026-10-01", "blocks": blocks, "reason": REASON}
    h.command("coordinator", command_kind, payload, key=key)
    h.configure("cohorts", {"name": oc["name"], "programme_id": p["id"], "starts_on": oc["starts_on"],
        "ends_on": oc["ends_on"]}, oc["id"], oc["revision"])
    with transaction(h.app.state.engine, write=True) as db:
        ident = db.get(m.Identity, h.seed["identities"]["coordinator"])
        ident.person_id = db.get(m.Identity, h.seed["identities"]["programme_lead"]).person_id
        for binding in db.scalars(select(m.RoleBinding).where(m.RoleBinding.identity_id == ident.id)):
            binding.revoked = True
        db.add(m.RoleBinding(identity_id=ident.id, department_id=h.seed["department_id"], role="programme_lead", starts_on=date(2020, 1, 1)))
    assert h.get("coordinator", "/api/catalogues")["rotations"]
    h.get("coordinator", "/api/commands/" + key, status=403)
    h.command("coordinator", command_kind, payload, key=key, status=403)


@pytest.mark.parametrize("kind", ["rotation.plan", "rotation.copy", "rotation.remove", "rotation.undo_plan", "rotation.request_change", "trainee_note.create"])
@pytest.mark.parametrize("envelope", ["target", "revision"])
def test_residency_payload_operations_cannot_spoof_audit_targets(r, kind, envelope):
    import uuid
    h, p, c, types, people, blocks = r
    rot = h.configure("rotations", rotation_value(people[0], c, types[0]))["record"]
    payloads = {
        "rotation.plan": {"cohort_id": c["id"], "person_ids": people[:1], "starts_on": "2026-10-01", "blocks": blocks, "reason": REASON},
        "rotation.copy": {"starts_on": "2026-10-01", "ends_on": "2026-11-01", "reason": REASON},
        "rotation.remove": {"items": [{"id": rot["id"], "revision": rot["revision"]}], "reason": REASON},
        "rotation.undo_plan": {"items": [{"id": rot["id"], "revision": rot["revision"]}], "reason": REASON},
        "rotation.request_change": {"rotation_id": rot["id"], "rotation_type_id": types[1]["id"], "starts_on": rot["starts_on"],
            "ends_on": rot["ends_on"], "private_reason": REASON},
        "trainee_note.create": {"person_id": people[0], "body": "Synthetic note"},
    }
    h.command("programme_lead", kind, payloads[kind], str(uuid.uuid4()) if envelope == "target" else None,
        1 if envelope == "revision" else None, status=422)
