"""Stored inverses bind the batch boundary and follow actual related-record revisions."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_events import event


def mixed_change(h, collection, operation):
    cls = m.Person if collection == "people" else m.Event
    source = h.get("admin", "/api/catalogues")[collection][0] if collection == "people" else event(h)
    tag = h.configure("tags", {"name": "Synthetic revision tag", "labels": [collection]})["record"]
    ref = {"kind": collection, "id": source["id"]}
    if operation == "rename":
        tag = h.command("admin", "tag.members", {"add": [ref], "reason": REASON},
                        tag["id"], tag["revision"])["record"]
        with transaction(h.app.state.engine) as db:
            source = record(db.get(cls, source["id"]))
    field = "title" if collection == "people" else "heading"
    items = [{"item_kind": "catalogue.update", "target_id": source["id"],
              "expected_revision": source["revision"], "payload": {"catalogue": collection},
              "changes": [{"field": field, "value": title}]} for title in ("First revision", "Second revision")]
    if operation == "rename":
        tag_value = {key: tag[key] for key in s.TagInput.model_fields if key in tag}
        items.append({"item_kind": "catalogue.update", "target_id": tag["id"],
                      "expected_revision": tag["revision"],
                      "payload": {"catalogue": "tags", "value": {**tag_value, "name": "Renamed revision tag"}}})
    else:
        items.append({"item_kind": "tag.members", "target_id": tag["id"],
                      "expected_revision": tag["revision"], "payload": {"add": [ref]}})
    return source, tag, {"items": items, "reason": REASON}


def snapshot(h, collection, source_id, tag_id):
    cls = m.Person if collection == "people" else m.Event
    with transaction(h.app.state.engine) as db:
        rows = [record(db.get(cls, source_id)), record(db.get(m.Tag, tag_id))]
        values = [{key: value for key, value in row.items() if key in s.CATALOGUE_SCHEMAS[kind].model_fields}
                  for kind, row in zip((collection, "tags"), rows)]
        members = sorted((row.kind, row.member_id) for row in db.scalars(select(m.TagMember).where(
            m.TagMember.tag_id == tag_id)))
        return values, members


@pytest.mark.parametrize("collection,operation", [("people", "add"), ("people", "rename"), ("events", "add")])
def test_bulk_undo_redo_repeated_updates_follow_transitive_tag_revisions(h, collection, operation):
    source, tag, body = mixed_change(h, collection, operation)
    before = snapshot(h, collection, source["id"], tag["id"])
    check = preview(h, "admin", body)
    assert check["counts"]["problem"] == 0, check
    assert snapshot(h, collection, source["id"], tag["id"]) == before
    applied = h.command("admin", "bulk.apply", body)
    after = snapshot(h, collection, source["id"], tag["id"])
    assert after != before
    inverse = applied["undo"]
    for expected in (before, after, before):
        unchanged = snapshot(h, collection, source["id"], tag["id"])
        check = preview(h, "admin", inverse["payload"])
        assert check["counts"]["problem"] == 0, check
        assert snapshot(h, collection, source["id"], tag["id"]) == unchanged
        result = h.command("admin", **inverse)
        assert snapshot(h, collection, source["id"], tag["id"]) == expected
        inverse = result["undo"]


@pytest.mark.parametrize("collection,changed", [("people", "source"), ("people", "tag"), ("events", "source")])
def test_bulk_transitive_revision_undo_still_refuses_external_changes_atomically(h, collection, changed):
    source, tag, body = mixed_change(h, collection, "add")
    applied = h.command("admin", "bulk.apply", body)
    catalogue, target = (collection, source) if changed == "source" else ("tags", tag)
    with transaction(h.app.state.engine) as db:
        cls = m.Person if catalogue == "people" else m.Event if catalogue == "events" else m.Tag
        current = record(db.get(cls, target["id"]))
    value = {key: deepcopy(current[key]) for key in s.CATALOGUE_SCHEMAS[catalogue].model_fields if key in current}
    value["title" if catalogue == "people" else "heading" if catalogue == "events" else "description"] = "External edit"
    h.configure(catalogue, value, current["id"], current["revision"], role="admin")
    before = snapshot(h, collection, source["id"], tag["id"])
    check = preview(h, "admin", applied["undo"]["payload"])
    assert any(issue["code"] == "STALE_REVISION" for row in check["items"] for issue in row["issues"])
    assert snapshot(h, collection, source["id"], tag["id"]) == before
    refused = h.command("admin", **applied["undo"], status=409)
    assert refused["error"]["code"] == "STALE_REVISION"
    assert refused["error"]["details"]["id"] == target["id"]
    assert snapshot(h, collection, source["id"], tag["id"]) == before
