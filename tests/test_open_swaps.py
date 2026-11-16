"""Open offers keep consent private and bind exactly one current eligible recipient."""
from .clock_support import freeze_clock
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import uuid

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import swaps
from apps.worker.main import deliver_notifications
from tests.outbox_support import process_one_after_undo as process_one
from .governance_support import identity, swap_automation, transition
from .test_staff_swaps import fixed_clock, source_pair  # noqa: F401


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def payload(publication, first, kind):
    return {"kind": kind, "publication_id": publication, "assignment_id": first,
            "expires_at": (m.now() + timedelta(days=1)).isoformat(), "reason": "Shared participant-only explanation"}


def reply(h, row, second, role="staff_two"):
    return transition(h, row, "accept" if row["kind"] == "give" else "counteroffer", role,
        {"reason": "Consent to this exact duty", **({"assignment_id": second} if row["kind"] == "exchange" else {})})["swap"]


def audience_ready(h):
    for _ in range(30):
        if not process_one(h.app.state.engine):
            return
    pytest.fail("Open-offer audience worker did not drain its bounded fixture backlog")


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_open_offer_claim_confirmation_review_and_exact_publication(both, kind):
    h = both
    swap_automation(h, prepare=False, publish=False)  # this test prepares and publishes by hand
    pid, first, second, source = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", {**payload(pid, first, kind), "recipient_person_id": None})["swap"]
    assert row["open_offer"] and row["recipient_person_id"] is None and row["recipient_name"] is None
    audience_ready(h)
    assert transition(h, row, "confirm", status=409)["error"]["code"] == "SWAP_STATE"
    assert transition(h, row, "prepare", "coordinator", status=409)["error"]["code"] == "SWAP_STATE"
    row = reply(h, row, second)
    assert row["recipient_person_id"] == h.seed["people"][1] and row["recipient_id"] == h.seed["identities"]["staff_two"]
    assert row["status"] == "counteroffered" and row["confirmed_at"] is None and row["open_offer"]
    assert transition(h, row, "prepare", "coordinator", status=409)["error"]["code"] == "SWAP_STATE"
    h.get("staff_three", "/api/swaps/" + row["id"], status=404)
    h.get("staff_three", "/api/swaps/options?swap_id=" + row["id"], status=404)
    assert h.get("staff_three", "/api/swaps")["total"] == 0
    row = transition(h, row, "confirm")["swap"]
    result = transition(h, row, "prepare", "coordinator")
    published = h.publish(result["workspace"])
    final = h.get("staff", "/api/swaps/" + row["id"])["swap"]
    assert final["status"] == "published" and final["result_publication_id"] == published["publication_id"]
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source
    after = h.get("coordinator", "/api/publications/" + published["publication_id"])["snapshot"]
    assert [s["participants"][0]["person_id"] for s in after["shifts"]] == [
        h.seed["people"][1], h.seed["people"][0 if kind == "exchange" else 1], h.seed["people"][2]]


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_open_visibility_is_only_offered_summary_and_own_return_choices(both, kind):
    h = both
    pid, first, second, source = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    audience_ready(h)
    for who in ("staff_two", "staff_three"):
        listed = h.get(who, "/api/swaps?limit=1")
        assert listed["total"] == 1 and not listed["has_more"]
        detail = h.get(who, "/api/swaps/" + row["id"])
        assert detail["events"] == [] and detail["swap"] == listed["swaps"][0]
        assert not {"reason", "proposer_id", "recipient_id", "expected_content_hash", "workspace_id"} & detail["swap"].keys()
        assert detail["swap"]["offered_assignment"]["assignment_id"] == first
        assert second not in str(detail) and "Shared participant" not in str(detail)
        choices = h.get(who, "/api/swaps/options?swap_id=" + row["id"])
        assert choices["recipients"] == []
        assert all(a["person_id"] == h.seed["people"][1 if who == "staff_two" else 2] for a in choices["assignments"])
        assert (choices["eligible"] and not choices["assignments"]) if kind == "give" else choices["assignments"]
        assert transition(h, row, "withdraw", who, status=403)["error"]["code"] == "FORBIDDEN"
    action = "accept" if kind == "give" else "counteroffer"
    own_reply = {"reason": "Refuse self response", **({"assignment_id": second} if kind == "exchange" else {})}
    assert transition(h, row, action, payload=own_reply, status=403)["error"]["code"] == "FORBIDDEN"
    identity(h, "unlinked", ["staff"])
    h.get("unlinked", "/api/swaps", status=403)
    h.get("unlinked", "/api/swaps/" + row["id"], status=403)
    for who in ("auditor", "operator", "credentialer"):
        h.get(who, "/api/swaps", status=403)
        h.get(who, "/api/swaps/" + row["id"], status=403)
    h.command("admin", "role.grant", {"identity_id": h.seed["identities"]["staff_three"], "role": "coordinator",
        "starts_on": "2020-01-01", "reason": "Synthetic colleague with an additional role"})
    assert h.get("staff_three", "/api/swaps/options?swap_id=" + row["id"])["swap_id"] == row["id"]
    assert h.get("coordinator", "/api/publications/" + pid)["snapshot"] == source


@pytest.mark.parametrize("kind", ["exchange", "give"])
@pytest.mark.parametrize("state", ["expired", "stale"])
def test_open_response_authorizes_participants_before_disclosing_liveness(both, monkeypatch, kind, state):
    h = both
    pid, first, second, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    row = reply(h, row, second)
    if state == "expired":
        expired = m.now() + timedelta(days=1, seconds=1)
        freeze_clock(monkeypatch, lambda: expired)
    else:
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Publication, pid).current = False
    action = "accept" if kind == "give" else "counteroffer"
    value = {"reason": "Unauthorized response", **({"assignment_id": second} if kind == "exchange" else {})}
    for who in ("staff", "staff_three"):
        error = transition(h, row, action, who, value, status=403)["error"]
        assert error["code"] == "FORBIDDEN"
    assert transition(h, row, action, "staff_two", value, status=409)["error"]["code"] == "SWAP_" + state.upper()


@pytest.mark.parametrize("kind", ["exchange", "give"])
@pytest.mark.postgres
def test_concurrent_open_responses_bind_only_one_recipient(pg, kind):
    h = pg
    pid, first, second, source = source_pair(h, third=True)
    third = source["shifts"][2]["participants"][0]["id"]
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    audience_ready(h)
    clients = {who: h.client(who) for who in ("staff_two", "staff_three")}
    barrier = threading.Barrier(2)

    def attempt(item):
        who, assignment = item
        barrier.wait()
        return who, clients[who].post("/api/commands", headers={"idempotency-key": str(uuid.uuid4())}, json={
            "kind": "swap.accept" if kind == "give" else "swap.counteroffer", "target_id": row["id"],
            "expected_revision": row["revision"], "payload": {"reason": "Concurrent exact consent",
                **({"assignment_id": assignment} if kind == "exchange" else {})}})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [("staff_two", second), ("staff_three", third)]))
    assert sorted(response.status_code for _, response in results) == [200, 409]
    winner = next(response.json()["swap"] for _, response in results if response.status_code == 200)
    loser, conflict = next((who, response) for who, response in results if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "STALE_REVISION"
    assert winner["revision"] == row["revision"] + 1 and winner["status"] == "counteroffered"
    assert len(h.get("staff", "/api/swaps/" + row["id"])["events"]) == 2
    h.get(loser, "/api/swaps/" + row["id"], status=404)


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_open_response_undo_reopens_unclaimed_without_rewriting_consent(both, kind):
    h = both
    pid, first, second, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    audience_ready(h)
    row = reply(h, row, second)
    again = transition(h, row, "undo", "staff_two")["swap"]
    assert again["id"] != row["id"] and again["reopened_from_id"] == row["id"] and again["open_offer"]
    assert again["recipient_person_id"] is None and again["recipient_id"] is None and again["second_assignment_id"] is None
    old = h.get("staff", "/api/swaps/" + row["id"])["swap"]
    assert old["status"] == "withdrawn" and old["recipient_person_id"] == h.seed["people"][1]
    audience_ready(h)
    assert h.get("staff_three", "/api/swaps/" + again["id"])["events"] == []
    with transaction(h.app.state.engine) as db:
        notice = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{again['id']}:1:reopened"))
        assert row["proposer_person_id"] in notice.payload["person_ids"]
    assert reply(h, again, second)["recipient_person_id"] == h.seed["people"][1]


@pytest.mark.parametrize("kind", ["exchange", "give"])
def test_open_undo_receipts_stay_bound_to_the_original_participant(both, kind):
    h = both
    pid, first, second, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    audience_ready(h)
    row = reply(h, row, second)
    key = str(uuid.uuid4())
    result = transition(h, row, "undo", "staff_two", key=key)
    assert transition(h, row, "undo", "staff_two", key=key) == result
    audience_ready(h)
    h.get("staff_two", "/api/commands/" + key)
    with transaction(h.app.state.engine, write=True) as db:
        db.get(m.Identity, h.seed["identities"]["staff_two"]).person_id = h.seed["people"][2]
    # Public visibility of the reopened offer must not authorize its historical full command receipt.
    assert h.get("staff_two", "/api/swaps/" + result["swap"]["id"])["events"] == []
    h.get("staff_two", "/api/commands/" + key, status=404)
    transition(h, row, "undo", "staff_two", key=key, status=404)


@pytest.mark.parametrize("kind", ["exchange", "give"])
@pytest.mark.postgres
def test_claim_between_visibility_check_and_swap_list_cannot_disclose_recipient(pg, monkeypatch, kind):
    h = pg
    pid, first, second, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, kind))["swap"]
    audience_ready(h)
    visible, claimed = swaps.visible_open_ids, []

    def claim_after_visibility(db, actor, swap_id=None):
        ids = visible(db, actor, swap_id)
        if actor.identity_id == h.seed["identities"]["staff_three"] and not claimed:
            claimed.append(reply(h, row, second))
        return ids

    monkeypatch.setattr(swaps, "visible_open_ids", claim_after_visibility)
    result = h.get("staff_three", "/api/swaps")
    assert claimed and result["swaps"] == [] and result["total"] == 0
    h.get("staff_three", "/api/swaps/" + row["id"], status=404)


def test_open_offer_reads_use_frozen_membership_without_eligibility(h, monkeypatch):
    pid, first, second, _ = source_pair(h, third=True)
    row = h.command("staff", "swap.offer", payload(pid, first, "exchange"))["swap"]
    h.command("staff_two", "swap.offer", payload(pid, second, "exchange"))
    audience_ready(h)

    def no_eligibility(*args, **kwargs):
        pytest.fail("Reading an open offer must use stored audience membership")

    monkeypatch.setattr(swaps.Eligibility, "recipient", no_eligibility)
    monkeypatch.setattr(swaps, "eligible_staff", no_eligibility)
    assert h.get("staff_three", "/api/swaps/" + row["id"])["swap"]["id"] == row["id"]
    assert h.get("staff_three", "/api/swaps")["total"] == 2
    assert h.get("staff_three", "/api/requests/all")["total"] == 2


@pytest.mark.parametrize("kind", ["exchange", "give"])
@pytest.mark.postgres
def test_open_notifications_freeze_audience_and_deliver_offered_and_taken_once(pg, kind):
    h = pg
    pid, first, second, _ = source_pair(h, third=True)
    key = str(uuid.uuid4())
    row = h.command("staff", "swap.offer", payload(pid, first, kind), key=key)["swap"]
    assert h.command("staff", "swap.offer", payload(pid, first, kind), key=key)["swap"] == row
    audience_ready(h)
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert set(event.payload["person_ids"]) == set(h.seed["people"][1:3])
    reply(h, row, second)
    for _ in range(30):
        if not process_one(h.app.state.engine):
            break
    label = "give-away" if kind == "give" else "swap"
    expected = [f"Open {label} offer", f"Open {label} offer taken"]
    def open_notices(who):
        return [n for n in h.get(who, "/api/notifications")["notifications"] if n["title"].startswith("Open ")]
    notices = open_notices("staff_three")
    assert sorted(n["title"] for n in notices) == expected
    assert all(n["link"] == {} and "Shared participant" not in n["message"] for n in notices)
    assert [n["title"] for n in open_notices("staff")] == [expected[1]]
    assert [n["title"] for n in open_notices("staff_two")] == [expected[0]]
    assert open_notices("staff")[0]["link"] == {"swap_id": row["id"]}
    # Dispatch excludes the command actor; a retained claimant notice must still open its consent thread.
    with transaction(h.app.state.engine, write=True) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:2:" +
            ("accept" if kind == "give" else "counteroffer")))
        db.add(m.Notification(outbox_id=event.id, identity_id=h.seed["identities"]["staff_two"],
            title=expected[1], message="Taken notification retained for its claimant"))
    taken = [n for n in open_notices("staff_two") if n["title"] == expected[1]]
    assert len(taken) == 1 and taken[0]["link"] == {"swap_id": row["id"]}
    with transaction(h.app.state.engine, write=True) as db:
        for event in db.scalars(select(m.Outbox).where(m.Outbox.kind == "swap")):
            deliver_notifications(db, event)
            deliver_notifications(db, event)
    assert open_notices("staff_three") == notices


def test_ineligible_colleague_is_hidden_and_not_in_initial_outbox_audience(h):
    pid, first, _, _ = source_pair(h)  # The third colleague has no return duty for an exchange.
    row = h.command("staff", "swap.offer", payload(pid, first, "exchange"))["swap"]
    audience_ready(h)
    assert h.get("staff_three", "/api/swaps")["total"] == 0
    h.get("staff_three", "/api/swaps/" + row["id"], status=404)
    h.get("staff_three", "/api/swaps/options?swap_id=" + row["id"], status=404)
    with transaction(h.app.state.engine) as db:
        event = db.scalar(select(m.Outbox).where(m.Outbox.event_key == f"swap:{row['id']}:1:offered"))
        assert event.payload["person_ids"] == [h.seed["people"][1]]
    assert h.command("staff", "swap.offer", payload(pid, first, "give"), status=409)["error"]["code"] == "SWAP_DUPLICATE"
