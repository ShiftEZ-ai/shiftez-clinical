"""Replacing person tags counts both membership deltas in each checked direction."""
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from .test_bulk import REASON, preview
from .test_bulk_nested_limits import limits


def test_bulk_person_tag_replacement_and_undo_count_added_and_removed_members(h):
    for name in ("Synthetic first team", "Synthetic next team"):
        h.configure("tags", {"name": name, "labels": ["people"]})
    person = h.configure("people", {"display_name": "Synthetic tag limit owner", "tags": ["Synthetic first team"]})["record"]
    body = {"item_kind": "catalogue.update", "common": {"catalogue": "people"}, "items": [{
        "target_id": person["id"], "expected_revision": person["revision"],
        "changes": [{"field": "tags", "how": "set", "value": ["Synthetic next team"]}]}], "reason": REASON}
    for _ in range(2):
        with transaction(h.app.state.engine) as db:
            before = db.get(m.Person, person["id"]).tags
        limits(h, 2)
        checked = preview(h, "coordinator", body)
        assert checked["items"][0]["issues"][0]["code"] == "BULK_ITEM_LIMIT"
        refused = h.command("coordinator", "bulk.apply", body, status=422 if body.get("undo_of") else 409)
        assert refused["error"]["code"] == "BULK_ITEM_LIMIT"
        with transaction(h.app.state.engine) as db:
            assert db.get(m.Person, person["id"]).tags == before
            assert len(list(db.scalars(select(m.TagMember).where(m.TagMember.member_id == person["id"])))) == 1
        limits(h, 3)
        assert preview(h, "coordinator", body)["counts"]["problem"] == 0
        applied = h.command("coordinator", "bulk.apply", body)
        body = applied["undo"]["payload"]
