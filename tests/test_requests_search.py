"""Requests search filters authorized SQL sources before paging, including stage totals."""
from datetime import datetime, timedelta, timezone

import pytest

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts.schemas import ROLE_NAMES
from .governance_support import identity


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def read(h, role="approver", **query):
    response = h.client(role).get("/api/requests/all", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def request_row(h, person, note, day=0, status="submitted", department=None, kind="leave"):
    start = datetime(2026, 10, 4, tzinfo=timezone.utc) + timedelta(days=day)
    return m.RequestRecord(department_id=department or h.seed["department_id"], person_id=person,
                           start=start, end=start + timedelta(days=1), private_reason=note, kind=kind,
                           status=status, created_by=h.seed["identities"]["coordinator"])


def test_search_finds_unloaded_history_and_pages_matching_rows_with_stage_counts(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][1]).display_name = "History needle clinician"
        db.add_all([request_row(h, h.seed["people"][0], "Ordinary request", day=n) for n in range(205)])
        matches = [request_row(h, h.seed["people"][1], "Private request reason", day=300 + n,
                               status="approved" if n == 2 else "submitted") for n in range(3)]
        db.add_all(matches)
        db.flush()
        expected = [row.id for row in matches]
    first = read(h)
    assert len(first["items"]) == 100 and first["total"] == 208
    assert not set(expected) & {row["id"] for row in first["items"]}
    pages = [read(h, q="  HISTORY needle  ", limit=2, offset=offset) for offset in (0, 2)]
    assert [row["id"] for page in pages for row in page["items"]] == expected
    assert all(page["total"] == 3 for page in pages)
    assert pages[0]["has_more"] and not pages[1]["has_more"]
    assert pages[0]["counts"]["stages"]["waiting"] == 2
    assert pages[0]["counts"]["stages"]["approved"] == 1
    assert sum(pages[0]["counts"]["stages"].values()) == 3
    approved = read(h, q="history needle", status="approved")
    assert approved["total"] == 1 and approved["items"][0]["id"] == expected[-1]
    assert approved["counts"] == pages[0]["counts"]  # stage picker ignores only the selected stage
    assert approved["counts"]["to_decide"] == first["counts"]["to_decide"]


def test_search_matches_visible_notes_names_and_canonical_kinds(both):
    h = both
    me = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, me).display_name = "Searchable clinician"
        db.add(request_row(h, me, "Shared needle", kind="clinic_change"))
    h.command("staff", "availability_pattern.propose", {"person_id": me, "weekdays": [1], "start_time": "13:00",
              "end_time": "17:00", "starts_on": "2026-10-01", "note": "Shared needle"})
    h.command("staff", "wish.create", {"person_id": me, "first_day": "2026-11-10", "last_day": "2026-11-10",
              "kind": "prefer_off", "points": 2, "note": "Shared needle"})
    for query, sources in (("shared NEEDLE", {"pattern", "wish"}),
                           ("searchable CLINICIAN", {"request", "pattern", "wish"})):
        result = read(h, q=query)
        assert {row["source"] for row in result["items"]} == sources
        assert result["total"] == sum(result["counts"]["stages"].values()) == len(sources)
    for query, source in (("clinic change", "request"), ("other", "request"), ("weekly", "pattern"),
                          ("prefer off", "wish"), ("wish", "wish")):
        result = read(h, q=query)
        assert result["total"] == 1 and result["items"][0]["source"] == source
    assert read(h, q="   ")["total"] == 3


def test_search_treats_sql_wildcards_and_escape_characters_literally(both):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, h.seed["people"][0]).display_name = r"100% planning_under\review"
        db.get(m.Person, h.seed["people"][1]).display_name = "100X planningXunderXreview"
        literal = request_row(h, h.seed["people"][0], "Private reason")
        db.add_all([literal, request_row(h, h.seed["people"][1], "Private reason")])
        db.flush()
        expected = literal.id
    for query in ("%", "_", "\\", r"100% planning_under\review"):
        result = read(h, q=query)
        assert result["total"] == 1 and result["items"][0]["id"] == expected
        assert sum(result["counts"]["stages"].values()) == 1


@pytest.mark.parametrize("display", ["none", "type", "details"])
def test_private_request_and_availability_reasons_never_affect_search_or_counts_for_any_role(both, display):
    h = both
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Department, h.seed["department_id"]).leave_reason_display = display
        for index, person in enumerate(h.seed["people"][:2]):
            db.get(m.Person, person).display_name = f"Visible clinician {index}"
            db.add_all([request_row(h, person, "Confidential needle", kind=kind)
                        for kind in ("leave", "clinic_change")])
    for person in h.seed["people"][:2]:
        h.command("coordinator", "availability.record", {"person_id": person, "start": "2026-11-01T00:00:00+03:00",
                  "end": "2026-11-02T00:00:00+03:00", "kind": "recorded_leave", "private_reason": "Confidential needle"})
    for role in ROLE_NAMES:
        if role not in h.seed["identities"]:
            identity(h, role, [role])
        response = h.client(role).get("/api/requests/all", params={"q": "Confidential needle"})
        if role not in ("admin", "coordinator", "approver", "staff", "auditor", "programme_lead"):
            assert response.status_code == 403, response.text
            continue
        assert response.status_code == 200, response.text
        hidden = response.json()
        assert hidden["items"] == [] and hidden["total"] == 0 and not hidden["has_more"]
        assert sum(hidden["counts"]["stages"].values()) == 0
        if role in ("auditor", "programme_lead"):
            assert read(h, role, q="Visible clinician")["total"] == 0
            continue
        people = 1 if role == "staff" else 2
        named = read(h, role, q="Visible clinician")
        assert named["total"] == sum(named["counts"]["stages"].values()) == 3 * people
        assert {row["person_id"] for row in named["items"]} == set(h.seed["people"][:people])
        assert read(h, role, q="clinic change")["total"] == people
        visible = read(h, role, q="recorded leave")
        assert visible["total"] == people
        assert all(("private_reason" in row) == (display == "details" and role != "staff") for row in visible["items"])


def test_person_filters_search_and_counts_preserve_role_and_department_scope(both):
    h = both
    me, other, third = h.seed["people"][:3]
    with transaction(h.app.state.engine, write=True) as db:
        foreign = m.Department(organisation_id=h.seed["organisation_id"], name="Separate test department")
        db.add(foreign)
        db.flush()
        for index, person in enumerate((me, other, third)):
            db.get(m.Person, person).display_name = f"Scope needle clinician {index}"
        db.add_all([request_row(h, person, "Scope needle") for person in (me, other, third)])
        db.add(request_row(h, other, "Scope needle", department=foreign.id))
    for role in ("admin", "coordinator", "approver"):
        result = read(h, role, q="Scope needle", person_ids=",".join((me, other)))
        assert {row["person_id"] for row in result["items"]} == {me, other}
        assert result["total"] == sum(result["counts"]["stages"].values()) == 2
        legacy = read(h, role, q="Scope needle", person_id=other)
        assert legacy["total"] == 1 and legacy["items"][0]["person_id"] == other
        assert read(h, role, person_id=third, person_ids=",".join((me, other)))["total"] == 0
    for query in ({"person_id": other}, {"person_ids": ",".join((other, third))}):
        result = read(h, "staff", q="Scope needle", **query)
        assert result["total"] == sum(result["counts"]["stages"].values()) == 0
        assert result["items"] == []
    identity(h, "programme_lead", ["programme_lead"])
    for role in ("credentialer", "operator"):
        response = h.client(role).get("/api/requests/all", params={"q": "Scope needle", "person_ids": me})
        assert response.status_code == 403
    for role in ("auditor", "programme_lead"):
        response = read(h, role, q="Scope needle", person_ids=me)
        assert response["items"] == [] and response["total"] == 0


def test_unlinked_staff_cannot_read_requests_patterns_or_availability(both):
    h = both
    me = h.seed["people"][0]
    with transaction(h.app.state.engine, write=True) as db:
        db.add(request_row(h, me, "Unlinked scope needle"))
    h.command("staff", "availability_pattern.propose", {"person_id": me, "weekdays": [1], "start_time": "13:00",
              "end_time": "17:00", "starts_on": "2026-10-01", "note": "Unlinked scope needle"})
    h.command("coordinator", "availability.record", {"person_id": me, "start": "2026-11-01T00:00:00+03:00",
              "end": "2026-11-02T00:00:00+03:00", "kind": "recorded_leave", "private_reason": "Hidden unlinked needle"})
    expected = read(h)["items"]
    assert {row["source"] for row in expected} == {"request", "pattern", "availability"}
    identity(h, "unlinked_staff", ["staff"])
    for query in ({}, {"q": "Unlinked scope needle"}, {"q": "Hidden unlinked needle"}, {"q": "leave"},
                  {"person_id": me}, {"person_ids": me}):
        result = read(h, "unlinked_staff", **query)
        assert result["items"] == [] and result["total"] == 0 and not result["has_more"]
        assert result["counts"]["to_decide"] == sum(result["counts"]["stages"].values()) == 0
    for row in expected:
        response = h.client("unlinked_staff").get("/api/requests/all", params={"id": row["id"]})
        assert response.status_code == 404


def test_swap_search_finds_both_participants_and_visible_note(both):
    h = both
    me, other, outsider = h.seed["people"][:3]
    identity(h, "staff_two", ["staff"], other)
    identity(h, "staff_three", ["staff"], outsider)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Person, me).display_name = "Swap proposer"
        db.get(m.Person, other).display_name = "Swap recipient"
    workspace = h.shift(h.workspace())["workspace"]
    workspace = h.shift(workspace, "2026-10-05T08:00:00+03:00", "2026-10-05T12:00:00+03:00",
                        people=[other])["workspace"]
    publication = h.publish(workspace)["publication_id"]
    assignment = h.get("coordinator", "/api/publications/" + publication)["snapshot"]["shifts"][0]["participants"][0]["id"]
    swap = h.command("staff", "swap.offer", {"publication_id": publication, "assignment_id": assignment,
                     "recipient_person_id": other, "expires_at": "2026-10-03T18:00:00+00:00", "reason": "Swap needle"})["swap"]
    for role in ("coordinator", "staff", "staff_two"):
        for query in ("Swap needle", "Swap proposer", "Swap recipient"):
            result = read(h, role, q=query)
            assert result["total"] == 1 and result["items"][0]["id"] == swap["id"]
            assert sum(result["counts"]["stages"].values()) == 1
    assert read(h, "coordinator", q="swap", person_ids=other)["total"] == 1
    assert read(h, "coordinator", q="swap", person_ids=outsider)["total"] == 0
    result = read(h, "staff_three", q="Swap needle", person_ids=me)
    assert result["total"] == sum(result["counts"]["stages"].values()) == 0


def test_search_query_and_person_selection_are_bounded(h):
    client = h.client("approver")
    for query in ({"q": "x" * 201}, {"person_ids": "invalid"},
                  {"person_ids": ",".join([h.seed["people"][0]] * 501)}, {"person_ids": "x" * 20001}):
        assert client.get("/api/requests/all", params=query).status_code == 422
    assert read(h, q="x" * 200)["total"] == 0
