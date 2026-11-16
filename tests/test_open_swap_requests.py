"""Requests use frozen audience membership and recheck exact consent when responding."""
from datetime import timedelta

import pytest

from apps.api import models as m
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import availability, swaps
from .governance_support import identity, transition
from .test_open_swaps import audience_ready
from .test_staff_swaps import fixed_clock, source_pair  # noqa: F401


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def offer(h, publication, assignment, kind, role="staff", reason="Participant-only shared reason"):
    row = h.command(role, "swap.offer", {"kind": kind, "publication_id": publication,
        "assignment_id": assignment, "expires_at": (m.now() + timedelta(days=1)).isoformat(),
        "reason": reason})["swap"]
    audience_ready(h)
    return row


def read(h, role="staff_three", **params):
    response = h.client(role).get("/api/requests/all", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_open_offer_and_rotation_change_keep_their_shared_request_fields(both):
    from .test_residency import change, plan, setup_residency
    h = both
    publication, _, second, _ = source_pair(h)
    invitation = offer(h, publication, second, "give", "staff_two")
    _, _, cohort, types, people, blocks = setup_residency(h)
    rotation = plan(h, cohort, people[:1], blocks)["created"][0]
    request = change(h, rotation, types[1])["record"]
    for params in ({}, {"sort_by": "person"}):
        items = {item["id"]: item for item in read(h, "staff", **params)["items"]}
        assert items[request["id"]]["can_decide"] is False
        assert types[1]["name"] in items[request["id"]]["summary"]
        assert items[invitation["id"]]["open_offer"] is True
        assert "reason" not in items[invitation["id"]]
    assert read(h, "approver", id=request["id"])["item"]["can_decide"] is True
    assert read(h, "staff", id=invitation["id"])["history"] == []


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_open_offers_are_filtered_and_sorted_before_paging_without_private_search_or_history(both, kind):
    h = both
    pid, first, second, _ = source_pair(h, third=True)
    rows = [offer(h, pid, first, kind), offer(h, pid, second, kind, "staff_two")]
    for params in ({}, {"type": "swap", "status": "reply"}):
        result = read(h, **params)
        assert {item["id"] for item in result["items"]} == {row["id"] for row in rows}
        assert result["total"] == result["counts"]["to_decide"] == result["counts"]["stages"]["reply"] == 2
        assert sum(result["counts"]["stages"].values()) == 2
    for role in ("staff_three", "approver"):
        for params, expected in (({"person_id": h.seed["people"][0]}, {rows[0]["id"]}),
                                 ({"person_ids": h.seed["people"][1]}, {rows[1]["id"]}),
                                 ({"person_id": h.seed["people"][0], "person_ids": h.seed["people"][1]}, set())):
            result = read(h, role, **params)
            assert {item["id"] for item in result["items"]} == expected
            assert result["total"] == result["counts"]["stages"]["reply"] == len(expected)
            assert sum(result["counts"]["stages"].values()) == len(expected)
            # The attention count is the actor's decision queue, independent of the selected person filter.
            assert result["counts"]["to_decide"] == (2 if role == "staff_three" else 0)
    for key in ("when", "person", "type"):
        full = read(h, sort_by=key, sort_direction="desc")["items"]
        pages = [read(h, sort_by=key, sort_direction="desc", limit=1, offset=n) for n in range(2)]
        assert [page["items"][0]["id"] for page in pages] == [item["id"] for item in full]
        assert [page["has_more"] for page in pages] == [True, False]
        assert all(page["total"] == 2 and page["counts"]["stages"]["reply"] == 2 for page in pages)
    hidden = read(h, q="Participant-only shared reason")
    assert hidden["items"] == [] and hidden["total"] == sum(hidden["counts"]["stages"].values()) == 0
    assert hidden["counts"]["to_decide"] == 2
    assert read(h, q="swap")["total"] == 2
    assert read(h, status="accepted")["total"] == 0
    for row in rows:
        detail = read(h, id=row["id"])
        item = detail["item"]
        assert detail["history"] == []
        assert item["open_offer"] is True and item["kind"] == kind and item["stage"] == "reply"
        assert item["offered_assignment"]["assignment_id"] == row["first_assignment_id"]
        assert item["recipient_person_id"] is None and item["recipient_name"] is None
        assert item["counteroffered_assignment"] is None and item["second_assignment_id"] is None
        assert not {"reason", "proposer_id", "recipient_id", "workspace_id", "first_assignment_id"} & item.keys()
        assert "Participant-only" not in str(detail)
        proposer = "staff" if row["proposer_person_id"] == h.seed["people"][0] else "staff_two"
        assert read(h, proposer, id=row["id"])["history"]
    identity(h, "unlinked_staff", ["staff"])
    assert read(h, "unlinked_staff")["total"] == 0
    h.get("unlinked_staff", "/api/requests/all?id=" + rows[0]["id"], status=404)


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_taken_open_offer_disappears_from_uninvolved_requests_and_home(both, kind):
    h = both
    pid, first, second, _ = source_pair(h, third=True)
    row = offer(h, pid, first, kind)
    assert read(h)["counts"]["to_decide"] == 1
    assert row["id"] in {item["id"] for item in h.get("staff_three", "/api/home")["my_requests"]}
    row = (transition(h, row, "accept", "staff_two") if kind == "give" else
           transition(h, row, "counteroffer", "staff_two", {"assignment_id": second,
               "reason": "Private return duty negotiation"}))["swap"]
    for params in ({}, {"q": "swap"}, {"q": "Private return"}, {"status": "reply"},
                   {"sort_by": "person"}, {"limit": 1, "offset": 1}):
        result = read(h, **params)
        assert result["items"] == [] and result["total"] == result["counts"]["to_decide"] == 0
        assert sum(result["counts"]["stages"].values()) == 0 and not result["has_more"]
    h.get("staff_three", "/api/requests/all?id=" + row["id"], status=404)
    assert row["id"] not in {item["id"] for item in h.get("staff_three", "/api/home")["my_requests"]}
    assert read(h, "staff", id=row["id"])["item"]["recipient_person_id"] == h.seed["people"][1]
    assert read(h, "staff")["counts"]["to_decide"] == 1
    assert read(h, "staff_two", id=row["id"])["history"][-1]["action"] == (
        "swap.accept" if kind == "give" else "swap.counteroffer")


@pytest.mark.parametrize("staff", [False, True])
def test_home_personal_rows_stay_separate_from_team_decisions_and_open_offer_filters(both, staff):
    from .test_scheduling_fact_approvals import propose
    from .test_requests_unified import leave

    h = both
    publication, first, _, _ = source_pair(h, third=True)
    invitation = offer(h, publication, first, "give")
    person = h.seed["people"][2]
    identity(h, "personal_planner", ["coordinator", "approver", *(["staff"] if staff else [])], person=person)
    own = leave(h, "coordinator", person)
    peer = leave(h)
    own_fact = propose(h, person=person)
    peer_fact = propose(h)
    home = h.get("personal_planner", "/api/home")
    ids = {row["id"] for row in home["my_requests"]}
    assert ids == {own["id"], own_fact["id"], *([invitation["id"]] if staff else [])}
    assert not {peer["id"], peer_fact["id"]} & ids
    # The decision view keeps its existing team scope; the explicit person filter still intersects it.
    assert {peer["id"], peer_fact["id"]} <= {row["id"] for row in read(h, "personal_planner")["items"]}
    assert invitation["id"] not in {row["id"] for row in read(h, "personal_planner", person_id=person)["items"]}
    assert all("private_reason" not in row and "reason" not in row for row in home["my_requests"])


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_requests_keep_frozen_audience_but_recheck_response_after_availability_changes(both, kind):
    h = both
    pid, first, _, source = source_pair(h, third=True)
    row = offer(h, pid, first, kind)
    assert read(h)["total"] == 1
    absence = h.command("coordinator", "availability.record", {"person_id": h.seed["people"][2],
        "start": "2026-10-04T08:00:00+03:00", "end": "2026-10-04T12:00:00+03:00",
        "kind": "actual_absence", "private_reason": "Private eligibility drift"})["record"]
    result = read(h, type="swap")
    assert [item["id"] for item in result["items"]] == [row["id"]]
    assert result["total"] == result["counts"]["to_decide"] == sum(result["counts"]["stages"].values()) == 1
    assert read(h, id=row["id"])["item"]["id"] == row["id"]
    third = source["shifts"][2]["participants"][0]["id"]
    response = transition(h, row, "accept" if kind == "give" else "counteroffer", "staff_three",
        {"reason": "Check current eligibility before binding consent",
         **({"assignment_id": third} if kind == "exchange" else {})}, status=422)
    assert response["error"]["code"] == "SWAP_INELIGIBLE"
    assert h.get("staff", "/api/swaps/" + row["id"])["swap"]["recipient_person_id"] is None
    h.command("coordinator", "availability.retract", {"reason": "Correct synthetic absence"},
        absence["id"], absence["revision"])
    assert read(h, type="swap")["total"] == 1


@pytest.mark.parametrize("kind", ["exchange", "give"])
@pytest.mark.parametrize("view", ["list", "detail", "counts"])
@pytest.mark.postgres
def test_claim_after_visibility_lookup_cannot_reveal_taken_offer_in_requests(pg, monkeypatch, kind, view):
    h = pg
    pid, first, second, _ = source_pair(h, third=True)
    row = offer(h, pid, first, kind)
    h.client("staff_two")
    h.client("staff_three")
    visibility, claimed = swaps.visible_open_ids, []

    def claim_after_lookup(db, actor, *args, **kwargs):
        visible = visibility(db, actor, *args, **kwargs)
        if actor.person_id == h.seed["people"][2] and row["id"] in visible and not claimed:
            claimed.append(True)
            transition(h, row, "accept" if kind == "give" else "counteroffer", "staff_two",
                {"reason": "Private winning recipient response",
                 **({"assignment_id": second} if kind == "exchange" else {})})
        return visible

    monkeypatch.setattr(swaps, "visible_open_ids", claim_after_lookup)
    if view == "detail":
        h.get("staff_three", "/api/requests/all?id=" + row["id"], status=404)
    elif view == "counts":
        actor = Actor(h.seed["identities"]["staff_three"], h.seed["department_id"], h.seed["organisation_id"],
            "Synthetic open offer reader", frozenset({"staff"}), h.seed["people"][2], "test")
        with transaction(h.app.state.engine) as db:
            assert availability._counts(db, actor) == {"to_decide": 0}
    else:
        result = read(h, type="swap", q="swap", sort_by="when")
        assert result["items"] == [] and result["total"] == 0 and not result["has_more"]
        assert result["counts"]["to_decide"] == sum(result["counts"]["stages"].values()) == 0
        assert second not in str(result) and "Private winning" not in str(result)
    assert claimed == [True]


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_different_recipient_after_open_undo_cannot_read_previous_recipient_history(both, kind):
    h = both
    pid, first, second, source = source_pair(h, third=True)
    third = source["shifts"][2]["participants"][0]["id"]
    row = offer(h, pid, first, kind)
    action = "accept" if kind == "give" else "counteroffer"
    row = transition(h, row, action, "staff_two", {"reason": "Previous recipient private response",
        **({"assignment_id": second} if kind == "exchange" else {})})["swap"]
    again = transition(h, row, "undo", "staff_two", {"reason": "Previous recipient private undo"})["swap"]
    audience_ready(h)
    again = transition(h, again, action, "staff_three", {"reason": "New recipient own response",
        **({"assignment_id": third} if kind == "exchange" else {})})["swap"]
    detail = read(h, id=again["id"])
    assert [event["action"] for event in detail["history"]] == ["swap." + action]
    assert detail["history"][0]["reason"] == "New recipient own response"
    assert "Previous recipient" not in str(detail) and "Fictional staff_two" not in str(detail["history"])
    assert second not in str(detail)
    direct = h.get("staff_three", "/api/swaps/" + again["id"])
    assert "Previous recipient" not in str(direct) and second not in str(direct)
    assert h.seed["identities"]["staff_two"] not in str(direct["events"])
    for who in ("staff", "coordinator"):
        history = read(h, who, id=again["id"])["history"]
        assert {"Previous recipient private response", "Previous recipient private undo"} <= {
            event["reason"] for event in history}
