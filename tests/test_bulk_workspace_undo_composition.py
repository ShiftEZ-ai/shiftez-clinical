"""A workspace's coalesced edit cannot be undone within the same bulk change."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from apps.api.snapshots import workspace_shifts
from .test_bulk import REASON, preview


def snapshot(h, workspace):
    with transaction(h.app.state.engine) as db:
        return (record(db.get(m.Workspace, workspace["id"])), workspace_shifts(db, workspace["id"]),
                [record(row) for row in db.scalars(select(m.ChangeSet).where(
                    m.ChangeSet.workspace_id == workspace["id"]).order_by(m.ChangeSet.id))])


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("shape", ["items", "actions", "session"])
def test_bulk_refuses_undo_after_an_edit_to_the_same_workspace_atomically(request, database, shape):
    h = request.getfixturevalue(database)
    created = h.shift(h.workspace())
    workspace, session = created["workspace"], created["shifts"][0]
    edit = {"item_kind": "shift.action", "target_id": workspace["id"], "expected_revision": workspace["revision"],
            "payload": {"workspace_id": workspace["id"], "shift_ids": [session["id"]], "action": "cancel"}}
    undo = {"item_kind": "shift.undo_batch", "target_id": workspace["id"], "expected_revision": workspace["revision"]}
    body = {"items": [edit, undo], "reason": REASON}
    if shape == "actions":
        body["items"] = [{"target_id": workspace["id"], "expected_revision": workspace["revision"], "changes": [
            {"item_kind": "shift.action", "payload": edit["payload"]}, {"item_kind": "shift.undo_batch"}]}]
    elif shape == "session":
        edit.update(target_id=session["id"], expected_revision=session["revision"], payload={"action": "cancel"})
    before = snapshot(h, workspace)
    checked = preview(h, "coordinator", body)
    failed_index = 0 if shape == "actions" else 1
    assert checked["items"][failed_index]["result"] == "problem"
    issue = checked["items"][failed_index]["issues"][0]
    assert issue["code"] == "BULK_UNDO_CONFLICT"
    assert "separate" in issue["message"]
    assert snapshot(h, workspace) == before
    refused = h.command("coordinator", "bulk.apply", body, status=409)["error"]
    assert refused["code"] == "BULK_UNDO_CONFLICT" and refused["details"]["failed_index"] == failed_index
    assert snapshot(h, workspace) == before


def test_bulk_can_edit_one_workspace_and_undo_another_without_an_invalid_returned_inverse(h):
    first = h.shift(h.workspace("First synthetic rota"))
    second = h.shift(h.workspace("Second synthetic rota"))
    a, b = first["workspace"], second["workspace"]
    before_a = snapshot(h, a)[1]
    body = {"reason": REASON, "items": [
        {"item_kind": "shift.action", "target_id": a["id"], "expected_revision": a["revision"],
         "payload": {"workspace_id": a["id"], "shift_ids": [first["shifts"][0]["id"]], "action": "cancel"}},
        {"item_kind": "shift.undo_batch", "target_id": b["id"], "expected_revision": b["revision"]}]}
    assert preview(h, "coordinator", body)["counts"]["problem"] == 0
    changed = h.command("coordinator", "bulk.apply", body)
    assert changed["undo_partial"] and len(changed["undo"]["payload"]["items"]) == 1
    assert changed["undo"]["payload"]["items"][0]["target_id"] == a["id"]
    assert snapshot(h, b)[1] == []
    h.command("coordinator", **changed["undo"])
    assert snapshot(h, a)[1] == before_a and snapshot(h, b)[1] == []
