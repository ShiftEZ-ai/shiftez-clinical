"""A complete stored inverse follows both edits and lifecycle transitions on one absence."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview
from .test_bulk_pending_privacy import DATABASES
from .test_bulk_single_commands import availability
from .governance_support import identity


def state(h, target):
    with transaction(h.app.state.engine) as db:
        return {key: value for key, value in record(db.get(m.Availability, target)).items()
                if key != "revision"}


def mixed(h, sequence):
    original = availability(h)
    items = []
    for index, action in enumerate(sequence.split(",")):
        items.append({"item_kind": "availability." + action, "target_id": original["id"],
            "expected_revision": original["revision"], "payload": {
                "start": f"2026-10-04T{9 + index:02}:00:00+03:00",
                "end": f"2026-10-04T{10 + index:02}:00:00+03:00"} if action == "update" else {}})
    return original, {"items": items, "reason": REASON}


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("sequence", ["update,retract", "retract,restore,update", "update,retract,restore,update,retract"])
def test_stored_availability_inverse_and_redo_follow_the_complete_revision_chain(request, database, sequence):
    h = request.getfixturevalue(database)
    original, body = mixed(h, sequence)
    before = state(h, original["id"])
    assert preview(h, "coordinator", body)["counts"]["problem"] == 0
    assert state(h, original["id"]) == before
    changed = h.command("coordinator", "bulk.apply", body)
    after = state(h, original["id"])
    assert after != before
    inverse = changed["undo"]
    for expected in (before, after, before):
        current = state(h, original["id"])
        assert preview(h, "coordinator", inverse["payload"])["counts"]["problem"] == 0
        assert state(h, original["id"]) == current
        undone = h.command("coordinator", **inverse)
        assert state(h, original["id"]) == expected
        inverse = undone["undo"]


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("outside_change", [False, True])
def test_mixed_availability_inverse_keeps_receipt_and_external_revision_guards(request, database, outside_change):
    h = request.getfixturevalue(database)
    original, body = mixed(h, "update,retract")
    applied = h.command("coordinator", "bulk.apply", body)
    inverse = deepcopy(applied["undo"])
    if outside_change:
        current = applied["items"][-1]["results"][0]["record"]
        h.command("coordinator", "availability.restore", {"reason": "An outside lifecycle change"},
                  original["id"], current["revision"])
    else:
        inverse["payload"].pop("undo_of")
    before = state(h, original["id"])
    checked = preview(h, "coordinator", inverse["payload"])
    assert any(issue["code"] == "STALE_REVISION" for row in checked["items"] for issue in row["issues"])
    refused = h.command("coordinator", **inverse, status=409)
    assert refused["error"]["code"] == "STALE_REVISION"
    assert state(h, original["id"]) == before


def approved_request(h, aggregate=False):
    identity(h, "request_inverse_decider", ["coordinator", "approver"])
    row = h.command("staff", "request.create", {"person_id": h.seed["people"][0], "kind": "leave",
        "start": "2026-11-01T00:00:00+03:00", "end": "2026-11-02T00:00:00+03:00",
        "private_reason": "Synthetic receipt-bound approval"})["record"]
    payload = {"reason": REASON, "decision": "approve"}
    if aggregate:
        reply = h.command("request_inverse_decider", "request.decide_many", {**payload,
            "items": [{"id": row["id"], "expected_revision": row["revision"]}]})
        row = reply["records"][0]
    else:
        reply = h.command("request_inverse_decider", "request.decide", payload, row["id"], row["revision"])
        row = reply["record"]
    with transaction(h.app.state.engine) as db:
        absence = record(db.scalar(select(m.Availability).where(m.Availability.request_id == row["id"])))
    item = {"item_kind": "request.undo", "payload": {"batch_id": reply["receipt_id"]} if aggregate else {},
            **({} if aggregate else {"target_id": row["id"], "expected_revision": row["revision"]})}
    return row, absence, item


@pytest.mark.parametrize("database", DATABASES)
@pytest.mark.parametrize("aggregate", [False, True])
def test_request_decision_inverse_restores_receipt_bound_absence_and_its_edits(request, database, aggregate):
    h = request.getfixturevalue(database)
    row, absence, item = approved_request(h, aggregate)
    original = state(h, absence["id"])
    change = {"items": [{"item_kind": "availability.update", "target_id": absence["id"],
        "expected_revision": absence["revision"], "payload": {"start": "2026-11-01T01:00:00+03:00"}}, item], "reason": REASON}
    applied = h.command("request_inverse_decider", "bulk.apply", change)
    after = state(h, absence["id"])
    inverse = applied["undo"]
    for expected in (original, after, original):
        checked = preview(h, "request_inverse_decider", inverse["payload"])
        assert checked["counts"]["problem"] == 0, checked
        undone = h.command("request_inverse_decider", **inverse)
        assert state(h, absence["id"]) == expected
        with transaction(h.app.state.engine) as db:
            assert list(db.scalars(select(m.Availability.id).where(m.Availability.request_id == row["id"]))) == [absence["id"]]
        inverse = undone["undo"]


@pytest.mark.parametrize("database", DATABASES)
def test_request_inverse_refuses_independently_changed_absence_atomically(request, database):
    h = request.getfixturevalue(database)
    row, absence, item = approved_request(h)
    applied = h.command("request_inverse_decider", "bulk.apply", {"items": [item], "reason": REASON})
    with transaction(h.app.state.engine) as db:
        current = record(db.get(m.Availability, absence["id"]))
    for action in ("restore", "retract"):
        current = h.command("coordinator", "availability." + action, {"reason": "Outside absence transition"},
                            current["id"], current["revision"])["record"]
    before = state(h, absence["id"])
    checked = preview(h, "request_inverse_decider", applied["undo"]["payload"])
    assert checked["items"][0]["issues"][0]["code"] == "STALE_REVISION"
    refused = h.command("request_inverse_decider", **applied["undo"], status=409)
    assert refused["error"]["code"] == "STALE_REVISION"
    assert state(h, absence["id"]) == before
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, row["id"]).status == "submitted"


def test_repeated_request_undo_in_one_receipt_keeps_each_original_absence(h):
    row, first, item = approved_request(h)
    body = {"items": [item, {"item_kind": "request.decide", "target_id": row["id"],
        "expected_revision": row["revision"], "payload": {"decision": "approve"}}, deepcopy(item)], "reason": REASON}
    applied = h.command("request_inverse_decider", "bulk.apply", body)
    assert preview(h, "request_inverse_decider", applied["undo"]["payload"])["counts"]["problem"] == 0
    h.command("request_inverse_decider", **applied["undo"])
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.Availability.id).where(m.Availability.request_id == row["id"],
            m.Availability.retracted.is_(False)))) == [first["id"]]
