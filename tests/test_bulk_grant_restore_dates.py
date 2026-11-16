"""A skill restoration's Undo restores its complete previously revoked interval."""
from datetime import date

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk_single_commands import relationship, undo


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("previous_end", ["2026-02-28", None])
def test_restore_grant_undo_restores_previous_dates_and_revocation_atomically(h, bulk, previous_end):
    grant, _, role = relationship(h, "grant", starts_on="2026-01-01", ends_on=previous_end)
    revoked = h.command(role, "grant.revoke", {"ends_on": previous_end, "reason": "Withdraw old evidence"},
                        grant["id"], grant["revision"])["record"]
    payload = {"starts_on": "2026-03-01", "ends_on": "2026-04-30", "reason": "Correct evidence dates"}
    if bulk:
        restored = h.command(role, "bulk.apply", {"item_kind": "grant.restore", "reason": payload["reason"],
            "items": [{"target_id": revoked["id"], "expected_revision": revoked["revision"], "payload": payload}]})
    else:
        restored = h.command(role, "grant.restore", payload, revoked["id"], revoked["revision"])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.CapabilityGrant, grant["id"])
        assert (row.starts_on, row.ends_on, row.revoked) == (date(2026, 3, 1), date(2026, 4, 30), False)
    undone = undo(h, role, restored["undo"])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.CapabilityGrant, grant["id"])
        assert (row.starts_on, row.ends_on, row.revoked, row.revision) == (
            date(2026, 1, 1), date.fromisoformat(previous_end) if previous_end else None, True, revoked["revision"] + 2)
        history = list(db.scalars(select(m.DefinitionHistory).where(m.DefinitionHistory.object_id == row.id)
                                 .order_by(m.DefinitionHistory.object_revision)))
        assert len(history) == 4
        assert history[-1].snapshot["starts_on"] == "2026-01-01"
        assert history[-1].snapshot["ends_on"] == previous_end
        assert history[-1].snapshot["revoked"] is True
    undo(h, role, restored["undo"], status=409)
    undo(h, role, undone["undo"])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.CapabilityGrant, grant["id"])
        assert (row.starts_on, row.ends_on, row.revoked) == (date(2026, 3, 1), date(2026, 4, 30), False)


def test_grant_revoke_refuses_invalid_complete_interval_without_changing_record(h):
    grant, _, role = relationship(h, "grant", starts_on="2026-03-01", ends_on="2026-04-30")
    result = h.command(role, "grant.revoke", {"starts_on": "2026-03-01", "ends_on": "2026-02-28",
                       "reason": "Invalid prior dates"}, grant["id"], grant["revision"], status=422)
    assert result["error"]["code"] == "SCHEMA_INVALID"
    assert any("End date must follow start date" in issue["message"] for issue in result["error"]["details"])
    with transaction(h.app.state.engine) as db:
        row = db.get(m.CapabilityGrant, grant["id"])
        assert (row.starts_on, row.ends_on, row.revoked, row.revision) == (
            date(2026, 3, 1), date(2026, 4, 30), False, grant["revision"])
