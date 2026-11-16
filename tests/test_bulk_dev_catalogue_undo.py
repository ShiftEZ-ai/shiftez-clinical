"""Creation Undo uses checked lifecycle changes when occurrences and receipts are retained evidence."""
import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.serialization import record
from packages.contracts import schemas as s
from .test_bulk import REASON, preview
from .test_events import event, event_type, value


def content():
    return {"name": "Synthetic bulk announcement", "description": "Synthetic agenda",
            "starts_on": "2020-01-01", "audience": {"roles": ["staff"]}, "notify": False}


def creations(typ):
    return {"item_kind": "catalogue.create", "items": [
        {"payload": {"catalogue": "events", "value": value(typ)}},
        {"payload": {"catalogue": "department_content", "value": content()}}], "reason": REASON}


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_bulk_event_and_announcement_creation_undo_retains_occurrences_receipts_and_history(request, database):
    h = request.getfixturevalue(database)
    typ = event_type(h)
    payload = creations(typ)
    assert preview(h, "coordinator", payload)["counts"]["ready"] == 2
    result = h.command("coordinator", "bulk.apply", payload)
    event_id, content_id = [item["record"]["id"] for item in result["items"]]
    assert result["undoable"] is True
    for item in result["undo"]["payload"]["items"]:
        assert item["item_kind"] == "catalogue.update"
        schema = s.CATALOGUE_SCHEMAS[item["payload"]["catalogue"]]
        schema.model_validate(item["payload"]["value"])
    with transaction(h.app.state.engine, write=True) as db:
        occurrences = list(db.scalars(select(m.EventOccurrence).where(m.EventOccurrence.event_id == event_id)))
        occurrence_ids = {row.id for row in occurrences}
        receipts = list(db.scalars(select(m.AnnouncementReceipt).where(m.AnnouncementReceipt.content_id == content_id)))
        assert len(occurrences) == 1 and receipts
        receipts[0].read_at = m.now()
        db.flush()
        receipt_evidence = {row.id: record(row) for row in receipts}
        history_evidence = {row.id: record(row) for row in db.scalars(select(m.DefinitionHistory).where(
            m.DefinitionHistory.object_id.in_({event_id, content_id, *occurrence_ids})))}
    undone = h.command("coordinator", **result["undo"])
    assert undone["counts"]["changed"] == 2
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Event, event_id).cancelled is True
        assert db.get(m.DepartmentContent, content_id).active is False
        assert all(db.get(m.EventOccurrence, ident).cancelled for ident in occurrence_ids)
        assert {ident: record(db.get(m.AnnouncementReceipt, ident)) for ident in receipt_evidence} == receipt_evidence
        assert {ident: record(db.get(m.DefinitionHistory, ident)) for ident in history_evidence} == history_evidence
    h.command("coordinator", **undone["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Event, event_id).cancelled is False
        assert db.get(m.DepartmentContent, content_id).active is True
        assert {row.id for row in db.scalars(select(m.EventOccurrence).where(
            m.EventOccurrence.event_id == event_id))} == occurrence_ids


@pytest.mark.parametrize("changed_index", [0, 1])
def test_bulk_creation_lifecycle_undo_is_revision_bound_and_atomic(h, changed_index):
    result = h.command("coordinator", "bulk.apply", creations(event_type(h)))
    original = result["items"][changed_index]["record"]
    catalogue = ("events", "department_content")[changed_index]
    schema = s.CATALOGUE_SCHEMAS[catalogue]
    edited = {key: original[key] for key in schema.model_fields if key in original}
    edited["heading" if catalogue == "events" else "name"] = "Synthetic later edit"
    h.configure(catalogue, edited, original["id"], original["revision"], role="coordinator")
    with transaction(h.app.state.engine) as db:
        before = [record(db.get(cls, item["record"]["id"])) for cls, item in zip(
            (m.Event, m.DepartmentContent), result["items"])]
    refused = h.command("coordinator", **result["undo"], status=409)
    assert refused["error"]["details"]["id"] == original["id"]
    assert (edited.get("name") or original["id"]) in refused["error"]["message"]
    with transaction(h.app.state.engine) as db:
        assert [record(db.get(cls, item["record"]["id"])) for cls, item in zip(
            (m.Event, m.DepartmentContent), result["items"])] == before


def test_bulk_event_type_creation_undo_archives_it_without_removing_referenced_event_evidence(h):
    created = h.command("admin", "bulk.apply", {"item_kind": "catalogue.create", "items": [
        {"payload": {"catalogue": "event_types", "value": {"name": "Synthetic created event type",
             "starts_on": "2020-01-01"}}}], "reason": REASON})
    typ = created["items"][0]["record"]
    row = event(h, typ)
    with transaction(h.app.state.engine) as db:
        occurrence_ids = set(db.scalars(select(m.EventOccurrence.id).where(m.EventOccurrence.event_id == row["id"])))
    inverse = created["undo"]["payload"]["items"][0]
    assert inverse["item_kind"] == "catalogue.update" and inverse["payload"]["value"]["active"] is False
    archived = h.command("admin", **created["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.EventType, typ["id"]).active is False
        assert db.get(m.Event, row["id"]).cancelled is False
        assert set(db.scalars(select(m.EventOccurrence.id).where(
            m.EventOccurrence.event_id == row["id"]))) == occurrence_ids
    h.command("admin", **archived["undo"])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.EventType, typ["id"]).active is True
