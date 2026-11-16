"""An overtime withdrawal includes the approval's not-yet-flushed fact records."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_overtime_volunteer import offer, offer_rule


@pytest.mark.parametrize("restore", [False, True])
def test_bulk_approve_withdraw_and_restore_overtime_facts_share_pending_state(h, restore):
    offer_rule(h)
    h.command("admin", "self_approval.grant", {"identity_id": h.seed["identities"]["coordinator"],
        "actions": ["request.decide"], "starts_on": "2026-01-01", "ends_on": "2027-01-01", "reason": REASON})
    row = offer(h, role="coordinator")["record"]
    def item(kind, decision=None):
        return {"item_kind": kind, "target_id": row["id"], "expected_revision": row["revision"],
                "payload": {"decision": decision} if decision else {}}
    items = [item("request.decide", "approve"), item("request.decide", "withdraw")]
    if restore:
        items.append(item("request.undo"))
    value = {"items": items, "reason": REASON}
    checked = preview(h, "coordinator", value)
    assert checked["counts"]["problem"] == 0
    with transaction(h.app.state.engine) as db:
        assert list(db.scalars(select(m.SchedulingFact.id).where(m.SchedulingFact.request_id == row["id"]))) == []
    reply = h.command("coordinator", "bulk.apply", value)
    assert [part["results"][0]["affected_count"] for part in reply["items"]] == [2] * len(items)
    with transaction(h.app.state.engine) as db:
        request = db.get(m.RequestRecord, row["id"])
        facts = list(db.scalars(select(m.SchedulingFact).where(m.SchedulingFact.request_id == row["id"])))
        assert request.status == ("approved" if restore else "withdrawn")
        assert len(facts) == 1
        assert (facts[0].status, facts[0].revision) == (("active", 3) if restore else ("retired", 2))
        assert request.overtime["withdrawn_facts"] == [facts[0].id]
