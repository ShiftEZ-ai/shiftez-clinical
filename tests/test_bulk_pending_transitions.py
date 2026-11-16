"""A bulk handler must observe earlier writes in the same transaction."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_availability_patterns import pattern


REASON = {"reason": "Synthetic pending transition review"}


def item(kind, payload=None, row=None):
    return {"item_kind": kind, "payload": payload or REASON,
            **({"target_id": row["id"], "expected_revision": row["revision"]} if row else {})}


def bulk(h, role, items, status=200):
    return h.command(role, "bulk.apply", {"items": items, **REASON}, status=status)


def wish(h, day="2026-10-05", points=1):
    return {"person_id": h.seed["people"][0], "first_day": day, "last_day": day,
            "kind": "prefer_off", "points": points}


def leave(h):
    return {"person_id": h.seed["people"][0], "kind": "recorded_leave", "start": "2026-10-05T08:00:00+03:00",
            "end": "2026-10-05T12:00:00+03:00", "private_reason": "Synthetic leave fixture"}


def request(h):
    payload = {key: value for key, value in leave(h).items() if key != "kind"}
    return h.command("staff", "request.create", payload)["record"]


@pytest.mark.parametrize("case,code", [("overlap", "WISH_OVERLAP"), ("budget", "WISH_BUDGET")])
def test_bulk_wish_creation_checks_earlier_pending_wishes(h, case, code):
    values = [wish(h), wish(h)] if case == "overlap" else [wish(h, points=6), wish(h, "2026-10-06", 5)]
    refused = bulk(h, "staff", [item("wish.create", payload) for payload in values], status=409)
    assert refused["error"]["code"] == code and refused["error"]["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.StaffWish.id))) == []


def test_bulk_wish_withdraw_then_recreate_uses_current_lifecycle(h):
    original = h.command("staff", "wish.create", wish(h, points=6))["wish"]
    bulk(h, "staff", [item("wish.withdraw", row=original), item("wish.create", wish(h, points=6))])
    with transaction(h.app.state.engine) as db:
        rows = list(db.scalars(select(m.StaffWish)))
        assert len(rows) == 2 and sorted(row.status for row in rows) == ["open", "withdrawn"]


def test_bulk_retract_leave_then_wish_uses_current_availability(h):
    original = h.command("coordinator", "availability.record", leave(h))["record"]
    bulk(h, "coordinator", [item("availability.retract", row=original), item("wish.create", wish(h))])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, original["id"]).retracted
        assert len(list(db.scalars(select(m.StaffWish.id)))) == 1


def test_bulk_restore_leave_then_wish_refuses_and_rolls_back(h):
    original = h.command("coordinator", "availability.record", leave(h))["record"]
    retracted = h.command("coordinator", "availability.retract", REASON, original["id"], original["revision"])["record"]
    refused = bulk(h, "coordinator", [item("availability.restore", row=retracted), item("wish.create", wish(h))], status=409)
    assert refused["error"]["code"] == "WISH_ON_LEAVE"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Availability, original["id"]).retracted
        assert list(db.scalars(select(m.StaffWish.id))) == []


def test_bulk_request_approval_then_wish_sees_pending_absence(h):
    original = request(h)
    refused = bulk(h, "admin", [item("request.decide", {"decision": "approve", **REASON}, original),
                                 item("wish.create", wish(h))], status=409)
    assert refused["error"]["code"] == "WISH_ON_LEAVE"
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, original["id"]).status == "submitted"
        assert list(db.scalars(select(m.Availability.id).where(m.Availability.request_id == original["id"]))) == []


def test_bulk_request_approval_then_undo_retracts_pending_absence(h):
    original = request(h)
    bulk(h, "approver", [item("request.decide", {"decision": "approve", **REASON}, original),
                          item("request.undo", row=original)])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.RequestRecord, original["id"]).status == "submitted"
        absences = list(db.scalars(select(m.Availability).where(m.Availability.request_id == original["id"])))
        assert len(absences) == 1 and absences[0].retracted


def test_bulk_pattern_end_then_restore_reads_pending_history(h):
    original = h.command("staff", "availability_pattern.propose", pattern(h.seed["people"][0]))["pattern"]
    bulk(h, "staff", [item("availability_pattern.end", row=original), item("availability_pattern.restore", row=original)])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.AvailabilityPattern, original["id"])
        assert row.status == "proposed" and row.revision == original["revision"] + 2


def test_bulk_exception_approval_then_end_keeps_one_history_per_revision(h):
    policy = h.policy({"kind": "max_duties", "maximum": 4, "window": "day"}, exception=True)
    original = h.command("coordinator", "exception.create", {"policy_id": policy["id"], "person_id": h.seed["people"][0],
        "starts_on": "2026-10-04", "ends_on": "2026-10-11", **REASON})["record"]
    bulk(h, "approver", [item("exception.approve", row=original), item("exception.end", row=original)])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.PolicyException, original["id"]).status == "ended"
        versions = list(db.scalars(select(m.DefinitionHistory.object_revision).where(
            m.DefinitionHistory.kind == "exception", m.DefinitionHistory.object_id == original["id"]).order_by(
                m.DefinitionHistory.object_revision)))
        assert versions == [1, 2, 3]


def test_bulk_grouped_rule_activation_then_retirement_restores_predecessor(h):
    from .test_rule_catalogue_conflicts import line, most, propose, approve
    original = approve(h, propose(h, "duties_per_week", [line(numbers=most(5))])["record"])
    replacement = propose(h, "duties_per_week", [line(numbers=most(4))], policy_id=original["id"], revision=original["revision"])["record"]
    bulk(h, "approver", [item("policy.activate", row=replacement), item("policy.retire", row=replacement)])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Policy, replacement["id"]).status == "retired"
        assert db.get(m.Policy, original["id"]).status == "active"
