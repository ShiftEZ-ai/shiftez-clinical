"""Request decisions and their inverses include their actual linked-record footprint."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits
from .test_overtime_volunteer import offer, offer_rule
from .test_request_decide_many import request_row
from .test_rotation_attachments import rotation_value


def request_case(h, source):
    if source == "leave":
        return request_row(h), 2
    if source == "overtime":
        offer_rule(h)
        return offer(h)["record"], 2
    value = rotation_value(h)
    rotation = h.configure("rotations", value, role="coordinator")["record"]
    return h.command("staff", "rotation.request_change", {"rotation_id": rotation["id"],
        **{key: value[key] for key in ("rotation_type_id", "service_id", "ends_on")},
        "starts_on": "2026-10-02", "private_reason": REASON})["record"], 4


def state(h):
    with transaction(h.app.state.engine) as db:
        return {cls.__tablename__: [record(row) for row in db.scalars(select(cls).order_by(cls.id))]
                for cls in (m.RequestRecord, m.Availability, m.SchedulingFact, m.Rotation, m.Mentorship)}


@pytest.mark.parametrize("source", ["leave", "overtime", "rotation"])
@pytest.mark.parametrize("aggregate", [False, True])
def test_bulk_request_decisions_and_undo_count_all_linked_records(h, source, aggregate):
    row, affected = request_case(h, source)
    role = "coordinator" if source == "overtime" else "approver"
    item = {"item_kind": "request.decide", "target_id": row["id"], "expected_revision": row["revision"],
            "payload": {"decision": "approve"}}
    if aggregate:
        item = {"item_kind": "request.decide_many", "payload": {"decision": "approve", "items": [
            {"source": "request", "id": row["id"], "expected_revision": row["revision"]}]}}
    body = {"items": [item], "reason": REASON}
    for _ in range(3):
        before = state(h)
        limits(h, affected - 1)
        checked = preview(h, role, body)
        assert checked["counts"]["problem"] == 1, checked
        assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
        refused = h.command(role, "bulk.apply", body, status=422)
        assert refused["error"]["code"] == "BULK_ITEM_LIMIT"
        assert state(h) == before
        limits(h, affected)
        assert preview(h, role, body)["counts"]["problem"] == 0
        applied = h.command(role, "bulk.apply", body)
        body = applied["undo"]["payload"]
