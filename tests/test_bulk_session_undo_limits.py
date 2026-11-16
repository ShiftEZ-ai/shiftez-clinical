"""A coalesced workspace inverse counts the sessions it actually restores or removes."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits


def state(h, workspace):
    with transaction(h.app.state.engine) as db:
        return record(db.get(m.Workspace, workspace["id"])), [record(row) for row in db.scalars(
            select(m.Shift).where(m.Shift.workspace_id == workspace["id"]).order_by(m.Shift.id))]


@pytest.mark.parametrize("stored", [False, True])
def test_bulk_session_undo_counts_every_changed_session_in_the_workspace_snapshot(h, stored):
    first = h.shift(h.workspace())
    second = h.shift(first["workspace"], start="2026-10-05T08:00:00+03:00", end="2026-10-05T12:00:00+03:00")
    workspace = second["workspace"]
    payload = {"workspace_id": workspace["id"], "shift_ids": [first["shifts"][0]["id"], second["shifts"][0]["id"]],
               "offset_days": 2, "reason": REASON}
    if stored:
        copied = h.command("coordinator", "bulk.apply", {"items": [{"item_kind": "shift.copy",
            "target_id": workspace["id"], "expected_revision": workspace["revision"], "payload": payload}], "reason": REASON})
        body = copied["undo"]["payload"]
        assert len(body["items"]) == 1
    else:
        copied = h.command("coordinator", "shift.copy", payload, workspace["id"], workspace["revision"])
        body = {"items": [{"item_kind": "shift.undo_batch", "target_id": workspace["id"],
            "expected_revision": copied["workspace"]["revision"]}], "reason": REASON}
    before = state(h, workspace)
    assert len(before[1]) == 4
    limits(h, 1)
    checked = preview(h, "coordinator", body)
    assert checked["counts"]["problem"] == 1
    assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
    refused = h.command("coordinator", "bulk.apply", body, status=422 if stored else 409)
    assert refused["error"]["code"] == "BULK_ITEM_LIMIT"
    assert state(h, workspace) == before
    limits(h, 2)
    assert preview(h, "coordinator", body)["counts"]["problem"] == 0
    assert state(h, workspace) == before
    h.command("coordinator", "bulk.apply", body)
    assert len(state(h, workspace)[1]) == 2
