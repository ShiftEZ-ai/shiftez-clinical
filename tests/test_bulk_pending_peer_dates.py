"""Dated relationship checks use preceding edits in the same atomic batch."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .governance_support import identity
from .test_bulk import REASON, preview
from .test_bulk_single_commands import relationship


def test_eligibility_extension_sees_permission_moved_from_before_to_after_in_same_batch(h):
    earlier, payload, role = relationship(h, "eligibility", starts_on="2026-01-01", ends_on="2026-02-01")
    target = h.command(role, "eligibility.create", {
        **payload, "starts_on": "2026-03-01", "ends_on": "2026-04-01"})["record"]
    change = {"items": [
        {"item_kind": "eligibility.update_dates", "target_id": earlier["id"],
         "expected_revision": earlier["revision"],
         "payload": {"starts_on": "2026-05-01", "ends_on": "2026-06-01"}},
        {"item_kind": "eligibility.extend", "target_id": target["id"],
         "expected_revision": target["revision"], "payload": {"ends_on": "2026-07-01"}},
    ], "resolutions": {"0": "shorten_anyway"}, "reason": REASON}

    checked = preview(h, role, change)
    assert checked["items"][1]["result"] == "problem"
    assert checked["items"][1]["issues"][0]["code"] == "DUPLICATE_RELATIONSHIP"
    assert checked["items"][1]["issues"][0]["message"] == "A later record already covers part of this period"
    refused = h.command(role, "bulk.apply", change, status=409)["error"]
    assert refused["code"] == "DUPLICATE_RELATIONSHIP" and refused["details"]["failed_index"] == 1
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Eligibility, earlier["id"]).starts_on.isoformat() == "2026-01-01"
        assert db.get(m.Eligibility, target["id"]).ends_on.isoformat() == "2026-04-01"
        assert list(db.scalars(select(m.AuditEvent.id).where(m.AuditEvent.action == "bulk.apply"))) == []


@pytest.mark.parametrize("kind", ["membership", "eligibility"])
def test_end_relationship_cannot_extend_through_another_permission(h, kind):
    original, payload, role = relationship(h, kind)
    h.command(role, kind + ".create", {**payload, "starts_on": "2026-12-01", "ends_on": "2027-02-01"})
    change = {"ends_on": "2027-01-01", "reason": REASON}
    refused = h.command(role, kind + ".end", change, original["id"], original["revision"], status=409)["error"]
    assert refused["code"] == "DUPLICATE_RELATIONSHIP"
    value = {"item_kind": kind + ".end", "items": [{"target_id": original["id"],
        "expected_revision": original["revision"], "payload": change}], "reason": REASON}
    assert preview(h, role, value)["items"][0]["issues"][0]["code"] == "DUPLICATE_RELATIONSHIP"
    assert h.command(role, "bulk.apply", value, status=409)["error"]["code"] == "DUPLICATE_RELATIONSHIP"
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Membership if kind == "membership" else m.Eligibility, original["id"])
        assert row.ends_on.isoformat() == original["ends_on"] and row.revision == original["revision"]


@pytest.mark.parametrize("action,end", [("end", "2026-12-01"), ("extend", "2026-12-01")])
def test_eligibility_end_and_extension_keep_single_relationship_independence(h, action, end):
    original, _, _ = relationship(h, "eligibility")
    identity(h, "own_bulk_permission", ["credentialer"], person=original["person_id"])
    payload = {"ends_on": end, "reason": REASON}
    refused = h.command("own_bulk_permission", "eligibility." + action, payload,
        original["id"], original["revision"], status=403)["error"]
    assert refused["code"] == "SELF_APPROVAL"
    value = {"item_kind": "eligibility." + action, "items": [{"target_id": original["id"],
        "expected_revision": original["revision"], "payload": payload}], "reason": REASON}
    assert preview(h, "own_bulk_permission", value)["items"][0]["issues"][0]["code"] == "SELF_APPROVAL"


def test_ending_own_eligibility_earlier_retains_existing_self_restriction(h):
    original, _, _ = relationship(h, "eligibility")
    identity(h, "own_bulk_permission", ["credentialer"], person=original["person_id"])
    payload = {"ends_on": "2026-10-15", "reason": REASON}
    value = {"item_kind": "eligibility.end", "items": [{"target_id": original["id"],
        "expected_revision": original["revision"], "payload": payload}], "reason": REASON}
    assert preview(h, "own_bulk_permission", value)["counts"]["problem"] == 0
    changed = h.command("own_bulk_permission", "eligibility.end", payload,
        original["id"], original["revision"])["record"]
    assert changed["ends_on"] == "2026-10-15"
