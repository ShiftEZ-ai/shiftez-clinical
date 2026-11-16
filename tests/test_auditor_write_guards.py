"""Auditor and staff read access never becomes a write path through broad commands."""

import pytest


def absence_bulk(h):
    return {
        "item_kind": "availability.record",
        "items": [{"payload": {
            "person_id": h.seed["people"][0],
            "kind": "actual_absence",
            "start": "2026-10-04T09:00:00+03:00",
            "end": "2026-10-04T10:00:00+03:00",
            "private_reason": "Synthetic auditor write guard",
        }}],
        "reason": "Synthetic auditor write guard",
    }


@pytest.mark.parametrize("role", ["auditor", "staff"])
def test_read_roles_cannot_apply_or_stage_bulk_writes(h, role):
    payload = absence_bulk(h)
    h.command(role, "bulk.apply", payload, status=403)
    h.command(role, "bulk.stage", {"payload": payload, "reason": payload["reason"]}, status=403)


def test_auditor_and_staff_cannot_undo_or_withdraw_someone_elses_request(h):
    request = h.command("coordinator", "request.create", {
        "person_id": h.seed["people"][1], "kind": "meeting",
        "start": "2026-10-04T09:00:00+03:00", "end": "2026-10-04T10:00:00+03:00",
        "private_reason": "Synthetic request owned by another person",
    })["record"]
    decided = h.command("approver", "request.decide", {
        "decision": "approve", "reason": "Synthetic independent decision",
    }, request["id"], request["revision"])["record"]
    for role in ("auditor", "staff"):
        h.command(role, "request.undo", {"reason": "Synthetic other-person request undo"},
                  decided["id"], decided["revision"], status=403)
        h.command(role, "request.decide", {"decision": "withdraw", "reason": "Synthetic other-person withdrawal"},
                  request["id"], request["revision"], status=403)
